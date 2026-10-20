# SPDX-License-Identifier: Apache-2.0
import io
import json

import pytest

from oaken.core.logging import MAX_FIELD, REDACTED, configure_logging, get_logger
from oaken.core.settings import LogSettings


def run(fmt: str = "json", level: str = "INFO") -> io.StringIO:
    out = io.StringIO()
    configure_logging(LogSettings(format=fmt, level=level), stream=out)  # type: ignore[arg-type]
    return out


def last(out: io.StringIO) -> dict[str, object]:
    return json.loads(out.getvalue().strip().splitlines()[-1])  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "key", ["token", "access_token", "client_secret", "Password", "Authorization", "x_TOKEN_y"]
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    out = run()
    get_logger().info("hello", **{key: "hunter2"})
    row = last(out)
    assert row[key] == REDACTED and "hunter2" not in out.getvalue()


def test_redaction_reaches_nested_values() -> None:
    out = run()
    nested = {"api": {"password": "hunter2"}}  # pragma: allowlist secret
    get_logger().info("hello", cfg=nested, items=[{"token": "t"}])
    text = out.getvalue()
    assert "hunter2" not in text and '"token": "t"' not in text


def test_long_fields_are_truncated() -> None:
    out = run()
    get_logger().info("body", body="x" * 5000, short="ok")
    row = last(out)
    assert isinstance(row["body"], str) and len(row["body"]) < MAX_FIELD + 40
    assert row["body"].startswith("x" * MAX_FIELD) and "truncated 4800" in row["body"]
    assert row["short"] == "ok"


def test_level_filter_and_console_renderer() -> None:
    out = run(fmt="console", level="WARNING")
    log = get_logger("t")
    log.info("quiet")
    log.warning("loud")
    text = out.getvalue()
    assert "loud" in text and "quiet" not in text


def test_logs_go_to_stderr_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LogSettings(format="json"))
    get_logger().info("to-stderr")
    captured = capsys.readouterr()
    assert "to-stderr" in captured.err and captured.out == ""
