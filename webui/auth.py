"""Access control for the web UI.

The web UI drives an agent that can run shell commands, so anything
that reaches the port can drive it too. A reverse proxy would be the
usual answer, but the app also has to be usable straight from a
terminal, so the check lives inside the app.

The scheme is the simplest one that still holds up: one username, one
shared secret, checked on every request with a constant-time
comparison. The browser shows its own prompt and caches the
credentials for the origin, so the static page, the JSON API, and the
SSE stream all work without the client knowing anything about auth.

Authentication is optional, and off by default: the web UI is a local
tool, and a tool that asks for a password on every start is a tool
people leave running without one. When it is off, the launcher says so,
because the web UI can run shell commands and "only on localhost" is
not a property a server keeps by itself.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import NamedTuple

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send


USER_ENV = "AGENT_RUNTIME_WEBUI_AUTH_USER"
PASSWORD_ENV = "AGENT_RUNTIME_WEBUI_AUTH_PASSWORD"
DEFAULT_USER = "admin"
REALM = "SunAgent"


class Credentials(NamedTuple):
    """The one pair of credentials the web UI accepts."""

    username: str
    password: str


def credentials_from_env() -> Credentials | None:
    """Read the credentials from the environment.

    Both variables are needed, and the user name is the switch: set
    ``AGENT_RUNTIME_WEBUI_AUTH_USER`` to ``admin`` (or any other
    name) together with a password, and every request has to carry
    them. Leave the user name out and the web UI is open.

    The password is read verbatim: no trimming, no defaults, and a
    password that is only whitespace still counts as a password.
    """
    user = os.getenv(USER_ENV, "").strip()
    password = os.getenv(PASSWORD_ENV)
    if not user or not password:
        return None
    return Credentials(username=user, password=password)


def auth_warning() -> str | None:
    """Why the web UI is served without a password, or ``None``.

    The launcher prints this so that an open web UI is never a silent
    accident: it can run shell commands, so everyone who can reach the
    port can drive the agent.
    """
    if credentials_from_env() is not None:
        return None
    user = os.getenv(USER_ENV, "").strip()
    password = os.getenv(PASSWORD_ENV)
    if user and not password:
        return f"{USER_ENV} is set but {PASSWORD_ENV} is not"
    if password and not user:
        return f"{PASSWORD_ENV} is set but {USER_ENV} is not"
    return "neither a user name nor a password is configured"


class BasicAuthMiddleware:
    """Require the right credentials on every request.

    This is written as plain ASGI rather than as a FastAPI dependency
    so that it also covers the mounted static files, and so that it
    does not wrap the event stream: the SSE response must reach the
    browser as one long-lived response, not as a buffered one.
    """

    def __init__(self, app: ASGIApp, credentials: Credentials,
                 realm: str = REALM) -> None:
        self.app = app
        self.credentials = credentials
        self.realm = realm
        self._expected = (f"{credentials.username}:"
                          f"{credentials.password}").encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self._authorized(scope):
            await self._challenge(send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        """Check the ``Authorization`` header of one request."""
        raw = Headers(scope=scope).get("authorization", "")
        scheme, _, payload = raw.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(payload.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        return secrets.compare_digest(decoded, self._expected)

    async def _challenge(self, send: Send) -> None:
        """Answer with ``401`` and ask the browser for credentials."""
        body = b"authentication required\n"
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate",
                 f'Basic realm="{self.realm}"'.encode("utf-8")),
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "PASSWORD_ENV",
    "USER_ENV",
    "BasicAuthMiddleware",
    "Credentials",
    "auth_warning",
    "credentials_from_env",
]
