"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth-context";

import ProfileForm from "./profile-form";
import ResumeSection from "./resume-section";
import SkillsSection from "./skills-section";

export default function DashboardPage() {
  const { status, user, accessToken, logout } = useAuth();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  // Protected navigation: the single redirect authority for leaving this
  // page whenever there's no valid session — covers both a mount-time
  // silent refresh (in AuthProvider) finding no session, and logout()
  // below setting status to "unauthenticated". handleLogout deliberately
  // does not also call router.push itself: a second, independent push to
  // the same href raced with this effect's (two overlapping App Router
  // transitions to /login), which could leave client navigation stuck.
  // See docs/decisions.md.
  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/login");
    }
  }, [status, router]);

  async function handleLogout() {
    setLoggingOut(true);
    await logout();
  }

  if (status === "loading") {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-zinc-50 p-8 dark:bg-black">
        <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
          Checking your session…
        </p>
      </main>
    );
  }

  if (status !== "authenticated" || !user || !accessToken) {
    // The redirect effect above is already firing; nothing meaningful
    // to show for the brief moment before navigation completes.
    return null;
  }

  return (
    <main className="flex min-h-screen flex-col items-center gap-6 bg-zinc-50 p-8 dark:bg-black">
      <div className="w-full max-w-sm text-center">
        <h1 className="mb-2 text-2xl font-semibold text-black dark:text-zinc-50">
          Dashboard
        </h1>
        <p className="mb-6 text-sm text-zinc-600 dark:text-zinc-400">
          Signed in as <span className="font-medium">{user.email}</span>
        </p>
        <button
          type="button"
          onClick={handleLogout}
          disabled={loggingOut}
          className="rounded bg-black px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-black"
        >
          {loggingOut ? "Logging out…" : "Log out"}
        </button>
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your profile
        </h2>
        <ProfileForm accessToken={accessToken} userId={user.id} />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your resumes
        </h2>
        <ResumeSection accessToken={accessToken} />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your skills
        </h2>
        <SkillsSection accessToken={accessToken} />
      </div>
    </main>
  );
}
