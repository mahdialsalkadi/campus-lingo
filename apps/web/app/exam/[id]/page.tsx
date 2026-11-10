"use client";

/**
 * /exam/[id] — Exam Crunch: cheatsheet + diagnostic timed mock exam.
 *
 * Two tabs share one server session:
 *   • **Cheatsheet** — the Markdown revision sheet, print-to-PDF ready.
 *   • **Mock exam** — 25-40 questions with a 45-minute countdown synchronized to
 *     the server's `started_at` (survives a reload); answers are collected
 *     locally and graded server-side on submit.
 */

import { AlertTriangle, Check, Clock, FileText, Printer, Send } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type ExamQuestionPublic, type ExamSessionResponse } from "@/lib/api";
import { playSfx, useQuizStore } from "@/stores/quizStore";

function formatClock(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const rest = safe % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function MarkdownBlock({ markdown }: { markdown: string }) {
  // Deliberately light rendering: headings, bullets and paragraphs only — no
  // HTML is ever injected, so a model answer cannot smuggle markup in.
  const blocks = useMemo(() => markdown.split("\n"), [markdown]);
  return (
    <div className="flex flex-col gap-1.5" dir="auto">
      {blocks.map((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) return <span key={`gap-${index}`} className="h-1.5" />;
        if (trimmed.startsWith("### ")) {
          return (
            <h4 key={index} className="mt-2 font-display text-sm font-black text-ink">
              {trimmed.slice(4)}
            </h4>
          );
        }
        if (trimmed.startsWith("## ")) {
          return (
            <h3
              key={index}
              className="mt-3 border-b-2 border-surface-border pb-1 font-display text-base font-black text-ink"
            >
              {trimmed.slice(3)}
            </h3>
          );
        }
        if (trimmed.startsWith("# ")) {
          return (
            <h2 key={index} className="font-display text-lg font-black text-ink">
              {trimmed.slice(2)}
            </h2>
          );
        }
        if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
          return (
            <p key={index} className="flex gap-2 font-body text-xs leading-6 text-ink-soft">
              <span className="text-feather-green">•</span>
              <span>{trimmed.slice(2)}</span>
            </p>
          );
        }
        return (
          <p key={index} className="font-body text-xs leading-6 text-ink-soft">
            {trimmed}
          </p>
        );
      })}
    </div>
  );
}

