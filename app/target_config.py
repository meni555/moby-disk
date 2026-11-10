"""Einrichtung der Backup-Ziele im Webinterface.

Die Werte liegen in DATA_DIR/target-config.json (Rechte 600, außerhalb der Datenbank, damit sie nicht in deren
Sicherungen landen) und überstehen damit jedes Neuerstellen des Containers. Beim Start und nach jedem Speichern werden sie
in die Umgebung des Prozesses übernommen; die Ziele lesen ihre Einstellungen weiterhin aus den Umgebungsvariablen.

Vorrang: Wert im Webinterface vor .env. Wird ein Wert im Webinterface geleert, gilt wieder der Wert aus der .env.
Zugangsdaten werden von der Oberfläche nie zurückgegeben, nur ob sie gesetzt sind.
"""
import json
import os
import threading

from . import applog
from .config import DATA_DIR

FILE = os.path.join(DATA_DIR, "target-config.json")
_lock = threading.RLock()


def _f(env, label, kind="text", help="", placeholder="", options=None, remote=False, url=False):
    return {"env": env, "label": label, "type": kind, "help": help, "placeholder": placeholder,
            "options": options or [], "remote": remote, "url": url}


BOOL_HELP = "Leer = Standard"
FIELDS = {
    "commvault": [
        _f("CV_URL", "WebServer-URL", help="CommServe/WebConsole-Host, z. B. https://commserve.example.com", url=True),
        _f("CV_WEBSERVICE_PATH", "Webservice-Pfad", placeholder="/SearchSvc/CVWebService.svc"),
        _f("CV_USERNAME", "Benutzer"),
        _f("CV_PASSWORD", "Passwort", "secret"),
        _f("CV_VERIFY_TLS", "TLS-Zertifikat prüfen", "bool", help="Bei selbstsignierten Zertifikaten aus"),
    ],
    "veeam": [
        _f("VEEAM_URL", "Server-URL", help="REST-API des Backup-Servers, Port 9419, z. B. https://vbr.example.com:9419", url=True),
        _f("VEEAM_USERNAME", "Benutzer", help="Meist mit Domäne bzw. Rechnernamen: DOMÄNE\\benutzer, benutzer@domain oder RECHNERNAME\\benutzer"),
        _f("VEEAM_PASSWORD", "Passwort", "secret"),
        _f("VEEAM_JOB", "Job", "select", help="Job, der den Backup-Ordner sichert. „Jobs laden“ fragt Veeam mit den gespeicherten Zugangsdaten ab.",
           remote=True),
        _f("VEEAM_VERIFY_TLS", "TLS-Zertifikat prüfen", "bool", help="Veeam installiert ein selbstsigniertes Zertifikat – meist aus"),
        _f("VEEAM_API_VERSION", "API-Version", help="Leer = automatisch die höchste Version des Servers (Datei- und Agent-Jobs gibt es ab 1.3)",
           placeholder="automatisch"),
    ],
    "pbs": [
        _f("PBS_REPOSITORY", "Repository", help="Benutzer@Realm!Token@Server:Port:Datastore",
           placeholder="backup@pbs!moby@pbs.example.com:8007:docker"),
        _f("PBS_PASSWORD", "Geheimnis des API-Tokens", "secret"),
        _f("PBS_FINGERPRINT", "Fingerabdruck", help="SHA-256 des Server-Zertifikats, nötig bei selbstsigniertem Zertifikat"),
        _f("PBS_NAMESPACE", "Namespace", help="Optional"),
        _f("PBS_KEYFILE", "Schlüsseldatei", help="Optional: clientseitige Verschlüsselung, z. B. /data/pbs.key"),
        _f("PBS_ENCRYPTION_PASSWORD", "Passwort der Schlüsseldatei", "secret"),
        _f("PBS_KEEP", "Aufbewahrung", help="Je Container, z. B. --keep-last 14 --keep-monthly 6. Leer = nichts löschen"),
    ],
    "restic": [
        _f("RESTIC_REPOSITORY", "Repository", help="Lokal, sftp:user@host:/pfad, s3:https://minio:9000/bucket, rest:https://…"),
        _f("RESTIC_PASSWORD", "Repository-Passwort", "secret"),
        _f("RESTIC_AUTO_INIT", "Repository beim ersten Lauf anlegen", "bool", help=BOOL_HELP),
        _f("RESTIC_KEEP", "Aufbewahrung", help="z. B. --keep-daily 14 --keep-monthly 6. Leer = nichts löschen"),
        _f("AWS_ACCESS_KEY_ID", "Zugangsschlüssel des Speichers (S3)"),
        _f("AWS_SECRET_ACCESS_KEY", "Geheimer Schlüssel des Speichers (S3)", "secret"),
    ],
    "command": [
        _f("COMMAND_LABEL", "Anzeigename", placeholder="Befehl"),
        _f("COMMAND_START", "Start-Befehl", help="Pflicht, z. B. /scripts/start.sh"),
        _f("COMMAND_STATUS", "Status-Befehl", help="Exit 0 = fertig, 10 = läuft noch, sonst Fehler"),
        _f("COMMAND_LIST", "Auflisten", help="z. B. /scripts/list.sh {container}"),
        _f("COMMAND_FETCH", "Zurückholen", help="z. B. /scripts/fetch.sh {container} {folder} {dest}"),
        _f("COMMAND_POLL_SECONDS", "Abstand der Statusabfrage (Sekunden)", placeholder="30"),
        _f("COMMAND_TIMEOUT", "Zeitlimit je Aufruf (Sekunden)", placeholder="21600"),
    ],
}
BY_ENV = {f["env"]: (tid, f) for tid, fields in FIELDS.items() for f in fields}

