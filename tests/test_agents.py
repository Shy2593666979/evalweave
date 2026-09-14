import json
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlmodel import Session

from evalweave.agents.executor import (
    _tabular_markdown_report,
    authenticate_target,
    detect_application_error,
    execute_data_program,
    execute_target_case,
    isolate_session_ids,
    normalize_target_body,
    parse_target_response,
    serialize_results,
    source_parsing_options,
    validate_target,
)
from evalweave.agents.inspection import load_source_rows
from evalweave.agents.model_config import encrypt_secret_payload
from evalweave.agents.planner import evaluate_target_records
from evalweave.agents.python_workspace import run_python_workspace
from evalweave.agents.workflow import (
    execute_agent_job,
    plan_agent_job,
    validate_http_target_semantics,
    validate_http_target_with_react,
)
from evalweave.core.config import AgentConfig, get_settings
from evalweave.db.models import AgentJob, FileObject
from evalweave.db.session import get_engine
from evalweave.storage import LocalFileStorage


def login_as_developer(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "agent_developer",
            "email": "agent_developer@example.com",
            "password": "developer-password",
            "user_type_id": development["id"],
        },
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "agent_developer", "password": "developer-password"},
    )
    assert response.status_code == 200


def test_agent_plans_waits_for_human_and_resumes(client: TestClient, monkeypatch) -> None:
    login_as_developer(client)
    runtime = client.get("/api/agent/runtime")
    assert runtime.status_code == 200
    assert "api_key" not in runtime.json()
    project = client.post("/api/projects", json={"name": "Agent workflow"}).json()
    upload = client.post(
        f"/api/projects/{project['id']}/files",
        files={
            "file": (
                "companion.json",
                b'[{"user_message":"I feel lonely","scene":"support"}]',
                "application/json",
            )
        },
    )
    assert upload.status_code == 201

    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Companion evaluation",
            "goal": "Evaluate conversation quality, tools and latency",
            "source_file_id": upload.json()["id"],
            "notification_targets": [{"channel": "email", "recipient": "reviewer@example.com"}],
        },
    )
    assert created.status_code == 201
    job_id = UUID(created.json()["id"])

    plan_agent_job(job_id)
    planned = client.get(f"/api/agent-jobs/{job_id}").json()
    assert planned["status"] == "waiting_human"
    assert planned["eval_spec"]["source"]["fields"] == ["scene", "user_message"]

    steps = client.get(f"/api/agent-jobs/{job_id}/steps").json()
    assert [step["name"] for step in steps] == ["discover_source", "generate_eval_spec"]

    tasks = client.get("/api/human-tasks?task_status=pending").json()
    assert len(tasks) == 1
    deliveries = client.get(f"/api/human-tasks/{tasks[0]['id']}/deliveries").json()
    assert deliveries[0]["status"] == "failed"
    assert "not configured" in deliveries[0]["error"]

    queued: list[tuple[str, UUID]] = []
    monkeypatch.setattr(
        "evalweave.api.routes.agents.enqueue_agent_task",
        lambda name, identifier: queued.append((name, identifier)),
    )
    decision = client.post(
        f"/api/human-tasks/{tasks[0]['id']}/decision",
        json={"decision": "approve", "reason": "Plan looks good"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"
    assert queued == [("evalweave.agent.execute", job_id)]

    execute_agent_job(job_id)
    completed = client.get(f"/api/agent-jobs/{job_id}").json()
    assert completed["status"] == "completed"
    assert completed["result"]["execution"]["execution_mode"] == "local_data"


def test_agent_assistant_returns_a_job_draft(client: TestClient, monkeypatch) -> None:
    login_as_developer(client)
    monkeypatch.setattr(
        "evalweave.api.routes.agents.assist_job_configuration",
        lambda *_: {
            "reply": "已识别接口和输出格式，可以创建任务。",
            "draft": {
                "title": "接口回复质量评测",
                "target_url": "https://model.test/chat",
                "target_body": {"message": "{{message}}"},
                "output_format": "xlsx",
            },
        },
    )
    response = client.post(
        "/api/agent/assist",
        json={"messages": [{"role": "user", "content": "评测这个接口并导出 Excel"}]},
    )
    assert response.status_code == 200
    assert response.json()["draft"]["output_format"] == "xlsx"


def test_source_parsing_options_accepts_model_field_mapping_shape() -> None:
    options = source_parsing_options(
        {
            "source": {
                "header_row": 2,
                "data_start_row": 3,
                "field_mappings": {
                    "student_name": "column_1（姓名）",
                    "student_id": "column_2（学号）",
                    "comprehensive_rank": "column_23（综测排名）",
                },
            }
        }
    )

    assert options[0:2] == (2, 3)
    assert options[2][0:2] == ["student_name", "student_id"]
    assert options[2][22] == "comprehensive_rank"


def test_result_output_formats() -> None:
    records = [
        {
            "case_index": 0,
            "status": "completed",
            "latency_ms": 12.5,
            "ttfb_ms": 4.5,
            "input": {"message": "你好"},
            "output": "您好",
            "evaluation": {
                "dimensions": [
                    {"key": "speed", "label": "速度评分", "score": 9, "reason": "响应较快"},
                    {"key": "relevance", "label": "匹配度评分", "score": 10, "reason": "回答匹配"},
                ],
                "overall_score": 9.5,
                "passed": True,
                "reason": "回答正确",
            },
        }
    ]
    excel, extension, _ = serialize_results(records, "xlsx")
    assert extension == "xlsx"
    workbook = load_workbook(BytesIO(excel), read_only=True)
    try:
        values = list(workbook.active.values)
        assert "综合评分" in values[0]
        assert "速度评分（1-10分）" in values[0]
        assert 9.5 in values[1]
        assert any("您好" in str(value) for value in values[1])
    finally:
        workbook.close()
    for output_format, expected_extension in [
        ("jsonl", "jsonl"),
        ("markdown", "md"),
        ("text", "txt"),
    ]:
        content, extension, _ = serialize_results(records, output_format)
        assert extension == expected_extension
        assert "您好" in content.decode()
        if output_format == "markdown":
            report = content.decode()
            assert "# AI 评测报告" in report
            assert "## 结论摘要" in report
            assert "## 核心指标" in report
            assert "## 代表性结果" in report
            assert "## 结果解读" in report
            assert "回答正确" in report


def test_tabular_markdown_is_a_readable_report_with_limited_preview() -> None:
    rows = [
        {
            "query": f"问题 {index}",
            "answer1": f"回答 A-{index}",
            "answer2": f"回答 B-{index}",
            "answer1_latency_ms": 100 + index,
            "answer2_latency_ms": 180 + index,
        }
        for index in range(8)
    ]
    summaries = [
        {
            "target": "接口 A",
            "total": 8,
            "succeeded": 8,
            "failed": 0,
            "average_latency_ms": 103.5,
            "p95_latency_ms": 107,
        },
        {
            "target": "接口 B",
            "total": 8,
            "succeeded": 7,
            "failed": 1,
            "average_latency_ms": 183.5,
            "p95_latency_ms": 187,
        },
    ]

    report = _tabular_markdown_report(rows, summaries, "双接口对比", "比较速度和回答")

    assert report.startswith("# 双接口对比")
    assert "## 结论摘要" in report
    assert "接口调用累计成功 **15/16** 次" in report
    assert "## 数据预览" in report
    assert "## 结果解读" in report
    assert "问题 4" in report
    assert "问题 5" not in report


def test_isolate_session_ids_replaces_nested_session_without_mutating_source() -> None:
    source = {"query": "1+1等于几", "session_id": "shared", "nested": [{"session_id": "shared"}]}
    isolated = isolate_session_ids(source)

    assert source["session_id"] == "shared"
    assert isolated["session_id"] != "shared"
    assert isolated["nested"][0]["session_id"] != "shared"
    assert isolated["session_id"] == isolated["nested"][0]["session_id"]
    assert isolate_session_ids(source)["session_id"] != isolated["session_id"]


def test_target_record_evaluation_accepts_dynamic_dimensions(monkeypatch) -> None:
    monkeypatch.setattr(
        "evalweave.agents.planner.request_json",
        lambda *_args, **_kwargs: {
            "evaluations": [
                {
                    "case_index": 0,
                    "dimensions": [
                        {"key": "speed", "label": "速度评分", "score": 8, "reason": "首包较快"}
                    ],
                    "overall_score": 8,
                    "passed": True,
                    "reason": "达到速度要求",
                }
            ]
        },
    )
    config = AgentConfig(enabled=True, base_url="https://model.test", model="judge")
    evaluations = evaluate_target_records(
        config,
        "对接口速度按1到10分评分",
        [
            {
                "case_index": 0,
                "status": "completed",
                "latency_ms": 850,
                "ttfb_ms": 120,
                "input": {"input": {"query": "你好"}, "expected": {"ttfb_ms_threshold": 500}},
                "output": "您好",
            }
        ],
    )

    assert evaluations[0]["dimensions"][0]["key"] == "speed"
    assert evaluations[0]["dimensions"][0]["score"] == 8


def test_agent_executes_http_target_without_host_allowlist(
    client: TestClient, monkeypatch
) -> None:
    class FakeResponse:
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"data": {"reply": "I hear you"}}

    class FakeClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Target execution"}).json()
    upload = client.post(
        f"/api/projects/{project['id']}/files",
        files={"file": ("cases.json", b'[{"message":"hello"}]', "application/json")},
    ).json()
    monkeypatch.setattr("evalweave.agents.executor.httpx.Client", FakeClient)
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "HTTP target",
            "goal": "Run target",
            "source_file_id": upload["id"],
            "requires_approval": False,
            "input_config": {
                "target": {
                    "url": "https://model.test/chat",
                    "body": {"message": "{{message}}"},
                    "response_path": "data.reply",
                }
            },
        },
    ).json()
    plan_agent_job(UUID(created["id"]))

    completed = client.get(f"/api/agent-jobs/{created['id']}").json()
    target = completed["result"]["execution"]["target"]
    assert target["executed"] is True
    assert target["succeeded"] == 1
    result_file = client.get(f"/api/files/{target['result_file_id']}/content")
    assert result_file.status_code == 200
    workbook = load_workbook(BytesIO(result_file.content), read_only=True)
    try:
        values = list(workbook.active.values)
        assert any("I hear you" in str(value) for value in values[1])
    finally:
        workbook.close()


