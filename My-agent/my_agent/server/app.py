"""FastAPI gateway for the browser WebUI and WebSocket chat channel."""

from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import hmac
import inspect
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import ValidationError

from my_agent.agent.loop import AgentLoop
from my_agent.agentspec.runtime import AgentSpecError, AgentSpecRuntime
from my_agent.bus.queue import MessageBus, OutboundMessage
from my_agent.command.builtin import builtin_command_palette
from my_agent.config.loader import (
    load_config,
    resolve_config_env_vars,
    save_config,
    set_config_path,
)
from my_agent.config.paths import get_agentspec_rules_dir, get_cron_dir, get_media_dir
from my_agent.config.schema import (
    ActionGuardrailsConfig,
    Config,
    PromptGuardrailsConfig,
    PrivilegedExecutionConfig,
    RuntimeIdentityConfig,
)
from my_agent.cron.service import CronService
from my_agent.cron.types import CronJob
from my_agent.guardrails import ActionGuardrailsRuntime, PromptGuardrailsRuntime, PromptGuardrailsVerdict
from my_agent.providers.factory import load_provider_snapshot
from my_agent.providers.image_generation import image_gen_provider_configs
from my_agent.security.auth import (
    AUTH_USER_METADATA_KEY,
    KNOWN_PERMISSIONS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    AuthUser,
    LoginSessionStore,
    UserStore,
    bind_current_user,
    reset_current_user,
)
from my_agent.security.runtime_identity import (
    enforce_startup_identity,
    runtime_identity_payload,
    validate_runtime_identity,
)
from my_agent.security.privileged import PrivilegedActionRuntime
from my_agent.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY, WorkspaceScopeError
from my_agent.server.runtime_host import (
    ensure_runtime_logging,
    export_diagnostics,
    open_logs,
    pick_folder,
    runtime_info,
)
from my_agent.utils.helpers import safe_filename
from my_agent.utils.media_decode import FileSizeExceeded, save_base64_data_url
from my_agent.webui.audit_center import get_audit_trace, list_audit_traces
from my_agent.webui.builtin_tools_api import builtin_tools_payload
from my_agent.webui.cli_apps_api import (
    CliAppError,
    cli_apps_action,
    cli_apps_import_archive,
    cli_apps_payload,
    normalize_cli_app_mentions,
)
from my_agent.webui.file_preview import WebUIFilePreviewError, file_preview_payload
from my_agent.webui.mcp_presets_api import (
    McpPresetError,
    attach_mcp_hot_reload_result,
    import_mcp_archive_action,
    mcp_presets_settings_action,
    normalize_mcp_preset_mentions,
)
from my_agent.webui.session_automations import (
    create_session_automation_job,
    session_automations_payload,
)
from my_agent.webui.settings_api import (
    WebUISettingsError,
    create_model_configuration,
    decorate_settings_payload,
    login_oauth_provider,
    logout_oauth_provider,
    provider_models_payload,
    settings_payload,
    settings_usage_payload,
    update_agent_settings,
    update_image_generation_settings,
    update_model_configuration,
    update_network_safety_settings,
    update_provider_settings,
    update_web_search_settings,
)
from my_agent.webui.sidebar_state import read_webui_sidebar_state, write_webui_sidebar_state
from my_agent.webui.skills_api import (
    WebUISkillImportError,
    webui_import_skill_archive_payload,
    webui_skill_detail_payload,
    webui_skills_payload,
)
from my_agent.webui.thread_disk import delete_webui_thread
from my_agent.webui.transcript import (
    WEBUI_MESSAGE_SOURCE_METADATA_KEY,
    WebUITranscriptRecorder,
    build_webui_thread_response,
    rewrite_local_markdown_images,
)
from my_agent.webui.workspaces import WebUIWorkspaceController

SERVER_START_TIME = time.time()


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _config_payload(config: Config) -> dict[str, Any]:
    return config.model_dump(mode="json", by_alias=True)


def _chat_id() -> str:
    return str(uuid.uuid4())


def _session_key(chat_id: str) -> str:
    return f"websocket:{chat_id}"


def _split_session_key(key: str) -> tuple[str, str]:
    if ":" not in key:
        return "websocket", key
    channel, chat_id = key.split(":", 1)
    return channel, chat_id


def _query_params(request: Request) -> dict[str, list[str]]:
    return {key: request.query_params.getlist(key) for key in request.query_params.keys()}


def _uploaded_archive_filename(request: Request) -> str | None:
    raw = request.headers.get("x-my-agent-filename") or request.headers.get("X-My-Agent-Filename")
    if not raw:
        return None
    return urllib.parse.unquote(raw).strip()[:240] or None


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "").strip()


def _mcp_query_params(request: Request) -> dict[str, list[str]]:
    query = _query_params(request)
    raw = request.headers.get("x-nanobot-mcp-values") or request.headers.get("X-Nanobot-MCP-Values")
    if not raw:
        return query
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise HTTPException(status_code=400, detail="MCP settings payload is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid MCP settings payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP settings payload must be a JSON object")
    merged = {key: list(values) for key, values in query.items()}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=400, detail="MCP settings payload contains an invalid key")
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if text:
            merged[key] = [text]
    return merged


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


class WebUITokenStore:
    """Short-lived bearer tokens for the embedded WebUI."""

    def __init__(self) -> None:
        self.issued_tokens: dict[str, tuple[float, str]] = {}
        self.api_tokens: dict[str, tuple[float, str]] = {}

    def issue_token(
        self,
        ttl_s: int | float,
        *,
        user_id: str,
        api_token: bool = False,
    ) -> str:
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.issued_tokens[token] = (expiry, user_id)
        if api_token:
            self.api_tokens[token] = (expiry, user_id)
        return token

    def _purge(self) -> None:
        now = time.monotonic()
        for store in (self.issued_tokens, self.api_tokens):
            for token, (expiry, _) in list(store.items()):
                if now > expiry:
                    store.pop(token, None)

    def api_user_id(self, token: str) -> str | None:
        self._purge()
        entry = self.api_tokens.get(token)
        return entry[1] if entry else None

    def ws_user_id(self, token: str | None) -> str | None:
        self._purge()
        if not token:
            return None
        entry = self.api_tokens.get(token) or self.issued_tokens.get(token)
        return entry[1] if entry else None


class FastAPIMediaGateway:
    """Media signing/staging helpers used by the FastAPI WebUI gateway."""

    def __init__(self, *, workspace_path: Path, logger: Any, secret: bytes) -> None:
        self.workspace_path = workspace_path
        self.logger = logger
        self.secret = secret

    def sign_media_path(self, abs_path: Path) -> str | None:
        try:
            media_root = get_media_dir(None).resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        signed = self.sign_media_path(path)
        if signed is not None:
            return {"url": signed, "name": path.name}
        try:
            if not path.is_file():
                return None
            target_dir = get_media_dir("websocket")
            safe_name = safe_filename(path.name) or "attachment"
            staged = target_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            shutil.copyfile(path, staged)
        except OSError as exc:
            self.logger.warning("failed to stage outbound media {}: {}", path, exc)
            return None
        signed = self.sign_media_path(staged)
        return {"url": signed, "name": path.name} if signed else None

    def augment_transcript_media(self, paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for value in paths:
            path = Path(value)
            signed = self.sign_or_stage_media_path(path)
            if not signed or not signed.get("url"):
                continue
            name = signed.get("name") or path.name
            mime, _ = mimetypes.guess_type(name)
            kind = "video" if mime and mime.startswith("video/") else "image" if mime and mime.startswith("image/") else "file"
            out.append({"kind": kind, "url": signed["url"], "name": name})
        return out

    def augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        return self.augment_transcript_media(paths)

    def rewrite_local_markdown_images(self, text: str) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=self.workspace_path,
            sign_path=self.sign_or_stage_media_path,
        )


