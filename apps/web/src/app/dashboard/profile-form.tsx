"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  ApiError,
  getProfile,
  updateProfile,
  type ExperienceLevel,
  type ProfileResponse,
} from "@/lib/api-client";

const EXPERIENCE_LEVELS: { value: ExperienceLevel; label: string }[] = [
  { value: "student", label: "Student" },
  { value: "junior", label: "Junior" },
  { value: "mid", label: "Mid-level" },
  { value: "senior", label: "Senior" },
];

interface ProfileFormProps {
  accessToken: string;
  userId: string;
}

interface FormState {
  fullName: string;
  headline: string;
  city: string;
  country: string;
  experienceLevel: ExperienceLevel | "";
  targetRoles: string;
  targetSkills: string;
}

const EMPTY_FORM: FormState = {
  fullName: "",
  headline: "",
  city: "",
  country: "",
  experienceLevel: "",
  targetRoles: "",
  targetSkills: "",
};

function toFormState(profile: ProfileResponse): FormState {
  return {
    fullName: profile.full_name ?? "",
    headline: profile.headline ?? "",
    city: profile.city ?? "",
    country: profile.country ?? "",
    experienceLevel: profile.experience_level ?? "",
    targetRoles: profile.target_roles.join(", "),
    targetSkills: profile.target_skills.join(", "),
  };
}

// Tag lists are edited as a single comma-separated field rather than a
// dedicated chip/tag widget — simpler to build and test, and the API
// already trims/dedupes/validates each item server-side either way.
function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

// The API treats a blank string as invalid for these fields — clearing
// one is always explicit `null`, never "" (see ProfileUpdateRequest) —
// so an empty input must become `null`, not "".
function toNullableText(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export default function ProfileForm({ accessToken, userId }: ProfileFormProps) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let cancelled = false;
    getProfile(accessToken, userId)
      .then((profile) => {
        if (!cancelled) {
          setForm(toFormState(profile));
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "something went wrong",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken, userId]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await updateProfile(accessToken, userId, {
        full_name: toNullableText(form.fullName),
        headline: toNullableText(form.headline),
        city: toNullableText(form.city),
        country: toNullableText(form.country),
        experience_level:
          form.experienceLevel === "" ? null : form.experienceLevel,
        target_roles: splitList(form.targetRoles),
        target_skills: splitList(form.targetSkills),
      });
      setForm(toFormState(updated));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
      queueMicrotask(() => errorRef.current?.focus());
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading profile…
      </p>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex w-full flex-col gap-4 text-left"
    >
      {error && (
        <p
          id="profile-form-error"
          ref={errorRef}
          role="alert"
          tabIndex={-1}
          className="rounded bg-red-100 px-3 py-2 text-sm text-red-800"
        >
          {error}
        </p>
      )}
      {saved && !error && (
        <p role="status" className="text-sm text-green-700 dark:text-green-400">
          Profile saved.
        </p>
      )}

      <div className="flex flex-col gap-1">
        <label
          htmlFor="full_name"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Full name
        </label>
        <input
          id="full_name"
          value={form.fullName}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, fullName: e.target.value }))
          }
          className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="headline"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Headline
        </label>
        <input
          id="headline"
          value={form.headline}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, headline: e.target.value }))
          }
          className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
        />
      </div>

      <div className="flex gap-4">
        <div className="flex flex-1 flex-col gap-1">
          <label
            htmlFor="city"
            className="text-sm font-medium text-black dark:text-zinc-50"
          >
            City
          </label>
          <input
            id="city"
            value={form.city}
            onChange={(e) =>
              setForm((prev) => ({ ...prev, city: e.target.value }))
            }
            className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
          />
        </div>
        <div className="flex flex-1 flex-col gap-1">
          <label
            htmlFor="country"
            className="text-sm font-medium text-black dark:text-zinc-50"
          >
            Country
          </label>
          <input
            id="country"
            value={form.country}
            onChange={(e) =>
              setForm((prev) => ({ ...prev, country: e.target.value }))
            }
            className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
          />
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="experience_level"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Experience level
        </label>
        <select
          id="experience_level"
          value={form.experienceLevel}
          onChange={(e) =>
            setForm((prev) => ({
              ...prev,
              experienceLevel: e.target.value as ExperienceLevel | "",
            }))
          }
          className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
        >
          <option value="">Not set</option>
          {EXPERIENCE_LEVELS.map((level) => (
            <option key={level.value} value={level.value}>
              {level.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="target_roles"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Target roles
        </label>
        <input
          id="target_roles"
          value={form.targetRoles}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, targetRoles: e.target.value }))
          }
          placeholder="Backend Engineer, SRE"
          aria-describedby="target_roles-hint"
          className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
        />
        <p
          id="target_roles-hint"
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          Comma-separated.
        </p>
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="target_skills"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Target skills
        </label>
        <input
          id="target_skills"
          value={form.targetSkills}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, targetSkills: e.target.value }))
          }
          placeholder="Python, SQL"
          aria-describedby="target_skills-hint"
          className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
        />
        <p
          id="target_skills-hint"
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          Comma-separated.
        </p>
      </div>

      <button
        type="submit"
        disabled={saving}
        className="rounded bg-black px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-black"
      >
        {saving ? "Saving…" : "Save profile"}
      </button>
    </form>
  );
}
