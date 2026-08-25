import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getGitHubConnectionMock = vi.fn();
const connectGitHubMock = vi.fn();
const disconnectGitHubMock = vi.fn();
const getLatestGitHubIngestionMock = vi.fn();
const listGitHubRepositoriesMock = vi.fn();
const startGitHubIngestionMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getGitHubConnection: (...args: unknown[]) =>
      getGitHubConnectionMock(...args),
    connectGitHub: (...args: unknown[]) => connectGitHubMock(...args),
    disconnectGitHub: (...args: unknown[]) => disconnectGitHubMock(...args),
    getLatestGitHubIngestion: (...args: unknown[]) =>
      getLatestGitHubIngestionMock(...args),
    listGitHubRepositories: (...args: unknown[]) =>
      listGitHubRepositoriesMock(...args),
    startGitHubIngestion: (...args: unknown[]) =>
      startGitHubIngestionMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";

import GitHubSection, { importSummary } from "./github-section";

const ACCESS_TOKEN = "tok";

function connection(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "user-1",
    username: "Octocat",
    github_user_id: 583231,
    public_repo_count: 8,
    last_verified_at: "2026-08-24T10:00:00Z",
    created_at: "2026-08-24T10:00:00Z",
    updated_at: "2026-08-24T10:00:00Z",
    ...overrides,
  };
}

function renderSection(onWorkComplete?: () => void) {
  return render(
    <GitHubSection
      accessToken={ACCESS_TOKEN}
      onWorkComplete={onWorkComplete}
    />,
  );
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    user_id: "user-1",
    status: "succeeded" as const,
    repositories_available: 12,
    repositories_forks_excluded: 0,
    repositories_total: 12,
    repositories_completed: 12,
    repositories_failed: 0,
    error_message: null,
    started_at: "2026-08-24T10:00:00Z",
    finished_at: "2026-08-24T10:01:00Z",
    created_at: "2026-08-24T10:00:00Z",
    updated_at: "2026-08-24T10:01:00Z",
    ...overrides,
  };
}

function repository(overrides: Record<string, unknown> = {}) {
  return {
    id: "repo-1",
    github_repo_id: 1,
    name: "alpha",
    full_name: "octocat/alpha",
    description: "A small tool",
    is_fork: false,
    is_archived: false,
    primary_language: "Python",
    stargazers_count: 5,
    forks_count: 1,
    pushed_at: "2026-08-20T10:00:00Z",
    languages: [{ language: "Python", byte_count: 900 }],
    topics: ["cli"],
    has_readme: true,
    detail_fetched: true,
    updated_at: "2026-08-24T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  getGitHubConnectionMock.mockReset();
  connectGitHubMock.mockReset();
  disconnectGitHubMock.mockReset();
  getLatestGitHubIngestionMock.mockReset();
  listGitHubRepositoriesMock.mockReset();
  startGitHubIngestionMock.mockReset();
  // Sensible defaults so the Prompt 3.1 tests below don't each have to
  // stub the Prompt 3.2 calls.
  getLatestGitHubIngestionMock.mockResolvedValue(null);
  listGitHubRepositoriesMock.mockResolvedValue([]);
});

