"""Geplante Backups: Zeitpläne (wochentags + Uhrzeit), lokale Aufbewahrung und anschließend
ein Commvault-Backup des Subclients. Die Zeitpläne liegen in DATA_DIR/schedules.json."""
import datetime
import json
import os
import re
import threading
import time
import uuid

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import applog, docker_api, insights, notify, targets
from .targets.base import TargetError
from .config import DATA_DIR

SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")
CHECK_INTERVAL = 15            # Sekunden zwischen den Prüfungen
CATCH_UP_WINDOW = 4 * 3600     # verpasste Läufe (z. B. Helper war aus) bis zu 4 h nachholen
_lock = threading.RLock()
_started = False


class ScheduleError(Exception):
    pass


def _tz():
    name = os.environ.get("TZ") or "Europe/Berlin"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.datetime.now().astimezone().tzinfo


def _now():
    return datetime.datetime.now(_tz())


# ───────── Speicher ─────────
def _load():
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []
    for sched in data:   # Zeitpläne der Vorgängerversion kennen nur die Commvault-Stufe
        if isinstance(sched, dict) and sched.get("targets") is None:
            try:
                sched["targets"] = targets.clean_runs(None, sched.get("cv_backup_level"))
            except (TargetError, ValueError):
                sched["targets"] = []
    return data


def _save(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SCHEDULES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, SCHEDULES_FILE)


# ───────── Zeitberechnung ─────────
def _slot(day, hhmm, tz):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz)


def latest_slot(sched, now=None):
    """Letzter geplanter Zeitpunkt <= jetzt."""
    now = now or _now()
    tz = now.tzinfo
    for back in range(0, 9):
        day = (now - datetime.timedelta(days=back)).date()
        if day.weekday() in sched["days"]:
            slot = _slot(day, sched["time"], tz)
            if slot <= now:
                return slot
    return None


def next_slot(sched, now=None):
    now = now or _now()
    tz = now.tzinfo
    for ahead in range(0, 9):
        day = (now + datetime.timedelta(days=ahead)).date()
        if day.weekday() in sched["days"]:
            slot = _slot(day, sched["time"], tz)
            if slot > now:
                return slot
    return None


# ───────── Validierung ─────────
def _clean(body, existing=None):
    existing = existing or {}
    name = str(body.get("name", existing.get("name", ""))).strip()
    if not name:
        raise ScheduleError("Name fehlt.")
    scope = body.get("scope", existing.get("scope", "selected"))
    if scope not in ("selected", "all_with_data"):
        raise ScheduleError("Ungültiger Geltungsbereich.")
    containers = body.get("containers", existing.get("containers", []))
    containers = [str(c).strip() for c in (containers or []) if str(c).strip()]
    if scope == "selected" and not containers:
        raise ScheduleError("Bitte mindestens einen Container wählen.")
    days = body.get("days", existing.get("days", [0, 1, 2, 3, 4, 5, 6]))
    try:
        days = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        raise ScheduleError("Ungültige Wochentage.")
    if not days or any(d < 0 or d > 6 for d in days):
        raise ScheduleError("Bitte mindestens einen Wochentag wählen.")
    when = str(body.get("time", existing.get("time", "02:00"))).strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", when):
        raise ScheduleError("Uhrzeit muss im Format HH:MM angegeben werden.")
    mode = body.get("mode", existing.get("mode", "stop"))
    if mode not in ("live", "stop"):
        raise ScheduleError("Modus muss 'live' oder 'stop' sein.")

    def num(key, default=0):
        try:
            value = int(body.get(key, existing.get(key, default)) or 0)
        except (TypeError, ValueError):
            raise ScheduleError(f"Ungültiger Wert für {key}.")
        if value < 0 or value > 3650:
            raise ScheduleError(f"Ungültiger Wert für {key}.")
        return value

    fmt = body.get("format", existing.get("format", "tar.gz"))
    if fmt not in ("tar", "tar.gz"):
        raise ScheduleError("Format muss 'tar' oder 'tar.gz' sein.")

    image_mode = body.get("image_mode", existing.get("image_mode"))
    if image_mode is None:   # ältere Zeitpläne: Häkchen 'Image mitsichern' → nur selbst gebaute
        image_mode = "own" if body.get("include_image", existing.get("include_image", True)) else "none"
    if image_mode not in ("none", "own", "all"):
        raise ScheduleError("Image-Modus muss 'none', 'own' oder 'all' sein.")

    legacy = body.get("cv_backup_level", existing.get("cv_backup_level"))
    try:
        legacy = int(legacy) if legacy not in (None, "", 0, "0") else None
        runs = targets.clean_runs(body["targets"] if "targets" in body else existing.get("targets"), legacy)
    except (TargetError, ValueError) as exc:
        raise ScheduleError(str(exc))
    level = next((r.get("level") for r in runs if r["id"] == "commvault"), None)   # für ältere Ansichten

    return {
        "name": name,
        "enabled": bool(body.get("enabled", existing.get("enabled", True))),
        "scope": scope,
        "containers": containers if scope == "selected" else [],
        "days": days,
        "time": when,
        "mode": mode,
        "image_mode": image_mode,
        "include_image": image_mode != "none",
        "stack_mode": bool(body.get("stack_mode", existing.get("stack_mode", True))),
        "retention": num("retention", 7),
        "retention_days": num("retention_days", 0),
        "cv_backup_level": level,
        "targets": runs,
        "format": fmt,
        "require_cv": bool(body.get("require_cv", existing.get("require_cv", False))),
    }


