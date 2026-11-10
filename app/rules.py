"""Backup-Umfang: Was wird von einem Container gesichert, was nicht?

Mehrere Schutzebenen, damit ein Mediencontainer nie versehentlich Terabyte packt:
  1. Netzwerk-/Fremdspeicher (CIFS, NFS, FUSE …) wird standardmäßig übersprungen
  2. Muster für Volume-Namen und Zielpfade (global) sowie Regeln pro Container (UI)
  3. Container-Labels:  mobydisk.skip=/media,/data/movies | all   mobydisk.include=/pfad
                        mobydisk.exclude-glob=*.cache,thumbs     mobydisk.backup=false
     (ältere cvhelper.*-Labels gelten weiter)
  4. Größenlimit pro Mount (Standard 100 GB, höchstens 80 % des freien Platzes) – wird beim Packen
     erzwungen, ein zu großes Archiv wird abgebrochen und wieder gelöscht
  5. Ausschlussmuster für Dateien/Ordner innerhalb eines Volumes

Dazu kommen Einstellungen pro Container (UI oder Label): Modus, Stop-Timeout, Hooks, Datenbank-Dump, Image.
"""
import fnmatch
import os
import shutil
import stat
import time

from . import dbdump, docker_api
from .config import config, settings

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs3", "nfs4", "9p", "ceph", "glusterfs", "davfs", "afs",
              "lustre", "gpfs", "beegfs", "ocfs2", "ncpfs"}

DEFAULTS = {
    "skip_network": True,
    "max_mount_gb": 100,
    "exclude_volume_patterns": [],
    "exclude_dest_patterns": [],
    "exclude_globs": [],
    "own_registries": [],
    "cv_direct_paths": [],      # Host-Pfade, die Commvault direkt (dateiweise) sichert – der Helper lässt sie weg
    "cv_synth_full_days": 7,    # nach einem Helper-Lauf Synthetic Full, wenn der letzte Full älter ist (0 = aus)
    "containers": {},
}

# Systempfade des Hosts: gehören nicht zu den Anwendungsdaten eines Containers
SYSTEM_PREFIXES = ("/etc", "/lib", "/lib64", "/usr", "/proc", "/sys", "/dev", "/run", "/var/run", "/boot", "/bin",
                   "/sbin")
SETTING_KEYS = ("mode", "stop_timeout", "pre", "post", "dump", "image")

_size_cache = {}
SIZE_TTL = 300


class RulesError(Exception):
    pass


# ───────── Regeln lesen/schreiben ─────────
def get_rules():
    stored = settings.get("backup_rules") or {}
    rules = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    rules["containers"] = dict(stored.get("containers") or {})
    return rules


def _lines(value):
    items = value.splitlines() if isinstance(value, str) else (value or [])
    return [str(x).strip() for x in items if str(x).strip()]


def update_rules(body):
    cur = get_rules()
    new = dict(cur)
    if "skip_network" in body:
        new["skip_network"] = bool(body["skip_network"])
    if "max_mount_gb" in body:
        try:
            gb = int(body["max_mount_gb"] or 0)
        except (TypeError, ValueError):
            raise RulesError("Größenlimit muss eine Zahl sein.")
        if gb < 0 or gb > 1000000:
            raise RulesError("Größenlimit muss zwischen 0 und 1000000 GB liegen (0 = aus).")
        new["max_mount_gb"] = gb
    for key in ("exclude_volume_patterns", "exclude_dest_patterns", "exclude_globs", "own_registries"):
        if key in body:
            new[key] = _lines(body[key])
    if "cv_direct_paths" in body:
        new["cv_direct_paths"] = clean_direct_paths(_lines(body["cv_direct_paths"]))
    if "cv_synth_full_days" in body:
        try:
            days = int(body["cv_synth_full_days"] or 0)
        except (TypeError, ValueError):
            raise RulesError("Synthetic-Full-Abstand muss eine Zahl sein.")
        if days < 0 or days > 365:
            raise RulesError("Synthetic-Full-Abstand muss zwischen 0 und 365 Tagen liegen (0 = aus).")
        new["cv_synth_full_days"] = days
    settings.update({"backup_rules": new})
    return new


