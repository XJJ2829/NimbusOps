import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Check,
  CircleAlert,
  Cpu,
  FolderOpen,
  HardDrive,
  Loader2,
  MemoryStick,
  ShieldCheck,
  Server,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StyledSelect } from "@/components/ui/styled-select";
import { fetchSandbox, updateSandbox } from "@/lib/api";
import type { SandboxMetrics, SandboxPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

export function SandboxSettings() {
  const { token, hasPermission } = useClient();
  const canManage = hasPermission("sandbox.manage");
  const [payload, setPayload] = useState<SandboxPayload | null>(null);
  const [backend, setBackend] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [workspacePath, setWorkspacePath] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const next = await fetchSandbox(token);
      setPayload(next);
      if (!quiet) {
        setBackend(next.configured_backend);
        setRestricted(next.restrict_to_workspace);
        setWorkspacePath(next.workspace);
      }
      setError(null);
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 2_500);
    return () => window.clearInterval(timer);
  }, [load]);

  const dirty = payload
    ? backend !== payload.configured_backend
      || restricted !== payload.restrict_to_workspace
      || workspacePath.trim() !== payload.workspace
    : false;
  const metrics = payload?.metrics;
  const backendLabel = useMemo(() => {
    if (!payload?.effective_backend) return "未启用";
    if (payload.effective_backend === "sandbox-exec") return "sandbox-exec";
    if (payload.effective_backend === "bwrap") return "Bubblewrap";
    return payload.effective_backend;
  }, [payload?.effective_backend]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const next = await updateSandbox(token, {
        backend,
        restrict_to_workspace: restricted,
        workspace_path: workspacePath,
      });
      setPayload(next);
      setBackend(next.configured_backend);
      setRestricted(next.restrict_to_workspace);
      setWorkspacePath(next.workspace);
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        正在读取沙箱状态...
      </div>
    );
  }

  return (
    <div className="space-y-7">
      <section className="border-b border-border/45 pb-4">
        <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold">
          <ShieldCheck className="h-3.5 w-3.5" />
          执行沙箱
        </div>
        <p className="mt-3 max-w-[720px] text-[13px] leading-5 text-muted-foreground">
          Linux 使用 Bubblewrap，macOS 使用 sandbox-exec。普通用户始终强制在沙箱中执行命令。
        </p>
      </section>

      {error ? (
        <div className="flex items-start gap-2 rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-3 text-[13px] text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          {error}
        </div>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        <MetricTile
          icon={Cpu}
          label="系统 CPU"
          value={metrics?.available ? `${(metrics.system_cpu_percent ?? 0).toFixed(1)}%` : "--"}
          caption={`物理 ${metrics?.system_cpu_cores ?? 0} 核 · 逻辑 ${metrics?.system_cpu_logical_cores ?? 0} 线程`}
        />
        <MetricTile
          icon={Server}
          label="主机名"
          value={metrics?.hostname ?? "--"}
          caption="NimbusOps 运行所在宿主机"
        />
        <MetricTile
          icon={MemoryStick}
          label="系统内存"
          value={metrics?.available
            ? `${formatBytes(metrics.system_memory_bytes_used ?? 0)} / ${formatBytes(metrics.system_memory_bytes_total ?? 0)}`
            : "--"}
          caption={`已用 ${(metrics?.system_memory_percent ?? 0).toFixed(1)}%`}
        />
        <MetricTile
          icon={HardDrive}
          label="系统磁盘"
          value={metrics?.available && metrics.system_disk_percent != null
            ? `${metrics.system_disk_percent.toFixed(1)}%`
            : "--"}
          caption={
            metrics?.available
              ? `${formatBytes(metrics.system_disk_bytes_used ?? 0)} / ${formatBytes(metrics.system_disk_bytes_total ?? 0)}`
              : "无法读取磁盘快照"
          }
        />
        <MetricTile
          icon={FolderOpen}
          label="工作区占用"
          value={
            metrics?.available && typeof metrics.workspace_bytes_used === "number"
              ? formatBytes(metrics.workspace_bytes_used)
              : "--"
          }
          caption={
            metrics?.available
              ? `${metrics.workspace_file_count ?? 0} 个文件 · ${metrics.workspace_dir_count ?? 0} 个目录`
              : "无法读取工作区快照"
          }
        />
        <MetricTile
          icon={Activity}
          label="负载均值"
          value={formatLoadAverage(metrics)}
          caption="1 / 5 / 15 分钟"
        />
        <MetricTile
          icon={Activity}
          label="端口预览"
          value={String(metrics?.listening_port_count ?? "--")}
          caption={formatPortDetailPreview(metrics?.listening_port_details ?? [], metrics?.listening_ports ?? [])}
        />
        <MetricTile
          icon={Activity}
          label="网络流量"
          value={
            metrics?.available &&
            typeof metrics.network_bytes_sent === "number" &&
            typeof metrics.network_bytes_recv === "number"
              ? `${formatBytes(metrics.network_bytes_sent)} / ${formatBytes(metrics.network_bytes_recv)}`
              : "--"
          }
          caption="发 / 收"
        />
        <MetricTile
          icon={Activity}
          label="进程数"
          value={String(metrics?.process_count ?? "--")}
          caption={`运行 ${formatDuration(metrics?.uptime_seconds ?? 0)}`}
        />
        <MetricTile
          icon={ShieldCheck}
          label="生效后端"
          value={backendLabel}
          caption={payload?.platform ?? "Unknown"}
        />
      </section>

      <section className="overflow-hidden rounded-[14px] border border-border/50 bg-card">
        <SettingRow
          title="沙箱后端"
          description={
            payload?.forced_for_user
              ? "当前账号由系统强制使用自动沙箱。"
              : "自动模式会根据操作系统选择可用后端。"
          }
        >
          <StyledSelect
            aria-label="沙箱后端"
            value={backend}
            disabled={!canManage}
            onChange={(event) => setBackend(event.target.value)}
            className="h-10 min-w-[220px]"
          >
            <option value="">关闭（仅管理员）</option>
            <option value="auto">自动</option>
            <option value="bwrap">Bubblewrap（Linux）</option>
            <option value="sandbox-exec">sandbox-exec（macOS）</option>
          </StyledSelect>
        </SettingRow>
        <SettingRow
          title="限制在工作区"
          description="文件与命令工作目录限制在当前项目工作区内。"
        >
          <Switch
            checked={restricted}
            disabled={!canManage}
            onChange={setRestricted}
            label="限制在工作区"
          />
        </SettingRow>
        <SettingRow
          title="当前工作区"
          description="作为项目选择和沙箱文件边界的根路径。"
        >
          <Input
            value={workspacePath}
            disabled={!canManage}
            onChange={(event) => setWorkspacePath(event.target.value)}
            placeholder="/Users/name/project"
            className="h-10 w-[min(34rem,calc(100vw-4rem))] rounded-[12px] border-border/55 bg-background/80 px-3 font-mono text-[12px]"
          />
        </SettingRow>
        <SettingRow title="后端可用性" description="由当前 NimbusOps 主机实时检测。">
          <div className="flex items-center gap-3 text-[12px]">
            <Availability
              label="bwrap"
              available={Boolean(payload?.available_backends.bwrap)}
            />
            <Availability
              label="sandbox-exec"
              available={Boolean(payload?.available_backends["sandbox-exec"])}
            />
          </div>
        </SettingRow>
      </section>

      {canManage ? (
        <div className="flex justify-end">
          <Button type="button" disabled={!dirty || saving || !workspacePath.trim()} onClick={() => void save()}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}
            保存沙箱设置
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
  icon: typeof Cpu;
  label: string;
  value: string;
  caption: string;
}) {
  return (
    <div className="rounded-[12px] border border-border/45 bg-card px-4 py-4">
      <div className="flex items-center gap-2 text-[11px] font-semibold text-muted-foreground">
        <Icon className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-3 flex min-w-0 items-center gap-2">
        <span className="truncate text-[17px] font-semibold" title={value}>
          {value}
        </span>
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground" title={caption}>
        {caption}
      </div>
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
        <div className="mt-1 break-words text-[11.5px] leading-5 text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Availability({ label, available }: { label: string; available: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", available ? "text-emerald-600" : "text-muted-foreground")}>
      <span className={cn("h-1.5 w-1.5 rounded-full", available ? "bg-emerald-500" : "bg-muted-foreground/35")} />
      {label}
    </span>
  );
}

