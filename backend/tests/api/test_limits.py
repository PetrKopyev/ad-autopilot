from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db.models import Session


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    from app.api import limits

    now = [1000.0]
    monkeypatch.setattr(limits, "monotonic", lambda: now[0])
    monkeypatch.setattr(limits.RateLimit, "limits", [])
    return now


def rate_limit(message: str, **options):
    from app.api.limits import RateLimit

    return RateLimit(message, **options)


def request(host: str | None = "203.0.113.7") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def test_window_slides_and_retry_after_counts_down(clock) -> None:
    limit = rate_limit("Подождите.", per_ip=2, seconds=60)
    limit(request(), None)
    clock[0] += 30
    limit(request(), None)
    with pytest.raises(HTTPException) as error:
        limit(request(), None)
    assert error.value.status_code == 429 and error.value.headers["Retry-After"] == "31"
    clock[0] += 30
    limit(request(), None)
    with pytest.raises(HTTPException):
        limit(request(), None)


def test_user_limit_is_separate_from_ip(clock) -> None:
    limit = rate_limit("Подождите.", per_ip=2, per_user=1)
    limit(request("198.51.100.1"), Session(user_id=1))
    with pytest.raises(HTTPException):
        limit(request("198.51.100.2"), Session(user_id=1))
    limit(request("198.51.100.2"), Session(user_id=2))
    limit(request("198.51.100.2"), None)
    with pytest.raises(HTTPException):
        limit(request("198.51.100.2"), None)


def test_requests_without_client_address_share_one_key(clock) -> None:
    limit = rate_limit("Подождите.", per_ip=1)
    limit(request(None), None)
    with pytest.raises(HTTPException):
        limit(request(None), None)


def test_key_count_is_capped_by_evicting_least_recent(clock, monkeypatch) -> None:
    monkeypatch.setattr("app.api.limits.MAX_KEYS", 3)
    limit = rate_limit("Подождите.", per_ip=1)
    for host in ("a", "b", "c"):
        limit(request(host), None)
    with pytest.raises(HTTPException):
        limit(request("a"), None)
    limit(request("d"), None)
    assert list(limit.hits) == ["ip:c", "ip:a", "ip:d"]
    with pytest.raises(HTTPException):
        limit(request("a"), None)


def test_ipv6_clients_are_limited_by_64_network_and_ipv4_by_address(clock) -> None:
    limit = rate_limit("Подождите.", per_ip=1)
    limit(request("2001:db8:1:2::1"), None)
    with pytest.raises(HTTPException):
        limit(request("2001:db8:1:2:ffff::9"), None)
    limit(request("2001:db8:1:3::1"), None)
    limit(request("198.51.100.1"), None)
    limit(request("198.51.100.2"), None)
    with pytest.raises(HTTPException):
        limit(request("::ffff:198.51.100.1"), None)
    assert list(limit.hits) == ["ip:2001:db8:1:2::/64", "ip:2001:db8:1:3::/64", "ip:198.51.100.2", "ip:198.51.100.1"]
