"""Admission and the routing queue, the two halves of the old binder.

The binder is gone, split into one name-only gate and one queue:

* ``router.admit_model(key, name)`` judges the *name* and hands back the bare
  inner id, or rejects with 400. It never looks at accounts, so a model stays
  admitted even when every pool is empty. There is no 403 any more — that code
  was the old channel-mismatch rejection and nothing answers with it now.
* ``router.account_queue(name, key)`` is the ordered list of accounts a request
  would spend, one flat cross-channel order (priority, channel order, id),
  narrowed by the name's prefix and by the key's channel.

``router.chat_after_name`` / ``router.responses_after_name`` walk that queue.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import providers
import router
import routing
import server


class _QwenStub:
    id = "qwenwork"
    display_name = "QwenWork"
    checkin_supported = False

    def __init__(self):
        self.last_payload = None
        self.last_api_key_info = None

    def list_models(self):
        return [{"id": "auto"}, {"id": "qwork-advanced"}]

    def alias_map(self):
        return {}

    def accepts_model(self, inner):
        return inner in {"auto", "qwork-advanced"}

    def translate_model(self, model):
        return model

    def pick_account(self, exclude_ids=None):
        return None

    async def pick_account_with_fallback(self, exclude_ids=None):
        return None

    async def has_usable_account(self):
        return False

    async def chat_completions(self, payload, api_key_info):
        self.last_payload = payload
        self.last_api_key_info = api_key_info
        return (
            "json",
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


@pytest.fixture()
def qwen_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    stub = _QwenStub()
    previous = providers._LOADED.get("qwenwork")
    providers.register_provider(stub)
    yield stub
    if previous is not None:
        providers._LOADED["qwenwork"] = previous
    else:
        providers._LOADED.pop("qwenwork", None)


def _pool(monkeypatch, **pools):
    """Give named channels a literal pool, so a queue never reads the real table."""
    table = {channel: [dict(row) for row in rows] for channel, rows in pools.items()}

    def fake_pool(channel):
        return [dict(row) for row in table.get(channel, [])]

    monkeypatch.setattr(routing, "channel_pool", fake_pool)
    return table


def _account(channel, account_id, priority=0, **extra):
    row = {
        "id": account_id,
        "provider": channel,
        "status": "active",
        "priority": priority,
    }
    row.update(extra)
    return row


def _channels(name=None, key=None):
    """The distinct channels of the queue, in the order the walk would try them."""
    seen = []
    for channel, _account_row in router.account_queue(name, key):
        if channel not in seen:
            seen.append(channel)
    return seen


def _rejected(key, name) -> dict:
    """Run admission and return its error body; a 403 here is a contract break."""
    with pytest.raises(HTTPException) as err:
        router.admit_model(key, name)
    error = err.value
    assert error.status_code == 400, error.detail
    assert error.status_code != 403, "admission must never answer 403 again"
    return error.detail["error"]


# --------------------------------------------------------------- admission


def test_unprefixed_auto_on_workbuddy_key():
    assert router.admit_model({"default_channel": "workbuddy"}, "auto") == "auto"


def test_namespaced_workbuddy_strips_inner():
    assert router.admit_model({"default_channel": "workbuddy"}, "workbuddy/glm-5.2") == "glm-5.2"


def test_workbuddy_gpt_alias_accepted():
    assert router.admit_model({"default_channel": "workbuddy"}, "workbuddy/gpt-5.5") == "gpt-5.5"


def test_bare_qwork_advanced_on_workbuddy_key_rejected():
    error = _rejected({"default_channel": "workbuddy"}, "qwork-advanced")
    assert error["code"] == "unknown_model"
    assert error["type"] == "invalid_request_error"


def test_any_channel_prefix_selects_that_channel(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    assert router.admit_model({"default_channel": "*"}, "qwenwork/qwork-advanced") == "qwork-advanced"
    assert _channels("qwenwork/qwork-advanced", {"default_channel": "*"}) == ["qwenwork"]


def test_any_channel_auto_uses_first_channel_with_an_account(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    assert router.admit_model({"default_channel": "*"}, "auto") == "auto"
    assert _channels("auto", {"default_channel": "*"}) == ["qwenwork"]


def test_any_channel_auto_stays_on_first_active_account_even_if_later_channels_exist(
    qwen_enabled, monkeypatch
):
    _pool(
        monkeypatch,
        workbuddy=[_account("workbuddy", 1)],
        qwenwork=[_account("qwenwork", 2)],
    )
    # An equal priority leaves the configured channel order to decide, and
    # workbuddy comes first there.
    assert _channels("auto", {"default_channel": "*"})[0] == "workbuddy"


def test_any_channel_bare_model_uses_the_channel_that_accepts_it(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    assert _channels("qwork-advanced", {"default_channel": "*"}) == ["qwenwork"]


def test_any_channel_unknown_bare_model_is_rejected(qwen_enabled):
    error = _rejected({"default_channel": "*"}, "not-a-real-model")
    assert error["code"] == "unknown_model"


def test_specific_key_still_rejects_another_channel_prefix(qwen_enabled):
    error = _rejected({"default_channel": "workbuddy"}, "qwenwork/qwork-advanced")
    assert error["code"] == "model_not_allowed"


def test_prefix_mismatch_is_400_not_403(qwen_enabled):
    """The old binder answered 403 here; the gate answers 400 model_not_allowed."""
    error = _rejected({"default_channel": "qwenwork"}, "workbuddy/auto")
    assert error["code"] == "model_not_allowed"
    assert "not available for this API key" in error["message"]


def test_every_rejection_code_is_a_400_and_never_a_403(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    cases = [
        ({"default_channel": "workbuddy"}, "workbuddy/", "invalid_model"),
        ({"default_channel": "workbuddy"}, 123, "invalid_model"),
        ({"default_channel": "workbuddy"}, "qclaw/default", "channel_retired"),
        # traework is a known channel id that this run does not enable.
        ({"default_channel": "workbuddy"}, "traework/auto", "unknown_channel"),
        ({"default_channel": "workbuddy"}, "qwenwork/qwork-advanced", "model_not_allowed"),
        ({"default_channel": "workbuddy"}, "not-a-real-model", "unknown_model"),
    ]
    for key, name, code in cases:
        error = _rejected(key, name)
        assert error["code"] == code, (name, error)
        assert error["type"] == "invalid_request_error"
        assert sorted(error) == ["code", "message", "type"]


def test_allowed_models_whitelist_accepts_a_translation_of_the_inner_id(monkeypatch):
    # The whitelist is matched against every legal spelling of the request, so a
    # key that stores the channel's translated id is not rejected.
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    key = {"default_channel": "qwenwork", "allowed_models": ["qwork-advanced"]}
    assert router.admit_model(key, "qwenwork/qwork-advanced") == "qwork-advanced"
    assert router.admit_model({"default_channel": "qwenwork", "allowed_models": ["qwenwork/qwork-advanced"]}, "qwenwork/qwork-advanced") == "qwork-advanced"
    error = _rejected({"default_channel": "qwenwork", "allowed_models": ["glm-5.2"]}, "qwenwork/qwork-advanced")
    assert error["code"] == "model_not_allowed"


def test_unprefixed_auto_on_qwenwork_key(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    assert router.admit_model({"default_channel": "qwenwork"}, "auto") == "auto"
    assert _channels("auto", {"default_channel": "qwenwork"}) == ["qwenwork"]


def test_unprefixed_auto_on_disabled_qwenwork_key(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    # The pin, not the model, is the reason: the key's own channel is switched
    # off, and that is what the caller has to fix. (Same answer the pre-refactor
    # binder gave for this exact case: auto + qwenwork key + qwenwork off.)
    error = _rejected({"default_channel": "qwenwork"}, "auto")
    assert error["code"] == "unknown_channel"


def test_glm_on_qwenwork_key_rejected(qwen_enabled):
    error = _rejected({"default_channel": "qwenwork"}, "glm-5.2")
    assert error["code"] == "unknown_model"


def test_qwenwork_flag_off_namespaced(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    error = _rejected({"default_channel": "qwenwork"}, "qwenwork/qwork-advanced")
    assert error["code"] == "unknown_channel"
    assert "Unknown or disabled channel 'qwenwork'" in error["message"]


def test_qwenwork_namespaced_ok(qwen_enabled):
    assert router.admit_model({"default_channel": "qwenwork"}, "qwenwork/qwork-advanced") == "qwork-advanced"


def test_qwenwork_glm_inner_invalid(qwen_enabled):
    error = _rejected({"default_channel": "qwenwork"}, "qwenwork/glm-5.2")
    assert error["code"] == "unknown_model"
    assert error["message"] == "Unknown model 'qwenwork/glm-5.2'"


# ------------------------------------------------------------------- queue


def test_queue_orders_by_account_priority_across_channels(monkeypatch):
    """One flat order: the best account wins, not the channel's best account."""
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,loomy")
    _pool(
        monkeypatch,
        workbuddy=[_account("workbuddy", 1, priority=0), _account("workbuddy", 2, priority=0)],
        loomy=[_account("loomy", 3, priority=9)],
    )
    assert router.admit_model({"default_channel": "*"}, "deepseek-v4.1-flash-1017") == "deepseek-v4.1-flash-1017"
    # loomy's single account outranks both workbuddy accounts, so the walk enters
    # loomy first even though workbuddy leads the configured channel order.
    assert [channel for channel, _row in router.account_queue("deepseek-v4.1-flash-1017", {"default_channel": "*"})] == [
        "loomy",
        "workbuddy",
        "workbuddy",
    ]


