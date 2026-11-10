"""Ziel „Befehl/Skript“ (pull oder push): bindet jede Backup-Lösung über eigene Befehle an.

Gedacht für alles, was der Helper nicht von Haus aus kennt (Veeam, Bacula, Borg, Kopia, rclone, ein eigenes Skript …).
Die Befehle stehen in der .env, werden OHNE Shell gestartet (Platzhalter werden als einzelne Argumente ersetzt, es
gibt keine Injektion über Container-Namen) und müssen im Helper-Container erreichbar sein (z. B. ein eingebundener
Ordner /scripts).

  COMMAND_START    Pflicht. Startet den Lauf. Umgebung: HELPER_ITEMS (JSON-Liste mit container, folder, path),
                   BACKUP_ROOT. Exit-Code 0 = Erfolg.
  COMMAND_STATUS   Optional. Wird nach dem Start wiederholt aufgerufen (Abstand COMMAND_POLL_SECONDS, Standard 30):
                   0 = fertig, 10 = läuft noch, alles andere = fehlgeschlagen.
  COMMAND_LIST     Optional. `… {container}` gibt JSON aus: {"<ordner>": {"time": <epoch>, "size": <bytes>}}
  COMMAND_FETCH    Optional. `… {container} {folder} {dest}` legt den Stand unter {dest}/<container>/<ordner>/ ab.
  COMMAND_LABEL    Anzeigename (Standard „Befehl“).   COMMAND_TIMEOUT  Sekunden je Aufruf (Standard 21600).
"""
import json
import os
import re
import shlex
import subprocess
import time

from ..config import config
from .base import FETCH, TRIGGER, VERSIONS, Run, RunResult, Target, TargetError, run_process, yes_no


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


