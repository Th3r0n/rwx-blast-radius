"""pytest plugin that records per-test verdicts for the labeling lab.

Loaded with `-p label_plugin` (its directory on PYTHONPATH). It streams one
JSON object per line to the file named by $LABEL_PLUGIN_OUT, flushing after
every line so a crashed or killed session still leaves partial evidence:

  {"ev": "fail", "nodeid": ..., "when": "setup|call|teardown", "subtest": bool}
  {"ev": "collect_error", "nodeid": ...}
  {"ev": "done", "exitstatus": int, "reported": int, "skipped": [...]}

A test fails when any of its reports failed: any phase (setup, call,
teardown), any subtest report (pytest 9 native subtests carry a `context`
and share the parent's nodeid; the parent itself may report PASSED), strict
xpass, or a pytest-timeout timeout (reported as a failed call). Collection
failures are recorded per collector nodeid; the runner expands them to the
tests of that file.

Under xdist the controller receives every worker report through the same
hooks, so only the controller writes (workers have `workerinput`).
"""

from __future__ import annotations

import json
import os

import pytest


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


class _Recorder:
    def __init__(self, config, path: str) -> None:
        self.config = config
        self.path = path
        self.stream = open(path, "a", encoding="utf-8")
        self.reported: set[str] = set()
        self.skipped: set[str] = set()
        self.failed: set[str] = set()

    def _emit(self, record: dict) -> None:
        self.stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.stream.flush()

    def pytest_runtest_logreport(self, report) -> None:
        nodeid = report.nodeid
        self.reported.add(nodeid)
        is_subtest = getattr(report, "context", None) is not None or type(
            report
        ).__name__ == "SubtestReport"
        if report.failed:
            self.failed.add(nodeid)
            try:
                text = str(report.longrepr)
            except Exception:  # pragma: no cover - defensive
                text = ""
            self._emit(
                {
                    "ev": "fail",
                    "nodeid": nodeid,
                    "when": report.when,
                    "subtest": bool(is_subtest),
                    "timeout": "from pytest-timeout" in text,
                    "duration": round(getattr(report, "duration", 0.0) or 0.0, 3),
                }
            )
        elif report.skipped and not hasattr(report, "wasxfail") and not is_subtest:
            self.skipped.add(nodeid)

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            try:
                text = str(report.longrepr)
            except Exception:  # pragma: no cover - defensive
                text = "<unprintable longrepr>"
            self._emit(
                {"ev": "collect_error", "nodeid": report.nodeid, "msg": text[-2000:]}
            )

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self._emit(
            {
                "ev": "done",
                "exitstatus": int(exitstatus),
                "reported": len(self.reported),
                "skipped": sorted(self.skipped - self.failed),
                "testscollected": getattr(session, "testscollected", None),
            }
        )
        self.stream.close()


# Tests whose memory peak dominates the run (test_max_nodes ~1.7 GB). When
# $LABEL_HEAVY_LOCK names a file, these run under an exclusive flock on it, so
# concurrent labelers in one container never hit their peaks at once. The lock
# is taken outside pytest-timeout's window (outermost hookwrapper) and only
# changes timing, never verdicts.
HEAVY_TESTS = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
}


class _HeavyLock:
    def __init__(self, path: str) -> None:
        self.path = path

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if item.nodeid not in HEAVY_TESTS:
            yield
            return
        import fcntl

        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def pytest_configure(config) -> None:
    lock = os.environ.get("LABEL_HEAVY_LOCK")
    if lock:
        config.pluginmanager.register(_HeavyLock(lock), "label_heavy_lock")
    path = os.environ.get("LABEL_PLUGIN_OUT")
    if not path or _is_worker(config):
        return
    config.pluginmanager.register(_Recorder(config, path), "label_recorder")
