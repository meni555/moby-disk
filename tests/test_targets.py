"""Tests der Ziel-Schicht (ohne Docker-Zugriff und ohne echte Backup-Lösung).

Aufruf im Image:  docker run --rm -v $PWD/tests:/tests:ro --network none --entrypoint python IMAGE -m unittest discover -s /tests -v
"""
import json
import os
import time
import unittest

import _env  # noqa: F401 – Umgebung, temporäre Ordner und alte Datenbank (muss vor app.* geladen werden)

from app import backup, insights, rules, store, targets  # noqa: E402
from app.targets.base import FETCH, TRIGGER, VERSIONS, Run, RunResult, Target, TargetError  # noqa: E402
from app.tasks import JobManager, confirmed_folders, sync_target_state  # noqa: E402

F1, F2, F3 = "2026-10-29T20-00-00-000000Z", "2026-10-30T06-00-00-000000Z", "2026-10-31T06-00-00-000000Z"


class FakeTarget(Target):
    """Gibt Stände vor, merkt sich Aufrufe und schreibt beim Restore ein minimales Backup-Set."""
    id, label, kind = "fake", "Fake", "push"
    capabilities = frozenset({TRIGGER, VERSIONS, FETCH})
    folders = {}
    unreachable = False
    started, fetched = [], []
    result = RunResult(True, "Completed")

    def configured(self):
        return True

    def test(self):
        return {"server": "fake"}

    def start(self, items, options, log_cb):
        FakeTarget.started.append((list(items), dict(options)))
        return Run(id="r1")

    def wait(self, run, progress_cb=None, max_wait=0):
        if progress_cb:
            progress_cb(100.0, "Fake: fertig")
        return FakeTarget.result

    def list_folders(self, container):
        if FakeTarget.unreachable:
            return None
        return {f: {"time": time.time(), "size": 111, "ref": "r1"} for f in FakeTarget.folders.get(container, [])}

    def list_containers(self):
        return None if FakeTarget.unreachable else sorted(FakeTarget.folders)

    def fetch(self, container, folder, dest, version=None, progress_cb=None, log_cb=None):
        FakeTarget.fetched.append((container, folder))
        d = os.path.join(dest, container, folder)
        os.makedirs(d)
        with open(os.path.join(d, "manifest.json"), "w") as fh:
            json.dump({"container": container, "ts": folder}, fh)


targets.REGISTRY["fake"] = FakeTarget


def reset_fake(**folders):
    FakeTarget.folders = folders
    FakeTarget.unreachable = False
    FakeTarget.started, FakeTarget.fetched = [], []
    FakeTarget.result = RunResult(True, "Completed")


def local_backup(container, folder, size=100):
    d = os.path.join(os.environ["BACKUP_ROOT"], container, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as fh:
        json.dump({"container": container, "ts": folder, "total_size_bytes": size, "image_data": None, "mode": "stop"}, fh)


class StoreAndRegistry(unittest.TestCase):
    def test_migration_from_cvstate(self):
        rows = store.entries("alt")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["target"], rows[0]["ref"], rows[0]["info"]), ("commvault", "7", {"size_bytes": 5}))
        with store._lock:
            self.assertIsNone(store._db().execute("SELECT 1 FROM sqlite_master WHERE name='cvstate'").fetchone())

    def test_registry(self):
        self.assertEqual(targets.enabled_ids(), ["commvault", "fake"])
        everything = {d["id"]: d for d in targets.describe_all()}
        self.assertTrue({"commvault", "restic", "veeam", "pbs", "command"} <= set(everything))   # alle bekannten Ziele
        self.assertEqual([i for i, d in everything.items() if d["enabled"]], ["commvault", "fake"])
        self.assertFalse(everything["veeam"]["enabled"])
        self.assertEqual([i for i in everything if i in ("commvault", "veeam", "pbs", "restic", "command")],
                         ["commvault", "veeam", "pbs", "restic", "command"])   # feste Reihenfolge, unabhängig von „aktiv“
        with self.assertRaises(TargetError):
            targets.get("gibtsnicht")

    def test_marks_per_target_keep_info(self):
        store.mark_verified("fake", "c1", F1, 100, "a", info={"size_bytes": 9})
        store.mark_verified("commvault", "c1", F1, 100, "b")
        store.mark_verified("fake", "c1", F1, 200, None, info={"size_bytes": 1})   # erneut: Eckdaten + Ref bleiben
        e = {x["target"]: x for x in store.entries("c1")}
        self.assertEqual((e["fake"]["ref"], e["fake"]["info"]["size_bytes"], e["fake"]["verified"]), ("a", 9, 200))
        self.assertEqual(store.verified_folders("c1"), {F1})
        self.assertEqual(store.verified_folders("c1", "fake"), {F1})
        self.assertEqual(store.drop_entries("fake", "c1", keep=[]), 1)
        self.assertEqual(store.verified_folders("c1", "fake"), set())
        self.assertEqual(store.verified_folders("c1", "commvault"), {F1})


