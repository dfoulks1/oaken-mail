# SPDX-License-Identifier: Apache-2.0
import io
import json
from pathlib import Path

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


def test_a_log_file_gets_the_lines_and_stderr_still_does(tmp_path: Path) -> None:
    path = tmp_path / "oaken.log"
    out = io.StringIO()
    configure_logging(LogSettings(format="json", file=str(path)), stream=out)
    get_logger().info("hello", token="hunter2")  # noqa: S106
    for text in (path.read_text(), out.getvalue()):
        assert json.loads(text.strip())["event"] == "hello"
        assert "hunter2" not in text
    assert path.stat().st_mode & 0o077 == 0


def test_a_log_file_rotates_by_size_and_keeps_the_configured_backups(tmp_path: Path) -> None:
    path = tmp_path / "oaken.log"
    configure_logging(
        LogSettings(format="json", file=str(path), file_max_bytes=1024, file_backups=2),
        stream=io.StringIO(),
    )
    for i in range(60):
        get_logger().info("line", n=i, pad="x" * 100)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["oaken.log", "oaken.log.1", "oaken.log.2"]
    assert all(p.stat().st_size <= 1024 + 200 for p in tmp_path.iterdir())
    newest = json.loads(path.read_text().strip().splitlines()[-1])
    assert newest["n"] == 59


def test_a_relative_log_file_sits_under_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path))
    configure_logging(LogSettings(file="logs/oaken.log"), stream=io.StringIO())
    get_logger().info("hello")
    assert (tmp_path / "data" / "logs" / "oaken.log").exists()
