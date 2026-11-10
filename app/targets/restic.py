"""Ziel „restic“ (push): der Helper schreibt jede Sicherung selbst in ein restic-Repository.

restic verschlüsselt, dedupliziert mit inhaltsbasierten Blöcken (die täglich neu geschriebenen tar-Archive
kosten dadurch nur ihre echten Änderungen) und schreibt nach lokalem Pfad, SFTP, S3/MinIO, Backblaze B2, rest-server …

Einrichtung nur über die Umgebung (.env) – es gelten die Standard-Variablen von restic:
  RESTIC_REPOSITORY            z. B. /mnt/backup/restic, sftp:user@host:/pfad, s3:https://minio:9000/bucket
  RESTIC_PASSWORD              (oder RESTIC_PASSWORD_FILE / RESTIC_PASSWORD_COMMAND)
  AWS_ACCESS_KEY_ID …          Zugangsdaten des Speichers, wie restic sie kennt
  RESTIC_AUTO_INIT=true        fehlendes Repository beim ersten Lauf anlegen
  RESTIC_KEEP="--keep-daily 14 --keep-monthly 6"   Aufbewahrung im Repository (leer = nichts löschen)
Jede Sicherung wird ein Snapshot mit Host = Container-Name und Tag folder=<Zeitstempel-Ordner>.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import datetime

from ..config import config
from .base import FETCH, TRIGGER, VERSIONS, Run, RunResult, Target, TargetError, run_process, secret_state, yes_no

BINARY = "restic"
TAG = "moby-disk"                      # kennzeichnet Snapshots von Moby Disk im Repository
OLD_TAGS = ("docker-backup-helper",)   # Snapshots aus der Zeit vor der Umbenennung


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _mask(text):
    """Zugangsdaten in Repository-URLs (https://user:pass@host) aus Meldungen entfernen."""
    return re.sub(r"(://[^/:@\s]+):[^@\s]+@", r"\1:***@", text or "")


def _epoch(value):
    try:
        value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        return datetime.datetime.fromisoformat(value).timestamp()
    except (AttributeError, ValueError):
        return None


class ResticTarget(Target):
    id = "restic"
    label = "restic"
    kind = "push"
    capabilities = frozenset({TRIGGER, VERSIONS, FETCH})

    # ───────── Aufruf ─────────
    def _env(self):
        env = dict(os.environ)
        env.setdefault("RESTIC_CACHE_DIR", os.path.join(os.environ.get("DATA_DIR", "/data"), "restic-cache"))
        return env

    def _run(self, args, timeout=6 * 3600, cwd=None, check=True, cancel=None):
        try:
            proc = run_process([BINARY, *args], timeout, cwd=cwd, env=self._env(), cancel=cancel)
        except FileNotFoundError:
            raise TargetError("restic ist nicht installiert.")
        except subprocess.TimeoutExpired:
            raise TargetError(f"restic {args[0]}: Zeitüberschreitung nach {timeout // 60} Minuten.")
        if check and proc.returncode != 0:
            tail = _mask((proc.stderr or proc.stdout or "").strip())[-600:]
            raise TargetError(f"restic {args[0]} fehlgeschlagen (Code {proc.returncode}): {tail}")
        return proc

    # ───────── Zustand ─────────
    def configured(self):
        env = os.environ
        return bool(shutil.which(BINARY) and env.get("RESTIC_REPOSITORY", "").strip() and (
            env.get("RESTIC_PASSWORD") or env.get("RESTIC_PASSWORD_FILE") or env.get("RESTIC_PASSWORD_COMMAND")))

    def info(self):
        env = os.environ
        has_pw = env.get("RESTIC_PASSWORD") or env.get("RESTIC_PASSWORD_FILE") or env.get("RESTIC_PASSWORD_COMMAND")
        return [("Repository", _mask(env.get("RESTIC_REPOSITORY", "")) or "nicht gesetzt"), ("Passwort", secret_state(has_pw)),
                ("Repository automatisch anlegen", yes_no(self._auto_init())),
                ("Aufbewahrung (RESTIC_KEEP)", env.get("RESTIC_KEEP", "").strip() or "keine – es wird nichts gelöscht"),
                ("restic", "installiert" if shutil.which(BINARY) else "nicht im Image")]

    def test(self):
        if not shutil.which(BINARY):
            raise TargetError("restic ist nicht installiert.")
        if not self.configured():
            raise TargetError("RESTIC_REPOSITORY und RESTIC_PASSWORD fehlen in der .env.")
        proc = self._run(["snapshots", "--json", "--latest", "1"], timeout=120, check=False)
        if proc.returncode != 0 and self._missing_repo(proc):
            raise TargetError("Das Repository existiert noch nicht" + (
                " – wird beim ersten Lauf angelegt (RESTIC_AUTO_INIT)." if self._auto_init() else
                ". RESTIC_AUTO_INIT=true setzen oder es mit `restic init` anlegen."))
        if proc.returncode != 0:
            raise TargetError(_mask((proc.stderr or "").strip())[-400:] or f"restic Code {proc.returncode}")
        version = (self._run(["version"], timeout=20).stdout or "").strip().split("\n")[0]
        return {"repository": _mask(os.environ["RESTIC_REPOSITORY"]), "version": version,
                "snapshots": len(json.loads(proc.stdout or "[]"))}

    @staticmethod
    def _missing_repo(proc):
        text = (proc.stderr or "") + (proc.stdout or "")
        return "Is there a repository" in text or "unable to open config file" in text

    @staticmethod
    def _auto_init():
        return os.environ.get("RESTIC_AUTO_INIT", "").strip().lower() in ("1", "true", "yes", "on")

    def _ensure_repo(self, log_cb):
        proc = self._run(["cat", "config"], timeout=120, check=False)
        if proc.returncode == 0:
            return
        if self._missing_repo(proc) and self._auto_init():
            log_cb("restic: Repository fehlt – lege es an (RESTIC_AUTO_INIT).")
            self._run(["init"], timeout=300)
            return
        raise TargetError("restic: " + (_mask((proc.stderr or "").strip())[-400:] or "Repository nicht erreichbar."))

    # ───────── Lauf ─────────
    def start(self, items, options, log_cb):
        """Schreibt die Sicherungen nacheinander ins Repository. Das Ende steht danach fest – wait() meldet es nur."""
        if not self.configured():
            raise TargetError("restic ist nicht eingerichtet (RESTIC_REPOSITORY, RESTIC_PASSWORD).")
        cancel = options.get("cancel_check")
        self._ensure_repo(log_cb)
        root = config.backup_root
        done, failed, snaps, added = [], [], [], 0
        for container, folder in items:
            if cancel:
                cancel()
            rel = os.path.join(_safe_name(container), folder)
            if not os.path.isdir(os.path.join(root, rel)):
                failed.append((container, f"Ordner {rel} nicht gefunden"))
                continue
            proc = self._run(["backup", "--json", "--host", _safe_name(container), "--tag", f"folder={folder}",
                              "--tag", TAG, f"./{rel}"], cwd=root, check=False, cancel=cancel)
            summary = self._summary(proc.stdout)
            if proc.returncode not in (0, 3) or not summary:   # 3 = einzelne Dateien nicht lesbar, Snapshot besteht
                failed.append((container, _mask((proc.stderr or proc.stdout or "").strip())[-300:]))
                log_cb(f"[{container}] restic-Sicherung fehlgeschlagen")
                continue
            done.append(container)
            snaps.append(summary.get("snapshot_id", "")[:8])
            added += int(summary.get("data_added") or 0)
            log_cb(f"[{container}] restic: Snapshot {summary.get('snapshot_id', '')[:8]}, "
                   f"{int(summary.get('data_added') or 0) / 2 ** 20:.1f} MB neu im Repository"
                   + (" (einzelne Dateien nicht lesbar)" if proc.returncode == 3 else ""))
        return Run(id=",".join(snaps) or None, info={"done": done, "failed": failed, "added": added})

    def wait(self, run, progress_cb=None, max_wait=0):
        failed = run.info.get("failed") or []
        if progress_cb:
            progress_cb(100.0, "restic: fertig")
        if failed:
            return RunResult(False, "teilweise fehlgeschlagen" if run.info.get("done") else "fehlgeschlagen",
                             "; ".join(f"{c}: {why}" for c, why in failed), done=run.info.get("done"))
        return RunResult(True, "Completed", done=run.info.get("done"))

    def after_success(self, run, log_cb):
        """Aufbewahrung im Repository (RESTIC_KEEP) und danach prune; Fehler brechen den Lauf nicht ab."""
        keep = shlex.split(os.environ.get("RESTIC_KEEP", ""))
        if not keep:
            return
        try:
            self._run(["forget", "--group-by", "host", "--prune", *keep])
            log_cb("restic: alte Snapshots entfernt (" + " ".join(keep) + ")")
        except TargetError as exc:
            log_cb(f"WARNUNG: restic forget fehlgeschlagen: {exc}")

    @staticmethod
    def _summary(stdout):
        for line in reversed((stdout or "").splitlines()):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("message_type") == "summary":
                return msg
        return None

    # ───────── Stände ─────────
    def _snapshots(self, *args):
        proc = self._run(["snapshots", "--json", *args], timeout=300, check=False)
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout or "[]")
        except ValueError:
            return None

    def list_folders(self, container):
        if not self.configured():
            return None
        snaps = self._snapshots("--host", _safe_name(container))
        if snaps is None:
            return None
        found = {}
        for snap in snaps:
            folder = next((t.split("=", 1)[1] for t in snap.get("tags") or [] if t.startswith("folder=")), None)
            if not folder:
                continue
            summary = snap.get("summary") or {}
            found[folder] = {"time": _epoch(snap.get("time")), "size": int(summary.get("total_bytes_processed") or 0),
                             "ref": (snap.get("short_id") or snap.get("id") or "")[:8]}
        return found

    def list_containers(self):
        if not self.configured():
            return None
        snaps = self._snapshots()
        if snaps is None:
            return None
        return sorted({s.get("hostname") for s in snaps if s.get("hostname")
                       and set(s.get("tags") or []) & {TAG, *OLD_TAGS}})

    # ───────── Restore ─────────
    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        log_cb = log_cb or (lambda msg: None)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", folder or "") or folder in (".", ".."):
            raise TargetError(f"Ungültiger Sicherungsordner: {folder}")
        snaps = self._snapshots("--host", _safe_name(container), "--tag", f"folder={folder}")
        if snaps is None:
            raise TargetError("restic: Repository nicht erreichbar.")
        if not snaps:
            raise TargetError(f"restic: kein Snapshot für {container}/{folder} gefunden.")
        snap = sorted(snaps, key=lambda s: s.get("time") or "")[-1]
        os.makedirs(dest, exist_ok=True)
        log_cb(f"[{container}] restic: hole Snapshot {snap.get('short_id') or snap['id'][:8]} ...")
        self._run(["restore", snap["id"], "--target", dest])
        if progress_cb:
            progress_cb(100.0, f"{container}: restic-Restore fertig")
