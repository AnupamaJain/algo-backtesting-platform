"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const PRAMANA = process.env.NEXT_PUBLIC_PRAMANA_URL ?? "";

/**
 * Cross-application navigation, on every Lab page.
 *
 * Two gaps this closes. The sidebar carrying the Apps links is
 * `hidden lg:block`, so on anything narrower than 1024px the Lab had no
 * navigation at all -- not to the other apps, not even to its own pages.
 * And there was no way back: arriving here from the terminal, the only
 * route home was the browser's own button.
 *
 * The three surfaces are one product, so the same row appears in the
 * terminal's header (templates/_header_partial.html). Keep them in step.
 */
export default function TopBar() {
  const pathname = usePathname();
  const segments = pathname.split("/").filter(Boolean);
  const here = segments.length ? segments[segments.length - 1] : "overview";

  const link =
    "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] font-semibold " +
    "text-slate-400 hover:bg-[var(--color-panel2)] hover:text-slate-200";

  return (
    <header className="sticky top-0 z-40 flex h-11 items-center gap-1 border-b border-[var(--color-line-soft)] bg-[var(--color-panel)] px-3">
      <button
        type="button"
        onClick={() => history.back()}
        className={link}
        title="Back"
      >
        <span aria-hidden="true">←</span>
        <span className="hidden sm:inline">Back</span>
      </button>

      <span className="mx-1.5 h-4 w-px bg-[var(--color-line)]" aria-hidden="true" />

      <a href={`${PRAMANA}/app`} className={link}>
        Workspace
      </a>
      <a href={`${PRAMANA}/go/terminal`} className={link}>
        Terminal
      </a>
      <Link
        href="/"
        aria-current="page"
        className={
          link +
          " bg-[color-mix(in_srgb,var(--color-accent)_14%,transparent)] text-[var(--color-accent)]"
        }
      >
        Lab
      </Link>

      {/* Where you are inside the Lab, for the widths where the sidebar
          is not on screen to tell you. */}
      <span className="ml-auto truncate pl-3 text-[11px] uppercase tracking-wider text-slate-500 lg:hidden">
        {here.replace(/-/g, " ")}
      </span>
    </header>
  );
}
