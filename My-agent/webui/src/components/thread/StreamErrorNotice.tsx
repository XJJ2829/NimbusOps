import { AlertTriangle, ShieldCheck, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { StreamError } from "@/lib/nanobot-client";

interface StreamErrorNoticeProps {
  error: StreamError;
  onDismiss: () => void;
  className?: string;
}

/**
 * Dismissible banner that surfaces transport-level faults the user needs to
 * know about. Rendered above the composer so the message the fault referred
 * to remains in view just above. ``role="alert"`` + ``aria-live="assertive"``
 * ensures screen readers announce the failure.
 */
export function StreamErrorNotice({ error, onDismiss, className }: StreamErrorNoticeProps) {
  const { t } = useTranslation();

  const { title, body } = resolveCopy(error, t);
  const isPromptGuardrailsBlock = error.kind === "prompt_guardrails_blocked";
  const Icon = isPromptGuardrailsBlock ? ShieldCheck : AlertTriangle;

  return (
    <div
      role="alert"
      aria-live="assertive"
      className={cn(
        "mb-2 flex w-full items-start gap-2 rounded-lg border px-3 py-2 text-[12px] leading-5",
        "mx-auto",
        "animate-in fade-in-0 slide-in-from-bottom-1",
        className,
        isPromptGuardrailsBlock
          ? "border-blue-500/25 bg-blue-50 text-blue-900 dark:bg-blue-950/40 dark:text-blue-100"
          : "border-destructive/30 bg-destructive/10 text-destructive",
      )}
    >
      <Icon
        className="mt-0.5 h-4 w-4 shrink-0"
        aria-hidden
      />
      <div className="flex-1">
        <p className="font-medium">{title}</p>
        <p
          className={cn(
            "mt-0.5",
            isPromptGuardrailsBlock
              ? "text-blue-900/75 dark:text-blue-100/75"
              : "text-destructive/80",
          )}
        >
          {body}
        </p>
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={onDismiss}
        aria-label={t("common.dismiss")}
        className={cn(
          "h-6 w-6 shrink-0",
          isPromptGuardrailsBlock
            ? "text-blue-900 hover:bg-blue-100 hover:text-blue-950 dark:text-blue-100 dark:hover:bg-blue-900/45 dark:hover:text-white"
            : "text-destructive hover:bg-destructive/15 hover:text-destructive",
        )}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

function resolveCopy(
  error: StreamError,
  t: (key: string) => string,
): { title: string; body: string } {
  switch (error.kind) {
    case "message_too_big":
      return {
        title: t("errors.messageTooBig.title"),
        body: t("errors.messageTooBig.body"),
      };
    case "workspace_scope_rejected":
      return {
        title: t("errors.workspaceScopeRejected.title"),
        body: t("errors.workspaceScopeRejected.body"),
      };
    case "prompt_guardrails_blocked":
      return {
        title: t("errors.promptGuardrailsBlocked.title"),
        body: error.message || (
          error.source === "indirect"
            ? t("errors.promptGuardrailsBlocked.indirectBody")
            : t("errors.promptGuardrailsBlocked.body")
        ),
      };
    case "action_guardrails_budget_stopped":
      return {
        title: t("errors.actionGuardrailsBudgetStopped.title"),
        body: error.message || t("errors.actionGuardrailsBudgetStopped.body"),
      };
    case "server_error":
      return {
        title: t("errors.serverError.title"),
        body: error.message || t("errors.serverError.body"),
      };
    default: {
      // Exhaustiveness guard: if a new StreamError kind is added, TS will
      // complain here until we add a corresponding i18n branch.
      const _exhaustive: never = error;
      return { title: String(_exhaustive), body: "" };
    }
  }
}
