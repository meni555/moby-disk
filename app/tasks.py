import atexit
import contextlib
import logging
import os
import re
import shutil
import threading
import time
import uuid

from . import applog, backup, docker_api, store, targets
from .backup import BackupError
from .cancel import Cancelled
from .config import config
from .targets import TargetError


def describe_error(exc):
    """Fehlertext, der nie leer ist: Typ und Meldung (Ausnahmen wie NotImplementedError haben oft keine)."""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text and not isinstance(exc, (BackupError, TargetError)) else (text or f"{name} (ohne Meldung)")


def target_folders(target_id, container):
    """Stände des Containers im Ziel: {ordner: zeit}; None, wenn das Ziel nicht erreichbar/eingerichtet ist.
    Bestätigte Ordner werden dauerhaft vermerkt (Grundlage für Schutzanzeige und Liste im Tab Backups)."""
    target = targets.get(target_id)
    if "versions" not in target.capabilities:
        # Ziel ohne Ordnerliste (z. B. Veeam): bestätigt ist, was ein erfolgreicher Lauf dort vermerkt hat
        return {folder: None for folder in store.verified_folders(container, target_id)} if target.ready() else None
    found = target.list_folders(container)
    if found is None:
        return None
    for folder, meta in found.items():
        store.mark_verified(target_id, container, folder, meta.get("time"), meta.get("ref"),
                            info={"size_bytes": meta["size"]} if meta.get("size") else None)
    return {folder: meta.get("time") for folder, meta in found.items()}


def confirmed_folders(container, target_ids):
    """Ordner, die in ALLEN genannten Zielen vorhanden sind; None, wenn eines nicht erreichbar ist."""
    result = None
    for tid in target_ids:
        found = target_folders(tid, container)
        if found is None:
            return None
        result = set(found) if result is None else result & set(found)
    return result if result is not None else set()


def sync_target_state(target_id, log_cb=None, progress_cb=None):
    """Gleicht die Vermerke mit dem Ziel ab: alle Container-Ordner, auch solche, die lokal längst gelöscht sind.
    Rückgabe: {'containers', 'folders', 'removed'}; None, wenn das Ziel nicht eingerichtet/erreichbar ist."""
    target = targets.get(target_id)
    if not target.ready():
        return None
    if "versions" not in target.capabilities:   # Ziel kann keine Ordner auflisten – nur Vermerke prüfen
        entries = store.entries(target=target_id)
        valid = target.validate_entries(entries) if "state_check" in target.capabilities else None
        if valid is None:
            return None
        removed = 0
        for e in entries:
            if (e["container"], e["folder"]) not in valid:
                removed += store.drop_entries(target_id, e["container"], keep=[
                    x["folder"] for x in entries if x["container"] == e["container"] and (x["container"], x["folder"]) in valid])
        result = {"containers": len({e["container"] for e in entries}), "folders": len(valid), "removed": removed,
                  "time": time.time()}
        store.kv_set(f"sync_{target_id}", result)
        return result
    names = target.list_containers()
    if names is None:
        return None
    folders = removed = 0
    for index, name in enumerate(names, 1):
        found = target_folders(target_id, name)
        if found is None:
            if log_cb:
                log_cb(f"[{name}] Abfrage bei {target.label} fehlgeschlagen – übersprungen")
        else:
            folders += len(found)
            gone = store.drop_entries(target_id, name, keep=found.keys())   # abgelaufen oder im Ziel gelöscht
            removed += gone
            if log_cb:
                log_cb(f"[{name}] {len(found)} Sicherung(en) in {target.label}"
                       + (f", {gone} nicht mehr vorhanden" if gone else ""))
        if progress_cb:
            progress_cb(index, len(names))
    known = {e["container"] for e in store.entries(target=target_id)}
    for name in sorted(known - set(names)):   # Container-Ordner gibt es im Ziel gar nicht mehr
        gone = store.drop_entries(target_id, name)
        removed += gone
        if log_cb and gone:
            log_cb(f"[{name}] {gone} Vermerk(e) entfernt – Ordner ist in {target.label} nicht mehr vorhanden")
    result = {"containers": len(names), "folders": folders, "removed": removed, "time": time.time()}
    store.kv_set(f"sync_{target_id}", result)
    return result


