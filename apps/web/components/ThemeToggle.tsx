"use client";

/**
 * Light/dark toggle for the dashboard header.
 *
 * Renders a neutral icon on the server and until mount (the persisted theme is
 * only known client-side), then flips the store's `theme` — which drives both
 * the icon and the <html data-theme> attribute that app/theme.css keys off.
 */

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { applyThemeToDom, useQuizStore } from "@/stores/quizStore";

export default function ThemeToggle() {
  const theme = useQuizStore((state) => state.theme);
  const toggleTheme = useQuizStore((state) => state.toggleTheme);
  // Unknown until hydration: the persisted value is not available on the server.
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    // Re-sync the DOM attribute after rehydration (the no-flash script in
    // app/layout.tsx already set it correctly for the first paint).
    applyThemeToDom(useQuizStore.getState().theme);
  }, []);

  useEffect(() => {
    if (mounted) applyThemeToDom(theme);
  }, [mounted, theme]);

  const isDark = mounted && theme === "dark";
  const label = isDark ? "التبديل إلى الوضع الفاتح" : "التبديل إلى الوضع الداكن";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="rounded-xl border-2 border-surface-border px-2 py-1"
      title={label}
      aria-label={label}
      aria-pressed={isDark}
    >
      {mounted ? (
        isDark ? (
          <Moon className="h-4 w-4 text-feather-blue" aria-hidden="true" />
        ) : (
          <Sun className="h-4 w-4 text-feather-orange" aria-hidden="true" />
        )
      ) : (
        <Moon className="h-4 w-4 text-ink-faint" aria-hidden="true" />
      )}
    </button>
  );
}
