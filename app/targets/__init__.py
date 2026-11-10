"""Registrierte Backup-Ziele. Welche aktiv sind, steht in der Umgebungsvariable TARGETS (kommagetrennt,
Standard: commvault). Zugangsdaten kommen wie bisher aus der .env."""
import os

from .base import TargetError  # noqa: F401 – wird von außen importiert
from .command import CommandTarget
from .commvault import CommvaultTarget
from .pbs import PbsTarget
from .restic import ResticTarget
from .veeam import VeeamTarget

# Feste Reihenfolge – so erscheinen die Ziele auch in den Einstellungen
REGISTRY = {"commvault": CommvaultTarget, "veeam": VeeamTarget, "pbs": PbsTarget, "restic": ResticTarget, "command": CommandTarget}


def enabled_ids():
    raw = os.environ.get("TARGETS", "").strip()
    ids = [t.strip().lower() for t in raw.split(",") if t.strip()] or ["commvault"]
    return [i for i in dict.fromkeys(ids) if i in REGISTRY]


def get(target_id):
    cls = REGISTRY.get(str(target_id).lower())
    if cls is None:
        raise TargetError(f"Unbekanntes Ziel '{target_id}'.")
    return cls()


def all_targets():
    return [get(i) for i in enabled_ids()]


def clean_runs(raw, legacy_level=None):
    """Prüft die Zielauswahl eines Auftrags: [{'id': 'restic'}, {'id': 'commvault', 'level': 2}].
    Ohne Angabe gilt die ältere Commvault-Stufe (1 Voll, 2 Inkrementell). Wirft TargetError."""
    if raw is None:
        raw = [{"id": "commvault", "level": legacy_level}] if legacy_level else []
    out, seen = [], set()
    for item in raw:
        item = {"id": item} if isinstance(item, str) else dict(item)
        target_id = str(item.get("id", "")).lower()
        if target_id in seen:
            continue
        seen.add(target_id)
        get(target_id)   # unbekannt → TargetError
        entry = {"id": target_id}
        if item.get("level") not in (None, "", 0, "0"):
            try:
                level = int(item["level"])
            except (TypeError, ValueError):
                raise TargetError("Die Backup-Stufe muss eine Zahl sein.")
            if level not in (1, 2):
                raise TargetError("Backup-Stufe muss Voll (1) oder Inkrementell (2) sein.")
            entry["level"] = level
        out.append(entry)
    return out


def describe_all():
    """Alle bekannten Ziele (auch nicht aktivierte) mit Kennzeichen 'enabled' – die Einstellungen zeigen sie alle,
    damit man sieht, was fehlt. Immer in der festen Reihenfolge der Registry."""
    from .. import target_config
    enabled = enabled_ids()
    out = []
    for target_id in REGISTRY:
        info = get(target_id).describe()
        info["enabled"] = target_id in enabled
        info["config"] = {"fields": target_config.public(target_id), "enabled_source": target_config.enabled_source()}
        out.append(info)
    return out
