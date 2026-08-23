"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth-context";

import ProfileForm from "./profile-form";

export default function DashboardPage() {
  const { status, user, accessToken, logout } = useAuth();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  // Protected navigation: if the mount-time silent refresh (in
  // AuthProvider) determined there's no valid session, leave.
  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/login");
    }
  }, [status, router]);

  async function handleLogout() {
    setLoggingOut(true);
    await logout();
    router.push("/login");
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
    </main>
  );
}
