"""Aufträge pausieren, fortsetzen und abbrechen – Steuerung, sichere Stellen, Ziele, Archivierung, Prozesse."""
import os
import subprocess
import threading
import time
import unittest

import _env  # noqa: F401

from app import backup, tasks  # noqa: E402
from app.backup import BackupError  # noqa: E402
from app.cancel import Cancelled  # noqa: E402
from app.targets.base import TRIGGER, Run, RunResult, Target, run_process  # noqa: E402
from app.tasks import JobManager  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Control(unittest.TestCase):
    def setUp(self):
        self.jm = JobManager()
        self.ticks = 0

    def start(self, steps=400, kind="backup"):
        def work(job_id):
            for _ in range(steps):
                self.jm.checkpoint(job_id)
                self.ticks += 1
                time.sleep(0.02)
            return {"ok": True}
        job = self.jm._new(kind, {"container": "x"})
        self.jm._start(job["id"], work)
        self.assertTrue(wait_for(lambda: self.ticks > 3))
        return job["id"]

    def test_pause_blocks_progress_and_resume_continues(self):
        jid = self.start()
        self.jm.control(jid, "pause")
        self.assertEqual(self.jm.get_job(jid)["control"], "pause")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "paused"))
        frozen = self.ticks
        time.sleep(0.6)
        self.assertEqual(self.ticks, frozen)                        # steht wirklich still
        self.assertTrue(self.jm.get_job(jid)["message"].startswith("Pausiert"))
        self.jm.control(jid, "resume")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "running"))
        self.assertTrue(wait_for(lambda: self.ticks > frozen + 3))
        self.assertIsNone(self.jm.get_job(jid)["control"])
        self.jm.control(jid, "cancel")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "cancelled"))

    def test_cancel_ends_job_as_cancelled(self):
        jid = self.start()
        self.jm.control(jid, "cancel")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "cancelled"))
        job = self.jm.get_job(jid)
        self.assertTrue(job["finished"] and job["error"] is None)
        self.assertEqual(job["message"], "Abgebrochen")
        stop = self.ticks
        time.sleep(0.3)
        self.assertEqual(self.ticks, stop)
        with self.assertRaises(BackupError):                       # beendet → nicht mehr steuerbar
            self.jm.control(jid, "pause")

    def test_cancel_while_paused(self):
        jid = self.start()
        self.jm.control(jid, "pause")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "paused"))
        self.jm.control(jid, "cancel")
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "cancelled"))

    def test_locked_job_refuses_control(self):
        jid = self.start()
        self.jm.lock(jid)
        for action in ("pause", "cancel"):
            with self.assertRaises(BackupError) as cm:
                self.jm.control(jid, action)
            self.assertIn("zurückgeschrieben", str(cm.exception))
        self.jm._ctl[jid]["cancel"] = True   # Aufräumen
        self.assertTrue(wait_for(lambda: self.jm.get_job(jid)["status"] == "cancelled"))

    def test_unknown_job_and_action(self):
        with self.assertRaises(BackupError):
            self.jm.control("gibtsnicht", "pause")
        jid = self.start()
        with self.assertRaises(BackupError):
            self.jm.control(jid, "explodieren")
        self.jm.control(jid, "cancel")

    def test_errors_still_end_as_failed(self):
        job = self.jm._new("backup", {"container": "x"})
        self.jm._start(job["id"], lambda jid: (_ for _ in ()).throw(RuntimeError("kaputt")))
        self.assertTrue(wait_for(lambda: self.jm.get_job(job["id"])["status"] == "failed"))

    def test_api(self):
        from app.main import app, jobs
        c = app.test_client()
        done = threading.Event()

        def work(job_id):
            while not done.is_set():
                jobs.checkpoint(job_id)
                time.sleep(0.02)
        job = jobs._new("backup", {"container": "api"})
        jobs._start(job["id"], work)
        r = c.post(f"/api/jobs/{job['id']}/pause")
        self.assertEqual((r.status_code, r.get_json()["control"]), (200, "pause"))
        self.assertTrue(wait_for(lambda: c.get(f"/api/jobs/{job['id']}").get_json()["status"] == "paused"))
        self.assertEqual(c.post(f"/api/jobs/{job['id']}/resume").status_code, 200)
        self.assertEqual(c.post(f"/api/jobs/{job['id']}/explodieren").status_code, 404)
        self.assertEqual(c.post(f"/api/jobs/{job['id']}/cancel").status_code, 200)
        self.assertTrue(wait_for(lambda: c.get(f"/api/jobs/{job['id']}").get_json()["status"] == "cancelled"))
        r = c.post(f"/api/jobs/{job['id']}/pause")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(c.post("/api/jobs/nix/cancel").status_code, 409)


class Archiving(unittest.TestCase):
    def test_tar_stops_on_cancel(self):
        src = os.path.join(_env.TMP, "cancel-src")
        os.makedirs(src, exist_ok=True)
        for i in range(900):
            with open(os.path.join(src, f"f{i:04d}"), "wb") as fh:
                fh.write(b"x" * 100)
        calls = []

        def cancel():
            calls.append(1)
            if len(calls) >= 2:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            backup._tar_dir(src, os.path.join(_env.TMP, "cancel.tar"), lambda m: None, compress=False, cancel=cancel)
        self.assertEqual(len(calls), 2)   # nach 200 und 400 Dateien gefragt, dann Schluss

    def test_tar_without_cancel_unchanged(self):
        out = os.path.join(_env.TMP, "nocancel.tar")
        self.assertEqual(backup._tar_dir(os.path.join(_env.TMP, "cancel-src"), out, lambda m: None, compress=False) if os.path.isdir(os.path.join(_env.TMP, "cancel-src")) else 900, 900)


