"use client";

/**
 * WarmupBanner — the sticky "Daily Memory Warm-up" ritual card at the very top
 * of the dashboard (milestone §2). Because the platform is web-only (no native
 * push), the review habit is made inescapable here:
 *
 * - total_due_count > 0  -> pulsing 🧠 + gold gem badge, the forgetting-curve
 *   subtitle, the 1.5x XP promise and a large tactile emerald CTA that opens
 *   the Rapid-Fire Review Arena (/review/warmup).
 * - total_due_count == 0 -> the calm "your memory is 100% active" badge.
 *
 * Data comes from GET /api/reviews/daily-queue; the bucket counts
 * (yesterday / midterm / critical) colour the mini stat chips.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, type DailyQueueResponse } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

/** Renders only while a userId exists; the dashboard gates on auth anyway. */
export default function WarmupBanner() {
  const userId = useQuizStore((state) => state.userId);
  const [queue, setQueue] = useState<DailyQueueResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    setLoading(true);
    api
      .dailyQueue(userId, 50)
      .then((body) => {
        if (!cancelled) setQueue(body);
      })
      .catch(() => {
        /* the banner stays silent when the ritual endpoint is unreachable */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (!userId || loading || !queue) {
    return null; // no flash of an empty ritual card during boot
  }

  const due = queue.total_due_count;

  if (due === 0) {
    // Calm, encouraging badge — the habit is rewarded, never nagged.
    return (
      <section className="flex items-center gap-3 rounded-3xl border-2 border-feather-green/40 bg-feather-green/10 px-4 py-3 shadow-card">
        <span className="text-2xl" aria-hidden="true">✨</span>
        <p className="font-body text-sm font-bold text-ink">
          ذاكرتك نشطة بنسبة 100%! لا توجد مراجعات مستحقة اليوم، أنت جاهز لدراسة مفاهيم جديدة.
        </p>
      </section>
    );
  }

  const minutes = queue.estimated_minutes;
  const subtitle = `لديك ${due} مفاهيم بدأت تتلاشى حسب منحنى النسيان (مما درسته بالأمس وقبل 3 أيام). ثبّتها في ${minutes} دقائق لتحصل على ${queue.xp_bonus_multiplier}x XP!`;

  return (
    <section className="relative overflow-hidden rounded-3xl border-2 border-b-4 border-feather-yellow/60 bg-gradient-to-b from-feather-yellow/15 to-surface px-4 py-4 shadow-card">
      {/* Streak-saver beat: the banner shouts harder when the flame is at stake. */}
      {queue.streak_saver && (
        <span className="absolute end-4 top-3 rounded-xl bg-feather-red/15 px-2 py-1 font-display text-[10px] font-extrabold uppercase text-feather-red">
          🔥 حماستك في خطر!
        </span>
      )}

      <div className="flex flex-wrap items-start gap-3">
        {/* Pulsing brain + gold gem badge */}
        <span className="brain-pulse relative flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-gradient-to-b from-feather-yellow/40 to-feather-orange/20 text-3xl" aria-hidden="true">
          🧠
          <span className="absolute -bottom-1 -end-1 flex h-6 w-6 items-center justify-center rounded-full border-2 border-surface bg-feather-yellow text-[11px] shadow-key-sm" aria-hidden="true">
            💎
          </span>
        </span>

        <div className="min-w-0 flex-1">
          <h2 className="font-display text-lg font-extrabold text-ink">
            إحماء الذاكرة اليومي <span className="text-ink-faint">(Daily Warm-up)</span>
          </h2>
          <p className="mt-0.5 font-body text-xs leading-relaxed text-ink-soft">{subtitle}</p>

          {/* Pedagogical bucket chips: Yesterday / Mid-Term / Critical Decay */}
          <div className="mt-2 flex flex-wrap gap-1.5 font-display text-[10px] font-extrabold">
            {queue.yesterday_count > 0 && (
              <span className="rounded-lg bg-feather-blue/15 px-2 py-0.5 text-feather-blue-dark">
                مراجعة الأمس · {queue.yesterday_count}
              </span>
            )}
            {queue.midterm_count > 0 && (
              <span className="rounded-lg bg-feather-purple/15 px-2 py-0.5 text-feather-purple">
                الأيام 3 و 7 · {queue.midterm_count}
              </span>
            )}
            {queue.critical_count > 0 && (
              <span className="rounded-lg bg-feather-red/15 px-2 py-0.5 text-feather-red">
                معرض للنسيان! · {queue.critical_count}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Large tactile emerald CTA */}
      <Link
        href="/review/warmup"
        className="mt-3 flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3.5 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0"
      >
        ⚡ ابدأ المراجعة السريعة ({minutes} دقائق)
      </Link>
    </section>
  );
}
