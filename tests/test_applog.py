"""Zentrales Log: Ringpuffer, Maskierung, Abfragen, Schnittstelle – und die Regression vom nächtlichen Veeam-Fehler."""
import json
import logging
import os
import time
import unittest

import _env  # noqa: F401

from app import applog, store, targets  # noqa: E402
from app.targets.base import STATE_CHECK, TRIGGER, Target  # noqa: E402
from app.tasks import JobManager, confirmed_folders, describe_error, target_folders  # noqa: E402

F1 = "2026-10-29T20-00-00-000000Z"


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class NoList(Target):
    """Wie Veeam: startet Läufe und prüft Vermerke, kann aber keine Ordner auflisten."""
    id, label, kind = "nolist", "NoList", "pull"
    capabilities = frozenset({TRIGGER, STATE_CHECK})

    def configured(self):
        return True


class Logging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.c = app.test_client()
        applog.set_level("DEBUG")

    @classmethod
    def tearDownClass(cls):
        applog.set_level("INFO")

    def entries(self, **kw):
        return applog.query("DEBUG", limit=500, **kw)

    def test_levels_search_and_since(self):
        mark = applog.last_id()
        applog.get("job").info("[abc123def456] Sicherung läuft")
        applog.get("job").error("[abc123def456] FEHLER: kaputt")
        applog.get("target.veeam").debug("GET /api/v1/jobs → 200")
        self.assertEqual([e["level"] for e in applog.query("DEBUG", since=mark)], ["INFO", "ERROR", "DEBUG"])
        self.assertEqual([e["level"] for e in applog.query("ERROR", since=mark)], ["ERROR"])
        self.assertEqual(len(applog.query("DEBUG", logger="job", since=mark)), 2)
        self.assertEqual(len(applog.query("DEBUG", logger="target", since=mark)), 1)         # Bereich inkl. Unterbereiche
        self.assertEqual(len(applog.query("DEBUG", q="abc123def456", since=mark)), 2)
        self.assertEqual(applog.query("DEBUG", q="gibtsnicht", since=mark), [])

    def test_secrets_are_masked_in_messages_tracebacks_and_file(self):
        os.environ["TEST_VEEAM_PASSWORD"] = "supergeheim-4711"
        try:
            try:
                raise RuntimeError("Anmeldung mit supergeheim-4711 abgelehnt")
            except RuntimeError:
                applog.get("target.veeam").exception("Passwort supergeheim-4711 im Text")
            entry = applog.query("DEBUG", q="Passwort", limit=1)[-1]
            self.assertNotIn("supergeheim-4711", json.dumps(entry))
            self.assertIn("***", entry["msg"])
            self.assertIn("***", entry["exc"])
            with open(applog.LOG_FILE, encoding="utf-8") as fh:
                self.assertNotIn("supergeheim-4711", fh.read())
        finally:
            del os.environ["TEST_VEEAM_PASSWORD"]

    def test_file_has_one_json_line_per_entry_and_survives_a_restart(self):
        applog.get("app").info("Eintrag für die Datei")
        with open(applog.LOG_FILE, encoding="utf-8") as fh:
            last = [json.loads(l) for l in fh if l.strip()][-1]
        self.assertEqual((last["logger"], last["msg"]), ("app", "Eintrag für die Datei"))
        before = applog.last_id()
        applog._buffer.clear()
        applog._load_tail()                                                                      # wie nach einem Neustart
        self.assertTrue(any(e["msg"] == "Eintrag für die Datei" for e in applog.query("DEBUG", limit=3000)))
        self.assertGreater(applog.last_id(), 0)
        del before

    def test_api(self):
        data = self.c.get("/api/logs?level=INFO&limit=5").get_json()
        self.assertLessEqual(len(data["entries"]), 5)
        self.assertEqual(data["level"], "DEBUG")
        self.assertEqual(self.c.put("/api/logs/config", json={"level": "NOPE"}).status_code, 400)
        self.assertEqual(self.c.get("/api/logs?since=x").status_code, 400)
        mark = applog.last_id()
        self.assertEqual(self.c.put("/api/logs/config", json={"level": "DEBUG"}).status_code, 200)
        self.c.get("/api/logs")                                                                  # das Log selbst wird nicht protokolliert
        text = " ".join(e["msg"] for e in applog.query("DEBUG", since=mark))
        self.assertIn("PUT /api/logs/config → 200", text)
        self.assertNotIn("GET /api/logs →", text)
        dl = self.c.get("/api/logs/download")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        self.assertIn("PUT /api/logs/config", dl.get_data(as_text=True))

    def test_unhandled_error_is_logged_with_traceback_and_returns_json(self):
        from unittest import mock
        mark = applog.last_id()
        with mock.patch("app.main.jobs.list_jobs", side_effect=NotImplementedError()):
            resp = self.c.get("/api/jobs")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("NotImplementedError", resp.get_json()["error"])
        errors = applog.query("ERROR", since=mark)
        self.assertTrue(any("NotImplementedError" in (e["exc"] or "") for e in errors))      # Traceback im Log
        self.assertTrue(any("→ 500" in e["msg"] for e in errors))                           # und die Zugriffszeile


