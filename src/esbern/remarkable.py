"""SSH/SFTP client wrapper for talking to the reMarkable."""

from __future__ import annotations

import posixpath
import re
import secrets
import shlex
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import paramiko

from esbern import config
from esbern.config import Config

_T = TypeVar("_T")
_CONNECT_TIMEOUT_SECONDS = 10
_OPERATION_TIMEOUT_SECONDS = 120
_ATTEMPTS = 5
_HOST_KEY_LOCK = threading.Lock()


class HostKeyMismatch(paramiko.SSHException):
    """Raised when a device no longer presents its trusted SSH host key."""


class _TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        del client
        presented = f"{key.get_name()} {key.get_base64()}"
        with _HOST_KEY_LOCK:
            if self.cfg.host_key is None:
                self.cfg.host_key = presented
                config.save(self.cfg)
                return
            if not secrets.compare_digest(self.cfg.host_key, presented):
                raise HostKeyMismatch(
                    f"SSH host key for {hostname} changed; run `esbern init` "
                    "only after verifying the device and connection"
                )


class Remarkable:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(_ATTEMPTS):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(_TrustOnFirstUsePolicy(self.cfg))
            kwargs: dict = {
                "hostname": self.cfg.host,
                "port": self.cfg.port,
                "username": self.cfg.user,
                "allow_agent": False,
                "look_for_keys": False,
                "timeout": _CONNECT_TIMEOUT_SECONDS,
                "auth_timeout": _CONNECT_TIMEOUT_SECONDS,
                "banner_timeout": _CONNECT_TIMEOUT_SECONDS,
                "channel_timeout": _CONNECT_TIMEOUT_SECONDS,
            }
            if self.cfg.key_path:
                kwargs["key_filename"] = self.cfg.key_path
                kwargs["look_for_keys"] = True
            if self.cfg.password:
                kwargs["password"] = self.cfg.password
            try:
                client.connect(**kwargs)
                transport = client.get_transport()
                if transport:
                    transport.set_keepalive(15)
                sftp = client.open_sftp()
                sftp.get_channel().settimeout(_OPERATION_TIMEOUT_SECONDS)
            except (
                HostKeyMismatch,
                paramiko.AuthenticationException,
                paramiko.BadHostKeyException,
            ):
                client.close()
                raise
            except (EOFError, OSError, paramiko.SSHException) as exc:
                client.close()
                last_error = exc
                if attempt + 1 == _ATTEMPTS:
                    raise
                continue
            self._client = client
            self._sftp = sftp
            return
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._client:
            self._client.close()
            self._client = None

    def _reconnect(self) -> None:
        self.close()
        self.connect()

    def _sftp_call(self, operation: Callable[[paramiko.SFTPClient], _T]) -> _T:
        """Retry an idempotent SFTP operation once on a dead Wi-Fi channel."""
        for attempt in range(_ATTEMPTS):
            try:
                return operation(self.sftp)
            except FileNotFoundError:
                raise
            except (EOFError, OSError, paramiko.SSHException):
                if attempt + 1 == _ATTEMPTS:
                    raise
                self._reconnect()
        raise AssertionError("unreachable")

    @property
    def sftp(self) -> paramiko.SFTPClient:
        assert self._sftp, "not connected"
        return self._sftp

    def remote_path(self, *parts: str) -> str:
        return posixpath.join(self.cfg.remote_root, *parts)

    def exists(self, path: str) -> bool:
        try:
            self._sftp_call(lambda sftp: sftp.stat(path))
            return True
        except FileNotFoundError:
            return False

    def stat_mtime(self, path: str) -> float:
        try:
            return self._sftp_call(lambda sftp: sftp.stat(path)).st_mtime or 0.0
        except FileNotFoundError:
            return 0.0

    def stat_size(self, path: str) -> int | None:
        try:
            size = self._sftp_call(lambda sftp: sftp.stat(path)).st_size
        except FileNotFoundError:
            return None
        return int(size) if size is not None else None

    def file_sha256(self, path: str) -> str | None:
        """Hash a payload on-device without transferring it over SFTP."""
        rc, out, _ = self.exec(f"sha256sum {shlex.quote(path)}")
        if rc != 0:
            return None
        digest = out.split(maxsplit=1)[0].lower() if out.strip() else ""
        return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None

    def newest_mtime(self, *paths: str) -> float:
        return max((self.stat_mtime(p) for p in paths), default=0.0)

    def _atomic_replace(
        self,
        remote: str,
        write: Callable[[paramiko.SFTPClient, str], None],
    ) -> None:
        temporary = f"{remote}.esbern-{secrets.token_hex(8)}.upload"

        def replace(sftp: paramiko.SFTPClient) -> None:
            write(sftp, temporary)
            # OpenSSH's posix-rename extension atomically replaces the old
            # file only after the complete temporary write is durable.
            sftp.posix_rename(temporary, remote)

        try:
            self._sftp_call(replace)
        finally:
            if self._sftp is not None:
                try:
                    self._sftp_call(lambda sftp: sftp.remove(temporary))
                except (EOFError, OSError, paramiko.SSHException):
                    pass

    def annotation_dir_mtime(self, uid: str) -> float:
        """Newest mtime among files inside <uuid>/ (annotation pages, thumbnails)."""
        d = self.remote_path(uid)
        try:
            entries = self._sftp_call(lambda sftp: sftp.listdir_attr(d))
        except FileNotFoundError:
            return 0.0
        return max((e.st_mtime or 0.0 for e in entries), default=0.0)

    def put_text(self, remote: str, text: str) -> None:
        def write(sftp: paramiko.SFTPClient, temporary: str) -> None:
            with sftp.file(temporary, "w") as f:
                f.write(text)

        self._atomic_replace(remote, write)

    def get_text(self, remote: str) -> str:
        def read(sftp: paramiko.SFTPClient) -> str:
            with sftp.file(remote, "r") as f:
                return f.read().decode("utf-8", "replace")

        return self._sftp_call(read)

    def put_file(
        self,
        local: Path,
        remote: str,
        callback: Callable[[int, int], None] | None = None,
    ) -> None:
        def upload(sftp: paramiko.SFTPClient, temporary: str) -> None:
            total = local.stat().st_size
            try:
                remote_size = int(sftp.stat(temporary).st_size or 0)
            except FileNotFoundError:
                remote_size = 0
            if remote_size < 0 or remote_size > total:
                sftp.remove(temporary)
                remote_size = 0

            with local.open("rb") as source:
                source.seek(remote_size)
                mode = "r+b" if remote_size else "wb"
                with sftp.file(temporary, mode) as target:
                    if remote_size:
                        target.seek(remote_size)
                    target.set_pipelined(True)
                    sent = remote_size
                    if callback is not None:
                        callback(sent, total)
                    while chunk := source.read(256 * 1024):
                        target.write(chunk)
                        sent += len(chunk)
                        if callback is not None:
                            callback(sent, total)

            uploaded = int(sftp.stat(temporary).st_size or 0)
            if uploaded != total:
                raise OSError(
                    f"incomplete temporary upload: {uploaded} of {total} bytes"
                )

        self._atomic_replace(remote, upload)

    def get_file(
        self,
        remote: str,
        local: Path,
        callback: Callable[[int, int], None] | None = None,
    ) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._sftp_call(lambda sftp: sftp.get(remote, str(local), callback=callback))

    def list_xochitl_metadata(self) -> list[tuple[str, float]]:
        """Return [(uuid, metadata_mtime)] for every item in xochitl/."""
        out: list[tuple[str, float]] = []
        try:
            entries = self._sftp_call(
                lambda sftp: sftp.listdir_attr(self.cfg.remote_root)
            )
        except FileNotFoundError:
            return out
        for e in entries:
            if e.filename.endswith(".metadata"):
                uid = e.filename[: -len(".metadata")]
                out.append((uid, e.st_mtime or 0.0))
        return out

    def read_xochitl_metadata(self) -> list[tuple[str, float, str]]:
        """Read all metadata through one device-local shell stream.

        A separate SFTP round trip for every metadata file is extremely slow
        and more likely to wedge over Wi-Fi. JSON cannot contain literal NUL
        bytes, so a NUL-delimited mtime table and filename/content records are
        unambiguous.
        """
        root = shlex.quote(self.cfg.remote_root)
        command = (
            f"cd {root} || exit $?; "
            "stat -c '%n %Y' *.metadata; "
            "printf '\\0'; "
            "for f in *.metadata; do "
            '[ -f "$f" ] || continue; '
            "printf '%s\\0' \"$f\"; "
            'cat "$f"; '
            "printf '\\0'; "
            "done"
        )
        rc, out, err = self.exec(command)
        if rc != 0:
            raise OSError(err.strip() or "unable to read reMarkable metadata")
        stat_block, separator, payload = out.partition("\0")
        if not separator:
            raise OSError("incomplete reMarkable metadata stream")
        mtimes: dict[str, float] = {}
        for line in stat_block.splitlines():
            filename, separator, raw_mtime = line.rpartition(" ")
            if not separator:
                continue
            try:
                mtimes[filename] = float(raw_mtime)
            except ValueError:
                mtimes[filename] = 0.0
        parts = payload.split("\0")
        if parts and not parts[-1]:
            parts.pop()
        if len(parts) % 2:
            raise OSError("incomplete reMarkable metadata stream")
        records: list[tuple[str, float, str]] = []
        for index in range(0, len(parts), 2):
            filename, raw = parts[index : index + 2]
            if not filename.endswith(".metadata"):
                continue
            uid = filename[: -len(".metadata")]
            records.append((uid, mtimes.get(filename, 0.0), raw))
        return records

    def exec(self, cmd: str) -> tuple[int, str, str]:
        for attempt in range(_ATTEMPTS):
            assert self._client, "not connected"
            try:
                _, stdout, stderr = self._client.exec_command(
                    cmd, timeout=_OPERATION_TIMEOUT_SECONDS
                )
                out = stdout.read().decode("utf-8", "replace")
                err = stderr.read().decode("utf-8", "replace")
                rc = stdout.channel.recv_exit_status()
                return rc, out, err
            except (EOFError, OSError, paramiko.SSHException):
                if attempt + 1 == _ATTEMPTS:
                    raise
                self._reconnect()
        raise AssertionError("unreachable")

    def restart_xochitl(self) -> None:
        self.exec("systemctl restart xochitl")


@contextmanager
def connected(cfg: Config) -> Iterator[Remarkable]:
    rm = Remarkable(cfg)
    rm.connect()
    try:
        yield rm
    finally:
        rm.close()
