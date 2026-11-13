"""Controlled one-shot privileged action requests for NimbusOps."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable

from loguru import logger

from my_agent.config.paths import get_runtime_subdir
from my_agent.config.schema import PrivilegedExecutionConfig
from my_agent.security.auth import AuthUser, current_user

ApprovalNotifier = Callable[[dict[str, Any]], Awaitable[None]]

_ORDINARY_USER_MESSAGE = (
    "当前账号为普通用户，无法执行需要 root 权限的操作。"
    "安装系统软件、修改系统服务或系统配置时，请联系管理员。"
)


class PrivilegedActionError(RuntimeError):
    """Base class for privileged action failures."""


class PrivilegedHelperUnavailable(PrivilegedActionError):
    """Raised when the root helper is unavailable or rejects the request."""


class PrivilegedActionDenied(PrivilegedActionError):
    """Raised when the current Web user may not request root execution."""

    def __init__(self, action_name: str, message: str = _ORDINARY_USER_MESSAGE) -> None:
        super().__init__(message)
        self.action_name = action_name
        self.fallback_message = message
        self.finalization_instruction = (
            "The current NimbusOps Web user is a regular user and cannot approve root actions. "
            "State that the requested privileged action did not execute. Explain that installing "
            "system software, changing system services, or changing protected system configuration "
            "requires an administrator. Do not suggest sudo, another command, another tool, or any "
            "workaround that bypasses this permission boundary. Respond in the user's language and "
            "end the turn."
        )


class PrivilegedActionRejected(PrivilegedActionError):
    """Raised when an administrator rejects a pending root action."""

    def __init__(self, action_name: str) -> None:
        message = "管理员已拒绝本次特权操作，该动作没有执行，也没有获得 root 权限。"
        super().__init__(message)
        self.action_name = action_name
        self.fallback_message = message
        self.finalization_instruction = (
            f"The administrator rejected the one-shot privileged action '{action_name}'. "
            "State that the action did not execute and no root permission was granted. Do not "
            "retry it, suggest sudo, switch tools, split it into smaller commands, or propose any "
            "workaround that reaches the same result. Respond in the user's language and end the turn."
        )


@dataclass(slots=True)
class PendingPrivilegeApproval:
    id: str
    session_key: str
    actor: AuthUser
    action_name: str
    parameters: dict[str, Any]
    action: dict[str, Any]
    reason: str
    created_at: float
    future: asyncio.Future[bool]

    def payload(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "session_key": self.session_key,
            "action_name": self.action_name,
            "parameters": self.parameters,
            "action": self.action,
            "reason": self.reason,
            "requested_by": self.actor.payload(),
            "created_at": self.created_at,
        }


class PrivilegedAuditStore:
    """Append-only application-side audit trail for privileged requests."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = Lock()

    def _path(self) -> Path:
        return self.path or (get_runtime_subdir("audit") / "privileged-actions.jsonl")

    def append(self, record: dict[str, Any]) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "id": uuid.uuid4().hex,
            "timestamp": time.time(),
            **record,
        }
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        path = self._path()
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in reversed(lines[-max(1, limit) :]):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
        return records


