"""Ziel „Proxmox Backup Server“ (push): jede Sicherung wird ein Snapshot im Datastore des PBS.

Moby Disk ruft dazu `proxmox-backup-client` auf (im Image enthalten). Der PBS dedupliziert mit inhaltsbasierten Blöcken
und kann clientseitig verschlüsseln – die täglich neu geschriebenen tar-Archive kosten dadurch nur ihre Änderungen.

Einrichtung nur über die .env – es gelten die Standard-Variablen des Clients:
  PBS_REPOSITORY      Benutzer@Realm[!Token]@Server:Port:Datastore, z. B. backup@pbs!moby@pbs.example.com:8007:docker
  PBS_PASSWORD        Passwort oder Geheimnis des API-Tokens
  PBS_FINGERPRINT     SHA-256-Fingerabdruck des Server-Zertifikats (bei selbstsigniertem Zertifikat nötig)
  PBS_NAMESPACE       optional: Namespace im Datastore
  PBS_KEYFILE         optional: Schlüsseldatei für clientseitige Verschlüsselung (+ PBS_ENCRYPTION_PASSWORD)
  PBS_KEEP            optional: Aufbewahrung je Container, z. B. "--keep-last 14 --keep-monthly 6" (leer = nichts löschen)
Der API-Token braucht auf dem Datastore mindestens die Rechte DatastoreBackup (Sichern), DatastoreReader (Restore) und
für PBS_KEEP zusätzlich DatastorePrune. Speicher freigeben (Garbage Collection) übernimmt der Server selbst.

Jede Sicherung wird ein Snapshot der Gruppe host/<container>; der Zeitstempel-Ordner steckt im Namen des Archivs
(<ordner>.pxar), der Zeitpunkt des Snapshots ist der des Ordners.
"""
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess

from .. import backup
from ..config import config
from .base import FETCH, TRIGGER, VERSIONS, Run, RunResult, Target, TargetError, run_process, secret_state, yes_no

BINARY = "proxmox-backup-client"
SUFFIXES = (".pxar.didx", ".pxar")


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _mask(text):
    """Geheimnisse (Token-Geheimnis, Passwort in URLs) aus Meldungen entfernen."""
    text = text or ""
    for var in ("PBS_PASSWORD", "PBS_ENCRYPTION_PASSWORD"):
        secret = os.environ.get(var)
        if secret and len(secret) > 3:
            text = text.replace(secret, "***")
    return re.sub(r"(://[^/:@\s]+):[^@\s]+@", r"\1:***@", text)


