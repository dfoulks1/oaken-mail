# SPDX-License-Identifier: Apache-2.0
"""Configuration. Precedence: defaults < ``config.toml`` < ``OAKEN_*`` environment variables.

Nested keys use a double underscore in the environment: ``OAKEN_API__PORT=8123``. Unknown keys
are an error, in the file and in code. No secrets live here: tokens belong in the OS keyring.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
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
    # Raw messages up to this size are stored inside raw.db; larger ones go to the LFS folder
    # (raw.db keeps a header stub and a link).
    inline_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    # Past this size the message is still kept (in LFS) but flagged ``oversize`` in its event.
    max_message_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    lfs_dir: str = "lfs"

    @model_validator(mode="after")
    def _limits_are_ordered(self) -> "StorageSettings":
        if self.inline_max_bytes > self.max_message_bytes:
            raise ValueError("inline_max_bytes cannot exceed max_message_bytes")
        return self


class AISettings(_Section):
    """Names only. Nothing in core calls a model; plugins resolve these strings."""

    embedder: str = "ollama"
    embed_model: str = "nomic-embed-text"
    llm_backend: str = "ollama"
    llm_model: str = "qwen3:8b"  # a starting point; set a larger model for better analysis
    vector_store: str = "sqlite-vec"
    # Chunking (prompt 07): the target size of a chunk and how much of the previous one a chunk
    # repeats, both in tokens as the configured tokenizer counts them.
    chunk_tokens: int = Field(default=256, ge=16, le=8192)
    chunk_overlap: int = Field(default=32, ge=0)

    @model_validator(mode="after")
    def _overlap_is_smaller(self) -> "AISettings":
        if self.chunk_overlap * 2 > self.chunk_tokens:
            raise ValueError("chunk_overlap cannot be more than half of chunk_tokens")
        return self


class IngestSettings(_Section):
    """How the ingest pipeline paces itself (prompt 09)."""

    # Chunks sent to the embedder in one call.
    embed_batch: int = Field(default=32, ge=1, le=1024)
    # While this many ingest jobs are queued, a backfill sync stops (cursor kept) and tries again
    # at its next run. New mail is never held back.
    backfill_queue_cap: int = Field(default=2000, ge=1)
    # Embedding and enrichment attempts before a message is parked as failed. Higher than
    # [jobs] max_attempts because a model outage is expected to end.
    stage_max_attempts: int = Field(default=8, ge=1)
    # How many of each model-bound job run at once (one local GPU: leave at 1).
    embed_concurrency: int = Field(default=1, ge=1)
    enrich_concurrency: int = Field(default=1, ge=1)
    # Rolling window for the throughput the status ETA uses.
    throughput_window_seconds: float = Field(default=600.0, gt=0)


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


class ThreadingSettings(_Section):
    """How ``thread_key`` is derived. The standard reference is jwz's threading algorithm."""

    # references: only Message-ID / In-Reply-To / References decide. references+subject also
    # derives a key from the normalized subject for replies that carry no reference headers.
    strategy: Literal["references", "references+subject"] = "references+subject"
    # Reply and forward prefixes removed from the front of a subject (case-insensitive, with an
    # optional counter such as ``Re[2]:``). Extend this list for other languages or clients.
    subject_prefixes: list[str] = Field(
        default_factory=lambda: [
            "re",
            "fwd",
            "fw",
            "aw",
            "wg",
            "sv",
            "vs",
            "antw",
            "tr",
            "rv",
            "enc",
            "res",
        ]
    )
    strip_list_tags: bool = True  # drop leading ``[list-name]`` tags from the subject
    # Whether the subject-derived key also includes the participants (sender and recipients).
    fallback_includes_participants: bool = True


class CleaningSettings(_Section):
    """Tuning for the cleaner. Every pattern is a regular expression matched against one line
    (long lines are cut before matching, so a hostile message cannot make a pattern slow)."""

    # Rule names (see docs/architecture/parsing.md) switched off entirely.
    disabled_rules: list[str] = Field(default_factory=list)
    extra_attribution_patterns: list[str] = Field(default_factory=list)
    extra_footer_patterns: list[str] = Field(default_factory=list)
    extra_disclaimer_patterns: list[str] = Field(default_factory=list)
    extra_boilerplate_patterns: list[str] = Field(default_factory=list)
    extra_closing_patterns: list[str] = Field(default_factory=list)
    # A signature after a closing phrase ("Thanks,") may be at most this many lines.
    signature_max_lines: int = Field(default=8, ge=1, le=50)
    # Use the text/plain alternative when it holds at least this fraction of the HTML's text.
    plain_min_ratio: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _patterns_compile(self) -> "CleaningSettings":
        import re

        for name in (
            "extra_attribution_patterns",
            "extra_footer_patterns",
            "extra_disclaimer_patterns",
            "extra_boilerplate_patterns",
            "extra_closing_patterns",
        ):
            for pattern in getattr(self, name):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"{name}: bad pattern {pattern!r}: {exc}") from exc
        return self


class AttachmentSettings(_Section):
    """Attachments are never opened or analysed. A quarantined one is flagged so that nothing
    previews, serves or extracts it without a deliberate action."""

    extra_quarantine_extensions: list[str] = Field(default_factory=list)
    extra_quarantine_types: list[str] = Field(default_factory=list)
    exempt_extensions: list[str] = Field(default_factory=list)  # never quarantined by extension
    # Flagged attachments are written, never opened, to a folder only the owner can use.
    quarantine: bool = True
    quarantine_dir: str = "quarantine"  # relative to the data folder unless absolute
    quarantine_max_bytes: int = Field(default=50 * 1024 * 1024, ge=0)  # per attachment


class SieveSettings(_Section):
    """Sieve filtering (RFC 5228). Scripts are ``*.sieve`` files in ``dir``, run in name order.
    The limits bound what a script can cost; hostile mail is the input they run against."""

    enabled: bool = True
    dir: str = "sieve"  # relative to the config folder unless absolute
    max_script_bytes: int = Field(default=256 * 1024, ge=1024)
    max_steps: int = Field(default=100_000, ge=100)
    max_actions: int = Field(default=200, ge=1)
    max_match_chars: int = Field(default=10_000, ge=100)  # text cut before a ``:matches`` glob
    max_regex_chars: int = Field(default=4096, ge=100)  # text cut before a ``:regex`` match
    max_variable_chars: int = Field(default=4096, ge=1)


class MailSettings(_Section):
    threading: ThreadingSettings = ThreadingSettings()
    cleaning: CleaningSettings = CleaningSettings()
    attachments: AttachmentSettings = AttachmentSettings()
    sieve: SieveSettings = SieveSettings()
    # A local Hugging Face tokenizer.json for exact token counts (for example the file that ships
    # with your Qwen model). Unset, counts are a cheap estimate. Nothing is ever downloaded.
    tokenizer_file: str | None = None
    # A text body longer than this is cut (the full message stays in raw.db) and a warning added.
    max_text_chars: int = Field(default=2_000_000, ge=1000)


class IndexSettings(_Section):
    """The derived search index (``index.db``)."""

    # A reply that names no parent joins the thread of an earlier message with the same subject,
    # but only if that message is this recent, so a recurring subject does not merge across months.
    subject_join_days: int = Field(default=30, ge=0, le=3650)
    max_query_chars: int = Field(default=1000, ge=10, le=100_000)
    max_query_terms: int = Field(default=32, ge=1, le=256)


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
    ingest: IngestSettings = IngestSettings()
    swarm: SwarmSettings = SwarmSettings()
    mail: MailSettings = MailSettings()
    index: IndexSettings = IndexSettings()
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
