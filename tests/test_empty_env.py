"""Leere Platzhalter in der .env (z. B. „PBS_FINGERPRINT=“) gelten als nicht gesetzt."""
import os
import subprocess
import sys
import unittest

import _env  # noqa: F401


class EmptyEnvTest(unittest.TestCase):
    def test_empty_target_variables_are_removed_at_import(self):
        env = dict(os.environ, PBS_FINGERPRINT="", VEEAM_JOB="  ", AWS_ACCESS_KEY_ID="", RESTIC_KEEP="--keep-last 3", DATA_DIR="/tmp")
        code = ("import os, sys; sys.path.insert(0, '/app'); import app.config; "
                "print(sorted(k for k in ('PBS_FINGERPRINT','VEEAM_JOB','AWS_ACCESS_KEY_ID','RESTIC_KEEP') if k in os.environ))")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "['RESTIC_KEEP']", out.stderr)


if __name__ == "__main__":
    unittest.main()