def test_a_leftover_factor_setting_cannot_move_the_queue(qwen_enabled, monkeypatch):
    """The switches are gone; their settings row must be dead data."""
    _pool(
        monkeypatch,
        workbuddy=[_account("workbuddy", 1, priority=0)],
        qwenwork=[_account("qwenwork", 2, priority=9)],
    )
    # Priority decides: qwenwork's 9 beats workbuddy's 0.
    assert _channels("auto", {"default_channel": "*"})[0] == "qwenwork"
    db.set_setting("routing_factors", {"priority": False, "channel_order": False})
    assert _channels("auto", {"default_channel": "*"})[0] == "qwenwork"


def test_queue_is_empty_without_a_pool(qwen_enabled, monkeypatch):
    _pool(monkeypatch)
    assert router.account_queue("qwenwork/qwork-advanced", {"default_channel": "qwenwork"}) == []
    assert router.account_queue(None, None) == []


def test_queue_keeps_the_whole_pool_when_no_model_is_named(qwen_enabled, monkeypatch):
    _pool(
        monkeypatch,
        workbuddy=[_account("workbuddy", 1)],
        qwenwork=[_account("qwenwork", 2)],
    )
    # name=None is the admin page's view: every channel, no model filter.
    assert sorted(_channels(None, None)) == ["qwenwork", "workbuddy"]


