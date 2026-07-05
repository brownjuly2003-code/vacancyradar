"""Coverage ratchet for src/cli.py: dispatcher fallthroughs, refdata,
auth (hh + tg), publish_slim (--dry + freshness gate), publish_events
(--dry + empty), publish_embeddings, report, _parse_hh_crawl_root,
_ingest_cbr.

These cover the cheapest uncovered blocks in cli.py. Each test isolates
the file system via tmp_path + chdir and mocks the underlying I/O so we
don't touch master/* or the network.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import polars as pl
import pytest

from src import cli


# --------------------------------------------------------------------------- #
# Dispatcher fallthroughs (each returns 1 on unknown subcommand)
# --------------------------------------------------------------------------- #


def test_publish_dispatcher_unknown_target_returns_1():
    assert cli._publish(Namespace(target="bogus")) == 1


@pytest.mark.parametrize(
    ("target", "handler_name", "return_code"),
    [
        ("slim", "_publish_slim", 10),
        ("events", "_publish_events", 11),
        ("weekly", "_publish_weekly", 12),
        ("embeddings", "_publish_embeddings", 13),
        ("hf-mirror", "_publish_hf_mirror", 16),
    ],
)
def test_publish_dispatcher_routes_known_targets(
    monkeypatch, target, handler_name, return_code
):
    import src.cli_modules.publish as publish_module

    seen = {}

    def fake_handler(args):
        seen["args"] = args
        return return_code

    args = Namespace(target=target)
    monkeypatch.setattr(publish_module, handler_name, fake_handler)

    assert cli._publish(args) == return_code
    assert seen["args"] is args


def test_enrich_dispatcher_unknown_kind_returns_1():
    assert cli._enrich(Namespace(kind="bogus")) == 1


def test_ingest_dispatcher_unknown_source_returns_1():
    assert cli._ingest(Namespace(source="bogus")) == 1


@pytest.mark.parametrize(
    ("source", "handler_name", "return_code"),
    [
        ("hh", "_ingest_hh", 20),
        ("hh-crawl", "_ingest_hh_crawl", 21),
        ("telegram", "_ingest_telegram", 22),
        ("cbr", "_ingest_cbr", 23),
    ],
)
def test_ingest_dispatcher_routes_known_sources(
    monkeypatch, source, handler_name, return_code
):
    import src.cli_modules.ingest as ingest_module

    seen = {}

    def fake_handler(args):
        seen["args"] = args
        return return_code

    args = Namespace(source=source)
    monkeypatch.setattr(ingest_module, handler_name, fake_handler)

    assert cli._ingest(args) == return_code
    assert seen["args"] is args


def test_prune_dispatcher_unknown_target_returns_1():
    assert cli._prune(Namespace(target="bogus")) == 1


def test_auth_unknown_provider_returns_1(capsys):
    assert cli._auth(Namespace(provider="bogus")) == 1
    assert "unknown provider: bogus" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _refdata: roles / areas / unknown kind
# --------------------------------------------------------------------------- #


def test_refdata_roles_cached_load_does_not_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    roles_yaml = data_dir / "professional_roles.yaml"
    roles_yaml.write_text(
        "categories:\n"
        "  - name: IT\n"
        "    roles:\n"
        "      - id: 1\n"
        "        name: Backend\n"
        "      - id: 2\n"
        "        name: Frontend\n",
        encoding="utf-8",
    )

    def boom(*_a, **_kw):
        raise AssertionError("network fetch must not happen when refresh=False")

    monkeypatch.setattr("src.ingest.refdata.fetch_professional_roles", boom)

    assert cli._refdata(Namespace(kind="roles", refresh=False)) == 0
    out = capsys.readouterr().out
    assert "1 categories, 2 roles" in out


def test_refdata_roles_refresh_calls_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    fetched = {"hit": 0}

    def fake_fetch_professional_roles():
        fetched["hit"] += 1
        return {"categories": [{"name": "IT", "roles": [{"id": "1", "name": "Backend"}]}]}

    monkeypatch.setattr("src.ingest.refdata.fetch_professional_roles", fake_fetch_professional_roles)

    assert cli._refdata(Namespace(kind="roles", refresh=True)) == 0
    assert fetched["hit"] == 1
    assert (tmp_path / "data" / "professional_roles.yaml").exists()
    assert "1 categories, 1 roles" in capsys.readouterr().out


def test_refdata_areas_refresh_calls_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    fetched = {"hit": 0}

    def fake_fetch_areas():
        fetched["hit"] += 1
        return [
            {
                "id": "113",
                "name": "Россия",
                "areas": [{"id": "1", "name": "Москва", "areas": []}],
            }
        ]

    monkeypatch.setattr("src.ingest.refdata.fetch_areas", fake_fetch_areas)

    assert cli._refdata(Namespace(kind="areas", refresh=True)) == 0
    assert fetched["hit"] == 1
    assert (tmp_path / "data" / "areas.yaml").exists()
    assert "Russia subjects" in capsys.readouterr().out


def test_refdata_areas_cached_load_does_not_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "areas.yaml").write_text(
        "- id: '113'\n"
        "  name: Russia\n"
        "  areas:\n"
        "    - id: '1'\n"
        "      name: Moscow\n"
        "      areas: []\n",
        encoding="utf-8",
    )

    def boom(*_a, **_kw):
        raise AssertionError("network fetch must not happen when refresh=False")

    monkeypatch.setattr("src.ingest.refdata.fetch_areas", boom)

    assert cli._refdata(Namespace(kind="areas", refresh=False)) == 0
    assert "1 Russia subjects" in capsys.readouterr().out


def test_refdata_unknown_kind_returns_1():
    assert cli._refdata(Namespace(kind="bogus", refresh=False)) == 1


# --------------------------------------------------------------------------- #
# _auth hh / tg
# --------------------------------------------------------------------------- #


def test_auth_hh_missing_creds_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    # Override any .env-loaded creds with empty string (dotenv override=False).
    monkeypatch.setenv("HH_CLIENT_ID", "")
    monkeypatch.setenv("HH_CLIENT_SECRET", "")
    rc = cli._auth(Namespace(provider="hh", client_id=None, client_secret=None))
    assert rc == 2
    assert "HH_CLIENT_ID" in capsys.readouterr().err


def test_auth_hh_happy_writes_token(tmp_path, monkeypatch, capsys):
    from src.ingest.hh_auth import TokenResponse

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HH_CLIENT_ID", "")
    monkeypatch.setenv("HH_CLIENT_SECRET", "")

    tok = TokenResponse(
        access_token="ACCESS_xyz",
        refresh_token="REFRESH_xyz",
        expires_in=14 * 86400,
        token_type="Bearer",
    )
    monkeypatch.setattr(
        "src.ingest.hh_auth.fetch_client_credentials_token",
        lambda cid, cs: tok,
    )
    written = {}

    def fake_upsert(path, key, value):
        written[key] = (path, value)

    monkeypatch.setattr("src.ingest.hh_auth.upsert_env_var", fake_upsert)

    rc = cli._auth(
        Namespace(
            provider="hh",
            client_id="CIDxxxx",
            client_secret="CSxxxx",
        )
    )
    assert rc == 0
    assert written["HH_ACCESS_TOKEN"][1] == "ACCESS_xyz"
    assert written["HH_REFRESH_TOKEN"][1] == "REFRESH_xyz"
    assert "expires in 14d" in capsys.readouterr().out


def test_auth_hh_happy_without_refresh_token(tmp_path, monkeypatch, capsys):
    from src.ingest.hh_auth import TokenResponse

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HH_CLIENT_ID", "")
    monkeypatch.setenv("HH_CLIENT_SECRET", "")

    tok = TokenResponse(
        access_token="ACCESS_xyz",
        refresh_token=None,
        expires_in=86400,
        token_type="Bearer",
    )
    monkeypatch.setattr(
        "src.ingest.hh_auth.fetch_client_credentials_token",
        lambda cid, cs: tok,
    )
    written = {}

    def fake_upsert(path, key, value):
        written[key] = (path, value)

    monkeypatch.setattr("src.ingest.hh_auth.upsert_env_var", fake_upsert)

    rc = cli._auth(
        Namespace(
            provider="hh",
            client_id="CIDxxxx",
            client_secret="CSxxxx",
        )
    )
    assert rc == 0
    assert set(written) == {"HH_ACCESS_TOKEN"}
    assert "expires in 1d" in capsys.readouterr().out


def test_auth_dispatches_tg_provider(monkeypatch):
    captured = {}

    def fake_auth_tg(args):
        captured["args"] = args
        return 7

    monkeypatch.setattr("src.cli_modules.auth._auth_tg", fake_auth_tg)

    args = Namespace(provider="tg", phone="+100")

    assert cli._auth(args) == 7
    assert captured["args"] is args


def test_auth_tg_missing_phone_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeef")
    # Override any .env-loaded TG_PHONE with empty (dotenv override=False).
    monkeypatch.setenv("TG_PHONE", "")
    rc = cli._auth_tg(Namespace(phone=None))
    assert rc == 2
    assert "--phone или TG_PHONE" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _parse_hh_crawl_root
# --------------------------------------------------------------------------- #


def test_parse_hh_crawl_root_default_area_zero_depth():
    seg = cli._parse_hh_crawl_root("area=113")
    assert seg.area == 113
    assert seg.professional_role is None
    assert seg.depth == 0


def test_parse_hh_crawl_root_area_only_nonroot_bumps_depth():
    seg = cli._parse_hh_crawl_root("area=1")
    assert seg.area == 1
    assert seg.depth == 1


def test_parse_hh_crawl_root_full_specifier_depth_4():
    seg = cli._parse_hh_crawl_root(
        "area=113,professional_role=10,period=7,schedule=remote"
    )
    assert seg.area == 113
    assert seg.professional_role == 10
    assert seg.period == 7
    assert seg.schedule == "remote"
    assert seg.depth == 4


def test_parse_hh_crawl_root_role_only_depth_2():
    seg = cli._parse_hh_crawl_root("area=113,professional_role=10")
    assert seg.depth == 2


def test_parse_hh_crawl_root_period_only_ignores_empty_parts():
    seg = cli._parse_hh_crawl_root("area=113,,period=7,")
    assert seg.area == 113
    assert seg.professional_role is None
    assert seg.period == 7
    assert seg.schedule is None
    assert seg.depth == 3


def test_parse_hh_crawl_root_missing_equals_raises_systemexit():
    with pytest.raises(SystemExit) as exc:
        cli._parse_hh_crawl_root("area")
    assert "invalid --root part" in str(exc.value)


def test_parse_hh_crawl_root_unknown_key_raises_systemexit():
    with pytest.raises(SystemExit) as exc:
        cli._parse_hh_crawl_root("area=113,bogus=42")
    assert "unknown --root key" in str(exc.value)


# --------------------------------------------------------------------------- #
# _ingest_cbr
# --------------------------------------------------------------------------- #


def test_ingest_cbr_dry_prints_target(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_cbr(Namespace(dry=True))
    assert rc == 0
    assert "cbr_rates.parquet" in capsys.readouterr().out


def test_ingest_cbr_no_rates_returns_4(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.ingest.cbr_rates.fetch_rates", lambda on: [])
    rc = cli._ingest_cbr(Namespace(dry=False))
    assert rc == 4
    assert "no rates returned" in capsys.readouterr().err


def test_ingest_cbr_no_rates_keeps_existing_rates_file(
    tmp_path, monkeypatch, capsys
):
    from datetime import date

    from src.ingest.cbr_rates import CBRRate, write_rates

    monkeypatch.chdir(tmp_path)
    write_rates(
        [CBRRate(date=date(2026, 5, 27), char_code="USD", value=90.0, nominal=1)],
        tmp_path / "master" / "ref" / "cbr_rates.parquet",
    )
    monkeypatch.setattr("src.ingest.cbr_rates.utc_today", lambda: date(2026, 5, 28))
    monkeypatch.setattr("src.ingest.cbr_rates.fetch_rates", lambda on: [])

    rc = cli._ingest_cbr(Namespace(dry=False))

    assert rc == 0
    assert "keeping existing" in capsys.readouterr().err


def test_ingest_cbr_happy_writes_parquet(tmp_path, monkeypatch, capsys):
    from datetime import date

    from src.ingest.cbr_rates import CBRRate

    monkeypatch.chdir(tmp_path)
    on = date(2026, 5, 18)
    rates = [
        CBRRate(date=on, char_code="USD", value=90.0, nominal=1),
        CBRRate(date=on, char_code="EUR", value=100.0, nominal=1),
        CBRRate(date=on, char_code="CNY", value=13.0, nominal=1),
    ]
    monkeypatch.setattr("src.ingest.cbr_rates.fetch_rates", lambda on: rates)
    rc = cli._ingest_cbr(Namespace(dry=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "USD=90.0" in out and "EUR=100.0" in out
    assert (tmp_path / "master" / "ref" / "cbr_rates.parquet").exists()


# --------------------------------------------------------------------------- #
# _publish_hf_mirror
# --------------------------------------------------------------------------- #


def test_publish_hf_mirror_missing_env_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "")
    monkeypatch.setenv("HF_TOKEN", "")

    rc = cli._publish_hf_mirror(Namespace(dry=False))

    assert rc == 2
    assert "HF_REPO_ID / HF_TOKEN" in capsys.readouterr().err


def test_publish_hf_mirror_missing_required_artifacts_returns_3(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "owner/repo")
    monkeypatch.setenv("HF_TOKEN", "hf_TEST")

    rc = cli._publish_hf_mirror(Namespace(dry=False))

    assert rc == 3
    err = capsys.readouterr().err
    assert "derived/slim_active.parquet" in err
    assert "derived/agg/weekly_role_salary.parquet" in err


def test_publish_hf_mirror_dry_prints_plan_without_upload(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "owner/repo")
    monkeypatch.setenv("HF_TOKEN", "hf_TEST")
    (tmp_path / "derived" / "agg").mkdir(parents=True)
    (tmp_path / "derived" / "slim_active.parquet").write_bytes(b"slim")
    (tmp_path / "derived" / "agg" / "weekly_role_salary.parquet").write_bytes(b"agg")

    def boom(*_args, **_kwargs):
        raise AssertionError("dry run must not upload")

    monkeypatch.setattr("src.publish.hf_mirror.upload_items", boom)

    rc = cli._publish_hf_mirror(Namespace(dry=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "https://huggingface.co/datasets/owner/repo/resolve/main" in out
    assert "slim/active.parquet" in out
    assert "agg" in out


def test_publish_hf_mirror_uploads_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "owner/repo")
    monkeypatch.setenv("HF_TOKEN", "hf_TEST")
    (tmp_path / "derived" / "agg").mkdir(parents=True)
    (tmp_path / "derived" / "slim_active.parquet").write_bytes(b"slim")
    (tmp_path / "derived" / "agg" / "weekly_role_salary.parquet").write_bytes(b"agg")
    captured = []

    def fake_upload_items(plan, cfg):
        captured.extend((item.path_in_repo, cfg.repo_id, cfg.token) for item in plan)

    monkeypatch.setattr("src.publish.hf_mirror.upload_items", fake_upload_items)

    rc = cli._publish_hf_mirror(Namespace(dry=False))

    assert rc == 0
    assert captured == [
        ("slim/active.parquet", "owner/repo", "hf_TEST"),
        ("agg", "owner/repo", "hf_TEST"),
    ]
    assert "HF_TOKEN" not in capsys.readouterr().out


def test_publish_hf_mirror_missing_cli_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "owner/repo")
    monkeypatch.setenv("HF_TOKEN", "hf_TEST")
    (tmp_path / "derived" / "agg").mkdir(parents=True)
    (tmp_path / "derived" / "slim_active.parquet").write_bytes(b"slim")
    (tmp_path / "derived" / "agg" / "weekly_role_salary.parquet").write_bytes(b"agg")

    def raise_missing_cli(plan, cfg):
        raise FileNotFoundError("huggingface-cli")

    monkeypatch.setattr("src.publish.hf_mirror.upload_items", raise_missing_cli)

    rc = cli._publish_hf_mirror(Namespace(dry=False))

    assert rc == 2
    assert "huggingface-cli not found" in capsys.readouterr().err


def test_publish_hf_mirror_called_process_error_returns_1(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_REPO_ID", "owner/repo")
    monkeypatch.setenv("HF_TOKEN", "hf_TEST")
    (tmp_path / "derived" / "agg").mkdir(parents=True)
    (tmp_path / "derived" / "slim_active.parquet").write_bytes(b"slim")
    (tmp_path / "derived" / "agg" / "weekly_role_salary.parquet").write_bytes(b"agg")

    def raise_upload_failure(plan, cfg):
        raise subprocess.CalledProcessError(
            1,
            ["huggingface-cli", "upload"],
            stderr="401 Unauthorized",
        )

    monkeypatch.setattr("src.publish.hf_mirror.upload_items", raise_upload_failure)

    rc = cli._publish_hf_mirror(Namespace(dry=False))

    assert rc == 1
    assert "401 Unauthorized" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _publish_slim / _publish_events / _publish_embeddings dry + arg plumbing
# --------------------------------------------------------------------------- #


def test_publish_slim_dry_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._publish_slim(
        Namespace(dry=True, strict=False, active_days=None, scope=None, dedup=False)
    )
    assert rc == 0
    assert "publish slim" in capsys.readouterr().out


def test_publish_slim_writes_artifact(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    fake_df = pl.DataFrame(
        {
            "last_seen_at": [datetime.now(timezone.utc)],
            "first_seen_at": [datetime.now(timezone.utc)],
        }
    )
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda *_a, **_kw: fake_df,
    )

    def fake_write(_df, out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"parquet")

    monkeypatch.setattr("src.transform.slim_export.write_slim_active", fake_write)

    rc = cli._publish_slim(
        Namespace(dry=False, strict=False, active_days=None, scope=None, dedup=False)
    )

    assert rc == 0
    assert (tmp_path / "derived" / "slim_active.parquet").exists()


def test_publish_slim_empty_lake_returns_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda *_a, **_kw: pl.DataFrame(),
    )
    rc = cli._publish_slim(
        Namespace(dry=False, strict=False, active_days=None, scope=None, dedup=False)
    )
    assert rc == 3
    assert "empty lake" in capsys.readouterr().err


def test_publish_slim_strict_stale_returns_4(tmp_path, monkeypatch, capsys):
    """When last_seen_at is older than 24h and --strict is set, exit 4."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.chdir(tmp_path)
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=48)
    fake_df = pl.DataFrame({"last_seen_at": [stale_ts.replace(tzinfo=None)]})
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda *_a, **_kw: fake_df,
    )
    rc = cli._publish_slim(
        Namespace(dry=False, strict=True, active_days=None, scope=None, dedup=False)
    )
    assert rc == 4
    assert "exceeds 24h threshold" in capsys.readouterr().err


