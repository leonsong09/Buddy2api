import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import credential_crypto
import database as db
from providers.xiaohuanxiong.headers import client_headers
from providers.xiaohuanxiong.models import parse_supplier_models
from providers.xiaohuanxiong.quota import _today_points, parse_balance, parse_bills
from providers.xiaohuanxiong import store
from providers.xiaohuanxiong.store import auth_candidates, session_to_account


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_auth_and_request_headers():
    parsed = session_to_account(
        {"access_token": "access-1", "refresh_token": "refresh-1", "user_id": "42", "name": "raccoon"}
    )
    assert parsed["provider"] == "xiaohuanxiong"
    assert parsed["uid"] == "42"
    headers = client_headers("access-1", json_body=True)
    assert headers["Authorization"] == "Bearer access-1"
    assert headers["X-Client-Platform"] == "desktop-windows"
    assert headers["X-Client-Version"] == "v1.0.0"
    assert headers["X-Raccoon-Language"] == "zh"
    assert headers["User-Agent"]


def test_catalog_and_balance_parse():
    models = parse_supplier_models(
        {
            "data": {
                "categories": [
                    {
                        "models": [
                            {"name": "sn-glm-5-3-flash", "context_window": 128000, "visible": True},
                            {"name": "hidden", "visible": False},
                        ]
                    }
                ]
            }
        }
    )
    assert [item["id"] for item in models] == ["sn-glm-5-3-flash"]
    assert models[0]["context_window"] == 128000
    balance = parse_balance({"code": 0, "data": {"available_points": 6000, "daily_points": 300}})
    assert balance["available"] == 6000
    assert balance["daily"] == 300


def test_today_bill_counts_as_claimed():
    bills = parse_bills(
        {"data": [{"points": 300, "created_at": "2026-10-15T01:02:03+08:00", "event_name": "daily"}]}
    )
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _today_points(bills, now=now) == 300


def test_claim_reports_existing_daily_bill(isolated_db, monkeypatch):
    from providers.xiaohuanxiong import quota

    account_id = db.add_account(
        {
            "name": "xhx",
            "uid": "42",
            "provider": "xiaohuanxiong",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)

    async def fake_refresh(row):
        return row

    async def fake_get(url, token):
        assert token == "access"
        if url.endswith("/setting_info"):
            return 200, {"code": 0, "data": {}}
        return 200, {"data": [{"points": 20, "created_at": datetime.now().astimezone().isoformat(), "event_name": "daily"}]}

    monkeypatch.setattr(quota, "ensure_ready", fake_refresh)
    monkeypatch.setattr(quota, "_get", fake_get)
    result = asyncio.run(quota.claim_checkin(account))
    assert result["ok"] is True
    assert result["already_claimed"] is True
    assert result["credit"] == 20


def _jwt(sub: str, exp: int = 1795278853) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub, "exp": exp, "iss": "xiaohuanxiong.com/iam"}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def test_session_to_account_falls_back_to_jwt_sub():
    """The desktop auth.json only has access_token and office_identity. The JWT `sub`
    is parsed into the uid so each account keeps a stable, distinct identity."""
    token = _jwt("pu-01a16474-fead-79f5-be91-db8d7bf0b5a6")
    parsed = session_to_account({"access_token": token, "office_identity": "personal"})
    assert parsed["uid"] == "pu-01a16474-fead-79f5-be91-db8d7bf0b5a6"
    assert parsed["name"] == "小浣熊-b5a6"
    assert parsed["access_token"] == token
    assert parsed["expires_at"] == 1795278853000


def test_session_to_account_explicit_uid_wins_over_jwt_sub():
    token = _jwt("pu-from-jwt")
    parsed = session_to_account({"access_token": token, "user_id": "42"})
    assert parsed["uid"] == "42"


def test_session_to_account_without_jwt_keeps_distinct_default_name():
    """When neither a user_id nor a parseable JWT `sub` is available the uid falls back
    to a sha256 digest of the token material, so distinct accounts still stay distinct."""
    parsed = session_to_account({"access_token": "not-a-jwt", "refresh_token": "r"})
    assert parsed["uid"].startswith("tok-")
    assert parsed["uid"] != "小浣熊"


