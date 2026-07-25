from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from services.job_runner import atomic_write_json
from services.schedule_config import (
    PARIS,
    load_schedule,
    paris_now,
    record_schedule_event,
)
from services.scheduled_instances import get_scheduled_instance


RCLONE_REMOTE_DEFAULT = "gdrive"
RCLONE_CONFIG_DEFAULT = Path.home() / ".config" / "rclone" / "rclone.conf"
DRIVE_STATES = {
    "not_configured",
    "pending",
    "uploading",
    "succeeded",
    "partially_succeeded",
    "failed",
    "retrying",
}
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DriveNotConfigured(RuntimeError):
    pass


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _config_path() -> Path:
    return Path(os.getenv("OTA_RCLONE_CONFIG", str(RCLONE_CONFIG_DEFAULT)))


def _remote_name() -> str:
    value = os.getenv("OTA_RCLONE_REMOTE", RCLONE_REMOTE_DEFAULT).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("Unsafe rclone remote name")
    return value


def _rclone_binary() -> str | None:
    override = os.getenv("OTA_RCLONE_BIN")
    if override:
        return override
    located = shutil.which("rclone")
    if located:
        return located
    user_local = Path.home() / ".local" / "bin" / "rclone"
    if user_local.is_file() and os.access(user_local, os.X_OK):
        return str(user_local)
    return None


