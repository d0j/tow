from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    "fixture",
    [
        "old-success",
        "current-success",
        "empty-page",
        "legacy-old",
        "legacy-current",
        "rollback-old",
        "rollback-current",
        "rollback-unknown",
        "recovered-old",
        "recovered-current",
        "superseded-old",
        "superseded-current",
        "failed-current",
        "failed-old",
        "refused-current",
        "interrupted-old",
        "idle-old",
        "idle-current",
        "active-preparing",
        "active-checking",
        "active-rollback",
        "retained-error",
        "missing-marker",
        "null-current",
        "boolean-current",
        "number-current",
        "rollback-null-current",
    ],
)
def test_update_reload_follows_loaded_document_and_preserves_operation(fixture):
    script = Path(__file__).parent / "js" / "update_page_version.mjs"
    result = subprocess.run([NODE, str(script), fixture], capture_output=True, text=True, timeout=30, check=True)
    assert all(json.loads(result.stdout.strip()).values())


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_web_update_recovery_and_live_journal():
    script = Path(__file__).parent / "js" / "update_recovery.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(verdict.values())


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_web_update_confirmation_lost_response_rollback_and_offline():
    script = Path(__file__).parent / "js" / "release_updates.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(verdict.values())


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