def test_auth_candidates_includes_office_raccoon_agent_dir(monkeypatch, tmp_path):
    """The SenseTime desktop agent writes ~/.office-raccoon/agent/config/auth.json,
    not the .box-agent path the code used to probe."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CB_XHX_AUTH_DIR", raising=False)
    monkeypatch.delenv("CB_XIAOHUANXIONG_AUTH_DIR", raising=False)
    paths = [str(p) for p in auth_candidates()]
    assert any(".office-raccoon" in p and p.endswith("auth.json") for p in paths)
    # the historical probe paths stay, so already-configured installs keep working
    assert any(".box-agent" in p for p in paths)
    # the new candidate must resolve under the patched home, not the real one
    assert any(str(home) in p and ".office-raccoon" in p for p in paths)


def test_resync_from_disk_adopts_rotated_token(isolated_db, monkeypatch, tmp_path):
    """The desktop client exposes no refresh_token, so a re-login there rotates the
    access token and the DB copy goes stale (401). resync_from_disk must adopt the
    new token from the client's auth.json instead of failing."""
    from providers.xiaohuanxiong import store as xhx_store
    from providers.xiaohuanxiong import token as xhx_token

    auth_dir = tmp_path / ".office-raccoon" / "agent" / "config"
    auth_dir.mkdir(parents=True)
    monkeypatch.setattr(xhx_store, "auth_candidates", lambda: [auth_dir / "auth.json"])

    stale = _jwt("pu-rotated")
    fresh = _jwt("pu-rotated", exp=1795365253)
    (auth_dir / "auth.json").write_text(
        json.dumps({"access_token": fresh, "office_identity": "personal"}), encoding="utf-8"
    )
    account_id = db.add_account(
        {
            "name": "pu-rotated",
            "uid": "pu-rotated",
            "provider": "xiaohuanxiong",
            "access_token": stale,
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)

    patched = xhx_token.resync_from_disk(account)
    assert patched is not None
    assert patched["access_token"] == fresh
    assert db.get_account(account_id)["access_token"] == fresh

    # A second call within the TTL window must not re-read the disk.
    def _boom():
        raise AssertionError("should not re-read")

    monkeypatch.setattr(xhx_store, "auth_candidates", _boom)
    assert xhx_token.resync_from_disk(account) is None


def test_resync_from_disk_skips_other_accounts(isolated_db, monkeypatch, tmp_path):
    """auth.json only ever holds one account, so a resync must never steal a token
    belonging to a different uid."""
    from providers.xiaohuanxiong import store as xhx_store
    from providers.xiaohuanxiong import token as xhx_token

    auth_dir = tmp_path / ".office-raccoon" / "agent" / "config"
    auth_dir.mkdir(parents=True)
    monkeypatch.setattr(xhx_store, "auth_candidates", lambda: [auth_dir / "auth.json"])
    (auth_dir / "auth.json").write_text(
        json.dumps({"access_token": _jwt("pu-someone-else"), "office_identity": "personal"}),
        encoding="utf-8",
    )
    account_id = db.add_account(
        {
            "name": "mine",
            "uid": "pu-mine",
            "provider": "xiaohuanxiong",
            "access_token": _jwt("pu-mine"),
            "expires_at": 9_999_999_999_999,
        }
    )
    assert xhx_token.resync_from_disk(db.get_account(account_id)) is None


def test_refresh_account_without_refresh_token_hits_upstream(isolated_db, monkeypatch):
    """The desktop client never writes a refresh_token. Renewing such an account used
    to return the account untouched whenever exp had not passed, so 续期 reported
    success while every API call kept failing with 401. It must really call the
    upstream instead."""
    from providers.xiaohuanxiong import token as xhx_token

    calls: list[str] = []

    async def fake_exchange(account, refresh):
        calls.append(refresh)
        return {**account, "access_token": "rotated"}

    monkeypatch.setattr(xhx_token, "_exchange", fake_exchange)
    async def alive_probe(token):
        return True

    monkeypatch.setattr(xhx_token, "probe_token", alive_probe)

    account = {
        "id": 1,
        "uid": "pu-1",
        "access_token": "old",
        "refresh_token": "",
        "expires_at": 9_999_999_999_999,
    }
    refreshed = asyncio.run(xhx_token.refresh_account(account))
    assert refreshed["access_token"] == "rotated"
    # the access token itself is used as the refresh credential
    assert calls == ["old"]


def test_refresh_account_reports_failure_when_upstream_rejects(isolated_db, monkeypatch):
    """A revoked session must surface as a failed 续期, never as a silent success."""
    from providers.xiaohuanxiong import token as xhx_token

    async def failing_exchange(account, refresh):
        raise xhx_token.XiaohuanxiongAuthError("refresh failed: HTTP 401")

    monkeypatch.setattr(xhx_token, "_exchange", failing_exchange)
    async def alive_probe(token):
        return True

    monkeypatch.setattr(xhx_token, "probe_token", alive_probe)

    account_id = db.add_account(
        {
            "name": "pu-2",
            "uid": "pu-2",
            "provider": "xiaohuanxiong",
            "access_token": "dead",
            "refresh_token": "",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    with pytest.raises(xhx_token.XiaohuanxiongAuthError):
        asyncio.run(xhx_token.refresh_account(account))
    # a dead session must show up as expired in the admin page, not 正常
    assert db.get_account(account_id)["status"] == "expired"


def test_ensure_ready_probes_token_before_trusting_exp(isolated_db, monkeypatch):
    """A token whose exp has not passed can still be revoked server-side. ensure_ready
    must probe it instead of returning it as-is."""
    from providers.xiaohuanxiong import token as xhx_token

    probed: list[str] = []

    async def fake_probe(token):
        probed.append(token)
        return False

    monkeypatch.setattr(xhx_token, "probe_token", fake_probe)
    monkeypatch.setattr(
        xhx_token, "resync_from_disk", lambda account: {**account, "access_token": "from-disk"}
    )


    account = {"id": 3, "uid": "pu-3", "access_token": "revoked", "refresh_token": "", "expires_at": 9999999999999}
    ready = asyncio.run(xhx_token.ensure_ready(account))
    assert probed == ["revoked"]
    assert ready["access_token"] == "from-disk"


def test_probe_token_treats_http_errors_as_dead(monkeypatch):
    from providers.xiaohuanxiong import token as xhx_token

    class FakeResponse:
        status_code = 401

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return FakeResponse()

    monkeypatch.setattr(xhx_token.httpx, "AsyncClient", lambda *a, **kw: FakeClient())


    assert asyncio.run(xhx_token.probe_token("dead")) is False


def test_probe_token_tolerates_network_blips(monkeypatch):
    """A transient network error must not be reported as a dead login."""
    from providers.xiaohuanxiong import token as xhx_token

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            raise xhx_token.httpx.HTTPError("boom")

    monkeypatch.setattr(xhx_token.httpx, "AsyncClient", lambda *a, **kw: FakeClient())


    assert asyncio.run(xhx_token.probe_token("anything")) is True


def test_ensure_ready_prefers_refresh_token(isolated_db, monkeypatch):
    """A refresh_token, when the user did supply one, still takes priority over the
    on-disk resync."""
    from providers.xiaohuanxiong import token as xhx_token

    calls: list[str] = []

    async def fake_refresh(account):
        calls.append("refresh")
        return {**account, "access_token": "refreshed"}

    def boom_resync(account):
        calls.append("resync")
        return None

    monkeypatch.setattr(xhx_token, "refresh_account", fake_refresh)
    monkeypatch.setattr(xhx_token, "resync_from_disk", boom_resync)

    account = {
        "id": 1,
        "uid": "u",
        "access_token": "stale",
        "refresh_token": "r",
        "expires_at": 1,
    }
    ready = asyncio.run(xhx_token.ensure_ready(account))
    assert ready["access_token"] == "refreshed"
    assert calls == ["refresh"]


def test_ensure_ready_falls_back_to_disk_resync(isolated_db, monkeypatch):
    from providers.xiaohuanxiong import token as xhx_token

    async def broken_refresh(account):
        raise xhx_token.XiaohuanxiongAuthError("no refresh token")

    monkeypatch.setattr(xhx_token, "refresh_account", broken_refresh)
    monkeypatch.setattr(
        xhx_token, "resync_from_disk", lambda account: {**account, "access_token": "from-disk"}
    )

    account = {"id": 2, "uid": "u", "access_token": "stale", "refresh_token": "", "expires_at": 1}
    ready = asyncio.run(xhx_token.ensure_ready(account))
    assert ready["access_token"] == "from-disk"


def test_ensure_ready_raises_when_nothing_available(isolated_db, monkeypatch):
    from providers.xiaohuanxiong import token as xhx_token

    async def broken_refresh(account):
        raise xhx_token.XiaohuanxiongAuthError("no refresh token")

    monkeypatch.setattr(xhx_token, "refresh_account", broken_refresh)
    monkeypatch.setattr(xhx_token, "resync_from_disk", lambda account: None)

    account = {"id": 3, "uid": "u", "access_token": "stale", "refresh_token": "", "expires_at": 1}
    with pytest.raises(xhx_token.XiaohuanxiongAuthError):
        asyncio.run(xhx_token.ensure_ready(account))
class _Response:
    def __init__(self, status, body="", lines=None, payload=None):
        self.status_code = status
        self.text = body
        self._body = body.encode("utf-8")
        self._lines = lines or []
        self._payload = payload

    async def aread(self):
        return self._body

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _patch_upstream(monkeypatch, responses):
    import httpx

    import auth_manager
    from providers.xiaohuanxiong import chat

    queue = list(responses)
    monkeypatch.setattr(db, "list_accounts", lambda **kwargs: [{"id": 9, "status": "active"}])
    logs = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def _next(self):
            return queue.pop(0)

        async def post(self, *_args, **_kwargs):
            return self._next()

        def stream(self, *_args, **_kwargs):
            return self._next()

    async def ready(account):
        return account

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(chat, "_ready", ready)
    monkeypatch.setattr(
        auth_manager,
        "pick_account",
        lambda excluded, provider="": {"id": 9, "name": "raccoon", "access_token": "token"},
    )
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "record_request", lambda data: logs.append(dict(data)))
    return chat, logs


def _brief(logs):
    return [
        (
            item["status_code"],
            item["finish_reason"],
            item["account_id"],
            item["increment_usage"],
            item["total_tokens"],
            item["error_msg"],
        )
        for item in logs
    ]


def _run(chat, stream):
    payload = {"model": "auto", "messages": [{"role": "user", "content": "ping"}], "stream": stream}
    key = {"id": 4, "name": "Key"}
    if not stream:
        return asyncio.run(chat.chat_completions(payload, key))

    kind, generator = asyncio.run(chat.chat_completions(payload, key))

    async def collect():
        return [chunk async for chunk in generator]

    return kind, asyncio.run(collect())


def test_stream_records_final_usage_and_forwards_events(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"pong"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}',
        "data: [DONE]",
    ]
    chat, logs = _patch_upstream(monkeypatch, [_Response(200, lines=lines)])
    kind, chunks = _run(chat, True)
    forwarded = b"".join(chunks).decode("utf-8")
    assert kind == "stream"
    assert "total_tokens" in forwarded
    assert "pong" in forwarded
    assert _brief(logs) == [(200, "stop", 9, True, 4, "")]


def test_stream_without_usage_keeps_zero_tokens(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(200, lines=['data: {"choices":[{"delta":{"content":"pong"}}]}']),
    ])
    kind, _chunks = _run(chat, True)
    assert kind == "stream"
    assert _brief(logs) == [(200, "stop", 9, True, 0, "")]


