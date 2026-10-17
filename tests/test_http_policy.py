from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from media_manager import http_policy as policy


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(policy, "_cooldowns", {})
    monkeypatch.setattr(policy.time, "sleep", lambda seconds: None)


def test_429_retries_then_succeeds():
    calls = []
    def opener(req, **kwargs):
        calls.append(req)
        if len(calls) < 3:
            raise HTTPError(req.full_url, 429, "limited", {"Retry-After": "0"}, BytesIO())
        return "ok"
    assert policy.open_with_retry(Request("https://example.test"), opener=opener) == "ok"
    assert len(calls) == 3


def test_long_delay_defers_and_blocks_further_requests():
    calls = []
    def opener(req, **kwargs):
        calls.append(req)
        raise HTTPError(req.full_url, 429, "limited", {"Retry-After": "3600"}, BytesIO())
    for _ in range(2):
        with pytest.raises(policy.ProviderDeferred):
            policy.open_with_retry(Request("https://example.test"), opener=opener)
    assert len(calls) == 1


def test_post_server_failure_not_replayed():
    calls = []
    def opener(req, **kwargs):
        calls.append(req)
        raise HTTPError(req.full_url, 503, "error", {}, BytesIO())
    with pytest.raises(HTTPError):
        policy.open_with_retry(Request("https://example.test", data=b"{}"), opener=opener)
    assert len(calls) == 1


def test_auth_failure_not_retried():
    def opener(req, **kwargs):
        raise HTTPError(req.full_url, 401, "unauthorized", {}, BytesIO())
    with pytest.raises(HTTPError):
        policy.open_with_retry(Request("https://example.test"), opener=opener)


def test_http_date_retry_after(monkeypatch):
    monkeypatch.setattr(policy.time, "time", lambda: 0)
    assert policy.retry_delay({"Retry-After": "Thu, 01 Jan 1970 00:01:00 GMT"}, 2) == 60
