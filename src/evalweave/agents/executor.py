from __future__ import annotations

import json
import math
import time
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlmodel import Session

from evalweave.agents.inspection import load_source_rows
from evalweave.core.config import get_settings
from evalweave.db.models import AgentJob, FileObject
from evalweave.storage import LocalFileStorage


def render_template(value: Any, row: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_template(item, row) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, row) for item in value]
    if not isinstance(value, str):
        return value
    if value == "{{row}}":
        return row
    if value.startswith("{{") and value.endswith("}}"):
        key = value[2:-2].strip()
        return row.get(key)
    result = value
    for key, item in row.items():
        result = result.replace(f"{{{{{key}}}}}", str(item))
    return result


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[max(index, 0)], 2)


def validate_target(target: dict[str, Any]) -> tuple[str, dict[str, str]]:
    url = str(target.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target URL must use http or https")
    allowed_hosts = {host.lower() for host in get_settings().agent.allowed_target_hosts}
    if parsed.hostname.lower() not in allowed_hosts:
        raise ValueError(f"Target host is not allowed: {parsed.hostname}")
    if "headers" in target:
        raise ValueError("Configure target headers in application.yaml, not in job input")
    return url, dict(get_settings().agent.target_headers)


def store_results(session: Session, job: AgentJob, records: list[dict[str, Any]]) -> FileObject:
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    file_id = uuid4()
    storage_key = f"projects/{job.project_id}/evaluation_result/{file_id.hex}"
    stored = LocalFileStorage(get_settings().storage.local_directory).put(
        storage_key, BytesIO(content.encode("utf-8")), max_bytes=100 * 1024 * 1024
    )
    file_object = FileObject(
        id=file_id,
        project_id=job.project_id,
        created_by=job.created_by,
        category="evaluation_result",
        original_name=f"agent-job-{job.id}-results.jsonl",
        storage_key=storage_key,
        content_type="application/x-ndjson",
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    session.add(file_object)
    session.commit()
    session.refresh(file_object)
    return file_object


def execute_http_target(session: Session, job: AgentJob) -> dict[str, Any]:
    target = job.input_config.get("target")
    if not isinstance(target, dict) or not target:
        return {
            "executed": False,
            "configuration_required": ["target"],
            "message": "No target connector was configured.",
        }
    if not job.source_file_id:
        raise ValueError("HTTP target execution requires a source file")
    source = session.get(FileObject, job.source_file_id)
    if source is None or source.project_id != job.project_id:
        raise ValueError("Source file does not belong to this project")
    url, headers = validate_target(target)
    max_cases = int(job.input_config.get("max_cases", 100))
    if not 1 <= max_cases <= 10_000:
        raise ValueError("max_cases must be between 1 and 10000")
    path = LocalFileStorage(get_settings().storage.local_directory).path_for(source.storage_key)
    rows = load_source_rows(path, source.original_name, max_cases)
    body_template = target.get("body", "{{row}}")
    response_path = str(target.get("response_path", "")).strip()
    timeout = float(
        target.get("timeout_seconds", get_settings().evaluation.default_timeout_seconds)
    )
    records = []
    latencies = []
    with httpx.Client(timeout=timeout) as client:
        for index, row in enumerate(rows):
            started = time.perf_counter()
            try:
                response = client.post(
                    url, headers=headers, json=render_template(body_template, row)
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies.append(elapsed_ms)
                response.raise_for_status()
                output: Any = response.json()
                if response_path:
                    for part in response_path.split("."):
                        output = output[int(part)] if isinstance(output, list) else output[part]
                records.append(
                    {
                        "case_index": index,
                        "status": "completed",
                        "latency_ms": round(elapsed_ms, 2),
                        "input": row,
                        "output": output,
                    }
                )
            except Exception as error:
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies.append(elapsed_ms)
                records.append(
                    {
                        "case_index": index,
                        "status": "failed",
                        "latency_ms": round(elapsed_ms, 2),
                        "input": row,
                        "error": str(error)[:2000],
                    }
                )
    result_file = store_results(session, job, records)
    job.result_file_id = result_file.id
    session.add(job)
    session.commit()
    succeeded = sum(record["status"] == "completed" for record in records)
    return {
        "executed": True,
        "total_cases": len(records),
        "succeeded": succeeded,
        "failed": len(records) - succeeded,
        "success_rate": round(succeeded / len(records), 4) if records else 0,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "result_file_id": str(result_file.id),
    }
