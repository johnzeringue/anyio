import socket
import sys
from contextlib import contextmanager
from ipaddress import ip_address
from typing import Callable, Set, List, Dict  # noqa: F401

import curio.io
import curio.socket
from async_generator import async_generator, asynccontextmanager, yield_

from .. import interfaces, T_Retval, claim_current_thread, _local
from ..exceptions import MultiError, CancelledError


def run(func: Callable[..., T_Retval], *args) -> T_Retval:
    kernel = None
    try:
        with curio.Kernel() as kernel:
            _local.cancel_scopes_by_task = {}  # type: Dict[curio.Task, CurioCancelScope]
            return kernel.run(func, *args)
    except BaseException:
        if kernel:
            kernel.run(shutdown=True)

        raise


@contextmanager
def translate_exceptions():
    try:
        yield
    except (curio.CancelledError, curio.TaskCancelled) as exc:
        raise CancelledError().with_traceback(exc.__traceback__) from None


#
# Timeouts and cancellation
#

class CurioCancelScope(interfaces.CancelScope):
    __slots__ = 'children', '_tasks', '_cancel_called'

    def __init__(self) -> None:
        self.children = set()  # type: Set[CurioCancelScope]
        self._tasks = set()  # type: Set[curio.Task]
        self._cancel_called = False

    def add_task(self, task: curio.Task) -> None:
        self._tasks.add(task)

    def remove_task(self, task: curio.Task) -> None:
        self._tasks.remove(task)

    async def cancel(self):
        if not self._cancel_called:
            self._cancel_called = True

            for task in self._tasks:
                if task.coro.cr_await is not None:
                    await task.cancel(blocking=False)

            for scope in self.children:
                await scope.cancel()


async def _check_cancelled():
    task = await curio.current_task()
    cancel_scope = _local.cancel_scopes_by_task.get(task)
    if cancel_scope is not None and cancel_scope._cancel_called:
        raise CancelledError


async def sleep(seconds: int):
    await _check_cancelled()
    await curio.sleep(seconds)


@asynccontextmanager
@async_generator
async def open_cancel_scope():
    await _check_cancelled()
    task = await curio.current_task()
    scope = CurioCancelScope()
    scope.add_task(task)
    parent_scope = _local.cancel_scopes_by_task.get(task)
    if parent_scope is not None:
        parent_scope.children.add(scope)

    _local.cancel_scopes_by_task[task] = scope
    try:
        await yield_(scope)
    finally:
        if parent_scope is not None:
            parent_scope.children.remove(scope)
            _local.cancel_scopes_by_task[task] = parent_scope
        else:
            del _local.cancel_scopes_by_task[task]


@asynccontextmanager
@async_generator
async def fail_after(delay: float):
    async with open_cancel_scope() as cancel_scope:
        async with curio.ignore_after(delay) as s:
            await yield_()

        if s.expired:
            await cancel_scope.cancel()
            raise TimeoutError


@asynccontextmanager
@async_generator
async def move_on_after(delay: float):
    async with curio.ignore_after(delay):
        await yield_()


#
# Task groups
#

class CurioTaskGroup:
    __slots__ = 'cancel_scope', '_active', '_tasks', '_host_task', '_exceptions'

    def __init__(self, cancel_scope: 'CurioCancelScope', host_task: curio.Task) -> None:
        self.cancel_scope = cancel_scope
        self._host_task = host_task
        self._active = True
        self._exceptions = []  # type: List[BaseException]
        self._tasks = set()  # type: Set[curio.Task]

    async def spawn(self, func: Callable, *args, name=None) -> None:
        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        task = await curio.spawn(func, *args, report_crash=False)
        task._taskgroup = self
        self._tasks.add(task)
        if name is not None:
            task.name = name

        # Make the spawned task inherit the current cancel scope
        current_task = await curio.current_task()
        cancel_scope = _local.cancel_scopes_by_task[current_task]
        cancel_scope.add_task(task)
        _local.cancel_scopes_by_task[task] = cancel_scope

    async def _task_done(self, task: curio.Task) -> None:
        self._tasks.remove(task)

    def _task_discard(self, task: curio.Task) -> None:
        # Remove the task from its cancel scope
        cancel_scope = _local.cancel_scopes_by_task.pop(task)  # type: CurioCancelScope
        cancel_scope.remove_task(task)

        self._tasks.discard(task)
        if task.terminated and task.exception is not None:
            if not isinstance(task.exception, (CancelledError, curio.TaskCancelled,
                                               curio.CancelledError)):
                self._exceptions.append(task.exception)


