from __future__ import annotations

from pathlib import Path

import pytest

from src.config import HHRateLimit, Settings, _load, load_settings


def test_load_returns_defaults_when_file_missing(tmp_path: Path):
    settings = _load(tmp_path / "absent.yaml")
    assert isinstance(settings, Settings)
    assert settings.hh.rate_limit.requests_per_second == 10.0
    assert settings.hh.search.area == 113
    assert settings.telegram.rate_limit.messages_per_second == 1.0


def test_load_reads_yaml(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "hh:\n"
        "  rate_limit:\n"
        "    requests_per_second: 7.5\n"
        "    max_retries: 9\n"
        "  search:\n"
        "    area: 1\n",
        encoding="utf-8",
    )
    settings = _load(cfg)

    assert settings.hh.rate_limit.requests_per_second == 7.5
    assert settings.hh.rate_limit.max_retries == 9
    # Defaults preserved для тех ключей, которые не заданы.
    assert settings.hh.rate_limit.backoff_min == 1.0
    assert settings.hh.search.area == 1
    assert settings.hh.search.per_page == 100


def test_load_ignores_unknown_keys(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "hh:\n  rate_limit:\n    requests_per_second: 5\n    fanciful_param: true\n",
        encoding="utf-8",
    )
    # Должно загрузиться без ValidationError благодаря extra='ignore'.
    settings = _load(cfg)
    assert settings.hh.rate_limit.requests_per_second == 5.0


def test_load_settings_repository_config_matches_pydantic_defaults():
    """config.yaml в репо — implicit contract: defaults в pydantic моделях
    должны равняться (или быть тише) тех значений, что записаны в YAML.
    Иначе пропуск config.yaml в проде даст другое поведение."""
    repo_cfg = Path("config.yaml")
    if not repo_cfg.exists():
        pytest.skip("config.yaml не в cwd — тест запущен вне репо")
    settings = _load(repo_cfg)

    # 10 rps уже зашит в HHRateLimit().requests_per_second; YAML должен совпадать.
    assert settings.hh.rate_limit.requests_per_second == HHRateLimit().requests_per_second


def test_load_settings_caches(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("hh:\n  rate_limit:\n    requests_per_second: 3.0\n", encoding="utf-8")
    load_settings.cache_clear()
    a = load_settings(cfg)
    b = load_settings(cfg)
    assert a is b
    load_settings.cache_clear()


def test_load_reads_it_market_scope(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "market:\n"
        "  live_scope: it\n"
        "  scopes:\n"
        "    it:\n"
        "      hh:\n"
        "        category_id: 11\n"
        "        category_name: Информационные технологии\n"
        "      telegram:\n"
        "        roles: [dev, data, qa]\n",
        encoding="utf-8",
    )

    settings = _load(cfg)
    scope = settings.market.require_scope("it")

    assert settings.market.live_scope == "it"
    assert scope.hh.category_id == 11
    assert scope.hh.category_name == "Информационные технологии"
    assert scope.telegram.roles == ["dev", "data", "qa"]


def test_it_scope_resolves_hh_role_ids_from_category():
    settings = Settings()
    scope = settings.market.require_scope("it")
    roles_data = {
        "categories": [
            {"id": "2", "name": "Продажи", "roles": [{"id": "70", "name": "Sales"}]},
            {
                "id": "11",
                "name": "Информационные технологии",
                "roles": [
                    {"id": "156", "name": "BI-аналитик"},
                    {"id": "160", "name": "DevOps-инженер"},
                ],
            },
        ]
    }

    assert scope.hh.resolve_role_ids(roles_data) == [156, 160]


def test_it_scope_filters_telegram_channels_by_role():
    settings = Settings()
    scope = settings.market.require_scope("it")
    channels = [
        {"username": "dev_jobs", "role": "dev"},
        {"username": "data_jobs", "role": "data"},
        {"username": "hr_jobs", "role": "hr"},
    ]

    assert scope.telegram.filter_channels(channels) == [
        {"username": "dev_jobs", "role": "dev"},
        {"username": "data_jobs", "role": "data"},
    ]


def test_hh_market_scope_resolves_explicit_role_ids_override():
    """`role_ids` явно задан → возвращается как есть (line 41), category lookup
    игнорируется."""
    from src.config import HHMarketScope

    scope = HHMarketScope(category_id=11, role_ids=[1, 2, 3])
    # Никакой category match даже не требуется.
    assert scope.resolve_role_ids({"categories": []}) == [1, 2, 3]


def test_hh_market_scope_resolves_via_category_name():
    """category_id mismatch, но `category_name` matches → fallback ветка (line 45-46)."""
    from src.config import HHMarketScope

    scope = HHMarketScope(category_id=999, category_name="Marketing")
    roles_data = {
        "categories": [
            {"id": "11", "name": "IT", "roles": [{"id": "100", "name": "Dev"}]},
            {"id": "12", "name": "Marketing", "roles": [{"id": "200", "name": "PR"}]},
        ]
    }
    assert scope.resolve_role_ids(roles_data) == [200]


def test_hh_market_scope_resolve_raises_when_missing():
    """Ни category_id, ни category_name не совпали → ValueError (line 47)."""
    from src.config import HHMarketScope

    scope = HHMarketScope(category_id=999, category_name="Nonexistent")
    with pytest.raises(ValueError, match="hh market scope category not found"):
        scope.resolve_role_ids({"categories": [{"id": "1", "name": "Other", "roles": []}]})


def test_telegram_market_scope_explicit_usernames_overrides_role_filter():
    """`usernames` задан → фильтр по nick'у (lines 57-63), `roles` игнорируется
    даже если канал имеет matching role."""
    from src.config import TelegramMarketScope

    scope = TelegramMarketScope(roles=["dev"], usernames=["@DevJobs", "datawave"])
    channels = [
        {"username": "DevJobs", "role": "dev"},
        {"username": "@datawave", "role": "marketing"},
        {"username": "noise", "role": "dev"},
    ]
    filtered = scope.filter_channels(channels)
    assert {c["username"] for c in filtered} == {"DevJobs", "@datawave"}


def test_market_settings_require_scope_unknown_raises():
    """`require_scope("missing")` → ValueError (lines 92-93). Должен сохранить
    KeyError context чистым (raise ... from None)."""
    from src.config import MarketSettings

    settings = MarketSettings()  # default = {"it": ...}
    with pytest.raises(ValueError, match="unknown market scope: missing"):
        settings.require_scope("missing")