def test_publish_slim_active_days_passes_window(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    seen_kwargs = {}
    fresh_ts = datetime.now(timezone.utc)
    fake_df = pl.DataFrame(
        {
            "last_seen_at": [fresh_ts],
            "first_seen_at": [fresh_ts],
        }
    )

    def fake_build(_lake, **kwargs):
        seen_kwargs.update(kwargs)
        return fake_df

    def fake_write(_df, out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"parquet")

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build)
    monkeypatch.setattr("src.transform.slim_export.write_slim_active", fake_write)

    rc = cli._publish_slim(
        Namespace(dry=False, strict=False, active_days=14, scope=None, dedup=False)
    )

    assert rc == 0
    assert seen_kwargs["active_window_days"] == 14
    assert "active-window filter" in capsys.readouterr().out


def test_publish_slim_scope_passes_market_scope(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    seen_kwargs = {}
    fresh_ts = datetime.now(timezone.utc)
    fake_df = pl.DataFrame(
        {
            "last_seen_at": [fresh_ts],
            "first_seen_at": [fresh_ts],
        }
    )

    def fake_build(_lake, **kwargs):
        seen_kwargs.update(kwargs)
        return fake_df

    def fake_write(_df, out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"parquet")

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build)
    monkeypatch.setattr("src.transform.slim_export.write_slim_active", fake_write)

    rc = cli._publish_slim(
        Namespace(dry=False, strict=False, active_days=None, scope="it", dedup=False)
    )

    assert rc == 0
    assert seen_kwargs["market_scope"] == "it"
    assert "market-scope filter: it" in capsys.readouterr().out


