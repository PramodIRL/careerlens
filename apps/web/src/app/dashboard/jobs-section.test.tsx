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
const importJobFromPdfMock = vi.fn();
const reorderSavedJobsMock = vi.fn();
// JobsSection renders a JobMatchPanel per job (Prompt 4.3), which
// fetches its own score. Without this mock the panel makes a real
// network call, fails, and renders a SECOND role="alert" — which made
// the error-path assertions below intermittently ambiguous.
const getJobMatchMock = vi.fn();
// Same reason as getJobMatch above: JobsSection now also renders a
// JobGapPanel per job, which fetches on its own.
const getJobGapsMock = vi.fn();
// 7.2 F1: these two complete the set of four per-job panels, so the
// disclosure tests below can count every request a job can make.
const getJobEligibilityMock = vi.fn();
const getJobSemanticMock = vi.fn();

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
    importJobFromPdf: (...args: unknown[]) => importJobFromPdfMock(...args),
    reorderSavedJobs: (...args: unknown[]) => reorderSavedJobsMock(...args),
    getJobMatch: (...args: unknown[]) => getJobMatchMock(...args),
    getJobGaps: (...args: unknown[]) => getJobGapsMock(...args),
    getJobEligibility: (...args: unknown[]) => getJobEligibilityMock(...args),
    getJobSemantic: (...args: unknown[]) => getJobSemanticMock(...args),
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
  importJobFromPdfMock.mockReset();
  reorderSavedJobsMock.mockReset();
  getJobMatchMock.mockReset().mockResolvedValue({
    formula_version: "skill_match_v1",
    overall_score: 0,
    earned_weight: 0,
    obtainable_weight: 0,
    has_requirements: false,
    required_matched: 0,
    required_total: 0,
    by_level: {
      required: { matched: 0, total: 0 },
      preferred: { matched: 0, total: 0 },
      mentioned: { matched: 0, total: 0 },
    },
    weights: { required: 3, preferred: 2, mentioned: 1 },
    matched_skills: [],
    missing_skills: [],
    required_missing: [],
  });
  getJobGapsMock.mockReset().mockResolvedValue({
    formula_version: "skill_gap_v1",
    required_gaps: [],
    preferred_gaps: [],
    informational_gaps: [],
    needs_confirmation: [],
    rejected_requirements: [],
    totals: {
      required_gaps: 0,
      preferred_gaps: 0,
      informational_gaps: 0,
      needs_confirmation: 0,
      rejected_requirements: 0,
      satisfied: 0,
      total_requirements: 0,
    },
  });
  getJobEligibilityMock.mockReset().mockResolvedValue({
    formula_version: "eligibility_v1",
    flag: "unknown",
    has_requirements: false,
    has_qualification_profile: true,
    totals: {
      satisfied: 0,
      not_satisfied: 0,
      unknown: 0,
      undetermined: 0,
      total_requirements: 0,
      required_not_satisfied: 0,
    },
    requirements: [],
  });
  getJobSemanticMock.mockReset().mockResolvedValue({
    formula_version: "semantic_fit_v1",
    fit: 0,
    band: "none",
    model_identifier: "mock",
    considered: 0,
    evidence: [],
  });
});

/** Every per-job panel request, as one number. */
function panelCalls() {
  return {
    match: getJobMatchMock.mock.calls.length,
    gaps: getJobGapsMock.mock.calls.length,
    eligibility: getJobEligibilityMock.mock.calls.length,
    semantic: getJobSemanticMock.mock.calls.length,
  };
}

/** Open one job's disclosure the way a user does. */
function openJob(index = 0) {
  fireEvent.click(screen.getAllByText("View match")[index]);
}

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

// --- three input paths (Prompt 4.1b) ---------------------------------

