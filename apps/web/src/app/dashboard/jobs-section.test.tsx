import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const listSavedJobsMock = vi.fn();
const createSavedJobMock = vi.fn();
const updateSavedJobMock = vi.fn();
const deleteSavedJobMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    listSavedJobs: (...args: unknown[]) => listSavedJobsMock(...args),
    createSavedJob: (...args: unknown[]) => createSavedJobMock(...args),
    updateSavedJob: (...args: unknown[]) => updateSavedJobMock(...args),
    deleteSavedJob: (...args: unknown[]) => deleteSavedJobMock(...args),
  };
});

import { ApiError } from "@/lib/api-client";
import JobsSection from "./jobs-section";

const ACCESS_TOKEN = "tok";

function job(overrides: Record<string, unknown> = {}) {
  return {
    id: "job-1",
    company: "Fictional Widgets Ltd",
    title: "Junior Backend Engineer",
    location: "Springfield",
    employment_type: "full_time" as const,
    source_url: "https://example.com/jobs/42",
    description: "Build and test internal web services.",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderSection() {
  return render(<JobsSection accessToken={ACCESS_TOKEN} />);
}

async function fillCreateForm() {
  fireEvent.change(screen.getByLabelText("Company"), {
    target: { value: "Acme" },
  });
  fireEvent.change(screen.getByLabelText("Job title"), {
    target: { value: "Engineer" },
  });
  fireEvent.change(screen.getByLabelText("Job description"), {
    target: { value: "Do the work." },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  listSavedJobsMock.mockResolvedValue([]);
  createSavedJobMock.mockReset();
  updateSavedJobMock.mockReset();
  deleteSavedJobMock.mockReset();
});

// --- loading / empty / list ------------------------------------------

describe("jobs section", () => {
  it("shows a loading state before the list arrives", () => {
    listSavedJobsMock.mockReturnValue(new Promise(() => {}));
    renderSection();
    expect(screen.getByRole("status")).toHaveTextContent(/loading saved jobs/i);
  });

  it("prompts for a first job when the list is empty", async () => {
    renderSection();
    expect(await screen.findByText(/no saved jobs yet/i)).toBeInTheDocument();
  });

  it("lists saved jobs with their details", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();

    expect(
      await screen.findByText("Junior Backend Engineer"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Fictional Widgets Ltd · Springfield · Full-time/),
    ).toBeInTheDocument();
  });

  it("links to the posting without leaking the referrer", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();

    const link = await screen.findByRole("link", { name: "View posting" });
    expect(link).toHaveAttribute("href", "https://example.com/jobs/42");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("omits the posting link when no url was saved", async () => {
    listSavedJobsMock.mockResolvedValue([job({ source_url: null })]);
    renderSection();

    await screen.findByText("Junior Backend Engineer");
    expect(
      screen.queryByRole("link", { name: "View posting" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a load failure instead of a blank section", async () => {
    listSavedJobsMock.mockRejectedValue(new ApiError(500, "server exploded"));
    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "server exploded",
    );
  });
});

// --- accessibility ----------------------------------------------------

describe("jobs form accessibility", () => {
  it("associates every control with a visible label", async () => {
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    // getByLabelText only resolves through a real label/control binding.
    expect(screen.getByLabelText("Company")).toBeInTheDocument();
    expect(screen.getByLabelText("Job title")).toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toBeInTheDocument();
    expect(screen.getByLabelText("Employment type")).toBeInTheDocument();
    expect(screen.getByLabelText("Link to posting")).toBeInTheDocument();
    expect(screen.getByLabelText("Job description")).toBeInTheDocument();
  });

  it("explains that the saved link is never opened", async () => {
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    const hint = screen.getByText(/we never open it/i);
    expect(screen.getByLabelText("Link to posting")).toHaveAttribute(
      "aria-describedby",
      hint.id,
    );
  });
});

// --- create -----------------------------------------------------------

describe("saving a job", () => {
  it("saves a job and shows it immediately", async () => {
    createSavedJobMock.mockResolvedValue(
      job({ id: "new", title: "Engineer", company: "Acme" }),
    );
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    expect(await screen.findByText("Engineer")).toBeInTheDocument();
    // No refetch needed — the response is the new row.
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);
  });

  it("sends blank optional fields as null, not empty strings", async () => {
    createSavedJobMock.mockResolvedValue(job());
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    await waitFor(() => expect(createSavedJobMock).toHaveBeenCalled());
    expect(createSavedJobMock).toHaveBeenCalledWith(ACCESS_TOKEN, {
      company: "Acme",
      title: "Engineer",
      description: "Do the work.",
      location: null,
      employment_type: null,
      source_url: null,
    });
  });

  it("keeps the submit button disabled until the required fields are filled", async () => {
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    expect(screen.getByRole("button", { name: "Save job" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Company"), {
      target: { value: "Acme" },
    });
    fireEvent.change(screen.getByLabelText("Job title"), {
      target: { value: "Engineer" },
    });

    expect(screen.getByRole("button", { name: "Save job" })).toBeEnabled();
  });

  it("prevents a duplicate submission while one is in flight", async () => {
    let resolve: ((value: unknown) => void) | undefined;
    createSavedJobMock.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    renderSection();
    await screen.findByText(/no saved jobs yet/i);
    await fillCreateForm();

    const button = screen.getByRole("button", { name: "Save job" });
    fireEvent.click(button);

    // Disabled while saving, so a second click cannot reach the API.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Saving…" }));
    expect(createSavedJobMock).toHaveBeenCalledTimes(1);

    resolve?.(job());
  });

  it("shows the API's validation message for a bad url", async () => {
    createSavedJobMock.mockRejectedValue(
      new ApiError(422, "source_url must be an http or https link"),
    );
    renderSection();
    await screen.findByText(/no saved jobs yet/i);

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "source_url must be an http or https link",
    );
  });

  it("keeps the existing list visible when saving fails", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    createSavedJobMock.mockRejectedValue(new ApiError(500, "nope"));
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    await screen.findByRole("alert");
    expect(screen.getByText("Junior Backend Engineer")).toBeInTheDocument();
  });
});

// --- edit -------------------------------------------------------------

describe("editing a job", () => {
  it("edits a job in place", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    updateSavedJobMock.mockResolvedValue(job({ title: "Senior Engineer" }));
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    // Both forms are on screen now, so the query is scoped to the edit
    // form by its accessible name — the ids are distinct per form.
    const form = screen.getByRole("form", {
      name: "Edit Junior Backend Engineer",
    });
    fireEvent.change(within(form).getByLabelText("Job title"), {
      target: { value: "Senior Engineer" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Senior Engineer")).toBeInTheDocument();
  });

  it("prefills the edit form with the current values", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const form = screen.getByRole("form", {
      name: "Edit Junior Backend Engineer",
    });
    expect(within(form).getByLabelText("Company")).toHaveValue(
      "Fictional Widgets Ltd",
    );
    expect(within(form).getByLabelText("Job description")).toHaveValue(
      "Build and test internal web services.",
    );
    // The create form has its own ids and is untouched — which is what
    // the per-form idPrefix exists to guarantee.
    expect(document.querySelector("#new-job-company")).toHaveValue("");
    expect(within(form).getByLabelText("Company")).toHaveAttribute(
      "id",
      "edit-job-1-company",
    );
  });

  it("cancels an edit without calling the API", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(updateSavedJobMock).not.toHaveBeenCalled();
    expect(screen.getByText("Junior Backend Engineer")).toBeInTheDocument();
  });

  it("keeps the edit form open when the update fails", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    updateSavedJobMock.mockRejectedValue(new ApiError(422, "title too long"));
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "title too long",
    );
    expect(
      screen.getByRole("button", { name: "Save changes" }),
    ).toBeInTheDocument();
  });
});

// --- delete -----------------------------------------------------------

describe("deleting a job", () => {
  it("requires confirmation before deleting", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    // Nothing has been deleted yet — a second, explicit step is needed.
    expect(deleteSavedJobMock).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Confirm delete" }),
    ).toBeInTheDocument();
  });

  it("deletes after confirmation", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    deleteSavedJobMock.mockResolvedValue(undefined);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() =>
      expect(
        screen.queryByText("Junior Backend Engineer"),
      ).not.toBeInTheDocument(),
    );
    expect(deleteSavedJobMock).toHaveBeenCalledWith(ACCESS_TOKEN, "job-1");
  });

  it("can back out of a pending delete", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(deleteSavedJobMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("keeps the job when deletion fails", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    deleteSavedJobMock.mockRejectedValue(new ApiError(500, "could not delete"));
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "could not delete",
    );
    expect(screen.getByText("Junior Backend Engineer")).toBeInTheDocument();
  });
});

// --- refresh ----------------------------------------------------------

describe("manual refresh", () => {
  it("refetches the list", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(listSavedJobsMock).toHaveBeenCalledTimes(2));
  });

  it("does not show the loading skeleton during a refresh", async () => {
    listSavedJobsMock.mockResolvedValue([job()]);
    renderSection();
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(screen.queryByText(/loading saved jobs/i)).not.toBeInTheDocument();
  });
});
