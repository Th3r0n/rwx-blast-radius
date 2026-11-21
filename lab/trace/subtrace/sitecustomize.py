"""Coverage for Python subprocesses started by traced tests (e.g. test_lazy_load).

The tracer puts this directory on PYTHONPATH and sets BR_SUBTRACE_DIR for the test;
site.py imports this module at interpreter startup. It must not import unittest (the
subprocess must keep sqlglot's UNITTEST flag off, as in a real run) and removes its own
directory from sys.path. Same keys as the tracer: (file relative to root, co_qualname,
co_firstlineno); each location fires once (DISABLE). Written at exit to
$BR_SUBTRACE_DIR/sub-<pid>.json.
"""

import os
import sys

_dir = os.environ.get("BR_SUBTRACE_DIR")
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]

if _dir:
    _prefix = os.environ.get("BR_SUBTRACE_ROOT", "/opt/sqlglot").rstrip("/") + "/"
    _tops = ("sqlglot/", "tests/", "sqlglotc/")
    _M = sys.monitoring
    _keys = {}
    _refs = []
    _codes = set()
    _lines = {}
    _missing = object()

    def _key(code):
        k = None
        fn = code.co_filename
        if fn.startswith(_prefix) and fn[len(_prefix):].startswith(_tops):
            k = (fn[len(_prefix):], code.co_qualname, code.co_firstlineno)
        _keys[id(code)] = k
        _refs.append(code)
        return k

    def _on_start(code, offset):
        k = _keys.get(id(code), _missing)
        if k is _missing:
            k = _key(code)
        if k is not None:
            _codes.add(k)
        return _M.DISABLE

    def _on_line(code, line):
        k = _keys.get(id(code), _missing)
        if k is _missing:
            k = _key(code)
        if k is not None:
            s = _lines.get(k)
            if s is None:
                _lines[k] = s = set()
            s.add(line)
        return _M.DISABLE

    def _dump():
        _M.set_events(_tool, 0)
        import json

        lines = {}
        for (f, q, l), s in _lines.items():
            lines.setdefault(f, {})[f"{q}@{l}"] = sorted(s)
        data = {"argv": list(sys.orig_argv), "codes": sorted(list(k) for k in _codes), "lines": lines}
        path = os.path.join(_dir, f"sub-{os.getpid()}.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    _tool = next(i for i in range(6) if _M.get_tool(i) is None)
    _M.use_tool_id(_tool, "br-subtrace")
    _M.register_callback(_tool, _M.events.PY_START, _on_start)
    _M.register_callback(_tool, _M.events.LINE, _on_line)
    _M.set_events(_tool, _M.events.PY_START | _M.events.LINE)

    import atexit

    atexit.register(_dump)