def test_publish_events_dry_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._publish_events(Namespace(dry=True))
    assert rc == 0
    assert "publish events" in capsys.readouterr().out


def test_publish_embeddings_dry_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._publish_embeddings(Namespace(dry=True))
    assert rc == 0


def test_publish_embeddings_missing_lance_returns_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._publish_embeddings(Namespace(dry=False))
    assert rc == 3
    assert "embeddings.lance missing" in capsys.readouterr().err


def test_publish_weekly_dry_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._publish_weekly(Namespace(dry=True, strict=False))
    assert rc == 0


# --------------------------------------------------------------------------- #
# _report — quarto missing / unknown kind
# --------------------------------------------------------------------------- #


def test_report_quarto_missing_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    # Make the fallback path return False for .exists()
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    rc = cli._report(Namespace(kind="monthly", month=None, employer=None, scope=None))
    assert rc == 2
    assert "quarto не найден" in capsys.readouterr().err


def test_report_unknown_kind_returns_2(tmp_path, monkeypatch, capsys):
    """quarto exists, but kind not in template_map."""
    monkeypatch.chdir(tmp_path)
    # Force quarto-found path: pretend the resolved binary exists.
    quarto_path = tmp_path / "fake_quarto.exe"
    quarto_path.write_text("stub")
    monkeypatch.setattr("shutil.which", lambda _name: str(quarto_path))
    rc = cli._report(Namespace(kind="bogus", month=None, employer=None, scope=None))
    assert rc == 2
    assert "нет шаблона для kind=bogus" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _publish_events non-dry paths
