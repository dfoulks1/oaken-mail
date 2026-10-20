# SPDX-License-Identifier: Apache-2.0
"""Configuration. Precedence: defaults < ``config.toml`` < ``OAKEN_*`` environment variables.

Nested keys use a double underscore in the environment: ``OAKEN_API__PORT=8123``. Unknown keys
are an error, in the file and in code. No secrets live here: tokens belong in the OS keyring.
"""

from pathlib import Path
from typing import Any, Literal

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
    llm_model: str = "qwen3:8b"  # a starting point; set a larger model for better analysis
    vector_store: str = "sqlite-vec"


class JobsSettings(_Section):
    workers: int = Field(default=4, ge=1, le=64)
    lease_seconds: float = Field(default=120.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    backoff_base_seconds: float = Field(default=5.0, gt=0)
    backoff_max_seconds: float = Field(default=3600.0, gt=0)
    backoff_jitter: float = Field(default=0.2, ge=0, le=1)
    # Per-kind cap on jobs running at once, e.g. {"embed": 1, "enrich": 1} for one local GPU.
    kind_limits: dict[str, int] = Field(default_factory=dict)
    max_result_bytes: int = Field(default=256 * 1024, ge=1024)


class SwarmSettings(_Section):
    """Optional external workers (agents) that lease jobs over the API, the CLI or MCP.

    Off by default, and a kind of job is never offered to an agent unless it is listed in
    ``allowed_kinds``: handing a job to an external model sends it that mail's text, so the
    user opts in kind by kind. Internal parallelism is ``[jobs] workers`` and ``kind_limits``.
    """

    enabled: bool = False
    allowed_kinds: list[str] = Field(default_factory=list)
    max_agents: int = Field(default=8, ge=1, le=256)  # active (unrevoked) agents
    default_max_leases: int = Field(default=2, ge=1)  # jobs one agent may hold at once
    default_max_per_hour: int = Field(default=600, ge=1)  # jobs one agent may claim per hour


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
    jobs: JobsSettings = JobsSettings()
    swarm: SwarmSettings = SwarmSettings()
    sync: SyncSettings = SyncSettings()
    api: APISettings = APISettings()
    log: LogSettings = LogSettings()
    # Free-form, per plugin: [plugins.gmail] ... reaches that plugin as ``ctx.config``.
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)

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