def clean_direct_paths(paths):
    """Prüft die Liste der direkt von Commvault gesicherten Host-Pfade; verschachtelte Einträge fallen weg."""
    out = []
    for raw in paths:
        p = os.path.normpath(raw)
        if not raw.startswith("/") or ".." in raw.split("/"):
            raise RulesError(f"'{raw}': bitte einen absoluten Host-Pfad angeben.")
        if p == "/" or any(p == s or p.startswith(s + "/") for s in SYSTEM_PREFIXES + ("/var/lib/docker/containers",)):
            raise RulesError(f"'{p}' ist ein Systempfad und kann nicht direkt gesichert werden.")
        if _overlaps(p, config.backup_root):
            raise RulesError(f"'{p}' überschneidet sich mit dem Backup-Ordner {config.backup_root} "
                             "(der ist ohnehin im Subclient).")
        if os.path.normpath(config.volumes_root) == p:
            raise RulesError(f"'{p}' enthält alle Docker-Volumes – bitte einzelne Volumes angeben.")
        out.append(p)
    out = sorted(set(out))
    return [p for p in out if not any(p != q and p.startswith(q.rstrip("/") + "/") for q in out)]


def direct_paths(rules=None):
    rules = rules or get_rules()
    return [os.path.normpath(p) for p in rules.get("cv_direct_paths") or []]


def update_container_rules(name, body):
    cur = get_rules()
    entry = dict(cur["containers"].get(name) or {})
    for key in ("skip", "include", "exclude_globs"):
        if key in body:
            entry[key] = _lines(body[key])
    # ein Pfad kann nicht gleichzeitig ein- und ausgeschlossen sein: der zuletzt gesetzte gewinnt
    for key, other in (("skip", "include"), ("include", "skip")):
        if key in body:
            entry[other] = [p for p in entry.get(other, []) if p not in entry.get(key, [])]
    entry.update(_clean_settings(body))
    for key in SETTING_KEYS:
        if entry.get(key) in (None, "", "auto", 0):
            entry.pop(key, None)
    if not any(entry.get(k) for k in ("skip", "include", "exclude_globs") + SETTING_KEYS):
        cur["containers"].pop(name, None)
    else:
        cur["containers"][name] = entry
    settings.update({"backup_rules": cur})
    return entry


def _clean_settings(body):
    out = {}
    if "mode" in body:
        if body["mode"] not in ("", None, "auto", "live", "stop"):
            raise RulesError("Modus muss 'live', 'stop' oder leer (wie der Auftrag) sein.")
        out["mode"] = body["mode"] or ""
    if "stop_timeout" in body:
        try:
            value = int(body["stop_timeout"] or 0)
        except (TypeError, ValueError):
            raise RulesError("Stop-Timeout muss eine Zahl (Sekunden) sein.")
        if value < 0 or value > 3600:
            raise RulesError("Stop-Timeout muss zwischen 1 und 3600 Sekunden liegen (leer = Standard).")
        out["stop_timeout"] = value
    for key in ("pre", "post"):
        if key in body:
            value = str(body[key] or "").strip()
            if len(value) > 4000:
                raise RulesError("Hook-Befehl ist zu lang.")
            out[key] = value
    if "dump" in body:
        if body["dump"] not in ("", None, "auto", "off") + dbdump.TYPES:
            raise RulesError("Ungültiger Dump-Typ.")
        out["dump"] = body["dump"] or ""
    if "image" in body:
        if body["image"] not in ("", None, "always", "never"):
            raise RulesError("Image-Einstellung muss 'always', 'never' oder leer sein.")
        out["image"] = body["image"] or ""
    return out


