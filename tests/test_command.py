"""Befehl/Skript-Ziel mit echten Shell-Skripten."""
import json
import os
import stat
import unittest

import _env  # noqa: F401

from app.targets.base import TargetError  # noqa: E402
from app.targets.command import CommandTarget  # noqa: E402

F1 = "2026-10-29T20-00-00-000000Z"
D = os.path.join(_env.TMP, "scripts")
os.makedirs(D, exist_ok=True)


def script(name, body):
    path = os.path.join(D, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class Command(unittest.TestCase):
    def setUp(self):
        for k in [k for k in os.environ if k.startswith("COMMAND_")]:
            del os.environ[k]
        self.t = CommandTarget()

    def test_not_configured_and_capabilities(self):
        self.assertFalse(self.t.configured())
        with self.assertRaises(TargetError):
            self.t.test()
        os.environ["COMMAND_START"] = script("start.sh", "exit 0")
        os.environ["COMMAND_LABEL"] = "Veeam"
        t = CommandTarget()
        self.assertEqual((t.configured(), t.label, sorted(t.capabilities)), (True, "Veeam", ["trigger"]))
        self.assertEqual(t.describe()["label"], "Veeam")

    def test_start_gets_items_and_failure_is_reported(self):
        out = os.path.join(D, "items.json")
        os.environ["COMMAND_START"] = script("start2.sh", f'echo "$HELPER_ITEMS" > {out}; echo hallo')
        log = []
        run = self.t.start([("app1", F1)], {}, log.append)
        items = json.load(open(out))
        self.assertEqual((items[0]["container"], items[0]["folder"]), ("app1", F1))
        self.assertTrue(items[0]["path"].endswith(f"app1/{F1}"))
        self.assertTrue(self.t.wait(run).ok)
        self.assertTrue(any("hallo" in m for m in log))
        os.environ["COMMAND_START"] = script("start3.sh", "echo kaputt >&2; exit 3")
        with self.assertRaises(TargetError) as cm:
            self.t.start([("app1", F1)], {}, log.append)
        self.assertIn("Code 3", str(cm.exception))
        self.assertIn("kaputt", str(cm.exception))

    def test_status_polling(self):
        counter = os.path.join(D, "n")
        open(counter, "w").write("0")
        os.environ["COMMAND_START"] = script("s.sh", "exit 0")
        os.environ["COMMAND_STATUS"] = script("st.sh", f'n=$(cat {counter}); echo $((n+1)) > {counter}; [ "$n" -ge 2 ] && exit 0; exit 10')
        os.environ["COMMAND_POLL_SECONDS"] = "1"
        res = self.t.wait(self.t.start([], {}, lambda m: None))
        self.assertTrue(res.ok)
        self.assertEqual(open(counter).read().strip(), "3")
        os.environ["COMMAND_STATUS"] = script("st2.sh", "exit 1")
        res = self.t.wait(self.t.start([], {}, lambda m: None))
        self.assertFalse(res.ok)
        self.assertIn("Code 1", res.detail)

    def test_list_and_no_shell_injection(self):
        args = os.path.join(D, "args.txt")
        os.environ["COMMAND_LIST"] = script("list.sh", f'printf "%s\\n" "$@" > {args}; echo \'{{"{F1}": {{"time": 1790000000, "size": 12}}, "evil/../x": {{}}}}\'') + " {container}"
        t = CommandTarget()
        found = t.list_folders("a; touch /tmp/pwned")
        self.assertEqual(list(found), [F1])
        self.assertEqual(found[F1]["size"], 12)
        self.assertEqual(open(args).read().strip(), "a; touch /tmp/pwned")   # ein einziges Argument
        self.assertFalse(os.path.exists("/tmp/pwned"))
        self.assertIn("app1", t.list_containers() or ["app1"])

    def test_fetch(self):
        os.environ["COMMAND_FETCH"] = script("fetch.sh", '[ "$1" = app1 ] || exit 4; mkdir -p "$3/$1/$2"; echo "{}" > "$3/$1/$2/manifest.json"') + " {container} {folder} {dest}"
        t = CommandTarget()
        dest = os.path.join(_env.TMP, "fetched")
        t.fetch("app1", F1, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "app1", F1, "manifest.json")))
        with self.assertRaises(TargetError):
            t.fetch("andere", F1, dest)
        with self.assertRaises(TargetError):
            t.fetch("app1", "../..", dest)
        del os.environ["COMMAND_FETCH"]
        with self.assertRaises(TargetError):
            CommandTarget().fetch("app1", F1, dest)

    def test_missing_program(self):
        os.environ["COMMAND_START"] = "/gibt/es/nicht.sh"
        with self.assertRaises(TargetError) as cm:
            self.t.start([], {}, lambda m: None)
        self.assertIn("nicht gefunden", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
