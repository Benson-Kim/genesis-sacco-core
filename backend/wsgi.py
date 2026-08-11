"""WSGI entrypoint for cPanel's "Setup Python App" (Phusion Passenger).

Passenger asks for a WSGI callable, not an ASGI one — the backend is
ASGI-only (FastAPI/Starlette) — so this adapts it with a2wsgi rather
than gambling on a specific Passenger build's ASGI passthrough.

MUST NOT be named passenger_wsgi.py. cPanel GENERATES its own
passenger_wsgi.py that does `load_source('wsgi', <startup file>)` and
re-exports `.application`. Point the startup file at passenger_wsgi.py
and that generated stub loads ITSELF — a RecursionError at boot. Set
the cPanel fields to:

    Application startup file : wsgi.py
    Application Entry point  : application

cPanel then overwrites backend/passenger_wsgi.py on every save; that
file is generated infrastructure and is gitignored.

FORK SAFETY — why the adapter is built lazily rather than at import.
LiteSpeed's lswsgi imports this module ONCE and then forks its worker
processes. a2wsgi's ASGIMiddleware owns a background event loop and its
thread; neither survives fork(), so a middleware built at import time
leaves every worker holding a handle to a loop that does not exist in
its process. The symptom is not a crash — it is a HANG: the request is
accepted, nothing ever answers, and LiteSpeed SIGTERMs the child and
retries forever (observed on the real host; a trivial WSGI app in the
same slot returned 200, which is what isolated it to this).

Building it on first request means it is constructed in the worker that
will serve it, after the fork. The double-checked lock keeps that to one
construction per process even if requests arrive concurrently.
"""

import threading
from collections.abc import Callable, Iterable

from a2wsgi import ASGIMiddleware

from genesis.api.app import create_app

_adapter: ASGIMiddleware | None = None
_adapter_lock = threading.Lock()


def _get_adapter() -> ASGIMiddleware:
    """Build (once per process) the ASGI->WSGI adapter."""
    global _adapter
    if _adapter is None:
        with _adapter_lock:
            # Re-checked under the lock: two threads can both see None.
            if _adapter is None:
                _adapter = ASGIMiddleware(create_app())
    return _adapter


def application(
    environ: dict[str, object],
    start_response: Callable[..., object],
) -> Iterable[bytes]:
    """WSGI callable — the name cPanel's generated stub re-exports."""
    return _get_adapter()(environ, start_response)