class PbsTarget(Target):
    id = "pbs"
    label = "Proxmox Backup Server"
    kind = "push"
    capabilities = frozenset({TRIGGER, VERSIONS, FETCH})

    # ───────── Aufruf ─────────
    def _args(self, *args):
        extra = []
        if os.environ.get("PBS_NAMESPACE", "").strip():
            extra += ["--ns", os.environ["PBS_NAMESPACE"].strip()]
        return [BINARY, *args, *extra]

    def _run(self, args, timeout=6 * 3600, check=True, keyfile=False, cancel=None):
        argv = self._args(*args)
        if keyfile and os.environ.get("PBS_KEYFILE", "").strip():
            argv += ["--keyfile", os.environ["PBS_KEYFILE"].strip()]
        # Der Client legt Index-Dateien mit O_TMPFILE an – das gibt es nicht auf dem Overlay-Dateisystem eines Containers.
        # Cache und Temp liegen deshalb unter DATA_DIR (ein Host-Ordner auf ext4/xfs).
        cache = os.path.join(os.environ.get("DATA_DIR", "/data"), "pbs-cache")
        os.makedirs(os.path.join(cache, "tmp"), exist_ok=True)
        env = dict(os.environ, XDG_CACHE_HOME=cache, TMPDIR=os.path.join(cache, "tmp"))
        try:
            proc = run_process(argv, timeout, env=env, cancel=cancel)
        except FileNotFoundError:
            raise TargetError("proxmox-backup-client ist nicht installiert (nicht in diesem Image enthalten).")
        except subprocess.TimeoutExpired:
            raise TargetError(f"proxmox-backup-client {args[0]}: Zeitüberschreitung nach {timeout // 60} Minuten.")
        if check and proc.returncode != 0:
            raise TargetError(f"proxmox-backup-client {args[0]} fehlgeschlagen (Code {proc.returncode}): "
                              f"{self._tail(proc)}")
        return proc

    @staticmethod
    def _tail(proc):
        text = _mask((proc.stderr or "").strip() or (proc.stdout or "").strip())
        if "Operation not supported" in text:
            text += ("  → DATA_DIR braucht ein Dateisystem mit O_TMPFILE-Unterstützung (ext4, xfs, tmpfs; nicht NFS/Overlay).")
        if "ENOENT" in text and os.environ.get("PBS_NAMESPACE", "").strip():
            text += "  → Existiert der Namespace in PBS_NAMESPACE im Datastore?"
        if "fingerprint" in text.lower() and "PBS_FINGERPRINT" not in os.environ:
            text += "  → Fingerabdruck des Server-Zertifikats in PBS_FINGERPRINT eintragen."
        return text[-500:]

    # ───────── Zustand ─────────
    def configured(self):
        env = os.environ
        return bool(shutil.which(BINARY) and env.get("PBS_REPOSITORY", "").strip() and env.get("PBS_PASSWORD"))

    def info(self):
        env = os.environ
        return [("Repository", _mask(env.get("PBS_REPOSITORY", "")) or "nicht gesetzt"), ("Zugangsgeheimnis", secret_state(env.get("PBS_PASSWORD"))),
                ("Fingerabdruck", "gesetzt" if env.get("PBS_FINGERPRINT") else "nicht gesetzt (nur mit gültigem Zertifikat)"),
                ("Namespace", env.get("PBS_NAMESPACE", "").strip() or "(Stamm)"),
                ("Clientseitige Verschlüsselung", yes_no(env.get("PBS_KEYFILE", "").strip())),
                ("Aufbewahrung (PBS_KEEP)", env.get("PBS_KEEP", "").strip() or "keine – es wird nichts gelöscht"),
                ("proxmox-backup-client", "installiert" if shutil.which(BINARY) else "nicht im Image")]

    def test(self):
        if not shutil.which(BINARY):
            raise TargetError("proxmox-backup-client ist nicht installiert (nicht in diesem Image enthalten).")
        if not self.configured():
            raise TargetError("PBS_REPOSITORY und PBS_PASSWORD fehlen in der .env.")
        proc = self._run(["list", "--output-format", "json"], timeout=120)
        try:
            groups = json.loads(proc.stdout or "[]")
        except ValueError:
            groups = []
        ver = self._run(["version"], timeout=60, check=False).stdout or ""
        return {"repository": _mask(os.environ["PBS_REPOSITORY"]),
                "version": ", ".join(l.strip() for l in ver.splitlines() if "version" in l) or "unbekannt",
                "namespace": os.environ.get("PBS_NAMESPACE", "").strip() or "(Stamm)",
                "verschlüsselt": bool(os.environ.get("PBS_KEYFILE", "").strip()),
                "gruppen": sum(1 for g in groups if g.get("backup-type") == "host")}

    # ───────── Lauf ─────────
    def start(self, items, options, log_cb):
        """Lädt die Sicherungen nacheinander hoch. Das Ende steht danach fest – wait() meldet es nur."""
        if not self.configured():
            raise TargetError("Proxmox Backup Server ist nicht eingerichtet (PBS_REPOSITORY, PBS_PASSWORD).")
        done, failed = [], []
        cancel = options.get("cancel_check")
        for container, folder in items:
            if cancel:
                cancel()
            path = os.path.join(config.backup_root, _safe_name(container), folder)
            if not os.path.isdir(path):
                failed.append((container, f"Ordner {path} nicht gefunden"))
                continue
            parsed = backup._parse_ts(folder)
            args = ["backup", f"{folder}.pxar:{path}", "--backup-type", "host", "--backup-id", _safe_name(container)]
            if parsed:
                args += ["--backup-time", str(int(parsed.timestamp()))]
            proc = self._run(args, check=False, keyfile=True, cancel=cancel)
            if proc.returncode != 0:
                failed.append((container, self._tail(proc)))
                log_cb(f"[{container}] PBS-Sicherung fehlgeschlagen")
                continue
            done.append(container)
            log_cb(f"[{container}] PBS: Snapshot host/{_safe_name(container)} ({folder}) hochgeladen")
        return Run(id=None, info={"done": done, "failed": failed})

    def wait(self, run, progress_cb=None, max_wait=0):
        failed = run.info.get("failed") or []
        if progress_cb:
            progress_cb(100.0, "Proxmox Backup Server: fertig")
        if failed:
            return RunResult(False, "teilweise fehlgeschlagen" if run.info.get("done") else "fehlgeschlagen",
                             "; ".join(f"{c}: {why}" for c, why in failed), done=run.info.get("done"))
        return RunResult(True, "Completed", done=run.info.get("done"))

    def after_success(self, run, log_cb):
        """Aufbewahrung (PBS_KEEP) für die gerade gesicherten Container; Fehler brechen den Lauf nicht ab."""
        keep = shlex.split(os.environ.get("PBS_KEEP", ""))
        if not keep:
            return
        for container in run.info.get("done") or []:
            try:
                self._run(["prune", f"host/{_safe_name(container)}", *keep])
            except TargetError as exc:
                log_cb(f"WARNUNG: PBS-Prune für {container} fehlgeschlagen: {exc}")
                return
        log_cb("PBS: alte Snapshots entfernt (" + " ".join(keep) + ")")

    # ───────── Stände ─────────
    @staticmethod
    def _folder(snapshot):
        for entry in snapshot.get("files") or []:
            name = entry.get("filename") if isinstance(entry, dict) else str(entry)
            for suffix in SUFFIXES:
                if name and name.endswith(suffix) and re.match(r"^\d{4}-\d{2}-\d{2}T", name):
                    return name[: -len(suffix)]
        return None

    def _snapshots(self, group):
        proc = self._run(["snapshot", "list", group, "--output-format", "json"], timeout=300, check=False)
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout or "[]")
        except ValueError:
            return None

    def list_folders(self, container):
        if not self.configured():
            return None
        snaps = self._snapshots(f"host/{_safe_name(container)}")
        if snaps is None:
            return None
        found = {}
        for snap in snaps:
            folder = self._folder(snap)
            if folder:
                found[folder] = {"time": snap.get("backup-time"), "size": int(snap.get("size") or 0),
                                 "ref": f"host/{snap.get('backup-id')}/{snap.get('backup-time')}"}
        return found

    def list_containers(self):
        if not self.configured():
            return None
        proc = self._run(["list", "--output-format", "json"], timeout=300, check=False)
        if proc.returncode != 0:
            return None
        try:
            return sorted(g["backup-id"] for g in json.loads(proc.stdout or "[]") if g.get("backup-type") == "host")
        except (ValueError, KeyError):
            return None

    # ───────── Restore ─────────
    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        log_cb = log_cb or (lambda msg: None)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", folder or "") or folder in (".", ".."):
            raise TargetError(f"Ungültiger Sicherungsordner: {folder}")
        snaps = self._snapshots(f"host/{_safe_name(container)}")
        if snaps is None:
            raise TargetError("Proxmox Backup Server nicht erreichbar.")
        match = [s for s in snaps if self._folder(s) == folder]
        if not match:
            raise TargetError(f"PBS: kein Snapshot für {container}/{folder} gefunden.")
        snap = sorted(match, key=lambda s: s.get("backup-time") or 0)[-1]
        target = os.path.join(dest, _safe_name(container), folder)
        os.makedirs(target, exist_ok=True)
        archive = next(e["filename"] for e in snap["files"] if isinstance(e, dict) and e.get("filename", "").startswith(folder))
        archive = re.sub(r"\.didx$", "", archive)
        stamp = datetime.datetime.fromtimestamp(snap["backup-time"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_cb(f"[{container}] PBS: hole Snapshot host/{snap['backup-id']}/{stamp} ...")
        # Die Sicherungsordner enthalten nur Dateien (tar, json): Attribute und ACLs sind ohne Bedeutung und scheitern
        # auf manchen Dateisystemen (Overlay, Netzlaufwerke) mit „Operation not supported“.
        self._run(["restore", f"host/{snap['backup-id']}/{stamp}", archive, target, "--allow-existing-dirs",
                   "--ignore-xattrs", "--ignore-acls"], keyfile=True)
        if progress_cb:
            progress_cb(100.0, f"{container}: PBS-Restore fertig")
