import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
  type ReactNode,
} from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Download,
  FileCheck2,
  ListChecks,
  LocateFixed,
  Menu,
  RefreshCcw,
  Search,
  ShieldCheck,
  TerminalSquare,
  UserRound,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StyledSelect } from "@/components/ui/styled-select";
import {
  fetchAuditTrace,
  fetchAuditTraceExport,
  fetchAuditTraces,
} from "@/lib/api";
import type {
  AuditAnomaly,
  AuditRiskLevel,
  AuditTimelineEvent,
  AuditTraceDetail,
  AuditTraceStatus,
  AuditTraceSummary,
  AuditTracesPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface AuditCenterProps {
  token: string;
  onToggleSidebar: () => void;
}

const EMPTY_PAYLOAD: AuditTracesPayload = {
  traces: [],
  summary: { total: 0, completed: 0, high_risk: 0, security_events: 0 },
};

const actionButtonClass =
  "h-8 shrink-0 gap-1.5 rounded-full px-3 text-[12px] font-semibold shadow-[0_8px_22px_rgba(15,23,42,0.045)] transition-all hover:-translate-y-px hover:shadow-[0_12px_28px_rgba(15,23,42,0.075)] dark:shadow-[0_8px_22px_rgba(0,0,0,0.14)]";

function formatDateTime(value?: string | null): string {
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatDuration(value: number): string {
  if (!value) return "--";
  if (value < 1000) return value + " ms";
  if (value < 60_000) return (value / 1000).toFixed(1) + " 秒";
  return Math.floor(value / 60_000) + " 分 " + Math.round((value % 60_000) / 1000) + " 秒";
}

function statusLabel(status: AuditTraceStatus): string {
  return status === "completed" ? "已完成" : "已中断";
}

function riskLabel(risk: AuditRiskLevel): string {
  if (risk === "high") return "高风险";
  if (risk === "medium") return "需关注";
  return "低风险";
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    received: "输入",
    reasoning: "决策",
    tool_call: "工具",
    security_check: "安全",
    approval: "审批",
    result: "输出",
    completed: "完成",
  };
  return labels[stage] ?? stage;
}

function stageIcon(stage: string): ReactNode {
  if (stage === "tool_call") return <TerminalSquare className="h-3.5 w-3.5" />;
  if (stage === "security_check" || stage === "approval") {
    return <ShieldCheck className="h-3.5 w-3.5" />;
  }
  if (stage === "completed") return <Check className="h-3.5 w-3.5" />;
  return <ListChecks className="h-3.5 w-3.5" />;
}

function RiskBadge({ risk }: { risk: AuditRiskLevel }) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center rounded-md border px-2 text-[11px] font-medium",
        risk === "high" && "border-red-500/25 bg-red-500/8 text-red-600 dark:text-red-400",
        risk === "medium" && "border-amber-500/25 bg-amber-500/8 text-amber-700 dark:text-amber-400",
        risk !== "high" && risk !== "medium" && "border-emerald-500/25 bg-emerald-500/8 text-emerald-700 dark:text-emerald-400",
      )}
    >
      {riskLabel(risk)}
    </span>
  );
}

function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: string | number;
  detail: string;
}) {
  return (
    <div className="min-w-0 px-4 py-3 first:pl-0 sm:px-5">
      <div className="text-[11px] font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-[20px] font-semibold text-foreground">{value}</span>
        <span className="truncate text-[11px] text-muted-foreground">{detail}</span>
      </div>
    </div>
  );
}

