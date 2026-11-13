import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  CalendarClock,
  CircleAlert,
  Clock3,
  MessageCircle,
  Pencil,
  RefreshCcw,
  Repeat2,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { StyledSelect } from "@/components/ui/styled-select";
import { Textarea } from "@/components/ui/textarea";
import { useSessionAutomationJobs } from "@/hooks/useSessionAutomationJobs";
import { currentLocale } from "@/i18n";
import { createSessionAutomation } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import type {
  ChatSummary,
  SessionAutomationCreateInput,
  SessionAutomationJob,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type ManualScheduleMode = "at" | "every" | "cron";
type EveryUnit = "minutes" | "hours" | "days";

const RELATIVE_THRESHOLDS: [number, Intl.RelativeTimeFormatUnit][] = [
  [60, "second"],
  [60, "minute"],
  [24, "hour"],
  [7, "day"],
  [4.345, "week"],
  [12, "month"],
  [Number.POSITIVE_INFINITY, "year"],
];

const EVERY_UNIT_MS: Record<EveryUnit, number> = {
  minutes: 60_000,
  hours: 3_600_000,
  days: 86_400_000,
};

interface AutomationsViewProps {
  session: ChatSummary | null;
  title: string;
  token: string;
  onCreateWithChat: () => void;
}

export function AutomationsView({
  session,
  title,
  token,
  onCreateWithChat,
}: AutomationsViewProps) {
  const { t } = useTranslation();
  const [manualOpen, setManualOpen] = useState(false);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const sessionKey = session?.key ?? "";
  const { jobs, loading, loadFailed, now } = useSessionAutomationJobs(
    Boolean(sessionKey),
    token,
    sessionKey,
    refreshSignal,
  );

  return (
    <section className="flex h-full min-w-0 flex-col overflow-hidden bg-background">
      <div className="flex shrink-0 flex-col gap-3 border-b border-border/55 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <CalendarClock className="h-4 w-4 shrink-0 text-muted-foreground" />
            <h1 className="truncate text-[18px] font-semibold text-foreground">
              {t("automations.title", { defaultValue: "自动运维" })}
            </h1>
          </div>
          <div className="mt-1 truncate text-[12.5px] text-muted-foreground">
            {session
              ? title || t("thread.sessionInfo.untitled")
              : t("automations.noSession", { defaultValue: "暂无当前会话" })}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!sessionKey}
            onClick={() => setRefreshSignal((value) => value + 1)}
            className="h-9 gap-2 rounded-lg"
          >
            <RefreshCcw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            {t("automations.refresh", { defaultValue: "刷新" })}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onCreateWithChat}
            className="h-9 gap-2 rounded-lg"
          >
            <MessageCircle className="h-3.5 w-3.5" />
            {t("automations.createWithChat", { defaultValue: "通过聊天创建" })}
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={!sessionKey}
            onClick={() => setManualOpen(true)}
            className="h-9 gap-2 rounded-lg"
          >
            <Pencil className="h-3.5 w-3.5" />
            {t("automations.manualCreate", { defaultValue: "手动创建" })}
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {!session ? (
          <EmptyPanel
            label={t("automations.selectSession", {
              defaultValue: "打开一个会话后可查看和手动创建自动运维。",
            })}
          />
        ) : loading ? (
          <StatePanel icon={<RefreshCcw className="h-4 w-4 animate-spin" />} label={t("thread.sessionInfo.loading")} />
        ) : loadFailed ? (
          <StatePanel
            tone="danger"
            icon={<CircleAlert className="h-4 w-4" />}
            label={t("thread.sessionInfo.loadFailed")}
          />
        ) : jobs.length ? (
          <div className="space-y-2">
            {jobs.map((job) => (
              <AutomationListRow key={job.id} job={job} now={now} />
            ))}
          </div>
        ) : (
          <EmptyPanel label={t("thread.sessionInfo.empty")} />
        )}
      </div>

      <ManualAutomationDialog
        open={manualOpen}
        onOpenChange={setManualOpen}
        token={token}
        sessionKey={sessionKey}
        onCreated={() => setRefreshSignal((value) => value + 1)}
      />
    </section>
  );
}

