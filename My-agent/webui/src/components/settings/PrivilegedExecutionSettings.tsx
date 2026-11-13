import { useCallback, useEffect, useState } from "react";
import {
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  ScrollText,
  ServerCog,
  ShieldCheck,
  UserRound,
} from "lucide-react";

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
import { Textarea } from "@/components/ui/textarea";
import {
  addPrivilegedAction,
  fetchPrivilegedAudit,
  fetchPrivilegedExecution,
  fetchPrivilegedPolicy,
  updatePrivilegedExecution,
} from "@/lib/api";
import type {
  PrivilegedActionDefinition,
  PrivilegedAuditRecord,
  PrivilegedExecutionPayload,
  PrivilegedPolicyActionDefinition,
  PrivilegedPolicyPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

interface PrivilegedForm {
  enabled: boolean;
  socket_path: string;
  approval_timeout: number;
  request_timeout: number;
}

interface ActionForm {
  name: string;
  label: string;
  description: string;
  category: string;
  impact: string;
  executable: string;
  arguments: string;
  parameters: string;
  timeout: number;
}

const EMPTY_ACTION_FORM: ActionForm = {
  name: "",
  label: "",
  description: "",
  category: "system",
  impact: "",
  executable: "",
  arguments: "[]",
  parameters: "{}",
  timeout: 60,
};

function collapseAuditRecords(records: PrivilegedAuditRecord[]): PrivilegedAuditRecord[] {
  const outcomeRank: Record<string, number> = {
    pending: 1,
    approved: 2,
    rejected: 3,
    timeout: 3,
    regular_user: 3,
    succeeded: 4,
    failed: 4,
  };
  const groups: PrivilegedAuditRecord[] = [];
  const byApprovalId = new Map<string, PrivilegedAuditRecord>();

  for (const record of records) {
    const approvalId = record.approval_id?.trim();
    const actorId = record.actor?.id || record.actor?.username || "";
    const fingerprint = `${record.session_key || ""}:${record.action_name || ""}:${actorId}`;
    let group = approvalId ? byApprovalId.get(approvalId) : undefined;

    if (!group && !approvalId) {
      group = groups.find((candidate) => {
        if (candidate.approval_id) return false;
        const candidateActorId = candidate.actor?.id || candidate.actor?.username || "";
        const candidateFingerprint =
          `${candidate.session_key || ""}:${candidate.action_name || ""}:${candidateActorId}`;
        const candidateTimestamp = candidate.completed_at || candidate.timestamp;
        const alreadyHasEvent = candidate.stages?.some((stage) => stage.event === record.event);
        return candidateFingerprint === fingerprint
          && Math.abs(candidateTimestamp - record.timestamp) <= 600
          && !alreadyHasEvent;
      });
    }

    if (!group) {
      group = {
        ...record,
        stages: [],
        requested_at: record.requested_at || record.timestamp,
        completed_at: record.completed_at || record.timestamp,
      };
      groups.push(group);
      if (approvalId) byApprovalId.set(approvalId, group);
    } else {
      if ((outcomeRank[record.outcome] || 0) > (outcomeRank[group.outcome] || 0)) {
        group.event = record.event;
        group.outcome = record.outcome;
        group.exit_code = record.exit_code;
        group.error = record.error;
      }
      group.timestamp = Math.max(group.timestamp, record.timestamp);
      group.requested_at = Math.min(
        group.requested_at || record.timestamp,
        record.requested_at || record.timestamp,
      );
      group.completed_at = Math.max(
        group.completed_at || record.timestamp,
        record.completed_at || record.timestamp,
      );
      group.action_name ||= record.action_name;
      group.parameters ||= record.parameters;
      group.reason ||= record.reason;
      group.actor ||= record.actor;
      group.session_key ||= record.session_key;
    }

    const incomingStages = record.stages?.length
      ? record.stages
      : [{
          id: record.id,
          timestamp: record.timestamp,
          event: record.event,
          outcome: record.outcome,
          exit_code: record.exit_code,
          error: record.error,
        }];
    const existingStageIds = new Set(group.stages?.map((stage) => stage.id));
    for (const stage of incomingStages) {
      if (!stage.id || !existingStageIds.has(stage.id)) {
        group.stages?.push(stage);
        if (stage.id) existingStageIds.add(stage.id);
      }
    }
  }

  for (const group of groups) {
    group.stages?.sort((left, right) => (left.timestamp || 0) - (right.timestamp || 0));
  }
  return groups.sort((left, right) => right.timestamp - left.timestamp);
}

export function PrivilegedExecutionSettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("sandbox.manage");
  const canAudit = hasPermission("privileged.audit");
  const canAddAction = hasPermission("privileged.approve");
  const [payload, setPayload] = useState<PrivilegedExecutionPayload | null>(null);
  const [policy, setPolicy] = useState<PrivilegedPolicyPayload | null>(null);
  const [form, setForm] = useState<PrivilegedForm | null>(null);
  const [audit, setAudit] = useState<PrivilegedAuditRecord[]>([]);
  const [selectedAction, setSelectedAction] = useState<PrivilegedActionDefinition | null>(null);
  const [selectedAudit, setSelectedAudit] = useState<PrivilegedAuditRecord | null>(null);
  const [actionEditorOpen, setActionEditorOpen] = useState(false);
  const [actionForm, setActionForm] = useState<ActionForm>({ ...EMPTY_ACTION_FORM });
  const [addingAction, setAddingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [next, auditPayload, policyPayload] = await Promise.all([
        fetchPrivilegedExecution(token),
        canAudit ? fetchPrivilegedAudit(token) : Promise.resolve({ records: [] }),
        canAddAction ? fetchPrivilegedPolicy(token) : Promise.resolve(null),
      ]);
      setPayload(next);
      setPolicy(policyPayload);
      setForm({
        enabled: next.enabled,
        socket_path: next.socket_path,
        approval_timeout: next.approval_timeout,
        request_timeout: next.request_timeout,
      });
      setAudit(collapseAuditRecords(auditPayload.records));
      setError(null);
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  }, [canAddAction, canAudit, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      const next = await updatePrivilegedExecution(token, form);
      setPayload(next);
      setForm({
        enabled: next.enabled,
        socket_path: next.socket_path,
        approval_timeout: next.approval_timeout,
        request_timeout: next.request_timeout,
      });
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const addAction = async () => {
    setAddingAction(true);
    setActionError(null);
    try {
      const args = JSON.parse(actionForm.arguments) as unknown;
      const parameters = JSON.parse(actionForm.parameters) as unknown;
      if (!Array.isArray(args) || !args.every((item) => typeof item === "string")) {
        throw new Error("固定参数必须是 JSON 字符串数组");
      }
      if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) {
        throw new Error("参数约束必须是 JSON 对象");
      }
      const next = await addPrivilegedAction(token, {
        name: actionForm.name.trim(),
        action: {
          label: actionForm.label.trim(),
          description: actionForm.description.trim(),
          category: actionForm.category.trim() || "system",
          impact: actionForm.impact.trim(),
          executable: actionForm.executable.trim(),
          arguments: args,
          parameters: parameters as Record<string, unknown>,
          timeout: actionForm.timeout,
        },
      });
      setPayload(next.execution);
      setPolicy((current) => ({
        policy: next.policy,
        policy_path: current?.policy_path,
        writable_by_agent: false,
        writable_by_admin: true,
      }));
      setActionForm({ ...EMPTY_ACTION_FORM });
      setActionEditorOpen(false);
    } catch (addError) {
      setActionError((addError as Error).message);
    } finally {
      setAddingAction(false);
    }
  };

  if (loading || !payload || !form) {
    return (
      <div className="flex h-36 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        正在检查特权执行器...
      </div>
    );
  }

  const dirty = JSON.stringify(form) !== JSON.stringify({
    enabled: payload.enabled,
    socket_path: payload.socket_path,
    approval_timeout: payload.approval_timeout,
    request_timeout: payload.request_timeout,
  });

  return (
    <section className="space-y-5 border-t border-border/45 pt-7">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold">
            <KeyRound className="h-3.5 w-3.5" />
            受控 root 执行
          </div>
          <p className="mt-3 max-w-[760px] text-[13px] leading-5 text-muted-foreground">
            Agent 始终保持受限账号身份。只有管理员批准的单次白名单动作，才会交给独立 root helper 执行。
          </p>
        </div>
        <Button type="button" variant="outline" size="icon" title="刷新状态" onClick={() => void load()}>
          <RefreshCw className="h-4 w-4" />
        </Button>
      </div>

      {error ? <Notice icon={CircleAlert}>{error}</Notice> : null}
      {payload.error ? <Notice icon={CircleAlert}>{payload.error}</Notice> : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <StatusTile
          icon={ServerCog}
          label="root helper"
          value={payload.available ? "已连接" : "未连接"}
          caption={payload.helper.peer_mode ? `进程校验：${payload.helper.peer_mode}` : payload.socket_path}
          healthy={payload.available}
        />
        <StatusTile
          icon={ShieldCheck}
          label="可批准动作"
          value={String(payload.actions.length)}
          caption="全部来自 root 所有的策略文件"
          healthy={payload.available && payload.actions.length > 0}
        />
        <StatusTile
          icon={Clock3}
          label="审批有效范围"
          value="单次"
          caption={`${payload.approval_timeout} 秒后自动拒绝`}
          healthy={payload.enabled}
        />
      </div>

      <div className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
        <SettingRow title="启用受控特权执行" description="关闭后，所有 privileged_action 请求都会失败。">
          <Toggle
            checked={form.enabled}
            disabled={!canManage}
            onChange={(enabled) => setForm({ ...form, enabled })}
            label="启用受控特权执行"
          />
        </SettingRow>
        <SettingRow title="Helper Socket" description="NimbusOps 主进程与独立 root helper 之间的 Unix Socket。">
          <Input
            value={form.socket_path}
            disabled={!canManage}
            className="h-10 w-[320px] max-w-[48vw] font-mono text-[12px]"
            onChange={(event) => setForm({ ...form, socket_path: event.target.value })}
          />
        </SettingRow>
        <SettingRow title="管理员审批超时" description="管理员没有在规定时间内处理时，默认拒绝并结束当前回合。">
          <NumberInput
            value={form.approval_timeout}
            disabled={!canManage}
            onChange={(approval_timeout) => setForm({ ...form, approval_timeout })}
          />
        </SettingRow>
        <SettingRow title="Helper 请求超时" description="root 动作达到超时时间后会被终止。">
          <NumberInput
            value={form.request_timeout}
            disabled={!canManage}
            onChange={(request_timeout) => setForm({ ...form, request_timeout })}
          />
        </SettingRow>
      </div>

      {canManage ? (
        <div className="flex justify-end">
          <Button type="button" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}
            保存特权执行设置
          </Button>
        </div>
      ) : null}

      <div>
        <div className="mb-3 flex items-end justify-between border-b border-border/45 pb-3">
          <div>
            <div className="text-[13px] font-semibold">已登记特权动作</div>
            <div className="mt-1 text-[11.5px] text-muted-foreground">点击动作可查看执行程序、固定参数和参数约束。</div>
          </div>
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-muted px-2.5 py-1 text-[11px] text-muted-foreground">{payload.actions.length}</span>
            {canAddAction ? (
              <Button
                type="button"
                size="sm"
                className="h-8 rounded-full px-3 text-[12px]"
                disabled={policy?.writable_by_admin !== true}
                title={policy?.writable_by_admin === true ? "添加特权动作" : "请先更新 root helper"}
                onClick={() => {
                  setActionError(null);
                  setActionForm({ ...EMPTY_ACTION_FORM });
                  setActionEditorOpen(true);
                }}
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                添加动作
              </Button>
            ) : null}
          </div>
        </div>
        {canAddAction && policy?.writable_by_admin !== true ? (
          <div className="mb-3 rounded-[10px] border border-amber-500/25 bg-amber-500/8 px-3 py-2.5 text-[11.5px] text-amber-800 dark:text-amber-200">
            当前 helper 版本不支持从管理员页面写入策略。运行 <span className="font-mono">./nimbusops-helper restart</span> 更新后即可添加。
          </div>
        ) : null}
        {payload.actions.length ? (
          <div className="grid gap-3 md:grid-cols-2">
            {payload.actions.map((action) => (
              <button
                key={action.name}
                type="button"
                aria-label={`查看特权动作 ${action.name}`}
                className="flex min-w-0 items-start gap-3 rounded-[12px] border border-border/45 px-4 py-3 text-left transition-colors hover:bg-muted/35"
                onClick={() => setSelectedAction(action)}
              >
                <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-muted/65">
                  <KeyRound className="h-4 w-4 text-muted-foreground" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-[13px]">{action.label}</strong>
                    <span className="font-mono text-[10.5px] text-muted-foreground">{action.name}</span>
                  </div>
                  <p className="mt-1 text-[11.5px] leading-5 text-muted-foreground">{action.description || action.impact}</p>
                </div>
                <ChevronRight className="mt-2.5 h-4 w-4 shrink-0 text-muted-foreground/60" />
              </button>
            ))}
          </div>
        ) : (
          <div className="py-10 text-center text-[12px] text-muted-foreground">root helper 未连接，或策略中还没有登记动作。</div>
        )}
      </div>

      {canAudit ? (
        <div>
          <div className="mb-3 flex items-center gap-2 border-b border-border/45 pb-3">
            <ScrollText className="h-4 w-4 text-muted-foreground" />
            <div>
              <div className="text-[13px] font-semibold">最近特权审计</div>
              <div className="mt-1 text-[11.5px] text-muted-foreground">每次特权请求只显示一条最终记录，点击可查看完整阶段与执行参数。</div>
            </div>
          </div>
          {audit.length ? (
            <div className="divide-y divide-border/40">
              {audit.slice(0, 20).map((record) => (
                <button
                  key={record.id}
                  type="button"
                  aria-label={`查看特权审计 ${record.action_name || record.event}`}
                  className="flex w-full min-w-0 items-center gap-3 py-3 text-left text-[12px] transition-colors hover:bg-muted/30"
                  onClick={() => setSelectedAudit(record)}
                >
                  <span className={cn(
                    "h-2 w-2 shrink-0 rounded-full",
                    record.outcome === "succeeded" || record.outcome === "approved"
                      ? "bg-emerald-500"
                      : record.outcome === "pending"
                        ? "bg-amber-500"
                        : "bg-red-500",
                  )} />
                  <span className="w-24 shrink-0 text-muted-foreground">{formatTime(record.timestamp)}</span>
                  <span className="min-w-0 flex-1 truncate font-mono">{record.action_name || record.event}</span>
                  <span className="hidden min-w-0 items-center gap-1.5 text-muted-foreground sm:flex">
                    <UserRound className="h-3.5 w-3.5 shrink-0" />
                    <span className="max-w-32 truncate">{actorLabel(record)}</span>
                  </span>
                  <span className="shrink-0 text-muted-foreground">{outcomeLabel(record.outcome)}</span>
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground/60" />
                </button>
              ))}
            </div>
          ) : (
            <div className="py-8 text-center text-[12px] text-muted-foreground">还没有特权操作记录。</div>
          )}
        </div>
      ) : null}

      <ActionDetailsDialog
        action={selectedAction}
        policyAction={selectedAction ? policy?.policy.actions[selectedAction.name] : undefined}
        onClose={() => setSelectedAction(null)}
      />
      <AuditDetailsDialog record={selectedAudit} onClose={() => setSelectedAudit(null)} />
      <ActionEditorDialog
        open={actionEditorOpen}
        form={actionForm}
        saving={addingAction}
        error={actionError}
        onChange={setActionForm}
        onClose={() => setActionEditorOpen(false)}
        onSave={() => void addAction()}
      />
    </section>
  );
}

