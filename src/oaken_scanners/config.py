# SPDX-License-Identifier: Apache-2.0
"""``[plugins.action.clamav]``. How to reach ``clamd``: a unix socket, or a host and port. Nothing
here is a secret."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClamavConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # One of these two. A unix socket (``/var/run/clamav/clamd.ctl``) is the usual local setup;
    # host/port is for a clamd reached over TCP. Give the socket or the host, not both.
    socket: str | None = Field(default=None, description="Path to clamd's unix socket.")
    host: str | None = Field(default=None, description="clamd host, for a TCP connection.")
    port: int = Field(default=3310, ge=1, le=65535, description="clamd TCP port.")
    timeout_seconds: float = Field(
        default=120.0, gt=0,
        description="How long to wait for clamd to connect and to answer. A timeout is an error "
        "(the file stays held), never a clean verdict.",
    )  # fmt: skip

    @model_validator(mode="after")
    def _one_transport(self) -> "ClamavConfig":
        if bool(self.socket) == bool(self.host):
            raise ValueError("clamav needs exactly one of `socket` or `host`")
        return self
