"""Single source of truth for runtime settings.

Reads `config.yaml` (см. репо корень). Все hardcoded defaults в
ingest-классах должны равняться значениям отсюда — иначе при отсутствии
config.yaml поведение не должно меняться.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


def _default_it_telegram_roles() -> list[str]:
    return [
        "analytics",
        "crypto",
        "data",
        "design",
        "dev",
        "devops",
        "game",
        "mobile",
        "product",
        "qa",
    ]


class HHMarketScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category_id: int | None = 11
    category_name: str | None = "Информационные технологии"
    role_ids: list[int] = Field(default_factory=list)
    roles_file: str = "data/professional_roles.yaml"

    def resolve_role_ids(self, roles_data: dict[str, Any]) -> list[int]:
        if self.role_ids:
            return [int(role_id) for role_id in self.role_ids]
        for category in roles_data.get("categories", []):
            if self.category_id is not None and str(category.get("id")) == str(self.category_id):
                return [int(role["id"]) for role in category.get("roles", [])]
            if self.category_name is not None and category.get("name") == self.category_name:
                return [int(role["id"]) for role in category.get("roles", [])]
        raise ValueError(f"hh market scope category not found: {self.category_id or self.category_name}")


class TelegramMarketScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    roles: list[str] = Field(default_factory=_default_it_telegram_roles)
    usernames: list[str] = Field(default_factory=list)
    channels_file: str | None = None

    def filter_channels(self, channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.usernames:
            wanted = {username.lstrip("@").lower() for username in self.usernames}
            return [
                channel
                for channel in channels
                if str(channel.get("username", "")).lstrip("@").lower() in wanted
            ]
        wanted_roles = set(self.roles)
        return [
            channel
            for channel in channels
            if channel.get("username") and channel.get("role") in wanted_roles
        ]


class MarketScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = "IT market"
    hh: HHMarketScope = Field(default_factory=HHMarketScope)
    telegram: TelegramMarketScope = Field(default_factory=TelegramMarketScope)


def _default_market_scopes() -> dict[str, MarketScope]:
    return {"it": MarketScope()}


class MarketSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    live_scope: str = "it"
    scopes: dict[str, MarketScope] = Field(default_factory=_default_market_scopes)

    def require_scope(self, name: str | None = None) -> MarketScope:
        scope_name = name or self.live_scope
        try:
            return self.scopes[scope_name]
        except KeyError:
            raise ValueError(f"unknown market scope: {scope_name}") from None


class HHRateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    requests_per_second: float = 10.0
    backoff_min: float = 1.0
    backoff_max: float = 60.0
    max_retries: int = 5


class HHSearch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    area: int = 113
    per_page: int = 100
    full_sweep_pages: int = 2000


class HHSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    api_base: str = "https://api.hh.ru"
    user_agent: str = "VacancyRadar/0.1 (research; contact: noreply@vacancyradar.example)"
    search: HHSearch = Field(default_factory=HHSearch)
    rate_limit: HHRateLimit = Field(default_factory=HHRateLimit)


class TelegramRateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    messages_per_second: float = 1.0


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channels_file: str = "data/tg_channels.yaml"
    rate_limit: TelegramRateLimit = Field(default_factory=TelegramRateLimit)


class SalarySettings(BaseModel):
    """Monthly RUB outlier thresholds applied in slim_export.

    Defaults match historical hardcoded values (10k floor, 5M ceiling) — see
    docstring on `_clamp_salary_outliers` for empirical justification. Override
    in config.yaml under `salary:` when corpus distribution shifts (e.g.,
    full-market sweep including non-IT roles with broader range).
    """

    model_config = ConfigDict(extra="ignore")
    outlier_floor: int = 10_000
    outlier_ceiling: int = 5_000_000


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hh: HHSettings = Field(default_factory=HHSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    market: MarketSettings = Field(default_factory=MarketSettings)
    salary: SalarySettings = Field(default_factory=SalarySettings)


DEFAULT_CONFIG_PATH = Path("config.yaml")


def _load(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)


@lru_cache(maxsize=4)
def load_settings(path: Path | None = None) -> Settings:
    """Cached load of config.yaml. Pass `path=None` for default discovery.

    Cache by path string — переиспользование между CLI subcommands в одном
    процессе. Tests могут передавать tmp-path и получать свежую копию.
    """
    return _load(path or DEFAULT_CONFIG_PATH)