class PrivilegedHelperClient:
    """JSON-lines client for the separately installed root helper."""

    def __init__(self, config: PrivilegedExecutionConfig) -> None:
        self.config = config

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        socket_path = Path(self.config.socket_path).expanduser()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(socket_path)),
                timeout=min(10, self.config.request_timeout),
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise PrivilegedHelperUnavailable(
                f"特权执行器不可用：无法连接 {socket_path}"
            ) from exc
        request = {
            "protocol": 1,
            "request_id": uuid.uuid4().hex,
            **payload,
        }
        try:
            writer.write(
                (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(),
                timeout=self.config.request_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise PrivilegedHelperUnavailable("特权执行器响应超时") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        if not raw:
            raise PrivilegedHelperUnavailable("特权执行器未返回结果")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrivilegedHelperUnavailable("特权执行器返回了无效响应") from exc
        if not isinstance(response, dict):
            raise PrivilegedHelperUnavailable("特权执行器返回格式不正确")
        if response.get("ok") is not True:
            raise PrivilegedHelperUnavailable(
                str(response.get("error") or "特权执行器拒绝了请求")
            )
        return response

    async def status(self) -> dict[str, Any]:
        return await self._request({"operation": "status"})

    async def list_actions(self) -> list[dict[str, Any]]:
        response = await self._request({"operation": "list_actions"})
        actions = response.get("actions")
        return [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []

    async def get_policy(self) -> dict[str, Any]:
        response = await self._request({"operation": "get_policy"})
        policy = response.get("policy")
        if not isinstance(policy, dict):
            raise PrivilegedHelperUnavailable("特权执行器未返回有效策略")
        return policy

    async def validate_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            {
                "operation": "validate_policy",
                "policy": policy,
            }
        )
        validated = response.get("policy")
        if not isinstance(validated, dict):
            raise PrivilegedHelperUnavailable("特权执行器未返回有效策略")
        return {
            "valid": True,
            "policy": validated,
            "actions": response.get("actions", []),
        }

    async def add_action(
        self,
        *,
        action_name: str,
        action: dict[str, Any],
        actor: AuthUser,
    ) -> dict[str, Any]:
        return await self._request(
            {
                "operation": "add_action",
                "action_name": action_name,
                "action": action,
                "actor": {
                    "id": actor.id,
                    "username": actor.username,
                    "display_name": actor.display_name,
                    "role": actor.role,
                },
            }
        )

    async def execute(
        self,
        *,
        action_name: str,
        parameters: dict[str, Any],
        approval_id: str,
        actor: AuthUser,
        reason: str,
    ) -> dict[str, Any]:
        return await self._request(
            {
                "operation": "execute",
                "action_name": action_name,
                "parameters": parameters,
                "approval": {
                    "id": approval_id,
                    "actor_id": actor.id,
                    "actor_username": actor.username,
                    "actor_role": actor.role,
                    "reason": reason,
                    "approved_at": time.time(),
                },
            }
        )


class PrivilegedActionRuntime:
    """Role checks, administrator approval, helper calls, and audit."""

    def __init__(
        self,
        config: PrivilegedExecutionConfig | None = None,
        *,
        audit_store: PrivilegedAuditStore | None = None,
    ) -> None:
        self.config = config or PrivilegedExecutionConfig()
        self.client = PrivilegedHelperClient(self.config)
        self.audit = audit_store or PrivilegedAuditStore()
        self._notifiers: dict[str, ApprovalNotifier] = {}
        self._pending: dict[str, PendingPrivilegeApproval] = {}
        self._registered_actions: list[dict[str, Any]] = []

    def configure(self, config: PrivilegedExecutionConfig) -> None:
        self.config = config
        self.client = PrivilegedHelperClient(config)
        self._registered_actions = []

    @property
    def registered_actions(self) -> list[dict[str, Any]]:
        """Return the latest helper-provided action metadata for model discovery."""
        return deepcopy(self._registered_actions)

    async def refresh_actions(
        self,
        *,
        tolerate_unavailable: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            actions = await self.client.list_actions()
        except PrivilegedHelperUnavailable:
            self._registered_actions = []
            if tolerate_unavailable:
                return []
            raise
        self._registered_actions = deepcopy(actions)
        return self.registered_actions

    def bind_notifier(self, session_key: str, notifier: ApprovalNotifier) -> None:
        self._notifiers[session_key] = notifier

    def unbind_notifier(self, session_key: str, notifier: ApprovalNotifier) -> None:
        if self._notifiers.get(session_key) is notifier:
            self._notifiers.pop(session_key, None)

    async def payload(self) -> dict[str, Any]:
        helper: dict[str, Any]
        actions: list[dict[str, Any]]
        try:
            helper = await self.client.status()
            actions = await self.refresh_actions()
            available = True
            error = None
        except PrivilegedHelperUnavailable as exc:
            helper = {}
            self._registered_actions = []
            actions = self.registered_actions
            available = False
            error = str(exc)
        return {
            "enabled": self.config.enabled,
            "available": available,
            "socket_path": self.config.socket_path,
            "approval_timeout": self.config.approval_timeout,
            "request_timeout": self.config.request_timeout,
            "helper": helper,
            "actions": actions,
            "pending": [item.payload() for item in self._pending.values()],
            "error": error,
        }

    async def request(
        self,
        *,
        session_key: str,
        action_name: str,
        parameters: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        actor = current_user()
        if (
            actor is None
            or actor.role != "system_admin"
            or not actor.has("privileged.approve")
        ):
            self.audit.append(
                {
                    "event": "denied",
                    "outcome": "regular_user",
                    "action_name": action_name,
                    "parameters": parameters,
                    "reason": reason,
                    "actor": actor.payload() if actor else None,
                    "session_key": session_key,
                }
            )
            raise PrivilegedActionDenied(action_name)
        if not self.config.enabled:
            raise PrivilegedHelperUnavailable("受控特权执行功能未启用")

        actions = await self.refresh_actions()
        action = next((item for item in actions if item.get("name") == action_name), None)
        if action is None:
            raise PrivilegedHelperUnavailable(
                f"特权动作 {action_name!r} 未在 root 策略中登记"
            )
        notifier = self._notifiers.get(session_key)
        if notifier is None:
            raise PrivilegedHelperUnavailable("当前会话没有可用的管理员审批通道")

        loop = asyncio.get_running_loop()
        pending = PendingPrivilegeApproval(
            id=uuid.uuid4().hex,
            session_key=session_key,
            actor=actor,
            action_name=action_name,
            parameters=parameters,
            action=action,
            reason=reason.strip(),
            created_at=time.time(),
            future=loop.create_future(),
        )
        self._pending[pending.id] = pending
        self.audit.append(
            {
                "event": "requested",
                "outcome": "pending",
                "approval_id": pending.id,
                "action_name": action_name,
                "parameters": parameters,
                "reason": reason,
                "actor": actor.payload(),
                "session_key": session_key,
            }
        )
        try:
            await notifier(pending.payload())
            approved = await asyncio.wait_for(
                pending.future,
                timeout=self.config.approval_timeout,
            )
        except asyncio.TimeoutError:
            approved = False
            self.audit.append(
                {
                    "event": "approval",
                    "outcome": "timeout",
                    "approval_id": pending.id,
                    "action_name": action_name,
                    "actor": actor.payload(),
                    "session_key": session_key,
                }
            )
        finally:
            self._pending.pop(pending.id, None)

        if not approved:
            self.audit.append(
                {
                    "event": "approval",
                    "outcome": "rejected",
                    "approval_id": pending.id,
                    "action_name": action_name,
                    "parameters": parameters,
                    "actor": actor.payload(),
                    "session_key": session_key,
                }
            )
            raise PrivilegedActionRejected(action_name)

        self.audit.append(
            {
                "event": "approval",
                "outcome": "approved",
                "approval_id": pending.id,
                "action_name": action_name,
                "parameters": parameters,
                "actor": actor.payload(),
                "session_key": session_key,
            }
        )
        try:
            result = await self.client.execute(
                action_name=action_name,
                parameters=parameters,
                approval_id=pending.id,
                actor=actor,
                reason=reason,
            )
        except PrivilegedHelperUnavailable as exc:
            self.audit.append(
                {
                    "event": "execution",
                    "outcome": "failed",
                    "approval_id": pending.id,
                    "action_name": action_name,
                    "parameters": parameters,
                    "actor": actor.payload(),
                    "session_key": session_key,
                    "error": str(exc),
                }
            )
            raise
        self.audit.append(
            {
                "event": "execution",
                "outcome": "succeeded" if result.get("exit_code") == 0 else "failed",
                "approval_id": pending.id,
                "action_name": action_name,
                "parameters": parameters,
                "actor": actor.payload(),
                "session_key": session_key,
                "exit_code": result.get("exit_code"),
            }
        )
        return result

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        *,
        approver: AuthUser,
    ) -> bool:
        if approver.role != "system_admin" or not approver.has("privileged.approve"):
            return False
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            return False
        if pending.actor.id != approver.id:
            return False
        pending.future.set_result(bool(approved))
        return True

    def audit_payload(self) -> dict[str, Any]:
        raw_records = self.audit.list(max(1, self.config.audit_limit * 4))
        grouped: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for record in raw_records:
            approval_id = str(record.get("approval_id") or "").strip()
            key = approval_id or f"record:{record.get('id')}"
            if key not in grouped:
                grouped[key] = {
                    **record,
                    "id": approval_id or record.get("id"),
                    "stages": [],
                    "completed_at": record.get("timestamp"),
                }
                order.append(key)
            item = grouped[key]
            item["stages"].append(
                {
                    "id": record.get("id"),
                    "timestamp": record.get("timestamp"),
                    "event": record.get("event"),
                    "outcome": record.get("outcome"),
                    "exit_code": record.get("exit_code"),
                    "error": record.get("error"),
                }
            )
            for field in (
                "approval_id",
                "action_name",
                "parameters",
                "reason",
                "actor",
                "session_key",
                "exit_code",
                "error",
            ):
                if item.get(field) is None and record.get(field) is not None:
                    item[field] = record[field]
            item["requested_at"] = record.get("timestamp")

        records: list[dict[str, Any]] = []
        for key in order[: self.config.audit_limit]:
            item = grouped[key]
            item["stages"].reverse()
            records.append(item)
        return {
            "records": records,
        }

    async def policy_payload(self) -> dict[str, Any]:
        policy = await self.client.get_policy()
        status = await self.client.status()
        capabilities = status.get("capabilities")
        writable_by_admin = (
            isinstance(capabilities, list)
            and "add_action" in capabilities
        )
        return {
            "policy": policy,
            "policy_path": status.get("policy_path"),
            "writable_by_agent": False,
            "writable_by_admin": writable_by_admin,
        }

    async def validate_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        return await self.client.validate_policy(policy)

    async def add_action(
        self,
        *,
        action_name: str,
        action: dict[str, Any],
        actor: AuthUser,
    ) -> dict[str, Any]:
        if actor.role != "system_admin" or not actor.has("privileged.approve"):
            raise PrivilegedActionDenied(action_name)
        result = await self.client.add_action(
            action_name=action_name,
            action=action,
            actor=actor,
        )
        await self.refresh_actions()
        return result
