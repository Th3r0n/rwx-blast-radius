"""In-process stand-in for concurrent.futures.ProcessPoolExecutor (stdlib only).

sqlglot's tests fan work out to spawn-context process pools (TestOptimizer.check_file,
TestExecutor._mp_execute). Under a tracer, or inside a forked child of a warm process,
real worker processes are both expensive (each spawn re-imports sqlglot) and invisible
(their coverage is lost). InlinePoolExecutor runs every call synchronously in the
calling process, but keeps the observable contract of a real pool:

* (fn, args, kwargs) are pickled with multiprocessing's ForkingPickler and unpickled
  again before the call, and the result or exception is round-tripped the same way
  after it. Unpicklable work or results fail the way a real pool fails them.
* submit() returns an already-completed concurrent.futures.Future, so wait(),
  as_completed(), Future.result() and Executor.map() behave as usual.
* initializer/initargs run (once, lazily at the first submit, after a pickle round
  trip of the pair); an initializer error breaks the pool like a dying worker does.
* Exceptions raised by fn come back with a __cause__ of _RemoteTraceback, as from a
  real worker.

Known, accepted differences: the work sees the caller's in-memory state (mocks,
module globals mutated by setUpClass) instead of a freshly spawned interpreter, and
the initializer's side effects land in the caller's process.
"""

from __future__ import annotations

import concurrent.futures
import concurrent.futures.process as _cfp
import os
import sys
import threading
import traceback
from concurrent.futures import Executor, Future
from concurrent.futures.process import BrokenProcessPool

try:  # The exact pickler multiprocessing queues use (extra reducers registered).
    from multiprocessing.reduction import ForkingPickler as _Pickler

    def _dumps(obj):
        return bytes(_Pickler.dumps(obj))

    _loads = _Pickler.loads
except Exception:  # pragma: no cover - multiprocessing always ships with CPython
    import pickle as _pickle

    _dumps = _pickle.dumps
    _loads = _pickle.loads

try:
    from concurrent.futures.process import _ExceptionWithTraceback, _RemoteTraceback
except Exception:  # pragma: no cover - private names, keep a faithful fallback

    class _RemoteTraceback(Exception):
        def __init__(self, tb):
            self.tb = tb

        def __str__(self):
            return self.tb

    def _rebuild_exc(exc, tb):
        exc.__cause__ = _RemoteTraceback(tb)
        return exc

    class _ExceptionWithTraceback:
        def __init__(self, exc, tb):
            tb = "".join(traceback.format_exception(type(exc), exc, tb))
            self.exc = exc
            self.exc.__traceback__ = None
            self.tb = '\n"""\n%s"""' % tb

        def __reduce__(self):
            return _rebuild_exc, (self.exc, self.tb)


__all__ = ["InlinePoolExecutor", "install", "uninstall", "REAL_PROCESS_POOL_EXECUTOR"]

REAL_PROCESS_POOL_EXECUTOR = _cfp.ProcessPoolExecutor

_BROKEN_MSG = (
    "A process in the process pool was terminated abruptly while the future was "
    "running or pending."
)


def _remote_cause(exc):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _RemoteTraceback('\n"""\n%s"""' % tb)