function draft(overrides: Record<string, unknown> = {}) {
  return {
    company: "Fictional Widgets Ltd",
    title: "Backend Engineer",
    location: "Springfield",
    employment_type: "full_time" as const,
    source_url: "https://example.com/j/1",
    description: "Build and test internal web services with Python.",
    notes: [],
    ...overrides,
  };
}

async function chooseMode(name: string) {
  fireEvent.click(await screen.findByRole("radio", { name }));
}

describe("input mode switcher", () => {
  it("offers both paths as one accessible choice", async () => {
    renderSection();
    const group = await screen.findByRole("radiogroup", {
      name: "How do you want to add a job?",
    });
    const options = within(group).getAllByRole("radio");
    expect(options.map((o) => o.textContent)).toEqual([
      "Paste manually",
      "Upload PDF",
    ]);
  });

  it("starts on manual, so the existing flow is the default", async () => {
    renderSection();
    expect(
      await screen.findByRole("radio", { name: "Paste manually" }),
    ).toHaveAttribute("aria-checked", "true");
    // The manual form is present and no importer is.
    expect(screen.getByLabelText("Job description")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Job description PDF"),
    ).not.toBeInTheDocument();
  });

  it("expands only the selected path", async () => {
    renderSection();
    await chooseMode("Upload PDF");

    expect(screen.getByLabelText("Job description PDF")).toBeInTheDocument();
    // The review form stays visible: a PDF draft lands in it, so the two
    // paths share one form rather than replacing each other.
    expect(screen.getByLabelText("Job description")).toBeInTheDocument();

    await chooseMode("Paste manually");
    expect(
      screen.queryByLabelText("Job description PDF"),
    ).not.toBeInTheDocument();
  });
});

