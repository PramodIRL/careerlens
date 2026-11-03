"use client";

import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { ApiError } from "@/lib/api-client";
import { sendChat, type ChatTurn } from "@/lib/ai-client";
import {
  hasAiKeyServerSnapshot,
  hasAiKeySnapshot,
  readAiModel,
  subscribeAiSettings,
  DEFAULT_AI_MODEL,
} from "@/lib/ai-settings";

import Markdown from "./markdown";

interface Message extends ChatTurn {
  id: number;
  toolsUsed?: string[];
}

// Mirrors MAX_CHAT_MESSAGES / MAX_CHAT_MESSAGE_CHARS in
// apps/api/app/schemas/assistant.py, so a long chat never 422s.
const MAX_HISTORY = 40;
const MAX_CHARS = 8000;

const MISSING_KEY = "Add your OpenRouter key in Settings";

const STARTERS = [
  "Which job am I closest to?",
  "What should I learn this week?",
  "What are my strongest skills, with evidence?",
  "How can my resume better fit my top job?",
];

const TOOL_LABELS: Record<string, string> = {
  get_profile: "Profile",
  get_skill_profile: "Skills",
  list_saved_jobs: "Saved jobs",
  get_job_fit: "Job fit",
  get_roadmap: "Roadmap",
  get_resume_text: "Resume",
};

function SparkIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      <path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z" />
      <path d="M19 15l.8 1.9 1.9.8-1.9.8L19 20.4l-.8-1.9-1.9-.8 1.9-.8z" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="h-4 w-4"
    >
      <path d="M5 12h13M13 6l6 6-6 6" />
    </svg>
  );
}

function ToolChips({ tools }: { tools: string[] }) {
  if (tools.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      <span className="text-[11px] text-muted">Checked</span>
      {tools.map((tool) => (
        <span
          key={tool}
          className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] font-medium text-muted"
        >
          {TOOL_LABELS[tool] ?? tool}
        </span>
      ))}
    </div>
  );
}

function ThinkingBubble() {
  return (
    <div className="flex items-start gap-3" role="status" aria-live="polite">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand text-accent-foreground shadow-sm">
        <SparkIcon />
      </div>
      <div className="rounded-2xl rounded-tl-md border border-border bg-surface-2 px-4 py-3">
        <div className="flex items-center gap-2 text-sm text-muted">
          <span className="flex gap-1" aria-hidden="true">
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:-0.3s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:-0.15s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent" />
          </span>
          Looking at your profile, jobs and skills...
        </div>
      </div>
    </div>
  );
}

function NoKeyState() {
  return (
    <div className="rounded-2xl border border-dashed border-border bg-surface-2/60 p-6 text-center">
      <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-brand text-accent-foreground shadow-sm">
        <SparkIcon className="h-5 w-5" />
      </div>
      <p className="mt-3 text-sm font-semibold text-foreground">
        Bring your own OpenRouter key
      </p>
      <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-muted">
        The assistant runs on a model you choose, billed to your own OpenRouter
        account. Your key stays in this browser.
      </p>
      <a
        href="#settings"
        className="mt-4 inline-flex items-center gap-1.5 rounded-xl bg-accent px-3.5 py-2 text-xs font-semibold text-accent-foreground shadow-sm hover:bg-accent/90"
      >
        Add your key in Settings
      </a>
    </div>
  );
}

