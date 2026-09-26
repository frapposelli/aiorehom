"""Exception hierarchy for aiorehom.

Exception messages are built only from values this library controls (method,
path, status code, exception class names).  They never include response
bodies, request or response headers, credentials or the API token.
"""

from __future__ import annotations


class RehomError(Exception):
    """Base class for every aiorehom error."""


class ForbiddenRequestError(RehomError):
    """A request is not on the read-only allowlist.

    Raised before any socket activity takes place.
    """


class RehomConnectionError(RehomError):
    """The controller could not be reached (DNS, TCP, protocol error)."""


class RehomTimeoutError(RehomConnectionError):
    """The request did not complete within the configured timeout."""


class RehomHttpError(RehomError):
    """The controller answered with an unexpected HTTP status."""

    def __init__(self, status: int | None, message: str | None = None) -> None:
        self.status = status
        text = message if message is not None else f"unexpected HTTP status {status}"
        super().__init__(text)


class RehomAuthenticationError(RehomHttpError):
    """Authentication failed or is missing (HTTP 401/403, or the client is not logged in)."""

    def __init__(self, status: int | None, message: str | None = None) -> None:
        text = message if message is not None else f"authentication failed (HTTP {status})"
        super().__init__(status, text)


class RehomRedirectError(RehomHttpError):
    """The controller answered with a 3xx; redirects are never followed."""

    def __init__(self, status: int) -> None:
        super().__init__(status, f"redirect (HTTP {status}) refused: redirects are never followed")


class RehomResponseError(RehomHttpError):
    """A 2xx response could not be decoded (for example invalid JSON)."""


class RehomNotReadyError(RehomError):
    """No complete sync yet (state/history/resync before connect()), or the client is closed."""


class CredentialsError(RehomError):
    """Credentials could not be read from the macOS Keychain."""
