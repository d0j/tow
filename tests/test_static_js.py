from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


def _verdict(name: str) -> dict:
    script = Path(__file__).parent / "js" / name
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_recent_messages_time_pause_read_store_and_undo_by_a_fake_clock():
    verdict = _verdict("messages_center.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 11


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_shows_a_message_once_then_keeps_it_in_the_header_history():
    verdict = _verdict("messages_page.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 11


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_failed_action_is_a_message_about_its_topic_not_a_paragraph_over_its_row():
    # 31-js-submit-failed-row: the error paragraph went into the row's icon form and covered the row.
    verdict = _verdict("action_failures.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 4


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
def test_the_add_form_records_the_title_it_guessed():
    script = Path(__file__).parent / "js" / "topic_title_guess.mjs"
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
def test_the_clock_names_why_the_last_attempt_failed_in_words():
    # The tooltip read "Последняя попытка: error": every class but the master key's was shown raw.
    script = Path(__file__).parent / "js" / "clock_error_words.mjs"
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True
    )
    verdict = json.loads(result.stdout.strip().splitlines()[-1])

    assert verdict == {
        "fromPage": "Последняя попытка: торрент-клиент",
        "afterPoll": "Последняя попытка: сайт недоступен",
    }


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_countdown_and_personal_timers_share_one_health_poll():
    # Two independent pollers asked /health.json at 0, 5, 15, 31, 35, 61 and 75 s.
    script = Path(__file__).parent / "js" / "health_poll.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])

    assert verdict["firstMinute"] == [0, 10, 30, 60]
    assert verdict["timerUpdated"] is True
    assert verdict["timerWords"] is True
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
def test_escape_folds_an_open_row_back_to_its_summary():
    script = Path(__file__).parent / "js" / "escape_closes_row.mjs"
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


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_closed_sign_in_prompt_leaves_the_address_and_gives_the_focus_back():
    script = Path(__file__).parent / "js" / "credential_prompt_close.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    assert verdict == {"modal": True, "reopeningIgnored": True, "address": "/?s=name", "rowFocused": True}, verdict


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_problem_triangle_tracks_confirmed_recovery_and_preserves_errors_when_offline():
    script = Path(__file__).parent / "js" / "attention_fold.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    verdict = json.loads(result.stdout.strip())
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 11


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_check_this_tab_started_is_followed_within_bounds_to_exactly_one_result():
    # D10: the page that followed a check navigated away from results the other clicks were waiting
    # for; a finished check opened a second page. One bounded scheduler follows them all instead.
    verdict = _verdict("check_scheduler.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 14


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_row_whose_check_ended_is_drawn_again_but_never_under_the_owners_hands():
    verdict = _verdict("row_refresh.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 5


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_check_is_sent_with_its_kept_action_id_and_followed_where_the_page_is():
    verdict = _verdict("check_submit.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 6


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_one_broken_health_reader_cannot_stall_or_spin_other_readers():
    script = Path(__file__).parent / "js" / "health_poll_reader_failure.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    assert all(json.loads(result.stdout.strip()).values())