def test_stream_retries_do_not_count_until_the_final_failure(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(502, "bad gateway"),
        _Response(502, "bad gateway"),
        _Response(502, "bad gateway"),
    ])
    kind, chunks = _run(chat, True)
    assert kind == "stream"
    assert len(chunks) == 1
    assert b"bad gateway" in chunks[0]
    assert _brief(logs) == [
        (502, "retry", 9, False, 0, "bad gateway"),
        (502, "retry", 9, False, 0, "bad gateway"),
        (502, "error", 9, True, 0, "bad gateway"),
    ]


def test_nonstream_retry_keeps_raw_error_then_records_success(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(502, "bad gateway"),
        _Response(200, payload={"choices": [{"finish_reason": "stop"}], "usage": {"total_tokens": 7}}),
    ])
    kind, data = _run(chat, False)
    assert kind == "json"
    assert data["usage"]["total_tokens"] == 7
    assert _brief(logs) == [
        (502, "retry", 9, False, 0, "bad gateway"),
        (200, "stop", 9, True, 7, ""),
    ]


def test_login_without_user_id_uses_token_subject():
    parsed = session_to_account(
        {
            "access_token": _jwt("user-a"),
            "refresh_token": "refresh-a",
            "office_identity": "org-1",
        }
    )
    assert parsed["uid"] == "user-a"
    assert parsed["name"] == "小浣熊-er-a"
    assert parsed["extra"]["office_identity"] == "org-1"


