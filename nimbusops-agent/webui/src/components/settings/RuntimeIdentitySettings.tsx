import { useCallback, useEffect, useState } from "react";
import {
  BadgeCheck,
  Check,
  CircleAlert,
  CircleUserRound,
  FileLock2,
  IdCard,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldOff,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StyledSelect } from "@/components/ui/styled-select";
import { PrivilegedExecutionSettings } from "@/components/settings/PrivilegedExecutionSettings";
import { fetchRuntimeIdentity, updateRuntimeIdentity } from "@/lib/api";
import type { RuntimeIdentityPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type IdentityForm = RuntimeIdentityPayload["configured"];
type IdentitySection = "identity" | "privilege";

export function RuntimeIdentitySettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("sandbox.manage");
  const [section, setSection] = useState<IdentitySection>("identity");
  const [payload, setPayload] = useState<RuntimeIdentityPayload | null>(null);
  const [form, setForm] = useState<IdentityForm | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetchRuntimeIdentity(token);
      setPayload(next);
      setForm(next.configured);
      setError(null);
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      const next = await updateRuntimeIdentity(token, form);
      setPayload(next);
      setForm(next.configured);
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (loading || !form || !payload) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        正在读取运行身份...
      </div>
    );
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(payload.configured);
  const description = section === "identity"
    ? "NimbusOps 使用独立受限系统账号运行。命令执行前校验有效 UID/GID，并用 umask 控制新建文件和目录的默认权限。"
    : "需要 root 权限的动作交给独立 helper 执行，并通过白名单策略、管理员审批和审计记录保持隔离。";

  return (
    <div className="space-y-7">
      <section className="border-b border-border/45 pb-4">
        <IdentitySectionSwitch value={section} onChange={setSection} />
        <p className="mt-3 max-w-[760px] text-[13px] leading-5 text-muted-foreground">
          {description}
        </p>
      </section>

      {section === "identity" ? (
        <>
          {error ? (
            <Notice tone="error" icon={CircleAlert}>{error}</Notice>
          ) : null}
          {payload.error ? (
            <Notice tone="warning" icon={CircleAlert}>
              {payload.error}
              {payload.setup_command ? (
                <span className="mt-1 block font-mono text-[11px]">{payload.setup_command}</span>
              ) : null}
            </Notice>
          ) : null}
          {payload.requires_restart ? (
            <Notice tone="warning" icon={RefreshCw}>
              配置已保存，但当前服务并非以目标账号运行。请切换到该账号后重启 NimbusOps。
            </Notice>
          ) : null}

          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatusTile
              icon={CircleUserRound}
              label="当前运行账号"
              value={payload.current.username}
              caption={`UID ${payload.current.uid ?? "--"} · GID ${payload.current.gid ?? "--"}`}
              healthy={!payload.current.is_root}
            />
            <StatusTile
              icon={BadgeCheck}
              label="目标受限账号"
              value={payload.target.username || "未配置"}
              caption={payload.target.exists ? `UID ${payload.target.uid} · ${payload.target.group}` : "系统中不存在"}
              healthy={payload.target.exists && payload.target_matches_current}
            />
            <StatusTile
              icon={ShieldOff}
              label="root 启动保护"
              value={payload.root_startup_blocked ? "已禁止" : "未启用"}
              caption={payload.platform}
              healthy={payload.root_startup_blocked}
            />
            <StatusTile
              icon={FileLock2}
              label="默认文件权限"
              value={payload.file_policy.new_file_mode}
              caption={`目录 ${payload.file_policy.new_directory_mode} · umask ${payload.file_policy.umask}`}
              healthy={payload.compliant}
            />
          </section>

          <section className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
            <SettingRow
              title="启用受限系统账号"
              description="启用后，NimbusOps 必须由目标系统账号启动，否则命令执行会被拒绝。"
            >
              <Toggle
                checked={form.enabled}
                disabled={!canManage}
                onChange={(enabled) => setForm({ ...form, enabled })}
                label="启用受限系统账号"
              />
            </SettingRow>
            <SettingRow
              title="系统账号"
              description="Linux 或 macOS 中已存在的低权限账号，不应属于 sudo、wheel 等管理组。"
            >
              <Input
                value={form.account}
                disabled={!canManage}
                className="h-10 w-[240px]"
                placeholder="nimbusops"
                onChange={(event) => setForm({ ...form, account: event.target.value })}
              />
            </SettingRow>
            <SettingRow
              title="系统用户组"
              description="留空时使用该账号的主用户组。"
            >
              <Input
                value={form.group}
                disabled={!canManage}
                className="h-10 w-[240px]"
                placeholder="使用账号主用户组"
                onChange={(event) => setForm({ ...form, group: event.target.value })}
              />
            </SettingRow>
            <SettingRow
              title="自动禁止 root 启动"
              description="检测到有效 UID 为 0 时，NimbusOps 会在开放服务端口前终止启动。"
            >
              <Toggle
                checked={form.deny_root_startup}
                disabled={!canManage}
                onChange={(deny_root_startup) => setForm({ ...form, deny_root_startup })}
                label="自动禁止 root 启动"
              />
            </SettingRow>
            <SettingRow
              title="文件权限策略"
              description="通过进程 umask 区分属主、同组用户和其他系统用户的文件访问权限。"
            >
              <StyledSelect
                aria-label="进程 umask"
                value={form.process_umask}
                disabled={!canManage}
                className="w-[240px]"
                onChange={(event) => setForm({ ...form, process_umask: event.target.value })}
              >
                <option value="027">平衡隔离（文件 0640 / 目录 0750）</option>
                <option value="077">严格隔离（文件 0600 / 目录 0700）</option>
                <option value="022">同组可读（文件 0644 / 目录 0755）</option>
              </StyledSelect>
            </SettingRow>
          </section>

          {canManage ? (
            <div className="flex justify-end">
              <Button type="button" disabled={!dirty || saving} onClick={() => void save()}>
                {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}
                保存运行身份设置
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <PrivilegedExecutionSettings />
      )}
    </div>
  );
}

function IdentitySectionSwitch({
  value,
  onChange,
}: {
  value: IdentitySection;
  onChange: (value: IdentitySection) => void;
}) {
  const activeIndex = value === "privilege" ? 1 : 0;
  return (
    <div
      role="tablist"
      aria-label="运行身份与权限隔离子页面"
      className="relative inline-grid h-8 grid-cols-2 rounded-full bg-muted/65 p-0.5 text-[12px] font-semibold text-muted-foreground"
    >
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-0.5 left-0.5 w-[calc(50%-2px)] rounded-full bg-background shadow-sm",
          "transition-transform duration-200 ease-out",
          activeIndex === 1 && "translate-x-full",
        )}
      />
      <IdentitySectionTab
        active={value === "identity"}
        icon={IdCard}
        label="运行身份"
        onClick={() => onChange("identity")}
      />
      <IdentitySectionTab
        active={value === "privilege"}
        icon={KeyRound}
        label="权限隔离"
        onClick={() => onChange("privilege")}
      />
    </div>
  );
}

