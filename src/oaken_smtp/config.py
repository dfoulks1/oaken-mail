# SPDX-License-Identifier: Apache-2.0
"""``[plugins.smtp]``. Nothing here is a secret: the password lives only in the OS keyring
(service ``oaken``, user ``smtp:<account>``). There is deliberately no way to ask for plaintext."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oaken.core.errors import PluginError

PORTS = {"starttls": 587, "tls": 465}


class SmtpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)  # 587 for starttls, 465 for tls
    security: Literal["starttls", "tls"] = "starttls"  # "starttls" upgrades; "tls" is implicit
    username: str = ""  # defaults to from_address
    from_address: str = Field(min_length=3)  # the envelope sender (MAIL FROM)
    account: str = "default"  # names the keyring entry: oaken / smtp:<account>
    timeout: float = Field(default=30.0, gt=0)  # seconds, for connecting and for each command

    @property
    def effective_port(self) -> int:
        return self.port or PORTS[self.security]

    @property
    def login(self) -> str:
        return self.username or self.from_address

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "SmtpConfig":
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            raise PluginError(f"smtp: invalid [plugins.smtp] settings ({fields})") from None
