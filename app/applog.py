"""Zentrales Logging von Moby Disk.

Jeder Eintrag landet an drei Stellen: im Ringpuffer (für den Reiter „Logs“), in einer rotierenden Datei
DATA_DIR/logs/mobydisk.jsonl (eine JSON-Zeile je Eintrag, überlebt Neustarts) und auf stderr (`docker logs`).
Zugangsdaten werden vor dem Schreiben maskiert: jeder Wert einer Umgebungsvariable, deren Name nach Passwort, Geheimnis
oder Token klingt, erscheint als ***.

Namen der Bereiche (Logger): api, auth, app, job, scheduler, config, notify, target.<id>, docker.
"""
import collections
import itertools
import json
import logging
import logging.handlers
import os
import re
import threading
import time

from .config import DATA_DIR, settings

LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "mobydisk.jsonl")
ROOT = "mobydisk"
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
BUFFER_SIZE = 6000
MAX_FILE_BYTES, KEEP_FILES = 5 * 1024 * 1024, 5

_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|ACCESS_KEY|PASSPHRASE)", re.I)
_buffer = collections.deque(maxlen=BUFFER_SIZE)
_ids = itertools.count(1)
_lock = threading.Lock()
_ready = False


def get(name):
    return logging.getLogger(f"{ROOT}.{name}")


def _secrets():
    return sorted({v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= 4}, key=len, reverse=True)


def redact(text):
    for secret in _secrets():
        if secret in text:
            text = text.replace(secret, "***")
    return text


class _Redacting(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class _Entries(logging.Handler):
    """Baut aus jedem Eintrag ein Wörterbuch, hält es im Ringpuffer und reicht es an die Datei weiter."""

    def __init__(self, file_handler):
        super().__init__()
        self.file_handler = file_handler

    def emit(self, record):
        try:
            exc = None
            if record.exc_info:
                exc = redact(logging.Formatter().formatException(record.exc_info))
            entry = {"t": round(record.created, 3), "level": record.levelname, "logger": record.name[len(ROOT) + 1:] or "app",
                     "msg": redact(record.getMessage()), "exc": exc}
            with _lock:
                entry["id"] = next(_ids)
                _buffer.append(entry)
            if self.file_handler:   # eine Kopie, damit die anderen Handler den Original-Eintrag behalten
                self.file_handler.emit(logging.makeLogRecord({
                    "name": record.name, "levelno": record.levelno, "levelname": record.levelname, "created": record.created,
                    "msg": json.dumps(entry, ensure_ascii=False), "args": None}))
        except Exception:   # Logging darf nie die Anwendung stören
            self.handleError(record)


def level_name():
    name = str(settings.get("log_level") or os.environ.get("LOG_LEVEL") or "INFO").upper()
    return name if name in LEVELS else "INFO"


def set_level(name):
    name = str(name).upper()
    if name not in LEVELS:
        raise ValueError("Log-Level muss DEBUG, INFO, WARNING oder ERROR sein.")
    settings.set("log_level", name)
    logging.getLogger(ROOT).setLevel(LEVELS[name])
    get("config").info("Log-Level auf %s gestellt", name)


def _load_tail():
    """Letzte Einträge aus der Datei (und der davor rotierten) in den Ringpuffer – damit das Log nach einem Neustart nicht leer ist."""
    files = [f"{LOG_FILE}.{i}" for i in range(KEEP_FILES, 0, -1)] + [LOG_FILE]
    lines = collections.deque(maxlen=BUFFER_SIZE)
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                lines.extend(fh)
        except OSError:
            continue
    for line in lines:
        try:
            entry = json.loads(line)
            entry["id"] = next(_ids)
            _buffer.append(entry)
        except ValueError:
            continue


def setup():
    """Einmal beim Start aufrufen (mehrfach ist harmlos)."""
    global _ready
    if _ready:
        return
    _ready = True
    root = logging.getLogger(ROOT)
    root.setLevel(LEVELS[level_name()])
    root.propagate = False
    file_handler = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _load_tail()
        file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=MAX_FILE_BYTES, backupCount=KEEP_FILES, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
    except OSError:
        pass   # ohne beschreibbaren Datenordner bleibt das Log im Speicher
    root.addHandler(_Entries(file_handler))
    stream = logging.StreamHandler()
    stream.setFormatter(_Redacting("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stream)


def query(level="INFO", logger="", q="", since=0, limit=500):
    """Einträge ab `since` (Id), mindestens in der Stufe `level`, passend zu Bereich und Suchtext; die neuesten `limit`, älteste zuerst."""
    minimum = LEVELS.get(str(level).upper(), logging.INFO)
    needle = (q or "").lower()
    prefix = (logger or "").lower()
    with _lock:
        items = list(_buffer)
    out = []
    for e in items:
        if e["id"] <= since or LEVELS.get(e["level"], 20) < minimum:
            continue
        if prefix and not (e["logger"].lower() == prefix or e["logger"].lower().startswith(prefix + ".")):
            continue
        if needle and needle not in (e["msg"] + " " + (e.get("exc") or "") + " " + e["logger"]).lower():
            continue
        out.append(e)
    return out[-limit:]


def last_id():
    with _lock:
        return _buffer[-1]["id"] if _buffer else 0


def as_text(entries=None):
    """Das ganze Log als Text zum Herunterladen."""
    rows = []
    for e in entries if entries is not None else query("DEBUG", limit=BUFFER_SIZE):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"])) + f".{int((e['t'] % 1) * 1000):03d}"
        rows.append(f"{stamp} {e['level']:<7} {e['logger']}: {e['msg']}")
        if e.get("exc"):
            rows.append(e["exc"])
    return "\n".join(rows) + "\n"


def file_info():
    try:
        return {"path": LOG_FILE, "bytes": os.path.getsize(LOG_FILE)}
    except OSError:
        return {"path": LOG_FILE, "bytes": 0}
