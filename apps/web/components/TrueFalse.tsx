"use client";

/**
 * TRUE_FALSE — one statement, two big verdict buttons. Wrong answers reveal
 * the explanation; correct answers play the success tone. One shot per
 * statement (the FSRS review loop handles re-testing).
 */

import { motion } from "framer-motion";
import { useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type TrueFalsePayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_TF = 8;

type Evaluation = "idle" | "correct" | "wrong";

export default function TrueFalse({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<TrueFalsePayload>) {
  const [verdict, setVerdict] = useState<boolean | null>(null);
  const [evaluation, setEvaluation] = useState<Evaluation>("idle");
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
        kind: "TRUE_FALSE",
        expected: payload.answer,
        given: verdict,
      },
    };
    onResult(result);
  };

  const choose = (answer: boolean) => {
    if (disabled || evaluation !== "idle") return;
    setVerdict(answer);
    const isCorrect = answer === payload.answer;
    if (isCorrect) {
      playSfx("correct");
      setEvaluation("correct");
      onXp?.(XP_FOR_TF);
    } else {
      playSfx("incorrect");
      setEvaluation("wrong");
      onHeartLoss?.();
    }
    report(isCorrect);
  };

  const tone = (value: boolean) => {
    if (evaluation === "idle") {
      return verdict === value
        ? "border-feather-blue bg-feather-blue/10"
        : "border-surface-border bg-surface hover:border-feather-blue/60";
    }
    if (value === payload.answer) return "border-feather-green bg-feather-green/15";
    if (verdict === value) return "border-feather-red bg-feather-red/15";
    return "border-surface-border bg-surface opacity-60";
  };

  return (
    <section className="flex flex-col gap-4" dir="auto">
      <header className="flex flex-col gap-2">
        <span className="w-fit rounded-xl bg-feather-purple/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-purple">
          {payload.instructions}
        </span>
        <h2 className="font-display text-lg font-extrabold leading-snug text-ink">
          <RichContent text={payload.statement} inline />
        </h2>
      </header>

      <div className="grid grid-cols-2 gap-3">
        <motion.button
          type="button"
          data-pick
          disabled={disabled || evaluation !== "idle"}
          whileTap={evaluation === "idle" ? { scale: 0.97 } : undefined}
          onClick={() => choose(true)}
          className={`rounded-2xl border-2 border-b-4 px-4 py-5 font-display text-base font-extrabold text-ink transition-colors disabled:cursor-default ${tone(
            true,
          )}`}
        >
          ✔ صح
        </motion.button>
        <motion.button
          type="button"
          data-pick
          disabled={disabled || evaluation !== "idle"}
          whileTap={evaluation === "idle" ? { scale: 0.97 } : undefined}
          onClick={() => choose(false)}
          className={`rounded-2xl border-2 border-b-4 px-4 py-5 font-display text-base font-extrabold text-ink transition-colors disabled:cursor-default ${tone(
            false,
          )}`}
        >
          ✘ خطأ
        </motion.button>
      </div>

      {evaluation !== "idle" && (
        <motion.p
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 px-4 py-3 font-body text-xs leading-relaxed ${
            evaluation === "correct"
              ? "border-feather-green bg-feather-green/10 text-feather-green-dark"
              : "border-feather-red bg-feather-red/10 text-feather-red-dark"
          }`}
          dir="auto"
        >
          <RichContent
            text={payload.one_liner_rationale?.trim() || payload.explanation}
          />
        </motion.p>
      )}
    </section>
  );
}
