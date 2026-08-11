"""WSGI entrypoint for cPanel's "Setup Python App" (Phusion Passenger).

Passenger asks for a WSGI callable, not an ASGI one — the backend is
ASGI-only (FastAPI/Starlette) — so this adapts it with a2wsgi rather
than gambling on a specific Passenger build's ASGI passthrough.

MUST NOT be named passenger_wsgi.py. cPanel GENERATES its own
passenger_wsgi.py that does `load_source('wsgi', <startup file>)` and
re-exports `.application`. Point the startup file at passenger_wsgi.py
and that generated stub loads ITSELF — a RecursionError at boot, which
is exactly how this file got its name. Set the cPanel fields to:

    Application startup file : wsgi.py
    Application Entry point  : application

cPanel then overwrites backend/passenger_wsgi.py on every save; that
file is generated infrastructure and is gitignored.
"""

from a2wsgi import ASGIMiddleware

from genesis.api.app import create_app

application = ASGIMiddleware(create_app())