function Switch({
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
        "relative inline-flex h-[22px] w-[38px] items-center rounded-full p-[2px] transition-colors disabled:opacity-50",
        checked ? "bg-[#2997FF]" : "bg-muted",
      )}
    >
      <span
        className={cn(
          "h-[18px] w-[18px] rounded-full bg-background shadow-sm transition-transform",
          checked ? "translate-x-[16px]" : "translate-x-0",
        )}
      />
    </button>
  );
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 MB";
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(0)} MB`;
}

function formatLoadAverage(metrics: SandboxMetrics | null | undefined): string {
  if (!metrics?.available) return "--";
  const parts = [metrics.load_average_1m, metrics.load_average_5m, metrics.load_average_15m]
    .map((value) => (typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "--"));
  return parts.join(" / ");
}

function formatPortDetailPreview(
  details: NonNullable<SandboxMetrics["listening_port_details"]>,
  ports: number[],
): string {
  if (details.length) {
    const preview = details
      .slice(0, 3)
      .map((item) => `${item.port}·${item.process}`)
      .join("，");
    return details.length > 3 ? `${preview} … +${details.length - 3}` : preview;
  }
  if (!ports.length) return "暂无监听端口";
  const preview = ports.slice(0, 4).join(", ");
  return ports.length > 4 ? `${preview} … +${ports.length - 4}` : preview;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}