# --------------------------------------------------------------------------- #


def _empty_slim_events_df():
    from src.transform.slim_events import SLIM_EVENTS_SCHEMA

    return pl.DataFrame(schema=SLIM_EVENTS_SCHEMA)


def _one_slim_event_df():
    from datetime import datetime, timezone

    from src.transform.slim_events import SLIM_EVENTS_SCHEMA

    return pl.DataFrame(
        {
            "event_id": ["e1"],
            "vacancy_id": ["hh:1"],
            "employer_id": ["hh:42"],
            "ts": [datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)],
            "type": ["appeared"],
            "payload": ["{}"],
            "source": ["hh"],
        },
        schema=SLIM_EVENTS_SCHEMA,
    )


def test_publish_events_no_events_prints_done_and_skips_write(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "src.transform.slim_events.build_slim_events_30d",
        lambda _db: _empty_slim_events_df(),
    )
    # Empty branch never calls write — assert by booby-trapping it.
    monkeypatch.setattr(
        "src.transform.slim_events.write_slim_events_partitioned",
        lambda *_a, **_kw: pytest.fail("write must not run on empty events"),
    )

    rc = cli._publish_events(Namespace(dry=False))
    assert rc == 0
    assert "no events in last 30 days" in capsys.readouterr().out


def test_publish_events_happy_wipes_stale_and_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "src.transform.slim_events.build_slim_events_30d",
        lambda _db: _one_slim_event_df(),
    )

    written_files: list[Path] = []
    stale_partition = tmp_path / "derived" / "slim_events_30d" / "stale.parquet"
    stale_partition.parent.mkdir(parents=True)
    stale_partition.write_bytes(b"stale")

    def fake_write(df, out_root):
        assert not stale_partition.exists()
        out_root.mkdir(parents=True, exist_ok=True)
        f = out_root / "year=2026" / "month=05" / "day=18" / "events.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"stub")
        written_files.append(f)
        return [f]

    monkeypatch.setattr(
        "src.transform.slim_events.write_slim_events_partitioned",
        fake_write,
    )

    rc = cli._publish_events(Namespace(dry=False))
    assert rc == 0
    assert len(written_files) == 1
    assert "1 events × 7 cols" in capsys.readouterr().out