function StatePanel({
  icon,
  label,
  tone = "muted",
}: {
  icon: ReactNode;
  label: string;
  tone?: "muted" | "danger";
}) {
  return (
    <div
      className={cn(
        "flex min-h-[160px] items-center justify-center rounded-lg border border-dashed px-4 text-sm",
        tone === "danger"
          ? "border-destructive/25 bg-destructive/5 text-destructive"
          : "border-border/70 bg-muted/20 text-muted-foreground",
      )}
    >
      <div className="flex items-center gap-2">
        {icon}
        <span>{label}</span>
      </div>
    </div>
  );
}

function EmptyPanel({ label }: { label: string }) {
  return (
    <StatePanel
      icon={<CalendarClock className="h-4 w-4" />}
      label={label}
    />
  );
}

function AutomationListRow({ job, now }: { job: SessionAutomationJob; now: number }) {
  const { t } = useTranslation();
  const schedule = formatSchedule(job, t);
  const nextRun = formatNextRun(job, t, now);
  const statusClass = job.enabled
    ? job.state.last_status === "error"
      ? "bg-destructive"
      : "bg-emerald-500"
    : "bg-muted-foreground/35";

  return (
    <article className="rounded-lg border border-border/60 bg-card px-4 py-3">
      <div className="flex min-w-0 items-start gap-3">
        <span className={cn("mt-2 h-2 w-2 shrink-0 rounded-full", statusClass)} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 className="min-w-0 truncate text-[14px] font-semibold text-foreground">
              {job.name}
            </h2>
            {!job.enabled ? (
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                {t("thread.sessionInfo.disabled")}
              </span>
            ) : null}
          </div>
          <p className="mt-1 line-clamp-2 text-[12.5px] leading-relaxed text-muted-foreground">
            {job.payload.message}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted-foreground/85">
            <span className="inline-flex items-center gap-1.5">
              <Repeat2 className="h-3.5 w-3.5" />
              {schedule}
            </span>
            <span className="inline-flex items-center gap-1.5" title={nextRun.title}>
              <Clock3 className="h-3.5 w-3.5" />
              {nextRun.label}
            </span>
          </div>
        </div>
      </div>
    </article>
  );
}

