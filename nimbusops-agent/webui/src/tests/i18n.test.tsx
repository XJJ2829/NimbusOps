import { describe, expect, it } from "vitest";

import { resources } from "@/i18n";
import { applyDocumentLocale, resolveInitialLocale } from "@/i18n/config";

const QUICK_ACTION_KEYS = ["plan", "analyze", "brainstorm", "code", "summarize", "more"];
const IMAGE_QUICK_ACTION_KEYS = ["icon", "sticker", "poster", "product", "portrait", "edit"];
const HERO_GREETING_KEYS = ["workOn", "start", "build", "tackle"];
const SLASH_COMMAND_KEYS = [
  "new",
  "stop",
  "restart",
  "status",
  "model",
  "history",
  "dream",
  "dream_log",
  "dream_restore",
  "goal",
  "help",
  "pairing",
];

describe("webui Simplified Chinese locale", () => {
  it("always starts in Simplified Chinese", () => {
    localStorage.setItem("nanobot.locale", "en");

    expect(resolveInitialLocale()).toBe("zh-CN");

    applyDocumentLocale(resolveInitialLocale());
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("keeps the landing page resources complete", () => {
    const empty = resources["zh-CN"].common.thread.empty;
    for (const key of HERO_GREETING_KEYS) {
      expect(empty.greetings[key as keyof typeof empty.greetings]).toBeTruthy();
    }
    for (const key of QUICK_ACTION_KEYS) {
      const action = empty.quickActions[key as keyof typeof empty.quickActions];
      expect(action.title).toBeTruthy();
      expect(action.prompt).toBeTruthy();
    }
    for (const key of IMAGE_QUICK_ACTION_KEYS) {
      const action = empty.imageQuickActions[key as keyof typeof empty.imageQuickActions];
      expect(action.title).toBeTruthy();
      expect(action.prompt).toBeTruthy();
    }
  });

  it("keeps slash commands complete", () => {
    const slash = resources["zh-CN"].common.thread.composer.slash;
    expect(slash.badges.current).toBeTruthy();
    expect(slash.badges.recent).toBeTruthy();
    expect(slash.details.stopRunning).toBeTruthy();
    for (const key of SLASH_COMMAND_KEYS) {
      const command = slash.commands[key as keyof typeof slash.commands];
      expect(command.title).toBeTruthy();
      expect(command.description).toBeTruthy();
    }
  });

  it("keeps core settings copy in Simplified Chinese", () => {
    const settings = resources["zh-CN"].common.settings;

    expect(settings.nav.browser).toBe("网页");
    expect(settings.sections.webSearch).toBe("网页搜索");
    expect(settings.byok.tabs.webSearch).toBe("网页搜索");
    expect(settings.overview.webSearch).toBe("网页搜索");
    expect(settings.overview.workspace).toBe("工作区");
  });
});
