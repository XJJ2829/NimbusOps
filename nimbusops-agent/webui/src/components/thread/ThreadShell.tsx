import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { WifiOff } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { PromptNavigator } from "@/components/thread/PromptNavigator";
import { SessionInfoPopover } from "@/components/thread/SessionInfoPopover";
import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { ThreadHeader } from "@/components/thread/ThreadHeader";
import { StreamErrorNotice } from "@/components/thread/StreamErrorNotice";
import { SecurityInspectionDialog } from "@/components/thread/SecurityInspectionDialog";
import { ActionGuardrailsBudgetDialog } from "@/components/thread/ActionGuardrailsBudgetDialog";
import { PrivilegedActionDialog } from "@/components/thread/PrivilegedActionDialog";
import { ThreadViewport, type ThreadViewportHandle } from "@/components/thread/ThreadViewport";
import { useNanobotStream, type SendImage, type SendOptions } from "@/hooks/useNanobotStream";
import { useSessionHistory } from "@/hooks/useSessions";
import { fetchCliApps, fetchMcpPresets, fetchSettings, listSlashCommands } from "@/lib/api";
import {
  CLI_APPS_CHANGED_EVENT,
  installedCliAppsFromPayload,
  isCliAppsPayload,
} from "@/lib/cli-app-events";
import {
  MCP_PRESETS_CHANGED_EVENT,
  installedMcpPresetsFromPayload,
  isMcpPresetsPayload,
} from "@/lib/mcp-preset-events";
import { inferProviderFromModelName, providerDisplayLabel } from "@/lib/provider-brand";
import type {
  ChatSummary,
  ConnectionStatus,
  SettingsPayload,
  SlashCommand,
  UIMessage,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";
import { normalizeLegacyLongTaskMessages } from "@/lib/thread-display-compat";
import { scrubSubagentUiMessages } from "@/lib/subagent-channel-display";

function projectWebuiThreadMessages(messages: UIMessage[]): UIMessage[] {
  return scrubSubagentUiMessages(normalizeLegacyLongTaskMessages(messages));
}

function isMessageSnapshotPrefix(current: UIMessage, snapshot: UIMessage): boolean {
  if (current.role !== snapshot.role || (current.kind ?? "") !== (snapshot.kind ?? "")) {
    return false;
  }
  if (!current.content.startsWith(snapshot.content)) return false;
  const currentReasoning = current.reasoning ?? "";
  const snapshotReasoning = snapshot.reasoning ?? "";
  return currentReasoning.startsWith(snapshotReasoning);
}

function isStaleThreadSnapshot(current: UIMessage[], snapshot: UIMessage[]): boolean {
  if (current.length === 0 || snapshot.length > current.length) return false;
  if (snapshot.length === 0) return true;
  let regressed = snapshot.length < current.length;
  for (let index = 0; index < snapshot.length; index += 1) {
    const live = current[index];
    const saved = snapshot[index];
    if (!isMessageSnapshotPrefix(live, saved)) return false;
    if (
      live.content.length > saved.content.length
      || (live.reasoning ?? "").length > (saved.reasoning ?? "").length
      || (!live.isStreaming && !!saved.isStreaming)
    ) {
      regressed = true;
    }
  }
  return regressed;
}

const FILE_PREVIEW_DEFAULT_WIDTH = 544;
const FILE_PREVIEW_MIN_WIDTH = 360;
const FILE_PREVIEW_MAX_WIDTH = 860;
const FILE_PREVIEW_MIN_MAIN_WIDTH = 420;
const FILE_PREVIEW_CLOSE_ANIMATION_MS = 320;

function clampFilePreviewWidth(width: number, maxWidth: number): number {
  return Math.min(Math.max(width, FILE_PREVIEW_MIN_WIDTH), maxWidth);
}

function maxFilePreviewWidth(containerWidth: number): number {
  return Math.max(
    FILE_PREVIEW_MIN_WIDTH,
    Math.min(FILE_PREVIEW_MAX_WIDTH, containerWidth - FILE_PREVIEW_MIN_MAIN_WIDTH),
  );
}

interface ThreadShellProps {
  session: ChatSummary | null;
  title: string;
  onToggleSidebar: () => void;
  onGoHome?: () => void;
  onNewChat?: () => void;
  onCreateChat?: (workspaceScope?: WorkspaceScopePayload | null) => Promise<string | null>;
  onTurnEnd?: () => void;
  theme?: "light" | "dark";
  onToggleTheme?: () => void;
  hideThemeButton?: boolean;
  hideHeader?: boolean;
  workspaceScope?: WorkspaceScopePayload | null;
  workspaceDefaultScope?: WorkspaceScopePayload | null;
  workspaceControls?: WorkspacesPayload["controls"] | null;
  workspaceScopeDisabled?: boolean;
  workspaceError?: string | null;
  onWorkspaceScopeChange?: (scope: WorkspaceScopePayload) => void;
  settingsSnapshot?: SettingsPayload | null;
  composerDraft?: { id: number; text: string } | null;
  onComposerDraftApplied?: () => void;
  onOpenModelSettings?: () => void;
}

function toModelBadgeLabel(modelName: string | null): string | null {
  if (!modelName) return null;
  const trimmed = modelName.trim();
  if (!trimmed) return null;
  const leaf = trimmed.split("/").pop() ?? trimmed;
  return leaf || trimmed;
}

interface ModelBadgeInfo {
  label: string | null;
  provider: string | null;
  providerLabel: string | null;
  needsSetup: boolean;
}

function activeModelPreset(settings: SettingsPayload | null): SettingsPayload["model_presets"][number] | null {
  if (!settings) return null;
  const configured = settings.agent.model_preset || "default";
  return (
    settings.model_presets.find((preset) => preset.name === configured)
    ?? settings.model_presets.find((preset) => preset.active)
    ?? null
  );
}

function resolvedModelProvider(settings: SettingsPayload | null, modelName: string | null): string | null {
  const preset = activeModelPreset(settings);
  const rawProvider = preset?.provider || settings?.agent.provider || null;
  if (rawProvider === "auto") {
    return settings?.agent.resolved_provider || inferProviderFromModelName(modelName) || null;
  }
  return rawProvider || inferProviderFromModelName(modelName);
}

function toModelBadgeInfo(modelName: string | null, settings: SettingsPayload | null): ModelBadgeInfo {
  const model = modelName || settings?.agent.model || null;
  const label = toModelBadgeLabel(model);
  const provider = resolvedModelProvider(settings, model);
  const providerRow = provider
    ? settings?.providers.find((item) => item.name === provider)
    : null;
  const needsSetup = Boolean(
    settings && (!model || !provider || !providerRow || !providerRow.configured),
  );
  return {
    label,
    provider,
    providerLabel: provider ? providerDisplayLabel(settings?.providers ?? [], provider) : null,
    needsSetup,
  };
}

const HERO_GREETING_KEYS = [
  "thread.empty.greetings.workOn",
  "thread.empty.greetings.start",
  "thread.empty.greetings.build",
  "thread.empty.greetings.tackle",
] as const;

function randomHeroGreetingKey(): (typeof HERO_GREETING_KEYS)[number] {
  const index = Math.floor(Math.random() * HERO_GREETING_KEYS.length);
  return HERO_GREETING_KEYS[index] ?? HERO_GREETING_KEYS[0];
}

interface PendingFirstMessage {
  content: string;
  images?: SendImage[];
  options?: SendOptions;
}

interface InstalledSettingItemsOptions<Payload, Item> {
  token: string;
  eventName: string;
  fetchPayload: (token: string) => Promise<Payload>;
  isPayload: (value: unknown) => value is Payload;
  selectItems: (payload: Payload) => Item[];
}

function useInstalledSettingItems<Payload, Item>({
  token,
  eventName,
  fetchPayload,
  isPayload,
  selectItems,
}: InstalledSettingItemsOptions<Payload, Item>): Item[] {
  const [items, setItems] = useState<Item[]>([]);

  const refresh = useCallback(async (isCancelled?: () => boolean) => {
    try {
      const payload = await fetchPayload(token);
      if (!isCancelled?.()) setItems(selectItems(payload));
    } catch {
      if (!isCancelled?.()) setItems([]);
    }
  }, [fetchPayload, selectItems, token]);

  useEffect(() => {
    let cancelled = false;
    void refresh(() => cancelled);

    const refreshOnFocus = () => {
      if (document.visibilityState === "hidden") return;
      void refresh();
    };
    const refreshOnChanged = (event: Event) => {
      const payload = (event as CustomEvent<unknown>).detail;
      if (isPayload(payload)) {
        setItems(selectItems(payload));
        return;
      }
      void refresh();
    };

    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    window.addEventListener(eventName, refreshOnChanged);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
      window.removeEventListener(eventName, refreshOnChanged);
    };
  }, [eventName, isPayload, refresh, selectItems]);

  return items;
}

