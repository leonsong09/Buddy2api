"""Cache the quota deadlines that providers already see.

``routing.urgency_days`` reads ``db.get_account_resource_cache`` through
``routing.resource_snapshot``, but until now only the workbuddy resource flow
ever wrote that row: a channel that reported its expiry in a check-in response
kept the deadline on the screen that triggered the request and lost it right
after. This module is the single place where "the upstream said this expires at
T" turns into a cached payload, so every caller writes the same keys.

Only observed facts are written. An unknown expiry must not invent a deadline,
and a quota number must not be guessed: ``routing.quota_exhausted`` drops an
account out of its channel pool as soon as a cached ``remaining`` reaches zero.

Both writers here are *partial* observations and therefore both go through
``db.merge_account_resource_cache``: ``store_quota_expiry`` records a deadline a
check-in revealed, ``store_quota_reading`` records a balance a quota check
actually measured. Neither may erase the other's keys.
"""

from __future__ import annotations

import time

import database as db

DAY_SECONDS = 86400.0


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def store_quota_expiry(account, expires_at=None, remaining=None, peak=None, source: str = "") -> dict:
    """Cache one account's quota deadline and, when known, its remaining quota.

    ``expires_at`` is an absolute unix timestamp (seconds). One timestamp is
    stored under three names because the readers disagree: ``expires_at`` and
    ``next_expire_days`` are what ``routing.urgency_days`` consumes, while
    ``next_expire_ts`` is the name ``auth_manager`` gives the same fact.

    Passing ``remaining`` (or ``peak``) writes the quota keys too; a value at or
    below zero is written as given and therefore makes routing treat the account
    as exhausted, so callers must only pass a number they actually observed.

    RED LINE — never hand this function the balance of a daily bucket or of a
    single grant as ``remaining``. ``routing.quota_exhausted`` drops an account
    from its channel pool the moment a cached ``remaining`` reads zero, and those
    numbers reach zero every day by design (a spent daily wallet, a consumed
    100-credit check-in reward). Only a figure describing the account's total
    quota belongs here; when in doubt leave ``remaining`` as ``None`` and cache
    the deadline alone. Do not "helpfully" fill it in later.

    ``next_expire_days`` is deliberately a ``float`` and must not become an
    ``int``: routing reads that key before ``expires_at``, so whole-day
    truncation would turn "expires in 11 hours" into "expires now" and erase the
    difference between two daily buckets. Keep the fraction.

    Returns the stored payload, or ``{}`` when there was nothing to record or
    nothing changed.
    """
    account_id = account.get("id") if isinstance(account, dict) else None
    if not account_id:
        return {}

    now = time.time()
    expires = _number(expires_at)
    if expires is not None and expires <= now:
        # An expiry that already passed is not a deadline; leave the rest of the
        # pipeline's knowledge about this account untouched.
        expires = None

    payload: dict = {"ok": True}
    observed = False
    if source:
        payload["source"] = source
    if expires is not None:
        # Routing reads next_expire_days before expires_at, so keep the fraction
        # of a day: rounding to whole days would collapse "expires in 11 hours"
        # (a daily wallet bucket) into "expires now" and flatten the urgency
        # factor. Do NOT turn this into an int. Three decimals also stop a fresh
        # 30-day grant from reading as 29.999.
        payload["next_expire_ts"] = expires
        payload["expires_at"] = expires
        payload["next_expire_days"] = max(0.0, round((expires - now) / DAY_SECONDS, 3))
        observed = True
    remaining_value = _number(remaining)
    if remaining_value is not None:
        payload["remaining"] = remaining_value
        payload["available_total"] = remaining_value
        # Stamp when *this* number was seen. The row `updated_at` below moves on
        # every partial write (an expiry-only check-in included), and the gate
        # ages the reading by this stamp, not by the row.
        payload["remaining_observed_at"] = int(now)
        observed = True
    peak_value = _number(peak)
    if peak_value is not None:
        payload["peak_remaining"] = peak_value
        observed = True
    if not observed:
        # Do not replace a richer payload (packages, totals) with an empty one.
        return {}
    payload["updated_at"] = int(now)
    # Merge: this is one observation, not a full snapshot. Replacing the row
    # would erase whatever else is known (the package totals a workbuddy sync
    # stored, the deadline a check-in stored).
    db.merge_account_resource_cache(int(account_id), payload)
    return payload


def store_quota_reading(account, remaining=None, peak=None, source: str = "quota-check") -> dict:
    """Cache one *measured* quota balance so routing can act on it.

    Routing only ever reads the cache, while a quota check sees the live number
    and used to throw it away: the accounts page reported "0 credits" for an
    account whose cached payload had no ``remaining`` key at all, so the row
    stayed first in its channel pool. This is the entry point that closes that
    gap — pass the figure the upstream just reported, with ``source`` naming the
    caller that saw it.

    The value is stored together with the moment it was observed
    (``remaining_observed_at``) so callers can show how old the number is. The
    stamp is *not* a freshness gate: ``routing.quota_exhausted`` treats a zero as
    proof with no expiry, so a drained account stays out of rotation until
    something writes a newer reading — the user's manual refresh, or the next
    request that gets refused upstream and measures the account again.

    RED LINE — identical to ``store_quota_expiry``: only a figure describing the
    account's *total* spendable quota belongs here. Never the balance of a
    per-day wallet, never a single grant, never a number this code inferred
    instead of read. Those hit zero by design (every day, or every claim) and
    would park a healthy account. When in doubt, write nothing.

    Returns the merged payload, or ``{}`` when there was nothing to record.
    """
    account_id = account.get("id") if isinstance(account, dict) else None
    if not account_id:
        return {}

    now = time.time()
    payload: dict = {"ok": True}
    if source:
        payload["source"] = source
    observed = False
    remaining_value = _number(remaining)
    if remaining_value is not None:
        payload["remaining"] = remaining_value
        payload["available_total"] = remaining_value
        payload["remaining_observed_at"] = int(now)
        observed = True
    peak_value = _number(peak)
    if peak_value is not None:
        payload["peak_remaining"] = peak_value
        observed = True
    if not observed:
        # Nothing was measured; do not stamp the row as if something had been.
        # (A non-empty ``source`` must not count as an observation.)
        return {}
    payload["updated_at"] = int(now)
    db.merge_account_resource_cache(int(account_id), payload)
    return payload