export default function ExamPage({ params }: { params: { id: string } }) {
  const examId = params.id;
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);

  const [exam, setExam] = useState<ExamSessionResponse | null>(null);
  const [tab, setTab] = useState<"cheatsheet" | "exam">("cheatsheet");
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    (async () => {
      try {
        const body = await api.examSession(examId, userId);
        if (cancelled) return;
        setExam(body);
        setSecondsLeft(body.seconds_remaining);
        if (body.status === "graded") router.replace(`/exam/${examId}/summary`);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "تعذّر تحميل الامتحان");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [examId, userId, router]);

  const submit = useCallback(
    async (auto = false) => {
      if (!userId || submitting) return;
      setSubmitting(true);
      setError(null);
      try {
        await api.submitExam(examId, userId, answers);
        playSfx("fanfare");
        router.push(`/exam/${examId}/summary`);
      } catch (err) {
        setError(
          auto
            ? "انتهى الوقت وتعذّر الإرسال — أعد المحاولة."
            : err instanceof Error
              ? err.message
              : "تعذّر إرسال الامتحان",
        );
        setSubmitting(false);
      }
    },
    [answers, examId, router, submitting, userId],
  );

  // Synchronized countdown: ticks down locally, auto-submits at zero.
  useEffect(() => {
    if (secondsLeft === null || exam?.status !== "active") return;
    if (secondsLeft <= 0) {
      void submit(true);
      return;
    }
    const timer = window.setTimeout(() => setSecondsLeft((prev) => (prev ?? 1) - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [secondsLeft, exam?.status, submit]);

  const questions: ExamQuestionPublic[] = exam?.questions ?? [];
  const current = questions[index];
  const answeredCount = Object.keys(answers).length;

  if (!userId) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="font-display text-lg font-extrabold text-ink-soft">
          سجّل الدخول لبدء جلسة Crunch.
        </p>
      </main>
    );
  }

  if (loading) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="animate-pulse font-display text-lg font-extrabold text-ink-soft">
          جارٍ تجميع ورقة المراجعة…
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col gap-4 px-4 py-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="font-display text-xl font-black text-ink">
            ⚡ Exam Crunch
          </h1>
          <p className="font-body text-xs text-ink-faint">
            {questions.length} سؤال · {Math.round((exam?.duration_seconds ?? 0) / 60)} دقيقة
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div
            className={`flex items-center gap-1.5 rounded-2xl border-2 px-3 py-2 font-display text-sm font-black ${
              (secondsLeft ?? 0) <= 300
                ? "border-feather-red bg-feather-red/10 text-feather-red-dark"
                : "border-surface-border bg-surface text-ink"
            }`}
          >
            <Clock className="h-4 w-4" aria-hidden="true" />
            {formatClock(secondsLeft ?? 0)}
          </div>
          <div className="flex overflow-hidden rounded-2xl border-2 border-surface-border">
            <button
              type="button"
              onClick={() => setTab("cheatsheet")}
              className={`flex items-center gap-1.5 px-3 py-2 font-display text-xs font-extrabold ${
                tab === "cheatsheet" ? "bg-feather-green text-white" : "bg-surface text-ink-soft"
              }`}
            >
              <FileText className="h-3.5 w-3.5" aria-hidden="true" />
              ورقة المراجعة
            </button>
            <button
              type="button"
              onClick={() => setTab("exam")}
              className={`px-3 py-2 font-display text-xs font-extrabold ${
                tab === "exam" ? "bg-feather-blue text-white" : "bg-surface text-ink-soft"
              }`}
            >
              الامتحان التجريبي
            </button>
          </div>
        </div>
      </header>

      {error && (
        <p className="rounded-2xl border-2 border-feather-red/40 bg-feather-red/10 px-3 py-2 font-body text-xs font-bold text-feather-red-dark">
          {error}
        </p>
      )}

      {tab === "cheatsheet" ? (
        <section
          id="cheatsheet"
          className="rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-display text-sm font-extrabold text-ink">
              ورقة الطوارئ عالية العائد
            </h2>
            <button
              type="button"
              onClick={() => window.print()}
              className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft active:translate-y-[2px] active:border-b-2"
            >
              <Printer className="h-3.5 w-3.5" aria-hidden="true" />
              طباعة / PDF
            </button>
          </div>
          <MarkdownBlock markdown={exam?.cheatsheet ?? ""} />
        </section>
      ) : (
        <section className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-border">
              <div
                className="h-full rounded-full bg-feather-blue transition-[width] duration-300"
                style={{
                  width: `${questions.length ? (answeredCount / questions.length) * 100 : 0}%`,
                }}
              />
            </div>
            <span className="shrink-0 font-display text-[11px] font-extrabold text-ink-soft">
              {answeredCount}/{questions.length}
            </span>
          </div>

          {current && (
            <article className="rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
              <p className="mb-1 font-body text-[11px] font-bold uppercase tracking-wide text-ink-faint">
                {current.chapter} · سؤال {index + 1}
              </p>
              <p className="mb-3 font-body text-sm font-extrabold text-ink" dir="auto">
                {current.prompt}
              </p>
              <div className="flex flex-col gap-2">
                {current.options.map((option, optionIndex) => {
                  const chosen = answers[current.challenge_id] === optionIndex;
                  return (
                    <button
                      key={`${option}-${optionIndex}`}
                      type="button"
                      onClick={() => {
                        playSfx("tap");
                        setAnswers((prev) => ({ ...prev, [current.challenge_id]: optionIndex }));
                      }}
                      className={`flex items-center justify-between gap-2 rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm font-bold transition-all active:translate-y-[2px] active:border-b-2 ${
                        chosen
                          ? "border-feather-blue-dark bg-feather-blue/15 text-feather-blue-dark"
                          : "border-surface-border bg-surface text-ink hover:bg-surface-muted"
                      }`}
                      dir="auto"
                    >
                      <span>{option}</span>
                      {chosen && <Check className="h-4 w-4 shrink-0" aria-hidden="true" />}
                    </button>
                  );
                })}
              </div>
            </article>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex gap-2">
              <button
                type="button"
                disabled={index === 0}
                onClick={() => setIndex((prev) => Math.max(0, prev - 1))}
                className="rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2 font-display text-xs font-extrabold text-ink-soft disabled:opacity-40 active:translate-y-[2px] active:border-b-2"
              >
                السابق
              </button>
              <button
                type="button"
                disabled={index >= questions.length - 1}
                onClick={() => setIndex((prev) => Math.min(questions.length - 1, prev + 1))}
                className="rounded-2xl border-2 border-b-4 border-feather-blue/50 bg-feather-blue/10 px-4 py-2 font-display text-xs font-extrabold text-feather-blue-dark disabled:opacity-40 active:translate-y-[2px] active:border-b-2"
              >
                التالي
              </button>
            </div>
            <button
              type="button"
              disabled={submitting}
              onClick={() => void submit(false)}
              className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-2 font-display text-xs font-extrabold text-white disabled:opacity-50 active:translate-y-[2px] active:border-b-2"
            >
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
              {submitting ? "جارٍ التصحيح…" : "إنهاء وعرض التشخيص"}
            </button>
          </div>

          {answeredCount < questions.length && (
            <p className="flex items-center gap-1.5 font-body text-[11px] text-ink-faint">
              <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
              الأسئلة غير المُجابة تُحسب خاطئة عند الإرسال.
            </p>
          )}
        </section>
      )}

      <Link
        href="/"
        className="mx-auto font-display text-xs font-extrabold text-ink-faint hover:text-ink"
      >
        عودة إلى المسار
      </Link>
    </main>
  );
}
