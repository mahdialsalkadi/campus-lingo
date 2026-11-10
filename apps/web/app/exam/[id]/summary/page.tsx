"use client";

/**
 * /exam/[id]/summary — the diagnostic report.
 *
 * Letter grade + estimated exam readiness, a per-chapter weakness heatmap
 * (high/medium/low risk), and a **Drill Weak Spots** button that loads only the
 * failed concepts into a rapid-fire review session.
 */

import { Flame, Printer, Target, TrendingUp, Zap } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { api, getNodeChallenges, type ExamSummary, type QueueItem } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

const RISK_STYLES: Record<string, string> = {
  high: "border-feather-red/60 bg-feather-red/10 text-feather-red-dark",
  medium: "border-feather-yellow/60 bg-feather-yellow/15 text-ink",
  low: "border-feather-green/50 bg-feather-green/10 text-feather-green-dark",
};

const RISK_LABELS: Record<string, string> = {
  high: "خطر مرتفع",
  medium: "يحتاج مراجعة",
  low: "متقن",
};

const GRADE_STYLES: Record<string, string> = {
  A: "bg-feather-green text-white",
  B: "bg-feather-blue text-white",
  C: "bg-feather-yellow text-ink",
  D: "bg-feather-orange text-white",
  F: "bg-feather-red text-white",
};

