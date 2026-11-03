"use client";

import { useId, useState, useSyncExternalStore } from "react";

import { ApiError } from "@/lib/api-client";
import { checkAiKey, type KeyCheckResult } from "@/lib/ai-client";
import {
  clearAiKey,
  CURATED_MODELS,
  DEFAULT_AI_MODEL,
  readAiKey,
  readAiModel,
  saveAiSettings,
  subscribeAiSettings,
} from "@/lib/ai-settings";
import { useAuth } from "@/lib/auth-context";

const CUSTOM = "__custom__";

type Notice = { tone: "success" | "warning" | "danger"; text: string };

const NOTICE_STYLES: Record<Notice["tone"], string> = {
  success: "border-success/30 bg-success/10 text-success",
  warning: "border-warning/30 bg-warning/10 text-warning",
  danger: "border-danger/30 bg-danger/10 text-danger",
};

function describeCheck(result: KeyCheckResult): Notice {
  if (!result.model_found) {
    return {
      tone: "warning",
      text: `Your key works, but OpenRouter does not know the model "${result.model}". Check the id.`,
    };
  }
  if (result.model_supports_tools === false) {
    return {
      tone: "warning",
      text: `Your key works, but ${result.model} does not support tool calling, which the assistant needs. Pick another model.`,
    };
  }
  const spend =
    result.usage === null
      ? ""
      : result.limit === null
        ? ` Used so far: $${result.usage.toFixed(2)}.`
        : ` Used $${result.usage.toFixed(2)} of a $${result.limit.toFixed(2)} limit.`;
  return {
    tone: "success",
    text: `Key works and ${result.model} is ready.${spend}`,
  };
}

