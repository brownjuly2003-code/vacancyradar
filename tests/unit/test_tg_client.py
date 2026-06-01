from __future__ import annotations

from datetime import datetime, timezone

import pytest

from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)

from src.ingest import tg_client
from src.ingest.tg_client import TGSessionError, fetch_channel_messages, open_session


class FakeMessage:
    def __init__(self, message_id: int, text: str | None, date: datetime, views: int | None = None):
        self.id = message_id
        self.text = text
        self.date = date
        self.views = views


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def iter_messages(self, channel_username, limit=500, offset_date=None):
        assert channel_username == "jobs"
        assert limit == 10
        assert offset_date is None
        yield from self.messages


def test_fetch_channel_messages_filters_text_only(monkeypatch):
    monkeypatch.setattr(tg_client, "Message", FakeMessage)
    messages = [
        FakeMessage(1, None, datetime(2026, 4, 28, tzinfo=timezone.utc)),
        FakeMessage(2, "", datetime(2026, 4, 28, tzinfo=timezone.utc)),
        FakeMessage(3, "Data analyst", datetime(2026, 4, 28, tzinfo=timezone.utc), views=42),
    ]

    out = fetch_channel_messages(FakeClient(messages), "jobs", limit=10)

    assert len(out) == 1
    assert out[0].message_id == 3
    assert out[0].text == "Data analyst"
    assert out[0].views == 42


def test_fetch_channel_messages_normalizes_to_utc(monkeypatch):
    monkeypatch.setattr(tg_client, "Message", FakeMessage)
    messages = [FakeMessage(1, "Backend", datetime(2026, 4, 28, 12, 30))]

    out = fetch_channel_messages(FakeClient(messages), "jobs", limit=10)

    assert out[0].date.tzinfo == timezone.utc
    assert out[0].date == datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc)


def test_open_session_raises_when_unauthorized(monkeypatch):
    class UnauthorizedClient:
        def __init__(self):
            self.disconnected = False

        def connect(self):
            return None

        def is_user_authorized(self):
            return False

        def disconnect(self):
            self.disconnected = True

    client = UnauthorizedClient()
    monkeypatch.setattr(tg_client, "_build_client", lambda session_path=None: client)

    with pytest.raises(TGSessionError, match="не авторизована"):
        open_session()

    assert client.disconnected is True


class _ConnectRaiserClient:
    """Test double whose connect() raises a configurable Telethon error."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.disconnected = False

    def connect(self):
        raise self.exc

    def is_user_authorized(self):  # pragma: no cover — connect() raised first
        return True

    def disconnect(self):
        self.disconnected = True


def _make_telethon_error(cls):
    """Construct a Telethon RPCError-subclass without invoking the RPC call site.
    Several Telethon error classes require an internal `request` arg in their
    __init__; for the unit tests we only care that the type matches the
    `except` arm, so we synthesize an instance via __new__."""
    return cls.__new__(cls)


@pytest.mark.parametrize(
    "error_cls, fragment",
    [
        (AuthKeyUnregisteredError, "key revoked"),
        (AuthKeyDuplicatedError, "used elsewhere"),
        (UserDeactivatedBanError, "banned"),
        (SessionPasswordNeededError, "2FA"),
    ],
)
def test_open_session_translates_telethon_auth_errors(monkeypatch, error_cls, fragment):
    err = _make_telethon_error(error_cls)
    client = _ConnectRaiserClient(err)
    monkeypatch.setattr(tg_client, "_build_client", lambda session_path=None: client)

    with pytest.raises(TGSessionError, match=fragment):
        open_session()

    assert client.disconnected is True


def test_tg_session_error_is_runtime_error_subclass():
    # Daily refresh treats RuntimeError-like exceptions as fatal; keeping the
    # subclass relation lets existing callers that catch RuntimeError still see
    # the new exception, while new callers can branch on TGSessionError.
    assert issubclass(TGSessionError, RuntimeError)


# ---------------------------------------------------------------------------
# Coverage gaps: _build_client body (40-44), non-Message skip (57),
# open_session happy path return (115).
# ---------------------------------------------------------------------------


def test_build_client_reads_env_and_constructs_telegram_client(monkeypatch):
    """`_build_client` берёт TG_API_ID/HASH/SESSION из env и зовёт TelegramClient
    (lines 40-44). Замокаем TelegramClient чтобы не trigger'нуть Telethon init."""
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abcdef1234567890abcdef1234567890")
    monkeypatch.setenv("TG_SESSION", "custom_session_name")

    captured: dict = {}

    class _FakeTelethon:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["session"] = session
            captured["api_id"] = api_id
            captured["api_hash"] = api_hash

    monkeypatch.setattr(tg_client, "TelegramClient", _FakeTelethon)

    from pathlib import Path

    result = tg_client._build_client(Path("vradar_session.session"))
    assert isinstance(result, _FakeTelethon)
    assert captured["api_id"] == 12345
    assert captured["api_hash"] == "abcdef1234567890abcdef1234567890"
    # session_path передан → строковое представление пути.
    assert "vradar_session" in captured["session"]


def test_build_client_falls_back_to_env_session_name(monkeypatch):
    """`session_path` не передан → используется TG_SESSION из env (line 42-43)."""
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.delenv("TG_SESSION", raising=False)

    captured: dict = {}

    class _FakeTelethon:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["session"] = session

    monkeypatch.setattr(tg_client, "TelegramClient", _FakeTelethon)

    tg_client._build_client()
    # Default session name == "vradar_session".
    assert captured["session"] == "vradar_session"


def test_build_client_uses_bounded_batch_settings(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")

    captured: dict = {}

    class _FakeTelethon:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(tg_client, "TelegramClient", _FakeTelethon)

    tg_client._build_client()

    assert captured["kwargs"] == {
        "auto_reconnect": False,
        "connection_retries": 2,
        "raise_last_call_error": True,
        "receive_updates": False,
        "request_retries": 2,
        "retry_delay": 2,
    }


def test_fetch_channel_messages_skips_non_message_objects(monkeypatch):
    """iter_messages может выдать Service-объекты (status updates, edits) которые
    не instanceof Message → пропускаются (line 56-57)."""
    monkeypatch.setattr(tg_client, "Message", FakeMessage)

    class _ServiceUpdate:
        """Не Message — должна быть отфильтрована на isinstance check (line 57)."""

        id = 0
        text = "looks like text"
        date = datetime(2026, 4, 28, tzinfo=timezone.utc)

    messages = [
        _ServiceUpdate(),
        FakeMessage(7, "Real vacancy", datetime(2026, 4, 28, tzinfo=timezone.utc)),
    ]

    out = fetch_channel_messages(FakeClient(messages), "jobs", limit=10)
    assert len(out) == 1
    assert out[0].message_id == 7


def test_open_session_returns_authorized_client(monkeypatch):
    """Happy path: connect OK + is_user_authorized() True → return client (line 115)."""

    class AuthorizedClient:
        def __init__(self):
            self.disconnected = False
            self.connected = False

        def connect(self):
            self.connected = True

        def is_user_authorized(self):
            return True

        def disconnect(self):  # pragma: no cover — не должно вызваться на happy path
            self.disconnected = True

    client = AuthorizedClient()
    monkeypatch.setattr(tg_client, "_build_client", lambda session_path=None: client)

    result = open_session()
    assert result is client
    assert client.connected is True
    assert client.disconnected is False
