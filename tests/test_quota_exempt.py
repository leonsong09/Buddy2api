"""The 放行 mark: one model on one channel can skip the zero-quota gate.

The failure this pins is the mirror image of the quota gate's: a channel whose
accounts are all out of paid quota drops out of routing entirely, so a model
that costs nothing becomes unreachable. The operator marks that model on that
channel, and the gate keeps the zero-balance accounts in the pool for it — the
accounts still have to be active and not cooling down, and every other model on
the channel is unaffected.

The mark lives on the channel's own catalog id, so the request id is translated
per channel ("deepseek-v4.1-flash-1017" is "DeepSeek-Flash" on qoder) before it
is compared, and it is carried for the whole request because a provider picks
its account lazily inside its stream.
"""

import asyncio
import json
import time

import pytest

import auth_manager
import catalog
import credential_crypto
import database as db
import router
import routing
import server


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def _new_account(provider="qoder", **extra):
    payload = {
        "name": extra.pop("name", "acct"),
        "uid": f"uid-{time.time_ns()}",
        "provider": provider,
        "access_token": "token",
    }
    payload.update(extra)
    return db.get_account(db.add_account(payload))


def _zero_quota(account_id):
    db.merge_account_resource_cache(
        account_id,
        {
            "remaining": 0.0,
            "source": "test-quota-check",
            "remaining_observed_at": int(time.time()),
        },
    )


def _alias(channel, alias, target):
    current = db.get_setting("model_aliases", {}) or {}
    current.setdefault(channel, {})[alias] = target
    db.set_setting("model_aliases", current)


def _queue(model):
    return [(channel, row["id"]) for channel, row in router.account_queue(model)]