@asynccontextmanager
@async_generator
async def open_task_group():
    async with open_cancel_scope() as cancel_scope:
        current_task = await curio.current_task()
        group = CurioTaskGroup(cancel_scope, current_task)
        try:
            with translate_exceptions():
                await yield_(group)
        except CancelledError:
            await cancel_scope.cancel()
        except BaseException as exc:
            group._exceptions.append(exc)
            await cancel_scope.cancel()

        while group._tasks:
            for task in set(group._tasks):
                await task.wait()
                # try:
                # # with suppress(CancelledError), translate_exceptions():
                #     await task.join()
                # except BaseException as exc:
                #     from pdb import set_trace; set_trace()
                #     raise

        group._active = False
        if len(group._exceptions) > 1:
            raise MultiError(group._exceptions)
        elif group._exceptions:
            raise group._exceptions[0]


#
# Threads
#

async def run_in_thread(func: Callable[..., T_Retval], *args) -> T_Retval:
    def wrapper():
        asynclib = sys.modules[__name__]
        with claim_current_thread(asynclib):
            return func(*args)

    thread = await curio.spawn_thread(wrapper)
    return await thread.join()


def run_async_from_thread(func: Callable[..., T_Retval], *args) -> T_Retval:
    return curio.AWAIT(func(*args))


#
# Networking
#

class CurioSocket(curio.io.Socket):
    async def accept(self):
        await _check_cancelled()
        return await super().accept()

    async def bind(self, address):
        # For IP address/port combinations, call bind() directly
        await _check_cancelled()
        if isinstance(address, tuple) and len(address) == 2:
            try:
                ip_address(address[0])
            except ValueError:
                pass
            else:
                self._socket.bind(address)
                return

        # In all other cases, do this in a worker thread to avoid blocking the event loop thread
        await run_in_thread(self._socket.bind, address)

    async def connect(self, address):
        await _check_cancelled()
        return await super().connect(address)

    async def recv(self, maxsize, flags=0):
        await _check_cancelled()
        return await super().recv(maxsize, flags)

    async def recv_into(self, buffer, nbytes=0, flags=0):
        await _check_cancelled()
        return await super().recv_into(buffer, nbytes, flags)

    async def send(self, data, flags=0):
        await _check_cancelled()
        return await super().send(data, flags)

    async def sendall(self, data, flags=0):
        await _check_cancelled()
        return await super().sendall(data, flags)

    async def recvfrom(self, buffersize, flags=0):
        await _check_cancelled()
        return await super().recvfrom(buffersize, flags)

    async def recvfrom_into(self, buffer, bytes=0, flags=0):
        await _check_cancelled()
        return await super().recvfrom_into(buffer, bytes, flags)

    async def sendto(self, bytes, flags_or_address, address=None):
        await _check_cancelled()
        return await super().sendto(bytes, flags_or_address, address)

    async def recvmsg(self, bufsize, ancbufsize=0, flags=0):
        await _check_cancelled()
        return await super().recvmsg(bufsize, ancbufsize, flags)

    async def recvmsg_into(self, buffers, ancbufsize=0, flags=0):
        await _check_cancelled()
        return await super().recvmsg_into(buffers, ancbufsize, flags)

    async def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
        await _check_cancelled()
        return await super().sendmsg(buffers, ancdata, flags, address)

    async def shutdown(self, how):
        await _check_cancelled()
        return await super().shutdown(how)


def create_socket(family: int, type: int, proto: int, fileno) -> interfaces.Socket:
    raw_socket = socket.socket(family, type, proto, fileno)
    return CurioSocket(raw_socket)


#
# Synchronization
#

class Lock(curio.Lock):
    async def __aenter__(self):
        await _check_cancelled()
        return await super().__aenter__()


class Condition(curio.Condition):
    async def __aenter__(self):
        await _check_cancelled()
        return await super().__aenter__()

    async def wait(self):
        await _check_cancelled()
        return await super().wait()


class Event(curio.Event):
    async def wait(self):
        await _check_cancelled()
        return await super().wait()


class Semaphore(curio.Semaphore):
    async def __aenter__(self):
        await _check_cancelled()
        return await super().__aenter__()


class Queue(curio.Queue):
    async def get(self):
        await _check_cancelled()
        return await super().get()

    async def put(self, item):
        await _check_cancelled()
        return await super().put(item)


interfaces.TaskGroup.register(CurioTaskGroup)
interfaces.Socket.register(curio.socket.SocketType)
interfaces.Lock.register(Lock)
interfaces.Condition.register(Condition)
interfaces.Event.register(Event)
interfaces.Semaphore.register(Semaphore)
interfaces.Queue.register(Queue)
