"""Regression tests for the pinned-hop contract of the one account queue.

Two production bugs motivated these:

* A hop was supposed to own exactly one account, but provider generators are
  lazy: the code that picks the account runs on the first ``__anext__``, which
  happened *after* the ``with pinned_account(...)`` block had exited.  Every
  hop therefore rotated through three accounts of the same channel and retried
  accounts that had just failed.
* Provider level loggers ignored ``info["_resolved_model"]``, so the log
  page showed the alias twice instead of "alias -> real backend model".
"""

import asyncio

import auth_manager
import router


async def _drain(agen):
    chunks = []
    async for chunk in agen:
        chunks.append(chunk)
    return chunks


def test_replay_keeps_the_pin_alive_while_the_stream_runs():
    """The pin must survive into the lazy generator, not just the dispatch."""

    seen = {}

    async def stream():
        account = auth_manager.pinned_for("workbuddy")
        seen["during"] = account and account["id"]
        yield b'data: {"choices": []}\n\n'

    pinned = {"id": 77, "provider": "workbuddy"}

    async def main():
        return await _drain(router._replay([], stream(), pinned))

    chunks = asyncio.run(main())
    assert chunks == [b'data: {"choices": []}\n\n']
    assert seen["during"] == 77
    # ... and the pin is released again once the stream is done.
    assert auth_manager.pinned_for("workbuddy") is None


def test_replay_without_an_account_does_not_pin_anything():
    seen = {}

    async def stream():
        seen["during"] = auth_manager.pinned_for("workbuddy")
        yield b"x"

    asyncio.run(_drain(router._replay([], stream(), None)))
    assert seen["during"] is None


def test_provider_fallback_never_drifts_off_a_pinned_account(monkeypatch):
    """Even the provider's own fallback scan must stop at the pinned hop."""
    import providers
    import providers.qoder as qoder

    rows = [
        {"id": 11, "provider": "qoder", "access_token": "t", "status": "active"},
        {"id": 12, "provider": "qoder", "access_token": "t", "status": "active"},
    ]

    monkeypatch.setattr(qoder, "is_token_expired", lambda account: False)
    monkeypatch.setattr(
        auth_manager.db, "get_active_accounts",
        lambda provider: [row for row in rows if row["provider"] == provider],
    )
    monkeypatch.setattr(
        auth_manager.db, "list_accounts",
        lambda provider=None: [row for row in rows if row["provider"] == provider],
    )
    monkeypatch.setattr(
        auth_manager.db, "get_account",
        lambda aid: next((row for row in rows if row["id"] == aid), None),
    )

    provider = providers.get_provider("qoder")
    with auth_manager.pinned_account(rows[0]):
        picked = asyncio.run(provider.pick_account_with_fallback(set()))
        assert picked["id"] == 11
        # The pinned account already failed on this hop: stop instead of
        # silently drifting to a sibling account.
        assert asyncio.run(provider.pick_account_with_fallback({11})) is None


def test_provider_logs_the_resolved_backend_model(monkeypatch):
    import providers.loomy.chat as loomy_chat

    captured = {}
    monkeypatch.setattr(loomy_chat.db, "record_request", lambda data: captured.update(data))
    monkeypatch.setattr(loomy_chat, "time", loomy_chat.time)

    loomy_chat._log(
        {"id": 7, "name": "buddy proxy", "_resolved_model": "DeepSeek-V4.1-Flash"},
        {"id": 14, "name": "185****1856"},
        "deepseek-v4.1-flash-1017",
        True, 0, 0, 0, "stop", 200, "", loomy_chat.time.time(),
    )
    assert captured["model"] == "deepseek-v4.1-flash-1017"
    assert captured["resolved_model"] == "DeepSeek-V4.1-Flash"


def test_provider_log_falls_back_to_the_alias_without_a_resolution(monkeypatch):
    import providers.loomy.chat as loomy_chat

    captured = {}
    monkeypatch.setattr(loomy_chat.db, "record_request", lambda data: captured.update(data))

    loomy_chat._log(
        {"id": 7, "name": "buddy proxy"},
        {"id": 14, "name": "185****1856"},
        "deepseek-v4.1-flash-1017",
        True, 0, 0, 0, "stop", 200, "", loomy_chat.time.time(),
    )
    assert captured["resolved_model"] == "deepseek-v4.1-flash-1017"
