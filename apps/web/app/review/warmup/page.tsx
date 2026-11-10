"use client";

/**
 * Rapid-Fire Review Arena (/review/warmup) — the Daily Memory Warm-up ritual
 * player (milestone §3). Fast-paced, low-friction: only the due cards from
 * GET /api/reviews/daily-queue, drilled as MCQ / Contextual Cloze /
 * Spot-the-Mistake, with no type picker and no session modal in between.
 *
 * Flow: warmup fetch -> arena intro card -> rapid-fire drill (one card at a
 * time, micro-toast on every correct answer: "🎯 رائع! تم تمديد استقرار
 * المفهوم...") -> completion modal (confetti + XP bonus + habit checkmark +
 * one-click CTA back to new chapters).
 *
 * Query params: ?lecture=<id> filters the ritual to one chapter (the
 * ChapterFolder memory-health pill's mini-review), ?chapter=<title> names it.
 */

import confetti from "canvas-confetti";
import { motion } from "framer-motion";
import { ArrowLeft, CheckCircle2, Loader2, Timer, Zap } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Component,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import BossFight from "@/components/BossFight";
import ContextualCloze from "@/components/ContextualCloze";
import MatchMadness from "@/components/MatchMadness";
import McqQuiz from "@/components/McqQuiz";
import SpotImposter from "@/components/SpotImposter";
import SpotMistake from "@/components/SpotMistake";
import SwipeBoolean from "@/components/SwipeBoolean";
import TapToFill from "@/components/TapToFill";
import TrueFalse from "@/components/TrueFalse";
import DiagramQuestion from "@/components/DiagramQuestion";
import {
  ApiError,
  api,
  type ChallengeOutcome,
  type ChallengePayload,
  type ChallengeType,
  type ContextualClozePayload,
  type DailyQueueCard,
  type DailyQueueResponse,
  type DiagramQaPayload,
  type MatchMadnessPayload,
  type McqQuizPayload,
  type ReviewResponse,
  type BossFightPayload,
  type SpotImposterPayload,
  type SpotMistakePayload,
  type SwipeBooleanPayload,
  type TapToFillPayload,
  type TrueFalsePayload,
} from "@/lib/api";
import { playSfx, useQuizStore } from "@/stores/quizStore";

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

/** The micro-toast shown for ~2s after every correct recall (milestone §3). */
const EXTEND_TOAST = "🎯 رائع! تم تمديد استقرار المفهوم في ذاكرتك";

/** Shown when the queue is empty — the day's ritual is already done. */
const EMPTY_QUEUE_MESSAGE = "🎉 أحسنت! انتهيت من جميع مراجعات اليوم";

/**
 * Defensive unwrapping of one daily-queue card's challenge data.
 *
 * GET /api/reviews/daily-queue is specified to flatten `type` + `payload_json`
 * onto the card, but a legacy/proxied server can nest the same data under a
 * `challenge` key (or an inner `payload` envelope). Resolve every plausible
 * location so a shape drift degrades to "invalid card" (filtered out below) —
 * never to a blank challenge shell.
 */
function resolveChallengeData(card: DailyQueueCard): {
  type: ChallengeType | null;
  payload: ChallengePayload | null;
} {
  const raw = card as DailyQueueCard & {
    challenge?: DailyQueueCard | null;
    payload?: ChallengePayload | null;
  };
  const challenge = raw.challenge ?? card;
  const payload = card.payload_json ?? challenge?.payload_json ?? raw.payload ?? null;
  const type = card.type ?? challenge?.type ?? payload?.type ?? null;
  return { type: type ?? null, payload };
}

/**
 * Structural checks so an empty/corrupt payload never renders a dead card:
 * MATCH_MADNESS with 0 pairs, an MCQ with no (correct) option, a cloze with no
 * answer — all filtered before the session starts.
 */