describe("import from pdf", () => {
  function pdfFile() {
    return new File(["%PDF-1.4 fake"], "job.pdf", { type: "application/pdf" });
  }

  it("accepts only pdf files at the picker", async () => {
    renderSection();
    await chooseMode("Upload PDF");

    expect(screen.getByLabelText("Job description PDF")).toHaveAttribute(
      "accept",
      ".pdf,application/pdf",
    );
  });

  it("fills the review form from the extracted draft without saving", async () => {
    importJobFromPdfMock.mockResolvedValue(
      draft({
        company: null,
        title: null,
        location: null,
        employment_type: null,
        source_url: null,
        notes: [
          "We read the description from your PDF. Add the company, job title and location below.",
        ],
      }),
    );
    renderSection();
    await chooseMode("Upload PDF");

    fireEvent.change(screen.getByLabelText("Job description PDF"), {
      target: { files: [pdfFile()] },
    });

    await waitFor(() =>
      expect(screen.getByLabelText("Job description")).toHaveValue(
        "Build and test internal web services with Python.",
      ),
    );
    expect(screen.getByLabelText("Company")).toHaveValue("");
    expect(
      await screen.findByText(/Add the company, job title/),
    ).toBeInTheDocument();
    expect(createSavedJobMock).not.toHaveBeenCalled();
  });

  it("falls back to manual entry when the pdf cannot be read", async () => {
    importJobFromPdfMock.mockRejectedValue(
      new ApiError(422, "we could not read any text from that PDF"),
    );
    renderSection();
    await chooseMode("Upload PDF");

    fireEvent.change(screen.getByLabelText("Job description PDF"), {
      target: { files: [pdfFile()] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "we could not read any text from that PDF",
    );
    expect(
      screen.getByRole("radio", { name: "Paste manually" }),
    ).toHaveAttribute("aria-checked", "true");
  });
});

describe("both paths converge", () => {
  function pdfFile() {
    return new File(["%PDF-1.4 fake"], "job.pdf", { type: "application/pdf" });
  }

  it("saves an imported draft through the same endpoint as manual entry", async () => {
    importJobFromPdfMock.mockResolvedValue(draft());
    createSavedJobMock.mockResolvedValue(job({ title: "Backend Engineer" }));
    renderSection();
    await chooseMode("Upload PDF");
    fireEvent.change(screen.getByLabelText("Job description PDF"), {
      target: { files: [pdfFile()] },
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Company")).toHaveValue(
        "Fictional Widgets Ltd",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    // The SAME create call the manual path makes — one persistence path.
    await waitFor(() => expect(createSavedJobMock).toHaveBeenCalledTimes(1));
    expect(createSavedJobMock).toHaveBeenCalledWith(ACCESS_TOKEN, {
      company: "Fictional Widgets Ltd",
      title: "Backend Engineer",
      description: "Build and test internal web services with Python.",
      location: "Springfield",
      employment_type: "full_time",
      source_url: "https://example.com/j/1",
    });
  });

  it("clears the review notice after a successful save", async () => {
    importJobFromPdfMock.mockResolvedValue(draft());
    createSavedJobMock.mockResolvedValue(job());
    renderSection();
    await chooseMode("Upload PDF");
    fireEvent.change(screen.getByLabelText("Job description PDF"), {
      target: { files: [pdfFile()] },
    });
    await screen.findByText(/check these details before saving/i);

    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    await waitFor(() =>
      expect(
        screen.queryByText(/check these details before saving/i),
      ).not.toBeInTheDocument(),
    );
  });
});

// =====================================================================
// 7.1c — telling the dashboard the collection moved
//
// The roadmap's "jobs to prepare for" ceiling IS this collection's size,
// and a plan it has drawn is about these specific jobs in this specific
// order. Without `onJobsChanged` it learned neither, so deleting a job
// left it asking for more jobs than existed and every Generate came back
// 422 until the page was reloaded. See roadmap-section.test.tsx.
//
// ONLY ON SUCCESS, throughout: a mutation the server refused did not
// change the collection, and saying otherwise would retire a roadmap
// that is still current.
// =====================================================================

describe("reordering jobs", () => {
  const first = job({ id: "job-1", title: "First" });
  const second = job({ id: "job-2", title: "Second" });

  it("sends the whole permutation, because the endpoint takes one", async () => {
    listSavedJobsMock.mockResolvedValue([first, second]);
    reorderSavedJobsMock.mockResolvedValue([second, first]);
    renderSection();
    await screen.findByText("First");

    fireEvent.click(
      screen.getByRole("button", { name: /move second up in priority/i }),
    );

    await waitFor(() =>
      expect(reorderSavedJobsMock).toHaveBeenCalledWith(ACCESS_TOKEN, [
        "job-2",
        "job-1",
      ]),
    );
  });

  it("restores the previous order when the reorder fails", async () => {
    listSavedJobsMock.mockResolvedValue([first, second]);
    reorderSavedJobsMock.mockRejectedValue(
      new ApiError(
        422,
        "job_ids must list each of your saved jobs exactly once",
      ),
    );
    renderSection();
    await screen.findByText("First");

    fireEvent.click(
      screen.getByRole("button", { name: /move second up in priority/i }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "job_ids must list each of your saved jobs exactly once",
    );
    // The optimistic swap was rolled back, so what is on screen is what
    // the server actually holds.
    const rows = screen.getAllByRole("listitem");
    expect(within(rows[0]).getByText("First")).toBeInTheDocument();
  });

  it("reports the change so the roadmap can re-rank", async () => {
    const onJobsChanged = vi.fn();
    listSavedJobsMock.mockResolvedValue([first, second]);
    reorderSavedJobsMock.mockResolvedValue([second, first]);
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText("First");

    fireEvent.click(
      screen.getByRole("button", { name: /move second up in priority/i }),
    );

    await waitFor(() => expect(onJobsChanged).toHaveBeenCalledTimes(1));
  });

  it("reports nothing when the reorder was refused", async () => {
    const onJobsChanged = vi.fn();
    listSavedJobsMock.mockResolvedValue([first, second]);
    reorderSavedJobsMock.mockRejectedValue(new ApiError(422, "nope"));
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText("First");

    fireEvent.click(
      screen.getByRole("button", { name: /move second up in priority/i }),
    );

    await screen.findByRole("alert");
    expect(onJobsChanged).not.toHaveBeenCalled();
  });
});

describe("reporting collection changes", () => {
  it("reports a saved job", async () => {
    const onJobsChanged = vi.fn();
    createSavedJobMock.mockResolvedValue(job());
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText(/no saved jobs yet/i);

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    await waitFor(() => expect(onJobsChanged).toHaveBeenCalledTimes(1));
  });

  it("reports a deleted job", async () => {
    const onJobsChanged = vi.fn();
    listSavedJobsMock.mockResolvedValue([job()]);
    deleteSavedJobMock.mockResolvedValue(undefined);
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(onJobsChanged).toHaveBeenCalledTimes(1));
  });

  it("reports nothing when a delete fails", async () => {
    const onJobsChanged = vi.fn();
    listSavedJobsMock.mockResolvedValue([job()]);
    deleteSavedJobMock.mockRejectedValue(new ApiError(500, "could not delete"));
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText("Junior Backend Engineer");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await screen.findByRole("alert");
    expect(onJobsChanged).not.toHaveBeenCalled();
  });

  it("reports nothing when a save fails", async () => {
    const onJobsChanged = vi.fn();
    createSavedJobMock.mockRejectedValue(new ApiError(422, "bad url"));
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText(/no saved jobs yet/i);

    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Save job" }));

    await screen.findByRole("alert");
    expect(onJobsChanged).not.toHaveBeenCalled();
  });

  it("is optional, so the section still works without a listener", async () => {
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
  });

  it("does not refetch its own list after its own mutation", async () => {
    // The dashboard deliberately keeps `jobsVersion` out of this
    // section's own `refreshKey` (see page.tsx): the mutation response
    // was already applied in place, so a refetch here would be a second
    // request for data the component already has.
    const onJobsChanged = vi.fn();
    listSavedJobsMock.mockResolvedValue([job()]);
    deleteSavedJobMock.mockResolvedValue(undefined);
    render(
      <JobsSection accessToken={ACCESS_TOKEN} onJobsChanged={onJobsChanged} />,
    );
    await screen.findByText("Junior Backend Engineer");
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(onJobsChanged).toHaveBeenCalled());
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);
  });
});

// =====================================================================
// 7.2 F1 — progressive disclosure that is real, not just visual
//
// <details> hides its children with CSS, but React MOUNTS them, so
// every panel's fetch effect used to run behind a closed disclosure:
// measured at four requests per saved job on load — 33 for eight jobs —
// for content nobody had asked to see. These tests are the guard on
// that, and they are written in terms of REQUESTS rather than markup,
// because a panel that renders but does not fetch would be an equally
// good fix and a panel that fetches invisibly is the actual bug.
// =====================================================================

describe("job detail panels are not loaded until they are opened", () => {
  it("makes no panel request for a dashboard of closed jobs", async () => {
    listSavedJobsMock.mockResolvedValue([
      job({ id: "job-1" }),
      job({ id: "job-2", title: "Data Engineer" }),
      job({ id: "job-3", title: "Platform Engineer" }),
    ]);

    renderSection();
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(3),
    );

    // Every disclosure is genuinely closed — the panels are absent
    // because they were never mounted, not because CSS hid them.
    for (const summary of screen.getAllByText("View match")) {
      expect(summary.closest("details")).not.toHaveAttribute("open");
    }
    expect(panelCalls()).toEqual({
      match: 0,
      gaps: 0,
      eligibility: 0,
      semantic: 0,
    });
  });

  it("makes exactly that job's four requests when one is opened", async () => {
    listSavedJobsMock.mockResolvedValue([
      job({ id: "job-1" }),
      job({ id: "job-2", title: "Data Engineer" }),
    ]);

    renderSection();
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(2),
    );

    openJob(0);

    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(1));
    // FOUR, AND FOUR ONLY. One job opened is one job's worth of work —
    // the second job is still closed and has cost nothing.
    expect(panelCalls()).toEqual({
      match: 1,
      gaps: 1,
      eligibility: 1,
      semantic: 1,
    });
    expect(getJobMatchMock).toHaveBeenCalledWith(ACCESS_TOKEN, "job-1");
  });

  it("does not refetch the first job when a second is opened", async () => {
    listSavedJobsMock.mockResolvedValue([
      job({ id: "job-1" }),
      job({ id: "job-2", title: "Data Engineer" }),
    ]);

    renderSection();
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(2),
    );

    openJob(0);
    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(1));

    openJob(1);
    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(2));

    // TWO JOBS, TWO REQUESTS EACH — never three. Opening one job must
    // not disturb another that is already open, which is what a shared
    // key or a remount would cause.
    expect(panelCalls()).toEqual({
      match: 2,
      gaps: 2,
      eligibility: 2,
      semantic: 2,
    });
    expect(getJobMatchMock.mock.calls.map((call) => call[1])).toEqual([
      "job-1",
      "job-2",
    ]);
  });

  it("keeps both jobs open at once rather than behaving as an accordion", async () => {
    // Comparing two jobs is the reason this is a set and not a single
    // open id: opening the second must not close the first.
    listSavedJobsMock.mockResolvedValue([
      job({ id: "job-1" }),
      job({ id: "job-2", title: "Data Engineer" }),
    ]);

    renderSection();
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(2),
    );

    openJob(0);
    openJob(1);

    for (const summary of screen.getAllByText("View match")) {
      expect(summary.closest("details")).toHaveAttribute("open");
    }
  });

  it("closes again on a second click, and reopening is the user's choice", async () => {
    listSavedJobsMock.mockResolvedValue([job({ id: "job-1" })]);

    renderSection();
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(1),
    );

    openJob(0);
    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(1));

    openJob(0);
    expect(
      screen.getByText("View match").closest("details"),
    ).not.toHaveAttribute("open");
    // Closing unmounts the panels; nothing is fetched on the way out.
    expect(getJobMatchMock).toHaveBeenCalledTimes(1);
  });
});

