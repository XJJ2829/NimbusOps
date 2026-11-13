import { useState } from "react";
import { CircleAlert, Eye, EyeOff, Loader2, LockKeyhole } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function AuthView({
  setup,
  error,
  busy,
  onSubmit,
}: {
  setup: boolean;
  error: string | null;
  busy: boolean;
  onSubmit: (values: {
    username: string;
    displayName: string;
    password: string;
  }) => void;
}) {
  const [username, setUsername] = useState(setup ? "admin" : "");
  const [displayName, setDisplayName] = useState(setup ? "系统管理员" : "");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const localError = setup && confirmPassword && password !== confirmPassword
    ? "两次输入的密码不一致"
    : null;
  const canSubmit = Boolean(
    username.trim()
      && password.length >= 8
      && (!setup || (displayName.trim() && password === confirmPassword)),
  );

  return (
    <div className="flex min-h-full w-full items-center justify-center bg-background px-5 py-10">
      <div className="w-full max-w-[390px]">
        <div className="mb-9 flex flex-col items-center text-center">
          <img
            src="/brand/NimbusOps.svg"
            alt=""
            className="mb-5 h-14 w-14 object-contain"
            draggable={false}
          />
          <h1 className="text-[26px] font-semibold tracking-normal text-foreground">
            {setup ? "初始化 NimbusOps" : "登录 NimbusOps"}
          </h1>
          <p className="mt-2 text-[13px] leading-5 text-muted-foreground">
            {setup
              ? "创建第一个系统管理员。完成后即可管理用户、权限与沙箱。"
              : "使用管理员分配的账号进入运维工作台。"}
          </p>
        </div>

        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit || busy) return;
            onSubmit({
              username: username.trim(),
              displayName: displayName.trim(),
              password,
            });
          }}
        >
          <AuthField label="用户名">
            <Input
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="请输入用户名"
              disabled={busy}
              className="h-11 rounded-[10px]"
            />
          </AuthField>

          {setup ? (
            <AuthField label="显示名称">
              <Input
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="例如：系统管理员"
                disabled={busy}
                className="h-11 rounded-[10px]"
              />
            </AuthField>
          ) : null}

          <AuthField label="密码">
            <div className="relative">
              <Input
                type={visible ? "text" : "password"}
                autoComplete={setup ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="至少 8 个字符"
                disabled={busy}
                className="h-11 rounded-[10px] pr-11"
              />
              <button
                type="button"
                aria-label={visible ? "隐藏密码" : "显示密码"}
                onClick={() => setVisible((current) => !current)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
              >
                {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </AuthField>

          {setup ? (
            <AuthField label="确认密码">
              <Input
                type={visible ? "text" : "password"}
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                placeholder="再次输入密码"
                disabled={busy}
                className="h-11 rounded-[10px]"
              />
            </AuthField>
          ) : null}

          {error || localError ? (
            <div className="flex items-start gap-2 rounded-[10px] border border-destructive/20 bg-destructive/5 px-3 py-2.5 text-[12.5px] text-destructive">
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{localError || error}</span>
            </div>
          ) : null}

          <Button
            type="submit"
            disabled={!canSubmit || busy}
            className="h-11 w-full rounded-[10px] text-[13px] font-semibold"
          >
            {busy ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <LockKeyhole className="mr-2 h-4 w-4" />
            )}
            {setup ? "创建管理员并进入" : "登录"}
          </Button>
        </form>
      </div>
    </div>
  );
}

function AuthField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-[12px] font-semibold text-foreground/80">{label}</span>
      {children}
    </label>
  );
}