describe("github section", () => {
  it("shows the connect form when nothing is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    expect(await screen.findByLabelText("GitHub username")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("shows the public-data-only notice before anything is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    expect(
      await screen.findByText(/We only analyze public data/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/never asks for your GitHub password/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/never requests access to your private repositories/i),
    ).toBeInTheDocument();
  });

  it("keeps the public-data-only notice visible after connecting", async () => {
    // The promise is the point: a user who has already connected is
    // exactly the person who might later wonder what was shared. The
    // notice is not a one-time onboarding message.
    getGitHubConnectionMock.mockResolvedValue(connection());

    renderSection();

    expect(
      await screen.findByText(/We only analyze public data/i),
    ).toBeInTheDocument();
  });

  it("describes the username field with the public-data notice", async () => {
    // aria-describedby, so the promise reaches a screen-reader user as
    // part of the field rather than as unassociated text nearby.
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();

    const input = await screen.findByLabelText("GitHub username");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      /We only analyze public data/i,
    );
  });

  it("never renders a password field", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    const { container } = renderSection();
    await screen.findByLabelText("GitHub username");

    expect(container.querySelector('input[type="password"]')).toBeNull();
    // A browser must not offer to autofill a credential into the
    // username box either.
    expect(screen.getByLabelText("GitHub username")).toHaveAttribute(
      "autocomplete",
      "off",
    );
  });

  it("connects a username and shows the account", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockResolvedValue(connection());

    renderSection();
    const input = await screen.findByLabelText("GitHub username");

    fireEvent.change(input, { target: { value: "octocat" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Octocat")).toBeInTheDocument();
    expect(connectGitHubMock).toHaveBeenCalledWith(ACCESS_TOKEN, "octocat");
    expect(screen.getByText("8 public repositories")).toBeInTheDocument();
    expect(screen.getByText(/Last checked/)).toBeInTheDocument();
  });

  it("links to the connected account on github.com", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());

    renderSection();

    const link = await screen.findByRole("link", { name: "Octocat" });
    expect(link).toHaveAttribute("href", "https://github.com/Octocat");
  });

  it("uses the singular form for a single repository", async () => {
    getGitHubConnectionMock.mockResolvedValue(
      connection({ public_repo_count: 1 }),
    );

    renderSection();

    expect(await screen.findByText("1 public repository")).toBeInTheDocument();
  });

  it("shows the API's message when the account does not exist", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(404, "no public GitHub account found for 'nope'"),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no public GitHub account found for 'nope'",
    );
    // Still on the form — nothing was connected.
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("shows the API's message when GitHub is rate-limited", async () => {
    // The reset time is the actionable part, so it must survive to the
    // user rather than being flattened into "something went wrong".
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(
        503,
        "GitHub's public API rate limit was reached. Try again after 15:30 UTC.",
      ),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "octocat" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Try again after 15:30 UTC.",
    );
  });

  it("shows the API's message when GitHub times out", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);
    connectGitHubMock.mockRejectedValue(
      new ApiError(
        504,
        "GitHub did not respond in time — try again in a moment",
      ),
    );

    renderSection();
    fireEvent.change(await screen.findByLabelText("GitHub username"), {
      target: { value: "octocat" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "GitHub did not respond in time",
    );
  });

  it("disconnects and returns to the connect form", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    disconnectGitHubMock.mockResolvedValue(undefined);

    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => {
      expect(screen.getByLabelText("GitHub username")).toBeInTheDocument();
    });
    expect(disconnectGitHubMock).toHaveBeenCalledWith(ACCESS_TOKEN);
    expect(screen.queryByText("Octocat")).not.toBeInTheDocument();
  });

  it("keeps the account visible when disconnecting fails", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    disconnectGitHubMock.mockRejectedValue(
      new ApiError(503, "service unavailable"),
    );

    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "service unavailable",
    );
    expect(screen.getByText("Octocat")).toBeInTheDocument();
  });

  it("surfaces an error when the initial load fails", async () => {
    getGitHubConnectionMock.mockRejectedValue(
      new ApiError(500, "server error"),
    );

    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent("server error");
  });

  it("does not submit an empty username", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();
    await screen.findByLabelText("GitHub username");

    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled();
    expect(connectGitHubMock).not.toHaveBeenCalled();
  });
});

