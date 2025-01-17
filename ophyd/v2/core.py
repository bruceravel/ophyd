"""Core Ophyd.v2 functionality like Device and Signal"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from abc import abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    TypeVar,
    cast,
    runtime_checkable,
)

from bluesky.protocols import (
    Configurable,
    Descriptor,
    HasName,
    Movable,
    Readable,
    Reading,
    Stageable,
    Status,
    Subscribable,
)
from bluesky.run_engine import call_in_bluesky_event_loop

T = TypeVar("T")
Callback = Callable[[T], None]


class AsyncStatus(Status):
    "Convert asyncio awaitable to bluesky Status interface"

    def __init__(
        self,
        awaitable: Awaitable,
        watchers: Optional[List[Callable]] = None,
    ):
        if isinstance(awaitable, asyncio.Task):
            self.task = awaitable
        else:
            self.task = asyncio.create_task(awaitable)  # type: ignore
        self.task.add_done_callback(self._run_callbacks)
        self._callbacks = cast(List[Callback[Status]], [])
        self._watchers = watchers

    def __await__(self):
        return self.task.__await__()

    def add_callback(self, callback: Callback[Status]):
        if self.done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def _run_callbacks(self, task: asyncio.Task):
        if not task.cancelled():
            for callback in self._callbacks:
                callback(self)

    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def success(self) -> bool:
        assert self.done, "Status has not completed yet"
        try:
            self.task.result()
        except (Exception, asyncio.CancelledError):
            logging.exception("Failed status")
            return False
        else:
            return True

    def watch(self, watcher: Callable):
        """Add watcher to the list of interested parties.

        Arguments as per Bluesky :external+bluesky:meth:`watch` protocol.
        """
        if self._watchers is not None:
            self._watchers.append(watcher)

    @classmethod
    def wrap(cls, f: Callable[[T], Coroutine]) -> Callable[[T], AsyncStatus]:
        @functools.wraps(f)
        def wrap_f(self) -> AsyncStatus:
            return AsyncStatus(f(self))

        return wrap_f


class Device(HasName):
    """Common base class for all Ophyd.v2 Devices"""

    #: The parent Device if it exists
    parent: Optional[Device] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of the Device"""

    @abstractmethod
    def set_name(self, name: str = ""):
        """Set ``self.name=name`` and each ``self.child.name=name+"-child"``.

        Parameters
        ----------
        name:
            New name to set, do nothing if blank or name is all set
        """

    @abstractmethod
    async def connect(self, prefix: str = "", sim=False):
        """Connect self and all child Devices.

        Parameters
        ----------
        prefix:
            Device specific prefix that can be used to nest Devices one within
            another. For example a PV prefix.
        sim:
            If True then connect in simulation mode.
        """


class NotConnected(Exception):
    """Exception to be raised if a `Device.connect` is cancelled"""

    def __init__(self, *lines: str):
        self.lines = list(lines)

    def __str__(self) -> str:
        return "\n".join(self.lines)


async def wait_for_connection(**coros: Awaitable[None]):
    """Call many underlying signals, accumulating `NotConnected` exceptions

    Raises
    ------
    `NotConnected` if cancelled
    """
    ts = {k: asyncio.create_task(c) for (k, c) in coros.items()}  # type: ignore
    try:
        await asyncio.wait(ts.values())
    except asyncio.CancelledError:
        for t in ts.values():
            t.cancel()
        lines: List[str] = []
        for k, t in ts.items():
            try:
                await t
            except NotConnected as e:
                if len(e.lines) == 1:
                    lines.append(f"{k}: {e.lines[0]}")
                else:
                    lines.append(f"{k}:")
                    lines += [f"  {line}" for line in e.lines]
        raise NotConnected(*lines)