def test_a_bound_key_never_leaves_its_channel(qwen_enabled, monkeypatch):
    _pool(
        monkeypatch,
        workbuddy=[_account("workbuddy", 1)],
        qwenwork=[_account("qwenwork", 2)],
    )
    assert _channels("auto", {"default_channel": "qwenwork"}) == ["qwenwork"]
    # "*" is the only value that unlocks the whole pool.
    assert _channels("auto", {"default_channel": "*"}) == ["workbuddy", "qwenwork"]


# --------------------------------------------------------------- dispatch


def test_empty_queue_is_503_channel_unavailable(qwen_enabled, monkeypatch):
    _pool(monkeypatch)
    result = asyncio.run(
        router.chat_after_name(
            "qwenwork/qwork-advanced",
            {"model": "qwenwork/qwork-advanced", "messages": []},
            {"default_channel": "qwenwork"},
        )
    )
    assert result[0] == "error"
    status, body = result[1]
    assert status == 503
    assert body["error"]["code"] == "channel_unavailable"
    assert qwen_enabled.last_payload is None


def test_after_queue_is_awaited_once_only_when_the_queue_is_not_empty(
    qwen_enabled, monkeypatch
):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    seen = []

    async def after_queue(queue):
        seen.append(queue)

    result = asyncio.run(
        router.chat_after_name(
            "qwenwork/qwork-advanced",
            {"model": "qwenwork/qwork-advanced", "messages": []},
            {"default_channel": "qwenwork"},
            after_queue=after_queue,
        )
    )
    assert result[0] == "json"
    assert len(seen) == 1
    assert [(channel, row["id"]) for channel, row in seen[0]] == [("qwenwork", 11)]

    # An empty queue is never counted against the key's daily quota.
    _pool(monkeypatch)
    empty_seen = []

    async def empty_after(queue):
        empty_seen.append(queue)

    result = asyncio.run(
        router.chat_after_name(
            "qwenwork/qwork-advanced",
            {"model": "qwenwork/qwork-advanced", "messages": []},
            {"default_channel": "qwenwork"},
            after_queue=empty_after,
        )
    )
    assert result[0] == "error"
    assert empty_seen == []


