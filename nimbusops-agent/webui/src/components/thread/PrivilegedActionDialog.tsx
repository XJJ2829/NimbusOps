import { KeyRound, ShieldCheck, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { PrivilegedActionApproval } from "@/lib/types";

export function PrivilegedActionDialog({
  approval,
  onDecision,
}: {
  approval: PrivilegedActionApproval | null;
  onDecision: (approved: boolean) => void;
}) {
  return (
    <Dialog
      open={approval !== null}
      onOpenChange={(open) => {
        if (!open && approval) onDecision(false);
      }}
    >
      <DialogContent className="min-w-0 max-h-[calc(100vh-2rem)] max-w-xl overflow-x-hidden overflow-y-auto">
        <DialogHeader className="min-w-0">
          <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-[14px] bg-amber-500/12 text-amber-700 dark:text-amber-300">
            <KeyRound className="h-5 w-5" aria-hidden />
          </div>
          <DialogTitle>请求执行一次 root 操作</DialogTitle>
          <DialogDescription>
            NimbusOps 不会把 root 权限交给 Agent。批准后，独立特权执行器只会执行下面这一项已登记动作。
          </DialogDescription>
        </DialogHeader>

        {approval ? (
          <div className="min-w-0 space-y-3">
            <div className="rounded-[12px] border border-amber-500/25 bg-amber-500/8 px-3 py-3">
              <div className="flex items-start gap-2">
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" />
                <div>
                  <div className="text-[13px] font-semibold">{approval.action.label}</div>
                  <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
                    {approval.action.description || approval.action.impact}
                  </p>
                </div>
              </div>
            </div>

            <DetailRow label="动作标识" value={approval.action_name} mono />
            <DetailRow label="申请原因" value={approval.reason || "未提供"} />
            <DetailRow
              label="申请账号"
              value={`${approval.requested_by.display_name}（${approval.requested_by.username}）`}
            />

            <div className="min-w-0">
              <p className="mb-2 text-[12px] font-medium text-muted-foreground">动作参数</p>
              <pre className="max-h-[260px] min-w-0 max-w-full overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-all rounded-[12px] border border-border/55 bg-muted/35 p-3 font-mono text-[12px] leading-5 text-foreground">
                {JSON.stringify(approval.parameters, null, 2)}
              </pre>
            </div>

            <div className="flex items-center gap-2 text-[11.5px] text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
              无 root Shell，不接受任意命令，批准仅对本次请求有效
            </div>
          </div>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onDecision(false)}>
            拒绝执行
          </Button>
          <Button type="button" onClick={() => onDecision(true)}>
            批准本次操作
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DetailRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-center gap-3 rounded-[12px] border border-border/55 bg-muted/35 px-3 py-2.5">
      <span className="shrink-0 text-[12px] text-muted-foreground">{label}</span>
      <strong className={`ml-auto min-w-0 break-all text-right text-[13px] ${mono ? "font-mono" : ""}`}>
        {value}
      </strong>
    </div>
  );
}
