"""The quota gate: a zero reading keeps an account out of its channel pool.

The failure this pins was invisible in the admin UI. A channel whose cached row
only carried an expiry date stayed first in its channel pool while the accounts
page reported "0 credits": the live number a quota check measured was printed to
the operator and thrown away, because routing reads nothing but the cache.

Two halves, tested here:
  * a zero means "no quota" and holds until something writes a newer reading —
    no timestamp ages it back into "unknown", and the account only returns when
    the user refreshes it by hand;
  * the writers that see the live number actually store it, and two partial
    writers cannot erase each other's keys.
"""

import asyncio
import time

import pytest

import control_plane
import credential_crypto
import database as db
import providers
import providers.qoder.chat as qoder_chat
import resource_sync
import routing
import server
from providers.qoder import quota as qoder_quota


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def _account(aid, provider="qoder", **extra):
    row = {"id": aid, "provider": provider, "priority": 0}
    row.update(extra)
    return row


def _snapshot(monkeypatch, payload):
    monkeypatch.setattr(routing, "resource_snapshot", lambda account: payload)


def _new_account(provider="qoder", **extra):
    """A real account row: account_resource_cache has a FK to accounts(id)."""
    payload = {"name": extra.pop("name", "acct"), "uid": f"uid-{time.time_ns()}", "provider": provider}
    payload.update(extra)
    return db.get_account(db.add_account(payload))


# ------------------------------------------------------------------- the gate


def test_zero_reading_leaves_the_pool(monkeypatch):
    _snapshot(monkeypatch, {"remaining": 0, "remaining_observed_at": int(time.time())})
    account = _account(1)
    assert routing.quota_exhausted(account) is True
    assert routing.account_available(account) is False


def test_bare_zero_without_any_timestamp_is_still_trusted(monkeypatch):
    # Hand-built snapshots (tests/test_routing.py builds these) have no clock.
    # Nothing reads a clock any more, so they are trusted like any other zero.
    _snapshot(monkeypatch, {"remaining": 0, "peak_remaining": 1000})
    assert routing.quota_exhausted(_account(1)) is True


def test_a_ten_day_old_zero_still_keeps_the_account_out(monkeypatch):
    # The reversal of the old "stale zero returns to the pool" rule: 0 is 0.
    # Only a newer reading (the user's manual refresh, or the next refused
    # request measuring the account again) may put it back in rotation.
    old = int(time.time()) - 10 * 86400
    _snapshot(monkeypatch, {"remaining": 0, "remaining_observed_at": old})
    account = _account(1)
    assert routing.quota_exhausted(account) is True
    assert routing.account_available(account) is False


def test_row_stamps_never_revive_a_zero(monkeypatch):
    """An expiry-only check-in rewrites the row clock; the zero is untouched."""
    now = int(time.time())
    long_ago = now - 10 * 86400
    _snapshot(
        monkeypatch,
        {
            "remaining": 0,
            "updated_at": long_ago,
            "cache_updated_at": long_ago,
            "age_seconds": 10 * 86400,
        },
    )
    assert routing.quota_exhausted(_account(1)) is True

    _snapshot(
        monkeypatch,
        {"remaining": 0, "remaining_observed_at": long_ago, "cache_updated_at": long_ago},
    )
    assert routing.quota_exhausted(_account(1)) is True
    assert routing.account_available(_account(1)) is False


def test_a_half_credit_is_not_exhausted(monkeypatch):
    _snapshot(monkeypatch, {"remaining": 0.5, "remaining_observed_at": time.time() - 10 * 86400})
    assert routing.quota_exhausted(_account(1)) is False
    assert routing.account_available(_account(1)) is True


def test_unknown_or_unsupported_readings_never_empty_the_pool(monkeypatch):
    fresh = int(time.time())
    for payload in (
        None,
        {},
        {"ok": False, "remaining": 0, "remaining_observed_at": fresh},
        {"unsupported": True, "remaining": 0, "remaining_observed_at": fresh},
        {"remaining": None, "remaining_observed_at": fresh},
    ):
        _snapshot(monkeypatch, payload)
        assert routing.quota_exhausted(_account(1)) is False, payload
        assert routing.account_available(_account(1)) is True, payload


def test_available_total_zero_is_proof_too(monkeypatch):
    _snapshot(monkeypatch, {"available_total": 0, "remaining_observed_at": int(time.time())})
    assert routing.quota_exhausted(_account(1)) is True


def test_positive_remaining_is_never_exhausted(monkeypatch):
    _snapshot(monkeypatch, {"remaining": 12.5, "remaining_observed_at": int(time.time())})
    assert routing.quota_exhausted(_account(1)) is False


# ------------------------------------------------------ partial cache writers


