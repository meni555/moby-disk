"""Veeam-Ziel gegen einen nachgebildeten Veeam-Server (REST-API von Veeam Backup & Replication).

Der Nachbau bildet die dokumentierte API nach (OAuth-Token, Jobs, Sitzungen, Backups, Restore-Punkte); er prüft die
Logik des Helpers, nicht die Wahrheit der echten API – dafür braucht es einen echten Server."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import _env  # noqa: F401

from app import store  # noqa: E402
from app.targets.base import TargetError  # noqa: E402
from app.targets.veeam import VeeamTarget  # noqa: E402
from app.tasks import JobManager, sync_target_state  # noqa: E402

JOB_ID = "11111111-2222-3333-4444-555555555555"
STATE = {}


def reset():
    STATE.clear()
    STATE.update(tokens=0, polls=0, started=[], result="Success", expire_first_token=False, job_disabled=False,
                 points=[], jobs=[{"id": JOB_ID, "name": "Docker-Backups", "type": "LinuxAgentBackup", "isDisabled": False}],
                 headers=[])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        STATE["headers"].append(self.headers.get("x-api-version"))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        path = urlparse(self.path).path
        if path == "/api/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            if form.get("password") != "geheim" or form.get("grant_type") != "password":
                STATE["logins"] = STATE.get("logins", 0) + 1
                return self.send(401, {"errorCode": "AccessDenied", "message": "Falsche Zugangsdaten"})
            STATE["tokens"] += 1
            STATE["logins"] = STATE.get("logins", 0) + 1
            return self.send(200, {"access_token": f"tok{STATE['tokens']}", "expires_in": 900})
        if not self.authorized():
            return
        if path == "/api/v1/sessions/sess-1/stop":
            STATE.setdefault("stopped", []).append("sess-1")
            return self.send(200, {"id": "sess-1", "state": "Stopping"})
        if path == f"/api/v1/jobs/{JOB_ID}/start":
            body = json.loads(raw or "{}")
            STATE["started"].append(body)
            return self.send(201, {"id": "sess-1", "state": "Starting", "jobId": JOB_ID})
        self.send(404, {"message": "nicht gefunden"})

    def authorized(self):
        auth = self.headers.get("Authorization", "")
        if STATE["expire_first_token"] and auth == "Bearer tok1":
            self.send(401, {"message": "Token abgelaufen"})
            return False
        if not auth.startswith("Bearer tok"):
            self.send(401, {"message": "nicht angemeldet"})
            return False
        return True

    def do_GET(self):
        STATE["headers"].append(self.headers.get("x-api-version"))
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.startswith("/swagger/v") and url.path.endswith("/swagger.json"):
            if url.path.split("/")[2][1:] in STATE.get("swagger", set()):
                return self.send(200, {})
            return self.send(404, {})
        if not self.authorized():
            return
        if url.path == "/api/v1/jobs/states":
            name = (parse_qs(url.query).get("nameFilter") or [""])[0].lower()
            return self.send(200, {"data": [s for s in STATE.get("states", []) if name in s["name"].lower()]})
        if url.path == "/api/v1/jobs":
            name = q.get("nameFilter", "").lower()
            return self.send(200, {"data": [dict(j, isDisabled=STATE["job_disabled"]) for j in STATE["jobs"] if name in j["name"].lower()]})
        if url.path == f"/api/v1/jobs/{JOB_ID}":
            return self.send(200, dict(STATE["jobs"][0], isDisabled=STATE["job_disabled"]))
        if url.path == "/api/v1/serverInfo":
            return self.send(200, {"name": "vbr-test", "buildVersion": "12.3.0.310"})
        if url.path == "/api/v1/sessions/sess-1":
            STATE["polls"] += 1
            if STATE["polls"] < 3:
                return self.send(200, {"id": "sess-1", "state": "Working", "progressPercent": 40})
            return self.send(200, {"id": "sess-1", "state": "Stopped", "progressPercent": 100,
                                   "result": {"result": STATE["result"], "message": "Speicher voll" if STATE["result"] == "Failed" else ""}})
        if url.path == "/api/v1/backups":
            return self.send(200, {"data": [{"id": "bk-1", "name": "Docker-Backups - docker-host", "jobId": JOB_ID}]})
        if url.path == "/api/v1/restorePoints":
            return self.send(200, {"data": [{"id": f"rp{i}", "creationTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))}
                                            for i, t in enumerate(STATE["points"])]})
        self.send(404, {"message": "nicht gefunden"})


def ts(epoch):
    return time.strftime("%Y-%m-%dT%H-%M-%S-000000Z", time.gmtime(epoch))


class Veeam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ.update(VEEAM_URL=f"http://127.0.0.1:{cls.server.server_port}", VEEAM_USERNAME="admin",
                          VEEAM_PASSWORD="geheim", VEEAM_JOB="Docker-Backups", VEEAM_POLL_SECONDS="1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for k in [k for k in os.environ if k.startswith("VEEAM_")]:
            del os.environ[k]

    def setUp(self):
        reset()
        os.environ["VEEAM_PASSWORD"] = "geheim"
        self.t = VeeamTarget()

    def test_configured_and_capabilities(self):
        self.assertTrue(self.t.configured())
        self.assertEqual(sorted(self.t.capabilities), ["state_check", "trigger"])
        del os.environ["VEEAM_JOB"]
        try:
            self.assertFalse(VeeamTarget().configured())
        finally:
            os.environ["VEEAM_JOB"] = "Docker-Backups"

    def test_connection_test_reports_job_and_restore_points(self):
        STATE["points"] = [time.time() - 86400, time.time()]
        info = self.t.test()
        self.assertEqual((info["job"], info["restore_points"], info["server"]), ("Docker-Backups", 2, "vbr-test 12.3.0.310"))
        self.assertTrue(all(h == "1.1-rev0" for h in STATE["headers"]))

    def test_wrong_password_and_unknown_job(self):
        os.environ["VEEAM_PASSWORD"] = "falsch"
        with self.assertRaises(TargetError) as cm:
            VeeamTarget().test()
        self.assertIn("Falsche Zugangsdaten", str(cm.exception))
        os.environ["VEEAM_PASSWORD"] = "geheim"
        os.environ["VEEAM_JOB"] = "Gibts-nicht"
        try:
            with self.assertRaises(TargetError) as cm:
                VeeamTarget().test()
            self.assertIn("nicht gefunden", str(cm.exception))
        finally:
            os.environ["VEEAM_JOB"] = "Docker-Backups"

    def test_start_wait_success_and_level(self):
        log = []
        run = self.t.start([("a", "x")], {"level": 2}, log.append)
        self.assertEqual(STATE["started"], [{"performActiveFull": False}])
        progress = []
        res = self.t.wait(run, lambda p, text: progress.append((p, text)))
        self.assertTrue(res.ok, res)
        self.assertEqual(res.status, "Success")
        self.assertEqual(progress[0][0], 40.0)
        STATE["polls"] = 0
        self.t.start([("a", "x")], {"level": 1}, log.append)
        self.assertEqual(STATE["started"][-1], {"performActiveFull": True})

    def test_failed_and_warning_results(self):
        STATE["result"] = "Failed"
        res = self.t.wait(self.t.start([], {}, lambda m: None))
        self.assertFalse(res.ok)
        self.assertIn("Speicher voll", res.detail)
        STATE["result"], STATE["polls"] = "Warning", 0
        res = self.t.wait(self.t.start([], {}, lambda m: None))
        self.assertTrue(res.ok)
        self.assertIn("Warnungen", res.status)

    def test_cancel_stops_the_session(self):
        run = self.t.start([], {}, lambda m: None)
        log = []
        self.t.cancel(run, log.append)
        self.assertEqual(STATE["stopped"], ["sess-1"])
        self.assertTrue(any("angehalten" in m for m in log))
        self.t.cancel(None, log.append)   # ohne Lauf: nichts passiert

    def test_rejected_login_is_not_repeated_and_explains_username(self):
        VeeamTarget._denied.clear()
        os.environ["VEEAM_PASSWORD"] = "falsch"
        STATE["logins"] = 0
        try:
            with self.assertRaises(TargetError) as first:
                VeeamTarget().test()
            self.assertIn("DOMÄNE", str(first.exception))                 # Hinweis, weil der Name keine Domäne hat
            self.assertEqual(STATE["logins"], 1)
            for _ in range(3):                                             # weitere Versuche erreichen den Server nicht
                with self.assertRaises(TargetError) as again:
                    VeeamTarget().test()
            self.assertEqual(STATE["logins"], 1)
            self.assertIn("Min.", str(again.exception))
            os.environ["VEEAM_USERNAME"] = "DOMAIN\\admin"                   # geänderte Zugangsdaten: sofort wieder erlaubt
            os.environ["VEEAM_PASSWORD"] = "geheim"
            self.assertEqual(VeeamTarget().test()["job"], "Docker-Backups")
            os.environ["VEEAM_PASSWORD"] = "falsch2"
            with self.assertRaises(TargetError) as with_domain:
                VeeamTarget().test()
            self.assertNotIn("DOMÄNE", str(with_domain.exception))         # Name hat eine Domäne → kein Hinweis
        finally:
            os.environ["VEEAM_USERNAME"], os.environ["VEEAM_PASSWORD"] = "admin", "geheim"
            VeeamTarget._denied.clear()

    def test_choices_list_jobs_from_both_endpoints_even_without_a_job_set(self):
        STATE["jobs"] = [{"id": "1", "name": "VMs", "type": "Backup"}]
        STATE["states"] = [{"id": "2", "name": "docker", "type": "Backup", "workload": "file", "status": "inactive"}]
        saved = os.environ.pop("VEEAM_JOB")
        try:
            choices = VeeamTarget().choices("VEEAM_JOB")
        finally:
            os.environ["VEEAM_JOB"] = saved
        self.assertEqual([c["value"] for c in choices], ["docker", "VMs"])
        self.assertIn("docker (Backup/file)", [c["label"] for c in choices])
        with self.assertRaises(TargetError):
            VeeamTarget().choices("VEEAM_URL")

    def test_api_version_is_picked_automatically(self):
        VeeamTarget._versions.clear()
        saved = os.environ.pop("VEEAM_API_VERSION", None)
        try:
            self.assertEqual(VeeamTarget()._api_version(), "1.1-rev0")        # Mock kennt keine neueren Versionen
            STATE["swagger"] = {"1.2-rev0"}
            VeeamTarget._versions.clear()
            self.assertEqual(VeeamTarget()._api_version(), "1.2-rev0")
            STATE["swagger"] = {"1.2-rev0", "1.3-rev1"}
            VeeamTarget._versions.clear()
            self.assertEqual(VeeamTarget()._api_version(), "1.3-rev1")
            os.environ["VEEAM_API_VERSION"] = "1.3-rev0"                          # feste Angabe gewinnt
            self.assertEqual(VeeamTarget()._api_version(), "1.3-rev0")
        finally:
            VeeamTarget._versions.clear()
            if saved is None:
                os.environ.pop("VEEAM_API_VERSION", None)
            else:
                os.environ["VEEAM_API_VERSION"] = saved

    def test_file_job_is_found_via_job_states(self):
        STATE["jobs"] = [{"id": JOB_ID, "name": "Andere", "type": "Backup", "isDisabled": False}]
        STATE["states"] = [{"id": JOB_ID, "name": "docker", "type": "Backup", "workload": "file", "status": "inactive"}]
        os.environ["VEEAM_JOB"] = "Docker"
        try:
            self.assertEqual(VeeamTarget().test()["job"], "docker")          # Groß-/Kleinschreibung egal
            VeeamTarget().start([("c", "f")], {}, lambda msg: None)
            self.assertEqual(STATE["started"], [{"performActiveFull": False}])
            STATE["states"][0]["status"] = "disabled"
            with self.assertRaises(TargetError) as off:
                VeeamTarget().test()
            self.assertIn("deaktiviert", str(off.exception))
        finally:
            os.environ["VEEAM_JOB"] = "Docker-Backups"

    def test_unknown_job_lists_what_the_api_knows(self):
        STATE["jobs"] = []
        STATE["states"] = [{"id": "x", "name": "Fotos", "type": "Backup", "workload": "vm", "status": "inactive"}]
        os.environ["VEEAM_JOB"] = "Gibts-nicht"
        try:
            with self.assertRaises(TargetError) as ctx:
                VeeamTarget().test()
            self.assertIn("nicht gefunden", str(ctx.exception))
            self.assertIn("Fotos (Backup/vm)", str(ctx.exception))
            self.assertIn("Vorhandene Backups: Docker-Backups - docker-host", str(ctx.exception))
            self.assertIn("Dateiserver: nicht abrufbar", str(ctx.exception))      # Mock kennt den Pfad nicht → still
        finally:
            os.environ["VEEAM_JOB"] = "Docker-Backups"

    def test_missing_job_is_reported_without_login(self):
        saved = os.environ.pop("VEEAM_JOB")
        STATE["logins"] = 0
        try:
            with self.assertRaises(TargetError) as ctx:
                VeeamTarget().test()
            self.assertIn("VEEAM_JOB", str(ctx.exception))
            self.assertEqual(STATE["logins"], 0)
        finally:
            os.environ["VEEAM_JOB"] = saved

    def test_disabled_job_is_refused(self):
        STATE["job_disabled"] = True
        with self.assertRaises(TargetError):
            self.t.start([], {}, lambda m: None)

    def test_expired_token_is_renewed_once(self):
        STATE["expire_first_token"] = True
        self.assertEqual(self.t.test()["job"], "Docker-Backups")
        self.assertEqual(STATE["tokens"], 2)

    def test_unreachable_server(self):
        os.environ["VEEAM_URL"] = "http://127.0.0.1:1"
        try:
            with self.assertRaises(TargetError) as cm:
                VeeamTarget().test()
            self.assertIn("nicht erreichbar", str(cm.exception))
            self.assertIsNone(VeeamTarget().validate_entries([{"container": "a", "folder": ts(0), "verified": 0, "local_deleted": None}]))
        finally:
            os.environ["VEEAM_URL"] = f"http://127.0.0.1:{self.server.server_port}"

    def test_validate_entries_uses_restore_point_windows(self):
        now = time.time()
        DAY = 86400
        STATE["points"] = [now - 10 * DAY, now - 2 * DAY]   # zwei Restore-Punkte übrig
        entries = [
            {"container": "neu", "folder": ts(now - DAY), "verified": now - DAY, "local_deleted": None},       # nach letztem Punkt → ungedeckt
            {"container": "alt", "folder": ts(now - 20 * DAY), "verified": now, "local_deleted": now - 15 * DAY},  # Fenster ohne Punkt → weg
            {"container": "ok", "folder": ts(now - 12 * DAY), "verified": now, "local_deleted": now - 9 * DAY},    # Punkt −10 Tage im Fenster
            {"container": "lokal", "folder": ts(now - 3 * DAY), "verified": now, "local_deleted": None},        # noch lokal, Punkt −2 Tage danach
        ]
        keep = self.t.validate_entries(entries)
        self.assertEqual(sorted(c for c, _ in keep), ["lokal", "ok"])

    def test_sync_removes_uncovered_marks(self):
        import app.targets as targets
        now = time.time()
        for e in store.entries(target="veeam"):
            store.drop_entries("veeam", e["container"])
        store.mark_verified("veeam", "vk", ts(now - 3 * 86400), now - 3 * 86400)
        store.mark_verified("veeam", "vk", ts(now - 30 * 86400), now - 30 * 86400)
        with store._lock:   # der alte Ordner wurde schon vor 20 Tagen lokal gelöscht – der einzige Punkt ist jünger
            store._db().execute("UPDATE target_state SET local_deleted = ? WHERE folder = ?", (now - 20 * 86400, ts(now - 30 * 86400)))
            store._db().commit()
        STATE["points"] = [now - 2 * 86400]
        os.environ["TARGETS"] = "veeam"
        try:
            res = sync_target_state("veeam")
        finally:
            os.environ["TARGETS"] = "commvault,fake"
        self.assertEqual(res["removed"], 1)
        self.assertEqual(store.verified_folders("vk", "veeam"), {ts(now - 3 * 86400)})


if __name__ == "__main__":
    unittest.main()