class InlinePoolExecutor(Executor):
    """Synchronous, pickle-faithful drop-in for ProcessPoolExecutor."""

    def __init__(
        self,
        max_workers=None,
        mp_context=None,
        initializer=None,
        initargs=(),
        *,
        max_tasks_per_child=None,
    ):
        if max_workers is None:
            count = getattr(os, "process_cpu_count", os.cpu_count)()
            self._max_workers = count or 1
        else:
            if max_workers <= 0:
                raise ValueError("max_workers must be greater than 0")
            self._max_workers = max_workers
        if initializer is not None and not callable(initializer):
            raise TypeError("initializer must be a callable")
        if max_tasks_per_child is not None:
            if not isinstance(max_tasks_per_child, int):
                raise TypeError("max_tasks_per_child must be an integer")
            if max_tasks_per_child <= 0:
                raise ValueError("max_tasks_per_child must be >= 1")
        self._mp_context = mp_context
        self._initializer = initializer
        self._initargs = initargs
        self._max_tasks_per_child = max_tasks_per_child
        self._initialized = False
        self._broken = False
        self._shutdown_thread = False
        self._shutdown_lock = threading.RLock()
        self._processes = {}
        self.submitted = 0  # introspection for tracers/tests

    # -- internals -------------------------------------------------------------

    def _mark_broken(self, cause):
        self._broken = cause
        print(f"InlinePoolExecutor: pool broken: {cause}", file=sys.stderr)

    def _broken_error(self, exc=None):
        err = BrokenProcessPool(_BROKEN_MSG)
        if exc is not None:
            err.__cause__ = _remote_cause(exc)
        return err

    def _run_initializer(self):
        """Runs the initializer like a fresh worker would; returns an error or None."""
        self._initialized = True
        if self._initializer is None:
            return None
        try:
            initializer, initargs = _loads(_dumps((self._initializer, self._initargs)))
        except BaseException as e:  # a real pool fails to start its worker
            if isinstance(e, KeyboardInterrupt):
                raise
            self._mark_broken("initializer could not be pickled: %r" % (e,))
            return self._broken_error(e)
        try:
            initializer(*initargs)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            # Real workers log this at CRITICAL and exit; the pool becomes broken.
            concurrent.futures._base.LOGGER.critical("Exception in initializer:", exc_info=True)
            self._mark_broken("initializer raised %r" % (e,))
            return self._broken_error(e)
        return None

    # -- Executor API ------------------------------------------------------------

    def submit(self, fn, /, *args, **kwargs):
        with self._shutdown_lock:
            if self._broken:
                raise BrokenProcessPool(self._broken)
            if self._shutdown_thread:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self.submitted += 1
            future = Future()
            if not future.set_running_or_notify_cancel():  # pragma: no cover
                return future

            if not self._initialized:
                err = self._run_initializer()
                if err is not None:
                    future.set_exception(err)
                    return future

            # Parent side: pickling the call item. A failure here is delivered to the
            # future (the queue feeder thread does this in a real pool).
            try:
                payload = _dumps((fn, args, kwargs))
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                e.__cause__ = _remote_cause(e)
                future.set_exception(e)
                return future

            # Worker side: unpickling the call item. A failure kills a real worker.
            try:
                fn2, args2, kwargs2 = _loads(payload)
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                self._mark_broken("call item could not be unpickled: %r" % (e,))
                future.set_exception(self._broken_error(e))
                return future

            exception = None
            result = None
            try:
                result = fn2(*args2, **kwargs2)
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                exception = _ExceptionWithTraceback(e, e.__traceback__)
            del fn2, args2, kwargs2, payload

            # Worker side: pickling the result (or the exception). On failure the
            # worker sends back the pickling error instead (_sendback_result).
            try:
                blob = _dumps((exception, result))
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                try:
                    blob = _dumps((_ExceptionWithTraceback(e, e.__traceback__), None))
                except BaseException as e2:  # pragma: no cover - very unusual
                    if isinstance(e2, KeyboardInterrupt):
                        raise
                    self._mark_broken("result could not be pickled: %r" % (e2,))
                    future.set_exception(self._broken_error(e2))
                    return future
            del exception, result

            # Parent side: unpickling the result. A failure breaks a real pool.
            try:
                exception, result = _loads(blob)
            except BaseException as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                self._mark_broken("result could not be unpickled: %r" % (e,))
                future.set_exception(self._broken_error(e))
                return future

            if exception is not None:
                future.set_exception(exception)
            else:
                future.set_result(result)
            return future

    submit.__doc__ = Executor.submit.__doc__

    def map(self, fn, *iterables, timeout=None, chunksize=1, **kwargs):
        if chunksize < 1:
            raise ValueError("chunksize must be >= 1.")
        return super().map(fn, *iterables, timeout=timeout, **kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._shutdown_lock:
            self._shutdown_thread = True

    def terminate_workers(self):  # Python 3.14 API
        self.shutdown()

    def kill_workers(self):  # Python 3.14 API
        self.shutdown()


_installed = []  # (module, attribute, previous value)


def install(modules=(), *, process_module=False):
    """Rebinds ProcessPoolExecutor to InlinePoolExecutor.

    Patches concurrent.futures (so `concurrent.futures.ProcessPoolExecutor` and later
    `from concurrent.futures import ProcessPoolExecutor` get the inline pool) and every
    given module that already bound the name (the sqlglot test modules do
    `from concurrent.futures import ProcessPoolExecutor`). With process_module=True,
    concurrent.futures.process is patched too. Returns the list of patched module
    names. Idempotent.
    """
    forced = [concurrent.futures] + ([_cfp] if process_module else [])
    patched = []
    seen = set()
    for module in forced + [m for m in modules if m is not None]:
        if id(module) in seen:
            continue
        seen.add(id(module))
        d = getattr(module, "__dict__", None)
        if d is None:
            continue
        current = d.get("ProcessPoolExecutor")
        if current is InlinePoolExecutor:
            continue
        is_real = isinstance(current, type) and issubclass(current, REAL_PROCESS_POOL_EXECUTOR)
        if not is_real and module not in forced:
            continue  # name absent, or someone else's object (e.g. a mock): leave it
        _installed.append((module, "ProcessPoolExecutor", current))
        setattr(module, "ProcessPoolExecutor", InlinePoolExecutor)
        patched.append(getattr(module, "__name__", repr(module)))
    return patched


def uninstall():
    """Reverts every binding made by install()."""
    while _installed:
        module, attr, previous = _installed.pop()
        if previous is None and module is concurrent.futures:
            module.__dict__.pop(attr, None)  # back to the lazy module __getattr__
        else:
            setattr(module, attr, previous)


def _selfcheck():  # pragma: no cover - manual smoke test
    import pickle

    def local(x):
        return x

    with InlinePoolExecutor() as pool:
        assert pool.submit(pow, 2, 5).result() == 32
        assert list(pool.map(abs, [-1, -2])) == [1, 2]
        f = pool.submit(local, 1)
        assert isinstance(f.exception(), (pickle.PicklingError, AttributeError, TypeError))
        f = pool.submit(int, "x")
        assert isinstance(f.exception(), ValueError)
        assert isinstance(f.exception().__cause__, _RemoteTraceback)
        done, _ = concurrent.futures.wait([pool.submit(pow, 3, 2)])
        assert [d.result() for d in done] == [9]
        assert [d.result() for d in concurrent.futures.as_completed([pool.submit(abs, -4)])] == [4]
        f = pool.submit(threading.Lock)  # result (a lock) cannot be pickled
        assert isinstance(f.exception(), TypeError), f.exception()
    try:
        pool.submit(abs, 1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit after shutdown must fail")
    print("inlinepool selfcheck ok")


if __name__ == "__main__":  # pragma: no cover
    _selfcheck()