def test_task_credentials_run_login_and_inject_token(client: TestClient) -> None:
    class LoginClient:
        cookies: dict[str, str] = {}

        def post(self, url: str, **kwargs):
            assert url == "https://auth.test/login"
            assert kwargs["json"] == {"username": "agent", "password": "secret"}
            return httpx.Response(
                200,
                json={"data": {"access_token": "runtime-token"}},
                request=httpx.Request("POST", url),
            )

    credentials = encrypt_secret_payload(
        {
            "headers": {"X-Task": "evaluation"},
            "auth": {
                "login_url": "https://auth.test/login",
                "body": {"username": "agent", "password": "secret"},
                "token_path": "data.access_token",
                "header_name": "Authorization",
                "header_prefix": "Bearer ",
            },
        }
    )

    _, headers = validate_target(
        {"url": "https://target.test/chat", "credentials": credentials}
    )
    authentication = authenticate_target(
        LoginClient(), "https://target.test/chat", headers, credentials
    )

    assert headers == {
        "X-Task": "evaluation",
        "Authorization": "Bearer runtime-token",
    }
    assert authentication["used"] is True
    assert authentication["source"] == "task"
    assert "runtime-token" not in str(authentication)


def test_python_workspace_registers_transformed_file(client: TestClient) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Python workspace"}).json()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["input", "output"])
    sheet.append(
        [
            json.dumps({"content": "你好"}, ensure_ascii=False),
            json.dumps({"answer": "您好"}, ensure_ascii=False),
        ]
    )
    content = BytesIO()
    workbook.save(content)
    workbook.close()
    uploaded = client.post(
        f"/api/projects/{project['id']}/files",
        files={
            "file": (
                "source.xlsx",
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    ).json()
    code = """
import json
from pathlib import Path
from openpyxl import Workbook, load_workbook

source = next(Path("inputs").glob("*.xlsx"))
workbook = load_workbook(source)
sheet = workbook.active
sheet["A2"] = json.loads(sheet["A2"].value)["content"]
sheet["B2"] = json.loads(sheet["B2"].value)["answer"]
workbook.save("outputs/cleaned.xlsx")
workbook.close()
"""

    with Session(get_engine()) as session:
        observation, updates = run_python_workspace(
            session,
            UUID(project["id"]),
            {"source_file_id": uploaded["id"]},
            code,
        )
        transformed = session.get(FileObject, UUID(updates["source_file_id"]))
        assert transformed is not None
        path = LocalFileStorage(get_settings().storage.local_directory).path_for(
            transformed.storage_key
        )
        rows = load_source_rows(path, transformed.original_name, 10)

    assert observation["outputs"][0]["file_name"] == "cleaned.xlsx"
    assert rows == [{"input": "你好", "output": "您好"}]


def test_agent_generates_cases_when_http_target_has_no_source_file(
    client: TestClient, monkeypatch
) -> None:
    class FakeResponse:
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"reply": "ok"}

    class FakeClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Generated cases"}).json()
    monkeypatch.setattr("evalweave.agents.executor.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "evalweave.agents.workflow.generate_test_cases",
        lambda *_, **__: [
            {"input": {"message": "hello"}, "expected": {}, "metadata": {"category": "normal"}},
            {
                "input": {"message": "edge case"},
                "expected": {},
                "metadata": {"category": "edge"},
            },
        ],
    )
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Generated HTTP cases",
            "goal": "Generate two chat cases and run the target",
            "requires_approval": False,
            "input_config": {
                "max_cases": 2,
                "target": {
                    "url": "https://model.test/chat",
                    "body": {"message": "{{message}}"},
                },
            },
        },
    ).json()
    plan_agent_job(UUID(created["id"]))

    completed = client.get(f"/api/agent-jobs/{created['id']}").json()
    target = completed["result"]["execution"]["target"]
    assert completed["status"] == "completed"
    assert target["case_source"] == "ai_generated"
    assert target["total_cases"] == 2
    assert completed["eval_spec"]["source"]["case_count"] == 2
    steps = client.get(f"/api/agent-jobs/{created['id']}/steps").json()
    assert [step["name"] for step in steps] == [
        "generate_eval_spec",
        "generate_test_cases",
        "validate_target",
        "execute_eval_spec",
        "summarize",
    ]
    event_stream = client.get(f"/api/agent-jobs/{created['id']}/events")
    assert event_stream.status_code == 200
    assert "event: agent-event" in event_stream.text
    assert "event: job-state" in event_stream.text
    assert '"steps":' in event_stream.text
    assert '"event_type": "model_start"' in event_stream.text
    assert "event: end" in event_stream.text