class AgentRuntime:
    """Owns config, the full nanobot-derived AgentLoop, and WebUI gateway state."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        if config_path is not None:
            set_config_path(config_path)
        self.config: Config | None = None
        self.loop: AgentLoop | None = None
        self.agentspec = AgentSpecRuntime()
        self.guardrails = ActionGuardrailsRuntime()
        self.prompt_guardrails = PromptGuardrailsRuntime()
        self._prompt_guardrails_runtime_config: PromptGuardrailsConfig | None = None
        self.privileged = PrivilegedActionRuntime()
        self.tokens = WebUITokenStore()
        self.users = UserStore()
        self.login_sessions = LoginSessionStore(self.users)
        self.transcripts = WebUITranscriptRecorder(logger)
        self.cron_service = CronService(get_cron_dir() / "jobs.json", on_job=self._run_cron_job)
        self._media_secret = secrets.token_bytes(32)
        self.media: FastAPIMediaGateway | None = None
        self._reload_lock = asyncio.Lock()
        self._webui_chat_sockets: dict[str, set[WebSocket]] = {}

    async def start(self) -> None:
        ensure_runtime_logging()
        startup_config = resolve_config_env_vars(load_config(self.config_path))
        enforce_startup_identity(startup_config.security.runtime_identity)
        await self.reload(save_default=True)
        await self.cron_service.start()

    async def stop(self) -> None:
        self.cron_service.stop()
        loop = self.loop
        if loop is None:
            return
        with suppress(Exception):
            loop.sessions.flush_all()
        with suppress(Exception):
            await loop.close_mcp()

    async def reload(self, *, save_default: bool = False) -> Config:
        async with self._reload_lock:
            old_loop = self.loop
            raw_config = load_config(self.config_path)
            if save_default and self.config_path is not None and not self.config_path.exists():
                save_config(raw_config, self.config_path)
            config = resolve_config_env_vars(raw_config)
            agentspec_config = config.security.agentspec
            rules_dir = (
                Path(agentspec_config.rules_dir).expanduser()
                if agentspec_config.rules_dir.strip()
                else get_agentspec_rules_dir()
            )
            if not rules_dir.is_absolute() and self.config_path is not None:
                rules_dir = self.config_path.parent / rules_dir
            self.agentspec.configure(
                rules_dir=rules_dir,
                enabled=agentspec_config.enabled,
                inspection_timeout=agentspec_config.inspection_timeout,
                fail_closed=agentspec_config.fail_closed,
            )
            self.guardrails.configure(config.guardrails.action)
            prompt_guardrails_config = self._prompt_guardrails_runtime_config
            if prompt_guardrails_config is None:
                prompt_guardrails_config = config.guardrails.prompt.model_copy(update={"enabled": False})
            self.prompt_guardrails.configure(
                prompt_guardrails_config,
                project_root=Path(__file__).resolve().parents[2],
                app_config=config,
            )
            self.privileged.configure(config.security.privileged_execution)
            if config.security.privileged_execution.enabled:
                await self.privileged.refresh_actions(tolerate_unavailable=True)
            loop = self._build_loop(config)
            self.config = config
            self.loop = loop
            self.media = FastAPIMediaGateway(
                workspace_path=config.workspace_path,
                logger=logger,
                secret=self._media_secret,
            )
            if old_loop is not None:
                with suppress(Exception):
                    old_loop.sessions.flush_all()
                with suppress(Exception):
                    await old_loop.close_mcp()
            return config

    async def replace_config(self, data: dict[str, Any], *, partial: bool = False) -> Config:
        async with self._reload_lock:
            base: dict[str, Any] = {}
            if partial and self.config is not None:
                base = _config_payload(self.config)
            next_data = _deep_merge(base, data) if partial else data
            try:
                config = Config.model_validate(next_data)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
            save_config(config, self.config_path)
        return await self.reload()

    def require_loop(self) -> AgentLoop:
        if self.loop is None:
            raise HTTPException(status_code=503, detail="Agent runtime is not ready.")
        return self.loop

    def require_config(self) -> Config:
        if self.config is None:
            raise HTTPException(status_code=503, detail="Config is not loaded.")
        return self.config

    def require_media(self) -> FastAPIMediaGateway:
        if self.media is None:
            raise HTTPException(status_code=503, detail="WebUI media gateway is not ready.")
        return self.media

    def issue_webui_token(self, user: AuthUser, ttl_s: int = 3600) -> str:
        return self.tokens.issue_token(ttl_s, user_id=user.id, api_token=True)

    def api_user(self, request: Request) -> AuthUser | None:
        user_id = self.tokens.api_user_id(_bearer_token(request))
        return self.users.find_by_id(user_id) if user_id else None

    def ws_user(self, token: str | None) -> AuthUser | None:
        user_id = self.tokens.ws_user_id(token)
        return self.users.find_by_id(user_id) if user_id else None

    def workspace_controller(self, user: AuthUser | None = None) -> WebUIWorkspaceController:
        config = self.require_config()
        default_workspace = config.workspace_path
        if user is not None and not user.has("sandbox.host_exec") and user.workspace_root:
            default_workspace = Path(user.workspace_root).expanduser().resolve(strict=False)
        return WebUIWorkspaceController(
            session_manager=self.require_loop().sessions,
            default_workspace=default_workspace,
            default_restrict_to_workspace=config.tools.restrict_to_workspace,
        )

    async def _run_cron_job(self, job: CronJob) -> str | None:
        """Execute a scheduled job and mirror delivered WebUI replies to its session."""
        loop = self.require_loop()
        if job.payload.kind != "agent_turn":
            logger.info("Cron system job '{}' is registered but has no NimbusOps handler.", job.name)
            return None

        message = job.payload.message.strip()
        if not message:
            return None

        target_key = self._cron_target_session_key(job)
        channel, chat_id = _split_session_key(target_key)
        display_text = f"自动运维任务「{job.name}」已触发"
        prompt = (
            f"自动运维任务「{job.name}」已触发：{message}\n\n"
            "请基于当前对话上下文执行该任务，并只返回面向用户的简洁结果。"
        )
        metadata = {
            **self.transcripts.client_turn_metadata(f"cron:{job.id}:{int(time.time() * 1000)}"),
            "webui": channel == "websocket",
            WEBUI_MESSAGE_SOURCE_METADATA_KEY: {
                "kind": "cron",
                "label": job.name,
            },
            "automation_job_id": job.id,
            "automation_name": job.name,
            "automation_session_key": target_key,
        }

        cron_tool = loop.tools.get("cron")
        cron_token = None
        if cron_tool is not None and hasattr(cron_tool, "set_cron_context"):
            cron_token = cron_tool.set_cron_context(True)
        try:
            should_stream_webui = job.payload.deliver and channel == "websocket" and bool(chat_id)
            if should_stream_webui:
                verdict = self.prompt_guardrails.inspect_direct_input(message)
                direct_scan = _prompt_guardrails_scan_payload(
                    verdict,
                    threshold=self.prompt_guardrails.config.threshold,
                )

                async def send_event(event: dict[str, Any]) -> None:
                    await self.push_webui_events(chat_id, [event])

                await _send_webui_automation_trigger(
                    self,
                    chat_id,
                    label=job.name,
                    metadata=metadata,
                    send_event=send_event,
                )
                if not verdict.allowed:
                    await _send_webui_prompt_blocked(
                        chat_id,
                        direct_scan,
                        metadata=metadata,
                        runtime=self,
                        send_event=send_event,
                    )
                    outbound = None
                else:
                    outbound = await _run_webui_agent_turn(
                        self,
                        content=prompt,
                        session_key=target_key,
                        channel=channel,
                        chat_id=chat_id,
                        media=[],
                        metadata=metadata,
                        direct_scan=direct_scan,
                        send_event=send_event,
                        include_source_on_final=True,
                    )
            else:
                async def _silent(*_args: Any, **_kwargs: Any) -> None:
                    return None

                outbound = await loop.process_direct(
                    prompt,
                    session_key=target_key,
                    channel=channel or "websocket",
                    chat_id=chat_id or "direct",
                    media=[],
                    metadata=metadata,
                    on_progress=_silent,
                )
        finally:
            if cron_token is not None and hasattr(cron_tool, "reset_cron_context"):
                cron_tool.reset_cron_context(cron_token)

        response = outbound.content.strip() if outbound is not None and outbound.content else ""
        if job.payload.deliver and channel == "websocket" and chat_id:
            await self.notify_webui_session_updated(chat_id, scope="thread")
        return response or None

    @staticmethod
    def _cron_target_session_key(job: CronJob) -> str:
        if job.payload.session_key:
            return job.payload.session_key
        if job.payload.channel and job.payload.to:
            return f"{job.payload.channel}:{job.payload.to}"
        return f"cron:{job.id}"

    def _record_cron_delivery(
        self,
        session_key: str,
        content: str,
        label: str,
        *,
        user_text: str | None = None,
        metadata: dict[str, Any] | None = None,
        persist_session: bool = True,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        channel, chat_id = _split_session_key(session_key)
        if channel != "websocket" or not chat_id:
            return None
        loop = self.require_loop()
        live_events: list[dict[str, Any]] = []
        metadata = {
            **dict(metadata or {}),
            WEBUI_MESSAGE_SOURCE_METADATA_KEY: {
                "kind": "cron",
                "label": label,
            },
        }
        if persist_session:
            with suppress(Exception):
                session = loop.sessions.get_or_create(session_key)
                session.add_message("assistant", content, **metadata)
                loop.sessions.save(session)
        if user_text:
            user_event = {"event": "user", "chat_id": chat_id, "text": user_text}
            self.transcripts.prepare_and_append(
                chat_id,
                user_event,
                metadata=metadata,
                phase="user",
            )
            live_events.append(dict(user_event))
        message_event = {"event": "message", "chat_id": chat_id, "text": content}
        self.transcripts.prepare_and_append(
            chat_id,
            message_event,
            metadata=metadata,
            phase="answer",
            include_source=True,
        )
        live_events.append(dict(message_event))
        turn_end_event = {"event": "turn_end", "chat_id": chat_id, "latency_ms": 0}
        self.transcripts.prepare_and_append(
            chat_id,
            turn_end_event,
            metadata=metadata,
            phase="complete",
        )
        live_events.append(dict(turn_end_event))
        return chat_id, live_events

    def attach_webui_socket(self, chat_id: str, websocket: WebSocket) -> None:
        if not _valid_chat_id(chat_id):
            return
        self._webui_chat_sockets.setdefault(chat_id, set()).add(websocket)

    def detach_webui_socket(self, chat_id: str, websocket: WebSocket) -> None:
        sockets = self._webui_chat_sockets.get(chat_id)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self._webui_chat_sockets.pop(chat_id, None)

    async def notify_webui_session_updated(self, chat_id: str, *, scope: str = "thread") -> None:
        sockets = list(self._webui_chat_sockets.get(chat_id) or ())
        stale: list[WebSocket] = []
        for websocket in sockets:
            try:
                await _safe_send(websocket, "session_updated", chat_id=chat_id, scope=scope)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.detach_webui_socket(chat_id, websocket)

    async def push_webui_events(self, chat_id: str, events: list[dict[str, Any]]) -> None:
        sockets = list(self._webui_chat_sockets.get(chat_id) or ())
        stale: list[WebSocket] = []
        for websocket in sockets:
            try:
                for event in events:
                    event_name = event.get("event")
                    if not isinstance(event_name, str) or not event_name:
                        continue
                    payload = {key: value for key, value in event.items() if key != "event"}
                    await _safe_send(websocket, event_name, **payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.detach_webui_socket(chat_id, websocket)

    def _build_loop(self, config: Config) -> AgentLoop:
        bus = MessageBus()
        return AgentLoop.from_config(
            config,
            bus=bus,
            cron_service=self.cron_service,
            image_generation_provider_configs=image_gen_provider_configs(config),
            provider_snapshot_loader=lambda **kwargs: load_provider_snapshot(
                self.config_path,
                **kwargs,
            ),
            agentspec_runtime=self.agentspec,
            action_guardrails_runtime=self.guardrails,
            prompt_guardrails_runtime=self.prompt_guardrails,
            privileged_runtime=self.privileged,
        )


def _require_webui_token(runtime: AgentRuntime, request: Request) -> AuthUser:
    user = runtime.api_user(request)
    if user is None or not user.enabled:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def _require_permission(
    runtime: AgentRuntime,
    request: Request,
    permission: str,
) -> AuthUser:
    user = _require_webui_token(runtime, request)
    if not user.has(permission):
        raise HTTPException(status_code=403, detail="当前用户没有执行此操作的权限")
    return user


def _browser_user(runtime: AgentRuntime, request: Request) -> AuthUser | None:
    return runtime.login_sessions.resolve(request.cookies.get(SESSION_COOKIE))


def _require_browser_user(runtime: AgentRuntime, request: Request) -> AuthUser:
    user = _browser_user(runtime, request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _set_login_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _session_owned_by(
    runtime: AgentRuntime,
    user: AuthUser,
    session_key: str,
    *,
    claim_missing: bool = False,
) -> bool:
    payload = runtime.require_loop().sessions.read_session_file(session_key)
    if payload is None:
        return claim_missing
    metadata = payload.get("metadata", {})
    owner_id = metadata.get(AUTH_USER_METADATA_KEY) if isinstance(metadata, dict) else None
    if owner_id:
        return owner_id == user.id or user.has("sessions.read_all")
    return user.has("sessions.read_all")


def _audit_session_row(runtime: AgentRuntime, row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    session_key = str(item.get("key") or "")
    payload = runtime.require_loop().sessions.read_session_file(session_key)
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    owner_id = metadata.get(AUTH_USER_METADATA_KEY) if isinstance(metadata, dict) else None
    owner = runtime.users.find_by_id(str(owner_id)) if owner_id else None
    item["actor_username"] = owner.username if owner is not None else "未知用户"
    return item


def _claim_session(runtime: AgentRuntime, user: AuthUser, session_key: str) -> None:
    session = runtime.require_loop().sessions.get_or_create(session_key)
    owner_id = session.metadata.get(AUTH_USER_METADATA_KEY)
    if owner_id and owner_id != user.id and not user.has("sessions.read_all"):
        raise HTTPException(status_code=403, detail="无权访问此会话")
    if not owner_id:
        session.metadata[AUTH_USER_METADATA_KEY] = user.id
        runtime.require_loop().sessions.save(session)


def _require_session_access(
    runtime: AgentRuntime,
    request: Request,
    session_key: str,
) -> AuthUser:
    user = _require_webui_token(runtime, request)
    if not _session_owned_by(runtime, user, session_key):
        raise HTTPException(status_code=404, detail="Not found")
    return user


def _action_guardrails_payload(
    runtime: AgentRuntime,
    user: AuthUser,
) -> dict[str, Any]:
    """Return guardrail settings plus selectable conversation/automation scopes."""
    loop = runtime.require_loop()
    payload = runtime.guardrails.payload(loop.tool_names)
    sessions: list[dict[str, str]] = []
    accessible_session_keys: set[str] = set()
    for row in loop.sessions.list_sessions():
        session_key = str(row.get("key") or "").strip()
        if not session_key or not _session_owned_by(runtime, user, session_key):
            continue
        accessible_session_keys.add(session_key)
        sessions.append(
            {
                "id": session_key,
                "name": str(row.get("title") or row.get("preview") or session_key),
            }
        )

    automations: list[dict[str, str]] = []
    if runtime.cron_service is not None:
        for job in runtime.cron_service.list_jobs(include_disabled=True):
            if job.payload.kind != "agent_turn":
                continue
            target_session = runtime._cron_target_session_key(job)
            if not user.has("sessions.read_all") and target_session not in accessible_session_keys:
                continue
            automations.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "sessionId": target_session,
                }
            )
    payload["scopeOptions"] = {
        "sessions": sessions,
        "automations": automations,
    }
    return payload


def _prompt_guardrails_payload(runtime: AgentRuntime) -> dict[str, Any]:
    return runtime.prompt_guardrails.payload()


def _prompt_guardrails_scan_payload(
    verdict: PromptGuardrailsVerdict,
    *,
    threshold: float,
    tool_name: str | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    return {
        "source": verdict.source,
        "allowed": verdict.allowed,
        "reason": verdict.reason,
        "score": verdict.score,
        "threshold": threshold,
        "label": verdict.label,
        "toolName": tool_name,
        "callId": call_id,
    }


def _complete_prompt_guardrails_scans(
    scans: list[dict[str, Any]] | None,
    *,
    runtime: AgentRuntime,
) -> list[dict[str, Any]]:
    if not scans:
        return []
    threshold = runtime.prompt_guardrails.config.threshold
    completed: list[dict[str, Any]] = []
    for scan in scans:
        if not isinstance(scan, dict):
            continue
        item = dict(scan)
        item.setdefault("threshold", threshold)
        completed.append(item)
    return completed


def _blocked_prompt_guardrails_scan(scans: list[dict[str, Any]]) -> dict[str, Any] | None:
    for scan in scans:
        if scan.get("allowed") is False:
            return scan
    return None


def _prompt_guardrails_block_source(scan: dict[str, Any]) -> str:
    source = str(scan.get("source") or "")
    return "direct" if source == "direct" else "indirect"


def _prompt_guardrails_block_reason(scan: dict[str, Any]) -> str:
    source = _prompt_guardrails_block_source(scan)
    if source == "direct":
        return "直接输入检测命中提示词攻击风险，消息没有进入会话。"
    return "间接输入检测命中提示词攻击风险，工具返回已被拦截，原始内容没有进入模型上下文。"


def _action_guardrails_budget_stop(outbound: OutboundMessage | None) -> str | None:
    if outbound is None:
        return None
    metadata = outbound.metadata if isinstance(outbound.metadata, dict) else {}
    if metadata.get("_stop_reason") != "guardrail_error":
        return None
    raw = metadata.get("_action_guardrails_budget_stop")
    reason = raw.get("reason") if isinstance(raw, dict) else None
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return outbound.content.strip() or None


async def _send_prepared_webui_event(
    runtime: AgentRuntime,
    chat_id: str,
    event: dict[str, Any],
    *,
    metadata: dict[str, Any],
    phase: str,
    send_event: Callable[[dict[str, Any]], Awaitable[None]],
    include_source: bool = False,
) -> None:
    runtime.transcripts.prepare_and_append(
        chat_id,
        event,
        metadata=metadata,
        phase=phase,
        include_source=include_source,
    )
    await send_event(event)


async def _send_webui_automation_trigger(
    runtime: AgentRuntime,
    chat_id: str,
    *,
    label: str,
    metadata: dict[str, Any],
    send_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    event = {
        "event": "user",
        "chat_id": chat_id,
        "text": f"自动运维任务「{label}」已触发",
        "media_urls": [
            {
                "url": "/brand/NimbusOps-32.png?v=4",
                "name": "NimbusOps-32.png",
            },
        ],
    }
    await _send_prepared_webui_event(
        runtime,
        chat_id,
        event,
        metadata=metadata,
        phase="user",
        send_event=send_event,
        include_source=True,
    )


async def _send_webui_prompt_blocked(
    chat_id: str,
    scan: dict[str, Any],
    *,
    metadata: dict[str, Any],
    runtime: AgentRuntime,
    send_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    await _send_prepared_webui_event(
        runtime,
        chat_id,
        {
            "event": "prompt_guardrails_scan",
            "chat_id": chat_id,
            "scans": [scan],
        },
        metadata=metadata,
        phase="activity",
        send_event=send_event,
    )
    await send_event(
        {
            "event": "error",
            "chat_id": chat_id,
            "detail": "prompt_guardrails_blocked",
            "source": "direct",
            "reason": _prompt_guardrails_block_reason(scan),
            "score": scan.get("score"),
        },
    )
    await _send_prepared_webui_event(
        runtime,
        chat_id,
        {
            "event": "turn_end",
            "chat_id": chat_id,
            "latency_ms": 0,
            "stopped": True,
        },
        metadata=metadata,
        phase="complete",
        send_event=send_event,
    )


async def _run_webui_agent_turn(
    runtime: AgentRuntime,
    *,
    content: str,
    session_key: str,
    channel: str,
    chat_id: str,
    media: list[str],
    metadata: dict[str, Any],
    direct_scan: dict[str, Any] | None,
    send_event: Callable[[dict[str, Any]], Awaitable[None]],
    include_source_on_final: bool = False,
) -> OutboundMessage | None:
    loop = runtime.require_loop()
    media_gateway = runtime.require_media()
    stream_id = f"{session_key}:{time.time_ns()}:0"
    chunks: list[str] = []
    t0 = time.time()

    async def send_prepared(
        event: dict[str, Any],
        *,
        phase: str,
        include_source: bool = False,
    ) -> None:
        await _send_prepared_webui_event(
            runtime,
            chat_id,
            event,
            metadata=metadata,
            phase=phase,
            send_event=send_event,
            include_source=include_source,
        )

    if direct_scan is not None and direct_scan.get("reason") != "disabled":
        await send_prepared(
            {
                "event": "prompt_guardrails_scan",
                "chat_id": chat_id,
                "scans": [direct_scan],
            },
            phase="activity",
        )
        await send_prepared(
            {
                "event": "message",
                "chat_id": chat_id,
                "text": "等待模型响应",
                "kind": "progress",
            },
            phase="activity",
        )

    async def on_stream(delta: str) -> None:
        chunks.append(delta)
        await send_prepared(
            {"event": "delta", "chat_id": chat_id, "text": delta, "stream_id": stream_id},
            phase="answer",
            include_source=include_source_on_final,
        )

    async def on_stream_end(*, resuming: bool = False) -> None:
        await send_prepared(
            {"event": "stream_end", "chat_id": chat_id, "stream_id": stream_id},
            phase="answer",
        )

    async def on_progress(
        progress: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        prompt_guardrails: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        if reasoning:
            await send_prepared(
                {
                    "event": "reasoning_delta",
                    "chat_id": chat_id,
                    "text": progress,
                    "stream_id": stream_id,
                },
                phase="reasoning",
            )
            return
        if reasoning_end:
            await send_prepared(
                {"event": "reasoning_end", "chat_id": chat_id, "stream_id": stream_id},
                phase="reasoning",
            )
            return
        if file_edit_events:
            await send_prepared(
                {"event": "file_edit", "chat_id": chat_id, "edits": file_edit_events},
                phase="activity",
            )
        prompt_scans = _complete_prompt_guardrails_scans(prompt_guardrails, runtime=runtime)
        if prompt_scans:
            await send_prepared(
                {"event": "prompt_guardrails_scan", "chat_id": chat_id, "scans": prompt_scans},
                phase="activity",
            )
            blocked_scan = _blocked_prompt_guardrails_scan(prompt_scans)
            if blocked_scan is not None:
                await send_event(
                    {
                        "event": "error",
                        "chat_id": chat_id,
                        "detail": "prompt_guardrails_blocked",
                        "source": _prompt_guardrails_block_source(blocked_scan),
                        "reason": _prompt_guardrails_block_reason(blocked_scan),
                        "score": blocked_scan.get("score"),
                    },
                )
        if progress or tool_events:
            event: dict[str, Any] = {
                "event": "message",
                "chat_id": chat_id,
                "text": progress,
                "kind": "tool_hint" if tool_hint else "progress",
            }
            if tool_events:
                event["tool_events"] = tool_events
            await send_prepared(event, phase="activity")

    try:
        outbound = await loop.process_direct(
            content,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            media=media,
            metadata=metadata,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
        final = outbound.content if outbound is not None else "".join(chunks)
        guardrail_stop_reason = _action_guardrails_budget_stop(outbound)
        if guardrail_stop_reason:
            await send_event(
                {
                    "event": "error",
                    "chat_id": chat_id,
                    "detail": "action_guardrails_budget_stopped",
                    "reason": guardrail_stop_reason,
                },
            )
        if final and not chunks:
            rewritten = media_gateway.rewrite_local_markdown_images(final)
            await send_prepared(
                {"event": "message", "chat_id": chat_id, "text": rewritten},
                phase="answer",
                include_source=include_source_on_final,
            )
            await _call_stream_end(on_stream_end)
        latency_ms = max(0, int((time.time() - t0) * 1000))
        await send_prepared(
            {
                "event": "turn_end",
                "chat_id": chat_id,
                "latency_ms": latency_ms,
            },
            phase="complete",
        )
        return outbound
    except asyncio.CancelledError:
        latency_ms = max(0, int((time.time() - t0) * 1000))
        with suppress(Exception):
            await send_prepared(
                {
                    "event": "turn_end",
                    "chat_id": chat_id,
                    "latency_ms": latency_ms,
                    "stopped": True,
                },
                phase="complete",
            )
        return None


def _run_command_text(command: list[str], *, timeout: float = 2.5) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    output = completed.stdout.strip()
    return output or None


def _read_linux_memory_snapshot() -> dict[str, int | float | None]:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return {}
    values: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"^([A-Za-z_()]+):\s+(\d+)\s+kB$", line.strip())
            if not match:
                continue
            values[match.group(1)] = int(match.group(2)) * 1024
    except Exception:
        return {}
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total:
        return {}
    if available is None:
        available = (
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
            + values.get("SReclaimable", 0)
            - values.get("Shmem", 0)
        )
    used = max(0, total - available)
    percent = (used / total * 100.0) if total else None
    return {
        "system_memory_bytes_total": total,
        "system_memory_bytes_available": max(0, available),
        "system_memory_bytes_used": used,
        "system_memory_percent": percent,
    }


def _read_darwin_memory_snapshot() -> dict[str, int | float | None]:
    total_pages = None
    total_page_size = None
    try:
        total_pages = os.sysconf("SC_PHYS_PAGES")
        total_page_size = os.sysconf("SC_PAGE_SIZE")
    except Exception:
        total_pages = total_page_size = None
    vm_stat = _run_command_text(["vm_stat"])
    if total_pages is None or total_page_size is None or not vm_stat:
        return {}
    total = int(total_pages) * int(total_page_size)
    page_size_match = re.search(r"page size of (\d+) bytes", vm_stat)
    page_size = int(page_size_match.group(1)) if page_size_match else 4096

    def _page_value(label: str) -> int:
        match = re.search(rf"{re.escape(label)}:\s+(\d+)\.", vm_stat)
        return int(match.group(1)) if match else 0

    available_pages = (
        _page_value("Pages free")
        + _page_value("Pages inactive")
        + _page_value("Pages speculative")
        + _page_value("Pages purgeable")
    )
    available = available_pages * page_size
    used = max(0, total - available)
    percent = (used / total * 100.0) if total else None
    return {
        "system_memory_bytes_total": total,
        "system_memory_bytes_available": max(0, available),
        "system_memory_bytes_used": used,
        "system_memory_percent": percent,
    }


def _read_memory_snapshot() -> dict[str, int | float | None]:
    if platform.system() == "Darwin":
        return _read_darwin_memory_snapshot()
    if platform.system() == "Linux":
        return _read_linux_memory_snapshot()
    return {}


def _read_system_cpu_percent() -> float | None:
    output = None
    if platform.system() == "Darwin":
        output = _run_command_text(["top", "-l", "1", "-n", "0"])
        if output:
            match = re.search(
                r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle",
                output,
            )
            if match:
                return round(100.0 - float(match.group(3)), 2)
    elif platform.system() == "Linux":
        output = _run_command_text(["top", "-bn1"])
        if output:
            match = re.search(
                r"%Cpu\(s\):\s*([\d.]+)\s*us,\s*([\d.]+)\s*sy,.*?([\d.]+)\s*id",
                output,
            )
            if match:
                return round(100.0 - float(match.group(3)), 2)
    return None


def _read_process_cpu_percent() -> float | None:
    output = _run_command_text(["ps", "-p", str(os.getpid()), "-o", "%cpu="])
    if not output:
        return None
    try:
        return float(output.splitlines()[0].strip())
    except Exception:
        return None


def _read_process_memory_bytes() -> int | None:
    output = _run_command_text(["ps", "-p", str(os.getpid()), "-o", "rss="])
    if not output:
        return None
    try:
        rss_kb = int(output.splitlines()[0].strip())
    except Exception:
        return None
    return rss_kb * 1024


def _read_process_count() -> int | None:
    output = _run_command_text(["ps", "-A", "-o", "pid="])
    if not output:
        return None
    return sum(1 for line in output.splitlines() if line.strip())


def _read_listening_ports() -> tuple[list[int], int]:
    ports: set[int] = set()
    lsof_output = _run_command_text(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if lsof_output:
        for line in lsof_output.splitlines()[1:]:
            match = re.search(r":(\d+)\s+\(LISTEN\)\s*$", line)
            if match:
                ports.add(int(match.group(1)))
    if not ports and platform.system() == "Linux":
        ss_output = _run_command_text(["ss", "-ltn"])
        if ss_output:
            for line in ss_output.splitlines():
                match = re.search(r":(\d+)\s*$", line.strip())
                if match and "LISTEN" in line:
                    ports.add(int(match.group(1)))
    return sorted(ports), len(ports)


def _read_listening_port_details(limit: int = 8) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    lsof_output = _run_command_text(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if lsof_output:
        for line in lsof_output.splitlines()[1:]:
            match = re.search(r":(\d+)\s+\(LISTEN\)\s*$", line)
            if not match:
                continue
            columns = line.split()
            if len(columns) < 2:
                continue
            process_name = columns[0]
            pid = columns[1]
            port = int(match.group(1))
            key = (port, pid)
            if key in seen:
                continue
            seen.add(key)
            details.append(
                {
                    "port": port,
                    "process": process_name,
                    "pid": pid,
                }
            )
            if len(details) >= limit:
                break
    if not details and platform.system() == "Linux":
        ss_output = _run_command_text(["ss", "-ltnp"])
        if ss_output:
            for line in ss_output.splitlines():
                if "LISTEN" not in line:
                    continue
                match = re.search(r":(\d+)\s*$", line.strip())
                if not match:
                    continue
                details.append({"port": int(match.group(1)), "process": "unknown", "pid": "?"})
                if len(details) >= limit:
                    break
    return details


def _read_network_bytes() -> tuple[int | None, int | None]:
    if platform.system() == "Linux":
        net_dev = Path("/proc/net/dev")
        if net_dev.exists():
            recv = sent = 0
            try:
                for line in net_dev.read_text(encoding="utf-8", errors="ignore").splitlines()[2:]:
                    if ":" not in line:
                        continue
                    _, payload = line.split(":", 1)
                    columns = payload.split()
                    if len(columns) >= 16:
                        recv += int(columns[0])
                        sent += int(columns[8])
                return sent, recv
            except Exception:
                pass
    if platform.system() == "Darwin":
        interfaces_output = _run_command_text(["ifconfig", "-l"])
        if interfaces_output:
            sent = recv = 0
            collected = 0
            for interface in interfaces_output.split():
                stats_output = _run_command_text(["netstat", "-ib", "-I", interface])
                if not stats_output:
                    continue
                lines = [line for line in stats_output.splitlines() if line.strip()]
                if len(lines) < 2:
                    continue
                header = lines[0].split()
                try:
                    ibytes_idx = header.index("Ibytes")
                    obytes_idx = header.index("Obytes")
                except ValueError:
                    continue
                for row in lines[1:]:
                    columns = row.split()
                    if len(columns) <= max(ibytes_idx, obytes_idx):
                        continue
                    if columns[0] != interface:
                        continue
                    try:
                        recv += int(columns[ibytes_idx])
                        sent += int(columns[obytes_idx])
                        collected += 1
                    except ValueError:
                        continue
                    break
            if collected:
                return sent, recv
    return None, None


def _workspace_usage_snapshot(workspace: str | Path | None) -> tuple[int | None, int | None, int | None]:
    root = Path(workspace or "/").expanduser().resolve(strict=False)
    if not root.exists():
        return None, None, None
    total_bytes = 0
    file_count = 0
    dir_count = 0
    try:
        for current_root, dirnames, filenames in os.walk(root, followlinks=False):
            dir_count += len(dirnames)
            file_count += len(filenames)
            current_dir = Path(current_root)
            for name in filenames:
                path = current_dir / name
                with suppress(OSError, PermissionError):
                    total_bytes += path.stat().st_size
    except Exception:
        return None, None, None
    return total_bytes, file_count, dir_count


def _read_platform_cpu_cores() -> tuple[int, int]:
    logical = os.cpu_count() or 0
    physical = logical
    if platform.system() == "Darwin":
        output = _run_command_text(["sysctl", "-n", "hw.physicalcpu"])
        if output:
            try:
                physical = int(output.strip())
            except Exception:
                physical = logical
    elif platform.system() == "Linux":
        output = _run_command_text(["getconf", "_NPROCESSORS_ONLN"])
        if output:
            try:
                logical = int(output.strip())
            except Exception:
                pass
        physical_output = _run_command_text(["lscpu"])
        if physical_output:
            match = re.search(r"Core\(s\) per socket:\s*(\d+)", physical_output)
            sockets = re.search(r"Socket\(s\):\s*(\d+)", physical_output)
            if match and sockets:
                try:
                    physical = int(match.group(1)) * int(sockets.group(1))
                except Exception:
                    physical = logical
    return physical or logical or 0, logical or 0


def _runtime_metrics(*, workspace: str | Path | None = None) -> dict[str, Any]:
    disk_target = Path(workspace or "/").expanduser().resolve(strict=False)
    disk_total = disk_used = disk_free = disk_percent = None
    with suppress(Exception):
        disk_usage = shutil.disk_usage(disk_target)
        disk_total = disk_usage.total
        disk_used = disk_usage.used
        disk_free = disk_usage.free
        disk_percent = (disk_usage.used / disk_usage.total * 100.0) if disk_usage.total else None

    load_1m = load_5m = load_15m = None
    with suppress(Exception):
        load_1m, load_5m, load_15m = os.getloadavg()

    cpu_count_physical, cpu_count_logical = _read_platform_cpu_cores()
    listening_ports, listening_port_count = _read_listening_ports()
    listening_port_details = _read_listening_port_details()
    network_bytes_sent, network_bytes_recv = _read_network_bytes()
    memory_snapshot = _read_memory_snapshot()
    workspace_bytes_used, workspace_file_count, workspace_dir_count = _workspace_usage_snapshot(workspace)
    hostname = socket.gethostname()
    current_user = getpass.getuser()

    available = False
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    memory_percent: float | None = None
    process_count: int | None = None
    uptime_seconds = max(0, time.time() - SERVER_START_TIME)

    try:
        import psutil

        process = psutil.Process()
        virtual_memory = psutil.virtual_memory()
        memory = process.memory_info()
        cpu_percent = process.cpu_percent(interval=0.05)
        memory_bytes = memory.rss
        memory_percent = process.memory_percent()
        process_count = len(list(psutil.pids()))
        cpu_system = psutil.cpu_percent(interval=0.05)
        if cpu_system is not None:
            cpu_percent = cpu_system
        memory_snapshot = {
            "system_memory_bytes_total": virtual_memory.total,
            "system_memory_bytes_available": virtual_memory.available,
            "system_memory_bytes_used": virtual_memory.used,
            "system_memory_percent": virtual_memory.percent,
        }
        network = psutil.net_io_counters()
        if network:
            network_bytes_sent = network.bytes_sent
            network_bytes_recv = network.bytes_recv
        with suppress(Exception):
            ports = {
                int(conn.laddr.port)
                for conn in psutil.net_connections(kind="inet")
                if conn.status == psutil.CONN_LISTEN and getattr(conn.laddr, "port", None) is not None
            }
            listening_ports = sorted(ports)
            listening_port_count = len(listening_ports)
        with suppress(Exception):
            uptime_seconds = max(0, time.time() - process.create_time())
        available = True
    except Exception:
        pass

    if cpu_percent is None:
        cpu_percent = _read_system_cpu_percent()
    if cpu_percent is None and load_1m is not None and cpu_count_logical:
        cpu_percent = min(100.0, (load_1m / max(1, cpu_count_logical)) * 100.0)
    if memory_bytes is None:
        memory_bytes = _read_process_memory_bytes()
    if memory_percent is None and memory_snapshot.get("system_memory_percent") is not None:
        memory_percent = float(memory_snapshot["system_memory_percent"] or 0)
    if process_count is None:
        process_count = _read_process_count() or 1
    if not available:
        available = any(
            value not in (None, 0, [], "")
            for value in [
                cpu_percent,
                memory_bytes,
                memory_snapshot.get("system_memory_bytes_total"),
                disk_total,
                load_1m,
                listening_port_count,
                network_bytes_sent,
                network_bytes_recv,
            ]
        )

    return {
        "available": available,
        "cpu_percent": cpu_percent or 0,
        "memory_bytes": memory_bytes or 0,
        "memory_percent": memory_percent or 0,
        "process_count": process_count or 1,
        "system_cpu_percent": cpu_percent or 0,
        "system_cpu_cores": cpu_count_physical,
        "system_cpu_logical_cores": cpu_count_logical,
        "system_memory_percent": memory_snapshot.get("system_memory_percent") or 0,
        "system_memory_bytes_total": memory_snapshot.get("system_memory_bytes_total") or 0,
        "system_memory_bytes_used": memory_snapshot.get("system_memory_bytes_used") or 0,
        "system_memory_bytes_available": memory_snapshot.get("system_memory_bytes_available") or 0,
        "system_disk_percent": disk_percent or 0,
        "system_disk_bytes_total": disk_total or 0,
        "system_disk_bytes_used": disk_used or 0,
        "system_disk_bytes_free": disk_free or 0,
        "load_average_1m": load_1m or 0,
        "load_average_5m": load_5m or 0,
        "load_average_15m": load_15m or 0,
        "network_bytes_sent": network_bytes_sent,
        "network_bytes_recv": network_bytes_recv,
        "listening_ports": listening_ports[:12],
        "listening_port_count": listening_port_count,
        "listening_port_details": listening_port_details,
        "hostname": hostname,
        "current_user": current_user,
        "workspace_bytes_used": workspace_bytes_used,
        "workspace_file_count": workspace_file_count,
        "workspace_dir_count": workspace_dir_count,
        "uptime_seconds": uptime_seconds,
    }


def _sandbox_payload(runtime: AgentRuntime, user: AuthUser) -> dict[str, Any]:
    config = runtime.require_config()
    configured = config.tools.exec.sandbox.strip()
    current_platform = platform.system()
    available_backends = {
        "bwrap": shutil.which("bwrap") is not None,
        "sandbox-exec": shutil.which("sandbox-exec") is not None,
    }
    effective = configured
    forced = not user.has("sandbox.host_exec")
    if forced and not effective:
        effective = "auto"
    if effective == "auto":
        effective = "sandbox-exec" if current_platform == "Darwin" else "bwrap"
    configured_restriction = config.tools.restrict_to_workspace
    return {
        "platform": current_platform,
        "configured_backend": configured,
        "effective_backend": effective,
        "enabled": bool(effective),
        "forced_for_user": forced,
        "available_backends": available_backends,
        "workspace": str(config.workspace_path),
        "configured_restrict_to_workspace": configured_restriction,
        "restrict_to_workspace": configured_restriction or forced,
        "host_exec_allowed": user.has("sandbox.host_exec"),
        "metrics": _runtime_metrics(workspace=config.workspace_path),
    }


def _prepare_workspace_directory(
    raw: Any,
    *,
    required: bool,
    required_detail: str,
) -> str | None:
    value = str(raw or "").strip()
    if not value:
        if required:
            raise HTTPException(status_code=400, detail=required_detail)
        return None
    if "\0" in value:
        raise HTTPException(status_code=400, detail="工作区路径包含非法字符")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="工作区路径必须是绝对路径")
    resolved = path.resolve(strict=False)
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法创建或访问工作区路径：{exc}") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="工作区路径必须是目录")
    return str(resolved)


def _prepare_user_workspace_root(raw: Any, *, required: bool) -> str | None:
    return _prepare_workspace_directory(
        raw,
        required=required,
        required_detail="普通用户必须配置工作区根路径",
    )


def _runtime_capability_overrides() -> dict[str, bool]:
    return {
        "can_restart_engine": True,
        "can_pick_folder": True,
        "can_open_logs": True,
        "can_export_diagnostics": True,
    }


def _restart_argv() -> list[str]:
    return [sys.executable, "-m", "my_agent", *sys.argv[1:]]


async def _restart_process_later(delay_s: float = 0.35) -> None:
    await asyncio.sleep(delay_s)
    os.execv(sys.executable, _restart_argv())


async def _safe_send(websocket: WebSocket, event: str, **payload: Any) -> None:
    await websocket.send_json({"event": event, **payload})


def _progress_event_from_tool_event(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    phase = str(item.get("phase") or item.get("event") or item.get("status") or "").lower()
    name = item.get("name") or item.get("tool") or item.get("tool_name") or item.get("display_name")
    payload = {"tool": name or "tool", "data": item}
    if phase in {"start", "started", "running"}:
        return "tool_start", payload
    if phase in {"end", "done", "finished", "complete", "completed", "error", "failed"}:
        return "tool_end", payload
    return "tool_event", payload


async def _call_stream_end(callback: Any, *, resuming: bool = False) -> None:
    if callback is None:
        return
    try:
        sig = inspect.signature(callback)
        if "resuming" in sig.parameters:
            await callback(resuming=resuming)
            return
    except (TypeError, ValueError):
        pass
    await callback()


_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)
_IMAGE_MIME_ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_VIDEO_MIME_ALLOWED = {"video/mp4", "video/webm", "video/quicktime"}
_UPLOAD_MIME_ALLOWED = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,128}$")


def _extract_data_url_mime(url: str) -> str | None:
    match = _DATA_URL_MIME_RE.match(url)
    return match.group(1).strip().lower() if match else None


def _valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.fullmatch(value) is not None


def _save_envelope_media(media: list[Any]) -> tuple[list[str], str | None]:
    image_count = 0
    video_count = 0
    for item in media:
        mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
        if mime in _VIDEO_MIME_ALLOWED:
            video_count += 1
        elif mime in _IMAGE_MIME_ALLOWED:
            image_count += 1
    if image_count > _MAX_IMAGES_PER_MESSAGE:
        return [], "too_many_images"
    if video_count > _MAX_VIDEOS_PER_MESSAGE:
        return [], "too_many_videos"

    media_dir = get_media_dir("websocket")
    paths: list[str] = []

    def abort(reason: str) -> tuple[list[str], str]:
        for item in paths:
            with suppress(OSError):
                Path(item).unlink(missing_ok=True)
        return [], reason

    for item in media:
        if not isinstance(item, dict):
            return abort("malformed")
        data_url = item.get("data_url")
        if not isinstance(data_url, str) or not data_url:
            return abort("malformed")
        mime = _extract_data_url_mime(data_url)
        if mime not in _UPLOAD_MIME_ALLOWED:
            return abort("mime")
        max_bytes = _MAX_VIDEO_BYTES if mime in _VIDEO_MIME_ALLOWED else _MAX_IMAGE_BYTES
        try:
            saved = save_base64_data_url(data_url, media_dir, max_bytes=max_bytes)
        except FileSizeExceeded:
            return abort("size")
        except Exception:
            logger.debug("media decode failed", exc_info=True)
            return abort("decode")
        if saved is None:
            return abort("decode")
        paths.append(saved)
    return paths, None


def _fallback_thread_from_session(session_key: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    ui_messages: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if isinstance(content, list):
            parts = [block.get("text", "") for block in content if isinstance(block, dict)]
            content = "\n".join(part for part in parts if isinstance(part, str))
        if not isinstance(content, str):
            content = ""
        timestamp = item.get("timestamp")
        created_at = int(time.time() * 1000)
        if isinstance(timestamp, str):
            with suppress(Exception):
                from datetime import datetime

                created_at = int(datetime.fromisoformat(timestamp).timestamp() * 1000)
        ui_messages.append(
            {
                "id": item.get("id") or uuid.uuid4().hex,
                "role": role,
                "content": content,
                "createdAt": created_at,
            }
        )
    if not ui_messages:
        return None
    return {
        "schemaVersion": 1,
        "sessionKey": session_key,
        "messages": ui_messages,
        "workspace_scope": (payload.get("metadata") or {}).get(WORKSPACE_SCOPE_METADATA_KEY),
    }


async def _handle_original_message(
    websocket: WebSocket,
    runtime: AgentRuntime,
    envelope: dict[str, Any],
    *,
    user: AuthUser,
) -> None:
    chat_id = envelope.get("chat_id")
    content = envelope.get("content")
    if not _valid_chat_id(chat_id):
        await _safe_send(websocket, "error", detail="invalid chat_id")
        return
    if not isinstance(content, str):
        await _safe_send(websocket, "error", chat_id=chat_id, detail="missing content")
        return

    raw_media = envelope.get("media")
    media_paths: list[str] = []
    if raw_media is not None:
        if not isinstance(raw_media, list):
            await _safe_send(websocket, "error", chat_id=chat_id, detail="image_rejected", reason="malformed")
            return
        media_paths, reason = _save_envelope_media(raw_media)
        if reason is not None:
            await _safe_send(websocket, "error", chat_id=chat_id, detail="image_rejected", reason=reason)
            return

    if not content.strip() and not media_paths:
        await _safe_send(websocket, "error", chat_id=chat_id, detail="missing content")
        return

    verdict = runtime.prompt_guardrails.inspect_direct_input(content)
    direct_scan = _prompt_guardrails_scan_payload(
        verdict,
        threshold=runtime.prompt_guardrails.config.threshold,
    )
    if not verdict.allowed:
        await _safe_send(
            websocket,
            "prompt_guardrails_scan",
            chat_id=chat_id,
            scans=[direct_scan],
            turn_id=envelope.get("turn_id"),
            turn_phase="activity",
        )
        await _safe_send(
            websocket,
            "error",
            chat_id=chat_id,
            detail="prompt_guardrails_blocked",
            source="direct",
            reason=_prompt_guardrails_block_reason(direct_scan),
            score=verdict.score,
        )
        return

    controller = runtime.workspace_controller(user)
    try:
        scope = controller.scope_for_message(
            envelope,
            chat_id=chat_id,
            chat_running=False,
            can_use_full_access=user.has("sandbox.host_exec"),
        )
    except WorkspaceScopeError as exc:
        await _safe_send(
            websocket,
            "error",
            chat_id=chat_id,
            detail="workspace_scope_rejected",
            reason=exc.message,
        )
        return
    controller.persist_scope(chat_id, scope)

    metadata: dict[str, Any] = {
        "webui": True,
        WORKSPACE_SCOPE_METADATA_KEY: scope.metadata(),
        **runtime.transcripts.client_turn_metadata(envelope.get("turn_id")),
    }
    cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
    if cli_apps:
        metadata["cli_apps"] = cli_apps
    mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
    if mcp_presets:
        metadata["mcp_presets"] = mcp_presets
    image_generation = envelope.get("image_generation")
    if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
        aspect_ratio = image_generation.get("aspect_ratio")
        metadata["image_generation"] = {
            "enabled": True,
            "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
        }

    runtime.transcripts.append_user_message(
        chat_id,
        content,
        metadata=metadata,
        media_paths=media_paths or None,
        cli_apps=cli_apps or None,
        mcp_presets=mcp_presets or None,
    )

    async def send_event(event: dict[str, Any]) -> None:
        await websocket.send_json(event)

    try:
        await _run_webui_agent_turn(
            runtime,
            content=content,
            session_key=_session_key(chat_id),
            channel="websocket",
            chat_id=chat_id,
            media=media_paths,
            metadata=metadata,
            direct_scan=direct_scan,
            send_event=send_event,
        )
    except Exception as exc:
        logger.exception("WebUI WebSocket chat turn failed")
        await _safe_send(websocket, "error", chat_id=chat_id, detail=str(exc))


async def _handle_legacy_chat_send(
    websocket: WebSocket,
    runtime: AgentRuntime,
    message: dict[str, Any],
    *,
    user: AuthUser,
) -> None:
    loop = runtime.require_loop()
    raw_session = str(message.get("sessionId") or _session_key(_chat_id()))
    channel, chat_id = _split_session_key(raw_session)
    session_key = raw_session if ":" in raw_session else f"{channel}:{chat_id}"
    content = str(message.get("content") or "").strip()
    media = message.get("media") if isinstance(message.get("media"), list) else []
    if not content and not media:
        await _safe_send(websocket, "error", message="Message content is empty.")
        return

    verdict = runtime.prompt_guardrails.inspect_direct_input(content)
    direct_scan = _prompt_guardrails_scan_payload(
        verdict,
        threshold=runtime.prompt_guardrails.config.threshold,
    )
    if not verdict.allowed:
        await _safe_send(
            websocket,
            "prompt_guardrails_scan",
            chat_id=chat_id,
            sessionId=session_key,
            scans=[direct_scan],
        )
        await _safe_send(
            websocket,
            "error",
            sessionId=session_key,
            detail="prompt_guardrails_blocked",
            source="direct",
            reason=_prompt_guardrails_block_reason(direct_scan),
            score=verdict.score,
        )
        return

    controller = runtime.workspace_controller(user)
    try:
        scope = controller.scope_for_message(
            message,
            chat_id=chat_id,
            chat_running=False,
            can_use_full_access=user.has("sandbox.host_exec"),
        )
    except WorkspaceScopeError as exc:
        await _safe_send(
            websocket,
            "error",
            sessionId=session_key,
            detail="workspace_scope_rejected",
            reason=exc.message,
        )
        return
    controller.persist_scope(chat_id, scope)
    metadata = {
        "webui": True,
        WORKSPACE_SCOPE_METADATA_KEY: scope.metadata(),
    }

    await _safe_send(websocket, "user_message", sessionId=session_key, content=content, media=media)
    if direct_scan.get("reason") != "disabled":
        await _safe_send(
            websocket,
            "prompt_guardrails_scan",
            chat_id=chat_id,
            sessionId=session_key,
            scans=[direct_scan],
        )
        await _safe_send(
            websocket,
            "progress",
            sessionId=session_key,
            content="等待模型响应",
        )
    chunks: list[str] = []

    async def on_stream(delta: str) -> None:
        chunks.append(delta)
        await _safe_send(websocket, "assistant_delta", sessionId=session_key, delta=delta)

    async def on_stream_end(*, resuming: bool = False) -> None:
        await _safe_send(websocket, "assistant_stream_end", sessionId=session_key, resuming=resuming)

    async def on_progress(
        progress: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        prompt_guardrails: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        if reasoning:
            await _safe_send(websocket, "reasoning_delta", sessionId=session_key, delta=progress)
        elif reasoning_end:
            await _safe_send(websocket, "reasoning_end", sessionId=session_key)
        elif tool_hint:
            await _safe_send(websocket, "tool_hint", sessionId=session_key, content=progress)
        elif progress:
            await _safe_send(websocket, "progress", sessionId=session_key, content=progress)
        prompt_scans = _complete_prompt_guardrails_scans(prompt_guardrails, runtime=runtime)
        if prompt_scans:
            await _safe_send(
                websocket,
                "prompt_guardrails_scan",
                chat_id=chat_id,
                sessionId=session_key,
                scans=prompt_scans,
            )
            blocked_scan = _blocked_prompt_guardrails_scan(prompt_scans)
            if blocked_scan is not None:
                await _safe_send(
                    websocket,
                    "error",
                    sessionId=session_key,
                    chat_id=chat_id,
                    detail="prompt_guardrails_blocked",
                    source=_prompt_guardrails_block_source(blocked_scan),
                    reason=_prompt_guardrails_block_reason(blocked_scan),
                    score=blocked_scan.get("score"),
                )
        for item in tool_events or []:
            event, payload = _progress_event_from_tool_event(item)
            await _safe_send(websocket, event, sessionId=session_key, **payload)
        for item in file_edit_events or []:
            await _safe_send(websocket, "file_edit_event", sessionId=session_key, data=item)

    try:
        outbound = await loop.process_direct(
            content,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            media=[str(item) for item in media if isinstance(item, str)],
            metadata=metadata,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
        final = outbound.content if outbound is not None else "".join(chunks)
        guardrail_stop_reason = _action_guardrails_budget_stop(outbound)
        if guardrail_stop_reason:
            await _safe_send(
                websocket,
                "error",
                sessionId=session_key,
                chat_id=chat_id,
                detail="action_guardrails_budget_stopped",
                reason=guardrail_stop_reason,
            )
        if final and not chunks:
            await _safe_send(websocket, "assistant_delta", sessionId=session_key, delta=final)
            await _call_stream_end(on_stream_end)
        await _safe_send(
            websocket,
            "turn_end",
            sessionId=session_key,
            content=final,
            sessions=loop.sessions.list_sessions(),
        )
    except asyncio.CancelledError:
        with suppress(Exception):
            await _safe_send(
                websocket,
                "turn_end",
                sessionId=session_key,
                content="",
                stopped=True,
                sessions=loop.sessions.list_sessions(),
            )
    except Exception as exc:
        logger.exception("WebSocket chat turn failed")
        await _safe_send(websocket, "error", sessionId=session_key, message=str(exc))


async def _settings_mutation(
    runtime: AgentRuntime,
    request: Request,
    func: Any,
    *,
    reload_runtime: bool = True,
) -> dict[str, Any]:
    _require_permission(runtime, request, "settings.manage")
    try:
        payload = func(_query_params(request))
    except WebUISettingsError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    if reload_runtime:
        await runtime.reload()
    return decorate_settings_payload(
        payload,
        runtime_capability_overrides=_runtime_capability_overrides(),
    )


def create_app(config_path: str | Path | None = None) -> FastAPI:
    resolved_config_path = Path(config_path).expanduser() if config_path else None
    runtime = AgentRuntime(resolved_config_path)
    app = FastAPI(title="NimbusOps", version="0.1.0")
    app.state.runtime = runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        await runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        user = _browser_user(runtime, request)
        return {
            "setup_required": runtime.users.setup_required(),
            "authenticated": user is not None,
            "user": user.payload() if user else None,
        }

    @app.post("/api/auth/setup")
    async def auth_setup(request: Request, response: Response) -> dict[str, Any]:
        if not runtime.users.setup_required():
            raise HTTPException(status_code=409, detail="管理员已经初始化")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        try:
            user = runtime.users.create(
                username=str(body.get("username") or ""),
                password=str(body.get("password") or ""),
                display_name=str(body.get("display_name") or ""),
                initial_setup=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = runtime.login_sessions.issue(user)
        _set_login_cookie(request, response, token)
        return {"user": user.payload()}

    @app.post("/api/auth/login")
    async def auth_login(request: Request, response: Response) -> dict[str, Any]:
        if runtime.users.setup_required():
            raise HTTPException(status_code=409, detail="请先初始化系统管理员")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        user = runtime.users.authenticate(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
        )
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = runtime.login_sessions.issue(user)
        _set_login_cookie(request, response, token)
        return {"user": user.payload()}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, response: Response) -> dict[str, bool]:
        runtime.login_sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def auth_me(request: Request) -> dict[str, Any]:
        return {"user": _require_browser_user(runtime, request).payload()}

    @app.put("/api/auth/password")
    async def auth_change_password(request: Request) -> dict[str, bool]:
        user = _require_browser_user(runtime, request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        authenticated = runtime.users.authenticate(
            user.username,
            str(body.get("current_password") or ""),
        )
        if authenticated is None:
            raise HTTPException(status_code=400, detail="当前密码不正确")
        try:
            runtime.users.update(
                user.id,
                {"password": str(body.get("new_password") or "")},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/auth/users")
    async def auth_users(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "users.manage")
        return {
            "users": [user.payload() for user in runtime.users.list_users()],
            "permissions": list(KNOWN_PERMISSIONS),
        }

    @app.post("/api/auth/users")
    async def auth_create_user(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "users.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        role = str(body.get("role") or "user")
        workspace_root = _prepare_user_workspace_root(
            body.get("workspace_root"),
            required=role != "system_admin",
        )
        try:
            user = runtime.users.create(
                username=str(body.get("username") or ""),
                password=str(body.get("password") or ""),
                display_name=str(body.get("display_name") or ""),
                role=role,
                permissions=body.get("permissions"),
                workspace_root=workspace_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.payload()}

    @app.put("/api/auth/users/{user_id}")
    async def auth_update_user(request: Request, user_id: str) -> dict[str, Any]:
        _require_permission(runtime, request, "users.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        next_body = dict(body)
        if "workspace_root" in next_body:
            current_user = runtime.users.find_by_id(user_id)
            role = str(next_body.get("role") or (current_user.role if current_user else "user"))
            next_body["workspace_root"] = _prepare_user_workspace_root(
                next_body.get("workspace_root"),
                required=role != "system_admin",
            )
        try:
            user = runtime.users.update(user_id, next_body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.payload()}

    @app.delete("/api/auth/users/{user_id}")
    async def auth_delete_user(request: Request, user_id: str) -> dict[str, bool]:
        actor = _require_permission(runtime, request, "users.manage")
        try:
            deleted = runtime.users.delete(user_id, actor_id=actor.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="用户不存在")
        return {"deleted": True}

    @app.get("/webui/bootstrap")
    async def webui_bootstrap(request: Request) -> dict[str, Any]:
        user = _require_browser_user(runtime, request)
        ttl = 3600
        token = runtime.issue_webui_token(user, ttl)
        loop = runtime.require_loop()
        return {
            "token": token,
            "expires_in": ttl,
            "ws_path": "/ws",
            "ws_url": None,
            "model_name": loop.model,
            "user": user.payload(),
        }

    @app.get("/api/config")
    async def get_config(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "settings.manage")
        return _config_payload(runtime.require_config())

    @app.put("/api/config")
    async def put_config(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "settings.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Config payload must be an object.")
        partial = request.query_params.get("partial", "").lower() in {"1", "true", "yes"}
        config = await runtime.replace_config(body, partial=partial)
        return _config_payload(config)

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        sessions = []
        for row in runtime.require_loop().sessions.list_sessions():
            item = dict(row)
            metadata: dict[str, Any] = {}
            raw = runtime.require_loop().sessions.read_session_file(str(item.get("key") or ""))
            if isinstance(raw, dict) and isinstance(raw.get("metadata"), dict):
                metadata = raw["metadata"]
            owner_id = metadata.get(AUTH_USER_METADATA_KEY)
            if owner_id != user.id and not user.has("sessions.read_all"):
                continue
            item["run_started_at"] = None
            item["workspace_scope"] = metadata.get(WORKSPACE_SCOPE_METADATA_KEY)
            sessions.append(item)
        return {"sessions": sessions}

    @app.get("/api/audit/traces")
    async def list_audit_traces_route(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        loop = runtime.require_loop()
        rows = [
            _audit_session_row(runtime, row)
            for row in loop.sessions.list_sessions()
            if _session_owned_by(runtime, user, str(row.get("key") or ""))
        ]
        privileged_records = (
            runtime.privileged.audit_payload().get("records", [])
            if user.has("privileged.audit")
            else []
        )
        return await asyncio.to_thread(
            list_audit_traces,
            loop.sessions,
            rows,
            privileged_records,
        )

    @app.get("/api/audit/traces/{trace_id}")
    async def get_audit_trace_route(
        request: Request,
        trace_id: str,
        session_key: str,
    ) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        if not _session_owned_by(runtime, user, session_key):
            raise HTTPException(status_code=404, detail="Not found")
        loop = runtime.require_loop()
        row = _audit_session_row(
            runtime,
            next(
                (
                    item
                    for item in loop.sessions.list_sessions()
                    if str(item.get("key") or "") == session_key
                ),
                {"key": session_key},
            ),
        )
        privileged_records = (
            runtime.privileged.audit_payload().get("records", [])
            if user.has("privileged.audit")
            else []
        )
        trace = await asyncio.to_thread(
            get_audit_trace,
            loop.sessions,
            row,
            trace_id,
            privileged_records,
        )
        if trace is None:
            raise HTTPException(status_code=404, detail="Not found")
        return trace

    @app.get("/api/audit/traces/{trace_id}/export")
    async def export_audit_trace_route(
        request: Request,
        trace_id: str,
        session_key: str,
    ) -> Response:
        trace = await get_audit_trace_route(request, trace_id, session_key)
        filename = f"nimbusops-audit-{safe_filename(trace_id)}.json"
        return Response(
            content=json.dumps(trace, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/sessions/{session_id:path}/webui-thread")
    async def get_webui_thread(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        loop = runtime.require_loop()
        media = runtime.require_media()
        payload = loop.sessions.read_session_file(session_id)
        thread = build_webui_thread_response(
            session_id,
            augment_user_media=media.augment_transcript_user_media,
            augment_assistant_media=media.augment_transcript_media,
            augment_assistant_text=media.rewrite_local_markdown_images,
            session_messages=payload.get("messages") if isinstance(payload, dict) else None,
        )
        if thread is None:
            thread = _fallback_thread_from_session(session_id, payload)
        if thread is None:
            raise HTTPException(status_code=404, detail="Not found")
        if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
            scope = payload["metadata"].get(WORKSPACE_SCOPE_METADATA_KEY)
            if scope is not None:
                thread["workspace_scope"] = scope
        return thread

    @app.get("/api/sessions/{session_id:path}/file-preview")
    async def get_file_preview(request: Request, session_id: str) -> dict[str, Any]:
        user = _require_session_access(runtime, request, session_id)
        scope = runtime.workspace_controller(user).scope_for_session_key(
            session_id,
            can_use_full_access=user.has("sandbox.host_exec"),
        )
        try:
            return file_preview_payload(request.query_params.get("path"), scope=scope)
        except WebUIFilePreviewError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/sessions/{session_id:path}/automations")
    async def get_session_automations(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        return session_automations_payload(runtime.require_loop().cron_service, session_id)

    @app.api_route("/api/sessions/{session_id:path}/automations/create", methods=["GET", "POST"])
    async def create_session_automation(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        raw_payload = request.query_params.get("payload")
        if raw_payload is None:
            raise HTTPException(status_code=400, detail="missing payload")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="payload must be JSON") from exc
        try:
            create_session_automation_job(runtime.require_loop().cron_service, session_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return session_automations_payload(runtime.require_loop().cron_service, session_id)

    @app.get("/api/sessions/{session_id:path}/delete")
    async def delete_session_get(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        deleted = runtime.require_loop().sessions.delete_session(session_id)
        if delete_webui_thread(session_id):
            deleted = True
        return {"deleted": deleted}

    @app.delete("/api/sessions/{session_id:path}")
    async def delete_session(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        deleted = runtime.require_loop().sessions.delete_session(session_id)
        if delete_webui_thread(session_id):
            deleted = True
        return {"deleted": deleted}

    @app.get("/api/sessions/{session_id:path}")
    async def get_session(request: Request, session_id: str) -> dict[str, Any]:
        _require_session_access(runtime, request, session_id)
        loop = runtime.require_loop()
        payload = loop.sessions.read_session_file(session_id)
        if payload is None:
            session = loop.sessions.get_or_create(session_id)
            return {
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "messages": session.messages,
            }
        return payload

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return settings_payload(runtime_capability_overrides=_runtime_capability_overrides())

    @app.get("/api/guardrails/action")
    async def get_action_guardrails(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        return _action_guardrails_payload(runtime, user)

    @app.put("/api/guardrails/action")
    async def update_action_guardrails(request: Request) -> dict[str, Any]:
        user = _require_permission(runtime, request, "settings.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Action Guardrails payload must be an object.",
            )
        config = load_config(runtime.config_path)
        if "requiredStepScopes" not in body and "required_step_scopes" not in body:
            body = dict(body)
            body["requiredStepScopes"] = [
                item.model_dump(mode="json", by_alias=True)
                for item in config.guardrails.action.required_step_scopes
            ]
        try:
            action_config = ActionGuardrailsConfig.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        config.guardrails.action = action_config
        save_config(config, runtime.config_path)
        await runtime.reload()
        return _action_guardrails_payload(runtime, user)

    @app.get("/api/guardrails/prompt")
    async def get_prompt_guardrails(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return _prompt_guardrails_payload(runtime)

    @app.put("/api/guardrails/prompt")
    async def update_prompt_guardrails(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "settings.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Prompt Guardrails payload must be an object.",
            )
        config = load_config(runtime.config_path)
        merged_body = config.guardrails.prompt.model_dump(mode="json", by_alias=True)
        merged_body.update(body)
        try:
            prompt_config = PromptGuardrailsConfig.model_validate(merged_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        config.guardrails.prompt = prompt_config.model_copy(update={"enabled": False})
        save_config(config, runtime.config_path)
        runtime._prompt_guardrails_runtime_config = prompt_config.model_copy(deep=True)
        await runtime.reload()
        if prompt_config.enabled:
            with suppress(Exception):
                runtime.prompt_guardrails.ensure_loaded()
        return _prompt_guardrails_payload(runtime)

    @app.get("/api/runtime/info")
    async def runtime_info_route(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return runtime_info(runtime.config_path, runtime.config)

    @app.api_route("/api/runtime/restart", methods=["GET", "POST"])
    async def restart_runtime_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "runtime.manage")
        asyncio.create_task(_restart_process_later())
        return {"ok": True, "restarting": True}

    @app.api_route("/api/runtime/open-logs", methods=["GET", "POST"])
    async def open_logs_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "runtime.manage")
        return await asyncio.to_thread(open_logs)

    @app.api_route("/api/runtime/export-diagnostics", methods=["GET", "POST"])
    async def export_diagnostics_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "runtime.manage")
        return await asyncio.to_thread(
            export_diagnostics,
            config_path=runtime.config_path,
            config=runtime.config,
            extra={
                "sessions": runtime.require_loop().sessions.list_sessions(),
                "cron": runtime.cron_service.status(),
            },
        )

    @app.api_route("/api/runtime/pick-folder", methods=["GET", "POST"])
    async def pick_folder_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "runtime.manage")
        return await asyncio.to_thread(pick_folder)

    @app.get("/api/settings/usage")
    async def get_settings_usage(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return settings_usage_payload()

    @app.get("/api/settings/update")
    async def update_settings_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_agent_settings)

    @app.get("/api/settings/model-configurations/create")
    async def create_model_configuration_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, create_model_configuration)

    @app.get("/api/settings/model-configurations/update")
    async def update_model_configuration_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_model_configuration)

    @app.get("/api/settings/provider/update")
    async def update_provider_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_provider_settings)

    @app.get("/api/settings/provider-models")
    async def provider_models_route(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        try:
            return await asyncio.to_thread(provider_models_payload, _query_params(request))
        except WebUISettingsError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/settings/provider/oauth-login")
    async def provider_oauth_login_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, login_oauth_provider)

    @app.get("/api/settings/provider/oauth-logout")
    async def provider_oauth_logout_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, logout_oauth_provider)

    @app.get("/api/settings/web-search/update")
    async def update_web_search_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_web_search_settings)

    @app.get("/api/settings/network-safety/update")
    async def update_network_safety_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_network_safety_settings)

    @app.get("/api/settings/image-generation/update")
    async def update_image_generation_route(request: Request) -> dict[str, Any]:
        return await _settings_mutation(runtime, request, update_image_generation_settings)

    @app.get("/api/settings/cli-apps")
    async def get_cli_apps(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return cli_apps_payload(
            installed_only=request.query_params.get("catalog") != "1"
        )

    @app.get("/api/settings/builtin-tools")
    async def get_builtin_tools(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return builtin_tools_payload(runtime.require_loop().tools)

    @app.post("/api/settings/cli-apps/import")
    async def import_cli_app_archive_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "tools.install")
        try:
            return cli_apps_import_archive(
                await request.body(),
                filename=_uploaded_archive_filename(request),
            )
        except CliAppError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc)) from exc

    @app.get("/api/settings/cli-apps/{action}")
    async def cli_apps_action_route(request: Request, action: str) -> dict[str, Any]:
        _require_permission(runtime, request, "tools.install")
        try:
            return cli_apps_action(action, _query_params(request))
        except CliAppError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc)) from exc

    async def reload_mcp() -> dict[str, Any]:
        await runtime.reload()
        return {
            "ok": True,
            "message": "MCP configuration reloaded.",
            "requires_restart": False,
        }

    @app.get("/api/settings/mcp-presets")
    async def get_mcp_presets(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return await mcp_presets_settings_action(None, _mcp_query_params(request), reload_mcp=reload_mcp)

    @app.get("/api/settings/mcp-presets/{action}")
    async def mcp_presets_action_route(request: Request, action: str) -> dict[str, Any]:
        _require_permission(runtime, request, "tools.install")
        try:
            return await mcp_presets_settings_action(action, _mcp_query_params(request), reload_mcp=reload_mcp)
        except McpPresetError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.post("/api/settings/mcp-presets/import-archive")
    async def import_mcp_archive_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "tools.install")
        try:
            payload = await asyncio.to_thread(
                import_mcp_archive_action,
                await request.body(),
                _uploaded_archive_filename(request),
            )
            return attach_mcp_hot_reload_result(payload, await reload_mcp())
        except McpPresetError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/commands")
    async def commands_route(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        return {"commands": builtin_command_palette()}

    @app.get("/api/webui/sidebar-state")
    async def sidebar_state_route(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        return read_webui_sidebar_state(user.id)

    @app.get("/api/webui/sidebar-state/update")
    async def sidebar_state_update_route(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        raw = request.query_params.get("state", "{}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid sidebar state") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="sidebar state must be an object")
        return write_webui_sidebar_state(payload, user.id)

    @app.get("/api/webui/skills")
    async def skills_route(request: Request) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        config = runtime.require_config()
        return webui_skills_payload(
            config.workspace_path,
            disabled_skills=set(config.agents.defaults.disabled_skills),
        )

    @app.post("/api/webui/skills/import")
    async def import_skill_archive_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "skills.install")
        config = runtime.require_config()
        try:
            return webui_import_skill_archive_payload(
                config.workspace_path,
                await request.body(),
                filename=_uploaded_archive_filename(request),
                disabled_skills=set(config.agents.defaults.disabled_skills),
            )
        except WebUISkillImportError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/webui/skills/{name}")
    async def skill_detail_route(request: Request, name: str) -> dict[str, Any]:
        _require_webui_token(runtime, request)
        config = runtime.require_config()
        payload = webui_skill_detail_payload(
            config.workspace_path,
            name,
            disabled_skills=set(config.agents.defaults.disabled_skills),
        )
        if payload is None:
            raise HTTPException(status_code=404, detail="Not found")
        return payload

    @app.get("/api/workspaces")
    async def workspaces_route(request: Request) -> dict[str, Any]:
        user = _require_webui_token(runtime, request)
        return runtime.workspace_controller(user).payload(
            can_use_full_access=user.has("sandbox.host_exec"),
        )

    @app.get("/api/security/sandbox")
    async def sandbox_status_route(request: Request) -> dict[str, Any]:
        user = _require_permission(runtime, request, "sandbox.view")
        return _sandbox_payload(runtime, user)

    @app.put("/api/security/sandbox")
    async def update_sandbox_route(request: Request) -> dict[str, Any]:
        user = _require_permission(runtime, request, "sandbox.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        backend = str(body.get("backend") or "").strip()
        if backend not in {"", "auto", "bwrap", "sandbox-exec", "seatbelt"}:
            raise HTTPException(status_code=400, detail="不支持的沙箱后端")
        config = runtime.require_config()
        config.tools.exec.sandbox = backend
        if isinstance(body.get("restrict_to_workspace"), bool):
            config.tools.restrict_to_workspace = body["restrict_to_workspace"]
        if "workspace_path" in body:
            workspace_path = _prepare_workspace_directory(
                body.get("workspace_path"),
                required=True,
                required_detail="必须配置当前工作区路径",
            )
            if workspace_path is not None:
                config.agents.defaults.workspace = workspace_path
        save_config(config, runtime.config_path)
        await runtime.reload()
        return _sandbox_payload(runtime, user)

    @app.get("/api/security/runtime-identity")
    async def runtime_identity_status_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "sandbox.view")
        config = runtime.require_config()
        return runtime_identity_payload(config.security.runtime_identity)

    @app.put("/api/security/runtime-identity")
    async def update_runtime_identity_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "sandbox.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        config = runtime.require_config()
        current = config.security.runtime_identity.model_dump(mode="python")
        try:
            next_identity = RuntimeIdentityConfig.model_validate(
                _deep_merge(current, body)
            )
            validate_runtime_identity(next_identity)
        except (ValidationError, ValueError) as exc:
            detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=422, detail=detail) from exc
        config.security.runtime_identity = next_identity
        save_config(config, runtime.config_path)
        await runtime.reload()
        return runtime_identity_payload(next_identity)

    @app.get("/api/security/privileged-execution")
    async def privileged_execution_status_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "sandbox.view")
        return await runtime.privileged.payload()

    @app.put("/api/security/privileged-execution")
    async def update_privileged_execution_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "sandbox.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求格式不正确")
        config = runtime.require_config()
        current = config.security.privileged_execution.model_dump(mode="python")
        try:
            next_config = PrivilegedExecutionConfig.model_validate(
                _deep_merge(current, body)
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        socket_path = Path(next_config.socket_path).expanduser()
        if not socket_path.is_absolute():
            raise HTTPException(status_code=422, detail="特权执行器 Socket 必须是绝对路径")
        config.security.privileged_execution = next_config
        save_config(config, runtime.config_path)
        await runtime.reload()
        return await runtime.privileged.payload()

    @app.get("/api/security/privileged-execution/audit")
    async def privileged_execution_audit_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "privileged.audit")
        return runtime.privileged.audit_payload()

    @app.get("/api/security/privileged-execution/policy")
    async def privileged_execution_policy_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "privileged.approve")
        try:
            return await runtime.privileged.policy_payload()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/security/privileged-execution/policy/validate")
    async def validate_privileged_execution_policy_route(
        request: Request,
    ) -> dict[str, Any]:
        _require_permission(runtime, request, "privileged.approve")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="策略必须是 JSON 对象")
        try:
            return await runtime.privileged.validate_policy(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/security/privileged-execution/actions")
    async def add_privileged_execution_action_route(
        request: Request,
    ) -> dict[str, Any]:
        actor = _require_permission(runtime, request, "privileged.approve")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="动作必须是 JSON 对象")
        action_name = str(body.get("name") or "").strip()
        action = body.get("action")
        if not action_name or not isinstance(action, dict):
            raise HTTPException(status_code=400, detail="动作名称和动作定义不能为空")
        try:
            result = await runtime.privileged.add_action(
                action_name=action_name,
                action=action,
                actor=actor,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            **result,
            "execution": await runtime.privileged.payload(),
        }

    @app.get("/api/security/agentspec")
    async def agentspec_payload_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.view")
        return runtime.agentspec.payload()

    @app.put("/api/security/agentspec")
    async def update_agentspec_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be boolean")
        config = runtime.require_config()
        config.security.agentspec.enabled = body["enabled"]
        save_config(config, runtime.config_path)
        await runtime.reload()
        return runtime.agentspec.payload()

    @app.post("/api/security/agentspec/rules")
    async def create_agentspec_rule_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Rule payload must be an object")
        text = str(body.get("text") or "").strip()
        filename = str(body.get("filename") or "").strip() or None
        if not text:
            raise HTTPException(status_code=400, detail="Rule text is required")
        try:
            runtime.agentspec.save_rule(text, filename=filename)
        except AgentSpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.agentspec.payload()

    @app.put("/api/security/agentspec/rules/{rule_id}/enabled")
    async def set_agentspec_rule_enabled_route(
        request: Request,
        rule_id: str,
    ) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be boolean")
        if not any(rule.id == rule_id for rule in runtime.agentspec.rules):
            raise HTTPException(status_code=404, detail="Rule not found")
        runtime.agentspec.set_enabled(rule_id, body["enabled"])
        return runtime.agentspec.payload()

    @app.put("/api/security/agentspec/rules/{rule_id}")
    async def update_agentspec_rule_route(
        request: Request,
        rule_id: str,
    ) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Rule payload must be an object")
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Rule text is required")
        try:
            runtime.agentspec.update_rule(rule_id, text)
        except AgentSpecError as exc:
            status = 404 if str(exc) == "Custom rule not found" else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return runtime.agentspec.payload()

    @app.delete("/api/security/agentspec/rules/{rule_id}")
    async def delete_agentspec_rule_route(request: Request, rule_id: str) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        if not runtime.agentspec.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="Custom rule not found")
        return runtime.agentspec.payload()

    @app.post("/api/security/agentspec/predicates")
    async def create_agentspec_predicate_route(request: Request) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Predicate payload must be an object")
        source = str(body.get("source") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="Predicate source is required")
        try:
            runtime.agentspec.save_predicate(source)
        except AgentSpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.agentspec.payload()

    @app.put("/api/security/agentspec/predicates/{predicate_name}")
    async def update_agentspec_predicate_route(
        request: Request,
        predicate_name: str,
    ) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Predicate payload must be an object")
        source = str(body.get("source") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="Predicate source is required")
        if not any(
            item["name"] == predicate_name and item["source_kind"] == "custom"
            for item in runtime.agentspec.payload()["predicates"]
        ):
            raise HTTPException(status_code=404, detail="Custom Predicate not found")
        try:
            runtime.agentspec.save_predicate(source, expected_name=predicate_name)
        except AgentSpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.agentspec.payload()

    @app.delete("/api/security/agentspec/predicates/{predicate_name}")
    async def delete_agentspec_predicate_route(
        request: Request,
        predicate_name: str,
    ) -> dict[str, Any]:
        _require_permission(runtime, request, "ops_shield.manage")
        try:
            deleted = runtime.agentspec.delete_predicate(predicate_name)
        except AgentSpecError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Custom Predicate not found")
        return runtime.agentspec.payload()

    @app.get("/api/media/{sig}/{payload}")
    async def media_route(sig: str, payload: str) -> Response:
        media = runtime.require_media()
        try:
            provided_mac = _b64url_decode(sig)
            rel_str = _b64url_decode(payload).decode("utf-8")
        except Exception as exc:
            raise HTTPException(status_code=401, detail="invalid media signature") from exc
        expected_mac = hmac.new(media.secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            raise HTTPException(status_code=401, detail="invalid media signature")
        try:
            media_root = get_media_dir(None).resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="not found") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not found")
        mime, _ = mimetypes.guess_type(candidate.name)
        return FileResponse(candidate, media_type=mime or "application/octet-stream")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        user = runtime.ws_user(token)
        if user is None or not user.enabled:
            await websocket.close(code=1008)
            return
        user_context = bind_current_user(user)
        await websocket.accept()
        default_chat_id = _chat_id()
        await _safe_send(
            websocket,
            "ready",
            chat_id=default_chat_id,
            client_id=f"web-{uuid.uuid4().hex[:12]}",
            user=user.payload(),
        )
        active_turns: dict[str, asyncio.Task[None]] = {}
        inspection_notifiers: dict[str, Any] = {}
        privilege_notifiers: dict[str, Any] = {}
        attached_chat_ids: set[str] = set()

        def can_access_chat(chat_id: str, *, claim_missing: bool = False) -> bool:
            return _session_owned_by(
                runtime,
                user,
                _session_key(chat_id),
                claim_missing=claim_missing,
            )

        def claim_chat(chat_id: str) -> None:
            _claim_session(runtime, user, _session_key(chat_id))

        def attach_chat_socket(chat_id: str) -> None:
            if not _valid_chat_id(chat_id):
                return
            attached_chat_ids.add(chat_id)
            runtime.attach_webui_socket(chat_id, websocket)

        def bind_inspection_notifier(chat_id: str) -> None:
            session_key = _session_key(chat_id)
            if session_key in inspection_notifiers:
                return

            async def notify(payload: dict[str, Any]) -> None:
                await _safe_send(
                    websocket,
                    "security_inspection",
                    chat_id=chat_id,
                    **payload,
                )

            inspection_notifiers[session_key] = notify
            runtime.agentspec.bind_notifier(session_key, notify)

        def bind_privilege_notifier(chat_id: str) -> None:
            session_key = _session_key(chat_id)
            if session_key in privilege_notifiers:
                return

            async def notify(payload: dict[str, Any]) -> None:
                await _safe_send(
                    websocket,
                    "privileged_action_approval",
                    chat_id=chat_id,
                    **payload,
                )

            privilege_notifiers[session_key] = notify
            runtime.privileged.bind_notifier(session_key, notify)

        def bind_approval_notifiers(chat_id: str) -> None:
            bind_inspection_notifier(chat_id)
            bind_privilege_notifier(chat_id)

        async def start_turn(chat_id: str, turn: Any) -> None:
            active = active_turns.get(chat_id)
            if active is not None and not active.done():
                await _safe_send(
                    websocket,
                    "error",
                    chat_id=chat_id,
                    detail="chat_running",
                    reason="This conversation is already processing a message.",
                )
                turn.close()
                return

            task = asyncio.create_task(turn)
            active_turns[chat_id] = task

            def clear_finished(done: asyncio.Task[None]) -> None:
                if active_turns.get(chat_id) is done:
                    active_turns.pop(chat_id, None)

            task.add_done_callback(clear_finished)

        async def stop_turn(chat_id: str) -> None:
            task = active_turns.get(chat_id)
            cancelled = task is not None and not task.done()
            if cancelled:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            with suppress(Exception):
                await runtime.require_loop().subagents.cancel_by_session(_session_key(chat_id))
            if not cancelled:
                await _safe_send(
                    websocket,
                    "turn_end",
                    chat_id=chat_id,
                    latency_ms=0,
                    stopped=True,
                )

        try:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    await _safe_send(websocket, "error", detail="WebSocket payload must be an object.")
                    continue
                kind = str(message.get("type") or message.get("event") or "")
                if kind == "new_chat":
                    chat_id = _chat_id()
                    bind_approval_notifiers(chat_id)
                    controller = runtime.workspace_controller(user)
                    scope = controller.scope_for_new_chat(
                        message,
                        can_use_full_access=user.has("sandbox.host_exec"),
                    )
                    controller.persist_scope(chat_id, scope)
                    claim_chat(chat_id)
                    attach_chat_socket(chat_id)
                    await _safe_send(websocket, "attached", chat_id=chat_id)
                    await _safe_send(
                        websocket,
                        "session_updated",
                        chat_id=chat_id,
                        scope="metadata",
                        workspace_scope=scope.payload(),
                    )
                elif kind == "attach":
                    chat_id = message.get("chat_id")
                    if not _valid_chat_id(chat_id):
                        await _safe_send(websocket, "error", detail="invalid chat_id")
                        continue
                    if not can_access_chat(chat_id):
                        await _safe_send(websocket, "error", detail="session_not_found")
                        continue
                    bind_approval_notifiers(chat_id)
                    attach_chat_socket(chat_id)
                    await _safe_send(websocket, "attached", chat_id=chat_id)
                elif kind == "set_workspace_scope":
                    chat_id = message.get("chat_id")
                    if not _valid_chat_id(chat_id):
                        await _safe_send(websocket, "error", detail="invalid chat_id")
                        continue
                    if not can_access_chat(chat_id, claim_missing=True):
                        await _safe_send(websocket, "error", detail="session_not_found")
                        continue
                    try:
                        controller = runtime.workspace_controller(user)
                        scope = controller.scope_for_set_request(
                            message,
                            chat_id=chat_id,
                            chat_running=(
                                chat_id in active_turns and not active_turns[chat_id].done()
                            ),
                            can_use_full_access=user.has("sandbox.host_exec"),
                        )
                    except WorkspaceScopeError as exc:
                        await _safe_send(
                            websocket,
                            "error",
                            chat_id=chat_id,
                            detail="workspace_scope_rejected",
                            reason=exc.message,
                        )
                        continue
                    controller.persist_scope(chat_id, scope)
                    claim_chat(chat_id)
                    await _safe_send(
                        websocket,
                        "session_updated",
                        chat_id=chat_id,
                        scope="metadata",
                        workspace_scope=scope.payload(),
                    )
                elif kind == "message":
                    chat_id = message.get("chat_id")
                    if not _valid_chat_id(chat_id):
                        await _safe_send(websocket, "error", detail="invalid chat_id")
                        continue
                    if not can_access_chat(chat_id, claim_missing=True):
                        await _safe_send(websocket, "error", detail="session_not_found")
                        continue
                    claim_chat(chat_id)
                    bind_approval_notifiers(chat_id)
                    attach_chat_socket(chat_id)
                    await start_turn(
                        chat_id,
                        _handle_original_message(
                            websocket,
                            runtime,
                            message,
                            user=user,
                        ),
                    )
                elif kind == "stop":
                    chat_id = message.get("chat_id")
                    if not _valid_chat_id(chat_id):
                        await _safe_send(websocket, "error", detail="invalid chat_id")
                        continue
                    await stop_turn(chat_id)
                elif kind == "chat.send":
                    raw_session = str(message.get("sessionId") or _session_key(default_chat_id))
                    _, chat_id = _split_session_key(raw_session)
                    if not _valid_chat_id(chat_id):
                        await _safe_send(websocket, "error", detail="invalid chat_id")
                        continue
                    if not can_access_chat(chat_id, claim_missing=True):
                        await _safe_send(websocket, "error", detail="session_not_found")
                        continue
                    claim_chat(chat_id)
                    bind_approval_notifiers(chat_id)
                    attach_chat_socket(chat_id)
                    await start_turn(
                        chat_id,
                        _handle_legacy_chat_send(
                            websocket,
                            runtime,
                            message,
                            user=user,
                        ),
                    )
                elif kind == "session.new":
                    await _safe_send(websocket, "session_new", sessionId=_session_key(_chat_id()))
                elif kind == "session.open":
                    session_id = str(message.get("sessionId") or "")
                    if not session_id:
                        await _safe_send(websocket, "error", message="sessionId is required.")
                        continue
                    if not _session_owned_by(runtime, user, session_id):
                        await _safe_send(websocket, "error", detail="session_not_found")
                        continue
                    payload = runtime.require_loop().sessions.read_session_file(session_id)
                    if payload is None:
                        payload = {"key": session_id, "messages": [], "metadata": {}}
                    await _safe_send(websocket, "session_open", sessionId=session_id, session=payload)
                elif kind == "config.reload":
                    if not user.has("settings.manage"):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="permission_denied",
                            reason="当前用户没有重新加载系统配置的权限。",
                        )
                        continue
                    config = await runtime.reload()
                    await _safe_send(
                        websocket,
                        "config_reloaded",
                        config=_config_payload(config),
                        sessions=runtime.require_loop().sessions.list_sessions(),
                    )
                elif kind == "security.inspection.resolve":
                    inspection_id = str(message.get("inspection_id") or "").strip()
                    approved = message.get("approved")
                    if not inspection_id or not isinstance(approved, bool):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="invalid security inspection response",
                        )
                        continue
                    if not runtime.agentspec.resolve_inspection(inspection_id, approved):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="security inspection is no longer pending",
                        )
                elif kind == "privileged_action.resolve":
                    approval_id = str(message.get("approval_id") or "").strip()
                    approved = message.get("approved")
                    if not approval_id or not isinstance(approved, bool):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="invalid privileged action approval response",
                        )
                        continue
                    if not user.has("privileged.approve"):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="permission_denied",
                            reason="当前用户没有批准 root 操作的权限。",
                        )
                        continue
                    if not runtime.privileged.resolve(
                        approval_id,
                        approved,
                        approver=user,
                    ):
                        await _safe_send(
                            websocket,
                            "error",
                            detail="privileged action approval is no longer pending",
                        )
                else:
                    await _safe_send(websocket, "error", detail=f"unknown type: {kind!r}")
        except WebSocketDisconnect:
            pass
        finally:
            for chat_id in list(attached_chat_ids):
                runtime.detach_webui_socket(chat_id, websocket)
            pending = [task for task in active_turns.values() if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for session_key, notifier in inspection_notifiers.items():
                runtime.agentspec.unbind_notifier(session_key, notifier)
            for session_key, notifier in privilege_notifiers.items():
                runtime.privileged.unbind_notifier(session_key, notifier)
            reset_current_user(user_context)

    dist_dir = Path(__file__).resolve().parents[2] / "webui" / "dist"
    index_file = dist_dir / "index.html"
    if dist_dir.exists() and index_file.exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
        for public_name in ("brand",):
            public_dir = dist_dir / public_name
            if public_dir.exists():
                app.mount(f"/{public_name}", StaticFiles(directory=public_dir), name=public_name)

        root_public_files = {
            "favicon.ico": "image/x-icon",
            "favicon.svg": "image/svg+xml",
            "favicon-32x32.png": "image/png",
            "favicon-256x256.png": "image/png",
            "apple-touch-icon.png": "image/png",
        }

        @app.get("/{asset_name}", include_in_schema=False)
        async def root_public_asset(asset_name: str) -> FileResponse:
            media_type = root_public_files.get(asset_name)
            if media_type is not None:
                candidate = dist_dir / asset_name
                if candidate.exists():
                    return FileResponse(candidate, media_type=media_type)
            return FileResponse(index_file)

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str = "") -> FileResponse:
            return FileResponse(index_file)

    return app
