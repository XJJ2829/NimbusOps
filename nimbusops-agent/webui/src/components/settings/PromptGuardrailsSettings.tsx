import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Check,
  CircleAlert,
  Loader2,
  RotateCcw,
  ScanSearch,
  ShieldCheck,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchPromptGuardrails, updatePromptGuardrails } from "@/lib/api";
import type { PromptGuardrailsConfig, PromptGuardrailsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const EMPTY_CONFIG: PromptGuardrailsConfig = {
  enabled: false,
  directInputEnabled: true,
  indirectInputEnabled: true,
  scanCliOutputs: true,
  scanMcpOutputs: true,
  scanOtherToolOutputs: false,
  modelPath: "Prompt guard model",
  threshold: 0.5,
  maxScanChars: 4096,
  failClosed: true,
};

export function PromptGuardrailsSettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("settings.manage");
  const [payload, setPayload] = useState<PromptGuardrailsPayload | null>(null);
  const [draft, setDraft] = useState<PromptGuardrailsConfig>(EMPTY_CONFIG);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPayload = useCallback((next: PromptGuardrailsPayload) => {
    setPayload(next);
    setDraft(next.config);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      applyPayload(await fetchPromptGuardrails(token));
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  }, [applyPayload, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = payload
    ? JSON.stringify(draft) !== JSON.stringify(payload.config)
    : false;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      applyPayload(await updatePromptGuardrails(token, draft));
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    if (payload) applyPayload(payload);
  };

  const status = payload?.status;
  const stats = payload?.statistics;
  const visibleStatusError =
    status?.error && !isTransientClassifierError(status.error) ? status.error : null;
  const availableMemory = useMemo(() => {
    const memory = status?.runtimeMemory ?? {};
    return firstNumber(
      memory.gpuAvailableMb,
      memory.mpsRecommendedMaxMb,
      memory.systemAvailableMb,
    );
  }, [status]);

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        正在读取 Prompt Guardrails...
      </div>
    );
  }

  return (
    <div className="space-y-7">
      <section className="border-b border-border/45 pb-4">
        <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold">
          <ScanSearch className="h-3.5 w-3.5" />
          Prompt Guardrails
        </div>
        <p className="mt-3 max-w-[760px] text-[13px] leading-5 text-muted-foreground">
          在用户输入进入 Agent 前、工具结果进入模型上下文前做第一层提示词注入检测。
        </p>
      </section>

      {error ? (
        <div className="flex items-start gap-2 rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-3 text-[13px] text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          {error}
        </div>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricTile
          icon={ShieldCheck}
          label="直接扫描"
          value={stats?.scanned_direct_inputs ?? 0}
          caption={`拦截 ${stats?.blocked_direct_inputs ?? 0} 次`}
        />
        <MetricTile
          icon={ScanSearch}
          label="间接扫描"
          value={stats?.scanned_indirect_inputs ?? 0}
          caption={`拦截 ${stats?.blocked_indirect_inputs ?? 0} 次`}
        />
        <MetricTile
          icon={Check}
          label="模型加载"
          value={stats?.model_loads ?? 0}
          caption={statusLabel(status?.state)}
        />
        <MetricTile
          icon={CircleAlert}
          label="加载失败"
          value={stats?.load_failures ?? 0}
          caption={status?.device || "未检测"}
        />
      </section>

      <section className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
        <SettingRow
          title="启用 Prompt Guardrails"
          description="开启后加载 NimbusOps 本地提示词护栏模型，关闭时不会占用模型内存。"
        >
          <Switch
            checked={draft.enabled}
            disabled={!canManage}
            label="启用 Prompt Guardrails"
            onChange={(enabled) => setDraft((current) => ({ ...current, enabled }))}
          />
        </SettingRow>
        <SettingRow
          title="直接 Prompt 输入检测"
          description="用户消息进入 Agent 会话前进行检测。"
        >
          <Switch
            checked={draft.directInputEnabled}
            disabled={!canManage || !draft.enabled}
            label="直接 Prompt 输入检测"
            onChange={(directInputEnabled) =>
              setDraft((current) => ({ ...current, directInputEnabled }))
            }
          />
        </SettingRow>
        <SettingRow
          title="间接 Prompt 输入检测"
          description="工具输出进入模型上下文前进行检测。"
        >
          <Switch
            checked={draft.indirectInputEnabled}
            disabled={!canManage || !draft.enabled}
            label="间接 Prompt 输入检测"
            onChange={(indirectInputEnabled) =>
              setDraft((current) => ({ ...current, indirectInputEnabled }))
            }
          />
        </SettingRow>
      </section>

      <section className="grid items-stretch gap-3 lg:grid-cols-2">
        <div className="h-full rounded-[14px] border border-border/50 bg-card p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="text-[15px] font-semibold">模型加载状态</h3>
              <p className="mt-1 text-[12px] text-muted-foreground">
                服务启动时默认不加载；保存启用后才加载检测器。
              </p>
            </div>
            <StatusPill state={status?.state ?? "disabled"} />
          </div>
          <div className="mt-5 grid gap-3 text-[13px]">
            <InfoLine label="运行设备" value={status?.device || "未检测"} />
            <InfoLine label="预计占用" value={`${formatMb(status?.estimatedMemoryMb)} MB`} />
            <InfoLine
              label="当前可用"
              value={availableMemory === null ? "无法读取" : `${formatMb(availableMemory)} MB`}
            />
          </div>
          {visibleStatusError ? (
            <div className="mt-4 rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-2 text-[12px] leading-5 text-destructive">
              {visibleStatusError}
            </div>
          ) : null}
        </div>

        <div className="h-full overflow-hidden rounded-[14px] border border-border/50 bg-card">
          <SettingRow
            title="CLI 返回检测"
            description="扫描通过 CLI App 返回给 Agent 的文本。"
          >
            <Switch
              checked={draft.scanCliOutputs}
              disabled={!canManage || !draft.enabled || !draft.indirectInputEnabled}
              label="CLI 返回检测"
              onChange={(scanCliOutputs) =>
                setDraft((current) => ({ ...current, scanCliOutputs }))
              }
            />
          </SettingRow>
          <SettingRow
            title="MCP 工具返回检测"
            description="扫描本地或远程 MCP 工具返回给 Agent 的文本。"
          >
            <Switch
              checked={draft.scanMcpOutputs}
              disabled={!canManage || !draft.enabled || !draft.indirectInputEnabled}
              label="MCP 工具返回检测"
              onChange={(scanMcpOutputs) =>
                setDraft((current) => ({ ...current, scanMcpOutputs }))
              }
            />
          </SettingRow>
          <SettingRow
            title="其他工具返回检测"
            description="扫描 exec、read_file、web_fetch 等系统内置工具的返回。"
          >
            <Switch
              checked={draft.scanOtherToolOutputs}
              disabled={!canManage || !draft.enabled || !draft.indirectInputEnabled}
              label="其他工具返回检测"
              onChange={(scanOtherToolOutputs) =>
                setDraft((current) => ({ ...current, scanOtherToolOutputs }))
              }
            />
          </SettingRow>
        </div>
      </section>

      <section>
        <SectionHeader
          title="检测参数"
          description="阈值越低越敏感；最大扫描长度用于控制长日志、长网页和长工具输出的检测开销。"
        />
        <div className="grid gap-3 sm:grid-cols-3">
          <NumberField
            label="风险阈值"
            value={draft.threshold}
            step={0.05}
            min={0}
            max={1}
            disabled={!canManage}
            onChange={(threshold) => setDraft((current) => ({ ...current, threshold }))}
          />
          <NumberField
            label="最大扫描字符"
            value={draft.maxScanChars}
            step={256}
            min={256}
            max={32768}
            disabled={!canManage}
            onChange={(maxScanChars) =>
              setDraft((current) => ({ ...current, maxScanChars }))
            }
          />
          <div className="rounded-[14px] border border-border/50 bg-card p-5">
            <div className="mb-4 flex items-center gap-2 text-[13px] font-semibold">
              <CircleAlert className="h-4 w-4 text-muted-foreground" />
              加载失败策略
            </div>
            <Switch
              checked={draft.failClosed}
              disabled={!canManage}
              label="加载失败时阻断"
              onChange={(failClosed) => setDraft((current) => ({ ...current, failClosed }))}
            />
            <p className="mt-3 text-[12px] leading-5 text-muted-foreground">
              开启后，模型不可用时会阻断对应输入，避免检测层静默失效。
            </p>
          </div>
        </div>
      </section>

      <div className="flex items-center justify-end gap-2">
        <Button type="button" variant="outline" disabled={!dirty || saving} onClick={reset}>
          <RotateCcw className="mr-2 h-4 w-4" />
          重置
        </Button>
        <Button type="button" disabled={!canManage || !dirty || saving} onClick={save}>
          {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}
          {draft.enabled ? "保存并加载" : "保存设置"}
        </Button>
      </div>
    </div>
  );
}

function MetricTile({
  icon: Icon,
  label,
  value,
  caption,
}: {
  icon: typeof ShieldCheck;
  label: string;
  value: number | string;
  caption: string;
}) {
  return (
    <div className="rounded-[12px] border border-border/45 bg-card px-4 py-4">
      <div className="flex items-center gap-2 text-[11px] font-semibold text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-3 text-[20px] font-semibold tracking-normal">{value}</div>
      <div className="mt-1 text-[11px] leading-4 text-muted-foreground">{caption}</div>
    </div>
  );
}

function SettingRow({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-4 border-b border-border/45 px-5 py-4 last:border-b-0 sm:grid-cols-[1fr_auto] sm:items-center">
      <div>
        <div className="text-[13px] font-semibold">{title}</div>
        <p className="mt-1 text-[12px] leading-5 text-muted-foreground">{description}</p>
      </div>
      <div className="flex justify-start sm:justify-end">{children}</div>
    </div>
  );
}

function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-4 border-b border-border/45 pb-3">
      <h3 className="text-[15px] font-semibold">{title}</h3>
      <p className="mt-1 text-[12px] text-muted-foreground">{description}</p>
    </div>
  );
}

