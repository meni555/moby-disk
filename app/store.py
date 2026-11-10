"""Dauerhafter Speicher (SQLite) für Jobverlauf, Stände in den Backup-Zielen und kleine Statuswerte.

Liegt in DATA_DIR/helper.db und überlebt Neustarts/Recreate des Containers."""
import json
import os
import sqlite3
import threading
import time

from .config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "helper.db")
KEEP_JOBS = 500
_lock = threading.RLock()
_conn = None


def _migrate_cvstate(conn):
    """Frühere Versionen kannten nur Commvault (Tabelle cvstate) – die Einträge wandern ins Ziel 'commvault'."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cvstate'").fetchone():
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cvstate)")}
    info = "info" if "info" in cols else "'{}'"
    gone = "local_deleted" if "local_deleted" in cols else "NULL"
    conn.execute(f"""INSERT OR IGNORE INTO target_state (target, container, folder, verified, ref, info, local_deleted)
                     SELECT 'commvault', container, folder, verified, cv_job, {info}, {gone} FROM cvstate""")
    conn.execute("DROP TABLE cvstate")


def _db():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, started REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_started ON jobs(started);
            CREATE TABLE IF NOT EXISTS target_state (
                target TEXT NOT NULL, container TEXT NOT NULL, folder TEXT NOT NULL, verified REAL NOT NULL,
                ref TEXT, info TEXT NOT NULL DEFAULT '{}', local_deleted REAL,
                PRIMARY KEY (target, container, folder));
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                container TEXT NOT NULL, folder TEXT NOT NULL, run_id TEXT, stack TEXT, created REAL NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (container, folder));
            CREATE INDEX IF NOT EXISTS runs_run ON runs(run_id);
        """)
        _migrate_cvstate(conn)
        conn.commit()
        _conn = conn
    return _conn


def _ident(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def live_db_files():
    """{(dev, inode): 'main' | 'aux'} der eigenen Datenbank samt WAL/SHM.

    Diese Dateien dürfen im eigenen Prozess nie direkt gelesen werden (z. B. beim Sichern von /data):
    Schließt der Prozess irgendeinen Dateideskriptor darauf, verliert er laut POSIX alle SQLite-Sperren,
    ein anderer Prozess löscht dann die WAL-Datei – und alle weiteren Schreibvorgänge gehen verloren."""
    files = {}
    for suffix, role in (("", "main"), ("-wal", "aux"), ("-shm", "aux"), ("-journal", "aux")):
        ident = _ident(DB_PATH + suffix)
        if ident:
            files[ident] = role
    return files


def snapshot_to(target):
    """Konsistente Kopie der Datenbank über SQLite selbst (VACUUM INTO) – ohne die Datei zu öffnen."""
    with _lock:
        db = _db()
        db.commit()
        db.execute("VACUUM INTO ?", (target,))


def reset_for_tests(path):  # pragma: no cover - nur für Tests
    global _conn, DB_PATH
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        DB_PATH = path


# ───────── Jobs ─────────
def save_job(job):
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO jobs (id, started, status, data) VALUES (?, ?, ?, ?)",
                   (job["id"], job["started"], job["status"], json.dumps(job, default=str)))
        db.commit()


def load_jobs(limit=200):
    with _lock:
        rows = _db().execute("SELECT data FROM jobs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    jobs = []
    for (data,) in rows:
        try:
            jobs.append(json.loads(data))
        except ValueError:
            continue
    return jobs


def prune_jobs(keep=KEEP_JOBS):
    with _lock:
        db = _db()
        db.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY started DESC LIMIT ?)", (keep,))
        db.commit()


# ───────── Stände in den Backup-Zielen ─────────
def mark_verified(target, container, folder, when=None, ref=None, info=None):
    """Sicherungsordner als im Ziel vorhanden vermerken. Bereits gespeicherte Eckdaten (info) und ein
    vermerktes lokales Löschen bleiben erhalten – der Vermerk gilt, solange das Ziel die Sicherung hat."""
    with _lock:
        db = _db()
        db.execute("""INSERT INTO target_state (target, container, folder, verified, ref, info) VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT (target, container, folder) DO UPDATE SET verified = excluded.verified,
                          ref = COALESCE(excluded.ref, target_state.ref),
                          info = CASE WHEN target_state.info = '{}' THEN excluded.info ELSE target_state.info END""",
                   (target, container, folder, when or time.time(), ref, json.dumps(info or {})))
        db.commit()


