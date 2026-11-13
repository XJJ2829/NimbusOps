import type {
  ActionGuardrailsConfig,
  ActionGuardrailsPayload,
  AgentSpecPayload,
  AuditTraceDetail,
  AuditTracesPayload,
  AuthStatusPayload,
  AuthUser,
  BuiltinToolsPayload,
  ChatSummary,
  CliAppsPayload,
  FilePreviewPayload,
  ImageGenerationSettingsUpdate,
  McpPresetsPayload,
  ModelConfigurationCreate,
  ModelConfigurationUpdate,
  NetworkSafetySettingsUpdate,
  ProviderModelsPayload,
  ProviderSettingsUpdate,
  PromptGuardrailsConfig,
  PromptGuardrailsPayload,
  PrivilegedActionCreateInput,
  PrivilegedActionCreatePayload,
  PrivilegedAuditPayload,
  PrivilegedExecutionPayload,
  PrivilegedPolicyPayload,
  SessionAutomationsPayload,
  SettingsPayload,
  SettingsUpdate,
  SandboxPayload,
  SandboxUpdate,
  RuntimeIdentityPayload,
  SessionAutomationCreateInput,
  SidebarStatePayload,
  SkillDetail,
  SkillsPayload,
  SlashCommand,
  WebSearchSettingsUpdate,
  WorkspacesPayload,
  UsersPayload,
  WebuiThreadPersistedPayload,
  WorkspaceScopePayload,
} from "./types";
import { fetchWithTimeout } from "./http";

const API_READ_TIMEOUT_MS = 20_000;

