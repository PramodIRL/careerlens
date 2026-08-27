const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const AUTH_BASE = `${API_BASE}/api/v1/auth`;
const PROFILE_BASE = `${API_BASE}/api/v1/profiles`;
const RESUME_BASE = `${API_BASE}/api/v1/resumes`;
const CANDIDATE_SKILL_BASE = `${API_BASE}/api/v1/candidate-skills`;
const SKILL_PROFILE_BASE = `${API_BASE}/api/v1/skill-profile`;
const SAVED_JOB_BASE = `${API_BASE}/api/v1/saved-jobs`;
const JOB_IMPORT_BASE = `${API_BASE}/api/v1/job-imports`;
const QUALIFICATIONS_BASE = `${API_BASE}/api/v1/qualifications`;
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
  /** A human-readable name for whatever `source_identifier` points at:
   * a resume's filename, or "owner/repo" for GitHub. Null for a manual
   * assertion (no external source to name) and for a source that no
   * longer resolves, such as a deleted resume. */
  source_label: string | null;
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

// --- Unified skill profile (Prompt 3.4) ------------------------------

/** Account-level rollup of the candidate's evidenced skills.
 *
 * Every field is a count or a boolean — a fact derived from stored rows,
 * never a computed rating. Scoring belongs to a later slice. */
export interface SkillProfileSummary {
  /** confirmed + suggested. Excludes rejected. */
  total: number;
  confirmed: number;
  suggested: number;
  /** Counted here but deliberately absent from `skills`: a rejection is
   * a tombstone meaning "this is not mine". */
  rejected: number;
  /** Counts distinct SKILLS per source, not evidence rows — one
   * repository can write four evidence rows for a single skill. */
  by_source: Record<EvidenceSourceType, number>;
  /** Skills backed by more than one distinct source type. */
  multi_source: number;
  /** True when nothing is left awaiting review. */
  reviewed: boolean;
}

export interface SkillProfileEntry {
  id: string;
  skill_id: string;
  skill_name: string;
  skill_category: string | null;
  status: CandidateSkillStatus;
  /** Distinct source types backing this skill, sorted. */
  sources: EvidenceSourceType[];
  evidence_count: number;
  /** max() over this skill's stored evidence confidences — a selection
   * of one existing value, NOT a blended skill score. */
  strongest_evidence_confidence: number;
  evidence: SkillEvidenceResponse[];
}

export interface SkillProfileResponse {
  summary: SkillProfileSummary;
  /** Confirmed and suggested skills only; see `summary.rejected`. */
  skills: SkillProfileEntry[];
}

/** The caller's unified skill profile — read-only, derived on request
 * from the same candidate-skill and evidence rows the review endpoint
 * mutates. The API only ever returns the caller's own. */
