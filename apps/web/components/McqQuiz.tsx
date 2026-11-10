"use client";

/**
 * MCQ_QUIZ — a classic multiple-choice question. Tapping an option locks it in:
 * correct plays the success tone and awards XP, wrong shakes, marks the right
 * answer green and reveals the explanation. One shot per question (Duolingo
 * practice style — the FSRS review loop handles re-testing).
 */

import { motion } from "framer-motion";
import { CheckCircle2, XCircle } from "lucide-react";
import { useRef, useState } from "react";

import RichContent from "@/components/RichContent";
import {
  shuffle,
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type McqQuizPayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_FOR_MCQ = 10;

type Evaluation = "idle" | "correct" | "wrong";

export default function McqQuiz({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<McqQuizPayload>) {
  // Shuffle once per mount so the correct answer is not always first.
  const options = useRef(shuffle([...payload.options])).current;

  const [picked, setPicked] = useState<string | null>(null);
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
        kind: "MCQ_QUIZ",
        expected: payload.options.find((option) => option.correct)?.text ?? "",
        given: picked,
      },
    };
    onResult(result);
  };

  const choose = (text: string) => {
    if (disabled || evaluation !== "idle") return;
    setPicked(text);
    const isCorrect =
      text.trim().toLowerCase() ===
      (payload.options.find((option) => option.correct)?.text ?? "")
        .trim()
        .toLowerCase();
    if (isCorrect) {
      playSfx("correct");
      setEvaluation("correct");
      onXp?.(XP_FOR_MCQ);
    } else {
      playSfx("incorrect");
      setEvaluation("wrong");
      onHeartLoss?.();
    }
    report(isCorrect);
  };

  const optionTone = (text: string) => {
    if (evaluation === "idle") {
      return picked === text
        ? "border-feather-blue bg-feather-blue/10"
        : "border-surface-border bg-surface hover:border-feather-blue/60";
    }
    const isAnswer =
      text.trim().toLowerCase() ===
      (payload.options.find((option) => option.correct)?.text ?? "")
        .trim()
        .toLowerCase();
    if (isAnswer) return "border-feather-green bg-feather-green/15";
    if (picked === text) return "border-feather-red bg-feather-red/15";
    return "border-surface-border bg-surface opacity-60";
  };

  return (
    <section className="flex flex-col gap-4" dir="auto">
      <header className="flex flex-col gap-2">
        <span className="w-fit rounded-xl bg-feather-blue/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-blue-dark">
          {payload.instructions}
        </span>
        <h2 className="font-display text-lg font-extrabold leading-snug text-ink">
          <RichContent text={payload.question} inline />
        </h2>
      </header>

      <div className="flex flex-col gap-2">
        {options.map((option) => (
          <motion.button
            key={option.text}
            type="button"
            data-pick
            disabled={disabled || evaluation !== "idle"}
            whileTap={evaluation === "idle" ? { scale: 0.98 } : undefined}
            onClick={() => choose(option.text)}
            className={`flex items-center gap-3 rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm font-bold text-ink transition-colors disabled:cursor-default ${optionTone(
              option.text,
            )}`}
          >
            <span
              className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 ${
                evaluation !== "idle" && option.correct
                  ? "border-feather-green bg-feather-green text-white"
                  : picked === option.text
                    ? "border-feather-blue bg-feather-blue text-white"
                    : "border-surface-border"
              }`}
            >
              {evaluation !== "idle" && option.correct && (
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              )}
              {picked === option.text && evaluation === "wrong" && (
                <XCircle className="h-4 w-4" aria-hidden="true" />
              )}
            </span>
            <RichContent text={option.text} inline />
          </motion.button>
        ))}
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
