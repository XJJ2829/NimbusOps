import { useState } from "react";
import { IdCard, LockKeyhole, UsersRound } from "lucide-react";

import { RuntimeIdentitySettings } from "@/components/settings/RuntimeIdentitySettings";
import { SandboxSettings } from "@/components/settings/SandboxSettings";
import { UserManagementSettings } from "@/components/settings/UserManagementSettings";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type SandboxArea = "execution" | "users" | "identity";

export function SandboxHub() {
  const { hasPermission } = useClient();
  const [area, setArea] = useState<SandboxArea>("execution");
  const showUsers = hasPermission("users.manage");

  return (
    <div>
      <div className="mb-7 flex w-full gap-1 overflow-x-auto rounded-[11px] bg-muted/65 p-1">
        <HubTab
          active={area === "execution"}
          icon={LockKeyhole}
          label="执行沙箱"
          onClick={() => setArea("execution")}
        />
        {showUsers ? (
          <HubTab
            active={area === "users"}
            icon={UsersRound}
            label="用户与权限"
            onClick={() => setArea("users")}
          />
        ) : null}
        <HubTab
          active={area === "identity"}
          icon={IdCard}
          label="运行身份与权限隔离"
          onClick={() => setArea("identity")}
        />
      </div>

      {area === "execution" ? <SandboxSettings /> : null}
      {area === "users" && showUsers ? <UserManagementSettings /> : null}
      {area === "identity" ? <RuntimeIdentitySettings /> : null}
    </div>
  );
}

function HubTab({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: typeof LockKeyhole;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-9 shrink-0 items-center gap-2 rounded-[8px] px-4 text-[12px] font-semibold transition-colors",
        active
          ? "bg-background text-foreground shadow-sm"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}
