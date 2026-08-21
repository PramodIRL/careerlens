"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type HealthStatus = "checking" | "ok" | "error";

const STATUS_STYLES: Record<HealthStatus, string> = {
  checking: "bg-zinc-200 text-zinc-800",
  ok: "bg-green-100 text-green-800",
  error: "bg-red-100 text-red-800",
};

const STATUS_LABEL: Record<HealthStatus, string> = {
  checking: "Checking API…",
  ok: "API is healthy",
  error: "API is unreachable",
};

export default function Home() {
  const [status, setStatus] = useState<HealthStatus>("checking");
  const [detail, setDetail] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function checkHealth() {
      try {
        const response = await fetch(`${API_URL}/api/v1/health`, {
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`API responded with status ${response.status}`);
        }
        const data = (await response.json()) as unknown;
        if (!cancelled) {
          setStatus("ok");
          setDetail(JSON.stringify(data));
        }
      } catch (error) {
        if (!cancelled) {
          setStatus("error");
          setDetail(error instanceof Error ? error.message : "Unknown error");
        }
      }
    }

    void checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-zinc-50 p-8 dark:bg-black">
      <h1 className="text-2xl font-semibold text-black dark:text-zinc-50">
        CareerLens
      </h1>
      <div
        data-testid="api-health-status"
        className={`rounded-full px-4 py-2 text-sm font-medium ${STATUS_STYLES[status]}`}
      >
        {STATUS_LABEL[status]}
      </div>
      {detail && (
        <p className="max-w-md text-center text-xs text-zinc-500 dark:text-zinc-400">
          {detail}
        </p>
      )}
    </main>
  );
}