export async function getSkillProfile(
  accessToken: string,
): Promise<SkillProfileResponse> {
  const response = await fetch(SKILL_PROFILE_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as SkillProfileResponse;
}

// --- Saved job descriptions (Prompt 4.1) -----------------------------

/** Employment arrangements a saved posting can be labelled with. Mirrors
 * the API's closed vocabulary. */
export type EmploymentType =
  "full_time" | "part_time" | "contract" | "internship" | "temporary";

export interface SavedJobResponse {
  id: string;
  company: string;
  title: string;
  location: string | null;
  employment_type: EmploymentType | null;
  /** Metadata only. CareerLens never fetches this URL — it exists so the
   * user can find the posting again. */
  source_url: string | null;
  /** The posting exactly as the user saved it. */
  description: string;
  created_at: string;
  updated_at: string;
}

export interface SavedJobCreateRequest {
  company: string;
  title: string;
  description: string;
  location?: string | null;
  employment_type?: EmploymentType | null;
  source_url?: string | null;
}

/** PATCH body. An omitted field means "leave unchanged"; an explicit
 * null clears an optional field. The three required fields cannot be
 * nulled — the API rejects that with a 422. */
export type SavedJobUpdateRequest = Partial<SavedJobCreateRequest>;

/** The caller's saved jobs, newest first. The API only ever returns the
 * caller's own. */
export async function listSavedJobs(
  accessToken: string,
): Promise<SavedJobResponse[]> {
  const response = await fetch(SAVED_JOB_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as SavedJobResponse[];
}

/** Save a posting. The owner comes from the access token — this never
 * sends a user id, and the API would reject one if it did. */
export async function createSavedJob(
  accessToken: string,
  body: SavedJobCreateRequest,
): Promise<SavedJobResponse> {
  const response = await fetch(SAVED_JOB_BASE, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as SavedJobResponse;
}

export async function updateSavedJob(
  accessToken: string,
  savedJobId: string,
  body: SavedJobUpdateRequest,
): Promise<SavedJobResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as SavedJobResponse;
}

export async function deleteSavedJob(
  accessToken: string,
  savedJobId: string,
): Promise<void> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}`, {
    method: "DELETE",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
}

// --- Job import: PDF (Prompt 4.1b) -----------------------------------

/** An UNSAVED draft produced by a PDF import.
 *
 * Nothing here is persisted. The user reviews and corrects it, then
 * `createSavedJob` stores it through the same endpoint manual entry
 * uses — so an import can never silently save unreviewed data.
 *
 * Every field except `description` may be null, meaning "we could not
 * determine this". It never means a guess: the extractor reads only
 * fields the document explicitly labels. */
export interface JobDraftResponse {
  company: string | null;
  title: string | null;
  location: string | null;
  employment_type: EmploymentType | null;
  source_url: string | null;
  description: string;
  /** Plain-language prompts for the review step. */
  notes: string[];
}

/** Read a job-description PDF and return a draft. The file is parsed
 * server-side and discarded — it is never stored. */
export async function importJobFromPdf(
  accessToken: string,
  file: File,
): Promise<JobDraftResponse> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${JOB_IMPORT_BASE}/from-pdf`, {
    method: "POST",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
    body,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobDraftResponse;
}

// --- Candidate <-> job matching (Prompt 4.3) -------------------------

export type RequirementLevel = "required" | "preferred" | "mentioned";

export interface MatchedEvidence {
  source_type: string;
  excerpt: string | null;
  /** Match quality — "does this string denote this skill" — NOT a
   * capability rating, and deliberately never multiplied into the
   * score. Shown so a human can read it for what it is. */
  confidence: number;
}

export interface LevelBreakdown {
  matched: number;
  total: number;
}

export interface MatchedSkill {
  skill_id: string;
  skill_name: string;
  requirement_level: RequirementLevel;
  /** The job's own words that produced this requirement. */
  job_excerpt: string;
  candidate_status: string;
  /** True for a "suggested" skill: real evidence exists, the user just
   * has not reviewed it. It still counts toward the score. */
  candidate_unreviewed: boolean;
  candidate_evidence: MatchedEvidence[];
}

export interface MissingSkill {
  skill_id: string;
  skill_name: string;
  requirement_level: RequirementLevel;
  job_excerpt: string;
  /** Distinguishes "you rejected this" from "you don't have this" —
   * very different messages for the user. */
  candidate_rejected: boolean;
}

export interface JobMatchResponse {
  /** Which arithmetic produced this score. A number without it cannot
   * be reproduced. */
  formula_version: string;
  overall_score: number;
  earned_weight: number;
  obtainable_weight: number;
  /** False when the job has no recognised skill requirements. The UI
   * must then say "no skill requirements detected", NOT "0% match" —
   * the first is about the job, the second about the candidate. */
  has_requirements: boolean;
  required_matched: number;
  required_total: number;
  by_level: Record<RequirementLevel, LevelBreakdown>;
  weights: Record<RequirementLevel, number>;
  matched_skills: MatchedSkill[];
  missing_skills: MissingSkill[];
  /** The subset of missing_skills the job marks required — the ones
   * that actually block the candidate. Reported separately because the
   * v1 formula applies no penalty for them. */
  required_missing: MissingSkill[];
}

/** How well the authenticated candidate matches one of their own saved
 * jobs. Recomputed server-side on every request from current rows, so
 * confirming a skill or editing the description is reflected
 * immediately — there is no stored score to go stale. */
export async function getJobMatch(
  accessToken: string,
  savedJobId: string,
): Promise<JobMatchResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}/match`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobMatchResponse;
}

// --- Explainable skill gaps (Prompt 4.4) -----------------------------

export interface GapEntry {
  skill_id: string;
  skill_name: string;
  requirement_level: RequirementLevel;
  /** The job's own words that produced this requirement. */
  job_excerpt: string;
  /** "confirmed" / "suggested" / "rejected", or null when the candidate
   * has no row for this skill at all — a different thing from a row
   * that says "rejected". */
  candidate_status: string | null;
  /** Real stored evidence for needs-confirmation and rejected entries.
   * Empty for genuinely missing skills — never a generated sentence. */
  candidate_evidence: MatchedEvidence[];
}

export interface GapTotals {
  required_gaps: number;
  preferred_gaps: number;
  informational_gaps: number;
  needs_confirmation: number;
  rejected_requirements: number;
  satisfied: number;
  total_requirements: number;
}

/** Explainable skill gaps for one saved job.
 *
 * FIVE BUCKETS, not one "missing" list. "We found nothing", "you told us
 * this is not yours", and "we found evidence you have not reviewed" are
 * different things to tell a person. */
export interface JobGapResponse {
  /** Separate from skill_match_v1: the bucketing policy can change
   * without implying the score formula did. */
  formula_version: string;
  required_gaps: GapEntry[];
  preferred_gaps: GapEntry[];
  informational_gaps: GapEntry[];
  needs_confirmation: GapEntry[];
  rejected_requirements: GapEntry[];
  totals: GapTotals;
}

/** Recomputed server-side on every request from current rows, so
 * confirming a skill or editing the description is reflected
 * immediately — there is no stored gap to go stale. */
export async function getJobGaps(
  accessToken: string,
  savedJobId: string,
): Promise<JobGapResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}/gaps`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobGapResponse;
}

