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
    assert len(verdict) == 12


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_shows_a_message_once_and_the_install_shared_list_with_shared_read_and_clear():
    verdict = _verdict("messages_page.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 24


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
    assert len(verdict) == 17


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_row_drawn_again_behaves_like_a_row_the_page_opened_with():
    # The progress link of a refreshed row opened downloads.json instead of the dialog.
    verdict = _verdict("row_listeners.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 4


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_row_whose_check_ended_is_drawn_again_by_the_one_revision_aware_row_writer():
    # The old /topics/<id>/row refresher replaced a row with a late answer, unchecked against the
    # revision another device's change had brought meanwhile (its busy rules moved to sync.js).
    verdict = _verdict("row_refresh.mjs")
    assert verdict == dict.fromkeys(
        ("oneWriterForTheEndedCheck", "checkAllRowsInSightThenLater", "noRowSyncNoRowRequest"), True
    ), verdict


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_check_is_sent_with_its_kept_action_id_and_followed_where_the_page_is():
    verdict = _verdict("check_submit.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 9


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_secret_fields_eye_shows_and_hides_what_was_typed_and_nothing_else():
    verdict = _verdict("secret_reveal.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 7


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_fields_a_box_turns_off_are_disabled_with_it():
    verdict = _verdict("enabled_groups.mjs")
    assert verdict == {"atStart": True, "followsTheBox": True, "followsAReset": True}


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_link_lands_below_the_header_and_the_message_strip():
    verdict = _verdict("message_landing.mjs")
    assert verdict == {"measured": True, "measuredAgain": True, "everyTargetClearsBoth": True}


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_lists_times_show_in_full_by_one_toggle_and_by_a_tap():
    """1.32.1 (C5): a time is no tab stop of its own (History's 300 lines were 300 stops); one
    "Full timestamps" toggle per list shows them all in full, also lines loaded later, and the tab
    keeps the choice; a tap still shows one time's full time."""
    verdict = _verdict("time_disclosure.mjs")
    keys = ("noTabStops", "tapShowsAndHides", "toggleShowsFull", "laterLinesFollow", "toggleBack", "choiceKept")
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_question_is_asked_in_the_pages_own_dialog():
    """1.32.1 (UI:C1): the browser's confirm ("127.0.0.1 says", OK in the browser's language) is the
    page's dialog: the action's words, Cancel first for a destructive one, Escape and the backdrop
    cancel, the focus goes back, and the confirmed form goes once with its own button."""
    verdict = _verdict("confirm_dialog.mjs")
    keys = (
        "dangerAsksWithCancelFirst",
        "noDoubleSubmit",
        "cancelRestoresFocus",
        "escapeAndBackdropCancel",
        "confirmedSendsOnceWithSubmitter",
        "labelAndFocusForAPlainQuestion",
        "scriptApi",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_file_field_speaks_the_pages_language():
    """1.32.1 (B2): "Choose a file" and the chosen file's name in the page's words; the buttons
    that need a file wait for one only once the script runs."""
    verdict = _verdict("file_chooser.mjs")
    assert verdict == dict.fromkeys(("waitsForAFile", "showsTheName", "resetPutsWordsBack", "scriptSync"), True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_header_search_filters_the_settings_sections():
    """1.32.1 (C2): matching sections stay, the others hide; cleared, all come back as they were."""
    verdict = _verdict("settings_search.mjs")
    assert verdict == dict.fromkeys(("matchingSectionsShown", "allWordsMatch", "clearingKeepsTheirState"), True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_saved_language_puts_the_focus_back_on_its_segment():
    """L1 (QA Q2): after the reload to #acc-language the chosen segment has the focus again, also
    after the browser's own move to the fragment; a focus the page moved itself is kept."""
    verdict = _verdict("language_focus.mjs")
    keys = ("chosenSegmentFocused", "keptAfterLoad", "otherFocusKept", "noFragmentNoChange")
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_new_reminder_without_a_new_password_is_held_with_what_was_typed():
    """§2 (QA Q6): the server refuses a new reminder without a new password on this computer, and
    the page came back with the saved one. The page says so under the reminder before sending and
    keeps the typed text; kept, cleared or with a new password it goes."""
    verdict = _verdict("reminder_needs_password.mjs")
    keys = ("newReminderAloneIsHeld", "withANewPasswordItGoes", "keptOrClearedGoes")
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_updates_card_writes_its_times_by_the_one_rule():
    verdict = _verdict("update_times.mjs")
    assert verdict == {"timesByTheOneRule": True, "olderServerKeepsItsLabel": True}


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_scripts_write_times_by_the_servers_rule_in_tows_zone():
    verdict = _verdict("time_short.mjs")
    assert verdict == dict.fromkeys(verdict, True), verdict
    assert len(verdict) == 10


@pytest.mark.allow_system
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_one_broken_health_reader_cannot_stall_or_spin_other_readers():
    script = Path(__file__).parent / "js" / "health_poll_reader_failure.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=True)
    assert all(json.loads(result.stdout.strip()).values())


# One state on every open page (sync.js): the poll, Home's rows, the reloads, the page's readers.


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_sync_poll_keeps_its_schedule_backoff_hints_and_sign_out():
    verdict = _verdict("sync_client.mjs")
    keys = (
        "randomPhaseThenEveryFive",
        "hiddenAsksNothingShownAsksOnce",
        "deltaHaveEtagAndNotModified",
        "failedReaderRetried",
        "unappliedOfferedAgainThrough304",
        "backoffFiveToSixty",
        "jitterBounded",
        "retryAfterHonoured",
        "tenSecondTimeout",
        "oneInFlightOnePending",
        "hintsCoalescedNeverThroughBackoff",
        "unauthorizedEnds",
        "lateAnswerRejected",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_home_rows_follow_the_topics_area_without_touching_rows_in_use():
    verdict = _verdict("sync_rows.mjs")
    keys = (
        "insertRemoveRedraw",
        "ringMissManifest",
        "protectedRowsWaitWithNote",
        "protectionCheckedBeforeApply",
        "staleDiscardedFailedRetried",
        "failedRowsAskedAgainBySettle",
        "olderAnswerDiscarded",
        "batchesBoundedAndDeferredAgain",
        "emptyHomeTransitions",
        "jobsRedrawRows",
        "signedOutStops",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_page_reloads_for_a_new_language_or_version_only_when_idle_and_never_loops():
    verdict = _verdict("sync_reload.mjs")
    keys = (
        "idleReloadsAfterDelay",
        "announcedAtOnce",
        "activityCancels",
        "busyPageToldThenReloaded",
        "noReloadLoop",
        "noStorageNoticeOnly",
        "droppedReasonClears",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_follows_header_jobs_undo_preferences_version_and_new_rows():
    verdict = _verdict("sync_page.mjs")
    keys = (
        "headerPatchedTogether",
        "jobsElsewhereShownAndCleaned",
        "undoLiveAndRemoteEligibility",
        "themeFollows",
        "languageNoticeThenIdleReload",
        "rowInsertedAnchorKept",
        "focusKeptFieldProtects",
        "hungRowsGivenUpAndAskedAgain",
        "versionDeferredAndCancelled",
        "hintScopedWithoutData",
        "messagesAppliedOnlyOnceListTaken",
        "signOutStopsPolling",
        "signOutEndsEveryRequestAndSettler",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_home_list_tools_follow_rows_another_device_changed():
    verdict = _verdict("list_refresh.mjs")
    keys = (
        "membershipOrderAndSearch",
        "trackerChoiceAdded",
        "toneCacheFollowsRedraw",
        "filterKeepsProtectedRow",
        "reorderWaitsForTheOwner",
        "localSortKept",
    )
    assert verdict == dict.fromkeys(keys, True)


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sites_settings_and_history_follow_their_fragments_without_overwriting_the_owner():
    verdict = _verdict("page_sync.mjs")
    keys = (
        "lifecycleIdempotent",
        "keyedListInsertRemoveReplace",
        "protectedSectionsWaitWithNote",
        "ownLinesAdopted",
        "historyMergedOncePerEventWithinBounds",
        "pageAreaAppliedFromItsRevision",
        "failedPageAskedAgainBySettle",
    )
    assert verdict == dict.fromkeys(keys, True), verdict


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_settings_section_drawn_again_gets_its_listeners_once_and_loses_the_old_ones():
    verdict = _verdict("page_components.mjs")
    assert verdict == dict.fromkeys(("mountedOnceEach", "remountedAfterReplace", "accordionReadsTheSectionsNow"), True)
