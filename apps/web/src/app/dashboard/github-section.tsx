"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  connectGitHub,
  disconnectGitHub,
  getGitHubConnection,
  getLatestGitHubIngestion,
  listGitHubRepositories,
  startGitHubIngestion,
  type GitHubConnectionResponse,
  type GitHubIngestionRunResponse,
  type GitHubRepositoryResponse,
  type IngestionStatus,
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

// How often to re-check an import while it is queued or processing.
// Matches ResumeSection's cadence; the effect stops entirely once the
// run reaches a terminal state.
const POLL_INTERVAL_MS = 2000;

const ACTIVE_STATUSES: IngestionStatus[] = ["queued", "processing"];

function isActive(run: GitHubIngestionRunResponse | null): boolean {
  return run !== null && ACTIVE_STATUSES.includes(run.status);
}

/** What to say about a finished import.
 *
 * THE POINT OF THIS FUNCTION is the capped case. A user with 47 public
 * repositories who is told "Imported 20 repositories" will reasonably
 * conclude they have 20. So when the cap was hit, the message leads
 * with the total and names the selection rule; when it was not hit,
 * there is no cap language at all, because for most accounts the cap is
 * simply not a fact about them.
 *
 * Forks are reported separately: they are excluded from importing, so
 * they explain the gap between "your account has N" and "we imported M"
 * without being confused with the cap. */
export function importSummary(run: GitHubIngestionRunResponse): string {
  const available = run.repositories_available ?? 0;
  const forks = run.repositories_forks_excluded ?? 0;
  const total = run.repositories_total ?? 0;
  const own = Math.max(available - forks, 0);

  const parts: string[] = [];
  if (own === 0) {
    parts.push("No public repositories found to import.");
  } else if (total < own) {
    parts.push(
      `Imported the ${total} most recently updated of your ${own} public repositories.`,
    );
  } else {
    parts.push(
      `Imported all ${total} of your public ${own === 1 ? "repository" : "repositories"}.`,
    );
  }

  if (forks > 0) {
    parts.push(
      `${forks} ${forks === 1 ? "fork was" : "forks were"} not included.`,
    );
  }
  if (run.repositories_failed > 0) {
    parts.push(
      `${run.repositories_failed} could not be fully imported — try again later.`,
    );
  }
  return parts.join(" ");
}

/** Progress text while a run is still going. `repositories_total` is
 * null until the listing finishes, and that null is a real state —
 * "still working out how much there is" — not a zero. */
function progressLabel(run: GitHubIngestionRunResponse): string {
  if (run.repositories_total === null) {
    return "Finding your public repositories…";
  }
  return `Importing… ${run.repositories_completed} of ${run.repositories_total} repositories`;
}

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
  const [run, setRun] = useState<GitHubIngestionRunResponse | null>(null);
  const [repositories, setRepositories] = useState<GitHubRepositoryResponse[]>(
    [],
  );
  const [importing, setImporting] = useState(false);

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

  // Deliberately side-effect free: it fetches and returns, and every
  // setState happens in a .then callback. Calling a setState-ing
  // function straight from an effect body triggers cascading renders,
  // which eslint's react-hooks/set-state-in-effect rightly rejects.
  //
  // Both are fetched together so the summary and the repository list
  // always describe the same moment.
  const fetchIngestion = useCallback(
    () =>
      Promise.all([
        getLatestGitHubIngestion(accessToken),
        listGitHubRepositories(accessToken),
      ]),
    [accessToken],
  );

  useEffect(() => {
    if (!connection) return;
    let cancelled = false;
    fetchIngestion()
      .then(([latest, repos]) => {
        if (cancelled) return;
        setRun(latest);
        setRepositories(repos);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "something went wrong",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [connection, fetchIngestion]);

  // Polls while an import is queued or processing, and stops as soon as
  // it isn't — including immediately, when there is nothing running.
  useEffect(() => {
    if (!isActive(run)) return;
    let cancelled = false;
    const timer = setInterval(() => {
      fetchIngestion()
        .then(([latest, repos]) => {
          if (cancelled) return;
          setRun(latest);
          setRepositories(repos);
        })
        .catch((err) => {
          if (!cancelled) {
            setError(
              err instanceof ApiError ? err.message : "something went wrong",
            );
          }
        });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [run, fetchIngestion]);

  async function handleImport() {
    setImporting(true);
    setError(null);
    try {
      setRun(await startGitHubIngestion(accessToken));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setImporting(false);
    }
  }

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
      // The server deletes the imported repositories and runs along with
      // the connection, so the view must not keep showing them.
      setRun(null);
      setRepositories([]);
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

          <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
            {run && isActive(run) ? (
              <p
                role="status"
                className="text-sm text-blue-600 dark:text-blue-400"
              >
                {progressLabel(run)}
              </p>
            ) : (
              <button
                type="button"
                onClick={handleImport}
                disabled={importing || busy}
                className="self-start rounded bg-black px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
              >
                {importing
                  ? "Starting…"
                  : run
                    ? "Re-import repositories"
                    : "Import public repositories"}
              </button>
            )}

            {run && run.status === "succeeded" && (
              <p className="text-xs text-zinc-600 dark:text-zinc-400">
                {importSummary(run)}
              </p>
            )}

            {run && run.status === "failed" && (
              <p className="text-xs text-red-700 dark:text-red-400">
                {run.error_message ?? "The import failed — try again."}
              </p>
            )}

            {repositories.length > 0 && (
              <ul className="mt-1 flex flex-col gap-2">
                {repositories.map((repository) => (
                  <li
                    key={repository.id}
                    className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <a
                        href={`https://github.com/${repository.full_name}`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="truncate text-sm font-medium text-black underline dark:text-zinc-50"
                      >
                        {repository.name}
                      </a>
                      <span className="shrink-0 text-xs text-zinc-500 dark:text-zinc-500">
                        ★ {repository.stargazers_count}
                      </span>
                    </div>
                    {repository.description && (
                      <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                        {repository.description}
                      </p>
                    )}
                    <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
                      {repository.primary_language ?? "No language detected"}
                      {repository.is_fork && " · Fork"}
                      {/* Says plainly when a repository was listed but
                          not fully read — a fork, or beyond the import
                          cap — rather than letting it look complete. */}
                      {!repository.detail_fetched && " · Basic details only"}
                    </p>
                    {repository.topics.length > 0 && (
                      <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
                        {repository.topics.join(" · ")}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
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
