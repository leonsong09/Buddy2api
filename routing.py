"""Account routing with explicit priority, channel precedence and weight.

User preferences may keep traffic on one account; rate and expiry readings are
for display only and never force redistribution. Measured zero quota removes
an account until a positive reading is stored, without disabling check-in.
Unknown quota remains eligible; free models may bypass only the quota gate.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Optional

import database as db

# Quota is read from the same cache the management page fills, so routing never
# blocks a request on an upstream billing round-trip.
DEFAULT_URGENCY_DAYS = 9999.0

# Channels whose quota is granted per day and therefore expires within the day.
# "Spend it before midnight" is the strongest urgency there is — but only loomy
# really resets daily. Every other channel keeps its quota until a real
# deadline (which the official "expiring soon" snapshot reports) or has no
# deadline at all, and "no deadline" must never be dressed up as "daily".
#
# Display only: expiry no longer orders anything (see urgency_days).
DAILY_QUOTA_URGENCY = 0.5

DEFAULT_CHANNEL_URGENCY: dict[str, float] = {
    "loomy": DAILY_QUOTA_URGENCY,
    "workbuddy": DEFAULT_URGENCY_DAYS,  # real data: next_expire_days from billing
    "workbuddy-i": DEFAULT_URGENCY_DAYS,
    "qoder": DEFAULT_URGENCY_DAYS,
    "qoder-i": DEFAULT_URGENCY_DAYS,
    "qwenwork": DEFAULT_URGENCY_DAYS,
    "traework": DEFAULT_URGENCY_DAYS,
    "qclaw": DEFAULT_URGENCY_DAYS,
    "xiaohuanxiong": DEFAULT_URGENCY_DAYS,  # points do not expire daily
}

DEFAULT_SETTINGS = {
    # Legacy diagnostic settings, retained for API compatibility only.
    "routing_account_rpm": 60,
    "routing_account_inflight": 4,
    # Requests started within this window count as "still in flight". Providers
    # do not report completion back to the router, so concurrency is measured
    # as a short recent-arrival window instead of a paired acquire/release.
    "routing_inflight_window_seconds": 15,
    # This threshold labels low balances for display; it never demotes accounts.
    "routing_low_watermark_pct": 5.0,
    # Per-channel urgency overrides, e.g. {"xiaohuanxiong": 0.5}.
    "routing_channel_urgency": {},
}


# ---------------------------------------------------------------- settings


def _setting(key: str):
    default = DEFAULT_SETTINGS.get(key)
    try:
        value = db.get_setting(key, default)
    except Exception:
        return default
    if value is None or value == "":
        return default
    return value


def _setting_int(key: str) -> int:
    try:
        return max(0, int(float(_setting(key))))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[key])


def _setting_float(key: str) -> float:
    try:
        return float(_setting(key))
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS[key])


def account_rpm_cap() -> int:
    """Max requests per account per minute. 0 disables the cap."""
    return _setting_int("routing_account_rpm")


def account_inflight_cap() -> int:
    """Max near-simultaneous requests per account. 0 disables the cap."""
    return _setting_int("routing_account_inflight")


def inflight_window_seconds() -> float:
    return max(1.0, _setting_float("routing_inflight_window_seconds"))


def low_watermark_pct() -> float:
    value = _setting_float("routing_low_watermark_pct")
    return max(0.0, min(100.0, value))


def channel_urgency_map() -> dict[str, float]:
    raw = _setting("routing_channel_urgency")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return out


def routing_settings() -> dict:
    """Everything the management page edits, in one payload."""
    return {
        "routing_account_rpm": account_rpm_cap(),
        "routing_account_inflight": account_inflight_cap(),
        "routing_inflight_window_seconds": inflight_window_seconds(),
        "routing_low_watermark_pct": low_watermark_pct(),
        "routing_channel_urgency": channel_urgency_map(),
    }


# ------------------------------------------------------------------ quota


def resource_snapshot(account: dict) -> dict:
    """Cached quota reading for an account, or {} when nothing is known."""
    aid = account.get("id")
    if not aid:
        return {}
    try:
        cached = db.get_account_resource_cache(int(aid))
    except Exception:
        return {}
    return cached if isinstance(cached, dict) else {}


def _first_number(*values) -> Optional[float]:
    for value in values:
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        return number
    return None


def _snapshot_remaining(snapshot: dict) -> Optional[float]:
    """Remaining quota inside a cached resource payload. None means unknown."""
    if not snapshot:
        return None
    if snapshot.get("unsupported") or snapshot.get("ok") is False:
        return None
    return _first_number(
        snapshot.get("remaining"),
        snapshot.get("available_total"),
        snapshot.get("total_dosage"),
    )


def remaining_quota(account: dict) -> Optional[float]:
    """Remaining quota from the cache. None means unknown, not zero."""
    return _snapshot_remaining(resource_snapshot(account))


def _peak_quota(account: dict) -> Optional[float]:
    snapshot = resource_snapshot(account)
    peak = _first_number(snapshot.get("peak_remaining"))
    if peak and peak > 0:
        return peak
    current = remaining_quota(account)
    return current if current and current > 0 else None


def quota_exhausted(account: dict) -> bool:
    """True when the quota is *provably* gone.

    One way a reading is not proof: an unknown one (nothing cached, an
    unsupported channel, a failed billing call) keeps the account in the pool,
    because a billing outage must not empty the pool.

    A zero is proof, and it does not expire. There is no age check here on
    purpose: a zero stays a zero until something writes a newer reading — the
    user's manual refresh, or the next request that gets refused upstream and
    measures the account again. Ageing a zero back into "unknown" would put a
    drained account back into rotation on its own, which is precisely what the
    operator said not to do ("0就是没额度……直到用户自己刷新").
    """
    remaining = _snapshot_remaining(resource_snapshot(account))
    if remaining is None:
        return False
    return remaining <= 0


def quota_low(account: dict) -> bool:
    """True when the account is close to empty.

    Diagnostic only -- it contributes no sort key. An account whose quota is
    provably gone already leaves the pool through ``quota_exhausted`` /
    ``account_available``, so demoting a merely-low account contradicted the
    drain-first order (spend the account that dies soonest, first). Kept because
    ``quota_state`` and the admin preview still report it.
    """
    watermark = low_watermark_pct()
    if watermark <= 0:
        return False
    remaining = remaining_quota(account)
    if remaining is None:
        return False
    if remaining <= 0:
        return True
    peak = _peak_quota(account)
    if not peak or peak <= 0:
        return False
    return (remaining / peak) * 100.0 < watermark


def quota_state(account: dict) -> str:
    if quota_exhausted(account):
        return "exhausted"
    if quota_low(account):
        return "low"
    return "ok"


# -------------------------------------------------------------- urgency


def urgency_days(account: dict, channel: Optional[str] = None) -> float:
    """How soon this account's quota stops being usable, in days. **Display only.**

    Reported to the management page ("expiring soon"); it does **not** take part
    in the routing order any more (see ``sort_key``). The upstream reading behind
    it is neither fresh nor reliable — the snapshot only refreshes on a check and
    a channel can answer HTTP 200 with a business error that parses to nothing —
    so expiry stopped being a routing decision.

    Manual value wins, then whatever the official "expiring soon" snapshot
    reports (``next_expire_days`` / ``expires_at`` — the data the management
    page shows), then "this quota resets daily", then the per-channel override
    setting, then "no deadline". Small numbers are more urgent.
    """
    manual = account.get("urgency_days")
    if manual is not None and manual != "":
        try:
            return max(0.0, float(manual))
        except (TypeError, ValueError):
            pass

    resolved_channel = str(channel or account.get("provider") or "")

    snapshot = resource_snapshot(account)
    if snapshot:
        days = _first_number(
            snapshot.get("next_expire_days"),
            snapshot.get("urgency_days"),
        )
        if days is not None:
            return max(0.0, days)
        expires_at = _first_number(snapshot.get("expires_at"))
        if expires_at:
            seconds = expires_at - time.time()
            if seconds > 0:
                return seconds / 86400.0

    default = DEFAULT_CHANNEL_URGENCY.get(resolved_channel, DEFAULT_URGENCY_DAYS)
    if default == DAILY_QUOTA_URGENCY:
        # A daily quota does not expire on a date, it expires at local midnight,
        # so its urgency ticks down through the day instead of sitting still.
        return daily_reset_urgency()

    override = channel_urgency_map().get(resolved_channel)
    if override is not None:
        return override
    return default


def urgency_source(account: dict, channel: Optional[str] = None) -> str:
    """Where the effective urgency came from, for the management page.

    Display only — it labels the "expiring soon" column and orders nothing.
    The values are the historical set: ``manual`` (hand-filled), ``quota``
    (official expiring-soon data), ``daily`` (resets at local midnight),
    ``channel`` (per-channel override setting) and ``default`` (no deadline).
    The page derives its three displays from them: a real number of days
    (manual/quota/channel), "daily", or "unlimited" (default).
    """
    manual = account.get("urgency_days")
    if manual is not None and manual != "":
        return "manual"
    resolved_channel = str(channel or account.get("provider") or "")
    snapshot = resource_snapshot(account)
    if snapshot:
        if _first_number(snapshot.get("next_expire_days"), snapshot.get("urgency_days")) is not None:
            return "quota"
        expires_at = _first_number(snapshot.get("expires_at"))
        if expires_at and expires_at - time.time() > 0:
            return "quota"
    if DEFAULT_CHANNEL_URGENCY.get(resolved_channel, DEFAULT_URGENCY_DAYS) == DAILY_QUOTA_URGENCY:
        return "daily"
    if channel_urgency_map().get(resolved_channel) is not None:
        return "channel"
    return "default"


def daily_reset_urgency(now: Optional[float] = None) -> float:
    """Days until local midnight — the urgency of a daily quota.

    Display only: this value is no longer part of the routing order.
    """
    current = time.time() if now is None else float(now)
    parts = time.localtime(current)
    midnight = time.mktime(
        (parts.tm_year, parts.tm_mon, parts.tm_mday + 1, 0, 0, 0, 0, 0, -1)
    )
    return max(0.0, (midnight - current) / 86400.0)


# -------------------------------------------------------------- rate caps

_rate_lock = threading.Lock()
_rate_windows: dict[int, deque] = {}
_RATE_WINDOW_SECONDS = 60.0
_MAX_TRACKED_ACCOUNTS = 4096


def _prune(window: deque, now: float, span: float) -> None:
    while window and now - window[0] > span:
        window.popleft()


def recent_starts(account_id: int, span: float) -> int:
    if not account_id:
        return 0
    now = time.monotonic()
    with _rate_lock:
        window = _rate_windows.get(account_id)
        if not window:
            return 0
        _prune(window, now, span)
        return len(window)


def rpm_used(account_id: int) -> int:
    return recent_starts(account_id, _RATE_WINDOW_SECONDS)


def note_pick(account_id: Optional[int]) -> None:
    """Record that a request was handed to this account."""
    if not account_id:
        return
    now = time.monotonic()
    with _rate_lock:
        window = _rate_windows.get(account_id)
        if window is None:
            if len(_rate_windows) >= _MAX_TRACKED_ACCOUNTS:
                _rate_windows.clear()
            window = deque()
            _rate_windows[account_id] = window
        window.append(now)
        _prune(window, now, max(_RATE_WINDOW_SECONDS, inflight_window_seconds()))


def reset_rate_windows() -> None:
    """Test hook: forget every recorded pick."""
    with _rate_lock:
        _rate_windows.clear()


# Channels whose requested model the operator marked 放行 in the model
# catalog. The gate below skips the quota reading for them. The set is a
# context value, not a parameter, because a provider picks its account lazily
# inside the stream: by then the dispatch call has returned, and only a value
# that lives as long as the request task is still visible. One task serves one
# request, so it cannot leak into another.
_QUOTA_EXEMPT: contextvars.ContextVar = contextvars.ContextVar(
    "buddy_quota_exempt_channels", default=frozenset()
)


def note_quota_exempt(channels) -> None:
    """Record the channels this request serves 放行 (not reset on purpose)."""
    _QUOTA_EXEMPT.set(frozenset(str(c) for c in (channels or ()) if c))


def quota_exempt_channels() -> frozenset:
    return _QUOTA_EXEMPT.get()


@contextmanager
def quota_exempt_scope(channels):
    """Temporary form, for callers that can scope it (tests, admin previews)."""
    token = _QUOTA_EXEMPT.set(frozenset(str(c) for c in (channels or ()) if c))
    try:
        yield
    finally:
        _QUOTA_EXEMPT.reset(token)


def account_available(account: dict) -> bool:
    """Gate: hard reasons to leave an account out of the pool entirely.

    A zero reading is proof of "no quota" and keeps the account out — unless
    this request's model is marked 放行 on the account's channel, because then
    the balance says nothing about whether that model can be served.
    """
    if str(account.get("provider") or "") in _QUOTA_EXEMPT.get():
        return True
    if quota_exhausted(account):
        return False
    return True


# ------------------------------------------------------------------ order


def priority_of(account: dict) -> int:
    try:
        return int(account.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def weight_of(account: dict) -> int:
    try:
        return max(1, int(account.get("weight") or 1))
    except (TypeError, ValueError):
        return 1


def sort_key(account: dict, channel: Optional[str] = None):
    """Preserve account priority/weight preference, without forced rotation.

    The caller inserts channel precedence after priority. Expiry, rate readings
    and low balances never override the operator's preference for an account.
    """
    weight = weight_of(account)
    try:
        requests = max(0, int(account.get("total_requests") or 0))
    except (TypeError, ValueError):
        requests = 0
    return (-priority_of(account), -weight, requests / weight,
            requests, int(account.get("id") or 0))


def order_accounts(accounts: list[dict], channel: Optional[str] = None) -> list[dict]:
    """Gate + order a channel's accounts. Returned list is the routing order.

    The 放行 exception is not a parameter: it is the context value set by
    ``quota_exempt_scope`` around the caller, because the account is picked
    lazily inside the provider's stream, long after this returns.
    """
    return sorted(
        [a for a in accounts if account_available(a)],
        key=lambda a: sort_key(a, channel),
    )


def static_order_index(
    accounts: list[dict], channel_order: Optional[list[str]] = None
) -> dict[int, int]:
    """Number every account 1..N, whatever state it is in.

    The account list is a table the operator sorts, not a routing view: an
    expired, disabled, cooling-down or out-of-quota account is still a row and
    still needs a position. So nothing here consults the pool, the quota or
    ``account_available`` — a status change must never renumber the table.

    Three static keys, in order:

      1. priority, descending (the operator's own tier)
      2. the account's channel position in ``channel_order``
      3. id, ascending

    A channel missing from ``channel_order`` (disabled, or added later) sorts
    after every listed one and still falls back to the id, so the numbering is
    total and deterministic. ``channel_order`` is passed in because it is the
    router's list (``router._channel_order``) and routing must not import the
    router. Returns ``{account id: 1-based number}``.
    """
    position = {str(cid): index for index, cid in enumerate(channel_order or [])}
    tail = len(position)

    def key(account: dict):
        return (
            -priority_of(account),
            position.get(str(account.get("provider") or ""), tail),
            -weight_of(account),
            int(account.get("id") or 0),
        )

    rows: list[tuple[int, dict]] = []
    for account in accounts:
        try:
            rows.append((int(account["id"]), account))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda pair: key(pair[1]))
    return {aid: number for number, (aid, row) in enumerate(rows, start=1)}


def channel_pool(channel: str) -> list[dict]:
    """Active, quota-available accounts of a channel, in routing order.

    This is the single answer to "can this channel serve a request right now?".
    The model catalog and the router both call it, so a channel is advertised
    exactly when it is routable. Wrap the call in ``quota_exempt_scope`` to keep
    the zero-balance accounts for a model the operator marked 放行.
    """
    try:
        accounts = db.get_active_accounts(channel)
    except Exception:
        return []
    return order_accounts(accounts, channel)


def describe_account(account: dict, rank: int, channel: Optional[str] = None) -> dict:
    """One row of the routing preview."""
    return {
        "rank": rank,
        "id": account.get("id"),
        "name": account.get("name") or "",
        "channel": channel or account.get("provider") or "",
        "priority": priority_of(account),
        "urgency_days": round(urgency_days(account, channel), 4),
        "urgency_source": urgency_source(account, channel),
        "quota_state": quota_state(account),
        "remaining": remaining_quota(account),
        "rpm_used": rpm_used(int(account.get("id") or 0)),
        "rpm_cap": account_rpm_cap(),
    }