describe("import summary copy", () => {
  // THE POINT OF THESE TESTS: a user with 47 public repositories who is
  // told "Imported 20 repositories" will reasonably conclude they have
  // 20. The capped message must lead with the real total and name the
  // selection rule.
  it("says 'all' when nothing was capped", () => {
    expect(
      importSummary(
        run({
          repositories_available: 12,
          repositories_forks_excluded: 0,
          repositories_total: 12,
        }),
      ),
    ).toBe("Imported all 12 of your public repositories.");
  });

  it("never implies the cap is the account's size", () => {
    const summary = importSummary(
      run({
        repositories_available: 47,
        repositories_forks_excluded: 0,
        repositories_total: 20,
      }),
    );
    expect(summary).toBe(
      "Imported the 20 most recently updated of your 47 public repositories.",
    );
    expect(summary).toContain("47");
  });

  it("reports forks separately from the cap", () => {
    expect(
      importSummary(
        run({
          repositories_available: 52,
          repositories_forks_excluded: 5,
          repositories_total: 20,
        }),
      ),
    ).toBe(
      "Imported the 20 most recently updated of your 47 public repositories. 5 forks were not included.",
    );
  });

  it("explains forks when nothing was capped", () => {
    expect(
      importSummary(
        run({
          repositories_available: 15,
          repositories_forks_excluded: 3,
          repositories_total: 12,
        }),
      ),
    ).toBe(
      "Imported all 12 of your public repositories. 3 forks were not included.",
    );
  });

  it("adds partial failures without calling the import a failure", () => {
    expect(
      importSummary(
        run({
          repositories_available: 12,
          repositories_forks_excluded: 0,
          repositories_total: 12,
          repositories_failed: 2,
        }),
      ),
    ).toContain("2 could not be fully imported");
  });

  it("handles an account with no public repositories", () => {
    expect(
      importSummary(
        run({
          repositories_available: 0,
          repositories_forks_excluded: 0,
          repositories_total: 0,
        }),
      ),
    ).toBe("No public repositories found to import.");
  });

  it("uses the singular for one repository", () => {
    expect(
      importSummary(
        run({
          repositories_available: 1,
          repositories_forks_excluded: 0,
          repositories_total: 1,
        }),
      ),
    ).toBe("Imported all 1 of your public repository.");
  });
});

describe("github import", () => {
  it("offers an import once an account is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());

    renderSection();

    expect(
      await screen.findByRole("button", { name: "Import public repositories" }),
    ).toBeInTheDocument();
  });

  it("starts an import and shows progress", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    startGitHubIngestionMock.mockResolvedValue(
      run({
        status: "processing",
        repositories_available: null,
        repositories_total: null,
        repositories_completed: 0,
      }),
    );

    renderSection();
    fireEvent.click(
      await screen.findByRole("button", { name: "Import public repositories" }),
    );

    // repositories_total is null until the listing finishes — that null
    // is a real state, not a zero.
    expect(
      await screen.findByText("Finding your public repositories…"),
    ).toBeInTheDocument();
    expect(startGitHubIngestionMock).toHaveBeenCalledWith(ACCESS_TOKEN);
  });

  it("shows counted progress once the total is known", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({
        status: "processing",
        repositories_available: 20,
        repositories_total: 20,
        repositories_completed: 7,
      }),
    );

    renderSection();

    expect(
      await screen.findByText("Importing… 7 of 20 repositories"),
    ).toBeInTheDocument();
  });

  it("shows the capped summary after a finished import", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({
        repositories_available: 47,
        repositories_forks_excluded: 0,
        repositories_total: 20,
        repositories_completed: 20,
      }),
    );

    renderSection();

    expect(
      await screen.findByText(
        "Imported the 20 most recently updated of your 47 public repositories.",
      ),
    ).toBeInTheDocument();
  });

  it("offers a re-import after a finished run", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(run());

    renderSection();

    expect(
      await screen.findByRole("button", { name: "Re-import repositories" }),
    ).toBeInTheDocument();
  });

  it("shows the failure reason from the API", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({
        status: "failed",
        error_message: "GitHub could not be reached — try importing again",
      }),
    );

    renderSection();

    expect(
      await screen.findByText(
        "GitHub could not be reached — try importing again",
      ),
    ).toBeInTheDocument();
  });

  it("surfaces the API message when an import cannot be started", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    startGitHubIngestionMock.mockRejectedValue(
      new ApiError(409, "an import is already in progress"),
    );

    renderSection();
    fireEvent.click(
      await screen.findByRole("button", { name: "Import public repositories" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "an import is already in progress",
    );
  });

  it("lists imported repositories", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(run());
    listGitHubRepositoriesMock.mockResolvedValue([repository()]);

    renderSection();

    const link = await screen.findByRole("link", { name: "alpha" });
    expect(link).toHaveAttribute("href", "https://github.com/octocat/alpha");
    expect(screen.getByText("A small tool")).toBeInTheDocument();
    expect(screen.getByText("cli")).toBeInTheDocument();
  });

  it("marks a repository that was only partially imported", async () => {
    // A fork, or one beyond the cap: it must not look complete.
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(run());
    listGitHubRepositoriesMock.mockResolvedValue([
      repository({ is_fork: true, detail_fetched: false }),
    ]);

    renderSection();

    expect(await screen.findByText(/Basic details only/)).toBeInTheDocument();
  });

  it("keeps the public-data notice visible throughout an import", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "processing", repositories_total: 5 }),
    );

    renderSection();

    expect(
      await screen.findByText(/We only analyze public data/i),
    ).toBeInTheDocument();
  });

  it("does not offer an import before an account is connected", async () => {
    getGitHubConnectionMock.mockResolvedValue(null);

    renderSection();
    await screen.findByLabelText("GitHub username");

    expect(
      screen.queryByRole("button", { name: "Import public repositories" }),
    ).not.toBeInTheDocument();
    expect(getLatestGitHubIngestionMock).not.toHaveBeenCalled();
  });
});

