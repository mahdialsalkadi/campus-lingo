"use client";

/**
 * MATCH_MADNESS — tap-to-pair drill with a generous, reward-driven timer.
 *
 * Matching is BIDIRECTIONAL: pick a term OR a definition first, then whichever
 * tile lands second on the other column resolves the pair. Changing your mind
 * on either side simply re-highlights that side — there is no penalty for
 * re-selecting. Correct pairs burst emerald and add +5s to the countdown;
 * mismatches shake red for 600ms and cost a heart.
 *
 * The clock is never punitive: it starts at a comfortable 60s, and reaching 0
 * does not fail the drill. It flips to "untimed practice" (half XP) so the
 * learner can finish cementing every concept calmly.
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
/** Comfortable floor: the drill never gives you less than a full minute. */
const MIN_MATCH_SECONDS = 60;
/** Every correct match buys back this much clock. */
const TIME_BONUS_SECONDS = 5;
/** Mismatch feedback duration (shake + red flash). */
const SHAKE_MS = 600;
/** XP multiplier once the clock runs out — finish calmly, earn half. */
const TIMED_OUT_XP_FACTOR = 0.5;

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
  const totalSeconds = Math.max(payload.seconds > 0 ? payload.seconds : 0, MIN_MATCH_SECONDS);

  // Two independent selections — either side may be picked first.
  const [selectedLeft, setSelectedLeft] = useState<Tile | null>(null);
  const [selectedRight, setSelectedRight] = useState<Tile | null>(null);
  const [matched, setMatched] = useState<number[]>([]);
  const [burstIds, setBurstIds] = useState<string[]>([]);
  const [shakeIds, setShakeIds] = useState<string[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [combo, setCombo] = useState(0);
  const [maxCombo, setMaxCombo] = useState(0);
  const [xpEarned, setXpEarned] = useState(0);
  const [secondsLeft, setSecondsLeft] = useState(totalSeconds);
  const [comboFlash, setComboFlash] = useState<number | null>(null);
  /** Timestamp of the last +5s bonus, drives the floating indicator. */
  const [timeBonus, setTimeBonus] = useState<number | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [finished, setFinished] = useState<{ correct: boolean; timedOut: boolean } | null>(null);

  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);

  const misses = attempts.filter((attempt) => !attempt.correct).length;

  const finish = useCallback(
    (correct: boolean, didTimeOut: boolean, finalAttempts: Attempt[], matchedCount: number) => {
      if (reported.current) return;
      reported.current = true;
      setFinished({ correct, timedOut: didTimeOut });
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
          timedOut: didTimeOut,
          bestCombo: maxCombo,
          xpEarned,
          attempts: finalAttempts,
        },
      });
    },
    [challengeId, onResult, totalPairs, maxCombo, xpEarned],
  );

  // Countdown — paused the moment time runs out (practice continues untimed).
  useEffect(() => {
    if (finished || disabled || timedOut) return;
    const timer = window.setInterval(() => {
      setSecondsLeft((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [finished, disabled, timedOut]);

  // Reaching zero is a soft, non-punitive boundary: switch to practice mode.
  useEffect(() => {
    if (secondsLeft === 0 && !finished && !timedOut) {
      setTimedOut(true);
    }
  }, [secondsLeft, finished, timedOut]);

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
      setSelectedLeft(null);
      setSelectedRight(null);

      if (isMatch) {
        const nextCombo = combo + 1;
        const multiplier = Math.min(MAX_MULTIPLIER, nextCombo);
        const gained = Math.round(
          (BASE_XP * multiplier) * (timedOut ? TIMED_OUT_XP_FACTOR : 1),
        );
        setCombo(nextCombo);
        setMaxCombo((prev) => Math.max(prev, nextCombo));
        setComboFlash(multiplier);
        setXpEarned((prev) => prev + gained);
        onXp?.(gained);
        playSfx("correct");

        // Reward beat: a correct match buys back clock time (never past the
        // starting baseline) and floats a green "+5 ثوانٍ" by the timer.
        if (!timedOut) {
          setSecondsLeft((prev) => Math.min(prev + TIME_BONUS_SECONDS, totalSeconds));
          setTimeBonus(Date.now());
          window.setTimeout(() => setTimeBonus(null), 1200);
        }

        const nextMatched = [...matched, term.pairIndex];
        setMatched(nextMatched);
        setBurstIds([term.id, definition.id]);
        window.setTimeout(() => setBurstIds([]), 480);
        window.setTimeout(() => setComboFlash(null), 700);

        if (nextMatched.length === totalPairs) {
          // Completing every pair is a pass even in untimed practice mode.
          finish(true, timedOut, nextAttempts, nextMatched.length);
        }
      } else {
        setCombo(0);
        playSfx("incorrect");
        setShakeIds([term.id, definition.id]);
        window.setTimeout(() => setShakeIds([]), SHAKE_MS);
        onHeartLoss?.();
      }
    },
    [attempts, combo, matched, totalPairs, finish, onHeartLoss, onXp, timedOut, totalSeconds],
  );

  /**
   * Bidirectional tap handler.
   *
   * Selecting a side highlights it; tapping the OTHER side then evaluates the
   * pair. Tapping a second tile on the SAME side just moves the highlight
   * (no penalty). The counterpart selection is captured before the state
   * update so a same-tick pair resolves correctly.
   */
  const onTileClick = (tile: Tile, side: "term" | "definition") => {
    if (disabled || finished || matched.includes(tile.pairIndex)) return;
    playSfx("tap");

    if (side === "term") {
      if (selectedLeft?.id === tile.id) {
        setSelectedLeft(null);
        return;
      }
      const pendingRight = selectedRight;
      setSelectedLeft(tile);
      if (pendingRight) resolvePair(tile, pendingRight);
      return;
    }

    if (selectedRight?.id === tile.id) {
      setSelectedRight(null);
      return;
    }
    const pendingLeft = selectedLeft;
    setSelectedRight(tile);
    if (pendingLeft) resolvePair(pendingLeft, tile);
  };

  const progress = (matched.length / totalPairs) * 100;
  const timeProgress = (secondsLeft / totalSeconds) * 100;
  const timeCritical = !timedOut && secondsLeft <= 10;
  const multiplier = Math.min(MAX_MULTIPLIER, combo || 1);

  const tileClass = (tile: Tile) => {
    const isMatched = matched.includes(tile.pairIndex);
    const isSelected = selectedLeft?.id === tile.id || selectedRight?.id === tile.id;
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
      ? { x: [0, -9, 9, -6, 6, 0], transition: { duration: SHAKE_MS / 1000 } }
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
            <span className="relative flex items-center gap-1">
              <span
                className={`flex items-center gap-1 ${
                  timeCritical ? "text-feather-red" : "text-ink-soft"
                }`}
              >
                <Timer className="h-4 w-4" aria-hidden="true" />
                {timedOut ? "∞" : `${secondsLeft}s`}
              </span>
              <AnimatePresence>
                {timeBonus && !timedOut && (
                  <motion.span
                    key={timeBonus}
                    initial={{ opacity: 0, y: 4, scale: 0.85 }}
                    animate={{ opacity: 1, y: -14, scale: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.5 }}
                    className="pointer-events-none absolute -top-1 start-0 whitespace-nowrap font-display text-[11px] font-extrabold text-feather-green-dark"
                    role="status"
                  >
                    +{TIME_BONUS_SECONDS} ثوانٍ ⏱️
                  </motion.span>
                )}
              </AnimatePresence>
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

        {/* Gentle, non-punitive timeout banner: the drill continues untimed. */}
        <AnimatePresence>
          {timedOut && !finished && (
            <motion.p
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="rounded-2xl border-2 border-feather-yellow/60 bg-feather-yellow/10 px-3 py-2 text-center font-body text-xs font-bold text-ink-soft"
              role="status"
            >
              انتهى الوقت! أكمل المطابقة بهدوء لترسيخ المفاهيم (نصف النقاط)
            </motion.p>
          )}
        </AnimatePresence>
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2" aria-label="المصطلحات">
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
        <div className="flex flex-col gap-2" aria-label="التعريفات">
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
              ? finished.timedOut
                ? "أحسنت! أكملت المطابقة بهدوء"
                : "ممتاز! مطابقة كاملة"
              : "أكملت المطابقة مع بعض الأخطاء"}
          </p>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {matched.length}/{totalPairs} أزواج · أفضل كومبو x{maxCombo || 1} · {xpEarned} XP ·{" "}
            {misses} خطأ · {Math.round((Date.now() - startedAt.current) / 1000)} ثانية
            {finished.timedOut ? " · وضع غير موقوت (نصف XP)" : ""}
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