def test_target_preflight_failure_returns_to_react_and_retries(
    client: TestClient, monkeypatch
) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Target repair"}).json()
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Repair target request",
            "goal": "Validate a chat endpoint",
            "requires_approval": False,
            "input_config": {
                "target": {
                    "url": "https://model.test/chat",
                    "body": '{"query":"{{generated_query}}"}',
                }
            },
        },
    ).json()
    attempts = 0

    def fake_validate(_session, _job):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("HTTP 422; response body: expected an object")
        return {"validated": True, "records": [], "response_modes": ["streaming"]}

    def fake_react(*_args):
        running = {"name": "update_task_draft", "status": "running"}
        yield "tool_start", running
        yield "tool_result", {
            **running,
            "status": "completed",
            "summary": {"ok": True, "updated_fields": ["target_body"]},
        }
        yield "result", {
            "draft": {
                "target_url": "https://model.test/chat",
                "target_body": {"query": "{{generated_query}}"},
                "target_validated": True,
                "expected_streaming": True,
            },
            "ui_action": None,
        }

    monkeypatch.setattr("evalweave.agents.workflow.validate_http_target", fake_validate)
    monkeypatch.setattr("evalweave.agents.workflow.stream_react_configuration", fake_react)
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test")
    with Session(get_engine()) as session:
        job = session.get(AgentJob, UUID(created["id"]))
        assert job is not None
        job.eval_spec = {
            "generated_cases": [{"input": {"generated_query": "hello"}}]
        }
        session.add(job)
        session.commit()
        result = validate_http_target_with_react(session, job, config)
        session.refresh(job)
        assert result["validated"] is True
        assert job.repair_attempts == 1
        assert job.input_config["target"]["body"] == {
            "query": "{{generated_query}}"
        }
    steps = client.get(f"/api/agent-jobs/{created['id']}/steps").json()
    assert [(step["name"], step["status"]) for step in steps] == [
        ("validate_target", "failed"),
        ("update_task_draft", "completed"),
        ("validate_target", "completed"),
    ]


