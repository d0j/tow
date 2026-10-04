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
    ("page", "current", "target", "status", "reload", "key"),
    [
        ("1.22.29", "1.22.30", "1.22.30", "ok", True, "js.releases.ok"),
        ("1.22.30", "1.22.30", "1.22.30", "ok", False, "js.releases.ok_current"),
        ("", "1.22.30", "1.22.30", "ok", True, "js.releases.ok"),
        ("1.22.29", "", "1.22.30", "ok", True, "js.releases.ok"),
        ("1.22.30", "", "1.22.30", "ok", False, "js.releases.ok_current"),
        ("1.22.30", "1.22.29", "1.22.30", "rolled_back", True, "js.releases.rolled_back"),
        ("1.22.29", "1.22.29", "1.22.30", "rolled_back", False, "js.releases.rolled_back"),
        ("1.22.30", "", "1.22.30", "rolled_back", False, "js.releases.rolled_back"),
        ("1.22.29", "1.22.30", "1.22.30", "recovered", True, "js.releases.recovered"),
        ("1.22.30", "1.22.30", "1.22.30", "recovered", False, "js.releases.recovered"),
        ("1.22.31", "1.22.30", "1.22.29", "superseded", True, "js.releases.superseded"),
        ("1.22.30", "1.22.30", "1.22.29", "superseded", False, "js.releases.superseded"),
        ("1.22.29", "1.22.29", "1.22.30", "failed", False, "js.releases.failed"),
        ("1.22.29", "1.22.30", "1.22.30", "failed", True, "js.releases.failed"),
        ("1.22.29", "1.22.29", "1.22.30", "refused", False, "js.releases.refused"),
        ("1.22.29", "1.22.30", "1.22.30", "interrupted", True, "js.releases.interrupted"),
        ("1.22.29", "1.22.30", "1.22.30", "idle", True, ""),
        ("1.22.30", "1.22.30", "1.22.30", "idle", False, ""),
    ],
)
def test_update_reload_follows_loaded_document_not_latest_server_version(page, current, target, status, reload, key):
    script = Path(__file__).parent / "js" / "update_page_version.mjs"
    scenario = {
        "page": page,
        "current": current,
        "target": target,
        "status": status,
        "reload": reload,
        "key": key,
        "mutate": True,
    }
    result = subprocess.run(
        [NODE, str(script), json.dumps(scenario)], capture_output=True, text=True, timeout=30, check=True
    )
    assert all(json.loads(result.stdout.strip()).values())


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    "scenario",
    [
        {
            "status": status,
            "active": True,
            "reload": False,
            "key": "js.releases.phase_checking" if status == "checking" else f"js.releases.{status}",
        }
        for status in ("preparing", "checking", "rolling_back")
    ]
    + [
        {"status": "failed", "reload": True, "key": "", "error": "Retained failure details"},
        {"status": "ok", "reload": True, "key": "js.releases.ok", "noMarker": True},
        {"status": "ok", "current": None, "reload": True, "key": "js.releases.ok"},
        {"status": "ok", "current": True, "reload": True, "key": "js.releases.ok"},
        {"status": "ok", "current": 123, "reload": True, "key": "js.releases.ok"},
        {"status": "rolled_back", "current": None, "reload": False, "key": "js.releases.rolled_back"},
    ],
)
def test_update_page_version_keeps_active_operation_and_failure_evidence(scenario):
    script = Path(__file__).parent / "js" / "update_page_version.mjs"
    case = {"page": "1.22.29", "current": "1.22.30", "target": "1.22.30", "mutate": True}
    case.update(scenario)
    result = subprocess.run(
        [NODE, str(script), json.dumps(case)], capture_output=True, text=True, timeout=30, check=True
    )
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
