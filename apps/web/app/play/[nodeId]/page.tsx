"use client";

import confetti from "canvas-confetti";
import { motion } from "framer-motion";
import { CheckCircle2, Heart, Home, RotateCcw, Trophy, XCircle, Zap } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import BossFight from "@/components/BossFight";
import ContextualCloze from "@/components/ContextualCloze";
import MatchMadness from "@/components/MatchMadness";
import McqQuiz from "@/components/McqQuiz";
import SpotImposter from "@/components/SpotImposter";
import SpotMistake from "@/components/SpotMistake";
import SwipeBoolean from "@/components/SwipeBoolean";
import TapToFill from "@/components/TapToFill";
import TrueFalse from "@/components/TrueFalse";
import TutorDrawer, { TUTOR_HINT_COST } from "@/components/TutorDrawer";
import {
  ApiError,
  CHALLENGE_ICONS,
  CHALLENGE_LABELS,
  CHALLENGE_TYPES,
  RATINGS,
  api,
  formatInterval,
  getNodeChallenges,
  type ChallengeOutcome,
  type ChallengePayload,
  type ChallengeType,
  type ContextualClozePayload,
  type DiagramQaPayload,
  type MatchMadnessPayload,
  type McqQuizPayload,
  type BossFightPayload,
  type NodeChallengesResponse,
  type QueueItem,
  type SpotImposterPayload,
  type SpotMistakePayload,
  type SwipeBooleanPayload,
  type TapToFillPayload,
  type TrueFalsePayload,
} from "@/lib/api";
import DiagramQuestion from "@/components/DiagramQuestion";
import { playSfx, useQuizStore } from "@/stores/quizStore";

const REVIEW_NODE_ID = "review";

