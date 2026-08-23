import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getProfileMock = vi.fn();
const updateProfileMock = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api-client")>(
      "@/lib/api-client",
    );
  return {
    ...actual,
    getProfile: (...args: unknown[]) => getProfileMock(...args),
    updateProfile: (...args: unknown[]) => updateProfileMock(...args),
  };
});

import ProfileForm from "./profile-form";

const ACCESS_TOKEN = "tok";
const USER_ID = "11111111-1111-1111-1111-111111111111";

const EMPTY_PROFILE = {
  user_id: USER_ID,
  full_name: null,
  headline: null,
  city: null,
  country: null,
  experience_level: null,
  target_roles: [],
  target_skills: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const FILLED_PROFILE = {
  ...EMPTY_PROFILE,
  full_name: "Alice Example",
  headline: "Backend engineer",
  city: "Toronto",
  country: "Canada",
  experience_level: "junior" as const,
  target_roles: ["Backend Engineer", "SRE"],
  target_skills: ["Python", "SQL"],
};

function renderForm() {
  return render(<ProfileForm accessToken={ACCESS_TOKEN} userId={USER_ID} />);
}

beforeEach(() => {
  getProfileMock.mockReset();
  updateProfileMock.mockReset();
});

describe("profile form", () => {
  it("loads and displays the existing profile", async () => {
    getProfileMock.mockResolvedValue(FILLED_PROFILE);

    renderForm();

    expect(await screen.findByLabelText(/full name/i)).toHaveValue(
      "Alice Example",
    );
    expect(screen.getByLabelText(/headline/i)).toHaveValue("Backend engineer");
    expect(screen.getByLabelText(/city/i)).toHaveValue("Toronto");
    expect(screen.getByLabelText(/country/i)).toHaveValue("Canada");
    expect(screen.getByLabelText(/experience level/i)).toHaveValue("junior");
    expect(screen.getByLabelText(/target roles/i)).toHaveValue(
      "Backend Engineer, SRE",
    );
    expect(screen.getByLabelText(/target skills/i)).toHaveValue("Python, SQL");
    expect(getProfileMock).toHaveBeenCalledWith(ACCESS_TOKEN, USER_ID);
  });

  it("renders empty defaults for a brand-new profile", async () => {
    getProfileMock.mockResolvedValue(EMPTY_PROFILE);

    renderForm();

    expect(await screen.findByLabelText(/full name/i)).toHaveValue("");
    expect(screen.getByLabelText(/experience level/i)).toHaveValue("");
  });

  it("saves edited fields, splitting comma-separated lists into arrays", async () => {
    getProfileMock.mockResolvedValue(EMPTY_PROFILE);
    updateProfileMock.mockResolvedValue({
      ...EMPTY_PROFILE,
      full_name: "Bob Example",
      target_roles: ["Data Analyst"],
      target_skills: ["Go", "SQL"],
    });

    renderForm();
    await screen.findByLabelText(/full name/i);

    fireEvent.change(screen.getByLabelText(/full name/i), {
      target: { value: "Bob Example" },
    });
    fireEvent.change(screen.getByLabelText(/target roles/i), {
      target: { value: "Data Analyst" },
    });
    fireEvent.change(screen.getByLabelText(/target skills/i), {
      target: { value: "Go, SQL" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(updateProfileMock).toHaveBeenCalled());
    expect(updateProfileMock).toHaveBeenCalledWith(ACCESS_TOKEN, USER_ID, {
      full_name: "Bob Example",
      headline: null,
      city: null,
      country: null,
      experience_level: null,
      target_roles: ["Data Analyst"],
      target_skills: ["Go", "SQL"],
    });
  });

  it("sends null (not an empty string) for a blank optional field", async () => {
    getProfileMock.mockResolvedValue(FILLED_PROFILE);
    updateProfileMock.mockResolvedValue(FILLED_PROFILE);

    renderForm();
    await screen.findByLabelText(/full name/i);

    fireEvent.change(screen.getByLabelText(/headline/i), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(updateProfileMock).toHaveBeenCalled());
    const [, , payload] = updateProfileMock.mock.calls[0];
    expect(payload.headline).toBeNull();
  });

  it("shows a saved confirmation after a successful save", async () => {
    getProfileMock.mockResolvedValue(EMPTY_PROFILE);
    updateProfileMock.mockResolvedValue(EMPTY_PROFILE);

    renderForm();
    await screen.findByLabelText(/full name/i);
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    expect(await screen.findByText(/profile saved/i)).toBeInTheDocument();
  });

  it("shows the API's error message and does not show a saved confirmation on failure", async () => {
    const { ApiError } = await import("@/lib/api-client");
    getProfileMock.mockResolvedValue(EMPTY_PROFILE);
    updateProfileMock.mockRejectedValue(
      new ApiError(422, "headline must not be blank — send null to clear it"),
    );

    renderForm();
    await screen.findByLabelText(/full name/i);
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "headline must not be blank",
    );
    expect(screen.queryByText(/profile saved/i)).not.toBeInTheDocument();
  });
});
