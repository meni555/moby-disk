"""Ziel „Veeam“ (pull): ein Job in Veeam Backup & Replication sichert BACKUP_ROOT, der Helper stößt ihn per REST an.

Voraussetzung ist ein Backup-Job im Veeam-Server (z. B. ein Agent-Job für Linux-Dateien), der `BACKUP_ROOT` des
Docker-Hosts sichert, und ein Benutzer mit dem Recht, ihn zu starten und Restore-Punkte zu lesen. Der Helper
verwendet die REST-API von Veeam Backup & Replication (Standard-Port 9419).

Einrichtung nur über die .env:
  VEEAM_URL          z. B. https://vbr.example.com:9419
  VEEAM_USERNAME / VEEAM_PASSWORD
  VEEAM_JOB          Name (oder ID) des Jobs, der BACKUP_ROOT sichert
  VEEAM_VERIFY_TLS   Standard false (Veeam installiert ein selbstsigniertes Zertifikat)
  VEEAM_API_VERSION  Leer = automatisch die höchste Version, die der Server anbietet (1.3-rev1, 1.2-rev0, sonst 1.1-rev0).
                     Wichtig: Datei- und Agent-Jobs kennt die API erst ab 1.3 – mit 1.1 sieht man nur VM-Jobs.

Was das Ziel kann und was nicht: Es startet den Job (voll oder inkrementell), wartet auf das Ende und merkt sich die
Sicherung als bestätigt. Welche Container-Ordner in einem Restore-Punkt stecken, liefert die REST-API nicht – ein Ordner
gilt daher als gesichert, wenn ein erfolgreicher Lauf nach seiner Entstehung stattfand, und der Vermerk verfällt, wenn
kein Restore-Punkt mehr aus der Zeit zwischen Entstehung und lokalem Löschen existiert. Restore: die Dateien in Veeam
nach `BACKUP_ROOT` zurückholen (Wiederherstellen → Dateien → ursprünglicher Ort); danach erscheint die Sicherung als
lokal und lässt sich wie jede andere wiederherstellen.
"""
import datetime
import os
import re
import time

import requests

from .. import applog, backup
from .base import STATE_CHECK, TRIGGER, Run, RunResult, Target, TargetError, secret_state, yes_no

UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _epoch(value):
    try:
        value = re.sub(r"(\.\d{6})\d+", r"\1", str(value).replace("Z", "+00:00"))
        stamp = datetime.datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError):
        return None


def _login_hint(username):
    """Veeam verlangt für die Anmeldung per REST einen Benutzernamen mit Domäne bzw. Rechnernamen."""
    if "\\" in username or "@" in username:
        return ""
    return (" Hinweis: VEEAM_USERNAME enthält keine Domäne – Veeam erwartet DOMÄNE\\Benutzer, Benutzer@domäne oder bei einem "
            "lokalen Konto RECHNERNAME\\Benutzer.")


