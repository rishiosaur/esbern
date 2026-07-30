from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CONFIG_DIR = Path(os.environ.get("ESBERN_CONFIG_DIR", Path.home() / ".config" / "esbern"))
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    host: str = "10.11.99.1"
    port: int = 22
    user: str = "root"
    password: str | None = None
    key_path: str | None = None
    # Trust-on-first-use SSH host key, persisted after the first connection.
    host_key: str | None = None
    # Where on the device xochitl stores documents.
    remote_root: str = "/home/root/.local/share/remarkable/xochitl"
    # Restart xochitl after sync so new files appear immediately.
    restart_xochitl: bool = True


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No config at {CONFIG_PATH}. Run `esbern init` first."
        )
    with CONFIG_PATH.open("rb") as f:
        data = tomllib.load(f)
    return Config(**data)


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in asdict(cfg).items():
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}")
        else:
            escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"')
    fd, temporary_name = tempfile.mkstemp(prefix=".config.", dir=CONFIG_DIR, text=True)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, CONFIG_PATH)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
