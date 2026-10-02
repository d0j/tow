from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_home_countdown_polls_with_backoff_and_never_in_a_hidden_tab():
    # F6: /health.json was polled every 5 s forever while a check was overdue, in every tab.
    script = Path(__file__).parent / "js" / "clock_poll.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])

    assert verdict["overdueGaps"][:4] == [10, 20, 40, 60]
    assert max(verdict["overdueGaps"]) == 60
    assert verdict["whileHidden"] == 0
    assert verdict["onShow"] == 1
    assert verdict["pollsAfterRecovery"] == 0
