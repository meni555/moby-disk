"""Direkt gesicherte Pfade: Regeln, Mount-Entscheidung, Packen, Subclient-Abgleich, Synthetic Full."""
import os
import tarfile
import time
import unittest

import _env  # noqa: F401

from app import backup, rules  # noqa: E402
from app.targets import commvault_content  # noqa: E402
from app.targets.commvault import CommvaultTarget  # noqa: E402
from app.targets.base import TargetError  # noqa: E402

VOL = os.environ["VOLUMES_ROOT"]


class Fake:
    configured = True

    def __init__(self):
        self.paths = [os.environ["BACKUP_ROOT"], "/home/fremd"]
        self.other = [{"excludePath": os.environ["BACKUP_ROOT"] + "/.staging"}]
        self.writes = []
        self.started = []
        self.last = None

    def subclient_content(self, sid):
        return list(self.paths), list(self.other)

    def set_subclient_content(self, sid, paths, other=()):
        self.writes.append((list(paths), list(other)))
        self.paths, self.other = list(paths), list(other)

    def last_full_time(self, identity):
        return self.last

    def start_backup(self, sid, level):
        self.started.append(level)
        return {"jobIds": ["99"]}

    def get_job(self, job_id):
        return {"status": "Completed", "percentComplete": 100}


class DirectPaths(unittest.TestCase):
    def test_1_validation(self):
        def bad(paths):
            with self.assertRaises(rules.RulesError):
                rules.clean_direct_paths(paths)
        bad(["data/x"])
        bad(["/etc/nginx"])
        bad([os.environ["BACKUP_ROOT"]])
        bad([VOL])
        self.assertEqual(rules.clean_direct_paths(["/srv/a/b", "/srv/a/", "/srv/c", "/srv/c"]), ["/srv/a", "/srv/c"])
        r = rules.update_rules({"cv_direct_paths": f"/srv/ollama\n{VOL}/nc/_data/data", "cv_synth_full_days": 7})
        self.assertEqual((len(r["cv_direct_paths"]), r["cv_synth_full_days"]), (2, 7))

    def test_2_mount_decisions(self):
        rules.update_rules({"cv_direct_paths": f"/srv/ollama\n{VOL}/nc/_data/data"})
        d = rules.classify({"type": "volume", "name": "nc", "source": f"{VOL}/nc/_data", "destination": "/var/www/html"}, "nextcloud")
        self.assertEqual((d["action"], d["direct_sub"][0]["rel"]), ("backup", "data"))
        self.assertIn("direkt über Commvault", d["text"])
        d = rules.classify({"type": "bind", "source": "/srv/ollama", "destination": "/root/.ollama"}, "ollama")
        self.assertEqual((d["action"], d["reason"]), ("skip", "cv_direct"))
        d = rules.classify({"type": "bind", "source": "/srv/ollama/models", "destination": "/m"}, "x")
        self.assertEqual(d["reason"], "cv_direct")

    def test_3_tar_skips_direct_part(self):
        os.makedirs(f"{VOL}/nc/_data/data/user")
        os.makedirs(f"{VOL}/nc/_data/config")
        open(f"{VOL}/nc/_data/data/user/foto.jpg", "wb").write(b"x" * 5000)
        open(f"{VOL}/nc/_data/config/config.php", "w").write("<?php")
        open(f"{VOL}/nc/_data/data.txt", "w").write("bleibt")
        out = os.path.join(os.environ["BACKUP_ROOT"], "out.tar")
        backup._tar_dir(f"{VOL}/nc/_data", out, lambda m: None, compress=False, skip_paths=[f"{VOL}/nc/_data/data"])
        names = tarfile.open(out).getnames()
        self.assertFalse(any(n == "data" or n.startswith("data/") for n in names))
        self.assertIn("config/config.php", names)
        self.assertIn("data.txt", names)
        size, _ = rules.bounded_size(f"{VOL}/nc/_data", skip=[f"{VOL}/nc/_data/data"])
        self.assertLess(size, 5000)

    def test_4_content_sync_keeps_foreign_and_filters(self):
        rules.update_rules({"cv_direct_paths": f"/srv/ollama\n{VOL}/nc/_data/data"})
        fake = Fake()
        commvault_content._client = lambda: (fake, {"subclient_id": 35, "subclient_name": "default"})
        st = commvault_content.status(None, fake, {"subclient_id": 35})
        self.assertEqual((len(st["missing"]), st["foreign"], st["in_sync"]), (2, ["/home/fremd"], False))
        res = commvault_content.sync()
        self.assertEqual(len(fake.writes), 1)
        self.assertIn("/home/fremd", fake.paths)
        self.assertEqual(fake.other, [{"excludePath": os.environ["BACKUP_ROOT"] + "/.staging"}])
        self.assertTrue(res["in_sync"] and res["changed"])
        rules.update_rules({"cv_direct_paths": "/srv/ollama"})
        commvault_content.sync()
        self.assertNotIn(f"{VOL}/nc/_data/data", fake.paths)
        self.assertIn("/home/fremd", fake.paths)
        self.assertIn("/srv/ollama", fake.paths)
        self.assertFalse(commvault_content.sync()["changed"])
        self.assertEqual(len(fake.writes), 2)

    def test_5_synthetic_full_only_when_due(self):
        target = CommvaultTarget()
        fake = Fake()
        target._ready = lambda: (fake, {"subclient_id": 35})
        log = []
        rules.update_rules({"cv_synth_full_days": 7})
        fake.last = time.time() - 2 * 86400
        target.after_success(None, log.append)
        self.assertEqual(fake.started, [])
        fake.last = time.time() - 8 * 86400
        target.after_success(None, log.append)
        self.assertEqual(fake.started, [4])
        fake.last = None
        target.after_success(None, log.append)
        self.assertEqual(fake.started, [4, 4])
        rules.update_rules({"cv_synth_full_days": 0})
        target.after_success(None, log.append)
        self.assertEqual(len(fake.started), 2)

    def test_5b_cancel_kills_commvault_job(self):
        from app.targets import commvault_api
        calls = []
        cv = commvault_api.Commvault(url="https://x", username="u", password="p")
        cv.login = lambda force=False: "t"
        cv._request = lambda method, ep, with_token=True, params=None, json_body=None: calls.append((method, ep)) or {}
        target = CommvaultTarget()
        target._client = lambda: cv
        log = []
        from app.targets.base import Run
        target.cancel(Run(id="55"), log.append)
        self.assertEqual(calls, [("POST", "Job/55/action/kill")])
        self.assertTrue(any("beendet" in m for m in log))

    def test_6_not_ready_is_reported(self):
        target = CommvaultTarget()
        self.assertFalse(target.configured())
        with self.assertRaises(TargetError):
            target.start([], {}, lambda m: None)


if __name__ == "__main__":
    unittest.main()
