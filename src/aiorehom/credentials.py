"""Read the controller credentials from the macOS Keychain.

The item is a generic password created by the user in their own terminal::

    security add-generic-password -s rehom-api -a <username> -w

``security`` is run as a subprocess with a fixed argument vector; stdout is
captured, stderr is captured and discarded (never echoed), and nothing read
here is ever logged or put into an exception message.
"""

from __future__ import annotations

import asyncio
import re
from typing import Final

from .exceptions import CredentialsError

__all__ = ["DEFAULT_SERVICE", "parse_account", "read_keychain_credentials"]

DEFAULT_SERVICE: Final = "rehom-api"
SECURITY_BIN: Final = "/usr/bin/security"
_SERVICE_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ACCT_QUOTED_RE: Final = re.compile(r'^\s*"acct"<blob>="(.*)"\s*$', re.MULTILINE)
_ACCT_HEX_RE: Final = re.compile(r'^\s*"acct"<blob>=0x([0-9A-Fa-f]+)(?:\s+".*")?\s*$', re.MULTILINE)
_SUBPROCESS_TIMEOUT: Final = 60.0


def _add_command_hint(service: str) -> str:
    return (
        f"Store it in your own terminal (not through a chat prompt) with:\n"
        f"    security add-generic-password -s {service} -a <username> -w"
    )


def parse_account(output: str) -> str | None:
    """Extract the account name from ``security find-generic-password`` attribute output."""
    match = _ACCT_QUOTED_RE.search(output)
    if match is not None:
        return match.group(1)
    match = _ACCT_HEX_RE.search(output)
    if match is not None:
        try:
            return bytes.fromhex(match.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


async def _run_security(args: list[str]) -> tuple[int, bytes]:
    """Run ``security`` and return (returncode, stdout).  stderr is discarded."""
    try:
        proc = await asyncio.create_subprocess_exec(
            SECURITY_BIN,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        raise CredentialsError(
            f"cannot run {SECURITY_BIN}: the macOS Keychain is not available on this system"
        ) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROCESS_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CredentialsError(
            "timed out waiting for the Keychain (was a prompt left open?)"
        ) from None
    del stderr  # captured only so that it is never echoed to the terminal
    return (proc.returncode if proc.returncode is not None else -1), stdout


async def read_keychain_credentials(
    service: str = DEFAULT_SERVICE, *, username: str | None = None
) -> tuple[str, str]:
    """Return ``(username, password)`` from the Keychain generic-password item ``service``.

    The account name is read from the item's ``acct`` attribute unless
    ``username`` overrides it (then the password lookup is restricted to that
    account).  Raises :class:`CredentialsError` with the exact ``security
    add-generic-password`` command when the item is missing.
    """
    if _SERVICE_RE.fullmatch(service) is None:
        raise CredentialsError("invalid Keychain service name")
    if username is not None and (not username or not username.isprintable() or len(username) > 150):
        raise CredentialsError("invalid --username value")

    if username is None:
        code, out = await _run_security(["find-generic-password", "-s", service])
        if code != 0:
            raise CredentialsError(
                f"Keychain item '{service}' not found (security exit code {code}). "
                + _add_command_hint(service)
            )
        account = parse_account(out.decode("utf-8", errors="replace"))
        del out
        if not account:
            raise CredentialsError(
                f"Keychain item '{service}' has no account name; delete it and re-create it. "
                + _add_command_hint(service)
            )
    else:
        account = username

    code, out = await _run_security(["find-generic-password", "-s", service, "-a", account, "-w"])
    if code != 0:
        raise CredentialsError(
            f"no password stored for this account in Keychain item '{service}' "
            f"(security exit code {code}). " + _add_command_hint(service)
        )
    try:
        password = out.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialsError("the Keychain password is not valid UTF-8") from None
    finally:
        del out
    if password.endswith("\n"):
        password = password[:-1]
    if not password:
        raise CredentialsError(f"the password in Keychain item '{service}' is empty")
    return account, password
