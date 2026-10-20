# SPDX-License-Identifier: Apache-2.0
"""Error taxonomy. Every error carries a stable machine ``code`` that clients can match on."""


class OakenError(Exception):
    """Base class for every error Oaken raises on purpose."""

    code = "oaken_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigError(OakenError):
    code = "config_error"


class StorageError(OakenError):
    code = "storage_error"


class PluginError(OakenError):
    code = "plugin_error"


class ProviderError(OakenError):
    code = "provider_error"


class AIBackendError(OakenError):
    code = "ai_backend_error"


class PermissionDenied(OakenError):
    code = "permission_denied"


class NotFound(OakenError):
    code = "not_found"


class Conflict(OakenError):
    code = "conflict"


class QuotaExceeded(OakenError):
    code = "quota_exceeded"


class InvalidResult(OakenError):
    """A job result that fails its schema or size limit. The job is left leased."""

    code = "invalid_result"