function isValidPayload(type: ChallengeType, payload: ChallengePayload): boolean {
  switch (type) {
    case "MATCH_MADNESS": {
      const p = payload as MatchMadnessPayload;
      return (
        Array.isArray(p.pairs) &&
        p.pairs.length > 0 &&
        p.pairs.every(
          (pair) =>
            typeof pair?.term === "string" &&
            pair.term.trim() !== "" &&
            typeof pair?.definition === "string" &&
            pair.definition.trim() !== "",
        )
      );
    }
    case "MCQ_QUIZ": {
      const p = payload as McqQuizPayload;
      return (
        typeof p.question === "string" &&
        p.question.trim() !== "" &&
        Array.isArray(p.options) &&
        p.options.length > 0 &&
        p.options.some((option) => option?.correct === true)
      );
    }
    case "CONTEXTUAL_CLOZE":
    case "TAP_TO_FILL": {
      const p = payload as ContextualClozePayload | TapToFillPayload;
      return (
        typeof p.sentence_with_blank === "string" &&
        p.sentence_with_blank.includes("____") &&
        typeof p.correct_answer === "string" &&
        p.correct_answer.trim() !== "" &&
        Array.isArray(p.distractors) &&
        p.distractors.length > 0
      );
    }
    case "SPOT_MISTAKE": {
      const p = payload as SpotMistakePayload;
      return (
        typeof p.code_or_text === "string" &&
        p.code_or_text.trim() !== "" &&
        typeof p.mistake_token === "string" &&
        p.mistake_token.trim() !== "" &&
        typeof p.explanation === "string" &&
        p.explanation.trim() !== ""
      );
    }
    case "SWIPE_BOOLEAN":
    case "TRUE_FALSE": {
      const p = payload as SwipeBooleanPayload | TrueFalsePayload;
      return typeof p.statement === "string" && p.statement.trim() !== "" && typeof p.answer === "boolean";
    }
    case "SPOT_IMPOSTER": {
      const p = payload as SpotImposterPayload;
      return (
        Array.isArray(p.statements) &&
        p.statements.length > 0 &&
        p.statements.some((statement) => statement?.is_imposter === true)
      );
    }
    case "BOSS_FIGHT": {
      const p = payload as BossFightPayload;
      return Array.isArray(p.questions) && p.questions.length > 0;
    }
    case "DIAGRAM_QA": {
      const p = payload as DiagramQaPayload;
      return (
        typeof p.image_url === "string" &&
        p.image_url !== "" &&
        Array.isArray(p.questions) &&
        p.questions.length > 0
      );
    }
    default:
      return false;
  }
}

/**
 * Render boundary for one drill card.
 *
 * A daily-queue card can carry a payload the renderer does not expect (an
 * unknown type, a null field such as a missing `one_liner_rationale` or
 * `explanation`). One bad card must never blank the whole arena, so a crash
 * here degrades to a message plus a skip button and the ritual continues.
 */
class ChallengeBoundary extends Component<
  { children: ReactNode; resetKey: string; onSkip: () => void },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error) {
    console.error("warmup challenge failed to render", error);
  }

  componentDidUpdate(previous: { resetKey: string }) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="rounded-3xl border-2 border-dashed border-feather-red/40 bg-surface p-6 text-center">
          <p className="font-body text-sm text-ink-soft">
            تعذّر عرض هذه البطاقة — تابع إلى البطاقة التالية.
          </p>
          <button
            type="button"
            onClick={this.props.onSkip}
            className="mt-3 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2 font-display text-xs font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0"
          >
            تخطّي البطاقة
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

/** Which buckets this chapter mini-review is limited to (from ?lecture=). */
function useLectureFilter(): { lectureId: string | null; chapterTitle: string | null } {
  const params = useSearchParams();
  return {
    lectureId: params.get("lecture"),
    chapterTitle: params.get("chapter"),
  };
}

/** Drillable card: the daily-queue card with a renderable challenge payload. */
interface WarmupItem {
  card: DailyQueueCard;
  payload: ChallengePayload;
  type: ChallengeType;
}

