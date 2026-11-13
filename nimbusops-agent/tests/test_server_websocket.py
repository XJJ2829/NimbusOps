import asyncio
import json

from fastapi.testclient import TestClient

from my_agent.bus.events import OutboundMessage
from my_agent.config.schema import Config
from my_agent.cron.types import CronJob, CronJobState, CronPayload
from my_agent.server import app as server_app


class FakeSession:
    def __init__(self):
        self.metadata = {}


class FakeSessions:
    def __init__(self):
        self._sessions = []
        self._items = {}

    def list_sessions(self):
        return self._sessions

    def read_session_file(self, key):
        return {"key": key, "messages": [], "metadata": {}}

    def delete_session(self, key):
        return True

    def flush_all(self):
        return 0

    def get_or_create(self, key):
        return self._items.setdefault(key, FakeSession())

    def save(self, session):
        return None


class FakeMedia:
    def rewrite_local_markdown_images(self, text):
        return text


class FakeTranscripts:
    def __init__(self):
        self.events = []

    def client_turn_metadata(self, value):
        return {"webui_turn_id": str(value)}

    def prepare_and_append(
        self,
        chat_id,
        event,
        *,
        metadata=None,
        phase=None,
        include_source=False,
    ):
        self.events.append(
            {
                "chat_id": chat_id,
                "event": event,
                "metadata": metadata or {},
                "phase": phase,
                "include_source": include_source,
            }
        )


class FakeLoop:
    def __init__(self):
        self.sessions = FakeSessions()
        self.cron_service = FakeCronService()
        self.model = "custom/test"

    async def process_direct(
        self,
        content,
        *,
        session_key,
        channel,
        chat_id,
        media,
        metadata=None,
        on_progress,
        on_stream,
        on_stream_end,
    ):
        await on_progress("", tool_events=[{"phase": "start", "name": "fake_tool"}])
        await on_stream(f"reply: {content}")
        await on_stream_end(resuming=False)
        await on_progress("", tool_events=[{"phase": "end", "name": "fake_tool"}])
        return OutboundMessage(channel=channel, chat_id=chat_id, content=f"reply: {content}")

    async def close_mcp(self):
        return None


class CronFakeLoop:
    def __init__(self):
        self.sessions = FakeSessions()
        self.tools = {}
        self.calls = []

    async def process_direct(
        self,
        content,
        *,
        session_key,
        channel,
        chat_id,
        media=None,
        metadata=None,
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        ephemeral=False,
        tools=None,
    ):
        self.calls.append(
            {
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "media": media,
                "metadata": metadata or {},
                "ephemeral": ephemeral,
            }
        )
        if on_progress is not None:
            await on_progress("")
        return OutboundMessage(channel=channel, chat_id=chat_id, content="cron result")


class FakeCronService:
    def __init__(self):
        self.jobs = []

    def list_jobs(self, *, include_disabled=False):
        if include_disabled:
            return list(self.jobs)
        return [job for job in self.jobs if job.enabled]

    def add_job(
        self,
        *,
        name,
        schedule,
        message,
        deliver=False,
        channel=None,
        to=None,
        delete_after_run=False,
        channel_meta=None,
        session_key=None,
    ):
        job = CronJob(
            id=f"job-{len(self.jobs) + 1}",
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                channel_meta=channel_meta or {},
                session_key=session_key,
            ),
            state=CronJobState(next_run_at_ms=schedule.at_ms or 1),
            delete_after_run=delete_after_run,
        )
        self.jobs.append(job)
        return job


class BlockingFakeLoop(FakeLoop):
    def __init__(self):
        super().__init__()
        self.cancelled = False

    async def process_direct(
        self,
        content,
        *,
        session_key,
        channel,
        chat_id,
        media,
        metadata=None,
        on_progress,
        on_stream,
        on_stream_end,
    ):
        await on_stream("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class InspectionFakeLoop(FakeLoop):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    async def process_direct(
        self,
        content,
        *,
        session_key,
        channel,
        chat_id,
        media,
        metadata=None,
        on_progress,
        on_stream,
        on_stream_end,
    ):
        approved = await self.runtime.agentspec.ask_human(
            session_key=session_key,
            rule_id="confirm_exec",
            action={"name": "exec", "input": {"command": "rm -rf target"}},
        )
        answer = "approved" if approved else "rejected"
        await on_stream(answer)
        await on_stream_end(resuming=False)
        return OutboundMessage(channel=channel, chat_id=chat_id, content=answer)


async def fake_start(self):
    self.config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "custom/test"}},
        }
    )
    self.loop = FakeLoop()
    self.media = FakeMedia()


async def fake_blocking_start(self):
    self.config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "custom/test"}},
        }
    )
    self.loop = BlockingFakeLoop()
    self.media = FakeMedia()


async def fake_inspection_start(self):
    self.config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "custom/test"}},
        }
    )
    self.loop = InspectionFakeLoop(self)
    self.media = FakeMedia()