class Sync(unittest.TestCase):
    def test_sync_prunes_and_survives_errors(self):
        for e in store.entries(target="fake"):   # Einträge anderer Tests entfernen
            store.drop_entries("fake", e["container"])
        for c, f in (("s1", F1), ("s1", F2), ("weg", F3)):
            store.mark_verified("fake", c, f)
        reset_fake(s1=[F1])
        res = sync_target_state("fake")
        self.assertEqual((res["containers"], res["folders"], res["removed"]), (1, 1, 2))
        self.assertEqual(store.verified_folders("s1", "fake"), {F1})
        self.assertEqual(store.verified_folders("weg", "fake"), set())
        FakeTarget.unreachable = True   # nicht erreichbar → nichts löschen
        self.assertIsNone(sync_target_state("fake"))
        self.assertEqual(store.verified_folders("s1", "fake"), {F1})
        reset_fake(s1=[])   # Ordner existiert im Ziel, hat aber keinen Stand mehr → Vermerke weg
        self.assertEqual(sync_target_state("fake")["removed"], 1)
        self.assertEqual(store.verified_folders("s1", "fake"), set())

    def test_confirmed_folders_need_all_targets(self):
        reset_fake(cf=[F1, F2])
        store.mark_verified("commvault", "cf", F1)
        self.assertEqual(confirmed_folders("cf", ["fake"]), {F1, F2})
        FakeTarget.unreachable = True
        self.assertIsNone(confirmed_folders("cf", ["fake"]))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.c = app.test_client()

    def test_targets_and_status(self):
        data = self.c.get("/api/targets").get_json()
        self.assertEqual([t["id"] for t in data if t["enabled"]], ["commvault", "fake"])
        self.assertIn("veeam", [t["id"] for t in data])
        fake = next(t for t in data if t["id"] == "fake")
        self.assertEqual((fake["kind"], fake["configured"]), ("push", True))
        self.assertIn("fetch", fake["capabilities"])
        self.assertEqual(self.c.post("/api/targets/fake/test").get_json()["server"], "fake")
        self.assertEqual(self.c.post("/api/targets/nix/test").status_code, 502)

    def test_backups_list_shows_targets_and_keeps_marks_after_delete(self):
        local_backup("api1", F1)
        local_backup("api1", F2)
        store.mark_verified("fake", "api1", F1)
        store.mark_verified("commvault", "api1", F1)
        rows = {(r["container"], r["folder"]): r for r in self.c.get("/api/backups").get_json()["backups"]}
        self.assertEqual(rows[("api1", F1)]["targets"], ["commvault", "fake"])
        self.assertTrue(rows[("api1", F1)]["in_commvault"])
        self.assertFalse(rows[("api1", F2)]["in_target"])
        self.assertEqual(self.c.post("/api/backups/delete", json={"items": [{"container": "api1", "folder": F1}]}).get_json()["deleted"], 1)
        rows = {(r["container"], r["folder"]): r for r in self.c.get("/api/backups").get_json()["backups"]}
        self.assertFalse(rows[("api1", F1)]["local"])
        self.assertEqual(rows[("api1", F1)]["targets"], ["commvault", "fake"])

    def test_versions_lists_each_target(self):
        reset_fake(vv=[F1, F2])
        data = self.c.get("/api/containers/vv/versions").get_json()
        self.assertEqual(sorted(f["folder"] for f in data["targets"]["fake"]["folders"]), [F1, F2])
        self.assertIsNone(data["targets"]["fake"]["error"])
        self.assertIn("nicht", data["targets"]["commvault"]["error"] or "")   # Commvault ist hier nicht eingerichtet
        self.assertEqual(store.verified_folders("vv", "fake"), {F1, F2})
        FakeTarget.unreachable = True
        data = self.c.get("/api/containers/vv/versions").get_json()
        self.assertIn("nicht erreichbar", data["targets"]["fake"]["error"])

    def test_versions_use_remembered_size_when_target_reports_zero(self):
        reset_fake(sz=[F1, F2])
        store.mark_verified("fake", "sz", F1, info={"size_bytes": 1234})       # beim Sichern gemerkt
        store.mark_verified("fake", "sz", F2, info={"size_bytes": 999})
        orig = FakeTarget.list_folders
        FakeTarget.list_folders = lambda self, c: {F1: {"time": 1.0, "size": 0, "ref": "r"}, F2: {"time": 2.0, "size": 500, "ref": "r"}}
        try:
            folders = {f["folder"]: f["size"] for f in self.c.get("/api/containers/sz/versions").get_json()["targets"]["fake"]["folders"]}
        finally:
            FakeTarget.list_folders = orig
        self.assertEqual(folders, {F1: 1234, F2: 500})   # 0 → gemerkter Wert, echter Wert bleibt

    def test_target_sync_job(self):
        reset_fake(sj=[F1])
        job = self.c.post("/api/targets/fake/sync").get_json()
        for _ in range(50):
            j = self.c.get(f"/api/jobs/{job['id']}").get_json()
            if j["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        self.assertEqual(j["status"], "finished", j)
        self.assertEqual(j["result"]["folders"], 1)
        self.assertEqual(store.kv_get("sync_fake")["folders"], 1)


class Insights(unittest.TestCase):
    def test_summary_counts_folders_once_and_per_target(self):
        store.mark_verified("fake", "ins", F1)
        store.mark_verified("commvault", "ins", F1)
        store.mark_verified("commvault", "ins", F2)
        s = insights.target_summary()["ins"]
        self.assertEqual(s["count"], 2)
        self.assertEqual({k: v["count"] for k, v in s["targets"].items()}, {"fake": 1, "commvault": 2})
        p = insights.protection_info({"has_data": True}, [], [], 10 ** 6, s["last"])
        self.assertEqual((p["state"], p["cv_only"]), ("ok", True))


class Restore(unittest.TestCase):
    def test_fetch_from_generic_target(self):
        reset_fake()
        jm = JobManager()
        job = jm._new("restore", {"container": "rr"})
        d, staging = jm._fetch_backup_dir(job["id"], "rr", None, F1, "fake")
        self.assertEqual(FakeTarget.fetched, [("rr", F1)])
        self.assertTrue(d.endswith(f"rr/{F1}") and staging and os.path.isdir(staging))
        with self.assertRaises(TargetError):   # True/„cv“ = Commvault (alte Aufrufer) – hier nicht eingerichtet
            jm._fetch_backup_dir(job["id"], "rr", None, F2, True)

    def test_local_source(self):
        local_backup("rl", F1)
        jm = JobManager()
        job = jm._new("restore", {"container": "rl"})
        d, staging = jm._fetch_backup_dir(job["id"], "rl", None, F1, "local")
        self.assertEqual((os.path.basename(d), staging), (F1, None))


class Deliver(unittest.TestCase):
    def run_deliver(self, container, runs, retention=1, require=True):
        jm = JobManager()
        job = jm._new("backup", {"container": container})
        failed = []
        confirmed = jm._deliver(job["id"], runs, [container], {container: F3}, retention, 0, require, failed)
        return confirmed, failed, jm.get_job(job["id"])

    def test_success_marks_syncs_and_prunes_confirmed_only(self):
        for f in (F1, F2, F3):
            local_backup("dl1", f)
        reset_fake(dl1=[F1, F3])   # F2 ist im Ziel nicht angekommen → bleibt lokal
        confirmed, failed, job = self.run_deliver("dl1", [{"id": "fake", "level": 2}])
        self.assertEqual((confirmed, failed), ({"fake": True}, []))
        self.assertEqual(FakeTarget.started[0][0], [("dl1", F3)])
        self.assertEqual(FakeTarget.started[0][1]["level"], 2)
        left = sorted(b["ts"] for b in backup.list_local_backups("dl1"))
        self.assertEqual(left, [F2, F3])   # F1 war bestätigt und überzählig → gelöscht; F2 nicht bestätigt → bleibt
        self.assertEqual(store.verified_folders("dl1", "fake"), {F1, F3})
        self.assertTrue(any("nicht erreichbar" not in l["message"] for l in job["log"]))

    def test_unreachable_target_pauses_retention(self):
        for f in (F1, F2):
            local_backup("dl2", f)
        reset_fake(dl2=[F1, F2])
        FakeTarget.result = RunResult(True, "Completed")
        orig = FakeTarget.list_folders
        FakeTarget.list_folders = lambda self, c: None   # Ziel antwortet nicht
        try:
            confirmed, failed, job = self.run_deliver("dl2", [{"id": "fake"}])
        finally:
            FakeTarget.list_folders = orig
        self.assertEqual(sorted(b["ts"] for b in backup.list_local_backups("dl2")), [F1, F2])
        self.assertTrue(any("ausgesetzt" in l["message"] for l in job["log"]))

    def test_failed_target_is_reported_and_not_marked(self):
        local_backup("dl3", F1)
        reset_fake(dl3=[])
        FakeTarget.result = RunResult(False, "Failed", "Ziel voll")
        confirmed, failed, job = self.run_deliver("dl3", [{"id": "fake"}], retention=0)
        self.assertEqual(confirmed, {"fake": False})
        self.assertEqual(store.verified_folders("dl3", "fake"), set())
        self.assertTrue(any("Ziel voll" in l["message"] for l in job["log"]))

    def test_target_error_goes_to_failed(self):
        local_backup("dl4", F1)
        reset_fake()
        orig = FakeTarget.start
        FakeTarget.start = lambda self, items, options, log_cb: (_ for _ in ()).throw(TargetError("Server weg"))
        try:
            confirmed, failed, job = self.run_deliver("dl4", [{"id": "fake"}], retention=0)
        finally:
            FakeTarget.start = orig
        self.assertEqual(confirmed, {"fake": False})
        self.assertEqual(failed, [("Ziel Fake", "Server weg")])

    def test_no_targets_no_delivery(self):
        local_backup("dl5", F1)
        reset_fake()
        confirmed, failed, _ = self.run_deliver("dl5", [], retention=0)
        self.assertEqual((confirmed, failed, FakeTarget.started), ({}, [], []))


class Dispatch(unittest.TestCase):
    def test_commvault_level_param_and_legacy_args(self):
        from app.targets import commvault_api
        calls = []
        c = commvault_api.Commvault(url="https://x", username="u", password="p")
        c.login = lambda force=False: "t"
        c._request = lambda method, ep, with_token=True, params=None, json_body=None: calls.append((method, ep, params, json_body)) or {}
        c.start_backup(35, 4)
        self.assertEqual((calls[-1][2], calls[-1][3]), ({"backupLevel": "Synthetic_full"}, None))

    def test_submit_batch_validates_targets(self):
        jm = JobManager()
        with self.assertRaises(TargetError):
            jm.submit_backup_batch(["x"], target_runs=[{"id": "nix"}])
        with self.assertRaises(TargetError):
            jm.submit_restore("x", source="nix")



class Describe(unittest.TestCase):
    def test_info_rows_never_contain_secrets(self):
        secrets = {"CV_PASSWORD": "cv-geheim-1", "VEEAM_PASSWORD": "vb-geheim-2", "PBS_PASSWORD": "pbs-geheim-3",
                   "RESTIC_PASSWORD": "re-geheim-4"}
        old = {k: os.environ.get(k) for k in secrets}
        os.environ.update(secrets)
        os.environ.update(PBS_REPOSITORY="u@pbs!t:pbs-geheim-3@host:8007:ds", RESTIC_REPOSITORY="rest:https://u:re-geheim-4@h/x",
                          VEEAM_URL="https://vbr:9419", VEEAM_JOB="J", COMMAND_START="/bin/true")
        try:
            from app.targets.command import CommandTarget
            from app.targets.pbs import PbsTarget
            from app.targets.restic import ResticTarget
            from app.targets.veeam import VeeamTarget
            from app.targets.commvault import CommvaultTarget
            for cls in (CommvaultTarget, VeeamTarget, PbsTarget, ResticTarget, CommandTarget):
                d = cls().describe()
                text = json.dumps(d, ensure_ascii=False)
                self.assertTrue(d["info"], cls)
                for secret in secrets.values():
                    self.assertNotIn(secret, text, f"{cls.__name__} verrät ein Geheimnis")
                self.assertTrue(all(isinstance(r["label"], str) and isinstance(r["value"], str) for r in d["info"]))
            pw = {r["label"]: r["value"] for r in VeeamTarget().describe()["info"]}["Passwort"]
            self.assertEqual(pw, "gesetzt")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            for k in ("PBS_REPOSITORY", "RESTIC_REPOSITORY", "VEEAM_URL", "VEEAM_JOB", "COMMAND_START"):
                os.environ.pop(k, None)



class ScheduleMigration(unittest.TestCase):
    def test_old_schedule_gets_commvault_target(self):
        from app import scheduler
        old = [{"id": "x1", "name": "Alt", "scope": "all_with_data", "containers": [], "days": [0], "time": "01:00", "mode": "stop",
                "cv_backup_level": 2, "enabled": False, "retention": 1, "retention_days": 0, "format": "tar", "require_cv": True},
               {"id": "x2", "name": "Ohne CV", "scope": "all_with_data", "containers": [], "days": [0], "time": "02:00", "mode": "stop",
                "cv_backup_level": None, "enabled": False}]
        with open(scheduler.SCHEDULES_FILE, "w") as fh:
            json.dump(old, fh)
        items = {s["name"]: s for s in scheduler.list_schedules()}
        self.assertEqual(items["Alt"]["targets"], [{"id": "commvault", "level": 2}])
        self.assertEqual(items["Ohne CV"]["targets"], [])


if __name__ == "__main__":
    unittest.main()
