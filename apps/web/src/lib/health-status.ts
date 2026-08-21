export type HealthStatus = "checking" | "ok" | "error";

export const STATUS_STYLES: Record<HealthStatus, string> = {
  checking: "bg-zinc-200 text-zinc-800",
  ok: "bg-green-100 text-green-800",
  error: "bg-red-100 text-red-800",
};

export const STATUS_LABEL: Record<HealthStatus, string> = {
  checking: "Checking API…",
  ok: "API is healthy",
  error: "API is unreachable",
};
