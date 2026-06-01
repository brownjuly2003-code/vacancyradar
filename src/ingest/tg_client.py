"""Public Telegram channel ingest для VacancyRadar.

Использует MTProto через Telethon. Креды (api_id/hash/phone) в .env, session-файл
`vradar_session.session` создаётся при первом auth (см. CLI команду `auth tg`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)
from telethon.sync import TelegramClient
from telethon.tl.types import Message


class TGSessionError(RuntimeError):
    """Telethon session is unusable (revoked, banned, 2FA needed, etc.).
    Distinct from transient channel errors so daily_refresh can surface
    auth issues separately and operators don't keep getting zero-message
    runs for days. KM audit 2026-05-17 P1."""


@dataclass(frozen=True)
class TGMessage:
    channel: str
    message_id: int
    date: datetime
    text: str
    views: int | None


def _build_client(session_path: Path | None = None) -> TelegramClient:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_name = os.environ.get("TG_SESSION", "vradar_session")
    session = str(session_path or session_name)
    return TelegramClient(
        session,
        api_id,
        api_hash,
        auto_reconnect=False,
        connection_retries=2,
        raise_last_call_error=True,
        receive_updates=False,
        request_retries=2,
        retry_delay=2,
    )


def fetch_channel_messages(
    client: TelegramClient,
    channel_username: str,
    limit: int = 500,
    offset_date: datetime | None = None,
) -> list[TGMessage]:
    """Pull last `limit` messages из публичного канала."""
    out: list[TGMessage] = []
    for msg in client.iter_messages(channel_username, limit=limit, offset_date=offset_date):
        if not isinstance(msg, Message):
            continue
        if msg.text is None or msg.text == "":
            continue
        date = msg.date.astimezone(timezone.utc) if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
        out.append(
            TGMessage(
                channel=channel_username,
                message_id=msg.id,
                date=date,
                text=msg.text,
                views=getattr(msg, "views", None),
            )
        )
    return out


def open_session(session_path: Path | None = None) -> TelegramClient:
    """Synchronous open: session-файл уже должен быть авторизован.

    Raises TGSessionError on auth-level problems (revoked session, banned
    account, 2FA required). Caller can catch this distinctly from transient
    channel errors and exit with a specific code so daily_refresh logs
    flag the auth issue instead of reporting OK with zero messages.
    """
    client = _build_client(session_path)
    try:
        client.connect()
    except AuthKeyUnregisteredError as exc:
        client.disconnect()
        raise TGSessionError(
            "TG session key revoked (AuthKeyUnregisteredError). Re-auth: "
            "`python -m src.cli auth tg --phone $TG_PHONE`"
        ) from exc
    except AuthKeyDuplicatedError as exc:
        client.disconnect()
        raise TGSessionError(
            "TG session key used elsewhere (AuthKeyDuplicatedError) — "
            "another process is using vradar_session.session. Re-auth."
        ) from exc
    except UserDeactivatedBanError as exc:
        client.disconnect()
        raise TGSessionError(
            "TG account banned/deactivated (UserDeactivatedBanError). "
            "Cannot recover — needs human attention."
        ) from exc
    except SessionPasswordNeededError as exc:
        client.disconnect()
        raise TGSessionError(
            "TG 2FA password required (SessionPasswordNeededError). "
            "Re-auth interactively: `python -m src.cli auth tg --phone $TG_PHONE`"
        ) from exc

    if not client.is_user_authorized():
        client.disconnect()
        raise TGSessionError(
            "TG session не авторизована. Запусти один раз: "
            "`python -m src.cli auth tg --phone $TG_PHONE`"
        )
    return client