def container_settings(name, labels=None, image=None, env=None, rules=None):
    """Wirksame Einstellungen eines Containers. Reihenfolge: Oberfläche vor Label vor Standard."""
    rules = rules or get_rules()
    ui = rules["containers"].get(name, {})
    labels = labels or {}

    def pick(key, label):
        if ui.get(key) not in (None, "", "auto", 0):
            return ui[key], "ui"
        value = str(docker_api.own_label(labels, label, "")).strip()
        if value:
            return value, "label"
        return None, None

    mode, mode_src = pick("mode", "mode")
    if mode not in ("live", "stop"):
        mode, mode_src = None, None
    timeout, timeout_src = pick("stop_timeout", "stop-timeout")
    try:
        timeout = int(timeout) if timeout else None
    except (TypeError, ValueError):
        timeout, timeout_src = None, None
    pre, pre_src = pick("pre", "pre")
    post, post_src = pick("post", "post")
    dump_setting, dump_src = pick("dump", "dump")
    detected = dbdump.detect(image, env)
    if dump_setting == "off":
        dump = None
    elif dump_setting in dbdump.TYPES:
        dump = dump_setting
    else:
        dump_setting, dump = "auto", detected
    img, img_src = pick("image", "image")
    if img not in ("always", "never"):
        img, img_src = None, None
    return {
        "mode": mode, "mode_source": mode_src,
        "stop_timeout": timeout, "stop_timeout_source": timeout_src,
        "pre": pre, "pre_source": pre_src, "post": post, "post_source": post_src,
        "dump": dump, "dump_setting": dump_setting, "dump_source": dump_src, "dump_detected": detected,
        "image": img, "image_source": img_src,
    }


# ───────── Dateisystem-Erkennung ─────────
def _mount_table():
    table = []
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    table.append((parts[1].replace("\\040", " "), parts[2]))
    except OSError:
        pass
    table.sort(key=lambda item: len(item[0]), reverse=True)
    return table


def fstype_of(path, table=None):
    if not path:
        return None
    table = table if table is not None else _mount_table()
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    for mount_point, fstype in table:
        if real == mount_point or real.startswith(mount_point.rstrip("/") + "/"):
            return fstype
    return None


def is_network_fs(fstype):
    return bool(fstype) and (fstype in NETWORK_FS or fstype.startswith("fuse."))


def volume_external_reason(volume_name):
    """Fragt Docker nach Treiber/Optionen des Volumes (z. B. type=cifs, NFS-Plugin)."""
    try:
        attrs = docker_api.client().volumes.get(volume_name).attrs
    except Exception:
        return None
    driver = attrs.get("Driver") or "local"
    opts = attrs.get("Options") or {}
    if driver != "local":
        return f"Volume-Treiber '{driver}'"
    vtype = (opts.get("type") or "").lower()
    if vtype in NETWORK_FS or vtype.startswith("fuse"):
        return f"Netzwerkspeicher ({vtype})"
    if str(opts.get("device", "")).startswith("//"):
        return "Netzwerkspeicher (SMB)"
    return None


# ───────── Labels ─────────
def label_rules(labels):
    labels = labels or {}

    def csv(key):
        return [x.strip() for x in str(docker_api.own_label(labels, key, "")).split(",") if x.strip()]

    skip = csv("skip")
    return {
        "skip_all": "all" in [s.lower() for s in skip] or str(docker_api.own_label(labels, "backup", "")).lower() in ("false", "off", "0", "no"),
        "skip": [s for s in skip if s.lower() != "all"],
        "include": csv("include"),
        "globs": csv("exclude-glob"),
    }


# ───────── Entscheidung ─────────
def size_limit_bytes(rules=None):
    rules = rules or get_rules()
    gb = rules.get("max_mount_gb") or 0
    if not gb:
        return None
    limit = gb * 2 ** 30
    try:
        free = shutil.disk_usage(config.backup_root).free
        limit = min(limit, int(free * 0.8))
    except OSError:
        pass
    return limit


def _overlaps(a, b):
    a, b = os.path.normpath(a or "/"), os.path.normpath(b or "/")
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def volume_path(mount):
    """Pfad der Daten im Helper (Volumes unter volumes_root, Bind-Mounts über die Host-Freigaben)."""
    if mount.get("type") == "volume":
        return os.path.join(config.volumes_root, mount.get("name") or "", "_data")
    return docker_api.to_local(mount.get("source"))


def _is_system(path):
    path = os.path.normpath(path or "/")
    return any(path == p or path.startswith(p + "/") for p in SYSTEM_PREFIXES)


