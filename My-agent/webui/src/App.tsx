import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { AuditCenter } from "@/components/AuditCenter";
import { AutomationsView } from "@/components/AutomationsView";
import { AuthView } from "@/components/auth/AuthView";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { PasswordDialog } from "@/components/account/PasswordDialog";
import { RenameChatDialog } from "@/components/RenameChatDialog";
import { Sidebar } from "@/components/Sidebar";
import { SessionSearchDialog } from "@/components/SessionSearchDialog";
import { SettingsView, type SettingsSectionKey } from "@/components/settings/SettingsView";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

import { useSessions } from "@/hooks/useSessions";
import { useDeferredTitleRefresh } from "@/hooks/useDeferredTitleRefresh";
import { useSidebarState } from "@/hooks/useSidebarState";
import { useSkills } from "@/hooks/useSkills";
import { ThemeProvider, useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import {
  deriveWsUrl,
  fetchBootstrap,
} from "@/lib/bootstrap";
import { deriveTitle } from "@/lib/format";
import { NanobotClient } from "@/lib/nanobot-client";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import type {
  ChatSummary,
  AuthUser,
  SettingsPayload,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import {
  ApiError,
  fetchAuthStatus,
  fetchCurrentUser,
  fetchSettings,
  fetchWorkspaces,
  loginUser,
  logoutUser,
  restartRuntime,
  setupAdmin,
} from "@/lib/api";
import { projectNameFromPath, resolveWorkspaceProjectPath } from "@/lib/workspace";

type BootState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "auth"; setup: boolean; error?: string }
  | {
      status: "ready";
      client: NanobotClient;
      token: string;
      tokenExpiresAt: number;
      modelName: string | null;
      user: AuthUser;
    };

const SIDEBAR_STORAGE_KEY = "my-agent-webui.sidebar";
const COMPLETED_RUNS_STORAGE_KEY = "my-agent-webui.sidebar.completed-runs.v1";
const RESTART_STARTED_KEY = "my-agent-webui.restartStartedAt";
const SIDEBAR_WIDTH = 272;
const SIDEBAR_RAIL_WIDTH = 56;
const TOKEN_REFRESH_MARGIN_MS = 30_000;
const TOKEN_REFRESH_MIN_DELAY_MS = 5_000;
type ShellView =
  | "chat"
  | "settings"
  | "automations"
  | "apps"
  | "skills"
  | "security"
  | "guardrails"
  | "sandbox"
  | "audit";
type ShellRoute = {
  view: ShellView;
  activeKey: string | null;
  settingsSection: SettingsSectionKey;
};

const AUTOMATION_CHAT_PROMPT =
  "我想设置一个自动化。先简要说明 NimbusOps 中的自动化如何运作，然后问我几个问题，以了解我希望 NimbusOps 做什么以及何时运行。";

const SETTINGS_SECTION_KEYS: SettingsSectionKey[] = [
  "overview",
  "models",
  "browser",
  "apps",
  "skills",
  "security",
  "guardrails",
  "sandbox",
  "runtime",
];

function isSettingsSectionKey(value: string | null): value is SettingsSectionKey {
  return SETTINGS_SECTION_KEYS.includes(value as SettingsSectionKey);
}

function defaultShellRoute(): ShellRoute {
  return { view: "chat", activeKey: null, settingsSection: "overview" };
}

function shellViewForSettingsSection(section: SettingsSectionKey): ShellView {
  if (
    section === "apps"
    || section === "skills"
    || section === "security"
    || section === "guardrails"
    || section === "sandbox"
  ) return section;
  return "settings";
}

function readShellRoute(): ShellRoute {
  if (typeof window === "undefined") return defaultShellRoute();
  const hash = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;
  if (!hash || hash === "/" || hash === "/new") return defaultShellRoute();

  const [path, query = ""] = hash.split("?", 2);
  const params = new URLSearchParams(query);
  const rawSettingsSection = params.get("section");
  const settingsSection = isSettingsSectionKey(rawSettingsSection)
    ? rawSettingsSection
    : "overview";
  const activeKey = params.get("chat")?.trim() || null;

  if (path === "/settings") {
    return {
      view: shellViewForSettingsSection(settingsSection),
      activeKey,
      settingsSection,
    };
  }
  if (path === "/apps") {
    return { view: "apps", activeKey, settingsSection: "apps" };
  }
  if (path === "/automations") {
    return { view: "automations", activeKey, settingsSection: "overview" };
  }
  if (path === "/skills") {
    return { view: "skills", activeKey, settingsSection: "skills" };
  }
  if (path === "/security") {
    return { view: "security", activeKey, settingsSection: "security" };
  }
  if (path === "/guardrails") {
    return { view: "guardrails", activeKey, settingsSection: "guardrails" };
  }
  if (path === "/sandbox") {
    return { view: "sandbox", activeKey, settingsSection: "sandbox" };
  }
  if (path === "/audit") {
    return { view: "audit", activeKey, settingsSection: "overview" };
  }
  if (path.startsWith("/chat/")) {
    const encoded = path.slice("/chat/".length);
    try {
      const key = decodeURIComponent(encoded).trim();
      return key
        ? { view: "chat", activeKey: key, settingsSection: "overview" }
        : defaultShellRoute();
    } catch {
      return defaultShellRoute();
    }
  }
  return defaultShellRoute();
}

function shellRouteHash(route: ShellRoute): string {
  if (route.view === "chat") {
    return route.activeKey
      ? `#/chat/${encodeURIComponent(route.activeKey)}`
      : "#/new";
  }
  if (route.view === "automations") {
    const params = new URLSearchParams();
    if (route.activeKey) params.set("chat", route.activeKey);
    const query = params.toString();
    return `#/automations${query ? `?${query}` : ""}`;
  }
  const params = new URLSearchParams();
  if (route.activeKey) params.set("chat", route.activeKey);
  if (route.view === "settings" && route.settingsSection !== "overview") {
    params.set("section", route.settingsSection);
  }
  const query = params.toString();
  return `#/${route.view}${query ? `?${query}` : ""}`;
}

function writeShellRoute(route: ShellRoute, replace = false): void {
  if (typeof window === "undefined") return;
  const nextHash = shellRouteHash(route);
  if (window.location.hash === nextHash) return;
  if (replace) {
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${nextHash}`,
    );
    return;
  }
  window.location.hash = nextHash;
}

function bootstrapTokenExpiresAt(expiresInSeconds: number): number {
  return Date.now() + Math.max(0, expiresInSeconds) * 1000;
}

function tokenRefreshDelayMs(expiresAt: number): number {
  const remaining = Math.max(0, expiresAt - Date.now());
  const margin = Math.min(
    TOKEN_REFRESH_MARGIN_MS,
    Math.max(1_000, remaining / 2),
  );
  return Math.max(TOKEN_REFRESH_MIN_DELAY_MS, remaining - margin);
}

function readSidebarOpen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

function readCompletedRunChatIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(COMPLETED_RUNS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((item): item is string => typeof item === "string"));
  } catch {
    return new Set();
  }
}

function writeCompletedRunChatIds(chatIds: Set<string>): void {
  try {
    window.localStorage.setItem(
      COMPLETED_RUNS_STORAGE_KEY,
      JSON.stringify(Array.from(chatIds)),
    );
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

function normalizeWorkspaceScope(scope: WorkspaceScopePayload): WorkspaceScopePayload {
  const accessMode = scope.access_mode === "restricted" ? "restricted" : "full";
  return {
    ...scope,
    project_name: scope.project_name ?? projectNameFromPath(scope.project_path),
    access_mode: accessMode,
    restrict_to_workspace: accessMode === "restricted",
  };
}

export default function App() {
  const { t } = useTranslation();
  const [state, setState] = useState<BootState>({ status: "loading" });
  const [authBusy, setAuthBusy] = useState(false);

  const refreshReadyClient = useCallback(
    async (client: NanobotClient) => {
      const boot = await fetchBootstrap();
      const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
      const tokenExpiresAt = bootstrapTokenExpiresAt(boot.expires_in);
      client.updateUrl(url);
      setState((current) =>
        current.status === "ready" && current.client === client
          ? {
              ...current,
              token: boot.token,
              tokenExpiresAt,
              modelName: boot.model_name ?? current.modelName,
            }
          : current,
      );
      return { token: boot.token, url };
    },
    [],
  );

  const bootstrapAuthenticated = useCallback(
    (knownUser?: AuthUser) => {
      let cancelled = false;
      (async () => {
        setState({ status: "loading" });
        try {
          const user = knownUser ?? await fetchCurrentUser();
          const boot = await fetchBootstrap();
          if (cancelled) return;
          const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
          const client = new NanobotClient({
            url,
            onReauth: async () => {
              try {
                const refreshed = await refreshReadyClient(client);
                return refreshed.url;
              } catch {
                return null;
              }
            },
          });
          client.connect();
          setState({
            status: "ready",
            client,
            token: boot.token,
            tokenExpiresAt: bootstrapTokenExpiresAt(boot.expires_in),
            modelName: boot.model_name ?? null,
            user: boot.user ?? user,
          });
        } catch (e) {
          if (cancelled) return;
          if (e instanceof ApiError && e.status === 401) {
            const status = await fetchAuthStatus().catch(() => null);
            setState({ status: "auth", setup: status?.setup_required ?? false });
          } else {
            const msg = (e as Error).message;
            if (msg.includes("HTTP 401") || msg.includes("HTTP 403")) {
              const status = await fetchAuthStatus().catch(() => null);
              setState({ status: "auth", setup: status?.setup_required ?? false });
            } else {
              setState({ status: "error", message: msg });
            }
          }
        }
      })();
      return () => {
        cancelled = true;
      };
    },
    [refreshReadyClient],
  );

  useEffect(() => {
    if (state.status !== "ready") return;
    const client = state.client;
    const timer = window.setTimeout(async () => {
      try {
        await refreshReadyClient(client);
      } catch (e) {
        const msg = (e as Error).message;
        if (msg.includes("HTTP 401") || msg.includes("HTTP 403")) {
          setState({ status: "auth", setup: false, error: "登录已过期，请重新登录" });
        }
      }
    }, tokenRefreshDelayMs(state.tokenExpiresAt));
    return () => window.clearTimeout(timer);
  }, [refreshReadyClient, state]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const status = await fetchAuthStatus();
        if (cancelled) return;
        if (status.setup_required) {
          setState({ status: "auth", setup: true });
          return;
        }
        if (!status.authenticated || !status.user) {
          setState({ status: "auth", setup: false });
          return;
        }
        bootstrapAuthenticated(status.user);
      } catch (error) {
        if (!cancelled) setState({ status: "error", message: (error as Error).message });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bootstrapAuthenticated]);

  if (state.status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 animate-in fade-in-0 duration-300">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
            </span>
            {t("app.loading.connecting")}
          </div>
        </div>
      </div>
    );
  }
  if (state.status === "auth") {
    return (
      <AuthView
        setup={state.setup}
        error={state.error ?? null}
        busy={authBusy}
        onSubmit={({ username, displayName, password }) => {
          setAuthBusy(true);
          const action = state.setup
            ? setupAdmin({ username, display_name: displayName, password })
            : loginUser(username, password);
          action
            .then((user) => {
              bootstrapAuthenticated(user);
            })
            .catch((error) => {
              setState({
                status: "auth",
                setup: state.setup,
                error: (error as Error).message,
              });
            })
            .finally(() => setAuthBusy(false));
        }}
      />
    );
  }
  if (state.status === "error") {
    return (
      <div className="flex h-full w-full items-center justify-center px-4 text-center">
        <div className="flex max-w-md flex-col items-center gap-3">
          <p className="text-lg font-semibold">{t("app.error.title")}</p>
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <p className="text-xs text-muted-foreground">
            {t("app.error.gatewayHint")}
          </p>
        </div>
      </div>
    );
  }

  const handleModelNameChange = (modelName: string | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, modelName } : current,
    );
  };

  const handleLogout = () => {
    if (state.status === "ready") {
      state.client.close();
    }
    void logoutUser();
    setState({ status: "auth", setup: false });
  };

  return (
    <ClientProvider
      client={state.client}
      token={state.token}
      modelName={state.modelName}
      user={state.user}
    >
      <Shell
        onModelNameChange={handleModelNameChange}
        onLogout={handleLogout}
      />
    </ClientProvider>
  );
}

function Shell({
  onModelNameChange,
  onLogout,
}: {
  onModelNameChange: (modelName: string | null) => void;
  onLogout: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { client, token, user, hasPermission } = useClient();
  const { theme, toggle } = useTheme();
  const { sessions, loading, refresh, createChat, deleteChat } = useSessions();
  const { state: sidebarState, update: updateSidebarState } =
    useSidebarState(sessions, !loading);
  const initialRouteRef = useRef<ShellRoute | null>(null);
  if (!initialRouteRef.current) initialRouteRef.current = readShellRoute();
  const [activeKey, setActiveKey] = useState<string | null>(
    initialRouteRef.current.activeKey,
  );
  const [view, setView] = useState<ShellView>(initialRouteRef.current.view);
  const [settingsInitialSection, setSettingsInitialSection] =
    useState<SettingsSectionKey>(initialRouteRef.current.settingsSection);
  const [sidebarOpen, setSidebarOpen] =
    useState<boolean>(readSidebarOpen);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [sessionSearchOpen, setSessionSearchOpen] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const [pendingRename, setPendingRename] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const [pendingProjectRename, setPendingProjectRename] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const restartSawDisconnectRef = useRef(false);
  const [restartToast, setRestartToast] = useState<string | null>(null);
  const [isRestarting, setIsRestarting] = useState(false);
  const [runningChatIds, setRunningChatIds] = useState<Set<string>>(() => new Set());
  const [completedChatIds, setCompletedChatIds] = useState<Set<string>>(readCompletedRunChatIds);
  const [workspaces, setWorkspaces] = useState<WorkspacesPayload | null>(null);
  const [provisionalSession, setProvisionalSession] = useState<ChatSummary | null>(null);
  const [pendingActivationSession, setPendingActivationSession] =
    useState<ChatSummary | null>(null);
  const { skills, refresh: refreshSkills } = useSkills(token);
  const [settingsSnapshot, setSettingsSnapshot] = useState<SettingsPayload | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [composerDraft, setComposerDraft] = useState<{
    id: number;
    text: string;
  } | null>(null);
  const [draftWorkspaceScope, setDraftWorkspaceScope] =
    useState<WorkspaceScopePayload | null>(null);
  const [workspaceOverrides, setWorkspaceOverrides] =
    useState<Record<string, WorkspaceScopePayload>>({});
  const runningChatIdsRef = useRef<Set<string>>(new Set());
  const activeChatIdRef = useRef<string | null>(null);
  const showMainSidebar = view !== "settings";

  const navigate = useCallback(
    (route: ShellRoute, options?: { replace?: boolean }) => {
      setActiveKey(route.activeKey);
      setView(route.view);
      setSettingsInitialSection(route.settingsSection);
      writeShellRoute(route, options?.replace);
    },
    [],
  );

  useEffect(() => {
    const applyRoute = () => {
      const route = readShellRoute();
      setActiveKey(route.activeKey);
      setView(route.view);
      setSettingsInitialSection(route.settingsSection);
      setWorkspaceError(null);
      if (route.view === "chat" && !route.activeKey) {
        setDraftWorkspaceScope(null);
      }
    };
    window.addEventListener("hashchange", applyRoute);
    return () => window.removeEventListener("hashchange", applyRoute);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) setSettingsSnapshot(payload);
      })
      .catch(() => {
        if (!cancelled) setSettingsSnapshot(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        sidebarOpen ? "1" : "0",
      );
    } catch {
      // ignore storage errors (private mode, etc.)
    }
  }, [sidebarOpen]);

  useEffect(() => {
    writeCompletedRunChatIds(completedChatIds);
  }, [completedChatIds]);

  const visibleActiveKey =
    activeKey ?? (view === "chat" ? pendingActivationSession?.key ?? null : null);
  const activeSession = useMemo<ChatSummary | null>(() => {
    if (!visibleActiveKey) return null;
    return (
      sessions.find((s) => s.key === visibleActiveKey)
      ?? (provisionalSession?.key === visibleActiveKey ? provisionalSession : null)
      ?? (pendingActivationSession?.key === visibleActiveKey ? pendingActivationSession : null)
    );
  }, [sessions, visibleActiveKey, provisionalSession, pendingActivationSession]);
  const runningChatIdList = useMemo(() => Array.from(runningChatIds), [runningChatIds]);
  const completedChatIdList = useMemo(() => Array.from(completedChatIds), [completedChatIds]);
  const activeChatId = activeSession?.chatId ?? null;
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
    if (!activeChatId) return;
    setCompletedChatIds((current) => {
      if (!current.has(activeChatId)) return current;
      const next = new Set(current);
      next.delete(activeChatId);
      return next;
    });
  }, [activeChatId]);
  const activeWorkspaceScope = useMemo<WorkspaceScopePayload | null>(() => {
    if (activeChatId && workspaceOverrides[activeChatId]) {
      return workspaceOverrides[activeChatId];
    }
    if (activeSession?.workspaceScope) {
      return activeSession.workspaceScope;
    }
    return draftWorkspaceScope ?? workspaces?.default_scope ?? null;
  }, [
    activeChatId,
    activeSession?.workspaceScope,
    draftWorkspaceScope,
    workspaceOverrides,
    workspaces?.default_scope,
  ]);
  const activeChatRunning = activeChatId ? runningChatIds.has(activeChatId) : false;

  const refreshWorkspaces = useCallback(async () => {
    try {
      const payload = await fetchWorkspaces(token);
      setWorkspaces(payload);
    } catch {
      setWorkspaces(null);
    }
  }, [token]);

  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);

  useEffect(() => {
    if (loading) return;
    const knownChatIds = new Set(sessions.map((session) => session.chatId));
    setCompletedChatIds((current) => {
      const next = new Set(
        Array.from(current).filter((chatId) => knownChatIds.has(chatId)),
      );
      return next.size === current.size ? current : next;
    });
    setWorkspaceOverrides((current) => {
      const entries = Object.entries(current).filter(([chatId]) => knownChatIds.has(chatId));
      return entries.length === Object.keys(current).length ? current : Object.fromEntries(entries);
    });
  }, [loading, sessions]);

  useEffect(() => {
    if (loading || !activeKey) return;
    if (sessions.some((session) => session.key === activeKey)) return;
    if (provisionalSession?.key === activeKey) return;
    const currentRoute = readShellRoute();
    navigate(
      currentRoute.view === "chat"
        ? defaultShellRoute()
        : {
            ...currentRoute,
            activeKey: null,
          },
      { replace: true },
    );
  }, [activeKey, loading, navigate, provisionalSession?.key, sessions]);

  useEffect(() => {
    if (!provisionalSession) return;
    if (sessions.some((session) => session.key === provisionalSession.key)) {
      setProvisionalSession(null);
      return;
    }
    if (activeKey && activeKey !== provisionalSession.key) {
      setProvisionalSession(null);
    }
  }, [activeKey, provisionalSession, sessions]);

  useEffect(() => {
    if (!pendingActivationSession) return;
    const key = pendingActivationSession.key;
    if (view !== "chat" || activeKey !== key) {
      navigate({
        view: "chat",
        activeKey: key,
        settingsSection: "overview",
      }, { replace: true });
      return;
    }
    if (sessions.some((session) => session.key === key)) {
      setPendingActivationSession(null);
    }
  }, [activeKey, navigate, pendingActivationSession, sessions, view]);

  useEffect(() => {
    return client.onSessionUpdate((_chatId, _scope, workspaceScope) => {
      if (!workspaceScope) return;
      const next = normalizeWorkspaceScope(workspaceScope);
      setWorkspaceOverrides((current) => ({
        ...current,
        [_chatId]: next,
      }));
      setDraftWorkspaceScope(next);
      setWorkspaceError(null);
      void refreshWorkspaces();
    });
  }, [client, refreshWorkspaces]);

  useEffect(() => {
    return client.onError((error) => {
      if (error.kind !== "workspace_scope_rejected") return;
      setWorkspaceError(t("errors.workspaceScopeRejected.body"));
      void refreshWorkspaces();
    });
  }, [client, refreshWorkspaces, t]);

  useEffect(() => {
    if (loading) return;
    const activeRunIds = sessions
      .filter((session) => typeof session.runStartedAt === "number")
      .map((session) => session.chatId);
    if (activeRunIds.length === 0) return;

    for (const chatId of activeRunIds) {
      client.attach(chatId);
    }
    setRunningChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (!next.has(chatId)) changed = true;
        next.add(chatId);
      }
      if (!changed) return current;
      runningChatIdsRef.current = next;
      return next;
    });
    setCompletedChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (next.delete(chatId)) changed = true;
      }
      return changed ? next : current;
    });
  }, [client, loading, sessions]);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
  }, []);

  const toggleSidebar = useCallback(() => {
    const isDesktop =
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 1024px)").matches;
    if (isDesktop) {
      setSidebarOpen((v) => !v);
    } else {
      setMobileSidebarOpen((v) => !v);
    }
  }, []);

  const applyWorkspaceScope = useCallback(
    (scope: WorkspaceScopePayload) => {
      const next = normalizeWorkspaceScope(scope);
      setWorkspaceError(null);
      if (activeChatId) {
        if (!activeChatRunning) {
          client.setWorkspaceScope(activeChatId, next);
        }
        return;
      }
      setDraftWorkspaceScope(next);
    },
    [activeChatId, activeChatRunning, client],
  );

  const onCreateChat = useCallback(async (workspaceScope?: WorkspaceScopePayload | null) => {
    try {
      const scope = workspaceScope ?? activeWorkspaceScope;
      const chatId = await createChat(scope);
      const key = `websocket:${chatId}`;
      const now = new Date().toISOString();
      const createdSession: ChatSummary = {
        key,
        channel: "websocket",
        chatId,
        createdAt: now,
        updatedAt: now,
        title: "",
        preview: "",
        workspaceScope: scope ?? null,
      };
      setProvisionalSession(createdSession);
      setPendingActivationSession(createdSession);
      navigate({
        view: "chat",
        activeKey: key,
        settingsSection: "overview",
      });
      setMobileSidebarOpen(false);
      if (scope) {
        setWorkspaceOverrides((current) => ({
          ...current,
          [chatId]: normalizeWorkspaceScope(scope),
        }));
      }
      return chatId;
    } catch (e) {
      console.error("Failed to create chat", e);
      if (e instanceof Error && e.message.startsWith("workspace_scope_rejected:")) {
        setWorkspaceError(t("errors.workspaceScopeRejected.body"));
      }
      return null;
    }
  }, [activeWorkspaceScope, createChat, navigate, t]);

  const onNewChat = useCallback(() => {
    setProvisionalSession(null);
    setPendingActivationSession(null);
    navigate(defaultShellRoute());
    setDraftWorkspaceScope(null);
    setWorkspaceError(null);
    setSessionSearchOpen(false);
    setMobileSidebarOpen(false);
  }, [navigate]);

  const onNewChatInProject = useCallback(
    (projectPath: string, projectName: string) => {
      const base = workspaces?.default_scope ?? activeWorkspaceScope;
      const workspaceRoot = workspaces?.workspace_root ?? workspaces?.default_scope.project_path ?? base?.project_path ?? "";
      const trimmed = projectPath.trim();
      if (!base || !trimmed) {
        onNewChat();
        return;
      }
      const resolvedPath = resolveWorkspaceProjectPath(workspaceRoot || base.project_path, trimmed);
      setPendingActivationSession(null);
      navigate(defaultShellRoute());
      setDraftWorkspaceScope(normalizeWorkspaceScope({
        project_path: resolvedPath,
        project_name: projectName || projectNameFromPath(resolvedPath),
        access_mode: base.access_mode,
        restrict_to_workspace: base.access_mode === "restricted",
      }));
      setWorkspaceError(null);
      setMobileSidebarOpen(false);
    },
    [activeWorkspaceScope, navigate, onNewChat, workspaces?.default_scope, workspaces?.workspace_root],
  );

  const onSelectChat = useCallback(
    (key: string) => {
      const selected = sessions.find((session) => session.key === key);
      const selectedChatId = selected?.chatId;
      if (selectedChatId) {
        setCompletedChatIds((current) => {
          if (!current.has(selectedChatId)) return current;
          const next = new Set(current);
          next.delete(selectedChatId);
          return next;
        });
      }
      if (selected?.workspaceScope) {
        setDraftWorkspaceScope(normalizeWorkspaceScope(selected.workspaceScope));
      } else {
        setDraftWorkspaceScope(null);
      }
      setWorkspaceError(null);
      setPendingActivationSession(null);
      const nextView = view === "automations" && key !== activeKey ? "automations" : "chat";
      navigate({ view: nextView, activeKey: key, settingsSection: "overview" });
      setMobileSidebarOpen(false);
    },
    [activeKey, navigate, sessions, view],
  );

  const onTogglePin = useCallback(
    (key: string) => {
      void updateSidebarState((current) => {
        const pinned = new Set(current.pinned_keys);
        if (pinned.has(key)) {
          pinned.delete(key);
        } else {
          pinned.add(key);
        }
        return {
          ...current,
          pinned_keys: Array.from(pinned),
        };
      });
    },
    [updateSidebarState],
  );

  const onRequestRename = useCallback((key: string, label: string) => {
    setPendingRename({ key, label });
  }, []);

  const onConfirmRename = useCallback(
    (title: string) => {
      if (!pendingRename) return;
      const key = pendingRename.key;
      setPendingRename(null);
      void updateSidebarState((current) => {
        const titleOverrides = { ...current.title_overrides };
        const cleaned = title.trim();
        if (cleaned) {
          titleOverrides[key] = cleaned;
        } else {
          delete titleOverrides[key];
        }
        return {
          ...current,
          title_overrides: titleOverrides,
        };
      });
    },
    [pendingRename, updateSidebarState],
  );

  const onToggleGroup = useCallback(
    (groupId: string) => {
      void updateSidebarState((current) => {
        const collapsedGroups = { ...current.collapsed_groups };
        if (groupId === "workspace:chats" || groupId === "date:all") {
          if (collapsedGroups[groupId] === false) {
            delete collapsedGroups[groupId];
          } else {
            collapsedGroups[groupId] = false;
          }
          return {
            ...current,
            collapsed_groups: collapsedGroups,
          };
        }
        if (collapsedGroups[groupId]) {
          delete collapsedGroups[groupId];
        } else {
          collapsedGroups[groupId] = true;
        }
        return {
          ...current,
          collapsed_groups: collapsedGroups,
        };
      });
    },
    [updateSidebarState],
  );

  const onRequestRenameProject = useCallback((key: string, label: string) => {
    setPendingProjectRename({ key, label });
  }, []);

  const onConfirmProjectRename = useCallback(
    (title: string) => {
      if (!pendingProjectRename) return;
      const key = pendingProjectRename.key;
      setPendingProjectRename(null);
      void updateSidebarState((current) => {
        const projectNameOverrides = { ...current.project_name_overrides };
        const cleaned = title.trim();
        if (cleaned) {
          projectNameOverrides[key] = cleaned;
        } else {
          delete projectNameOverrides[key];
        }
        return {
          ...current,
          project_name_overrides: projectNameOverrides,
        };
      });
    },
    [pendingProjectRename, updateSidebarState],
  );

  const onToggleArchive = useCallback(
    (key: string) => {
      void updateSidebarState((current) => {
        const archived = new Set(current.archived_keys);
        const pinned = current.pinned_keys.filter((item) => item !== key);
        if (archived.has(key)) {
          archived.delete(key);
        } else {
          archived.add(key);
        }
        return {
          ...current,
          pinned_keys: pinned,
          archived_keys: Array.from(archived),
        };
      });
      if (activeKey === key && !sidebarState.archived_keys.includes(key)) {
        const archived = new Set([...sidebarState.archived_keys, key]);
        const next = sessions.find((session) => !archived.has(session.key));
        navigate({
          view: "chat",
          activeKey: next?.key ?? null,
          settingsSection: "overview",
        });
      }
    },
    [activeKey, navigate, sessions, sidebarState.archived_keys, updateSidebarState],
  );

  const onToggleArchived = useCallback(() => {
    void updateSidebarState((current) => ({
      ...current,
      view: {
        ...current.view,
        show_archived: !current.view.show_archived,
      },
    }));
  }, [updateSidebarState]);

  const onOpenSessionSearch = useCallback(() => {
    setMobileSidebarOpen(false);
    setSessionSearchOpen(true);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const commandShiftO =
        (event.metaKey || event.ctrlKey) && event.shiftKey && !event.altKey;
      if (commandShiftO && event.key.toLowerCase() === "o") {
        event.preventDefault();
        onNewChat();
        return;
      }
      const plainCommandK =
        (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey;
      if (!plainCommandK) return;
      if (event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      onOpenSessionSearch();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onNewChat, onOpenSessionSearch]);

  const onSelectSearchResult = useCallback(
    (key: string) => {
      setSessionSearchOpen(false);
      onSelectChat(key);
    },
    [onSelectChat],
  );

  const onOpenSettings = useCallback((section: SettingsSectionKey = "overview") => {
    if (!hasPermission("settings.manage")) {
      navigate({ view: "security", activeKey, settingsSection: "security" });
      return;
    }
    setSessionSearchOpen(false);
    navigate({ view: "settings", activeKey, settingsSection: section });
    setMobileSidebarOpen(false);
  }, [activeKey, hasPermission, navigate]);

  const onOpenModelSettings = useCallback(() => {
    onOpenSettings("models");
  }, [onOpenSettings]);

  const onOpenApps = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "apps", activeKey, settingsSection: "apps" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onOpenAutomations = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "automations", activeKey, settingsSection: "overview" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onCreateAutomationWithChat = useCallback(() => {
    const nextKey =
      activeKey && sessions.some((session) => session.key === activeKey)
        ? activeKey
        : null;
    setSessionSearchOpen(false);
    navigate({ view: "chat", activeKey: nextKey, settingsSection: "overview" });
    setComposerDraft({ id: Date.now(), text: AUTOMATION_CHAT_PROMPT });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate, sessions]);

  const onOpenSkills = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "skills", activeKey, settingsSection: "skills" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onOpenSecurity = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "security", activeKey, settingsSection: "security" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onOpenGuardrails = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "guardrails", activeKey, settingsSection: "guardrails" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onOpenSandbox = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "sandbox", activeKey, settingsSection: "sandbox" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  const onOpenAudit = useCallback(() => {
    setSessionSearchOpen(false);
    navigate({ view: "audit", activeKey, settingsSection: "overview" });
    setMobileSidebarOpen(false);
  }, [activeKey, navigate]);

  useEffect(() => {
    const denied =
      (view === "settings" && !hasPermission("settings.manage"));
    if (!denied) return;
    navigate(
      { view: "security", activeKey, settingsSection: "security" },
      { replace: true },
    );
  }, [activeKey, hasPermission, navigate, view]);

  const onSettingsSectionChange = useCallback(
    (section: SettingsSectionKey) => {
      navigate({
        view: shellViewForSettingsSection(section),
        activeKey,
        settingsSection: section,
      });
    },
    [activeKey, navigate],
  );

  const onBackToChat = useCallback(() => {
    setMobileSidebarOpen(false);
    const nextKey = (() => {
      if (!activeKey) return null;
      if (sessions.some((session) => session.key === activeKey)) return activeKey;
      return sessions[0]?.key ?? null;
    })();
    navigate({
      view: "chat",
      activeKey: nextKey,
      settingsSection: "overview",
    });
  }, [activeKey, navigate, sessions]);

  const onRestart = useCallback(() => {
    restartSawDisconnectRef.current = false;
    setIsRestarting(true);
    try {
      window.localStorage.setItem(RESTART_STARTED_KEY, String(Date.now()));
    } catch {
      // ignore storage errors
    }
    restartRuntime(token).catch(() => {
      setIsRestarting(false);
    });
  }, [token]);

  useEffect(() => {
    return client.onRuntimeModelUpdate((modelName) => {
      onModelNameChange(modelName);
    });
  }, [client, onModelNameChange]);

  useEffect(() => {
    return client.onRunStatus((chatId, startedAt) => {
      if (startedAt != null) {
        const nextRunning = new Set(runningChatIdsRef.current);
        nextRunning.add(chatId);
        runningChatIdsRef.current = nextRunning;
        setRunningChatIds(nextRunning);
        setCompletedChatIds((current) => {
          if (!current.has(chatId)) return current;
          const next = new Set(current);
          next.delete(chatId);
          return next;
        });
        return;
      }

      if (!runningChatIdsRef.current.has(chatId)) return;
      const nextRunning = new Set(runningChatIdsRef.current);
      nextRunning.delete(chatId);
      runningChatIdsRef.current = nextRunning;
      setRunningChatIds(nextRunning);
      setCompletedChatIds((current) => {
        const next = new Set(current);
        if (activeChatIdRef.current === chatId) {
          next.delete(chatId);
        } else {
          next.add(chatId);
        }
        return next;
      });
    });
  }, [client]);

  useEffect(() => {
    return client.onStatus((status) => {
      const startedAt = (() => {
        try {
          return Number(window.localStorage.getItem(RESTART_STARTED_KEY) ?? "0");
        } catch {
          return 0;
        }
      })();
      if (!startedAt) return;
      if (status !== "open") {
        restartSawDisconnectRef.current = true;
        return;
      }
      const elapsedMs = Date.now() - startedAt;
      if (!restartSawDisconnectRef.current && elapsedMs < 1500) return;
      try {
        window.localStorage.removeItem(RESTART_STARTED_KEY);
      } catch {
        // ignore storage errors
      }
      setIsRestarting(false);
      setRestartToast(t("app.restart.completed", { seconds: (elapsedMs / 1000).toFixed(1) }));
      window.setTimeout(() => setRestartToast(null), 3_500);
    });
  }, [client, t]);

  const onTurnEnd = useDeferredTitleRefresh(activeSession, refresh);

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const key = pendingDelete.key;
    const deletingActive = activeKey === key;
    const currentIndex = sessions.findIndex((s) => s.key === key);
    const fallbackKey = deletingActive
      ? (sessions[currentIndex + 1]?.key ?? sessions[currentIndex - 1]?.key ?? null)
      : activeKey;
    setPendingDelete(null);
    if (deletingActive) {
      navigate({
        view: "chat",
        activeKey: fallbackKey,
        settingsSection: "overview",
      }, { replace: true });
    }
    try {
      await deleteChat(key);
    } catch (e) {
      if (deletingActive) {
        navigate({
          view: "chat",
          activeKey: key,
          settingsSection: "overview",
        }, { replace: true });
      }
      console.error("Failed to delete session", e);
    }
  }, [pendingDelete, deleteChat, activeKey, navigate, sessions]);

  const headerTitle = activeSession
    ? sidebarState.title_overrides[activeSession.key] ||
      activeSession.title ||
      deriveTitle(activeSession.preview, t("chat.newChat"))
    : t("app.brand");

  useEffect(() => {
    if (view === "settings") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.sidebar.title"),
      });
      return;
    }
    if (view === "apps") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.nav.apps", { defaultValue: "Tools" }),
      });
      return;
    }
    if (view === "automations") {
      document.title = t("app.documentTitle.chat", {
        title: t("automations.title", { defaultValue: "自动运维" }),
      });
      return;
    }
    if (view === "skills") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.nav.skills", { defaultValue: "Skills" }),
      });
      return;
    }
    if (view === "security") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.nav.security", { defaultValue: "OpsShield" }),
      });
      return;
    }
    if (view === "guardrails") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.nav.guardrails", { defaultValue: "Guardrails" }),
      });
      return;
    }
    if (view === "sandbox") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.nav.sandbox", { defaultValue: "沙箱" }),
      });
      return;
    }
    if (view === "audit") {
      document.title = t("app.documentTitle.chat", {
        title: "审计中心",
      });
      return;
    }
    document.title = activeSession
      ? t("app.documentTitle.chat", { title: headerTitle })
      : t("app.documentTitle.base");
  }, [activeSession, headerTitle, i18n.resolvedLanguage, t, view]);

  const sidebarProps = {
    sessions,
    activeKey: visibleActiveKey,
    loading,
    onNewChat,
    onSelect: onSelectChat,
    onRequestDelete: (key: string, label: string) =>
      setPendingDelete({ key, label }),
    onTogglePin,
    onRequestRename,
    onToggleArchive,
    onToggleGroup,
    onRequestRenameProject,
    onNewChatInProject,
    onOpenSettings,
    onOpenApps,
    onOpenAutomations,
    onOpenSkills,
    onOpenSecurity,
    onOpenGuardrails,
    onOpenSandbox,
    onOpenAudit,
    onOpenSearch: onOpenSessionSearch,
    user,
    onLogout,
    onChangePassword: () => setPasswordDialogOpen(true),
    showApps: true,
    showSkills: true,
    showSettings: hasPermission("settings.manage"),
    activeUtility:
      view === "apps"
        || view === "automations"
        || view === "skills"
        || view === "security"
        || view === "guardrails"
        || view === "sandbox"
        || view === "audit"
        ? view
        : null,
    onToggleArchived,
    pinnedKeys: sidebarState.pinned_keys,
    archivedKeys: sidebarState.archived_keys,
    titleOverrides: sidebarState.title_overrides,
    projectNameOverrides: sidebarState.project_name_overrides,
    collapsedGroups: sidebarState.collapsed_groups,
    runningChatIds: runningChatIdList,
    completedChatIds: completedChatIdList,
    viewState: sidebarState.view,
    showArchived: sidebarState.view.show_archived,
    archivedCount: sidebarState.archived_keys.length,
    defaultWorkspacePath: workspaces?.workspace_root ?? workspaces?.default_scope.project_path ?? null,
  };
  const sidebarFlowWidth = sidebarOpen ? SIDEBAR_WIDTH : SIDEBAR_RAIL_WIDTH;

  return (
    <ThemeProvider theme={theme}>
      <div className="relative h-full w-full overflow-hidden">
        <div className="relative flex h-full w-full overflow-hidden">
          {showMainSidebar ? (
            <aside
              className={cn(
                "relative z-20 hidden shrink-0 overflow-hidden transition-[width] duration-300 ease-out lg:block",
              )}
              style={{ width: sidebarFlowWidth }}
            >
              <div className="h-full w-full overflow-hidden bg-sidebar shadow-inner-right">
                <Sidebar
                  {...sidebarProps}
                  collapsed={!sidebarOpen}
                  onCollapse={() => setSidebarOpen(false)}
                  onExpand={() => setSidebarOpen(true)}
                />
              </div>
            </aside>
          ) : null}

          {showMainSidebar ? (
            <Sheet
              open={mobileSidebarOpen}
              onOpenChange={(open) => setMobileSidebarOpen(open)}
            >
              <SheetContent
                side="left"
                showCloseButton={false}
                aria-describedby={undefined}
                className="p-0 lg:hidden"
                style={{ width: SIDEBAR_WIDTH, maxWidth: SIDEBAR_WIDTH }}
              >
                <SheetTitle className="sr-only">{t("sidebar.navigation")}</SheetTitle>
                <Sidebar
                  {...sidebarProps}
                  onCollapse={closeMobileSidebar}
                  containActionMenus
                />
              </SheetContent>
            </Sheet>
          ) : null}

          <SessionSearchDialog
            open={sessionSearchOpen}
            onOpenChange={setSessionSearchOpen}
            sessions={sessions}
            activeKey={visibleActiveKey}
            loading={loading}
            titleOverrides={sidebarState.title_overrides}
            onSelect={onSelectSearchResult}
          />

          <main className="relative flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-background">
            <div
              className={cn(
                "absolute inset-0 flex flex-col",
                view !== "chat" && "invisible pointer-events-none",
              )}
            >
              <ThreadShell
                session={activeSession}
                title={headerTitle}
                onToggleSidebar={toggleSidebar}
                onNewChat={onNewChat}
                onCreateChat={onCreateChat}
                onTurnEnd={onTurnEnd}
                theme={theme}
                onToggleTheme={toggle}
                hideHeader={false}
                workspaceScope={activeWorkspaceScope}
                workspaceDefaultScope={workspaces?.default_scope ?? null}
                workspaceControls={workspaces?.controls ?? null}
                workspaceScopeDisabled={activeChatRunning}
                workspaceError={workspaceError}
                onWorkspaceScopeChange={applyWorkspaceScope}
                settingsSnapshot={settingsSnapshot}
                composerDraft={composerDraft}
                onComposerDraftApplied={() => setComposerDraft(null)}
                onOpenModelSettings={
                  hasPermission("settings.manage") ? onOpenModelSettings : undefined
                }
              />
            </div>
            {view === "automations" ? (
              <div className="absolute inset-0 flex flex-col">
                <AutomationsView
                  session={activeSession}
                  title={headerTitle}
                  token={token}
                  onCreateWithChat={onCreateAutomationWithChat}
                />
              </div>
            ) : null}
            {view === "audit" ? (
              <div className="absolute inset-0 flex flex-col">
                <AuditCenter token={token} onToggleSidebar={toggleSidebar} />
              </div>
            ) : null}
            {view !== "chat" && view !== "automations" && view !== "audit" && (
              <div className="absolute inset-0 flex flex-col">
                <SettingsView
                  initialSection={settingsInitialSection}
                  initialSettings={settingsSnapshot}
                  showSidebar={view === "settings"}
                  onBackToChat={onBackToChat}
                  onModelNameChange={onModelNameChange}
                  onSettingsChange={setSettingsSnapshot}
                  skills={skills}
                  onSkillsChange={refreshSkills}
                  onWorkspaceSettingsChange={refreshWorkspaces}
                  onSectionChange={onSettingsSectionChange}
                  onLogout={onLogout}
                  onRestart={onRestart}
                  isRestarting={isRestarting}
                />
              </div>
            )}
          </main>
        </div>

        <DeleteConfirm
          open={!!pendingDelete}
          title={pendingDelete?.label ?? ""}
          onCancel={() => setPendingDelete(null)}
          onConfirm={onConfirmDelete}
        />
        <RenameChatDialog
          open={!!pendingRename}
          title={pendingRename?.label ?? ""}
          onCancel={() => setPendingRename(null)}
          onConfirm={onConfirmRename}
        />
        <PasswordDialog
          open={passwordDialogOpen}
          onOpenChange={setPasswordDialogOpen}
        />
        <RenameChatDialog
          open={!!pendingProjectRename}
          title={pendingProjectRename?.label ?? ""}
          dialogTitle={t("chat.renameProjectTitle")}
          description={t("chat.renameProjectDescription")}
          placeholder={t("chat.renameProjectPlaceholder")}
          onCancel={() => setPendingProjectRename(null)}
          onConfirm={onConfirmProjectRename}
        />
        {restartToast ? (
          <div
            role="status"
            className="fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full border border-border/70 bg-popover px-4 py-2 text-sm font-medium text-popover-foreground shadow-lg"
          >
            {restartToast}
          </div>
        ) : null}
      </div>
    </ThemeProvider>
  );
}