async def connect_children(device: Device, prefix: str, sim: bool):
    """Call ``child.connect(prefix, sim)`` on all child devices in parallel.

    Typically used to implement `Device.connect` like this::

        async def connect(self, prefix: str = "", sim=False):
            await connect_children(self, prefix + self.prefix, sim)
    """
    coros = {
        k: c.connect(prefix, sim)
        for k, c in device.__dict__.items()
        if k != "parent" and isinstance(c, Device)
    }
    await wait_for_connection(**coros)


class DeviceCollector:
    """Collector of top level Device instances to be used as a context manager

    Parameters
    ----------
    set_name:
        If True, call ``device.set_name(variable_name)`` on all collected
        Devices
    connect:
        If True, call ``device.connect(prefix, sim)`` in parallel on all
        collected Devices
    sim:
        If True, connect Signals in simulation mode
    prefix:
        If passed, pass a global prefix to all device connects
    timeout:
        How long to wait for connect before logging an exception

    Notes
    -----
    Example usage::

        [async] with DeviceCollector():
            t1x = motor.Motor("BLxxI-MO-TABLE-01:X")
            t1y = motor.Motor("pva://BLxxI-MO-TABLE-01:Y")
            # Names and connects devices here
        assert t1x.comm.velocity.source
        assert t1x.name == "t1x"

    """

    def __init__(
        self,
        set_name=True,
        connect=True,
        sim=False,
        prefix: str = "",
        timeout: float = 10.0,
    ):
        self._set_name = set_name
        self._connect = connect
        self._sim = sim
        self._prefix = prefix
        self._timeout = timeout
        self._names_on_enter: Set[str] = set()
        self._objects_on_exit: Dict[str, Any] = {}

    def _caller_locals(self):
        """Walk up until we find a stack frame that doesn't have us as self"""
        try:
            raise ValueError
        except ValueError:
            _, _, tb = sys.exc_info()
            assert tb, "Can't get traceback, this shouldn't happen"
            caller_frame = tb.tb_frame
            while caller_frame.f_locals.get("self", None) is self:
                caller_frame = caller_frame.f_back
            return caller_frame.f_locals

    def __enter__(self):
        # Stash the names that were defined before we were called
        self._names_on_enter = set(self._caller_locals())
        return self

    async def __aenter__(self):
        return self.__enter__()

    async def _on_exit(self):
        # Name and kick off connect for devices
        tasks: Dict[asyncio.Task, str] = {}
        for name, obj in self._objects_on_exit.items():
            if name not in self._names_on_enter and isinstance(obj, Device):
                if self._set_name:
                    obj.set_name(name)
                if self._connect:
                    task = asyncio.create_task(obj.connect(self._prefix, self._sim))
                    tasks[task] = name
        # Wait for all the signals to have finished
        if tasks:
            await self._wait_for_tasks(tasks)

    async def _wait_for_tasks(self, tasks: Dict[asyncio.Task, str]):
        done, pending = await asyncio.wait(tasks, timeout=self._timeout)
        not_connected = list(pending) + list(t for t in done if t.exception())
        if not_connected:
            msg = f"{len(not_connected)} Devices did not connect:"
            for t in pending:
                t.cancel()
            for t in pending:
                with suppress(Exception):
                    await t
            for task in not_connected:
                e = task.exception()
                msg += f"\n  {tasks[task]}: {type(e).__name__}"
                lines = str(e).splitlines()
                if len(lines) <= 1:
                    msg += str(e)
                else:
                    msg += "".join(f"\n    {line}" for line in lines)
            logging.error(msg)

    async def __aexit__(self, type, value, traceback):
        self._objects_on_exit = self._caller_locals()
        await self._on_exit()

    def __exit__(self, type_, value, traceback):
        self._objects_on_exit = self._caller_locals()
        return call_in_bluesky_event_loop(self._on_exit())


def _fail(self, other, *args, **kwargs):
    if isinstance(other, Signal):
        raise TypeError(
            "Can't compare two Signals, did you mean await signal.get_value() instead?"
        )
    else:
        return NotImplemented


