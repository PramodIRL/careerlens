"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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

  // Set the instant an explicit login() starts, and never reset — see
  // login() below and docs/decisions.md. The mount-time refresh effect
  // checks this before applying its own result, so a login that
  // completes *while the initial silent refresh is still in flight*
  // can't be clobbered by that refresh settling later (with a stale
  // success *or* a stale failure — either would otherwise overwrite a
  // real, more recent session). A ref rather than state: reading it must
  // never itself cause a re-render, and it's only ever read from inside
  // the effect below, not from render output.
  const supersededRef = useRef(false);

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
        if (cancelled || supersededRef.current) return;
        await applySession(tokens.access_token);
      })
      .catch(() => {
        if (!cancelled && !supersededRef.current) {
          setStatus("unauthenticated");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [applySession]);

  const login = useCallback(
    async (email: string, password: string) => {
      supersededRef.current = true;
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