def classify(mount, container_name, labels=None, rules=None, table=None, use_docker=False, is_self=False):
    """Entscheidet für einen Mount: {'action': 'backup'|'skip', 'reason': …, 'text': …, …}."""
    rules = rules or get_rules()
    c_rules = rules["containers"].get(container_name, {})
    lab = label_rules(labels)
    dest = mount.get("destination") or ""
    name = mount.get("name") or ""
    path = volume_path(mount)
    fstype = fstype_of(path, table) if path else None
    kind = "dir"
    if mount.get("type") == "bind" and path:
        try:
            mode = os.stat(path).st_mode
            kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "special"
        except OSError:
            kind = "missing"
    info = {"destination": dest, "type": mount.get("type"), "name": name or None, "source": mount.get("source"),
            "readonly": bool(mount.get("readonly")), "fstype": fstype, "guard": True, "local": path, "kind": kind}

    # Direkt von Commvault gesicherte Host-Pfade: ganzer Mount → überspringen, Unterordner → beim Packen weglassen
    host = os.path.normpath(mount.get("source") or "") if mount.get("source") else None
    direct = direct_paths(rules) if host else []
    covering = next((p for p in direct if host == p or host.startswith(p.rstrip("/") + "/")), None)
    subs = [{"host": p, "rel": os.path.relpath(p, host), "local": os.path.join(path, os.path.relpath(p, host)) if path else None}
            for p in direct if p.startswith(host.rstrip("/") + "/")] if host else []

    def done(action, reason, text, guard=True):
        if action == "backup" and subs:
            info["direct_sub"] = subs
            text += " – ohne " + ", ".join(s["rel"] + "/" for s in subs) + " (direkt über Commvault)"
        info.update(action=action, reason=reason, text=text, guard=guard)
        return info

    if covering:
        return done("skip", "cv_direct", f"wird direkt von Commvault gesichert ({covering})")
    if mount.get("type") == "bind":   # Sicherheitsregeln, die auch 'include' nicht aufhebt
        src = mount.get("source") or ""
        if _overlaps(src, config.backup_root):
            return done("skip", "backup_root", "enthält das Backup-Ziel (würde sich selbst sichern)")
        root = os.path.normpath(config.volumes_root)
        if (is_self and _overlaps(src, root)) or os.path.normpath(src) == root or root.startswith(os.path.normpath(src) + "/"):
            return done("skip", "volumes_root", "enthält alle Docker-Volumes (gehören nicht zu diesem Container)")
        if is_self and os.path.normpath(src) in docker_api.host_share_paths():
            return done("skip", "host_share", "Host-Freigabe zum Lesen fremder Bind-Mounts (gehört nicht zum Helper)")
        if _is_system(src):
            return done("skip", "system", "Systempfad des Hosts (z. B. Zeitzone, Socket) – wird nicht gesichert")
    if dest in c_rules.get("include", []) or dest in lab["include"]:
        return done("backup", "included", "explizit eingeschlossen (kein Größenlimit)", guard=False)
    if mount.get("type") == "bind" and any(os.path.normpath(mount.get("source") or "/") == p or p.startswith(
            os.path.normpath(mount.get("source") or "/").rstrip("/") + "/") for p in docker_api.host_share_paths()):
        return done("skip", "host_share", "bindet eine ganze Host-Freigabe des Helpers ein (fremde Daten) – "
                                         "bei Bedarf gezielt einschließen")
    if dest in c_rules.get("skip", []) or dest in lab["skip"] or lab["skip_all"]:
        return done("skip", "manual", "manuell ausgeschlossen" + (" (Label)" if dest in lab["skip"] or lab["skip_all"] else ""))
    for pat in rules["exclude_volume_patterns"]:
        if name and fnmatch.fnmatch(name.lower(), pat.lower()):
            return done("skip", "pattern", f"Volume-Muster '{pat}'")
    for pat in rules["exclude_dest_patterns"]:
        if fnmatch.fnmatch(dest, pat):
            return done("skip", "pattern", f"Pfad-Muster '{pat}'")
    if rules["skip_network"]:
        if is_network_fs(fstype):
            return done("skip", "network", f"Netzwerkspeicher ({fstype})")
        if use_docker and mount.get("type") == "volume":
            reason = volume_external_reason(name)
            if reason:
                return done("skip", "network", reason)
    if mount.get("type") == "bind":
        if not path:
            return done("skip", "unreachable", "Host-Pfad ist in Moby Disk nicht freigegeben (siehe docs/scope-and-settings.md: Host-Freigaben)")
        if kind == "missing":
            return done("skip", "missing", "existiert auf dem Host nicht")
        if kind == "special":
            return done("skip", "special", "weder Ordner noch Datei (Socket/Gerät)")
        if kind == "file":
            return done("backup", "default", "Einzeldatei – wird gesichert")
    return done("backup", "default", "wird gesichert")


