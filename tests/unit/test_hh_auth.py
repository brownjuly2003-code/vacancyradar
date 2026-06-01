from __future__ import annotations

from pathlib import Path

import pytest

from src.ingest.hh_auth import (
    TokenResponse,
    fetch_client_credentials_token,
    upsert_env_var,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.last_call: dict | None = None

    def post(self, url, data=None, timeout=None):
        self.last_call = {"url": url, "data": dict(data or {}), "timeout": timeout}
        return self.response


def test_fetch_token_posts_client_credentials():
    payload = {
        "access_token": "abc123",
        "expires_in": 86400,
        "refresh_token": "ref456",
        "token_type": "Bearer",
    }
    sess = FakeSession(FakeResponse(200, payload))

    token = fetch_client_credentials_token("cid", "cs", session=sess)

    assert isinstance(token, TokenResponse)
    assert token.access_token == "abc123"
    assert token.refresh_token == "ref456"
    assert sess.last_call["url"].endswith("/token")
    assert sess.last_call["data"] == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "cs",
    }


def test_fetch_token_raises_on_4xx():
    import requests

    sess = FakeSession(FakeResponse(401, {"error": "invalid_client"}))

    with pytest.raises(requests.HTTPError):
        fetch_client_credentials_token("bad", "bad", session=sess)


def test_upsert_env_appends_when_key_missing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FOO=1\nBAR=2\n", encoding="utf-8")

    upsert_env_var(env, "HH_ACCESS_TOKEN", "tok")

    text = env.read_text(encoding="utf-8")
    assert "HH_ACCESS_TOKEN=tok" in text
    assert "FOO=1" in text and "BAR=2" in text


def test_upsert_env_appends_after_final_line_without_newline(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FOO=1", encoding="utf-8")

    upsert_env_var(env, "HH_ACCESS_TOKEN", "tok")

    assert env.read_text(encoding="utf-8") == "FOO=1\nHH_ACCESS_TOKEN=tok\n"


def test_upsert_env_replaces_existing_key(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FOO=1\nHH_ACCESS_TOKEN=old\nBAR=2\n", encoding="utf-8")

    upsert_env_var(env, "HH_ACCESS_TOKEN", "new")

    text = env.read_text(encoding="utf-8")
    assert "HH_ACCESS_TOKEN=new" in text
    assert "HH_ACCESS_TOKEN=old" not in text
    assert text.count("HH_ACCESS_TOKEN=") == 1


def test_upsert_env_creates_file_if_missing(tmp_path: Path):
    env = tmp_path / "fresh.env"
    upsert_env_var(env, "K", "v")
    assert env.read_text(encoding="utf-8") == "K=v\n"
