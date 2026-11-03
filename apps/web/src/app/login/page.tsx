"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";

import AuthCard from "../auth-card";

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") ?? "");
    const password = String(form.get("password") ?? "");

    setSubmitting(true);
    setError(null);
    try {
      await login(email, password);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
      setSubmitting(false);
      queueMicrotask(() => errorRef.current?.focus());
    }
  }

  return (
    <AuthCard
      title="Log in"
      subtitle="Welcome back. Pick up where you left off."
      footer={
        <>
          Don&apos;t have an account?{" "}
          <Link
            href="/register"
            className="font-medium text-accent hover:underline"
          >
            Register
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
        {error && (
          <p
            id="form-error"
            ref={errorRef}
            role="alert"
            tabIndex={-1}
            className="rounded-lg bg-danger/10 px-3 py-2 text-sm text-danger"
          >
            {error}
          </p>
        )}
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="email"
            className="text-sm font-medium text-foreground"
          >
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            required
            autoComplete="email"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "form-error" : undefined}
            className="rounded-lg border border-border px-3 py-2.5 text-sm"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="password"
            className="text-sm font-medium text-foreground"
          >
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            required
            autoComplete="current-password"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "form-error" : undefined}
            className="rounded-lg border border-border px-3 py-2.5 text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="mt-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground shadow-sm shadow-accent/30 hover:bg-accent/90 disabled:opacity-50"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthCard>
  );
}
