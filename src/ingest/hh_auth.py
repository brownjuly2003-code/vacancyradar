from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests


HH_TOKEN_URL = "https://api.hh.ru/token"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str | None
    token_type: str

    @classmethod
    def from_payload(cls, payload: dict) -> "TokenResponse":
        return cls(
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 0)),
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
        )


def fetch_client_credentials_token(
    client_id: str,
    client_secret: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> TokenResponse:
    sess = session or requests.Session()
    r = sess.post(
        HH_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return TokenResponse.from_payload(r.json())


def upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Replace `key=...` line in .env-style file or append it."""
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    env_path.write_text(text, encoding="utf-8")