def test_websocket_chat_event_order(monkeypatch):
    monkeypatch.setattr(server_app.AgentRuntime, "start", fake_start)

    with TestClient(server_app.create_app()) as client:
        token = client.get("/webui/bootstrap").json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "ready"

            ws.send_json(
                {
                    "type": "chat.send",
                    "sessionId": f"websocket:{connected['chat_id']}",
                    "content": "hello",
                }
            )
            events = [ws.receive_json()["event"] for _ in range(6)]

    assert events == [
        "user_message",
        "tool_start",
        "assistant_delta",
        "assistant_stream_end",
        "tool_end",
        "turn_end",
    ]


def test_websocket_stop_interrupts_active_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(server_app.AgentRuntime, "start", fake_blocking_start)
    app = server_app.create_app(tmp_path / "config.json")

    with TestClient(app) as client:
        token = client.get("/webui/bootstrap").json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ready = ws.receive_json()
            chat_id = ready["chat_id"]
            ws.send_json(
                {
                    "type": "message",
                    "chat_id": chat_id,
                    "content": "keep working",
                    "webui": True,
                }
            )

            assert ws.receive_json()["event"] == "delta"

            ws.send_json({"type": "stop", "chat_id": chat_id})
            stopped = ws.receive_json()

            assert stopped["event"] == "turn_end"
            assert stopped["stopped"] is True
            assert app.state.runtime.loop.cancelled is True


def test_websocket_user_inspection_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(server_app.AgentRuntime, "start", fake_inspection_start)
    app = server_app.create_app(tmp_path / "config.json")

    with TestClient(app) as client:
        token = client.get("/webui/bootstrap").json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ready = ws.receive_json()
            chat_id = ready["chat_id"]
            ws.send_json(
                {
                    "type": "message",
                    "chat_id": chat_id,
                    "content": "run cleanup",
                    "webui": True,
                }
            )

            inspection = ws.receive_json()
            assert inspection["event"] == "security_inspection"
            assert inspection["rule_id"] == "confirm_exec"
            assert inspection["action"]["name"] == "exec"

            ws.send_json(
                {
                    "type": "security.inspection.resolve",
                    "inspection_id": inspection["inspection_id"],
                    "approved": True,
                }
            )

            assert ws.receive_json()["event"] == "delta"
            assert ws.receive_json()["event"] == "stream_end"
            assert ws.receive_json()["event"] == "message"
            assert ws.receive_json()["event"] == "turn_end"


def test_session_automation_create_route(monkeypatch, tmp_path):
    monkeypatch.setattr(server_app.AgentRuntime, "start", fake_start)
    app = server_app.create_app(tmp_path / "config.json")

    with TestClient(app) as client:
        client.post(
            "/api/auth/setup",
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": "password123",
            },
        )
        token = client.get("/webui/bootstrap").json()["token"]
        payload = {
            "name": "Morning check",
            "message": "Check system status",
            "schedule": {"kind": "every", "every_ms": 3_600_000},
        }
        response = client.get(
            "/api/sessions/websocket:chat-1/automations/create",
            params={"payload": json.dumps(payload)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"][0]["name"] == "Morning check"
    assert body["jobs"][0]["payload"]["message"] == "Check system status"


def test_cron_job_runs_inside_bound_webui_session(tmp_path):
    runtime = server_app.AgentRuntime(tmp_path / "config.json")
    loop = CronFakeLoop()
    transcripts = FakeTranscripts()
    notifications = []
    pushed_events = []
    runtime.loop = loop
    runtime.transcripts = transcripts

    async def notify(chat_id, *, scope="thread"):
        notifications.append((chat_id, scope))

    async def push(chat_id, events):
        pushed_events.append((chat_id, events))

    runtime.notify_webui_session_updated = notify
    runtime.push_webui_events = push

    job = CronJob(
        id="job-1",
        name="Morning check",
        enabled=True,
        payload=CronPayload(
            kind="agent_turn",
            message="Check system status",
            deliver=True,
            channel="websocket",
            to="chat-1",
            session_key="websocket:chat-1",
        ),
        state=CronJobState(),
    )

    result = asyncio.run(runtime._run_cron_job(job))

    assert result == "cron result"
    assert len(loop.calls) == 1
    call = loop.calls[0]
    assert call["session_key"] == "websocket:chat-1"
    assert call["channel"] == "websocket"
    assert call["chat_id"] == "chat-1"
    assert call["ephemeral"] is False
    assert "当前对话上下文" in call["content"]
    assert call["metadata"]["automation_session_key"] == "websocket:chat-1"

    assert [item["event"]["event"] for item in transcripts.events] == [
        "user",
        "message",
        "turn_end",
    ]
    assert transcripts.events[0]["chat_id"] == "chat-1"
    assert transcripts.events[0]["event"]["text"] == (
        "自动运维任务「Morning check」已触发：Check system status"
    )
    assert transcripts.events[1]["include_source"] is True
    assert len(pushed_events) == 1
    assert pushed_events[0][0] == "chat-1"
    assert [event["event"] for event in pushed_events[0][1]] == [
        "user",
        "message",
        "turn_end",
    ]
    assert notifications == [("chat-1", "thread")]
