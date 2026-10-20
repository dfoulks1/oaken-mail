# SPDX-License-Identifier: Apache-2.0
"""``[plugins.imap]``. Nothing here is a secret: the password lives only in the OS keyring
(service ``oaken``, user ``imap:<account>``). There is deliberately no way to ask for plaintext."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oaken.core.errors import PluginError

PORTS = {"tls": 993, "starttls": 143}


class ImapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)  # 993 for tls, 143 for starttls
    security: Literal["tls", "starttls"] = "tls"  # "tls" is implicit TLS; "starttls" upgrades
    username: str = Field(min_length=1)
    account: str = "default"  # names the keyring entry: oaken / imap:<account>
    include_folders: list[str] = []  # folder names; empty means every folder
    exclude_folders: list[str] = []  # folder names; Junk and Trash are always excluded
    trash_folder: str | None = None  # where "trash" moves mail, if the server does not say
    archive_folder: str | None = None  # where "archive" moves mail, if the server does not say
    page_size: int = Field(default=25, ge=1, le=200)  # messages fetched per command
    flag_scan_limit: int = Field(
        default=2000, ge=0
    )  # newest messages re-read for flags (no CONDSTORE)
    timeout: float = Field(default=60.0, gt=0)  # seconds, for connecting and for each command

    @property
    def effective_port(self) -> int:
        return self.port or PORTS[self.security]

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "ImapConfig":
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            raise PluginError(f"imap: invalid [plugins.imap] settings ({fields})") from None
