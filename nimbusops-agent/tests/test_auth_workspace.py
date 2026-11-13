from pathlib import Path

import pytest

from my_agent.agent.tools.filesystem import WriteFileTool
from my_agent.security.auth import (
    AuthUser,
    bind_current_user,
    reset_current_user,
)
from my_agent.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    current_scope_allows_loopback,
    current_tool_workspace,
    reset_workspace_scope,
)
from my_agent.webui.workspaces import WebUIWorkspaceController


def _user(role: str) -> AuthUser:
    permissions = (
        frozenset({"sandbox.host_exec"})
        if role == "system_admin"
        else frozenset({"sandbox.view"})
    )
    return AuthUser(
        id=role,
        username=role,
        display_name=role,
        role=role,
        permissions=permissions,
    )


def test_regular_user_forces_workspace_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    user_token = bind_current_user(_user("user"))
    scope_token = bind_workspace_scope(build_workspace_scope(workspace, "full", source_channel="websocket"))
    try:
        access = current_tool_workspace(workspace)
        assert access.restrict_to_workspace is True
        assert current_scope_allows_loopback(enabled=True) is False

        tool = WriteFileTool(workspace=workspace, restrict_to_workspace=False)
        with pytest.raises(PermissionError):
            tool._resolve(str(outside))
    finally:
        reset_workspace_scope(scope_token)
        reset_current_user(user_token)


def test_regular_user_ignores_historical_full_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    old_project = tmp_path / "old-project"
    workspace.mkdir()
    old_project.mkdir()

    class Sessions:
        def read_session_file(self, key: str) -> dict:
            return {
                "metadata": {
                    "workspace_scope": {
                        "project_path": str(old_project),
                        "access_mode": "full",
                    }
                }
            }

    controller = WebUIWorkspaceController(
        session_manager=Sessions(),
        default_workspace=workspace,
        default_restrict_to_workspace=False,
    )
    scope = controller.scope_for_message(
        {},
        chat_id="old-chat",
        chat_running=False,
        controls_available=False,
    )

    assert scope.project_path == workspace.resolve()
    assert scope.access_mode == "restricted"
    assert scope.restrict_to_workspace is True