def test_semantic_preflight_rejects_when_all_sample_responses_are_wrong(
    client: TestClient, monkeypatch
) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Semantic preflight"}).json()
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Validate response meaning",
            "goal": "检查回答是否正确",
            "requires_approval": False,
            "input_config": {"target": {"url": "https://model.test/chat"}},
        },
    ).json()
    records = [
        {
            "case_index": 0,
            "status": "completed",
            "input": {"input": {"content": "太阳系行星顺序"}, "expected": {"correct": True}},
            "output": {"code": 200, "data": "错误答案"},
        },
        {
            "case_index": 1,
            "status": "completed",
            "input": {"input": {"content": "计算价格"}, "expected": {"correct": True}},
            "output": {"code": 200, "data": "无法回答"},
        },
    ]
    monkeypatch.setattr(
        "evalweave.agents.workflow.validate_http_target",
        lambda *_: {"validated": True, "records": records},
    )
    monkeypatch.setattr(
        "evalweave.agents.workflow.evaluate_target_records",
        lambda *_args, **_kwargs: [
            {
                "case_index": record["case_index"],
                "passed": False,
                "reason": "回答与问题和预期不符",
            }
            for record in records
        ],
    )
    monkeypatch.setattr(
        "evalweave.agents.workflow.run_streamed_model_output",
        lambda _job, _phase, _label, operation: operation(lambda _delta: None),
    )
    config = AgentConfig(enabled=True, base_url="https://judge.test", model="judge")

    with Session(get_engine()) as session:
        job = session.get(AgentJob, UUID(created["id"]))
        assert job is not None
        with pytest.raises(ValueError, match="抽检响应全部不符合测试预期"):
            validate_http_target_semantics(session, job, config)


