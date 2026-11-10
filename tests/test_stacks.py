"""Stack-Ansicht: Übersicht je Compose-Stack und Backup-Läufe mit ihren Quellen."""
import unittest
from unittest import mock

import _env  # noqa: F401

from app import store, targets  # noqa: E402
from app.targets.base import FETCH, TRIGGER, Target  # noqa: E402

OLD, NEW = "2026-10-29T10-00-00-000000Z", "2026-10-30T10-00-00-000000Z"


class Fakey(Target):
    id, label, kind = "fakey", "Fakey", "pull"
    capabilities = frozenset({TRIGGER, FETCH})

    def configured(self):
        return True


def container(name, stack, running=True, **extra):
    return {"name": name, "stack": stack, "running": running, "state": "running" if running else "exited",
            "image": "img", "has_data": True, **extra}


class StacksApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.c = app.test_client()
        targets.REGISTRY["fakey"] = Fakey
        import os
        cls._targets = os.environ["TARGETS"]
        os.environ["TARGETS"] = cls._targets + ",fakey"
        store.record_run("r-old", "app", OLD, "shop")
        store.record_run("r-old", "db", OLD, "shop")
        store.record_run("r-new", "app", NEW, "shop")
        store.record_run("r-new", "db", NEW, "shop")
        store.record_run("r-gone", "x1", NEW, "verschwunden")
        store.record_run("r-solo", "solo", NEW, None)
        for name in ("app", "db"):
            store.mark_verified("fakey", name, NEW, 100, "a")

    @classmethod
    def tearDownClass(cls):
        import os
        os.environ["TARGETS"] = cls._targets
        targets.REGISTRY.pop("fakey", None)

    def run_with(self, path):
        live = [container("app", "shop"), container("db", "shop", running=False), container("cache", "shop"),
                container("shop-clone", "shop", clone_of="app"), container("solo", None)]
        manifest = lambda name, folder: {} if (name == "app" and folder == NEW) else None   # nur app liegt lokal
        with mock.patch("app.docker_api.list_containers", return_value=live), \
             mock.patch("app.backup.read_manifest", side_effect=manifest):
            return self.c.get(path).get_json()

    def test_overview_groups_by_stack_and_ignores_clones_and_loose_containers(self):
        data = {s["name"]: s for s in self.run_with("/api/stacks")}
        self.assertEqual(sorted(data), ["shop", "verschwunden"])
        shop = data["shop"]
        self.assertEqual([c["name"] for c in shop["containers"]], ["app", "db", "cache"])
        self.assertEqual((shop["running"], shop["present"]), (2, True))
        last = shop["last_run"]
        self.assertEqual((last["run_id"], len(last["members"])), ("r-new", 2))
        self.assertEqual(last["added_since"], ["cache"])                        # neu im Stack, nicht im Lauf
        self.assertFalse(last["sources"]["local"])                              # db liegt nicht lokal
        self.assertTrue(last["sources"]["fakey"])
        self.assertFalse(data["verschwunden"]["present"])
        self.assertIsNotNone(data["verschwunden"]["last_run"])

    def test_runs_are_newest_first_with_sources(self):
        data = self.run_with("/api/stacks/shop/runs")
        self.assertEqual([r["run_id"] for r in data["runs"]], ["r-new", "r-old"])
        self.assertIn("fakey", [s["id"] for s in data["sources"]])
        self.assertEqual(data["sources"][0]["id"], "local")
        self.assertGreater(data["runs"][0]["time"], data["runs"][1]["time"])
        self.assertFalse(data["runs"][1]["sources"]["fakey"])                   # der alte Lauf ist im Ziel nicht bestätigt

    def test_stack_without_any_run_has_no_last_run(self):
        data = {s["name"]: s for s in self.run_with("/api/stacks")}
        self.assertEqual(self.run_with("/api/stacks/nix/runs")["runs"], [])
        self.assertIsNone(data["shop"]["last_run"]["run_id"] and None)


if __name__ == "__main__":
    unittest.main()
