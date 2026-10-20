# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken import __version__
from oaken.cli import main


def test_cli_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert capsys.readouterr().out.strip() == f"oaken {__version__}"
