"""Persistent background jobs for phone-friendly API clients."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from esbern.server_library import install_books


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class JobStore:
    def __init__(self, library_root: Path):
        self.library_root = library_root
        self.directory = library_root / ".esbern" / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="esbern-job"
        )
        self._mark_interrupted_jobs()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def enqueue_book(self, payload: dict[str, object]) -> dict[str, object]:
        job_id = uuid.uuid4().hex
        now = _now()
        record: dict[str, object] = {
            "id": job_id,
            "type": "install_book",
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "request": payload,
            "result": None,
            "error": None,
        }
        self._save(record)
        self._executor.submit(self._run_book, job_id, payload)
        return record

    def get(self, job_id: str) -> dict[str, object]:
        try:
            normalized = uuid.UUID(hex=job_id).hex
        except ValueError as error:
            raise ValueError("Invalid job id.") from error
        job_path = self.directory / f"{normalized}.json"
        with job_path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise TypeError("Stored job is invalid.")
        return value

    def _run_book(self, job_id: str, payload: dict[str, object]) -> None:
        record = self.get(job_id)
        record["status"] = "running"
        record["updated_at"] = _now()
        self._save(record)
        try:
            result = install_books(self.library_root, payload)
            catalog_result = result.pop("catalog", None)
            if isinstance(catalog_result, dict):
                result["catalog_count"] = catalog_result.get("count")
            record["status"] = "succeeded"
            record["result"] = result
        except Exception as error:  # noqa: BLE001 - persist background failures
            record["status"] = "failed"
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        record["updated_at"] = _now()
        self._save(record)

    def _save(self, record: dict[str, object]) -> None:
        job_path = self.directory / f"{record['id']}.json"
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{record['id']}.", suffix=".tmp", dir=self.directory
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.chmod(temporary_name, 0o600)
                os.replace(temporary_name, job_path)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _mark_interrupted_jobs(self) -> None:
        for job_path in self.directory.glob("*.json"):
            try:
                with job_path.open(encoding="utf-8") as stream:
                    record = json.load(stream)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("status") not in {
                "queued",
                "running",
            }:
                continue
            record["status"] = "failed"
            record["updated_at"] = _now()
            record["error"] = {
                "type": "InterruptedError",
                "message": "The server restarted before this job completed.",
            }
            self._save(record)