class VeeamTarget(Target):
    id = "veeam"
    label = "Veeam"
    kind = "pull"
    capabilities = frozenset({TRIGGER, STATE_CHECK})

    # Nach einer abgelehnten Anmeldung wird nicht sofort erneut probiert: Veeam sperrt Konten nach wenigen Fehlversuchen
    # („Too many attempts“) – das wäre schlimmer als der ursprüngliche Fehler. Gilt je Prozess und Zugangsdaten.
    _denied = {}
    DENIED_PAUSE = 300
    API_VERSIONS = ("1.3-rev1", "1.2-rev0")      # darunter gilt 1.1-rev0 (VBR 12)
    _versions = {}

    def __init__(self):
        self._token = None
        self._token_time = 0.0
        self._session = requests.Session()
        self._session.verify = self._verify()
        if not self._session.verify:
            requests.packages.urllib3.disable_warnings()

    # ───────── Einrichtung ─────────
    @staticmethod
    def _verify():
        return os.environ.get("VEEAM_VERIFY_TLS", "").strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _base():
        return os.environ.get("VEEAM_URL", "").strip().rstrip("/")

    def missing(self):
        env = os.environ
        return [n for n, ok in (("VEEAM_URL", self._base()), ("VEEAM_USERNAME", env.get("VEEAM_USERNAME")),
                                ("VEEAM_PASSWORD", env.get("VEEAM_PASSWORD")), ("VEEAM_JOB", env.get("VEEAM_JOB", "").strip())) if not ok]

    def configured(self):
        env = os.environ
        return bool(self._base() and env.get("VEEAM_USERNAME") and env.get("VEEAM_PASSWORD") and env.get("VEEAM_JOB", "").strip())

    def info(self):
        env = os.environ
        return [("Server-URL", self._base() or "nicht gesetzt"), ("Benutzer", env.get("VEEAM_USERNAME") or "nicht gesetzt"),
                ("Passwort", secret_state(env.get("VEEAM_PASSWORD"))), ("Job", env.get("VEEAM_JOB", "").strip() or "nicht gesetzt"),
                ("API-Version", env.get("VEEAM_API_VERSION", "").strip() or "automatisch (höchste des Servers)"),
                ("TLS prüfen", yes_no(self._verify()))]

    # ───────── HTTP ─────────
    def _api_version(self):
        wanted = os.environ.get("VEEAM_API_VERSION", "").strip()
        if wanted:
            return wanted
        base = self._base()
        if base not in self._versions:
            found = "1.1-rev0"
            for version in self.API_VERSIONS:
                try:
                    resp = self._session.get(f"{base}/swagger/v{version}/swagger.json", stream=True, timeout=10)
                    resp.close()
                except requests.exceptions.RequestException:
                    return found                  # nicht erreichbar: nicht merken, die eigentliche Anfrage meldet es
                if resp.status_code == 200:
                    found = version
                    break
            self._versions[base] = found
        return self._versions[base]

    def _headers(self, auth=True):
        headers = {"x-api-version": self._api_version(), "Accept": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _cred_key():
        import hashlib
        env = os.environ
        return hashlib.sha256(f"{env.get('VEEAM_URL')}|{env.get('VEEAM_USERNAME')}|{env.get('VEEAM_PASSWORD')}".encode()).hexdigest()

    def _login(self):
        key = self._cred_key()
        until, message = self._denied.get(key, (0, ""))
        if time.time() < until:
            raise TargetError(f"{message} (erneuter Versuch frühestens in {int((until - time.time()) // 60) + 1} Min. – "
                              "damit Veeam das Konto nicht sperrt)")
        log = applog.get("target.veeam")
        started = time.time()
        try:
            resp = self._session.post(self._base() + "/api/oauth2/token", headers=self._headers(auth=False), timeout=30,
                                      data={"grant_type": "password", "username": os.environ["VEEAM_USERNAME"],
                                            "password": os.environ["VEEAM_PASSWORD"]})
        except requests.exceptions.RequestException as exc:
            log.warning("Anmeldung: %s nicht erreichbar (%s)", self._base(), exc.__class__.__name__)
            raise TargetError(f"Veeam nicht erreichbar ({self._base()}): {exc.__class__.__name__}")
        if resp.status_code != 200:
            message = f"Veeam-Anmeldung fehlgeschlagen (HTTP {resp.status_code}): {self._why(resp)}"
            log.warning("Anmeldung als '%s' abgelehnt (HTTP %d, %.0f ms): %s", os.environ.get("VEEAM_USERNAME"), resp.status_code,
                        (time.time() - started) * 1000, self._why(resp))
            if resp.status_code in (400, 401, 403):
                message += _login_hint(os.environ.get("VEEAM_USERNAME", ""))
                self._denied[key] = (time.time() + self.DENIED_PAUSE, message)
            raise TargetError(message)
        self._denied.pop(key, None)
        self._token = resp.json().get("access_token")
        self._token_time = time.time()
        log.info("Angemeldet als '%s' (API %s, %.0f ms)", os.environ.get("VEEAM_USERNAME"), self._api_version(),
                 (time.time() - started) * 1000)
        if not self._token:
            raise TargetError("Veeam hat kein Zugriffstoken geliefert.")

    @staticmethod
    def _why(resp):
        try:
            body = resp.json()
            return str(body.get("message") or body.get("error_description") or body)[:300]
        except ValueError:
            return (resp.text or "")[:200]

    def _call(self, method, path, params=None, body=None):
        if self.missing() and self.missing() != ["VEEAM_JOB"]:
            raise TargetError("Veeam ist nicht vollständig eingerichtet – es fehlt: " + ", ".join(self.missing()) + ".")
        for attempt in (1, 2):
            if not self._token or time.time() - self._token_time > 600:
                self._login()
            log = applog.get("target.veeam")
            started = time.time()
            try:
                resp = self._session.request(method, self._base() + path, headers=self._headers(), params=params,
                                             json=body, timeout=60)
            except requests.exceptions.RequestException as exc:
                log.warning("%s %s: nicht erreichbar (%s)", method, path, exc.__class__.__name__)
                raise TargetError(f"Veeam nicht erreichbar: {exc.__class__.__name__}")
            log.debug("%s %s → %d (%.0f ms)", method, path, resp.status_code, (time.time() - started) * 1000)
            if resp.status_code == 401 and attempt == 1:   # Token abgelaufen
                log.info("Token abgelaufen – melde mich neu an")
                self._token = None
                continue
            if resp.status_code >= 400:
                log.warning("%s %s → %d: %s", method, path, resp.status_code, self._why(resp))
                raise TargetError(f"Veeam-API {method} {path}: HTTP {resp.status_code} – {self._why(resp)}")
            return resp.json() if resp.content else {}

    # ───────── Job und Restore-Punkte ─────────
    def _states(self, **params):
        """Alle Jobs mit Zustand – anders als /jobs auch Datei-, Server- und Arbeitsplatz-Jobs (Agent, Dateifreigabe)."""
        rows = self._call("GET", "/api/v1/jobs/states", params=dict(params, limit=200)).get("data") or []
        return [dict(r, isDisabled=r.get("status") == "disabled") for r in rows]

    def _names(self, path):
        """Namen aus einer Listen-Abfrage – nur für Fehlermeldungen, ein Fehler dabei bleibt still."""
        try:
            rows = self._call("GET", path, params={"limit": 20}).get("data") or []
            return ", ".join(sorted(str(r.get("name") or r.get("hostName") or r.get("id")) for r in rows)) or "keine"
        except TargetError:
            return "nicht abrufbar"

    def choices(self, env):
        """Jobs für das Dropdown in den Einstellungen: alle, die die API über /jobs oder /jobs/states liefert."""
        if env != "VEEAM_JOB":
            return super().choices(env)
        env_ = os.environ
        if not all([self._base(), env_.get("VEEAM_USERNAME"), env_.get("VEEAM_PASSWORD")]):
            raise TargetError("Zuerst URL, Benutzer und Passwort eintragen und speichern.")
        seen = {}
        for rows in (self._call("GET", "/api/v1/jobs", params={"limit": 200}).get("data") or [], self._states()):
            for job in rows:
                seen.setdefault(job.get("id") or job.get("name"), job)
        return [{"value": j.get("name"), "label": f"{j.get('name')} ({j.get('type', '?')}" + (f"/{j['workload']}" if j.get("workload") else "") + ")"}
                for j in sorted(seen.values(), key=lambda j: (j.get("name") or "").lower()) if j.get("name")]

    def _job(self):
        wanted = os.environ["VEEAM_JOB"].strip()
        if UUID.match(wanted):
            try:
                return self._call("GET", f"/api/v1/jobs/{wanted}")
            except TargetError:
                found = self._states(idFilter=wanted)
                if found:
                    return found[0]
                raise
        found = self._call("GET", "/api/v1/jobs", params={"nameFilter": wanted}).get("data") or []
        exact = [j for j in found if (j.get("name") or "").lower() == wanted.lower()]
        if not exact:
            exact = [j for j in self._states(nameFilter=wanted) if (j.get("name") or "").lower() == wanted.lower()]
        if len(exact) != 1:
            if exact:
                raise TargetError(f"Veeam-Job '{wanted}' ist nicht eindeutig – bitte die Job-ID in VEEAM_JOB eintragen.")
            try:
                known = ", ".join(f"{j.get('name', '?')} ({j.get('type', '?')}/{j.get('workload', '?')})"
                                  for j in sorted(self._states(), key=lambda j: j.get("name", ""))[:15]) or "keine"
            except TargetError:
                known = "nicht abrufbar"
            raise TargetError(f"Veeam-Job '{wanted}' nicht gefunden. Die API kennt diese Jobs: {known}. "
                              f"Vorhandene Backups: {self._names('/api/v1/backups')}. "
                              f"Dateiserver: {self._names('/api/v1/inventory/unstructuredDataServers')}.")
        return exact[0]

    def _restore_point_times(self, job):
        """Zeitpunkte aller Restore-Punkte des Jobs (epoch), neueste zuletzt."""
        backups = self._call("GET", "/api/v1/backups", params={"nameFilter": job.get("name")}).get("data") or []
        ids = [b["id"] for b in backups if (b.get("name") or "").lower().startswith((job.get("name") or "").lower())
               or b.get("jobId") == job.get("id")]
        times = []
        for backup_id in ids:
            points = self._call("GET", "/api/v1/restorePoints", params={"backupIdFilter": backup_id, "limit": 1000}).get("data") or []
            times += [t for t in (_epoch(p.get("creationTime")) for p in points) if t]
        return sorted(times)

    def test(self):
        if not self.configured():
            raise TargetError("Veeam ist nicht vollständig eingerichtet – es fehlt: " + ", ".join(self.missing())
                              + ". (Die Anmeldung wurde noch nicht versucht.)")
        job = self._job()
        info = {"job": job.get("name"), "typ": job.get("type")}
        if job.get("isDisabled"):
            raise TargetError(f"Der Veeam-Job '{job.get('name')}' ist deaktiviert.")
        try:
            server = self._call("GET", "/api/v1/serverInfo")
            info["server"] = f"{server.get('name', '?')} {server.get('buildVersion', '')}".strip()
        except TargetError:
            pass
        info["restore_points"] = len(self._restore_point_times(job))
        return info

    # ───────── Lauf ─────────
    def start(self, items, options, log_cb):
        job = self._job()
        if job.get("isDisabled"):
            raise TargetError(f"Der Veeam-Job '{job.get('name')}' ist deaktiviert.")
        full = options.get("level") == 1
        session = self._call("POST", f"/api/v1/jobs/{job['id']}/start", body={"performActiveFull": full})
        log_cb(f"Veeam-Job '{job.get('name')}' gestartet ({'aktiver Full' if full else 'inkrementell'}), Sitzung {session.get('id')}")
        return Run(id=session.get("id"), raw=session, info={"job": job})

    def wait(self, run, progress_cb=None, max_wait=7200):
        if not run.id:
            return RunResult(False, "unbekannt", "keine Sitzungs-ID von Veeam")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            session = self._call("GET", f"/api/v1/sessions/{run.id}")
            state = session.get("state") or ""
            percent = float(session.get("progressPercent") or 0)
            if progress_cb:
                progress_cb(percent, f"Veeam: {state} ({percent:.0f}%)")
            if state == "Stopped":
                result = (session.get("result") or {})
                outcome = result.get("result") or "Unknown"
                ok = outcome in ("Success", "Warning")
                return RunResult(ok, outcome + (" (Warnungen)" if outcome == "Warning" else ""),
                                 "" if ok else str(result.get("message") or "")[:300])
            time.sleep(int(os.environ.get("VEEAM_POLL_SECONDS") or 10))
        raise TargetError(f"Zeitüberschreitung beim Warten auf die Veeam-Sitzung {run.id}.")

    def cancel(self, run, log_cb):
        if not run or not run.id:
            return
        try:
            self._call("POST", f"/api/v1/sessions/{run.id}/stop")
            log_cb(f"Veeam-Sitzung {run.id} wurde angehalten.")
        except TargetError as exc:
            log_cb(f"Hinweis: Veeam-Sitzung {run.id} konnte nicht angehalten werden ({exc}) – bitte in Veeam prüfen.")

    # ───────── Vermerke ─────────
    def validate_entries(self, entries):
        """Welche Vermerke sind noch durch einen Restore-Punkt gedeckt? Rückgabe: Menge (container, ordner) oder
        None, wenn Veeam nicht antwortet (dann bleibt alles stehen)."""
        try:
            points = self._restore_point_times(self._job())
        except TargetError:
            return None
        keep = set()
        for e in entries:
            parsed = backup._parse_ts(e["folder"])
            created = parsed.timestamp() if parsed else e["verified"]
            until = e.get("local_deleted")   # danach steckt der Ordner in keinem neuen Restore-Punkt mehr
            if any(p >= created and (until is None or p <= until + 60) for p in points):
                keep.add((e["container"], e["folder"]))
        return keep
