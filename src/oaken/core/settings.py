# SPDX-License-Identifier: Apache-2.0
"""Configuration. Precedence: defaults < ``config.toml`` < ``OAKEN_*`` environment variables.

Nested keys use a double underscore in the environment: ``OAKEN_API__PORT=8123``. Unknown keys
are an error, in the file and in code. No secrets live here: tokens belong in the OS keyring.
"""

import ipaddress
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
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


def _refuse_if_writable(path: Path) -> None:
    """config.toml is the source of truth for capabilities (MCP exposure, enrichers). A file any
    other user can write is a door around every ``mcp="never"`` control, so refuse a world-writable
    one. Group-writable is left alone: it is common under a umask of 002 and usually benign (a
    shared dev group), and refusing it would block normal setups. World-writable is the unambiguous
    hole. Missing file is fine (defaults, or ensure_default_config writes it 0o600)."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return  # no file: defaults apply, nothing to guard
    if mode & stat.S_IWOTH:
        raise ConfigError(
            f"{path} is world-writable ({stat.filemode(mode)}); it controls what MCP clients may "
            f"do, so it must not be writable by other users. Fix it with: chmod o-w {path}"
        )


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
    llm_model: str = "qwen2.5:3b"  # small on purpose; set a larger model for better analysis
    # The model ``ask`` uses (working out what was asked, and a cited answer). Empty means the
    # backend's own model. A question a person is waiting on may deserve a better one than enrich.
    ask_model: str = ""
    # While a question a person is waiting on uses the model, background work (enrich) holds its
    # next model call for at most this many seconds, then goes ahead. 0 means it never holds back.
    background_yield_seconds: float = Field(default=120.0, ge=0)
    # Without this, a backend that talks to a non-loopback address refuses to start (prompt 10).
    allow_remote: bool = False
    # Longest model answer, in characters, that is accepted before it counts as invalid output.
    max_output_chars: int = Field(default=64 * 1024, ge=256)
    vector_store: str = "sqlite-vec"
    # The LLM answer cache plugin (entry-point name). Empty keeps core's own SQLite cache.
    cache: str = ""
    # Most tokens of one message's cleaned text an LLM enricher sends. A longer message keeps its
    # head and tail with a marker between, and the result says ``truncated``.
    enrich_max_input_tokens: int = Field(default=3000, ge=200)
    # Most tokens of evidence (mail text, summaries and headers) one ``ask`` puts in front of the
    # model. The context builder never goes over it.
    ask_context_tokens: int = Field(default=3000, ge=200)
    # Chunking (prompt 07): the target size of a chunk and how much of the previous one a chunk
    # repeats, both in tokens as the configured tokenizer counts them.
    chunk_tokens: int = Field(default=256, ge=16, le=8192)
    chunk_overlap: int = Field(default=32, ge=0)

    @model_validator(mode="after")
    def _overlap_is_smaller(self) -> "AISettings":
        if self.chunk_overlap * 2 > self.chunk_tokens:
            raise ValueError("chunk_overlap cannot be more than half of chunk_tokens")
        return self


class WorkspaceSettings(_Section):
    """A named place AI work happens (prompt 10). The LLM's cache and usage are kept apart per
    workspace, and a ``secure`` one only ever talks to a backend that runs on this machine."""

    trust: Literal["secure", "insecure"] = "secure"
    # Entry-point name of the LLM backend; empty means ``ai.llm_backend``.
    llm_backend: str = ""


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
    # A handler that runs longer than this is cancelled and the job retried, so one wedged call
    # (a hung model request) never holds its worker forever and freezes the queue. A kind may set
    # its own in register(); this is the fallback. 0 disables the guard.
    handler_timeout_seconds: float = Field(default=600.0, ge=0)


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
    # How long an agent holds an ingest.enrich job before it must heartbeat. Long, because a large
    # model on modest hardware is slow.
    enrich_lease_seconds: float = Field(default=600.0, gt=0)


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


class SearchSettings(_Section):
    """Hybrid retrieval (prompt 15): how keyword and vector results are merged and how far a
    search looks before it gives up on a filter."""

    # Reciprocal Rank Fusion: a message scores sum(weight / (rrf_k + rank)) over the retrievers
    # that found it. A larger rrf_k flattens the difference between the first and the tenth hit.
    rrf_k: int = Field(default=60, ge=1, le=1000)
    keyword_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    # Keyword search that accepts any of the words (``ask``) keeps only messages that contain at
    # least this share of them, like Elasticsearch's minimum_should_match. Without it a question
    # fills the keyword side with messages that share one common word.
    keyword_min_match: float = Field(default=0.5, ge=0, le=1)
    # A query with a quoted phrase or a token containing a digit (an order number, a ticket id) is
    # about exact text, which meaning cannot tell apart: the keyword weight is multiplied by this.
    exact_boost: float = Field(default=3.0, ge=1)
    # Each retriever is asked for ``limit * overfetch`` candidates so filters that only raw.db can
    # answer (category, priority, unread) and the collapse of chunks to messages still leave
    # enough; a search that comes up short widens up to ``max_candidates``.
    overfetch: int = Field(default=5, ge=1, le=50)
    max_candidates: int = Field(default=500, ge=10, le=10_000)
    # Messages ``ask`` retrieves before the context builder decides how much of each to send.
    ask_hits: int = Field(default=10, ge=1, le=50)
    # Of those, how many get their matched chunks into the context (the rest are only a header and
    # a summary line), and how many also get the chunks beside the match and their thread's
    # summaries. The context never goes over ``ai.ask_context_tokens`` whatever these are.
    ask_chunk_hits: int = Field(default=5, ge=1, le=50)
    ask_neighbor_hits: int = Field(default=2, ge=0, le=50)
    # The vector side gives up (and the result says ``degraded``) after this long.
    vector_timeout_seconds: float = Field(default=10.0, gt=0)
    # Ask the language model to rephrase the free text before it is embedded. Off by default: it
    # costs a model call per search and sends the question to the model.
    llm_rewrite: bool = False


class SyncSettings(_Section):
    # Rarely needed: each account names its own provider. Only code that asks for "the" provider
    # without naming one reads this, and core knows no provider by name.
    provider: str = ""
    # The schedule a new account starts with (every 15 minutes). After that the schedule lives in
    # the database and is edited there; changing this does not touch existing schedules.
    interval_seconds: int = Field(default=900, ge=1)
    # The timezone a new schedule starts with, an IANA name from the system's tz database. It
    # matters for cron schedules and quiet hours; an interval does not care.
    timezone: str = "UTC"
    # The shortest interval a schedule may have, enforced when it is set (Gmail has quotas).
    min_interval_seconds: int = Field(default=60, ge=1)
    # How many accounts may sync at the same time; the rest wait their turn.
    max_concurrent: int = Field(default=2, ge=1, le=64)
    # After a failed sync the next try is interval * 2**failures, at most this long, +/- jitter.
    backoff_max_seconds: int = Field(default=3600, ge=1)
    backoff_jitter: float = Field(default=0.2, ge=0, le=1)
    batch_size: int = Field(default=100, ge=1)


class ReadStateSettings(_Section):
    """What may mark mail read, and how the audit log is cut up (prompt 12)."""

    # False turns off every *automatic* mark (opening, summarizing). An explicit mark read or
    # mark unread still works.
    auto_mark: bool = True
    # Message ids per audit row. A bigger change is several rows that share a batch id, and one
    # undo reverses them all.
    audit_chunk: int = Field(default=500, ge=1, le=5000)


class OperationsSettings(_Section):
    """Limits and the confirm gate of the operation registry (prompt 13). The ``mcp_`` values are
    what an MCP caller gets; tighter, because every character an agent reads costs tokens."""

    # Most messages one call may list or search, and most characters of mail text one reply holds.
    max_results: int = Field(default=50, ge=1, le=1000)
    max_chars: int = Field(default=20_000, ge=500)
    mcp_max_results: int = Field(default=20, ge=1, le=1000)
    mcp_max_chars: int = Field(default=8_000, ge=500)
    # How much of a message a listing or search shows by default (the full text is get_message).
    snippet_chars: int = Field(default=240, ge=20, le=5000)
    # Message ids in one mark-read, mark-unread or undo; one audit batch, one undo.
    max_batch_ids: int = Field(default=200, ge=1, le=5000)
    # Messages summarized in one call: each is a model call, so the cap is lower.
    max_summarize_ids: int = Field(default=20, ge=1, le=500)
    # Bytes of a raw message ``get_raw`` returns (the full message stays in raw.db).
    max_raw_bytes: int = Field(default=256 * 1024, ge=1024)
    # How long a proposal's confirmation token lives, and how many one caller may hold.
    confirm_ttl_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_pending_confirmations: int = Field(default=1000, ge=1)
    # Changes listed in a proposal (the total is always stated).
    proposal_changes_shown: int = Field(default=50, ge=1, le=1000)


class McpSettings(_Section):
    """Which operations MCP clients may call, from the file so Oaken runs headless.

    ``allowed`` is the user-choice layer, not the code ceiling. The registry's ``mcp="never"`` is
    an immovable floor no entry here can lift; a key is an operation name (``get_message``) or a
    plugin name (``gmail``, every op it offers), mapped to true (allow) or false (deny). An
    operation's own line beats its plugin's line. A key naming an unknown or ``never`` operation is
    warned about and ignored on load, a typo must never brick a headless start. The file is the
    source of truth; the ``operation_settings`` table is the live-read cache seeded from here.
    """

    allowed: dict[str, bool] = Field(default_factory=dict)
    # Which workspaces an MCP client may reach: only mail whose account is in one of these. An empty
    # list means every workspace. A name that is not a workspace matches nothing (a typo hides mail,
    # it never exposes it). A fresh install lists only ``default``. See ``oaken.core.operations.scope``.
    workspaces: list[str] = Field(default_factory=lambda: ["default"])


class UISettings(_Section):
    # How long a message must stay presented before core marks it read. Core owns the timer, so
    # every client behaves the same.
    mark_read_delay_ms: int = Field(default=2000, ge=0, le=600_000)


# Spells OAKEN on a phone keypad, and is above Linux's ephemeral range (32768 to 60999).
DEFAULT_PORT = 62536


class APISettings(_Section):
    # The address the API listens on. Anything but loopback needs ``allow_remote = true``: the
    # console pages (/, /config, /jobs, /stats) stay for clients on this machine either way, and
    # only the token-protected API is reachable from elsewhere. The token travels in clear text,
    # so reach it through an SSH tunnel or a TLS proxy, not over an open network.
    host: str = "127.0.0.1"
    allow_remote: bool = False
    # Names (``oaken.lan``) or addresses a client may put in the Host header besides loopback and
    # ``host`` itself, for a reverse proxy or a DNS name. The port is added for you.
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def _host_is_an_address(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("host must be an IP address such as 127.0.0.1 or 0.0.0.0") from None
        return value

    @model_validator(mode="after")
    def _remote_is_deliberate(self) -> "APISettings":
        if not ipaddress.ip_address(self.host).is_loopback and not self.allow_remote:
            raise ValueError(
                f"host {self.host} is not loopback: set allow_remote = true to accept that the "
                "API (token-protected) is reachable from other machines"
            )
        return self

    # A fixed default, so clients find core even without the runtime file. 0 still picks a free port.
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    # The MCP server (ADR 0004) mounts at ``/mcp`` on this listener when true. It has its own
    # token and its own limits, so a heavy MCP call cannot starve the general API.
    mcp: bool = False
    mcp_max_body_bytes: int = Field(default=64 * 1024, ge=1024)
    mcp_max_concurrent: int = Field(default=8, ge=1, le=256)
    # Bytes of one tool result (a larger one is refused with ``result_too_large``).
    mcp_max_result_bytes: int = Field(default=64 * 1024, ge=1024)
    # A folder of static files served at ``/`` (for a client UI). Empty or missing: core's
    # own welcome page.
    static_dir: str = ""
    # One browser origin allowed to call the API cross-origin, for UI development only, for
    # example ``http://localhost:5173``. Empty means no CORS headers at all.
    dev_origin: str = ""
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    # Mutating requests per client per minute (a token bucket; the burst is a tenth of it, at
    # least ten).
    rate_limit_per_minute: int = Field(default=600, ge=1)
    # Events kept for ``Last-Event-ID`` resume, and the quiet time before a keep-alive comment.
    event_ring_size: int = Field(default=1000, ge=1, le=100_000)
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    # Seconds in-flight jobs get to finish when the daemon is told to stop.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0)

    @field_validator("dev_origin")
    @classmethod
    def _dev_origin_is_one_origin(cls, value: str) -> str:
        if value and not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?", value):
            raise ValueError("dev_origin must be one origin like http://localhost:5173")
        return value


class LogSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["console", "json"] = "console"
    # A log file, in addition to stderr; relative paths sit under the data directory. Empty: stderr
    # only (which a detached core discards).
    file: str = ""
    file_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    file_backups: int = Field(default=5, ge=0, le=100)  # rotated copies kept: oaken.log.1 ...
    # Every event core publishes (ids, kinds and numbers, never mail text) as one JSON line, so
    # ``oaken events`` has history after a restart. Relative paths sit under the data directory;
    # empty turns the log off. Owner-only, and rotated like the log file.
    events_file: str = "events.jsonl"
    events_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    events_backups: int = Field(default=3, ge=0, le=100)


class Settings(BaseSettings):
    """``Settings(...)`` is pure: it reads neither the file nor the environment.
    ``Settings.load()`` applies the full precedence."""

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    storage: StorageSettings = StorageSettings()
    ai: AISettings = AISettings()
    jobs: JobsSettings = JobsSettings()
    ingest: IngestSettings = IngestSettings()
    # [workspaces.<name>]; "default" always exists (secure, ``ai.llm_backend``) unless redefined.
    workspaces: dict[str, WorkspaceSettings] = Field(default_factory=dict)
    swarm: SwarmSettings = SwarmSettings()
    mail: MailSettings = MailSettings()
    index: IndexSettings = IndexSettings()
    search: SearchSettings = SearchSettings()
    sync: SyncSettings = SyncSettings()
    read_state: ReadStateSettings = ReadStateSettings()
    operations: OperationsSettings = OperationsSettings()
    mcp: McpSettings = McpSettings()
    ui: UISettings = UISettings()
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
        _refuse_if_writable(path)

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


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return str(value)


def default_config_text(mcp_menu: list[tuple[str, str | None, bool]] | None = None) -> str:
    """A ``config.toml`` with every setting at its default, commented out, built from the models
    so it cannot drift from the code. Uncomment a line to change it.

    ``mcp_menu`` (name, plugin, mcp_exposed) for each togglable operation, from the registry, which
    this module must not import, produces the commented ``[mcp.allowed]`` menu. Each operation and
    each plugin group is shown at its code default so a reader can uncomment and flip it."""
    lines = [
        "# Oaken Mail configuration. Every line below shows the default and is commented out:",
        "# uncomment and edit what you want to change, then reload (or use the /config page).",
        "# Unknown keys are an error. Secrets never go here; they live in the OS keyring.",
        "",
    ]
    for name, field in Settings.model_fields.items():
        default = field.get_default(call_default_factory=True)
        if not isinstance(default, BaseModel):
            continue  # free-form tables ([plugins.*], [workspaces.*]) are added by hand
        lines.append(f"# [{name}]")
        for key, value in default.model_dump().items():
            if isinstance(value, dict):
                continue
            lines.append(f"# {key} = {_toml_value(value)}")
        lines.append("")
    if mcp_menu:
        lines += _mcp_allowed_menu(mcp_menu)
    return "\n".join(lines)


def _mcp_allowed_menu(menu: list[tuple[str, str | None, bool]]) -> list[str]:
    """The commented ``[mcp.allowed]`` block: which operations MCP clients may call. A key is an
    operation name or a plugin name (every op it offers); an op's own line beats its plugin's.
    ``mcp="never"`` operations are not listed (no entry can expose them). File wins on reload."""
    lines = [
        "# [mcp.allowed]",
        "# Which operations MCP clients may call, from the file so Oaken runs headless.",
        "# Key = an operation name, or a plugin name (all its operations). true allows, false denies.",  # noqa: E501
        "# An operation's own line beats its plugin's line. Never-exposable operations are omitted.",  # noqa: E501
        "# Each line shows the code default; uncomment to override. Regenerate with: oaken mcp scaffold",  # noqa: E501
    ]
    plugins = sorted({p for _, p, _ in menu if p is not None})
    for plugin in plugins:
        lines.append(f"# {_toml_value(plugin)[1:-1]} = true  # all of {plugin}")
    for name, _plugin, exposed in sorted(menu):
        lines.append(f"# {name} = {_toml_value(exposed)}")
    lines.append("")
    return lines


def mcp_allowed_menu_from_registry() -> list[tuple[str, str | None, bool]]:
    """(name, plugin, mcp_exposed) for every togglable operation in the core registry, for the
    commented ``[mcp.allowed]`` menu. Lazy import: settings must not depend on the registry at
    module load. Plugin operations are not here (plugins load later); ``oaken mcp scaffold``
    regenerates the menu against the live registry once plugins are up."""
    from oaken.core.operations.builtin import core_registry

    return [
        (op.name, op.plugin, op.mcp_exposed)
        for op in core_registry().all()
        if op.mcp_allowed  # omit mcp="never": no entry can expose them
    ]


def ensure_default_config() -> Path | None:
    """Write the commented default ``config.toml`` if there is none. Returns its path when it
    was written, ``None`` when a file was already there (it is never overwritten)."""
    path = paths.config_dir() / CONFIG_FILE
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(default_config_text(mcp_allowed_menu_from_registry()))
    except FileExistsError:
        return None
    path.chmod(0o600)
    return path


def _describe(exc: ValidationError, path: Path) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return f"invalid configuration ({path}): " + "; ".join(parts)
