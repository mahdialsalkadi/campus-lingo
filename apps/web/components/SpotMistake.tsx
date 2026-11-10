"use client";

/**
 * SPOT_MISTAKE — every word of the sentence/code block is a clickable token.
 * Wrong token: the card vibrates, a heart is deducted, the token is disabled.
 * Correct token: green highlight, explanation card, XP awarded.
 * After three wrong taps the answer is revealed and the attempt is graded as a
 * miss (the learner should never be stuck with no way forward).
 */

import { AnimatePresence, motion } from "framer-motion";
import { CheckCircle2, Lightbulb, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  findMistakeTokenIndices,
  tokenize,
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type SpotMistakePayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_SPOT = 15;
const MAX_WRONG_TAPS = 3;

export default function SpotMistake({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<SpotMistakePayload>) {
  const tokens = useMemo(() => tokenize(payload.code_or_text), [payload.code_or_text]);
  const mistakeIndices = useMemo(() => findMistakeTokenIndices(payload), [payload]);
  const mistakeIndex = mistakeIndices.length > 0 ? mistakeIndices[0] : -1;

  const [wrongTaps, setWrongTaps] = useState<number[]>([]);
  const [solved, setSolved] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [vibrating, setVibrating] = useState(false);
  const [showExplanation, setShowExplanation] = useState(false);
  const [xpEarned, setXpEarned] = useState(0);

  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  const report = (isCorrect: boolean) => {
    if (reported.current) return;
    reported.current = true;
    onResult({
      isCorrect,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: wrongTaps.length,
      responseJson: {
        challengeId,
        kind: "SPOT_MISTAKE",
        mistakeToken: payload.mistake_token,
        clickedIndices: wrongTaps,
        mistakeTokenIndices: mistakeIndices,
        revealed,
      },
    });
  };

  const tapToken = (index: number) => {
    if (disabled || solved || revealed) return;
    if (wrongTaps.includes(index)) return;

    if (mistakeIndices.includes(index)) {
      playSfx("correct");
      setSolved(true);
      setShowExplanation(true);
      setXpEarned(XP_FOR_SPOT);
      onXp?.(XP_FOR_SPOT);
      window.setTimeout(() => report(true), 400);
      return;
    }

    if (mistakeIndex < 0) {
      // Malformed payload (token not found): don't punish the learner.
      setRevealed(true);
      setShowExplanation(true);
      playSfx("incorrect");
      window.setTimeout(() => report(false), 400);
      return;
    }

    playSfx("incorrect");
    setVibrating(true);
    window.setTimeout(() => setVibrating(false), 420);
    onHeartLoss?.();

    const nextWrong = [...wrongTaps, index];
    setWrongTaps(nextWrong);
    if (nextWrong.length >= MAX_WRONG_TAPS) {
      setRevealed(true);
      setShowExplanation(true);
      window.setTimeout(() => report(false), 500);
    }
  };

  // Keep the timer honest even if the learner never taps.
  useEffect(() => {
    if (solved || revealed || disabled) return;
    const guard = window.setTimeout(() => {
      if (!reported.current) {
        setRevealed(true);
        setShowExplanation(true);
        report(false);
      }
    }, 180_000);
    return () => window.clearTimeout(guard);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [solved, revealed, disabled]);

  const tokenClass = (index: number) => {
    const isMistake = mistakeIndices.includes(index);
    const wasWrong = wrongTaps.includes(index);
    const base =
      "rounded-lg px-1.5 py-0.5 text-start font-body text-base leading-relaxed transition-colors duration-150";
    if (solved && isMistake) return `${base} bg-feather-green/25 font-bold text-feather-green-dark`;
    if (revealed && isMistake) return `${base} bg-feather-green/20 font-bold text-feather-green-dark`;
    if (wasWrong) {
      return `${base} cursor-not-allowed bg-feather-red/15 text-feather-red-dark line-through`;
    }
    if (solved || revealed) return `${base} text-ink-soft`;
    return `${base} text-ink hover:bg-feather-blue/15 hover:text-feather-blue-dark`;
  };

  return (
    <section className="flex w-full flex-col gap-4" aria-label="Spot the Mistake challenge">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-display text-lg font-extrabold text-ink">اكتشف الخطأ</h2>
          <div className="flex items-center gap-3 font-display text-xs font-bold text-ink-faint">
            <span className="flex items-center gap-1 text-feather-red">
              <XCircle className="h-4 w-4" aria-hidden="true" />
              {wrongTaps.length}/{MAX_WRONG_TAPS}
            </span>
            {xpEarned > 0 && (
              <motion.span
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                className="text-feather-purple"
              >
                +{xpEarned} XP
              </motion.span>
            )}
          </div>
        </div>
        <p className="font-body text-sm text-ink-soft">{payload.instructions}</p>
      </header>

      <motion.div
        animate={vibrating ? { x: [0, -10, 10, -7, 7, 0] } : { x: 0 }}
        transition={{ duration: 0.4 }}
        className={`rounded-2xl border-2 p-4 shadow-card ${
          solved
            ? "border-feather-green bg-feather-green/5"
            : revealed
              ? "border-feather-yellow bg-feather-yellow/5"
              : "border-surface-border bg-surface"
        }`}
      >
        <p className="flex flex-wrap items-center gap-y-1" dir="auto">
          {tokens.map((token, index) => (
            <button
              key={`${token}-${index}`}
              type="button"
              disabled={disabled || solved || revealed || wrongTaps.includes(index)}
              onClick={() => tapToken(index)}
              className={tokenClass(index)}
            >
              {token}
              {mistakeIndices.includes(index) && solved && (
                <CheckCircle2 className="ms-1 inline h-4 w-4 text-feather-green" aria-hidden="true" />
              )}
            </button>
          ))}
        </p>
      </motion.div>

      <AnimatePresence>
        {showExplanation && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className={`rounded-2xl border-2 p-4 ${
              solved
                ? "border-feather-green bg-feather-green/10"
                : "border-feather-red bg-feather-red/10"
            }`}
          >
            <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
              <Lightbulb className="h-4 w-4 text-feather-yellow" aria-hidden="true" />
              {solved ? "أصبت! هذه هي الكلمة الخاطئة" : "الكلمة الخاطئة كانت"}
            </p>
            <p className="mt-2 font-body text-sm text-ink">
              <code className="rounded bg-surface px-1.5 py-0.5 font-bold">
                {payload.mistake_token}
              </code>
            </p>
            <p className="mt-2 font-body text-sm leading-relaxed text-ink-soft">
              <RichContent
                text={payload.one_liner_rationale?.trim() || payload.explanation}
              />
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