async function publicRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetchWithTimeout(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
    },
    credentials: "same-origin",
  }, API_READ_TIMEOUT_MS);
  if (!res.ok) {
    const text = (await res.text()).trim();
    let message = text || `HTTP ${res.status}`;
    try {
      const body = JSON.parse(text) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the plain response.
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  timeoutMs: number = 0,
): Promise<T> {
  const res = await fetchWithTimeout(
    url,
    {
      ...(init ?? {}),
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      credentials: "same-origin",
    },
    timeoutMs,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    let message = text;
    if (text) {
      try {
        const payload = JSON.parse(text) as { detail?: unknown; message?: unknown };
        if (typeof payload.detail === "string") message = payload.detail;
        else if (typeof payload.message === "string") message = payload.message;
      } catch {
        // Preserve plain-text gateway errors.
      }
    }
    throw new ApiError(res.status, message || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    const text = typeof res.text === "function" ? await res.text() : "";
    const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype");
    throw new ApiError(
      res.status,
      isHtml
        ? "Gateway returned WebUI HTML instead of JSON. Restart python -m my_agent serve and try again."
        : "Gateway returned a non-JSON response.",
    );
  }
  return (await res.json()) as T;
}

function mcpValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "X-Nanobot-MCP-Values": JSON.stringify(payload) };
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function fetchAuthStatus(base: string = ""): Promise<AuthStatusPayload> {
  return publicRequest<AuthStatusPayload>(`${base}/api/auth/status`);
}

export async function fetchCurrentUser(base: string = ""): Promise<AuthUser> {
  const payload = await publicRequest<{ user: AuthUser }>(`${base}/api/auth/me`);
  return payload.user;
}

export async function setupAdmin(
  values: { username: string; display_name: string; password: string },
  base: string = "",
): Promise<AuthUser> {
  const payload = await publicRequest<{ user: AuthUser }>(`${base}/api/auth/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  return payload.user;
}

export async function loginUser(
  username: string,
  password: string,
  base: string = "",
): Promise<AuthUser> {
  const payload = await publicRequest<{ user: AuthUser }>(`${base}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return payload.user;
}

export async function logoutUser(base: string = ""): Promise<void> {
  await publicRequest<{ ok: boolean }>(`${base}/api/auth/logout`, { method: "POST" });
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
  base: string = "",
): Promise<void> {
  await publicRequest<{ ok: boolean }>(`${base}/api/auth/password`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

export async function fetchUsers(token: string, base: string = ""): Promise<UsersPayload> {
  return request<UsersPayload>(`${base}/api/auth/users`, token, undefined, API_READ_TIMEOUT_MS);
}

export async function createUser(
  token: string,
  values: {
    username: string;
    display_name: string;
    password: string;
    role: "system_admin" | "user";
    workspace_root?: string | null;
  },
  base: string = "",
): Promise<AuthUser> {
  const payload = await request<{ user: AuthUser }>(`${base}/api/auth/users`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  return payload.user;
}

export async function updateUser(
  token: string,
  userId: string,
  values: Partial<{
    display_name: string;
    password: string;
    role: "system_admin" | "user";
    enabled: boolean;
    permissions: string[];
    workspace_root: string | null;
  }>,
  base: string = "",
): Promise<AuthUser> {
  const payload = await request<{ user: AuthUser }>(
    `${base}/api/auth/users/${encodeURIComponent(userId)}`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    },
  );
  return payload.user;
}

export async function deleteUser(
  token: string,
  userId: string,
  base: string = "",
): Promise<void> {
  await request<{ deleted: boolean }>(
    `${base}/api/auth/users/${encodeURIComponent(userId)}`,
    token,
    { method: "DELETE" },
  );
}

export async function fetchSandbox(
  token: string,
  base: string = "",
): Promise<SandboxPayload> {
  return request<SandboxPayload>(
    `${base}/api/security/sandbox`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSandbox(
  token: string,
  values: SandboxUpdate,
  base: string = "",
): Promise<SandboxPayload> {
  return request<SandboxPayload>(`${base}/api/security/sandbox`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function fetchRuntimeIdentity(
  token: string,
  base: string = "",
): Promise<RuntimeIdentityPayload> {
  return request<RuntimeIdentityPayload>(
    `${base}/api/security/runtime-identity`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateRuntimeIdentity(
  token: string,
  values: RuntimeIdentityPayload["configured"],
  base: string = "",
): Promise<RuntimeIdentityPayload> {
  return request<RuntimeIdentityPayload>(
    `${base}/api/security/runtime-identity`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    },
  );
}

export async function fetchPrivilegedExecution(
  token: string,
  base: string = "",
): Promise<PrivilegedExecutionPayload> {
  return request<PrivilegedExecutionPayload>(
    `${base}/api/security/privileged-execution`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updatePrivilegedExecution(
  token: string,
  values: {
    enabled: boolean;
    socket_path: string;
    approval_timeout: number;
    request_timeout: number;
  },
  base: string = "",
): Promise<PrivilegedExecutionPayload> {
  return request<PrivilegedExecutionPayload>(
    `${base}/api/security/privileged-execution`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    },
  );
}

export async function fetchPrivilegedAudit(
  token: string,
  base: string = "",
): Promise<PrivilegedAuditPayload> {
  return request<PrivilegedAuditPayload>(
    `${base}/api/security/privileged-execution/audit`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAuditTraces(
  token: string,
  base: string = "",
): Promise<AuditTracesPayload> {
  return request<AuditTracesPayload>(
    `${base}/api/audit/traces`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAuditTrace(
  token: string,
  traceId: string,
  sessionKey: string,
  base: string = "",
): Promise<AuditTraceDetail> {
  const query = new URLSearchParams({ session_key: sessionKey });
  return request<AuditTraceDetail>(
    `${base}/api/audit/traces/${encodeURIComponent(traceId)}?${query.toString()}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAuditTraceExport(
  token: string,
  traceId: string,
  sessionKey: string,
  base: string = "",
): Promise<Blob> {
  const query = new URLSearchParams({ session_key: sessionKey });
  const response = await fetchWithTimeout(
    `${base}/api/audit/traces/${encodeURIComponent(traceId)}/export?${query.toString()}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    },
    API_READ_TIMEOUT_MS,
  );
  if (!response.ok) throw new ApiError(response.status, `HTTP ${response.status}`);
  return response.blob();
}

export async function fetchPrivilegedPolicy(
  token: string,
  base: string = "",
): Promise<PrivilegedPolicyPayload> {
  return request<PrivilegedPolicyPayload>(
    `${base}/api/security/privileged-execution/policy`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function addPrivilegedAction(
  token: string,
  values: PrivilegedActionCreateInput,
  base: string = "",
): Promise<PrivilegedActionCreatePayload> {
  return request<PrivilegedActionCreatePayload>(
    `${base}/api/security/privileged-execution/actions`,
    token,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    },
  );
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    run_started_at?: number | null;
    workspace_scope?: WorkspaceScopePayload | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    runStartedAt: s.run_started_at ?? null,
    workspaceScope: s.workspace_scope ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  const res = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function fetchFilePreview(
  token: string,
  key: string,
  path: string,
  base: string = "",
): Promise<FilePreviewPayload> {
  const query = new URLSearchParams();
  query.set("path", path);
  return request<FilePreviewPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/file-preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSessionAutomations(
  token: string,
  key: string,
  base: string = "",
): Promise<SessionAutomationsPayload> {
  return request<SessionAutomationsPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createSessionAutomation(
  token: string,
  key: string,
  values: SessionAutomationCreateInput,
  base: string = "",
): Promise<SessionAutomationsPayload> {
  const query = new URLSearchParams();
  query.set("payload", JSON.stringify(values));
  return request<SessionAutomationsPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/automations/create?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/webui/skills`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function importSkillArchive(
  token: string,
  file: File,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/webui/skills/import`,
    token,
    {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/zip",
        "X-My-Agent-Filename": encodeURIComponent(file.name),
      },
      body: file,
    },
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkillDetail(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  return request<SkillDetail>(
    `${base}/api/webui/skills/${encodeURIComponent(name)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAgentSpecPolicies(
  token: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchActionGuardrails(
  token: string,
  base: string = "",
): Promise<ActionGuardrailsPayload> {
  return request<ActionGuardrailsPayload>(
    `${base}/api/guardrails/action`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateActionGuardrails(
  token: string,
  values: ActionGuardrailsConfig,
  base: string = "",
): Promise<ActionGuardrailsPayload> {
  return request<ActionGuardrailsPayload>(`${base}/api/guardrails/action`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function fetchPromptGuardrails(
  token: string,
  base: string = "",
): Promise<PromptGuardrailsPayload> {
  return request<PromptGuardrailsPayload>(
    `${base}/api/guardrails/prompt`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updatePromptGuardrails(
  token: string,
  values: PromptGuardrailsConfig,
  base: string = "",
): Promise<PromptGuardrailsPayload> {
  return request<PromptGuardrailsPayload>(`${base}/api/guardrails/prompt`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function setAgentSpecEnabled(
  token: string,
  enabled: boolean,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(`${base}/api/security/agentspec`, token, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function createAgentSpecRule(
  token: string,
  text: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(`${base}/api/security/agentspec/rules`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function updateAgentSpecRule(
  token: string,
  ruleId: string,
  text: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec/rules/${encodeURIComponent(ruleId)}`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    },
  );
}

export async function setAgentSpecRuleEnabled(
  token: string,
  ruleId: string,
  enabled: boolean,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec/rules/${encodeURIComponent(ruleId)}/enabled`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    },
  );
}

export async function deleteAgentSpecRule(
  token: string,
  ruleId: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec/rules/${encodeURIComponent(ruleId)}`,
    token,
    { method: "DELETE" },
  );
}

export async function createAgentSpecPredicate(
  token: string,
  source: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(`${base}/api/security/agentspec/predicates`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
}

export async function updateAgentSpecPredicate(
  token: string,
  predicateName: string,
  source: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec/predicates/${encodeURIComponent(predicateName)}`,
    token,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    },
  );
}

export async function deleteAgentSpecPredicate(
  token: string,
  predicateName: string,
  base: string = "",
): Promise<AgentSpecPayload> {
  return request<AgentSpecPayload>(
    `${base}/api/security/agentspec/predicates/${encodeURIComponent(predicateName)}`,
    token,
    { method: "DELETE" },
  );
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function restartRuntime(
  token: string,
  base: string = "",
): Promise<{ ok: boolean; restarting: boolean }> {
  return request<{ ok: boolean; restarting: boolean }>(
    `${base}/api/runtime/restart`,
    token,
    { method: "POST" },
  );
}

export async function openRuntimeLogs(
  token: string,
  base: string = "",
): Promise<{ ok: boolean; path: string }> {
  return request<{ ok: boolean; path: string }>(
    `${base}/api/runtime/open-logs`,
    token,
    { method: "POST" },
  );
}

export async function exportRuntimeDiagnostics(
  token: string,
  base: string = "",
): Promise<{ ok: boolean; path: string }> {
  return request<{ ok: boolean; path: string }>(
    `${base}/api/runtime/export-diagnostics`,
    token,
    { method: "POST" },
    API_READ_TIMEOUT_MS,
  );
}

export async function pickRuntimeFolder(
  token: string,
  base: string = "",
): Promise<{ path: string | null }> {
  return request<{ path: string | null }>(
    `${base}/api/runtime/pick-folder`,
    token,
    { method: "POST" },
    0,
  );
}

export async function fetchSettingsUsage(
  token: string,
  base: string = "",
): Promise<NonNullable<SettingsPayload["usage"]>> {
  return request<NonNullable<SettingsPayload["usage"]>>(
    `${base}/api/settings/usage`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchWorkspaces(
  token: string,
  base: string = "",
): Promise<WorkspacesPayload> {
  return request<WorkspacesPayload>(
    `${base}/api/workspaces`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchBuiltinTools(
  token: string,
  base: string = "",
): Promise<BuiltinToolsPayload> {
  return request<BuiltinToolsPayload>(
    `${base}/api/settings/builtin-tools`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function importCliAppArchive(
  token: string,
  file: File,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps/import`,
    token,
    {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/zip",
        "X-My-Agent-Filename": encodeURIComponent(file.name),
      },
      body: file,
    },
    API_READ_TIMEOUT_MS,
  );
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps/${action}?${query}`, token);
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  base: string = "",
): Promise<ProviderModelsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<ProviderModelsPayload>(
    `${base}/api/settings/provider-models?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${action}?${query}`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    { headers: mcpValuesHeader({ config }) },
  );
}

export async function importMcpArchive(
  token: string,
  file: File,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import-archive`,
    token,
    {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/zip",
        "X-My-Agent-Filename": encodeURIComponent(file.name),
      },
      body: file,
    },
    API_READ_TIMEOUT_MS,
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    { headers: mcpValuesHeader({ name, enabled_tools: enabledTools }) },
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(
    `${base}/api/commands`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  const query = new URLSearchParams();
  query.set("state", JSON.stringify(state));
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state/update?${query}`,
    token,
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.modelPreset !== undefined) {
    query.set("model_preset", update.modelPreset ?? "default");
  }
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  if (update.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(update.contextWindowTokens));
  }
  if (update.timezone !== undefined) query.set("timezone", update.timezone);
  if (update.botName !== undefined) query.set("bot_name", update.botName);
  if (update.botIcon !== undefined) query.set("bot_icon", update.botIcon);
  if (update.workspacePath !== undefined) query.set("workspace_path", update.workspacePath);
  if (update.toolHintMaxLength !== undefined) {
    query.set("tool_hint_max_length", String(update.toolHintMaxLength));
  }
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (configuration.name !== undefined) query.set("name", configuration.name);
  query.set("label", configuration.label);
  query.set("provider", configuration.provider);
  query.set("model", configuration.model);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/create?${query}`,
    token,
  );
}

export async function updateModelConfiguration(
  token: string,
  configuration: ModelConfigurationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("name", configuration.name);
  if (configuration.label !== undefined) query.set("label", configuration.label);
  if (configuration.provider !== undefined) query.set("provider", configuration.provider);
  if (configuration.model !== undefined) query.set("model", configuration.model);
  if (configuration.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(configuration.contextWindowTokens));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/update?${query}`,
    token,
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.apiBase !== undefined) query.set("api_base", update.apiBase);
  if (update.apiType !== undefined) query.set("api_type", update.apiType);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
  );
}

export async function loginProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth-login?${query}`,
    token,
  );
}

export async function logoutProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth-logout?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  if (update.maxResults !== undefined) query.set("max_results", String(update.maxResults));
  if (update.timeout !== undefined) query.set("timeout", String(update.timeout));
  if (update.useJinaReader !== undefined) {
    query.set("use_jina_reader", String(update.useJinaReader));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}

export async function updateNetworkSafetySettings(
  token: string,
  update: NetworkSafetySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("webui_allow_local_service_access", String(update.webuiAllowLocalServiceAccess));
  query.set("webui_default_access_mode", update.webuiDefaultAccessMode);
  return request<SettingsPayload>(
    `${base}/api/settings/network-safety/update?${query}`,
    token,
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("default_aspect_ratio", update.defaultAspectRatio);
  query.set("default_image_size", update.defaultImageSize);
  query.set("max_images_per_turn", String(update.maxImagesPerTurn));
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation/update?${query}`,
    token,
  );
}
