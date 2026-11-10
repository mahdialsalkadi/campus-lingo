"use client";

/**
 * TAP_TO_FILL — one-touch cloze.
 *
 * A short sentence with one blank; the learner taps the missing word from
 * three pill buttons pinned at the bottom. A wrong tap reveals the one-liner
 * rationale — never a heavy explanatory paragraph.
 */

import { motion } from "framer-motion";
import { useMemo, useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type TapToFillPayload,
} from "@/lib/api";
import { playSfx, useQuizStore } from "@/stores/quizStore";

const XP_FOR_FILL = 6;

/** Split the sentence around the canonical blank for highlighted rendering. */
function splitAroundBlank(sentence: string): [string, string] {
  const index = sentence.indexOf("_____");
  if (index < 0) return [sentence, ""];
  return [sentence.slice(0, index), sentence.slice(index + "_____".length)];
}

export default function TapToFill({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<TapToFillPayload>) {
  const soundEnabled = useQuizStore((state) => state.soundEnabled);
  const [picked, setPicked] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  // Deterministic shuffle keyed by the challenge id so the pills reorder per
  // challenge but stay stable across re-renders (no hydration mismatch).
  const options = useMemo(() => {
    const list = [payload.correct_answer, ...payload.distractors];
    let seed = 0;
    for (const char of challengeId) seed = (seed * 31 + char.charCodeAt(0)) % 2_147_483_647;
    const shuffled: string[] = [];
    while (list.length > 0) {
      seed = (seed * 1103515245 + 12345) % 2_147_483_648;
      shuffled.push(list.splice(seed % list.length, 1)[0]);
    }
    return shuffled;
  }, [challengeId, payload.correct_answer, payload.distractors]);

  const [before, after] = useMemo(
    () => splitAroundBlank(payload.sentence_with_blank),
    [payload.sentence_with_blank],
  );

  const report = (isCorrect: boolean) => {
    if (reported.current) return;
    reported.current = true;
    const result: ChallengeOutcome = {
      isCorrect,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: isCorrect ? 0 : 1,
      responseJson: { challengeId, kind: "TAP_TO_FILL", expected: payload.correct_answer, given: picked },
    };
    onResult(result);
  };

  const pick = (option: string) => {
    if (disabled || revealed) return;
    setPicked(option);
    const isCorrect = option === payload.correct_answer;
    if (isCorrect) {
      if (soundEnabled) playSfx("correct");
      onXp?.(XP_FOR_FILL);
    } else {
      if (soundEnabled) playSfx("incorrect");
      onHeartLoss?.();
    }
    setRevealed(true);
    report(isCorrect);
  };

  const pillTone = (option: string) => {
    if (!revealed) {
      return "border-surface-border bg-surface text-ink hover:border-feather-blue/60";
    }
    if (option === payload.correct_answer) {
      return "border-feather-green bg-feather-green/15 text-feather-green-dark";
    }
    if (option === picked) return "border-feather-red bg-feather-red/15 text-feather-red-dark";
    return "border-surface-border bg-surface opacity-60";
  };

  return (
    <section className="flex flex-col gap-5" dir="auto">
      <span className="w-fit rounded-xl bg-feather-orange/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-orange">
        👆 {payload.instructions}
      </span>

      <motion.div
        layout
        className="rounded-3xl border-2 border-surface-border bg-surface p-6 text-center shadow-card"
      >
        <p className="font-display text-lg font-extrabold leading-loose text-ink">
          <RichContent text={before} inline />
          <span
            className={`mx-1 inline-block min-w-24 rounded-lg border-b-4 px-3 font-display ${
              revealed
                ? "border-feather-green text-feather-green-dark"
                : "border-feather-blue text-feather-blue-dark"
            }`}
          >
            {revealed ? payload.correct_answer : "?"}
          </span>
          <RichContent text={after} inline />
        </p>
      </motion.div>

      {/* Three pill buttons at the bottom */}
      <div className="grid grid-cols-3 gap-2">
        {options.map((option) => (
          <motion.button
            key={option}
            type="button"
            data-pick
            disabled={disabled || revealed}
            whileTap={!revealed && !disabled ? { scale: 0.94 } : undefined}
            onClick={() => pick(option)}
            className={`rounded-full border-2 border-b-4 px-3 py-3 font-display text-sm font-extrabold transition-colors disabled:cursor-default ${pillTone(
              option,
            )}`}
            dir="auto"
          >
            {option}
          </motion.button>
        ))}
      </div>

      {revealed && (
        <motion.p
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 px-4 py-3 font-body text-xs font-bold leading-relaxed ${
            picked === payload.correct_answer
              ? "border-feather-green bg-feather-green/10 text-feather-green-dark"
              : "border-feather-red bg-feather-red/10 text-feather-red-dark"
          }`}
          dir="auto"
        >
          {payload.one_liner_rationale}
        </motion.p>
      )}
    </section>
  );
}