export function ThreadShell({
  session,
  title,
  onToggleSidebar,
  onCreateChat,
  onTurnEnd,
  theme = "light",
  onToggleTheme = () => {},
  hideThemeButton = false,
  hideHeader = false,
  workspaceScope = null,
  workspaceDefaultScope = null,
  workspaceControls = null,
  workspaceScopeDisabled = false,
  workspaceError = null,
  onWorkspaceScopeChange,
  settingsSnapshot = null,
  composerDraft = null,
  onComposerDraftApplied,
  onOpenModelSettings,
}: ThreadShellProps) {
  const { t } = useTranslation();
  const chatId = session?.chatId ?? null;
  const historyKey = session?.key ?? null;
  const {
    messages: historical,
    loading,
    hasPendingToolCalls,
    refresh: refreshHistory,
    version: historyVersion,
  } = useSessionHistory(historyKey);
  const { client, modelName, token } = useClient();
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>(client.status);
  const [booting, setBooting] = useState(false);
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([]);
  const cliApps = useInstalledSettingItems({
    token,
    eventName: CLI_APPS_CHANGED_EVENT,
    fetchPayload: fetchCliApps,
    isPayload: isCliAppsPayload,
    selectItems: installedCliAppsFromPayload,
  });
  const mcpPresets = useInstalledSettingItems({
    token,
    eventName: MCP_PRESETS_CHANGED_EVENT,
    fetchPayload: fetchMcpPresets,
    isPayload: isMcpPresetsPayload,
    selectItems: installedMcpPresetsFromPayload,
  });
  const [settings, setSettings] = useState<SettingsPayload | null>(settingsSnapshot);
  const [heroGreetingKey, setHeroGreetingKey] = useState(randomHeroGreetingKey);
  const [scrollToBottomSignal, setScrollToBottomSignal] = useState(0);
  const [filePreviewPath, setFilePreviewPath] = useState<string | null>(null);
  const [filePreviewClosing, setFilePreviewClosing] = useState(false);
  const [filePreviewWidth, setFilePreviewWidth] = useState(FILE_PREVIEW_DEFAULT_WIDTH);
  const shellRef = useRef<HTMLElement | null>(null);
  const filePreviewWidthRef = useRef(FILE_PREVIEW_DEFAULT_WIDTH);
  const filePreviewCloseTimerRef = useRef<number | null>(null);
  const pendingFirstRef = useRef<PendingFirstMessage | null>(null);
  const viewportRef = useRef<ThreadViewportHandle | null>(null);
  const messageCacheRef = useRef<Map<string, UIMessage[]>>(new Map());
  /** Last chatId we associated with the in-memory thread (for cache-on-switch). */
  const prevChatIdForCacheRef = useRef<string | null>(null);
  /** Skip one message-cache write right after chatId changes (messages may not match yet). */
  const skipLayoutCacheRef = useRef(false);
  const appliedHistoryVersionRef = useRef<Map<string, number>>(new Map());
  const pendingCanonicalHydrateRef = useRef<Set<string>>(new Set());
  const sessionKeyByChatIdRef = useRef<Map<string, string>>(new Map());

  const initial = useMemo(() => {
    if (!chatId) return historical;
    return messageCacheRef.current.get(chatId) ?? historical;
  }, [chatId, historical]);
  const handleTurnEnd = useCallback(() => {
    onTurnEnd?.();
  }, [onTurnEnd]);
  const {
    messages,
    isStreaming,
    runStartedAt,
    goalState,
    securityInspection,
    resolveSecurityInspection,
    privilegedActionApproval,
    resolvePrivilegedAction,
    send,
    stop,
    setMessages,
    streamError,
    dismissStreamError,
  } = useNanobotStream(chatId, initial, hasPendingToolCalls, handleTurnEnd);

  useEffect(() => client.onStatus(setConnectionStatus), [client]);

  const websocketReady = connectionStatus === "open";
  const connectionBlocked = !websocketReady;
  const connectionLabel = t(`connection.${connectionStatus}`);
  const disconnectedPlaceholder = t("thread.composer.placeholderDisconnected");
  const actionGuardrailsBudgetError =
    streamError?.kind === "action_guardrails_budget_stopped" ? streamError : null;

  useEffect(() => {
    if (chatId && historyKey) sessionKeyByChatIdRef.current.set(chatId, historyKey);
  }, [chatId, historyKey]);

  useEffect(() => {
    filePreviewWidthRef.current = filePreviewWidth;
  }, [filePreviewWidth]);

  useEffect(() => {
    if (filePreviewCloseTimerRef.current !== null) {
      window.clearTimeout(filePreviewCloseTimerRef.current);
      filePreviewCloseTimerRef.current = null;
    }
    setFilePreviewClosing(false);
    setFilePreviewPath(null);
  }, [historyKey]);

  useEffect(() => {
    return () => {
      if (filePreviewCloseTimerRef.current !== null) {
        window.clearTimeout(filePreviewCloseTimerRef.current);
      }
    };
  }, []);

  const displayMessages = useMemo(() => projectWebuiThreadMessages(messages), [messages]);

  const showHeroComposer = messages.length === 0 && !loading;
  const wasShowingHeroComposerRef = useRef(showHeroComposer);
  const modelBadge = useMemo(
    () => toModelBadgeInfo(modelName, settings),
    [modelName, settings],
  );
  const modelBadgeLabel = modelBadge.needsSetup
    ? t("thread.composer.modelNotConfigured", { defaultValue: "Model not configured" })
    : modelBadge.label;
  useEffect(() => {
    if (showHeroComposer && !wasShowingHeroComposerRef.current) {
      setHeroGreetingKey(randomHeroGreetingKey());
    }
    wasShowingHeroComposerRef.current = showHeroComposer;
  }, [showHeroComposer]);

  const withWorkspaceScope = useCallback(
    (options?: SendOptions): SendOptions | undefined => {
      if (!workspaceScope) return options;
      return {
        ...(options ?? {}),
        workspaceScope,
      };
    },
    [workspaceScope],
  );

  const refreshModelSettings = useCallback(async () => {
    try {
      setSettings(await fetchSettings(token));
    } catch {
      if (!settingsSnapshot) setSettings(null);
    }
  }, [settingsSnapshot, token]);

  useEffect(() => {
    if (settingsSnapshot) {
      setSettings(settingsSnapshot);
      return;
    }
    void refreshModelSettings();
  }, [refreshModelSettings, settingsSnapshot]);

  useEffect(() => {
    return client.onRuntimeModelUpdate(() => {
      void refreshModelSettings();
    });
  }, [client, refreshModelSettings]);

  useEffect(() => {
    if (!chatId || loading) return;
    const cached = messageCacheRef.current.get(chatId);
    const appliedVersion = appliedHistoryVersionRef.current.get(chatId) ?? 0;
    const hasPendingCanonicalHydrate = pendingCanonicalHydrateRef.current.has(chatId);
    const hasNewCanonicalHistory = hasPendingCanonicalHydrate && historyVersion > appliedVersion;
    // When the user switches away and back, keep the local in-memory thread
    // state (including not-yet-persisted messages) instead of replacing it with
    // whatever the history endpoint currently knows about. Once a fresh
    // canonical replay arrives (e.g. after ``session_updated`` refresh), prefer it
    // so rendering converges to the same shape as a manual refresh.
    setMessages((prev) => {
      const normalizedHistory = projectWebuiThreadMessages(historical);
      const keepLiveMessages = (messagesToKeep: UIMessage[]) => {
        const projected = projectWebuiThreadMessages(messagesToKeep);
        messageCacheRef.current.set(chatId, projected);
        return projected;
      };
      if (hasNewCanonicalHistory && historical.length > 0) {
        if (isStaleThreadSnapshot(prev, normalizedHistory)) return keepLiveMessages(prev);
        pendingCanonicalHydrateRef.current.delete(chatId);
        appliedHistoryVersionRef.current.set(chatId, historyVersion);
        messageCacheRef.current.set(chatId, normalizedHistory);
        return normalizedHistory;
      }
      if (cached && cached.length > 0) {
        const normalizedCached = projectWebuiThreadMessages(cached);
        if (isStaleThreadSnapshot(prev, normalizedCached)) return keepLiveMessages(prev);
        return normalizedCached;
      }
      if (isStaleThreadSnapshot(prev, normalizedHistory)) return keepLiveMessages(prev);
      appliedHistoryVersionRef.current.set(chatId, historyVersion);
      if (normalizedHistory.length > 0) messageCacheRef.current.set(chatId, normalizedHistory);
      return normalizedHistory;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, chatId, historical, historyVersion]);

  useEffect(() => {
    if (!chatId) return;
    return client.onSessionUpdate((updatedChatId, scope) => {
      if (updatedChatId !== chatId) return;
      if (scope === "metadata") return;
      pendingCanonicalHydrateRef.current.add(chatId);
      refreshHistory();
    });
  }, [chatId, client, refreshHistory]);

  useEffect(() => {
    if (!chatId || loading) return;
    setScrollToBottomSignal((value) => value + 1);
  }, [chatId, loading, historical]);

  useEffect(() => {
    if (chatId) return;
    setMessages(projectWebuiThreadMessages(historical));
  }, [chatId, historical, setMessages]);

  useLayoutEffect(() => {
    if (chatId) {
      const prev = prevChatIdForCacheRef.current;
      if (prev && prev !== chatId) {
        messageCacheRef.current.set(prev, projectWebuiThreadMessages(messages));
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = chatId;
    } else {
      if (prevChatIdForCacheRef.current) {
        messageCacheRef.current.set(
          prevChatIdForCacheRef.current,
          projectWebuiThreadMessages(messages),
        );
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = null;
    }
  }, [chatId, messages]);

  // Persist thread to in-memory cache after paint so ``useNanobotStream``'s chat switch
  // ``useEffect`` reset has flushed; ``skipLayoutCacheRef`` drops the first run that still
  // sees the *previous* chat's ``messages`` (avoids stale rows leaking across sessions).
  useEffect(() => {
    if (!chatId) {
      return;
    }
    if (skipLayoutCacheRef.current) {
      skipLayoutCacheRef.current = false;
      return;
    }
    if (loading) {
      return;
    }
    messageCacheRef.current.set(chatId, projectWebuiThreadMessages(messages));
  }, [chatId, loading, messages]);

  useEffect(() => {
    if (!chatId) return;
    const pending = pendingFirstRef.current;
    if (!pending) return;
    pendingFirstRef.current = null;
    setScrollToBottomSignal((value) => value + 1);
    send(pending.content, pending.images, pending.options);
    setBooting(false);
  }, [chatId, send]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const commands = await listSlashCommands(token);
        if (!cancelled) setSlashCommands(commands);
      } catch {
        if (!cancelled) setSlashCommands([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleWelcomeSend = useCallback(
    async (content: string, images?: SendImage[], options?: SendOptions) => {
      if (booting) return;
      setBooting(true);
      pendingFirstRef.current = { content, images, options: withWorkspaceScope(options) };
      const newId = await onCreateChat?.(workspaceScope);
      if (!newId) {
        pendingFirstRef.current = null;
        setBooting(false);
      }
    },
    [booting, onCreateChat, withWorkspaceScope, workspaceScope],
  );

  const handleThreadSend = useCallback(
    (content: string, images?: SendImage[], options?: SendOptions) => {
      setScrollToBottomSignal((value) => value + 1);
      send(content, images, withWorkspaceScope(options));
    },
    [send, withWorkspaceScope],
  );

  const handleOpenFilePreview = useCallback((path: string) => {
    if (filePreviewCloseTimerRef.current !== null) {
      window.clearTimeout(filePreviewCloseTimerRef.current);
      filePreviewCloseTimerRef.current = null;
    }
    setFilePreviewClosing(false);
    setFilePreviewPath(path);
  }, []);

  const handleCloseFilePreview = useCallback(() => {
    if (!filePreviewPath || filePreviewClosing) return;
    setFilePreviewClosing(true);
    filePreviewCloseTimerRef.current = window.setTimeout(() => {
      filePreviewCloseTimerRef.current = null;
      setFilePreviewPath(null);
      setFilePreviewClosing(false);
    }, FILE_PREVIEW_CLOSE_ANIMATION_MS);
  }, [filePreviewClosing, filePreviewPath]);

  const handleFilePreviewResizeStart = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const panel = event.currentTarget.closest<HTMLElement>("[data-file-preview-panel]");
    const shellRect = shellRef.current?.getBoundingClientRect();
    const rightEdge = shellRect?.right ?? window.innerWidth;
    const maxWidth = maxFilePreviewWidth(shellRect?.width ?? window.innerWidth);
    const originalBodyCursor = document.body.style.cursor;
    const originalBodyUserSelect = document.body.style.userSelect;
    const originalPanelTransition = panel?.style.transition ?? "";
    let nextWidth = filePreviewWidthRef.current;
    let frame: number | null = null;

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    if (panel) panel.style.transition = "none";

    const applyWidth = (clientX: number) => {
      nextWidth = clampFilePreviewWidth(rightEdge - clientX, maxWidth);
      filePreviewWidthRef.current = nextWidth;
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        frame = null;
        panel?.style.setProperty("--file-preview-width", `${nextWidth}px`);
        panel?.style.setProperty("--file-preview-slot-width", `${nextWidth}px`);
      });
    };
    const handlePointerMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      applyWidth(moveEvent.clientX);
    };
    const handlePointerUp = () => {
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
        frame = null;
      }
      panel?.style.setProperty("--file-preview-width", `${nextWidth}px`);
      panel?.style.setProperty("--file-preview-slot-width", `${nextWidth}px`);
      if (panel) panel.style.transition = originalPanelTransition;
      setFilePreviewWidth(nextWidth);
      document.body.style.cursor = originalBodyCursor;
      document.body.style.userSelect = originalBodyUserSelect;
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };

    applyWidth(event.clientX);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
  }, []);

  useEffect(() => {
    if (!filePreviewPath) return;
    const clampToShell = () => {
      const shellWidth = shellRef.current?.getBoundingClientRect().width ?? window.innerWidth;
      const maxWidth = maxFilePreviewWidth(shellWidth);
      const nextWidth = clampFilePreviewWidth(filePreviewWidthRef.current, maxWidth);
      filePreviewWidthRef.current = nextWidth;
      setFilePreviewWidth(nextWidth);
    };
    clampToShell();
    window.addEventListener("resize", clampToShell);
    return () => {
      window.removeEventListener("resize", clampToShell);
    };
  }, [filePreviewPath]);

  const composer = (
    <>
      {connectionBlocked ? (
        <div
          role="status"
          aria-live="polite"
          className="mb-2 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px] leading-5 text-amber-800 dark:text-amber-200"
        >
          <WifiOff className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div className="flex-1">
            <p className="font-medium">{t("thread.connectionUnavailable.title")}</p>
            <p className="mt-0.5 text-amber-800/80 dark:text-amber-200/80">
              {t("thread.connectionUnavailable.body", { status: connectionLabel })}
            </p>
          </div>
        </div>
      ) : null}
      {streamError && streamError.kind !== "action_guardrails_budget_stopped" ? (
        <StreamErrorNotice
          error={streamError}
          onDismiss={dismissStreamError}
          className={showHeroComposer ? "max-w-[58rem]" : "max-w-[49.5rem]"}
        />
      ) : null}
      {session ? (
        <ThreadComposer
          onSend={handleThreadSend}
          disabled={!chatId || connectionBlocked}
          isStreaming={isStreaming}
          placeholder={
            connectionBlocked
              ? disconnectedPlaceholder
              : showHeroComposer
              ? t("thread.composer.placeholderHero")
              : t("thread.composer.placeholderThread")
          }
          modelLabel={modelBadgeLabel}
          modelProvider={modelBadge.provider}
          modelProviderLabel={modelBadge.providerLabel}
          modelNeedsSetup={modelBadge.needsSetup}
          onModelBadgeClick={modelBadge.needsSetup ? onOpenModelSettings : undefined}
          variant={showHeroComposer ? "hero" : "thread"}
          slashCommands={slashCommands}
          cliApps={cliApps}
          mcpPresets={mcpPresets}
          onStop={stop}
          runStartedAt={runStartedAt}
          goalState={goalState}
          workspaceScope={workspaceScope}
          workspaceDefaultScope={workspaceDefaultScope}
          workspaceControls={workspaceControls}
          workspaceScopeDisabled={workspaceScopeDisabled}
          workspaceError={workspaceError}
          onWorkspaceScopeChange={onWorkspaceScopeChange}
          pendingQueueKey={chatId}
          draft={composerDraft}
          onDraftApplied={onComposerDraftApplied}
        />
      ) : (
        <ThreadComposer
          onSend={handleWelcomeSend}
          disabled={booting || connectionBlocked}
          isStreaming={isStreaming}
          placeholder={
            connectionBlocked
              ? disconnectedPlaceholder
              : booting
              ? t("thread.composer.placeholderOpening")
              : t("thread.composer.placeholderHero")
          }
          modelLabel={modelBadgeLabel}
          modelProvider={modelBadge.provider}
          modelProviderLabel={modelBadge.providerLabel}
          modelNeedsSetup={modelBadge.needsSetup}
          onModelBadgeClick={modelBadge.needsSetup ? onOpenModelSettings : undefined}
          variant="hero"
          slashCommands={slashCommands}
          cliApps={cliApps}
          mcpPresets={mcpPresets}
          runStartedAt={runStartedAt}
          goalState={goalState}
          workspaceScope={workspaceScope}
          workspaceDefaultScope={workspaceDefaultScope}
          workspaceControls={workspaceControls}
          workspaceScopeDisabled={workspaceScopeDisabled}
          workspaceError={workspaceError}
          onWorkspaceScopeChange={onWorkspaceScopeChange}
          draft={composerDraft}
          onDraftApplied={onComposerDraftApplied}
        />
      )}
    </>
  );

  const emptyState = loading ? (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {t("thread.loadingConversation")}
    </div>
  ) : (
    <div className="flex w-full flex-col items-center text-center animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <h1 className="whitespace-pre-line text-balance text-[36px] font-normal leading-tight tracking-normal text-foreground sm:text-[44px]">
        {t(heroGreetingKey)}
      </h1>
    </div>
  );
  const sessionInfoAction = historyKey ? (
    <SessionInfoPopover sessionKey={historyKey} token={token} title={title} />
  ) : undefined;
  const promptNavigatorAction = historyKey ? (
    <PromptNavigator
      messages={displayMessages}
      onJumpToPrompt={(promptId) => viewportRef.current?.jumpToUserPrompt(promptId)}
    />
  ) : undefined;

  return (
    <section ref={shellRef} className="relative flex min-h-0 flex-1 overflow-hidden">
      <SecurityInspectionDialog
        inspection={securityInspection}
        onDecision={resolveSecurityInspection}
      />
      <ActionGuardrailsBudgetDialog
        error={actionGuardrailsBudgetError}
        onClose={dismissStreamError}
      />
      <PrivilegedActionDialog
        approval={privilegedActionApproval}
        onDecision={resolvePrivilegedAction}
      />
      <div className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
        {!hideHeader ? (
          <ThreadHeader
            title={title}
            onToggleSidebar={onToggleSidebar}
            theme={theme}
            onToggleTheme={onToggleTheme}
            hideThemeButton={hideThemeButton}
            minimal={!session && !loading}
            promptNavigatorAction={promptNavigatorAction}
            sessionInfoAction={sessionInfoAction}
          />
        ) : null}
        <ThreadViewport
          ref={viewportRef}
          messages={displayMessages}
          isStreaming={isStreaming}
          emptyState={emptyState}
          composer={composer}
          scrollToBottomSignal={scrollToBottomSignal}
          conversationKey={historyKey}
          showScrollToBottomButton={!!session}
          cliApps={cliApps}
          mcpPresets={mcpPresets}
          onOpenFilePreview={historyKey ? handleOpenFilePreview : undefined}
        />
      </div>
      {filePreviewPath && historyKey ? (
        <FilePreviewPanel
          sessionKey={historyKey}
          path={filePreviewPath}
          token={token}
          desktopWidth={filePreviewWidth}
          isClosing={filePreviewClosing}
          onResizeStart={handleFilePreviewResizeStart}
          onClose={handleCloseFilePreview}
        />
      ) : null}
    </section>
  );
}