def test_responses_after_name_dispatches_to_selected_provider(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])

    async def fail_workbuddy(*_args, **_kwargs):
        raise AssertionError("Responses bypassed the selected provider")

    monkeypatch.setattr("proxy.proxy_chat_completions", fail_workbuddy)

    result = asyncio.run(router.responses_after_name(
        "qwenwork/qwork-advanced",
        {
            "model": "qwenwork/qwork-advanced",
            "input": "hello",
            "reasoning": {"effort": "low"},
        },
        {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
    ))

    assert result[0] == "json"
    assert result[1]["model"] == "qwenwork/qwork-advanced"
    assert result[1]["output"][0]["content"][0]["text"] == "ok"
    assert qwen_enabled.last_payload["model"] == "qwork-advanced"
    assert qwen_enabled.last_payload["reasoning_effort"] == "low"
    assert qwen_enabled.last_api_key_info["_bind_channel"] == "qwenwork"


def test_responses_after_name_rewrites_nested_stream_model(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])

    async def stream_response(payload, api_key_info):
        qwen_enabled.last_payload = payload
        qwen_enabled.last_api_key_info = api_key_info

        async def chunks():
            yield (
                'data: {"id":"chatcmpl-stub","model":"qwork-advanced",'
                '"choices":[{"index":0,"delta":{"content":"ok"},'
                '"finish_reason":null}]}\n\n'
            ).encode()
            yield (
                'data: {"id":"chatcmpl-stub","model":"qwork-advanced",'
                '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            yield b"data: [DONE]\n\n"

        return ("stream", chunks())

    monkeypatch.setattr(qwen_enabled, "chat_completions", stream_response)
    original = "qwenwork/qwork-advanced"

    async def collect_events():
        result = await router.responses_after_name(
            original,
            {"model": original, "input": "hello", "stream": True},
            {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
        )
        assert result[0] == "stream"
        return [chunk async for chunk in result[1]]

    chunks = asyncio.run(collect_events())
    events = [
        json.loads(line[6:])
        for chunk in chunks
        for line in (
            chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        ).splitlines()
        if line.startswith("data: {")
    ]
    snapshots = [event["response"] for event in events if "response" in event]

    assert snapshots
    assert all(snapshot["model"] == original for snapshot in snapshots)
    assert all(event.get("model", original) == original for event in events)


def test_responses_after_name_preserves_provider_error(qwen_enabled, monkeypatch):
    _pool(monkeypatch, qwenwork=[_account("qwenwork", 11)])
    expected = (429, {"error": {"message": "busy", "type": "rate_limit_error"}})

    async def fail_response(_payload, _api_key_info):
        return ("error", expected)

    monkeypatch.setattr(qwen_enabled, "chat_completions", fail_response)
    original = "qwenwork/qwork-advanced"

    result = asyncio.run(router.responses_after_name(
        original,
        {"model": original, "input": "hello"},
        {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
    ))

    assert result == ("error", expected)


class _FailChanStub:
    id = "xcfail"
    display_name = "XCFail"

    def __init__(self):
        self.calls = 0

    def list_models(self):
        return [{"id": "auto"}]

    def alias_map(self):
        return {}

    def accepts_model(self, inner):
        return inner in {"auto", "xchat"}

    def translate_model(self, model):
        return model

    def pick_account(self, exclude_ids=None):
        return None

    async def pick_account_with_fallback(self, exclude_ids=None):
        return None

    async def has_usable_account(self):
        return True

    async def chat_completions(self, payload, api_key_info):
        self.calls += 1
        return ("error", (500, {"error": {"message": "xcfail down", "type": "server_error"}}))


class _OkChanStub:
    id = "xcok"
    display_name = "XCOk"

    def __init__(self):
        self.last_payload = None
        self.calls = 0

    def list_models(self):
        return [{"id": "auto"}]

    def alias_map(self):
        return {}

    def accepts_model(self, inner):
        return inner in {"auto", "xchat"}

    def translate_model(self, model):
        return model

    def pick_account(self, exclude_ids=None):
        return None

    async def pick_account_with_fallback(self, exclude_ids=None):
        return None

    async def has_usable_account(self):
        return True

    async def chat_completions(self, payload, api_key_info):
        self.last_payload = payload
        self.last_api_key_info = api_key_info
        self.calls += 1
        return (
            "json",
            {
                "id": "chatcmpl-xcok",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            },
        )


@pytest.fixture()
def two_cross_channels(monkeypatch):
    fail = _FailChanStub()
    ok = _OkChanStub()
    providers.register_provider(fail)
    providers.register_provider(ok)
    monkeypatch.setattr(
        providers, "enabled_provider_ids",
        lambda: ["workbuddy", "xcfail", "xcok"],
    )
    # The stub channels have no rows in the accounts table, but a channel is only
    # tried when it has something to spend (routing.channel_pool). Give the stubs
    # a pool so the cross-channel walk itself is what gets exercised.
    _pool(
        monkeypatch,
        xcfail=[_account("xcfail", 9001, priority=10)],
        xcok=[_account("xcok", 9002, priority=9)],
    )
    yield fail, ok
    providers._LOADED.pop("xcfail", None)
    providers._LOADED.pop("xcok", None)


def test_cross_channel_failover_falls_through_to_working_channel(two_cross_channels):
    fail, ok = two_cross_channels
    # "xchat" is only served by the two stub channels, so the real workbuddy
    # channel (which would otherwise answer "auto") is never a candidate.
    result = asyncio.run(router.chat_after_name(
        "xchat", {"model": "xchat", "messages": [{"role": "user", "content": "hi"}]},
        {"id": 7, "name": "k", "default_channel": "*"},
    ))
    assert result[0] == "json"
    assert ok.calls == 1
    assert fail.calls == 1


def test_cross_channel_failover_writes_no_pseudo_rows(two_cross_channels, monkeypatch):
    # The walk is visible through the providers' own per-account rows; the old
    # "failover" summary row (account_name=failover, no account) is gone.
    fail, ok = two_cross_channels
    captured = []
    monkeypatch.setattr(router.db, "record_request", lambda data: captured.append(data))
    result = asyncio.run(router.chat_after_name(
        "xchat", {"model": "xchat", "messages": [{"role": "user", "content": "hi"}]},
        {"id": 7, "name": "k", "default_channel": "*"},
    ))
    assert result[0] == "json"
    assert not [c for c in captured if c.get("finish_reason") == "failover"]


def test_cross_channel_failover_stream_detects_exhausted_channel(two_cross_channels, monkeypatch):
    fail, ok = two_cross_channels

    async def fail_stream(payload, api_key_info):
        err = json.dumps({"error": {"message": "all accounts down", "type": "channel_unavailable"}})
        async def chunks():
            yield f"data: {err}\n\n".encode()
            yield b"data: [DONE]\n\n"
        return ("stream", chunks())

    fail.chat_completions = fail_stream
    result = asyncio.run(router.chat_after_name(
        "xchat", {"model": "xchat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        {"id": 7, "name": "k", "default_channel": "*"},
    ))
    assert result[0] == "json"
    assert ok.calls == 1


def test_cross_channel_failover_disabled_returns_first_channel_error(two_cross_channels, monkeypatch):
    fail, ok = two_cross_channels
    monkeypatch.setenv("BUDDY_CROSS_CHANNEL_FAILOVER", "0")
    result = asyncio.run(router.chat_after_name(
        "xchat", {"model": "xchat", "messages": [{"role": "user", "content": "hi"}]},
        {"id": 7, "name": "k", "default_channel": "*"},
    ))
    assert result[0] == "error"
    assert ok.calls == 0


# ---------------------------------------------------------------------------
# On the wire: a rejected name is a 400, and nothing answers 403 any more.
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_db(tmp_path, monkeypatch):
    """An empty gateway database with a master key, so a real key can be made."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()


def _post_chat(payload, key="sk-admission-test"):
    async def run():
        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
            return await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )

    return asyncio.run(run())


def test_a_rejected_model_is_a_400_on_the_wire_and_never_a_403(client_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    db.add_api_key("sk-admission-test", "test", default_channel="workbuddy")

    response = _post_chat({
        "model": "qwenwork/qwork-advanced",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 400
    assert response.status_code != 403
    assert response.json()["detail"]["error"]["code"] == "model_not_allowed"


def test_admission_passes_before_the_pool_is_asked(client_db, monkeypatch):
    """A name the key may use is admitted even when no account can serve it."""
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    db.add_api_key("sk-admission-test", "test", default_channel="workbuddy")

    response = _post_chat({
        "model": "workbuddy/glm-5.2",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 503
    assert response.status_code != 403
    assert response.json()["error"]["code"] == "channel_unavailable"