export default function ExamSummaryPage({ params }: { params: { id: string } }) {
  const examId = params.id;
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);
  const startQueueSession = useQuizStore((state) => state.startQueueSession);

  const [summary, setSummary] = useState<ExamSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drilling, setDrilling] = useState(false);

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    (async () => {
      try {
        const body = await api.examSummary(examId, userId);
        if (!cancelled) setSummary(body);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "تعذّر تحميل التشخيص");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [examId, userId]);

  /**
   * Drill Weak Spots: pull the failed challenges back out of their nodes and
   * start a rapid-fire session with them — a real review queue, so the attempt
   * flows through FSRS exactly like any other practice.
   */
  const drillWeakSpots = useCallback(async () => {
    if (!summary || drilling) return;
    setDrilling(true);
    setError(null);
    try {
      const nodeIds = [...new Set(summary.weak_spots.map((spot) => spot.node_id).filter(Boolean))];
      const wanted = new Set(summary.weak_spots.map((spot) => spot.challenge_id));
      const items: QueueItem[] = [];
      for (const nodeId of nodeIds) {
        const node = await getNodeChallenges(nodeId as string);
        for (const challenge of node.challenges) {
          if (!wanted.has(challenge.id)) continue;
          items.push({
            card_id: "",
            challenge_id: challenge.id,
            node_id: node.node_id,
            node_title: node.node_title,
            lecture_id: node.lecture_id,
            type: challenge.type,
            payload_json: challenge.payload_json,
            state: 1,
            stability: 0,
            difficulty: challenge.difficulty ?? 0,
            due_date: new Date().toISOString(),
            retrievability: null,
            is_mistake: true,
            lapses: 1,
          });
        }
      }
      if (items.length === 0) {
        setError("لا تحديات قابلة لإعادة التدريب — قد تكون المحاضرة حُذفت.");
        return;
      }
      startQueueSession(items);
      router.push("/play/review");
    } catch (err) {
      setError(err instanceof Error ? err.message : "تعذّر بدء جلسة التدريب");
    } finally {
      setDrilling(false);
    }
  }, [drilling, router, startQueueSession, summary]);

  if (!userId) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="font-display text-lg font-extrabold text-ink-soft">
          سجّل الدخول لعرض التشخيص.
        </p>
      </main>
    );
  }

  if (loading) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="animate-pulse font-display text-lg font-extrabold text-ink-soft">
          جارٍ تحليل الأداء…
        </p>
      </main>
    );
  }

  if (!summary) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="font-display text-sm font-extrabold text-feather-red-dark">
          {error ?? "لا يوجد تشخيص بعد."}
        </p>
      </main>
    );
  }

  const gradeStyle = GRADE_STYLES[summary.grade] ?? "bg-surface-muted text-ink";

  return (
    <main className="flex flex-1 flex-col gap-4 px-4 py-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="font-display text-xl font-black text-ink">تشخيص الامتحان</h1>
          <p className="font-body text-xs text-ink-faint" dir="auto">
            {summary.course_name}
          </p>
        </div>
        <button
          type="button"
          onClick={() => window.print()}
          className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft active:translate-y-[2px] active:border-b-2"
        >
          <Printer className="h-3.5 w-3.5" aria-hidden="true" />
          طباعة التقرير
        </button>
      </header>

      {/* Score hero -------------------------------------------------------- */}
      <section className="flex flex-wrap items-center gap-4 rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
        <div
          className={`flex h-20 w-20 shrink-0 items-center justify-center rounded-3xl font-display text-4xl font-black ${gradeStyle}`}
        >
          {summary.grade}
        </div>
        <div className="min-w-0 flex-1">
          <p className="font-display text-2xl font-black text-ink">
            {summary.score}/{summary.total}{" "}
            <span className="font-body text-sm font-bold text-ink-faint">
              ({Math.round(summary.percent * 100)}%)
            </span>
          </p>
          <div className="mt-2 flex items-center gap-2">
            <Target className="h-4 w-4 text-feather-purple" aria-hidden="true" />
            <span className="font-body text-xs font-bold text-ink-soft">
              جاهزية تقديرية للامتحان
            </span>
            <span className="font-display text-sm font-black text-feather-purple">
              {Math.round(summary.readiness * 100)}%
            </span>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-surface-border">
            <div
              className="h-full rounded-full bg-feather-purple transition-[width] duration-700"
              style={{ width: `${Math.round(summary.readiness * 100)}%` }}
            />
          </div>
        </div>
      </section>

      {/* Weakness heatmap -------------------------------------------------- */}
      <section className="rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
        <h2 className="mb-3 flex items-center gap-2 font-display text-sm font-extrabold text-ink">
          <Flame className="h-4 w-4 text-feather-orange" aria-hidden="true" />
          خريطة نقاط الضعف حسب الفصل
        </h2>
        {summary.chapters.length === 0 ? (
          <p className="py-4 text-center font-body text-xs text-ink-faint">
            لا فصول في هذا الامتحان.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {summary.chapters.map((chapter) => (
              <li
                key={chapter.chapter}
                className={`rounded-2xl border-2 p-3 ${RISK_STYLES[chapter.risk] ?? RISK_STYLES.low}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="truncate font-body text-sm font-bold" dir="auto">
                    {chapter.chapter}
                  </span>
                  <span className="font-display text-[11px] font-extrabold">
                    {chapter.correct}/{chapter.total} · {Math.round(chapter.accuracy * 100)}% ·{" "}
                    {RISK_LABELS[chapter.risk] ?? chapter.risk}
                  </span>
                </div>
                {chapter.weak_concepts.length > 0 && (
                  <p className="mt-1 font-body text-[11px] opacity-90" dir="auto">
                    نقاط تحتاج مراجعة: {chapter.weak_concepts.join(" · ")}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Drill ------------------------------------------------------------- */}
      {summary.weak_spots.length > 0 && (
        <section className="rounded-3xl border-2 border-feather-purple/40 bg-feather-purple/5 p-4">
          <h2 className="mb-2 flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <TrendingUp className="h-4 w-4 text-feather-purple" aria-hidden="true" />
            عالج نقاط ضعفك الآن
          </h2>
          <p className="mb-3 font-body text-xs text-ink-soft">
            {summary.weak_spots.length} سؤالاً أخطأت فيه — جلسة سريعة تستهدف هذه المفاهيم فقط.
          </p>
          <ul className="mb-3 flex flex-wrap gap-1.5">
            {[...new Set(summary.weak_spots.map((spot) => spot.chapter))].map((chapter) => (
              <li
                key={chapter}
                className="rounded-lg bg-surface px-2 py-0.5 font-body text-[10px] font-bold text-ink-soft"
                dir="auto"
              >
                {chapter}
              </li>
            ))}
          </ul>
          <button
            type="button"
            disabled={drilling}
            onClick={() => void drillWeakSpots()}
            className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple px-4 py-2.5 font-display text-xs font-extrabold text-white disabled:opacity-50 active:translate-y-[2px] active:border-b-2"
          >
            <Zap className="h-3.5 w-3.5" aria-hidden="true" />
            {drilling ? "جارٍ التحضير…" : "تدرّب على نقاط الضعف"}
          </button>
        </section>
      )}

      {error && (
        <p className="rounded-2xl border-2 border-feather-red/40 bg-feather-red/10 px-3 py-2 font-body text-xs font-bold text-feather-red-dark">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Link
          href="/"
          className="rounded-2xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft"
        >
          عودة إلى المسار
        </Link>
        <Link
          href={`/exam/${examId}`}
          className="rounded-2xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft"
        >
          عرض ورقة المراجعة
        </Link>
      </div>
    </main>
  );
}
