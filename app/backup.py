import datetime
import hashlib
import json
import os
import re
import shutil
import tarfile
import time

from . import dbdump, docker_api, rules, store
from .cancel import Cancelled
from .config import config


class BackupError(Exception):
    pass


class PartialBackup(BackupError):
    """Die Sicherung liegt vor, aber ein Teil ist fehlgeschlagen (Dump, Post-Hook, Neustart)."""

    def __init__(self, message, ts):
        super().__init__(message)
        self.ts = ts


class MountTooLarge(Exception):
    def __init__(self, size, limit):
        super().__init__(f"{size} > {limit}")
        self.size, self.limit = size, limit


IMAGE_MODES = ("none", "own", "all")
DEFAULT_STOP_TIMEOUT = 120
SLOW_STOP_SECONDS = 45
HOOK_TIMEOUT = 900


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "container"


def _overlaps(path_a, path_b):
    """True, wenn einer der Pfade gleich oder Unterpfad des anderen ist."""
    a = os.path.normpath(path_a or "/")
    b = os.path.normpath(path_b or "/")
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _ts_now():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%S") + f"-{now.microsecond:06d}Z"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fmt_mb(n):
    return f"{n / 2 ** 30:.2f} GB" if n >= 2 ** 30 else f"{n / 2 ** 20:.1f} MB"


DB_NAME = os.path.basename(store.DB_PATH)


def _add_path(tar, full, arcname, target_file, db_files):
    """Wie tar.add – aber die eigene Datenbank kommt als konsistenter Snapshot ins Archiv, WAL/SHM gar nicht
    (direktes Lesen würde die SQLite-Sperren des Helpers aufheben, siehe store.live_db_files)."""
    role = None
    if db_files and os.path.basename(full).startswith(DB_NAME):
        st = os.stat(full)
        role = db_files.get((st.st_dev, st.st_ino))
    if role is None:
        tar.add(full, arcname=arcname)
        return True
    if role == "aux":
        return False
    snap = target_file + ".dbsnap"
    if os.path.exists(snap):
        os.remove(snap)
    try:
        store.snapshot_to(snap)
        info = tar.gettarinfo(snap, arcname=arcname)
        info.mode, info.uid, info.gid, info.mtime = st.st_mode & 0o7777, st.st_uid, st.st_gid, int(st.st_mtime)
        with open(snap, "rb") as fh:
            tar.addfile(info, fh)
    finally:
        if os.path.exists(snap):
            os.remove(snap)
    return True


def _tar_dir(source_dir, target_file, log_cb, compress=True, exclude=None, max_bytes=None, skip_paths=None, cancel=None):
    """Packt ein Verzeichnis als tar(.gz). Sortierte Reihenfolge, damit gleiche Daten gleiche Archive
    ergeben – das hilft der Deduplizierung in Commvault (besonders ohne gzip).
    'max_bytes' bricht das Packen ab (MountTooLarge), 'exclude' sind Ausschlussmuster,
    'skip_paths' absolute Pfade, die Commvault direkt sichert (werden komplett weggelassen)."""
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    count = skipped = vanished = 0
    skip_paths = {os.path.normpath(p) for p in skip_paths or ()}
    db_files = store.live_db_files()
    with tarfile.open(target_file, "w:gz" if compress else "w") as tar:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            rel_root = os.path.relpath(root, source_dir)
            if skip_paths:
                dirs[:] = [d for d in dirs if os.path.normpath(os.path.join(root, d)) not in skip_paths]
            if exclude:
                keep = []
                for d in dirs:
                    rel = d if rel_root == "." else f"{rel_root}/{d}"
                    if rules.excluded(rel, exclude):
                        skipped += 1
                    else:
                        keep.append(d)
                dirs[:] = keep
            for name in sorted(files):
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, source_dir)
                if skip_paths and os.path.normpath(full) in skip_paths:
                    continue
                if exclude and rules.excluded(arcname, exclude):
                    skipped += 1
                    continue
                try:
                    if not _add_path(tar, full, arcname, target_file, db_files):
                        continue
                except FileNotFoundError:
                    # Datei ist zwischen Auflisten und Packen verschwunden (rotiertes Log, Temp-Datei) – kein Grund abzubrechen,
                    # vor allem bei Ordnern, die ein anderer, weiterlaufender Container mitbenutzt
                    vanished += 1
                    continue
                count += 1
                if max_bytes and tar.offset > max_bytes:
                    raise MountTooLarge(tar.offset, max_bytes)
                if count % 200 == 0:
                    if cancel:
                        cancel()   # wirft Cancelled
                    try:
                        if shutil.disk_usage(os.path.dirname(target_file)).free < MIN_FREE_BYTES:
                            raise BackupError("Speicherplatz im Backup-Ordner ist aufgebraucht – Backup abgebrochen.")
                    except OSError:
                        pass
    log_cb(f"  {os.path.basename(target_file)}: {count} Dateien gesichert"
           + (f", {skipped} per Ausschlussmuster übersprungen" if skipped else "")
           + (f", {vanished} während des Packens verschwunden (übersprungen)" if vanished else ""))
    return count


def _tar_file(source_file, target_file, log_cb, compress=False):
    """Einzelne Datei (Bind-Mount einer Datei, z. B. eine Konfigurationsdatei) als Archiv mit einem Eintrag."""
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with tarfile.open(target_file, "w:gz" if compress else "w") as tar:
        _add_path(tar, source_file, os.path.basename(source_file), target_file, store.live_db_files())
    log_cb(f"  {os.path.basename(target_file)}: Datei {os.path.basename(source_file)} gesichert")


def _safe_member(member):
    parts = member.name.replace("\\", "/").split("/")
    return not member.name.startswith("/") and ".." not in parts


def _extract_tar(tar_file, target_dir, log_cb):
    os.makedirs(target_dir, exist_ok=True)
    count = 0
    with tarfile.open(tar_file, "r:*") as tar:   # erkennt tar und tar.gz selbst
        for member in tar:
            if not _safe_member(member):
                continue
            tar.extract(member, target_dir, numeric_owner=True, filter="fully_trusted")
            count += 1
    log_cb(f"  {os.path.basename(tar_file)}: {count} Dateien wiederhergestellt")
    return count