class CommandTarget(Target):
    id = "command"
    kind = "pull"

    @property
    def label(self):
        return os.environ.get("COMMAND_LABEL", "").strip() or "Befehl"

    @property
    def capabilities(self):
        caps = set()
        if self._cmd("COMMAND_START"):
            caps.add(TRIGGER)
        if self._cmd("COMMAND_LIST"):
            caps.add(VERSIONS)
        if self._cmd("COMMAND_FETCH"):
            caps.add(FETCH)
        return frozenset(caps)

    @staticmethod
    def _cmd(name):
        return shlex.split(os.environ.get(name, ""))

    def configured(self):
        return bool(self._cmd("COMMAND_START"))

    def _run(self, name, subs=None, extra_env=None, timeout=None, cancel=None):
        argv = [part for part in self._cmd(name)]
        if not argv:
            raise TargetError(f"{name} ist nicht gesetzt.")
        subs = subs or {}
        argv = [re.sub(r"\{(container|folder|dest)\}", lambda m: subs.get(m.group(1), m.group(0)), a) for a in argv]
        env = dict(os.environ, BACKUP_ROOT=config.backup_root, **(extra_env or {}))
        try:
            return run_process(argv, timeout or int(os.environ.get("COMMAND_TIMEOUT") or 21600), env=env, cancel=cancel)
        except FileNotFoundError:
            raise TargetError(f"{name}: Programm '{argv[0]}' nicht gefunden (im Helper-Container eingebunden?).")
        except PermissionError:
            raise TargetError(f"{name}: '{argv[0]}' ist nicht ausführbar.")
        except subprocess.TimeoutExpired:
            raise TargetError(f"{name}: Zeitüberschreitung.")

    @staticmethod
    def _tail(proc):
        return ((proc.stderr or "").strip() or (proc.stdout or "").strip())[-400:]

    def info(self):
        env = os.environ
        rows = [("Anzeigename", self.label)]
        for name, text in (("COMMAND_START", "Start-Befehl"), ("COMMAND_STATUS", "Status-Befehl"), ("COMMAND_LIST", "Auflisten"),
                           ("COMMAND_FETCH", "Zurückholen")):
            rows.append((text, "gesetzt" if self._cmd(name) else "nicht gesetzt"))
        rows.append(("Zeitlimit je Aufruf", f"{env.get('COMMAND_TIMEOUT') or 21600} s"))
        return rows

    def test(self):
        if not self.configured():
            raise TargetError("COMMAND_START fehlt in der .env.")
        missing = [n for n in ("COMMAND_START", "COMMAND_STATUS", "COMMAND_LIST", "COMMAND_FETCH")
                   if self._cmd(n) and not os.path.exists(self._cmd(n)[0]) and "/" in self._cmd(n)[0]]
        if missing:
            raise TargetError("Programm nicht gefunden für: " + ", ".join(missing))
        return {"commands": [n.replace("COMMAND_", "").lower() for n in
                             ("COMMAND_START", "COMMAND_STATUS", "COMMAND_LIST", "COMMAND_FETCH") if self._cmd(n)]}

    # ───────── Lauf ─────────
    def start(self, items, options, log_cb):
        payload = [{"container": c, "folder": f, "path": os.path.join(config.backup_root, _safe_name(c), f)} for c, f in items]
        proc = self._run("COMMAND_START", extra_env={"HELPER_ITEMS": json.dumps(payload)}, cancel=options.get("cancel_check"))
        if proc.returncode != 0:
            raise TargetError(f"COMMAND_START endete mit Code {proc.returncode}: {self._tail(proc)}")
        if (proc.stdout or "").strip():
            log_cb(f"{self.label}: {proc.stdout.strip()[-300:]}")
        return Run(id=None, info={"items": payload})

    def wait(self, run, progress_cb=None, max_wait=7200):
        if not self._cmd("COMMAND_STATUS"):
            if progress_cb:
                progress_cb(100.0, f"{self.label}: fertig")
            return RunResult(True, "Completed")
        poll = max(1, int(os.environ.get("COMMAND_POLL_SECONDS") or 30))
        deadline = time.time() + max_wait
        while time.time() < deadline:
            proc = self._run("COMMAND_STATUS", extra_env={"HELPER_ITEMS": json.dumps(run.info.get("items") or [])})
            if proc.returncode == 0:
                return RunResult(True, "Completed")
            if proc.returncode != 10:
                return RunResult(False, "Failed", f"COMMAND_STATUS Code {proc.returncode}: {self._tail(proc)}")
            if progress_cb:
                progress_cb(None, f"{self.label}: läuft ...")
            time.sleep(poll)
        raise TargetError(f"Zeitüberschreitung beim Warten auf {self.label}.")

    # ───────── Stände ─────────
    def list_folders(self, container):
        if not self._cmd("COMMAND_LIST"):
            return None
        try:
            proc = self._run("COMMAND_LIST", {"container": container}, timeout=600)
            if proc.returncode != 0:
                return None
            data = json.loads(proc.stdout or "{}")
        except (TargetError, ValueError):
            return None
        return {f: {"time": float(m.get("time") or 0) or None, "size": int(m.get("size") or 0), "ref": m.get("ref")}
                for f, m in data.items() if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9A-Za-z._-]+", f or "") and isinstance(m, dict)}

    def list_containers(self):
        """Das Programm kennt keine Gesamtliste – bekannt sind die lokalen Container-Ordner und die bisherigen Vermerke."""
        if not self._cmd("COMMAND_LIST"):
            return None
        from .. import store
        names = {e["container"] for e in store.entries(target=self.id)}
        root = config.backup_root
        if os.path.isdir(root):
            names |= {n for n in os.listdir(root) if not n.startswith(".") and os.path.isdir(os.path.join(root, n))}
        return sorted(names)

    # ───────── Restore ─────────
    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        if not self._cmd("COMMAND_FETCH"):
            raise TargetError(f"{self.label}: COMMAND_FETCH ist nicht gesetzt – Restore nur über die Lösung selbst.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", folder or "") or folder in (".", ".."):
            raise TargetError(f"Ungültiger Sicherungsordner: {folder}")
        os.makedirs(dest, exist_ok=True)
        proc = self._run("COMMAND_FETCH", {"container": container, "folder": folder, "dest": dest},
                         extra_env={"HELPER_CONTAINER": container, "HELPER_FOLDER": folder, "HELPER_DEST": dest})
        if proc.returncode != 0:
            raise TargetError(f"COMMAND_FETCH endete mit Code {proc.returncode}: {self._tail(proc)}")
        if progress_cb:
            progress_cb(100.0, f"{container}: Restore fertig")
