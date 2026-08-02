"""Regression tests for the real Hermes Dashboard plugin-loader contract."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

REPO = Path(__file__).resolve().parent.parent
PLUGIN_API = REPO / "dashboard" / "plugin_api.py"


def test_plugin_api_imports_by_file_path_without_dashboard_on_sys_path(tmp_path: Path) -> None:
    """Hermes imports plugin_api.py by path and does not add dashboard/ to sys.path."""
    probe = textwrap.dedent(
        """
        import importlib.util
        from pathlib import Path
        import sys

        api_path = Path(sys.argv[1])
        module_name = "hermes_dashboard_plugin_daily-ledger"
        spec = importlib.util.spec_from_file_location(module_name, api_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        routes = {getattr(route, "path", "") for route in module.router.routes}
        required = {"/health", "/month", "/day", "/recap", "/session-summary/batch", "/session-summary/batches"}
        missing = required - routes
        assert not missing, f"missing routes: {sorted(missing)}"

        # Verify batch routes exist by checking methods attribute
        route_methods = {}
        for route in module.router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if path:
                route_methods.setdefault(path, set()).update(methods)

        # POST /session-summary/batch should exist
        assert "/session-summary/batch" in route_methods, "POST /session-summary/batch missing"
        assert "POST" in route_methods["/session-summary/batch"], "POST method missing on /session-summary/batch"
        # GET /session-summary/batch should exist
        assert "GET" in route_methods["/session-summary/batch"], "GET method missing on /session-summary/batch"
        # GET /session-summary/batches should exist
        assert "/session-summary/batches" in route_methods, "GET /session-summary/batches missing"
        assert "GET" in route_methods["/session-summary/batches"], "GET method missing on /session-summary/batches"
        """
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_HOME"] = str(tmp_path / ".hermes")

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(PLUGIN_API)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
