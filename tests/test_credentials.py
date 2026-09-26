"""Keychain credential parsing with a mocked subprocess (the real Keychain is never touched)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aiorehom import credentials
from aiorehom.credentials import parse_account, read_keychain_credentials
from aiorehom.exceptions import CredentialsError

ATTRS = b"""keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="rehom-api"
    0x00000008 <blob>=<NULL>
    "acct"<blob>="alice"
    "cdat"<timedate>=0x32303236303932353130303030305A00  "20260925100000Z\\000"
    "svce"<blob>="rehom-api"
"""


class FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"STDERR-NOISE") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class FakeExec:
    def __init__(self, results: list[FakeProc | BaseException]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProc:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def fake_exec(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(results: list[FakeProc | BaseException]) -> FakeExec:
        fake = FakeExec(results)
        monkeypatch.setattr(credentials.asyncio, "create_subprocess_exec", fake)
        return fake

    return install


def test_parse_account() -> None:
    assert parse_account(ATTRS.decode()) == "alice"
    hex_attrs = '    "acct"<blob>=0x616C6963C3A9  "alic\\303\\251"\n'
    assert parse_account(hex_attrs) == "alicé"
    assert parse_account('    "acct"<blob>=0xZZ\n') is None
    assert parse_account('    "acct"<blob>=0xFF\n') is None
    assert parse_account('    "acct"<blob>=<NULL>\n') is None
    assert parse_account("") is None


async def test_reads_account_then_password(fake_exec: Any) -> None:
    fake = fake_exec([FakeProc(0, ATTRS), FakeProc(0, b"pa ss\tword\n")])
    user, password = await read_keychain_credentials()
    assert (user, password) == ("alice", "pa ss\tword")
    (args1, kwargs1), (args2, kwargs2) = fake.calls
    assert args1 == (credentials.SECURITY_BIN, "find-generic-password", "-s", "rehom-api")
    assert args2 == (
        credentials.SECURITY_BIN,
        "find-generic-password",
        "-s",
        "rehom-api",
        "-a",
        "alice",
        "-w",
    )
    for kwargs in (kwargs1, kwargs2):
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.PIPE  # captured, then discarded
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL


async def test_username_override_skips_account_lookup(fake_exec: Any) -> None:
    fake = fake_exec([FakeProc(0, b"pw\n")])
    assert await read_keychain_credentials("svc", username="other") == ("other", "pw")
    ((args, _kwargs),) = fake.calls
    assert args[-3:] == ("-a", "other", "-w")
    assert args[3] == "svc"


async def test_missing_item_gives_add_command(fake_exec: Any) -> None:
    fake_exec([FakeProc(44, b"", b"security: SecKeychainSearchCopyNext: not found")])
    with pytest.raises(CredentialsError) as info:
        await read_keychain_credentials()
    message = str(info.value)
    assert "security add-generic-password -s rehom-api -a <username> -w" in message
    assert "SecKeychain" not in message


async def test_missing_password_gives_add_command(fake_exec: Any) -> None:
    fake_exec([FakeProc(0, ATTRS), FakeProc(44, b"")])
    with pytest.raises(CredentialsError, match="add-generic-password"):
        await read_keychain_credentials()


async def test_no_account_attribute(fake_exec: Any) -> None:
    fake_exec([FakeProc(0, b'attributes:\n    "acct"<blob>=<NULL>\n')])
    with pytest.raises(CredentialsError, match="no account name"):
        await read_keychain_credentials()


async def test_empty_and_undecodable_password(fake_exec: Any) -> None:
    fake_exec([FakeProc(0, b"\n")])
    with pytest.raises(CredentialsError, match="empty"):
        await read_keychain_credentials(username="u")
    fake_exec([FakeProc(0, b"\xff\xfe\n")])
    with pytest.raises(CredentialsError, match="UTF-8") as info:
        await read_keychain_credentials(username="u")
    assert "\\xff" not in str(info.value)


async def test_security_binary_missing(fake_exec: Any) -> None:
    fake_exec([FileNotFoundError("x")])
    with pytest.raises(CredentialsError, match="not available"):
        await read_keychain_credentials()


async def test_real_binary_is_never_reached_in_tests() -> None:
    # conftest points SECURITY_BIN at a non-existent path
    with pytest.raises(CredentialsError, match="not available"):
        await read_keychain_credentials()


async def test_timeout(fake_exec: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowProc(FakeProc):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

    proc = SlowProc(0, b"")
    fake_exec([proc])
    monkeypatch.setattr(credentials, "_SUBPROCESS_TIMEOUT", 0.01)
    with pytest.raises(CredentialsError, match="timed out"):
        await read_keychain_credentials(username="u")
    assert proc.killed


@pytest.mark.parametrize("service", ["", "a b", "x;rm", "a" * 65])
async def test_invalid_service(service: str) -> None:
    with pytest.raises(CredentialsError, match="service"):
        await read_keychain_credentials(service)


@pytest.mark.parametrize("username", ["", "a\nb", "x" * 151])
async def test_invalid_username(username: str) -> None:
    with pytest.raises(CredentialsError, match="username"):
        await read_keychain_credentials(username=username)
