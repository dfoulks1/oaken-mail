# SPDX-License-Identifier: Apache-2.0
"""Configuration. Precedence: defaults < ``config.toml`` < ``OAKEN_*`` environment variables.

Nested keys use a double underscore in the environment: ``OAKEN_API__PORT=8123``. Unknown keys
are an error, in the file and in code. No secrets live here: tokens belong in the OS keyring.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from oaken.core import paths
from oaken.core.errors import ConfigError

CONFIG_FILE = "config.toml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageSettings(_Section):
    raw_db: str = "raw.db"
    index_db: str = "index.db"
    read_pool_size: int = Field(default=4, ge=1, le=32)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class AISettings(_Section):
    """Names only. Nothing in core calls a model; plugins resolve these strings."""

    embedder: str = "ollama"
    embed_model: str = "nomic-embed-text"
    llm_backend: str = "ollama"
    llm_model: str = "llama3.2:3b"
    vector_store: str = "sqlite-vec"


class SyncSettings(_Section):
    provider: str = "gmail"
    interval_seconds: int = Field(default=300, ge=1)
    batch_size: int = Field(default=100, ge=1)


class APISettings(_Section):
    host: Literal["127.0.0.1"] = "127.0.0.1"  # fixed: the API is never exposed off-host
    port: int = Field(default=0, ge=0, le=65535)  # 0 = pick a free port


class LogSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["console", "json"] = "console"


class Settings(BaseSettings):
    """``Settings(...)`` is pure: it reads neither the file nor the environment.
    ``Settings.load()`` applies the full precedence."""

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    storage: StorageSettings = StorageSettings()
    ai: AISettings = AISettings()
    sync: SyncSettings = SyncSettings()
    api: APISettings = APISettings()
    log: LogSettings = LogSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)

    @classmethod
    def load(cls, config_file: Path | None = None) -> "Settings":
        """Defaults, then ``config.toml`` (in ``config_dir()`` unless given), then ``OAKEN_*``."""
        path = config_file or paths.config_dir() / CONFIG_FILE

        class _Loaded(cls):  # type: ignore[valid-type,misc]
            model_config = SettingsConfigDict(
                extra="forbid",
                frozen=True,
                env_prefix="OAKEN_",
                env_nested_delimiter="__",
            )

            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                return (
                    EnvSettingsSource(settings_cls),
                    TomlConfigSettingsSource(settings_cls, toml_file=path),
                )

        try:
            return cls.model_validate(_Loaded().model_dump())
        except ValidationError as exc:
            raise ConfigError(_describe(exc, path)) from exc
        except (OSError, ValueError) as exc:  # unreadable or malformed TOML
            raise ConfigError(f"cannot read {path}: {exc}") from exc


def _describe(exc: ValidationError, path: Path) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return f"invalid configuration ({path}): " + "; ".join(parts)
