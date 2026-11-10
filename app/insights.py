"""Auswertungen aus den vorhandenen Sicherungen: Schutzstatus (RPO) und Speicher-Forecast.
Reine Berechnungen – keine Schreibzugriffe."""
import shutil
import time

from . import backup, docker_api, notify, rules, store
from .config import config

INF = float("inf")
HORIZON_DAYS = 365


# ───────── Schutzstatus ─────────
def _ts_epoch(ts):
    parsed = backup._parse_ts(ts)
    return parsed.timestamp() if parsed else None


def covered_names(schedules, containers):
    """Container → Liste der aktiven Zeitpläne, die sie sichern."""
    result = {c["name"]: [] for c in containers}
    for sched in schedules:
        if not sched.get("enabled"):
            continue
        if sched.get("scope") == "all_with_data":
            names = [c["name"] for c in containers if docker_api.backup_candidate(c)]
        else:
            names = [n for n in sched.get("containers", []) if n in result]
        for name in names:
            result[name].append(sched)
    return result


def target_summary():
    """Container → {'count', 'last', 'targets'}: in Backup-Zielen vermerkte Sicherungen (jeder Ordner einmal, egal in
    wie vielen Zielen) und Zeitpunkt der jüngsten (aus dem Ordnernamen – wann gesichert wurde, nicht wann bestätigt)."""
    out, seen = {}, set()
    for entry in store.entries():
        when = _ts_epoch(entry["folder"]) or entry["verified"]
        info = out.setdefault(entry["container"], {"count": 0, "last": None, "targets": {}})
        if (entry["container"], entry["folder"]) not in seen:
            seen.add((entry["container"], entry["folder"]))
            info["count"] += 1
        info["last"] = max(info["last"] or 0, when or 0) or None
        t = info["targets"].setdefault(entry["target"], {"count": 0, "last": None})
        t["count"] += 1
        t["last"] = max(t["last"] or 0, when or 0) or None
    return out


def protection_info(container, local_backups, schedules_for, rpo_hours, cv_last, now=None):
    """Schutzstatus eines Containers – zählt lokale Kopien und Sicherungen, die nur noch in Backup-Zielen liegen.
    state: ok (frisch genug) | stale (zu alt) | none (nie gesichert) | n/a (keine Daten)."""
    now = now or time.time()
    if not container.get("has_data"):
        return {"state": "n/a", "last_backup": None, "age_hours": None, "scheduled": bool(schedules_for),
                "cv_last": cv_last}
    local_last = _ts_epoch(local_backups[0]["ts"]) if local_backups else None
    last = max(local_last or 0, cv_last or 0) or None
    if last is None:
        return {"state": "none", "last_backup": None, "age_hours": None, "scheduled": bool(schedules_for),
                "cv_last": cv_last}
    age = (now - last) / 3600.0
    return {"state": "ok" if age <= rpo_hours else "stale", "last_backup": last, "age_hours": round(age, 1),
            "scheduled": bool(schedules_for), "cv_last": cv_last, "cv_only": local_last is None}


