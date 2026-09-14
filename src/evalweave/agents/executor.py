from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from copy import deepcopy
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlmodel import Session

from evalweave.agents.inspection import load_source_rows
from evalweave.agents.model_config import decrypt_secret_payload
from evalweave.core.config import TargetAuthFlowConfig, get_settings
from evalweave.db.models import AgentJob, FileObject
from evalweave.storage import LocalFileStorage


class TargetApplicationError(ValueError):
    def __init__(self, message: str, response: httpx.Response):
        super().__init__(message)
        self.response = response


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


def normalize_target_body(value: Any) -> Any:
    """Turn JSON pasted as text into the structured body expected by httpx."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, (dict, list)) else value


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
    if "headers" in target:
        raise ValueError("Configure target headers in application.yaml, not in job input")
    headers = dict(get_settings().agent.target_headers)
    credentials = target.get("credentials")
    if isinstance(credentials, str) and credentials:
        task_credentials = decrypt_secret_payload(credentials)
        task_headers = task_credentials.get("headers")
        if isinstance(task_headers, dict):
            headers.update({str(key): str(value) for key, value in task_headers.items()})
    return url, headers


def value_at_path(value: Any, path: str) -> Any:
    current_values = [value]
    used_wildcard = False
    for raw_part in path.split("."):
        if not raw_part:
            continue
        match = re.fullmatch(r"([^\[]+)(?:\[(\*|\d+)\])?", raw_part)
        if match is None:
            raise KeyError(raw_part)
        key, selector = match.groups()
        next_values: list[Any] = []
        for current in current_values:
            selected = current[int(key)] if isinstance(current, list) else current[key]
            if selector == "*":
                if not isinstance(selected, list):
                    raise TypeError(f"Path segment {raw_part} is not a list")
                next_values.extend(selected)
                used_wildcard = True
            elif selector is not None:
                next_values.append(selected[int(selector)])
            else:
                next_values.append(selected)
        current_values = next_values
    if used_wildcard:
        if current_values and all(isinstance(item, str) for item in current_values):
            return "".join(current_values)
        return current_values
    return current_values[0] if current_values else None


def authenticate_target(
    client: httpx.Client,
    target_url: str,
    headers: dict[str, str],
    credentials: str | None = None,
) -> dict[str, Any]:
    target_host = (urlparse(target_url).hostname or "").lower()
    flow = None
    source = "configured"
    if credentials:
        task_auth = decrypt_secret_payload(credentials).get("auth")
        if isinstance(task_auth, dict):
            flow = TargetAuthFlowConfig.model_validate(task_auth)
            source = "task"
    if flow is None:
        flow = get_settings().agent.target_auth_flows.get(target_host)
    if flow is None:
        return {"used": False}
    try:
        response = client.post(flow.login_url, json=flow.body)
        response.raise_for_status()
    except Exception as error:
        raise ValueError(f"目标接口登录工具执行失败：{error}") from error
    if flow.token_path:
        try:
            token = value_at_path(response.json(), flow.token_path)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError(
                f"登录成功，但未能从响应路径 {flow.token_path} 读取访问令牌"
            ) from error
        if not isinstance(token, str) or not token:
            raise ValueError(f"登录响应路径 {flow.token_path} 未返回有效访问令牌")
        headers[flow.header_name] = f"{flow.header_prefix}{token}"
    return {
        "used": True,
        "source": source,
        "login_url": flow.login_url,
        "header_name": flow.header_name if flow.token_path else None,
        "cookie_names": sorted(client.cookies.keys()),
    }


def parse_target_response(response: Any, response_path: str) -> tuple[Any, str]:
    headers = getattr(response, "headers", {})
    content_type = str(headers.get("content-type", "")).lower()
    response_text = str(getattr(response, "text", ""))
    is_streaming = "text/event-stream" in content_type or response_text.lstrip().startswith("data:")
    if is_streaming:
        events: list[Any] = []
        for line in response_text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                events.append(payload)
        output: Any = {"events": events}
        response_mode = "streaming"
    else:
        output = response.json()
        response_mode = "json"
    if response_path:
        output = value_at_path(output, response_path)
    return output, response_mode


def isolate_session_ids(value: Any, session_id: str | None = None) -> Any:
    """Give each independent evaluation case a fresh conversation session."""
    isolated_session_id = session_id or uuid4().hex
    if isinstance(value, dict):
        return {
            key: isolated_session_id
            if re.sub(r"[_-]", "", key).lower() == "sessionid"
            else isolate_session_ids(item, isolated_session_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [isolate_session_ids(item, isolated_session_id) for item in value]
    return value


def detect_application_error(response: httpx.Response) -> str | None:
    """Detect common JSON error envelopes returned with a successful HTTP status."""
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    reason: str | None = None
    if payload.get("success") is False:
        reason = "success=false"
    code = payload.get("code")
    numeric_code = (
        float(code)
        if isinstance(code, str) and re.fullmatch(r"\d+(?:\.\d+)?", code.strip())
        else code
    )
    if (
        isinstance(numeric_code, int | float)
        and not isinstance(numeric_code, bool)
        and numeric_code >= 400
    ):
        reason = f"code={numeric_code:g}"
    status_code = payload.get("status_code", payload.get("statusCode"))
    if (
        isinstance(status_code, int | float)
        and not isinstance(status_code, bool)
        and status_code >= 400
    ):
        reason = f"status_code={status_code:g}"
    status = str(payload.get("status", "")).strip().lower()
    if status in {"error", "failed", "failure"}:
        reason = f"status={status}"
    if reason is None:
        return None

    message = str(payload.get("msg") or payload.get("message") or payload.get("error") or "")
    return f"{reason}（{message[:500]}）" if message else reason


def _markdown_value(value: Any, limit: int = 180) -> str:
    if value is None:
        return "—"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _evaluation_markdown_report(
    records: list[dict[str, Any]], title: str, goal: str
) -> str:
    total = len(records)
    succeeded = sum(record.get("status") == "completed" for record in records)
    failed = total - succeeded
    latencies = [
        float(record["latency_ms"])
        for record in records
        if isinstance(record.get("latency_ms"), int | float)
    ]
    scores = [
        float(evaluation["overall_score"])
        for record in records
        if isinstance((evaluation := record.get("evaluation")), dict)
        and isinstance(evaluation.get("overall_score"), int | float)
    ]
    success_rate = succeeded / total * 100 if total else 0
    average_latency = sum(latencies) / len(latencies) if latencies else None
    average_score = sum(scores) / len(scores) if scores else None
    summary_parts = [
        f"本次共执行 **{total}** 条评测，成功 **{succeeded}** 条，"
        f"失败 **{failed}** 条，成功率为 **{success_rate:.1f}%**。"
    ]
    if average_latency is not None:
        summary_parts.append(f"成功请求的平均响应耗时为 **{average_latency:.2f} ms**。")
    if average_score is not None:
        summary_parts.append(f"已评分样本的平均综合评分为 **{average_score:.2f}/10**。")

    metric_rows = [
        ("评测总数", str(total), "本次纳入统计的样本数量"),
        ("成功率", f"{success_rate:.1f}%", "接口成功返回的样本占比"),
        ("失败数量", str(failed), "请求失败或执行异常的样本数量"),
    ]
    if average_latency is not None:
        metric_rows.append(("平均响应耗时", f"{average_latency:.2f} ms", "全量有效耗时均值"))
    if average_score is not None:
        metric_rows.append(("平均综合评分", f"{average_score:.2f}/10", "已完成模型评分的样本均值"))

    sample_lines = [
        "| 序号 | 状态 | 耗时 | 输入摘要 | 输出摘要 | 评分与说明 |",
        "| ---: | --- | ---: | --- | --- | --- |",
    ]
    for record in records[:5]:
        evaluation = record.get("evaluation")
        score_reason = "—"
        if isinstance(evaluation, dict):
            score = evaluation.get("overall_score")
            reason = evaluation.get("reason")
            score_reason = " / ".join(
                part for part in (f"{score}/10" if score is not None else "", str(reason or ""))
                if part
            ) or "—"
        output = record.get("output", record.get("error"))
        sample_lines.append(
            "| "
            + " | ".join(
                [
                    str(int(record.get("case_index", 0)) + 1),
                    _markdown_value(record.get("status")),
                    (
                        f"{record.get('latency_ms')} ms"
                        if record.get("latency_ms") is not None
                        else "—"
                    ),
                    _markdown_value(record.get("input")),
                    _markdown_value(output),
                    _markdown_value(score_reason),
                ]
            )
            + " |"
        )

    interpretation = (
        "全部样本均已成功完成，当前结果未发现请求级故障。"
        if failed == 0
        else f"共有 {failed} 条样本未成功，应优先核查失败记录中的错误信息和请求参数。"
    )
    latency_note = (
        f"响应耗时范围为 {min(latencies):.2f}–{max(latencies):.2f} ms；"
        "均值适合观察整体表现，极值可帮助定位慢请求。"
        if latencies
        else "本次结果没有可用于分析的响应耗时数据。"
    )
    score_note = (
        f"综合评分均值为 {average_score:.2f}/10，建议结合下方样例中的评分理由判断具体改进方向。"
        if average_score is not None
        else "本次结果未包含模型评分，结论主要依据执行状态和响应指标。"
    )
    goal_block = f"> **评测目标：** {goal.strip()}\n\n" if goal.strip() else ""
    return "\n".join(
        [
            f"# {title.strip() or 'AI 评测报告'}",
            "",
            goal_block.rstrip(),
            "## 结论摘要",
            "",
            " ".join(summary_parts),
            "",
            "## 核心指标",
            "",
            "| 指标 | 结果 | 说明 |",
            "| --- | ---: | --- |",
            *[f"| {name} | {value} | {note} |" for name, value, note in metric_rows],
            "",
            "## 代表性结果",
            "",
            *sample_lines,
            "",
            f"> 上表展示前 {min(total, 5)} 条代表性结果，用于快速阅读关键输入、输出与评分依据。",
            "",
            "## 结果解读",
            "",
            f"- **稳定性：** {interpretation}",
            f"- **响应速度：** {latency_note}",
            f"- **回复质量：** {score_note}",
            "",
            "## 建议",
            "",
            "- 优先复核失败样本和评分偏低样本，确认问题来自接口、提示词还是数据本身。",
            "- 对耗时明显高于平均值的样本单独复测，避免平均值掩盖长尾延迟。",
            "- 如需逐行筛选、排序或二次计算，建议同时选择 Excel 作为明细分析格式。",
            "",
        ]
    )


def serialize_results(
    records: list[dict[str, Any]],
    output_format: str,
    *,
    title: str = "AI 评测报告",
    goal: str = "",
) -> tuple[bytes, str, str]:
    if output_format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "评测结果"
        is_local_data = bool(records) and all(
            "output" not in record and "error" not in record for record in records
        )
        if is_local_data:
            fields = list(
                dict.fromkeys(str(key) for record in records for key in (record.get("input") or {}))
            )
            sheet.append(["序号", "状态", *fields])
            for record in records:
                row = record.get("input") or {}
                sheet.append(
                    [
                        record.get("case_index"),
                        "已完成" if record.get("status") == "completed" else record.get("status"),
                        *[row.get(key) for key in fields],
                    ]
                )
            sheet.column_dimensions["A"].width = 10
            sheet.column_dimensions["B"].width = 14
            for index in range(3, len(fields) + 3):
                sheet.column_dimensions[get_column_letter(index)].width = 18
        else:
            dimension_keys: list[str] = []
            dimension_labels: dict[str, str] = {}
            for record in records:
                evaluation = record.get("evaluation") or {}
                for dimension in evaluation.get("dimensions") or []:
                    if not isinstance(dimension, dict):
                        continue
                    key = str(dimension.get("key", ""))
                    if key and key not in dimension_labels:
                        dimension_keys.append(key)
                        dimension_labels[key] = str(dimension.get("label") or key)
            sheet.append(
                [
                    "序号",
                    "状态",
                    "总耗时（毫秒）",
                    "首包耗时（毫秒）",
                    *[f"{dimension_labels[key]}（1-10分）" for key in dimension_keys],
                    "综合评分",
                    "是否通过",
                    "评分理由",
                    "输入",
                    "输出",
                    "错误",
                ]
            )
            for record in records:
                evaluation = record.get("evaluation") or {}
                dimensions = {
                    str(item.get("key")): item
                    for item in evaluation.get("dimensions") or []
                    if isinstance(item, dict)
                }
                sheet.append(
                    [
                        record.get("case_index"),
                        record.get("status"),
                        record.get("latency_ms"),
                        record.get("ttfb_ms"),
                        *[
                            (dimensions.get(key) or {}).get("score")
                            for key in dimension_keys
                        ],
                        evaluation.get("overall_score"),
                        evaluation.get("passed"),
                        evaluation.get("reason"),
                        json.dumps(record.get("input"), ensure_ascii=False),
                        json.dumps(record.get("output"), ensure_ascii=False)
                        if "output" in record
                        else None,
                        record.get("error"),
                    ]
                )
            widths = [10, 14, 18, 18, *([18] * len(dimension_keys)), 14, 12, 42, 45, 45, 40]
            for index, width in enumerate(widths, start=1):
                sheet.column_dimensions[get_column_letter(index)].width = width
        header_fill = PatternFill("solid", fgColor="2F6FED")
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        sheet.row_dimensions[1].height = 24
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        return (
            stream.getvalue(),
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if output_format == "markdown":
        report = _evaluation_markdown_report(records, title, goal)
        return report.encode(), "md", "text/markdown; charset=utf-8"
    if output_format == "text":
        blocks = []
        for record in records:
            output = record.get("output", record.get("error", ""))
            blocks.append(
                "\n".join(
                    [
                        (
                            f"第 {int(record.get('case_index', 0)) + 1} 条 · "
                            f"{record.get('status', '')}"
                        ),
                        f"耗时：{record.get('latency_ms', '')} 毫秒",
                        f"输入：{json.dumps(record.get('input'), ensure_ascii=False)}",
                        f"输出：{json.dumps(output, ensure_ascii=False)}",
                    ]
                )
            )
        return ("\n\n".join(blocks) + "\n").encode(), "txt", "text/plain; charset=utf-8"
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    return content.encode(), "jsonl", "application/x-ndjson"


def store_results(session: Session, job: AgentJob, records: list[dict[str, Any]]) -> FileObject:
    output_format = str(job.input_config.get("output_format", "xlsx"))
    content, extension, content_type = serialize_results(
        records, output_format, title=job.title, goal=job.goal
    )
    file_id = uuid4()
    storage_key = f"projects/{job.project_id}/evaluation_result/{file_id.hex}"
    stored = LocalFileStorage(get_settings().storage.local_directory).put(
        storage_key, BytesIO(content), max_bytes=100 * 1024 * 1024
    )
    file_object = FileObject(
        id=file_id,
        project_id=job.project_id,
        created_by=job.created_by,
        category="evaluation_result",
        original_name=f"agent-job-{job.id}-results.{extension}",
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    session.add(file_object)
    session.commit()
    session.refresh(file_object)
    return file_object


def _tabular_cell(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def _tabular_markdown_report(
    rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    title: str,
    goal: str,
) -> str:
    headers = list(dict.fromkeys(str(key) for row in rows for key in row))
    preferred_terms = (
        "query", "问题", "answer", "回复", "score", "评分",
        "reason", "原因", "latency", "耗时", "status", "状态",
    )
    preview_headers = [
        header
        for header in headers
        if any(term in header.lower() for term in preferred_terms)
    ][:6]
    preview_headers.extend(
        header for header in headers if header not in preview_headers
    )
    preview_headers = preview_headers[:6]

    summary_parts = [
        f"本次任务共处理 **{len(rows)}** 行数据，最终结果包含 **{len(headers)}** 个字段。"
    ]
    target_summaries = [item for item in summaries if item.get("target")]
    if target_summaries:
        succeeded = sum(int(item.get("succeeded") or 0) for item in target_summaries)
        total = sum(int(item.get("total") or 0) for item in target_summaries)
        summary_parts.append(
            f"接口调用累计成功 **{succeeded}/{total}** 次。"
        )

    metric_lines = [
        "| 对象 | 成功/总数 | 平均耗时 | P95 耗时 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in target_summaries:
        metric_lines.append(
            f"| {_markdown_value(item.get('target'), 60)} | "
            f"{item.get('succeeded', '—')}/{item.get('total', '—')} | "
            f"{item.get('average_latency_ms', '—')} ms | "
            f"{item.get('p95_latency_ms', '—')} ms |"
        )
    if not target_summaries:
        metric_lines.append(f"| 数据处理 | {len(rows)}/{len(rows)} | — | — |")

    if preview_headers:
        preview_lines = [
            "| " + " | ".join(preview_headers) + " |",
            "| " + " | ".join("---" for _ in preview_headers) + " |",
            *[
                "| "
                + " | ".join(_markdown_value(row.get(header)) for header in preview_headers)
                + " |"
                for row in rows[:5]
            ],
        ]
    else:
        preview_lines = ["本次任务没有生成可展示的数据行。"]

    interpretation: list[str] = []
    if target_summaries:
        valid_latency = [
            item
            for item in target_summaries
            if isinstance(item.get("average_latency_ms"), int | float)
        ]
        if valid_latency:
            fastest = min(valid_latency, key=lambda item: float(item["average_latency_ms"]))
            interpretation.append(
                f"**速度表现：** {fastest.get('target')} 的平均耗时最低，"
                f"为 {fastest.get('average_latency_ms')} ms；仍应结合 P95 判断长尾波动。"
            )
        failed = sum(
            int(item.get("failed") or 0) for item in target_summaries
        )
        interpretation.append(
            "**调用质量：** 所有接口调用均成功完成。"
            if failed == 0
            else f"**调用质量：** 共发现 {failed} 次失败调用，应结合错误列逐条排查。"
        )
    aggregate_summaries = [
        item for item in summaries if item.get("type") == "aggregate"
    ]
    if aggregate_summaries:
        fields = [
            key
            for item in aggregate_summaries
            for key in item
            if key.endswith("_average")
        ]
        if fields:
            interpretation.append(
                "**汇总计算：** 已对数值字段生成平均值，关键结果可结合数据预览和原始字段理解。"
            )
    if not interpretation:
        interpretation.append(
            "**数据处理：** 已按任务要求完成字段转换或内容补充，可通过下方预览快速核对结果结构。"
        )

    goal_block = f"> **任务目标：** {goal.strip()}\n\n" if goal.strip() else ""
    return "\n".join(
        [
            f"# {title.strip() or '数据评测报告'}",
            "",
            goal_block.rstrip(),
            "## 结论摘要",
            "",
            " ".join(summary_parts),
            "",
            "## 核心指标",
            "",
            *metric_lines,
            "",
            "## 数据预览",
            "",
            *preview_lines,
            "",
            (
                f"> 表格仅展示前 {min(len(rows), 5)} 行及最多 6 个关键字段，"
                "避免报告退化为难以阅读的数据堆积。"
            ),
            "",
            "## 结果解读",
            "",
            *[f"- {item}" for item in interpretation],
            "",
            "## 建议",
            "",
            "- 抽查新增字段、模型评分和转换结果，确认其与原始数据语义一致。",
            "- 对失败调用或明显慢于平均值的记录单独复测并记录原因。",
            "- 若需要查看、筛选全部逐行数据，Excel 或 JSONL 更适合作为明细交付格式。",
            "",
        ]
    )


def store_tabular_result(
    session: Session,
    job: AgentJob,
    rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> FileObject | None:
    output_format = str(job.input_config.get("output_format", "xlsx"))
    if output_format == "text":
        return None
    headers = list(dict.fromkeys(str(key) for row in rows for key in row))
    if output_format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "结果"
        sheet.append(headers)
        for row in rows:
            sheet.append([_tabular_cell(row.get(header)) for header in headers])
        header_fill = PatternFill("solid", fgColor="2F6FED")
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for index, header in enumerate(headers, start=1):
            width = max(12, min(48, len(header) * 2 + 6))
            sheet.column_dimensions[get_column_letter(index)].width = width
        if summaries:
            summary_sheet = workbook.create_sheet("汇总")
            summary_headers = list(
                dict.fromkeys(str(key) for item in summaries for key in item)
            )
            summary_sheet.append(summary_headers)
            for item in summaries:
                summary_sheet.append(
                    [_tabular_cell(item.get(header)) for header in summary_headers]
                )
            for cell in summary_sheet[1]:
                cell.fill = header_fill
                cell.font = Font(color="FFFFFF", bold=True)
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        content = stream.getvalue()
        extension = "xlsx"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif output_format == "markdown":
        content = _tabular_markdown_report(
            rows, summaries, job.title, job.goal
        ).encode()
        extension = "md"
        content_type = "text/markdown; charset=utf-8"
    else:
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
        extension = "jsonl"
        content_type = "application/x-ndjson"
    file_id = uuid4()
    storage_key = f"projects/{job.project_id}/evaluation_result/{file_id.hex}"
    stored = LocalFileStorage(get_settings().storage.local_directory).put(
        storage_key, BytesIO(content), max_bytes=100 * 1024 * 1024
    )
    file_object = FileObject(
        id=file_id,
        project_id=job.project_id,
        created_by=job.created_by,
        category="evaluation_result",
        original_name=f"agent-job-{job.id}-results.{extension}",
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    session.add(file_object)
    session.commit()
    session.refresh(file_object)
    return file_object


def execute_data_program(
    session: Session,
    job: AgentJob,
    model_mapper: Callable[
        [str, list[dict[str, Any]], list[dict[str, Any]], int], list[dict[str, Any]]
    ],
) -> dict[str, Any]:
    if not job.source_file_id:
        raise ValueError("Data programs require an uploaded source file")
    source = session.get(FileObject, job.source_file_id)
    if source is None or source.project_id != job.project_id:
        raise ValueError("Source file does not belong to this project")
    path = LocalFileStorage(get_settings().storage.local_directory).path_for(source.storage_key)
    max_rows = min(int(job.input_config.get("max_cases", 100)), 1000)
    header_row, data_start_row, field_names = source_parsing_options(job.eval_spec)
    rows = load_source_rows(
        path,
        source.original_name,
        max_rows,
        header_row=header_row,
        data_start_row=data_start_row,
        field_names=field_names,
    )
    program = job.eval_spec.get("data_program") or {}
    steps = program.get("steps") if isinstance(program, dict) else None
    if not isinstance(steps, list):
        steps = [{"type": "convert"}]
    if len(steps) > 20:
        raise ValueError("Data program exceeds the 20-step limit")
    summaries: list[dict[str, Any]] = []
    configured_targets = job.input_config.get("targets")
    targets = configured_targets if isinstance(configured_targets, list) else []
    source_spec = job.eval_spec.get("source")
    source_mapping = (
        source_spec.get("field_mappings") or source_spec.get("column_mapping")
        if isinstance(source_spec, dict)
        else None
    )
    field_mapping = (
        program.get("field_mapping") or program.get("field_mappings") or source_mapping
        if isinstance(program, dict)
        else source_mapping
    )
    mapped_fields = field_mapping if isinstance(field_mapping, dict) else {}

    def model_input_row(row: dict[str, Any], fields: list[Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for raw_field in fields:
            field = str(raw_field)
            if field in row:
                resolved[field] = row[field]
                continue
            source_field = next(
                (
                    str(source_name)
                    for source_name, mapped_name in mapped_fields.items()
                    if str(mapped_name) == field
                ),
                None,
            )
            resolved[field] = row.get(source_field) if source_field else None
        return resolved

    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Invalid data program step {step_index + 1}")
        step_type = str(
            step.get("type")
            or step.get("primitive")
            or step.get("action")
            or step.get("tool")
            or ""
        )
        step_type = {
            "format_convert": "convert",
            "data_transform": "model_map",
            "llm_map": "model_map",
            "target_call": "http_map",
        }.get(step_type, step_type)
        if step_type == "convert":
            continue
        if step_type == "model_map":
            instruction = str(step.get("instruction", "")).strip()
            output_columns = step.get("output_columns")
            if not isinstance(output_columns, list):
                raise ValueError("model_map requires output_columns")
            input_fields = step.get("input_fields")
            model_rows = rows
            if isinstance(input_fields, list) and input_fields:
                model_rows = [model_input_row(row, input_fields) for row in rows]
            additions = model_mapper(instruction, model_rows, output_columns, step_index)
            if len(additions) != len(rows):
                raise ValueError("model_map returned a different row count")
            for row, values in zip(rows, additions, strict=True):
                row.update(values)
            continue
        if step_type == "http_map":
            target_index = int(step.get("target_index", 0))
            if target_index < 0 or target_index >= len(targets):
                raise ValueError(f"http_map target_index {target_index} is not configured")
            target = targets[target_index]
            if not isinstance(target, dict):
                raise ValueError("Configured HTTP target must be an object")
            url, headers = validate_target(target)
            timeout = float(
                target.get("timeout_seconds", get_settings().evaluation.default_timeout_seconds)
            )
            answer_column = str(
                step.get("answer_column")
                or target.get("answer_column")
                or f"answer{target_index + 1}"
            )
            latency_column = str(
                step.get("latency_column")
                or target.get("latency_column")
                or f"latency{target_index + 1}_ms"
            )
            ttfb_column = str(
                step.get("ttfb_column")
                or target.get("ttfb_column")
                or f"ttfb{target_index + 1}_ms"
            )
            latencies: list[float] = []
            ttfbs: list[float] = []
            succeeded = 0
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                authenticate_target(client, url, headers, target.get("credentials"))
                for row_index, row in enumerate(rows):
                    record = execute_target_case(client, url, headers, target, row, row_index)
                    if record["status"] == "completed":
                        row[answer_column] = record.get("output")
                        row[latency_column] = record.get("latency_ms")
                        row[ttfb_column] = record.get("ttfb_ms")
                        latencies.append(float(record["latency_ms"]))
                        if record.get("ttfb_ms") is not None:
                            ttfbs.append(float(record["ttfb_ms"]))
                        succeeded += 1
                    else:
                        row[answer_column] = None
                        row[latency_column] = record.get("latency_ms")
                        row[ttfb_column] = record.get("ttfb_ms")
                        row[f"{answer_column}_error"] = record.get("error")
            summaries.append(
                {
                    "target": str(target.get("name") or f"接口{target_index + 1}"),
                    "url": url,
                    "total": len(rows),
                    "succeeded": succeeded,
                    "failed": len(rows) - succeeded,
                    "average_latency_ms": (
                        round(sum(latencies) / len(latencies), 2) if latencies else None
                    ),
                    "average_ttfb_ms": round(sum(ttfbs) / len(ttfbs), 2) if ttfbs else None,
                    "p50_latency_ms": percentile(latencies, 0.5),
                    "p95_latency_ms": percentile(latencies, 0.95),
                }
            )
            continue
        if step_type == "aggregate":
            numeric_fields = list(
                dict.fromkeys(
                    key
                    for row in rows
                    for key, value in row.items()
                    if isinstance(value, int | float) and not isinstance(value, bool)
                )
            )
            aggregate: dict[str, Any] = {
                "type": "aggregate",
                "instruction": str(step.get("instruction", "")),
                "total_rows": len(rows),
            }
            for field in numeric_fields:
                values = [
                    float(row[field])
                    for row in rows
                    if isinstance(row.get(field), int | float)
                    and not isinstance(row.get(field), bool)
                ]
                if values:
                    aggregate[f"{field}_average"] = round(sum(values) / len(values), 2)
            status_field = next(
                (
                    field
                    for field in ("status", "call_status", "状态")
                    if any(field in row for row in rows)
                ),
                None,
            )
            if status_field:
                successful = sum(
                    str(row.get(status_field, "")).lower()
                    in {"completed", "success", "succeeded", "ok", "已完成", "成功"}
                    for row in rows
                )
                aggregate["successful_rows"] = successful
                aggregate["success_rate"] = (
                    round(successful / len(rows), 4) if rows else 0
                )
            summaries.append(aggregate)
            continue
        if step_type == "summarize":
            # The workflow has a dedicated model-backed summarize step after data execution.
            continue
        raise ValueError(f"Unsupported data program primitive: {step_type}")

    result_file = store_tabular_result(session, job, rows, summaries)
    job.result_file_id = result_file.id if result_file else None
    session.add(job)
    session.commit()
    return {
        "executed": True,
        "mode": "data_program",
        "total_rows": len(rows),
        "program_steps": len(steps),
        "rows": rows,
        "summaries": summaries,
        "result_file_id": str(result_file.id) if result_file else None,
    }


def source_parsing_options(eval_spec: dict[str, Any]) -> tuple[int, int | None, list[str] | None]:
    source = eval_spec.get("source", {})
    if not isinstance(source, dict):
        source = {}
    header_row = max(int(source.get("header_row", 1)), 1)
    data_start_row = source.get("data_start_row")
    mapping = source.get("field_mappings") or source.get("column_mapping")
    data_program = eval_spec.get("data_program")
    if isinstance(data_program, dict):
        header_row = max(int(data_program.get("header_row", header_row)), 1)
        data_start_row = data_program.get("data_start_row", data_start_row)

    plan = eval_spec.get("plan")
    if isinstance(plan, list):
        normalize_step = next(
            (
                item
                for item in plan
                if isinstance(item, dict) and item.get("operation") == "normalize"
            ),
            None,
        )
        if isinstance(normalize_step, dict):
            parameters = normalize_step.get("parameters", {})
            parsing = parameters.get("parsing", {}) if isinstance(parameters, dict) else {}
            if isinstance(parsing, dict):
                header_row = max(int(parsing.get("header_row", header_row)), 1)
                data_start_row = parsing.get("data_start_row", data_start_row)
                mapping = parsing.get("column_mapping", mapping)

    field_names = None
    if isinstance(mapping, dict) and mapping:
        indexed: dict[int, str] = {}
        for key, value in mapping.items():
            key_text = str(key)
            value_text = str(value)
            if key_text.isdigit():
                indexed[int(key_text)] = value_text
                continue
            key_match = re.search(r"column[_\s-]*(\d+)", key_text, re.IGNORECASE)
            if key_match:
                indexed[int(key_match.group(1))] = value_text
                continue
            value_match = re.search(r"column[_\s-]*(\d+)", value_text, re.IGNORECASE)
            if value_match:
                indexed[int(value_match.group(1))] = key_text
        if indexed:
            field_names = [
                indexed.get(index, f"column_{index}") for index in range(1, max(indexed) + 1)
            ]
    return header_row, int(data_start_row) if data_start_row else None, field_names


def execute_local_source(session: Session, job: AgentJob) -> dict[str, Any]:
    if not job.source_file_id:
        raise ValueError("本地数据评测需要上传数据文件")
    source = session.get(FileObject, job.source_file_id)
    if source is None or source.project_id != job.project_id:
        raise ValueError("数据文件不属于当前项目")
    max_cases = int(job.input_config.get("max_cases", 100))
    header_row, data_start_row, field_names = source_parsing_options(job.eval_spec)
    path = LocalFileStorage(get_settings().storage.local_directory).path_for(source.storage_key)
    rows = load_source_rows(
        path,
        source.original_name,
        max_cases,
        header_row=header_row,
        data_start_row=data_start_row,
        field_names=field_names,
    )
    records = [
        {"case_index": index, "status": "completed", "input": row} for index, row in enumerate(rows)
    ]
    result_file_id = None
    if job.input_config.get("output_format") != "text":
        result_file = store_results(session, job, records)
        result_file_id = result_file.id
    job.result_file_id = result_file_id
    session.add(job)
    session.commit()
    return {
        "executed": True,
        "mode": "local_data",
        "total_rows": len(rows),
        "rows": rows,
        "result_file_id": str(result_file_id) if result_file_id else None,
    }


def prepare_http_target(
    session: Session, job: AgentJob
) -> tuple[dict[str, Any], str, dict[str, str], list[dict[str, Any]], str, float]:
    target = job.input_config.get("target")
    if not isinstance(target, dict) or not target:
        raise ValueError("No target connector was configured")
    normalized_body = normalize_target_body(target.get("body", "{{row}}"))
    if normalized_body is not target.get("body"):
        target = {**target, "body": normalized_body}
        job.input_config = {**job.input_config, "target": target}
        session.add(job)
        session.commit()
    url, headers = validate_target(target)
    max_cases = int(job.input_config.get("max_cases", 100))
    if not 1 <= max_cases <= 10_000:
        raise ValueError("max_cases must be between 1 and 10000")
    if job.source_file_id:
        source = session.get(FileObject, job.source_file_id)
        if source is None or source.project_id != job.project_id:
            raise ValueError("Source file does not belong to this project")
        path = LocalFileStorage(get_settings().storage.local_directory).path_for(source.storage_key)
        rows = load_source_rows(path, source.original_name, max_cases)
        case_source = "uploaded_file"
    else:
        generated_cases = job.eval_spec.get("generated_cases")
        if not isinstance(generated_cases, list) or not generated_cases:
            raise ValueError("HTTP target execution requires uploaded or AI-generated test cases")
        if not all(
            isinstance(case, dict) and isinstance(case.get("input"), dict) and case["input"]
            for case in generated_cases
        ):
            raise ValueError("AI-generated test cases must contain non-empty input objects")
        rows = generated_cases[:max_cases]
        case_source = "ai_generated"
    timeout = float(
        target.get("timeout_seconds", get_settings().evaluation.default_timeout_seconds)
    )
    return target, url, headers, rows, case_source, timeout


def execute_target_case(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    target: dict[str, Any],
    row: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    body_template = target.get("body", "{{row}}")
    response_path = str(target.get("response_path", "")).strip()
    request_values = row.get("input") if isinstance(row.get("input"), dict) else row
    request_body = isolate_session_ids(render_template(body_template, request_values))
    recorded_input = deepcopy(row)
    if body_template == "{{row}}" and isinstance(recorded_input.get("input"), dict):
        recorded_input["input"] = request_body
    started = time.perf_counter()
    ttfb_ms: float | None = None
    try:
        if hasattr(client, "stream"):
            with client.stream("POST", url, headers=headers, json=request_body) as streamed:
                streamed.raise_for_status()
                chunks: list[bytes] = []
                for chunk in streamed.iter_bytes():
                    if chunk and ttfb_ms is None:
                        ttfb_ms = (time.perf_counter() - started) * 1000
                    chunks.append(chunk)
                buffered_headers = {
                    key: value
                    for key, value in streamed.headers.items()
                    if key.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                response = httpx.Response(
                    status_code=streamed.status_code,
                    headers=buffered_headers,
                    content=b"".join(chunks),
                    request=httpx.Request("POST", url),
                )
                if ttfb_ms is None:
                    ttfb_ms = (time.perf_counter() - started) * 1000
        else:
            response = client.post(url, headers=headers, json=request_body)
            ttfb_ms = (time.perf_counter() - started) * 1000
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        application_error = detect_application_error(response)
        if application_error:
            raise TargetApplicationError(
                f"目标接口返回业务错误：{application_error}", response
            )
        output, response_mode = parse_target_response(response, response_path)
        return {
            "case_index": index,
            "status": "completed",
            "status_code": getattr(response, "status_code", 200),
            "latency_ms": round(elapsed_ms, 2),
            "ttfb_ms": round(ttfb_ms, 2),
            "input": recorded_input,
            "output": output,
            "response_mode": response_mode,
        }
    except Exception as error:
        elapsed_ms = (time.perf_counter() - started) * 1000
        response = getattr(error, "response", None)
        response_text = str(getattr(response, "text", "")).strip()
        return {
            "case_index": index,
            "status": "failed",
            "latency_ms": round(elapsed_ms, 2),
            "ttfb_ms": round(ttfb_ms, 2) if ttfb_ms is not None else None,
            "input": recorded_input,
            "error": str(error)[:2000],
            "status_code": getattr(response, "status_code", None),
            "response_body": response_text[:2000] if response_text else None,
        }


def validate_http_target(session: Session, job: AgentJob) -> dict[str, Any]:
    target, url, headers, rows, case_source, timeout = prepare_http_target(session, job)
    probe_count = min(3, len(rows))
    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout) as client:
        authentication = authenticate_target(client, url, headers, target.get("credentials"))
        for index, row in enumerate(rows[:probe_count]):
            record = execute_target_case(client, url, headers, target, row, index)
            records.append(record)
            if record["status"] == "failed":
                break
    failures = [record for record in records if record["status"] == "failed"]
    if failures:
        failure = failures[0]
        error = str(failure.get("error", "Unknown target error"))
        response_body = str(failure.get("response_body") or "").strip()
        if response_body:
            error = f"{error}; response body: {response_body}"
        if "401" in error or "Unauthorized" in error:
            raise ValueError(
                "目标接口预检失败：HTTP 401 未授权。"
                "请通过 Agent 提供本任务的请求头或登录流程后重试。"
            )
        raise ValueError(f"目标接口预检在第 {len(records)} 条失败：{error}")
    response_modes = sorted({str(record["response_mode"]) for record in records})
    expected_streaming = target.get("expected_streaming")
    if isinstance(expected_streaming, bool):
        actual_streaming = "streaming" in response_modes
        if actual_streaming != expected_streaming:
            expected_label = "流式 SSE" if expected_streaming else "普通 JSON"
            actual_label = "流式 SSE" if actual_streaming else "普通 JSON"
            raise ValueError(
                f"目标接口响应方式与描述不一致：预期 {expected_label}，实际 {actual_label}"
            )
    return {
        "validated": True,
        "case_source": case_source,
        "probe_count": probe_count,
        "response_modes": response_modes,
        "authentication": authentication,
        "records": records,
    }


def execute_http_target(
    session: Session,
    job: AgentJob,
    preflight: dict[str, Any] | None = None,
    evaluate_records: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    target, url, headers, rows, case_source, timeout = prepare_http_target(session, job)
    preflight_records = preflight.get("records", []) if isinstance(preflight, dict) else []
    records = [dict(record) for record in preflight_records]
    with httpx.Client(timeout=timeout) as client:
        authenticate_target(client, url, headers, target.get("credentials"))
        for index, row in enumerate(rows[len(records) :], start=len(records)):
            records.append(execute_target_case(client, url, headers, target, row, index))
    if evaluate_records is not None:
        evaluations = {
            item["case_index"]: item for item in evaluate_records(records)
        }
        for record in records:
            if record.get("case_index") in evaluations:
                record["evaluation"] = evaluations[record["case_index"]]
    latencies = [float(record["latency_ms"]) for record in records]
    result_file_id = None
    if job.input_config.get("output_format") != "text":
        result_file = store_results(session, job, records)
        result_file_id = result_file.id
    job.result_file_id = result_file_id
    session.add(job)
    session.commit()
    succeeded = sum(record["status"] == "completed" for record in records)
    scored = [
        record["evaluation"]
        for record in records
        if isinstance(record.get("evaluation"), dict)
    ]
    dimension_scores: dict[str, dict[str, Any]] = {}
    for evaluation in scored:
        for dimension in evaluation.get("dimensions") or []:
            if not isinstance(dimension, dict) or not dimension.get("key"):
                continue
            key = str(dimension["key"])
            bucket = dimension_scores.setdefault(
                key,
                {"label": str(dimension.get("label") or key), "scores": []},
            )
            bucket["scores"].append(float(dimension["score"]))
    average_dimension_scores = {
        key: {
            "label": value["label"],
            "score": round(sum(value["scores"]) / len(value["scores"]), 2),
        }
        for key, value in dimension_scores.items()
        if value["scores"]
    }
    ttfb_values = [
        float(record["ttfb_ms"])
        for record in records
        if record.get("ttfb_ms") is not None
    ]
    return {
        "executed": True,
        "case_source": case_source,
        "preflight_cases": len(preflight_records),
        "response_modes": sorted(
            {str(record["response_mode"]) for record in records if record.get("response_mode")}
        ),
        "total_cases": len(records),
        "succeeded": succeeded,
        "failed": len(records) - succeeded,
        "success_rate": round(succeeded / len(records), 4) if records else 0,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "ttfb_ms": {
            "p50": percentile(ttfb_values, 0.5),
            "p95": percentile(ttfb_values, 0.95),
            "p99": percentile(ttfb_values, 0.99),
        },
        "evaluation": {
            "scored_cases": len(scored),
            "dimension_average_scores": average_dimension_scores,
            "average_overall_score": round(
                sum(float(item["overall_score"]) for item in scored) / len(scored), 2
            ) if scored else None,
        },
        "result_file_id": str(result_file_id) if result_file_id else None,
    }
