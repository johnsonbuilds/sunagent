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

Authentication is not optional: the web UI refuses to start without a
password, because it can run shell commands and "only on localhost" is
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

    ``None`` means no password is configured. The password is read
    verbatim: no trimming, no defaults, and a password that is only
    whitespace still counts as a password.
    """
    password = os.getenv(PASSWORD_ENV)
    if not password:
        return None
    return Credentials(username=os.getenv(USER_ENV) or DEFAULT_USER,
                       password=password)


class AuthNotConfigured(RuntimeError):
    """Raised when the web UI is started without a password."""


def require_credentials() -> Credentials:
    """The credentials the web UI refuses to start without.

    The web UI can run shell commands, so it is never served open --
    not even on localhost. A missing password is a configuration
    mistake, not a mode to fall back to.
    """
    credentials = credentials_from_env()
    if credentials is None:
        raise AuthNotConfigured(
            f"The web UI needs a password. Set {PASSWORD_ENV} in .env "
            f"(and optionally {USER_ENV}), for example:\n"
            f'  {PASSWORD_ENV}="$(python -c "import secrets; '
            f'print(secrets.token_urlsafe(24))")"')
    return credentials


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
    "credentials_from_env",
]
