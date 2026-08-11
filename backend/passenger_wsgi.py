"""Passenger entrypoint for MochaHost's cPanel "Setup Python App".

cPanel's Python Application Manager (Phusion Passenger) asks for a WSGI
callable object, not an ASGI one — the backend itself is ASGI-only
(FastAPI/Starlette), so this module adapts it with a2wsgi rather than
gambling on a specific Passenger build's ASGI passthrough support.

cPanel names the module "passenger_wsgi" and the callable "application"
by convention (set as the Application startup file / entry point in the
cPanel UI); nothing here is guessable/optional.
"""

from a2wsgi import ASGIMiddleware

from genesis.api.app import create_app

application = ASGIMiddleware(create_app())