function IdentitySectionTab({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: typeof IdCard;
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
        "relative z-10 flex h-7 items-center justify-center gap-1.5 rounded-full px-3 transition-colors duration-200",
        active ? "text-foreground" : "hover:text-foreground",
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}

function StatusTile({
  icon: Icon,
  label,
  value,
  caption,
  healthy,
}: {
  icon: typeof IdCard;
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
        <span className="truncate text-[17px] font-semibold">{value}</span>
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", healthy ? "bg-emerald-500" : "bg-amber-500")} />
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">{caption}</div>
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
        <div className="mt-1 max-w-[560px] text-[11.5px] leading-5 text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Toggle({
  checked,
  disabled,
  onChange,
  label,
}: {
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
        checked && "translate-x-[16px]",
      )} />
    </button>
  );
}

function Notice({
  tone,
  icon: Icon,
  children,
}: {
  tone: "error" | "warning";
  icon: typeof CircleAlert;
  children: React.ReactNode;
}) {
  return (
    <div className={cn(
      "flex items-start gap-2 rounded-[12px] border px-3 py-3 text-[13px]",
      tone === "error"
        ? "border-destructive/20 bg-destructive/5 text-destructive"
        : "border-amber-500/20 bg-amber-500/5 text-amber-700 dark:text-amber-300",
    )}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <div>{children}</div>
    </div>
  );
}
