import { useCallback, useEffect, useState } from "react";
import { Check, ChevronDown, Folder } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type {
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  projectNameFromPath,
  resolveWorkspaceProjectPath,
  selectedProjectScope,
  shortWorkspacePath,
  workspaceRelativePath,
} from "@/lib/workspace";

export function WorkspaceProjectPicker({
  isHero,
  disabled,
  scope,
  defaultScope,
  controls,
  error,
  onChange,
}: {
  isHero: boolean;
  disabled?: boolean;
  scope: WorkspaceScopePayload | null;
  defaultScope: WorkspaceScopePayload | null;
  controls: WorkspacesPayload["controls"] | null;
  error?: string | null;
  onChange?: (scope: WorkspaceScopePayload) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [pathDraft, setPathDraft] = useState("");
  const [pathError, setPathError] = useState<string | null>(null);
  const currentProjectScope = selectedProjectScope(scope, defaultScope);
  const workspaceRoot = defaultScope?.project_path ?? "";
  const workspaceRootLabel = workspaceRoot.replace(/\/+$/, "");
  const projectLabel = currentProjectScope
    ? currentProjectScope.project_name || projectNameFromPath(currentProjectScope.project_path)
    : t("thread.composer.workspace.projectPlaceholder");
  const visible = isHero
    && !!defaultScope
    && !!onChange
    && controls?.can_change_project !== false;

  useEffect(() => {
    if (!open) return;
    setPathDraft(
      currentProjectScope
        ? workspaceRelativePath(currentProjectScope.project_path, workspaceRoot)
        : "",
    );
    setPathError(null);
  }, [currentProjectScope, open, workspaceRoot]);

  useEffect(() => {
    if (error && visible) setOpen(true);
  }, [error, visible]);

  const applyProjectPath = useCallback(
    (projectPath: string, projectName?: string) => {
      const base = scope ?? defaultScope;
      const root = workspaceRoot || base?.project_path || "";
      let trimmed = projectPath.trim();
      if (!base || !onChange) return;
      if (trimmed) {
        const normalizedRoot = root.replace(/\\/g, "/").replace(/\/+$/, "");
        const normalizedInput = trimmed.replace(/\\/g, "/").replace(/\/+$/, "");
        if (normalizedInput === normalizedRoot) {
          trimmed = "";
        } else if (normalizedInput.startsWith(`${normalizedRoot}/`)) {
          trimmed = normalizedInput.slice(normalizedRoot.length + 1);
        }
      }
      if (trimmed.startsWith("..") || trimmed.includes("/../") || trimmed.includes("\\..\\")) {
        setPathError(t("workspace.dialog.pathEscapesRoot", { defaultValue: "项目路径必须位于当前工作区内" }));
        return;
      }
      const resolvedPath = trimmed
        ? resolveWorkspaceProjectPath(root || base.project_path, trimmed)
        : root || base.project_path;
      onChange({
        ...base,
        project_path: resolvedPath,
        project_name: projectName || projectNameFromPath(resolvedPath),
        restrict_to_workspace: base.access_mode === "restricted",
      });
      setPathError(null);
      setOpen(false);
    },
    [defaultScope, onChange, scope, t, workspaceRoot],
  );

  if (!visible || !defaultScope || !onChange) return null;

  return (
    <div className="flex items-center rounded-b-[28px] border-t border-border/25 bg-muted/60 px-4 py-1.5 dark:bg-white/[0.055]">
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={t("thread.composer.workspace.projectAria")}
            className={cn(
              "inline-flex h-7 max-w-[18rem] items-center gap-2 rounded-full px-2.5",
              "text-[12px] font-medium text-muted-foreground/90 transition-colors",
              "hover:bg-background/70 hover:text-foreground disabled:pointer-events-none disabled:opacity-55",
              currentProjectScope && "text-foreground/82",
            )}
          >
            <Folder className={cn("h-3.5 w-3.5 shrink-0", currentProjectScope && "text-primary")} />
            <span className="truncate">{projectLabel}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          side="bottom"
          sideOffset={8}
          className="w-[min(25rem,calc(100vw-2rem))] rounded-[22px]"
        >
          <DropdownMenuItem
            onSelect={() => applyProjectPath(defaultScope.project_path, defaultScope.project_name)}
            className="flex min-h-[48px] cursor-default gap-3 rounded-[16px] px-3 py-2.5 focus:bg-muted/55"
          >
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[12px] bg-muted text-foreground/80">
              <Folder className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-semibold text-foreground">
                {t("workspace.dialog.defaultProject")}
              </span>
              <span className="block truncate text-[11.5px] text-muted-foreground">
                {shortWorkspacePath(defaultScope.project_path)}
              </span>
            </span>
            {!currentProjectScope ? <Check className="h-4 w-4 text-foreground/80" /> : null}
          </DropdownMenuItem>
          <div className="my-1 h-px bg-border/45" />
          <div
            className="space-y-1.5 px-1.5 py-1.5"
            onKeyDown={(event) => {
              if (event.key !== "Escape") event.stopPropagation();
            }}
          >
            <form
              className="flex items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                applyProjectPath(pathDraft);
              }}
            >
              <div
                className={cn(
                  "flex h-9 min-w-0 flex-1 items-center overflow-hidden rounded-full border border-border/55 bg-background/80",
                  "focus-within:ring-1 focus-within:ring-foreground/10",
                )}
              >
                <span className="max-w-[58%] shrink-0 truncate border-r border-border/45 px-3 font-mono text-[11.5px] text-muted-foreground">
                  {workspaceRootLabel}/
                </span>
                <input
                  value={pathDraft}
                  disabled={disabled}
                  onChange={(event) => {
                    setPathDraft(event.target.value);
                    setPathError(null);
                  }}
                  placeholder={t("workspace.dialog.manualPlaceholder")}
                  aria-label={t("workspace.dialog.manual")}
                  className="h-full min-w-0 flex-1 bg-transparent px-3 text-[12.5px] outline-none placeholder:text-muted-foreground/65 disabled:cursor-not-allowed"
                />
              </div>
              <Button
                type="submit"
                disabled={disabled}
                className="h-9 shrink-0 rounded-full px-3 text-[12px]"
              >
                {t("workspace.dialog.usePath")}
              </Button>
            </form>
            {pathError || error ? (
              <p role="alert" className="px-1 text-[11.5px] font-medium text-destructive">
                {pathError ?? error}
              </p>
            ) : null}
          </div>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
