import { useCallback, useEffect, useState } from "react";
import {
  CircleAlert,
  Loader2,
  Pencil,
  Plus,
  ShieldCheck,
  Trash2,
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
import { StyledSelect } from "@/components/ui/styled-select";
import { createUser, deleteUser, fetchUsers, updateUser } from "@/lib/api";
import type { AuthUser, UserRole } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type UserForm = {
  username: string;
  displayName: string;
  password: string;
  role: UserRole;
  enabled: boolean;
  workspaceRoot: string;
};

const EMPTY_FORM: UserForm = {
  username: "",
  displayName: "",
  password: "",
  role: "user",
  enabled: true,
  workspaceRoot: "",
};

export function UserManagementSettings() {
  const { token, user: currentUser } = useClient();
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<AuthUser | null>(null);
  const [form, setForm] = useState<UserForm>(EMPTY_FORM);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await fetchUsers(token);
      setUsers(payload.users);
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

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setEditorOpen(true);
  };

  const openEdit = (user: AuthUser) => {
    setEditing(user);
    setForm({
      username: user.username,
      displayName: user.display_name,
      password: "",
      role: user.role,
      enabled: user.enabled,
      workspaceRoot: user.workspace_root ?? "",
    });
    setEditorOpen(true);
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      if (editing) {
        await updateUser(token, editing.id, {
          display_name: form.displayName,
          role: form.role,
          enabled: form.enabled,
          workspace_root: form.role === "user" ? form.workspaceRoot : null,
          ...(form.password ? { password: form.password } : {}),
        });
      } else {
        await createUser(token, {
          username: form.username,
          display_name: form.displayName,
          password: form.password,
          role: form.role,
          workspace_root: form.role === "user" ? form.workspaceRoot : null,
        });
      }
      setEditorOpen(false);
      await load();
    } catch (saveError) {
      setError((saveError as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (user: AuthUser) => {
    if (!window.confirm(`确定删除用户“${user.display_name}”吗？`)) return;
    setBusy(true);
    try {
      await deleteUser(token, user.id);
      await load();
    } catch (deleteError) {
      setError((deleteError as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section className="border-b border-border/45 pb-4">
        <div className="inline-flex items-center gap-2 rounded-full bg-muted/65 px-3 py-1.5 text-[12px] font-semibold">
          <UserRound className="h-3.5 w-3.5" />
          用户与权限
        </div>
        <p className="mt-3 max-w-[720px] text-[13px] leading-5 text-muted-foreground">
          系统管理员可管理配置和主机执行；普通用户只能访问自己的会话，并强制使用执行沙箱。
        </p>
      </section>

      {error ? (
        <div className="flex items-start gap-2 rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-3 text-[13px] text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          {error}
        </div>
      ) : null}

      <section>
        <div className="flex items-center justify-between border-b border-border/45 pb-3">
          <div>
            <h2 className="text-[13px] font-semibold">账号</h2>
            <p className="mt-1 text-[12px] text-muted-foreground">共 {users.length} 个用户</p>
          </div>
          <Button type="button" size="sm" className="h-8 rounded-full px-3 text-[12px]" onClick={openCreate}>
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            新建用户
          </Button>
        </div>

        {loading ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在加载用户...
          </div>
        ) : (
          <div className="grid gap-x-8 gap-y-1 py-3 md:grid-cols-2">
            {users.map((user) => (
              <div
                key={user.id}
                className="flex min-w-0 items-center gap-3 rounded-[12px] px-3 py-3 transition-colors hover:bg-muted/45"
              >
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-[11px] bg-muted text-[12px] font-semibold">
                  {(user.display_name || user.username).slice(0, 1).toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[13.5px] font-semibold">{user.display_name}</span>
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                      {user.role === "system_admin" ? "系统管理员" : "普通用户"}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-[11.5px] text-muted-foreground">
                    <span className="truncate">{user.username}</span>
                    <span className={cn("h-1.5 w-1.5 rounded-full", user.enabled ? "bg-emerald-500" : "bg-muted-foreground/35")} />
                    <span>{user.enabled ? "已启用" : "已禁用"}</span>
                  </div>
                  {user.role !== "system_admin" ? (
                    <div className="mt-1 truncate text-[11px] text-muted-foreground/80">
                      工作区：{user.workspace_root || "未配置"}
                    </div>
                  ) : null}
                </div>
                <Button type="button" variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(user)}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                {user.id !== currentUser.id ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    disabled={busy}
                    className="h-8 w-8 text-muted-foreground hover:text-destructive"
                    onClick={() => void remove(user)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>

      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "编辑用户" : "新建用户"}</DialogTitle>
            <DialogDescription>
              普通用户的命令会强制进入系统沙箱，且只能看到自己的会话。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <Input
              value={form.username}
              disabled={Boolean(editing)}
              placeholder="用户名"
              onChange={(event) => setForm({ ...form, username: event.target.value })}
            />
            <Input
              value={form.displayName}
              placeholder="显示名称"
              onChange={(event) => setForm({ ...form, displayName: event.target.value })}
            />
            <Input
              type="password"
              value={form.password}
              placeholder={editing ? "留空表示不修改密码" : "初始密码，至少 8 个字符"}
              onChange={(event) => setForm({ ...form, password: event.target.value })}
            />
            <label className="block">
              <span className="mb-2 block text-[12px] font-semibold">用户角色</span>
              <StyledSelect
                value={form.role}
                onChange={(event) => setForm({ ...form, role: event.target.value as UserRole })}
              >
                <option value="user">普通用户</option>
                <option value="system_admin">系统管理员</option>
              </StyledSelect>
            </label>
            {form.role === "user" ? (
              <label className="block">
                <span className="mb-2 block text-[12px] font-semibold">工作区根路径</span>
                <Input
                  value={form.workspaceRoot}
                  placeholder="/Users/name/project"
                  onChange={(event) => setForm({ ...form, workspaceRoot: event.target.value })}
                />
                <span className="mt-1.5 block text-[11px] leading-4 text-muted-foreground">
                  普通用户只能在这个目录下选择项目，所有执行沙箱都以该范围为边界。
                </span>
              </label>
            ) : null}
            {editing ? (
              <button
                type="button"
                role="switch"
                aria-checked={form.enabled}
                onClick={() => setForm({ ...form, enabled: !form.enabled })}
                className="flex w-full items-center justify-between rounded-[10px] border border-border/50 px-3 py-3 text-left"
              >
                <span>
                  <span className="block text-[13px] font-semibold">允许登录</span>
                  <span className="mt-0.5 block text-[11px] text-muted-foreground">禁用后，该账号的会话立即失效。</span>
                </span>
                <span className={cn("relative h-[22px] w-[38px] rounded-full p-[2px]", form.enabled ? "bg-[#2997FF]" : "bg-muted")}>
                  <span className={cn("block h-[18px] w-[18px] rounded-full bg-background shadow-sm transition-transform", form.enabled && "translate-x-[16px]")} />
                </span>
              </button>
            ) : null}
            <div className="rounded-[10px] bg-muted/45 px-3 py-2.5 text-[11.5px] leading-5 text-muted-foreground">
              <ShieldCheck className="mr-1.5 inline h-3.5 w-3.5" />
              {form.role === "system_admin"
                ? "可管理用户、配置、Tools、OpsShield 和主机执行。"
                : "可聊天、查看 Tools 和 Skills，命令强制进入沙箱，并限制在自己的工作区根路径内。"}
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setEditorOpen(false)}>取消</Button>
            <Button
              type="button"
              disabled={
                busy
                || !form.displayName.trim()
                || (form.role === "user" && !form.workspaceRoot.trim())
                || (!editing && (!form.username.trim() || form.password.length < 8))
                || (Boolean(editing) && Boolean(form.password) && form.password.length < 8)
              }
              onClick={() => void save()}
            >
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
