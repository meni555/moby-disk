"""Gemeinsame Testumgebung: temporäre Daten-/Backup-Ordner, aktive Ziele, alte Datenbank für den Migrationstest."""
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=os.path.join(TMP, "data"), BACKUP_ROOT=os.path.join(TMP, "backups"),
                  VOLUMES_ROOT=os.path.join(TMP, "volumes"), APP_PASSWORD="", DISABLE_SCHEDULER="1", TARGETS="commvault,fake")
os.makedirs(os.environ["DATA_DIR"])
os.makedirs(os.environ["BACKUP_ROOT"])
sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

# Alte Datenbank mit der Tabelle cvstate aus Vorgängerversionen (Migration prüfen)
_old = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "helper.db"))
_old.executescript("""CREATE TABLE cvstate (container TEXT NOT NULL, folder TEXT NOT NULL, verified REAL NOT NULL,
                      cv_job INTEGER, info TEXT NOT NULL DEFAULT '{}', local_deleted REAL, PRIMARY KEY (container, folder));
                      INSERT INTO cvstate VALUES ('alt', '2026-10-01T10-00-00-000000Z', 1790000000, 7, '{"size_bytes": 5}', NULL);""")
_old.commit()
_old.close()

