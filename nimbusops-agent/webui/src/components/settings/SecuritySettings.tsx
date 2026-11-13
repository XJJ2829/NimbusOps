import { useEffect, useMemo, useState } from "react";
import {
  Braces,
  Check,
  CircleAlert,
  FileCode2,
  Loader2,
  LockKeyhole,
  Pencil,
  Plus,
  ShieldCheck,
  Trash2,
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
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { StyledSelect } from "@/components/ui/styled-select";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  createAgentSpecPredicate,
  createAgentSpecRule,
  deleteAgentSpecPredicate,
  deleteAgentSpecRule,
  fetchAgentSpecPolicies,
  setAgentSpecEnabled,
  setAgentSpecRuleEnabled,
  updateAgentSpecPredicate,
  updateAgentSpecRule,
} from "@/lib/api";
import type {
  AgentSpecDecision,
  AgentSpecPayload,
  AgentSpecPredicate,
  AgentSpecRule,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const ENFORCEMENT_LABELS: Record<string, string> = {
  allow: "允许执行",
  stop: "直接阻止",
  user_inspection: "需要人工确认",
  invoke_action: "替换动作",
  skip: "跳过动作",
  llm_self_reflect: "要求重新规划",
};

const PREDICATE_DESCRIPTIONS: Record<string, string> = {
  is_destructive: "检测工具参数中的破坏性文件系统操作和高权限 Shell 命令。",
  is_destructive_file_edit: "检测使用空内容覆盖已有文件，或使用空文本替换大量文件内容的操作。",
};

interface RuleForm {
  name: string;
  trigger: string;
  predicate: string;
  enforcement: Extract<AgentSpecDecision, "user_inspection" | "stop">;
}

interface PredicateForm {
  name: string;
  description: string;
  implementation: string;
}

const EMPTY_RULE_FORM: RuleForm = {
  name: "",
  trigger: "",
  predicate: "",
  enforcement: "user_inspection",
};

const EMPTY_PREDICATE_FORM: PredicateForm = {
  name: "",
  description: "",
  implementation: "return False",
};

function buildRuleText(form: RuleForm): string {
  return `rule @${form.name.trim()}
trigger
    ${form.trigger.trim()}
check
    ${form.predicate}
enforce
    ${form.enforcement}
end
`;
}

function buildPredicateSource(form: PredicateForm): string {
  const implementation = form.implementation
    .trim()
    .split("\n")
    .map((line) => `    ${line}`)
    .join("\n");
  return `def ${form.name.trim()}(user_input, tool_input, history):
    ${JSON.stringify(form.description.trim())}
${implementation}
`;
}

export function SecuritySettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("ops_shield.manage");
  const [activeSection, setActiveSection] = useState<"policies" | "predicates">("policies");
  const [payload, setPayload] = useState<AgentSpecPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [ruleEditorOpen, setRuleEditorOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<AgentSpecRule | null>(null);
  const [ruleText, setRuleText] = useState("");
  const [ruleForm, setRuleForm] = useState<RuleForm>(EMPTY_RULE_FORM);
  const [deleteRule, setDeleteRule] = useState<AgentSpecRule | null>(null);

  const [selectedPredicate, setSelectedPredicate] = useState<AgentSpecPredicate | null>(null);
  const [predicateEditorOpen, setPredicateEditorOpen] = useState(false);
  const [editingPredicate, setEditingPredicate] = useState<AgentSpecPredicate | null>(null);
  const [predicateForm, setPredicateForm] = useState<PredicateForm>(EMPTY_PREDICATE_FORM);
  const [predicateEditorError, setPredicateEditorError] = useState<string | null>(null);
  const [deletePredicate, setDeletePredicate] = useState<AgentSpecPredicate | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setPayload(await fetchAgentSpecPolicies(token));
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // The access token identifies the active local gateway session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const runAction = async (key: string, action: () => Promise<AgentSpecPayload>) => {
    setBusyKey(key);
    setError(null);
    try {
      setPayload(await action());
      return true;
    } catch (actionError) {
      setError((actionError as Error).message);
      return false;
    } finally {
      setBusyKey(null);
    }
  };

  const rules = payload?.rules ?? [];
  const predicates = useMemo(() => buildPredicateList(payload), [payload]);
  const enabledCount = rules.filter((rule) => rule.enabled).length;
  const rulePreview = buildRuleText(ruleForm);
  const canCreateRule = Boolean(
    /^[A-Za-z_][A-Za-z0-9_]*$/.test(ruleForm.name.trim())
      && ruleForm.trigger.trim()
      && ruleForm.predicate,
  );

  const openCreateRule = () => {
    setEditingRule(null);
    setRuleForm(EMPTY_RULE_FORM);
    setRuleText("");
    setRuleEditorOpen(true);
  };

  const openRule = (rule: AgentSpecRule) => {
    setEditingRule(rule);
    setRuleText(rule.raw);
    setRuleEditorOpen(true);
  };

  const saveRule = async () => {
    const text = editingRule ? ruleText : rulePreview;
    const ok = await runAction(
      editingRule ? `save:${editingRule.id}` : "create-rule",
      () => editingRule
        ? updateAgentSpecRule(token, editingRule.id, text)
        : createAgentSpecRule(token, text),
    );
    if (ok) setRuleEditorOpen(false);
  };

  const confirmDeleteRule = async () => {
    if (!deleteRule) return;
    const ok = await runAction(
      `delete-rule:${deleteRule.id}`,
      () => deleteAgentSpecRule(token, deleteRule.id),
    );
    if (ok) setDeleteRule(null);
  };

  const openCreatePredicate = () => {
    setEditingPredicate(null);
    setPredicateForm(EMPTY_PREDICATE_FORM);
    setPredicateEditorError(null);
    setPredicateEditorOpen(true);
  };

  const openEditPredicate = (predicate: AgentSpecPredicate) => {
    setSelectedPredicate(null);
    setEditingPredicate(predicate);
    setPredicateForm({
      name: predicate.name,
      description: predicate.description === "No description provided." ? "" : predicate.description,
      implementation: extractPredicateImplementation(predicate.source) || "return False",
    });
    setPredicateEditorError(null);
    setPredicateEditorOpen(true);
  };

  const savePredicate = async () => {
    const key = editingPredicate ? `save-predicate:${editingPredicate.name}` : "create-predicate";
    const source = buildPredicateSource(predicateForm);
    setBusyKey(key);
    setPredicateEditorError(null);
    try {
      const nextPayload = await (editingPredicate
        ? updateAgentSpecPredicate(token, editingPredicate.name, source)
        : createAgentSpecPredicate(token, source));
      setPayload(nextPayload);
      setPredicateEditorOpen(false);
    } catch (saveError) {
      const message = saveError instanceof ApiError && saveError.status === 405
        ? "当前后端尚未加载新建 Predicate 接口。请重启 NimbusOps 服务后再保存。"
        : (saveError as Error).message;
      setPredicateEditorError(message);
    } finally {
      setBusyKey(null);
    }
  };

  const confirmDeletePredicate = async () => {
    if (!deletePredicate) return;
    const ok = await runAction(
      `delete-predicate:${deletePredicate.name}`,
      () => deleteAgentSpecPredicate(token, deletePredicate.name),
    );
    if (ok) {
      setDeletePredicate(null);
      setSelectedPredicate(null);
    }
  };

  return (
    <div className="space-y-7">
      <section className="border-b border-border/45 pb-4">
        <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold text-foreground">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
          OpsShield 策略
        </div>
        <p className="mt-3 max-w-[720px] text-[13px] leading-5 text-muted-foreground">
          OpsShield 在模型生成动作后、工具执行前评估规则。Trigger 必须与运行时工具名称完全一致。
        </p>
      </section>

      <section className="overflow-hidden rounded-[16px] border border-border/45 bg-card/72 shadow-[0_10px_30px_rgba(15,23,42,0.045)]">
        <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-[12px] bg-emerald-500/10 text-emerald-700 dark:text-emerald-300">
              <LockKeyhole className="h-4.5 w-4.5" aria-hidden />
            </span>
            <div>
              <h2 className="text-[14px] font-semibold text-foreground">OpsShield Enforcement</h2>
              <p className="mt-0.5 text-[12px] text-muted-foreground">
                {enabledCount} 条策略启用，共 {rules.length} 条
              </p>
            </div>
          </div>
          <SecurityToggle
            checked={payload?.enabled ?? false}
            ariaLabel="OpsShield Enforcement"
            disabled={!canManage || !payload || busyKey === "engine"}
            onChange={() => {
              if (!payload) return;
              void runAction("engine", () => setAgentSpecEnabled(token, !payload.enabled));
            }}
          />
        </div>
      </section>

      {error ? (
        <div className="flex items-start gap-2 rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-3 text-[13px] text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
      ) : null}

      {payload?.errors.map((item) => (
        <div
          key={item.file}
          className="rounded-[12px] border border-amber-500/25 bg-amber-500/8 px-3 py-3 text-[12.5px] text-amber-800 dark:text-amber-200"
        >
          <span className="font-semibold">{item.file}</span>
          <span className="ml-2">{item.message}</span>
        </div>
      ))}

      <section>
        <div
          role="tablist"
          aria-label="OpsShield 策略配置"
          className="inline-flex rounded-[10px] bg-muted/70 p-1"
        >
          <SectionTab
            active={activeSection === "policies"}
            label="已有策略"
            onClick={() => setActiveSection("policies")}
          />
          <SectionTab
            active={activeSection === "predicates"}
            label="Predicate 函数"
            onClick={() => setActiveSection("predicates")}
          />
        </div>

        {activeSection === "policies" ? (
          <CatalogSection
            title="已有策略"
            description="规则通过 Trigger 选择动作，再调用 Predicate 判断是否执行 Enforcement。"
            action={canManage ? (
              <Button
                type="button"
                size="sm"
                onClick={openCreateRule}
                className="h-8 rounded-full px-3 text-[12px] font-semibold"
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                新建策略
              </Button>
            ) : <span />}
          >
            {loading ? (
              <LoadingState label="正在加载安全策略..." />
            ) : rules.length ? (
              <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
                {rules.map((rule) => (
                  <PolicyRow
                    key={`${rule.source}:${rule.id}`}
                    rule={rule}
                    busy={busyKey?.endsWith(`:${rule.id}`) ?? false}
                    readonly={!canManage}
                    onOpen={() => openRule(rule)}
                    onToggle={() => {
                      void runAction(
                        `toggle:${rule.id}`,
                        () => setAgentSpecRuleEnabled(token, rule.id, !rule.enabled),
                      );
                    }}
                    onDelete={canManage && rule.source === "custom" ? () => setDeleteRule(rule) : undefined}
                  />
                ))}
              </div>
            ) : (
              <EmptyState label="还没有 OpsShield 策略。" />
            )}
          </CatalogSection>
        ) : (
          <CatalogSection
            title="Predicate 函数"
            description="这些 Python 函数来自 predicate_table，返回 True 时规则才会执行 Enforcement。"
            action={(
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
                  {predicates.length}
                </span>
                {canManage ? (
                  <Button
                    type="button"
                    size="sm"
                    onClick={openCreatePredicate}
                    className="h-8 rounded-full px-3 text-[12px] font-semibold"
                  >
                    <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    新建函数
                  </Button>
                ) : null}
              </div>
            )}
          >
            {loading ? (
              <LoadingState label="正在加载 Predicate..." />
            ) : predicates.length ? (
              <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
                {predicates.map((predicate) => (
                  <PredicateRow
                    key={predicate.name}
                    predicate={predicate}
                    onOpen={() => setSelectedPredicate(predicate)}
                    onDelete={
                      canManage && predicate.source_kind === "custom"
                        ? () => setDeletePredicate(predicate)
                        : undefined
                    }
                  />
                ))}
              </div>
            ) : (
              <EmptyState label="当前没有注册 Predicate 函数。" />
            )}
          </CatalogSection>
        )}
      </section>

      <RuleEditor
        open={ruleEditorOpen}
        editingRule={editingRule}
        ruleText={ruleText}
        ruleForm={ruleForm}
        rulePreview={rulePreview}
        predicates={predicates}
        busy={busyKey === "create-rule" || busyKey?.startsWith("save:") === true}
        canSave={editingRule ? Boolean(ruleText.trim()) : canCreateRule}
        onOpenChange={setRuleEditorOpen}
        onRuleTextChange={setRuleText}
        onRuleFormChange={setRuleForm}
        onSave={() => void saveRule()}
      />

      <PredicateEditor
        open={predicateEditorOpen}
        editingPredicate={editingPredicate}
        form={predicateForm}
        error={predicateEditorError}
        busy={
          busyKey === "create-predicate"
          || busyKey === `save-predicate:${editingPredicate?.name}`
        }
        onOpenChange={(open) => {
          setPredicateEditorOpen(open);
          if (!open) setPredicateEditorError(null);
        }}
        onFormChange={(value) => {
          setPredicateForm(value);
          setPredicateEditorError(null);
        }}
        onSave={() => void savePredicate()}
      />

      <PredicateDetail
        predicate={selectedPredicate}
        canManage={canManage}
        onOpenChange={(open) => {
          if (!open) setSelectedPredicate(null);
        }}
        onEdit={openEditPredicate}
        onDelete={setDeletePredicate}
      />

      <DeleteDialog
        open={deleteRule !== null}
        title="删除策略"
        description={`确定删除自定义策略“${deleteRule?.id ?? ""}”吗？对应的 .rule 文件也会被删除。`}
        busy={busyKey === `delete-rule:${deleteRule?.id}`}
        onOpenChange={(open) => !open && setDeleteRule(null)}
        onConfirm={() => void confirmDeleteRule()}
      />

      <DeleteDialog
        open={deletePredicate !== null}
        title="删除 Predicate"
        description={`确定删除自定义 Predicate“${deletePredicate?.name ?? ""}”吗？引用它的规则将无法加载。`}
        busy={busyKey === `delete-predicate:${deletePredicate?.name}`}
        onOpenChange={(open) => !open && setDeletePredicate(null)}
        onConfirm={() => void confirmDeletePredicate()}
      />
    </div>
  );
}

