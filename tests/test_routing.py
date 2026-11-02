"""The routing decision, in one place: priority tier, id.

These rules were audited against the live gateway, so they are pinned here: a
later refactor that quietly reorders them (or drops the daily-quota branch) must
fail loudly instead of only showing up as odd account picks in production.

Expiry is deliberately absent from the order: the upstream quota reading is
neither fresh nor reliable (a channel can answer 200 with a business error), so
``urgency_days`` is shown on the operator's page and orders nothing.
"""

import time

import pytest

import routing


@pytest.fixture()
def no_channel_override(monkeypatch):
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {})


def _account(aid, priority=0, provider="workbuddy", **extra):
    row = {"id": aid, "priority": priority, "provider": provider}
    row.update(extra)
    return row


def _order(accounts, channel="workbuddy"):
    return [a["id"] for a in sorted(accounts, key=lambda a: routing.sort_key(a, channel))]


def _order_by_provider(accounts):
    return [a["id"] for a in sorted(accounts, key=lambda a: routing.sort_key(a, a["provider"]))]


# ------------------------------------------------------------------ ordering


def test_priority_tier_beats_the_id(no_channel_override):
    # A barely-urgent account on a higher tier still goes first.
    assert _order([_account(1, priority=5), _account(2, priority=1)]) == [1, 2]


def test_the_id_orders_inside_one_tier_regardless_of_urgency(no_channel_override):
    # Urgency is reversed on purpose: whatever the expiry says, one tier is
    # ordered by id alone.
    rows = [_account(1, urgency_days=9.0), _account(2, urgency_days=0.5), _account(3, urgency_days=3.0)]
    assert _order(rows) == [1, 2, 3]
    assert _order(list(reversed(rows))) == [1, 2, 3]


def test_id_breaks_ties(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    assert _order([_account(7), _account(3), _account(5)]) == [3, 5, 7]


def test_exhausted_account_leaves_the_pool_even_at_priority_99(monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"remaining": 0, "peak_remaining": 1000})
    assert routing.account_available(_account(1, priority=99)) is False


def test_unknown_quota_is_not_treated_as_empty(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    assert routing.quota_exhausted(_account(1)) is False
    assert routing.account_available(_account(1)) is True
    assert routing.urgency_days(_account(1), "workbuddy") == routing.DEFAULT_URGENCY_DAYS


def test_an_old_zero_is_still_a_zero(monkeypatch):
    """No freshness window: a ten-day-old 0 keeps the account out of the pool."""
    old = int(time.time()) - 10 * 86400
    monkeypatch.setattr(
        routing,
        "resource_snapshot",
        lambda a: {"remaining": 0, "remaining_observed_at": old, "cache_updated_at": old},
    )
    assert routing.quota_exhausted(_account(1)) is True
    assert routing.account_available(_account(1)) is False


def test_low_quota_is_reported_but_no_longer_demotes(monkeypatch):
    """A drained-but-not-empty account keeps its rank; only "gone" leaves the pool."""
    monkeypatch.setattr(
        routing,
        "resource_snapshot",
        lambda a: {"remaining": 3, "peak_remaining": 1000} if a["id"] == 1 else {"remaining": 900, "peak_remaining": 1000},
    )
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {})
    low = _account(1)
    # Still reported as "low" for the preview...
    assert routing.quota_state(low) == "low"
    assert routing.account_available(low) is True
    # ...but nothing in the sort key reacts to it: the pair falls back to the id.
    rows = sorted([_account(2), low], key=lambda a: routing.sort_key(a, "workbuddy"))
    assert [a["id"] for a in rows] == [1, 2]


# ------------------------------------------------------------------- urgency


def test_manual_urgency_wins(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"next_expire_days": 30})
    row = _account(1, urgency_days=0.25)
    assert routing.urgency_days(row, "workbuddy") == 0.25
    assert routing.urgency_source(row, "workbuddy") == "manual"


def test_the_official_snapshot_wins_over_the_channel_override(monkeypatch):
    # "Expiring soon" is real data; a per-channel override must not hide it.
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {"workbuddy": 2.0})
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"next_expire_days": 30})
    row = _account(1)
    assert routing.urgency_days(row, "workbuddy") == 30.0
    assert routing.urgency_source(row, "workbuddy") == "quota"


def test_channel_override_fills_in_when_nothing_official_is_known(monkeypatch):
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {"workbuddy": 2.0})
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    row = _account(1)
    assert routing.urgency_days(row, "workbuddy") == 2.0
    assert routing.urgency_source(row, "workbuddy") == "channel"


def test_a_stale_snapshot_expiry_does_not_beat_the_channel_override(monkeypatch):
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {"workbuddy": 2.0})
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"expires_at": time.time() - 3600})
    row = _account(1)
    assert routing.urgency_days(row, "workbuddy") == 2.0
    assert routing.urgency_source(row, "workbuddy") == "channel"


