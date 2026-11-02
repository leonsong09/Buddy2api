import asyncio

import httpx
import pytest

import auth_manager as auth


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch):
    monkeypatch.setattr(auth, "backend_url", lambda account: "https://checkin.example")


@pytest.mark.parametrize("claim", [False, True])
@pytest.mark.parametrize("status,body,content_type,expected", [
    (200, "", "application/json", "空响应"),
    (403, "<html>private-token</html>", "text/html", "HTML"),
    (502, "Bad Gateway private-token", "text/plain", "非 JSON"),
    (302, "", "text/html", "重定向"),
    (429, "slow down", "text/plain", "频繁"),
])
def test_malformed_reply_preserves_stage_and_status(monkeypatch, claim, status, body, content_type, expected):
    account = {"id": 1, "name": "test"}
    async def headers(row): return {"Authorization": "Bearer private-token"}
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    monkeypatch.setattr(auth.db, "get_account", lambda aid: account)
    monkeypatch.setattr(auth, "request_timeout", lambda default: default)
    def respond(request):
        if claim and request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(200, json={"code": 0, "data": {"active": True, "today_checked_in": False}})
        return httpx.Response(status, text=body, headers={"Content-Type": content_type})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(auth.db, "upsert_account_checkin_cache", lambda *args: None)
    call = auth.claim_daily_checkin(account) if claim else auth.fetch_checkin_status(account, force=True, allow_stale=False)
    result = asyncio.run(call)
    assert result["ok"] is False
    assert result["status_code"] == status
    assert ("领取积分" if claim else "查询签到状态") in result["message"]
    assert expected in result["message"]
    assert "private-token" not in result["message"]


def test_transport_failure_is_not_a_json_error(monkeypatch):
    async def headers(row): return {"Authorization":"Bearer secret"}
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    monkeypatch.setattr(auth, "request_timeout", lambda default: default)
    def respond(request): raise httpx.ConnectError("private-token https://private.example", request=request)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = asyncio.run(auth.fetch_checkin_status({}, force=True, allow_stale=False))
    assert result["status_code"] == 0
    assert "ConnectError" in result["message"] and "查询签到状态" in result["message"]
    assert "private-token" not in result["message"] and "private.example" not in result["message"]


def test_successful_json_reply_is_unchanged(monkeypatch):
    async def headers(row): return {"Authorization":"Bearer simulated"}
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    monkeypatch.setattr(auth, "request_timeout", lambda default: default)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code":0,"data":{"active":True,"today_checked_in":True,"today_credit":10}}))
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    result = asyncio.run(auth.fetch_checkin_status({}, force=True, allow_stale=False))
    assert result["ok"] and result["already_claimed"] and result["credit"] == 10
