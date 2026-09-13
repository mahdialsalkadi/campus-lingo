"use client";

/**
 * CONTEXTUAL_CLOZE — one sentence with a blank, a word bank underneath.
 * Tapping a word fills the blank and evaluates instantly: correct shows the
 * explanation and awards XP, wrong shakes the blank, costs a heart and reveals
 * the answer plus why.
 */

import { AnimatePresence, motion } from "framer-motion";
import { CheckCircle2, Eraser, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  shuffle,
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type ContextualClozePayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_CLOZE = 12;
const BLANK_MARKER = "_____";

type Evaluation = "idle" | "correct" | "wrong";

export default function ContextualCloze({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<ContextualClozePayload>) {
  const wordBank = useMemo(
    () => shuffle([payload.correct_answer, ...payload.distractors]),
    [payload],
  );

  const [selected, setSelected] = useState<string | null>(null);
  const [evaluation, setEvaluation] = useState<Evaluation>("idle");
  const [mistakes, setMistakes] = useState<string[]>([]);
  const [xpEarned, setXpEarned] = useState(0);

  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  const report = (isCorrect: boolean, finalMistakes: string[]) => {
    if (reported.current) return;
    reported.current = true;
    onResult({
      isCorrect,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: finalMistakes.length,
      responseJson: {
        challengeId,
        kind: "CONTEXTUAL_CLOZE",
        expected: payload.correct_answer,
        given: selected,
        wrongPicks: finalMistakes,
      },
    });
  };

  // Auto-advance shortly after a correct answer so the rhythm stays snappy.
  useEffect(() => {
    if (evaluation !== "correct") return;
    const timer = window.setTimeout(() => report(true, mistakes), 900);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evaluation]);

  const choose = (word: string) => {
    if (disabled || evaluation === "correct") return;
    setSelected(word);
    playSfx("tap");
    if (word.trim().toLowerCase() === payload.correct_answer.trim().toLowerCase()) {
      playSfx("correct");
      setEvaluation("correct");
      setXpEarned(XP_FOR_CLOZE);
      onXp?.(XP_FOR_CLOZE);
      return;
    }
    playSfx("incorrect");
    setEvaluation("wrong");
    onHeartLoss?.();
    setMistakes((prev) => (prev.includes(word) ? prev : [...prev, word]));
  };

  const [before, after = ""] = useMemo(() => {
    const split = payload.sentence_with_blank
      .replace("____", BLANK_MARKER)
      .split(BLANK_MARKER);
    return [split[0] ?? "", split.slice(1).join(BLANK_MARKER)];
  }, [payload.sentence_with_blank]);

  const blankTone =
    evaluation === "correct"
      ? "border-feather-green bg-feather-green/15 text-feather-green-dark"
      : evaluation === "wrong"
        ? "border-feather-red bg-feather-red/15 text-feather-red-dark"
        : selected
          ? "border-feather-blue bg-feather-blue/10 text-ink"
          : "border-dashed border-ink-faint bg-surface text-ink-faint";

  const revealed = evaluation === "wrong";

  return (
    <section className="flex w-full flex-col gap-4" aria-label="Contextual Cloze challenge">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-display text-lg font-extrabold text-ink">أكمل الفراغ</h2>
          <div className="flex items-center gap-3 font-display text-xs font-bold">
            {mistakes.length > 0 && (
              <span className="flex items-center gap-1 text-feather-red">
                <XCircle className="h-4 w-4" aria-hidden="true" />
                {mistakes.length}
              </span>
            )}
            {xpEarned > 0 && (
              <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-feather-purple">
                +{xpEarned} XP
              </motion.span>
            )}
          </div>
        </div>
        <p className="font-body text-sm text-ink-soft">{payload.instructions}</p>
      </header>

      <motion.div
        animate={evaluation === "wrong" ? { x: [0, -8, 8, -5, 5, 0] } : { x: 0 }}
        transition={{ duration: 0.4 }}
        className="rounded-2xl border-2 border-surface-border bg-surface-muted p-4"
      >
        <p className="font-body text-base leading-loose text-ink" dir="auto">
          {before}
          <span
            className={`mx-1 inline-flex min-w-[7rem] items-center justify-center rounded-lg border-b-2 px-2 py-0.5 font-display text-sm font-bold ${blankTone}`}
          >
            {evaluation === "correct" ? payload.correct_answer : (selected ?? "…")}
          </span>
          {after}
        </p>
      </motion.div>

      <div className="flex flex-wrap gap-2" dir="auto">
        {wordBank.map((word) => {
          const used = selected === word;
          const rejected = mistakes.includes(word);
          return (
            <motion.button
              key={word}
              type="button"
              whileTap={{ scale: 0.94 }}
              disabled={disabled || evaluation === "correct" || rejected}
              onClick={() => choose(word)}
              className={`rounded-xl border-2 border-b-4 px-3 py-2 font-body text-sm transition-colors duration-150 ${
                rejected
                  ? "cursor-not-allowed border-feather-red/40 bg-feather-red/10 text-feather-red-dark line-through"
                  : used && evaluation === "correct"
                    ? "border-feather-green bg-feather-green/15 text-ink"
                    : used
                      ? "border-feather-red bg-feather-red/10 text-ink"
                      : "border-surface-border bg-surface text-ink hover:bg-surface-muted"
              }`}
            >
              {word}
            </motion.button>
          );
        })}
      </div>

      <div className="flex items-center justify-between">
        <button
          type="button"
          disabled={!selected || evaluation === "correct" || disabled}
          onClick={() => {
            playSfx("tap");
            setSelected(null);
            setEvaluation("idle");
          }}
          className={`flex items-center gap-1 font-display text-xs font-bold uppercase ${
            !selected || evaluation === "correct"
              ? "cursor-not-allowed text-ink-faint"
              : "text-feather-blue"
          }`}
        >
          <Eraser className="h-3.5 w-3.5" aria-hidden="true" />
          مسح
        </button>
        {revealed && evaluation === "wrong" && reported.current === false && (
          <span className="font-body text-xs text-ink-faint">
            الإجابة الصحيحة: <b>{payload.correct_answer}</b>
          </span>
        )}
      </div>

      <AnimatePresence>
        {evaluation !== "idle" && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className={`rounded-2xl border-2 p-4 ${
              evaluation === "correct"
                ? "border-feather-green bg-feather-green/10"
                : "border-feather-red bg-feather-red/10"
            }`}
          >
            <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
              {evaluation === "correct" ? (
                <>
                  <CheckCircle2 className="h-4 w-4 text-feather-green" aria-hidden="true" />
                  إجابة صحيحة!
                </>
              ) : (
                <>
                  <XCircle className="h-4 w-4 text-feather-red" aria-hidden="true" />
                  الإجابة الصحيحة: {payload.correct_answer}
                </>
              )}
            </p>
            <p className="mt-2 font-body text-sm leading-relaxed text-ink-soft">
              {payload.explanation}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
