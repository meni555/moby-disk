import functools
import hashlib
import json
import os
import shutil
import socket
import time

from flask import Flask, Response, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from . import applog, backup, docker_api, insights, notify, rules, scheduler, store, target_config, targets
from .targets.base import TargetError
from .targets.commvault_api import Commvault, CVError
from .config import DATA_DIR, config, settings

app = Flask(__name__)
app.secret_key = config.session_secret
applog.setup()
log = applog.get("api")
auth_log = applog.get("auth")

from .tasks import jobs  # noqa: E402

scheduler.start(jobs)


def _client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


@app.before_request
def _timer_start():
    g.t0 = time.time()


@app.after_request
def _access_log(resp):
    path = request.path
    if path.startswith("/static") or path in ("/", "/api/logs") or request.method == "OPTIONS":
        return resp   # Ausliefern der Oberfläche und das Log selbst würden das Log nur fluten
    ms = (time.time() - getattr(g, "t0", time.time())) * 1000
    if resp.status_code >= 500 and resp.status_code not in (502, 503):   # 502/503: Ziel bzw. Docker nicht erreichbar – erwartbar
        level = 40
    elif resp.status_code in (502, 503):
        level = 30
    elif resp.status_code >= 400 and resp.status_code != 401:
        level = 30
    else:
        level = 20 if request.method != "GET" else 10
    log.log(level, "%s %s → %d (%.0f ms) von %s", request.method, path, resp.status_code, ms, _client_ip())
    return resp


@app.errorhandler(Exception)
def _unhandled(exc):
    if isinstance(exc, HTTPException):
        return exc
    log.exception("Unbehandelter Fehler bei %s %s", request.method, request.path)
    return jsonify({"error": f"Interner Fehler ({type(exc).__name__}: {exc or 'ohne Meldung'}) – Details im Log."}), 500


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        password = config.app_password
        if password and not session.get("authenticated"):
            return jsonify({"error": "Nicht angemeldet"}), 401
        return view(*args, **kwargs)
    return wrapped


