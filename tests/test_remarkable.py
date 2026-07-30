from __future__ import annotations

from types import SimpleNamespace

import pytest

from esbern.config import Config
from esbern.remarkable import HostKeyMismatch, Remarkable, _TrustOnFirstUsePolicy


class _StalledSftp:
    def __init__(self) -> None:
        self.calls = 0

    def stat(self, path: str):
        self.calls += 1
        raise TimeoutError("stalled channel")


class _WorkingSftp:
    def __init__(self) -> None:
        self.calls = 0

    def stat(self, path: str):
        self.calls += 1
        return SimpleNamespace(st_mtime=12.0, st_size=34)


class _HostKey:
    def __init__(self, encoded: str):
        self.encoded = encoded

    def get_name(self) -> str:
        return "ssh-ed25519"

    def get_base64(self) -> str:
        return self.encoded


def test_host_key_is_pinned_on_first_use_and_rechecked(monkeypatch) -> None:
    cfg = Config()
    saved: list[Config] = []
    monkeypatch.setattr("esbern.remarkable.config.save", lambda item: saved.append(item))
    policy = _TrustOnFirstUsePolicy(cfg)

    policy.missing_host_key(None, "remarkable", _HostKey("first"))
    policy.missing_host_key(None, "remarkable", _HostKey("first"))

    assert cfg.host_key == "ssh-ed25519 first"
    assert saved == [cfg]
    with pytest.raises(HostKeyMismatch, match="host key.*changed"):
        policy.missing_host_key(None, "remarkable", _HostKey("different"))


def test_sftp_operation_reconnects_once_after_timeout(monkeypatch) -> None:
    rm = Remarkable(Config())
    stalled = _StalledSftp()
    working = _WorkingSftp()
    rm._sftp = stalled
    reconnects = 0

    def reconnect() -> None:
        nonlocal reconnects
        reconnects += 1
        rm._sftp = working

    monkeypatch.setattr(rm, "_reconnect", reconnect)

    assert rm.stat_size("book.epub") == 34
    assert stalled.calls == 1
    assert working.calls == 1
    assert reconnects == 1


def test_sftp_operation_stops_after_one_retry(monkeypatch) -> None:
    rm = Remarkable(Config())
    stalled = _StalledSftp()
    rm._sftp = stalled
    reconnects = 0

    def reconnect() -> None:
        nonlocal reconnects
        reconnects += 1

    monkeypatch.setattr(rm, "_reconnect", reconnect)

    with pytest.raises(TimeoutError, match="stalled channel"):
        rm.stat_size("book.epub")

    assert stalled.calls == 2
    assert reconnects == 1


def test_bulk_metadata_stream_is_parsed_without_sftp_round_trips(monkeypatch) -> None:
    rm = Remarkable(Config())
    stream = (
        "first.metadata 10\nsecond.metadata 20\n\0"
        'first.metadata\0{"visibleName":"First"}\0'
        "second.metadata\0"
        '{"visibleName":"Second"}\0'
    )
    monkeypatch.setattr(rm, "exec", lambda command: (0, stream, ""))

    assert rm.read_xochitl_metadata() == [
        ("first", 10.0, '{"visibleName":"First"}'),
        ("second", 20.0, '{"visibleName":"Second"}'),
    ]


def test_bulk_metadata_command_exits_when_remote_root_cannot_be_entered(
    monkeypatch,
) -> None:
    rm = Remarkable(Config(remote_root="/missing xochitl"))
    commands: list[str] = []

    def execute(command: str) -> tuple[int, str, str]:
        commands.append(command)
        return 1, "", "directory unavailable"

    monkeypatch.setattr(rm, "exec", execute)

    with pytest.raises(OSError, match="directory unavailable"):
        rm.read_xochitl_metadata()

    assert commands[0].startswith("cd '/missing xochitl' || exit $?;")
