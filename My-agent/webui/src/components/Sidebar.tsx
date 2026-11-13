import { useState, type ReactNode } from "react";
import {
  Archive,
  Brain,
  Box,
  CalendarClock,
  ChevronUp,
  Gauge,
  KeyRound,
  LogOut,
  Menu,
  Search,
  ScrollText,
  ShieldCheck,
  Settings,
  SquarePen,
  Blocks,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type {
  AuthUser,
  ChatSummary,
  SidebarViewState,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onTogglePin: (key: string) => void;
  onRequestRename: (key: string, label: string) => void;
  onToggleArchive: (key: string) => void;
  onToggleGroup: (groupId: string) => void;
  onRequestRenameProject: (projectKey: string, label: string) => void;
  onNewChatInProject: (projectPath: string, projectName: string) => void;
  onOpenSettings: () => void;
  onOpenApps: () => void;
  onOpenAutomations: () => void;
  onOpenSkills: () => void;
  onOpenSecurity: () => void;
  onOpenGuardrails: () => void;
  onOpenSandbox: () => void;
  onOpenAudit: () => void;
  onOpenSearch: () => void;
  user: AuthUser;
  onLogout: () => void;
  onChangePassword: () => void;
  showApps?: boolean;
  showSkills?: boolean;
  showSettings?: boolean;
  activeUtility?: "apps" | "automations" | "skills" | "security" | "guardrails" | "sandbox" | "audit" | null;
  onToggleArchived: () => void;
  onCollapse: () => void;
  onExpand?: () => void;
  containActionMenus?: boolean;
  collapsed?: boolean;
  pinnedKeys?: string[];
  archivedKeys?: string[];
  titleOverrides?: Record<string, string>;
  projectNameOverrides?: Record<string, string>;
  collapsedGroups?: Record<string, boolean>;
  runningChatIds?: string[];
  completedChatIds?: string[];
  viewState?: SidebarViewState;
  showArchived?: boolean;
  archivedCount?: number;
  defaultWorkspacePath?: string | null;
}

type NavigatorWithUserAgentData = Navigator & {
  userAgentData?: { platform?: string };
};

function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform = navigator.platform || "";
  const userAgentPlatform =
    (navigator as NavigatorWithUserAgentData).userAgentData?.platform || "";
  return /mac|iphone|ipad|ipod/i.test(`${platform} ${userAgentPlatform}`);
}

