import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  fetchAuditTrace: vi.fn(),
  fetchAuditTraceExport: vi.fn(),
  fetchAuditTraces: vi.fn(),
}));

import { AuditCenter } from "@/components/AuditCenter";
import { fetchAuditTrace, fetchAuditTraces } from "@/lib/api";

const traceSummary = {
  trace_id: "trace-missing-service",
  session_key: "websocket:demo",
  session_title: "服务诊断",
  actor_username: "operator01",
  user_input: "诊断不存在的服务",
  started_at: "2026-07-08T21:40:00+08:00",
  completed_at: "2026-07-08T21:40:01+08:00",
  duration_ms: 420,
  status: "completed" as const,
  risk_level: "medium" as const,
  tool_count: 2,
  security_event_count: 0,
  anomaly_count: 2,
  integrity: "complete" as const,
};

describe("AuditCenter anomaly traceback", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("summarizes the first anomaly and locates its timeline node", async () => {
    vi.mocked(fetchAuditTraces).mockResolvedValue({
      traces: [traceSummary],
      summary: {
        total: 1,
        completed: 1,
        high_risk: 0,
        security_events: 0,
      },
    });
    vi.mocked(fetchAuditTrace).mockResolvedValue({
      ...traceSummary,
      schema_version: 1,
      exported_at: "2026-07-08T21:41:00+08:00",
      anomalies: [
        {
          event_id: "trace-missing-service:tool:read-unit",
          stage: "tool_call",
          severity: "warning",
          category: "empty_evidence",
          title: "未读取到目标配置",
          cause: "/usr/lib/systemd/system/nimbusops-demo-missing.service 未返回任何配置内容。",
          impact: "无法从该配置获得有效证据，目标文件可能为空或不存在。",
          tool_name: "read_config",
        },
        {
          event_id: "trace-missing-service:tool:read-vendor-unit",
          stage: "tool_call",
          severity: "warning",
          category: "empty_evidence",
          title: "未读取到目标配置",
          cause: "/etc/systemd/system/nimbusops-demo-missing.service 未返回任何配置内容。",
          impact: "无法从该配置获得有效证据，目标文件可能为空或不存在。",
          tool_name: "read_config",
        },
      ],
      timeline: [
        {
          id: "trace-missing-service:received",
          sequence: 1,
          stage: "received",
          status: "success",
          title: "接收指令",
          summary: "诊断不存在的服务",
        },
        {
          id: "trace-missing-service:tool:read-unit",
          sequence: 2,
          stage: "tool_call",
          status: "warning",
          title: "read_config",
          summary: "/usr/lib/systemd/system/nimbusops-demo-missing.service 未返回任何配置内容。",
          details: {
            path: "/usr/lib/systemd/system/nimbusops-demo-missing.service",
            size_bytes: 0,
            content: "",
          },
        },
        {
          id: "trace-missing-service:tool:read-vendor-unit",
          sequence: 3,
          stage: "tool_call",
          status: "warning",
          title: "read_config",
          summary: "/etc/systemd/system/nimbusops-demo-missing.service 未返回任何配置内容。",
          details: {
            path: "/etc/systemd/system/nimbusops-demo-missing.service",
            size_bytes: 0,
            content: "",
          },
        },
        {
          id: "trace-missing-service:complete",
          sequence: 4,
          stage: "completed",
          status: "success",
          title: "最终报告",
          summary: "目标服务不存在。",
        },
      ],
    });

    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    render(<AuditCenter token="test-token" onToggleSidebar={vi.fn()} />);

    expect(await screen.findByText("异常回溯")).toBeInTheDocument();
    expect(screen.getByText("共 2 个异常")).toBeInTheDocument();
    expect(screen.getByText("异常 1")).toBeInTheDocument();
    expect(screen.getByText("异常 2")).toBeInTheDocument();
    expect(screen.getByText("operator01")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-audit-anomaly]")).toHaveLength(2);
    document.querySelectorAll("[data-audit-anomaly]").forEach((node) => {
      expect(node.querySelector("[data-timeline-icon]")).toBeInTheDocument();
      expect(node.querySelector("[data-timeline-connector]")).toHaveClass(
        "left-4",
        "-translate-x-1/2",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "定位首个异常" }));
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
  });
});