def _clear_dir(path):
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def read_manifest(container_name, folder):
    path = os.path.join(config.backup_root, safe_name(container_name), folder, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_local_backups(container_name):
    base = os.path.join(config.backup_root, safe_name(container_name))
    if not os.path.isdir(base):
        return []
    result = []
    for entry in sorted(os.listdir(base), reverse=True):
        manifest_path = os.path.join(base, entry, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            manifest = {}
        result.append({
            "skipped": len(manifest.get("skipped_mounts") or []),
            "ts": entry,
            "size_bytes": manifest.get("total_size_bytes"),
            "image": manifest.get("image"),
            "image_saved": bool(manifest.get("image_data")),
            "mode": manifest.get("mode"),
            "stack": manifest.get("stack"),
            "run_id": manifest.get("run_id"),
            "dumps": [d.get("type") for d in manifest.get("dumps") or [] if d.get("file")],
        })
    return result


def list_all_local_backups():
    """Alle lokalen Sicherungen aller Ordner unter backup_root (auch von gelöschten Containern)."""
    root = config.backup_root
    names = sorted(n for n in os.listdir(root) if not n.startswith(".")) if os.path.isdir(root) else []
    rows = []
    for name in names:
        for entry in list_local_backups(name):
            rows.append({"container": name, "folder": entry["ts"], "size_bytes": entry["size_bytes"] or 0,
                         "image": entry["image_saved"], "mode": entry.get("mode"),
                         "skipped": entry.get("skipped", 0), "stack": entry.get("stack"), "dumps": entry["dumps"],
                         "created": (_parse_ts(entry["ts"]).timestamp() if _parse_ts(entry["ts"]) else None)})
    return rows


def delete_local_backups(items, log_cb=None):
    """Löscht einzelne Sicherungsordner. items: [{'container', 'folder'}]. Rückgabe: (gelöscht, freigegebene Bytes, Fehler)."""
    deleted, freed, errors = 0, 0, []
    root = os.path.realpath(config.backup_root)
    for item in items:
        name, folder = str(item.get("container", "")), str(item.get("folder", ""))
        if not _parse_ts(folder) or "/" in folder or "/" in name or name.startswith(".") or not name:
            errors.append(f"{name}/{folder}: ungültiger Eintrag")
            continue
        path = os.path.realpath(os.path.join(root, name, folder))
        if os.path.dirname(os.path.dirname(path)) != root or not os.path.isfile(os.path.join(path, "manifest.json")):
            errors.append(f"{name}/{folder}: Sicherung nicht gefunden")
            continue
        size = 0
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    st = os.stat(os.path.join(dirpath, f))
                    if st.st_nlink <= 1:     # per Hardlink geteilte Images geben erst mit der letzten Kopie Platz frei
                        size += st.st_size
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            errors.append(f"{name}/{folder}: konnte nicht vollständig gelöscht werden")
            continue
        deleted += 1
        freed += size
        if log_cb:
            log_cb(f"Lokale Sicherung gelöscht: {name}/{folder}")
        try:
            os.rmdir(os.path.join(root, name))     # leeren Container-Ordner entfernen
        except OSError:
            pass
    return deleted, freed, errors


FORMATS = ("tar", "tar.gz")
MIN_FREE_BYTES = 512 * 1024 * 1024


def check_space(container_name, log_cb=None):
    """Bricht ab, wenn der freie Platz für eine weitere Sicherung (Größe der letzten + Puffer) nicht reicht."""
    log_cb = log_cb or (lambda msg: None)
    try:
        free = shutil.disk_usage(config.backup_root).free
    except OSError:
        return
    last = list_local_backups(container_name)
    estimate = int((last[0].get("size_bytes") or 0) * 1.15) if last else 0
    need = estimate + MIN_FREE_BYTES
    if free < need:
        raise BackupError(
            f"Zu wenig Speicherplatz in {config.backup_root}: frei {free // 2**20} MB, "
            f"benötigt etwa {need // 2**20} MB. Backup wurde nicht gestartet.")


# ───────── Images ─────────
def image_decision(image_mode, setting, snapshot, own_registries=()):
    """Soll das Image gesichert werden? Rückgabe: (ja/nein, Begründung, Herkunft)."""
    origin = docker_api.image_origin(snapshot.get("image_id") or snapshot.get("image"), own_registries)
    if setting == "never":
        return False, "per Container-Einstellung nie", origin
    if origin["origin"] == "unknown":
        return False, origin["text"], origin
    if setting == "always":
        return True, f"{origin['text']}, per Container-Einstellung immer", origin
    if image_mode == "all":
        return True, origin["text"], origin
    if image_mode == "own":
        if origin["origin"] in ("local", "private"):
            return True, origin["text"], origin
        return False, f"{origin['text']} – wird beim Restore exakt per Digest neu geladen", origin
    return False, "Images werden in diesem Auftrag nicht gesichert", origin


def _previous_image(container_name, image_id):
    """Ein bereits gesichertes image.tar desselben Images aus einer älteren lokalen Sicherung."""
    for entry in list_local_backups(container_name)[:10]:
        manifest = read_manifest(container_name, entry["ts"]) or {}
        data = manifest.get("image_data") or {}
        if data and (data.get("image_id") or manifest.get("image_id")) == image_id:
            path = os.path.join(config.backup_root, safe_name(container_name), entry["ts"], "data", data["tar"])
            if os.path.isfile(path) and os.path.getsize(path) == data.get("size_bytes"):
                return {"path": path, "size": data["size_bytes"], "sha256": data.get("sha256"),
                        "from": f"{container_name}/{entry['ts']}"}
    return None


# ───────── Ein Backup-Lauf eines Containers ─────────
class BackupRun:
    """Ein Backup eines Containers in Schritten, damit mehrere Container eines Compose-Stacks gemeinsam
    gestoppt werden können: prepare (Pre-Hook, Dump) → stop → archive (Daten) → restart (Start, Post-Hook)
    → complete (Image, Manifest) → cleanup. Das Image wird erst nach dem Neustart exportiert."""

    def __init__(self, name, mode="live", image_mode="own", fmt="tar", log_cb=None, progress_cb=None,
                 run_id=None, stack=None, image_cache=None, cancel_check=None):
        self.cancel_check = cancel_check
        self.name = name
        self.requested_mode = mode
        self.mode = mode
        self.image_mode = image_mode
        self.fmt = fmt
        self.log = log_cb or (lambda msg: None)
        self.progress = progress_cb or (lambda done, total: None)
        self.run_id, self.stack = run_id, stack
        self.image_cache = image_cache if image_cache is not None else {}
        self.snapshot = None
        self.settings = {}
        self.ts = self.base_dir = self.data_dir = None
        self.was_running = self.stopped = self.completed = False
        self.prepared = False
        self.error = None
        self.dumps, self.hooks = [], {}
        self.dump_error = self.post_error = self.restart_error = self.image_error = None
        self.manifest = None
        self.parts, self.data_done = None, False

    # 1) Vorbereiten: Konfiguration sichern, Pre-Hook und Dump laufen noch bei laufendem Container
    def prepare(self):
        if self.mode not in ("live", "stop"):
            raise BackupError("mode muss 'live' oder 'stop' sein.")
        if self.fmt not in FORMATS:
            raise BackupError("Format muss 'tar' oder 'tar.gz' sein.")
        if self.image_mode not in IMAGE_MODES:
            raise BackupError("Image-Modus muss 'none', 'own' oder 'all' sein.")
        check_space(self.name, self.log)
        self.self_backup = docker_api.is_self(self.name)
        self.snapshot = docker_api.inspect_snapshot(self.name)
        self.rule_set = rules.get_rules()
        self.settings = rules.container_settings(self.name, self.snapshot.get("labels"), self.snapshot.get("image"),
                                                 self.snapshot.get("env"), self.rule_set)
        if self.settings["mode"] and self.settings["mode"] != self.mode:
            self.log(f"Container-Einstellung: Modus '{self.settings['mode']}' statt '{self.mode}'.")
            self.mode = self.settings["mode"]
        if self.self_backup and self.mode == "stop":
            # Stoppt sich der Helper selbst, bricht das Backup ab und der Container bleibt aus.
            self.log("Hinweis: Das ist der Helper-Container selbst – er wird immer live gesichert, nie gestoppt.")
            self.mode = "live"
        self.was_running = docker_api.container_running(self.name)

        self.ts = _ts_now()
        self.base_dir = os.path.join(config.backup_root, safe_name(self.name), self.ts)
        self.data_dir = os.path.join(self.base_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.prepared = True
        with open(os.path.join(self.base_dir, "container.json"), "w", encoding="utf-8") as fh:
            json.dump(self.snapshot, fh, indent=2)
        self.log("Container-Konfiguration gesichert.")

        if not self.was_running:
            if self.settings["pre"] or self.settings["dump"]:
                self.log("Container läuft nicht – Hooks und Datenbank-Dump entfallen.")
            return
        if self.settings["pre"]:
            ok, detail = self._hook("pre")
            if not ok:
                raise BackupError(f"Pre-Hook fehlgeschlagen ({detail}) – Container wurde nicht gesichert.")
        if self.settings["dump"]:
            self._dump(self.settings["dump"])

    def _hook(self, which):
        cmd = self.settings[which]
        label = "Pre" if which == "pre" else "Post"
        self.log(f"{label}-Hook: {cmd}")
        start = time.time()
        try:
            code, out, err = docker_api.exec_stream(self.name, ["sh", "-c", cmd], timeout=HOOK_TIMEOUT)
        except docker_api.DockerError as exc:
            self.hooks[which] = {"cmd": cmd, "exit": None, "error": str(exc)}
            return False, str(exc)
        tail = (out + err).strip()
        for line in tail.splitlines()[-5:]:
            self.log(f"  {line[:300]}")
        self.hooks[which] = {"cmd": cmd, "exit": code, "seconds": round(time.time() - start, 1)}
        return code == 0, f"Exit-Code {code}" + (f": {tail[-200:]}" if tail and code else "")

    def _dump(self, kind):
        label = dbdump.LABELS.get(kind, kind)
        start = time.time()
        if kind == "redis":
            self.log(f"{label}: Speicherstand schreiben (SAVE) ...")
            try:
                code, _out, err = docker_api.exec_stream(self.name, dbdump.dump_command(kind), timeout=HOOK_TIMEOUT)
            except docker_api.DockerError as exc:
                code, err = None, str(exc)
            if code == 0:
                self.dumps.append({"type": kind, "file": None, "seconds": round(time.time() - start, 1)})
                self.log("  Redis-Speicherstand (dump.rdb) geschrieben – er liegt im gesicherten Volume.")
            else:
                self.dump_error = f"{label}-SAVE fehlgeschlagen: {(err or '').strip()[-300:]}"
                self.log(f"WARNUNG: {self.dump_error}")
            return
        file_name = dbdump.DUMP_FILE[kind]
        target = os.path.join(self.data_dir, file_name)
        self.log(f"Datenbank-Dump ({label}) ...")
        try:
            code, _out, err = docker_api.exec_stream(self.name, dbdump.dump_command(kind), out_path=target)
        except docker_api.DockerError as exc:
            code, err = None, str(exc)
        if code != 0:
            try:
                os.remove(target)
            except OSError:
                pass
            self.dump_error = (f"{label}-Dump fehlgeschlagen (Exit {code}): {(err or '').strip()[-300:]} – "
                               "bei Bedarf in den Backup-Einstellungen des Containers auf 'aus' stellen.")
            self.log(f"WARNUNG: {self.dump_error}")
            return
        size = os.path.getsize(target)
        self.dumps.append({"type": kind, "file": file_name, "size_bytes": size, "sha256": _sha256(target),
                           "seconds": round(time.time() - start, 1)})
        self.log(f"  {file_name}: {_fmt_mb(size)} in {time.time() - start:.0f} s")

    # 2) Stoppen (nur Modus 'stop'; der Helper selbst nie)
    def stop(self):
        if self.mode != "stop" or not self.was_running or self.stopped:
            return
        timeout = self.settings.get("stop_timeout") or DEFAULT_STOP_TIMEOUT
        self.log(f"Stoppe Container '{self.name}' für konsistentes Backup ...")
        start = time.time()
        docker_api.stop_container(self.name, timeout=timeout)
        self.stopped = True
        took = time.time() - start
        if took > SLOW_STOP_SECONDS and not self.settings.get("stop_timeout"):
            self.log(f"Hinweis: Stoppen dauerte {took:.0f} s – der Container reagiert vermutlich nicht auf SIGTERM. "
                     "Ein kürzerer Stop-Timeout in den Backup-Einstellungen spart Zeit.")

    # 3) Daten packen (Container ist im Modus 'stop' jetzt gestoppt)
    def archive(self):
        snapshot, data_dir, fmt = self.snapshot, self.data_dir, self.fmt
        ext = ".tar" if fmt == "tar" else ".tar.gz"
        volume_entries, bind_entries, skipped_mounts = [], [], []
        mount_index = 0
        total_mounts = len([m for m in snapshot["mounts"] if m["type"] in ("volume", "bind")])
        done = 0
        labels = snapshot.get("labels") or {}
        decisions = {d["destination"]: d for d in
                     rules.plan_mounts(self.name, snapshot["mounts"], labels, self.rule_set, use_docker=True,
                                       is_self=self.self_backup)}
        exclude = rules.exclude_globs(self.name, labels, self.rule_set) or None
        limit = rules.size_limit_bytes(self.rule_set)
        if exclude:
            self.log("Ausschlussmuster: " + ", ".join(exclude))

        def skip_mount(mount, reason, text):
            nonlocal done
            skipped_mounts.append({"destination": mount.get("destination"), "type": mount["type"],
                                   "name": mount.get("name"), "source": mount.get("source"),
                                   "reason": reason, "text": text})
            self.log(f"Überspringe '{mount.get('destination')}' ({mount.get('name') or mount.get('source')}): {text}")
            done += 1
            self.progress(done, total_mounts)

        direct = []   # was Commvault direkt sichert – fürs Manifest und den Restore-Hinweis

        def direct_skip(decision, local_root):
            subs = (decision or {}).get("direct_sub") or []
            for sub in subs:
                direct.append({"destination": decision.get("destination"), "host_path": sub["host"], "rel": sub["rel"]})
            return [os.path.join(local_root, sub["rel"]) for sub in subs]

        def direct_note(decision):
            subs = (decision or {}).get("direct_sub") or []
            return (" (ohne " + ", ".join(s["rel"] + "/" for s in subs) + " – sichert Commvault direkt)") if subs else ""

        too_large = ("abgebrochen: größer als das Limit von {} (Schutz vor riesigen Mediensammlungen – "
                     "in den Einstellungen anpassbar oder gezielt einschließen)")
        for mount in snapshot["mounts"]:
            decision = decisions.get(mount.get("destination")) if mount["type"] in ("volume", "bind") else None
            if decision and decision["action"] == "skip":
                skip_mount(mount, decision["reason"], decision["text"])
                if decision["reason"] == "cv_direct":
                    direct.append({"destination": mount.get("destination"), "host_path": mount.get("source"), "rel": "."})
                continue
            mount_limit = None if (decision and not decision["guard"]) else limit
            if mount["type"] == "volume":
                name = mount.get("name") or os.path.basename(mount.get("source") or "")
                source_dir = os.path.join(config.volumes_root, name, "_data")
                if not os.path.isdir(source_dir):
                    self.log(f"Überspringe Volume '{name}': Pfad {source_dir} existiert nicht.")
                    continue
                tar_name = f"volume-{safe_name(name)}{ext}"
                self.progress(done, total_mounts)
                self.log(f"Sichere Volume '{name}' ..." + direct_note(decision))
                full_path = os.path.join(data_dir, tar_name)
                try:
                    _tar_dir(source_dir, full_path, self.log, compress=(fmt != "tar"), exclude=exclude,
                             max_bytes=mount_limit, skip_paths=direct_skip(decision, source_dir), cancel=self.cancel_check)
                except MountTooLarge as big:
                    os.remove(full_path)
                    skip_mount(mount, "too_large", too_large.format(rules._gb(big.limit)))
                    continue
                volume_entries.append({
                    "name": name,
                    "destination": mount.get("destination"),
                    "readonly": bool(mount.get("readonly")),
                    "tar": tar_name,
                    "size_bytes": os.path.getsize(full_path),
                    "sha256": _sha256(full_path),
                })
            elif mount["type"] == "bind":
                source = mount.get("source")
                local = decision.get("local") if decision else None
                kind = decision.get("kind") if decision else None
                if not local or kind not in ("dir", "file"):
                    self.log(f"Überspringe Bind-Mount '{mount.get('destination')}': {source} nicht lesbar.")
                    continue
                tar_name = f"bindmount-{mount_index}{ext}"
                mount_index += 1
                self.progress(done, total_mounts)
                self.log(f"Sichere Bind-Mount '{mount.get('destination')}' ({source}) ..." + direct_note(decision))
                full_path = os.path.join(data_dir, tar_name)
                try:
                    if kind == "file":
                        _tar_file(local, full_path, self.log, compress=(fmt != "tar"))
                    else:
                        _tar_dir(local, full_path, self.log, compress=(fmt != "tar"), exclude=exclude,
                                 max_bytes=mount_limit, skip_paths=direct_skip(decision, local), cancel=self.cancel_check)
                except MountTooLarge as big:
                    os.remove(full_path)
                    skip_mount(mount, "too_large", too_large.format(rules._gb(big.limit)))
                    continue
                bind_entries.append({
                    "source": source,
                    "destination": mount.get("destination"),
                    "readonly": bool(mount.get("readonly")),
                    "kind": kind,
                    "tar": tar_name,
                    "size_bytes": os.path.getsize(full_path),
                    "sha256": _sha256(full_path),
                })
            else:
                continue
            done += 1
            self.progress(done, total_mounts)

        self.parts = {"volumes": volume_entries, "binds": bind_entries, "skipped": skipped_mounts, "exclude": exclude,
                      "cv_direct": direct}
        self.data_done = True

    # 4) Nach dem Neustart: Image sichern und Manifest schreiben (verlängert die Ausfallzeit nicht)
    def complete(self):
        if self.error is not None or not self.data_done:
            return
        try:
            self._write_manifest()
        except Exception as exc:
            self.error = exc

    def _write_manifest(self):
        snapshot, fmt = self.snapshot, self.fmt
        labels = snapshot.get("labels") or {}
        volume_entries, bind_entries = self.parts["volumes"], self.parts["binds"]
        skipped_mounts, exclude = self.parts["skipped"], self.parts["exclude"]
        image_entry = None
        save, why, origin = image_decision(self.image_mode, self.settings.get("image"), snapshot,
                                           self.rule_set.get("own_registries") or ())
        if save:
            self.log(f"Sichere Image '{snapshot['image']}' ({why}) ...")
            try:
                image_entry = self._store_image()
            except Cancelled:
                raise
            except Exception as exc:
                self.image_error = f"Image-Export fehlgeschlagen: {exc}"
                self.log(f"WARNUNG: {self.image_error}")
                try:
                    os.remove(os.path.join(self.data_dir, "image.tar"))
                except OSError:
                    pass
        else:
            self.log(f"Image '{snapshot['image']}' wird nicht gesichert: {why}.")

        compose = docker_api.compose_info(labels)
        manifest = {
            "helper_version": "1.1",
            "container": self.name,
            "ts": self.ts,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "mode": self.mode,
            "format": fmt,
            "run_id": self.run_id,
            "stack": self.stack or compose["stack"],
            "service": compose["service"],
            "depends_on": compose["depends_on"],
            "image": snapshot["image"],
            "image_id": snapshot.get("image_id"),
            "image_repo_digests": snapshot.get("image_repo_digests"),
            "image_origin": origin.get("origin"),
            "volumes": volume_entries,
            "bind_mounts": bind_entries,
            "skipped_mounts": skipped_mounts,
            "excluded_globs": exclude or [],
            "cv_direct": self.parts.get("cv_direct") or [],
            "image_data": image_entry,
            "dumps": self.dumps,
            "dump_error": self.dump_error,
            "hooks": self.hooks,
            "config_file": "container.json",
            "total_size_bytes": sum(
                [e["size_bytes"] for e in volume_entries + bind_entries]
                + [d.get("size_bytes") or 0 for d in self.dumps]
                + ([image_entry["size_bytes"]] if image_entry else [])
            ),
        }
        with open(os.path.join(self.base_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        self.manifest = manifest
        self.completed = True
        self.log(f"Backup abgeschlossen: {self.base_dir}")

    def _store_image(self):
        image_id = self.snapshot.get("image_id")
        target = os.path.join(self.data_dir, "image.tar")
        source = self.image_cache.get(image_id) or _previous_image(self.name, image_id)
        if source and os.path.isfile(source["path"]):
            try:
                os.link(source["path"], target)
                sha = source.get("sha256") or _sha256(target)
                self.log(f"  Image unverändert – als Hardlink aus {source['from']} übernommen (kein Export, kein Zusatzplatz).")
                self.image_cache[image_id] = {**source, "sha256": sha}
                return {"tar": "image.tar", "size_bytes": source["size"], "sha256": sha, "image_id": image_id,
                        "ref": self.snapshot["image"], "linked_from": source["from"]}
            except OSError:
                pass
        try:
            image = docker_api.long_client().images.get(image_id or self.snapshot["image"])
        except Exception as exc:
            self.log(f"Warnung: Image konnte nicht gelesen werden: {exc}")
            return None
        named = self.snapshot["image"] if self.snapshot["image"] in (image.tags or []) else (
            image.tags[0] if image.tags else False)
        with open(target, "wb") as fh:
            for chunk in image.save(named=named):
                fh.write(chunk)
                if self.cancel_check:
                    self.cancel_check()
        size, sha = os.path.getsize(target), _sha256(target)
        self.log(f"  image.tar: {_fmt_mb(size)}")
        self.image_cache[image_id] = {"path": target, "size": size, "sha256": sha, "from": f"{self.name}/{self.ts}"}
        return {"tar": "image.tar", "size_bytes": size, "sha256": sha, "image_id": image_id,
                "ref": self.snapshot["image"], "named": bool(named)}

    # 5) Wieder starten (nur wenn hier gestoppt) und Post-Hook – auch nach Fehlern
    def restart(self):
        if self.stopped:
            self.log(f"Starte Container '{self.name}' wieder ...")
            try:
                docker_api.start_container(self.name)
                self.stopped = False
            except Exception as exc:
                self.restart_error = f"Container konnte nicht wieder gestartet werden: {exc}"
                self.log(f"FEHLER: {self.restart_error}")
        if self.prepared and self.was_running and self.settings.get("post") and not self.restart_error:
            ok, detail = self._hook("post")
            if not ok:
                self.post_error = f"Post-Hook fehlgeschlagen ({detail})"
                self.log(f"WARNUNG: {self.post_error}")

    def cleanup(self):
        if not self.completed and self.prepared and self.base_dir:
            shutil.rmtree(self.base_dir, ignore_errors=True)   # abgebrochene Sicherung nicht liegen lassen

    def finalize(self):
        """Einzel-Container: starten, Image + Manifest, aufräumen."""
        self.restart()
        self.complete()
        self.cleanup()

    def result(self):
        if self.error:
            raise self.error
        if not self.completed:
            raise BackupError(self.restart_error or "Backup nicht abgeschlossen.")
        problems = [p for p in (self.restart_error, self.dump_error, self.image_error, self.post_error) if p]
        if problems:
            raise PartialBackup("; ".join(problems), self.ts)
        return {"ts": self.ts, "dir": self.base_dir, "manifest": self.manifest}


def create_backup(container_name, mode="live", include_image=False, retention=0, progress_cb=None, log_cb=None,
                  retention_days=0, fmt="tar.gz", prune=True, image_mode=None, run_id=None, image_cache=None):
    """Backup eines einzelnen Containers (für Sammel-Backups und Stacks siehe tasks.JobManager)."""
    if image_mode is None:
        image_mode = "own" if include_image else "none"
    run = BackupRun(container_name, mode, image_mode, fmt, log_cb, progress_cb, run_id=run_id, image_cache=image_cache)
    try:
        run.prepare()
        run.stop()
        run.archive()
    except Exception as exc:
        run.error = exc
    run.finalize()
    result = run.result()
    if prune:
        apply_retention(container_name, retention, retention_days, log_cb=log_cb)
    return result


def start_order(items):
    """Startreihenfolge für Container eines Stacks: Abhängigkeiten (depends_on) zuerst, sonst Datenbanken
    zuerst. items: [{'name', 'service', 'depends_on', 'image', 'env_keys'}]. Gestoppt wird umgekehrt."""
    by_service = {it.get("service"): it["name"] for it in items if it.get("service")}
    deps = {it["name"]: [by_service[d] for d in it.get("depends_on") or []
                         if d in by_service and by_service[d] != it["name"]] for it in items}
    remaining = sorted(items, key=lambda it: (0 if dbdump.detect(it.get("image"), it.get("env_keys")) else 1,
                                              it["name"]))
    ordered, done = [], set()
    while remaining:
        ready = [it for it in remaining if all(d in done for d in deps[it["name"]])] or remaining[:1]
        item = ready[0]
        ordered.append(item["name"])
        done.add(item["name"])
        remaining.remove(item)
    return ordered


def _parse_ts(ts):
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})", ts or "")
    if not match:
        return None
    try:
        return datetime.datetime.fromisoformat(
            f"{match.group(1)}T{match.group(2)}:{match.group(3)}:{match.group(4)}+00:00")
    except ValueError:
        return None


def incomplete_backups(min_age_hours=2, container_name=None):
    """Sicherungsordner ohne manifest.json (abgebrochene/unterbrochene Läufe), älter als min_age_hours."""
    root = config.backup_root
    found = []
    names = [safe_name(container_name)] if container_name else sorted(
        n for n in os.listdir(root) if not n.startswith(".")) if os.path.isdir(root) else []
    limit = time.time() - min_age_hours * 3600
    for name in names:
        base = os.path.join(root, name)
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            path = os.path.join(base, entry)
            if not os.path.isdir(path) or os.path.isfile(os.path.join(path, "manifest.json")):
                continue
            if _parse_ts(entry) is None or os.path.getmtime(path) > limit:
                continue
            size = 0
            for r, _d, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(r, f))
                    except OSError:
                        pass
            found.append({"container": name, "folder": entry, "path": path, "size": size})
    return found


def cleanup_incomplete(min_age_hours=2, container_name=None, log_cb=None):
    log_cb = log_cb or (lambda msg: None)
    freed = 0
    items = incomplete_backups(min_age_hours, container_name)
    for item in items:
        log_cb(f"Entferne unvollständige Sicherung {item['container']}/{item['folder']} ({item['size'] // 2**20} MB)")
        shutil.rmtree(item["path"], ignore_errors=True)
        freed += item["size"]
    return len(items), freed


def apply_retention(container_name, keep=0, days=0, allowed_folders=None, log_cb=None):
    """Löscht lokale Sicherungen nach Anzahl ('keep': die letzten N) und/oder Alter ('days').
    Die neueste bleibt immer erhalten. Ist 'allowed_folders' gesetzt (z. B. die in Commvault bestätigten
    Ordner), werden nur diese gelöscht – alle anderen bleiben, bis sie dort angekommen sind.
    Rückgabe: (gelöscht, behalten_weil_nicht_in_commvault)."""
    log_cb = log_cb or (lambda msg: None)
    if not (keep and keep > 0) and not (days and days > 0):
        return [], []
    now = datetime.datetime.now(datetime.timezone.utc)
    deleted, held = [], []
    for index, entry in enumerate(list_local_backups(container_name)):   # neueste zuerst
        if index == 0:
            continue
        too_many = bool(keep and keep > 0 and index >= keep)
        created = _parse_ts(entry["ts"])
        too_old = bool(days and days > 0 and created and (now - created).days >= days)
        if not (too_many or too_old):
            continue
        if allowed_folders is not None and entry["ts"] not in allowed_folders:
            held.append(entry["ts"])
            continue
        path = os.path.join(config.backup_root, safe_name(container_name), entry["ts"])
        why = f"älter als {days} Tage" if too_old and not too_many else f"mehr als {keep} Sicherungen"
        log_cb(f"Lösche lokale Sicherung ({why}): {entry['ts']}")
        shutil.rmtree(path, ignore_errors=True)
        deleted.append(entry["ts"])
        if not os.path.exists(path):   # Commvault-Vermerk behalten, Eckdaten für die Anzeige merken
            store.mark_local_deleted(container_name, entry["ts"], info={
                "size_bytes": entry.get("size_bytes"), "image": entry.get("image_saved"), "mode": entry.get("mode"),
                "skipped": entry.get("skipped", 0), "stack": entry.get("stack"), "dumps": entry.get("dumps") or []})
    if held:
        log_cb(f"Behalte {len(held)} Sicherung(en), die laut Aufbewahrung gelöscht würden, "
               "aber noch nicht in Commvault bestätigt sind.")
    return deleted, held


def prune_local(container_name, keep, log_cb=None):
    return apply_retention(container_name, keep=keep, log_cb=log_cb)


def prune_local_by_age(container_name, days, log_cb=None):
    return apply_retention(container_name, days=days, log_cb=log_cb)


# ───────── Wiederherstellung ─────────
def _load_manifest_from_dir(dir_path):
    for root, _dirs, files in os.walk(dir_path):
        if "manifest.json" in files:
            manifest_path = os.path.join(root, "manifest.json")
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            return manifest, root
    raise BackupError(f"Keine manifest.json unter {dir_path} gefunden.")


def load_backup(backup_dir):
    """(manifest, snapshot, verzeichnis) einer Sicherung."""
    manifest, dir_path = _load_manifest_from_dir(backup_dir)
    with open(os.path.join(dir_path, manifest.get("config_file") or "container.json"), "r", encoding="utf-8") as fh:
        snapshot = json.load(fh)
    return manifest, snapshot, dir_path


def _verify_entry(entry, data_dir, log_cb):
    full_path = os.path.join(data_dir, entry["tar"])
    if not os.path.isfile(full_path):
        raise BackupError(f"Datei fehlt nach Restore: {entry['tar']}")
    actual = _sha256(full_path)
    if entry.get("sha256") and actual != entry["sha256"]:
        raise BackupError(f"Checksumme passt nicht für {entry['tar']}.")
    log_cb(f"  Prüfsumme OK: {entry['tar']}")


def _write_host_dir(tar_local, host_dir, log_cb, clear=False):
    """Entpackt ein Archiv in einen Host-Ordner über den Hilfs-Container (der Helper sieht Host-Pfade nur lesend).
    Läuft der Helper nicht in Docker, wird direkt geschrieben."""
    if docker_api.own_mounts() is None:
        os.makedirs(host_dir, exist_ok=True)
        if clear:
            _clear_dir(host_dir)
        return _extract_tar(tar_local, host_dir, log_cb) if tar_local else 0
    binds = {host_dir: {"bind": "/target", "mode": "rw"}}
    if tar_local:
        tar_host = docker_api.to_host(tar_local)
        if not tar_host:
            raise BackupError(f"Archiv {tar_local} liegt nicht auf einem Host-Pfad.")
        binds[os.path.dirname(tar_host)] = {"bind": "/archive", "mode": "ro"}
        args = ["extract", "/archive/" + os.path.basename(tar_host), "/target"] + (["--clear"] if clear else [])
    else:
        args = ["clear", "/target"]
    out = docker_api.run_worker(args, binds)
    count = out.splitlines()[-1] if out else "?"
    if tar_local:
        log_cb(f"  {os.path.basename(tar_local)}: {count} Dateien wiederhergestellt")
    return count


def ensure_image(manifest, snapshot, data_dir, log_cb):
    """Stellt sicher, dass das gesicherte Image vorhanden ist: gesichertes image.tar laden, sonst exakt per
    Digest aus der Registry ziehen, zuletzt per Tag. Rückgabe: Image-Referenz zum Anlegen des Containers."""
    ref = snapshot.get("image") or manifest.get("image")
    image_id = manifest.get("image_id") or snapshot.get("image_id")
    if image_id and docker_api.image_present(image_id):
        if ref and not docker_api.image_present(ref):
            docker_api.tag_image(image_id, ref)
        if ref and docker_api.client().images.get(ref).id == image_id:
            return ref
        return image_id
    data = manifest.get("image_data")
    if data and os.path.isfile(os.path.join(data_dir, data["tar"])):
        log_cb(f"Lade gesichertes Image aus {data['tar']} ...")
        docker_api.load_image(os.path.join(data_dir, data["tar"]))
        if ref and image_id and docker_api.image_present(image_id) and not docker_api.image_present(ref):
            docker_api.tag_image(image_id, ref)
        return ref if ref and docker_api.image_present(ref) else image_id
    digests = manifest.get("image_repo_digests") or snapshot.get("image_repo_digests") or []
    for digest in digests:
        try:
            log_cb(f"Lade Image exakt per Digest: {digest} ...")
            docker_api.pull_image(digest)
            pulled = docker_api.client().images.get(digest)
            if ref and not docker_api.image_present(ref):
                docker_api.tag_image(pulled.id, ref)
            return ref if ref and docker_api.image_present(ref) else pulled.id
        except docker_api.DockerError as exc:
            log_cb(f"  nicht möglich: {exc}")
    if ref:
        if not docker_api.image_present(ref):
            log_cb(f"Lade Image per Tag: {ref} (Version kann vom Backup abweichen) ...")
            docker_api.pull_image(ref)
        return ref
    raise BackupError("Kein Image für die Wiederherstellung verfügbar.")


def dump_plan(manifest, snapshot):
    """Für den Neuaufbau aus dem Dump: (typ, dump-datei, mount des Datenverzeichnisses) oder None."""
    for dump in manifest.get("dumps") or []:
        if dump.get("file"):
            mount = dbdump.data_mount(dump["type"], snapshot.get("mounts"), snapshot.get("env"))
            if mount:
                return dump["type"], dump["file"], mount
    return None


def import_dump(container_name, kind, dump_path, log_cb, wait_seconds=240):
    """Wartet, bis die Datenbank bereit ist, und spielt den Dump ein."""
    label = dbdump.LABELS.get(kind, kind)
    if not docker_api.container_running(container_name):
        log_cb(f"Starte '{container_name}' für den Dump-Import ...")
        docker_api.start_container(container_name)
    log_cb(f"Warte auf {label} in '{container_name}' ...")
    deadline, ready = time.time() + wait_seconds, False
    while time.time() < deadline:
        try:
            code, _o, _e = docker_api.exec_stream(container_name, dbdump.ready_command(kind), timeout=60)
            if code == 0:
                ready = True
                break
        except docker_api.DockerError:
            pass
        time.sleep(3)
    if not ready:
        raise BackupError(f"{label} in '{container_name}' wurde nicht rechtzeitig bereit – Dump nicht eingespielt.")
    time.sleep(3)
    name = "mobydisk-restore-" + os.path.basename(dump_path)
    log_cb(f"Kopiere Dump ({_fmt_mb(os.path.getsize(dump_path))}) in den Container ...")
    docker_api.put_file(container_name, "/tmp", name, dump_path)
    log_cb(f"Spiele {label}-Dump ein ...")
    start = time.time()
    try:
        code, _out, err = docker_api.exec_stream(container_name, dbdump.import_command(kind, f"/tmp/{name}"))
    finally:
        try:
            docker_api.exec_stream(container_name, ["rm", "-f", f"/tmp/{name}"], timeout=60)
        except docker_api.DockerError:
            pass
    errors, first = dbdump.count_errors(err)
    if code != 0:
        raise BackupError(f"Dump-Import fehlgeschlagen (Exit {code}): {err.strip()[-400:]}")
    log_cb(f"Dump eingespielt in {time.time() - start:.0f} s" + (f" – {errors} Meldung(en) mit ERROR (bei pg_dumpall "
                                                                  "z. B. 'role already exists', meist harmlos)" if errors else "."))
    for line in first:
        log_cb(f"  {line[:300]}")


def restore_from_backup(container_name, backup_dir, restore_bind_mounts=True,
                        restart_container=True, recreate_if_missing=True,
                        progress_cb=None, log_cb=None, use_dump=False, manage_lifecycle=True):
    """Spielt eine Sicherung in den Container zurück. Mit use_dump wird die Datenbank nicht aus der
    Dateikopie, sondern frisch initialisiert und aus dem Dump aufgebaut. manage_lifecycle=False: der
    Aufrufer stoppt/startet (Stack-Restore); dann liefert das Ergebnis 'pending_dump' für den Import."""
    progress_cb = progress_cb or (lambda done, total: None)
    log_cb = log_cb or (lambda msg: None)

    if docker_api.is_self(container_name):
        raise BackupError("Der Helper kann sich nicht selbst wiederherstellen – das würde den "
                          "laufenden Container beenden. Bitte manuell per Docker/Portainer zurückspielen.")

    manifest, snapshot, dir_path = load_backup(backup_dir)
    data_dir = os.path.join(dir_path, "data")
    for d in manifest.get("cv_direct") or []:
        where = d["host_path"] if d.get("rel") == "." else f"{d['host_path']} ({d['rel']}/ in {d['destination']})"
        log_cb(f"Hinweis: {where} sichert Commvault direkt – bleibt hier unverändert; ein älterer Stand kommt "
               "bei Bedarf über Commvault (Restore dieses Pfads) zurück.")

    plan = dump_plan(manifest, snapshot) if use_dump else None
    if use_dump and not plan:
        log_cb("Hinweis: kein verwendbarer Datenbank-Dump in dieser Sicherung – stelle die Dateikopie wieder her.")
    data_dest = plan[2]["destination"] if plan else None
    if plan:
        log_cb(f"Datenbank wird aus dem Dump neu aufgebaut ({dbdump.LABELS[plan[0]]}): "
               f"Datenverzeichnis {data_dest} wird geleert, der Container initialisiert es neu.")

    exists = True
    try:
        docker_api.get_container(container_name)
    except docker_api.DockerError:
        exists = False

    was_running = docker_api.container_running(container_name) if exists else False
    stopped_for_restore = False
    created = False
    try:
        if manage_lifecycle and exists and was_running:
            log_cb(f"Stoppe Container '{container_name}' für die Wiederherstellung ...")
            docker_api.stop_container(container_name)
            stopped_for_restore = True
        elif not manage_lifecycle and docker_api.container_running(container_name):
            raise BackupError(f"'{container_name}' läuft noch – Stack-Restore erwartet gestoppte Container.")

        binds = manifest["bind_mounts"] if restore_bind_mounts else []
        total_steps = max(len(manifest["volumes"]) + len(binds), 1)
        done = 0
        progress_cb(0, total_steps)

        for entry in manifest["volumes"]:
            progress_cb(done, total_steps)
            volume_name = entry["name"]
            docker_api.ensure_volume(volume_name)
            target = os.path.join(config.volumes_root, volume_name, "_data")
            if entry.get("destination") == data_dest:
                log_cb(f"Leere Volume '{volume_name}' (Datenbank wird aus dem Dump aufgebaut) ...")
                os.makedirs(target, exist_ok=True)
                _clear_dir(target)
            else:
                log_cb(f"Stelle Volume '{volume_name}' wieder her ...")
                _verify_entry(entry, data_dir, log_cb)
                _extract_tar(os.path.join(data_dir, entry["tar"]), target, log_cb)
            done += 1
            progress_cb(done, total_steps)

        for entry in binds:
            progress_cb(done, total_steps)
            source = entry["source"]
            host_dir = os.path.dirname(source) if entry.get("kind") == "file" else source
            if entry.get("destination") == data_dest:
                log_cb(f"Leere {source} (Datenbank wird aus dem Dump aufgebaut) ...")
                _write_host_dir(None, host_dir, log_cb)
            else:
                log_cb(f"Stelle Bind-Mount '{entry['destination']}' nach {source} wieder her ...")
                _verify_entry(entry, data_dir, log_cb)
                _write_host_dir(os.path.join(data_dir, entry["tar"]), host_dir, log_cb)
            done += 1
            progress_cb(done, total_steps)

        if not exists and recreate_if_missing:
            log_cb(f"Container '{container_name}' existiert nicht. Lege neu an ...")
            image = ensure_image(manifest, snapshot, data_dir, log_cb)
            docker_api.recreate_container({**snapshot, "image": image})
            created = True
            log_cb(f"Container '{container_name}' wurde neu angelegt.")

        progress_cb(total_steps, total_steps)
        log_cb("Datenwiederherstellung abgeschlossen.")
    finally:
        if manage_lifecycle and stopped_for_restore and restart_container and not docker_api.container_running(container_name):
            log_cb(f"Starte Container '{container_name}' wieder ...")
            docker_api.start_container(container_name)

    pending = None
    if plan:
        pending = {"container": container_name, "kind": plan[0], "path": os.path.join(data_dir, plan[1])}
        if manage_lifecycle:
            import_dump(container_name, plan[0], pending["path"], log_cb)
            pending = None
    if manage_lifecycle and created and restart_container:
        log_cb(f"Starte neu angelegten Container '{container_name}' ...")
        docker_api.start_container(container_name)
    return {"container": container_name, "ts": manifest.get("ts"), "pending_dump": pending,
            "was_running": was_running, "created": created}


# ───────── Restore als Klon (Sandbox) ─────────
CLONE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def clone_from_backup(container_name, backup_dir, clone_name, network="none", host_port=None, start=True,
                      attach_skipped=True, source=None, progress_cb=None, log_cb=None):
    """Stellt eine Sicherung als neuen Container mit eigenen Volumes bereit – das Original bleibt unberührt.
    Ohne Ports, ohne Neustart-Richtlinie, im isolierten Netz (oder Standard-Bridge)."""
    progress_cb = progress_cb or (lambda done, total: None)
    log_cb = log_cb or (lambda msg: None)
    if not CLONE_NAME_RE.match(clone_name or ""):
        raise BackupError("Ungültiger Klon-Name (Buchstaben, Ziffern, . _ -).")
    try:
        docker_api.get_container(clone_name)
        raise BackupError(f"Es gibt bereits einen Container '{clone_name}'.")
    except docker_api.DockerError:
        pass
    if network not in ("none", "bridge"):
        raise BackupError("Netzwerk muss 'none' oder 'bridge' sein.")

    manifest, snapshot, dir_path = load_backup(backup_dir)
    data_dir = os.path.join(dir_path, "data")
    for d in manifest.get("cv_direct") or []:
        log_cb(f"Hinweis: {d['host_path']} sichert Commvault direkt – im Klon fehlt dieser Teil "
               f"({d['destination']}{'' if d.get('rel') == '.' else '/' + d['rel']}).")
    image = ensure_image(manifest, snapshot, data_dir, log_cb)
    vol_labels = docker_api.own_labels({"clone": clone_name, "clone-of": container_name})
    created_volumes, mounts = [], []
    steps = max(len(manifest["volumes"]) + len(manifest["bind_mounts"]), 1)
    done = 0

    def new_volume(suffix):
        name = f"{clone_name}_{safe_name(suffix)}"[:120]
        if docker_api.volume_exists(name):
            raise BackupError(f"Volume '{name}' existiert bereits.")
        docker_api.ensure_volume(name, labels=vol_labels)
        created_volumes.append(name)
        return name, os.path.join(config.volumes_root, name, "_data")

    try:
        for entry in manifest["volumes"]:
            vname, target = new_volume(entry["name"])
            log_cb(f"Volume '{entry['name']}' → '{vname}' ...")
            _verify_entry(entry, data_dir, log_cb)
            _extract_tar(os.path.join(data_dir, entry["tar"]), target, log_cb)
            mounts.append(docker_api.docker.types.Mount(target=entry["destination"], source=vname, type="volume",
                                                        read_only=bool(entry.get("readonly"))))
            done += 1
            progress_cb(done, steps)
        for index, entry in enumerate(manifest["bind_mounts"]):
            vname, target = new_volume(f"bind{index}")
            log_cb(f"Bind-Mount '{entry['destination']}' ({entry['source']}) → Volume '{vname}' ...")
            _verify_entry(entry, data_dir, log_cb)
            _extract_tar(os.path.join(data_dir, entry["tar"]), target, log_cb)
            mount = docker_api.docker.types.Mount(target=entry["destination"], source=vname, type="volume",
                                                  read_only=bool(entry.get("readonly")))
            if entry.get("kind") == "file":
                mount["VolumeOptions"] = {"Subpath": os.path.basename(entry["source"])}
            mounts.append(mount)
            done += 1
            progress_cb(done, steps)
        if attach_skipped:
            for skipped in manifest.get("skipped_mounts") or []:
                src = skipped.get("source") or ""
                if skipped.get("reason") in ("special", "missing", "backup_root", "volumes_root", "host_share") \
                        or src.endswith(".sock"):
                    continue
                if skipped.get("type") == "volume" and skipped.get("name"):
                    mounts.append(docker_api.docker.types.Mount(target=skipped["destination"], source=skipped["name"],
                                                                type="volume", read_only=True))
                elif skipped.get("type") == "bind" and src:
                    mounts.append(docker_api.docker.types.Mount(target=skipped["destination"], source=src,
                                                                type="bind", read_only=True))
                else:
                    continue
                log_cb(f"Nicht gesicherter Mount '{skipped['destination']}' wird schreibgeschützt eingebunden "
                       f"({skipped.get('name') or src}).")

        kwargs = docker_api.create_kwargs({**snapshot, "name": clone_name, "image": image})
        kwargs.pop("_attach_networks", None)
        kwargs["mounts"] = mounts
        kwargs["labels"] = {**{k: v for k, v in (snapshot.get("labels") or {}).items()
                               if not k.startswith("com.docker.compose.")}, **docker_api.neutral_compose_labels(image),
                            **docker_api.own_labels({"clone": "1", "clone-of": container_name,
                                                     "clone-source": json.dumps(source or {}), "backup": "false"})}
        kwargs["restart_policy"] = {"Name": "no"}
        kwargs.pop("mac_address", None)
        kwargs.pop("ports", None)
        kwargs["network_mode"] = network
        if host_port:
            ports = list((snapshot.get("ports") or {}).keys()) or list(snapshot.get("exposed_ports") or [])
            if not ports:
                raise BackupError("Der Container hat keinen Port, der veröffentlicht werden könnte.")
            kwargs["ports"] = {ports[0]: int(host_port)}
            kwargs["network_mode"] = "bridge"
            log_cb(f"Port {ports[0]} des Klons ist unter Host-Port {host_port} erreichbar.")
        docker_api.create_container(kwargs)
        log_cb(f"Klon '{clone_name}' angelegt (Netz: {kwargs['network_mode']}, keine Neustart-Richtlinie).")
    except Exception:
        for name in created_volumes:
            try:
                docker_api.remove_volume(name)
            except docker_api.DockerError:
                pass
        raise

    status, health = None, None
    if start:
        log_cb(f"Starte Klon '{clone_name}' ...")
        docker_api.start_container(clone_name)
        status, health = docker_api.wait_state(clone_name, 90)
        log_cb(f"Klon-Status: {status}" + (f", Health: {health}" if health else ""))
        if status != "running":
            log_cb("Hinweis: Der Klon läuft nicht – Logs mit 'docker logs " + clone_name + "' prüfen.")
    return {"clone": clone_name, "volumes": created_volumes, "status": status, "health": health}


def remove_clone(clone_name, log_cb=None):
    log_cb = log_cb or (lambda msg: None)
    container = docker_api.get_container(clone_name)
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    if docker_api.own_label(labels, "clone") != "1":
        raise BackupError(f"'{clone_name}' ist kein vom Helper angelegter Klon.")
    docker_api.remove_container(clone_name)
    removed = []
    for name in docker_api.volumes_with_label("clone", clone_name):
        if docker_api.remove_volume(name):
            removed.append(name)
    log_cb(f"Klon '{clone_name}' entfernt ({len(removed)} Volumes).")
    return {"clone": clone_name, "volumes": removed, "source": docker_api.own_label(labels, "clone-source"),
            "clone_of": docker_api.own_label(labels, "clone-of")}


def scan_staging(staging_dir):
    found = []
    if not os.path.isdir(staging_dir):
        return found
    for root, _dirs, files in os.walk(staging_dir):
        if "manifest.json" in files:
            try:
                with open(os.path.join(root, "manifest.json"), "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
                found.append({
                    "dir": root,
                    "container": manifest.get("container"),
                    "ts": manifest.get("ts"),
                    "image": manifest.get("image"),
                    "total_size_bytes": manifest.get("total_size_bytes"),
                })
            except (OSError, ValueError):
                continue
    return found
