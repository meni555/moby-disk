"""Proxmox-Backup-Server-Ziel. Die Tests gegen einen echten PBS laufen nur, wenn PBS_TEST_REPOSITORY, PBS_TEST_PASSWORD
und PBS_TEST_FINGERPRINT gesetzt sind (z. B. mit dem Image dockurr/proxmox-backup); die übrigen laufen immer."""
import json
import os
import shutil
import time
import unittest

import _env  # noqa: F401

from app.targets.base import TargetError  # noqa: E402
from app.targets.pbs import PbsTarget, _mask  # noqa: E402

F1, F2 = "2026-10-29T20-17-23-824692Z", "2026-10-30T06-00-00-000001Z"
ROOT = os.environ["BACKUP_ROOT"]
REAL = all(os.environ.get(k) for k in ("PBS_TEST_REPOSITORY", "PBS_TEST_PASSWORD", "PBS_TEST_FINGERPRINT"))


def make_backup(container, folder, payload):
    d = os.path.join(ROOT, container, folder, "data")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "volume-x.tar"), "wb") as fh:
        fh.write(payload)
    with open(os.path.join(ROOT, container, folder, "manifest.json"), "w") as fh:
        json.dump({"container": container, "ts": folder}, fh)


class Offline(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith("PBS_") and not k.startswith("PBS_TEST_")]}

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("PBS_") and not k.startswith("PBS_TEST_")]:
            del os.environ[k]
        os.environ.update(self.saved)

    def test_not_configured(self):
        t = PbsTarget()
        self.assertFalse(t.configured())
        self.assertEqual(sorted(t.capabilities), ["fetch", "trigger", "versions"])
        with self.assertRaises(TargetError):
            t.test()
        with self.assertRaises(TargetError):
            t.start([("a", F1)], {}, lambda m: None)
        self.assertIsNone(t.list_folders("a"))

    def test_secrets_are_masked(self):
        os.environ["PBS_PASSWORD"] = "supergeheim123"
        self.assertEqual(_mask("Fehler mit supergeheim123 im Text"), "Fehler mit *** im Text")
        self.assertEqual(_mask("https://user:pass@host/x"), "https://user:***@host/x")

    def test_folder_from_archive_name(self):
        snap = {"files": [{"filename": f"{F1}.pxar.didx"}, {"filename": "catalog.pcat1.didx"}, {"filename": "index.json.blob"}]}
        self.assertEqual(PbsTarget._folder(snap), F1)
        self.assertIsNone(PbsTarget._folder({"files": [{"filename": "other.pxar.didx"}]}))
        self.assertIsNone(PbsTarget._folder({"files": []}))

    def test_fetch_rejects_bad_folder(self):
        with self.assertRaises(TargetError):
            PbsTarget().fetch("a", "../x", os.path.join(_env.TMP, "d"))