function TraceRow({
  trace,
  selected,
  onSelect,
}: {
  trace: AuditTraceSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  const anomalyCount = trace.anomaly_count ?? 0;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full border-b border-border/45 px-4 py-3.5 text-left transition-colors last:border-b-0 hover:bg-muted/45",
        selected && "bg-[#2997FF]/8 hover:bg-[#2997FF]/10",
      )}
    >
      <div className="flex items-start gap-3">
        <span
          className={cn(
            "mt-1.5 h-2 w-2 shrink-0 rounded-full",
            anomalyCount
              ? "bg-amber-500"
              : trace.status === "completed"
                ? "bg-emerald-500"
                : "bg-amber-500",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-3">
            <span className="truncate text-[13px] font-semibold text-foreground">
              {trace.session_title}
            </span>
            <span className="shrink-0 text-[10.5px] text-muted-foreground">
              {formatDateTime(trace.started_at)}
            </span>
          </div>
          <p className="mt-1 line-clamp-2 text-[12px] leading-5 text-muted-foreground">
            {trace.user_input}
          </p>
          <div className="mt-2 flex items-center gap-2 text-[10.5px] text-muted-foreground">
            <RiskBadge risk={trace.risk_level} />
            {anomalyCount ? <span>{anomalyCount} 个异常</span> : null}
            <span>{trace.tool_count} 次工具调用</span>
            <span aria-hidden>·</span>
            <span>{formatDuration(trace.duration_ms)}</span>
          </div>
        </div>
        <ChevronRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground/70" />
      </div>
    </button>
  );
}

