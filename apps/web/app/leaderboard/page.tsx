"use client";

/**
 * /leaderboard — weekly leagues.
 *
 * Cohorts are real accounts in the same league tier, ranked on the XP earned
 * since Sunday 00:00. The top 5 climb a tier and the bottom 5 drop when the
 * cycle rolls over, which the server performs lazily on each read.
 */

import { motion } from "framer-motion";
import { Loader2, TrendingDown, TrendingUp, Trophy } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, type Leaderboard } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

/** Whole days/hours until the next Sunday midnight rollover. */
function countdown(iso: string | undefined): string {
  if (!iso) return "—";
  const target = new Date(iso).getTime();
  if (!Number.isFinite(target)) return "—";
  const delta = Math.max(0, target - Date.now());
  const days = Math.floor(delta / 86_400_000);
  const hours = Math.floor((delta % 86_400_000) / 3_600_000);
  return days > 0 ? `${days} يوم و${hours} ساعة` : `${hours} ساعة`;
}

export default function LeaderboardPage() {
  const ensureUser = useQuizStore((state) => state.ensureUser);
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const id = await ensureUser();
      setBoard(await api.leaderboard(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load the leaderboard");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main className="flex flex-1 flex-col gap-5 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 font-display text-2xl font-black text-ink">
            <Trophy className="h-6 w-6 text-feather-yellow" aria-hidden="true" />
            دوري {board?.league_name ?? "…"}
          </h1>
          <p className="mt-0.5 font-body text-sm text-ink-soft">
            30 طالباً يتنافسون على أعلى XP هذا الأسبوع · أعلى 5 يتأهلون، أدنى 5 يهبطون.
          </p>
        </div>
        <div className="text-end">
          <p className="font-body text-[11px] uppercase text-ink-faint">التبديل القادم بعد</p>
          <p className="font-display text-sm font-extrabold text-ink">
            {countdown(board?.rollover_at)}
          </p>
        </div>
      </header>

      {loading && (
        <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink-soft">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          جارٍ حساب ترتيبك…
        </p>
      )}
      {error && (
        <p className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-2.5 font-body text-sm text-feather-red-dark">
          {error}
        </p>
      )}

      {board && !loading && (
        <>
          <div className="flex flex-wrap gap-3">
            <div className="rounded-2xl border-2 border-surface-border bg-surface px-4 py-2.5">
              <p className="font-body text-[11px] uppercase text-ink-faint">ترتيبك</p>
              <p className="font-display text-xl font-black text-ink">
                {board.my_rank ?? "—"} <span className="text-sm text-ink-faint">/ {board.cohort_size}</span>
              </p>
            </div>
            <div className="rounded-2xl border-2 border-surface-border bg-surface px-4 py-2.5">
              <p className="font-body text-[11px] uppercase text-ink-faint">XP الأسبوع</p>
              <p className="font-display text-xl font-black text-feather-purple">{board.weekly_xp}</p>
            </div>
            <div
              className={`flex items-center gap-2 rounded-2xl border-2 px-4 py-2.5 font-display text-sm font-extrabold ${
                board.zone === "promotion"
                  ? "border-feather-green bg-feather-green/10 text-feather-green-dark"
                  : board.zone === "relegation"
                    ? "border-feather-red bg-feather-red/10 text-feather-red-dark"
                    : "border-surface-border bg-surface text-ink-soft"
              }`}
            >
              {board.zone === "promotion" ? (
                <TrendingUp className="h-4 w-4" aria-hidden="true" />
              ) : board.zone === "relegation" ? (
                <TrendingDown className="h-4 w-4" aria-hidden="true" />
              ) : null}
              {board.zone === "promotion"
                ? "منطقة التأهل"
                : board.zone === "relegation"
                  ? "منطقة الهبوط"
                  : "منطقة البقاء"}
            </div>
          </div>

          <ol className="flex flex-col gap-1.5">
            {board.rows.map((row) => {
              const promoting = row.rank <= board.promotion_zone;
              const relegating =
                board.cohort_size > board.promotion_zone &&
                row.rank > board.cohort_size - board.relegation_zone;
              return (
                <motion.li
                  key={row.user_id}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  className={`flex items-center gap-3 rounded-2xl border-2 px-3 py-2 font-display text-sm ${
                    row.is_me
                      ? "border-feather-purple bg-feather-purple/10 font-black text-ink"
                      : promoting
                        ? "border-feather-green/40 bg-feather-green/5 text-ink"
                        : relegating
                          ? "border-feather-red/40 bg-feather-red/5 text-ink-soft"
                          : "border-surface-border bg-surface text-ink-soft"
                  }`}
                >
                  <span className="w-7 text-center font-black tabular-nums">{row.rank}</span>
                  <span className="min-w-0 flex-1 truncate font-body text-xs font-bold" dir="ltr">
                    {row.email}
                    {row.is_me && <span className="ms-2 text-feather-purple">(أنت)</span>}
                  </span>
                  <span className="font-black tabular-nums text-feather-purple">
                    {row.weekly_xp} XP
                  </span>
                </motion.li>
              );
            })}
          </ol>

          {board.cohort_size < 5 && (
            <p className="rounded-2xl border-2 border-dashed border-surface-border px-4 py-3 font-body text-xs text-ink-faint">
              دوريك صغير الآن — كلما انضم طلاب أكثر (أو شاركت مقررك بكود CL) امتلأ الترتيب
              تلقائياً.
            </p>
          )}
        </>
      )}
    </main>
  );
}