// =====================================================================
// 7.2 F4 — each panel hears only the changes it can actually see
//
// One shared counter used to refetch all four panels for a change only
// one of them could observe. What each panel reads is a fact about its
// handler, so these tests assert the routing rather than the plumbing.
// The job is OPENED first in each case: a closed job has no panels to
// refresh, which F1 above already guarantees.
// =====================================================================

describe("refresh signals reach only the panels that can change", () => {
  async function renderOneOpenJob(props: Record<string, number> = {}) {
    listSavedJobsMock.mockResolvedValue([job({ id: "job-1" })]);
    const view = render(<JobsSection accessToken={ACCESS_TOKEN} {...props} />);
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(1),
    );
    openJob(0);
    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(1));
    return view;
  }

  it("refreshes match and gaps when a skill is confirmed, and nothing else", async () => {
    const { rerender } = await renderOneOpenJob({ matchRefreshKey: 0 });

    rerender(<JobsSection accessToken={ACCESS_TOKEN} matchRefreshKey={1} />);

    await waitFor(() => expect(getJobMatchMock).toHaveBeenCalledTimes(2));
    // `/eligibility` never reads a skill row and `/semantic` reads
    // stored embeddings, so neither can have moved.
    expect(panelCalls()).toEqual({
      match: 2,
      gaps: 2,
      eligibility: 1,
      semantic: 1,
    });
  });

  it("refreshes eligibility when qualifications change, and nothing else", async () => {
    const { rerender } = await renderOneOpenJob({ eligibilityRefreshKey: 0 });

    rerender(
      <JobsSection accessToken={ACCESS_TOKEN} eligibilityRefreshKey={1} />,
    );

    await waitFor(() => expect(getJobEligibilityMock).toHaveBeenCalledTimes(2));
    // Neither `/match` nor `/gaps` reads a qualification row.
    expect(panelCalls()).toEqual({
      match: 1,
      gaps: 1,
      eligibility: 2,
      semantic: 1,
    });
  });

  it("refreshes every panel when resume or GitHub work lands new evidence", async () => {
    // THE ONE SIGNAL THAT REACHES ALL FOUR, and it should: async
    // ingestion can write skills, evidence and embeddings, and can
    // suggest qualification facts.
    const { rerender } = await renderOneOpenJob({
      matchRefreshKey: 0,
      eligibilityRefreshKey: 0,
      semanticRefreshKey: 0,
    });

    rerender(
      <JobsSection
        accessToken={ACCESS_TOKEN}
        matchRefreshKey={1}
        eligibilityRefreshKey={1}
        semanticRefreshKey={1}
      />,
    );

    await waitFor(() => expect(getJobSemanticMock).toHaveBeenCalledTimes(2));
    expect(panelCalls()).toEqual({
      match: 2,
      gaps: 2,
      eligibility: 2,
      semantic: 2,
    });
  });

  it("refreshes nothing for a job the user has not opened", async () => {
    listSavedJobsMock.mockResolvedValue([job({ id: "job-1" })]);
    const { rerender } = render(
      <JobsSection accessToken={ACCESS_TOKEN} matchRefreshKey={0} />,
    );
    await waitFor(() =>
      expect(screen.getAllByText("View match")).toHaveLength(1),
    );

    rerender(<JobsSection accessToken={ACCESS_TOKEN} matchRefreshKey={1} />);

    expect(panelCalls()).toEqual({
      match: 0,
      gaps: 0,
      eligibility: 0,
      semantic: 0,
    });
  });
});