function TimelineItem({
  event,
  last,
  highlighted = false,
  anomalyNumber,
  targetRef,
}: {
  event: AuditTimelineEvent;
  last: boolean;
  highlighted?: boolean;
  anomalyNumber?: number;
  targetRef?: RefObject<HTMLDivElement>;
}) {
  const [expanded, setExpanded] = useState(highlighted);
  const hasDetails = event.details && Object.keys(event.details).length > 0;
  const tone =
    event.status === "error"
      ? "border-red-500/30 bg-red-500/8 text-red-600 dark:text-red-400"
      : event.status === "warning"
        ? "border-amber-500/30 bg-amber-500/8 text-amber-700 dark:text-amber-400"
        : "border-emerald-500/30 bg-emerald-500/8 text-emerald-700 dark:text-emerald-400";

  return (
    <div
      ref={targetRef}
      data-audit-anomaly={highlighted ? anomalyNumber : undefined}
      className="relative flex scroll-mt-5 gap-3 pb-5"
    >
      {highlighted ? (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute -inset-x-3 -top-2 bottom-2 z-0 rounded-md border border-amber-500/30 bg-amber-500/5"
        />
      ) : null}
      {!last ? (
        <span
          data-timeline-connector
          className="absolute bottom-0 left-4 top-8 z-[1] w-px -translate-x-1/2 bg-border/75"
        />
      ) : null}
      <span
        data-timeline-icon
        className={cn("relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border", tone)}
      >
        {stageIcon(event.stage)}
      </span>
      <div className="relative z-10 min-w-0 flex-1 pt-0.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[13px] font-semibold text-foreground">{event.title}</span>
          <span className="rounded border border-border/55 bg-muted/30 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {stageLabel(event.stage)}
          </span>
          {highlighted ? (
            <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-400">
              异常 {anomalyNumber}
            </span>
          ) : null}
          {event.timestamp ? (
            <span className="ml-auto text-[10.5px] text-muted-foreground">
              {formatDateTime(event.timestamp)}
            </span>
          ) : null}
        </div>
        <p className="mt-1 whitespace-pre-wrap break-words text-[12px] leading-5 text-muted-foreground">
          {event.summary}
        </p>
        {hasDetails ? (
          <>
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="mt-1.5 inline-flex items-center gap-1 text-[11px] font-medium text-[#147CE5] hover:text-[#0A66C2]"
            >
              <ChevronDown className={cn("h-3 w-3 transition-transform", expanded && "rotate-180")} />
              {expanded ? "收起证据" : "查看证据"}
            </button>
            {expanded ? (
              <pre className="mt-2 max-h-72 overflow-auto rounded-md border border-border/55 bg-muted/35 p-3 text-[10.5px] leading-5 text-foreground">
                {JSON.stringify(event.details, null, 2)}
              </pre>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function AnomalySummary({
  anomaly,
  count,
  onLocate,
}: {
  anomaly: AuditAnomaly;
  count: number;
  onLocate: () => void;
}) {
  const error = anomaly.severity === "error";
  return (
    <div
      className={cn(
        "mb-5 rounded-md border p-4",
        error
          ? "border-red-500/25 bg-red-500/5"
          : "border-amber-500/25 bg-amber-500/5",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-md border",
              error
                ? "border-red-500/30 bg-red-500/8 text-red-600 dark:text-red-400"
                : "border-amber-500/30 bg-amber-500/8 text-amber-700 dark:text-amber-400",
            )}
          >
            <CircleAlert className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[13px] font-semibold text-foreground">异常回溯</h3>
              <span className="rounded border border-border/60 bg-background/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                共 {count} 个异常
              </span>
            </div>
            <p className="mt-1 text-[13px] font-medium text-foreground">{anomaly.title}</p>
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onLocate}
          className={actionButtonClass}
        >
          <LocateFixed className="h-3.5 w-3.5" />
          定位首个异常
        </Button>
      </div>
      <dl className="mt-3 grid gap-2 text-[11.5px] leading-5 sm:grid-cols-2">
        <div className="min-w-0">
          <dt className="font-medium text-muted-foreground">异常原因</dt>
          <dd className="mt-0.5 break-words text-foreground">{anomaly.cause}</dd>
        </div>
        <div className="min-w-0">
          <dt className="font-medium text-muted-foreground">后续影响</dt>
          <dd className="mt-0.5 break-words text-foreground">{anomaly.impact}</dd>
        </div>
      </dl>
    </div>
  );
}

export function AuditCenter({ token, onToggleSidebar }: AuditCenterProps) {
  const [payload, setPayload] = useState<AuditTracesPayload>(EMPTY_PAYLOAD);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditTraceDetail | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [risk, setRisk] = useState("all");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const firstAnomalyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchAuditTraces(token)
      .then((next) => {
        if (cancelled) return;
        setPayload(next);
        setSelectedId((current) =>
          current && next.traces.some((item) => item.trace_id === current)
            ? current
            : next.traces[0]?.trace_id ?? null,
        );
      })
      .catch(() => {
        if (!cancelled) setError("审计记录加载失败，请确认服务已更新后重试。");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, token]);

  const selectedSummary = useMemo(
    () => payload.traces.find((item) => item.trace_id === selectedId) ?? null,
    [payload.traces, selectedId],
  );
  const detailAnomalies = detail?.anomalies ?? [];

  useEffect(() => {
    if (!selectedSummary) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    fetchAuditTrace(token, selectedSummary.trace_id, selectedSummary.session_key)
      .then((next) => {
        if (!cancelled) setDetail(next);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSummary, token]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return payload.traces.filter((item) => {
      if (status !== "all" && item.status !== status) return false;
      if (risk !== "all" && item.risk_level !== risk) return false;
      if (!normalized) return true;
      return (
        item.session_title.toLowerCase().includes(normalized)
        || item.user_input.toLowerCase().includes(normalized)
        || item.trace_id.toLowerCase().includes(normalized)
        || item.actor_username.toLowerCase().includes(normalized)
      );
    });
  }, [payload.traces, query, risk, status]);

  const exportSelected = useCallback(async () => {
    if (!selectedSummary) return;
    const blob = await fetchAuditTraceExport(
      token,
      selectedSummary.trace_id,
      selectedSummary.session_key,
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "nimbusops-audit-" + selectedSummary.trace_id + ".json";
    anchor.click();
    URL.revokeObjectURL(url);
  }, [selectedSummary, token]);

  const completionRate = payload.summary.total
    ? Math.round((payload.summary.completed / payload.summary.total) * 100)
    : 0;

  return (
    <section className="flex h-full min-w-0 flex-col overflow-hidden bg-background">
      <header className="shrink-0 border-b border-border/55 px-4 py-4 sm:px-5">
        <div className="flex items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={onToggleSidebar}
              aria-label="打开导航"
              title="打开导航"
              className="h-8 w-8 shrink-0 lg:hidden"
            >
              <Menu className="h-4 w-4" />
            </Button>
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border/60 bg-muted/35">
              <FileCheck2 className="h-4 w-4 text-foreground" />
            </span>
            <div className="min-w-0">
              <h1 className="text-[18px] font-semibold text-foreground">审计中心</h1>
              <p className="mt-0.5 truncate text-[12px] text-muted-foreground">
                追溯任务输入、决策、工具调用、安全检查与最终结果
              </p>
            </div>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
            aria-label="刷新"
            title="刷新"
            className={cn(actionButtonClass, "h-9 px-3.5")}
          >
            <RefreshCcw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            <span className="hidden sm:inline">刷新</span>
          </Button>
        </div>

        <div className="mt-4 grid grid-cols-2 divide-x divide-y divide-border/55 border-t border-border/55 sm:grid-cols-4 sm:divide-y-0">
          <Stat label="审计任务" value={payload.summary.total} detail="当前可追溯" />
          <Stat label="完整完成" value={completionRate + "%"} detail={payload.summary.completed + " 条"} />
          <Stat label="高风险" value={payload.summary.high_risk} detail="需优先复核" />
          <Stat label="安全事件" value={payload.summary.security_events} detail="扫描与审批" />
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-rows-[minmax(230px,40%)_minmax(0,1fr)] lg:grid-cols-[minmax(320px,38%)_minmax(0,1fr)] lg:grid-rows-1">
        <div className="flex min-h-0 flex-col border-b border-border/55 lg:border-b-0 lg:border-r">
          <div className="grid shrink-0 grid-cols-2 gap-2 border-b border-border/55 p-3">
            <label className="relative col-span-2">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索任务、会话或追溯编号"
                className="h-9 pl-9 text-[12px]"
              />
            </label>
            <StyledSelect value={status} onChange={(event) => setStatus(event.target.value)} className="h-9 text-[12px]">
              <option value="all">全部状态</option>
              <option value="completed">已完成</option>
              <option value="interrupted">已中断</option>
            </StyledSelect>
            <StyledSelect value={risk} onChange={(event) => setRisk(event.target.value)} className="h-9 text-[12px]">
              <option value="all">全部风险</option>
              <option value="high">高风险</option>
              <option value="medium">需关注</option>
              <option value="low">低风险</option>
            </StyledSelect>
          </div>

          <div className="flex items-center justify-between border-b border-border/45 px-4 py-2 text-[10.5px] text-muted-foreground">
            <span>任务追溯</span>
            <span>{filtered.length} 条记录</span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex h-40 items-center justify-center gap-2 text-[12px] text-muted-foreground">
                <RefreshCcw className="h-4 w-4 animate-spin" />
                正在整理审计链路
              </div>
            ) : error ? (
              <div className="m-4 flex items-start gap-2 rounded-md border border-red-500/20 bg-red-500/5 p-3 text-[12px] text-red-600 dark:text-red-400">
                <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                {error}
              </div>
            ) : filtered.length ? (
              filtered.map((trace) => (
                <TraceRow
                  key={trace.trace_id}
                  trace={trace}
                  selected={trace.trace_id === selectedId}
                  onSelect={() => setSelectedId(trace.trace_id)}
                />
              ))
            ) : (
              <div className="flex h-40 flex-col items-center justify-center text-center">
                <Search className="h-5 w-5 text-muted-foreground/60" />
                <p className="mt-2 text-[12px] text-muted-foreground">没有符合条件的审计记录</p>
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col">
          {selectedSummary ? (
            <>
              <div className="shrink-0 border-b border-border/55 px-4 py-3 sm:px-5">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <h2 className="min-w-0 flex-1 basis-52 truncate text-[14px] font-semibold text-foreground">
                        {selectedSummary.session_title}
                      </h2>
                      <RiskBadge risk={selectedSummary.risk_level} />
                      <span className="inline-flex h-6 items-center rounded-md border border-border/60 px-2 text-[10.5px] text-muted-foreground">
                        {statusLabel(selectedSummary.status)}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[12px] leading-5 text-muted-foreground">
                      {selectedSummary.user_input}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void exportSelected()}
                    aria-label="导出证据"
                    title="导出证据"
                    className={cn(actionButtonClass, "xl:px-3.5")}
                  >
                    <Download className="h-3.5 w-3.5" />
                    <span className="hidden xl:inline">导出证据</span>
                  </Button>
                </div>
                <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[10.5px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1.5">
                    <Clock3 className="h-3 w-3" />
                    {formatDateTime(selectedSummary.started_at)}
                  </span>
                  <span>耗时 {formatDuration(selectedSummary.duration_ms)}</span>
                  <span>{selectedSummary.tool_count} 次工具调用</span>
                  <span
                    className="inline-flex min-w-0 max-w-48 items-center gap-1.5"
                    title={`用户：${selectedSummary.actor_username}`}
                  >
                    <UserRound className="h-3 w-3 shrink-0" />
                    <span className="truncate">{selectedSummary.actor_username}</span>
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    {selectedSummary.integrity === "complete" ? (
                      <FileCheck2 className="h-3 w-3 text-emerald-600" />
                    ) : (
                      <AlertTriangle className="h-3 w-3 text-amber-600" />
                    )}
                    {selectedSummary.integrity === "complete" ? "记录完整" : "记录不完整"}
                  </span>
                </div>
              </div>

              <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-5">
                {detailLoading ? (
                  <div className="flex h-36 items-center justify-center gap-2 text-[12px] text-muted-foreground">
                    <RefreshCcw className="h-4 w-4 animate-spin" />
                    正在加载追溯详情
                  </div>
                ) : detail ? (
                  <div className="mx-auto max-w-3xl">
                    {detailAnomalies[0] ? (
                      <AnomalySummary
                        anomaly={detailAnomalies[0]}
                        count={detailAnomalies.length}
                        onLocate={() => firstAnomalyRef.current?.scrollIntoView({
                          behavior: "smooth",
                          block: "center",
                        })}
                      />
                    ) : null}
                    <div className="mb-4 flex items-center justify-between">
                      <h3 className="text-[12px] font-semibold text-foreground">执行时间线</h3>
                      <span className="text-[10.5px] text-muted-foreground">
                        Trace {detail.trace_id.slice(0, 12)}
                      </span>
                    </div>
                    {detail.timeline.map((event, index) => {
                      const anomalyIndex = detailAnomalies.findIndex(
                        (anomaly) => anomaly.event_id === event.id,
                      );
                      return (
                        <TimelineItem
                          key={event.id}
                          event={event}
                          last={index === detail.timeline.length - 1}
                          highlighted={anomalyIndex >= 0}
                          anomalyNumber={anomalyIndex >= 0 ? anomalyIndex + 1 : undefined}
                          targetRef={anomalyIndex === 0 ? firstAnomalyRef : undefined}
                        />
                      );
                    })}
                  </div>
                ) : (
                  <div className="flex h-36 items-center justify-center gap-2 text-[12px] text-muted-foreground">
                    <CircleAlert className="h-4 w-4" />
                    追溯详情暂时不可用
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <FileCheck2 className="h-8 w-8 text-muted-foreground/45" />
              <p className="mt-3 text-[13px] font-medium text-foreground">暂无可查看的任务追溯</p>
              <p className="mt-1 text-[11px] text-muted-foreground">完成一次任务后，审计链路会出现在这里</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
