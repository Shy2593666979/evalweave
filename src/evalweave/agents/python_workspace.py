from __future__ import annotations

import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session

from evalweave.core.config import AgentConfig, get_settings
from evalweave.db.models import FileObject
from evalweave.storage import LocalFileStorage

PYTHON_DIALOG_TIMEOUT_SECONDS = 30
PYTHON_ENVIRONMENT_ALLOWLIST = {
    "ALL_PROXY",
    "COMSPEC",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}


def _redact_secrets(value: str, secrets: list[str]) -> str:
    redacted = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _file_contains_secret(path: Path, secrets: list[str]) -> bool:
    encoded = [secret.encode("utf-8") for secret in secrets if secret]
    if not encoded:
        return False
    raw = path.read_bytes()
    if any(secret in raw for secret in encoded):
        return True
    if not zipfile.is_zipfile(path):
        return False
    inspected_bytes = 0
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            inspected_bytes += item.file_size
            if inspected_bytes > 64 * 1024 * 1024:
                break
            if any(secret in archive.read(item) for secret in encoded):
                return True
    return False


def _safe_name(name: str, used: set[str]) -> str:
    candidate = Path(name).name or "input"
    stem = Path(candidate).stem or "input"
    suffix = Path(candidate).suffix
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_python_workspace(
    session: Session,
    project_id: UUID | None,
    draft: dict[str, Any],
    code: str,
    source_file_ids: list[str] | None = None,
    primary_output: str | None = None,
    created_by: UUID | None = None,
    timeout_seconds: float | None = PYTHON_DIALOG_TIMEOUT_SECONDS,
    model_config: AgentConfig | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if project_id is None:
        raise ValueError("Python 文件工具需要当前项目")
    if not code.strip():
        raise ValueError("Python 文件工具需要可执行脚本")
    requested_ids = (
        source_file_ids if source_file_ids is not None else [str(draft.get("source_file_id") or "")]
    )
    requested_ids = [item for item in requested_ids if item]
    sources: list[FileObject] = []
    for raw_id in requested_ids:
        try:
            source = session.get(FileObject, UUID(str(raw_id)))
        except ValueError as error:
            raise ValueError(f"无效文件标识：{raw_id}") from error
        if source is None or source.project_id != project_id:
            raise ValueError(f"文件不存在或不属于当前项目：{raw_id}")
        sources.append(source)

    settings = get_settings()
    storage = LocalFileStorage(settings.storage.local_directory)
    workspace_root = settings.storage.workspace_directory.resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="evalweave-python-",
        dir=workspace_root,
    ) as workspace_name:
        workspace = Path(workspace_name)
        inputs = workspace / "inputs"
        outputs = workspace / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        used_names: set[str] = set()
        manifest: list[dict[str, str]] = []
        for source in sources:
            staged_name = _safe_name(source.original_name, used_names)
            shutil.copyfile(storage.path_for(source.storage_key), inputs / staged_name)
            manifest.append(
                {
                    "file_id": str(source.id),
                    "original_name": source.original_name,
                    "path": f"inputs/{staged_name}",
                }
            )
        (workspace / "manifest.json").write_text(
            json.dumps({"files": manifest}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        script = workspace / "script.py"
        script.write_text(code, encoding="utf-8")
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in PYTHON_ENVIRONMENT_ALLOWLIST
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        injected_secrets: list[str] = []
        if model_config and model_config.enabled and model_config.model:
            environment.update(
                {
                    "EVALWEAVE_MODEL_BASE_URL": model_config.base_url,
                    "EVALWEAVE_MODEL_NAME": model_config.model,
                    "EVALWEAVE_MODEL_API_MODE": model_config.api_mode,
                    "EVALWEAVE_MODEL_API_KEY": model_config.api_key,
                }
            )
            if model_config.api_key:
                injected_secrets.append(model_config.api_key)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(script)],
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        started_at = time.monotonic()
        while True:
            try:
                raw_stdout, raw_stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired as error:
                if cancellation_check is not None:
                    try:
                        cancellation_check()
                    except BaseException:
                        _stop_process_tree(process)
                        process.communicate()
                        raise
                if timeout_seconds is not None and time.monotonic() - started_at >= timeout_seconds:
                    _stop_process_tree(process)
                    process.communicate()
                    raise ValueError(f"Python 文件工具执行超过 {timeout_seconds:g} 秒") from error
        stdout = _redact_secrets(raw_stdout[-8000:].strip(), injected_secrets)
        stderr = _redact_secrets(raw_stderr[-8000:].strip(), injected_secrets)
        if process.returncode != 0:
            raw_detail = stderr or stdout or f"退出码 {process.returncode}"
            detail_lines = raw_detail.splitlines()
            detail = "\n".join(detail_lines[-24:])
            raise ValueError(f"Python 文件工具执行失败：{detail}")

        output_paths = sorted(path for path in outputs.rglob("*") if path.is_file())
        if len(output_paths) > 16:
            raise ValueError("Python 文件工具一次最多生成 16 个文件")
        if any(_file_contains_secret(path, injected_secrets) for path in output_paths):
            raise ValueError("生成文件包含受保护的模型凭据，已拒绝保存")
        output_owner_id = sources[0].created_by if sources else created_by
        if output_paths and output_owner_id is None:
            raise ValueError("Python 文件工具缺少输出文件创建者")
        created: list[FileObject] = []
        max_bytes = get_settings().evaluation.max_file_size_mb * 1024 * 1024
        for output_path in output_paths:
            relative_name = output_path.relative_to(outputs).as_posix()
            file_id = uuid4()
            storage_key = f"projects/{project_id}/dataset_source/{file_id.hex}"
            stored = storage.put(
                storage_key, BytesIO(output_path.read_bytes()), max_bytes=max_bytes
            )
            file_object = FileObject(
                id=file_id,
                project_id=project_id,
                created_by=output_owner_id,
                category="dataset_source",
                original_name=relative_name,
                storage_key=storage_key,
                content_type=mimetypes.guess_type(relative_name)[0],
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
            session.add(file_object)
            created.append(file_object)
        session.commit()
        for file_object in created:
            session.refresh(file_object)

    selected = next(
        (
            item
            for item in created
            if primary_output and item.original_name.casefold() == primary_output.casefold()
        ),
        created[0] if created else None,
    )
    observation = {
        "stdout": stdout,
        "outputs": [
            {
                "file_id": str(item.id),
                "file_name": item.original_name,
                "size_bytes": item.size_bytes,
            }
            for item in created
        ],
        "primary_output_file_id": str(selected.id) if selected else None,
    }
    updates: dict[str, Any] = {}
    if selected is not None:
        updates = {
            "source_file_id": str(selected.id),
            "source_file_name": selected.original_name,
            "source_inspected": False,
            "source_fields": [],
        }
    return observation, updates