# ───────── CRUD ─────────
def _public(sched, now=None):
    now = now or _now()
    out = dict(sched)
    nxt = next_slot(sched, now) if sched.get("enabled") else None
    out["next_run"] = nxt.timestamp() if nxt else None
    return out


def list_schedules():
    with _lock:
        now = _now()
        return [_public(s, now) for s in _load()]


def create_schedule(body):
    with _lock:
        data = _clean(body)
        now = _now()
        sched = {"id": uuid.uuid4().hex[:10], "created": time.time(), "last_run": None,
                 "last_status": None, "last_message": "", "last_job_id": None, **data}
        slot = latest_slot(sched, now)
        sched["last_slot"] = slot.isoformat() if slot else None   # neuer Plan läuft nicht sofort los
        items = _load()
        items.append(sched)
        _save(items)
        return _public(sched, now)


def update_schedule(sched_id, body):
    with _lock:
        items = _load()
        for index, sched in enumerate(items):
            if sched["id"] == sched_id:
                merged = {**sched, **_clean(body, sched)}
                slot = latest_slot(merged)
                merged["last_slot"] = slot.isoformat() if slot else None   # Änderungen lösen keinen Nachhollauf aus
                items[index] = merged
                _save(items)
                return _public(merged)
    raise ScheduleError("Zeitplan nicht gefunden.")


def delete_schedule(sched_id):
    with _lock:
        items = _load()
        kept = [s for s in items if s["id"] != sched_id]
        if len(kept) == len(items):
            raise ScheduleError("Zeitplan nicht gefunden.")
        _save(kept)


def _patch(sched_id, **fields):
    with _lock:
        items = _load()
        for sched in items:
            if sched["id"] == sched_id:
                sched.update(fields)
                _save(items)
                return


# ───────── Ausführung ─────────
def _resolve_targets(sched):
    containers = docker_api.list_containers()
    existing = {c["name"]: c for c in containers}
    if sched["scope"] == "all_with_data":
        return [c["name"] for c in containers if docker_api.backup_candidate(c)], []
    found = [n for n in sched["containers"] if n in existing]
    missing = [n for n in sched["containers"] if n not in existing]
    return found, missing


def _is_active_for(jobs, sched_id):
    return any(j["status"] in ("queued", "running", "paused") and (j.get("params") or {}).get("schedule_id") == sched_id
               for j in jobs.list_jobs())


