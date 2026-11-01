"""The admin routing preview must show what the request path would really do.

A prefixed id such as ``qoder/auto`` names a channel *and* an inner model. The
preview hands the raw name to the same queue the request path walks, so it can
never disagree with what a request would really do.

The account order is one flat queue across channels, and ``/admin/accounts`` and
``/admin/routing-plan`` both read it through the router — neither re-sorts. These
tests pin that order against the real pool code so a later refactor cannot
quietly bring back a second, drifting copy of the sort keys.
"""

import asyncio
import json

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import providers
import router
import routing
import server


def _plan(monkeypatch, **kwargs):
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    return asyncio.run(server.admin_routing_plan(authorization=None, **kwargs))


def _accounts(monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    return asyncio.run(server.admin_list_accounts(authorization=None))


def _queue_channels():
    """The channel order the one queue really walks (first appearance)."""
    order = []
    for channel, _account in router.account_queue():
        if channel not in order:
            order.append(channel)
    return order


def _get_routing_settings(monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    return asyncio.run(server.admin_get_routing_settings(authorization=None))


def _put_routing_settings(monkeypatch, payload):
    class FakeRequest:
        async def stream(self):
            yield json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    return asyncio.run(
        server.admin_update_routing_settings(FakeRequest(), authorization=None)
    )


def test_prefixed_model_previews_through_the_real_queue(monkeypatch):
    """The raw name goes to the queue, and the gate is the only prefix judge."""
    captured = []

    def fake_queue(name=None, api_key_info=None):
        captured.append((name, api_key_info))
        return []

    monkeypatch.setattr(router, "account_queue", fake_queue)

    plan = _plan(monkeypatch, model="qoder/auto")

    # The queue is asked with the name exactly as the client sent it.
    assert captured[0] == ("qoder/auto", None)
    # Nothing can be spent right now, so the plan falls back to reporting the
    # channels that know this model, with no accounts to list.
    assert [block["channel"] for block in plan["channels"]] == ["qoder"]
    assert plan["channels"][0]["accounts"] == []


def test_unbindable_model_still_previews_its_raw_name(monkeypatch):
    captured = []

    def fake_queue(name=None, api_key_info=None):
        captured.append((name, api_key_info))
        return []

    monkeypatch.setattr(router, "account_queue", fake_queue)

    plan = _plan(monkeypatch, model="nope")

    assert captured[0] == ("nope", None)
    # Nobody knows it, and the page must not invent a channel for it.
    assert plan["channels"] == []


def test_the_plan_channel_filter_locks_the_preview_to_that_channel(monkeypatch):
    """Without ?channel= the preview walks the whole pool, so priority can win;
    with it the preview is the queue a key bound to that channel would get."""
    captured = []

    def fake_queue(name=None, api_key_info=None):
        captured.append((name, api_key_info))
        return []

    monkeypatch.setattr(router, "account_queue", fake_queue)

    _plan(monkeypatch, model="hy3", channel="qoder")
    assert ("hy3", {"default_channel": "qoder"}) in captured

    _plan(monkeypatch, model="hy3")
    assert ("hy3", None) in captured


# ---------------------------------------------------- global rank / the queue


@pytest.fixture()
def routing_db(tmp_path, monkeypatch):
    """A throwaway DB so the real pool and ordering code runs on known rows."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    # qoder first in the model-config list, so a priority win has to override it.
    db.set_setting("channel_order", ["qoder", "workbuddy", "traework"])
    yield
    credential_crypto.reset_cache()


def _account(provider, priority, name, **extra):
    return db.add_account({
        "provider": provider,
        "priority": priority,
        "name": name,
        "access_token": "token",
        **extra,
    })


def test_global_rank_walks_the_one_account_queue(routing_db, monkeypatch):
    wb_high = _account("workbuddy", 5, "wb-high")
    wb_low = _account("workbuddy", 1, "wb-low")
    qoder_only = _account("qoder", 3, "qoder-only")

    rows = _accounts(monkeypatch)
    by_id = {row["id"]: row for row in rows}

    expected = [account["id"] for _cid, account in router.account_queue()]
    # One queue across channels: priority 5 -> 3 -> 1.
    assert expected == [wb_high, qoder_only, wb_low]
    assert [by_id[aid]["routing"]["global_rank"] for aid in expected] == [1, 2, 3]

    # rank stays channel-local and channel_rank stays the model-config position.
    assert by_id[wb_high]["routing"]["rank"] == 1
    assert by_id[wb_low]["routing"]["rank"] == 2
    assert by_id[qoder_only]["routing"]["rank"] == 1
    assert by_id[qoder_only]["routing"]["channel_rank"] == 0
    assert by_id[wb_high]["routing"]["channel_rank"] == 1

    ranks = sorted(
        row["routing"]["global_rank"]
        for row in rows
        if row["routing"]["global_rank"] is not None
    )
    assert ranks == list(range(1, len(expected) + 1))


def test_global_rank_is_one_flat_order_across_channels(routing_db, monkeypatch):
    """The regression this replaces: a channel-first order wasted the pool.

    Grouping by channel first put workbuddy's priority-1 account ahead of
    qoder's priority-3 one, simply because workbuddy had the better *best*
    account. Priority is a property of the account, so it decides the order.
    """
    wb_high = _account("workbuddy", 5, "wb-high")
    wb_low = _account("workbuddy", 1, "wb-low")
    qoder_mid = _account("qoder", 3, "qoder-mid")

    rows = _accounts(monkeypatch)
    by_global_rank = [row["id"] for row in sorted(rows, key=lambda row: row["routing"]["global_rank"])]
    assert by_global_rank == [wb_high, qoder_mid, wb_low]

    # What a channel-first order would have produced instead (qoder leads the
    # configured list, then workbuddy's own accounts by priority).
    channel_first = [a["id"] for cid in router._channel_order() for a in routing.channel_pool(cid)]
    assert channel_first == [qoder_mid, wb_high, wb_low]
    assert by_global_rank != channel_first


def test_equal_channel_priority_breaks_ties_by_config_order(routing_db):
    _account("workbuddy", 5, "wb-high")
    _account("qoder", 3, "qoder-mid")
    _account("traework", 3, "trae-tie")

    # qoder and traework tie at 3, so the config list (qoder before traework)
    # decides; workbuddy leads on its 5.
    assert _queue_channels() == ["workbuddy", "qoder", "traework"]


def test_accounts_outside_every_pool_have_no_global_rank(routing_db, monkeypatch):
    active = _account("workbuddy", 1, "wb-active")
    exhausted = _account("workbuddy", 99, "wb-exhausted")
    db.upsert_account_resource_cache(exhausted, {"remaining": 0, "peak_remaining": 100})
    expired = _account("qoder", 50, "qoder-expired", status="expired")

    rows = _accounts(monkeypatch)
    by_id = {row["id"]: row for row in rows}

    assert by_id[active]["routing"]["global_rank"] == 1
    for gated in (exhausted, expired):
        assert by_id[gated]["routing"]["global_rank"] is None
        assert by_id[gated]["routing"]["rank"] is None
    # A provably empty quota must not promote its account — or its channel.
    assert routing.channel_pool("workbuddy")[0]["id"] == active
    assert _queue_channels() == ["workbuddy"]


def test_routing_plan_without_a_model_shows_the_real_queue(routing_db, monkeypatch):
    _account("workbuddy", 5, "wb-high")
    _account("workbuddy", 1, "wb-low")
    _account("qoder", 3, "qoder-mid")
    _account("traework", 3, "trae-tie")

    plan = _plan(monkeypatch)

    assert [block["channel"] for block in plan["channels"]] == _queue_channels()
    assert [
        (block["channel"], block["try_order"], block["channel_rank"])
        for block in plan["channels"]
    ] == [("workbuddy", 1, 1), ("qoder", 2, 0), ("traework", 3, 2)]


def test_plan_accounts_carry_the_same_global_rank_as_the_accounts_page(routing_db, monkeypatch):
    _account("workbuddy", 5, "wb-high")
    _account("workbuddy", 1, "wb-low")
    _account("qoder", 3, "qoder-mid")
    _account("traework", 4, "trae-high")
    _account("traework", 2, "trae-low")

    page = {row["id"]: row["routing"]["global_rank"] for row in _accounts(monkeypatch)}
    plan = _plan(monkeypatch)
    from_plan = {
        row["id"]: row["global_rank"]
        for block in plan["channels"]
        for row in block["accounts"]
    }

    # Without a model every account is listed, numbered 1..N without a gap.
    assert [block["channel"] for block in plan["channels"]] == ["workbuddy", "traework", "qoder"]
    assert sorted(from_plan.values()) == list(range(1, len(from_plan) + 1))
    assert from_plan == {aid: rank for aid, rank in page.items() if rank is not None}
    # Each account still starts at 1 inside its own channel block.
    assert [[row["rank"] for row in block["accounts"]] for block in plan["channels"]] == [
        [1, 2],
        [1, 2],
        [1],
    ]


def test_plan_global_rank_stays_global_when_a_model_narrows_the_channels(
    routing_db, monkeypatch
):
    """A model only traework serves: the numbers keep holes, they do not restart."""
    wb = _account("workbuddy", 5, "wb-high")
    _account("workbuddy", 1, "wb-low")
    qoder = _account("qoder", 3, "qoder-mid")
    trae_high = _account("traework", 4, "trae-high")
    trae_low = _account("traework", 2, "trae-low")

    class _OnlyTrae:
        display_name = "traework only"

        def accepts_model(self, inner):
            return inner == "trae-only"

    monkeypatch.setattr(providers, "get_provider", lambda cid: _OnlyTrae())
    class _OnlyTraeModel:
        display_name = "traework only"

        def accepts_model(self, inner):
            return inner == "trae-only"

    monkeypatch.setattr(providers, "get_provider", lambda cid: _OnlyTraeModel())

    page = {row["id"]: row["routing"]["global_rank"] for row in _accounts(monkeypatch)}
    plan = _plan(monkeypatch, model="traework/trae-only")

    assert [block["channel"] for block in plan["channels"]] == ["traework"]
    rows = plan["channels"][0]["accounts"]
    assert [row["id"] for row in rows] == [trae_high, trae_low]
    # The numbers are one queue: workbuddy takes 1, traework 2 and 4, qoder 3.
    # A narrowed model keeps those holes, it never restarts at 1.
    assert [row["global_rank"] for row in rows] == [2, 4]
    assert [page[wb], page[qoder]] == [1, 3]
    assert [row["global_rank"] for row in rows] == [page[trae_high], page[trae_low]]


def test_the_preview_is_the_queue_and_not_a_second_sort(routing_db, monkeypatch):
    """One implementation: the page lists the queue, per channel, unsorted."""
    _account("workbuddy", 5, "wb-high")
    _account("workbuddy", 1, "wb-low")
    _account("qoder", 3, "qoder-mid")

    plan = _plan(monkeypatch)

    assert [block["channel"] for block in plan["channels"]] == _queue_channels()
    for block in plan["channels"]:
        from_queue = [
            account["id"]
            for cid, account in router.account_queue()
            if cid == block["channel"]
        ]
        assert [row["id"] for row in block["accounts"]] == from_queue


# ------------------------------------------------------------------- order
#
# The chain is fixed: priority -> channel order -> id. The switches that used to
# disable a factor were removed, so a leftover routing_factors row must be inert.


def test_the_priority_tier_wins_over_the_model_config_order(routing_db):
    _account("workbuddy", 5, "wb-high")
    _account("qoder", 3, "qoder-mid")

    # workbuddy's best (5) beats qoder's (3) although qoder leads the config list.
    assert _queue_channels() == ["workbuddy", "qoder"]
    # A leftover switch row changes nothing.
    db.set_setting("routing_factors", {"priority": False})
    assert _queue_channels() == ["workbuddy", "qoder"]


def test_the_queue_still_filters_to_channels_with_an_account(routing_db):
    _account("workbuddy", 1, "wb")
    # qoder and traework are configured but hold no account, so they are never
    # tried — no ordering rule may change that gate.
    assert _queue_channels() == ["workbuddy"]
    assert router.account_queue("qoder/auto") == []
    assert _queue_channels() == ["workbuddy"]


def test_the_channel_order_breaks_ties_on_equal_priority(routing_db):
    _account("workbuddy", 5, "wb")
    _account("traework", 5, "tw")
    _account("qoder", 5, "qd")

    # All three tie on priority, so the model-config order decides.
    assert _queue_channels() == ["qoder", "workbuddy", "traework"]
    db.set_setting("routing_factors", {"channel_order": False})
    assert _queue_channels() == ["qoder", "workbuddy", "traework"]
    # Reordering the model-config list really does move the answer.
    db.set_setting("channel_order", ["traework", "qoder", "workbuddy"])
    assert _queue_channels() == ["traework", "qoder", "workbuddy"]


def test_the_priority_tier_still_leads_the_channel_order(routing_db):
    _account("qoder", 1, "qd-low")
    _account("traework", 9, "tw-high")

    assert _queue_channels() == ["traework", "qoder"]


def test_global_rank_is_one_continuous_numbering(routing_db, monkeypatch):
    wb_high = _account("workbuddy", 5, "wb-high")
    qoder_mid = _account("qoder", 3, "qoder-mid")

    def ranks():
        rows = _accounts(monkeypatch)
        return {row["id"]: row["routing"]["global_rank"] for row in rows}

    first = ranks()
    assert [first[wb_high], first[qoder_mid]] == [1, 2]
    # A leftover switch row cannot move a rank any more.
    db.set_setting("routing_factors", {"priority": False})
    after = ranks()
    assert [after[wb_high], after[qoder_mid]] == [1, 2]
    assert sorted(v for v in after.values() if v is not None) == [1, 2]


def test_expiry_readings_no_longer_move_the_account_pool(routing_db):
    slow = _account("workbuddy", 1, "wb-slow")
    urgent = _account("workbuddy", 1, "wb-urgent")
    db.upsert_account_resource_cache(urgent, {"next_expire_days": 1})
    db.upsert_account_resource_cache(slow, {"next_expire_days": 40})

    # Same tier: the id decides, whatever the two expiry readings say. The old
    # chain would have put the 1-day account first.
    assert routing.urgency_days({"id": urgent, "provider": "workbuddy"}, "workbuddy") == 1.0
    assert [a["id"] for a in routing.channel_pool("workbuddy")] == [slow, urgent]


def test_the_settings_endpoint_no_longer_carries_the_factor_switches(
    routing_db, monkeypatch
):
    got = _get_routing_settings(monkeypatch)
    assert "routing_factors" not in got
    assert "routing_factors" not in got["settings"]
    assert "routing_factors" not in got["defaults"]

    written = _put_routing_settings(monkeypatch, {"routing_factors": {"priority": False}})
    assert written["status"] == "ok"
    assert "routing_factors" not in written["written"]
    assert "routing_factors" not in written["settings"]
    assert "routing_factors" not in _get_routing_settings(monkeypatch)
    # An unknown payload key is ignored rather than stored.
    assert db.get_setting("routing_factors", None) is None
    assert not hasattr(routing, "routing_factors")
    assert not hasattr(routing, "DEFAULT_ROUTING_FACTORS")


def test_routing_settings_still_validates_its_other_fields(routing_db, monkeypatch):
    written = _put_routing_settings(monkeypatch, {"routing_account_rpm": 12})
    assert written["written"]["routing_account_rpm"] == 12
    with pytest.raises(HTTPException) as error:
        _put_routing_settings(monkeypatch, {"routing_account_rpm": "many"})
    assert error.value.status_code == 400


def test_a_throttled_account_keeps_its_place_with_the_real_rpm_setting(routing_db):
    """User m10969: nobody knows when a throttle lifts, so keep trying in turn.

    The account stays in its tier at its own position — the rpm reading is a
    statistic, not a routing key.
    """
    db.set_setting("routing_account_rpm", 1)
    first = _account("workbuddy", 5, "wb-first")
    second = _account("workbuddy", 5, "wb-second")
    routing.reset_rate_windows()

    rows = routing.channel_pool("workbuddy")
    assert [a["id"] for a in rows] == [first, second]
    before = routing.sort_key(rows[0], "workbuddy")

    routing.note_pick(first)
    # The pick really is recorded through the real settings/db path...
    assert routing.rpm_used(first) >= 1
    assert routing.account_available({"id": first, "provider": "workbuddy"}) is True
    # ...and neither the pool order nor the key of the throttled account moved.
    assert [a["id"] for a in routing.channel_pool("workbuddy")] == [first, second]
    assert routing.sort_key(rows[0], "workbuddy") == before
    assert routing.sort_key(rows[0], "workbuddy") < routing.sort_key(rows[1], "workbuddy")
    routing.reset_rate_windows()


def test_the_queue_keeps_its_order_while_an_account_is_picked(routing_db):
    """The fixed queue, and while one account is being spent.

    ``channel_order`` is deliberately set to the reverse of what priority is
    about to produce, so anything that still leaked the config order into the
    channel key would show up here immediately.
    """
    db.set_setting(
        "channel_order",
        ["xiaohuanxiong", "traework", "qwenwork", "workbuddy", "loomy", "qoder"],
    )
    _account("qoder", 6, "qd")
    _account("loomy", 5, "lm")
    wb = _account("workbuddy", 4, "wb")
    _account("qwenwork", 3, "qw")
    _account("traework", 2, "tw")
    _account("xiaohuanxiong", 1, "xh")

    expected = ["qoder", "loomy", "workbuddy", "qwenwork", "traework", "xiaohuanxiong"]
    assert _queue_channels() == expected

    # Spending workbuddy's only account must not move its channel either.
    db.set_setting("routing_account_rpm", 1)
    routing.reset_rate_windows()
    routing.note_pick(wb)
    assert routing.rpm_used(wb) >= 1
    assert _queue_channels() == expected
    routing.reset_rate_windows()


# ------------------------------------------------- static rank / the account list


def test_static_rank_is_priority_then_channel_order_then_id(routing_db, monkeypatch):
    """The list's own key. channel_order here is qoder, workbuddy, traework."""
    qoder_first = _account("qoder", 9, "qoder-a")
    qoder_second = _account("qoder", 9, "qoder-b")
    wb_low = _account("workbuddy", 1, "wb-low")
    trae_low = _account("traework", 1, "trae-low")

    ranks = {row["id"]: row["routing"]["static_rank"] for row in _accounts(monkeypatch)}

    # tier 9 first, then the tier-1 rows by channel order, ids breaking the tie
    assert ranks == {qoder_first: 1, qoder_second: 2, wb_low: 3, trae_low: 4}


def test_static_rank_numbers_the_rows_the_routing_ranks_hide(routing_db, monkeypatch):
    """Disabled, expired and used-up accounts are rows too: never null, no gap."""
    active = _account("workbuddy", 1, "wb-active")
    exhausted = _account("workbuddy", 99, "wb-exhausted")
    db.upsert_account_resource_cache(exhausted, {"remaining": 0, "peak_remaining": 100})
    expired = _account("qoder", 50, "qoder-expired", status="expired")
    disabled = _account("traework", 80, "trae-disabled", status="disabled")

    rows = _accounts(monkeypatch)
    by_id = {row["id"]: row for row in rows}

    # The routing view drops all three of them...
    for gated in (exhausted, expired, disabled):
        assert by_id[gated]["routing"]["global_rank"] is None
        assert by_id[gated]["routing"]["rank"] is None
    # ...the static numbering still gives every row a place: 99, 80, 50, 1.
    assert by_id[exhausted]["routing"]["static_rank"] == 1
    assert by_id[disabled]["routing"]["static_rank"] == 2
    assert by_id[expired]["routing"]["static_rank"] == 3
    assert by_id[active]["routing"]["static_rank"] == 4
    assert sorted(row["routing"]["static_rank"] for row in rows) == [1, 2, 3, 4]


def test_static_rank_does_not_move_when_the_account_state_changes(routing_db, monkeypatch):
    """The probe the operator cares about: state moves, the list numbering does not."""
    first = _account("workbuddy", 5, "wb-first")
    second = _account("qoder", 3, "qoder-second")

    before = {row["id"]: row["routing"]["static_rank"] for row in _accounts(monkeypatch)}
    assert before == {first: 1, second: 2}

    db.update_account(first, {"status": "disabled"})
    db.upsert_account_resource_cache(second, {"remaining": 0, "peak_remaining": 100})

    after_rows = _accounts(monkeypatch)
    after = {row["id"]: row["routing"]["static_rank"] for row in after_rows}
    assert after == before == {first: 1, second: 2}
    # Not vacuous: the routing ranks did react to both changes.
    by_id = {row["id"]: row for row in after_rows}
    assert by_id[first]["routing"]["global_rank"] is None
    assert by_id[second]["routing"]["global_rank"] is None
    assert router.account_queue() == []