function newChatShortcutLabel(): string {
  return isApplePlatform() ? "⌘⇧O" : "Ctrl+Shift+O";
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [menuPortalContainer, setMenuPortalContainer] =
    useState<HTMLElement | null>(null);
  const collapsed = Boolean(props.collapsed);
  const newChatShortcut = newChatShortcutLabel();

  return (
    <nav
      ref={props.containActionMenus ? setMenuPortalContainer : undefined}
      aria-label={t("sidebar.navigation")}
      className={cn(
        "flex h-full w-full min-w-0 flex-col border-r border-sidebar-border/60 bg-sidebar text-sidebar-foreground",
      )}
    >
      <div
        className={cn(
          "flex items-center px-3 pb-2.5",
          "pt-3",
          collapsed ? "w-14 justify-start" : "justify-between",
        )}
      >
        <button
          type="button"
          aria-label="NimbusOps home"
          title="NimbusOps home"
          onClick={() => {
            props.onNewChat();
            if (collapsed) props.onExpand?.();
          }}
      className={cn(
        "flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl transition-colors",
        "ml-0.5 hover:bg-sidebar-accent/75",
      )}
        >
          <img
            src="/brand/NimbusOps.svg"
            alt=""
            className="h-8 w-8 select-none object-contain"
            draggable={false}
          />
        </button>
        {!collapsed && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-7 w-7 rounded-lg text-muted-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          >
            <Menu className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      <div
        className={cn(
          "space-y-1.5 px-2 pb-2",
          collapsed && "flex w-14 flex-col items-center px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.newChat")}
          onClick={props.onNewChat}
          icon={<SquarePen className="h-4 w-4" />}
          shortcut={newChatShortcut}
          ariaKeyShortcuts="Meta+Shift+O Control+Shift+O"
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.searchAria")}
          onClick={props.onOpenSearch}
          icon={<Search className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.automations", { defaultValue: "自动运维" })}
          onClick={props.onOpenAutomations}
          active={props.activeUtility === "automations"}
          icon={<CalendarClock className="h-4 w-4" />}
        />
        {props.showApps !== false ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={t("sidebar.apps")}
            onClick={props.onOpenApps}
            active={props.activeUtility === "apps"}
            icon={<Blocks className="h-4 w-4" />}
          />
        ) : null}
        {props.showSkills !== false ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={t("sidebar.skills.title")}
            onClick={props.onOpenSkills}
            active={props.activeUtility === "skills"}
            icon={<Brain className="h-4 w-4" />}
          />
        ) : null}
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.security")}
          onClick={props.onOpenSecurity}
          active={props.activeUtility === "security"}
          icon={<ShieldCheck className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.guardrails", { defaultValue: "Guardrails" })}
          onClick={props.onOpenGuardrails}
          active={props.activeUtility === "guardrails"}
          icon={<Gauge className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.sandbox")}
          onClick={props.onOpenSandbox}
          active={props.activeUtility === "sandbox"}
          icon={<Box className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.audit", { defaultValue: "审计中心" })}
          onClick={props.onOpenAudit}
          active={props.activeUtility === "audit"}
          icon={<ScrollText className="h-4 w-4" />}
        />
        {props.archivedCount ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={props.showArchived ? t("chat.hideArchived") : t("chat.showArchived")}
            onClick={props.onToggleArchived}
            icon={<Archive className="h-4 w-4" />}
          />
        ) : null}
      </div>
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden transition-opacity duration-200",
          collapsed && "pointer-events-none opacity-0",
        )}
      >
        {!collapsed && (
          <ChatList
            sessions={props.sessions}
            activeKey={props.activeKey}
            loading={props.loading}
            emptyLabel={t("chat.noSessions")}
            onSelect={props.onSelect}
            onRequestDelete={props.onRequestDelete}
            onTogglePin={props.onTogglePin}
            onRequestRename={props.onRequestRename}
            onToggleArchive={props.onToggleArchive}
            onToggleGroup={props.onToggleGroup}
            onRequestRenameProject={props.onRequestRenameProject}
            onNewChatInProject={props.onNewChatInProject}
            pinnedKeys={props.pinnedKeys}
            archivedKeys={props.archivedKeys}
            titleOverrides={props.titleOverrides}
            projectNameOverrides={props.projectNameOverrides}
            collapsedGroups={props.collapsedGroups}
            runningChatIds={props.runningChatIds}
            completedChatIds={props.completedChatIds}
            density={props.viewState?.density}
            showPreviews={props.viewState?.show_previews}
            showTimestamps={props.viewState?.show_timestamps}
            sort={props.viewState?.sort}
            showArchived={props.showArchived}
            defaultWorkspacePath={props.defaultWorkspacePath}
            actionMenuPortalContainer={
              props.containActionMenus ? menuPortalContainer : undefined
            }
          />
        )}
      </div>
      <Separator className="bg-sidebar-border/50" />
      <div
        className={cn(
          "flex items-center gap-1 px-2 py-2 text-xs",
          collapsed && "w-14 flex-col px-0",
        )}
      >
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={cn(
                "flex min-w-0 items-center rounded-[10px] text-left transition-colors hover:bg-sidebar-accent/75",
                collapsed ? "h-9 w-9 justify-center" : "h-11 flex-1 gap-2.5 px-2",
              )}
            >
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[#386BF3] text-[11px] font-semibold text-white">
                {(props.user.display_name || props.user.username).slice(0, 1).toUpperCase()}
              </span>
              {!collapsed ? (
                <>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px] font-semibold text-sidebar-foreground">
                      {props.user.display_name}
                    </span>
                    <span className="block truncate text-[10.5px] text-muted-foreground">
                      {props.user.role === "system_admin" ? "系统管理员" : "普通用户"}
                    </span>
                  </span>
                  <ChevronUp className="h-3.5 w-3.5 text-muted-foreground" />
                </>
              ) : null}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side="top"
            align="start"
            className="w-56"
            portalContainer={props.containActionMenus ? menuPortalContainer : undefined}
          >
            <DropdownMenuLabel>
              <span className="block text-foreground">{props.user.display_name}</span>
              <span className="mt-0.5 block font-normal">{props.user.username}</span>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            {props.showSettings !== false ? (
              <DropdownMenuItem onSelect={() => props.onOpenSettings()}>
                <Settings className="h-4 w-4" />
                设置
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuItem onSelect={props.onChangePassword}>
              <KeyRound className="h-4 w-4" />
              修改密码
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={props.onLogout}
              className="text-destructive focus:text-destructive"
            >
              <LogOut className="h-4 w-4" />
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {!collapsed ? <ConnectionBadge /> : null}
      </div>
    </nav>
  );
}

function SidebarActionButton({
  collapsed,
  label,
  icon,
  onClick,
  active = false,
  className,
  shortcut,
  ariaKeyShortcuts,
}: {
  collapsed: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
  className?: string;
  shortcut?: string;
  ariaKeyShortcuts?: string;
}) {
  const title = shortcut ? `${label} (${shortcut})` : collapsed ? label : undefined;

  return (
    <Button
      type="button"
      variant="ghost"
      aria-label={label}
      aria-current={active ? "page" : undefined}
      aria-keyshortcuts={ariaKeyShortcuts}
      title={title}
      onClick={() => onClick()}
      className={cn(
        "group h-8 min-w-0 gap-2 overflow-hidden rounded-full font-medium text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
        "transition-[width,padding,border-radius,color,background-color] duration-300 ease-out",
        collapsed
          ? "w-9 justify-center gap-0 rounded-xl px-0"
          : "w-full justify-start gap-2 px-3 text-[12.5px]",
        active && "bg-sidebar-accent text-sidebar-foreground shadow-[inset_0_0_0_1px_hsl(var(--sidebar-border)/0.55)]",
        className,
      )}
    >
      <span
        className={cn(
          "flex shrink-0 items-center justify-center transition-transform duration-300 ease-out",
          collapsed ? "translate-x-0" : "translate-x-0",
        )}
        aria-hidden
      >
        {icon}
      </span>
      <span
        className={cn(
          "min-w-0 overflow-hidden truncate whitespace-nowrap transition-[max-width,opacity,transform] duration-200 ease-out",
          collapsed
            ? "max-w-0 -translate-x-1 opacity-0"
            : "max-w-[12rem] translate-x-0 opacity-100",
        )}
      >
        {label}
      </span>
    </Button>
  );
}