class Signal(Device):
    """Signals are like ophyd Signals, but async"""

    _name = ""

    @property
    def name(self) -> str:
        return self._name

    def set_name(self, name: str = ""):
        self._name = name

    @property
    @abstractmethod
    def source(self) -> str:
        """Like ca://PV_PREFIX:SIGNAL, or "" if not set"""

    __lt__ = __le__ = __eq__ = __ge__ = __gt__ = __ne__ = _fail

    def __hash__(self):
        # Restore the default implementation so we can use in a set or dict
        return hash(id(self))


class SignalR(Signal, Readable, Stageable, Subscribable, Generic[T]):
    """Signal that can be read from and monitored"""

    @abstractmethod
    async def read(self, cached: Optional[bool] = None) -> Dict[str, Reading]:
        """Return a single item dict with the reading in it"""

    @abstractmethod
    async def describe(self) -> Dict[str, Descriptor]:
        """Return a single item dict with the descriptor in it"""

    @abstractmethod
    def stage(self) -> List[Any]:
        """Start caching this signal"""

    @abstractmethod
    def unstage(self) -> List[Any]:
        """Stop caching this signal"""

    @abstractmethod
    async def get_value(self, cached: Optional[bool] = None) -> T:
        """The current value"""

    @abstractmethod
    def subscribe_value(self, function: Callback[T]):
        """Subscribe to updates in value of a device"""

    @abstractmethod
    def subscribe(self, function: Callback[Dict[str, Reading]]) -> None:
        """Subscribe to updates in the reading"""

    @abstractmethod
    def clear_sub(self, function: Callback) -> None:
        """Remove a subscription."""


class SignalW(Signal, Movable, Generic[T]):
    """Signal that can be set"""

    @abstractmethod
    def set(self, value: T, wait=True) -> AsyncStatus:
        """Set the value and return a status saying when it's done"""


class SignalRW(SignalR[T], SignalW[T]):
    """Signal that can be both read and set"""


async def observe_value(signal: SignalR[T]) -> AsyncGenerator[T, None]:
    """Subscribe to the value of a signal so it can be iterated from.

    Parameters
    ----------
    signal:
        Call subscribe_value on this at the start, and clear_sub on it at the
        end

    Notes
    -----
    Example usage::

        async for value in observe_value(sig):
            do_something_with(value)
    """
    q: asyncio.Queue[T] = asyncio.Queue()
    signal.subscribe_value(q.put_nowait)
    try:
        while True:
            yield await q.get()
    finally:
        signal.clear_sub(q.put_nowait)


async def wait_for_value(signal: SignalR[T], match, timeout=None):
    """Wait for a signal to have a matching value.

    Parameters
    ----------
    signal:
        Call subscribe_value on this at the start, and clear_sub on it at the
        end
    match:
        If a callable, it should return True if the value matches. If not
        callable then value will be checked for euqlity with match.
    timeout:
        How long to wait for the value to match

    Notes
    -----
    Example usage::

        wait_for_value(device.acquiring, 1, timeout=1)

    Or::

        wait_for_value(device.num_captured, lambda v: v > 45, timeout=1)
    """

    async def _wait_for_value():
        if not callable(match):

            def match(value, match=match):
                return value == match

        async for value in observe_value(signal):
            if match(value):
                return

    _wait_for_value.__name__ = f"wait_for_{signal.name}_to_match_{match}"
    await asyncio.wait_for(_wait_for_value(), timeout)


@runtime_checkable
class AsyncReadable(Protocol):
    """Readable narrowed to only be async"""

    @abstractmethod
    async def read(self) -> Dict[str, Reading]:
        ...

    @abstractmethod
    async def describe(self) -> Dict[str, Descriptor]:
        ...


@dataclass
class _ReadableRenamer(AsyncReadable, Stageable):
    readable: AsyncReadable
    device: Device

    def _rename(self, d: Dict[str, T]) -> Dict[str, T]:
        return {self.device.name: v for v in d.values()}

    async def read(self) -> Dict[str, Reading]:
        return self._rename(await self.readable.read())

    async def describe(self) -> Dict[str, Descriptor]:
        return self._rename(await self.readable.describe())

    def stage(self) -> List[Any]:
        if isinstance(self.readable, Stageable):
            return self.readable.stage()
        else:
            return []

    def unstage(self) -> List[Any]:
        if isinstance(self.readable, Stageable):
            return self.readable.unstage()
        else:
            return []


