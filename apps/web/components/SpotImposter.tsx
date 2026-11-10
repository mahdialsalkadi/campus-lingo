"use client";

/**
 * SPOT_IMPOSTER — one false statement hiding among three truths.
 *
 * Four short factual statements about the same concept; exactly one carries a
 * subtly altered idea. The learner taps the imposter. Wrong taps reveal the
 * one-liner rationale instead of a heavy paragraph.
 */

import { motion } from "framer-motion";
import { useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type SpotImposterPayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_CATCH = 9;

export default function SpotImposter({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<SpotImposterPayload>) {
  const [picked, setPicked] = useState<number | null>(null);
  const [revealed, setRevealed] = useState(false);
  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  const report = (isCorrect: boolean) => {
    if (reported.current) return;
    reported.current = true;
    const result: ChallengeOutcome = {
      isCorrect,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: isCorrect ? 0 : 1,
      responseJson: {
        challengeId,
        kind: "SPOT_IMPOSTER",
        expected: payload.statements.findIndex((statement) => statement.is_imposter),
        given: picked,
      },
    };
    onResult(result);
  };

  const pick = (index: number) => {
    if (disabled || revealed) return;
    setPicked(index);
    const isCorrect = payload.statements[index]?.is_imposter === true;
    if (isCorrect) {
      playSfx("correct");
      onXp?.(XP_FOR_CATCH);
    } else {
      playSfx("incorrect");
      onHeartLoss?.();
    }
    setRevealed(true);
    report(isCorrect);
  };

  const tone = (index: number) => {
    const isImposter = payload.statements[index]?.is_imposter === true;
    if (!revealed) {
      return "border-surface-border bg-surface hover:border-feather-purple/60";
    }
    if (isImposter) return "border-feather-red bg-feather-red/15";
    if (picked === index) return "border-feather-red bg-feather-red/10 opacity-70";
    return "border-feather-green bg-feather-green/10 opacity-80";
  };

  return (
    <section className="flex flex-col gap-4" dir="auto">
      <header className="flex flex-col gap-2">
        <span className="w-fit rounded-xl bg-feather-purple/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-purple">
          🕵️ {payload.instructions}
        </span>
      </header>

      <div className="grid gap-2.5">
        {payload.statements.map((statement, index) => (
          <motion.button
            key={index}
            type="button"
            data-pick
            disabled={disabled || revealed}
            whileTap={!revealed && !disabled ? { scale: 0.98 } : undefined}
            onClick={() => pick(index)}
            className={`flex items-start gap-3 rounded-2xl border-2 border-b-4 px-4 py-3.5 text-start transition-colors disabled:cursor-default ${tone(
              index,
            )}`}
          >
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink/5 font-display text-[11px] font-black text-ink-soft">
              {index + 1}
            </span>
            <span className="font-body text-sm font-bold leading-relaxed text-ink">
              <RichContent text={statement.text} inline />
            </span>
            {revealed && statement.is_imposter && (
              <span className="ms-auto shrink-0 rounded-lg bg-feather-red/15 px-2 py-0.5 font-display text-[10px] font-black text-feather-red">
                الدخيل
              </span>
            )}
          </motion.button>
        ))}
      </div>

      {revealed && (
        <motion.p
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 px-4 py-3 font-body text-xs font-bold leading-relaxed ${
            picked !== null && payload.statements[picked]?.is_imposter
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
