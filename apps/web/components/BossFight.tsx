"use client";

/**
 * BOSS_FIGHT — chapter culmination.
 *
 * Rapid-fire questions synthesized across the whole chapter, answered against
 * a per-question countdown (15s default) with a boss HP bar that depletes on
 * every correct answer. The fight ends when the HP bar empties or every
 * question is spent; the whole engagement reports as one challenge outcome.
 */

import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type BossFightPayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

export default function BossFight({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<BossFightPayload>) {
  const total = payload.questions.length;
  const hpPerQuestion = 100 / Math.max(1, total);

  const [index, setIndex] = useState(0);
  const [hp, setHp] = useState(100);
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(payload.seconds_per_question);
  const [correctCount, setCorrectCount] = useState(0);
  const [finished, setFinished] = useState(false);
  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);
  const correctRef = useRef(0);
  const hpRef = useRef(100);

  const question = payload.questions[index];

  const finish = useCallback(
    (finalCorrect: number) => {
      if (reported.current || finished) return;
      reported.current = true;
      setFinished(true);
      const isCorrect = finalCorrect === total && total > 0;
      const result: ChallengeOutcome = {
        // The boss only falls to a flawless run; anything else is a partial
        // clear that still records the attempt (rating comes from speed).
        isCorrect: isCorrect || finalCorrect >= Math.ceil(total * 0.8),
        elapsedMs: Date.now() - startedAt.current,
        hintsUsed: finalCorrect === total ? 0 : 1,
        responseJson: {
          challengeId,
          kind: "BOSS_FIGHT",
          boss: payload.boss_name,
          correct: finalCorrect,
          total,
        },
      };
      onResult(result);
    },
    [challengeId, finished, onResult, payload.boss_name, total],
  );

  const advance = useCallback(
    (wasCorrect: boolean) => {
      const nextHp = Math.max(0, hpRef.current - (wasCorrect ? hpPerQuestion : 0));
      hpRef.current = nextHp;
      setHp(nextHp);
      if (wasCorrect) {
        correctRef.current += 1;
        setCorrectCount(correctRef.current);
        onXp?.(10);
      } else {
        onHeartLoss?.();
      }
      if (index + 1 >= total || nextHp <= 0) {
        finish(correctRef.current);
        return;
      }
      setIndex(index + 1);
      setSelected(null);
      setReveal(false);
      setSecondsLeft(payload.seconds_per_question);
    },
    [finish, hpPerQuestion, index, onHeartLoss, onXp, payload.seconds_per_question, total],
  );

  const choose = (option: number) => {
    if (disabled || reveal || finished || !question) return;
    setSelected(option);
    const wasCorrect = question.options[option]?.correct === true;
    if (wasCorrect) {
      playSfx("correct");
      setReveal(true);
      window.setTimeout(() => advance(true), 650);
    } else {
      playSfx("incorrect");
      setReveal(true);
      window.setTimeout(() => advance(false), 900);
    }
  };

  // Countdown: reaching 0 counts as a hit on the student, not on the boss.
  useEffect(() => {
    if (finished || reveal || disabled) return;
    const timer = window.setInterval(() => {
      setSecondsLeft((previous) => {
        if (previous <= 1) {
          playSfx("incorrect");
          advance(false);
          return payload.seconds_per_question;
        }
        return previous - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [advance, disabled, finished, payload.seconds_per_question, reveal]);

  if (!question) return null;

  const urgent = secondsLeft <= 5;

  return (
    <section className="flex flex-col gap-4" dir="auto">
      {/* Boss banner + HP bar */}
      <div className="rounded-3xl border-2 border-feather-red/40 bg-gradient-to-b from-feather-red/10 to-surface p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="font-display text-base font-black text-ink" dir="auto">
            {payload.boss_emoji ?? "🐲"} {payload.boss_name}
          </span>
          <span
            className={`rounded-xl px-2 py-0.5 font-display text-sm font-black tabular-nums ${
              urgent ? "bg-feather-red/15 text-feather-red" : "bg-ink/5 text-ink-soft"
            }`}
          >
            ⏱ {secondsLeft}s
          </span>
        </div>
        <div className="mt-2 h-4 overflow-hidden rounded-full border-2 border-surface-border bg-surface-muted">
          <motion.div
            className="h-full rounded-full bg-gradient-to-r from-feather-red to-feather-orange"
            animate={{ width: `${hp}%` }}
            transition={{ type: "spring", stiffness: 120, damping: 20 }}
          />
        </div>
        <div className="mt-1 flex items-center justify-between font-display text-[10px] font-extrabold text-ink-faint">
          <span>
            HP {Math.round(hp)}%
          </span>
          <span>
            {index + 1}/{total} · إصابات صحيحة {correctCount}
          </span>
        </div>
      </div>

      <span className="w-fit rounded-xl bg-feather-red/15 px-3 py-1 font-display text-[11px] font-extrabold uppercase text-feather-red">
        {payload.instructions}
      </span>

      <h2 className="font-display text-lg font-extrabold leading-snug text-ink" dir="auto">
        {question.question}
      </h2>

      <div className="grid gap-2.5">
        {question.options.map((option, optionIndex) => {
          const isCorrectOption = option.correct === true;
          let tone = "border-surface-border bg-surface hover:border-feather-purple/60";
          if (reveal) {
            if (isCorrectOption) tone = "border-feather-green bg-feather-green/15";
            else if (selected === optionIndex) tone = "border-feather-red bg-feather-red/15";
            else tone = "border-surface-border bg-surface opacity-60";
          }
          return (
            <motion.button
              key={optionIndex}
              type="button"
              data-pick
              disabled={disabled || reveal || finished}
              whileTap={!reveal && !finished ? { scale: 0.98 } : undefined}
              onClick={() => choose(optionIndex)}
              className={`rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm font-bold text-ink transition-colors disabled:cursor-default ${tone}`}
              dir="auto"
            >
              {option.text}
            </motion.button>
          );
        })}
      </div>

      {finished && (
        <motion.p
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 px-4 py-3 text-center font-body text-xs font-bold ${
            hp <= 0
              ? "border-feather-green bg-feather-green/10 text-feather-green-dark"
              : "border-feather-orange bg-feather-orange/10 text-feather-orange"
          }`}
          dir="auto"
        >
          {hp <= 0
            ? `🏆 هزمت ${payload.boss_name}! (${correctCount}/${total})`
            : `انتهت المعركة — ${correctCount}/${total} إصابة صحيحة`}{" "}
          {payload.one_liner_rationale}
        </motion.p>
      )}
    </section>
  );
}
