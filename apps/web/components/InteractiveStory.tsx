"use client";

/**
 * INTERACTIVE_STORY — a conversation, not a quiz.
 * Each dialogue node appears as a chat bubble from its speaker, followed by the
 * decision prompt. Choosing correctly advances the scenario; choosing wrongly
 * appends the character's reaction (and the option is retried disabled), exactly
 * like a branching dialogue tree.
 */

import { AnimatePresence, motion } from "framer-motion";
import { MessageSquare, RotateCcw, Send } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  type ChallengeComponentProps,
  type ChallengeOutcome,
  type InteractiveStoryPayload,
  type StoryNode,
  type StoryOption,
} from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const XP_PER_NODE = 15;

type BubbleRole = "character" | "learner" | "reaction";

interface Bubble {
  id: string;
  role: BubbleRole;
  speaker: string;
  text: string;
  ok?: boolean;
}

interface DecisionLogEntry {
  nodeIndex: number;
  speaker: string;
  option: string;
  correct: boolean;
}

export default function InteractiveStory({
  challengeId,
  payload,
  onResult,
  onHeartLoss,
  onXp,
  disabled = false,
}: ChallengeComponentProps<InteractiveStoryPayload>) {
  const nodes = payload.dialogue;

  const [nodeIndex, setNodeIndex] = useState(0);
  const [bubbles, setBubbles] = useState<Bubble[]>(() =>
    nodes.length > 0
      ? [
          {
            id: `node-0`,
            role: "character",
            speaker: nodes[0].speaker,
            text: nodes[0].text,
          },
        ]
      : [],
  );
  const [rejected, setRejected] = useState<string[]>([]);
  const [pending, setPending] = useState<StoryOption | null>(null);
  const [decisions, setDecisions] = useState<DecisionLogEntry[]>([]);
  const [xpEarned, setXpEarned] = useState(0);
  const [finished, setFinished] = useState(false);

  const startedAt = useRef<number>(Date.now());
  const reported = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const node: StoryNode | undefined = nodes[nodeIndex];
  const wrongCount = decisions.filter((entry) => !entry.correct).length;
  const clearedNodes = decisions.filter((entry) => entry.correct).length;

  const report = (finalDecisions: DecisionLogEntry[]) => {
    if (reported.current) return;
    reported.current = true;
    const clean = finalDecisions.every((entry) => entry.correct);
    onResult({
      isCorrect: clean,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: finalDecisions.filter((entry) => !entry.correct).length,
      responseJson: {
        challengeId,
        kind: "INTERACTIVE_STORY",
        scenario: payload.scenario_title,
        decisions: finalDecisions,
      },
    });
  };

  const finish = (finalDecisions: DecisionLogEntry[]) => {
    setFinished(true);
    playSfx(finalDecisions.every((entry) => entry.correct) ? "fanfare" : "incorrect");
    window.setTimeout(() => report(finalDecisions), 300);
  };

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [bubbles.length, pending]);

  const choose = (option: StoryOption) => {
    if (!node || disabled || finished || pending) return;
    const entry: DecisionLogEntry = {
      nodeIndex,
      speaker: node.speaker,
      option: option.text,
      correct: option.correct,
    };
    const nextDecisions = [...decisions, entry];
    setDecisions(nextDecisions);
    setPending(option);
    playSfx(option.correct ? "correct" : "incorrect");

    setBubbles((prev) => [
      ...prev,
      { id: `you-${nodeIndex}-${option.text}`, role: "learner", speaker: "أنت", text: option.text },
      {
        id: `react-${nodeIndex}-${option.text}`,
        role: "reaction",
        speaker: node.speaker,
        text: option.feedback,
        ok: option.correct,
      },
    ]);

    if (!option.correct) {
      onHeartLoss?.();
      setRejected((prev) => [...prev, option.text]);
    } else {
      setXpEarned((prev) => prev + XP_PER_NODE);
      onXp?.(XP_PER_NODE);
    }
  };

  const advance = () => {
    if (!pending) return;
    const wasCorrect = pending.correct;
    setPending(null);
    if (!wasCorrect) return; // retry the same node

    setRejected([]);
    const nextIndex = nodeIndex + 1;
    if (nextIndex >= nodes.length) {
      finish(decisions);
      return;
    }
    setNodeIndex(nextIndex);
    setBubbles((prev) => [
      ...prev,
      {
        id: `node-${nextIndex}`,
        role: "character",
        speaker: nodes[nextIndex].speaker,
        text: nodes[nextIndex].text,
      },
    ]);
  };

  const progress = useMemo(
    () => (nodes.length === 0 ? 0 : (clearedNodes / nodes.length) * 100),
    [clearedNodes, nodes.length],
  );

  return (
    <section className="flex w-full flex-col gap-4" aria-label="Interactive Story challenge">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
            <MessageSquare className="h-5 w-5 text-feather-purple" aria-hidden="true" />
            {payload.scenario_title}
          </h2>
          <div className="flex items-center gap-3 font-display text-xs font-bold">
            {wrongCount > 0 && <span className="text-feather-red">{wrongCount} قرار خاطئ</span>}
            <span className="text-feather-purple">{xpEarned} XP</span>
          </div>
        </div>
        <p className="font-body text-sm text-ink-soft">{payload.instructions}</p>
        <div className="h-3 w-full overflow-hidden rounded-full bg-surface-border">
          <motion.div
            className="h-full rounded-full bg-feather-purple"
            animate={{ width: `${progress}%` }}
            transition={{ type: "spring", stiffness: 120, damping: 20 }}
          />
        </div>
      </header>

      {/* Conversation transcript */}
      <div
        ref={scrollRef}
        className="flex max-h-[22rem] flex-col gap-2 overflow-y-auto rounded-2xl border-2 border-surface-border bg-surface-muted p-3"
      >
        <AnimatePresence initial={false}>
          {bubbles.map((bubble) => {
            const isLearner = bubble.role === "learner";
            const isReaction = bubble.role === "reaction";
            return (
              <motion.div
                key={bubble.id}
                initial={{ opacity: 0, y: 12, scale: 0.97 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ type: "spring", stiffness: 220, damping: 24 }}
                className={`flex w-full ${isLearner ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] rounded-2xl border-2 px-3 py-2 ${
                    isLearner
                      ? "border-feather-blue/40 bg-feather-blue/10"
                      : isReaction
                        ? bubble.ok
                          ? "border-feather-green/50 bg-feather-green/10"
                          : "border-feather-red/50 bg-feather-red/10"
                        : "border-surface-border bg-surface"
                  }`}
                >
                  <p className="font-display text-[11px] font-bold uppercase tracking-wide text-ink-faint">
                    {bubble.speaker}
                  </p>
                  <p className="mt-0.5 font-body text-sm leading-relaxed text-ink" dir="auto">
                    {bubble.text}
                  </p>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>

      {/* Decision panel */}
      {!finished && node && (
        <div className="flex flex-col gap-2">
          <p className="font-display text-sm font-extrabold text-ink">{node.action_prompt}</p>
          {node.options.map((option) => {
            const isRejected = rejected.includes(option.text);
            const isPending = pending?.text === option.text;
            let cls =
              "w-full rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm transition-colors duration-150";
            if (isRejected) {
              cls +=
                " cursor-not-allowed border-feather-red/40 bg-feather-red/10 text-feather-red-dark line-through";
            } else if (isPending && option.correct) {
              cls += " border-feather-green bg-feather-green/15 text-ink";
            } else if (isPending) {
              cls += " border-feather-red bg-feather-red/10 text-ink";
            } else if (pending) {
              cls += " border-surface-border bg-surface-muted text-ink-faint";
            } else {
              cls += " border-surface-border bg-surface text-ink hover:bg-surface-muted";
            }
            return (
              <motion.button
                key={option.text}
                type="button"
                whileTap={{ scale: 0.98 }}
                disabled={disabled || isRejected || pending !== null}
                onClick={() => choose(option)}
                className={cls}
              >
                <span className="me-2 font-display text-xs font-bold text-ink-faint">
                  <Send className="inline h-3.5 w-3.5" aria-hidden="true" />
                </span>
                {option.text}
              </motion.button>
            );
          })}

          {pending && (
            <motion.button
              type="button"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              onClick={() => {
                playSfx("tap");
                advance();
              }}
              className="flex items-center justify-center gap-2 self-start rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple px-4 py-2 font-display text-xs font-extrabold uppercase tracking-wide text-white shadow-key"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              {pending.correct ? "أكمل الحوار" : "جرّب خياراً آخر"}
            </motion.button>
          )}
        </div>
      )}

      {finished && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className={`rounded-2xl border-2 p-4 ${
            wrongCount === 0
              ? "border-feather-green bg-feather-green/10"
              : "border-feather-yellow bg-feather-yellow/10"
          }`}
        >
          <p className="font-display text-base font-extrabold text-ink">
            {wrongCount === 0 ? "قرارات مثالية!" : "انتهى الموقف مع بعض التعثرات"}
          </p>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {clearedNodes}/{nodes.length} خطوات صحيحة · {wrongCount} قرار خاطئ · {xpEarned} XP
          </p>
          <ol className="mt-3 flex flex-col gap-2">
            {decisions.map((entry, index) => (
              <li
                key={index}
                className={`rounded-xl border-2 p-2 ${
                  entry.correct ? "border-feather-green/40 bg-surface" : "border-feather-red/40 bg-surface"
                }`}
              >
                <p className="font-body text-xs font-bold text-ink-faint">
                  {entry.speaker} · مقولة {entry.nodeIndex + 1}
                </p>
                <p className="font-body text-sm text-ink" dir="auto">
                  {entry.correct ? "✓" : "✗"} {entry.option}
                </p>
              </li>
            ))}
          </ol>
        </motion.div>
      )}
    </section>
  );
}