def test_parse_target_response_detects_server_sent_events() -> None:
    class StreamingResponse:
        headers = {"content-type": "text/event-stream; charset=utf-8"}
        text = 'data: {"delta":"hello"}\n\ndata: {"delta":" world"}\n\ndata: [DONE]\n'

    output, response_mode = parse_target_response(StreamingResponse(), "")

    assert response_mode == "streaming"
    assert output == {"events": [{"delta": "hello"}, {"delta": " world"}]}


def test_parse_target_response_supports_stream_wildcard_path() -> None:
    class StreamingResponse:
        headers = {"content-type": "text/event-stream"}
        text = (
            'data: {"data":{"message":"hello"}}\n\n'
            'data: {"data":{"message":" world"}}\n\n'
            "data: [DONE]\n"
        )

    output, response_mode = parse_target_response(
        StreamingResponse(), "events[*].data.message"
    )

    assert response_mode == "streaming"
    assert output == "hello world"


def test_normalize_target_body_decodes_json_object_template() -> None:
    body = normalize_target_body('{"query":"{{generated_query}}","plugins":[]}')

    assert body == {"query": "{{generated_query}}", "plugins": []}


def test_session_id_isolation_supports_camel_and_snake_case() -> None:
    isolated = isolate_session_ids(
        {
            "sessionId": "shared-camel",
            "nested": {"session_id": "shared-snake"},
        },
        session_id="isolated-case",
    )

    assert isolated["sessionId"] == "isolated-case"
    assert isolated["nested"]["session_id"] == "isolated-case"