def test_quota_reading_and_expiry_survive_each_other(isolated_db):
    account = _new_account()
    expires = time.time() + 10 * 86400
    resource_sync.store_quota_expiry(account, expires_at=expires, source="qoder-campaign-validity")
    resource_sync.store_quota_reading(account, remaining=0.0, source="qoder-quota-check")

    cached = routing.resource_snapshot(account)
    assert cached["remaining"] == 0.0
    assert cached["next_expire_ts"] == pytest.approx(expires)
    assert cached["source"] == "qoder-quota-check"
    assert routing.quota_exhausted(account) is True


def test_expiry_written_after_a_reading_keeps_the_reading(isolated_db):
    account = _new_account()
    resource_sync.store_quota_reading(account, remaining=0.0)
    resource_sync.store_quota_expiry(account, expires_at=time.time() + 86400)

    cached = routing.resource_snapshot(account)
    assert cached["remaining"] == 0.0
    assert "next_expire_ts" in cached
    # The expiry-only write moved the row clock but the zero is not aged out.
    assert routing.quota_exhausted(account) is True


def test_full_snapshot_write_keeps_replace_semantics(isolated_db):
    """The workbuddy resource flow refreshes a complete snapshot: replace."""
    account = _new_account(provider="workbuddy")
    resource_sync.store_quota_reading(account, remaining=0.0)
    db.upsert_account_resource_cache(
        account["id"], {"ok": True, "next_expire_days": 3.0, "next_expire_ts": time.time() + 3 * 86400}
    )

    cached = routing.resource_snapshot(account)
    assert "remaining" not in cached
    assert cached["next_expire_days"] == pytest.approx(3.0)
    assert routing.quota_exhausted(account) is False


def test_peak_remaining_is_carried_forward(isolated_db):
    account = _new_account()
    resource_sync.store_quota_reading(account, remaining=500.0)
    resource_sync.store_quota_reading(account, remaining=0.0)
    assert routing.resource_snapshot(account)["peak_remaining"] == pytest.approx(500.0)


def test_reading_without_a_number_writes_nothing(isolated_db):
    account = _new_account()
    db.merge_account_resource_cache(account["id"], {"next_expire_ts": time.time() + 86400})

    assert resource_sync.store_quota_reading(account, remaining=None) == {}
    assert resource_sync.store_quota_reading(account, remaining="0") == {}
    assert resource_sync.store_quota_reading(account, remaining=True) == {}
    assert resource_sync.store_quota_reading({"provider": "qoder"}, remaining=0.0) == {}

    cached = routing.resource_snapshot(account)
    assert "remaining" not in cached
    assert "next_expire_ts" in cached


def test_cache_meta_keys_are_never_stored(isolated_db):
    account = _new_account()
    db.upsert_account_resource_cache(
        account["id"], {"remaining": 5, "cached": True, "age_seconds": 10**6, "cache_updated_at": 1}
    )
    cached = routing.resource_snapshot(account)
    assert cached["cached"] is True
    assert cached["age_seconds"] < 60
    assert cached["cache_updated_at"] > 1


# ------------------------------------------------- what the pages write back


def test_accounts_page_reading_feeds_the_gate(isolated_db, monkeypatch):
    aid = db.add_account({"name": "q", "uid": "q", "provider": "qoder", "priority": 10})
    account = db.get_account(aid)

    class Snapshot:
        ok = True
        unit = "credit"
        remaining = 0.0
        unsupported = False
        extra = {}
        message = ""
        channel = "qoder"
        account_id = aid

    class Provider:
        display_name = "qoder"

        async def fetch_quota(self, row):
            return Snapshot()

    monkeypatch.setattr(server, "_check_admin", lambda authorization: None)
    monkeypatch.setattr(providers, "get_provider", lambda channel: Provider() if channel == "qoder" else None)

    result = asyncio.run(server.admin_account_resources(aid=aid, force=0, authorization=None))
    assert result["remaining"] == 0.0
    assert routing.resource_snapshot(account)["remaining"] == 0.0
    assert routing.quota_exhausted(account) is True


def test_dashboard_summary_caches_a_measured_credit(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        resource_sync,
        "store_quota_reading",
        lambda account, remaining=None, peak=None, source="": calls.append((account.get("id"), remaining, source)) or {},
    )

    class Snapshot:
        ok = True
        unit = "credit"
        remaining = 0.0
        unsupported = False
        extra = {}
        account_id = 21
        message = ""

    class Provider:
        display_name = "qoder"

        async def fetch_quota(self, row):
            return Snapshot()

    summary = asyncio.run(control_plane._summarize_quota_channel("qoder", Provider(), [{"id": 21, "provider": "qoder"}]))
    assert summary["remaining"] == 0.0
    assert calls == [(21, 0.0, "dashboard-quota-check")]


