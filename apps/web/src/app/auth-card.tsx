import type { ReactNode } from "react";

import BrandMark from "./brand-mark";

interface AuthCardProps {
  title: string;
  subtitle: string;
  footer: ReactNode;
  children: ReactNode;
}

/** The centered card the login and register pages share. */
export default function AuthCard({
  title,
  subtitle,
  footer,
  children,
}: AuthCardProps) {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center overflow-x-clip px-4 py-12">
      <div
        aria-hidden="true"
        className="bg-brand pointer-events-none absolute top-1/4 left-1/2 h-64 w-64 -translate-x-1/2 rounded-full opacity-20 blur-3xl sm:w-96"
      />
      <BrandMark className="relative mb-8" />
      <div className="card relative w-full max-w-sm p-6 sm:p-8">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1.5 mb-6 text-sm text-muted">{subtitle}</p>
        {children}
      </div>
      <p className="relative mt-6 text-sm text-muted">{footer}</p>
    </main>
  );
}