def test_official_expiry_is_reported_as_quota(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"next_expire_days": 10})
    row = _account(1)
    assert routing.urgency_days(row, "workbuddy") == 10.0
    assert routing.urgency_source(row, "workbuddy") == "quota"


def test_an_already_passed_expiry_is_not_called_quota(no_channel_override, monkeypatch):
    # A stale expires_at must not be advertised as official data: fall through to
    # the channel default and say so.
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {"expires_at": time.time() - 3600})
    row = _account(1)
    assert routing.urgency_source(row, "workbuddy") == "default"
    assert routing.urgency_days(row, "workbuddy") == routing.DEFAULT_URGENCY_DAYS


def test_loomy_is_the_only_daily_channel(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    row = _account(1, provider="loomy")
    value = routing.urgency_days(row, "loomy")
    assert 0.0 < value <= 1.0
    assert value == pytest.approx(routing.daily_reset_urgency(), abs=1e-3)
    assert routing.urgency_source(row, "loomy") == "daily"


@pytest.mark.parametrize(
    "channel",
    ["workbuddy", "workbuddy-i", "qoder", "qoder-i", "qwenwork", "traework", "qclaw", "xiaohuanxiong"],
)
def test_every_other_channel_has_no_deadline(channel, no_channel_override, monkeypatch):
    # The user's rule: only loomy is daily, everything else without official
    # expiry data is unlimited — not "daily" dressed up as an urgent quota.
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    row = _account(1, provider=channel)
    assert routing.urgency_days(row, channel) == routing.DEFAULT_URGENCY_DAYS
    assert routing.urgency_source(row, channel) == "default"


def test_a_daily_channel_still_resets_even_with_a_channel_override(monkeypatch):
    # daily is checked before the override: a daily reset is a fact about the
    # quota, the override only fills in for channels with no deadline at all.
    monkeypatch.setattr(routing, "channel_urgency_map", lambda: {"loomy": 7.0})
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    row = _account(1, provider="loomy")
    assert routing.urgency_days(row, "loomy") == pytest.approx(routing.daily_reset_urgency(), abs=1e-3)
    assert routing.urgency_source(row, "loomy") == "daily"


def test_daily_urgency_shrinks_as_midnight_approaches(monkeypatch):
    midnight = time.mktime((2026, 10, 31, 0, 0, 0, 0, 0, -1))
    monkeypatch.setattr(routing.time, "time", lambda: midnight - 3600 * 6)
    assert routing.daily_reset_urgency() == pytest.approx(0.25, abs=1e-3)


def test_channels_without_a_deadline_expire_never(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    row = _account(1)
    assert routing.urgency_days(row, "workbuddy") == routing.DEFAULT_URGENCY_DAYS
    assert routing.urgency_source(row, "workbuddy") == "default"


def test_expiry_no_longer_orders_anything(no_channel_override, monkeypatch):
    # workbuddy reports a 10-day expiry, loomy resets at midnight: under the old
    # decision chain loomy went first. Expiry is display-only now, so the id
    # decides and the order is the same with the readings reversed.
    monkeypatch.setattr(
        routing,
        "resource_snapshot",
        lambda a: {"next_expire_days": 10} if a["provider"] == "workbuddy" else None,
    )
    rows = [_account(1, provider="workbuddy"), _account(2, provider="loomy")]
    assert _order_by_provider(rows) == [1, 2]
    assert routing.urgency_days(rows[0], "workbuddy") == 10.0
    assert routing.urgency_source(rows[1], "loomy") == "daily"
    monkeypatch.setattr(
        routing,
        "resource_snapshot",
        lambda a: {"next_expire_days": 1} if a["provider"] == "workbuddy" else None,
    )
    assert _order_by_provider(rows) == [1, 2]


# ------------------------------------------------------------- fixed chain
#
# The chain is fixed: priority -> channel order -> id. The switches that used to
# turn a factor off are gone: a half-disabled order silently disagreed with the
# rank the page shows, so no setting may influence the order any more.


def test_the_order_reads_no_factor_setting(no_channel_override, monkeypatch):
    """A leftover routing_factors row must not be able to change anything."""
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {})
    rows = [_account(1, priority=1), _account(2, priority=9)]
    assert _order(rows) == [2, 1]

    monkeypatch.setattr(
        routing,
        "_setting",
        lambda key: {"priority": False, "channel_order": False},
    )
    assert _order(rows) == [2, 1]
    assert not hasattr(routing, "routing_factors")
    assert not hasattr(routing, "DEFAULT_ROUTING_FACTORS")
    assert "routing_factors" not in routing.routing_settings()


def test_the_priority_tier_comes_from_the_account_alone(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {})
    rows = [_account(1, priority=1), _account(2, priority=9), _account(3, priority=5)]
    assert _order(rows) == [2, 3, 1]
    assert _order(list(reversed(rows))) == [2, 3, 1]


def test_a_merely_low_quota_no_longer_touches_the_sort_key(no_channel_override, monkeypatch):
    """Quota-low demotion is gone: a drained-but-not-empty account keeps its rank."""
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: a["snap"])
    monkeypatch.setattr(routing, "low_watermark_pct", lambda: 5.0)
    low = _account(1, snap={"remaining": 3, "peak_remaining": 1000})
    full = _account(1, snap={"remaining": 1000, "peak_remaining": 1000})
    # The diagnostic reading is unchanged...
    assert routing.quota_low(low) is True
    assert routing.quota_low(full) is False
    assert routing.quota_state(low) == "low"
    # ...but it contributes nothing to the order: same account, same tier, same
    # id, so a low reading and a full one must produce the very same key.
    assert routing.sort_key(low, "workbuddy") == routing.sort_key(full, "workbuddy")


