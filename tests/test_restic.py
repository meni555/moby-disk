"""restic-Ziel gegen ein echtes lokales Repository (braucht das restic-Programm im Image)."""
import os
import shutil
import tarfile
import time
import unittest

import _env  # noqa: F401

from app.targets.base import TargetError  # noqa: E402
from app.targets.restic import ResticTarget, _mask  # noqa: E402

F1, F2 = "2026-10-29T20-00-00-000000Z", "2026-10-30T06-00-00-000000Z"
ROOT = os.environ["BACKUP_ROOT"]


def make_backup(container, folder, payload):
    d = os.path.join(ROOT, container, folder, "data")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "volume-x.tar"), "wb") as fh:
        fh.write(payload)
    with open(os.path.join(ROOT, container, folder, "manifest.json"), "w") as fh:
        fh.write('{"container": "%s", "ts": "%s"}' % (container, folder))


@unittest.skipUnless(shutil.which("restic"), "restic nicht installiert")
class Restic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = os.path.join(_env.TMP, "restic-repo")
        os.environ.update(RESTIC_REPOSITORY=cls.repo, RESTIC_PASSWORD="geheim", RESTIC_AUTO_INIT="true",
                          RESTIC_CACHE_DIR=os.path.join(_env.TMP, "restic-cache"))
        os.environ.pop("RESTIC_KEEP", None)
        cls.t = ResticTarget()

    def test_1_not_configured_without_env(self):
        saved = os.environ.pop("RESTIC_PASSWORD")
        try:
            self.assertFalse(ResticTarget().configured())
            with self.assertRaises(TargetError):
                ResticTarget().start([("a", F1)], {}, lambda m: None)
        finally:
            os.environ["RESTIC_PASSWORD"] = saved

    def test_2_missing_repo_reported_then_auto_init_on_run(self):
        self.assertTrue(self.t.configured())
        with self.assertRaises(TargetError) as cm:
            self.t.test()
        self.assertIn("RESTIC_AUTO_INIT", str(cm.exception))
        make_backup("app1", F1, os.urandom(300_000))
        log = []
        run = self.t.start([("app1", F1)], {}, log.append)
        res = self.t.wait(run)
        self.assertTrue(res.ok, res)
        self.assertEqual(res.done, ["app1"])
        self.assertTrue(any("lege es an" in m for m in log))
        self.assertEqual(self.t.test()["snapshots"], 1)

    def test_3_versions_and_containers(self):
        make_backup("app1", F2, os.urandom(1000))
        make_backup("db2", F1, b"x" * 5000)
        res = self.t.wait(self.t.start([("app1", F2), ("db2", F1)], {}, lambda m: None))
        self.assertTrue(res.ok)
        self.assertEqual(sorted(self.t.list_folders("app1")), [F1, F2])
        self.assertEqual(sorted(self.t.list_folders("db2")), [F1])
        self.assertEqual(self.t.list_containers(), ["app1", "db2"])
        meta = self.t.list_folders("app1")[F2]
        self.assertTrue(meta["time"] and abs(meta["time"] - time.time()) < 600, meta)

    def test_4_dedup_second_identical_backup_adds_almost_nothing(self):
        payload = os.urandom(2_000_000)
        make_backup("dd", F1, payload)
        make_backup("dd", F2, payload)
        log = []
        self.t.start([("dd", F1)], {}, log.append)
        self.t.start([("dd", F2)], {}, log.append)
        added = [float(m.split(", ")[1].split(" MB")[0]) for m in log if "MB neu" in m]
        self.assertGreater(added[0], 1.5)
        self.assertLess(added[1], 0.1)

    def test_5_fetch_restores_into_staging(self):
        make_backup("rs", F1, b"wichtig" * 1000)
        self.t.start([("rs", F1)], {}, lambda m: None)
        dest = os.path.join(_env.TMP, "staging")
        self.t.fetch("rs", F1, dest)
        found = [r for r, _d, f in os.walk(dest) if "manifest.json" in f]
        self.assertEqual(len(found), 1)
        self.assertEqual(open(os.path.join(found[0], "data", "volume-x.tar"), "rb").read(), b"wichtig" * 1000)
        with self.assertRaises(TargetError):
            self.t.fetch("rs", F2, dest)
        with self.assertRaises(TargetError):
            self.t.fetch("rs", "../x", dest)

    def test_6_partial_failure_reports_only_failed_item(self):
        make_backup("ok1", F1, b"a" * 100)
        run = self.t.start([("ok1", F1), ("fehlt", F1)], {}, lambda m: None)
        res = self.t.wait(run)
        self.assertFalse(res.ok)
        self.assertEqual(res.done, ["ok1"])
        self.assertIn("fehlt", res.detail)

    def test_7_retention_forget(self):
        for i in range(3):
            make_backup("ret", f"2026-10-0{i + 1}T06-00-00-000000Z", os.urandom(500))
            self.t.start([("ret", f"2026-10-0{i + 1}T06-00-00-000000Z")], {}, lambda m: None)
            time.sleep(1.1)
        self.assertEqual(len(self.t.list_folders("ret")), 3)
        os.environ["RESTIC_KEEP"] = "--keep-last 2"
        log = []
        try:
            self.t.after_success(None, log.append)
        finally:
            os.environ.pop("RESTIC_KEEP")
        self.assertEqual(len(self.t.list_folders("ret")), 2)
        self.assertEqual(len(self.t.list_folders("app1")), 2)   # andere Container bleiben unberührt (group-by host)

    def test_8_secrets_are_masked(self):
        self.assertEqual(_mask("Fatal: rest:https://user:pass123@host/x failed"), "Fatal: rest:https://user:***@host/x failed")
        os.environ["RESTIC_REPOSITORY"], keep = "/nicht/da/repo", os.environ["RESTIC_REPOSITORY"]
        try:
            with self.assertRaises(TargetError) as cm:
                ResticTarget().test()
            self.assertNotIn("geheim", str(cm.exception))
        finally:
            os.environ["RESTIC_REPOSITORY"] = keep


if __name__ == "__main__":
    unittest.main()
