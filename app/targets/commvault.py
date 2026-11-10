"""Ziel „Commvault“ (pull): der Subclient sichert BACKUP_ROOT, der Helper stößt ihn per REST an und liest zurück."""
import os
import re
import time

from .. import rules
from ..config import config
from . import commvault_content
from .base import (DIRECT_PATHS, FETCH, SYNTHETIC_FULL, TRIGGER, VERSIONS, Run, RunResult, Target, TargetError,
                   secret_state, yes_no)
from .commvault_api import Commvault, CVError, terminal_job_status

OK_STATUS = ("Completed", "Completed w/ one or more warnings")


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _parse_ts(ts):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", ts or ""))


class CommvaultTarget(Target):
    id = "commvault"
    label = "Commvault"
    kind = "pull"
    capabilities = frozenset({TRIGGER, VERSIONS, FETCH, DIRECT_PATHS, SYNTHETIC_FULL})

    def _client(self):
        cv = Commvault()
        if not cv.configured:
            raise TargetError("Commvault-Zugangsdaten fehlen (CV_URL, CV_USERNAME, CV_PASSWORD).")
        return cv

    def _ready(self):
        """(client, identity) – wirft TargetError, wenn Client/Subclient nicht gewählt sind."""
        cv = self._client()
        identity = config.effective_cv_identity()
        if not all([identity.get("client_id"), identity.get("subclient_id"), identity.get("app_type_id")]):
            raise TargetError("Commvault: Client/Subclient ist nicht gewählt (Einstellungen).")
        return cv, identity

    def configured(self):
        return Commvault().configured

    def ready(self):
        identity = config.effective_cv_identity()
        return self.configured() and all([identity.get("client_id"), identity.get("subclient_id"),
                                          identity.get("app_type_id")])

    def info(self):
        ident = config.effective_cv_identity()
        return [("WebServer-URL", config.cv_url or "nicht gesetzt"), ("Webservice-Pfad", config.cv_webservice_path),
                ("Benutzer", config.cv_username or "nicht gesetzt"), ("Passwort", secret_state(config.cv_password)),
                ("TLS prüfen", yes_no(config.cv_verify_tls)),
                ("Client", ident.get("client_name") or "nicht gewählt"),
                ("Subclient", ident.get("subclient_name") or "nicht gewählt")]

    def test(self):
        try:
            return self._client().test_connection()
        except CVError as exc:
            raise TargetError(str(exc))

    # ───────── Lauf ─────────
    def start(self, items, options, log_cb):
        cv, identity = self._ready()
        try:
            response = cv.start_backup(identity["subclient_id"], options.get("level") or 2)
        except CVError as exc:
            raise TargetError(str(exc))
        log_cb(f"Commvault-Backup angenommen: {response}")
        ids = response.get("jobIds") or ([response["jobId"]] if response.get("jobId") else [])
        if not ids:
            log_cb("Hinweis: Commvault hat keine Job-ID geliefert – es wird nicht auf das Ende gewartet.")
        return Run(id=str(ids[0]) if ids else None, raw=response)

    def wait(self, run, progress_cb=None, max_wait=7200):
        if not run.id:
            return RunResult(False, "unbekannt", "keine Job-ID – Ende nicht überwachbar")
        cv = self._client()
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                summary = cv.get_job(run.id)
            except CVError as exc:
                if progress_cb:
                    progress_cb(None, f"Job-Status-Abfrage fehlgeschlagen: {exc}")
                time.sleep(10)
                continue
            if summary:
                status = summary.get("status") or ""
                try:
                    percent = float(summary.get("percentComplete") or 0.0)
                except (TypeError, ValueError):
                    percent = 0.0
                if progress_cb:
                    progress_cb(percent, f"Commvault: {status} ({percent:.0f}%)")
                if terminal_job_status(status):
                    return RunResult(status in OK_STATUS, status,
                                     str(summary.get("pendingReason") or "") if status not in OK_STATUS else "")
            time.sleep(5)
        raise TargetError(f"Zeitüberschreitung beim Warten auf Commvault-Job {run.id}.")

    def cancel(self, run, log_cb):
        if not run or not run.id:
            return
        try:
            self._client().kill_job(run.id)
            log_cb(f"Commvault-Job {run.id} wurde beendet.")
        except (CVError, TargetError) as exc:
            log_cb(f"Hinweis: Commvault-Job {run.id} konnte nicht beendet werden ({exc}) – bitte in Commvault prüfen.")

    def after_success(self, run, log_cb):
        """Synthetic Full, wenn der letzte Full älter als der eingestellte Abstand ist – ohne Fulls schließt
        Commvault keinen Zyklus ab und die Aufbewahrung greift nie."""
        days = rules.get_rules().get("cv_synth_full_days") or 0
        if not days:
            return
        try:
            cv, identity = self._ready()
            last = cv.last_full_time(identity)
            if last and time.time() - last < days * 86400 - 3 * 3600:
                log_cb(f"Letzter Full vor {(time.time() - last) / 86400:.1f} Tagen – kein Synthetic Full nötig "
                       f"(Abstand {days} Tage)")
                return
            log_cb("Starte Synthetic Full in Commvault (" + (
                f"letzter Full vor {(time.time() - last) / 86400:.1f} Tagen" if last else "noch kein Full gefunden") + ") ...")
            response = cv.start_backup(identity["subclient_id"], 4)
            ids = response.get("jobIds") or ([response["jobId"]] if response.get("jobId") else [])
            if not ids:
                log_cb(f"Hinweis: Synthetic Full ohne Job-ID angenommen: {response}")
                return
            result = self.wait(Run(id=str(ids[0])))
            log_cb(f"Synthetic Full {ids[0]}: Status={result.status}")
        except (CVError, TargetError) as exc:
            log_cb(f"WARNUNG: Synthetic Full nicht möglich: {exc}")

    # ───────── Stände ─────────
    def list_folders(self, container):
        try:
            cv, identity = self._ready()
            rows = cv.browse_versions(identity, f"{config.backup_root.rstrip('/')}/{_safe_name(container)}",
                                      op_type=0, show_deleted=True)
        except (CVError, TargetError):
            return None
        found = {}
        for row in rows:
            if row.get("directory") and row.get("name") and _parse_ts(row["name"]):
                found[row["name"]] = {"time": row.get("backup_time") or row.get("modification_time") or time.time(),
                                      "size": int(row.get("size") or 0), "ref": row.get("backup_job_id"),
                                      "deleted_locally": bool(row.get("deleted"))}
        return found

    def list_containers(self):
        try:
            cv, identity = self._ready()
            rows = cv.browse_versions(identity, config.backup_root.rstrip("/"), op_type=0, show_deleted=True)
        except (CVError, TargetError):
            return None
        return sorted({r["name"] for r in rows if r.get("directory") and r.get("name") and not r["name"].startswith(".")})

    # ───────── Restore ─────────
    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        log_cb = log_cb or (lambda msg: None)
        cv, identity = self._ready()
        path = f"{config.backup_root.rstrip('/')}/{_safe_name(container)}"
        if folder:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", folder) or folder in (".", ".."):
                raise TargetError(f"Ungültiger Sicherungsordner: {folder}")
            path = f"{path}/{folder}"
            log_cb(f"[{container}] Hole Sicherungsstand '{folder}' aus Commvault.")
        os.makedirs(dest, exist_ok=True)
        to_time = int(float(version)) if version and version != "latest" and not folder else None
        try:
            job = cv.restore(identity, path, dest, to_time=to_time)
        except CVError as exc:
            raise TargetError(str(exc))
        log_cb(f"[{container}] Commvault-Restore-Job {job} gestartet.")
        result = self.wait(Run(id=str(job)), progress_cb)
        log_cb(f"[{container}] Commvault-Restore: Status={result.status}")
        if result.status == "Failed":
            raise TargetError(f"Commvault-Restore fehlgeschlagen: {result.detail or result.status}")
        if "Completed" not in result.status and result.status != "Committed":
            raise TargetError(f"Commvault-Restore endete mit Status '{result.status}'.")

    # ───────── Direkt-Pfade ─────────
    def paths_status(self):
        try:
            return commvault_content.public(commvault_content.status())
        except CVError as exc:
            raise TargetError(str(exc))

    def paths_sync(self, rule_set=None):
        try:
            return commvault_content.public(commvault_content.sync(rule_set))
        except CVError as exc:
            raise TargetError(str(exc))