@unittest.skipUnless(REAL and shutil.which("proxmox-backup-client"), "kein echter PBS konfiguriert (PBS_TEST_*)")
class Real(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = {k: os.environ.get(k) for k in ("PBS_REPOSITORY", "PBS_PASSWORD", "PBS_FINGERPRINT", "PBS_NAMESPACE", "PBS_KEEP")}
        os.environ.update(PBS_REPOSITORY=os.environ["PBS_TEST_REPOSITORY"], PBS_PASSWORD=os.environ["PBS_TEST_PASSWORD"],
                          PBS_FINGERPRINT=os.environ["PBS_TEST_FINGERPRINT"])
        os.environ.pop("PBS_NAMESPACE", None)
        os.environ.pop("PBS_KEEP", None)
        cls.t = PbsTarget()
        cls.prefix = f"t{int(time.time()) % 100000}"

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def name(self, n):
        return f"{self.prefix}-{n}"

    def test_1_connection(self):
        info = self.t.test()
        self.assertIn("server version", info["version"])
        self.assertFalse(info["verschlüsselt"])

    def test_2_wrong_fingerprint_is_explained(self):
        os.environ["PBS_FINGERPRINT"] = "00:" * 31 + "00"
        try:
            with self.assertRaises(TargetError) as cm:
                PbsTarget().test()
            self.assertIn("ingerprint", str(cm.exception))
        finally:
            os.environ["PBS_FINGERPRINT"] = os.environ["PBS_TEST_FINGERPRINT"]

    def test_3_backup_list_and_restore_roundtrip(self):
        c = self.name("rt")
        payload = os.urandom(400_000)
        make_backup(c, F1, payload)
        make_backup(c, F2, payload)
        log = []
        res = self.t.wait(self.t.start([(c, F1), (c, F2)], {}, log.append))
        self.assertTrue(res.ok, res)
        self.assertEqual(res.done, [c, c])
        found = self.t.list_folders(c)
        self.assertEqual(sorted(found), [F1, F2])
        self.assertAlmostEqual(found[F1]["time"], 1793305043, delta=1)   # Snapshot-Zeit = Zeit des Ordners
        self.assertIn(c, self.t.list_containers())
        dest = os.path.join(_env.TMP, "pbs-restore")
        self.t.fetch(c, F1, dest)
        d = os.path.join(dest, c, F1)
        self.assertEqual(open(os.path.join(d, "data", "volume-x.tar"), "rb").read(), payload)
        self.assertEqual(json.load(open(os.path.join(d, "manifest.json")))["container"], c)
        with self.assertRaises(TargetError):
            self.t.fetch(c, "2020-01-01T00-00-00-000000Z", dest)

    def test_4_partial_failure(self):
        c = self.name("pf")
        make_backup(c, F1, b"x" * 1000)
        res = self.t.wait(self.t.start([(c, F1), (self.name("fehlt"), F1)], {}, lambda m: None))
        self.assertFalse(res.ok)
        self.assertEqual(res.done, [c])
        self.assertIn("nicht gefunden", res.detail)

    def test_5_prune_keeps_requested_snapshots(self):
        c = self.name("pr")
        folders = ["2026-10-01T06-00-00-000001Z", "2026-10-02T06-00-00-000001Z", "2026-10-03T06-00-00-000001Z"]
        for f in folders:
            make_backup(c, f, os.urandom(2000))
        run = self.t.start([(c, f) for f in folders], {}, lambda m: None)
        self.assertEqual(len(self.t.list_folders(c)), 3)
        os.environ["PBS_KEEP"] = "--keep-last 2"
        log = []
        try:
            self.t.after_success(run, log.append)
        finally:
            os.environ.pop("PBS_KEEP")
        self.assertEqual(sorted(self.t.list_folders(c)), folders[1:])
        self.assertTrue(any("entfernt" in m for m in log))

    def test_7_client_side_encryption(self):
        import subprocess
        key = os.path.join(_env.TMP, "pbs.key")
        subprocess.run(["proxmox-backup-client", "key", "create", key, "--kdf", "none"], check=True, capture_output=True)
        c = self.name("enc")
        payload = os.urandom(50_000)
        make_backup(c, F1, payload)
        os.environ["PBS_KEYFILE"] = key
        try:
            self.assertTrue(self.t.test()["verschlüsselt"])
            self.assertTrue(self.t.wait(self.t.start([(c, F1)], {}, lambda m: None)).ok)
            dest = os.path.join(_env.TMP, "pbs-enc")
            self.t.fetch(c, F1, dest)
            self.assertEqual(open(os.path.join(dest, c, F1, "data", "volume-x.tar"), "rb").read(), payload)
        finally:
            os.environ.pop("PBS_KEYFILE")
        with self.assertRaises(TargetError):   # ohne Schlüssel lässt sich der Stand nicht wiederherstellen
            self.t.fetch(c, F1, os.path.join(_env.TMP, "pbs-enc2"))

    def test_6_missing_namespace_is_reported(self):
        os.environ["PBS_NAMESPACE"] = "gibt-es-nicht"
        try:
            with self.assertRaises(TargetError) as cm:
                PbsTarget().test()
            self.assertIn("Namespace", str(cm.exception))
        finally:
            os.environ.pop("PBS_NAMESPACE")


if __name__ == "__main__":
    unittest.main()
