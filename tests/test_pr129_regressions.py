"""Routing contracts agreed during the PR #129 review."""
import asyncio
import time
from unittest.mock import Mock

import pytest

import aliases
import auth_manager
import database as db
import providers
import router
import routing
import server
from providers.protocol import QuotaSnapshot
from providers.xiaohuanxiong import token, store


@pytest.fixture
def model_setup(monkeypatch):
    channels = ["workbuddy", "qoder"]
    models = {"workbuddy": [{"id": "example-model"}], "qoder": [{"id": "example-model-x"}]}
    mappings = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: mappings if key == "model_aliases" else default)
    monkeypatch.setattr(providers, "enabled_provider_ids", lambda: channels)
    import catalog
    monkeypatch.setattr(catalog, "models_for", lambda channel, fallback: models.get(channel, fallback))
    monkeypatch.setattr(routing, "channel_pool", lambda channel: [{"id": channels.index(channel)+1, "provider": channel, "priority": 0}])
    monkeypatch.setattr(auth_manager, "account_is_cooling_down", lambda aid: False)
    return mappings


def test_suffixes_are_distinct_without_mapping(model_setup):
    assert [c for c,a in router.account_queue("example-model", {"default_channel":"*"})] == ["workbuddy"]
    assert [c for c,a in router.account_queue("example-model-x", {"default_channel":"*"})] == ["qoder"]


def test_explicit_mapping_enables_cross_channel_queue(model_setup):
    model_setup["qoder"] = {"example-model":"example-model-x"}
    assert [c for c,a in router.account_queue("example-model", {"default_channel":"*"})] == ["workbuddy", "qoder"]
    assert [c for c,a in router.account_queue("qoder/example-model", {"default_channel":"*"})] == ["qoder"]


def test_channel_qualified_whitelist_restricts_each_hop(model_setup):
    model_setup["qoder"] = {"example-model":"example-model-x"}
    key = {"default_channel":"*", "allowed_models":["workbuddy/example-model"]}
    router.admit_model(key, "example-model")
    assert [c for c,a in router.account_queue("example-model", key)] == ["workbuddy"]
    key["allowed_models"] = ["qoder/example-model-x"]
    router.admit_model(key, "example-model")
    assert [c for c,a in router.account_queue("example-model", key)] == ["qoder"]


@pytest.mark.parametrize("responses_api", [False, True])
def test_first_chunk_exception_tries_next_hop_and_counts_once(monkeypatch, responses_api):
    monkeypatch.setenv("BUDDY_CROSS_CHANNEL_FAILOVER", "1")
    monkeypatch.setattr(router, "_quota_exempt_channels", lambda base: set())
    monkeypatch.setattr(router, "account_queue", lambda *args: [("workbuddy", {"id":1,"provider":"workbuddy"}), ("qoder", {"id":2,"provider":"qoder"})])
    calls, reservations = [], []
    async def broken():
        raise RuntimeError("connection closed before first chunk")
        yield b""
    async def dispatch(bound, payload, info):
        calls.append(bound.channel)
        return ("stream", broken()) if bound.channel == "workbuddy" else ("json", {"id":"chatcmpl-ok", "model":"auto", "choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]})
    async def reserve(queue):
        reservations.append(len(queue))
    monkeypatch.setattr(router, "_chat_after_bind_no_echo", dispatch)
    handler = router.responses_after_name if responses_api else router.chat_after_name
    payload = {"input":"ping"} if responses_api else {"messages":[{"role":"user","content":"ping"}]}
    result = asyncio.run(handler("auto", payload, {"default_channel":"*"}, after_queue=reserve))
    assert result[0] == "json" and calls == ["workbuddy", "qoder"]
    assert reservations == [2]
    assert auth_manager.pinned_for("workbuddy") is None


def test_expired_disk_resync_ttl_allows_rescan(monkeypatch):
    monkeypatch.setattr(token, "_resync_cache", {123: time.time()-600})
    candidates = Mock(return_value=[])
    monkeypatch.setattr(store, "auth_candidates", candidates)
    token.resync_from_disk({"id":123,"access_token":"old"})
    candidates.assert_called_once()
    token.resync_from_disk({"id":123,"access_token":"old"})
    candidates.assert_called_once()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_billing_failure_does_not_expire_login(monkeypatch, status):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs):
            return type("Response", (), {"status_code":status})()
    monkeypatch.setattr(token.httpx, "AsyncClient", lambda **kwargs: Client())
    assert asyncio.run(token.probe_token("token")) is True


def test_weight_overrides_stickiness_without_forced_rotation(monkeypatch):
    rows = [{"id":1,"provider":"workbuddy","priority":0,"weight":1}, {"id":2,"provider":"workbuddy","priority":0,"weight":8}]
    monkeypatch.setattr(db, "get_active_accounts", lambda provider: rows)
    monkeypatch.setattr(routing, "account_available", lambda account: True)
    auth_manager._sticky_account_id["workbuddy"] = 1
    assert auth_manager.pick_account(provider="workbuddy")["id"] == 2
    assert auth_manager.pick_account(provider="workbuddy")["id"] == 2
    rows[0]["priority"] = 10
    assert auth_manager.pick_account(provider="workbuddy")["id"] == 1


def test_manual_balance_refresh_restores_routing_after_checkin(tmp_path, monkeypatch):
    import credential_crypto
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-key")
    credential_crypto.reset_cache()
    db.init_db()
    aid = db.add_account({"name":"q", "uid":"q", "provider":"qoder"})
    account = db.get_account(aid)
    import resource_sync
    resource_sync.store_quota_reading(account, remaining=0)
    assert routing.quota_exhausted(account)
    assert db.get_account(aid)["status"] == "active"
    class Provider:
        async def fetch_quota(self, account):
            return QuotaSnapshot(True, "qoder", aid, "credit", 100)
    monkeypatch.setattr(providers, "get_provider", lambda channel: Provider())
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    asyncio.run(server.admin_account_resources(aid, force=1, authorization=None))
    assert not routing.quota_exhausted(account)
    credential_crypto.reset_cache()
