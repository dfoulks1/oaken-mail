# SPDX-License-Identifier: Apache-2.0
"""The Lifecycle starts units in order and stops the started ones in reverse (ADR 0011)."""

import pytest

from oaken.core.errors import OakenError
from oaken.daemon.units import Lifecycle, ServiceUnit, unit


class _Recorder:
    """Builds units that append ``start:<name>`` / ``stop:<name>`` to a shared log."""

    def __init__(self) -> None:
        self.log: list[str] = []

    def make(self, name: str, *, fail_start: bool = False, fail_stop: bool = False) -> ServiceUnit:
        async def start() -> None:
            self.log.append(f"start:{name}")
            if fail_start:
                raise OakenError(f"{name} refused to start")

        async def stop() -> None:
            self.log.append(f"stop:{name}")
            if fail_stop:
                raise OakenError(f"{name} refused to stop")

        return unit(name, start, stop)


async def test_units_start_in_order_and_record_their_names() -> None:
    rec = _Recorder()
    lifecycle = Lifecycle([rec.make("a"), rec.make("b"), rec.make("c")])
    started: list[str] = []
    await lifecycle.start(started)
    assert rec.log == ["start:a", "start:b", "start:c"]
    assert started == ["a", "b", "c"]


async def test_stop_runs_in_reverse_and_clears_the_record() -> None:
    rec = _Recorder()
    lifecycle = Lifecycle([rec.make("a"), rec.make("b"), rec.make("c")])
    started: list[str] = []
    await lifecycle.start(started)
    rec.log.clear()
    await lifecycle.stop(started)
    assert rec.log == ["stop:c", "stop:b", "stop:a"]
    assert started == []


async def test_a_unit_that_fails_to_start_aborts_and_stops_the_already_started() -> None:
    rec = _Recorder()
    lifecycle = Lifecycle([rec.make("a"), rec.make("b", fail_start=True), rec.make("c")])
    started: list[str] = []
    with pytest.raises(OakenError, match="b refused to start"):
        await lifecycle.start(started)
    # a started, b attempted and raised, c never started; a is stopped on the way out.
    assert rec.log == ["start:a", "start:b", "stop:a"]
    assert started == []  # nothing left up


async def test_stop_is_safe_when_nothing_started() -> None:
    rec = _Recorder()
    lifecycle = Lifecycle([rec.make("a"), rec.make("b")])
    started: list[str] = []
    await lifecycle.stop(started)  # no-op, no raise
    assert rec.log == [] and started == []


async def test_a_failing_stop_is_logged_and_the_rest_still_stop() -> None:
    rec = _Recorder()
    lifecycle = Lifecycle([rec.make("a"), rec.make("b", fail_stop=True), rec.make("c")])
    started: list[str] = []
    await lifecycle.start(started)
    rec.log.clear()
    await lifecycle.stop(started)  # b's stop raises, but a and c still stop
    assert rec.log == ["stop:c", "stop:b", "stop:a"]
    assert started == []