function NumberField({
  label,
  value,
  disabled,
  onChange,
  min,
  max,
  step = 1,
}: {
  label: string;
  value: number;
  disabled: boolean;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
}) {
  return (
    <label className="rounded-[14px] border border-border/50 bg-card p-5">
      <span className="block text-[13px] font-semibold text-muted-foreground">{label}</span>
      <Input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        className="mt-5 h-11 rounded-[12px]"
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, next)));
        }}
      />
    </label>
  );
}

function Switch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full p-[2px]",
        "transition-colors duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50",
        checked
          ? "bg-[#2997FF] shadow-[inset_0_0_0_1px_rgba(0,0,0,0.035)]"
          : "bg-muted shadow-[inset_0_0_0_1px_rgba(0,0,0,0.035)] hover:bg-muted/80",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-[18px] w-[18px] rounded-full bg-background shadow-[0_1px_2px_rgba(0,0,0,0.18),0_2px_7px_rgba(0,0,0,0.11)]",
          "transition-transform duration-200 ease-out",
          checked ? "translate-x-[16px]" : "translate-x-0",
        )}
      />
    </button>
  );
}

function StatusPill({ state }: { state: string }) {
  const label = statusLabel(state);
  return (
    <span
      className={cn(
        "rounded-full px-2.5 py-1 text-[12px] font-semibold",
        state === "loaded" && "bg-emerald-50 text-emerald-700",
        state === "error" && "bg-destructive/10 text-destructive",
        state !== "loaded" && state !== "error" && "bg-muted text-muted-foreground",
      )}
    >
      {label}
    </span>
  );
}

function InfoLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border/40 pb-2 last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="max-w-[70%] truncate font-medium">{value}</span>
    </div>
  );
}

function statusLabel(state?: string) {
  if (state === "loaded") return "已加载";
  if (state === "not_loaded") return "未加载";
  if (state === "error") return "加载失败";
  return "已关闭";
}

function formatMb(value: number | null | undefined) {
  if (typeof value !== "number" || Number.isNaN(value)) return "--";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 1 });
}

function firstNumber(...values: Array<unknown>) {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function isTransientClassifierError(error: string) {
  return error.includes("Prompt Guardrails classifier returned empty content");
}
