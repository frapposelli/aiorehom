"""Per-request send guard: one call of ours puts exactly one request on the wire.

aiohttp can send more than one request for a single ``session.request()`` or
``session.ws_connect()`` call:

* it follows redirects (``ws_connect`` always does: it has no
  ``allow_redirects`` option), to any path, port or host the ``Location``
  names;
* it silently resends an idempotent request (GET, and the WebSocket
  handshake) once after ``ClientOSError``/``ServerDisconnectedError``.

Both bypass the allowlist, the pacing and the request log.  Two layers stop
them:

1. :func:`disable_transparent_retry` turns the resend off on every session
   aiorehom creates (``ClientSession._retry_connection`` is private, so it is
   set only if present and tests pin its effect).  A session supplied by the
   caller is never modified: there, layer 2 alone refuses the resend.
2. :class:`SingleSendGuard`, an aiohttp client middleware (aiohttp >= 3.12),
   runs before every hop that aiohttp sends.  It refuses any hop whose
   method or URL differs from the one request we meant to send, refuses a
   second hop, and (for the WebSocket) turns a 3xx answer into a handshake
   error so that no redirect is ever followed.
"""

from __future__ import annotations

from typing import Final

import aiohttp
from yarl import URL

from .exceptions import ForbiddenRequestError

__all__ = ["ResendRefusedError", "SingleSendGuard", "disable_transparent_retry"]

_REDIRECT_STATUSES: Final = range(300, 400)


class ResendRefusedError(aiohttp.ClientConnectionError):
    """aiohttp tried to send a second request for one call (transparent retry)."""


def disable_transparent_retry(session: aiohttp.ClientSession) -> None:
    """Stop aiohttp from resending an idempotent request after a dropped connection."""
    if hasattr(session, "_retry_connection"):
        session._retry_connection = False


class SingleSendGuard:
    """aiohttp client middleware that lets exactly one ``method url`` request out.

    Call :meth:`arm` before each request the guard should allow (the guard
    starts armed).  With ``refuse_redirects=True`` a 3xx response is closed and
    raised as :class:`aiohttp.WSServerHandshakeError` (status kept, headers
    dropped), before aiohttp can follow it.
    """

    __slots__ = ("_method", "_refuse_redirects", "_sent", "_url")

    def __init__(self, method: str, url: URL, *, refuse_redirects: bool = False) -> None:
        self._method = method
        self._url = url
        self._refuse_redirects = refuse_redirects
        self._sent = 0

    @property
    def sent(self) -> int:
        """Requests let through since the last :meth:`arm`."""
        return self._sent

    def arm(self) -> None:
        self._sent = 0

    async def __call__(
        self, request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        if request.method != self._method or request.url != self._url:
            raise ForbiddenRequestError(
                f"{request.method!s:.10} {request.url.path!r:.120} refused: aiohttp tried to send "
                "a request other than the one allowed (redirects are never followed)"
            )
        if self._sent:
            raise ResendRefusedError(
                f"{self._method} {self._url.path}: second send refused "
                "(at most one request per call)"
            )
        self._sent += 1
        response = await handler(request)
        if self._refuse_redirects and response.status in _REDIRECT_STATUSES:
            status = response.status
            request_info = response.request_info
            response.close()
            raise aiohttp.WSServerHandshakeError(
                request_info,
                (),
                status=status,
                message="redirect refused: redirects are never followed",
                headers=None,
            )
        return response
