"""Benachrichtigungen (Webhook, ntfy, E-Mail), Heartbeat-Ping und Tagesbericht.
Die Einstellungen liegen in DATA_DIR/settings.json unter dem Schlüssel 'notify'."""
import base64
import copy
import re
import smtplib
import socket
import threading
import time
from email.message import EmailMessage

import requests

from . import applog, store
from .config import settings

TIMEOUT = 12

DEFAULTS = {
    "events": {"failure": True, "success": False, "missed": True, "digest": True},
    "digest_time": "08:00",
    "digest_always": False,
    "rpo_hours": 36,
    "storage_warn_days": 30,
    "storage_warn_free_gb": 20,
    "heartbeat_url": "",
    "channels": {
        "webhook": {"enabled": False, "url": ""},
        "ntfy": {"enabled": False, "url": "", "token": ""},
        "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
                  "user": "", "password": "", "from": "", "to": ""},
    },
}
SECRETS = {("ntfy", "token"), ("email", "password")}


class NotifyError(Exception):
    pass


def _merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def get_config():
    return _merge(DEFAULTS, settings.get("notify") or {})


def public_config():
    cfg = get_config()
    for channel, field in SECRETS:
        cfg["channels"][channel][field + "_set"] = bool(cfg["channels"][channel].get(field))
        cfg["channels"][channel][field] = ""
    return cfg


def update_config(body):
    cur = get_config()
    new = _merge(cur, body or {})
    for channel, field in SECRETS:                      # leeres Geheimnis = vorhandenes behalten
        sent = ((body or {}).get("channels", {}).get(channel, {}) or {}).get(field)
        if not sent:
            new["channels"][channel][field] = cur["channels"][channel].get(field, "")
        new["channels"][channel].pop(field + "_set", None)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(new["digest_time"])):
        raise NotifyError("Uhrzeit des Tagesberichts muss HH:MM sein.")
    for key, lo, hi in (("rpo_hours", 1, 24 * 365), ("storage_warn_days", 1, 730), ("storage_warn_free_gb", 0, 100000)):
        try:
            new[key] = int(new[key])
        except (TypeError, ValueError):
            raise NotifyError(f"Ungültiger Wert für {key}.")
        if not lo <= new[key] <= hi:
            raise NotifyError(f"{key} muss zwischen {lo} und {hi} liegen.")
    for name in ("webhook", "ntfy"):
        url = new["channels"][name]["url"].strip()
        if new["channels"][name]["enabled"] and not re.match(r"^https?://", url):
            raise NotifyError(f"{name}: URL muss mit http:// oder https:// beginnen.")
        new["channels"][name]["url"] = url
    mail = new["channels"]["email"]
    if mail["enabled"] and not (mail["host"] and mail["to"] and mail["from"]):
        raise NotifyError("E-Mail: Server, Absender und Empfänger sind Pflicht.")
    try:
        mail["port"] = int(mail["port"])
    except (TypeError, ValueError):
        raise NotifyError("E-Mail: ungültiger Port.")
    if new["heartbeat_url"] and not re.match(r"^https?://", new["heartbeat_url"].strip()):
        raise NotifyError("Heartbeat-URL muss mit http:// oder https:// beginnen.")
    new["heartbeat_url"] = new["heartbeat_url"].strip()
    settings.update({"notify": new})
    return public_config()


# ───────── Kanäle ─────────
def _send_webhook(cfg, payload):
    resp = requests.post(cfg["url"], json=payload, timeout=TIMEOUT)
    resp.raise_for_status()


def _send_ntfy(cfg, payload):
    prio = {"info": "3", "warn": "4", "error": "5"}.get(payload["level"], "3")
    tags = {"info": "floppy_disk", "warn": "warning", "error": "rotating_light"}.get(payload["level"], "floppy_disk")
    title = payload["title"] or "Moby Disk"
    if not title.isascii():   # ntfy erwartet RFC 2047 für Nicht-ASCII-Zeichen in Headern
        title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="
    headers = {"Title": title, "Priority": prio, "Tags": tags}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    resp = requests.post(cfg["url"], data=payload["message"].encode("utf-8"), headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()


def _send_email(cfg, payload):
    msg = EmailMessage()
    msg["Subject"] = f"[{payload['host']}] {payload['title']}"
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(payload["message"])
    port = int(cfg.get("port") or 587)
    if cfg.get("security") == "ssl":
        smtp = smtplib.SMTP_SSL(cfg["host"], port, timeout=TIMEOUT)
    else:
        smtp = smtplib.SMTP(cfg["host"], port, timeout=TIMEOUT)
    try:
        if cfg.get("security") == "starttls":
            smtp.starttls()
        if cfg.get("user"):
            smtp.login(cfg["user"], cfg.get("password", ""))
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


_SENDERS = {"webhook": _send_webhook, "ntfy": _send_ntfy, "email": _send_email}


def _payload(title, message, level, event):
    return {"title": title, "message": message, "level": level, "event": event,
            "host": socket.gethostname(), "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def send_sync(title, message, level="info", event="custom", only=None):
    """Sendet über alle aktivierten Kanäle und liefert [(kanal, ok, fehler)]."""
    cfg = get_config()["channels"]
    payload = _payload(title, message, level, event)
    results = []
    for name, sender in _SENDERS.items():
        if only and name not in only:
            continue
        if not cfg[name].get("enabled") and not only:
            continue
        try:
            sender(cfg[name], payload)
            results.append((name, True, ""))
            applog.get("notify").info("Nachricht über %s gesendet: %s", name, title)
        except Exception as exc:
            results.append((name, False, str(exc)[:300]))
            applog.get("notify").warning("Senden über %s fehlgeschlagen (%s): %s", name, title, str(exc)[:300])
    return results


def send(title, message, level="info", event="custom"):
    """Sendet im Hintergrund (blockiert keinen Job)."""
    def run():
        for name, ok, err in send_sync(title, message, level, event):
            if not ok:
                applog.get("notify").warning("%s fehlgeschlagen: %s", name, err)
    threading.Thread(target=run, name="notify", daemon=True).start()


def test(channel=None):
    cfg = get_config()["channels"]
    names = [channel] if channel else [n for n in cfg if cfg[n].get("enabled")]
    if not names:
        raise NotifyError("Kein Kanal aktiviert.")
    return send_sync("Testnachricht", "Die Benachrichtigung von Moby Disk funktioniert.",
                     "info", "test", only=names)


# ───────── Job-Ergebnisse, Heartbeat ─────────
def _heartbeat(ok):
    url = get_config().get("heartbeat_url")
    if not url:
        return
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=TIMEOUT)
    except Exception as exc:
        applog.get("notify").warning("Heartbeat fehlgeschlagen: %s", exc)


def _fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 60} min {sec % 60} s" if sec >= 60 else f"{sec} s"