@dataclass
class _UncachedSignal(AsyncReadable):
    signal: SignalR

    async def read(self) -> Dict[str, Reading]:
        return await self.signal.read(cached=False)

    async def describe(self) -> Dict[str, Descriptor]:
        return await self.signal.describe()


async def merge_gathered_dicts(
    coros: Iterable[Awaitable[Dict[str, T]]]
) -> Dict[str, T]:
    """Merge dictionaries produced by a sequence of coroutines.

    Can be used for merging ``read()`` or ``describe``. For instance::

        combined_read = await merge_gathered_dicts(s.read() for s in signals)
    """
    ret: Dict[str, T] = {}
    for result in await asyncio.gather(*coros):
        ret.update(result)
    return ret


class StandardReadable(Readable, Configurable, Stageable, Device):
    """Device that owns its children and provides useful default behavior.

    - When its name is set it renames child Devices
    - Signals can be registered for read() and read_configuration()
    - These signals will be subscribed for read() between stage() and unstage()
    """

    _name = ""

    def __init__(
        self,
        prefix: str,
        name: str = "",
        primary: Optional[SignalR] = None,
        read: Sequence[AsyncReadable] = (),
        read_uncached: Sequence[SignalR] = (),
        config: Sequence[AsyncReadable] = (),
    ):
        """
        Parameters
        ----------
        prefix:
            This will be passed as a prefix to all child Device connects
        name:
            If set, name the Device and its children
        primary:
            Optional single Signal that will be named self.name
        read:
            Signals to make up `read()` that can be cached
        read_uncached:
            Signals to make up `read()` that should not be cached
        conf:
            Signals to make up `read_configuration()` that can be cached
        """
        self._init_prefix = prefix
        self._read_signals = tuple(read) + tuple(
            _UncachedSignal(sig) for sig in read_uncached
        )
        if primary:
            self._read_signals = (_ReadableRenamer(primary, self),) + self._read_signals
        self._conf_signals = tuple(config)
        self._staged = False
        # Call this last so child Signals are renamed
        self.set_name(name)

    @property
    def name(self) -> str:
        return self._name

    def set_name(self, name: str = ""):
        if name and not self._name:
            self._name = name
            for attr_name, attr in self.__dict__.items():
                # TODO: support lists and dicts of devices
                if isinstance(attr, Device):
                    attr.set_name(f"{name}-{attr_name.rstrip('_')}")
                    attr.parent = self

    async def connect(self, prefix: str = "", sim=False):
        # Add pv prefix to child Signals and connect them
        await connect_children(self, prefix + self._init_prefix, sim)

    def stage(self) -> List[Any]:
        self._staged = True
        staged = [self]
        for sig in self._read_signals + self._conf_signals:
            if isinstance(sig, Stageable):
                staged += sig.stage()
        return staged

    def unstage(self) -> List[Any]:
        self._staged = False
        unstaged = [self]
        for sig in self._read_signals + self._conf_signals:
            if isinstance(sig, Stageable):
                unstaged += sig.unstage()
        return unstaged

    async def describe(self) -> Dict[str, Descriptor]:
        return await merge_gathered_dicts(sig.describe() for sig in self._read_signals)

    async def read(self) -> Dict[str, Reading]:
        return await merge_gathered_dicts(sig.read() for sig in self._read_signals)

    async def describe_configuration(self) -> Dict[str, Descriptor]:
        return await merge_gathered_dicts(sig.describe() for sig in self._conf_signals)

    async def read_configuration(self) -> Dict[str, Reading]:
        return await merge_gathered_dicts(sig.read() for sig in self._conf_signals)