def test_dashboard_does_not_cache_a_token_channel_or_a_failed_read(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        resource_sync,
        "store_quota_reading",
        lambda account, remaining=None, peak=None, source="": calls.append((account.get("id"), remaining)) or {},
    )

    class Snapshot:
        ok = True
        unit = "token"
        remaining = 0.0
        unsupported = False
        extra = {}
        account_id = 22
        message = ""

    class Provider:
        display_name = "qclaw"

        async def fetch_quota(self, row):
            return Snapshot()

    asyncio.run(control_plane._summarize_quota_channel("qclaw", Provider(), [{"id": 22, "provider": "qclaw"}]))
    assert calls == []
    control_plane._cache_quota_reading(None, 1.0)
    control_plane._cache_quota_reading({"id": None}, 1.0)
    control_plane._cache_quota_reading({}, 1.0)
    assert calls == []


# --------------------------------------------------- upstream quota refusals

QUOTA_BODY = (
    '{"code":"112","message":"{\\"pricingUrl\\":'
    '\\"https://qoder.com.cn/pricing?client=qoder\\"}"}'
)


def test_quota_refusal_detection():
    assert qoder_chat.is_quota_refusal(QUOTA_BODY) is True
    assert qoder_chat.is_quota_refusal('{"code":112}') is True
    assert qoder_chat.is_quota_refusal("pricingUrl") is True
    assert qoder_chat.is_quota_refusal('{"code":"500","message":"boom"}') is False
    assert qoder_chat.is_quota_refusal('<html>oops</html>') is False
    assert qoder_chat.is_quota_refusal("") is False


def test_upstream_refusal_does_not_invent_zero_when_the_read_fails(isolated_db, monkeypatch):
    account = _new_account()

    async def boom(row):
        raise RuntimeError("quota endpoint is down")

    monkeypatch.setattr(qoder_quota, "fetch_quota", boom)
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", account))

    cached = routing.resource_snapshot(account)
    assert cached == {}
    assert routing.quota_exhausted(account) is False


def test_upstream_refusal_does_not_invent_zero_on_a_slow_read(isolated_db, monkeypatch):
    account = _new_account()

    async def slow(row):
        await asyncio.sleep(5)

    monkeypatch.setattr(qoder_quota, "fetch_quota", slow)
    monkeypatch.setattr(qoder_chat, "QUOTA_REFUSAL_FETCH_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", account))
    # The refusal is already the answer: the bounded read must not hold it open.
    assert time.monotonic() - started < 1.0
    assert routing.resource_snapshot(account) == {}
    assert routing.quota_exhausted(account) is False


def test_upstream_refusal_caches_the_live_reading(isolated_db, monkeypatch):
    account = _new_account()

    class Snapshot:
        ok = True
        unit = "credit"
        remaining = 7.5
        unsupported = False
        extra = {}
        message = ""
        channel = "qoder"
        account_id = 0

    async def read(row):
        return Snapshot()

    monkeypatch.setattr(qoder_quota, "fetch_quota", read)
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", account))

    cached = routing.resource_snapshot(account)
    assert cached["remaining"] == 7.5
    assert cached["source"] == "qoder-upstream-quota"
    # A refusal that turns out to have quota left is not a death sentence.
    assert routing.quota_exhausted(account) is False


def test_upstream_refusal_caches_a_measured_zero(isolated_db, monkeypatch):
    account = _new_account()

    class Snapshot:
        ok = True
        unit = "credit"
        remaining = 0.0
        unsupported = False
        extra = {}
        message = ""
        channel = "qoder"
        account_id = 0

    async def read(row):
        return Snapshot()

    monkeypatch.setattr(qoder_quota, "fetch_quota", read)
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", account))

    cached = routing.resource_snapshot(account)
    assert cached["remaining"] == 0.0
    assert cached["source"] == "qoder-upstream-quota"
    assert routing.quota_exhausted(account) is True


def test_upstream_refusal_without_a_number_preserves_unknown(isolated_db, monkeypatch):
    account = _new_account()

    class Snapshot:
        ok = False
        unit = "credit"
        remaining = None
        unsupported = False
        extra = {}
        message = "HTTP 500"
        channel = "qoder"
        account_id = 0

    async def read(row):
        return Snapshot()

    monkeypatch.setattr(qoder_quota, "fetch_quota", read)
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", account))

    assert routing.resource_snapshot(account) == {}
    assert routing.quota_exhausted(account) is False


def test_upstream_refusal_without_an_account_id_is_a_no_op(isolated_db):
    asyncio.run(qoder_chat.mark_quota_exhausted("qoder", {}))
    assert routing.resource_snapshot({}) == {}
