"use client";

import { useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  connectGitHub,
  disconnectGitHub,
  getGitHubConnection,
  type GitHubConnectionResponse,
} from "@/lib/api-client";

interface GitHubSectionProps {
  accessToken: string;
}

// Rendered in BOTH the connected and not-connected states, always
// visible, never behind a hover or a disclosure. Same principle the
// skills section applies to evidence: the thing a user needs in order to
// trust the feature is not something to make them go looking for. It is
// also the input's accessible description (aria-describedby), so a
// screen-reader user hears it as part of the field rather than as
// decoration they might skip.
const PUBLIC_DATA_NOTICE_ID = "github-public-data-notice";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function PublicDataNotice() {
  return (
    <p
      id={PUBLIC_DATA_NOTICE_ID}
      className="rounded border border-zinc-300 bg-zinc-100 px-3 py-2 text-xs text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
    >
      <span className="font-semibold">We only analyze public data.</span>{" "}
      CareerLens never asks for your GitHub password and never requests access
      to your private repositories. We read only what anyone can see on your
      public profile.
    </p>
  );
}

export default function GitHubSection({ accessToken }: GitHubSectionProps) {
  const [connection, setConnection] = useState<GitHubConnectionResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGitHubConnection(accessToken)
      .then((current) => {
        if (!cancelled) setConnection(current);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "something went wrong",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  async function handleConnect(event: FormEvent) {
    event.preventDefault();
    const trimmed = username.trim();
    if (!trimmed) return;

    setBusy(true);
    setError(null);
    try {
      setConnection(await connectGitHub(accessToken, trimmed));
      setUsername("");
    } catch (err) {
      // The API's own message is the useful one here — it distinguishes
      // "no such account", "that's an organization", "GitHub timed out"
      // and "rate limited, try again after HH:MM", all of which need
      // different things from the user.
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setBusy(false);
    }
  }

  async function handleDisconnect() {
    setBusy(true);
    setError(null);
    try {
      await disconnectGitHub(accessToken);
      setConnection(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading GitHub connection…
      </p>
    );
  }

  return (
    <div className="flex w-full flex-col gap-3 text-left">
      {error && (
        <p
          role="alert"
          className="rounded bg-red-100 px-3 py-2 text-sm text-red-800"
        >
          {error}
        </p>
      )}

      <PublicDataNotice />

      {connection ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-start justify-between gap-3 rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700">
            <div className="min-w-0">
              <a
                href={`https://github.com/${connection.username}`}
                target="_blank"
                rel="noreferrer noopener"
                className="truncate text-sm font-medium text-black underline dark:text-zinc-50"
              >
                {connection.username}
              </a>
              <p className="text-xs text-zinc-500 dark:text-zinc-500">
                {connection.public_repo_count}{" "}
                {connection.public_repo_count === 1
                  ? "public repository"
                  : "public repositories"}
              </p>
              {/* The repository count is a snapshot, so it is always
                  shown next to when it was taken rather than presented
                  as a current figure. */}
              <p className="text-xs text-zinc-500 dark:text-zinc-500">
                Last checked {formatDate(connection.last_verified_at)}
              </p>
            </div>
            <button
              type="button"
              onClick={handleDisconnect}
              disabled={busy}
              className="shrink-0 rounded border border-red-300 px-2 py-1 text-xs text-red-700 disabled:opacity-50 dark:border-red-800 dark:text-red-400"
            >
              {busy ? "Disconnecting…" : "Disconnect"}
            </button>
          </div>
          <p className="text-xs text-zinc-500 dark:text-zinc-500">
            Connecting a different account replaces this one.
          </p>
        </div>
      ) : (
        <form onSubmit={handleConnect} className="flex flex-col gap-1">
          <label
            htmlFor="github-username"
            className="text-sm font-medium text-black dark:text-zinc-50"
          >
            GitHub username
          </label>
          <div className="flex gap-2">
            {/* Deliberately a plain text field. There is no password
                input anywhere in this flow, and autoComplete is off so a
                browser never offers to fill a credential into it. */}
            <input
              id="github-username"
              type="text"
              inputMode="text"
              autoComplete="off"
              spellCheck={false}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={busy}
              placeholder="octocat"
              aria-describedby={PUBLIC_DATA_NOTICE_ID}
              className="min-w-0 flex-1 rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
            />
            <button
              type="submit"
              disabled={busy || !username.trim()}
              className="shrink-0 rounded bg-black px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
            >
              {busy ? "Connecting…" : "Connect"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
