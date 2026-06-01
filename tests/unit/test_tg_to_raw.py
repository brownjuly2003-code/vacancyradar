from __future__ import annotations

from datetime import datetime, timezone

from src.ingest.tg_client import TGMessage
from src.transform.tg_to_raw import tg_message_to_slim_row


def _message(text: str, message_id: int = 42) -> TGMessage:
    return TGMessage(
        channel="ch",
        message_id=message_id,
        date=datetime(2026, 4, 28, 12, tzinfo=timezone.utc),
        text=text,
        views=None,
    )


def test_vacancy_id_stable_for_same_channel_message_id():
    first = tg_message_to_slim_row(_message("Data analyst"))
    second = tg_message_to_slim_row(_message("Backend engineer"))

    assert first["vacancy_id"] == second["vacancy_id"] == "tg:ch:42"


def test_salary_parsed_from_text():
    row = tg_message_to_slim_row(_message("Data analyst\nЗП 200-300к ₽"))

    assert row["salary_rub_min"] == 200_000
    assert row["salary_rub_max"] == 300_000
    assert row["salary_disclosed"] is True


def test_remote_type_default_unknown_for_short_text():
    row = tg_message_to_slim_row(_message("Data analyst"))

    assert row["remote_type"] == "unknown"


def test_source_url_format():
    row = tg_message_to_slim_row(_message("Data analyst", message_id=42))

    assert row["source_url"] == "https://t.me/ch/42"


def test_title_skips_leading_empty_lines():
    """Title extractor пропускает пустые/whitespace-only строки в начале."""
    row = tg_message_to_slim_row(_message("\n   \nData engineer\nЗП 200к"))

    assert row["title"] == "Data engineer"


def test_title_untitled_fallback_on_whitespace_only():
    """Если весь text пустой/whitespace — fallback `(untitled)`."""
    row = tg_message_to_slim_row(_message("   \n  \n\n"))

    assert row["title"] == "(untitled)"
