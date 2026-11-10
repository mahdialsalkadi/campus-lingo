"use client";

/**
 * SWIPE_BOOLEAN — "Tinder for Study".
 *
 * One crisp conceptual statement; the learner swipes (or taps) right for
 * True ✅ and left for False ❌ in a 3-second rapid-fire loop. On a wrong
 * swipe the one-liner rationale is revealed instead of a heavy paragraph.
 */

import { motion, useMotionValue, useTransform } from "framer-motion";
import { useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type SwipeBooleanPayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_SWIPE = 6;

type Evaluation = "idle" | "correct" | "wrong";

export default function SwipeBoolean({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<SwipeBooleanPayload>) {
  const [evaluation, setEvaluation] = useState<Evaluation>("idle");
  const [swiped, setSwiped] = useState<boolean | null>(null);
  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);
  const x = useMotionValue(0);
  const rotate = useTransform(x, [-160, 160], [-12, 12]);
  const stampOpacity = useTransform(x, [-80, 0, 80], [0, 0, 0]);
  const rightStampOpacity = useTransform(x, [40, 110], [0, 1]);
  const leftStampOpacity = useTransform(x, [-110, -40], [1, 0]);

  const report = (given: boolean, isCorrect: boolean) => {
    if (reported.current) return;
    reported.current = true;
    const result: ChallengeOutcome = {
      isCorrect,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: isCorrect ? 0 : 1,
      responseJson: { challengeId, kind: "SWIPE_BOOLEAN", expected: payload.answer, given },
    };
    onResult(result);
  };

  const choose = (given: boolean) => {
    if (disabled || evaluation !== "idle") return;
    setSwiped(given);
    const isCorrect = given === payload.answer;
    if (isCorrect) {
      playSfx("correct");
      setEvaluation("correct");
      onXp?.(XP_FOR_SWIPE);
    } else {
      playSfx("incorrect");
      setEvaluation("wrong");
      onHeartLoss?.();
    }
    report(given, isCorrect);
  };

  return (
    <section className="flex flex-col items-center gap-5" dir="auto">
      <span className="w-fit rounded-xl bg-feather-blue/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-blue-dark">
        {payload.instructions}
      </span>

      {/* The swipeable card */}
      <motion.div
        style={{ x, rotate }}
        drag={evaluation === "idle" && !disabled ? "x" : false}
        dragConstraints={{ left: 0, right: 0 }}
        dragElastic={0.6}
        onDragEnd={(_, info) => {
          if (info.offset.x > 90) choose(true);
          else if (info.offset.x < -90) choose(false);
        }}
        className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-6 text-center shadow-card"
      >
        <p className="font-display text-lg font-extrabold leading-snug text-ink">
          <RichContent text={payload.statement} inline />
        </p>
        {/* Swipe stamps (opacity follows the drag position) */}
        <motion.span
          style={{ opacity: rightStampOpacity }}
          className="absolute right-4 top-4 rounded-lg border-2 border-feather-green px-2 py-0.5 font-display text-xs font-black text-feather-green"
          aria-hidden="true"
        >
          TRUE
        </motion.span>
        <motion.span
          style={{ opacity: leftStampOpacity }}
          className="absolute left-4 top-4 rounded-lg border-2 border-feather-red px-2 py-0.5 font-display text-xs font-black text-feather-red"
          aria-hidden="true"
        >
          FALSE
        </motion.span>
      </motion.div>

      {/* Big tactile verdict buttons (keyboard/touch fallback to dragging) */}
      <div className="grid w-full max-w-md grid-cols-2 gap-3">
        <motion.button
          type="button"
          data-pick
          disabled={disabled || evaluation !== "idle"}
          whileTap={evaluation === "idle" ? { scale: 0.96 } : undefined}
          onClick={() => choose(true)}
          className={`rounded-2xl border-2 border-b-4 px-4 py-4 font-display text-base font-extrabold transition-colors disabled:cursor-default ${
            evaluation === "idle"
              ? "border-surface-border bg-surface text-ink hover:border-feather-green/60"
              : payload.answer === true
                ? "border-feather-green bg-feather-green/15 text-feather-green-dark"
                : swiped === true
                  ? "border-feather-red bg-feather-red/15 text-feather-red-dark"
                  : "border-surface-border bg-surface opacity-60"
          }`}
        >
          👉 صح ✅
        </motion.button>
        <motion.button
          type="button"
          data-pick
          disabled={disabled || evaluation !== "idle"}
          whileTap={evaluation === "idle" ? { scale: 0.96 } : undefined}
          onClick={() => choose(false)}
          className={`rounded-2xl border-2 border-b-4 px-4 py-4 font-display text-base font-extrabold transition-colors disabled:cursor-default ${
            evaluation === "idle"
              ? "border-surface-border bg-surface text-ink hover:border-feather-red/60"
              : payload.answer === false
                ? "border-feather-green bg-feather-green/15 text-feather-green-dark"
                : swiped === false
                  ? "border-feather-red bg-feather-red/15 text-feather-red-dark"
                  : "border-surface-border bg-surface opacity-60"
          }`}
        >
          👈 خطأ ❌
        </motion.button>
      </div>

      {evaluation !== "idle" && (
        <motion.p
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className={`w-full max-w-md rounded-2xl border-2 px-4 py-3 text-center font-body text-xs font-bold leading-relaxed ${
            evaluation === "correct"
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