def job_finished(job):
    if not job or job.get("kind") not in ("backup", "restore"):
        return
    cfg = get_config()
    params = job.get("params") or {}
    ok = job.get("status") == "finished"
    kind = "Backup" if job["kind"] == "backup" else "Klon" if params.get("target") == "clone" else "Restore"
    names = params.get("containers") or [params.get("container", "")]
    subject = names[0] if len(names) == 1 else f"{len(names)} Container"
    where = f" (Zeitplan '{params['schedule_name']}')" if params.get("schedule_name") else ""
    took = _fmt_duration((job.get("finished") or time.time()) - job["started"])
    if params.get("schedule_id"):
        threading.Thread(target=_heartbeat, args=(ok,), daemon=True).start()
    if ok and cfg["events"]["success"]:
        send(f"{kind} erfolgreich: {subject}{where}", f"Dauer: {took}\nContainer: {', '.join(names)}\nJob: {job['id']}",
             "info", "success")
    elif not ok and cfg["events"]["failure"]:
        send(f"{kind} FEHLGESCHLAGEN: {subject}{where}",
             f"{job.get('error') or job.get('message')}\nDauer: {took}\nContainer: {', '.join(names)}\nJob: {job['id']}",
             "error", "failure")


def schedule_missed(name, text):
    if get_config()["events"]["missed"]:
        send(f"Zeitplan verpasst: {name}", text, "warn", "missed")


# ───────── Tagesbericht ─────────
def build_alerts(containers, schedules, forecast_data):
    """Liste auffälliger Punkte für den Tagesbericht. containers: Ergebnis von /api/containers."""
    cfg = get_config()
    alerts = []
    for c in containers:
        prot = c.get("protection") or {}
        if prot.get("state") == "none":
            alerts.append(("error", f"Nie gesichert: {c['name']}"))
        elif prot.get("state") == "stale":
            alerts.append(("warn", f"Veraltet ({prot.get('age_hours')} h > {cfg['rpo_hours']} h): {c['name']}"))
        if prot.get("state") in ("none", "stale") and not prot.get("scheduled"):
            alerts[-1] = (alerts[-1][0], alerts[-1][1] + " – in keinem Zeitplan")
    for s in schedules:
        if s.get("enabled") and s.get("last_status") in ("failed", "missed"):
            alerts.append(("warn", f"Zeitplan '{s['name']}': {s.get('last_status')} – {s.get('last_message', '')}"))
    if forecast_data:
        days = forecast_data.get("days_until_full")
        if days is not None and days <= cfg["storage_warn_days"]:
            alerts.append(("error", f"Speicher voraussichtlich in {days} Tagen voll."))
        free = forecast_data.get("free")
        if free is not None and free < cfg["storage_warn_free_gb"] * 2 ** 30:
            alerts.append(("error", f"Nur noch {free / 2 ** 30:.1f} GB frei im Backup-Ordner."))
    return alerts


def maybe_send_digest(now, compute):
    """Sendet einmal pro Tag nach 'digest_time'. 'compute' liefert (containers, schedules, forecast)."""
    cfg = get_config()
    if not cfg["events"]["digest"]:
        return
    today = now.strftime("%Y-%m-%d")
    if store.kv_get("digest_last") == today or now.strftime("%H:%M") < cfg["digest_time"]:
        return
    store.kv_set("digest_last", today)
    alerts = build_alerts(*compute())
    if not alerts and not cfg["digest_always"]:
        return
    level = "error" if any(l == "error" for l, _ in alerts) else "warn" if alerts else "info"
    body = "\n".join(f"• {text}" for _, text in alerts) or "Alles in Ordnung."
    send(f"Tagesbericht: {len(alerts)} Hinweis(e)" if alerts else "Tagesbericht: alles in Ordnung", body, level, "digest")