export default function AiSettingsPanel() {
  const { accessToken } = useAuth();
  const ids = useId();
  const storedKey = useSyncExternalStore(
    subscribeAiSettings,
    readAiKey,
    () => null,
  );
  const storedModel = useSyncExternalStore(
    subscribeAiSettings,
    readAiModel,
    () => DEFAULT_AI_MODEL,
  );

  // null means "not edited": the field shows what is stored.
  const [keyDraft, setKeyDraft] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState<string | null>(null);
  const [customPicked, setCustomPicked] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const key = keyDraft ?? storedKey ?? "";
  const model = modelDraft ?? storedModel;
  const isCurated = CURATED_MODELS.some((m) => m.id === model);
  const selectValue = customPicked || !isCurated ? CUSTOM : model;
  const curatedNote = CURATED_MODELS.find((m) => m.id === model)?.note;
  const dirty =
    (keyDraft !== null && keyDraft !== (storedKey ?? "")) ||
    (modelDraft !== null && modelDraft !== storedModel);

  function onSave() {
    saveAiSettings(key, model.trim() || DEFAULT_AI_MODEL);
    setKeyDraft(null);
    setModelDraft(null);
    setNotice({ tone: "success", text: "Saved in this browser." });
  }

  function onClear() {
    clearAiKey();
    setKeyDraft(null);
    setShowKey(false);
    setNotice({ tone: "success", text: "Key removed from this browser." });
  }

  async function onTest() {
    if (!accessToken || !key.trim()) return;
    setTesting(true);
    setNotice(null);
    try {
      const result = await checkAiKey(
        accessToken,
        key.trim(),
        model.trim() || DEFAULT_AI_MODEL,
      );
      setNotice(describeCheck(result));
    } catch (err) {
      setNotice({
        tone: "danger",
        text: err instanceof ApiError ? err.message : "something went wrong",
      });
    } finally {
      setTesting(false);
    }
  }

  const inputClass =
    "w-full rounded-xl border border-border px-3 py-2 text-sm text-foreground";
  const secondaryButton =
    "rounded-xl border border-border bg-surface px-3.5 py-2 text-xs font-semibold text-foreground hover:border-accent/50 disabled:opacity-50";

  return (
    <section
      aria-labelledby={`${ids}-heading`}
      className="rounded-2xl border border-border bg-surface p-5 shadow-[var(--shadow)] backdrop-blur-xl sm:p-6"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2
            id={`${ids}-heading`}
            className="text-base font-semibold tracking-tight text-foreground"
          >
            AI settings
          </h2>
          <p className="mt-1 text-xs text-muted">
            The assistant and resume tailor run on your own{" "}
            <a
              href="https://openrouter.ai/keys"
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-accent underline underline-offset-2"
            >
              OpenRouter key
            </a>
            . Usage is billed to your OpenRouter account.
          </p>
        </div>
        <span
          className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold ${
            storedKey
              ? "border-success/30 bg-success/10 text-success"
              : "border-border bg-surface-2 text-muted"
          }`}
        >
          {storedKey ? "Key saved" : "No key yet"}
        </span>
      </div>

      <div className="mt-5 grid gap-4">
        <div className="flex flex-col gap-1">
          <label
            htmlFor={`${ids}-key`}
            className="text-xs font-medium text-foreground"
          >
            OpenRouter API key
          </label>
          <div className="flex gap-2">
            <input
              id={`${ids}-key`}
              type={showKey ? "text" : "password"}
              value={key}
              onChange={(event) => setKeyDraft(event.target.value)}
              placeholder="sk-or-v1-..."
              autoComplete="off"
              spellCheck={false}
              className={`${inputClass} font-mono`}
            />
            <button
              type="button"
              onClick={() => setShowKey((shown) => !shown)}
              aria-pressed={showKey}
              className={secondaryButton}
            >
              {showKey ? "Hide" : "Show"}
            </button>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label
              htmlFor={`${ids}-model`}
              className="text-xs font-medium text-foreground"
            >
              Model
            </label>
            <select
              id={`${ids}-model`}
              value={selectValue}
              onChange={(event) => {
                const value = event.target.value;
                if (value === CUSTOM) {
                  setCustomPicked(true);
                } else {
                  setCustomPicked(false);
                  setModelDraft(value);
                }
              }}
              className={inputClass}
            >
              {CURATED_MODELS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
              <option value={CUSTOM}>Other model id...</option>
            </select>
            {selectValue !== CUSTOM && curatedNote && (
              <p className="text-[11px] text-muted">{curatedNote}</p>
            )}
          </div>
          {selectValue === CUSTOM && (
            <div className="flex flex-col gap-1">
              <label
                htmlFor={`${ids}-custom`}
                className="text-xs font-medium text-foreground"
              >
                Model id
              </label>
              <input
                id={`${ids}-custom`}
                value={model}
                onChange={(event) => setModelDraft(event.target.value)}
                placeholder="provider/model-name"
                spellCheck={false}
                className={`${inputClass} font-mono`}
              />
              <p className="text-[11px] text-muted">
                Must support tool calling. Test key checks it.
              </p>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onSave}
            disabled={!dirty}
            className="rounded-xl bg-accent px-3.5 py-2 text-xs font-semibold text-accent-foreground shadow-sm hover:bg-accent/90 disabled:opacity-50"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => void onTest()}
            disabled={testing || !key.trim() || !accessToken}
            className={secondaryButton}
          >
            {testing ? "Testing..." : "Test key"}
          </button>
          <button
            type="button"
            onClick={onClear}
            disabled={!storedKey}
            className="rounded-xl px-3.5 py-2 text-xs font-semibold text-danger hover:bg-danger/10 disabled:opacity-40"
          >
            Clear key
          </button>
        </div>

        {notice && (
          <p
            role={notice.tone === "danger" ? "alert" : "status"}
            className={`rounded-xl border px-3 py-2 text-xs ${NOTICE_STYLES[notice.tone]}`}
          >
            {notice.text}
          </p>
        )}

        <div className="rounded-xl border border-border bg-surface-2/70 p-3 text-xs leading-relaxed text-muted">
          <p className="font-semibold text-foreground">Privacy</p>
          <p className="mt-1">
            Your key is stored only in this browser and sent with each AI
            request. CareerLens does not save or log it. When you use the
            assistant or the tailor, your resume text, profile, skills and saved
            job details are sent to OpenRouter and the model provider you chose.
          </p>
        </div>
      </div>
    </section>
  );
}
