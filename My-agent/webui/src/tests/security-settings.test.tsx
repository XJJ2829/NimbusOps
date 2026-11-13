import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SecuritySettings } from "@/components/settings/SecuritySettings";
import type { AuthUser } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const ADMIN_USER: AuthUser = {
  id: "admin",
  username: "admin",
  display_name: "Administrator",
  role: "system_admin",
  permissions: ["ops_shield.view", "ops_shield.manage"],
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

describe("SecuritySettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("switches from policies to registered Predicate functions and shows source", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          enabled: true,
          rules: [],
          errors: [],
          registered_predicates: ["is_destructive"],
          predicates: [
            {
              name: "is_destructive",
              description: "Detect destructive operations in tool input.",
              module: "my_agent.agentspec.predicates",
              qualified_name: "is_destructive",
              signature: "(user_input, tool_input, intermediate_steps)",
              source: "def is_destructive(user_input, tool_input, intermediate_steps):\n    return True",
              source_kind: "builtin",
            },
          ],
          pending_inspections: [],
          trigger_naming: "Trigger must exactly match the runtime tool name.",
        }),
      ),
    );

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <SecuritySettings />
      </ClientProvider>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Predicate 函数" }));

    const predicate = await screen.findByRole("button", {
      name: "查看 Predicate is_destructive",
    });
    expect(screen.getByText("Detect destructive operations in tool input.")).toBeInTheDocument();
    expect(screen.queryByText(/is_destructive\(user_input/)).not.toBeInTheDocument();

    fireEvent.click(predicate);

    expect(await screen.findByText("函数实现")).toBeInTheDocument();
    expect(screen.getByText(/def is_destructive/)).toBeInTheDocument();
    expect(screen.getByText("my_agent.agentspec.predicates")).toBeInTheDocument();
  });

  it("shows registered Predicate names while an older server process is still running", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          enabled: true,
          rules: [],
          errors: [],
          registered_predicates: ["is_destructive"],
          pending_inspections: [],
          trigger_naming: "Trigger must exactly match the runtime tool name.",
        }),
      ),
    );

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <SecuritySettings />
      </ClientProvider>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Predicate 函数" }));

    expect(await screen.findByText("is_destructive")).toBeInTheDocument();
    expect(
      screen.getByText("检测工具参数中的破坏性文件系统操作和高权限 Shell 命令。"),
    ).toBeInTheDocument();
  });

  it("builds a new policy from fields instead of pre-filling a concrete rule", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          enabled: true,
          rules: [],
          errors: [],
          registered_predicates: ["is_destructive"],
          predicates: [],
          pending_inspections: [],
          trigger_naming: "Trigger must exactly match the runtime tool name.",
        }),
      ),
    );

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <SecuritySettings />
      </ClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "新建策略" }));

    expect(screen.getByPlaceholderText("your_policy_name")).toHaveValue("");
    expect(screen.getByPlaceholderText("runtime_tool_name")).toHaveValue("");
    expect(screen.getByLabelText("Predicate")).toHaveValue("");
    expect(screen.getByLabelText("Enforcement")).toHaveValue("user_inspection");
    expect(screen.getByText(/stop（直接退出并阻止执行）/)).toBeInTheDocument();
  });

  it("opens a creation editor for a custom Predicate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          enabled: true,
          rules: [],
          errors: [],
          registered_predicates: ["is_destructive"],
          predicates: [],
          pending_inspections: [],
          trigger_naming: "Trigger must exactly match the runtime tool name.",
        }),
      ),
    );

    render(
      <ClientProvider client={{} as never} token="tok" user={ADMIN_USER}>
        <SecuritySettings />
      </ClientProvider>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Predicate 函数" }));
    fireEvent.click(screen.getByRole("button", { name: "新建函数" }));

    expect(screen.getByRole("dialog", { name: "新建 Predicate 函数" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("your_predicate")).toHaveValue("");
    expect(screen.getByLabelText("函数作用")).toHaveValue("");
    expect(screen.getByLabelText("函数实现")).toHaveValue("return False");

    fireEvent.change(screen.getByLabelText("函数名称"), {
      target: { value: "needs_review" },
    });
    fireEvent.change(screen.getByLabelText("函数作用"), {
      target: { value: "检测需要人工确认的工具输入" },
    });

    expect(screen.getByText(/def needs_review\(user_input, tool_input, history\)/)).toBeInTheDocument();
    expect(screen.getByText(/检测需要人工确认的工具输入/)).toBeInTheDocument();
  });
});
