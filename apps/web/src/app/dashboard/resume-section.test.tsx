import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const listResumesMock = vi.fn();
const uploadResumeMock = vi.fn();
const deleteResumeMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    listResumes: (...args: unknown[]) => listResumesMock(...args),
    uploadResume: (...args: unknown[]) => uploadResumeMock(...args),
    deleteResume: (...args: unknown[]) => deleteResumeMock(...args),
  };
});

import ResumeSection from "./resume-section";

const ACCESS_TOKEN = "tok";

const RESUME_A = {
  id: "aaaaaaaa-1111-1111-1111-111111111111",
  user_id: "user-1",
  original_filename: "Resume.pdf",
  content_type: "application/pdf",
  file_size_bytes: 154_000,
  status: "succeeded" as const,
  error_message: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderSection(onWorkComplete?: () => void) {
  return render(
    <ResumeSection
      accessToken={ACCESS_TOKEN}
      onWorkComplete={onWorkComplete}
    />,
  );
}

function pdfFile(name = "resume.pdf") {
  return new File(["%PDF-1.4 fake"], name, { type: "application/pdf" });
}

beforeEach(() => {
  listResumesMock.mockReset();
  uploadResumeMock.mockReset();
  deleteResumeMock.mockReset();
});

describe("resume section", () => {
  it("loads and displays the resume list", async () => {
    listResumesMock.mockResolvedValue([RESUME_A]);

    renderSection();

    expect(await screen.findByText("Resume.pdf")).toBeInTheDocument();
    expect(screen.getByText(/150\.4 KB/)).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(listResumesMock).toHaveBeenCalledWith(ACCESS_TOKEN);
  });

  it("shows an empty state with no resumes", async () => {
    listResumesMock.mockResolvedValue([]);

    renderSection();

    expect(
      await screen.findByText(/no resumes uploaded yet/i),
    ).toBeInTheDocument();
  });

  it("shows the safe error message for a failed resume", async () => {
    listResumesMock.mockResolvedValue([
      {
        ...RESUME_A,
        status: "failed",
        error_message: "the document could not be read",
      },
    ]);

    renderSection();

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(
      screen.getByText("the document could not be read"),
    ).toBeInTheDocument();
  });

  it("uploads a selected file and adds it to the list", async () => {
    listResumesMock.mockResolvedValue([]);
    uploadResumeMock.mockResolvedValue({
      ...RESUME_A,
      status: "queued",
      error_message: null,
    });

    renderSection();
    await screen.findByText(/no resumes uploaded yet/i);

    fireEvent.change(screen.getByLabelText(/upload resume/i), {
      target: { files: [pdfFile()] },
    });

    await waitFor(() => expect(uploadResumeMock).toHaveBeenCalled());
    const [token, file] = uploadResumeMock.mock.calls[0];
    expect(token).toBe(ACCESS_TOKEN);
    expect(file.name).toBe("resume.pdf");
    expect(await screen.findByText("Resume.pdf")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
  });

  it("shows the API's error message when an upload is rejected", async () => {
    const { ApiError } = await import("@/lib/api-client");
    listResumesMock.mockResolvedValue([]);
    uploadResumeMock.mockRejectedValue(
      new ApiError(422, "only .pdf and .docx files are supported"),
    );

    renderSection();
    await screen.findByText(/no resumes uploaded yet/i);

    fireEvent.change(screen.getByLabelText(/upload resume/i), {
      target: { files: [pdfFile("resume.exe")] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "only .pdf and .docx files are supported",
    );
  });

  it("deletes a resume and removes it from the list", async () => {
    listResumesMock.mockResolvedValue([RESUME_A]);
    deleteResumeMock.mockResolvedValue(undefined);

    renderSection();
    await screen.findByText("Resume.pdf");

    fireEvent.click(screen.getByRole("button", { name: /delete/i }));

    await waitFor(() =>
      expect(deleteResumeMock).toHaveBeenCalledWith(ACCESS_TOKEN, RESUME_A.id),
    );
    await waitFor(() =>
      expect(screen.queryByText("Resume.pdf")).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByText(/no resumes uploaded yet/i),
    ).toBeInTheDocument();
  });

  it("shows the API's error message when a delete fails, without removing the row", async () => {
    const { ApiError } = await import("@/lib/api-client");
    listResumesMock.mockResolvedValue([RESUME_A]);
    deleteResumeMock.mockRejectedValue(
      new ApiError(403, "not authorized to access this resume"),
    );

    renderSection();
    await screen.findByText("Resume.pdf");

    fireEvent.click(screen.getByRole("button", { name: /delete/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "not authorized to access this resume",
    );
    expect(screen.getByText("Resume.pdf")).toBeInTheDocument();
  });

  it("polls while a resume is queued and stops once it reaches a terminal state", async () => {
    const queued = {
      ...RESUME_A,
      status: "queued" as const,
      error_message: null,
    };
    const succeeded = {
      ...RESUME_A,
      status: "succeeded" as const,
      error_message: null,
    };
    listResumesMock
      .mockResolvedValueOnce([queued])
      .mockResolvedValueOnce([succeeded]);

    renderSection();
    await screen.findByText("Queued");
    expect(listResumesMock).toHaveBeenCalledTimes(1);

    // The real 2s poll interval firing brings back a terminal status.
    await waitFor(() => expect(listResumesMock).toHaveBeenCalledTimes(2), {
      timeout: 4000,
    });
    await screen.findByText("Ready");

    // No further polling once nothing is pending.
    listResumesMock.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(listResumesMock).not.toHaveBeenCalled();
  }, 10000);
});

// --- completion notification (auto-refresh coordination) -------------

describe("resume completion notification", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const queued = {
    ...RESUME_A,
    status: "queued" as const,
    error_message: null,
  };
  const succeeded = {
    ...RESUME_A,
    status: "succeeded" as const,
    error_message: null,
  };
  const failed = {
    ...RESUME_A,
    status: "failed" as const,
    error_message: "the document could not be read",
  };

  it("notifies once when a resume reaches a terminal state", async () => {
    const onWorkComplete = vi.fn();
    listResumesMock
      .mockResolvedValueOnce([queued])
      .mockResolvedValue([succeeded]);

    renderSection(onWorkComplete);
    await screen.findByText("Queued");
    // Nothing has settled yet — the resume is still pending.
    expect(onWorkComplete).not.toHaveBeenCalled();

    await screen.findByText("Ready", {}, { timeout: 4000 });
    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1));
  }, 10000);

  it("does not re-notify while the status stays terminal", async () => {
    const onWorkComplete = vi.fn();
    listResumesMock
      .mockResolvedValueOnce([queued])
      .mockResolvedValue([succeeded]);

    renderSection(onWorkComplete);
    await screen.findByText("Ready", {}, { timeout: 4000 });
    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1));

    // Polling has stopped, so further ticks cannot re-fire it.
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(onWorkComplete).toHaveBeenCalledTimes(1);
  }, 10000);

  it("notifies on failure too, and keeps the existing error state", async () => {
    const onWorkComplete = vi.fn();
    listResumesMock.mockResolvedValueOnce([queued]).mockResolvedValue([failed]);

    renderSection(onWorkComplete);
    await screen.findByText("Failed", {}, { timeout: 4000 });

    await waitFor(() => expect(onWorkComplete).toHaveBeenCalledTimes(1));
    // The per-resume failure message is still rendered as before.
    expect(
      screen.getByText("the document could not be read"),
    ).toBeInTheDocument();
  }, 10000);

  it("never notifies when nothing was ever pending", async () => {
    const onWorkComplete = vi.fn();
    listResumesMock.mockResolvedValue([succeeded]);

    renderSection(onWorkComplete);
    await screen.findByText("Ready");

    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(onWorkComplete).not.toHaveBeenCalled();
  }, 10000);

  it("stops polling after unmount", async () => {
    listResumesMock.mockResolvedValue([queued]);

    const { unmount } = renderSection();
    await screen.findByText("Queued");
    unmount();

    listResumesMock.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(listResumesMock).not.toHaveBeenCalled();
  }, 10000);
});
