"""Paths and settings.

Path resolution has one override (``ZIPF_HOME``) and one fallback (XDG). The
override exists so tests, scratch databases, and Litestream targets all point
somewhere else with a single environment variable.

Precedence for values: explicit init argument, then environment, then ``.env``,
then ``config.toml``, then the default. Secrets live only in the environment;
they are never read from the TOML file.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

APP_NAME = "zipf"


@dataclass(frozen=True)
class Paths:
    """Every location Zipf writes to."""

    root: Path
    db_file: Path
    config_file: Path
    state_dir: Path  # OAuth tokens and other regenerable credentials cache

    @classmethod
    def resolve(cls) -> Paths:
        home = os.environ.get("ZIPF_HOME")
        if home:
            root = Path(home).expanduser()
            return cls(
                root=root,
                db_file=root / "zipf.db",
                config_file=root / "config.toml",
                state_dir=root / "state",
            )

        data_home = Path(
            os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        ).expanduser()
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        ).expanduser()

        return cls(
            root=data_home / APP_NAME,
            db_file=data_home / APP_NAME / "zipf.db",
            config_file=config_home / APP_NAME / "config.toml",
            state_dir=data_home / APP_NAME / "state",
        )

    def ensure(self) -> None:
        """Create the directories Zipf writes to."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)


class LlmSettings(BaseModel):
    """Model panel configuration. Consumed at M5."""

    models: list[str] = Field(default_factory=lambda: ["claude-opus-5", "gpt-5", "gemini-2.5-pro"])
    n: int = Field(default=5, ge=1, le=25)


class Settings(BaseSettings):
    """Runtime settings.

    Secrets are optional here and validated where they are used. A missing
    DataForSEO password should fail when a tier-1 call is attempted, naming the
    capability that needed it, not when the CLI starts up to show cached data.
    """

    model_config = SettingsConfigDict(
        env_prefix="ZIPF_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    monthly_ceiling_usd: float = Field(default=20.0, gt=0)
    #: 0.0 confirms every spend. Any tier-1 call costs at least the vendor's
    #: per-call base, so a threshold below that base gates nothing extra.
    confirm_threshold_usd: float = Field(default=0.0, ge=0)
    own_domain: str | None = None
    #: Search Console property, exactly as it appears there: either
    #: ``sc-domain:example.com`` or ``https://example.com/``. The two are
    #: different properties with different data, so this is not derived from
    #: ``own_domain``.
    gsc_site_url: str | None = None

    llm: LlmSettings = Field(default_factory=LlmSettings)

    # Secrets. Read from the environment without the ZIPF_ prefix so they match
    # the names the vendors' own tooling uses.
    dataforseo_login: str | None = Field(default=None, alias="DATAFORSEO_LOGIN")
    dataforseo_password: str | None = Field(default=None, alias="DATAFORSEO_PASSWORD")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gsc_client_id: str | None = Field(default=None, alias="GSC_CLIENT_ID")
    gsc_client_secret: str | None = Field(default=None, alias="GSC_CLIENT_SECRET")

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: object) -> object:
        """Treat ``KEY=`` in .env as absent.

        Without this, an empty credential is a present-but-useless value that
        fails deep inside a vendor client instead of at the capability boundary.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The TOML path depends on ZIPF_HOME, so it is built here rather than
        # declared statically in model_config.
        toml = TomlConfigSettingsSource(settings_cls, toml_file=Paths.resolve().config_file)
        return (init_settings, env_settings, dotenv_settings, toml, file_secret_settings)


DEFAULT_CONFIG_TOML = """\
# Zipf configuration. Secrets belong in the environment or .env, never here.

monthly_ceiling_usd   = 20.0
confirm_threshold_usd = 0.0
# own_domain          = "example.com"
# gsc_site_url        = "sc-domain:example.com"

[llm]
models = ["claude-opus-5", "gpt-5", "gemini-2.5-pro"]
n      = 5
"""


def load_settings(**overrides: Any) -> Settings:
    """Load settings, applying any explicit overrides at the highest precedence."""
    return Settings(**overrides)


def missing_credentials(variables: Sequence[str]) -> list[str]:
    """Return the names in ``variables`` that resolve to nothing.

    Resolution goes through :class:`Settings`, not ``os.environ``. Credentials
    normally live in ``.env``, which the process environment never sees, so
    checking ``os.environ`` directly would report a configured key as missing.
    """
    settings = load_settings()
    by_alias = {(field.alias or name): name for name, field in Settings.model_fields.items()}

    missing: list[str] = []
    for variable in variables:
        attribute = by_alias.get(variable)
        if attribute is None or not getattr(settings, attribute, None):
            missing.append(variable)
    return missing
