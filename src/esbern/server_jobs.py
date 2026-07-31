"""Persistent background jobs for phone-friendly API clients."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from esbern.server_library import install_books

_MAX_PROGRESS_EVENTS = 200
_MIN_PROGRESS_INTERVAL = 0.25


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
            "progress": {
                "sequence": 0,
                "message": "Queued",
                "detail": None,
                "updated_at": now,
            },
            "progress_events": [
                {
                    "sequence": 0,
                    "message": "Queued",
                    "detail": None,
                    "updated_at": now,
                }
            ],
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
        progress_lock = threading.Lock()
        last_saved_at = 0.0

        def report_progress(
            message: str | None,
            detail: str | None,
            *,
            force: bool = False,
        ) -> None:
            nonlocal last_saved_at
            with progress_lock:
                previous = record.get("progress")
                if not isinstance(previous, dict):
                    previous = {
                        "sequence": -1,
                        "message": "Working",
                        "detail": None,
                    }
                effective_message = message or str(previous.get("message") or "Working")
                previous_detail = previous.get("detail")
                if (
                    effective_message == previous.get("message")
                    and detail == previous_detail
                ):
                    return

                monotonic_now = time.monotonic()
                phase_changed = (
                    message is not None and effective_message != previous.get("message")
                )
                if (
                    not force
                    and not phase_changed
                    and monotonic_now - last_saved_at < _MIN_PROGRESS_INTERVAL
                ):
                    return

                now = _now()
                progress = {
                    "sequence": int(previous.get("sequence", -1)) + 1,
                    "message": effective_message,
                    "detail": detail,
                    "updated_at": now,
                }
                events = record.get("progress_events")
                if not isinstance(events, list):
                    events = []
                events.append(progress.copy())
                record["progress_events"] = events[-_MAX_PROGRESS_EVENTS:]
                record["progress"] = progress
                record["updated_at"] = now
                self._save(record)
                last_saved_at = monotonic_now

        record["status"] = "running"
        record["updated_at"] = _now()
        self._save(record)
        report_progress("Starting book installation", None, force=True)
        try:
            result = install_books(
                self.library_root,
                payload,
                progress_callback=report_progress,
            )
            catalog_result = result.pop("catalog", None)
            if isinstance(catalog_result, dict):
                result["catalog_count"] = catalog_result.get("count")
            record["status"] = "succeeded"
            record["result"] = result
            report_progress("Book installation finished", None, force=True)
        except Exception as error:  # noqa: BLE001 - persist background failures
            record["status"] = "failed"
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            report_progress("Book installation failed", str(error), force=True)
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
            previous = record.get("progress")
            sequence = (
                int(previous.get("sequence", -1)) if isinstance(previous, dict) else -1
            )
            progress = {
                "sequence": sequence + 1,
                "message": "Book installation interrupted",
                "detail": "The server restarted before this job completed.",
                "updated_at": record["updated_at"],
            }
            record["progress"] = progress
            events = record.get("progress_events")
            if not isinstance(events, list):
                events = []
            events.append(progress.copy())
            record["progress_events"] = events[-_MAX_PROGRESS_EVENTS:]
            self._save(record)