// =====================================================================
// 7.2 F5 — the saved-job count, reported rather than refetched
// =====================================================================

describe("reporting the saved-job count upward", () => {
  it("reports the count once a list has actually arrived", async () => {
    const onJobCountChange = vi.fn();
    listSavedJobsMock.mockResolvedValue([
      job({ id: "job-1" }),
      job({ id: "job-2" }),
    ]);

    render(
      <JobsSection
        accessToken={ACCESS_TOKEN}
        onJobCountChange={onJobCountChange}
      />,
    );

    await waitFor(() => expect(onJobCountChange).toHaveBeenCalledWith(2));
    // ONE LIST REQUEST for the page — the count costs nothing extra.
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);
  });

  it("reports the new count after a delete, without re-reading the list", async () => {
    const onJobCountChange = vi.fn();
    listSavedJobsMock.mockResolvedValue([job({ id: "job-1" })]);
    deleteSavedJobMock.mockResolvedValue(undefined);

    render(
      <JobsSection
        accessToken={ACCESS_TOKEN}
        onJobCountChange={onJobCountChange}
      />,
    );
    await waitFor(() => expect(onJobCountChange).toHaveBeenCalledWith(1));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(onJobCountChange).toHaveBeenCalledWith(0));
    expect(listSavedJobsMock).toHaveBeenCalledTimes(1);
  });

  it("says nothing about the count when the list could not be loaded", async () => {
    // NOT KNOWN IS NOT ZERO. Reporting 0 here would bound the roadmap's
    // input to nothing on a transient error, which is a false statement
    // about the user's data rather than a missing one.
    const onJobCountChange = vi.fn();
    listSavedJobsMock.mockRejectedValue(new ApiError(500, "server on fire"));

    render(
      <JobsSection
        accessToken={ACCESS_TOKEN}
        onJobCountChange={onJobCountChange}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "server on fire",
    );
    expect(onJobCountChange).not.toHaveBeenCalled();
  });
});
