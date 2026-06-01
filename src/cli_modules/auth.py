"""`vradar auth {hh,tg}` impls. Extracted from src/cli.py (Kimi P1-1)."""
from __future__ import annotations

import argparse
import sys


def _auth(args: argparse.Namespace) -> int:
    if args.provider == "tg":
        return _auth_tg(args)
    if args.provider != "hh":
        print(f"[err] unknown provider: {args.provider}", file=sys.stderr)
        return 1
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    from src.ingest.hh_auth import fetch_client_credentials_token, upsert_env_var

    load_dotenv()
    cid = args.client_id or os.environ.get("HH_CLIENT_ID")
    cs = args.client_secret or os.environ.get("HH_CLIENT_SECRET")
    if not cid or not cs:
        print(
            "[err] need --client-id и --client-secret (или HH_CLIENT_ID / HH_CLIENT_SECRET в .env).\n"
            "      Получи их на https://dev.hh.ru/admin → Создать приложение",
            file=sys.stderr,
        )
        return 2
    print(f"[auth] requesting hh.ru token for client_id={cid[:6]}...")
    tok = fetch_client_credentials_token(cid, cs)
    env_path = Path(".env")
    upsert_env_var(env_path, "HH_ACCESS_TOKEN", tok.access_token)
    if tok.refresh_token:
        upsert_env_var(env_path, "HH_REFRESH_TOKEN", tok.refresh_token)
    expires_days = tok.expires_in // 86400
    print(
        f"[auth] HH_ACCESS_TOKEN written to {env_path} (expires in {expires_days}d). "
        f"Run `python -m src.cli ingest hh --pages 1` to verify."
    )
    return 0


def _auth_tg(args: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv
    from telethon.sync import TelegramClient

    load_dotenv()
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    phone = args.phone or os.environ.get("TG_PHONE")
    if not phone:
        print("[err] нужен --phone или TG_PHONE в .env", file=sys.stderr)
        return 2
    session_name = os.environ.get("TG_SESSION", "vradar_session")
    client = TelegramClient(session_name, api_id, api_hash)
    client.start(phone=phone)
    me = client.get_me()
    client.disconnect()
    print(
        f"[auth-tg] OK: signed in as {me.username or me.first_name} ({me.id}). "
        f"Session saved to {session_name}.session"
    )
    return 0
