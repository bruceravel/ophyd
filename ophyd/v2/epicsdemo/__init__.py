"""Demo EPICS Devices for the tutorial"""

import asyncio
import time
from enum import Enum
from typing import Callable, List, Optional

import numpy as np
from bluesky.protocols import Movable, Stoppable

from ophyd.v2.core import AsyncStatus, StandardReadable, observe_value
from ophyd.v2.epics import EpicsSignalR, EpicsSignalRW, EpicsSignalX


class EnergyMode(Enum):
    """Energy mode for `Sensor`"""

    #: Low energy mode
    low = "Low Energy"
    #: High energy mode
    high = "High Energy"


class Sensor(StandardReadable):
    """A demo sensor that produces a scalar value based on X and Y Movers"""

    def __init__(self, prefix: str, name="") -> None:
        # Define some signals
        self.value = EpicsSignalR(float, "Value")
        self.mode = EpicsSignalRW(EnergyMode, "Mode")
        # Set prefix, name, and signals for read() and read_configuration()
        super().__init__(
            prefix=prefix,
            name=name,
            read=[self.value],
            config=[self.mode],
        )


class Mover(StandardReadable, Movable, Stoppable):
    """A demo movable that moves based on velocity"""

    def __init__(self, prefix: str, name="") -> None:
        # Define some signals
        self.setpoint = EpicsSignalRW(float, "Setpoint")
        self.readback = EpicsSignalR(float, "Readback")
        self.velocity = EpicsSignalRW(float, "Velocity")
        self.units = EpicsSignalR(str, "Readback.EGU")
        self.precision = EpicsSignalR(int, "Readback.PREC")
        # Signals that collide with standard methods should have a trailing underscore
        self.stop_ = EpicsSignalX("Stop.PROC", write_value=1)
        self._success = True
        # Set prefix, name, and signals for read() and read_configuration()
        super().__init__(
            prefix=prefix,
            name=name,
            primary=self.readback,
            config=[self.velocity, self.units],
        )

    async def _move(self, new_position: float, watchers: List[Callable] = []):
        self._success = True
        # time.monotonic won't go backwards in case of NTP corrections
        start = time.monotonic()
        old_position, units, precision = await asyncio.gather(
            self.setpoint.get_value(),
            self.units.get_value(),
            self.precision.get_value(),
        )
        # Wait for the value to set, but don't wait for put completion callback
        await self.setpoint.set(new_position, wait=False)
        async for current_position in observe_value(self.readback):
            for watcher in watchers:
                watcher(
                    name=self.name,
                    current=current_position,
                    initial=old_position,
                    target=new_position,
                    unit=units,
                    precision=precision,
                    time_elapsed=time.monotonic() - start,
                )
            if np.isclose(current_position, new_position):
                break
        if not self._success:
            raise RuntimeError("Motor was stopped")

    def move(self, new_position: float, timeout: Optional[float] = None):
        """Commandline only synchronous move of a Motor"""
        from bluesky.run_engine import call_in_bluesky_event_loop, in_bluesky_event_loop

        if in_bluesky_event_loop():
            raise RuntimeError("Will deadlock run engine if run in a plan")
        call_in_bluesky_event_loop(self._move(new_position), timeout)  # type: ignore

    # TODO: this fails if we call from the cli, but works if we "ipython await" it
    def set(self, new_position: float, timeout: Optional[float] = None) -> AsyncStatus:
        watchers: List[Callable] = []
        coro = asyncio.wait_for(self._move(new_position, watchers), timeout=timeout)
        return AsyncStatus(coro, watchers)

    async def stop(self, success=True):
        self._success = success
        await self.stop_.execute()


class SampleStage(StandardReadable):
    """A demo sample stage with X and Y movables"""

    def __init__(self, prefix: str, name="") -> None:
        # Define some child Devices
        self.x = Mover("X:")
        self.y = Mover("Y:")
        # Set prefix and name
        super().__init__(prefix, name)


def start_ioc_subprocess() -> str:
    """Start an IOC subprocess with EPICS database for sample stage and sensor
    with the same pv prefix
    """
    import atexit
    import random
    import string
    import subprocess
    import sys
    from pathlib import Path

    pv_prefix = "".join(random.choice(string.ascii_uppercase) for _ in range(12)) + ":"
    here = Path(__file__).absolute().parent
    args = [sys.executable, "-m", "epicscorelibs.ioc"]
    args += ["-m", f"P={pv_prefix}"]
    args += ["-d", str(here / "sensor.db")]
    for suff in "XY":
        args += ["-m", f"P={pv_prefix}{suff}:"]
        args += ["-d", str(here / "mover.db")]
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    atexit.register(process.communicate, "exit")
    return pv_prefix