def run_schedule(sched_id, jobs, manual=False):
    """Startet den Zeitplan als Sammel-Backup-Job. Rückgabe: Job oder None (übersprungen)."""
    with _lock:
        sched = next((s for s in _load() if s["id"] == sched_id), None)
    if sched is None:
        raise ScheduleError("Zeitplan nicht gefunden.")
    log = applog.get("scheduler")
    log.info("Zeitplan '%s' wird ausgeführt%s", sched["name"], " (manuell)" if manual else "")
    if _is_active_for(jobs, sched_id):
        log.warning("Zeitplan '%s' übersprungen: der vorherige Lauf ist noch aktiv", sched["name"])
        _patch(sched_id, last_status="skipped", last_message="Übersprungen: der vorherige Lauf ist noch aktiv.")
        return None
    try:
        names, missing = _resolve_targets(sched)
    except docker_api.DockerError as exc:
        log.error("Zeitplan '%s': Docker nicht erreichbar: %s", sched["name"], exc)
        _patch(sched_id, last_run=time.time(), last_status="failed", last_message=str(exc))
        return None
    if not names:
        log.error("Zeitplan '%s': keine passenden Container gefunden", sched["name"])
        _patch(sched_id, last_run=time.time(), last_status="failed",
               last_message="Keine passenden Container gefunden.")
        return None
    job = jobs.submit_backup_batch(
        names, mode=sched["mode"], image_mode=sched.get("image_mode") or ("own" if sched.get("include_image") else "none"),
        stack_mode=sched.get("stack_mode", True), retention=sched["retention"],
        target_runs=sched.get("targets") or targets.clean_runs(None, sched.get("cv_backup_level")),
        retention_days=sched["retention_days"],
        fmt=sched.get("format", "tar.gz"), require_cv=sched.get("require_cv", False),
        extra={"schedule_id": sched["id"], "schedule_name": sched["name"], "manual": manual},
    )
    log.info("Zeitplan '%s': Auftrag %s mit %d Container(n) gestartet%s", sched["name"], job["id"], len(names),
             f", nicht gefunden: {', '.join(missing)}" if missing else "")
    note = f" (nicht gefunden: {', '.join(missing)})" if missing else ""
    _patch(sched_id, last_run=time.time(), last_status="running", last_job_id=job["id"],
           last_message=f"{len(names)} Container{note}")
    return job


def _tick(jobs):
    now = _now()
    with _lock:
        items = _load()
    for sched in items:
        # Ergebnis des laufenden Jobs übernehmen
        if sched.get("last_status") == "running" and sched.get("last_job_id"):
            job = jobs.get_job(sched["last_job_id"])
            if job is None:
                _patch(sched["id"], last_status="unknown", last_message="Job nicht mehr vorhanden (Neustart?).")
            elif job["status"] in ("finished", "failed"):
                _patch(sched["id"], last_status="ok" if job["status"] == "finished" else "failed",
                       last_message=job.get("error") or "Erfolgreich abgeschlossen.")
        if not sched.get("enabled"):
            continue
        slot = latest_slot(sched, now)
        if slot is None or sched.get("last_slot") == slot.isoformat():
            continue
        _patch(sched["id"], last_slot=slot.isoformat())
        if (now - slot).total_seconds() > CATCH_UP_WINDOW:
            text = f"Lauf um {slot.strftime('%d.%m. %H:%M')} verpasst (Helper war nicht aktiv)."
            applog.get("scheduler").warning("Zeitplan '%s': %s", sched["name"], text)
            _patch(sched["id"], last_status="missed", last_message=text)
            notify.schedule_missed(sched["name"], text)
            continue
        run_schedule(sched["id"], jobs)


def start(jobs):
    """Startet den Hintergrund-Thread (einmalig pro Prozess)."""
    global _started
    if _started or os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    _started = True
    applog.get("scheduler").info("Zeitplan-Takt gestartet (Prüfung alle %d s)", CHECK_INTERVAL)

    def loop():
        time.sleep(5)
        while True:
            try:
                _tick(jobs)
            except Exception:  # den Scheduler nie sterben lassen
                applog.get("scheduler").exception("Fehler im Zeitplan-Takt")
            try:
                notify.maybe_send_digest(_now(), lambda: insights.digest_inputs(list_schedules()))
            except Exception:
                applog.get("notify").exception("Tagesbericht fehlgeschlagen")
            time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, name="scheduler", daemon=True).start()
