import { Gauge, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { StreamError } from "@/lib/nanobot-client";

type ActionGuardrailsBudgetError = Extract<
  StreamError,
  { kind: "action_guardrails_budget_stopped" }
>;

export function ActionGuardrailsBudgetDialog({
  error,
  onClose,
}: {
  error: ActionGuardrailsBudgetError | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();

  return (
    <Dialog
      open={error !== null}
      onOpenChange={(open) => {
        if (!open && error) onClose();
      }}
    >
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-xl overflow-x-hidden overflow-y-auto">
        <DialogHeader className="min-w-0">
          <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-[14px] bg-amber-500/12 text-amber-700 dark:text-amber-300">
            <ShieldAlert className="h-5 w-5" aria-hidden />
          </div>
          <DialogTitle>{t("errors.actionGuardrailsBudgetStopped.title")}</DialogTitle>
          <DialogDescription>
            {t("errors.actionGuardrailsBudgetStopped.body")}
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <div className="flex min-w-0 items-start gap-2 rounded-[12px] border border-border/55 bg-muted/35 px-3 py-2.5">
            <Gauge className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-[12px] font-medium text-muted-foreground">
                {t("errors.actionGuardrailsBudgetStopped.reasonLabel")}
              </p>
              <p className="mt-1 whitespace-pre-wrap break-words text-[13px] leading-5 text-foreground">
                {error.message}
              </p>
            </div>
          </div>
        ) : null}

        <DialogFooter>
          <Button type="button" onClick={onClose}>
            {t("errors.actionGuardrailsBudgetStopped.dismiss")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
