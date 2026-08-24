const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const AUTH_BASE = `${API_BASE}/api/v1/auth`;
const PROFILE_BASE = `${API_BASE}/api/v1/profiles`;
const RESUME_BASE = `${API_BASE}/api/v1/resumes`;
const CANDIDATE_SKILL_BASE = `${API_BASE}/api/v1/candidate-skills`;
const GITHUB_CONNECTION_BASE = `${API_BASE}/api/v1/github-connection`;

export interface AccessTokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserResponse {
  id: string;
  email: string;
  created_at: string;
}

/** Thrown for any non-2xx response, with the API's own error message
 * where one was given (see parseErrorMessage). */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface FastApiErrorBody {
  detail?: string | { msg?: string }[];
}

function isFastApiErrorBody(value: unknown): value is FastApiErrorBody {
  return typeof value === "object" && value !== null && "detail" in value;
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isFastApiErrorBody(body)) {
      const { detail } = body;
      if (typeof detail === "string") {
        return detail;
      }
      if (Array.isArray(detail) && typeof detail[0]?.msg === "string") {
        return detail[0].msg;
      }
    }
  } catch {
    // Response wasn't JSON (or had no body) — fall through.
  }
  return "something went wrong";
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${AUTH_BASE}${path}`, {
    method: "POST",
    // Required for the browser to send/accept the HttpOnly refresh-token
    // cookie on this cross-origin (same-site) request to the API.
    credentials: "include",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function register(
  email: string,
  password: string,
): Promise<UserResponse> {
  return postJson<UserResponse>("/register", { email, password });
}

export function login(
  email: string,
  password: string,
): Promise<AccessTokenResponse> {
  return postJson<AccessTokenResponse>("/login", { email, password });
}

let inFlightRefresh: Promise<AccessTokenResponse> | null = null;

/** Exchanges the HttpOnly refresh-token cookie (sent automatically via
 * credentials: "include") for a new access token. Never takes or
 * returns the refresh token itself — see docs/decisions.md.
 *
 * Deduplicates concurrent calls into a single request. The API rotates
 * the refresh token on every use and revokes the whole session if an
 * already-used token is presented again — so two callers racing to
 * refresh independently (e.g. React Strict Mode's double effect-invoke
 * in development, or any other accidental double-call in the same tab)
 * would otherwise have the second one reuse the first's now-rotated-away
 * cookie and revoke the session the first call just established. */
export function refresh(): Promise<AccessTokenResponse> {
  if (inFlightRefresh) {
    return inFlightRefresh;
  }
  inFlightRefresh = postJson<AccessTokenResponse>("/refresh").finally(() => {
    inFlightRefresh = null;
  });
  return inFlightRefresh;
}

export function logout(): Promise<void> {
  return postJson<void>("/logout");
}

export async function getCurrentUser(
  accessToken: string,
): Promise<UserResponse> {
  const response = await fetch(`${AUTH_BASE}/me`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as UserResponse;
}

// --- Candidate profile (Prompt 1.3) ---

export type ExperienceLevel = "student" | "junior" | "mid" | "senior";

export interface ProfileResponse {
  user_id: string;
  full_name: string | null;
  headline: string | null;
  city: string | null;
  country: string | null;
  experience_level: ExperienceLevel | null;
  target_roles: string[];
  target_skills: string[];
  created_at: string;
  updated_at: string;
}

/** PATCH body. Mirrors the API's partial-update contract: a field left
 * `undefined` is omitted from the JSON body entirely (unchanged
 * server-side); `null` explicitly clears an optional scalar field;
 * `target_roles`/`target_skills`, when included, fully replace the
 * profile's existing set. See apps/api/app/schemas/profile.py. */
export interface ProfileUpdatePayload {
  full_name?: string | null;
  headline?: string | null;
  city?: string | null;
  country?: string | null;
  experience_level?: ExperienceLevel | null;
  target_roles?: string[];
  target_skills?: string[];
}

/** Fetches the given user's profile — the API only ever returns 200 for
 * the caller's own `userId` (403 otherwise); see the ownership-enforced
 * `/api/v1/profiles/{user_id}` routes and docs/decisions.md. */
export async function getProfile(
  accessToken: string,
  userId: string,
): Promise<ProfileResponse> {
  const response = await fetch(`${PROFILE_BASE}/${userId}`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as ProfileResponse;
}

export async function updateProfile(
  accessToken: string,
  userId: string,
  patch: ProfileUpdatePayload,
): Promise<ProfileResponse> {
  const response = await fetch(`${PROFILE_BASE}/${userId}`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(patch),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as ProfileResponse;
}

// --- Resumes (Prompt 2.1 upload/list/delete; Prompt 2.2 extraction status) ---

export type ResumeStatus = "queued" | "processing" | "succeeded" | "failed";

/** Resume metadata — deliberately has no storage path or extracted-text
 * field. See apps/api/app/schemas/resume.py's ResumeResponse: `status`
 * and `error_message` are this app's extraction *status* endpoint —
 * there is no separate route, and no endpoint returns the extracted
 * text itself in this prompt. */
export interface ResumeResponse {
  id: string;
  user_id: string;
  original_filename: string;
  content_type: string;
  file_size_bytes: number;
  status: ResumeStatus;
  // A curated, safe reason — only ever set when status === "failed".
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

/** Uploads a resume file (PDF or DOCX). The API validates extension,
 * content type, file signature, size, and filename — see
 * apps/api/app/api/v1/resume.py — and this function surfaces whatever
 * it rejects as an ApiError, the same as every other call here. */
export async function uploadResume(
  accessToken: string,
  file: File,
): Promise<ResumeResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(RESUME_BASE, {
    method: "POST",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
    // No Content-Type header here: the browser sets
    // multipart/form-data with the correct boundary itself when the
    // body is a FormData — setting it manually would omit that
    // boundary and break parsing on the server.
    body: formData,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as ResumeResponse;
}

/** Lists the caller's own resumes, newest first — the API only ever
 * returns the caller's own (see the ownership-enforced
 * `/api/v1/resumes` routes). */
export async function listResumes(
  accessToken: string,
): Promise<ResumeResponse[]> {
  const response = await fetch(RESUME_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as ResumeResponse[];
}

export async function deleteResume(
  accessToken: string,
  resumeId: string,
): Promise<void> {
  const response = await fetch(`${RESUME_BASE}/${resumeId}`, {
    method: "DELETE",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
}

// --- Candidate skills (Prompt 2.4) ---

/** Review state of an extracted skill. "rejected" is a persistent
 * tombstone rather than a deletion — it is what stops a future
 * extraction run from re-suggesting a skill the user turned down, so
 * rejected rows are still returned by the API and shown (collapsed) in
 * the UI with the option to restore. There is deliberately no delete
 * endpoint. See apps/api/app/schemas/skill.py. */
export type CandidateSkillStatus = "suggested" | "confirmed" | "rejected";

/** Only "confirmed" and "rejected" can be set by a user — "suggested"
 * means "the extractor proposed this and nobody has reviewed it yet",
 * which is not a state a person can return a skill to. */
export type CandidateSkillDecision = "confirmed" | "rejected";

export type EvidenceSourceType = "resume" | "github" | "manual";

export interface SkillEvidenceResponse {
  id: string;
  source_type: EvidenceSourceType;
  /** For resume evidence this is the caller's own resume id; for manual
   * evidence, their user id. See the API model for the per-type
   * contract. */
  source_identifier: string;
  /** A verbatim span from the source document. Null for evidence with
   * nothing quotable, such as a manual assertion. */
  excerpt: string | null;
  extraction_method: string;
  /** 0..1. Stored exactly server-side; exposed as a number here. */
  confidence: number;
  created_at: string;
}

export interface CandidateSkillResponse {
  id: string;
  skill_id: string;
  skill_name: string;
  skill_category: string | null;
  status: CandidateSkillStatus;
  evidence: SkillEvidenceResponse[];
  created_at: string;
  updated_at: string;
}

/** The caller's own candidate skills with their supporting evidence —
 * the API only ever returns the caller's own. */
export async function listCandidateSkills(
  accessToken: string,
): Promise<CandidateSkillResponse[]> {
  const response = await fetch(CANDIDATE_SKILL_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as CandidateSkillResponse[];
}

/** Manually claim a skill. `name` must be a curated taxonomy skill or a
 * known alias — anything else is rejected with 422 and writes nothing.
 * This is deliberately narrower than the profile's free-text target
 * skills (Prompt 1.3), which do coin new skills; see
 * apps/api/app/api/v1/candidate_skill.py for why. */
export async function addCandidateSkill(
  accessToken: string,
  name: string,
): Promise<CandidateSkillResponse> {
  const response = await fetch(CANDIDATE_SKILL_BASE, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as CandidateSkillResponse;
}

export async function updateCandidateSkillStatus(
  accessToken: string,
  candidateSkillId: string,
  status: CandidateSkillDecision,
): Promise<CandidateSkillResponse> {
  const response = await fetch(`${CANDIDATE_SKILL_BASE}/${candidateSkillId}`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ status }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as CandidateSkillResponse;
}

/** A connected PUBLIC GitHub account (Prompt 3.1).
 *
 * Only public, non-sensitive facts: which account, how many public
 * repositories it had when it was last checked, and when that was.
 * There is no token, email, or private data here because none is stored
 * server-side — CareerLens never asks for a GitHub password and never
 * requests private access. See apps/api/app/models/github_connection.py. */
export interface GitHubConnectionResponse {
  user_id: string;
  /** GitHub's canonical spelling of the login, not what the user typed. */
  username: string;
  /** GitHub's immutable numeric account id. Kept because a username can
   * be renamed and later reused by someone else. */
  github_user_id: number;
  /** A snapshot taken at `last_verified_at`, not a live count — always
   * present it alongside that timestamp. */
  public_repo_count: number;
  last_verified_at: string;
  created_at: string;
  updated_at: string;
}

/** The caller's own connection, or null when they haven't connected one.
 * "Not connected" is a normal state rather than an error, so this
 * resolves to null instead of throwing. */
export async function getGitHubConnection(
  accessToken: string,
): Promise<GitHubConnectionResponse | null> {
  const response = await fetch(GITHUB_CONNECTION_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as GitHubConnectionResponse | null;
}

/** Verify a public GitHub username and connect it, replacing any
 * existing connection (the server keys one connection per user).
 *
 * Sends the username and nothing else — the API rejects any extra field,
 * which is how "we never ask for a password or token" is enforced rather
 * than merely promised. Failures arrive as ApiError with the API's own
 * message: 404 no such public account, 422 unusable username or an
 * organization, 503 GitHub unavailable or rate-limited, 504 timeout. */
export async function connectGitHub(
  accessToken: string,
  username: string,
): Promise<GitHubConnectionResponse> {
  const response = await fetch(GITHUB_CONNECTION_BASE, {
    method: "PUT",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ username }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as GitHubConnectionResponse;
}

/** Remove the caller's connection. Idempotent — succeeds whether or not
 * one existed. */
export async function disconnectGitHub(accessToken: string): Promise<void> {
  const response = await fetch(GITHUB_CONNECTION_BASE, {
    method: "DELETE",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
}

/** Processing state of a GitHub import (Prompt 3.2). The same four
 * states resume extraction uses. A run paused by GitHub rate limiting
 * stays "processing" — it is waiting, not broken. */
export type IngestionStatus = "queued" | "processing" | "succeeded" | "failed";

/** One GitHub import run.
 *
 * THE THREE COUNTS ARE NOT INTERCHANGEABLE, and the UI needs all of
 * them to tell the truth about a capped import:
 *
 *   repositories_available       every public repo GitHub listed, forks
 *                                included — the honest "your account has
 *                                N repositories" figure
 *   repositories_forks_excluded  how many of those were forks
 *   repositories_total           how many this run actually imported in
 *                                full (the capped set)
 *
 * Reporting only the last one would tell a user with 47 repositories
 * that they have 20. */
export interface GitHubIngestionRunResponse {
  id: string;
  user_id: string;
  status: IngestionStatus;
  /** Null until the listing finishes — "still working out how much
   * there is", not zero. */
  repositories_available: number | null;
  repositories_forks_excluded: number | null;
  repositories_total: number | null;
  repositories_completed: number;
  /** Repositories whose languages/README could not be read. Their basic
   * details are still imported, so this never means "nothing imported".
   * Rate limiting never lands here — it pauses the run instead. */
  repositories_failed: number;
  /** Curated, safe text — only ever set when status is "failed". */
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GitHubRepositoryLanguageResponse {
  language: string;
  byte_count: number;
}

/** One imported public repository. No README text: it exists for
 * server-side excerpt extraction, not for a list view. */
export interface GitHubRepositoryResponse {
  id: string;
  github_repo_id: number;
  name: string;
  full_name: string;
  description: string | null;
  is_fork: boolean;
  is_archived: boolean;
  primary_language: string | null;
  stargazers_count: number;
  forks_count: number;
  pushed_at: string | null;
  languages: GitHubRepositoryLanguageResponse[];
  topics: string[];
  has_readme: boolean;
  /** False means basic details only — a fork, or beyond this import's
   * repository cap. */
  detail_fetched: boolean;
  updated_at: string;
}

/** Queue an import of the caller's public repositories. 409 if no
 * account is connected, or if an import is already running. */
export async function startGitHubIngestion(
  accessToken: string,
): Promise<GitHubIngestionRunResponse> {
  const response = await fetch(`${GITHUB_CONNECTION_BASE}/ingestions`, {
    method: "POST",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as GitHubIngestionRunResponse;
}

/** The caller's most recent import, or null if they've never run one.
 * "Never imported" is a normal state, not an error. */
export async function getLatestGitHubIngestion(
  accessToken: string,
): Promise<GitHubIngestionRunResponse | null> {
  const response = await fetch(`${GITHUB_CONNECTION_BASE}/ingestions/latest`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as GitHubIngestionRunResponse | null;
}

/** Repositories imported for the caller. Excludes any that are no
 * longer on GitHub. */
export async function listGitHubRepositories(
  accessToken: string,
): Promise<GitHubRepositoryResponse[]> {
  const response = await fetch(`${GITHUB_CONNECTION_BASE}/repositories`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as GitHubRepositoryResponse[];
}
