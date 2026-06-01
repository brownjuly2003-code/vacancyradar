from __future__ import annotations

from src.ingest.tg_client import TGMessage
from src.ingest.tg_parse import parse_city, parse_remote_type, parse_salary, parse_seniority


def tg_message_to_slim_row(msg: TGMessage) -> dict:
    """TG message → slim_active-совместимая dict."""
    text = msg.text
    salary = parse_salary(text)
    city = parse_city(text)
    remote = parse_remote_type(text)
    seniority = parse_seniority(text)
    return {
        "vacancy_id": f"tg:{msg.channel}:{msg.message_id}",
        "source": "tg",
        "title": _extract_title(text),
        "employer_name": None,
        "employer_id": None,
        "city": city,
        "remote_type": remote,
        "seniority": seniority,
        "skills": [],
        "salary_rub_min": salary.min,
        "salary_rub_max": salary.max,
        "salary_disclosed": salary.disclosed,
        "salary_currency": salary.currency,
        "posted_at": msg.date,
        "description_teaser": text[:500],
        "source_url": f"https://t.me/{msg.channel}/{msg.message_id}",
    }


def _extract_title(text: str) -> str:
    """Title heuristic: первая непустая строка, обрезанная до 200 chars."""
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line[:200]
    return "(untitled)"