class Processes(unittest.TestCase):
    def test_run_process_normal(self):
        res = run_process(["sh", "-c", "echo hallo; echo fehler >&2; exit 3"], 10)
        self.assertEqual((res.returncode, res.stdout.strip(), res.stderr.strip()), (3, "hallo", "fehler"))

    def test_run_process_cancel_terminates_child(self):
        pidfile = os.path.join(_env.TMP, "child.pid")
        calls = []

        def cancel():
            calls.append(1)
            if len(calls) >= 2:
                raise Cancelled()
        started = time.time()
        with self.assertRaises(Cancelled):
            run_process(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"], 120, cancel=cancel)
        self.assertLess(time.time() - started, 15)
        pid = int(open(pidfile).read())
        self.assertFalse(wait_for(lambda: os.path.exists(f"/proc/{pid}") and "sleep" in open(f"/proc/{pid}/cmdline").read(), 1.0))

    def test_run_process_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process(["sleep", "30"], 2)


class SlowTarget(Target):
    id, label, kind = "slow", "Slow", "pull"
    capabilities = frozenset({TRIGGER})
    cancelled_with = None

    def configured(self):
        return True

    def start(self, items, options, log_cb):
        return Run(id="job-7")

    def wait(self, run, progress_cb=None, max_wait=0):
        for _ in range(500):
            progress_cb(10.0, "Slow: läuft")
            time.sleep(0.02)
        return RunResult(True, "Completed")

    def cancel(self, run, log_cb):
        SlowTarget.cancelled_with = run.id
        log_cb("Lauf beim Ziel gestoppt")

    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        os.makedirs(os.path.join(dest, "teil"), exist_ok=True)
        for _ in range(500):
            progress_cb(5.0, "holt …")
            time.sleep(0.02)


class TargetsAndRestore(unittest.TestCase):
    def setUp(self):
        from app import targets
        targets.REGISTRY["slow"] = SlowTarget
        SlowTarget.cancelled_with = None
        self.jm = JobManager()

    def test_cancel_while_waiting_for_target_stops_remote_run(self):
        job = self.jm._new("backup", {"container": "x"})
        failed = []
        result = {}

        def work(job_id):
            try:
                self.jm._deliver(job_id, [{"id": "slow"}], ["c1"], {"c1": "2026-10-29T20-00-00-000000Z"}, 0, 0, False, failed)
            except Cancelled:
                result["cancelled"] = True
                raise
        self.jm._start(job["id"], work)
        time.sleep(0.5)
        self.jm.control(job["id"], "cancel")
        self.assertTrue(wait_for(lambda: self.jm.get_job(job["id"])["status"] == "cancelled"))
        self.assertEqual((SlowTarget.cancelled_with, result.get("cancelled")), ("job-7", True))
        self.assertTrue(any("beim Ziel gestoppt" in l["message"] for l in self.jm.get_job(job["id"])["log"]))

    def test_cancel_during_fetch_removes_staging(self):
        job = self.jm._new("restore", {"container": "x"})
        staging = {}

        def work(job_id):
            d, s = self.jm._fetch_backup_dir(job_id, "cx", None, "2026-10-29T20-00-00-000000Z", "slow")
            return d
        self.jm._start(job["id"], work)
        time.sleep(0.6)
        self.jm.control(job["id"], "cancel")
        self.assertTrue(wait_for(lambda: self.jm.get_job(job["id"])["status"] == "cancelled"))
        left = [n for n in os.listdir(os.path.join(os.environ["BACKUP_ROOT"], ".staging"))] if os.path.isdir(os.path.join(os.environ["BACKUP_ROOT"], ".staging")) else []
        self.assertFalse([n for n in left if job["id"] in n])



class VanishingFiles(unittest.TestCase):
    def test_file_vanishing_while_packing_is_skipped(self):
        src = os.path.join(_env.TMP, "vanish-src")
        os.makedirs(src, exist_ok=True)
        for name in ("a.log", "b.log.1.gz", "c.log"):
            with open(os.path.join(src, name), "w") as fh:
                fh.write(name)
        real = backup._add_path
        seen = []

        def flaky(tar, full, arcname, target_file, db_files):
            if arcname == "b.log.1.gz":
                raise FileNotFoundError(2, "No such file or directory", full)
            seen.append(arcname)
            return real(tar, full, arcname, target_file, db_files)
        log = []
        backup._add_path = flaky
        try:
            count = backup._tar_dir(src, os.path.join(_env.TMP, "vanish.tar"), log.append, compress=False)
        finally:
            backup._add_path = real
        self.assertEqual((count, seen), (2, ["a.log", "c.log"]))
        import tarfile
        self.assertEqual(sorted(tarfile.open(os.path.join(_env.TMP, "vanish.tar")).getnames()), ["a.log", "c.log"])
        self.assertTrue(any("1 während des Packens verschwunden" in m for m in log))


if __name__ == "__main__":
    unittest.main()
