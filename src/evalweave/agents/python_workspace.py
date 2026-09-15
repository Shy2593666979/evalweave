from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session

from evalweave.core.config import get_settings
from evalweave.db.models import FileObject
from evalweave.storage import LocalFileStorage

PYTHON_WORKSPACE_TIMEOUT_SECONDS = 300


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


def run_python_workspace(
    session: Session,
    project_id: UUID | None,
    draft: dict[str, Any],
    code: str,
    source_file_ids: list[str] | None = None,
    primary_output: str | None = None,
    created_by: UUID | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if project_id is None:
        raise ValueError("Python 文件工具需要当前项目")
    if not code.strip():
        raise ValueError("Python 文件工具需要可执行脚本")
    requested_ids = (
        source_file_ids
        if source_file_ids is not None
        else [str(draft.get("source_file_id") or "")]
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

    storage = LocalFileStorage(get_settings().storage.local_directory)
    with tempfile.TemporaryDirectory(prefix="evalweave-python-") as workspace_name:
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
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(script)],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PYTHON_WORKSPACE_TIMEOUT_SECONDS,
                creationflags=flags,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"Python 文件工具执行超过 {PYTHON_WORKSPACE_TIMEOUT_SECONDS} 秒"
            ) from error
        stdout = completed.stdout[-8000:].strip()
        stderr = completed.stderr[-8000:].strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"退出码 {completed.returncode}"
            raise ValueError(f"Python 文件工具执行失败：{detail}")

        output_paths = sorted(path for path in outputs.rglob("*") if path.is_file())
        if len(output_paths) > 16:
            raise ValueError("Python 文件工具一次最多生成 16 个文件")
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