def drive_queue_data_dirs(instance_id: str) -> list[Path]:
    """Return current and legacy isolated queue roots for one logical instance."""

    definition = get_scheduled_instance(instance_id)
    aliases = {
        "near_30_days": ("instance_1", "period_1"),
        "medium_31_120_days": ("instance_2", "period_2"),
        "long_121_365_days": ("instance_3", "period_3"),
    }[definition.instance_id]
    candidates = [
        definition.data_dir,
        *(
            definition.data_dir.parent / alias
            for alias in aliases
        ),
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _base_command() -> list[str]:
    binary = _rclone_binary()
    if not binary:
        raise DriveNotConfigured(
            "rclone is not installed. Install it with: sudo apt install rclone"
        )
    config = _config_path()
    if not config.is_file():
        raise DriveNotConfigured(
            f"rclone configuration is missing at {config}. "
            "Run: scripts/ota-drive configure"
        )
    return [binary, "--config", str(config)]


def _safe_folder_id(folder_id: str) -> str:
    value = str(folder_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        raise ValueError("Invalid Google Drive folder ID")
    return value


def _safe_remote_directory(value: str) -> str:
    normalized = str(value or "").strip("/")
    if (
        not normalized
        or ".." in normalized.split("/")
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", normalized)
    ):
        raise ValueError("Unsafe Drive remote directory")
    return normalized


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: str | Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def drive_state_path(data_dir: str | Path, run_id: int) -> Path:
    return (
        Path(data_dir)
        / "status"
        / "drive_uploads"
        / f"run_{int(run_id)}.json"
    )


def load_drive_state(data_dir: str | Path, run_id: int) -> dict[str, Any]:
    path = drive_state_path(data_dir, run_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_drive_state(
    data_dir: str | Path,
    run_id: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    status = str(state.get("drive_upload_status") or "")
    if status not in DRIVE_STATES:
        raise ValueError(f"Unsupported Drive upload state: {status}")
    state["run_id"] = int(run_id)
    atomic_write_json(drive_state_path(data_dir, run_id), state)
    try:
        from database import db

        db.update_collection_run_delivery(
            int(run_id),
            backend="sqlite",
            **{
                key: state.get(key)
                for key in (
                    "drive_upload_status",
                    "drive_folder_id",
                    "drive_remote_directory",
                    "drive_csv_path",
                    "drive_excel_path",
                    "drive_manifest_path",
                    "drive_uploaded_at",
                    "drive_upload_error",
                    "drive_upload_attempts",
                    "drive_last_attempt_at",
                    "drive_csv_checksum",
                    "drive_excel_checksum",
                )
            },
        )
    except Exception:
        # Local state is authoritative for delivery retries.  A collection DB
        # metadata failure must not discard a successful or retryable upload.
        pass
    try:
        from services.operational_status import update_operational_status

        update_operational_status(
            str(state.get("instance_id")),
            data_dir=data_dir,
            google_drive_upload_status=state.get("drive_upload_status"),
            google_drive_folder_id=state.get("drive_folder_id"),
            google_drive_remote_filename=state.get(
                "google_drive_remote_filename"
            ),
            google_drive_remote_bytes=state.get(
                "google_drive_remote_bytes"
            ),
            google_drive_upload_error=state.get("drive_upload_error"),
        )
    except Exception:
        pass
    return state


def drive_configuration_status(
    instance_id: str,
    *,
    folder_id: str | None = None,
    runner: Runner = _run,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    target = _safe_folder_id(folder_id or definition.drive_folder_id)
    binary = _rclone_binary()
    config = _config_path()
    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "folder_id": target,
        "remote": _remote_name(),
        "rclone_binary": binary,
        "config_path": str(config),
        "setup_command": "scripts/ota-drive configure",
    }
    if not binary:
        return {
            **payload,
            "status": "not_configured",
            "error": "rclone is not installed",
            "installation_command": "sudo apt install rclone",
        }
    if not config.is_file():
        return {
            **payload,
            "status": "not_configured",
            "error": f"rclone configuration is missing at {config}",
        }
    result = runner(
        [
            *_base_command(),
            "listremotes",
        ]
    )
    remotes = {
        line.rstrip(":")
        for line in result.stdout.splitlines()
        if line.strip()
    }
    if result.returncode != 0 or _remote_name() not in remotes:
        return {
            **payload,
            "status": "not_configured",
            "error": (
                result.stderr.strip()
                or f"rclone remote {_remote_name()!r} is not configured"
            ),
        }
    return {**payload, "status": "configured", "error": None}


def _remote_spec(remote_path: str = "") -> str:
    clean = str(remote_path or "").strip("/")
    return f"{_remote_name()}:{clean}"


def _folder_flags(folder_id: str) -> list[str]:
    return ["--drive-root-folder-id", _safe_folder_id(folder_id)]


def _raise_command_error(
    action: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    if result.returncode == 0:
        return
    message = result.stderr.strip() or result.stdout.strip() or "unknown error"
    raise RuntimeError(f"rclone {action} failed: {message}")


def _remote_metadata(
    remote_path: str,
    folder_id: str,
    *,
    runner: Runner,
) -> dict[str, Any] | None:
    result = runner(
        [
            *_base_command(),
            "lsjson",
            _remote_spec(remote_path),
            "--stat",
            "--hash",
            *_folder_flags(folder_id),
        ]
    )
    if result.returncode != 0:
        message = (result.stderr or "").lower()
        if "not found" in message or "directory not found" in message:
            return None
        _raise_command_error("lsjson", result)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("rclone returned invalid lsjson metadata") from exc
    return payload if isinstance(payload, dict) else None


def _remote_matches_local(
    local_path: Path,
    metadata: dict[str, Any],
) -> bool:
    if int(metadata.get("Size") or -1) != local_path.stat().st_size:
        return False
    hashes = metadata.get("Hashes")
    if isinstance(hashes, dict) and hashes.get("MD5"):
        return str(hashes["MD5"]).lower() == md5_file(local_path).lower()
    return True


def _upload_one(
    local_path: Path,
    remote_path: str,
    folder_id: str,
    *,
    runner: Runner,
) -> str:
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    remote_path = _safe_remote_directory(remote_path)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            existing = _remote_metadata(
                remote_path,
                folder_id,
                runner=runner,
            )
            if existing is not None:
                if _remote_matches_local(local_path, existing):
                    return "already_present"
                raise FileExistsError(
                    f"Refusing to overwrite different Drive file: {remote_path}"
                )
            result = runner(
                [
                    *_base_command(),
                    "copyto",
                    str(local_path),
                    _remote_spec(remote_path),
                    "--immutable",
                    *_folder_flags(folder_id),
                ]
            )
            _raise_command_error("copyto", result)
            verified = _remote_metadata(
                remote_path,
                folder_id,
                runner=runner,
            )
            if verified is None or not _remote_matches_local(
                local_path,
                verified,
            ):
                raise RuntimeError(
                    "Drive verification did not match local file: "
                    f"{remote_path}"
                )
            return "uploaded"
        except FileExistsError:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            message = str(exc).lower()
            transient = any(
                token in message
                for token in (
                    "timeout",
                    "temporar",
                    "rate limit",
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                    "connection",
                    "network",
                )
            )
            if not transient or attempt == 3:
                raise
            time.sleep(2 ** (attempt - 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("Drive upload failed without an error")


def test_drive_folder_access(
    instance_id: str,
    *,
    folder_id: str | None = None,
    runner: Runner = _run,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    target = _safe_folder_id(folder_id or definition.drive_folder_id)
    status = drive_configuration_status(
        instance_id,
        folder_id=target,
        runner=runner,
    )
    if status["status"] != "configured":
        return status
    listing = runner(
        [
            *_base_command(),
            "lsf",
            _remote_spec(),
            "--max-depth",
            "1",
            *_folder_flags(target),
        ]
    )
    _raise_command_error("folder listing", listing)
    probe_name = f".ota_write_probe_{instance_id}_{uuid4().hex}.txt"
    with tempfile.TemporaryDirectory(prefix="ota-drive-probe-") as temporary:
        probe = Path(temporary) / probe_name
        probe.write_text(
            f"Disposable OTA Drive write test for {instance_id}\n",
            encoding="utf-8",
        )
        _upload_one(probe, probe_name, target, runner=runner)
    delete_result = runner(
        [
            *_base_command(),
            "deletefile",
            _remote_spec(probe_name),
            *_folder_flags(target),
        ]
    )
    _raise_command_error("disposable probe cleanup", delete_result)
    return {
        **status,
        "status": "verified",
        "read_access": True,
        "write_access": True,
        "disposable_probe_removed": True,
    }


def _manifest_payload(
    *,
    instance_id: str,
    run: dict[str, Any],
    csv_path: Path,
    excel_path: Path | None,
    remote_directory: str,
    folder_id: str,
    csv_remote_path: str,
    excel_remote_path: str | None,
    manifest_remote_path: str,
    schedule: dict[str, Any],
    upload_timestamp: str,
) -> dict[str, Any]:
    sqlite_rows = int(run.get("authoritative_row_count") or 0)
    return {
        "instance_id": instance_id,
        "run_id": int(run["id"]),
        "source": run.get("source"),
        "city": run.get("city_or_region"),
        "resolved_start_date": (
            run.get("resolved_start_date") or run.get("checkin_date")
        ),
        "resolved_end_date": run.get("resolved_end_date"),
        "date_mode": run.get("date_mode") or "manual",
        "schedule_slot": run.get("schedule_slot"),
        "frequency_configuration": (
            run.get("frequency_configuration")
            or {
                "frequency_mode": schedule.get("frequency_mode"),
                "interval_minutes": schedule.get("interval_minutes"),
                "runs_per_day": schedule.get("runs_per_day"),
                "daily_run_times": schedule.get("daily_run_times"),
            }
        ),
        "run_status": run.get("status"),
        "sqlite_row_count": sqlite_rows,
        "csv_row_count": int(run.get("csv_rows_exported") or sqlite_rows),
        "excel_row_count": (
            int(run.get("excel_rows_exported") or sqlite_rows)
            if excel_path is not None
            else None
        ),
        "collection_started_at": run.get("started_at"),
        "collection_completed_at": run.get("completed_at"),
        "local_csv_path": str(csv_path.resolve()),
        "local_excel_path": (
            str(excel_path.resolve()) if excel_path is not None else None
        ),
        "upload_timestamp": upload_timestamp,
        "csv_sha256": sha256_file(csv_path),
        "excel_sha256": (
            sha256_file(excel_path) if excel_path is not None else None
        ),
        "drive_folder_id": folder_id,
        "drive_remote_directory": remote_directory,
        "drive_csv_path": csv_remote_path,
        "drive_excel_path": excel_remote_path,
        "drive_manifest_path": manifest_remote_path,
    }


def upload_run_bundle(
    instance_id: str,
    run: dict[str, Any],
    *,
    data_dir: str | Path,
    csv_path: str | Path,
    excel_path: str | Path | None = None,
    folder_id: str | None = None,
    upload_csv: bool = True,
    upload_excel: bool = True,
    retry: bool = False,
    runner: Runner = _run,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    target = _safe_folder_id(folder_id or definition.drive_folder_id)
    if target != definition.drive_folder_id:
        raise ValueError("Drive folder does not match this instance")
    run_id = int(run["id"])
    csv_file = Path(csv_path).resolve()
    excel_file = Path(excel_path).resolve() if excel_path else None
    prior = load_drive_state(data_dir, run_id)
    csv_checksum = sha256_file(csv_file)
    excel_checksum = sha256_file(excel_file) if excel_file else None
    if (
        prior.get("drive_upload_status") == "succeeded"
        and prior.get("drive_csv_checksum") == csv_checksum
        and prior.get("drive_excel_checksum") == excel_checksum
    ):
        return prior
    completed = datetime.fromisoformat(
        str(run.get("completed_at") or paris_now().isoformat())
    )
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=PARIS)
    completed = completed.astimezone(PARIS)
    timestamp = completed.strftime("%Y%m%d_%H%M%S")
    remote_directory = _safe_remote_directory(
        f"{completed:%Y}/{completed:%m}/run_{run_id}_{timestamp}"
    )
    csv_remote = f"{remote_directory}/{csv_file.name}"
    excel_remote = (
        f"{remote_directory}/{excel_file.name}" if excel_file else None
    )
    manifest_name = (
        f"ota_manifest_{instance_id}_run_{run_id}_{timestamp}.json"
    )
    manifest_remote = f"{remote_directory}/{manifest_name}"
    attempts = int(prior.get("drive_upload_attempts") or 0) + 1
    manifest_created_at = str(
        prior.get("manifest_created_at")
        or paris_now().isoformat(timespec="seconds")
    )
    state = {
        **prior,
        "schema_version": 1,
        "instance_id": instance_id,
        "run_id": run_id,
        "drive_upload_status": "pending",
        "drive_folder_id": target,
        "drive_remote_directory": remote_directory,
        "drive_csv_path": csv_remote if upload_csv else None,
        "drive_excel_path": excel_remote if upload_excel else None,
        "drive_manifest_path": manifest_remote,
        "drive_uploaded_at": None,
        "drive_upload_error": None,
        "drive_upload_attempts": attempts,
        "drive_last_attempt_at": paris_now().isoformat(timespec="seconds"),
        "drive_csv_checksum": csv_checksum,
        "drive_excel_checksum": excel_checksum,
        "local_csv_bytes": csv_file.stat().st_size,
        "local_excel_bytes": (
            excel_file.stat().st_size if excel_file else None
        ),
        "google_drive_remote_filename": csv_file.name,
        "google_drive_remote_bytes": None,
        "csv_upload_status": (
            prior.get("csv_upload_status") or "pending"
            if upload_csv
            else "disabled"
        ),
        "excel_upload_status": (
            prior.get("excel_upload_status") or "pending"
            if upload_excel
            else "disabled"
        ),
        "manifest_upload_status": prior.get("manifest_upload_status")
        or "pending",
        "manifest_created_at": manifest_created_at,
        "local_csv_path": str(csv_file),
        "local_excel_path": str(excel_file) if excel_file else None,
    }
    _save_drive_state(data_dir, run_id, state)
    record_schedule_event(
        instance_id,
        "drive_upload_pending",
        data_dir=data_dir,
        run_id=run_id,
        drive_upload_status="pending",
    )
    state["drive_upload_status"] = "retrying" if retry else "uploading"
    _save_drive_state(data_dir, run_id, state)
    configuration = drive_configuration_status(
        instance_id,
        folder_id=target,
        runner=runner,
    )
    if configuration["status"] != "configured":
        # The verified local export remains durable and queued. Missing local
        # authentication is an operational prerequisite, not a lost export.
        state["drive_upload_status"] = "pending"
        state["drive_upload_error"] = configuration.get("error")
        _save_drive_state(data_dir, run_id, state)
        record_schedule_event(
            instance_id,
            "drive_upload_pending",
            data_dir=data_dir,
            run_id=run_id,
            drive_upload_status="pending",
            error=state["drive_upload_error"],
        )
        return state
    errors: list[str] = []
    successes = 0
    if upload_csv:
        try:
            _upload_one(csv_file, csv_remote, target, runner=runner)
            csv_metadata = _remote_metadata(
                csv_remote,
                target,
                runner=runner,
            ) or {}
            state["csv_upload_status"] = "succeeded"
            state["google_drive_remote_filename"] = csv_file.name
            state["google_drive_remote_bytes"] = int(
                csv_metadata.get("Size") or csv_file.stat().st_size
            )
            state["google_drive_remote_checksum"] = (
                (csv_metadata.get("Hashes") or {}).get("MD5")
                if isinstance(csv_metadata.get("Hashes"), dict)
                else None
            )
            successes += 1
        except Exception as exc:
            state["csv_upload_status"] = "failed"
            errors.append(f"CSV: {exc}")
    if upload_excel:
        try:
            if excel_file is None:
                raise FileNotFoundError("No successful local Excel export")
            _upload_one(excel_file, excel_remote, target, runner=runner)
            state["excel_upload_status"] = "succeeded"
            successes += 1
        except Exception as exc:
            state["excel_upload_status"] = "failed"
            errors.append(f"Excel: {exc}")
    schedule = load_schedule(instance_id, data_dir=data_dir)
    manifest = _manifest_payload(
        instance_id=instance_id,
        run=run,
        csv_path=csv_file,
        excel_path=excel_file,
        remote_directory=remote_directory,
        folder_id=target,
        csv_remote_path=csv_remote,
        excel_remote_path=excel_remote,
        manifest_remote_path=manifest_remote,
        schedule=schedule,
        upload_timestamp=manifest_created_at,
    )
    manifest_path = Path(data_dir) / "exports" / manifest_name
    if not manifest_path.exists():
        atomic_write_json(manifest_path, manifest)
    state["local_manifest_path"] = str(manifest_path.resolve())
    try:
        _upload_one(
            manifest_path,
            manifest_remote,
            target,
            runner=runner,
        )
        state["manifest_upload_status"] = "succeeded"
        successes += 1
    except Exception as exc:
        state["manifest_upload_status"] = "failed"
        errors.append(f"Manifest: {exc}")
    requested = int(upload_csv) + int(upload_excel) + 1
    if not errors and successes == requested:
        state["drive_upload_status"] = "succeeded"
        state["drive_uploaded_at"] = paris_now().isoformat(timespec="seconds")
    elif successes:
        state["drive_upload_status"] = "partially_succeeded"
    else:
        state["drive_upload_status"] = "failed"
    state["drive_upload_error"] = "; ".join(errors) or None
    _save_drive_state(data_dir, run_id, state)
    event = (
        "drive_upload_succeeded"
        if state["drive_upload_status"] == "succeeded"
        else "drive_upload_failed"
    )
    record_schedule_event(
        instance_id,
        event,
        data_dir=data_dir,
        run_id=run_id,
        drive_upload_status=state["drive_upload_status"],
        error=state["drive_upload_error"],
    )
    if retry:
        record_schedule_event(
            instance_id,
            "drive_upload_retried",
            data_dir=data_dir,
            run_id=run_id,
            drive_upload_status=state["drive_upload_status"],
        )
    return state


def retry_latest_failed_upload(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    runner: Runner = _run,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dirs = (
        [Path(data_dir).resolve()]
        if data_dir is not None
        else drive_queue_data_dirs(instance_id)
    )
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for resolved_dir in resolved_dirs:
        states_dir = resolved_dir / "status" / "drive_uploads"
        for path in states_dir.glob("run_*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if state.get("drive_upload_status") not in {
                "failed",
                "partially_succeeded",
                "pending",
                "not_configured",
            }:
                continue
            candidates.append((path.stat().st_mtime, resolved_dir, state))
    if not candidates:
        return {
            "drive_upload_status": "no_pending_upload",
            "instance_id": instance_id,
        }
    _, resolved_dir, state = sorted(
        candidates,
        key=lambda value: value[0],
        reverse=True,
    )[0]
    run_id = int(state["run_id"])
    from database import db

    previous_database = db.SQLITE_DB_PATH
    try:
        db.SQLITE_DB_PATH = (
            resolved_dir / "hotel_price_collector.sqlite"
        )
        run = db.fetch_collection_run_by_id(run_id, backend="sqlite")
        if run is None:
            raise ValueError(f"Run {run_id} no longer exists")
        return upload_run_bundle(
            instance_id,
            run,
            data_dir=resolved_dir,
            csv_path=state["local_csv_path"],
            excel_path=state["local_excel_path"],
            folder_id=state["drive_folder_id"],
            upload_csv=state.get("csv_upload_status") != "disabled",
            upload_excel=state.get("excel_upload_status") != "disabled",
            retry=True,
            runner=runner,
        )
    finally:
        db.SQLITE_DB_PATH = previous_database


def pending_upload_states(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    definition = get_scheduled_instance(instance_id)
    rows = []
    roots = (
        [Path(data_dir).resolve()]
        if data_dir is not None
        else drive_queue_data_dirs(instance_id)
    )
    for data_root in roots:
        root = data_root / "status" / "drive_uploads"
        for path in root.glob("run_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if payload.get("drive_upload_status") in {
                "pending",
                "failed",
                "partially_succeeded",
                "not_configured",
            }:
                rows.append(
                    {**payload, "queue_data_dir": str(data_root)}
                )
    return sorted(
        rows,
        key=lambda value: str(value.get("drive_last_attempt_at") or ""),
        reverse=True,
    )