def _ui_version():
    """Kennung der ausgelieferten Oberfläche (Hash über Template und Static-Dateien) – für Cache-Busting und
    damit ein seit dem Update offener Browser-Tab merkt, dass er neu laden sollte."""
    digest = hashlib.sha256()
    base = os.path.dirname(__file__)
    for rel in ("templates/index.html", "static/app.js", "static/style.css"):
        try:
            with open(os.path.join(base, rel), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            pass
    return digest.hexdigest()[:12]


UI_VERSION = _ui_version()
applog.get("app").info("Moby Disk gestartet: Oberfläche %s, aktive Ziele: %s, Datenordner: %s, Backup-Ordner: %s, Log-Level: %s",
                       UI_VERSION, ", ".join(targets.enabled_ids()), DATA_DIR, config.backup_root, applog.level_name())


@app.get("/")
def index():
    resp = app.make_response(render_template("index.html", ui_version=UI_VERSION))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/api/login")
def login():
    password = config.app_password
    if not password:
        session["authenticated"] = True
        return jsonify({"ok": True, "auth": "disabled"})
    if request.json and request.json.get("password") == password:
        session["authenticated"] = True
        auth_log.info("Anmeldung erfolgreich von %s", _client_ip())
        return jsonify({"ok": True})
    auth_log.warning("Anmeldung fehlgeschlagen von %s", _client_ip())
    return jsonify({"error": "Falsches Passwort"}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/status")
@login_required
def status():
    docker_ok = True
    docker_error = None
    try:
        docker_api.client().ping()
    except docker_api.DockerError as exc:
        docker_ok = False
        docker_error = str(exc)
    cv = Commvault()
    disk = None
    try:
        usage = shutil.disk_usage(config.backup_root)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        pass
    return jsonify({
        "backup_disk": disk,
        "host": socket.gethostname(),
        "docker_ok": docker_ok,
        "docker_error": docker_error,
        "cv_configured": cv.configured,
        "cv_url": config.cv_url or "",
        "backup_root": config.backup_root,
        "staging_root": config.staging_root,
        "volumes_root": config.volumes_root,
        "host_shares": docker_api.host_share_paths() if docker_ok else [],
        "targets": targets.describe_all(),
        "ui_version": UI_VERSION,
        "identity": config.effective_cv_identity(),
        "auth_enabled": bool(config.app_password),
        "password_is_default": config.app_password == "changeme",
    })


@app.get("/api/containers")
@login_required
def containers():
    try:
        items = docker_api.list_containers()
    except docker_api.DockerError as exc:
        return jsonify({"error": str(exc)}), 503
    insights.annotate_containers(items, scheduler.list_schedules())
    return jsonify(items)


# ───────── Stacks (Compose-Projekte) ─────────
def _stack_sources():
    """Quellen, aus denen ein Stack zurückgeholt werden kann: lokal und alle aktiven Ziele mit Restore."""
    return [("local", "Lokal")] + [(t.id, t.label) for t in targets.all_targets() if "fetch" in t.capabilities]


def _stack_run_info(run, current):
    """Einen Stack-Lauf für die Oberfläche aufbereiten: Zeitpunkt, Mitglieder, Verfügbarkeit je Quelle."""
    members = run["members"]
    first = backup._parse_ts(members[0]["folder"]) if members else None
    local = {m["container"]: backup.read_manifest(m["container"], m["folder"]) is not None for m in members}
    sources = {"local": all(local.values())}
    for target_id, _label in _stack_sources()[1:]:
        sources[target_id] = all(m["folder"] in store.verified_folders(m["container"], target_id) for m in members)
    names = [m["container"] for m in members]
    return {"run_id": run["run_id"], "time": first.timestamp() if first else run["created"],
            "members": [{"container": m["container"], "folder": m["folder"], "local": local[m["container"]],
                         "exists": m["container"] in current} for m in members],
            "sources": sources, "added_since": sorted(set(current) - set(names))}


@app.get("/api/stacks")
@login_required
def stacks():
    """Alle Compose-Stacks: laufende Container und der letzte gemeinsame Backup-Lauf."""
    try:
        items = docker_api.list_containers()
    except docker_api.DockerError as exc:
        return jsonify({"error": str(exc)}), 503
    live = {}
    for c in items:
        if c.get("stack") and not c.get("clone_of"):
            live.setdefault(c["stack"], []).append(c)
    out = []
    for name in sorted(set(live) | set(store.stack_names())):
        members = live.get(name, [])
        current = [c["name"] for c in members]
        runs = store.stack_runs(name, limit=1)
        last = _stack_run_info(runs[0], current) if runs else None
        out.append({"name": name, "present": bool(members),
                    "containers": [{"name": c["name"], "running": bool(c.get("running")), "state": c.get("state"),
                                    "image": c.get("image"), "is_self": bool(c.get("is_self")),
                                    "has_data": bool(c.get("has_data"))} for c in members],
                    "running": sum(1 for c in members if c.get("running")), "last_run": last,
                    "sources": [{"id": i, "label": l} for i, l in _stack_sources()]})
    return jsonify(out)


@app.get("/api/stacks/<name>/runs")
@login_required
def stack_runs(name):
    """Backup-Läufe eines Stacks (neueste zuerst) mit den Quellen, aus denen sie zurückgeholt werden können."""
    try:
        current = [c["name"] for c in docker_api.list_containers() if c.get("stack") == name and not c.get("clone_of")]
    except docker_api.DockerError:
        current = []
    return jsonify({"runs": [_stack_run_info(r, current) for r in store.stack_runs(name)],
                    "sources": [{"id": i, "label": l} for i, l in _stack_sources()]})


@app.get("/api/containers/<name>/versions")
@login_required
def versions(name):
    """Lokale Kopien und die Stände in allen Backup-Zielen. 'commvault'/'cv_error' bleiben für die bisherige
    Oberfläche erhalten; 'targets' enthält alle Ziele."""
    result = {"local": backup.list_local_backups(name), "commvault": [], "cv_error": None, "targets": {}}
    for target in targets.all_targets():
        entry = {"label": target.label, "folders": [], "error": None}
        result["targets"][target.id] = entry
        if "versions" not in target.capabilities:
            continue
        if not target.ready():
            entry["error"] = f"{target.label} nicht konfiguriert oder nicht fertig eingerichtet. Bitte Einstellungen prüfen."
        else:
            meta = target.list_folders(name)
            if meta is None:
                entry["error"] = f"{target.label} nicht erreichbar."
            else:
                # Jeder Lauf legt einen eigenen Zeitstempel-Ordner an: alle Unterordner (auch lokal
                # bereits gelöschte) sind einzeln wiederherstellbare Sicherungsstände.
                remembered = {e["folder"]: (e["info"] or {}).get("size_bytes") for e in store.entries(name, target.id)}
                for folder, m in meta.items():
                    store.mark_verified(target.id, name, folder, m.get("time"), m.get("ref"),
                                        info={"size_bytes": m["size"]} if m.get("size") else None)
                    entry["folders"].append({
                        # Commvault meldet für lokal gelöschte Ordner 0 Byte – dann gilt die beim Sichern gemerkte Größe
                        "folder": folder, "backup_time": m.get("time"), "size": m.get("size") or remembered.get(folder) or 0,
                        "backup_job_id": m.get("ref"), "deleted_locally": bool(m.get("deleted_locally"))})
                entry["folders"].sort(key=lambda r: r.get("backup_time") or 0, reverse=True)
        if target.id == "commvault":
            result["commvault"], result["cv_error"] = entry["folders"], entry["error"]
    if "commvault" not in result["targets"]:
        result["cv_error"] = "Kein Commvault-Ziel aktiv (Umgebungsvariable TARGETS)."
    return jsonify(result)


@app.post("/api/backup")
@login_required
def create_backup():
    body = request.json or {}
    names = body.get("containers")
    if isinstance(names, list) and names:
        names = [str(n).strip() for n in names if str(n).strip()]
    else:
        single = (body.get("container") or "").strip()
        names = [single] if single else []
    if not names:
        return jsonify({"error": "Container fehlt"}), 400
    try:
        existing = {c["name"]: c for c in docker_api.list_containers()}
    except docker_api.DockerError as exc:
        return jsonify({"error": str(exc)}), 503
    missing = [n for n in names if n not in existing]
    if missing:
        return jsonify({"error": f"Container nicht gefunden: {', '.join(missing)}"}), 404
    if body.get("whole_stacks"):   # alle Container der betroffenen Compose-Stacks ergänzen
        stacks = {existing[n]["stack"] for n in names if existing[n].get("stack")}
        names += [c["name"] for c in existing.values() if c.get("stack") in stacks and c["name"] not in names
                  and not c.get("clone_of")]
    fmt = body.get("format") or "tar"
    if fmt not in backup.FORMATS:
        return jsonify({"error": "Format muss 'tar' oder 'tar.gz' sein."}), 400
    image_mode = body.get("image_mode") or ("own" if body.get("include_image") else "none")
    if image_mode not in backup.IMAGE_MODES:
        return jsonify({"error": "Image-Modus muss 'none', 'own' oder 'all' sein."}), 400
    try:
        target_runs = targets.clean_runs(body.get("targets"), int(body.get("cv_backup_level") or 0) or None)
    except (TargetError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    options = dict(
        mode=body.get("mode", "live"),
        image_mode=image_mode,
        stack_mode=bool(body.get("stack_mode", True)),
        retention=int(body.get("retention") or 0),
        retention_days=int(body.get("retention_days") or 0),
        target_runs=target_runs,
        fmt=fmt,
        require_cv=bool(body.get("require_cv")),
    )
    job = jobs.submit_backup_batch(names, **options)
    return jsonify(job)


def _stack_members(name, folder, prefer_local=True, target="commvault"):
    """Alle Sicherungen desselben Backup-Laufs aus demselben Stack (für den Stack-Restore)."""
    manifest = backup.read_manifest(name, folder) or {}
    run = store.run_of(name, folder) or {}
    run_id = manifest.get("run_id") or run.get("run_id")
    stack = manifest.get("stack") or run.get("stack")
    if not run_id or not stack:
        return stack, []
    members = []
    for m in store.run_members(run_id):
        if m["stack"] != stack:
            continue
        local = backup.read_manifest(m["container"], m["folder"]) is not None
        members.append({"container": m["container"], "folder": m["folder"], "local": local,
                        "in_cv": m["folder"] in store.verified_folders(m["container"], target),
                        "source": "local" if (local and prefer_local) else target})
    return stack, members


@app.get("/api/containers/<name>/backup-info")
@login_required
def backup_info(name):
    """Was steckt in einer Sicherung (Dumps, Stack, Ports) – für den Restore-Dialog."""
    folder = (request.args.get("folder") or "").strip()
    if not folder:
        local = backup.list_local_backups(name)
        folder = local[0]["ts"] if local else ""
    manifest = backup.read_manifest(name, folder) if folder else None
    run = store.run_of(name, folder) if folder else None
    info = {"folder": folder, "local": manifest is not None, "dumps": [], "mode": None, "stack": None,
            "members": [], "image_saved": None, "ports": [], "cv_direct": []}
    if manifest:
        info.update(dumps=[d["type"] for d in manifest.get("dumps") or [] if d.get("file")], mode=manifest.get("mode"),
                    image_saved=bool(manifest.get("image_data")), image=manifest.get("image"),
                    cv_direct=manifest.get("cv_direct") or [])
        try:
            with open(os.path.join(config.backup_root, backup.safe_name(name), folder, "container.json"),
                      encoding="utf-8") as fh:
                snap = json.load(fh)
            info["ports"] = list((snap.get("ports") or {}).keys()) or list(snap.get("exposed_ports") or [])
        except (OSError, ValueError):
            pass
    elif run:
        meta = run.get("meta") or {}
        info.update(dumps=meta.get("dumps") or [], mode=meta.get("mode"), image_saved=meta.get("image_saved"))
    try:
        snap_now = docker_api.inspect_snapshot(name)
        if not info["ports"]:
            info["ports"] = list((snap_now.get("ports") or {}).keys()) or list(snap_now.get("exposed_ports") or [])
        if not manifest:   # Sicherung nur in Commvault: Direkt-Pfade nach den aktuellen Regeln anzeigen
            for d in rules.plan_mounts(name, snap_now["mounts"], snap_now.get("labels")):
                if d["action"] == "skip" and d["reason"] == "cv_direct":
                    info["cv_direct"].append({"destination": d["destination"], "host_path": d["source"], "rel": "."})
                for sub in d.get("direct_sub") or []:
                    info["cv_direct"].append({"destination": d["destination"], "host_path": sub["host"], "rel": sub["rel"]})
    except docker_api.DockerError:
        pass
    if folder:
        kind = request.args.get("kind") or "local"
        info["stack"], members = _stack_members(name, folder, prefer_local=kind == "local",
                                                target=kind if kind != "local" else "commvault")
        info["members"] = members if len(members) > 1 else []
    return jsonify(info)


@app.post("/api/restore")
@login_required
def restore():
    body = request.json or {}
    container_name = (body.get("container") or "").strip()
    if not container_name:
        return jsonify({"error": "Container fehlt"}), 400
    target = body.get("target") or "original"
    clone = None
    if target == "clone":
        c = body.get("clone") or {}
        clone = {"name": (c.get("name") or f"{container_name}-clone").strip(), "network": c.get("network") or "none",
                 "host_port": int(c["host_port"]) if str(c.get("host_port") or "").isdigit() else None,
                 "start": bool(c.get("start", True)), "attach_skipped": bool(c.get("attach_skipped", True))}
        if not backup.CLONE_NAME_RE.match(clone["name"]):
            return jsonify({"error": "Ungültiger Klon-Name (Buchstaben, Ziffern, . _ -)."}), 400
        try:
            docker_api.get_container(clone["name"])
            return jsonify({"error": f"Es gibt bereits einen Container '{clone['name']}'."}), 409
        except docker_api.DockerError:
            pass
    source = body.get("source") or ("commvault" if body.get("use_cv", True) else "local")
    if source != "local":
        try:
            targets.get(source)
        except TargetError as exc:
            return jsonify({"error": str(exc)}), 400
    members = None
    if target == "original" and body.get("whole_stack") and body.get("folder"):
        _stack, members = _stack_members(container_name, body["folder"], prefer_local=source == "local", target=source)
        if len(members) < 2:
            return jsonify({"error": "Zu dieser Sicherung gibt es keinen Stack-Lauf mit weiteren Containern."}), 400
        missing = [m["container"] for m in members if m["source"] != "local" and not m["in_cv"]]
        if missing:
            return jsonify({"error": "Nicht lokal und im Ziel nicht bestätigt: " + ", ".join(missing)}), 400
    job = jobs.submit_restore(
        container_name,
        version=body.get("version"),
        folder=body.get("folder"),
        restore_bind_mounts=bool(body.get("restore_bind_mounts", True)),
        restart_container=bool(body.get("restart_container", True)),
        recreate_if_missing=bool(body.get("recreate_if_missing", True)),
        source=source,
        use_dump=bool(body.get("use_dump")),
        members=members,
        target=target,
        clone=clone,
    )
    return jsonify(job)


@app.delete("/api/clones/<name>")
@login_required
def clone_delete(name):
    try:
        return jsonify(backup.remove_clone(name))
    except (backup.BackupError, docker_api.DockerError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/clones/<name>/promote")
@login_required
def clone_promote(name):
    """Klon übernehmen: denselben Sicherungsstand ins Original zurückspielen, danach den Klon entfernen."""
    try:
        labels = (docker_api.get_container(name).attrs.get("Config") or {}).get("Labels") or {}
    except docker_api.DockerError as exc:
        return jsonify({"error": str(exc)}), 404
    if docker_api.own_label(labels, "clone") != "1":
        return jsonify({"error": f"'{name}' ist kein vom Helper angelegter Klon."}), 400
    try:
        source = json.loads(docker_api.own_label(labels, "clone-source") or "{}")
    except ValueError:
        source = {}
    original = docker_api.own_label(labels, "clone-of")
    if not original or not source.get("folder"):
        return jsonify({"error": "Herkunft des Klons ist unbekannt."}), 400
    body = request.json or {}
    job = jobs.submit_restore(original, version=source.get("version"), folder=source["folder"],
                              source=source.get("target") or ("commvault" if source.get("kind") == "cv" else "local"), use_dump=bool(body.get("use_dump")),
                              remove_clone=name)
    return jsonify(job)


@app.get("/api/schedules")
@login_required
def schedules_list():
    return jsonify(scheduler.list_schedules())


@app.post("/api/schedules")
@login_required
def schedules_create():
    try:
        return jsonify(scheduler.create_schedule(request.json or {}))
    except scheduler.ScheduleError as exc:
        return jsonify({"error": str(exc)}), 400


@app.put("/api/schedules/<sched_id>")
@login_required
def schedules_update(sched_id):
    try:
        return jsonify(scheduler.update_schedule(sched_id, request.json or {}))
    except scheduler.ScheduleError as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/schedules/<sched_id>")
@login_required
def schedules_delete(sched_id):
    try:
        scheduler.delete_schedule(sched_id)
    except scheduler.ScheduleError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True})


@app.post("/api/schedules/<sched_id>/run")
@login_required
def schedules_run(sched_id):
    try:
        job = scheduler.run_schedule(sched_id, jobs, manual=True)
    except scheduler.ScheduleError as exc:
        return jsonify({"error": str(exc)}), 404
    if job is None:
        return jsonify({"error": "Nicht gestartet: Lauf noch aktiv oder keine passenden Container."}), 409
    return jsonify(job)


@app.get("/api/containers/<name>/plan")
@login_required
def container_plan(name):
    try:
        snap = docker_api.inspect_snapshot(name)
    except docker_api.DockerError as exc:
        return jsonify({"error": str(exc)}), 404
    self_c = docker_api.is_self(name)
    if request.args.get("sizes") == "1":
        plan, limit = rules.plan_with_sizes(name, snap["mounts"], snap.get("labels"), is_self=self_c)
    else:
        plan = rules.plan_mounts(name, snap["mounts"], snap.get("labels"), use_docker=True, is_self=self_c)
        limit = rules.size_limit_bytes()
    rule_set = rules.get_rules()
    settings_now = rules.container_settings(name, snap.get("labels"), snap.get("image"), snap.get("env"), rule_set)
    origin = docker_api.image_origin(snap.get("image_id") or snap.get("image"), rule_set.get("own_registries") or ())
    info = docker_api.compose_info(snap.get("labels"))
    return jsonify({"mounts": plan, "limit": limit, "is_self": self_c,
                    "exclude_globs": rules.exclude_globs(name, snap.get("labels")),
                    "rules": rule_set["containers"].get(name, {}), "settings": settings_now,
                    "image_origin": origin, "stack": info["stack"], "service": info["service"],
                    "depends_on": info["depends_on"]})


@app.get("/api/rules")
@login_required
def rules_get():
    return jsonify(rules.get_rules())


@app.put("/api/rules")
@login_required
def rules_put():
    body = request.json or {}
    before = rules.direct_paths()
    try:
        new = rules.update_rules(body)
    except rules.RulesError as exc:
        return jsonify({"error": str(exc)}), 400
    result = dict(new)
    if "cv_direct_paths" in body and (rules.direct_paths(new) != before or body.get("cv_push")):
        try:   # geänderte Direkt-Pfade gleich an den Commvault-Subclient übertragen
            result["cv_content"] = targets.get("commvault").paths_sync(new)
        except TargetError as exc:
            result["cv_content_error"] = str(exc)
    return jsonify(result)


@app.get("/api/cv/content")
@login_required
def cv_content_get():
    try:
        return jsonify(targets.get("commvault").paths_status())
    except TargetError as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/cv/content/sync")
@login_required
def cv_content_sync():
    try:
        return jsonify(targets.get("commvault").paths_sync())
    except TargetError as exc:
        return jsonify({"error": str(exc)}), 502


@app.put("/api/rules/containers/<name>")
@login_required
def rules_container_put(name):
    try:
        return jsonify(rules.update_container_rules(name, request.json or {}))
    except rules.RulesError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/backups")
@login_required
def all_backups():
    """Lokale Sicherungen plus solche, die nur noch in Backup-Zielen liegen (lokal gelöscht)."""
    rows = backup.list_all_local_backups()
    entries = {}
    for e in store.entries():
        entries.setdefault((e["container"], e["folder"]), []).append(e)
    for r in rows:
        found = entries.get((r["container"], r["folder"])) or []
        r["local"] = True
        r["targets"] = sorted(e["target"] for e in found)
        r["in_commvault"] = any(e["target"] == "commvault" for e in found)
        r["in_target"] = bool(found)
        r["cv_job"] = next((e["ref"] for e in found if e["target"] == "commvault"), None)
    local_keys = {(r["container"], r["folder"]) for r in rows}
    only = []
    for key, found in entries.items():
        if key in local_keys:
            continue
        e = found[0]
        info, ts = e["info"], backup._parse_ts(e["folder"])
        only.append({"container": e["container"], "folder": e["folder"], "local": False, "in_target": True,
                     "in_commvault": any(x["target"] == "commvault" for x in found),
                     "targets": sorted(x["target"] for x in found),
                     "size_bytes": int(info.get("size_bytes") or 0), "image": info.get("image"),
                     "mode": info.get("mode"), "skipped": info.get("skipped", 0), "stack": info.get("stack"),
                     "dumps": info.get("dumps") or [],
                     "cv_job": next((x["ref"] for x in found if x["target"] == "commvault"), None),
                     "local_deleted": e["local_deleted"], "created": ts.timestamp() if ts else e["verified"]})
    return jsonify({"backups": rows + only, "total_bytes": sum(r["size_bytes"] for r in rows),
                    "cv_only": len(only), "cv_sync": store.kv_get("sync_commvault"),
                    "syncs": {t.id: store.kv_get(f"sync_{t.id}") for t in targets.all_targets()}})


@app.get("/api/logs")
@login_required
def logs():
    """Einträge des zentralen Logs (Ringpuffer). since = letzte bekannte Id, damit die Oberfläche nur Neues holt."""
    a = request.args
    try:
        since, limit = int(a.get("since") or 0), min(int(a.get("limit") or 500), 3000)
    except ValueError:
        return jsonify({"error": "since und limit müssen Zahlen sein."}), 400
    return jsonify({"entries": applog.query(a.get("level") or "INFO", a.get("logger") or "", a.get("q") or "", since, limit),
                    "last_id": applog.last_id(), "level": applog.level_name(), "file": applog.file_info()})


@app.put("/api/logs/config")
@login_required
def logs_config():
    """Mit welcher Ausführlichkeit protokolliert wird (DEBUG, INFO, WARNING, ERROR) – gilt sofort und bleibt gespeichert."""
    try:
        applog.set_level((request.get_json(silent=True) or {}).get("level", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "level": applog.level_name()})


@app.get("/api/logs/download")
@login_required
def logs_download():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(applog.as_text(), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="mobydisk-log-{stamp}.txt"'})


@app.get("/api/targets")
@login_required
def targets_list():
    return jsonify(targets.describe_all())


def _active_jobs():
    return [j for j in jobs.list_jobs() if j["status"] in ("queued", "running", "paused")]


@app.put("/api/targets/<target_id>/config")
@login_required
def target_config_save(target_id):
    """Einstellungen eines Ziels aus dem Webinterface speichern (wirkt sofort, ohne Neustart)."""
    if _active_jobs():
        return jsonify({"error": "Solange ein Auftrag läuft, lassen sich die Ziele nicht ändern."}), 409
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    try:
        targets.get(target_id)
        target_config.save(target_id, body.get("values") or {}, enabled if isinstance(enabled, bool) else None,
                           targets.enabled_ids())
    except (TargetError, target_config.ConfigError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "target": next(t for t in targets.describe_all() if t["id"] == target_id)})


@app.get("/api/targets/<target_id>/choices/<env>")
@login_required
def target_choices(target_id, env):
    """Auswahlliste für ein Feld, das vom Ziel abgefragt wird (z. B. die Veeam-Jobs)."""
    try:
        return jsonify({"choices": targets.get(target_id).choices(env)})
    except TargetError as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/targets/<target_id>/test")
@login_required
def target_test(target_id):
    try:
        return jsonify({"ok": True, **targets.get(target_id).test()})
    except TargetError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:   # unerwartet: lesbar melden statt nackter „HTTP 500“
        app.logger.exception("Verbindungstest %s", target_id)
        return jsonify({"error": f"Unerwarteter Fehler im Verbindungstest ({exc.__class__.__name__}: {exc})"}), 500


@app.post("/api/targets/<target_id>/sync")
@login_required
def target_sync(target_id):
    """Alle Container-Ordner im Ziel nachschlagen und die Vermerke angleichen (Auftrag)."""
    try:
        target = targets.get(target_id)
    except TargetError as exc:
        return jsonify({"error": str(exc)}), 404
    if any(j["kind"] == "cvsync" and j["status"] in ("queued", "running", "paused") and (j.get("params") or {}).get("target") == target_id
           for j in jobs.list_jobs()):
        return jsonify({"error": f"Der Abgleich mit {target.label} läuft bereits."}), 409
    return jsonify(jobs.submit_target_sync(target_id))


@app.post("/api/backups/cv-sync")
@login_required
def backups_cv_sync():
    return target_sync("commvault")


@app.post("/api/backups/delete")
@login_required
def delete_backups():
    items = (request.get_json(silent=True) or {}).get("items") or []
    if not items:
        return jsonify({"error": "Keine Sicherungen ausgewählt."}), 400
    if any(j["kind"] in ("backup", "restore") and j["status"] in ("queued", "running", "paused") for j in jobs.list_jobs()):
        return jsonify({"error": "Es läuft gerade ein Backup/Restore – bitte danach löschen."}), 409
    before = {(r["container"], r["folder"]): r for r in backup.list_all_local_backups()}
    deleted, freed, errors = backup.delete_local_backups(items)
    for it in items:   # der Commvault-Vermerk bleibt – die Sicherung liegt dort ja weiterhin
        key = (str(it.get("container", "")), str(it.get("folder", "")))
        row = before.get(key)
        if row and backup.read_manifest(*key) is None:
            store.mark_local_deleted(*key, info={k: row.get(k) for k in
                                                 ("size_bytes", "image", "mode", "skipped", "stack", "dumps")})
    return jsonify({"deleted": deleted, "bytes": freed, "errors": errors})


@app.get("/api/cleanup/incomplete")
@login_required
def cleanup_preview():
    items = backup.incomplete_backups()
    return jsonify({"count": len(items), "bytes": sum(i["size"] for i in items),
                    "items": [{"container": i["container"], "folder": i["folder"], "size": i["size"]} for i in items]})


@app.post("/api/cleanup/incomplete")
@login_required
def cleanup_run():
    if any(j["kind"] == "backup" and j["status"] in ("queued", "running", "paused") for j in jobs.list_jobs()):
        return jsonify({"error": "Es läuft gerade ein Backup – bitte danach aufräumen."}), 409
    count, freed = backup.cleanup_incomplete()
    return jsonify({"count": count, "bytes": freed})


@app.get("/api/forecast")
@login_required
def forecast_view():
    def num(key, cast, default):
        try:
            return cast(request.args.get(key)) if request.args.get(key) not in (None, "") else default
        except ValueError:
            return default
    keep = num("keep", int, None)
    runs_mult = max(0.0, min(num("runs_mult", float, 1.0), 50.0))
    items = docker_api.list_containers()
    return jsonify(insights.forecast(items, scheduler.list_schedules(),
                                     keep_override=keep if keep and keep > 0 else None, runs_mult=runs_mult))


@app.get("/api/notify")
@login_required
def notify_get():
    return jsonify(notify.public_config())


@app.put("/api/notify")
@login_required
def notify_put():
    try:
        return jsonify(notify.update_config(request.json or {}))
    except notify.NotifyError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/notify/test")
@login_required
def notify_test():
    try:
        results = notify.test((request.json or {}).get("channel"))
    except notify.NotifyError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify([{"channel": c, "ok": ok, "error": err} for c, ok, err in results])


@app.get("/api/jobs")
@login_required
def list_jobs():
    return jsonify(jobs.list_jobs())


@app.post("/api/jobs/<job_id>/<action>")
@login_required
def job_control(job_id, action):
    """Auftrag pausieren, fortsetzen oder abbrechen (wirkt an den nächsten sicheren Stellen)."""
    if action not in ("pause", "resume", "cancel"):
        return jsonify({"error": "Unbekannte Aktion."}), 404
    try:
        return jsonify(jobs.control(job_id, action))
    except backup.BackupError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/jobs/<job_id>")
@login_required
def get_job(job_id):
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"error": "Job nicht gefunden"}), 404
    return jsonify(job)


