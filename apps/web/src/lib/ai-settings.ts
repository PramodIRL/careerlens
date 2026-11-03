/** The user's own OpenRouter key and model, kept in THIS browser only.
 *
 * Never sent to CareerLens storage: the key lives in localStorage and
 * travels per request in the `X-OpenRouter-Key` header, and the API
 * forwards it to OpenRouter without logging or storing it (see
 * apps/api/app/assistant/openrouter.py). Every storage access is wrapped
 * in try/catch, because private windows and blocked site data make
 * localStorage throw, and the AI panels must still render then. */

const KEY_STORAGE = "careerlens.openrouter.key";
const MODEL_STORAGE = "careerlens.openrouter.model";
const CHANGE_EVENT = "careerlens:ai-settings";

/** Supports tool calling and structured output; checked against
 * https://openrouter.ai/api/v1/models on 2026-11-03. */
export const DEFAULT_AI_MODEL = "anthropic/claude-haiku-5.5";

export interface CuratedModel {
  id: string;
  label: string;
  note: string;
}

/** A short list, all confirmed to support tools + JSON output on
 * OpenRouter on 2026-11-03. Any other id can be typed in. */
export const CURATED_MODELS: CuratedModel[] = [
  {
    id: "anthropic/claude-haiku-5.5",
    label: "Claude Haiku 5.5",
    note: "Fast, cheap, reliable with tools. Default.",
  },
  {
    id: "openai/gpt-6-luna",
    label: "GPT-6 Luna",
    note: "Small and cheap OpenAI model.",
  },
  {
    id: "google/gemini-3.8-flash",
    label: "Gemini 3.8 Flash",
    note: "Quick, with a very long context.",
  },
  {
    id: "deepseek/deepseek-v4.1-flash-1017",
    label: "DeepSeek V4.1 Flash",
    note: "Low cost open-weights option.",
  },
  {
    id: "anthropic/claude-sonnet-5.5",
    label: "Claude Sonnet 5.5",
    note: "Stronger reasoning, costs more per message.",
  },
];

function read(name: string): string | null {
  try {
    const value = window.localStorage.getItem(name);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

function write(name: string, value: string | null): void {
  try {
    if (value === null) {
      window.localStorage.removeItem(name);
    } else {
      window.localStorage.setItem(name, value);
    }
  } catch {
    // Storage unavailable: the setting simply does not persist.
  }
}

function announce(): void {
  try {
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // No window (server render): nothing to notify.
  }
}

export function readAiKey(): string | null {
  if (typeof window === "undefined") return null;
  return read(KEY_STORAGE);
}

export function readAiModel(): string {
  if (typeof window === "undefined") return DEFAULT_AI_MODEL;
  return read(MODEL_STORAGE) ?? DEFAULT_AI_MODEL;
}

export function saveAiSettings(key: string, model: string): void {
  write(KEY_STORAGE, key.trim() || null);
  write(MODEL_STORAGE, model.trim() || null);
  announce();
}

export function clearAiKey(): void {
  write(KEY_STORAGE, null);
  announce();
}

/** For useSyncExternalStore: fires on changes made in this tab (via
 * saveAiSettings/clearAiKey) and in other tabs (the storage event). */
export function subscribeAiSettings(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/** Snapshot helpers. Primitives, so useSyncExternalStore never loops. */
export function hasAiKeySnapshot(): boolean {
  return readAiKey() !== null;
}

export function hasAiKeyServerSnapshot(): boolean {
  return false;
}