function WarmupArena() {
  const router = useRouter();
  const { lectureId, chapterTitle } = useLectureFilter();

  const userId = useQuizStore((state) => state.userId);
  const email = useQuizStore((state) => state.email);
  const refreshStats = useQuizStore((state) => state.refreshStats);

  const [queue, setQueue] = useState<DailyQueueResponse | null>(null);
  const [items, setItems] = useState<WarmupItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [started, setStarted] = useState(false);
  const [index, setIndex] = useState(0);
  const [answered, setAnswered] = useState(false);
  const [outcome, setOutcome] = useState<ChallengeOutcome | null>(null);
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const [xpEarned, setXpEarned] = useState(0);
  const [correctCount, setCorrectCount] = useState(0);
  const startedAt = useRef<number>(Date.now());
  const submitting = useRef(false);

  const current = items[index] ?? null;
  const total = items.length;

  /** Bucket pill colors for the header chip row. */
  const bucketChip = useMemo(
    () =>
      queue
        ? [
            { count: queue.yesterday_count, label: "مراجعة الأمس", cls: "bg-feather-blue/15 text-feather-blue-dark" },
            { count: queue.midterm_count, label: "الأيام 3 و 7", cls: "bg-feather-purple/15 text-feather-purple" },
            { count: queue.critical_count, label: "معرض للنسيان!", cls: "bg-feather-red/15 text-feather-red" },
          ].filter((chip) => chip.count > 0)
        : [],
    [queue],
  );

  /** Load the warm-up queue; keep only cards with a renderable challenge. */
  useEffect(() => {
    if (!userId) {
      setLoading(false);
      setError("انتهت الجلسة — سجّل الدخول من جديد");
      return;
    }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const body = await api.dailyQueue(userId, 50);
        if (cancelled) return;
        let cards = body.cards;
        if (lectureId) {
          cards = cards.filter((card) => card.lecture_id === lectureId);
        }
        // Sanitize: resolve the challenge payload defensively and drop any
        // card that is empty or corrupt (MATCH_MADNESS with 0 pairs, MCQ with
        // no options, …) so the student only meets complete challenges.
        const sanitized: WarmupItem[] = [];
        for (const card of cards) {
          const { type, payload } = resolveChallengeData(card);
          if (!type || !payload || !isValidPayload(type, payload)) {
            console.warn(
              "warmup: skipping card with empty/corrupt challenge payload",
              card.challenge_id,
            );
            continue;
          }
          sanitized.push({ card, payload, type });
        }
        // The server envelope is frozen; the drillable truth is the sanitized
        // list, so re-derive the counts the intro card renders (25s/card is
        // the server's SECONDS_PER_CARD estimate).
        setQueue({
          ...body,
          cards,
          total_due_count: sanitized.length,
          estimated_minutes: Math.max(1, Math.round((sanitized.length * 25) / 60)),
          yesterday_count: sanitized.filter((item) => item.card.bucket === "yesterday").length,
          midterm_count: sanitized.filter((item) => item.card.bucket === "midterm").length,
          critical_count: sanitized.filter((item) => item.card.bucket === "critical").length,
        });
        setItems(sanitized);
      } catch (err) {
        if (!cancelled) setError(errorText(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId, lectureId]);

  /** Submit the graded attempt: FSRS + Ebbinghaus write-back on the server. */
  const submit = useCallback(
    async (result: ChallengeOutcome) => {
      if (!current || submitting.current || !userId) return;
      submitting.current = true;
      setAnswered(true);
      setOutcome(result);
      try {
        const body = await api.submitReview({
          user_id: userId,
          challenge_id: current.card.challenge_id,
          rating: ratingFor(result),
          elapsed_ms: result.elapsedMs,
          response_json: {
            source: "warmup",
            kind: current.type,
            ...(result.responseJson ?? {}),
          },
          context: "warmup",
        });
        setReview(body);
        setXpEarned((prev) => prev + body.xp_awarded);
        if (result.isCorrect) {
          setCorrectCount((prev) => prev + 1);
          setToast(EXTEND_TOAST);
          setTimeout(() => setToast(null), 2200);
        }
      } catch (err) {
        setError(errorText(err));
      } finally {
        submitting.current = false;
      }
    },
    [current, userId],
  );

  /** Advance to the next card, or fire the completion ritual. */
  const advance = () => {
    if (index + 1 >= total) {
      setFinished(true);
      playSfx("fanfare");
      void confetti({
        particleCount: 120,
        spread: 75,
        origin: { y: 0.7 },
        colors: ["#58CC02", "#1CB0F6", "#FFC800", "#FF9600", "#CE82FF", "#FF4B4B"],
      });
      void refreshStats();
      return;
    }
    setIndex((prev) => prev + 1);
    setAnswered(false);
    setOutcome(null);
    setReview(null);
    startedAt.current = Date.now();
    playSfx("whoosh");
  };

  // --- loading / error / intro ----------------------------------------------

  if (loading) {
    return (
      <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-4 px-4 py-6" aria-busy="true">
        {/* Loading skeleton: the arena's own layout (HUD + card) so the page
            never flashes an empty black container while the queue loads. */}
        <div className="flex items-center gap-3">
          <div className="h-3 flex-1 animate-pulse rounded-full bg-surface-border" />
          <div className="h-3 w-10 animate-pulse rounded-full bg-surface-border" />
        </div>
        <div className="h-5 w-2/3 animate-pulse rounded-full bg-surface-muted" />
        <div className="h-64 animate-pulse rounded-3xl border-2 border-surface-border bg-surface-muted" />
        <div className="mt-auto flex items-center justify-center gap-2 font-body text-xs text-ink-faint">
          <Loader2 className="h-4 w-4 animate-spin text-feather-blue" aria-hidden="true" />
          جارٍ تجهيز إحماء الذاكرة…
        </div>
      </main>
    );
  }

  if (error && !queue) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 px-4 py-16 text-center">
        <p className="font-display text-lg font-extrabold text-feather-red">{error}</p>
        <Link
          href="/"
          className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2.5 font-display text-sm font-extrabold uppercase text-white"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          العودة للرئيسية
        </Link>
      </main>
    );
  }

  // --- completion modal -------------------------------------------------------

  if (finished) {
    const perfect = correctCount === total && total > 0;
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-10">
        <motion.div
          initial={{ opacity: 0, scale: 0.92, y: 16 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          className="w-full max-w-md rounded-3xl border-2 border-b-4 border-feather-green/60 bg-surface p-6 text-center shadow-card"
        >
          <span className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-feather-green/15 text-4xl" aria-hidden="true">
            🧠✨
          </span>
          <h1 className="mt-3 font-display text-2xl font-black text-ink">انتهى الإحماء!</h1>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {perfect ? "جولة مثالية — ذاكرتك أقوى من منحنى النسيان!" : "أحسنت! مفاهيم اليوم أصبحت مثبتة."}
          </p>

          <div className="mt-4 grid grid-cols-3 gap-2">
            <div className="rounded-2xl border-2 border-surface-border p-2">
              <p className="font-body text-[10px] uppercase text-ink-faint">بطاقات</p>
              <p className="font-display text-lg font-extrabold text-ink">{total}</p>
            </div>
            <div className="rounded-2xl border-2 border-surface-border p-2">
              <p className="font-body text-[10px] uppercase text-ink-faint">صحيحة</p>
              <p className="font-display text-lg font-extrabold text-feather-green">{correctCount}</p>
            </div>
            <div className="rounded-2xl border-2 border-surface-border p-2">
              <p className="font-body text-[10px] uppercase text-ink-faint">XP (x{queue?.xp_bonus_multiplier ?? 1.5})</p>
              <p className="font-display text-lg font-extrabold text-feather-purple">+{xpEarned}</p>
            </div>
          </div>

          {/* Habit checkmark: today's Review Habit is done. */}
          <p className="mt-3 flex items-center justify-center gap-1.5 rounded-2xl border-2 border-feather-green/40 bg-feather-green/10 px-3 py-2 font-display text-xs font-extrabold text-feather-green-dark">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            عادة المراجعة اليومية مكتملة ✓
          </p>

          {/* One-click CTA: straight into new material. */}
          <Link
            href="/"
            className="mt-4 flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3.5 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0"
          >
            🚀 الانتقال لدراسة الفصول الجديدة
          </Link>
        </motion.div>
      </main>
    );
  }

  // --- intro card (bucket summary before the ritual starts) --------------------

  if (!started) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-10">
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
        >
          <span className="brain-pulse mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-gradient-to-b from-feather-yellow/40 to-feather-orange/20 text-4xl" aria-hidden="true">
            🧠
          </span>
          <h1 className="mt-3 text-center font-display text-xl font-black text-ink">
            {chapterTitle ? `مراجعة فصل: ${chapterTitle}` : "إحماء الذاكرة اليومي"}
          </h1>
          <p className="mt-1 text-center font-body text-sm text-ink-soft" dir="auto">
            {email ? `${queue?.total_due_count ?? items.length} مفاهيم مستحقة` : "مراجعة سريعة"} · حوالي{" "}
            {queue?.estimated_minutes ?? 1} دقيقة · مكافأة {queue?.xp_bonus_multiplier ?? 1.5}x XP
          </p>

          {bucketChip.length > 0 && (
            <div className="mt-3 flex flex-wrap justify-center gap-1.5 font-display text-[10px] font-extrabold">
              {bucketChip.map((chip) => (
                <span key={chip.label} className={`rounded-lg px-2 py-0.5 ${chip.cls}`}>
                  {chip.label} · {chip.count}
                </span>
              ))}
            </div>
          )}

          {items.length === 0 ? (
            <div className="mt-4 flex flex-col items-center gap-3">
              <p className="rounded-2xl border-2 border-dashed border-surface-border p-4 text-center font-display text-sm font-extrabold text-ink">
                {EMPTY_QUEUE_MESSAGE}
              </p>
              <p className="text-center font-body text-xs text-ink-soft">
                لا توجد مفاهيم مستحقة الآن — ذاكرتك نشطة 100%.
              </p>
              <Link
                href="/"
                className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2.5 font-display text-xs font-extrabold uppercase text-white"
              >
                <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                العودة للرئيسية
              </Link>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => {
                setStarted(true);
                startedAt.current = Date.now();
                playSfx("tap");
              }}
              className="mt-4 flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3.5 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0"
            >
              <Zap className="h-4 w-4" aria-hidden="true" />
              ابدأ المراجعة السريعة
            </button>
          )}
        </motion.div>
      </main>
    );
  }

  // --- the rapid-fire drill -----------------------------------------------------

  const progress = total > 0 ? (index / total) * 100 : 0;

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col px-4 py-4">
      {/* HUD: progress bar, counter, XP bonus */}
      <header className="flex items-center gap-3">
        <div className="h-3 flex-1 overflow-hidden rounded-full bg-surface-border">
          <motion.div
            className="h-full rounded-full bg-feather-green"
            animate={{ width: `${progress}%` }}
            transition={{ type: "spring", stiffness: 90, damping: 20 }}
          />
        </div>
        <span className="font-display text-xs font-extrabold text-ink-soft">
          {index + 1}/{total}
        </span>
        <span className="flex items-center gap-1 rounded-lg bg-feather-purple/15 px-2 py-0.5 font-display text-[10px] font-extrabold text-feather-purple">
          <Zap className="h-3 w-3" aria-hidden="true" /> x{queue?.xp_bonus_multiplier ?? 1.5} XP
        </span>
      </header>

      {/* Card context: bucket + chapter + decay info */}
      <div className="mt-2 flex flex-wrap items-center gap-1.5 font-display text-[10px] font-extrabold text-ink-faint">
        <span className={`rounded-lg px-2 py-0.5 ${
          current?.card.bucket === "critical"
            ? "bg-feather-red/15 text-feather-red"
            : current?.card.bucket === "midterm"
              ? "bg-feather-purple/15 text-feather-purple"
              : "bg-feather-blue/15 text-feather-blue-dark"
        }`}>
          {current?.card.bucket_label ?? ""}
        </span>
        {current?.card.node_title && <span dir="auto">{current.card.node_title}</span>}
        <span className="flex items-center gap-1">
          <Timer className="h-3 w-3" aria-hidden="true" />
          استقرار {(current?.card.stability ?? 0).toFixed(1)} ·{" "}
          {Math.round((current?.card.retrievability_score ?? 0) * 100)}% تذكّر متوقع
        </span>
      </div>

      {/* The challenge itself (reuses the lesson components one-to-one) */}
      <section className="mt-4 flex-1">
        <ChallengeBoundary resetKey={current?.card.challenge_id ?? "empty"} onSkip={advance}>
          <motion.div key={current?.card.challenge_id} initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }}>
            {current ? renderChallenge(current, submit) : null}
          </motion.div>
        </ChallengeBoundary>
      </section>

      {/* Micro-toast: the memory-extension feedback on correct answers */}
      {toast && (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          className="pointer-events-none fixed inset-x-0 bottom-24 z-50 mx-auto w-fit rounded-2xl border-2 border-feather-green bg-feather-green/10 bg-surface px-4 py-2 font-display text-xs font-extrabold text-feather-green-dark shadow-card"
          role="status"
        >
          {toast}
        </motion.div>
      )}

      {/* Footer: continue after grading */}
      <footer className="mt-4 pb-6">
        {answered && outcome && (
          <motion.button
            type="button"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            onClick={advance}
            className={`flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 px-4 py-3.5 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0 ${
              outcome.isCorrect
                ? "border-feather-green-dark bg-feather-green"
                : "border-feather-red-dark bg-feather-red"
            }`}
          >
            {outcome.isCorrect ? "متابعة" : "المحاولة التالية"}
            {review && (
              <span className="rounded-lg bg-white/20 px-1.5 py-0.5 text-[10px]">
                +{review.xp_awarded} XP
              </span>
            )}
          </motion.button>
        )}
      </footer>
    </main>
  );
}