def test_explicit_user_id_wins_over_token_subject():
    parsed = session_to_account(
        {"access_token": _jwt("user-a"), "refresh_token": "refresh-a", "user_id": "42"}
    )
    assert parsed["uid"] == "42"


def test_second_login_does_not_replace_first(isolated_db):
    first = store.upsert_account(
        session_to_account({"access_token": _jwt("user-a"), "refresh_token": "refresh-a"})
    )
    second = store.upsert_account(
        session_to_account({"access_token": _jwt("user-b"), "refresh_token": "refresh-b"})
    )
    assert first["updated"] is False
    assert second["updated"] is False
    assert first["id"] != second["id"]
    rows = {row["uid"]: row for row in db.list_accounts(provider="xiaohuanxiong")}
    assert rows["user-a"]["refresh_token"] == "refresh-a"
    assert rows["user-b"]["refresh_token"] == "refresh-b"


def test_same_subject_updates_and_legacy_generic_uid_is_adopted(isolated_db):
    account_id = db.add_account(
        {
            "name": "小浣熊",
            "nickname": "工作号",
            "uid": "小浣熊",
            "provider": "xiaohuanxiong",
            "access_token": _jwt("user-a"),
            "refresh_token": "old-refresh",
            "expires_at": 1,
        }
    )
    result = store.upsert_account(
        session_to_account({"access_token": _jwt("user-a"), "refresh_token": "new-refresh"})
    )
    assert result == {"id": account_id, "updated": True}
    row = db.get_account(account_id)
    assert row["uid"] == "user-a"
    assert row["refresh_token"] == "new-refresh"
    assert row["nickname"] == "工作号"
    other = store.upsert_account(
        session_to_account({"access_token": _jwt("user-b"), "refresh_token": "refresh-b"})
    )
    assert other["updated"] is False
    assert db.get_account(account_id)["refresh_token"] == "new-refresh"


def test_new_client_auth_path_is_discovered(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(store.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CB_XHX_AUTH_DIR", raising=False)
    monkeypatch.delenv("CB_XIAOHUANXIONG_AUTH_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    auth = tmp_path / ".office-raccoon" / "agent" / "config" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"access_token": _jwt("user-a"), "refresh_token": "refresh-a"}),
        encoding="utf-8",
    )
    found = store.discover()
    paths = [item["path"] for item in found["files"]]
    assert str(auth) in paths
    assert found["valid_count"] == 1
    parsed = store.import_discovered(str(auth))
    assert parsed["uid"] == "user-a"
    assert parsed["extra"]["auth_path"] == str(auth.resolve())
