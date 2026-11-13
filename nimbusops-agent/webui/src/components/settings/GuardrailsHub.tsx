import { useState } from "react";
import { ScanSearch, Workflow } from "lucide-react";

import { ActionGuardrailsSettings } from "@/components/settings/ActionGuardrailsSettings";
import { PromptGuardrailsSettings } from "@/components/settings/PromptGuardrailsSettings";
import { cn } from "@/lib/utils";

type GuardrailsArea = "action" | "prompt-injection";

export function GuardrailsHub() {
  const [area, setArea] = useState<GuardrailsArea>("action");

  return (
    <div>
      <div className="mb-7 flex w-full gap-1 overflow-x-auto rounded-[11px] bg-muted/65 p-1">
        <HubTab
          active={area === "action"}
          icon={Workflow}
          label="Action Guardrails"
          onClick={() => setArea("action")}
        />
        <HubTab
          active={area === "prompt-injection"}
          icon={ScanSearch}
          label="Prompt inject Guardrails"
          onClick={() => setArea("prompt-injection")}
        />
      </div>

      {area === "action" ? <ActionGuardrailsSettings /> : null}
      {area === "prompt-injection" ? <PromptGuardrailsSettings /> : null}
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
  icon: typeof Workflow;
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
