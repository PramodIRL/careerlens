const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const AUTH_BASE = `${API_BASE}/api/v1/auth`;

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
