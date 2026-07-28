"""Pytest plugin that blocks and records non-loopback socket connections.

Usage:
  PYTHONPATH=/path/to/project/scripts:$PYTHONPATH \
    python -m pytest -p pytest_no_external_network -q
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import traceback
from pathlib import Path

_LOG = Path(os.environ.get("PYTEST_NETWORK_GUARD_LOG", "/tmp/pytest-network-guard.log"))
_LOCK = threading.Lock()
_ORIGINAL_CONNECT = socket.socket.connect


def _is_loopback(host: object) -> bool:
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return str(host).lower() == "localhost"


def _guarded_connect(sock: socket.socket, address: object):
    if isinstance(address, str):
        return _ORIGINAL_CONNECT(sock, address)  # type: ignore[arg-type]

    host = address[0] if isinstance(address, tuple) and address else ""
    if _is_loopback(host):
        return _ORIGINAL_CONNECT(sock, address)  # type: ignore[arg-type]

    with _LOCK:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"TEST={os.environ.get('PYTEST_CURRENT_TEST', '<unknown>')}\n")
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else "?"
            handle.write(f"ADDRESS={host}:{port}\n")
            handle.write("".join(traceback.format_stack(limit=40)))
            handle.write("\n---\n")
    raise ConnectionRefusedError(
        f"pytest network guard blocked non-loopback connection to {host}"
    )


def pytest_configure(config) -> None:
    del config
    _LOG.unlink(missing_ok=True)
    socket.socket.connect = _guarded_connect


def pytest_unconfigure(config) -> None:
    del config
    socket.socket.connect = _ORIGINAL_CONNECT