export default function AssistantPanel({
  accessToken,
}: {
  accessToken: string;
}) {
  const hasKey = useSyncExternalStore(
    subscribeAiSettings,
    hasAiKeySnapshot,
    hasAiKeyServerSnapshot,
  );
  const model = useSyncExternalStore(
    subscribeAiSettings,
    readAiModel,
    () => DEFAULT_AI_MODEL,
  );
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nextId = useRef(1);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Scrolls the conversation box only, never the page around it.
    const box = scrollRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [messages, pending]);

  async function send(text: string) {
    const content = text.trim();
    if (!content || pending) return;
    const userMessage: Message = {
      id: nextId.current++,
      role: "user",
      content: content.slice(0, MAX_CHARS),
    };
    const conversation = [...messages, userMessage];
    setMessages(conversation);
    setDraft("");
    setError(null);
    setPending(true);
    try {
      const history: ChatTurn[] = conversation
        .slice(-MAX_HISTORY)
        .map(({ role, content: body }) => ({
          role,
          content: body.slice(0, MAX_CHARS),
        }));
      const result = await sendChat(accessToken, history);
      setMessages((current) => [
        ...current,
        {
          id: nextId.current++,
          role: "assistant",
          content: result.reply,
          toolsUsed: result.tools_used,
        },
      ]);
    } catch (err) {
      // Put the question back so nothing typed is lost.
      setMessages((current) => current.filter((m) => m.id !== userMessage.id));
      setDraft(content);
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setPending(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void send(draft);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void send(draft);
    }
  }

  const empty = messages.length === 0;

  return (
    <section
      aria-labelledby="assistant-heading"
      className="flex flex-col rounded-2xl border border-border bg-surface shadow-[var(--shadow)] backdrop-blur-xl"
    >
      <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand text-accent-foreground shadow-sm">
            <SparkIcon />
          </div>
          <div>
            <h2
              id="assistant-heading"
              className="text-base font-semibold tracking-tight text-foreground"
            >
              Career assistant
            </h2>
            <p className="text-xs text-muted">
              Answers from your own profile, skills, jobs and resume.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="hidden max-w-[12rem] truncate rounded-full border border-border bg-surface-2 px-2.5 py-1 font-mono text-[11px] text-muted sm:inline-block"
            title={model}
          >
            {model}
          </span>
          {!empty && (
            <button
              type="button"
              onClick={() => {
                setMessages([]);
                setError(null);
              }}
              disabled={pending}
              className="rounded-lg border border-border px-2.5 py-1 text-xs font-medium text-muted hover:text-foreground disabled:opacity-50"
            >
              New chat
            </button>
          )}
        </div>
      </header>

      <div
        ref={scrollRef}
        className="flex max-h-[34rem] min-h-[20rem] flex-col gap-5 overflow-y-auto scroll-smooth px-5 py-5"
        aria-live="polite"
      >
        {empty && !hasKey && <NoKeyState />}

        {empty && hasKey && (
          <div className="my-auto text-center">
            <p className="text-sm font-semibold text-foreground">
              Ask about your next move
            </p>
            <p className="mx-auto mt-1 max-w-md text-xs text-muted">
              The assistant reads your CareerLens data before it answers, and it
              will tell you when something is missing rather than guess.
            </p>
            <div className="mx-auto mt-5 grid max-w-xl gap-2 sm:grid-cols-2">
              {STARTERS.map((starter) => (
                <button
                  key={starter}
                  type="button"
                  onClick={() => void send(starter)}
                  className="rounded-xl border border-border bg-surface-2/70 px-3.5 py-2.5 text-left text-xs font-medium text-foreground hover:border-accent/50 hover:bg-surface-2"
                >
                  {starter}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-md bg-brand px-4 py-2.5 text-sm text-accent-foreground shadow-sm">
                {message.content}
              </div>
            </div>
          ) : (
            <div key={message.id} className="flex items-start gap-3">
              <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand text-accent-foreground shadow-sm">
                <SparkIcon />
              </div>
              <div className="min-w-0 max-w-[85%] rounded-2xl rounded-tl-md border border-border bg-surface-2 px-4 py-3 text-foreground">
                <Markdown source={message.content} />
                <ToolChips tools={message.toolsUsed ?? []} />
              </div>
            </div>
          ),
        )}

        {pending && <ThinkingBubble />}
      </div>

      {error && (
        <div
          role="alert"
          className="mx-5 mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          <span>{error}</span>
          {error === MISSING_KEY && (
            <a
              href="#settings"
              className="font-semibold underline underline-offset-2"
            >
              Open Settings
            </a>
          )}
        </div>
      )}

      <form onSubmit={onSubmit} className="border-t border-border p-3 sm:p-4">
        <div className="flex items-end gap-2 rounded-2xl border border-border bg-surface-2/60 p-1.5 focus-within:border-accent">
          <label htmlFor="assistant-input" className="sr-only">
            Message the career assistant
          </label>
          <textarea
            id="assistant-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            maxLength={MAX_CHARS}
            placeholder="Ask about jobs, skills or your resume..."
            className="max-h-40 min-h-[2.5rem] flex-1 resize-none rounded-xl border-0 bg-transparent px-3 py-2 text-sm focus-visible:shadow-none"
          />
          <button
            type="submit"
            disabled={pending || !draft.trim()}
            aria-label="Send"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent text-accent-foreground shadow-sm hover:bg-accent/90 disabled:opacity-40"
          >
            <SendIcon />
          </button>
        </div>
        <p className="mt-2 px-1 text-[11px] text-muted">
          Enter to send, Shift+Enter for a new line. Replies can be wrong: check
          anything important.
        </p>
      </form>
    </section>
  );
}