def annotate_containers(items, schedules, rpo_hours=None):
    """Ergänzt die Container-Liste um lokale Sicherungen, Größe und Schutzstatus."""
    rpo = rpo_hours or notify.get_config()["rpo_hours"]
    cover = covered_names(schedules, items)
    now = time.time()
    rule_set, table = rules.get_rules(), rules._mount_table()
    own_regs = rule_set.get("own_registries") or ()
    in_cv = target_summary()
    for item in items:
        plan = [rules.classify(m, item["name"], item.get("labels"), rule_set, table, is_self=item.get("is_self", False))
                for m in item["mounts"] if m.get("type") in ("volume", "bind")]
        item["mount_plan"] = plan
        item["skipped_mounts"] = sum(1 for d in plan if d["action"] == "skip" and d["reason"] != "cv_direct")
        item["direct_mounts"] = sum(1 for d in plan if d["reason"] == "cv_direct" or d.get("direct_sub"))
        local = backup.list_local_backups(item["name"])
        item["local_backups"] = len(local)
        item["local_size"] = sum(b.get("size_bytes") or 0 for b in local)
        cs = rules.container_settings(item["name"], item.get("labels"), item.get("image"), item.get("env_keys"), rule_set)
        origin = docker_api.image_origin(item.get("image_id") or item.get("image"), own_regs)
        item["backup_settings"] = {"dump": cs["dump"], "mode": cs["mode"], "pre": bool(cs["pre"]),
                                   "post": bool(cs["post"]), "image": cs["image"], "stop_timeout": cs["stop_timeout"],
                                   "image_origin": origin["origin"], "image_origin_text": origin["text"]}
        cv = in_cv.get(item["name"]) or {}
        item["cv_backups"] = cv.get("count", 0)
        item["target_backups"] = cv.get("targets", {})
        item["protection"] = protection_info(item, local, cover.get(item["name"], []), rpo, cv.get("last"), now)
        if item.get("clone_of"):
            item["protection"] = {"state": "clone", "scheduled": False}
        elif not docker_api.backup_candidate(item) and item.get("has_data"):
            item["protection"]["state"] = "excluded"   # per Label mobydisk.backup=false
        elif item.get("has_data") and plan and all(d["reason"] == "cv_direct" for d in plan):
            item["protection"]["state"] = "direct"     # alle Daten sichert Commvault direkt
        elif item.get("has_data") and plan and item["skipped_mounts"] == len(plan):
            item["protection"]["state"] = "external"   # alle Daten liegen extern bzw. sind ausgeschlossen
    return items


def digest_inputs(schedules):
    items = annotate_containers(docker_api.list_containers(), schedules)
    return items, schedules, forecast(items, schedules)


# ───────── Forecast ─────────
def growth_per_day(points):
    """Lineare Regression der Archivgröße über die Zeit (Bytes/Tag) aus den letzten 30 Sicherungen.
    Schrumpfen wird als 0 gewertet (konservativ). Rückgabe: (bytes_pro_tag, konfidenz)."""
    pts = points[-30:]
    if len(pts) < 2:
        return 0.0, "niedrig"
    t0 = pts[0][0]
    xs = [(t - t0) / 86400.0 for t, _ in pts]
    ys = [float(v) for _, v in pts]
    span = xs[-1] - xs[0]
    if span < 1.0 / 24:   # alle Sicherungen innerhalb einer Stunde – kein Trend ableitbar
        return 0.0, "niedrig"
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom else 0.0
    confidence = "hoch" if len(pts) >= 8 and span >= 7 else "mittel" if len(pts) >= 3 and span >= 1 else "niedrig"
    return max(slope, 0.0), confidence


def _cap_for(schedules_for):
    """Wie viele Kopien bleiben im Dauerbetrieb höchstens erhalten? (strengster Zeitplan gewinnt)."""
    caps = []
    for sched in schedules_for:
        runs_per_day = len(sched.get("days", [])) / 7.0
        by_count = sched.get("retention") or INF
        by_age = (sched.get("retention_days") or 0) * runs_per_day or INF
        caps.append(max(1.0, min(by_count, by_age)))
    return min(caps) if caps else None