/** Short tab labels for the review type filter (matches the filter bar spec). */
const REVIEW_FILTER_LABELS: Partial<Record<ChallengeType, string>> = {
  MCQ_QUIZ: "خيارات",
  SPOT_MISTAKE: "اكتشف الخطأ",
  CONTEXTUAL_CLOZE: "أكمل الفراغ",
  MATCH_MADNESS: "مطابقة",
  TRUE_FALSE: "صح أم خطأ",
  SWIPE_BOOLEAN: "اسحب",
  SPOT_IMPOSTER: "الدخيل",
  TAP_TO_FILL: "أكمل",
  BOSS_FIGHT: "الزعيم",
  DIAGRAM_QA: "فهم الرسم",
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

export default function PlayPage({ params }: { params: { nodeId: string } }) {
  const nodeId = params.nodeId;
  const router = useRouter();

  const userId = useQuizStore((state) => state.userId);
  const xp = useQuizStore((state) => state.xp);
  const gems = useQuizStore((state) => state.gems);
  const queue = useQuizStore((state) => state.queue);
  const index = useQuizStore((state) => state.index);
  const clearedChallengeIds = useQuizStore((state) => state.clearedChallengeIds);
  const sessionXp = useQuizStore((state) => state.sessionXp);
  const correctCount = useQuizStore((state) => state.correctCount);
  const wrongCount = useQuizStore((state) => state.wrongCount);
  const sessionMistakes = useQuizStore((state) => state.sessionMistakes);
  const lastReview = useQuizStore((state) => state.lastReview);
  const finishedAt = useQuizStore((state) => state.finishedAt);
  const submitting = useQuizStore((state) => state.submitting);

  const ensureUser = useQuizStore((state) => state.ensureUser);
  const refreshStats = useQuizStore((state) => state.refreshStats);
  const startNodeSession = useQuizStore((state) => state.startNodeSession);
  const startQueueSession = useQuizStore((state) => state.startQueueSession);
  const recordAttempt = useQuizStore((state) => state.recordAttempt);
  const submitSessionRating = useQuizStore((state) => state.submitSessionRating);
  const addXp = useQuizStore((state) => state.addXp);
  const nextChallenge = useQuizStore((state) => state.nextChallenge);
  const inRemedial = useQuizStore((state) => state.inRemedial);
  const resetSession = useQuizStore((state) => state.resetSession);

  const [node, setNode] = useState<NodeChallengesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answered, setAnswered] = useState(false);
  const [outcome, setOutcome] = useState<ChallengeOutcome | null>(null);
  const [ratedInterval, setRatedInterval] = useState<{ rating: number; interval: number } | null>(null);
  const confettiFired = useRef(false);
  // Session combo: consecutive correct answers (resets on a miss) — the
  // multiplier badge in the header that makes streaks feel earned.
  const [combo, setCombo] = useState(0);
  const [bestCombo, setBestCombo] = useState(0);
  // Immersion: intercept the ✕ with a progress confirmation modal.
  const [exitPrompt, setExitPrompt] = useState(false);
  // Pre-session type chooser: the fetched node is held here until the learner
  // confirms which challenge kinds to train, then the session starts filtered.
  const [pendingNode, setPendingNode] = useState<NodeChallengesResponse | null>(null);
  const [pickerSelection, setPickerSelection] = useState<ChallengeType[]>([]);
  // Review customizer: the fetched FSRS queue is held here until the learner
  // picks which challenge kinds to drill. Filtering is client-side only — no
  // card is ever removed from the database. Review mode waits on this picker.
  const [pendingReviewItems, setPendingReviewItems] = useState<QueueItem[] | null>(null);
  const [reviewSelection, setReviewSelection] = useState<ChallengeType[]>([]);
  // Socratic tutor drawer (💡 أحتاج تلميحاً) — 10 gems per streamed hint.
  const [hintOpen, setHintOpen] = useState(false);

  const current = queue[index] ?? null;
  const isReviewMode = nodeId === REVIEW_NODE_ID;
  const total = queue.length;
  const finished = finishedAt !== null && total > 0;

  // Duolingo-style progress: cleared UNIQUE challenges / total UNIQUE
  // challenges. Recycled mistake clones keep their original id, so the
  // denominator stays stable when a mistake is re-queued.
  const totalUniqueCount = useMemo(
    () => new Set(queue.map((item) => item.challengeId)).size,
    [queue],
  );
  const clearedUniqueCount = clearedChallengeIds.length;

  // -- bootstrap -------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const id = await ensureUser();
        if (cancelled) return;
        if (isReviewMode) {
          const { items } = await api.queue(id, 20, true);
          if (cancelled) return;
          const kinds = Array.from(new Set(items.map((item) => item.type))) as ChallengeType[];
          if (kinds.length > 1) {
            // More than one kind is due: offer the type filter pills first.
            setReviewSelection(kinds);
            setPendingReviewItems(items);
          } else {
            startQueueSession(items);
          }
        } else {
          const response = await getNodeChallenges(nodeId);
          if (cancelled) return;
          setNode(response);
          const kinds = new Set(response.challenges.map((c) => c.type));
          if (kinds.size > 1) {
            // More than one kind available: show the type chooser first.
            setPickerSelection([...kinds] as ChallengeType[]);
            setPendingNode(response);
          } else {
            startNodeSession(response);
          }
        }
      } catch (err) {
        if (!cancelled) setError(errorText(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  // Which kinds exist in the pending node's deck (drives the pre-session picker).
  const availableTypes = useMemo(() => {
    if (!pendingNode) return [];
    return CHALLENGE_TYPES.filter((type) =>
      pendingNode.challenges.some((challenge) => challenge.type === type),
    );
  }, [pendingNode]);

  // Review customizer derivations: present kinds, whether "All" is active, and
  // the filtered queue the session will actually load.
  const reviewTypes = useMemo(() => {
    if (!pendingReviewItems) return [];
    return CHALLENGE_TYPES.filter((type) =>
      pendingReviewItems.some((item) => item.type === type),
    );
  }, [pendingReviewItems]);

  const reviewAllSelected =
    pendingReviewItems !== null &&
    reviewTypes.length > 0 &&
    reviewSelection.length === reviewTypes.length;

  const filteredReviewItems = useMemo(() => {
    if (!pendingReviewItems) return [];
    return pendingReviewItems.filter((item) => reviewSelection.includes(item.type));
  }, [pendingReviewItems, reviewSelection]);

  useEffect(() => {
    // Pull server-authoritative gems/XP (passive gem regen included) when the
    // session starts. Hearts are infinite — no refill/regen plumbing here.
    void ensureUser().then(() => refreshStats());
    return () => resetSession();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-arm on every queue POSITION, not on challengeId: a recycled mistake
  // clone carries the same challengeId as its original, so a challengeId-keyed
  // reset would miss it (and its `key` would suppress the remount below).
  useEffect(() => {
    setAnswered(false);
    setOutcome(null);
  }, [index]);

  // End-of-session celebration
  useEffect(() => {
    if (!finished || confettiFired.current) return;
    confettiFired.current = true;
    if (wrongCount === 0) {
      void confetti({ particleCount: 160, spread: 80, origin: { y: 0.7 } });
    } else {
      void confetti({ particleCount: 70, spread: 60, origin: { y: 0.75 } });
    }
  }, [finished, wrongCount]);

  const handleResult = useCallback(
    async (result: ChallengeOutcome) => {
      if (!current || answered) return;
      setAnswered(true);
      setOutcome(result);
      // Combo counter: consecutive correct answers, reset on a miss.
      setCombo((prev) => {
        const next = result.isCorrect ? prev + 1 : 0;
        setBestCombo((best) => Math.max(best, next));
        return next;
      });
      if (result.isCorrect) {
        playSfx("correct");
      } else {
        playSfx("incorrect");
        // Missed questions are recorded by recordAttempt and surface in a
        // single "⚡ جولة تصحيح سريعة" once the main set is cleared. They are
        // never re-queued inline, so the session can always end.
      }
      await recordAttempt({
        challengeId: current.challengeId,
        challengeType: current.type,
        isCorrect: result.isCorrect,
        elapsedMs: result.elapsedMs,
        hintsUsed: result.hintsUsed,
        responseJson: result.responseJson,
      });
    },
    [answered, current, recordAttempt],
  );

  // Keyboard shortcuts (1-4) → pick option N in MCQ/cloze-style challenges.
  // Components expose their pickable options via data-pick attributes; we
  // forward keydown to the first matching visible button.
  useEffect(() => {
    if (answered || finished) return;
    const onKey = (event: KeyboardEvent) => {
      const slot = Number(event.key);
      if (!Number.isInteger(slot) || slot < 1 || slot > 4) return;
      const buttons = Array.from(
        document.querySelectorAll<HTMLButtonElement>("[data-pick]"),
      ).filter((button) => button.getBoundingClientRect().width > 0);
      const target = buttons[slot - 1];
      if (target && !target.disabled) {
        event.preventDefault();
        target.click();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [answered, finished, index]);

  const progressPct =
    totalUniqueCount === 0 ? 0 : (clearedUniqueCount / totalUniqueCount) * 100;
  const accuracy =
    correctCount + wrongCount === 0
      ? 0
      : Math.round((correctCount / (correctCount + wrongCount)) * 100);

  const renderChallenge = () => {
    if (!current) return null;
    const payload = current.payload as ChallengePayload;
    const shared = {
      // Position-unique key: a recycled clone (same challengeId, new index)
      // must remount fresh instead of inheriting its original's solved state.
      key: `${current.challengeId}-${index}`,
      challengeId: current.challengeId,
      onResult: handleResult,
      // Hearts are infinite: wrong answers never drain lives, so the
      // onHeartLoss hook is intentionally not wired up.
      onXp: addXp,
      disabled: finished,
    };
    switch (payload.type) {
      case "MATCH_MADNESS":
        return <MatchMadness {...shared} payload={payload as MatchMadnessPayload} />;
      case "SPOT_MISTAKE":
        return <SpotMistake {...shared} payload={payload as SpotMistakePayload} />;
      case "CONTEXTUAL_CLOZE":
        return <ContextualCloze {...shared} payload={payload as ContextualClozePayload} />;
      case "MCQ_QUIZ":
        return <McqQuiz {...shared} payload={payload as McqQuizPayload} />;
      case "TRUE_FALSE":
        return <TrueFalse {...shared} payload={payload as TrueFalsePayload} />;
      case "SWIPE_BOOLEAN":
        return <SwipeBoolean {...shared} payload={payload as SwipeBooleanPayload} />;
      case "SPOT_IMPOSTER":
        return <SpotImposter {...shared} payload={payload as SpotImposterPayload} />;
      case "TAP_TO_FILL":
        return <TapToFill {...shared} payload={payload as TapToFillPayload} />;
      case "BOSS_FIGHT":
        return <BossFight {...shared} payload={payload as BossFightPayload} />;
      case "DIAGRAM_QA":
        return <DiagramQuestion {...shared} payload={payload as DiagramQaPayload} />;
      default:
        return (
          <p className="font-body text-sm text-ink-soft" dir="auto">
            نوع تحدٍّ غير معروف: {current.type}
          </p>
        );
    }
  };

  // Text handed to the tutor model: the prompt plus whatever context we can
  // extract without leaking the answer (explanations are passed as background
  // only — the system prompt forbids revealing the answer).
  const hintContext = useMemo(() => {
    if (!current) return null;
    const payload = current.payload;
    let question = "";
    let context: string | null = null;
    switch (payload.type) {
      case "MCQ_QUIZ":
        question = payload.question;
        context = payload.explanation;
        break;
      case "TRUE_FALSE":
        question = payload.statement;
        context = payload.explanation;
        break;
      case "CONTEXTUAL_CLOZE":
        question = payload.sentence_with_blank;
        context = payload.explanation;
        break;
      case "SPOT_MISTAKE":
        question = payload.code_or_text;
        context = payload.explanation;
        break;
      case "MATCH_MADNESS":
        question = `${payload.instructions}\n${payload.pairs.map((pair) => `${pair.term} = ${pair.definition}`).join("\n")}`;
        break;
      case "SWIPE_BOOLEAN":
        question = payload.statement;
        context = payload.one_liner_rationale;
        break;
      case "SPOT_IMPOSTER":
        question = `${payload.instructions}\n${payload.statements.map((item, i) => `${i + 1}. ${item.text}`).join("\n")}`;
        context = payload.one_liner_rationale;
        break;
      case "TAP_TO_FILL":
        question = payload.sentence_with_blank;
        context = payload.one_liner_rationale;
        break;
      case "BOSS_FIGHT":
        question = `${payload.boss_name}: ${payload.questions[0]?.question ?? ""}`;
        break;
      case "DIAGRAM_QA":
        question = `${payload.caption || "figure"}\n${payload.questions[0]?.question ?? ""}`;
        context = payload.explanation;
        break;
    }
    const choice =
      outcome && !outcome.isCorrect
        ? JSON.stringify((outcome.responseJson as { given?: unknown } | null)?.given ?? null)
        : null;
    return { challengeId: current.challengeId, question, context, choice };
  }, [current, outcome]);

  const ratingHint = useMemo(() => {
    if (!answered) return "";
    if (outcome?.isCorrect) {
      return outcome.elapsedMs <= 8000 ? "سريع ودقيق — يستحق Easy" : "إجابة صحيحة";
    }
    return inRemedial
      ? "أُضيفت إلى دفتر الأخطاء وستُجدول عبر FSRS — لن تتكرر في هذه الجلسة"
      : "أُضيفت إلى دفتر الأخطاء وستظهر في جولة التصحيح السريعة";
  }, [answered, outcome, inRemedial]);

  // -- states ---------------------------------------------------------------

  if (loading) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-10">
        <p className="animate-pulse font-display text-lg font-extrabold text-ink-soft">
          جارٍ تحضير الجلسة…
        </p>
      </main>
    );
  }

  // Pre-session type chooser: pick which question kinds to train before the
  // lesson starts (all available kinds pre-checked). Skipped in review mode —
  // the FSRS queue decides what is due.
  if (pendingNode !== null) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-5 px-4 py-10">
        <div className="text-center">
          <h1 className="font-display text-xl font-extrabold text-ink">اختر أنواع الأسئلة</h1>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {pendingNode.node_title ?? "هذه الوحدة"} — {pendingNode.count} تحديداً متاحة
          </p>
        </div>
        <div className="flex w-full max-w-sm flex-col gap-2">
          {availableTypes.map((type) => {
            const checked = pickerSelection.includes(type);
            const count = pendingNode.challenges.filter((c) => c.type === type).length;
            return (
              <button
                key={type}
                type="button"
                onClick={() =>
                  setPickerSelection((prev) =>
                    prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type],
                  )
                }
                className={`flex items-center justify-between rounded-2xl border-2 border-b-4 px-4 py-3 font-display text-sm font-extrabold transition-colors ${
                  checked
                    ? "border-feather-green bg-feather-green/15 text-ink"
                    : "border-surface-border bg-surface text-ink-faint"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span aria-hidden="true">{CHALLENGE_ICONS[type]}</span>
                  {CHALLENGE_LABELS[type].ar}
                </span>
                <span className="font-body text-xs font-bold text-ink-faint">{count}</span>
              </button>
            );
          })}
        </div>
        <button
          type="button"
          disabled={pickerSelection.length === 0}
          onClick={() => {
            if (!pendingNode) return;
            startNodeSession(pendingNode, pickerSelection);
            setPendingNode(null);
          }}
          className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-8 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] disabled:opacity-50"
        >
          ابدأ الجلسة
        </button>
      </main>
    );
  }

  // Review customizer: pick which question kinds to drill from the due queue.
  // This only filters the in-memory session queue — no card is modified or
  // deleted in the database, so selecting "MCQ" loads a targeted MCQ-only drill.
  if (pendingReviewItems !== null) {
    const countFor = (type: ChallengeType) =>
      pendingReviewItems.filter((item) => item.type === type).length;
    const toggleType = (type: ChallengeType) =>
      setReviewSelection((prev) =>
        prev.includes(type) ? prev.filter((entry) => entry !== type) : [...prev, type],
      );
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-5 px-4 py-10">
        <div className="text-center">
          <h1 className="font-display text-xl font-extrabold text-ink">جلسة المراجعة</h1>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {pendingReviewItems.length} بطاقة مستحقة — اختر أنواع الأسئلة التي تريد التدرب عليها
          </p>
        </div>
        <div className="flex w-full max-w-md flex-wrap justify-center gap-2">
          <button
            type="button"
            onClick={() => setReviewSelection(reviewAllSelected ? [] : reviewTypes)}
            className={`rounded-full border-2 border-b-4 px-4 py-2 font-display text-xs font-extrabold transition-colors ${
              reviewAllSelected
                ? "border-feather-blue-dark bg-feather-blue text-white"
                : "border-surface-border bg-surface text-ink-faint"
            }`}
          >
            الكل
          </button>
          {reviewTypes.map((type) => {
            const checked = reviewSelection.includes(type);
            return (
              <button
                key={type}
                type="button"
                onClick={() => toggleType(type)}
                className={`flex items-center gap-1.5 rounded-full border-2 border-b-4 px-3.5 py-2 font-display text-xs font-extrabold transition-colors ${
                  checked
                    ? "border-feather-green bg-feather-green/15 text-ink"
                    : "border-surface-border bg-surface text-ink-faint"
                }`}
              >
                <span aria-hidden="true">{CHALLENGE_ICONS[type]}</span>
                {REVIEW_FILTER_LABELS[type] ?? CHALLENGE_LABELS[type].ar}
                <span className="font-body text-[11px] font-bold text-ink-faint">
                  {countFor(type)}
                </span>
              </button>
            );
          })}
        </div>
        <button
          type="button"
          disabled={filteredReviewItems.length === 0}
          onClick={() => {
            startQueueSession(filteredReviewItems);
            setPendingReviewItems(null);
          }}
          className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-8 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] disabled:opacity-50"
        >
          ابدأ الجلسة ({filteredReviewItems.length})
        </button>
      </main>
    );
  }

  if (error) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 px-4 py-10">
        <p className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-3 font-body text-sm text-feather-red-dark">
          {error}
        </p>
        <Link
          href="/"
          className="rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-5 py-3 font-display text-sm font-extrabold uppercase text-ink"
        >
          لوحة التحكم
        </Link>
      </main>
    );
  }

  if (total === 0) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 px-4 py-10 text-center">
        <p className="font-display text-xl font-extrabold text-ink">
          {isReviewMode ? "لا يوجد ما هو مستحق الآن 🎉" : "لا توجد تحديات لهذه الوحدة"}
        </p>
        <p className="max-w-md font-body text-sm text-ink-soft">
          {isReviewMode
            ? "جدول FSRS لا يطلب مراجعة أي بطاقة الآن. جرّب وحدة من خريطة الوحدات أو ارجع لاحقاً."
            : "أعد رفع ملف المحاضرة من لوحة التحكم لتوليد تحديات هذه الوحدة."}
        </p>
        <Link
          href="/"
          className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-5 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key"
        >
          رجوع
        </Link>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-1 flex-col px-4 py-5 sm:px-6">
      {/* Progress bar + combo + hearts ------------------------------------- */}
      <header className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => setExitPrompt(true)}
          className="font-display text-lg font-extrabold text-ink-faint transition-colors hover:text-feather-red"
          aria-label="خروج من الجلسة"
        >
          ✕
        </button>
        <div className="mx-2 h-3 flex-1 overflow-hidden rounded-full bg-surface-border">
          <motion.div
            className="h-full rounded-full bg-feather-green"
            animate={{ width: `${progressPct}%` }}
            transition={{ type: "spring", stiffness: 120, damping: 20 }}
          />
        </div>
        {combo >= 2 && (
          <motion.span
            key={combo}
            initial={{ scale: 0.6, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            className="rounded-xl bg-feather-orange/15 px-2 py-0.5 font-display text-xs font-black text-feather-orange"
            title={`أفضل سلسلة: ${bestCombo}`}
          >
            🔥 ×{combo}
          </motion.span>
        )}
        <div className="flex items-center gap-2 font-display text-sm font-bold">
          {/* Infinite hearts: no lives are ever lost during practice. */}
          <span
            className="flex items-center gap-1 text-feather-red"
            title="قلوب لا نهائية — تدريب بلا قيود"
          >
            <Heart className="h-4 w-4 fill-feather-red text-feather-red" aria-hidden="true" />
            ∞
          </span>
          <span className="flex items-center gap-1 text-feather-purple">
            <Zap className="h-4 w-4" aria-hidden="true" />
            {xp}
          </span>
        </div>
      </header>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 font-body text-xs text-ink-faint">
        <span dir="auto">
          {isReviewMode ? "جلسة المراجعة" : (node?.node_title ?? current?.nodeTitle ?? "درس")} ·
          {" "}أُنجز {clearedUniqueCount} من {totalUniqueCount}
        </span>
        <span dir="auto">
          {CHALLENGE_LABELS[current?.type as keyof typeof CHALLENGE_LABELS]?.ar ?? current?.type}
        </span>
      </div>

      {/* One-shot remedial round banner ------------------------------- */}
      {inRemedial && !finished && (
        <motion.div
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          className="mt-3 flex items-center justify-center gap-2 rounded-2xl border-2 border-feather-orange bg-feather-orange/15 px-4 py-2 text-center font-display text-sm font-extrabold text-feather-orange"
        >
          ⚡ جولة تصحيح سريعة — الأسئلة التي أخطأت فيها مرة واحدة فقط
        </motion.div>
      )}

      <div className="mt-5 flex flex-1 flex-col gap-5 pb-40">{renderChallenge()}</div>

      {/* Socratic tutor trigger: 💡 أحتاج تلميحاً (10 gems, Tier 1 model) --- */}
      {!finished && (
        <button
          type="button"
          onClick={() => setHintOpen(true)}
          className="mx-auto mb-40 flex w-full max-w-3xl items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-purple/60 bg-feather-purple/10 px-4 py-2.5 font-display text-sm font-extrabold text-feather-purple transition-all active:translate-y-[2px] active:border-b-0 sm:mb-32"
        >
          💡 أحتاج تلميحاً
          <span className="rounded-lg bg-feather-purple/20 px-1.5 py-0.5 font-body text-[10px] font-bold">
            {TUTOR_HINT_COST} 💎
          </span>
        </button>
      )}

      {hintContext && (
        <TutorDrawer
          open={hintOpen}
          onClose={() => setHintOpen(false)}
          userId={userId}
          challengeId={hintContext.challengeId}
          question={hintContext.question}
          studentChoice={hintContext.choice}
          context={hintContext.context}
          gems={gems}
          onCharged={() => void refreshStats()}
        />
      )}

      {/* Bottom action footer: the tactile CHECK/CONTINUE bar --------------- */}
      <footer
        className={`fixed inset-x-0 bottom-0 z-30 border-t-2 px-4 py-3 backdrop-blur sm:px-6 ${
          !answered
            ? "border-surface-border bg-surface/95"
            : outcome?.isCorrect
              ? "border-feather-green bg-feather-green/10"
              : "border-feather-red bg-feather-red/10"
        }`}
      >
        <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            {answered ? (
              // Animated drawer reveal: slides up in emerald (correct) or rose
              // (wrong) with the academic correction, like a result sheet.
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ type: "spring", stiffness: 260, damping: 24 }}
              >
                <p
                  className={`font-display text-base font-extrabold ${
                    outcome?.isCorrect ? "text-feather-green-dark" : "text-feather-red-dark"
                  }`}
                >
                  {outcome?.isCorrect
                    ? "أحسنت!"
                    : inRemedial
                      ? "لا بأس — أُضيف التحدي إلى دفتر الأخطاء (FSRS)"
                      : "لا بأس — سيظهر هذا التحدي في جولة التصحيح السريعة"}
                </p>
                <p className="font-body text-xs text-ink-soft" dir="auto">
                  {ratingHint}
                  {lastReview
                    ? ` · المراجعة القادمة بعد ${formatInterval(lastReview.interval_days)}`
                    : " · جارٍ تحديث جدول FSRS…"}
                </p>
              </motion.div>
            ) : (
              <p className="hidden font-body text-xs text-ink-faint sm:block">
                أكمل التحدي للمتابعة · أرقام 1-4 لاختيار سريع
              </p>
            )}
          </div>
          <button
            type="button"
            disabled={!answered || submitting}
            onClick={() => nextChallenge()}
            className={`w-full rounded-2xl border-2 border-b-4 px-8 py-3 font-display text-sm font-black uppercase tracking-wider transition-all sm:w-auto ${
              !answered || submitting
                ? "cursor-not-allowed border-surface-border bg-surface-muted text-ink-faint"
                : "border-feather-green-dark bg-feather-green text-white shadow-key active:translate-y-[2px] active:border-b-0"
            }`}
          >
            {index + 1 >= total ? "إنهاء الجلسة" : "متابعة"}
          </button>
        </div>
      </footer>

      {/* Exit confirmation: protect progress from accidental dismissal ------ */}
      {exitPrompt && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            <h2 className="font-display text-lg font-black text-ink">أتريد الخروج؟</h2>
            <p className="mt-1 font-body text-sm text-ink-soft">
              أكملت {clearedUniqueCount} من {totalUniqueCount} — تقدّمك محفوظ حتى الآن.
            </p>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setExitPrompt(false)}
                className="flex-1 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-sm font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
              >
                أكمل الجلسة
              </button>
              <button
                type="button"
                onClick={() => router.push("/")}
                className="flex-1 rounded-2xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-4 py-2.5 font-display text-sm font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0"
              >
                خروج
              </button>
            </div>
          </motion.div>
        </div>
      )}

      {/* End-of-session modal --------------------------------------------- */}
      {finished && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ type: "spring", stiffness: 220, damping: 24 }}
            className="w-full max-w-lg rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            {/* Victory header: accuracy ring draws itself on mount */}
            <div className="flex items-center gap-4">
              <svg viewBox="0 0 84 84" className="h-20 w-20 shrink-0 -rotate-90">
                <circle
                  cx="42"
                  cy="42"
                  r="36"
                  fill="none"
                  stroke="var(--cl-surface-border)"
                  strokeWidth="9"
                />
                <circle
                  cx="42"
                  cy="42"
                  r="36"
                  fill="none"
                  stroke={accuracy >= 80 ? "var(--cl-green)" : "var(--cl-orange)"}
                  strokeWidth="9"
                  strokeLinecap="round"
                  strokeDasharray={2 * Math.PI * 36}
                  strokeDashoffset={2 * Math.PI * 36 * (1 - accuracy / 100)}
                  className="ring-draw"
                  style={{ "--ring-circumference": 2 * Math.PI * 36 } as React.CSSProperties}
                />
              </svg>
              <div>
                <div className="flex items-center gap-2">
                  <Trophy className="h-6 w-6 text-feather-yellow" aria-hidden="true" />
                  <h2 className="font-display text-2xl font-black text-ink">
                    {wrongCount === 0 ? "جلسة مثالية!" : "انتهت الجلسة"}
                  </h2>
                </div>
                <p className="font-body text-sm text-ink-soft">
                  دقة {accuracy}% · {correctCount} صحيحة · {wrongCount} خاطئة
                </p>
                {bestCombo >= 3 && (
                  <p className="mt-0.5 font-display text-xs font-extrabold text-feather-orange">
                    🔥 أطول سلسلة صحيحة: {bestCombo}
                  </p>
                )}
              </div>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <span className="flex items-center gap-1 rounded-xl bg-feather-purple/10 px-3 py-2 font-display text-sm font-extrabold text-feather-purple">
                <Zap className="h-4 w-4" aria-hidden="true" /> +{sessionXp} XP
              </span>
              <span
                className="flex items-center gap-1 rounded-xl bg-feather-red/10 px-3 py-2 font-display text-sm font-extrabold text-feather-red"
                title="قلوب لا نهائية"
              >
                <Heart className="h-4 w-4" aria-hidden="true" /> ∞
              </span>
              {sessionMistakes.length > 0 && (
                <span className="rounded-xl bg-feather-yellow/20 px-3 py-2 font-display text-sm font-extrabold text-ink">
                  {sessionMistakes.length} في Mistakes Inbox
                </span>
              )}
            </div>

            {/* 4 FSRS rating buttons */}
            <div className="mt-5 flex flex-col gap-2">
              <p className="font-display text-sm font-extrabold text-ink">
                كيف كان أداؤك؟ سيحدّد هذا موعد المراجعة القادم (FSRS-4.5)
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {RATINGS.map((rating) => (
                  <button
                    key={rating.value}
                    type="button"
                    disabled={submitting}
                    onClick={async () => {
                      const review = await submitSessionRating(rating.value);
                      if (review) {
                        setRatedInterval({
                          rating: rating.value,
                          interval: review.interval_days,
                        });
                      }
                    }}
                    className={`rounded-2xl border-2 border-b-4 px-3 py-2 font-display text-xs font-extrabold uppercase transition-all active:translate-y-[2px] disabled:opacity-60 ${
                      ratedInterval?.rating === rating.value
                        ? "border-ink bg-ink text-white"
                        : "border-surface-border bg-surface text-ink hover:bg-surface-muted"
                    }`}
                    style={
                      ratedInterval?.rating === rating.value
                        ? undefined
                        : { borderBottomColor: rating.color }
                    }
                  >
                    <span className="block">{rating.ar}</span>
                    <span className="block text-[10px] font-bold opacity-70">{rating.en}</span>
                  </button>
                ))}
              </div>
              {ratedInterval && (
                <motion.p
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="font-body text-xs text-ink-soft"
                >
                  تم التقييم ({RATINGS.find((r) => r.value === ratedInterval.rating)?.en}) — المراجعة
                  القادمة بعد {formatInterval(ratedInterval.interval)}.
                  {lastReview && ` استقرار ${lastReview.stability.toFixed(2)} · صعوبة ${lastReview.difficulty.toFixed(2)} · حالة ${lastReview.state_name}`}
                </motion.p>
              )}
            </div>

            <div className="mt-5 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  confettiFired.current = false;
                  resetSession();
                  router.push("/");
                }}
                className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-5 py-2.5 font-display text-xs font-black uppercase tracking-wider text-white shadow-key active:translate-y-[2px] active:border-b-0"
              >
                <Home className="h-4 w-4" aria-hidden="true" />
                إلى الأمام
              </button>
              <button
                type="button"
                onClick={() => {
                  confettiFired.current = false;
                  router.refresh();
                }}
                className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key"
              >
                <RotateCcw className="h-4 w-4" aria-hidden="true" />
                إعادة التدريب
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </main>
  );
}
