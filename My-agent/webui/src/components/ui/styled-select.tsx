import { ChevronDown } from "lucide-react";
import type { SelectHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function StyledSelect({
  className,
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="relative block">
      <select
        {...props}
        className={cn(
          "h-10 w-full appearance-none rounded-[10px] border border-border/55 bg-muted/35 px-3 pr-10 text-[13px] text-foreground",
          "outline-none transition-colors hover:bg-muted/50 focus:border-[#2997FF]/55 focus:ring-2 focus:ring-[#2997FF]/15",
          "disabled:cursor-not-allowed disabled:opacity-55",
          className,
        )}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden
      />
    </span>
  );
}