// --------------------------------------------------------------------
// Semantic relevance (Prompt 5.2b)
//
// SUPPORTING EVIDENCE, NOT A SKILL CLAIM AND NOT A RANKING. `fit` and
// `band` come from provisional thresholds tuned against a tiny fixture,
// so they indicate "there is relevant evidence worth reading", never
// "this candidate has skill X" or "this candidate is better than that
// one". Deliberately a separate request from getJobMatch: a cold or
// unconfigured model must never delay or break the deterministic score.
// --------------------------------------------------------------------

/** One piece of the candidate's own stored evidence that sits near the
 * job's wording. There is no skill field, on purpose. */
export interface SemanticEvidence {
  embedding_id: string;
  source_type: string;
  source_id: string;
  evidence_id: string;
  excerpt: string | null;
  evidence_source_type: string;
  evidence_source_identifier: string;
  similarity: number;
}

export interface JobSemanticResponse {
  formula_version: string;
  /** 0-20. NOT a percentage and not part of overall_score. */
  fit: number;
  /** "strong" | "moderate" | "weak" | "none" */
  band: string;
  model_identifier: string;
  considered: number;
  evidence: SemanticEvidence[];
}

export async function getJobSemantic(
  accessToken: string,
  savedJobId: string,
): Promise<JobSemanticResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}/semantic`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobSemanticResponse;
}

// --------------------------------------------------------------------
// Structured LLM explanation (Prompt 6.1)
//
// THE MODEL EXPLAINS; IT NEVER DECIDES. `overall_score` below is the
// same number `getJobMatch` returns — echoed from the persisted facts,
// not produced by a model, which can neither compute nor adjust it.
//
// A REJECTION IS A NORMAL 200. When `status` is "rejected" the score is
// still here and every generated field is empty: the API returns no
// ungrounded content, not even the parts that passed validation.
// --------------------------------------------------------------------

/** One real `skill_evidence` row an explanation cited. The excerpt is
 * the candidate's own stored text, hydrated server-side from the facts
 * — the model chooses which rows to point at and supplies none of their
 * words. */
export interface CitedEvidence {
  evidence_id: string;
  source_type: string;
  source_identifier: string;
  excerpt: string | null;
}

export interface ExplanationClaim {
  text: string;
  /** Always a subset of `cited_evidence`'s ids. */
  evidence_ids: string[];
}

export interface JobExplanationResponse {
  /** "generated" | "rejected" */
  status: string;
  /** Machine-readable, null when generated (e.g. "unknown_evidence_id"). */
  reason: string | null;
  schema_version: string;
  provider: string;
  /** Deterministic — present whether or not the explanation was accepted. */
  match_formula_version: string;
  overall_score: number;
  has_requirements: boolean;
  gap_formula_version: string;
  semantic_formula_version: string;
  /** Generated — null/empty when rejected. */
  summary: string | null;
  strengths: ExplanationClaim[];
  gaps: ExplanationClaim[];
  next_steps: string[];
  cited_evidence: CitedEvidence[];
}

export async function getJobExplanation(
  accessToken: string,
  savedJobId: string,
): Promise<JobExplanationResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}/explanation`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobExplanationResponse;
}

// --------------------------------------------------------------------
// Job eligibility (Prompt 5.1a)
//
// A SEPARATE DOMAIN FROM THE SKILL MATCH. Nothing below feeds into
// skill_match_v1 or skill_gap_v1, and the two are never shown as one
// number: "82% skill match" and "does not meet the CGPA bar" are
// different kinds of claim, and collapsing them would destroy both.
// --------------------------------------------------------------------

/** What the candidate has declared about themselves. `null` means
 * UNKNOWN — never zero, and never a default. Nothing here is inferred:
 * every value was typed by the person it describes. */
/** Where one fact came from, and whether the candidate has reviewed it.
 * `excerpt` is a verbatim resume line — never generated prose. */
export interface QualificationFactDetail {
  status: "suggested" | "confirmed" | "rejected";
  source_type: "resume" | "manual" | "github";
  source_identifier: string | null;
  excerpt: string | null;
  extraction_method: string | null;
  confidence: number | null;
}

