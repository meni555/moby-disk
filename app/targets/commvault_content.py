"""Inhalt des Commvault-Subclients: Backup-Ordner des Helpers plus die direkt gesicherten Host-Pfade.

Der Helper verwaltet nur die Pfade, die er selbst eingetragen hat (gemerkt in kv 'cv_content_managed').
Was jemand in Commvault zusätzlich eingetragen hat, bleibt unangetastet; Filter/Ausnahmen ebenso."""
import os

from .. import rules, store
from .commvault_api import Commvault, CVError
from ..config import config

KV_KEY = "cv_content_managed"


def _norm(p):
    return os.path.normpath(p) if p else p


def desired_paths(rule_set=None):
    return [_norm(config.backup_root)] + rules.direct_paths(rule_set)


def _client():
    cv = Commvault()
    identity = config.effective_cv_identity()
    if not cv.configured or not identity.get("subclient_id"):
        raise CVError("Commvault ist nicht konfiguriert oder kein Subclient gewählt.")
    return cv, identity


def status(rule_set=None, cv=None, identity=None):
    if cv is None:
        cv, identity = _client()
    current, other = cv.subclient_content(identity["subclient_id"])
    current = [_norm(p) for p in current]
    desired = desired_paths(rule_set)
    managed = [_norm(p) for p in store.kv_get(KV_KEY, []) or []]
    missing = [p for p in desired if p not in current]
    remove = [p for p in current if p in managed and p not in desired]
    foreign = [p for p in current if p not in desired and p not in managed]
    return {"subclient": identity.get("subclient_name") or identity["subclient_id"], "current": current,
            "desired": desired, "missing": missing, "remove": remove, "foreign": foreign,
            "filters": len(other), "in_sync": not missing and not remove, "_other": other}


def sync(rule_set=None):
    """Bringt den Subclient-Inhalt auf den gewünschten Stand und prüft das Ergebnis per erneutem Lesen."""
    cv, identity = _client()
    st = status(rule_set, cv, identity)
    if not st["in_sync"]:
        paths = [p for p in st["current"] if p not in st["remove"]] + st["missing"]
        cv.set_subclient_content(identity["subclient_id"], paths, st["_other"])
    store.kv_set(KV_KEY, st["desired"])
    after = status(rule_set, cv, identity)
    if not after["in_sync"]:
        raise CVError(f"Commvault zeigt nach dem Schreiben noch einen anderen Inhalt: {after['current']}")
    after["changed"] = not st["in_sync"]
    return after


def public(st):
    return {k: v for k, v in st.items() if not k.startswith("_")}