def test_the_id_is_the_only_final_tie_break(no_channel_override, monkeypatch):
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: {})
    assert _order([_account(7), _account(3), _account(5)]) == [3, 5, 7]
    # Priority 0 then the id — nothing else sits behind them.
    assert routing.sort_key(_account(3), "workbuddy") == (0, -1, 0.0, 0, 3)
    # order_accounts gates on availability, which no setting touches.
    assert [a["id"] for a in routing.order_accounts([_account(7), _account(3)])] == [3, 7]


# --------------------------------------------------------- rate window counters


def test_rate_window_still_counts_a_hot_account_then_forgets_it():
    """The counter is untouched: only its influence on the order went away."""
    routing.reset_rate_windows()
    cap = routing.account_inflight_cap()
    row = _account(1)
    for _ in range(cap):
        routing.note_pick(1)
    assert routing.rpm_used(1) == cap
    assert routing.recent_starts(1, routing.inflight_window_seconds()) == cap
    assert routing.account_available(row) is True
    routing.reset_rate_windows()
    assert routing.rpm_used(1) == 0
    assert routing.recent_starts(1, routing.inflight_window_seconds()) == 0


def test_a_picked_account_has_the_identical_sort_key(no_channel_override, monkeypatch):
    """Regression: picking a single account must not move it in the order."""
    monkeypatch.setattr(routing, "resource_snapshot", lambda a: None)
    monkeypatch.setattr(routing, "account_rpm_cap", lambda: 1)
    row = _account(1, priority=3)
    peer = _account(2, priority=3)
    routing.reset_rate_windows()
    before = routing.sort_key(row, "workbuddy")
    routing.note_pick(1)
    # The account really is over its rpm cap now...
    assert routing.rpm_used(1) >= 1
    # ...and its key did not budge: same tier, the id still decides.
    assert routing.sort_key(row, "workbuddy") == before
    assert routing.sort_key(row, "workbuddy") < routing.sort_key(peer, "workbuddy")
    routing.reset_rate_windows()


def test_inflight_window_counts_only_recent_picks(no_channel_override):
    routing.reset_rate_windows()
    routing.note_pick(1)
    window = routing.inflight_window_seconds()
    assert routing.recent_starts(1, window) == 1
    routing.reset_rate_windows()
    assert routing.recent_starts(1, window) == 0


# ----------------------------------------------------------- static table rank


def test_static_order_index_numbers_by_priority_channel_then_id():
    """The operator's list order: tier, then channel order, then id."""
    rows = [
        _account(1, priority=0, provider="traework"),
        _account(2, priority=5, provider="workbuddy"),
        _account(3, priority=5, provider="qoder"),
        _account(4, priority=5, provider="qoder"),
    ]
    index = routing.static_order_index(rows, ["qoder", "traework", "workbuddy"])
    assert index == {3: 1, 4: 2, 2: 3, 1: 4}
    # The input order is not an input: same rows, same numbering.
    assert routing.static_order_index(list(reversed(rows)), ["qoder", "traework", "workbuddy"]) == index


def test_static_order_index_ignores_every_state_marker():
    """Disabled, expired or used-up rows still get a number: this is not a pool."""
    rows = [
        _account(1, priority=1, status="expired"),
        _account(2, priority=1, status="disabled"),
        _account(3, priority=1, quota_state="exhausted"),
        _account(4, priority=1, remaining=0),
    ]
    assert routing.static_order_index(rows, ["workbuddy"]) == {1: 1, 2: 2, 3: 3, 4: 4}


def test_static_order_index_is_total_for_unlisted_channels_and_no_input():
    rows = [_account(1, priority=0, provider="qclaw"), _account(2, priority=0, provider="workbuddy")]
    # A channel missing from the configured order sorts after every listed one.
    assert routing.static_order_index(rows, ["workbuddy"]) == {2: 1, 1: 2}
    # Without an order every channel ties, so the id alone decides.
    assert routing.static_order_index(rows) == {1: 1, 2: 2}
    assert routing.static_order_index([]) == {}
