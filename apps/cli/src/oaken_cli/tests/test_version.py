# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken import __version__
from oaken_cli import main


def test_cli_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"oaken {__version__}"
