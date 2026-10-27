"""Status observes all three authorities without changing their state."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manage import status_report
from akernel_upgrade.errors import UpgradeError


class StatusTests(unittest.TestCase):
    def test_live_adx_and_raw_scaler_status_are_shown_alongside_local_progress(self):
        class Runner:
            def load(self, **kwargs): return {"phase": "NeedsAttention"}
        class Manager:
            def status(self, state): return {"phase": "Active", "revision": 9}
        class Kube:
            def get(self, kind, name):
                return {"data": {"status.json": json.dumps({"handoff": "Preparing"})}}
        value = status_report(Runner(), Manager(), Kube(), "guard", "task")
        self.assertEqual(value["local"]["value"]["phase"], "NeedsAttention")
        self.assertEqual(value["adx"]["value"]["revision"], 9)
        self.assertEqual(value["scaler"]["value"]["handoff"], "Preparing")

    def test_failed_read_is_unknown_and_does_not_hide_other_channels_or_print_secrets(self):
        class Runner:
            def load(self, **kwargs): raise UpgradeError("secret-value")
        class Manager:
            def status(self, state): return {"phase": "Active"}
        class Kube:
            def get(self, kind, name): return {"data": {"status.json": "invalid"}}
        value = status_report(Runner(), Manager(), Kube(), "guard", "task")
        self.assertEqual(value["local"]["outcome"], "unknown")
        self.assertEqual(value["adx"]["outcome"], "observed")
        self.assertEqual(value["scaler"]["outcome"], "unknown")
        self.assertNotIn("secret-value", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