@app.get("/api/settings")
@login_required
def get_settings():
    identity = config.effective_cv_identity()
    return jsonify({
        "cv_url": config.cv_url,
        "cv_webservice_path": config.cv_webservice_path,
        "cv_username": config.cv_username,
        "cv_password_set": bool(config.cv_password),
        "cv_verify_tls": config.cv_verify_tls,
        "identity": identity,
        "backup_root": config.backup_root,
        "staging_root": config.staging_root,
        "settings": settings.as_dict(),
    })


@app.post("/api/settings")
@login_required
def update_settings():
    body = request.json or {}
    allowed = {
        "cv_client_id": int, "cv_client_name": str,
        "cv_subclient_id": int, "cv_subclient_name": str,
        "cv_app_type_id": int, "cv_instance_id": int, "cv_instance_name": str,
        "cv_backup_set_id": int, "cv_backup_set_name": str,
    }
    updates = {}
    for key, cast in allowed.items():
        if key in body and body[key] not in (None, ""):
            try:
                updates[key] = cast(body[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"Ungültiger Wert für {key}"}), 400
        elif key in body:
            updates[key] = None
    settings.update(updates)
    return jsonify({"ok": True, "identity": config.effective_cv_identity()})


@app.post("/api/settings/test-cv")
@login_required
def test_cv():
    cv = Commvault()
    if not cv.configured:
        return jsonify({"error": "CV_URL, CV_USERNAME oder CV_PASSWORD fehlt in der .env"}), 400
    try:
        info = cv.test_connection()
    except CVError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, **info})


@app.get("/api/settings/clients")
@login_required
def list_clients():
    cv = Commvault()
    try:
        clients = cv.list_clients()
    except CVError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(clients)


@app.get("/api/settings/subclients")
@login_required
def list_subclients():
    client_id = request.args.get("client_id")
    if not client_id:
        return jsonify({"error": "client_id fehlt"}), 400
    cv = Commvault()
    try:
        subclients = cv.list_subclients(int(client_id))
    except CVError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(subclients)


@app.post("/api/settings/auto-detect")
@login_required
def auto_detect():
    cv = Commvault()
    try:
        match = cv.find_client_id(socket.gethostname())
    except CVError as exc:
        return jsonify({"error": str(exc)}), 502
    if not match:
        return jsonify({"error": f"Kein Commvault-Client mit Hostname '{socket.gethostname()}' gefunden."}), 404
    return jsonify(match)