export interface Qualifications {
  cgpa: string | null;
  /** Reported beside the value, always. A CGPA without its scale is not
   * a weaker signal, it is an incomparable one. */
  cgpa_scale: string | null;
  class_10_percentage: string | null;
  class_12_percentage: string | null;
  highest_degree: string | null;
  field_of_study: string | null;
  graduation_year: number | null;
  years_experience: string | null;
  updated_at: string | null;
  /** Per-fact provenance, keyed by fact type. Absent for a fact we do
   * not hold — which is how the UI tells "not found in your resume"
   * apart from a value of zero. */
  facts: Record<string, QualificationFactDetail>;
}

/** A partial update. An omitted key is left unchanged; an explicit
 * `null` clears the fact back to unknown. */
export type QualificationsUpdate = Partial<
  Omit<Qualifications, "updated_at" | "facts"> & { cgpa_scale: string | null }
>;

/** The four states, never collapsed to two.
 *
 * `unknown` means the CANDIDATE has not told us something and can fix
 * it. `undetermined` means the REQUIREMENT cannot be evaluated as
 * written — an unstated CGPA scale, or "or a related field" — and
 * nothing the candidate does resolves it. NEITHER is a failure. */
export type EligibilityState =
  "satisfied" | "not_satisfied" | "unknown" | "undetermined";

/** Not a percentage, deliberately: a CGPA floor and a degree
 * requirement are not commensurable, so weighting them against each
 * other would invent a judgement nothing can justify. */
export type EligibilityFlag = "eligible" | "not_eligible" | "unknown";

export type EligibilityRequirementType =
  | "cgpa"
  | "class_10_percentage"
  | "class_12_percentage"
  | "highest_degree"
  | "field_of_study"
  | "graduation_year"
  | "years_experience";

export type EligibilityComparator = "gte" | "lte" | "eq" | "in" | "between";

/** One bar, resolved against the caller's own declared facts. */
export interface EligibilityEntry {
  requirement_type: EligibilityRequirementType;
  state: EligibilityState;
  comparator: EligibilityComparator;
  requirement_numeric: string | null;
  requirement_max: string | null;
  requirement_scale: string | null;
  accepted_values: string[];
  requirement_level: string;
  candidate_numeric: string | null;
  candidate_text: string | null;
  candidate_scale: string | null;
  /** A stable machine token, never a sentence — the API owns the fact
   * and this layer owns the wording, so a copy change is not an API
   * change. */
  reason: string;
  /** A verbatim slice of the saved job's description. */
  excerpt: string;
}

export interface EligibilityTotals {
  satisfied: number;
  not_satisfied: number;
  unknown: number;
  undetermined: number;
  total_requirements: number;
  required_not_satisfied: number;
}

export interface JobEligibilityResponse {
  formula_version: string;
  flag: EligibilityFlag;
  /** False when the posting states no bar we recognise. Distinct from
   * "the candidate clears none of them" — both render as an empty list
   * and they mean opposite things. */
  has_requirements: boolean;
  /** False when the candidate has asserted no qualifications yet.
   * Separates "set up your profile" from "your profile has gaps" —
   * both otherwise arrive as a wall of `unknown`, and only one of them
   * is fixed by filling in a form. */
  has_qualification_profile: boolean;
  totals: EligibilityTotals;
  requirements: EligibilityEntry[];
}

export async function getQualifications(
  accessToken: string,
): Promise<Qualifications> {
  const response = await fetch(QUALIFICATIONS_BASE, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as Qualifications;
}

export async function updateQualifications(
  accessToken: string,
  updates: QualificationsUpdate,
): Promise<Qualifications> {
  const response = await fetch(QUALIFICATIONS_BASE, {
    method: "PATCH",
    credentials: "include",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(updates),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as Qualifications;
}

/** Recomputed server-side on every request from current rows. One edit
 * to the candidate's own facts changes this answer for every saved job
 * at once, which is exactly why no verdict is stored. */
export async function getJobEligibility(
  accessToken: string,
  savedJobId: string,
): Promise<JobEligibilityResponse> {
  const response = await fetch(`${SAVED_JOB_BASE}/${savedJobId}/eligibility`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as JobEligibilityResponse;
}

/** Record that an extracted fact is not the candidate's.
 *
 * A tombstone, not a delete: the fact reads as unknown afterwards, and
 * the next resume extraction will not re-suggest it. */
export async function rejectQualification(
  accessToken: string,
  factType: string,
): Promise<Qualifications> {
  const response = await fetch(`${QUALIFICATIONS_BASE}/${factType}`, {
    method: "DELETE",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  return (await response.json()) as Qualifications;
}
