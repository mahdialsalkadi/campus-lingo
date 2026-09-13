"use client";

/**
 * MATCH_MADNESS — 60-second tap-to-pair drill.
 * Tap a term, then its definition. Matching tiles burst green and raise the
 * combo multiplier; a mismatch shakes both tiles and costs a heart.
 * The round ends when every pair is matched or the countdown hits zero.
 */

import { AnimatePresence, motion } from "framer-motion";
import { Flame, Timer, Zap } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  shuffle,
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type MatchMadnessPayload,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

interface Tile {
  id: string;
  pairIndex: number;
  text: string;
}

interface Attempt {
  term: string;
  definition: string;
  correct: boolean;
}

const BASE_XP = 10;
const MAX_MULTIPLIER = 5;

export default function MatchMadness({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<MatchMadnessPayload>) {
  const termTiles = useMemo<Tile[]>(
    () =>
      shuffle(
        payload.pairs.map((pair, index) => ({
          id: `t-${index}`,
          pairIndex: index,
          text: pair.term,
        })),
      ),
    [payload],
  );
  const definitionTiles = useMemo<Tile[]>(
    () =>
      shuffle(
        payload.pairs.map((pair, index) => ({
          id: `d-${index}`,
          pairIndex: index,
          text: pair.definition,
        })),
      ),
    [payload],
  );

  const totalPairs = payload.pairs.length;
  const totalSeconds = payload.seconds > 0 ? payload.seconds : 60;

  const [selectedTerm, setSelectedTerm] = useState<Tile | null>(null);
  const [matched, setMatched] = useState<number[]>([]);
  const [burstIds, setBurstIds] = useState<string[]>([]);
  const [shakeIds, setShakeIds] = useState<string[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [combo, setCombo] = useState(0);
  const [maxCombo, setMaxCombo] = useState(0);
  const [xpEarned, setXpEarned] = useState(0);
  const [secondsLeft, setSecondsLeft] = useState(totalSeconds);
  const [comboFlash, setComboFlash] = useState<number | null>(null);
  const [finished, setFinished] = useState<{ correct: boolean; timedOut: boolean } | null>(null);

  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  const misses = attempts.filter((attempt) => !attempt.correct).length;

  const finish = useCallback(
    (correct: boolean, timedOut: boolean, finalAttempts: Attempt[], matchedCount: number) => {
      if (reported.current) return;
      reported.current = true;
      setFinished({ correct, timedOut });
      playSfx(correct ? "fanfare" : "incorrect");
      onResult({
        isCorrect: correct,
        elapsedMs: Date.now() - startedAt.current,
        hintsUsed: finalAttempts.filter((attempt) => !attempt.correct).length,
        responseJson: {
          challengeId,
          kind: "MATCH_MADNESS",
          matched: matchedCount,
          total: totalPairs,
          timedOut,
          bestCombo: maxCombo,
          xpEarned,
          attempts: finalAttempts,
        },
      });
    },
    [challengeId, onResult, totalPairs, maxCombo, xpEarned],
  );

  // Countdown
  useEffect(() => {
    if (finished || disabled) return;
    const timer = window.setInterval(() => {
      setSecondsLeft((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [finished, disabled]);

  useEffect(() => {
    if (secondsLeft === 0 && !finished && !reported.current) {
      finish(false, true, attempts, matched.length);
    }
  }, [secondsLeft, finished, attempts, matched.length, finish]);

  const resolvePair = useCallback(
    (term: Tile, definition: Tile) => {
      const isMatch = term.pairIndex === definition.pairIndex;
      const attempt: Attempt = {
        term: term.text,
        definition: definition.text,
        correct: isMatch,
      };
      const nextAttempts = [...attempts, attempt];
      setAttempts(nextAttempts);
      setSelectedTerm(null);

      if (isMatch) {
        const nextCombo = combo + 1;
        const multiplier = Math.min(MAX_MULTIPLIER, nextCombo);
        const gained = BASE_XP * multiplier;
        setCombo(nextCombo);
        setMaxCombo((prev) => Math.max(prev, nextCombo));
        setComboFlash(multiplier);
        setXpEarned((prev) => prev + gained);
        onXp?.(gained);
        playSfx("correct");

        const nextMatched = [...matched, term.pairIndex];
        setMatched(nextMatched);
        setBurstIds([term.id, definition.id]);
        window.setTimeout(() => setBurstIds([]), 480);
        window.setTimeout(() => setComboFlash(null), 700);

        if (nextMatched.length === totalPairs) {
          finish(true, false, nextAttempts, nextMatched.length);
        }
      } else {
        setCombo(0);
        playSfx("incorrect");
        setShakeIds([term.id, definition.id]);
        window.setTimeout(() => setShakeIds([]), 480);
        onHeartLoss?.();
      }
    },
    [attempts, combo, matched, totalPairs, finish, onHeartLoss, onXp],
  );

  const onTileClick = (tile: Tile, side: "term" | "definition") => {
    if (disabled || finished || matched.includes(tile.pairIndex)) return;
    if (side === "term") {
      playSfx("tap");
      if (selectedTerm?.id === tile.id) {
        setSelectedTerm(null);
        return;
      }
      setSelectedTerm(tile);
      return;
    }
    if (!selectedTerm) {
      playSfx("tap");
      return;
    }
    resolvePair(selectedTerm, tile);
  };

  const progress = (matched.length / totalPairs) * 100;
  const timeProgress = (secondsLeft / totalSeconds) * 100;
  const timeCritical = secondsLeft <= 10;
  const multiplier = Math.min(MAX_MULTIPLIER, combo || 1);

  const tileClass = (tile: Tile) => {
    const isMatched = matched.includes(tile.pairIndex);
    const isSelected = selectedTerm?.id === tile.id;
    const isShaking = shakeIds.includes(tile.id);
    const isBursting = burstIds.includes(tile.id);
    const base =
      "relative w-full overflow-hidden rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm transition-colors duration-150 disabled:cursor-not-allowed";
    if (isMatched) return `${base} border-feather-green bg-feather-green/15 text-ink opacity-70`;
    if (isBursting) return `${base} border-feather-green bg-feather-green/25 text-ink`;
    if (isShaking) return `${base} border-feather-red bg-feather-red/15 text-feather-red-dark`;
    if (isSelected) return `${base} border-feather-blue bg-feather-blue/10 text-ink shadow-key-sm`;
    return `${base} border-surface-border bg-surface text-ink hover:bg-surface-muted`;
  };

  const animateFor = (tile: Tile) =>
    shakeIds.includes(tile.id)
      ? { x: [0, -9, 9, -6, 6, 0], transition: { duration: 0.42 } }
      : burstIds.includes(tile.id)
        ? { scale: [1, 1.06, 1], transition: { duration: 0.4 } }
        : { x: 0, scale: 1 };

  return (
    <section className="flex w-full flex-col gap-4" aria-label="Match Madness challenge">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-display text-lg font-extrabold text-ink">مطابقة سريعة</h2>
          <div className="flex items-center gap-3 font-display text-sm font-bold">
            <motion.span
              key={multiplier}
              initial={{ scale: 1.4, opacity: 0.4 }}
              animate={{ scale: 1, opacity: 1 }}
              className={`flex items-center gap-1 ${
                combo > 1 ? "text-feather-orange" : "text-ink-faint"
              }`}
            >
              <Flame className="h-4 w-4" aria-hidden="true" />
              x{multiplier}
            </motion.span>
            <span className="flex items-center gap-1 text-feather-purple">
              <Zap className="h-4 w-4" aria-hidden="true" />
              {xpEarned}
            </span>
            <span
              className={`flex items-center gap-1 ${
                timeCritical ? "text-feather-red" : "text-ink-soft"
              }`}
            >
              <Timer className="h-4 w-4" aria-hidden="true" />
              {secondsLeft}s
            </span>
          </div>
        </div>
        <p className="font-body text-sm text-ink-soft">{payload.instructions}</p>

        <div className="h-3 w-full overflow-hidden rounded-full bg-surface-border">
          <motion.div
            className="h-full rounded-full bg-feather-green"
            animate={{ width: `${progress}%` }}
            transition={{ type: "spring", stiffness: 120, damping: 20 }}
          />
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-surface-border">
          <motion.div
            className={`h-full rounded-full ${timeCritical ? "bg-feather-red" : "bg-feather-yellow"}`}
            animate={{ width: `${timeProgress}%` }}
            transition={{ duration: 1, ease: "linear" }}
          />
        </div>

        <AnimatePresence>
          {comboFlash && comboFlash > 1 && (
            <motion.p
              key={comboFlash}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="font-display text-xs font-extrabold uppercase text-feather-orange"
            >
              Combo x{comboFlash}! نقاط مضاعفة
            </motion.p>
          )}
        </AnimatePresence>
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          {termTiles.map((tile) => (
            <motion.button
              key={tile.id}
              type="button"
              layout
              animate={animateFor(tile)}
              disabled={disabled || matched.includes(tile.pairIndex)}
              onClick={() => onTileClick(tile, "term")}
              className={tileClass(tile)}
            >
              {tile.text}
              {burstIds.includes(tile.id) && (
                <motion.span
                  className="pointer-events-none absolute inset-0 rounded-2xl bg-feather-green/40"
                  initial={{ opacity: 0.8, scale: 0.6 }}
                  animate={{ opacity: 0, scale: 1.25 }}
                  transition={{ duration: 0.45 }}
                />
              )}
            </motion.button>
          ))}
        </div>
        <div className="flex flex-col gap-2">
          {definitionTiles.map((tile) => (
            <motion.button
              key={tile.id}
              type="button"
              layout
              animate={animateFor(tile)}
              disabled={disabled || matched.includes(tile.pairIndex)}
              onClick={() => onTileClick(tile, "definition")}
              className={tileClass(tile)}
            >
              {tile.text}
              {burstIds.includes(tile.id) && (
                <motion.span
                  className="pointer-events-none absolute inset-0 rounded-2xl bg-feather-green/40"
                  initial={{ opacity: 0.8, scale: 0.6 }}
                  animate={{ opacity: 0, scale: 1.25 }}
                  transition={{ duration: 0.45 }}
                />
              )}
            </motion.button>
          ))}
        </div>
      </div>

      {finished && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 p-4 ${
            finished.correct
              ? "border-feather-green bg-feather-green/10"
              : "border-feather-red bg-feather-red/10"
          }`}
        >
          <p className="font-display text-base font-extrabold text-ink">
            {finished.correct
              ? "ممتاز! مطابقة كاملة"
              : finished.timedOut
                ? "انتهى الوقت!"
                : "أكملت المطابقة مع بعض الأخطاء"}
          </p>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {matched.length}/{totalPairs} أزواج · أفضل كومبو x{maxCombo || 1} · {xpEarned} XP ·{" "}
            {misses} خطأ · {Math.round((Date.now() - startedAt.current) / 1000)} ثانية
          </p>
          {misses > 0 && (
            <ul className="mt-3 flex flex-col gap-1">
              {attempts
                .filter((attempt) => !attempt.correct)
                .slice(0, 5)
                .map((attempt, index) => (
                  <li key={index} className="font-body text-xs text-ink-soft">
                    <span className="font-bold text-feather-red-dark">{attempt.term}</span> ≠{" "}
                    {attempt.definition}
                  </li>
                ))}
            </ul>
          )}
        </motion.div>
      )}
    </section>
  );
}
