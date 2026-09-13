"use client";

import confetti from "canvas-confetti";
import { motion } from "framer-motion";
import { Heart, Home, RotateCcw, Trophy, Zap } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ContextualCloze from "@/components/ContextualCloze";
import InteractiveStory from "@/components/InteractiveStory";
import MatchMadness from "@/components/MatchMadness";
import SpotMistake from "@/components/SpotMistake";
import {
  ApiError,
  CHALLENGE_LABELS,
  RATINGS,
  api,
  formatInterval,
  getNodeChallenges,
  type ChallengeOutcome,
  type ChallengePayload,
  type ContextualClozePayload,
  type InteractiveStoryPayload,
  type MatchMadnessPayload,
  type NodeChallengesResponse,
  type SpotMistakePayload,
} from "@/lib/api";
import { MAX_HEARTS, useQuizStore } from "@/stores/quizStore";

const REVIEW_NODE_ID = "review";

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

export default function PlayPage({ params }: { params: { nodeId: string } }) {
  const nodeId = params.nodeId;
  const router = useRouter();

  const userId = useQuizStore((state) => state.userId);
  const hearts = useQuizStore((state) => state.hearts);
  const xp = useQuizStore((state) => state.xp);
  const queue = useQuizStore((state) => state.queue);
  const index = useQuizStore((state) => state.index);
  const sessionXp = useQuizStore((state) => state.sessionXp);
  const correctCount = useQuizStore((state) => state.correctCount);
  const wrongCount = useQuizStore((state) => state.wrongCount);
  const sessionMistakes = useQuizStore((state) => state.sessionMistakes);
  const lastReview = useQuizStore((state) => state.lastReview);
  const finishedAt = useQuizStore((state) => state.finishedAt);
  const submitting = useQuizStore((state) => state.submitting);

  const ensureUser = useQuizStore((state) => state.ensureUser);
  const refreshHearts = useQuizStore((state) => state.refreshHearts);
  const refreshStats = useQuizStore((state) => state.refreshStats);
  const startNodeSession = useQuizStore((state) => state.startNodeSession);
  const startQueueSession = useQuizStore((state) => state.startQueueSession);
  const recordAttempt = useQuizStore((state) => state.recordAttempt);
  const submitSessionRating = useQuizStore((state) => state.submitSessionRating);
  const loseHeart = useQuizStore((state) => state.loseHeart);
  const addXp = useQuizStore((state) => state.addXp);
  const nextChallenge = useQuizStore((state) => state.nextChallenge);
  const finishSession = useQuizStore((state) => state.finishSession);
  const resetSession = useQuizStore((state) => state.resetSession);
  const refillHearts = useQuizStore((state) => state.refillHearts);

  const [node, setNode] = useState<NodeChallengesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answered, setAnswered] = useState(false);
  const [outcome, setOutcome] = useState<ChallengeOutcome | null>(null);
  const [ratedInterval, setRatedInterval] = useState<{ rating: number; interval: number } | null>(null);
  const confettiFired = useRef(false);

  const current = queue[index] ?? null;
  const isReviewMode = nodeId === REVIEW_NODE_ID;
  const total = queue.length;
  const finished = finishedAt !== null && total > 0;

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
          startQueueSession(items);
        } else {
          const response = await getNodeChallenges(nodeId);
          if (cancelled) return;
          setNode(response);
          startNodeSession(response);
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

  useEffect(() => {
    refreshHearts();
    void ensureUser().then(() => refreshStats());
    return () => resetSession();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const challengeId = current?.challengeId ?? null;
  useEffect(() => {
    setAnswered(false);
    setOutcome(null);
  }, [challengeId]);

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

  const progressPct = total === 0 ? 0 : ((index + (answered ? 1 : 0)) / total) * 100;
  const accuracy =
    correctCount + wrongCount === 0
      ? 0
      : Math.round((correctCount / (correctCount + wrongCount)) * 100);

  const renderChallenge = () => {
    if (!current) return null;
    const payload = current.payload as ChallengePayload;
    const shared = {
      key: current.challengeId,
      challengeId: current.challengeId,
      onResult: handleResult,
      onHeartLoss: loseHeart,
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
      case "INTERACTIVE_STORY":
        return <InteractiveStory {...shared} payload={payload as InteractiveStoryPayload} />;
      default:
        return (
          <p className="font-body text-sm text-ink-soft" dir="auto">
            نوع تحدٍّ غير معروف: {current.type}
          </p>
        );
    }
  };

  const ratingHint = useMemo(() => {
    if (!answered) return "";
    if (outcome?.isCorrect) {
      return outcome.elapsedMs <= 8000 ? "سريع ودقيق — يستحق Easy" : "إجابة صحيحة";
    }
    return "راجع البطاقة في جلسة قادمة";
  }, [answered, outcome]);

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

  // Out of hearts gate
  if (hearts <= 0 && !finished) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-4 px-4 py-10 text-center">
        <p className="font-display text-2xl font-extrabold text-feather-red-dark">
          نفدت القلوب
        </p>
        <p className="max-w-sm font-body text-sm text-ink-soft">
          يتجدد قلب كل 30 دقيقة، أو أعد التعبئة الآن بـ 350 💎.
        </p>
        <div className="flex flex-wrap justify-center gap-3">
          <button
            type="button"
            onClick={() => void refillHearts()}
            className="rounded-2xl border-2 border-b-4 border-feather-orange bg-feather-yellow px-5 py-3 font-display text-sm font-extrabold uppercase text-ink shadow-key"
          >
            إعادة تعبئة القلوب
          </button>
          <button
            type="button"
            onClick={() => router.push("/")}
            className="rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-5 py-3 font-display text-sm font-extrabold uppercase text-ink"
          >
            لوحة التحكم
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col px-4 py-5 sm:px-6">
      {/* Progress bar + hearts --------------------------------------------- */}
      <header className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => router.push("/")}
          className="font-display text-lg font-extrabold text-ink-faint"
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
        <div className="flex items-center gap-2 font-display text-sm font-bold">
          <span className="flex items-center gap-0.5 text-feather-red">
            {Array.from({ length: MAX_HEARTS }).map((_, heartIndex) => (
              <Heart
                key={heartIndex}
                className={`h-4 w-4 ${
                  heartIndex < hearts ? "fill-feather-red text-feather-red" : "text-ink-faint"
                }`}
                aria-hidden="true"
              />
            ))}
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
          {" "}تحدٍّ {index + 1} من {total}
        </span>
        <span dir="auto">
          {CHALLENGE_LABELS[current?.type as keyof typeof CHALLENGE_LABELS]?.ar ?? current?.type}
        </span>
      </div>

      <div className="mt-5 flex flex-1 flex-col gap-5">{renderChallenge()}</div>

      {/* Footer: consequence banner + continue ----------------------------- */}
      <footer
        className={`sticky bottom-0 -mx-4 mt-6 border-t-2 px-4 py-3 sm:-mx-6 sm:px-6 ${
          !answered
            ? "border-surface-border bg-surface"
            : outcome?.isCorrect
              ? "border-feather-green bg-feather-green/10"
              : "border-feather-red bg-feather-red/10"
        }`}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            {answered ? (
              <>
                <p
                  className={`font-display text-base font-extrabold ${
                    outcome?.isCorrect ? "text-feather-green-dark" : "text-feather-red-dark"
                  }`}
                >
                  {outcome?.isCorrect ? "أحسنت!" : "لا بأس — أُضيفت البطاقة إلى Mistakes Inbox"}
                </p>
                <p className="font-body text-xs text-ink-soft" dir="auto">
                  {ratingHint}
                  {lastReview
                    ? ` · المراجعة القادمة بعد ${formatInterval(lastReview.interval_days)}`
                    : " · جارٍ تحديث جدول FSRS…"}
                </p>
              </>
            ) : (
              <p className="font-body text-xs text-ink-faint">أكمل التحدي للمتابعة.</p>
            )}
          </div>
          <button
            type="button"
            disabled={!answered || submitting}
            onClick={() => {
              if (index + 1 >= total) finishSession();
              else nextChallenge();
            }}
            className={`rounded-2xl border-2 border-b-4 px-6 py-3 font-display text-sm font-extrabold uppercase tracking-wide transition-all ${
              !answered || submitting
                ? "cursor-not-allowed border-surface-border bg-surface-muted text-ink-faint"
                : "border-feather-green-dark bg-feather-green text-white shadow-key active:translate-y-[2px]"
            }`}
          >
            {index + 1 >= total ? "إنهاء الجلسة" : "متابعة"}
          </button>
        </div>
      </footer>

      {/* End-of-session modal --------------------------------------------- */}
      {finished && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ type: "spring", stiffness: 220, damping: 24 }}
            className="w-full max-w-lg rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            <div className="flex items-center gap-2">
              <Trophy className="h-6 w-6 text-feather-yellow" aria-hidden="true" />
              <h2 className="font-display text-2xl font-extrabold text-ink">
                {wrongCount === 0 ? "جلسة مثالية!" : "انتهت الجلسة"}
              </h2>
            </div>
            <p className="mt-1 font-body text-sm text-ink-soft">
              {correctCount} صحيحة · {wrongCount} خاطئة · دقة {accuracy}%
            </p>

            <div className="mt-4 flex flex-wrap gap-2">
              <span className="flex items-center gap-1 rounded-xl bg-feather-purple/10 px-3 py-2 font-display text-sm font-extrabold text-feather-purple">
                <Zap className="h-4 w-4" aria-hidden="true" /> +{sessionXp} XP
              </span>
              <span className="flex items-center gap-1 rounded-xl bg-feather-red/10 px-3 py-2 font-display text-sm font-extrabold text-feather-red">
                <Heart className="h-4 w-4" aria-hidden="true" /> {hearts}/{MAX_HEARTS}
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
                className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key"
              >
                <Home className="h-4 w-4" aria-hidden="true" />
                لوحة التحكم
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