class NightlyRegression(unittest.TestCase):
    """Veeam kann keine Ordner auflisten: die Aufbewahrung darf daran nicht den ganzen Auftrag kippen."""

    def setUp(self):
        targets.REGISTRY["nolist"] = NoList
        self.jm = JobManager()

    def tearDown(self):
        targets.REGISTRY.pop("nolist", None)

    def test_folders_of_a_target_without_list_come_from_the_marks(self):
        store.mark_verified("nolist", "web", F1, time.time(), "s1")
        self.assertEqual(set(target_folders("nolist", "web")), {F1})
        self.assertEqual(confirmed_folders("web", ["nolist"]), {F1})
        self.assertEqual(confirmed_folders("unbekannt", ["nolist"]), set())

    def test_empty_exception_messages_are_never_shown_empty(self):
        self.assertEqual(describe_error(NotImplementedError()), "NotImplementedError (ohne Meldung)")
        self.assertEqual(describe_error(KeyError("id")), "KeyError: 'id'")
        self.assertEqual(describe_error(ValueError("zu groß")), "ValueError: zu groß")

    def test_failed_job_logs_type_and_traceback(self):
        job = self.jm._new("backup", {"container": "x"})

        def work(job_id):
            raise NotImplementedError()
        self.jm._start(job["id"], work)
        self.assertTrue(wait_for(lambda: self.jm.get_job(job["id"])["status"] == "failed"))
        done = self.jm.get_job(job["id"])
        self.assertIn("NotImplementedError", done["error"])
        self.assertTrue(any("NotImplementedError" in l["message"] for l in done["log"] if l["message"].startswith("FEHLER")))
        entry = [e for e in applog.query("ERROR", logger="job", q=job["id"])][-1]
        self.assertIn("Traceback", entry["exc"])

    def test_retention_error_is_only_a_warning(self):
        job = self.jm._new("backup", {"container": "x"})
        import app.tasks as tasks_mod
        original = tasks_mod.backup.apply_retention
        tasks_mod.backup.apply_retention = lambda *a, **k: (_ for _ in ()).throw(NotImplementedError())
        try:
            def work(job_id):
                self.jm._deliver(job_id, [], ["c1"], {"c1": F1}, 3, 0, False, [])
            self.jm._start(job["id"], work)
            self.assertTrue(wait_for(lambda: self.jm.get_job(job["id"])["status"] in ("finished", "failed")))
        finally:
            tasks_mod.backup.apply_retention = original
        done = self.jm.get_job(job["id"])
        self.assertEqual(done["status"], "finished")
        self.assertTrue(any(l["message"].startswith("WARNUNG: [c1] Aufbewahrung nicht angewendet") for l in done["log"]))


if __name__ == "__main__":
    unittest.main()
