import { useRef, useState, type DragEvent } from "react";
import { CheckCircle2, CircleAlert, FileArchive, Loader2, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const ZIP_MIME_TYPES = new Set([
  "application/zip",
  "application/x-zip-compressed",
  "application/octet-stream",
]);

export function ArchiveImportPanel({
  title,
  subtitle,
  busy,
  message,
  error,
  onImport,
}: {
  title: string;
  subtitle: string;
  busy: boolean;
  message: string | null;
  error: string | null;
  onImport: (file: File) => Promise<void>;
}) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const importFile = async (file: File | undefined) => {
    if (!file || busy) return;
    const validType = !file.type || ZIP_MIME_TYPES.has(file.type);
    if (!file.name.toLowerCase().endsWith(".zip") || !validType) {
      setLocalError(
        t("settings.archive.zipOnly", { defaultValue: "Only .zip archives are supported." }),
      );
      return;
    }
    setLocalError(null);
    await onImport(file);
    if (inputRef.current) inputRef.current.value = "";
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void importFile(event.dataTransfer.files[0]);
  };

  const visibleError = localError || error;

  return (
    <section className="overflow-hidden rounded-[16px] border border-border/45 bg-card/72 shadow-[0_10px_30px_rgba(15,23,42,0.045)]">
      <div className="flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-[11px] bg-muted text-muted-foreground">
            <FileArchive className="h-4 w-4" aria-hidden />
          </span>
          <div className="min-w-0">
            <h3 className="text-[13px] font-semibold leading-5 text-foreground">{title}</h3>
            <p className="truncate text-[12px] text-muted-foreground">{subtitle}</p>
          </div>
        </div>
        <Button
          type="button"
          size="sm"
          variant={open ? "default" : "outline"}
          onClick={() => setOpen((value) => !value)}
          className="h-8 shrink-0 rounded-full px-3 text-[12px] font-semibold"
        >
          <Upload className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          {t("settings.archive.importAction", { defaultValue: "Import" })}
        </Button>
      </div>

      {open ? (
        <div className="border-t border-border/35 bg-muted/18 px-3 py-3">
          <div
            role="button"
            tabIndex={0}
            data-testid="archive-drop-zone"
            aria-label={t("settings.archive.dropAria", {
              defaultValue: "Drop a zip archive here or choose a file",
            })}
            onClick={() => !busy && inputRef.current?.click()}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                if (!busy) inputRef.current?.click();
              }
            }}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={(event) => {
              if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
              setDragging(false);
            }}
            onDrop={onDrop}
            className={cn(
              "flex min-h-28 cursor-pointer flex-col items-center justify-center rounded-[12px] border border-dashed px-5 py-5 text-center transition-colors",
              dragging
                ? "border-foreground/45 bg-background"
                : "border-border/75 bg-background/65 hover:border-foreground/25 hover:bg-background",
              busy && "cursor-wait opacity-70",
            )}
          >
            {busy ? (
              <Loader2 className="mb-2 h-5 w-5 animate-spin text-muted-foreground" aria-hidden />
            ) : (
              <Upload className="mb-2 h-5 w-5 text-muted-foreground" aria-hidden />
            )}
            <p className="text-[13px] font-medium text-foreground">
              {busy
                ? t("settings.archive.validating", {
                    defaultValue: "Validating and installing...",
                  })
                : t("settings.archive.dropTitle", {
                    defaultValue: "Drop a ZIP archive here",
                  })}
            </p>
            <p className="mt-1 text-[12px] text-muted-foreground">
              {t("settings.archive.dropSubtitle", {
                defaultValue: "or click to choose a file · maximum 25 MB",
              })}
            </p>
            <input
              ref={inputRef}
              type="file"
              accept=".zip,application/zip"
              className="sr-only"
              disabled={busy}
              onChange={(event) => void importFile(event.target.files?.[0])}
            />
          </div>

          {visibleError ? (
            <div className="mt-3 flex items-start gap-2 text-[12.5px] text-destructive">
              <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{visibleError}</span>
            </div>
          ) : message ? (
            <div className="mt-3 flex items-start gap-2 text-[12.5px] text-emerald-700 dark:text-emerald-300">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{message}</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
