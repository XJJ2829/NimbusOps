import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PrivilegedExecutionSettings } from "@/components/settings/PrivilegedExecutionSettings";
import type { AuthUser } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const ADMIN_USER: AuthUser = {
  id: "admin-1",
  username: "admin",
  display_name: "Administrator",
  role: "system_admin",
  permissions: ["sandbox.manage", "privileged.approve", "privileged.audit"],
  enabled: true,
  created_at: 0,
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => body,
  } as unknown as Response;
}

const executionPayload = {
  enabled: true,
  available: true,
  socket_path: "/var/run/nimbusops/privileged.sock",
  approval_timeout: 300,
  request_timeout: 120,
  helper: { peer_mode: "uid" },
  actions: [{
    name: "flush_dns_cache",
    label: "刷新 DNS 缓存",
    description: "向 mDNSResponder 发送 HUP 信号。",
    category: "system",
    impact: "本机 DNS 缓存会被刷新",
    parameters: {},
    timeout: 15,
  }],
  pending: [],
  error: null,
};

describe("PrivilegedExecutionSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows action details and one merged audit row with the username", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/audit")) {
        return jsonResponse({
          records: [
            {
              id: "3",
              timestamp: 1_750_000_000,
              event: "execution",
              outcome: "succeeded",
              approval_id: "approval-1",
              action_name: "flush_dns_cache",
              parameters: {},
              actor: ADMIN_USER,
              session_key: "websocket:chat-1",
              exit_code: 0,
            },
            {
              id: "2",
              timestamp: 1_749_999_999,
              event: "approval",
              outcome: "approved",
              approval_id: "approval-1",
              action_name: "flush_dns_cache",
              parameters: {},
              actor: ADMIN_USER,
              session_key: "websocket:chat-1",
            },
            {
              id: "1",
              timestamp: 1_749_999_998,
              event: "requested",
              outcome: "pending",
              approval_id: "approval-1",
              action_name: "flush_dns_cache",
              parameters: {},
              reason: "刷新 DNS 缓存",
              actor: ADMIN_USER,
              session_key: "websocket:chat-1",
            },
          ],
        });
      }
      if (url.endsWith("/policy")) {
        return jsonResponse({
          policy: {
            version: 1,
            allowedUsers: ["admin"],
            peerVerification: { mode: "uid" },
            actions: {
              flush_dns_cache: {
                label: "刷新 DNS 缓存",
                executable: "/usr/bin/killall",
                arguments: ["-HUP", "mDNSResponder"],
                parameters: {},
                timeout: 15,
              },
            },
          },
          policy_path: "/etc/nimbusops/privileged-actions.json",
          writable_by_agent: false,
          writable_by_admin: true,
        });
      }
      return jsonResponse(executionPayload);
    }));

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <PrivilegedExecutionSettings />
      </ClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "添加动作" })).toBeInTheDocument();
    expect(screen.getByText("Administrator（admin）")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "查看特权审计 flush_dns_cache" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "查看特权动作 flush_dns_cache" }));
    expect(await screen.findByText("/usr/bin/killall")).toBeInTheDocument();
    expect(screen.getAllByText(/mDNSResponder/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.click(screen.getByRole("button", { name: "查看特权审计 flush_dns_cache" }));
    expect(await screen.findByText("执行参数")).toBeInTheDocument();
    expect(screen.getByText("提交申请")).toBeInTheDocument();
    expect(screen.getByText("helper 执行")).toBeInTheDocument();
  });

  it("submits a new action through the administrator-only endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/audit")) return jsonResponse({ records: [] });
      if (url.endsWith("/policy")) {
        return jsonResponse({
          policy: {
            version: 1,
            allowedUsers: ["admin"],
            peerVerification: { mode: "uid" },
            actions: {},
          },
          writable_by_agent: false,
          writable_by_admin: true,
        });
      }
      if (url.endsWith("/actions") && init?.method === "POST") {
        return jsonResponse({
          action: executionPayload.actions[0],
          actions: executionPayload.actions,
          policy: {
            version: 1,
            allowedUsers: ["admin"],
            peerVerification: { mode: "uid" },
            actions: {},
          },
          execution: executionPayload,
        });
      }
      return jsonResponse(executionPayload);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <PrivilegedExecutionSettings />
      </ClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "添加动作" }));
    fireEvent.change(screen.getByLabelText("动作标识"), { target: { value: "check_helper" } });
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "检查 helper" } });
    fireEvent.change(screen.getByLabelText("执行程序"), { target: { value: "/usr/bin/true" } });
    fireEvent.change(screen.getByLabelText("执行影响"), { target: { value: "只检查 helper" } });
    fireEvent.click(screen.getByRole("button", { name: "添加并立即生效" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/security/privileged-execution/actions",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
