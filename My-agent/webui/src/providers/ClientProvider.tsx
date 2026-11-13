import { createContext, useContext, type ReactNode } from "react";

import type { NanobotClient } from "@/lib/nanobot-client";
import type { AuthUser } from "@/lib/types";

interface ClientContextValue {
  client: NanobotClient;
  token: string;
  modelName: string | null;
  user: AuthUser;
  hasPermission: (permission: string) => boolean;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export function ClientProvider({
  client,
  token,
  modelName = null,
  user,
  children,
}: {
  client: NanobotClient;
  token: string;
  modelName?: string | null;
  user: AuthUser;
  children: ReactNode;
}) {
  const hasPermission = (permission: string) => user.permissions.includes(permission);
  return (
    <ClientContext.Provider value={{ client, token, modelName, user, hasPermission }}>
      {children}
    </ClientContext.Provider>
  );
}

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}