def test_target_case_rejects_application_error_inside_http_200() -> None:
    class FakeClient:
        def post(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"code": 500, "msg": "服务器内部错误!", "data": None},
                request=httpx.Request("POST", "https://model.test/chat"),
            )

    record = execute_target_case(
        FakeClient(),
        "https://model.test/chat",
        {},
        {"body": "{{row}}"},
        {"input": {"sessionId": "shared", "content": "hello"}},
        0,
    )

    assert record["status"] == "failed"
    assert record["status_code"] == 200
    assert "code=500" in record["error"]
    assert "服务器内部错误" in record["response_body"]


def test_application_error_detector_allows_success_envelope() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"code": 200, "msg": "ok", "data": {"answer": "hello"}},
    )

    assert detect_application_error(response) is None


def test_assistant_conversation_streams_and_persists_draft(client: TestClient, monkeypatch) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Assistant project"}).json()
    conversation = client.post("/api/assistant/conversations", json={"project_id": project["id"]})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    greeting = client.get(f"/api/assistant/conversations/{conversation_id}/messages").json()
    assert greeting[0]["role"] == "assistant"

    uploaded = client.post(
        f"/api/projects/{project['id']}/files",
        data={"category": "dataset_source"},
        files={
            "file": (
                "cases.xlsx",
                b"test workbook",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert uploaded.status_code == 201

    monkeypatch.setattr(
        "evalweave.api.routes.agents.assist_job_configuration",
        lambda *_: {
            "reply": "已收到需求。",
            "draft": {
                "title": "成绩比拼",
                "goal": "比较综合成绩和各项排名",
                "target_url": "https://model.test/scores",
                "target_body": {"score": "{{score}}"},
                "target_validated": True,
                "auth_required": True,
            },
        },
    )
    response = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages/stream",
        json={
            "content": "比较成绩",
            "source_file_id": uploaded.json()["id"],
        },
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert events[-1]["stage"] == "choose_output"
    assert events[-1]["content"] == "已收到需求。"

    stored = client.get("/api/assistant/conversations").json()[0]
    assert stored["draft"]["goal"] == "比较综合成绩和各项排名"
    assert stored["status"] == "choose_output"
    messages = client.get(f"/api/assistant/conversations/{conversation_id}/messages").json()
    assert [message["role"] for message in messages] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[1]["attachment_file_id"] == uploaded.json()["id"]
    assert messages[1]["attachment_name"] == "cases.xlsx"
    assert messages[1]["attachment_content_type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert messages[1]["attachment_size_bytes"] == len(b"test workbook")

    monkeypatch.setattr(
        "evalweave.api.routes.agents.assist_job_configuration",
        lambda *_: {
            "reply": "将使用上传的数据比较成绩并生成 Excel 结果，是否开始执行？",
            "draft": {},
            "ui_action": {"type": "confirm"},
        },
    )
    ready_response = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages/stream",
        json={"content": "Excel 文件", "output_format": "xlsx"},
    )
    ready_events = [json.loads(line) for line in ready_response.text.splitlines()]
    assert ready_events[-1]["stage"] == "ready"
    assert ready_events[-1]["draft"]["output_format"] == "xlsx"
    assert ready_events[-1]["content"] == (
        "将使用上传的数据比较成绩并生成 Excel 结果，是否开始执行？"
    )

    job = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "成绩比拼",
            "goal": "比较综合成绩和各项排名",
            "output_format": "xlsx",
            "input_config": {"target": {"url": "https://model.test/scores"}},
        },
    ).json()
    started = client.post(
        f"/api/assistant/conversations/{conversation_id}/started",
        json={"agent_job_id": job["id"]},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "started"
    assert started.json()["agent_job_id"] == job["id"]
    messages = client.get(f"/api/assistant/conversations/{conversation_id}/messages").json()
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "确认并开启"


def test_assistant_stream_forwards_react_tool_events(client: TestClient, monkeypatch) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "ReAct project"}).json()
    conversation = client.post(
        "/api/assistant/conversations", json={"project_id": project["id"]}
    ).json()
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test-model")
    monkeypatch.setattr("evalweave.api.routes.agents.resolve_agent_config", lambda *_: config)

    def fake_react(*_):
        running = {"name": "probe_http_target", "label": "验证目标接口", "status": "running"}
        completed = {**running, "status": "completed", "summary": {"status_code": 200}}
        yield "delta", "The endpoint is reachable. "
        yield "tool_start", running
        yield "tool_result", completed
        yield "delta", "Choose a delivery format."
        yield "result", {
            "reply": "Choose a delivery format.",
            "draft": {
                "title": "接口评测",
                "goal": "验证接口质量",
                "target_url": "https://model.test/chat",
                "target_body": {"query": "{{query}}"},
                "target_validated": True,
            },
            "ui_action": {"type": "choose_output"},
            "react_trace": [completed],
        }

    monkeypatch.setattr(
        "evalweave.api.routes.agents.stream_react_configuration", fake_react
    )
    response = client.post(
        f"/api/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "评测这个接口"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]

    assert [event["type"] for event in events] == [
        "start",
        "delta",
        "tool_start",
        "tool_result",
        "delta",
        "done",
    ]
    assert events[-1]["content"] == "The endpoint is reachable. Choose a delivery format."
    assert events[-1]["stage"] == "choose_output"
    assert events[-1]["draft"]["react_trace"][0]["status"] == "completed"
    stored_messages = client.get(
        f"/api/assistant/conversations/{conversation['id']}/messages"
    ).json()
    assert stored_messages[-1]["content"] == events[-1]["content"]


def test_assistant_user_input_keeps_conversation_collecting(
    client: TestClient, monkeypatch
) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Input required"}).json()
    conversation = client.post(
        "/api/assistant/conversations", json={"project_id": project["id"]}
    ).json()
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test-model")
    monkeypatch.setattr("evalweave.api.routes.agents.resolve_agent_config", lambda *_: config)

    def fake_react(*_):
        yield "result", {
            "reply": "Please provide the missing request field.",
            "draft": {
                "title": "API evaluation",
                "goal": "Evaluate answer quality",
                "target_url": "https://model.test/chat",
                "target_body": {"query": "{{query}}"},
                "target_validated": True,
                "output_format": "xlsx",
            },
            "ui_action": {
                "type": "user_input",
                "question": "Please provide the missing request field.",
                "options": [],
            },
            "react_trace": [],
        }

    monkeypatch.setattr(
        "evalweave.api.routes.agents.stream_react_configuration", fake_react
    )
    response = client.post(
        f"/api/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "Continue configuring"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]

    assert events[-1]["stage"] == "collecting"


def test_assistant_completed_tool_reply_does_not_request_task_confirmation(
    client: TestClient, monkeypatch
) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Completed file operation"}).json()
    conversation = client.post(
        "/api/assistant/conversations", json={"project_id": project["id"]}
    ).json()
    generated_file = client.post(
        f"/api/projects/{project['id']}/files",
        data={"category": "dataset_source"},
        files={"file": ("scored_results.xlsx", b"generated workbook", "application/xlsx")},
    ).json()
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test-model")
    monkeypatch.setattr("evalweave.api.routes.agents.resolve_agent_config", lambda *_: config)

    def fake_react(*_):
        yield "result", {
            "reply": "文件已经重新评分并生成，可以直接下载。",
            "draft": {
                "title": "模型回答质量评分",
                "goal": "重新评估回答相关性",
                "task_mode": "local_analysis",
                "source_file_id": generated_file["id"],
                "source_inspected": True,
                "output_format": "xlsx",
            },
            "ui_action": None,
            "react_trace": [
                {
                    "name": "run_python",
                    "label": "运行 Python 文件处理",
                    "status": "completed",
                    "summary": {
                        "ok": True,
                        "primary_output_file_id": generated_file["id"],
                        "outputs": [
                            {
                                "file_id": generated_file["id"],
                                "file_name": "scored_results.xlsx",
                            }
                        ],
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "evalweave.api.routes.agents.stream_react_configuration", fake_react
    )
    response = client.post(
        f"/api/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "重新评分这个文件"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]

    assert events[-1]["type"] == "done", events
    assert events[-1]["stage"] == "collecting"
    assert events[-1]["draft"]["ui_action"] is None
    assert events[-1]["attachment_file_id"] == generated_file["id"]
    messages = client.get(
        f"/api/assistant/conversations/{conversation['id']}/messages"
    ).json()
    assert messages[-1]["attachment_file_id"] == generated_file["id"]
    assert messages[-1]["attachment_name"] == "scored_results.xlsx"


def test_generic_data_program_combines_model_and_multiple_http_targets(
    client: TestClient, monkeypatch
) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Generic data program"}).json()
    source = client.post(
        f"/api/projects/{project['id']}/files",
        files={
            "file": (
                "queries.json",
                b'[{"query":"hello"},{"query":"edge case"}]',
                "application/json",
            )
        },
    ).json()
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Generic enrichment",
            "goal": "Generate cases and compare two APIs",
            "source_file_id": source["id"],
            "output_format": "xlsx",
            "requires_approval": False,
            "input_config": {
                "targets": [
                    {
                        "name": "API 1",
                        "url": "https://model.test/one",
                        "body": {"query": "{{query}}"},
                    },
                    {
                        "name": "API 2",
                        "url": "https://model.test/two",
                        "body": {"query": "{{query}}"},
                    },
                ]
            },
        },
    ).json()
    monkeypatch.setattr(
        "evalweave.agents.executor.authenticate_target", lambda *_args, **_kwargs: "none"
    )

    def fake_target_case(_client, url, _headers, _target, row, index):
        target_number = 1 if url.endswith("/one") else 2
        return {
            "case_index": index,
            "status": "completed",
            "latency_ms": float(100 * target_number + index),
            "ttfb_ms": float(50 * target_number + index),
            "input": row,
            "output": f"answer-{target_number}-{index}",
            "response_mode": "json",
        }

    monkeypatch.setattr("evalweave.agents.executor.execute_target_case", fake_target_case)
    with Session(get_engine()) as session:
        job = session.get(AgentJob, UUID(created["id"]))
        assert job is not None
        job.eval_spec = {
            "source": {
                "header_row": 1,
                "field_mappings": {"query": "canonical_query"},
            },
            "data_program": {
                "steps": [
                    {"primitive": "convert"},
                    {
                        "primitive": "model_map",
                        "instruction": "Generate two additional cases",
                        "input_fields": ["canonical_query"],
                        "output_columns": [{"name": "generated_cases", "type": "array"}],
                    },
                    {"action": "http_map", "target_index": 0, "answer_column": "answer1"},
                    {"action": "http_map", "target_index": 1, "answer_column": "answer2"},
                    {"primitive": "aggregate", "instruction": "Summarize numeric metrics"},
                    {"primitive": "summarize"},
                    {"primitive": "format_convert"},
                ]
            },
        }
        session.add(job)
        session.commit()

        result = execute_data_program(
            session,
            job,
            lambda _instruction, rows, _columns, _index: [
                {
                    "generated_cases": [
                        f"{row['canonical_query']}-normal",
                        f"{row['canonical_query']}-edge",
                    ]
                }
                for row in rows
            ],
        )
        result_file_id = result["result_file_id"]

    assert result["summaries"][0]["average_latency_ms"] == 100.5
    assert result["summaries"][1]["average_latency_ms"] == 200.5
    download = client.get(f"/api/files/{result_file_id}/content")
    workbook = load_workbook(BytesIO(download.content), read_only=True)
    try:
        rows = list(workbook["结果"].values)
        assert "generated_cases" in rows[0]
        assert "answer1" in rows[0]
        assert "answer2" in rows[0]
        assert "hello-normal" in rows[1][rows[0].index("generated_cases")]
        assert workbook["汇总"].max_row == 4
    finally:
        workbook.close()