function RuleEditor({
  open,
  editingRule,
  ruleText,
  ruleForm,
  rulePreview,
  predicates,
  busy,
  canSave,
  onOpenChange,
  onRuleTextChange,
  onRuleFormChange,
  onSave,
}: {
  open: boolean;
  editingRule: AgentSpecRule | null;
  ruleText: string;
  ruleForm: RuleForm;
  rulePreview: string;
  predicates: AgentSpecPredicate[];
  busy: boolean;
  canSave: boolean;
  onOpenChange: (open: boolean) => void;
  onRuleTextChange: (value: string) => void;
  onRuleFormChange: (value: RuleForm) => void;
  onSave: () => void;
}) {
  const readonly = editingRule?.source === "builtin";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100vh-2rem)] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editingRule ? editingRule.id : "新建 OpsShield 策略"}</DialogTitle>
          <DialogDescription>
            {editingRule
              ? "查看或修改 OpsShield DSL。Trigger 区分大小写。"
              : "填写规则要素，系统会生成并保存 OpsShield .rule 文件。"}
          </DialogDescription>
        </DialogHeader>

        {editingRule ? (
          <Textarea
            value={ruleText}
            onChange={(event) => onRuleTextChange(event.target.value)}
            readOnly={readonly}
            spellCheck={false}
            className="min-h-[340px] resize-y font-mono text-[13px] leading-6"
          />
        ) : (
          <div className="space-y-5">
            <FormField label="策略名称" hint="只能使用字母、数字和下划线">
              <div className="relative">
                <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 font-mono text-sm text-muted-foreground">
                  @
                </span>
                <Input
                  aria-label="策略名称"
                  value={ruleForm.name}
                  placeholder="your_policy_name"
                  onChange={(event) => onRuleFormChange({ ...ruleForm, name: event.target.value })}
                  className="pl-7 font-mono"
                />
              </div>
            </FormField>
            <FormField label="Trigger" hint="必须精确填写 ToolRegistry 中的运行时工具名">
              <Input
                aria-label="Trigger"
                value={ruleForm.trigger}
                placeholder="runtime_tool_name"
                onChange={(event) => onRuleFormChange({ ...ruleForm, trigger: event.target.value })}
                className="font-mono"
              />
            </FormField>
            <FormField label="Predicate" hint="选择一个已经注册的判断函数">
              <StyledSelect
                aria-label="Predicate"
                value={ruleForm.predicate}
                onChange={(event) => onRuleFormChange({ ...ruleForm, predicate: event.target.value })}
              >
                <option value="">请选择 Predicate</option>
                {predicates.map((predicate) => (
                  <option key={predicate.name} value={predicate.name}>
                    {predicate.name}
                  </option>
                ))}
              </StyledSelect>
            </FormField>
            <FormField label="Enforcement" hint="Predicate 返回 True 后采取的动作">
              <StyledSelect
                aria-label="Enforcement"
                value={ruleForm.enforcement}
                onChange={(event) => onRuleFormChange({
                  ...ruleForm,
                  enforcement: event.target.value as RuleForm["enforcement"],
                })}
              >
                <option value="user_inspection">user_inspection（请求人工确认）</option>
                <option value="stop">stop（直接退出并阻止执行）</option>
              </StyledSelect>
            </FormField>
            <div>
              <p className="mb-2 text-[12px] font-semibold text-foreground/80">生成的 OpsShield 规则</p>
              <pre className="overflow-x-auto rounded-[10px] border border-border/55 bg-muted/45 p-4 font-mono text-[12px] leading-5 text-foreground">
                <code>{rulePreview}</code>
              </pre>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          {!readonly ? (
            <Button type="button" disabled={!canSave || busy} onClick={onSave}>
              {busy ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <Check className="mr-2 h-4 w-4" aria-hidden />
              )}
              保存策略
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PredicateEditor({
  open,
  editingPredicate,
  form,
  error,
  busy,
  onOpenChange,
  onFormChange,
  onSave,
}: {
  open: boolean;
  editingPredicate: AgentSpecPredicate | null;
  form: PredicateForm;
  error: string | null;
  busy: boolean;
  onOpenChange: (open: boolean) => void;
  onFormChange: (value: PredicateForm) => void;
  onSave: () => void;
}) {
  const validName = /^[A-Za-z_][A-Za-z0-9_]*$/.test(form.name.trim());
  const canSave = validName && Boolean(form.description.trim() && form.implementation.trim());
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-3xl overflow-x-hidden overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editingPredicate ? `编辑 ${editingPredicate.name}` : "新建 Predicate 函数"}</DialogTitle>
          <DialogDescription>
            函数必须接收 user_input、tool_input、history 三个参数，并返回布尔值。
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-[10px] border border-border/50 bg-muted/35 px-3 py-2.5 text-[12px] leading-5 text-muted-foreground">
          函数作用会写入 Python docstring，并作为 Predicate 列表中的说明文字。
        </div>
        {error ? (
          <div
            role="alert"
            className="flex min-w-0 items-start gap-2 rounded-[10px] border border-destructive/25 bg-destructive/5 px-3 py-2.5 text-[12.5px] leading-5 text-destructive"
          >
            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span className="min-w-0 break-words">{error}</span>
          </div>
        ) : null}
        <div className="min-w-0 space-y-5">
          <FormField label="函数名称" hint="只能使用字母、数字和下划线">
            <Input
              aria-label="函数名称"
              value={form.name}
              disabled={Boolean(editingPredicate)}
              placeholder="your_predicate"
              onChange={(event) => onFormChange({ ...form, name: event.target.value })}
              className="font-mono"
            />
          </FormField>
          <FormField label="函数作用" hint="用于说明这个判断在什么情况下返回 True">
            <Input
              aria-label="函数作用"
              value={form.description}
              placeholder="例如：检测工具参数中的高风险操作"
              onChange={(event) => onFormChange({ ...form, description: event.target.value })}
            />
          </FormField>
          <FormField label="函数实现" hint="可直接使用 user_input、tool_input、history">
            <Textarea
              aria-label="函数实现"
              value={form.implementation}
              onChange={(event) => onFormChange({ ...form, implementation: event.target.value })}
              spellCheck={false}
              className="min-h-[260px] resize-y font-mono text-[13px] leading-6"
            />
          </FormField>
          <div>
            <p className="mb-2 text-[12px] font-semibold text-foreground/80">生成的 Python 函数</p>
            <pre className="max-h-52 w-full min-w-0 max-w-full overflow-y-auto whitespace-pre-wrap break-all rounded-[10px] border border-border/55 bg-muted/40 p-4 font-mono text-[12px] leading-5 text-foreground">
              <code>{validName ? buildPredicateSource(form) : "填写有效的函数名称后生成预览。"}</code>
            </pre>
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button
            type="button"
            disabled={!canSave || busy}
            onClick={onSave}
          >
            {busy ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Check className="mr-2 h-4 w-4" aria-hidden />
            )}
            保存函数
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PredicateDetail({
  predicate,
  canManage,
  onOpenChange,
  onEdit,
  onDelete,
}: {
  predicate: AgentSpecPredicate | null;
  canManage: boolean;
  onOpenChange: (open: boolean) => void;
  onEdit: (predicate: AgentSpecPredicate) => void;
  onDelete: (predicate: AgentSpecPredicate) => void;
}) {
  if (!predicate) return null;
  const custom = predicate.source_kind === "custom";
  return (
    <Sheet open onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[min(42rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <div className="border-b border-border/50 px-6 py-6 pr-12">
          <div className="flex items-start gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-[12px] bg-muted/70 text-muted-foreground">
              <Braces className="h-5 w-5" aria-hidden />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <SheetTitle className="truncate font-mono text-[17px]">{predicate.name}</SheetTitle>
                <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">
                  {custom ? "自定义" : "系统内置"}
                </span>
              </div>
              <SheetDescription className="mt-1.5 text-[13px] leading-5">
                {predicate.description}
              </SheetDescription>
            </div>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <DetailRow label="注册来源">
                <p className="text-[13px] text-foreground">{custom ? "用户自定义" : "NimbusOps 内置"}</p>
              </DetailRow>
              <DetailRow label="Python 模块">
                <p className="break-all font-mono text-[12px] leading-5 text-foreground">
                  {predicate.module || "等待后端进程重启后显示"}
                </p>
              </DetailRow>
            </div>
            <DetailRow label="函数实现">
              <pre className="max-h-[52vh] overflow-auto rounded-[10px] border border-border/55 bg-muted/40 p-4 font-mono text-[12px] leading-5 text-foreground">
                <code>{predicate.source || "重启 NimbusOps 后即可读取并展示完整函数源码。"}</code>
              </pre>
            </DetailRow>
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-border/50 px-6 py-4">
          {custom && canManage ? (
            <>
              <Button type="button" variant="outline" onClick={() => onDelete(predicate)}>
                <Trash2 className="mr-2 h-4 w-4" aria-hidden />
                删除
              </Button>
              <Button type="button" onClick={() => onEdit(predicate)}>
                <Pencil className="mr-2 h-4 w-4" aria-hidden />
                编辑函数
              </Button>
            </>
          ) : (
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              关闭
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function DeleteDialog({
  open,
  title,
  description,
  busy,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  title: string;
  description: string;
  busy: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
            删除
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SectionTab({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "h-8 rounded-[8px] px-4 text-[12px] font-semibold transition-colors",
        active
          ? "bg-background text-foreground shadow-sm"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}

function CatalogSection({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description: string;
  action: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-5">
      <div className="flex items-center justify-between gap-4 border-b border-border/45 pb-3">
        <div>
          <h2 className="text-[13px] font-semibold text-foreground/85">{title}</h2>
          <p className="mt-1 text-[12px] text-muted-foreground">{description}</p>
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

function FormField({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="flex items-baseline justify-between gap-3">
        <span className="text-[13px] font-semibold text-foreground">{label}</span>
        <span className="text-[11px] text-muted-foreground">{hint}</span>
      </span>
      <span className="mt-2 block">{children}</span>
    </label>
  );
}

function SecurityToggle({
  checked,
  disabled = false,
  ariaLabel,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  ariaLabel: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
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

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-2 text-[11px] font-semibold uppercase text-muted-foreground">{label}</p>
      {children}
    </div>
  );
}

function extractPredicateImplementation(source: string | null): string {
  if (!source) return "";
  const lines = source.replace(/\r/g, "").split("\n");
  const definitionIndex = lines.findIndex((line) => /^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(/.test(line));
  if (definitionIndex < 0) return "";

  const bodyLines = lines.slice(definitionIndex + 1);
  while (bodyLines.length && !bodyLines[0].trim()) bodyLines.shift();
  const indents = bodyLines
    .filter((line) => line.trim())
    .map((line) => line.match(/^\s*/)?.[0].length ?? 0);
  const indent = indents.length ? Math.min(...indents) : 0;
  const normalized = bodyLines.map((line) => line.slice(Math.min(indent, line.length)));

  if (normalized.length) {
    const first = normalized[0].trim();
    if (/^(['"]).*\1$/.test(first)) {
      normalized.shift();
    } else if (first.startsWith('"""') || first.startsWith("'''")) {
      const quote = first.slice(0, 3);
      if (first.slice(3).includes(quote)) {
        normalized.shift();
      } else {
        normalized.shift();
        while (normalized.length) {
          const line = normalized.shift();
          if (line?.includes(quote)) break;
        }
      }
    }
  }
  while (normalized.length && !normalized[0].trim()) normalized.shift();
  return normalized.join("\n").trimEnd();
}

function buildPredicateList(payload: AgentSpecPayload | null): AgentSpecPredicate[] {
  if (!payload) return [];
  const detailed = new Map((payload.predicates ?? []).map((predicate) => [predicate.name, predicate]));
  for (const name of payload.registered_predicates ?? []) {
    if (detailed.has(name)) continue;
    detailed.set(name, {
      name,
      description: PREDICATE_DESCRIPTIONS[name] ?? "已注册到 predicate_table 的安全判断函数。",
      module: "",
      qualified_name: name,
      signature: "(user_input, tool_input, history)",
      source: null,
      source_kind: "builtin",
    });
  }
  return [...detailed.values()].sort((left, right) => left.name.localeCompare(right.name));
}

function LoadingState({ label }: { label: string }) {
  return (
    <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return <div className="px-3 py-12 text-center text-sm text-muted-foreground">{label}</div>;
}

function PredicateRow({
  predicate,
  onOpen,
  onDelete,
}: {
  predicate: AgentSpecPredicate;
  onOpen: () => void;
  onDelete?: () => void;
}) {
  return (
    <div className="group flex min-w-0 items-center gap-3 rounded-[16px] px-3 py-3 transition-colors hover:bg-muted/45">
      <button
        type="button"
        aria-label={`查看 Predicate ${predicate.name}`}
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none"
      >
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-[14px] bg-muted/70 text-muted-foreground">
          <Braces className="h-5 w-5" strokeWidth={1.8} aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate font-mono text-[14px] font-semibold text-foreground">
              {predicate.name}
            </span>
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-muted-foreground">
              {predicate.source_kind === "builtin" ? "内置" : "自定义"}
            </span>
          </span>
          <span className="mt-1 line-clamp-2 block text-[13px] leading-5 text-muted-foreground">
            {predicate.description}
          </span>
        </span>
      </button>

      {onDelete ? (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={`删除 ${predicate.name}`}
          onClick={onDelete}
          className="h-8 w-8 shrink-0 rounded-full text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden />
        </Button>
      ) : (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={`查看 ${predicate.name}`}
          onClick={onOpen}
          className="h-8 w-8 shrink-0 rounded-full text-muted-foreground"
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden />
        </Button>
      )}
    </div>
  );
}

function PolicyRow({
  rule,
  busy,
  readonly,
  onOpen,
  onToggle,
  onDelete,
}: {
  rule: AgentSpecRule;
  busy: boolean;
  readonly: boolean;
  onOpen: () => void;
  onToggle: () => void;
  onDelete?: () => void;
}) {
  return (
    <div
      className={cn(
        "group flex min-w-0 items-center gap-3 rounded-[16px] px-3 py-3 transition-colors hover:bg-muted/45",
        !rule.enabled && "opacity-60",
      )}
    >
      <button
        type="button"
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none"
      >
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-[14px] bg-muted/70 text-muted-foreground">
          <FileCode2 className="h-5 w-5" strokeWidth={1.8} aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-[15px] font-semibold text-foreground">{rule.id}</span>
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-muted-foreground">
              {rule.source === "builtin" ? "内置" : "自定义"}
            </span>
          </span>
          <span className="mt-1 block truncate text-[13px] text-muted-foreground">
            {rule.event} · {ENFORCEMENT_LABELS[rule.enforcement] ?? rule.enforcement}
          </span>
          <span className="mt-1 block truncate text-[11.5px] text-muted-foreground/75">
            {rule.predicates.join(" && ")}
          </span>
        </span>
      </button>

      <SecurityToggle
        checked={rule.enabled}
        ariaLabel={`${rule.id} ${rule.enabled ? "已启用" : "已停用"}`}
        disabled={readonly || busy}
        onChange={onToggle}
      />

      {onDelete ? (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={`删除 ${rule.id}`}
          onClick={onDelete}
          className="h-8 w-8 shrink-0 rounded-full text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden />
        </Button>
      ) : (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={`查看 ${rule.id}`}
          onClick={onOpen}
          className="h-8 w-8 shrink-0 rounded-full text-muted-foreground"
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden />
        </Button>
      )}
    </div>
  );
}