function ActionDetailsDialog({
  action,
  policyAction,
  onClose,
}: {
  action: PrivilegedActionDefinition | null;
  policyAction?: PrivilegedPolicyActionDefinition;
  onClose: () => void;
}) {
  return (
    <Dialog open={action !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-2xl overflow-x-hidden overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{action?.label || "特权动作详情"}</DialogTitle>
          <DialogDescription>该动作由 root helper 按策略中的固定程序和参数模板执行。</DialogDescription>
        </DialogHeader>
        {action ? (
          <div className="space-y-3">
            <DetailRow label="动作标识" value={action.name} mono />
            <DetailRow label="分类" value={action.category || "system"} />
            <DetailRow label="影响" value={action.impact || "需要 root 权限"} />
            <DetailRow label="超时" value={`${action.timeout} 秒`} />
            <DetailRow label="说明" value={action.description || "未填写"} />
            {policyAction ? (
              <>
                <DetailRow label="执行程序" value={policyAction.executable} mono />
                <JsonBlock label="固定参数模板" value={policyAction.arguments} />
              </>
            ) : (
              <div className="rounded-[12px] border border-border/50 bg-muted/30 px-3 py-3 text-[12px] text-muted-foreground">
                只有系统管理员可以查看 root 策略中的执行程序和固定参数。
              </div>
            )}
            <JsonBlock label="参数约束" value={policyAction?.parameters ?? action.parameters ?? {}} />
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AuditDetailsDialog({
  record,
  onClose,
}: {
  record: PrivilegedAuditRecord | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={record !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-2xl overflow-x-hidden overflow-y-auto">
        <DialogHeader>
          <DialogTitle>特权审计详情</DialogTitle>
          <DialogDescription>一次请求的申请、审批和执行阶段已合并到这条记录中。</DialogDescription>
        </DialogHeader>
        {record ? (
          <div className="space-y-3">
            <DetailRow label="动作标识" value={record.action_name || record.event} mono />
            <DetailRow label="最终结果" value={outcomeLabel(record.outcome)} />
            <DetailRow label="申请账号" value={actorLabel(record)} />
            <DetailRow label="申请原因" value={record.reason || "未提供"} />
            <DetailRow label="会话" value={record.session_key || "未知"} mono />
            {record.exit_code !== undefined && record.exit_code !== null ? (
              <DetailRow label="退出码" value={String(record.exit_code)} mono />
            ) : null}
            {record.error ? <DetailRow label="错误" value={record.error} /> : null}
            <JsonBlock label="执行参数" value={record.parameters ?? {}} />
            {record.stages?.length ? (
              <div>
                <div className="mb-2 text-[12px] font-medium text-muted-foreground">处理阶段</div>
                <div className="overflow-hidden rounded-[12px] border border-border/50">
                  {record.stages.map((stage, index) => (
                    <div
                      key={stage.id || `${stage.event}-${stage.timestamp}-${index}`}
                      className="flex items-center gap-3 border-b border-border/45 px-3 py-2.5 text-[12px] last:border-b-0"
                    >
                      <span className="w-20 shrink-0 text-muted-foreground">
                        {stage.timestamp ? formatTime(stage.timestamp) : "--"}
                      </span>
                      <span className="min-w-0 flex-1">{eventLabel(stage.event)}</span>
                      <span className="shrink-0 text-muted-foreground">
                        {outcomeLabel(stage.outcome || "")}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ActionEditorDialog({
  open,
  form,
  saving,
  error,
  onChange,
  onClose,
  onSave,
}: {
  open: boolean;
  form: ActionForm;
  saving: boolean;
  error: string | null;
  onChange: (form: ActionForm) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const requiredMissing = !form.name.trim()
    || !form.label.trim()
    || !form.executable.trim()
    || !form.impact.trim();
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-3xl overflow-x-hidden overflow-y-auto">
        <DialogHeader>
          <DialogTitle>添加特权动作</DialogTitle>
          <DialogDescription>
            仅系统管理员可以添加。保存后由 root helper 校验并写入当前策略，动作会立即对后续请求生效。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="动作标识">
            <Input
              aria-label="动作标识"
              value={form.name}
              placeholder="flush_dns_cache"
              onChange={(event) => onChange({ ...form, name: event.target.value })}
            />
          </Field>
          <Field label="显示名称">
            <Input
              aria-label="显示名称"
              value={form.label}
              placeholder="刷新 DNS 缓存"
              onChange={(event) => onChange({ ...form, label: event.target.value })}
            />
          </Field>
          <Field label="分类">
            <Input
              aria-label="分类"
              value={form.category}
              placeholder="system"
              onChange={(event) => onChange({ ...form, category: event.target.value })}
            />
          </Field>
          <Field label="超时时间（秒）">
            <Input
              aria-label="超时时间（秒）"
              type="number"
              min={1}
              max={1800}
              value={form.timeout}
              onChange={(event) => onChange({ ...form, timeout: Number(event.target.value) })}
            />
          </Field>
          <Field label="执行程序" wide>
            <Input
              aria-label="执行程序"
              value={form.executable}
              placeholder="/usr/bin/killall"
              className="font-mono text-[12px]"
              onChange={(event) => onChange({ ...form, executable: event.target.value })}
            />
          </Field>
          <Field label="动作说明" wide>
            <Input
              aria-label="动作说明"
              value={form.description}
              placeholder="向 macOS mDNSResponder 发送 HUP 信号"
              onChange={(event) => onChange({ ...form, description: event.target.value })}
            />
          </Field>
          <Field label="执行影响" wide>
            <Input
              aria-label="执行影响"
              value={form.impact}
              placeholder="本机 DNS 缓存会被刷新"
              onChange={(event) => onChange({ ...form, impact: event.target.value })}
            />
          </Field>
          <Field label="固定参数模板（JSON 字符串数组）" wide>
            <Textarea
              aria-label="固定参数模板"
              value={form.arguments}
              className="min-h-24 font-mono text-[12px]"
              placeholder={'["-HUP", "mDNSResponder"]'}
              onChange={(event) => onChange({ ...form, arguments: event.target.value })}
            />
          </Field>
          <Field label="参数约束（JSON 对象）" wide>
            <Textarea
              aria-label="参数约束"
              value={form.parameters}
              className="min-h-28 font-mono text-[12px]"
              placeholder={'{"service":{"type":"string","enum":["nginx.service"],"required":true}}'}
              onChange={(event) => onChange({ ...form, parameters: event.target.value })}
            />
          </Field>
        </div>
        <div className="rounded-[12px] border border-amber-500/25 bg-amber-500/8 px-3 py-3 text-[11.5px] leading-5 text-amber-800 dark:text-amber-200">
          固定参数中的占位符使用 {"{parameter_name}"}，且必须在参数约束中登记。helper 会拒绝相对路径、无效参数和未登记占位符。
        </div>
        {error ? <Notice icon={CircleAlert}>{error}</Notice> : null}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={saving} onClick={onClose}>取消</Button>
          <Button type="button" disabled={saving || requiredMissing} onClick={onSave}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
            添加并立即生效
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  wide = false,
  children,
}: {
  label: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={wide ? "sm:col-span-2" : ""}>
      <span className="mb-2 block text-[12px] font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function DetailRow({ label, value, mono = false }: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-[12px] border border-border/50 bg-muted/25 px-3 py-2.5">
      <span className="w-20 shrink-0 text-[12px] text-muted-foreground">{label}</span>
      <strong className={cn("min-w-0 flex-1 break-all text-right text-[12.5px]", mono && "font-mono")}>
        {value}
      </strong>
    </div>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="mb-2 text-[12px] font-medium text-muted-foreground">{label}</div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-[12px] border border-border/50 bg-muted/30 p-3 font-mono text-[12px] leading-5">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function SettingRow({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-[72px] flex-col gap-3 border-b border-border/45 px-4 py-4 last:border-b-0 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold">{title}</div>
        <div className="mt-1 max-w-[560px] text-[11.5px] leading-5 text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function StatusTile({ icon: Icon, label, value, caption, healthy }: {
  icon: typeof KeyRound;
  label: string;
  value: string;
  caption: string;
  healthy: boolean;
}) {
  return (
    <div className="rounded-[12px] border border-border/45 bg-card px-4 py-4">
      <div className="flex items-center gap-2 text-[11px] font-semibold text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <span className="text-[17px] font-semibold">{value}</span>
        <span className={cn("h-1.5 w-1.5 rounded-full", healthy ? "bg-emerald-500" : "bg-amber-500")} />
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">{caption}</div>
    </div>
  );
}

function Toggle({ checked, disabled, onChange, label }: {
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-label={label}
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-[22px] w-[38px] rounded-full p-[2px] transition-colors",
        checked ? "bg-[#2997FF]" : "bg-muted",
        disabled && "cursor-not-allowed opacity-55",
      )}
    >
      <span className={cn(
        "block h-[18px] w-[18px] rounded-full bg-background shadow-sm transition-transform",
        checked && "translate-x-4",
      )} />
    </button>
  );
}

function NumberInput({ value, disabled, onChange }: {
  value: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <Input
        type="number"
        min={1}
        max={3600}
        value={value}
        disabled={disabled}
        className="h-10 w-28"
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <span className="text-[12px] text-muted-foreground">秒</span>
    </div>
  );
}

function Notice({ icon: Icon, children }: { icon: typeof CircleAlert; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded-[12px] border border-amber-500/25 bg-amber-500/8 px-3 py-3 text-[12px] text-amber-800 dark:text-amber-200">
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{children}</span>
    </div>
  );
}

function formatTime(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function outcomeLabel(outcome: string): string {
  const labels: Record<string, string> = {
    pending: "等待审批",
    approved: "已批准",
    rejected: "已拒绝",
    timeout: "审批超时",
    succeeded: "执行成功",
    failed: "执行失败",
    regular_user: "普通用户无权申请",
  };
  return labels[outcome] || outcome;
}

function eventLabel(event?: string): string {
  const labels: Record<string, string> = {
    requested: "提交申请",
    approval: "管理员审批",
    execution: "helper 执行",
    denied: "权限拒绝",
  };
  return labels[event || ""] || event || "未知阶段";
}

function actorLabel(record: PrivilegedAuditRecord): string {
  const actor = record.actor;
  if (!actor) return "未知账号";
  if (actor.display_name && actor.display_name !== actor.username) {
    return `${actor.display_name}（${actor.username}）`;
  }
  return actor.username || actor.display_name || "未知账号";
}
