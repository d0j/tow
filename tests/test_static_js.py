from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_content_picker_boundaries_and_async_selection():
    script = Path(__file__).parent / "js" / "content_picker.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    assert all(json.loads(result.stdout.strip()).values())


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_update_notice_and_disabled_check_status():
    script = Path(__file__).parent / "js" / "update_notice.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    assert all(json.loads(result.stdout.strip()).values())


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_update_reload_follows_loaded_document_and_preserves_operation():
    script = Path(__file__).parent / "js" / "update_page_version.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip())
    assert verdict.pop("scenarios") == 30
    assert all(verdict.values())


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
def test_a_pasted_site_link_never_overwrites_what_the_owner_typed():
    script = Path(__file__).parent / "js" / "site_guess_fill.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(verdict.values()), verdict


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


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_countdown_and_personal_timers_share_one_health_poll():
    # Two independent pollers asked /health.json at 0, 5, 15, 31, 35, 61 and 75 s.
    script = Path(__file__).parent / "js" / "health_poll.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])

    assert verdict["firstMinute"] == [0, 10, 30, 60]
    assert verdict["timerUpdated"] is True
    assert verdict["clock"] == "00:00:00"
    assert verdict["whileHidden"] == 0
    assert verdict["onShow"] == 1


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_escape_folds_the_open_add_form():
    script = Path(__file__).parent / "js" / "escape_closes_add.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(verdict.values()), verdict


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_slow_action_button_says_it_is_working_and_recovers_after_a_failure():
    script = Path(__file__).parent / "js" / "busy_label.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(verdict.values()), verdict
