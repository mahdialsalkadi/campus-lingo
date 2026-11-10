"use client";

import { Flame } from "lucide-react";
import { useMemo } from "react";

import { useQuizStore } from "@/stores/quizStore";

/**
 * Duolingo-style week strip for the streak mechanic.
 *
 * Seven day-dots (Sat..Fri, like the app) derived from the timestamps of
 * this week's graded attempts (`sessionAttempts[].at`), optionally seeded
 * with `since` (e.g. the server's streak-since date) — no new store fields
 * required. The flame sways calmly when today is already secured, and
 * flickers urgently while today's dot is still empty: the loss-aversion
 * nudge Duolingo is famous for.
 */

const DAY_KEYS = ["sat", "sun", "mon", "tue", "wed", "thu", "fri"] as const;
const DAY_AR: Record<(typeof DAY_KEYS)[number], string> = {
  sat: "س",
  sun: "ح",
  mon: "ن",
  tue: "ث",
  wed: "ر",
  thu: "خ",
  fri: "ج",
};

function dayKeyOf(date: Date): (typeof DAY_KEYS)[number] {
  // getDay(): 0=Sunday..6=Saturday — remap onto the Sat-first strip.
  return DAY_KEYS[(date.getDay() + 1) % 7];
}

function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export default function StreakCalendar({
  compact = false,
  since,
}: {
  compact?: boolean;
  /** Optional ISO date that counts as active (e.g. server streak anchor). */
  since?: string;
}) {
  const streak = useQuizStore((state) => state.streak);
  const attemptTimes = useQuizStore((state) =>
    state.sessionAttempts.map((attempt) => attempt.at),
  );

  const activeDays = useMemo(() => {
    const days = new Set<string>();
    if (since) days.add(isoDate(new Date(since)));
    for (const stamp of attemptTimes) days.add(isoDate(new Date(stamp)));
    return days;
  }, [since, attemptTimes]);

  const week = useMemo(() => {
    const today = new Date();
    const base = new Date(today);
    base.setDate(base.getDate() - 6); // strip starts six days back
    return Array.from({ length: 7 }, (_, index) => {
      const date = new Date(base);
      date.setDate(base.getDate() + index);
      const iso = isoDate(date);
      return {
        iso,
        key: dayKeyOf(date),
        isToday: iso === isoDate(today),
        active: activeDays.has(iso),
      };
    });
  }, [activeDays]);

  // The strip ends at today, so the last cell IS today.
  const todayDone = week[week.length - 1]?.active ?? false;

  return (
    <div
      className="flex items-center gap-3 rounded-2xl border-2 border-surface-border bg-surface px-3 py-2 shadow-card"
      title="سلسلة الأيام المتتالية"
    >
      <div className="flex flex-col items-center">
        <Flame
          className={`h-6 w-6 ${todayDone ? "flame-sway" : "flame-urgent"} text-feather-orange`}
          fill="currentColor"
          aria-hidden="true"
        />
        <span className="font-display text-base font-extrabold text-feather-orange">
          {streak}
        </span>
        <span className="sr-only">أيام متتالية</span>
      </div>

      <div className="flex items-center gap-1.5" aria-hidden="true">
        {week.map((day) => (
          <span key={day.iso} className="flex flex-col items-center gap-0.5">
            <span
              className={`flex h-6 w-6 items-center justify-center rounded-full border-2 font-display text-[10px] font-extrabold transition-colors ${
                day.active
                  ? "border-feather-orange bg-feather-orange text-white"
                  : day.isToday
                    ? "animate-pulse border-dashed border-feather-orange/70 bg-feather-orange/10 text-feather-orange"
                    : "border-surface-border bg-surface-muted text-ink-faint"
              }`}
            >
              {day.active ? "✓" : day.isToday ? "•" : ""}
            </span>
            {!compact && (
              <span
                className={`font-display text-[9px] font-bold ${
                  day.isToday ? "text-feather-orange" : "text-ink-faint"
                }`}
              >
                {DAY_AR[day.key]}
              </span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}
