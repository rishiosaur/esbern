from __future__ import annotations

import io
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


class _AtomicUploadSftp:
    def __init__(self) -> None:
        self.files = {"book.epub": b"original"}
        self.events: list[tuple[str, str]] = []

    class _File(io.BytesIO):
        def __init__(self, owner, path: str, initial: bytes):
            super().__init__(initial)
            self.owner = owner
            self.path = path

        def set_pipelined(self, value: bool) -> None:
            pass

        def close(self) -> None:
            if not self.closed:
                self.owner.files[self.path] = self.getvalue()
                self.owner.events.append(("write", self.path))
            super().close()

    def stat(self, path: str):
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_size=len(self.files[path]))

    def file(self, path: str, mode: str):
        initial = self.files.get(path, b"") if "r+" in mode else b""
        return self._File(self, path, initial)

    def posix_rename(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)
        self.events.append(("rename", destination))

    def remove(self, path: str) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        self.files.pop(path)


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


def test_sftp_operation_stops_after_bounded_retries(monkeypatch) -> None:
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

    assert stalled.calls == 5
    assert reconnects == 4


def test_file_upload_replaces_payload_only_after_atomic_temporary_upload(
    tmp_path, monkeypatch
) -> None:
    local = tmp_path / "book.epub"
    local.write_bytes(b"normalized")
    rm = Remarkable(Config())
    sftp = _AtomicUploadSftp()
    sftp.files["book.epub.esbern-token.upload"] = b"norm"
    rm._sftp = sftp
    monkeypatch.setattr("esbern.remarkable.secrets.token_hex", lambda length: "token")

    rm.put_file(local, "book.epub")

    assert sftp.files == {"book.epub": b"normalized"}
    assert sftp.events == [
        ("write", "book.epub.esbern-token.upload"),
        ("rename", "book.epub"),
    ]


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
