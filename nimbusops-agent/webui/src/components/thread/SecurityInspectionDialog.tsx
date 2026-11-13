import { LockKeyhole, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { SecurityInspection } from "@/lib/types";

export function SecurityInspectionDialog({
  inspection,
  onDecision,
}: {
  inspection: SecurityInspection | null;
  onDecision: (approved: boolean) => void;
}) {
  return (
    <Dialog
      open={inspection !== null}
      onOpenChange={(open) => {
        if (!open && inspection) onDecision(false);
      }}
    >
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-xl overflow-x-hidden overflow-y-auto">
        <DialogHeader className="min-w-0">
          <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-[14px] bg-amber-500/12 text-amber-700 dark:text-amber-300">
            <ShieldAlert className="h-5 w-5" aria-hidden />
          </div>
          <DialogTitle>OpsShield 请求人工确认</DialogTitle>
          <DialogDescription>
            OpsShield 策略“{inspection?.rule_id}”拦截了这个动作。只有明确批准后，工具才会执行。
          </DialogDescription>
        </DialogHeader>

        {inspection ? (
          <div className="min-w-0 space-y-3">
            <div className="flex min-w-0 items-center gap-2 rounded-[12px] border border-border/55 bg-muted/35 px-3 py-2.5">
              <LockKeyhole className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="shrink-0 text-[12px] text-muted-foreground">动作</span>
              <strong className="ml-auto min-w-0 break-all text-right font-mono text-[13px] font-semibold text-foreground">
                {inspection.action.name}
              </strong>
            </div>
            <div className="min-w-0">
              <p className="mb-2 text-[12px] font-medium text-muted-foreground">工具参数</p>
              <pre className="max-h-[320px] min-w-0 max-w-full overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-all rounded-[12px] border border-border/55 bg-muted/35 p-3 font-mono text-[12px] leading-5 text-foreground">
                {JSON.stringify(inspection.action.input, null, 2)}
              </pre>
            </div>
          </div>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onDecision(false)}>
            拒绝执行
          </Button>
          <Button type="button" onClick={() => onDecision(true)}>
            批准并继续
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