// --- import completion notification (auto-refresh coordination) ------

describe("github import completion notification", () => {
  it("notifies once when an import reaches a terminal state", async () => {
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock
      .mockResolvedValueOnce(run({ status: "processing" }))
      .mockResolvedValue(run({ status: "succeeded" }));
    listGitHubRepositoriesMock.mockResolvedValue([repository()]);

    renderSection(onWorkComplete);

    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1), {
      timeout: 4000,
    });
  }, 10000);

  it("notifies once on a failed import, without starting another", async () => {
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock
      .mockResolvedValueOnce(run({ status: "processing" }))
      .mockResolvedValue(
        run({ status: "failed", error_message: "GitHub could not be reached" }),
      );
    listGitHubRepositoriesMock.mockResolvedValue([]);

    renderSection(onWorkComplete);

    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1), {
      timeout: 4000,
    });
    // Reporting that a run ended must never trigger a new one.
    expect(startGitHubIngestionMock).not.toHaveBeenCalled();
  }, 10000);

  it("does not notify while a run is rate-limited and still processing", async () => {
    // A throttled run stays "processing" for as long as an hour. It has
    // produced nothing new, so it must not notify — and must not spin.
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "processing" }),
    );
    listGitHubRepositoriesMock.mockResolvedValue([]);

    renderSection(onWorkComplete);
    await waitFor(() =>
      expect(getLatestGitHubIngestionMock).toHaveBeenCalled(),
    );

    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(onWorkComplete).not.toHaveBeenCalled();
    expect(startGitHubIngestionMock).not.toHaveBeenCalled();
  }, 10000);

  it("does not notify for an already-finished run on mount", async () => {
    // No transition happened while we were watching, so there is nothing
    // new to refetch.
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "succeeded" }),
    );
    listGitHubRepositoriesMock.mockResolvedValue([repository()]);

    renderSection(onWorkComplete);
    await waitFor(() =>
      expect(getLatestGitHubIngestionMock).toHaveBeenCalled(),
    );

    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(onWorkComplete).not.toHaveBeenCalled();
  }, 10000);

  it("stops polling after unmount", async () => {
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "processing" }),
    );
    listGitHubRepositoriesMock.mockResolvedValue([]);

    const { unmount } = renderSection();
    await waitFor(() =>
      expect(getLatestGitHubIngestionMock).toHaveBeenCalled(),
    );
    unmount();

    getLatestGitHubIngestionMock.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(getLatestGitHubIngestionMock).not.toHaveBeenCalled();
  }, 10000);
});

// --- disconnect also notifies (a purge is not a status transition) ---

describe("github disconnect notification", () => {
  it("notifies on disconnect, so the skill sections refetch", async () => {
    // Disconnecting purges the GitHub-derived skill evidence. The
    // terminal-edge effect cannot see this: it fires on a run's status
    // TRANSITION, and disconnect sets `run` to null, which that guard
    // ignores by design.
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "succeeded" }),
    );
    listGitHubRepositoriesMock.mockResolvedValue([repository()]);
    disconnectGitHubMock.mockResolvedValue(undefined);

    renderSection(onWorkComplete);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1));
  });

  it("does not notify when the disconnect fails", async () => {
    const onWorkComplete = vi.fn();
    getGitHubConnectionMock.mockResolvedValue(connection());
    getLatestGitHubIngestionMock.mockResolvedValue(
      run({ status: "succeeded" }),
    );
    listGitHubRepositoriesMock.mockResolvedValue([repository()]);
    disconnectGitHubMock.mockRejectedValue(
      new ApiError(503, "service unavailable"),
    );

    renderSection(onWorkComplete);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(onWorkComplete).not.toHaveBeenCalled();
  });
});
