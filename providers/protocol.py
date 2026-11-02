"""Provider protocol types for Buddy2api 2.0 channel isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

ChannelId = Literal[
    "workbuddy",
    "workbuddy-i",
    "qclaw",
    "qwenwork",
    "qoderwork",
    "qoder",
    "qoder-i",
    "traework",
    "loomy",
    "xiaohuanxiong",
]

KNOWN_CHANNEL_IDS: tuple[ChannelId, ...] = (
    "workbuddy",
    "workbuddy-i",
    "qclaw",
    "qwenwork",
    "qoderwork",
    "qoder",
    "qoder-i",
    "traework",
    "loomy",
    "xiaohuanxiong",
)
KNOWN_CHANNEL_SET = frozenset(KNOWN_CHANNEL_IDS)
# QClaw stopped operating. Credits moved to WorkBuddy. Kept as a known id so
# old env lists and stored keys fail with a retired message instead of a crash.
RETIRED_CHANNEL_IDS = frozenset({"qclaw"})
RETIRED_CHANNEL_MESSAGE = (
    "QClaw is retired. Credits moved to WorkBuddy; switch this API key to workbuddy."
)


class CheckinUnsupported(Exception):
    """Channel has no daily check-in claim API."""


class UnknownChannel(Exception):
    def __init__(self, channel: str, message: str = ""):
        self.channel = channel
        super().__init__(message or f"Unknown or disabled channel '{channel}'")


@dataclass(frozen=True)
class BindResult:
    """One dispatch hop: which channel serves, and what it is asked for.

    ``original`` is the model id the client sent (logged as ``model``),
    ``inner`` the bare id the channel's catalog is keyed by, and ``channel``
    the provider that will translate and spend an account. The name carries no
    routing weight any more — admission already judged it — so a hop is purely
    descriptive.
    """

    channel: ChannelId
    inner: str
    original: str


@dataclass(frozen=True)
class DiscoveredFile:
    channel: ChannelId
    path: str
    valid: bool
    reason: str
    account_name: str
    uid_masked: str
    already_imported: bool
    extra_preview: dict = field(default_factory=dict)


@dataclass
class DiscoverResult:
    dirs: list[dict]
    files: list[DiscoveredFile]
    file_count: int
    valid_count: int
    importable_count: int
    preview_token: str = ""


@dataclass
class QuotaSnapshot:
    ok: bool
    channel: ChannelId
    account_id: int
    unit: str  # "credit" | "token" | "unknown"
    remaining: float | None
    extra: dict = field(default_factory=dict)
    unsupported: bool = False
    message: str = ""


@runtime_checkable
class Provider(Protocol):
    id: ChannelId
    display_name: str
    checkin_supported: bool

    def list_models(self) -> list[dict]: ...

    def alias_map(self) -> dict[str, str]: ...

    def accepts_model(self, inner: str) -> bool: ...

    def translate_model(self, model: str) -> str: ...

    def pick_account(self, exclude_ids: set[int] | None = None) -> dict | None: ...

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> dict | None: ...

    async def has_usable_account(self) -> bool: ...

    async def chat_completions(
        self, payload: dict, api_key_info: dict | None
    ) -> tuple: ...
