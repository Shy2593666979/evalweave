import json
from io import BytesIO
from uuid import UUID

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlmodel import Session

from evalweave.agents.executor import (
    _tabular_markdown_report,
    execute_data_program,
    isolate_session_ids,
    normalize_target_body,
    parse_target_response,
    serialize_results,
    source_parsing_options,
)
from evalweave.agents.planner import evaluate_target_records
from evalweave.agents.workflow import (
    execute_agent_job,
    plan_agent_job,
    validate_http_target_with_react,
)
from evalweave.core.config import AgentConfig, get_settings
from evalweave.db.models import AgentJob
from evalweave.db.session import get_engine


def login_as_developer(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "agent_developer",
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


def test_agent_executes_allowlisted_http_target(client: TestClient, monkeypatch) -> None:
    class FakeResponse:
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
    settings = get_settings()
    original_hosts = list(settings.agent.allowed_target_hosts)
    settings.agent.allowed_target_hosts = ["model.test"]
    monkeypatch.setattr("evalweave.agents.executor.httpx.Client", FakeClient)
    try:
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
    finally:
        settings.agent.allowed_target_hosts = original_hosts

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


def test_agent_generates_cases_when_http_target_has_no_source_file(
    client: TestClient, monkeypatch
) -> None:
    class FakeResponse:
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
    settings = get_settings()
    original_hosts = list(settings.agent.allowed_target_hosts)
    settings.agent.allowed_target_hosts = ["model.test"]
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
    try:
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
    finally:
        settings.agent.allowed_target_hosts = original_hosts

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
        yield "tool_start", running
        yield "tool_result", completed
        yield "result", {
            "reply": "接口预检通过，请选择交付方式。",
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
        "tool_start",
        "tool_result",
        "done",
    ]
    assert events[-1]["stage"] == "choose_output"
    assert events[-1]["draft"]["react_trace"][0]["status"] == "completed"


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
    settings = get_settings()
    original_hosts = list(settings.agent.allowed_target_hosts)
    settings.agent.allowed_target_hosts = ["model.test"]
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
    try:
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
    finally:
        settings.agent.allowed_target_hosts = original_hosts

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