/** Map the raw outcome onto the FSRS 1..4 scale (fast + correct = Easy). */
function ratingFor(result: ChallengeOutcome): 1 | 2 | 3 | 4 {
  if (!result.isCorrect) return 1;
  if (result.hintsUsed > 0) return 2;
  if (result.elapsedMs > 0 && result.elapsedMs <= 8_000) return 4;
  if (result.elapsedMs >= 45_000) return 2;
  return 3;
}

/** Render the drillable challenge with the shared lesson components. */
function renderChallenge(item: WarmupItem, onResult: (outcome: ChallengeOutcome) => void) {
  const shared = { challengeId: item.card.challenge_id, payload: item.payload, onResult };
  switch (item.type) {
    case "MCQ_QUIZ":
      return <McqQuiz {...shared} payload={item.payload as McqQuizPayload} />;
    case "CONTEXTUAL_CLOZE":
      return <ContextualCloze {...shared} payload={item.payload as ContextualClozePayload} />;
    case "SPOT_MISTAKE":
      return <SpotMistake {...shared} payload={item.payload as SpotMistakePayload} />;
    case "TRUE_FALSE":
      return <TrueFalse {...shared} payload={item.payload as TrueFalsePayload} />;
    case "SWIPE_BOOLEAN":
      return <SwipeBoolean {...shared} payload={item.payload as SwipeBooleanPayload} />;
    case "SPOT_IMPOSTER":
      return <SpotImposter {...shared} payload={item.payload as SpotImposterPayload} />;
    case "TAP_TO_FILL":
      return <TapToFill {...shared} payload={item.payload as TapToFillPayload} />;
    case "BOSS_FIGHT":
      return <BossFight {...shared} payload={item.payload as BossFightPayload} />;
    case "DIAGRAM_QA":
      return <DiagramQuestion {...shared} payload={item.payload as DiagramQaPayload} />;
    case "MATCH_MADNESS": {
      // The real tap-to-pair game renders in the drill too. The queue is
      // sanitized upstream so pairs/seconds are always present (never an empty
      // shell, and no inner continue button — the footer owns advancement).
      const mm = item.payload as MatchMadnessPayload;
      return (
        <MatchMadness
          {...shared}
          payload={{
            ...mm,
            pairs: mm.pairs ?? [],
            seconds: mm.seconds || 60,
          }}
        />
      );
    }
    default:
      // Unknown/unsupported kind: never leave the learner stuck on a card with
      // no way forward — offer an explicit skip.
      return (
        <div className="rounded-3xl border-2 border-dashed border-surface-border bg-surface p-6 text-center">
          <p className="font-body text-sm text-ink-soft">
            نوع بطاقة غير مدعوم في الإحماء — تابع إلى البطاقة التالية.
          </p>
          <button
            type="button"
            onClick={() => onResult({ isCorrect: true, elapsedMs: 0, hintsUsed: 0, responseJson: {} })}
            className="mt-3 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2 font-display text-xs font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0"
          >
            تخطّي البطاقة
          </button>
        </div>
      );
  }
}

export default function WarmupPage() {
  // useSearchParams needs a Suspense boundary during prerendering.
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center px-4 py-16">
          <Loader2 className="h-8 w-8 animate-spin text-feather-blue" aria-hidden="true" />
        </main>
      }
    >
      <WarmupArena />
    </Suspense>
  );
}