# Stand der .env beim Start – darauf fällt ein geleerter Wert aus dem Webinterface zurück
_BASE = {env: os.environ.get(env) for env in list(BY_ENV) + ["TARGETS"]}
_state = {"values": {}, "enabled": None}


class ConfigError(ValueError):
    pass


def _read():
    try:
        with open(FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"values": {}, "enabled": None}
    values = {k: str(v) for k, v in (data.get("values") or {}).items() if k in BY_ENV and str(v).strip()}
    enabled = data.get("enabled")
    return {"values": values, "enabled": [str(i) for i in enabled] if isinstance(enabled, list) else None}


def _write(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, FILE)


def apply():
    """Werte aus dem Webinterface in die Umgebung übernehmen, sonst gilt der Stand der .env."""
    with _lock:
        for env in BY_ENV:
            if _state["values"].get(env):
                os.environ[env] = _state["values"][env]
            elif _BASE.get(env):
                os.environ[env] = _BASE[env]
            else:
                os.environ.pop(env, None)
        if _state["enabled"]:
            os.environ["TARGETS"] = ",".join(_state["enabled"])
        elif _BASE.get("TARGETS"):
            os.environ["TARGETS"] = _BASE["TARGETS"]
        else:
            os.environ.pop("TARGETS", None)


def load():
    with _lock:
        _state.update(_read())
        apply()


def _clean(env, value):
    """Prüft und bereinigt einen Wert. Rückgabe: String (nicht leer) oder None = Wert aus dem Webinterface entfernen."""
    _, field = BY_ENV[env]
    value = str(value).strip()
    if not value:
        return None
    if len(value) > 2000 or any(c in value for c in "\r\n\0"):
        raise ConfigError(f"{field['label']}: ungültiger Wert (zu lang oder Zeilenumbruch).")
    if field["type"] == "bool":
        if value.lower() not in ("true", "false"):
            raise ConfigError(f"{field['label']}: nur „true“ oder „false“.")
        return value.lower()
    if field["url"] and not value.lower().startswith(("http://", "https://")):
        raise ConfigError(f"{field['label']}: die Adresse muss mit https:// (oder http://) beginnen.")
    return value


def public(target_id):
    """Felder eines Ziels für die Oberfläche – Zugangsdaten nur als gesetzt/nicht gesetzt."""
    with _lock:
        out = []
        for f in FIELDS.get(target_id, []):
            env = f["env"]
            current = os.environ.get(env, "")
            source = "ui" if _state["values"].get(env) else "env" if _BASE.get(env) else "unset"
            out.append({**f, "set": bool(current), "source": source, "value": "" if f["type"] == "secret" else current})
        return out


def enabled_source():
    return "ui" if _state["enabled"] else "env"


def save(target_id, values, enabled=None, enabled_now=()):
    """values: {ENV: Text | None}. Leerer Text/None entfernt den Wert aus dem Webinterface (Geheimnisse: nur None;
    ein leerer Text lässt sie unverändert). enabled: True/False schaltet das Ziel in TARGETS ein bzw. aus."""
    if target_id not in FIELDS:
        raise ConfigError(f"Unbekanntes Ziel '{target_id}'.")
    with _lock:
        new = {"values": dict(_state["values"]), "enabled": list(_state["enabled"]) if _state["enabled"] else None}
        for env, raw in (values or {}).items():
            if env not in BY_ENV or BY_ENV[env][0] != target_id:
                raise ConfigError(f"'{env}' gehört nicht zu diesem Ziel.")
            secret = BY_ENV[env][1]["type"] == "secret"
            if raw is None:
                new["values"].pop(env, None)
            elif secret and not str(raw).strip():
                continue
            else:
                cleaned = _clean(env, raw)
                if cleaned is None:
                    new["values"].pop(env, None)
                else:
                    new["values"][env] = cleaned
        if enabled is not None:
            ids = list(enabled_now)
            if enabled and target_id not in ids:
                ids.append(target_id)
            if not enabled:
                ids = [i for i in ids if i != target_id]
            if not ids:
                raise ConfigError("Mindestens ein Backup-Ziel muss aktiv bleiben.")
            new["enabled"] = ids
        changed = sorted(env for env in set(values or {}) if new["values"].get(env) != _state["values"].get(env))
        if new["enabled"] != _state["enabled"]:
            changed.append("aktiv" if enabled else "inaktiv")
        _write(new)
        _state.update(new)
        apply()
        applog.get("config").info("Ziel %s geändert (nur Namen, keine Werte): %s", target_id, ", ".join(changed) or "nichts")


load()