function ManualAutomationDialog({
  open,
  onOpenChange,
  token,
  sessionKey,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  token: string;
  sessionKey: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState<ManualScheduleMode>("at");
  const [atValue, setAtValue] = useState(() => defaultDateTimeParts());
  const [everyValue, setEveryValue] = useState("1");
  const [everyUnit, setEveryUnit] = useState<EveryUnit>("hours");
  const [cronExpr, setCronExpr] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState(() => defaultTimezone());
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = Boolean(sessionKey && name.trim() && message.trim() && !submitting);

  const reset = () => {
    setName("");
    setMessage("");
    setMode("at");
    setAtValue(defaultDateTimeParts());
    setEveryValue("1");
    setEveryUnit("hours");
    setCronExpr("0 9 * * *");
    setTimezone(defaultTimezone());
    setError(null);
    setSubmitting(false);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;
    const schedule = buildScheduleInput(mode, {
      atDate: atValue.date,
      atTime: atValue.time,
      everyValue,
      everyUnit,
      cronExpr,
      timezone,
    });
    if (!schedule) {
      setError(t("automations.dialog.invalidSchedule", { defaultValue: "计划时间无效。" }));
      return;
    }

    const payload: SessionAutomationCreateInput = {
      name: name.trim(),
      message: message.trim(),
      schedule,
    };
    setSubmitting(true);
    setError(null);
    try {
      await createSessionAutomation(token, sessionKey, payload);
      onCreated();
      reset();
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message || t("automations.dialog.createFailed", { defaultValue: "创建失败。" }));
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        onOpenChange(nextOpen);
        if (!nextOpen) reset();
      }}
    >
      <DialogContent className="max-w-xl rounded-lg">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>{t("automations.dialog.title", { defaultValue: "手动创建自动运维" })}</DialogTitle>
            <DialogDescription>
              {t("automations.dialog.description", { defaultValue: "为当前会话创建一个自动运维。" })}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3">
            <label className="grid gap-1.5 text-[12.5px] font-medium text-foreground">
              {t("automations.dialog.name", { defaultValue: "名称" })}
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t("automations.dialog.namePlaceholder", { defaultValue: "每日巡检" })}
                className="rounded-lg"
              />
            </label>
            <label className="grid gap-1.5 text-[12.5px] font-medium text-foreground">
              {t("automations.dialog.message", { defaultValue: "任务内容" })}
              <Textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder={t("automations.dialog.messagePlaceholder", { defaultValue: "检查系统状态并汇总异常。" })}
                className="min-h-[104px] rounded-lg"
              />
            </label>

            <div className="grid gap-2">
              <div className="text-[12.5px] font-medium text-foreground">
                {t("automations.dialog.schedule", { defaultValue: "计划" })}
              </div>
              <div className="grid grid-cols-3 gap-1 rounded-lg bg-muted/45 p-1">
                <ModeButton active={mode === "at"} onClick={() => setMode("at")}>
                  {t("automations.dialog.once", { defaultValue: "一次" })}
                </ModeButton>
                <ModeButton active={mode === "every"} onClick={() => setMode("every")}>
                  {t("automations.dialog.interval", { defaultValue: "间隔" })}
                </ModeButton>
                <ModeButton active={mode === "cron"} onClick={() => setMode("cron")}>
                  Cron
                </ModeButton>
              </div>

              {mode === "at" ? (
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_9rem]">
                  <Input
                    type="date"
                    value={atValue.date}
                    aria-label={t("automations.dialog.date", { defaultValue: "日期" })}
                    onChange={(event) =>
                      setAtValue((current) => ({ ...current, date: event.target.value }))
                    }
                    className="rounded-lg"
                  />
                  <Input
                    type="time"
                    value={atValue.time}
                    aria-label={t("automations.dialog.time", { defaultValue: "时间" })}
                    onChange={(event) =>
                      setAtValue((current) => ({ ...current, time: event.target.value }))
                    }
                    className="rounded-lg"
                  />
                </div>
              ) : null}
              {mode === "every" ? (
                <div className="grid grid-cols-[minmax(0,1fr)_8rem] gap-2">
                  <Input
                    type="number"
                    min="1"
                    value={everyValue}
                    onChange={(event) => setEveryValue(event.target.value)}
                    className="rounded-lg"
                  />
                  <StyledSelect
                    value={everyUnit}
                    onChange={(event) => setEveryUnit(event.target.value as EveryUnit)}
                  >
                    <option value="minutes">{t("automations.dialog.minutes", { defaultValue: "分钟" })}</option>
                    <option value="hours">{t("automations.dialog.hours", { defaultValue: "小时" })}</option>
                    <option value="days">{t("automations.dialog.days", { defaultValue: "天" })}</option>
                  </StyledSelect>
                </div>
              ) : null}
              {mode === "cron" ? (
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_11rem]">
                  <Input
                    value={cronExpr}
                    onChange={(event) => setCronExpr(event.target.value)}
                    placeholder="0 9 * * *"
                    className="rounded-lg font-mono"
                  />
                  <Input
                    value={timezone}
                    onChange={(event) => setTimezone(event.target.value)}
                    placeholder="Asia/Shanghai"
                    className="rounded-lg"
                  />
                </div>
              ) : null}
            </div>
          </div>

          {error ? (
            <div className="rounded-lg border border-destructive/25 bg-destructive/5 px-3 py-2 text-[12px] text-destructive">
              {error}
            </div>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              className="rounded-lg"
            >
              {t("common.cancel", { defaultValue: "取消" })}
            </Button>
            <Button type="submit" disabled={!canSubmit} className="rounded-lg">
              {submitting ? (
                <RefreshCcw className="mr-2 h-3.5 w-3.5 animate-spin" />
              ) : null}
              {t("automations.dialog.create", { defaultValue: "创建" })}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "h-8 rounded-md px-2 text-[12.5px] font-medium transition-colors",
        active
          ? "bg-background text-foreground shadow-sm"
          : "text-muted-foreground hover:bg-background/55 hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function buildScheduleInput(
  mode: ManualScheduleMode,
  values: {
    atDate: string;
    atTime: string;
    everyValue: string;
    everyUnit: EveryUnit;
    cronExpr: string;
    timezone: string;
  },
): SessionAutomationCreateInput["schedule"] | null {
  if (mode === "at") {
    if (!values.atDate || !values.atTime) return null;
    const atMs = new Date(`${values.atDate}T${values.atTime}`).getTime();
    return Number.isFinite(atMs) ? { kind: "at", at_ms: atMs } : null;
  }
  if (mode === "every") {
    const count = Number(values.everyValue);
    if (!Number.isFinite(count) || count <= 0) return null;
    return { kind: "every", every_ms: Math.round(count * EVERY_UNIT_MS[values.everyUnit]) };
  }
  const expr = values.cronExpr.trim();
  if (!expr) return null;
  return { kind: "cron", expr, tz: values.timezone.trim() || null };
}

function defaultDateTimeParts(): { date: string; time: string } {
  const next = new Date(Date.now() + 60 * 60 * 1000);
  next.setMinutes(0, 0, 0);
  const pad = (value: number) => String(value).padStart(2, "0");
  const date = [
    next.getFullYear(),
    "-",
    pad(next.getMonth() + 1),
    "-",
    pad(next.getDate()),
  ].join("");
  return { date, time: `${pad(next.getHours())}:${pad(next.getMinutes())}` };
}

function defaultTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  } catch {
    return "Asia/Shanghai";
  }
}

