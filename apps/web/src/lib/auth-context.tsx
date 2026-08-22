"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import * as api from "@/lib/api-client";
import type { UserResponse } from "@/lib/api-client";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

interface AuthContextValue {
  status: AuthStatus;
  user: UserResponse | null;
  accessToken: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<UserResponse | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);

  const applySession = useCallback(async (token: string) => {
    const currentUser = await api.getCurrentUser(token);
    setAccessToken(token);
    setUser(currentUser);
    setStatus("authenticated");
  }, []);

  // On mount (including a full page reload), try to silently exchange
  // the HttpOnly refresh cookie — if the browser still has one — for a
  // fresh access token, so a reload doesn't look logged-out. Neither
  // token nor cookie value ever passes through application state here;
  // the cookie is handled entirely by the browser.
  useEffect(() => {
    let cancelled = false;

    api
      .refresh()
      .then(async (tokens) => {
        if (cancelled) return;
        await applySession(tokens.access_token);
      })
      .catch(() => {
        if (!cancelled) {
          setStatus("unauthenticated");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [applySession]);

  const login = useCallback(
    async (email: string, password: string) => {
      const tokens = await api.login(email, password);
      await applySession(tokens.access_token);
    },
    [applySession],
  );

  const register = useCallback(async (email: string, password: string) => {
    await api.register(email, password);
  }, []);

  const logout = useCallback(async () => {
    // Best-effort: even if the request fails (e.g. already logged out
    // server-side), clear local state so the UI reflects "logged out".
    await api.logout().catch(() => undefined);
    setAccessToken(null);
    setUser(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, accessToken, login, register, logout }),
    [status, user, accessToken, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