# --------------------------------------------------------------------------- #
# _ingest_hh_crawl — dry + happy
# --------------------------------------------------------------------------- #


def test_ingest_hh_crawl_dry(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master").mkdir()
    progress = tmp_path / "master" / "crawl_progress.json"
    progress.write_text('{"stub": true}', encoding="utf-8")
    rc = cli._ingest_hh_crawl(
        Namespace(
            root="area=113",
            reset=True,
            dry=True,
            max_depth=4,
            max_vacancies=100,
            rate=1.0,
        )
    )
    assert rc == 0
    # --reset deleted the existing progress file before --dry returned
    assert not progress.exists()
    out = capsys.readouterr().out
    assert "[dry] hh-crawl" in out and "max_depth=4" in out


def test_ingest_hh_crawl_happy_runs_crawl(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master").mkdir()

    called = {}

    def fake_crawl(root, *, max_depth, max_vacancies, rate_limit_sec,
                   progress_path, lake_root, client):
        called["root"] = root
        called["max_depth"] = max_depth
        called["rate_limit_sec"] = rate_limit_sec
        return {"stats": {"requests": 42, "vacancies_fetched": 1234, "segments_done": 7}}

    monkeypatch.setattr("src.ingest.hh_crawler.crawl", fake_crawl)
    # Don't actually open a network client — _ingest_hh_crawl constructs one
    # before calling crawl. Patch HHShardsClient to a no-op stub.
    monkeypatch.setattr(
        "src.ingest.hh_shards.HHShardsClient",
        lambda _cfg: object(),
    )

    rc = cli._ingest_hh_crawl(
        Namespace(
            root="area=113,professional_role=10",
            reset=False,
            dry=False,
            max_depth=2,
            max_vacancies=999,
            rate=0.5,
        )
    )
    assert rc == 0
    assert called["max_depth"] == 2
    assert called["rate_limit_sec"] == 0.5
    out = capsys.readouterr().out
    assert "requests=42" in out and "vacancies=1234" in out and "segments_done=7" in out


# --------------------------------------------------------------------------- #
# Phase 3 — remaining low-hanging cli.py gaps
# --------------------------------------------------------------------------- #


def test_publish_embeddings_happy_writes_parquet(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    # Pretend the Lance store exists (mtime+path probe). The export function
    # is mocked so the directory's contents don't matter.
    lance_path = tmp_path / "master" / "embeddings.lance"
    lance_path.mkdir(parents=True)

    out_path = tmp_path / "derived" / "embeddings.parquet"
    out_path.parent.mkdir(parents=True)

    def fake_export(out, src):
        # Match the cli expectation: write a non-empty parquet, return row count.
        pl.DataFrame({"x": [1, 2, 3]}).write_parquet(out)
        return 3

    monkeypatch.setattr("src.publish.embeddings_export.export_to_parquet", fake_export)

    rc = cli._publish_embeddings(Namespace(dry=False))
    assert rc == 0
    assert "3 rows" in capsys.readouterr().out


def test_publish_embeddings_empty_lance_returns_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master" / "embeddings.lance").mkdir(parents=True)
    monkeypatch.setattr(
        "src.publish.embeddings_export.export_to_parquet",
        lambda out, src: 0,
    )
    rc = cli._publish_embeddings(Namespace(dry=False))
    assert rc == 3
    assert "empty Lance store" in capsys.readouterr().err


def test_publish_slim_dedup_branch(tmp_path, monkeypatch, capsys):
    """Cover the --dedup branch of _publish_slim."""
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)

    fresh_ts = datetime.now(timezone.utc).replace(tzinfo=None)
    fake_df = pl.DataFrame({"last_seen_at": [fresh_ts], "x": [1]})

    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda *_a, **_kw: fake_df,
    )

    seen = {}

    def fake_dedup(df):
        seen["called"] = True
        # Return df unchanged + one fake (hh, tg) pair so the dedup print fires.
        return df, [("hh:1", "tg:abc")]

    monkeypatch.setattr(
        "src.transform.slim_export.apply_cross_source_dedup", fake_dedup
    )

    def fake_write(df, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 128)
        return out

    monkeypatch.setattr("src.transform.slim_export.write_slim_active", fake_write)

    rc = cli._publish_slim(
        Namespace(
            dry=False, strict=False, active_days=None, scope=None, dedup=True
        )
    )
    assert rc == 0
    assert seen.get("called")
    assert "[dedup] cross-source pairs=1" in capsys.readouterr().out


def test_auth_tg_happy_path_mocks_telethon(tmp_path, monkeypatch, capsys):
    """End-to-end _auth_tg with a faked telethon.TelegramClient class."""
    import sys
    import types

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeef")
    monkeypatch.setenv("TG_PHONE", "+79991234567")
    monkeypatch.setenv("TG_SESSION", "test_session")

    started = {}

    class FakeMe:
        username = "julia_test"
        first_name = "Julia"
        id = 42

    class FakeClient:
        def __init__(self, session, api_id, api_hash):
            started["session"] = session
            started["api_id"] = api_id
            started["api_hash"] = api_hash

        def start(self, phone):
            started["phone"] = phone

        def get_me(self):
            return FakeMe()

        def disconnect(self):
            started["disconnected"] = True

    fake_telethon_sync = types.ModuleType("telethon.sync")
    fake_telethon_sync.TelegramClient = FakeClient
    # cli does `from telethon.sync import TelegramClient` — make sure the parent
    # telethon package is still importable AS-IS; only override telethon.sync.
    monkeypatch.setitem(sys.modules, "telethon.sync", fake_telethon_sync)

    rc = cli._auth_tg(Namespace(phone=None))  # falls back to TG_PHONE env
    assert rc == 0
    assert started["api_id"] == 12345
    assert started["phone"] == "+79991234567"
    assert started["session"] == "test_session"
    assert started.get("disconnected") is True
    out = capsys.readouterr().out
    assert "[auth-tg] OK: signed in as julia_test (42)" in out
    assert "test_session.session" in out


# --------------------------------------------------------------------------- #
# _ingest_telegram validation paths
# --------------------------------------------------------------------------- #


def test_ingest_telegram_negative_channel_start_returns_2(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_telegram(
        Namespace(
            dry=True,
            channels=None,
            channel_start=-1,
            channel_file=None,
            limit=10,
            scope=None,
        )
    )
    assert rc == 2
    assert "--channel-start must be >= 0" in capsys.readouterr().err


def test_ingest_telegram_zero_channels_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_telegram(
        Namespace(
            dry=True,
            channels=0,
            channel_start=0,
            channel_file=None,
            limit=10,
            scope=None,
        )
    )
    assert rc == 2
    assert "--channels must be >= 1" in capsys.readouterr().err


def test_ingest_telegram_dry_no_scope_prints_summary(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_telegram(
        Namespace(
            dry=True,
            channels=5,
            channel_start=0,
            channel_file=None,
            limit=20,
            scope=None,
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry] tg ingest channel_start=0" in out
    assert "channels=5" in out and "limit=20" in out


# --------------------------------------------------------------------------- #
# _ingest_hh validation paths
# --------------------------------------------------------------------------- #


def _hh_ns(**overrides):
    base = {
        "pages": 1,
        "page_start": 1,
        "overlap_pages": 0,
        "transport": "shards",
        "scope": None,
        "area": 113,
        "per_page": 50,
        "dry": True,
        "detect_closed": False,
    }
    base.update(overrides)
    return Namespace(**base)


def test_ingest_hh_negative_pages_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_hh(_hh_ns(pages=0))
    assert rc == 2
    assert "--pages must be >= 1" in capsys.readouterr().err


def test_ingest_hh_zero_page_start_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_hh(_hh_ns(page_start=0))
    assert rc == 2
    assert "--page-start must be >= 1" in capsys.readouterr().err


def test_ingest_hh_overlap_ge_pages_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_hh(_hh_ns(pages=2, overlap_pages=2))
    assert rc == 2
    assert "--overlap-pages must be >= 0 and lower than --pages" in capsys.readouterr().err


def test_ingest_hh_dry_no_scope_prints_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli._ingest_hh(_hh_ns(pages=3, page_start=2, overlap_pages=1))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry] hh.ru transport=shards area=113" in out
    assert "pages=2-4 overlap=1" in out
    assert "next_page_start=4" in out  # 2 + 3 - 1


def test_ingest_hh_dry_with_scope_includes_role_ids(
    tmp_path, monkeypatch, capsys
):
    """--dry --scope it prints resolved professional_role IDs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "_resolve_hh_scope_role_ids",
        lambda scope: (object(), [10, 11, 12]),
    )
    rc = cli._ingest_hh(_hh_ns(scope="it"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "scope=it" in out and "professional_role=10,11,12" in out


def test_ingest_hh_dry_scope_resolution_failure_returns_2(
    tmp_path, monkeypatch, capsys
):
    """--scope refers to an unknown profile — exit 2 with the inner message."""
    monkeypatch.chdir(tmp_path)

    def boom(_scope):
        raise ValueError("unknown scope 'bogus'")

    monkeypatch.setattr(cli, "_resolve_hh_scope_role_ids", boom)
    rc = cli._ingest_hh(_hh_ns(scope="bogus"))
    assert rc == 2
    assert "unknown scope 'bogus'" in capsys.readouterr().err
