/** Fetch helpers for the AI assistant and resume tailor
 * (apps/api/app/api/v1/ai.py). Same shape as api-client.ts: bearer
 * auth, credentials included, and any non-2xx thrown as its ApiError
 * with the API's own message, so a 400 "Add your OpenRouter key in
 * Settings" reaches the UI word for word. */

import { ApiError } from "@/lib/api-client";
import { readAiKey, readAiModel } from "@/lib/ai-settings";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const AI_BASE = `${API_BASE}/api/v1/ai`;

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatReply {
  /** Markdown. */
  reply: string;
  /** Distinct tool names, in the order the assistant first used them. */
  tools_used: string[];
  model: string;
}

export interface TailoredBullet {
  original: string;
  rewritten: string;
  why: string;
}

export interface TailorResult {
  summary: string;
  bullets: TailoredBullet[];
  keywords: string[];
  /** Skills the job wants that the resume lacks. Shown as gaps, never
   * written into a bullet. Always includes CareerLens's computed gaps. */
  missing_skills: string[];
  ats_note: string;
  saved_job_id: string;
  job_title: string;
  company: string;
  resume_id: string;
  resume_filename: string;
  /** skill_match_v1 from /match, echoed. */
  match_score: number;
  model: string;
}

export interface KeyCheckResult {
  model: string;
  /** False when OpenRouter does not know this model id. */
  model_found: boolean;
  model_supports_tools: boolean | null;
  usage: number | null;
  limit: number | null;
  limit_remaining: number | null;
  is_free_tier: boolean | null;
}

/** Overrides for "Test key", which checks what is typed in the form
 * before it is saved. Everything else uses the stored settings. */
interface AiCredentials {
  key?: string | null;
  model?: string | null;
}

function aiHeaders(
  accessToken: string,
  credentials: AiCredentials = {},
): Record<string, string> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${accessToken}`,
    "Content-Type": "application/json",
  };
  const key = credentials.key ?? readAiKey();
  const model = credentials.model ?? readAiModel();
  // No header at all when there is no key, so the API's server
  // fallback (if configured) or its clean 400 applies.
  if (key) headers["X-OpenRouter-Key"] = key;
  if (model) headers["X-OpenRouter-Model"] = model;
  return headers;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const { detail } = body as { detail?: unknown };
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail) && typeof detail[0]?.msg === "string") {
        return detail[0].msg as string;
      }
    }
  } catch {
    // Not JSON: fall through.
  }
  return "something went wrong";
}

async function request<T>(
  path: string,
  accessToken: string,
  init: { method: "GET" | "POST"; body?: unknown; credentials?: AiCredentials },
): Promise<T> {
  const response = await fetch(`${AI_BASE}${path}`, {
    method: init.method,
    credentials: "include",
    headers: aiHeaders(accessToken, init.credentials),
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response));
  }
  return (await response.json()) as T;
}

/** One assistant turn. The whole visible conversation is sent each
 * time: nothing about it is stored server-side. */
export function sendChat(
  accessToken: string,
  messages: ChatTurn[],
): Promise<ChatReply> {
  return request<ChatReply>("/chat", accessToken, {
    method: "POST",
    body: { messages },
  });
}

/** Tailor a resume to one saved job. Without `resumeId`, the API uses
 * the newest resume whose text was extracted. */
export function tailorResume(
  accessToken: string,
  savedJobId: string,
  resumeId?: string | null,
): Promise<TailorResult> {
  return request<TailorResult>("/tailor", accessToken, {
    method: "POST",
    body: resumeId
      ? { saved_job_id: savedJobId, resume_id: resumeId }
      : { saved_job_id: savedJobId },
  });
}

/** "Test key": checks the key with OpenRouter and that the model id
 * exists. Generates no tokens, so it costs nothing. */
export function checkAiKey(
  accessToken: string,
  key: string,
  model: string,
): Promise<KeyCheckResult> {
  return request<KeyCheckResult>("/key", accessToken, {
    method: "GET",
    credentials: { key, model },
  });
}
