# SPDX-License-Identifier: Apache-2.0
"""``[plugins.gmail]``. Nothing here is a secret: the client secret is read from the file named
by ``client_secret_file`` and the refresh token lives only in the OS keyring."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oaken.core.errors import PluginError

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a password)


class GmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret_file: Path | None = None  # the OAuth client JSON from Google Cloud
    account: str = "default"  # names the keyring entry: oaken / gmail:<account>
    include_labels: list[str] = []  # label names or ids; empty means all mail
    exclude_labels: list[str] = []  # label names or ids; Spam and Trash are always excluded
    page_size: int = Field(default=100, ge=1, le=500)  # messages per batch of a full sync
    concurrency: int = Field(default=8, ge=1, le=50)  # requests in flight at once
    timeout: float = Field(default=30.0, gt=0)  # seconds, per request
    max_retries: int = Field(default=5, ge=0, le=20)  # extra tries on 429 / rate limit / 5xx
    backoff: float = Field(default=1.0, ge=0)  # seconds, doubled each retry

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "GmailConfig":
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            raise PluginError(f"gmail: invalid [plugins.gmail] settings ({fields})") from None