def forecast(containers, schedules, keep_override=None, runs_mult=1.0, horizon=HORIZON_DAYS):
    """Prognose der lokalen Belegung des Backup-Ordners.

    Modell je Container: Kopien(t) = min(heutige + Läufe/Tag · t, Obergrenze aus der Aufbewahrung),
    Größe je Kopie wächst linear mit dem Trend der bisherigen Sicherungen. Container ohne Zeitplan
    bleiben konstant. Bewusst konservativ: alle Kopien zählen mit der dann aktuellen Größe."""
    cover = covered_names(schedules, containers)
    rows, raw = [], []
    for c in containers:
        name = c["name"]
        entries = backup.list_local_backups(name)             # neueste zuerst
        points = sorted((_ts_epoch(e["ts"]), e.get("size_bytes") or 0) for e in entries if _ts_epoch(e["ts"]))
        sched_for = cover.get(name, [])
        if not entries and not sched_for:
            continue
        size0 = points[-1][1] if points else 0
        copies0 = len(entries)
        actual = sum(e.get("size_bytes") or 0 for e in entries)
        growth, confidence = growth_per_day(points)
        runs = sum(len(s.get("days", [])) / 7.0 for s in sched_for) * runs_mult
        cap = _cap_for(sched_for)
        if keep_override and runs > 0:
            cap = float(keep_override)
        raw.append({"name": name, "size0": size0, "copies0": copies0, "actual": actual, "growth": growth,
                    "confidence": confidence, "runs": runs, "cap": cap, "scheduled": bool(sched_for)})

    def usage(r, t):
        if t == 0 or r["runs"] <= 0:
            return float(r["actual"])
        copies = r["copies0"] + r["runs"] * t
        if r["cap"] is not None:
            copies = min(copies, r["cap"])
        return copies * (r["size0"] + r["growth"] * t)

    total_at = lambda t: sum(usage(r, t) for r in raw)
    for r in raw:
        rows.append({
            "name": r["name"], "copies": r["copies0"], "size_per_copy": r["size0"],
            "growth_per_day": r["growth"], "confidence": r["confidence"],
            "runs_per_day": round(r["runs"], 3), "scheduled": r["scheduled"],
            "cap": None if r["cap"] is None or r["cap"] == INF else r["cap"],
            "unlimited": r["runs"] > 0 and (r["cap"] is None or r["cap"] == INF),
            "usage_now": usage(r, 0), "usage_30": usage(r, 30), "usage_90": usage(r, 90),
            "usage_365": usage(r, 365),
        })
    rows.sort(key=lambda x: x["usage_365"], reverse=True)

    try:
        disk = shutil.disk_usage(config.backup_root)
        free, fs_total = disk.free, disk.total
    except OSError:
        free, fs_total = None, None
    reserve = max((r["size0"] for r in raw), default=0)    # Platz für einen Restore im Staging
    now_total = total_at(0)
    days_until_full = None
    if free is not None:
        budget = free - reserve
        for t in range(1, 731):
            if total_at(t) - now_total > budget:
                days_until_full = t
                break
    series = [[t, total_at(t)] for t in sorted(set(list(range(0, horizon + 1, 5)) + [30, 90, horizon]))]

    warnings = []
    if days_until_full is not None and days_until_full <= 30:
        warnings.append({"level": "error", "text": f"Der Speicher reicht bei der aktuellen Planung nur noch etwa {days_until_full} Tage."})
    elif days_until_full is not None and days_until_full <= 90:
        warnings.append({"level": "warn", "text": f"Der Speicher ist voraussichtlich in etwa {days_until_full} Tagen voll."})
    unlimited = [r["name"] for r in rows if r["unlimited"]]
    if unlimited:
        warnings.append({"level": "warn", "text": "Ohne Aufbewahrung wächst die Belegung unbegrenzt: " + ", ".join(unlimited[:6]) + (" …" if len(unlimited) > 6 else "")})
    if free is not None and reserve and free < reserve * 1.5:
        warnings.append({"level": "warn", "text": "Der freie Platz reicht kaum für einen Restore (Staging) der größten Sicherung."})
    if not any(r["scheduled"] for r in rows):
        warnings.append({"level": "info", "text": "Es ist kein Zeitplan aktiv – die Prognose zeigt nur den heutigen Stand."})
    if any(r["confidence"] == "niedrig" and r["scheduled"] for r in rows):
        warnings.append({"level": "info", "text": "Für einige Container gibt es noch zu wenige Sicherungen für einen Wachstumstrend (Annahme: keine Größenänderung)."})

    return {
        "free": free, "fs_total": fs_total, "reserve": reserve, "used_local": now_total,
        "days_until_full": days_until_full, "series": series,
        "totals": {"now": now_total, "d30": total_at(30), "d90": total_at(90), "d365": total_at(365)},
        "containers": rows, "warnings": warnings,
        "assumptions": {"keep_override": keep_override, "runs_mult": runs_mult},
        "generated": time.time(),
    }
