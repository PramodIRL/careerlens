export type HealthStatus = "checking" | "ok" | "error";

export const STATUS_STYLES: Record<HealthStatus, string> = {
  checking: "bg-surface-2 text-muted",
  ok: "bg-green-500/10 text-green-700 dark:text-green-400",
  error: "bg-red-500/10 text-red-700 dark:text-red-400",
};

export const STATUS_LABEL: Record<HealthStatus, string> = {
  checking: "Checking API…",
  ok: "API is healthy",
  error: "API is unreachable",
};