def plan_mounts(container_name, mounts, labels=None, rules=None, use_docker=False, is_self=False):
    rules = rules or get_rules()
    table = _mount_table()
    return [classify(m, container_name, labels, rules, table, use_docker, is_self)
            for m in mounts if m.get("type") in ("volume", "bind")]


def exclude_globs(container_name, labels=None, rules=None):
    rules = rules or get_rules()
    return list(rules["exclude_globs"]) + list(rules["containers"].get(container_name, {}).get("exclude_globs", [])) \
        + label_rules(labels)["globs"]


def excluded(rel, patterns):
    """True, wenn der relative Pfad zu einem Ausschlussmuster passt. Muster ohne '/' gelten für jede
    Pfadkomponente (z. B. 'cache', '*.tmp'); 'dir/**' schließt ein Verzeichnis samt Inhalt aus."""
    if not patterns:
        return False
    parts = rel.split("/")
    for pat in patterns:
        if pat.endswith("/**") and (rel == pat[:-3] or rel.startswith(pat[:-2])):
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
        if "/" not in pat and any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False


# ───────── Größenschätzung (für die Vorschau) ─────────
def bounded_size(path, limit=None, budget=8.0, skip=()):
    """Summiert Dateigrößen, bricht aber bei 'limit' oder nach 'budget' Sekunden ab. 'skip': Ordner, die nicht
    mitgezählt werden (z. B. direkt von Commvault gesicherte). Rückgabe: (bytes, vollständig?)."""
    skip = tuple(sorted(os.path.normpath(p) for p in skip or ()))
    key = (path, limit, skip)
    cached = _size_cache.get(key)
    if cached and time.time() - cached[0] < SIZE_TTL:
        return cached[1]
    start, total = time.time(), 0
    stack = [path]
    complete = True
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if not skip or os.path.normpath(entry.path) not in skip:
                                stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if (limit and total > limit) or time.time() - start > budget:
                        complete = False
                        stack.clear()
                        break
        except OSError:
            continue
    result = (total, complete)
    _size_cache[key] = (time.time(), result)
    return result


def plan_with_sizes(container_name, mounts, labels=None, is_self=False):
    rules = get_rules()
    limit = size_limit_bytes(rules)
    plan = plan_mounts(container_name, mounts, labels, rules, use_docker=True, is_self=is_self)
    for item in plan:
        item["size"] = None
        item["size_complete"] = None
        if item["action"] != "backup":
            continue
        path = item.get("local")
        if path and item.get("kind") == "file":
            try:
                item["size"], item["size_complete"] = os.path.getsize(path), True
            except OSError:
                pass
        elif path and os.path.isdir(path):
            cap = limit if item["guard"] else None
            item["size"], item["size_complete"] = bounded_size(   # ohne die direkt gesicherten Unterordner
                path, cap, skip=[s["local"] for s in item.get("direct_sub") or [] if s.get("local")])
            if item["guard"] and limit and not item["size_complete"] and item["size"] > limit:
                item.update(action="skip", reason="too_large",
                            text=f"größer als das Limit ({_gb(limit)}) – wird übersprungen")
    return plan, limit


def _gb(n):
    return f"{n / 2 ** 30:.0f} GB" if n >= 2 ** 30 else f"{n / 2 ** 20:.0f} MB"
