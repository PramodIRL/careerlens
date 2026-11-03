import {
  Bot,
  Briefcase,
  FolderGit2,
  LayoutDashboard,
  Route,
  Settings,
  Sparkles,
  UserRound,
  WandSparkles,
  type LucideIcon,
} from "lucide-react";
import { useSyncExternalStore } from "react";

export type DashboardView =
  | "overview"
  | "profile"
  | "resume"
  | "jobs"
  | "roadmap"
  | "skills"
  | "assistant"
  | "tailor"
  | "settings";

export interface ViewConfig {
  id: DashboardView;
  label: string;
  /** Shown under the page title while the view is open. */
  description: string;
  icon: LucideIcon;
  group: "workspace" | "ai" | "account";
}

export const VIEWS: ViewConfig[] = [
  {
    id: "overview",
    label: "Overview",
    description: "Where your profile stands and what to do next.",
    icon: LayoutDashboard,
    group: "workspace",
  },
  {
    id: "profile",
    label: "Profile",
    description:
      "Who you are, what you are aiming for, and the facts employers check.",
    icon: UserRound,
    group: "workspace",
  },
  {
    id: "resume",
    label: "Resume & GitHub",
    description: "The sources CareerLens reads your skills from.",
    icon: FolderGit2,
    group: "workspace",
  },
  {
    id: "jobs",
    label: "Jobs",
    description: "Saved postings with matches, gaps and eligibility for each.",
    icon: Briefcase,
    group: "workspace",
  },
  {
    id: "roadmap",
    label: "Roadmap",
    description: "A learning plan built from your saved jobs and your gaps.",
    icon: Route,
    group: "workspace",
  },
  {
    id: "skills",
    label: "Skills",
    description: "Every skill your evidence supports, and the ones to review.",
    icon: Sparkles,
    group: "workspace",
  },
  {
    id: "assistant",
    label: "AI Assistant",
    description: "Ask about your profile, your matches and your plan.",
    icon: Bot,
    group: "ai",
  },
  {
    id: "tailor",
    label: "Resume Tailor",
    description: "Rework your resume for a specific saved job.",
    icon: WandSparkles,
    group: "ai",
  },
  {
    id: "settings",
    label: "Settings",
    description: "Choose how the AI features run for your account.",
    icon: Settings,
    group: "account",
  },
];

/** In-page anchors that live inside a view. Following one opens that
 * view and then scrolls to the element with the same id. */
const ANCHOR_VIEWS: Record<string, DashboardView> = {
  qualifications: "profile",
};

const VIEW_IDS = new Set<string>(VIEWS.map((view) => view.id));

export function resolveHash(hash: string): {
  view: DashboardView;
  anchor: string | null;
} {
  const key = hash.replace(/^#/, "");
  if (VIEW_IDS.has(key)) return { view: key as DashboardView, anchor: null };
  if (key in ANCHOR_VIEWS) return { view: ANCHOR_VIEWS[key], anchor: key };
  return { view: "overview", anchor: null };
}

function subscribe(onChange: () => void) {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

/** The current `location.hash`, re-read on every hashchange. */
export function useLocationHash(): string {
  return useSyncExternalStore(
    subscribe,
    () => window.location.hash,
    () => "",
  );
}
