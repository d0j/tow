import pytest

from tow.bind import (
    is_loopback_bind,
    origin_matches_request,
    resolve_bind,
    validate_bind,
)


def test_bind_defaults_to_loopback_and_accepts_loopback_addresses():
    assert resolve_bind(None) == "127.0.0.1"
    assert is_loopback_bind("127.0.0.1") is True
    assert is_loopback_bind("::1") is True
    assert validate_bind("127.0.0.1") == "127.0.0.1"


def test_non_loopback_bind_requires_explicit_lan_opt_in():
    with pytest.raises(ValueError, match="loopback"):
        validate_bind("0.0.0.0")
    assert validate_bind("192.168.1.10", allow_lan=True) == "192.168.1.10"
    assert validate_bind("0.0.0.0", allow_lan=True) == "0.0.0.0"
    assert validate_bind("::", allow_lan=True) == "0.0.0.0"


@pytest.mark.parametrize("origin", [None, "", "null", "not-an-origin", "http://user:pass@testserver"])
def test_origin_check_rejects_missing_or_malformed_origin(origin):
    assert origin_matches_request(origin, "testserver") is False


@pytest.mark.parametrize(
    "request_host", ["attacker@trusted.example", "trusted.example/evil", "trusted.example?x=1", "trusted.example#x"]
)
def test_origin_check_rejects_malformed_request_authority(request_host):
    assert origin_matches_request("http://trusted.example", request_host) is False


def test_origin_check_requires_same_scheme_host_and_effective_port():
    assert origin_matches_request("http://testserver", "testserver", "http") is True
    assert origin_matches_request("https://testserver", "testserver", "http") is False
    assert origin_matches_request("http://testserver:8080", "testserver", "http") is False
    assert origin_matches_request("http://other.testserver", "testserver", "http") is False
