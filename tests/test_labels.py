"""Label-Präfix mobydisk.* mit Rückfall auf das ältere cvhelper.*."""
import unittest

import _env  # noqa: F401

from app import docker_api, rules  # noqa: E402


class Labels(unittest.TestCase):
    def test_new_prefix_wins_old_still_read(self):
        self.assertEqual(docker_api.own_label({"mobydisk.backup": "false"}, "backup"), "false")
        self.assertEqual(docker_api.own_label({"cvhelper.backup": "false"}, "backup"), "false")
        self.assertEqual(docker_api.own_label({"mobydisk.mode": "live", "cvhelper.mode": "stop"}, "mode"), "live")
        self.assertIsNone(docker_api.own_label({}, "mode"))
        self.assertIsNone(docker_api.own_label(None, "mode"))

    def test_written_labels_carry_both_prefixes(self):
        labels = docker_api.own_labels({"task": "1", "clone-of": "app"})
        self.assertEqual(labels, {"mobydisk.task": "1", "cvhelper.task": "1", "mobydisk.clone-of": "app", "cvhelper.clone-of": "app"})

    def test_backup_candidate_respects_both(self):
        self.assertFalse(docker_api.backup_candidate({"labels": {"mobydisk.backup": "false"}, "has_data": True}))
        self.assertFalse(docker_api.backup_candidate({"labels": {"cvhelper.backup": "off"}, "has_data": True}))

    def test_rules_read_mobydisk_labels(self):
        lab = rules.label_rules({"mobydisk.skip": "/media,/cache", "mobydisk.include": "/data", "cvhelper.exclude-glob": "*.tmp"})
        self.assertEqual((lab["skip"], lab["include"], lab["globs"], lab["skip_all"]), (["/media", "/cache"], ["/data"], ["*.tmp"], False))
        self.assertTrue(rules.label_rules({"mobydisk.backup": "false"})["skip_all"])
        st = rules.container_settings("x", {"mobydisk.mode": "live", "mobydisk.stop-timeout": "30", "cvhelper.pre": "echo hi"})
        self.assertEqual((st["mode"], st["stop_timeout"], st["pre"]), ("live", 30, "echo hi"))


if __name__ == "__main__":
    unittest.main()
