"""Einrichtung der Ziele im Webinterface: Vorrang vor der .env, Geheimnisse, Persistenz, Prüfungen."""
import json
import os
import stat
import unittest
from unittest import mock

import _env  # noqa: F401

from app import target_config as tc  # noqa: E402


class TargetConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.c = app.test_client()

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in list(tc.BY_ENV) + ["TARGETS"]}
        self.base = dict(tc._BASE)
        self.idle = mock.patch("app.main._active_jobs", return_value=[])      # keine Aufträge anderer Tests im Weg
        self.idle.start()
        tc._state.update(values={}, enabled=None)
        tc._BASE["COMMAND_LABEL"] = "aus der .env"
        for k in ("RESTIC_PASSWORD", "RESTIC_REPOSITORY"):
            tc._BASE.pop(k, None)
        tc.apply()

    def tearDown(self):
        self.idle.stop()
        tc._BASE.clear()
        tc._BASE.update(self.base)
        tc._state.update(values={}, enabled=None)
        if os.path.exists(tc.FILE):
            os.remove(tc.FILE)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def put(self, target, **body):
        return self.c.put(f"/api/targets/{target}/config", json=body)

    def test_value_overrides_env_and_falls_back_when_cleared(self):
        self.assertEqual(os.environ["COMMAND_LABEL"], "aus der .env")
        self.assertEqual(self.put("command", values={"COMMAND_LABEL": "Mein Ziel"}).status_code, 200)
        self.assertEqual(os.environ["COMMAND_LABEL"], "Mein Ziel")
        field = next(f for f in tc.public("command") if f["env"] == "COMMAND_LABEL")
        self.assertEqual((field["source"], field["value"]), ("ui", "Mein Ziel"))
        self.put("command", values={"COMMAND_LABEL": ""})                 # geleert → wieder .env
        self.assertEqual(os.environ["COMMAND_LABEL"], "aus der .env")
        self.assertEqual(next(f for f in tc.public("command") if f["env"] == "COMMAND_LABEL")["source"], "env")

    def test_secrets_are_never_returned_and_empty_keeps_them(self):
        self.put("restic", values={"RESTIC_PASSWORD": "geheim-123", "RESTIC_REPOSITORY": "/mnt/r"})
        self.assertEqual(os.environ["RESTIC_PASSWORD"], "geheim-123")
        for path in ("/api/targets", "/api/status"):
            self.assertNotIn("geheim-123", self.c.get(path).get_data(as_text=True))
        field = next(f for f in tc.public("restic") if f["env"] == "RESTIC_PASSWORD")
        self.assertEqual((field["set"], field["value"]), (True, ""))
        self.put("restic", values={"RESTIC_PASSWORD": ""})                # leer = unverändert
        self.assertEqual(os.environ["RESTIC_PASSWORD"], "geheim-123")
        self.put("restic", values={"RESTIC_PASSWORD": None})              # None = entfernen
        self.assertNotIn("RESTIC_PASSWORD", os.environ)

    def test_file_is_private_and_survives_a_restart(self):
        self.put("restic", values={"RESTIC_PASSWORD": "geheim-123", "RESTIC_REPOSITORY": "/mnt/r"})
        self.assertEqual(stat.S_IMODE(os.stat(tc.FILE).st_mode), 0o600)
        with open(tc.FILE) as fh:
            self.assertEqual(json.load(fh)["values"]["RESTIC_REPOSITORY"], "/mnt/r")
        os.environ.pop("RESTIC_REPOSITORY"), os.environ.pop("RESTIC_PASSWORD")
        tc._state.update(values={}, enabled=None)
        tc.load()                                                          # wie nach einem Neustart
        self.assertEqual((os.environ["RESTIC_REPOSITORY"], os.environ["RESTIC_PASSWORD"]), ("/mnt/r", "geheim-123"))

    def test_invalid_values_are_refused(self):
        self.assertEqual(self.put("veeam", values={"VEEAM_URL": "ftp://x"}).status_code, 400)
        self.assertEqual(self.put("restic", values={"RESTIC_AUTO_INIT": "vielleicht"}).status_code, 400)
        self.assertEqual(self.put("command", values={"COMMAND_LABEL": "a\nb"}).status_code, 400)
        self.assertEqual(self.put("command", values={"CV_URL": "https://x"}).status_code, 400)   # gehört zu einem anderen Ziel
        self.assertEqual(self.put("nix", values={}).status_code, 400)
        self.assertFalse(os.path.exists(tc.FILE))

    def test_enabling_targets_and_keeping_at_least_one(self):
        before = os.environ["TARGETS"]
        self.assertEqual(self.put("command", enabled=True).status_code, 200)
        self.assertIn("command", os.environ["TARGETS"].split(","))
        self.assertEqual(self.put("command", enabled=False).status_code, 200)
        self.assertNotIn("command", os.environ["TARGETS"].split(","))
        with self.assertRaises(tc.ConfigError):
            tc.save("commvault", {}, False, enabled_now=["commvault"])
        tc._state.update(enabled=None)
        tc.apply()
        self.assertEqual(os.environ["TARGETS"], before)

    def test_changes_are_refused_while_a_job_runs(self):
        with mock.patch("app.main._active_jobs", return_value=[{"id": "j1", "status": "running"}]):
            resp = self.put("command", values={"COMMAND_LABEL": "x"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(os.environ["COMMAND_LABEL"], "aus der .env")

    def test_targets_are_listed_in_fixed_order(self):
        known = ["commvault", "veeam", "pbs", "restic", "command"]
        ids = [t["id"] for t in self.c.get("/api/targets").get_json()]
        self.assertEqual([i for i in ids if i in known], known)


if __name__ == "__main__":
    unittest.main()