def _source_of(params):
    return params.get("source") or ("commvault" if params.get("use_cv") else "local")


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()
        self._max_jobs = 200
        self._saved_at = {}
        self._container_locks = {}
        self._locks_guard = threading.Lock()
        self._ctl = {}   # Auftrag → {"pause": bool, "cancel": bool}
        self._load()
        atexit.register(self.flush)

    # ───────── Persistenz ─────────
    def flush(self):
        """Beim Beenden alle noch nicht gespeicherten Jobzustände sichern."""
        with self._lock:
            for job in self._jobs.values():
                if job["status"] in ("queued", "running", "paused"):
                    self._persist(job, force=True)

    def _load(self):
        try:
            for job in store.load_jobs(self._max_jobs):
                if job.get("status") in ("queued", "running", "paused"):
                    job["status"] = "failed"
                    job["error"] = "Unterbrochen: der Helper wurde während des Auftrags neu gestartet."
                    job["message"] = "Unterbrochen durch Neustart"
                    job["finished"] = job.get("finished") or time.time()
                    job.setdefault("log", []).append({"time": time.time(), "message": job["error"]})
                    store.save_job(job)
                self._jobs[job["id"]] = job
            store.prune_jobs()
        except Exception as exc:  # der Verlauf darf den Start nie verhindern
            print(f"[jobs] Verlauf konnte nicht geladen werden: {exc}", flush=True)

    def _persist(self, job, force=False):
        now = time.time()
        if not force and now - self._saved_at.get(job["id"], 0) < 1.5:
            return
        self._saved_at[job["id"]] = now
        try:
            store.save_job(job)
        except Exception as exc:
            print(f"[jobs] Speichern fehlgeschlagen: {exc}", flush=True)

    @contextlib.contextmanager
    def exclusive(self, name):
        """Pro Container darf nur ein Backup/Restore gleichzeitig laufen."""
        with self._locks_guard:
            lock = self._container_locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            raise BackupError(f"'{name}' wird gerade gesichert oder wiederhergestellt – bitte warten.")
        try:
            yield
        finally:
            lock.release()

    def _new(self, kind, params):
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "kind": kind,
            "params": params,
            "status": "queued",
            "progress": 0.0,
            "message": "In Warteschlange",
            "log": [],
            "error": None,
            "result": None,
            "started": time.time(),
            "finished": None,
            "control": None,     # angeforderte Steuerung: "pause" | "cancel"
            "locked": False,     # ab hier nicht mehr pausier-/abbrechbar (z. B. Daten werden zurückgeschrieben)
        }
        self._ctl[job_id] = {"pause": False, "cancel": False}
        with self._lock:
            self._jobs[job_id] = job
            while len(self._jobs) > self._max_jobs:
                oldest = sorted(self._jobs.values(), key=lambda j: j["started"])[0]
                del self._jobs[oldest["id"]]
            self._persist(job, force=True)
        return job

    def _update(self, job_id, **fields):
        with self._lock:
            job = self._jobs[job_id]
            job.update(fields)
            self._persist(job, force="status" in fields)

    def _log(self, job_id, message):
        with self._lock:
            job = self._jobs[job_id]
            job["log"].append({"time": time.time(), "message": message})
            self._persist(job)
        level = logging.ERROR if message.startswith("FEHLER") or "] FEHLER" in message else \
            logging.WARNING if message.startswith("WARNUNG") or message.startswith("Hinweis") else logging.INFO
        applog.get("job").log(level, "[%s] %s", job_id, message)

    def _warn(self, job_id, text, exc):
        """Nebenschritt fehlgeschlagen: als Warnung ins Auftragsprotokoll, mit Traceback ins zentrale Log – der Auftrag läuft weiter."""
        self._log(job_id, f"WARNUNG: {text}: {describe_error(exc)}")
        applog.get("job").warning("[%s] %s", job_id, text, exc_info=(type(exc), exc, exc.__traceback__))

    def _start(self, job_id, func, *args):
        def run():
            started = time.time()
            try:
                self._update(job_id, status="running")
                applog.get("job").info("[%s] Auftrag gestartet: %s", job_id, self._jobs[job_id].get("kind"))
                result = func(job_id, *args)
                self._update(job_id, status="finished", progress=100.0,
                             message="Abgeschlossen", result=result, finished=time.time())
                applog.get("job").info("[%s] Auftrag beendet (%.0f s)", job_id, time.time() - started)
            except Cancelled:
                self._log(job_id, "Auftrag wurde abgebrochen.")
                self._update(job_id, status="cancelled", message="Abgebrochen", control=None, finished=time.time())
                self._ctl.pop(job_id, None)
                return
            except Exception as exc:
                text = describe_error(exc)
                self._log(job_id, f"FEHLER: {text}")
                applog.get("job").error("[%s] Auftrag fehlgeschlagen nach %.0f s: %s", job_id, time.time() - started, text,
                                        exc_info=(type(exc), exc, exc.__traceback__))
                self._update(job_id, status="failed", message=text, error=text,
                             finished=time.time())
            self._ctl.pop(job_id, None)
            self._notify(job_id)
        thread = threading.Thread(target=run, name=f"job-{job_id}", daemon=True)
        thread.start()
        return self._jobs[job_id]

    # ───────── Pausieren und Abbrechen ─────────
    def control(self, job_id, action):
        """action: 'pause' | 'resume' | 'cancel'. Wirkt an den nächsten sicheren Stellen des Auftrags (zwischen Containern,
        vor und zwischen Zielen, beim Warten auf ein Ziel); laufende Container werden nie in halbem Zustand angehalten.
        Rückgabe: der Auftrag. Wirft BackupError, wenn die Steuerung nicht (mehr) möglich ist."""
        with self._lock:
            job = self._jobs.get(job_id)
        ctl = self._ctl.get(job_id)
        if not job:
            raise BackupError("Auftrag nicht gefunden.")
        if job["status"] not in ("queued", "running", "paused") or ctl is None:
            raise BackupError("Der Auftrag läuft nicht mehr.")
        if job.get("locked"):
            raise BackupError("Jetzt nicht mehr möglich: Daten werden gerade zurückgeschrieben – das lässt sich nicht "
                              "gefahrlos unterbrechen.")
        if action == "pause":
            if ctl["cancel"]:
                raise BackupError("Der Auftrag wird bereits abgebrochen.")
            ctl["pause"] = True
            self._update(job_id, control="pause")
            self._log(job_id, "Pause angefordert – wirkt an der nächsten sicheren Stelle.")
        elif action == "resume":
            ctl["pause"] = False
            self._update(job_id, control=None)
        elif action == "cancel":
            ctl["cancel"] = True
            ctl["pause"] = False
            self._update(job_id, control="cancel")
            self._log(job_id, "Abbruch angefordert – wirkt an der nächsten sicheren Stelle"
                              + (" (laufende Archivierung: nach der aktuellen Datei)." if job["kind"] == "backup" else "."))
        else:
            raise BackupError("Unbekannte Aktion.")
        return self.get_job(job_id)

    def cancel_check(self, job_id):
        """Wirft Cancelled, wenn der Abbruch angefordert wurde (für lange Schleifen; pausiert nicht)."""
        ctl = self._ctl.get(job_id)
        if ctl and ctl["cancel"]:
            raise Cancelled()

    def checkpoint(self, job_id):
        """Sichere Stelle: wirft Cancelled bei Abbruch und wartet, solange der Auftrag pausiert ist."""
        ctl = self._ctl.get(job_id)
        if ctl is None:
            return
        if ctl["cancel"]:
            raise Cancelled()
        if not ctl["pause"]:
            return
        before = self.get_job(job_id).get("message") or ""
        self._log(job_id, "Pausiert.")
        self._update(job_id, status="paused", message="Pausiert – " + before[:120])
        while ctl["pause"] and not ctl["cancel"]:
            time.sleep(0.5)
        if ctl["cancel"]:
            raise Cancelled()
        self._update(job_id, status="running", message=before, control=None)
        self._log(job_id, "Fortgesetzt.")

    def lock(self, job_id):
        """Ab hier ist der Auftrag nicht mehr pausier- oder abbrechbar."""
        self._update(job_id, locked=True)

    def _notify(self, job_id):
        try:
            from . import notify
            notify.job_finished(self.get_job(job_id))
        except Exception as exc:
            print(f"[jobs] Benachrichtigung fehlgeschlagen: {exc}", flush=True)

    # ───────── Abgleich mit einem Ziel ─────────
    def submit_target_sync(self, target_id):
        label = targets.get(target_id).label
        job = self._new("cvsync", {"container": f"Abgleich mit {label}", "target": target_id})
        return self._start(job["id"], self._run_target_sync, target_id)

    def _run_target_sync(self, job_id, target_id):
        label = targets.get(target_id).label
        self._update(job_id, message=f"Lese Container-Ordner aus {label} …")

        def progress(done, total):
            self._update(job_id, progress=round(done / total * 100.0, 1), message=f"Abgleich mit {label} ({done}/{total})")
            self.checkpoint(job_id)

        result = sync_target_state(target_id, log_cb=lambda msg: self._log(job_id, msg), progress_cb=progress)
        if result is None:
            raise TargetError(f"{label} ist nicht eingerichtet oder nicht erreichbar.")
        self._log(job_id, f"Abgleich fertig: {result['folders']} Sicherungen von {result['containers']} Containern "
                          f"in {label} vermerkt" + (f", {result['removed']} nicht mehr vorhandene entfernt"
                                                     if result["removed"] else ""))
        return result

    # ───────── Backup ─────────
    def submit_backup(self, container_name, **options):
        return self.submit_backup_batch([container_name], **options)

    def submit_backup_batch(self, container_names, mode="live", include_image=None, retention=0,
                            cv_backup_level=None, retention_days=0, extra=None, fmt="tar",
                            require_cv=False, image_mode=None, stack_mode=True, target_runs=None):
        names = list(dict.fromkeys(container_names))
        if target_runs is None:   # ältere Aufrufer: nur die Commvault-Stufe
            target_runs = [{"id": "commvault", "level": cv_backup_level}] if cv_backup_level else []
        for tr in target_runs:
            targets.get(tr["id"])   # unbekanntes Ziel früh melden
        if image_mode is None:
            image_mode = "own" if include_image else "none"
        params = {
            "container": names[0] if len(names) == 1 else f"{len(names)} Container",
            "containers": names,
            "mode": mode,
            "image_mode": image_mode,
            "include_image": image_mode != "none",
            "retention": retention,
            "retention_days": retention_days,
            "cv_backup_level": cv_backup_level,
            "targets": target_runs,
            "format": fmt,
            "require_cv": require_cv,
            "stack_mode": stack_mode,
        }
        params.update(extra or {})
        job = self._new("backup", params)
        self._start(job["id"], self._run_backup_batch, names, mode, image_mode, retention,
                    target_runs, retention_days, fmt, require_cv, stack_mode)
        return job

    @staticmethod
    def _groups(names, stack_mode):
        """Container in Gruppen: ein Compose-Stack wird gemeinsam gestoppt (in Abhängigkeitsreihenfolge),
        alle anderen einzeln. Rückgabe: [(stack oder None, [namen in Startreihenfolge])]."""
        try:
            info = {c["name"]: c for c in docker_api.list_containers()}
        except docker_api.DockerError:
            info = {}
        groups, index = [], {}
        for name in names:
            stack = (info.get(name) or {}).get("stack") if stack_mode else None
            if stack and stack in index:
                groups[index[stack]][1].append(name)
                continue
            if stack:
                index[stack] = len(groups)
            groups.append((stack, [name]))
        result = []
        for stack, members in groups:
            if stack and len(members) > 1:
                members = backup.start_order([info[n] for n in members])
            result.append((stack if len(members) > 1 else None, members))
        return result

    def _deliver(self, job_id, target_runs, done, new_folder, retention, retention_days, require_cv, failed):
        """Übergibt die fertigen Sicherungen an die gewählten Ziele, wendet danach die lokale Aufbewahrung an
        (bei require_cv nur für in allen Zielen bestätigte Ordner) und gleicht die Vermerke ab.
        Rückgabe: {ziel_id: erfolgreich?}. Fehler der Ziele landen in 'failed'."""
        confirmed = {}   # Ziel-ID → True/False
        for tr in target_runs if done else []:
            self.checkpoint(job_id)
            target = targets.get(tr["id"])
            self._update(job_id, progress=75.0, message=f"Übergebe an {target.label} ...")
            run = None
            try:
                options = dict(tr, cancel_check=lambda: self.cancel_check(job_id))

                def waiting(pct, text):
                    self._update(job_id, message=text, **({"progress": round(75 + pct / 100.0 * 15, 1)} if pct is not None else {}))
                    self.checkpoint(job_id)   # beim Warten auf das Ziel: pausieren oder abbrechen möglich

                run = target.start([(n, new_folder[n]) for n in done], options, lambda msg: self._log(job_id, msg))
                result = target.wait(run, waiting)
                self._log(job_id, f"{target.label}: Status={result.status}")
                confirmed[target.id] = result.ok
                for name in (done if result.done is None and result.ok else result.done or []):
                    store.mark_verified(target.id, name, new_folder[name], ref=run.id)
                if result.ok:
                    target.after_success(run, lambda msg: self._log(job_id, msg))
                else:
                    self._log(job_id, f"WARNUNG: {target.label} meldet keinen Erfolg: {result.detail or result.status}")
                    failed.append((f"Ziel {target.label}", result.detail or result.status))
            except Cancelled:
                if run is not None:
                    target.cancel(run, lambda msg: self._log(job_id, msg))   # laufenden Lauf beim Ziel stoppen
                raise
            except TargetError as exc:
                confirmed[target.id] = False
                self._log(job_id, f"FEHLER {target.label}: {exc}")
                failed.append((f"Ziel {target.label}", str(exc)))
            except Exception as exc:   # unerwartet: nicht den ganzen Auftrag kippen, die übrigen Ziele kommen noch dran
                confirmed[target.id] = False
                self._log(job_id, f"FEHLER {target.label}: {describe_error(exc)}")
                applog.get("job").error("[%s] Unerwarteter Fehler bei %s", job_id, target.label,
                                        exc_info=(type(exc), exc, exc.__traceback__))
                failed.append((f"Ziel {target.label}", describe_error(exc)))

        if (retention or retention_days) and done:
            self._update(job_id, progress=92.0, message="Wende Aufbewahrung an ...")
            required = [tr["id"] for tr in target_runs] or [t.id for t in targets.all_targets() if t.ready()]
            for name in done:
                allowed = None
                try:
                    if require_cv:
                        allowed = confirmed_folders(name, required) if required else set()
                        if allowed is None:
                            self._log(job_id, f"[{name}] Aufbewahrung ausgesetzt: ein Backup-Ziel ist nicht erreichbar "
                                              "oder nicht eingerichtet, Löschen nur nach Bestätigung dort.")
                            continue
                    backup.apply_retention(name, retention, retention_days, allowed_folders=allowed,
                                           log_cb=lambda msg, name=name: self._log(job_id, f"[{name}] {msg}"))
                except Cancelled:
                    raise
                except Exception as exc:   # Aufräumen ist Nebensache: die Sicherungen selbst sind fertig
                    self._warn(job_id, f"[{name}] Aufbewahrung nicht angewendet", exc)

        for tid, ok in confirmed.items():   # Vermerke angleichen – dort abgelaufene Stände verschwinden auch hier
            if not ok:
                continue
            self._update(job_id, progress=96.0, message="Gleiche Vermerke ab ...")
            try:
                res = sync_target_state(tid)
            except Cancelled:
                raise
            except Exception as exc:
                self._warn(job_id, f"Abgleich mit {targets.get(tid).label} nicht möglich", exc)
                continue
            if res:
                self._log(job_id, f"Abgleich mit {targets.get(tid).label}: {res['folders']} Sicherungen vermerkt"
                                  + (f", {res['removed']} abgelaufene entfernt" if res["removed"] else ""))
        return confirmed

    def _run_backup_batch(self, job_id, names, mode, image_mode, retention, target_runs,
                          retention_days=0, fmt="tar", require_cv=False, stack_mode=True):
        """Sichert die Container nacheinander (Modus 'stop' soll nicht mehrere gleichzeitig stoppen –
        außer den Containern eines Compose-Stacks, die für einen gemeinsamen Zeitpunkt zusammen ruhen),
        übergibt sie danach an die gewählten Backup-Ziele und wendet erst dann die Aufbewahrung an."""
        total = len(names)
        failed, done, new_folder = [], [], {}
        image_cache = {}
        self._log(job_id, f"Starte Backup für {total} Container (Modus: {mode}, Format: {fmt}, Images: "
                          f"{ {'none': 'keine', 'own': 'nur selbst gebaute', 'all': 'alle'}[image_mode] }): "
                          f"{', '.join(names)}")
        position = 0
        cancelled = False
        cancel_check = lambda: self.cancel_check(job_id)   # noqa: E731
        for stack, members in self._groups(names, stack_mode):
            self.checkpoint(job_id)   # zwischen Containern/Stacks darf pausiert oder abgebrochen werden
            if stack:
                self._log(job_id, f"Stack '{stack}': {len(members)} Container gemeinsam – Start in dieser Reihenfolge: "
                                  f"{' → '.join(members)} (gestoppt wird umgekehrt)")
            runs = []
            with contextlib.ExitStack() as locks:
                for name in members:
                    index = position
                    position += 1

                    def progress(count_done, count, index=index, name=name):
                        frac = (count_done / count) if count else 0.0
                        self._update(job_id, progress=round((index + frac) / total * 70.0, 1),
                                     message=f"{name} ({index + 1}/{total}): Sichere Daten ({count_done}/{count})")

                    def log_cb(msg, name=name):
                        self._log(job_id, f"[{name}] {msg}")

                    try:
                        locks.enter_context(self.exclusive(name))
                    except BackupError as exc:
                        failed.append((name, str(exc)))
                        self._log(job_id, f"[{name}] FEHLER: {exc}")
                        continue
                    backup.cleanup_incomplete(0, name, log_cb)
                    runs.append(backup.BackupRun(name, mode, image_mode, fmt, log_cb, progress, run_id=job_id,
                                                 stack=stack, image_cache=image_cache, cancel_check=cancel_check))
                    self._update(job_id, progress=round(index / total * 70.0, 1), message=f"{name} ({index + 1}/{total})")

                # 1) vorbereiten (Hooks, Dumps) solange alles läuft
                for run in runs:
                    try:
                        run.prepare()
                    except Exception as exc:
                        run.error = exc
                # 2) stoppen – im Stack in umgekehrter Startreihenfolge
                for run in reversed(runs):
                    if run.error is None:
                        try:
                            self.cancel_check(job_id)   # vor dem Stoppen noch abbrechbar
                            run.stop()
                        except Exception as exc:
                            run.error = exc
                # 3) packen
                for run in runs:
                    if run.error is None:
                        try:
                            run.archive()
                        except Exception as exc:
                            run.error = exc
                # 4) starten in Startreihenfolge + Post-Hooks, danach Images und Manifeste (Container laufen wieder)
                for run in runs:
                    run.restart()
                for run in runs:
                    run.complete()
                    run.cleanup()
                for run in runs:
                    name = run.name
                    try:
                        result = run.result()
                        new_folder[name] = result["ts"]
                        done.append(name)
                        self._log(job_id, f"[{name}] OK")
                    except backup.PartialBackup as exc:
                        new_folder[name] = exc.ts
                        done.append(name)
                        failed.append((name, str(exc)))
                        self._log(job_id, f"[{name}] FEHLER (Sicherung liegt vor): {exc}")
                    except Cancelled:
                        cancelled = True
                        self._log(job_id, f"[{name}] abgebrochen – unvollständige Sicherung entfernt, Container läuft wieder")
                        continue
                    except Exception as exc:  # weiter mit dem nächsten Container
                        failed.append((name, str(exc)))
                        self._log(job_id, f"[{name}] FEHLER: {exc}")
                        continue
                    manifest = run.manifest or {}
                    store.record_run(job_id, name, new_folder[name], manifest.get("stack"), {
                        "mode": manifest.get("mode"), "dumps": [d["type"] for d in manifest.get("dumps") or [] if d.get("file")],
                        "image_saved": bool(manifest.get("image_data")), "service": manifest.get("service")})

            if cancelled:
                break
        if cancelled:
            self._log(job_id, f"Abgebrochen: {len(done)} von {total} Container sind lokal gesichert (nicht an Ziele übergeben).")
            raise Cancelled()
        self._update(job_id, progress=70.0, message=f"{len(done)} von {total} Backups erstellt")
        self.checkpoint(job_id)

        confirmed = self._deliver(job_id, target_runs, done, new_folder, retention, retention_days, require_cv, failed)

        if failed:
            own = [f for f in failed if not f[0].startswith("Ziel ")]
            raise BackupError((f"{len(own)} von {total} Backups fehlgeschlagen: " if own else "Übergabe fehlgeschlagen: ")
                              + "; ".join(f"{n} ({why[:120]})" for n, why in failed))
        return {"containers": names, "confirmed": confirmed, "cv_confirmed": confirmed.get("commvault", False)}

    # ───────── Restore / Klon ─────────
    def submit_restore(self, container_name, version=None, restore_bind_mounts=True,
                       restart_container=True, recreate_if_missing=True, use_cv=True, folder=None,
                       use_dump=False, members=None, target="original", clone=None, remove_clone=None, source=None):
        if source is None:   # ältere Aufrufer: use_cv=True heißt Commvault
            source = "commvault" if use_cv else "local"
        if source != "local":
            targets.get(source)
        params = {
            "container": container_name,
            "version": version,
            "folder": folder,
            "restore_bind_mounts": restore_bind_mounts,
            "restart_container": restart_container,
            "recreate_if_missing": recreate_if_missing,
            "use_cv": source != "local",
            "source": source,
            "use_dump": use_dump,
            "members": members,
            "target": target,
            "clone": clone,
            "remove_clone": remove_clone,
        }
        job = self._new("restore", params)
        self._start(job["id"], self._run_restore, params)
        return job

    def _run_restore(self, job_id, params):
        if params.get("target") == "clone":
            return self._run_clone(job_id, params)
        if params.get("members"):
            return self._run_restore_stack(job_id, params)
        with self.exclusive(params["container"]):
            result = self._run_restore_locked(job_id, params)
        if params.get("remove_clone"):
            backup.remove_clone(params["remove_clone"], lambda msg: self._log(job_id, msg))
        return result

    def _fetch_backup_dir(self, job_id, container_name, version, folder, source, progress_offset=5, progress_scale=60):
        """Liefert (verzeichnis, staging_zum_aufräumen) – aus einem Ziel ins Staging oder lokal.
        'source': 'local' oder die ID eines Ziels (True/'cv' aus älteren Aufrufen heißt Commvault)."""
        def log_cb(msg):
            self._log(job_id, msg)

        if source in (True, "cv"):
            source = "commvault"
        if source and source != "local":
            target = targets.get(source)
            staging_target = os.path.join(config.staging_root, f"restore-{job_id}-{backup.safe_name(container_name)}")
            os.makedirs(staging_target, exist_ok=True)
            self._update(job_id, progress=progress_offset, message=f"{container_name}: Restore aus {target.label} ...")

            def progress(pct, text):
                if pct is not None:
                    self._update(job_id, progress=round(progress_offset + pct / 100.0 * progress_scale, 1), message=text)
                self.checkpoint(job_id)

            try:
                target.fetch(container_name, folder, staging_target, version=version, progress_cb=progress, log_cb=log_cb)
            except BaseException:
                shutil.rmtree(staging_target, ignore_errors=True)   # abgebrochen oder fehlgeschlagen: nichts liegen lassen
                raise

            found = backup.scan_staging(staging_target)
            if not found:
                raise BackupError(f"Keine Backup-Daten im Staging-Verzeichnis gefunden: {staging_target}")
            chosen = next((e for e in found if e["container"] == container_name), None)
            if chosen is None:
                chosen = found[0]
                log_cb(f"Hinweis: Manifest für '{chosen['container']}' statt '{container_name}' gefunden.")
            log_cb(f"[{container_name}] Backup-Set gefunden: {chosen['dir']}")
            return chosen["dir"], staging_target

        local = backup.list_local_backups(container_name)
        if not local:
            raise BackupError(f"Keine lokale Sicherung für '{container_name}' gefunden.")
        wanted = folder or (version if version and version != "latest" else None)
        if wanted:
            if not any(b["ts"] == wanted for b in local):
                raise BackupError(f"Lokale Version '{wanted}' nicht gefunden.")
        else:
            wanted = local[0]["ts"]
        backup_dir = os.path.join(config.backup_root, backup.safe_name(container_name), wanted)
        log_cb(f"[{container_name}] Verwende lokale Sicherung: {backup_dir}")
        return backup_dir, None

    def _run_restore_locked(self, job_id, params):
        def log_cb(msg):
            self._log(job_id, msg)

        container_name = params["container"]
        version = params.get("version")
        log_cb(f"Starte Restore für Container '{container_name}'"
               + (f" (Stand {params.get('folder') or version})" if (params.get("folder") or version) else " (neueste Version)"))
        backup_dir, staging = self._fetch_backup_dir(job_id, container_name, version, params.get("folder"),
                                                     _source_of(params))
        try:
            self.checkpoint(job_id)   # letzte Möglichkeit: danach wird in Volumes geschrieben
        except Cancelled:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            raise
        self.lock(job_id)

        def progress(done, total):
            if total:
                self._update(job_id, progress=round(65 + done / total * 30.0, 1),
                             message=f"Stelle Daten wieder her ({done}/{total})")

        try:
            result = backup.restore_from_backup(
                container_name,
                backup_dir,
                restore_bind_mounts=params["restore_bind_mounts"],
                restart_container=params["restart_container"],
                recreate_if_missing=params["recreate_if_missing"],
                progress_cb=progress,
                log_cb=log_cb,
                use_dump=bool(params.get("use_dump")),
            )
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
                log_cb("Staging-Verzeichnis aufgeräumt.")
        return result

    def _run_restore_stack(self, job_id, params):
        """Mehrere Container (z. B. ein Compose-Stack) auf denselben Backup-Lauf zurücksetzen: erst alle
        Sicherungen holen, dann gemeinsam stoppen, zurückspielen und in Startreihenfolge wieder starten."""
        members = params["members"]
        names = [m["container"] for m in members]
        self._log(job_id, f"Stack-Restore für {len(members)} Container: {', '.join(names)}")
        staging_dirs, dirs, items = [], {}, []
        with contextlib.ExitStack() as locks:
            for name in names:
                locks.enter_context(self.exclusive(name))
            try:
                for index, member in enumerate(members):
                    span = 50.0 / len(members)
                    backup_dir, staging = self._fetch_backup_dir(
                        job_id, member["container"], None, member["folder"], member.get("source"),
                        progress_offset=round(5 + index * span, 1), progress_scale=span)
                    if staging:
                        staging_dirs.append(staging)
                    dirs[member["container"]] = backup_dir
                    _manifest, snapshot, _d = backup.load_backup(backup_dir)
                    info = docker_api.compose_info(snapshot.get("labels"))
                    items.append({"name": member["container"], "service": info["service"],
                                  "depends_on": info["depends_on"], "image": snapshot.get("image"),
                                  "env_keys": [str(e).split("=", 1)[0] for e in snapshot.get("env") or []]})
                order = backup.start_order(items)
                self._log(job_id, f"Startreihenfolge: {' → '.join(order)}")
                self.checkpoint(job_id)   # letzte Möglichkeit: danach werden Container gestoppt
                self.lock(job_id)

                running = {}
                for name in reversed(order):
                    running[name] = docker_api.container_running(name)
                    if running[name]:
                        if docker_api.is_self(name):
                            raise BackupError("Der Helper selbst kann nicht Teil eines Stack-Restores sein.")
                        self._log(job_id, f"[{name}] Stoppe ...")
                        docker_api.stop_container(name)
                results = {}
                try:
                    for index, name in enumerate(order):
                        self._update(job_id, progress=round(55 + index / len(order) * 30.0, 1),
                                     message=f"{name}: Stelle Daten wieder her")
                        results[name] = backup.restore_from_backup(
                            name, dirs[name], restore_bind_mounts=params["restore_bind_mounts"], restart_container=False,
                            recreate_if_missing=params["recreate_if_missing"], use_dump=bool(params.get("use_dump")),
                            manage_lifecycle=False, log_cb=lambda msg, name=name: self._log(job_id, f"[{name}] {msg}"))
                except Exception:
                    # nichts gestoppt zurücklassen: zuvor laufende Container wieder starten
                    for name in order:
                        if running.get(name) and not docker_api.container_running(name):
                            try:
                                docker_api.start_container(name)
                                self._log(job_id, f"[{name}] nach Fehler wieder gestartet")
                            except Exception as exc:
                                self._log(job_id, f"[{name}] FEHLER beim Wiederstarten: {exc}")
                    raise
                if params["restart_container"]:
                    for index, name in enumerate(order):
                        self._update(job_id, progress=round(85 + index / len(order) * 14.0, 1), message=f"{name}: Start")
                        res = results[name]
                        log = (lambda msg, name=name: self._log(job_id, f"[{name}] {msg}"))
                        if running.get(name) or res["created"] or res["pending_dump"]:
                            log("Starte ...")
                            docker_api.start_container(name)
                        if res["pending_dump"]:
                            backup.import_dump(name, res["pending_dump"]["kind"], res["pending_dump"]["path"], log)
                else:
                    self._log(job_id, "Container bleiben gestoppt (so gewählt).")
            finally:
                for staging in staging_dirs:
                    shutil.rmtree(staging, ignore_errors=True)
        return {"containers": names}

    def _run_clone(self, job_id, params):
        opts = params.get("clone") or {}
        name = params["container"]
        self._log(job_id, f"Restore als Klon: '{name}' → '{opts.get('name')}' (das Original bleibt unberührt)")
        backup_dir, staging = self._fetch_backup_dir(job_id, name, params.get("version"), params.get("folder"),
                                                     _source_of(params))
        try:
            self.checkpoint(job_id)
        except Cancelled:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            raise
        self.lock(job_id)
        src = _source_of(params)
        source = {"kind": "local" if src == "local" else "cv", "target": src, "folder": params.get("folder")
                  or os.path.basename(backup_dir.rstrip("/")), "version": params.get("version")}

        def progress(done, total):
            if total:
                self._update(job_id, progress=round(65 + done / total * 30.0, 1),
                             message=f"Klon: Daten ({done}/{total})")

        try:
            return backup.clone_from_backup(
                name, backup_dir, opts.get("name"), network=opts.get("network") or "none",
                host_port=opts.get("host_port") or None, start=opts.get("start", True),
                attach_skipped=opts.get("attach_skipped", True), source=source,
                progress_cb=progress, log_cb=lambda msg: self._log(job_id, msg))
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)

    def list_jobs(self):
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j["started"], reverse=True)
        return jobs

    def get_job(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)


jobs = JobManager()