def drop_entries(target, container, keep=None):
    """Vermerke eines Containers entfernen, die das Ziel nicht mehr hat (Aufbewahrung abgelaufen, gelöscht).
    'keep': Ordner, die bleiben; None = alle Vermerke des Containers in diesem Ziel. Rückgabe: Anzahl."""
    with _lock:
        db = _db()
        keep = None if keep is None else list(keep)
        if not keep:   # keine Ausnahmen (NOT IN () würde in SQL nie zutreffen)
            cur = db.execute("DELETE FROM target_state WHERE target = ? AND container = ?", (target, container))
        else:
            marks = ",".join("?" * len(keep))
            cur = db.execute(f"DELETE FROM target_state WHERE target = ? AND container = ? AND folder NOT IN ({marks})",
                             (target, container, *keep))
        db.commit()
        return cur.rowcount


def mark_local_deleted(container, folder, info=None):
    """Lokale Kopie wurde gelöscht: die Vermerke in den Zielen bleiben, die Eckdaten werden für die Anzeige gemerkt."""
    with _lock:
        db = _db()
        db.execute("UPDATE target_state SET local_deleted = ?, info = ? WHERE container = ? AND folder = ?",
                   (time.time(), json.dumps(info or {}), container, folder))
        db.commit()


def entries(container=None, target=None):
    """Alle vermerkten Sicherungen, optional nur eines Containers und/oder Ziels."""
    where, args = [], []
    if container:
        where.append("container = ?")
        args.append(container)
    if target:
        where.append("target = ?")
        args.append(target)
    sql = "SELECT target, container, folder, verified, ref, info, local_deleted FROM target_state"
    with _lock:
        rows = _db().execute(sql + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()
    out = []
    for r in rows:
        try:
            info = json.loads(r[5] or "{}")
        except ValueError:
            info = {}
        out.append({"target": r[0], "container": r[1], "folder": r[2], "verified": r[3], "ref": r[4], "info": info,
                    "local_deleted": r[6]})
    return out


def verified_folders(container, target=None):
    """Ordner des Containers, die im Ziel (ohne Angabe: in mindestens einem Ziel) bestätigt sind."""
    with _lock:
        if target:
            rows = _db().execute("SELECT folder FROM target_state WHERE container = ? AND target = ?",
                                 (container, target)).fetchall()
        else:
            rows = _db().execute("SELECT folder FROM target_state WHERE container = ?", (container,)).fetchall()
    return {r[0] for r in rows}


# ───────── Kleine Statuswerte ─────────
def kv_get(key, default=None):
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def kv_set(key, value):
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        db.commit()


# ───────── Backup-Läufe (welche Sicherungen gehören zum selben Lauf/Stack) ─────────
def record_run(run_id, container, folder, stack=None, meta=None):
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO runs (container, folder, run_id, stack, created, meta) VALUES (?, ?, ?, ?, ?, ?)",
                   (container, folder, run_id, stack, time.time(), json.dumps(meta or {})))
        db.commit()


def run_of(container, folder):
    with _lock:
        row = _db().execute("SELECT run_id, stack, meta FROM runs WHERE container = ? AND folder = ?",
                            (container, folder)).fetchone()
    if not row:
        return None
    return {"run_id": row[0], "stack": row[1], "meta": json.loads(row[2] or "{}")}


def stack_names():
    """Alle Stacks, für die es Backup-Läufe gibt (auch solche, die auf dem Host nicht mehr existieren)."""
    with _lock:
        rows = _db().execute("SELECT DISTINCT stack FROM runs WHERE stack IS NOT NULL AND stack != ''").fetchall()
    return sorted(r[0] for r in rows)


def stack_runs(stack, limit=40):
    """Backup-Läufe eines Stacks, neueste zuerst: [{run_id, created, members: [{container, folder}]}]."""
    with _lock:
        rows = _db().execute("SELECT run_id, container, folder, created FROM runs WHERE stack = ? AND run_id IS NOT NULL "
                             "ORDER BY created DESC, container", (stack,)).fetchall()
    runs, index = [], {}
    for run_id, container, folder, created in rows:
        if run_id not in index:
            if len(runs) >= limit:
                continue
            index[run_id] = len(runs)
            runs.append({"run_id": run_id, "created": created, "members": []})
        runs[index[run_id]]["members"].append({"container": container, "folder": folder})
    return runs


def run_members(run_id):
    with _lock:
        rows = _db().execute("SELECT container, folder, stack, meta FROM runs WHERE run_id = ? ORDER BY container",
                             (run_id,)).fetchall()
    return [{"container": r[0], "folder": r[1], "stack": r[2], "meta": json.loads(r[3] or "{}")} for r in rows]
