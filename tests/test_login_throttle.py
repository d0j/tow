"""Login throttling: lockout after repeated failures, reset on success."""

import threading

from fastapi.testclient import TestClient
from helpers import flash_of, open_network

from tow.ratelimit import LoginThrottle
from tow.web import app


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_lockout_starts_after_max_failures_and_doubles():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=3, base_delay_sec=30, clock=clock)
    for _ in range(2):
        throttle.failure("192.168.1.9")
    assert throttle.retry_after("192.168.1.9") == 0
    throttle.failure("192.168.1.9")
    assert throttle.retry_after("192.168.1.9") == 30
    throttle.failure("192.168.1.9")
    assert throttle.retry_after("192.168.1.9") == 60
    assert throttle.retry_after("192.168.1.10") == 0


def test_lockout_expires_and_success_resets():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=1, base_delay_sec=30, clock=clock)
    throttle.failure("peer")
    clock.now += 31
    assert throttle.retry_after("peer") == 0
    throttle.failure("peer")
    assert throttle.retry_after("peer") > 0
    throttle.success("peer")
    assert throttle.retry_after("peer") == 0


def test_failures_outside_the_window_do_not_count():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=2, window_sec=600, clock=clock)
    throttle.failure("peer")
    clock.now += 601
    throttle.failure("peer")
    assert throttle.retry_after("peer") == 0


def test_an_attempt_is_reserved_before_the_check_and_given_back_on_success():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=2, base_delay_sec=30, clock=clock)
    assert throttle.attempt("peer") == 0
    assert throttle.attempt("peer") == 0  # two tries in flight: the second one locks
    assert throttle.attempt("peer") == 30
    throttle.success("peer")  # one of them was right
    assert throttle.attempt("peer") == 0


def test_parallel_attempts_cannot_all_pass_the_check():
    throttle = LoginThrottle(max_failures=5)
    start = threading.Barrier(30)
    results: list[int] = []

    def guess():
        start.wait()
        results.append(throttle.attempt("192.168.1.9"))

    threads = [threading.Thread(target=guess) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(0) == 5


def test_many_addresses_share_one_failure_budget():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=5, global_max_failures=10, base_delay_sec=30, clock=clock)
    for n in range(10):
        assert throttle.attempt(f"192.168.1.{n}") == 0  # one guess each: no address is locked by itself
    assert throttle.attempt("192.168.1.200") == 30  # ... but all of them together are
    assert throttle.retry_after("192.168.1.0") == 30
    clock.now += 31
    assert throttle.attempt("192.168.1.200") == 0
    clock.now += 601  # the window passed
    assert throttle.attempt("192.168.1.201") == 0


def test_parallel_wrong_passwords_over_http_get_at_most_the_budget(monkeypatch):
    from tow.web import services

    open_network(monkeypatch, token="t" * 32)
    throttle = services.login_throttle
    reserve = throttle.attempt
    decided = [0]
    changed = threading.Condition()

    def counted_attempt(client):
        try:
            return reserve(client)
        finally:
            with changed:
                decided[0] += 1
                changed.notify_all()

    def slow_wrong(_candidate, _expected):
        # The real check (PBKDF2) takes time: it answers only once the throttle has decided on
        # every guess, so all of them are in flight at once.
        with changed:
            assert changed.wait_for(lambda: decided[0] == 30, timeout=10)
        return False

    monkeypatch.setattr(throttle, "attempt", counted_attempt)
    monkeypatch.setattr("tow.auth.token_matches", slow_wrong)
    start = threading.Barrier(30)
    codes: list[int] = []

    def guess():
        client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
        start.wait()
        codes.append(client.post("/login", data={"token": "wrong"}).status_code)

    threads = [threading.Thread(target=guess) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert codes.count(401) == 5
    assert codes.count(429) == 25


def test_the_current_password_check_in_settings_is_throttled_too(monkeypatch):
    from cryptography.fernet import Fernet

    from tow.auth import issue_session, lan_password_record, lan_password_session_key
    from tow.config import load_config, save_config
    from tow.store import save_secrets

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    record = lan_password_record("right-password")
    save_secrets({"lan_auth": record})
    cfg = load_config()
    cfg["allow_lan"] = True
    save_config(cfg)
    monkeypatch.setattr("tow.web.routes_password.lan_password_matches", lambda *_a: False)
    lan = TestClient(
        app,
        client=("192.168.1.7", 50000),
        headers={"Origin": "http://127.0.0.1"},
        cookies={"tow_session": issue_session(lan_password_session_key(record))},
    )
    flashes = [
        lan.post("/settings/password", data={"current_password": "x", "hint": ""}, follow_redirects=False).headers[
            "location"
        ]
        for _ in range(6)
    ]
    from tow.i18n import t

    assert all(flash_of(flash) == t("web.password.current_wrong", "ru") for flash in flashes[:5])
    assert flash_of(flashes[5]) == t("web.login.too_many", "ru", sec=30)


def test_login_endpoint_returns_429_after_repeated_wrong_passwords(monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    codes = [client.post("/login", data={"token": "wrong"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
