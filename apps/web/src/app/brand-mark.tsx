import { Aperture } from "lucide-react";
import Link from "next/link";

/** The CareerLens logo lockup, linking home. */
export default function BrandMark({ className = "" }: { className?: string }) {
  return (
    <Link
      href="/"
      className={`inline-flex items-center gap-2.5 font-semibold tracking-tight text-foreground ${className}`}
    >
      <span className="bg-brand flex size-8 items-center justify-center rounded-lg text-accent-foreground shadow-lg shadow-accent/25">
        <Aperture className="size-4.5" aria-hidden="true" />
      </span>
      <span className="text-[15px]">CareerLens</span>
    </Link>
  );
}