# ------------------------------------------------------------------- the gate
def test_marking_a_model_exempt_keeps_its_zero_balance_account_routable(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    assert routing.channel_pool("qoder") == []
    assert _queue("DeepSeek-Flash") == []

    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    assert _queue("DeepSeek-Flash") == [("qoder", acct["id"])]
    # every other model on that channel still meets the gate
    assert _queue("GLM-5.3") == []


def test_the_mark_is_removed_again(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)
    assert _queue("DeepSeek-Flash") == [("qoder", acct["id"])]

    result = catalog.set_exempt("qoder", "DeepSeek-Flash", False)

    assert result["exempt"] is False
    assert catalog.exempt_models("qoder") == []
    assert _queue("DeepSeek-Flash") == []


def test_the_mark_follows_the_channel_alias(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    _alias("qoder", "ds-flash", "DeepSeek-Flash")
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    assert _queue("ds-flash") == [("qoder", acct["id"])]
    assert _queue("qoder/DeepSeek-Flash") == [("qoder", acct["id"])]


def test_the_mark_does_not_leak_to_another_channel(isolated_db):
    qoder = _new_account("qoder", priority=10)
    workbuddy = _new_account("workbuddy", priority=10)
    _zero_quota(qoder["id"])
    _zero_quota(workbuddy["id"])
    _alias("qoder", "ds-flash", "DeepSeek-Flash")
    _alias("workbuddy", "ds-flash", "deepseek-v4.1-flash")
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    assert [channel for channel, _ in _queue("ds-flash")] == ["qoder"]


def test_a_disabled_account_is_not_revived_by_the_mark(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)
    db.update_account(acct["id"], {"status": "disabled"})

    assert _queue("DeepSeek-Flash") == []


def test_a_cooling_account_is_not_revived_by_the_mark(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)
    auth_manager.mark_account_failure(acct["id"], 500)

    assert _queue("DeepSeek-Flash") == []


def test_the_gate_itself_honours_the_scope(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    row = db.get_account(acct["id"])
    assert routing.account_available(row) is False
    with routing.quota_exempt_scope({"qoder"}):
        assert routing.account_available(row) is True
        # another channel's request is untouched
        other = dict(row, provider="workbuddy")
        assert routing.account_available(other) is False
    assert routing.account_available(row) is False


def test_a_channel_whose_only_pool_is_exempt_still_counts_as_routable(isolated_db):
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    assert "qoder" not in router.routable_channel_ids()

    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    assert "qoder" in router.routable_channel_ids()


# -------------------------------------------- the lazy pick (the actual lifeline)
def _stop_after_queue(seen):
    async def hook(queue):
        seen["queue"] = [(channel, row["id"]) for channel, row in queue]
        raise RuntimeError("stop before dispatch")

    return hook


def test_chat_after_name_keeps_the_note_alive_for_the_lazy_pick(isolated_db):
    """The exemption has to outlive ``account_queue``, not just sit in it.

    ``chat_after_name`` notes the request's 放行 channels in a ContextVar and
    deliberately never unsets it, because the account is chosen inside the
    stream the provider returns — after dispatch has returned. A test that only
    reads ``account_queue`` cannot tell the difference: the queue is correct
    either way, and without the note the marked model still answers 503.
    """
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)
    seen = {}

    async def scenario():
        try:
            await router.chat_after_name(
                "DeepSeek-Flash", {}, None, after_queue=_stop_after_queue(seen)
            )
        except RuntimeError:
            pass
        # ... what the provider does later, still in this request's task
        return (
            sorted(routing.quota_exempt_channels()),
            (auth_manager.pick_account(provider="qoder") or {}).get("id"),
        )

    note, picked = asyncio.run(scenario())

    assert seen["queue"] == [("qoder", acct["id"])]
    assert note == ["qoder"]
    assert picked == acct["id"]


def test_a_later_request_in_the_same_task_does_not_inherit_the_note(isolated_db):
    """The note is rewritten per request, so a plain model stays plain."""
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)
    seen = {}

    async def scenario():
        notes = []
        for model in ("DeepSeek-Flash", "GLM-5.3"):
            try:
                await router.chat_after_name(
                    model, {}, None, after_queue=_stop_after_queue(seen)
                )
            except RuntimeError:
                pass
            notes.append((model, sorted(routing.quota_exempt_channels())))
        return notes, auth_manager.pick_account(provider="qoder")

    notes, picked = asyncio.run(scenario())

    assert notes == [("DeepSeek-Flash", ["qoder"]), ("GLM-5.3", [])]
    assert picked is None


def test_without_the_note_the_lazy_pick_finds_nothing(isolated_db):
    """Mirror image: a correct queue is not enough — the note is what serves it."""
    acct = _new_account("qoder", priority=10)
    _zero_quota(acct["id"])
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    assert _queue("DeepSeek-Flash") == [("qoder", acct["id"])]
    routing.note_quota_exempt(())
    assert auth_manager.pick_account(provider="qoder") is None


# ----------------------------------------------------------------- the catalog
def test_snapshot_marks_the_exempt_rows(isolated_db):
    catalog.set_exempt("qoder", "DeepSeek-Flash", True)

    sources = {item["channel"]: item for item in catalog.catalog_snapshot()["sources"]}
    rows = {row["id"]: row for row in sources["qoder"]["models"]}

    assert rows["DeepSeek-Flash"]["exempt"] is True
    assert rows["GLM-5.3"]["exempt"] is False


def test_the_setting_survives_and_tolerates_a_channel_prefix(isolated_db):
    catalog.set_exempt("qoder", "qoder/DeepSeek-Flash", True)

    assert catalog.exempt_models("qoder") == ["DeepSeek-Flash"]
    assert catalog.is_exempt("qoder", "DeepSeek-Flash") is True
    assert db.get_setting(catalog.EXEMPT_SETTING) == {"qoder": ["DeepSeek-Flash"]}


def test_unknown_channel_and_empty_id_are_rejected(isolated_db):
    with pytest.raises(catalog.CatalogError):
        catalog.set_exempt("nope", "DeepSeek-Flash", True)
    with pytest.raises(catalog.CatalogError):
        catalog.set_exempt("qoder", "   ", True)


# --------------------------------------------------------------------- the API
def _post(payload):
    class FakeRequest:
        async def stream(self):
            yield json.dumps(payload).encode("utf-8")

    return FakeRequest()


def test_admin_endpoint_sets_and_clears_the_mark(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)

    result = asyncio.run(
        server.admin_set_model_exempt(
            _post({"channel": "qoder", "id": "DeepSeek-Flash"}), authorization=None
        )
    )
    assert result["exempt"] is True
    assert catalog.exempt_models("qoder") == ["DeepSeek-Flash"]

    result = asyncio.run(
        server.admin_set_model_exempt(
            _post({"channel": "qoder", "id": "DeepSeek-Flash", "exempt": False}),
            authorization=None,
        )
    )
    assert result["exempt"] is False
    assert catalog.exempt_models("qoder") == []


def test_admin_endpoint_rejects_an_unknown_channel(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            server.admin_set_model_exempt(
                _post({"channel": "nope", "id": "DeepSeek-Flash"}), authorization=None
            )
        )

    assert "400" in str(excinfo.value) or "disabled channel" in str(excinfo.value)


def test_admin_endpoint_parses_the_flag_strictly(isolated_db, monkeypatch):
    """A string "false" must not mark a model 放行 (bool("false") is True)."""
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)

    async def send(payload):
        return await server.admin_set_model_exempt(_post(payload), authorization=None)

    assert asyncio.run(send({"channel": "qoder", "id": "GLM-5.3", "exempt": "false"}))[
        "exempt"
    ] is False
    assert catalog.exempt_models("qoder") == []

    assert asyncio.run(send({"channel": "qoder", "id": "GLM-5.3", "exempt": "true"}))[
        "exempt"
    ] is True
    assert catalog.exempt_models("qoder") == ["GLM-5.3"]

    for bad in ("maybe", 2, None, []):
        with pytest.raises(Exception) as excinfo:
            asyncio.run(send({"channel": "qoder", "id": "GLM-5.3", "exempt": bad}))
        assert "400" in str(excinfo.value)
