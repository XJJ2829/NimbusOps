import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Braces,
  Check,
  CircleAlert,
  ListChecks,
  Loader2,
  Plus,
  RotateCcw,
  ShieldCheck,
  Trash2,
  Wrench,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StyledSelect } from "@/components/ui/styled-select";
import { fetchActionGuardrails, updateActionGuardrails } from "@/lib/api";
import type {
  ActionGuardrailsConfig,
  ActionGuardrailsPayload,
  ActionGuardrailsRequiredStepScope,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

interface PrerequisiteDraft {
  id: string;
  actionTool: string;
  prerequisiteTool: string;
  matchArg: string;
}

interface RequiredStepScopeDraft {
  id: string;
  scopeType: "session" | "automation";
  scopeId: string;
  steps: string;
}

const EMPTY_CONFIG: ActionGuardrailsConfig = {
  enabled: true,
  rescueToolCalls: true,
  maxResponseRetries: 3,
  maxToolErrors: 2,
  maxPrerequisiteViolations: 2,
  requiredSteps: [],
  requiredStepScopes: [],
  toolPrerequisites: {},
};

export function ActionGuardrailsSettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("settings.manage");
  const [payload, setPayload] = useState<ActionGuardrailsPayload | null>(null);
  const [draft, setDraft] = useState<ActionGuardrailsConfig>(EMPTY_CONFIG);
  const [prerequisites, setPrerequisites] = useState<PrerequisiteDraft[]>([]);
  const [requiredSteps, setRequiredSteps] = useState("");
  const [requiredStepScopes, setRequiredStepScopes] = useState<RequiredStepScopeDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPayload = useCallback((next: ActionGuardrailsPayload) => {
    setPayload(next);
    setDraft(next.config);
    setRequiredSteps(next.config.requiredSteps.join(", "));
    setRequiredStepScopes(flattenRequiredStepScopes(next.config.requiredStepScopes ?? []));
    setPrerequisites(flattenPrerequisites(next.config.toolPrerequisites));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      applyPayload(await fetchActionGuardrails(token));
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  }, [applyPayload, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const nextConfig = useMemo<ActionGuardrailsConfig>(() => ({
    ...draft,
    requiredSteps: splitToolList(requiredSteps),
    requiredStepScopes: buildRequiredStepScopes(requiredStepScopes),
    toolPrerequisites: buildPrerequisites(prerequisites),
  }), [draft, prerequisites, requiredStepScopes, requiredSteps]);

  const dirty = payload
    ? JSON.stringify(canonicalConfig(nextConfig))
      !== JSON.stringify(canonicalConfig(payload.config))
    : false;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      applyPayload(await updateActionGuardrails(token, nextConfig));
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    if (payload) applyPayload(payload);
  };

  const addPrerequisite = () => {
    setPrerequisites((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        actionTool: "",
        prerequisiteTool: "",
        matchArg: "",
      },
    ]);
  };

  const addRequiredStepScope = () => {
    const session = payload?.scopeOptions?.sessions?.[0];
    const automation = payload?.scopeOptions?.automations?.[0];
    const scopeType = session ? "session" : "automation";
    setRequiredStepScopes((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        scopeType,
        scopeId: session?.id ?? automation?.id ?? "",
        steps: "",
      },
    ]);
  };

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        正在读取 Action Guardrails...
      </div>
    );
  }

  const stats = payload?.statistics;

  return (
    <div className="space-y-7">
      <section className="border-b border-border/45 pb-4">
        <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold">
          <ShieldCheck className="h-3.5 w-3.5" />
          Action Guardrails
        </div>
        <p className="mt-3 max-w-[760px] text-[13px] leading-5 text-muted-foreground">
          在模型生成动作之后、工具执行之前校验调用，并对格式错误、重复失败和缺失前置步骤进行纠正。
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
          icon={Braces}
          label="已恢复调用"
          value={stats?.rescued_tool_calls ?? 0}
          caption="从普通文本恢复为工具调用"
        />
        <MetricTile
          icon={Wrench}
          label="无效调用"
          value={stats?.blocked_invalid_calls ?? 0}
          caption="工具名或参数校验未通过"
        />
        <MetricTile
          icon={ListChecks}
          label="前置依赖"
          value={stats?.blocked_prerequisites ?? 0}
          caption="动作缺少必要的先行步骤"
        />
        <MetricTile
          icon={CircleAlert}
          label="预算终止"
          value={stats?.stopped_error_budgets ?? 0}
          caption="连续错误超过允许次数"
        />
      </section>

      <section className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
        <SettingRow
          title="启用 Action Guardrails"
          description="对主 Agent 和 Subagent 的所有工具调用生效。"
        >
          <Switch
            checked={draft.enabled}
            disabled={!canManage}
            label="启用 Action Guardrails"
            onChange={(enabled) => setDraft((current) => ({ ...current, enabled }))}
          />
        </SettingRow>
        <SettingRow
          title="工具调用抢救"
          description="识别模型误写到普通文本中的 JSON、XML 和原生工具调用格式。"
        >
          <Switch
            checked={draft.rescueToolCalls}
            disabled={!canManage || !draft.enabled}
            label="工具调用抢救"
            onChange={(rescueToolCalls) =>
              setDraft((current) => ({ ...current, rescueToolCalls }))
            }
          />
        </SettingRow>
      </section>

      <section>
        <SectionHeader
          title="错误预算"
          description="连续异常超过预算后结束当前回合，防止 Agent 陷入无效重试。"
        />
        <div className="grid gap-3 sm:grid-cols-3">
          <NumberField
            label="响应纠正次数"
            value={draft.maxResponseRetries}
            disabled={!canManage}
            onChange={(maxResponseRetries) =>
              setDraft((current) => ({ ...current, maxResponseRetries }))
            }
          />
          <NumberField
            label="工具错误次数"
            value={draft.maxToolErrors}
            disabled={!canManage}
            onChange={(maxToolErrors) =>
              setDraft((current) => ({ ...current, maxToolErrors }))
            }
          />
          <NumberField
            label="依赖违规次数"
            value={draft.maxPrerequisiteViolations}
            disabled={!canManage}
            onChange={(maxPrerequisiteViolations) =>
              setDraft((current) => ({ ...current, maxPrerequisiteViolations }))
            }
          />
        </div>
      </section>

      <section>
        <SectionHeader
          title="必需步骤"
          description="按适用范围要求工具在当前回合结束前至少成功执行一次，自动化任务优先于指定对话。"
          action={canManage ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={addRequiredStepScope}
              disabled={
                !(payload?.scopeOptions?.sessions?.length)
                && !(payload?.scopeOptions?.automations?.length)
              }
            >
              <Plus className="mr-1.5 h-4 w-4" />
              添加范围
            </Button>
          ) : null}
        />
        <div className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
          <div className="grid gap-3 border-b border-border/45 px-4 py-4 md:grid-cols-[220px_1fr] md:items-center">
            <div>
              <div className="text-[12px] font-semibold">全部对话</div>
              <div className="mt-1 text-[11px] leading-4 text-muted-foreground">
                没有更具体规则时使用的全局默认值
              </div>
            </div>
            <Input
              value={requiredSteps}
              disabled={!canManage}
              onChange={(event) => setRequiredSteps(event.target.value)}
              placeholder="留空表示普通对话没有必需工具"
              className="h-10 rounded-[10px] border-border/55 bg-background px-3 font-mono text-[12px]"
              list="action-guardrails-tools"
            />
          </div>

          {requiredStepScopes.map((rule) => (
            <RequiredStepScopeRow
              key={rule.id}
              rule={rule}
              payload={payload}
              disabled={!canManage}
              onChange={(next) =>
                setRequiredStepScopes((current) =>
                  current.map((item) => item.id === next.id ? next : item)
                )
              }
              onDelete={() =>
                setRequiredStepScopes((current) =>
                  current.filter((item) => item.id !== rule.id)
                )
              }
            />
          ))}

          {!requiredStepScopes.length ? (
            <div className="px-4 py-5 text-center text-[11.5px] text-muted-foreground">
              当前没有指定对话或自动化任务规则。
            </div>
          ) : null}
        </div>
      </section>

      <section>
        <SectionHeader
          title="工具前置依赖"
          description="指定某个工具执行前必须成功调用的工具，可按同名参数匹配。"
          action={canManage ? (
            <Button type="button" size="sm" variant="outline" onClick={addPrerequisite}>
              <Plus className="mr-1.5 h-4 w-4" />
              添加依赖
            </Button>
          ) : null}
        />
        {prerequisites.length ? (
          <div className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
            {prerequisites.map((rule) => (
              <PrerequisiteRow
                key={rule.id}
                rule={rule}
                disabled={!canManage}
                onChange={(next) =>
                  setPrerequisites((current) =>
                    current.map((item) => item.id === next.id ? next : item)
                  )
                }
                onDelete={() =>
                  setPrerequisites((current) =>
                    current.filter((item) => item.id !== rule.id)
                  )
                }
              />
            ))}
          </div>
        ) : (
          <div className="rounded-[14px] border border-dashed border-border/60 px-4 py-8 text-center text-[12px] text-muted-foreground">
            当前没有配置工具前置依赖。
          </div>
        )}
      </section>

      <section>
        <SectionHeader
          title="已启用能力"
          description="这些能力由当前 Action Guardrails 运行时直接提供。"
        />
        <div className="grid gap-3 sm:grid-cols-2">
          {(payload?.capabilities ?? []).map((capability) => (
            <div
              key={capability.id}
              className="flex min-h-[86px] items-start gap-3 rounded-[12px] border border-border/45 bg-card px-4 py-4"
            >
              <div
                className={cn(
                  "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px]",
                  capability.enabled
                    ? "bg-[#2997FF]/10 text-[#1683E8]"
                    : "bg-muted text-muted-foreground",
                )}
              >
                <Check className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-[13px] font-semibold">{capability.name}</div>
                <div className="mt-1 text-[11.5px] leading-5 text-muted-foreground">
                  {capability.description}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      <datalist id="action-guardrails-tools">
        {(payload?.available_tools ?? []).map((tool) => (
          <option key={tool} value={tool} />
        ))}
      </datalist>

      {canManage ? (
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" disabled={!dirty || saving} onClick={reset}>
            <RotateCcw className="mr-2 h-4 w-4" />
            重置
          </Button>
          <Button type="button" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Check className="mr-2 h-4 w-4" />
            )}
            保存 Guardrails
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function MetricTile({
  icon: Icon,
  label,
  value,
  caption,
}: {
  icon: typeof Braces;
  label: string;
  value: number;
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
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[72px] flex-col gap-3 border-b border-border/45 px-4 py-4 last:border-b-0 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold">{title}</div>
        <div className="mt-1 text-[11.5px] leading-5 text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function SectionHeader({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4 border-b border-border/45 pb-3">
      <div>
        <div className="text-[13px] font-semibold">{title}</div>
        <div className="mt-1 text-[11.5px] leading-5 text-muted-foreground">{description}</div>
      </div>
      {action}
    </div>
  );
}

function NumberField({
  label,
  value,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block rounded-[12px] border border-border/45 bg-card px-4 py-4">
      <span className="text-[11px] font-semibold text-muted-foreground">{label}</span>
      <Input
        type="number"
        min={0}
        max={20}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(clampBudget(event.target.value))}
        className="mt-3 h-10 rounded-[10px] border-border/55 bg-background font-mono text-[13px]"
      />
    </label>
  );
}

function PrerequisiteRow({
  rule,
  disabled,
  onChange,
  onDelete,
}: {
  rule: PrerequisiteDraft;
  disabled: boolean;
  onChange: (rule: PrerequisiteDraft) => void;
  onDelete: () => void;
}) {
  return (
    <div className="grid gap-3 border-b border-border/45 px-4 py-4 last:border-b-0 md:grid-cols-[1fr_1fr_0.8fr_auto] md:items-end">
      <Field label="目标工具">
        <Input
          value={rule.actionTool}
          disabled={disabled}
          list="action-guardrails-tools"
          onChange={(event) => onChange({ ...rule, actionTool: event.target.value })}
          placeholder="write_file"
          className="h-10 rounded-[10px] font-mono text-[12px]"
        />
      </Field>
      <Field label="前置工具">
        <Input
          value={rule.prerequisiteTool}
          disabled={disabled}
          list="action-guardrails-tools"
          onChange={(event) =>
            onChange({ ...rule, prerequisiteTool: event.target.value })
          }
          placeholder="read_file"
          className="h-10 rounded-[10px] font-mono text-[12px]"
        />
      </Field>
      <Field label="匹配参数（可选）">
        <Input
          value={rule.matchArg}
          disabled={disabled}
          onChange={(event) => onChange({ ...rule, matchArg: event.target.value })}
          placeholder="path"
          className="h-10 rounded-[10px] font-mono text-[12px]"
        />
      </Field>
      {!disabled ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="删除依赖"
          title="删除依赖"
          onClick={onDelete}
          className="h-10 w-10 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      ) : null}
    </div>
  );
}

function RequiredStepScopeRow({
  rule,
  payload,
  disabled,
  onChange,
  onDelete,
}: {
  rule: RequiredStepScopeDraft;
  payload: ActionGuardrailsPayload | null;
  disabled: boolean;
  onChange: (rule: RequiredStepScopeDraft) => void;
  onDelete: () => void;
}) {
  const options = rule.scopeType === "session"
    ? payload?.scopeOptions?.sessions ?? []
    : payload?.scopeOptions?.automations ?? [];
  const selectedExists = options.some((option) => option.id === rule.scopeId);

  return (
    <div className="grid gap-3 border-b border-border/45 px-4 py-4 last:border-b-0 md:grid-cols-[180px_minmax(220px,0.9fr)_minmax(260px,1.2fr)_auto] md:items-end">
      <Field label="适用范围">
        <StyledSelect
          value={rule.scopeType}
          disabled={disabled}
          onChange={(event) => {
            const scopeType = event.target.value as RequiredStepScopeDraft["scopeType"];
            const nextOptions = scopeType === "session"
              ? payload?.scopeOptions?.sessions ?? []
              : payload?.scopeOptions?.automations ?? [];
            onChange({
              ...rule,
              scopeType,
              scopeId: nextOptions[0]?.id ?? "",
            });
          }}
        >
          <option value="session">指定对话</option>
          <option value="automation">指定自动化任务</option>
        </StyledSelect>
      </Field>
      <Field label={rule.scopeType === "session" ? "选择对话" : "选择自动化任务"}>
        <StyledSelect
          value={rule.scopeId}
          disabled={disabled || !options.length}
          onChange={(event) => onChange({ ...rule, scopeId: event.target.value })}
        >
          {!options.length ? <option value="">暂无可选项</option> : null}
          {!selectedExists && rule.scopeId ? (
            <option value={rule.scopeId}>{rule.scopeId}</option>
          ) : null}
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {option.name}
            </option>
          ))}
        </StyledSelect>
      </Field>
      <Field label="必需工具">
        <Input
          value={rule.steps}
          disabled={disabled}
          onChange={(event) => onChange({ ...rule, steps: event.target.value })}
          placeholder="例如：get_system_metrics, check_service_status"
          className="h-10 rounded-[10px] bg-background font-mono text-[12px]"
          list="action-guardrails-tools"
        />
      </Field>
      {!disabled ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="删除作用域规则"
          title="删除作用域规则"
          onClick={onDelete}
          className="h-10 w-10 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      ) : null}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 block text-[11px] font-semibold text-muted-foreground">
        {label}
      </span>
      {children}
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
  disabled: boolean;
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

function clampBudget(raw: string): number {
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) return 0;
  return Math.min(20, Math.max(0, value));
}

function splitToolList(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\s,，]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function flattenRequiredStepScopes(
  values: ActionGuardrailsRequiredStepScope[],
): RequiredStepScopeDraft[] {
  return values.map((value) => ({
    id: crypto.randomUUID(),
    scopeType: value.scopeType,
    scopeId: value.scopeId,
    steps: value.steps.join(", "),
  }));
}

function buildRequiredStepScopes(
  rows: RequiredStepScopeDraft[],
): ActionGuardrailsRequiredStepScope[] {
  return rows.flatMap((row) => {
    const scopeId = row.scopeId.trim();
    const steps = splitToolList(row.steps);
    if (!scopeId || !steps.length) return [];
    return [{
      scopeType: row.scopeType,
      scopeId,
      steps,
    }];
  });
}

function flattenPrerequisites(
  values: ActionGuardrailsConfig["toolPrerequisites"],
): PrerequisiteDraft[] {
  return Object.entries(values).flatMap(([actionTool, definitions]) =>
    definitions.map((definition) => ({
      id: crypto.randomUUID(),
      actionTool,
      prerequisiteTool:
        typeof definition === "string" ? definition : definition.tool,
      matchArg:
        typeof definition === "string" ? "" : definition.matchArg ?? "",
    })),
  );
}

function buildPrerequisites(
  rows: PrerequisiteDraft[],
): ActionGuardrailsConfig["toolPrerequisites"] {
  const result: ActionGuardrailsConfig["toolPrerequisites"] = {};
  rows.forEach((row) => {
    const actionTool = row.actionTool.trim();
    const prerequisiteTool = row.prerequisiteTool.trim();
    if (!actionTool || !prerequisiteTool) return;
    const matchArg = row.matchArg.trim();
    const definition = matchArg
      ? { tool: prerequisiteTool, matchArg }
      : prerequisiteTool;
    (result[actionTool] ??= []).push(definition);
  });
  return result;
}

function canonicalConfig(config: ActionGuardrailsConfig): ActionGuardrailsConfig {
  const toolPrerequisites = Object.fromEntries(
    Object.entries(config.toolPrerequisites)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([tool, definitions]) => [
        tool,
        definitions.map((definition) =>
          typeof definition === "string"
            ? { tool: definition, matchArg: null }
            : { tool: definition.tool, matchArg: definition.matchArg ?? null }
        ),
      ]),
  );
  return {
    ...config,
    requiredSteps: [...config.requiredSteps],
    requiredStepScopes: [...config.requiredStepScopes]
      .map((scope) => ({
        scopeType: scope.scopeType,
        scopeId: scope.scopeId,
        steps: [...scope.steps],
      }))
      .sort((left, right) =>
        `${left.scopeType}:${left.scopeId}`.localeCompare(
          `${right.scopeType}:${right.scopeId}`,
        )
      ),
    toolPrerequisites,
  };
}
