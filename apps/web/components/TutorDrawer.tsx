"use client";

/**
 * TutorDrawer — the in-quiz "💡 أحتاج تلميحاً" panel.
 *
 * Slides up from the bottom as a glassmorphic sheet and streams the Tier 1
 * (Qwen 3.5 0.8B) Socratic clue word-by-word. The 10-gem price is charged
 * server-side by ``POST /api/tutor/hint`` (402 when the balance is short), so
 * the client only mirrors the result via ``onCharged``.
 *
 * Aborting the stream on dismiss is important: an abandoned hint must stop
 * consuming GPU time the learner is no longer reading.
 */

import { AnimatePresence, motion } from "framer-motion";
import { Bot, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, streamTutorHint } from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

export const TUTOR_HINT_COST = 10;

/**
 * If the model tier is busy (or the SSE stream never flushes), wait at most
 * this long for the first token before aborting and showing a canned nudge.
 * Without it the drawer sat on "يفكّر المعلّم…" indefinitely.
 */
const FIRST_CHUNK_TIMEOUT_MS = 8_000;

const TUTOR_BUSY_FALLBACK =
  "معلم الذكاء الاصطناعي مشغول حالياً، إليك تلميح سريع: ركز على الكلمات المفتاحية في رأس السؤال.";

export default function TutorDrawer({
  open,
  onClose,
  userId,
  challengeId,
  question,
  studentChoice,
  context,
  gems,
  onCharged,
}: {
  open: boolean;
  onClose: () => void;
  userId: string | null;
  challengeId?: string;
  question: string;
  studentChoice?: string | null;
  context?: string | null;
  gems: number;
  /** Called after a successful (charged) hint so the HUD can resync gems. */
  onCharged: () => void;
}): JSX.Element {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }, []);

  const request = useCallback(async () => {
    if (busy) return;
    setError(null);
    setText("");
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;

    // Watchdog: abort the stream when the first token never lands. The abort
    // surfaces as an AbortError in the catch below, so the fallback set here is
    // what the learner actually reads.
    let gotFirstChunk = false;
    const watchdog = window.setTimeout(() => {
      if (gotFirstChunk || controller.signal.aborted) return;
      controller.abort();
      setText(TUTOR_BUSY_FALLBACK);
      setBusy(false);
    }, FIRST_CHUNK_TIMEOUT_MS);

    try {
      await streamTutorHint(
        {
          challenge_id: challengeId,
          question,
          student_choice: studentChoice ?? undefined,
          context: context ?? undefined,
          user_id: userId ?? undefined,
        },
        (event) => {
          if (event.type === "delta" && event.text) {
            gotFirstChunk = true;
            window.clearTimeout(watchdog);
            setText((previous) => previous + event.text);
          } else if (event.type === "error") {
            setError("تعذّر الاتصال بمعلّم الذكاء الاصطناعي — جرّب لاحقاً.");
          }
        },
        controller.signal,
      );
      // A stream that closed cleanly without ever emitting a token still needs
      // to leave the drawer with something actionable.
      if (!gotFirstChunk && !controller.signal.aborted) {
        setText((previous) => previous || TUTOR_BUSY_FALLBACK);
      }
      onCharged();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(
        err instanceof ApiError
          ? err.status === 402
            ? `تحتاج ${TUTOR_HINT_COST} جواهر للحصول على تلميح (لديك ${gems}).`
            : err.message
          : "تعذّر تحميل التلميح.",
      );
    } finally {
      window.clearTimeout(watchdog);
      setBusy(false);
      abortRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, challengeId, context, gems, onCharged, question, studentChoice, userId]);

  // Kick off the stream the moment the drawer opens; abort when it closes.
  useEffect(() => {
    if (open) {
      playSfx("whoosh");
      void request();
    } else {
      stop();
    }
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-ink/30 backdrop-blur-[2px]"
          />
          <motion.aside
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", stiffness: 260, damping: 30 }}
            className="fixed inset-x-0 bottom-0 z-50 mx-auto w-full max-w-2xl rounded-t-3xl border-2 border-b-0 border-white/25 bg-surface/80 p-5 shadow-card backdrop-blur-xl"
            role="dialog"
            aria-modal="true"
            aria-label="تلميح المعلّم الذكي"
          >
            <header className="flex items-center gap-3">
              <motion.span
                animate={{ scale: [1, 1.08, 1] }}
                transition={{ repeat: busy ? Infinity : 0, duration: 1.4 }}
                className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-feather-purple to-feather-blue text-white shadow-key"
              >
                <Bot className="h-5 w-5" aria-hidden="true" />
              </motion.span>
              <div className="min-w-0 flex-1">
                <h2 className="font-display text-base font-black text-ink">CampusTutor</h2>
                <p className="flex items-center gap-1 font-body text-[11px] text-ink-faint">
                  <Sparkles className="h-3 w-3" aria-hidden="true" />
                  {TUTOR_HINT_COST} جواهر لكل تلميح · Qwen 0.8B
                </p>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl p-1.5 text-ink-faint transition-colors hover:bg-surface-muted hover:text-ink"
                aria-label="إغلاق التلميح"
              >
                <X className="h-5 w-5" aria-hidden="true" />
              </button>
            </header>

            <div className="mt-3 max-h-[45vh] overflow-y-auto rounded-2xl border-2 border-surface-border bg-surface/70 p-4">
              {error ? (
                <p className="font-body text-sm text-feather-red-dark" dir="auto">
                  {error}
                </p>
              ) : text ? (
                <p
                  className="font-body text-sm leading-relaxed text-ink"
                  dir="auto"
                  data-testid="tutor-stream"
                >
                  {text}
                  {busy && <span className="ms-0.5 animate-pulse">▍</span>}
                </p>
              ) : (
                <p className="animate-pulse font-body text-sm text-ink-faint">
                  يفكّر المعلّم… 💭
                </p>
              )}
            </div>

            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={request}
                disabled={busy}
                className="rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple/10 px-4 py-2 font-display text-xs font-extrabold uppercase text-feather-purple active:translate-y-[2px] active:border-b-0 disabled:opacity-50"
              >
                تلميح آخر
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2 font-display text-xs font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
              >
                فهمت، أكمل
              </button>
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
