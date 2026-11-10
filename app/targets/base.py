"""Schnittstelle für Backup-Ziele.

Ein Ziel ist eine Backup-Lösung, an die der Helper seine fertigen Sicherungen übergibt bzw. von der er sie
zurückholt. Zwei Arten:
  pull  – die Lösung holt die Archive selbst ab (Commvault, Veeam …): der Helper legt sie unter BACKUP_ROOT
          ab, stößt die Lösung an, wartet und prüft, ob die Sicherung dort angekommen ist.
  push  – der Helper schreibt die Sicherung selbst ins Ziel (restic, Proxmox Backup Server …).

Was ein Ziel kann, steht in `capabilities`; die Oberfläche und der Ablauf fragen das ab, statt Ziele zu raten.
Beim Restore holt jedes Ziel einen Stand in einen Staging-Ordner zurück – ab dort ist der Ablauf für alle gleich.
"""
import subprocess
import time
from dataclasses import dataclass, field

from ..cancel import Cancelled

def secret_state(value):
    """Für die Anzeige: ob ein Geheimnis gesetzt ist – nie der Wert selbst."""
    return "gesetzt" if value else "nicht gesetzt"


def yes_no(flag):
    return "ja" if flag else "nein"


# Fähigkeiten
TRIGGER = "trigger"            # kann einen Sicherungslauf starten und dessen Ende melden
VERSIONS = "versions"          # kann die vorhandenen Sicherungsstände eines Containers auflisten
FETCH = "fetch"                # kann einen Stand ins Staging zurückholen (Restore)
DIRECT_PATHS = "direct_paths"  # kann Host-Pfade direkt (dateiweise) sichern, ohne Archiv des Helpers
SYNTHETIC_FULL = "synthetic_full"
STATE_CHECK = "state_check"    # kann Vermerke prüfen, ohne Ordner aufzulisten (validate_entries)


class TargetError(Exception):
    """Fehler eines Ziels – mit lesbarer Meldung für Oberfläche und Protokoll."""


def run_process(argv, timeout, cwd=None, env=None, cancel=None):
    """Wie subprocess.run mit Textausgabe, aber abbrechbar: 'cancel' wird etwa jede Sekunde gefragt und wirft
    Cancelled; dann wird der Prozess beendet. Wirft subprocess.TimeoutExpired nach 'timeout' Sekunden."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            cwd=cwd, env=env)
    deadline = time.time() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=1)
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            pass
        try:
            if cancel:
                cancel()
            if time.time() > deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
        except BaseException:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise


@dataclass
class Run:
    """Ein angestoßener Lauf beim Ziel."""
    id: str | None = None          # Kennung beim Ziel (z. B. Commvault-Job); None = nicht überwachbar
    raw: object = None             # Antwort des Ziels, nur fürs Protokoll
    info: dict = field(default_factory=dict)


@dataclass
class RunResult:
    ok: bool
    status: str = ""
    detail: str = ""
    done: list | None = None   # Container, die das Ziel übernommen hat (None = alle, wenn ok)


class Target:
    id = ""
    label = ""
    kind = "pull"
    capabilities = frozenset()

    # ───────── Zustand ─────────
    def configured(self):
        """Zugangsdaten vorhanden? (Ohne Netzwerkzugriff.)"""
        return False

    def ready(self):
        """Vollständig eingerichtet (zusätzlich zu den Zugangsdaten z. B. Client/Subclient gewählt)?"""
        return self.configured()

    def test(self):
        """Verbindung prüfen. Gibt ein dict mit Infos zurück, wirft TargetError."""
        raise NotImplementedError

    def info(self):
        """Eingestellte Werte für die Oberfläche: [(Bezeichnung, Wert)]. Nie Geheimnisse – nur „gesetzt“/„nicht gesetzt“."""
        return []

    def choices(self, env):
        """Auswahlliste für ein Einstellungsfeld, das vom Ziel selbst abgefragt wird: [{'value','label'}]."""
        raise TargetError(f"{self.label}: für {env} gibt es keine Auswahlliste.")

    def describe(self):
        return {"id": self.id, "label": self.label, "kind": self.kind, "capabilities": sorted(self.capabilities),
                "configured": bool(self.configured()), "ready": bool(self.ready()),
                "info": [{"label": k, "value": v} for k, v in self.info()]}

    # ───────── Sicherungslauf ─────────
    def start(self, items, options, log_cb):
        """Lauf anstoßen. items: [(container, ordner)] der neu erzeugten Sicherungen (für Push-Ziele die
        Quelle, für Pull-Ziele nur zur Information). Rückgabe: Run."""
        raise TargetError(f"{self.label} kann keine Läufe starten.")

    def wait(self, run, progress_cb=None, max_wait=7200):
        """Auf das Ende des Laufs warten. progress_cb(prozent, text). Rückgabe: RunResult."""
        raise TargetError(f"{self.label} kann keine Läufe überwachen.")

    def cancel(self, run, log_cb):
        """Einen beim Ziel laufenden Lauf abbrechen (soweit das Ziel das kann). Fehler dürfen nicht stören."""

    def after_success(self, run, log_cb):
        """Nach erfolgreichem Lauf (z. B. Synthetic Full). Fehler dürfen den Lauf nicht abbrechen."""

    # ───────── Stände ─────────
    def list_folders(self, container):
        """{ordner: {'time', 'size', 'ref'}} aller Stände des Containers; None, wenn nicht erreichbar."""
        raise TargetError(f"{self.label} kann keine Stände auflisten.")

    def list_containers(self):
        """Namen aller Container-Ordner im Ziel (auch lokal längst gelöschte); None, wenn nicht erreichbar."""
        raise TargetError(f"{self.label} kann keine Container-Ordner auflisten.")

    def validate_entries(self, entries):
        """Für Ziele ohne Ordnerliste: welche Vermerke (dict mit container, folder, verified, local_deleted) sind noch
        gedeckt? Rückgabe: Menge von (container, ordner) oder None, wenn das Ziel nicht antwortet."""
        return None

    # ───────── Restore ─────────
    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        """Stand ins Staging-Verzeichnis `dest` zurückholen (dort entsteht <container>/<ordner>/…)."""
        raise TargetError(f"{self.label} kann keine Stände zurückholen.")

    # ───────── Direkt gesicherte Pfade ─────────
    def paths_status(self):
        raise TargetError(f"{self.label} kennt keine Direkt-Pfade.")

    def paths_sync(self):
        raise TargetError(f"{self.label} kennt keine Direkt-Pfade.")