function formatSchedule(job: SessionAutomationJob, t: TFunction) {
  const locale = currentLocale();
  if (job.schedule.kind === "at" && job.schedule.at_ms) {
    return t("thread.sessionInfo.schedule.at", { time: fmtDateTime(job.schedule.at_ms, locale) });
  }
  if (job.schedule.kind === "every" && job.schedule.every_ms) {
    return t("thread.sessionInfo.schedule.every", {
      duration: formatDuration(job.schedule.every_ms, locale),
    });
  }
  if (job.schedule.kind === "cron" && job.schedule.expr) {
    return job.schedule.tz
      ? t("thread.sessionInfo.schedule.cronWithTz", {
          expr: job.schedule.expr,
          tz: job.schedule.tz,
        })
      : t("thread.sessionInfo.schedule.cron", { expr: job.schedule.expr });
  }
  return t("thread.sessionInfo.schedule.unknown");
}

function formatNextRun(job: SessionAutomationJob, t: TFunction, now: number) {
  const locale = currentLocale();
  if (!job.enabled) {
    return { label: t("thread.sessionInfo.next.disabled"), title: "" };
  }
  const next = job.state.next_run_at_ms;
  if (!next) {
    return { label: t("thread.sessionInfo.next.none"), title: "" };
  }
  return {
    label: t("thread.sessionInfo.next.label", { time: relativeTimeFrom(next, now, locale) }),
    title: fmtDateTime(next, locale),
  };
}

function relativeTimeFrom(value: number, now: number, locale: string): string {
  let delta = (value - now) / 1000;
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [step, unit] of RELATIVE_THRESHOLDS) {
    if (Math.abs(delta) < step) {
      return formatter.format(Math.round(delta), unit);
    }
    delta /= step;
  }
  return formatter.format(Math.round(delta), "year");
}

function formatDuration(ms: number, locale: string): string {
  const units: Array<[Intl.NumberFormatOptions["unit"], number]> = [
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
    ["second", 1000],
  ];
  for (const [unit, size] of units) {
    if (ms >= size && ms % size === 0) {
      return new Intl.NumberFormat(locale, {
        style: "unit",
        unit,
        unitDisplay: "long",
        maximumFractionDigits: 0,
      }).format(ms / size);
    }
  }
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: "minute",
    unitDisplay: "long",
    maximumFractionDigits: 1,
  }).format(ms / 60_000);
}
