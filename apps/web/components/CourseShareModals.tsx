"use client";

/**
 * Course sharing UI: two small dialogs used from the dashboard.
 *
 * - ShareCourseModal  — shows the course's share code (fetched lazily via
 *   GET /api/courses/{id}/share-code) with copy-to-clipboard.
 * - ImportCourseModal — pastes a code (CL-4F8A2B or bare 4F8A2B) and deep-clones
 *   the shared course into the user's account via POST /api/courses/import.
 */

import { Check, Copy, Download, Loader2, Share2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

// ---------------------------------------------------------------------------
// Share
// ---------------------------------------------------------------------------

interface ShareCourseModalProps {
  courseId: string | null;
  courseName: string;
  onClose: () => void;
}

export function ShareCourseModal({ courseId, courseName, onClose }: ShareCourseModalProps) {
  const [shareCode, setShareCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setShareCode(null);
    api
      .getShareCode(courseId)
      .then((response) => {
        if (!cancelled) setShareCode(response.share_code);
      })
      .catch((err) => {
        if (!cancelled) setError(errorText(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [courseId]);

  if (!courseId) return null;

  const copy = async () => {
    if (!shareCode) return;
    try {
      await navigator.clipboard.writeText(shareCode);
    } catch {
      // Clipboard API can be unavailable (http, permissions) — fall back.
      const area = document.createElement("textarea");
      area.value = shareCode;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
        <div className="flex items-start justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
            <Share2 className="h-5 w-5 text-feather-blue" aria-hidden="true" />
            مشاركة المقرر
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-ink-faint hover:bg-surface-muted"
            aria-label="إغلاق"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <p className="mt-1 font-body text-xs text-ink-soft">
          شارك كود «{courseName}» مع زملائك — يستوردونه من شاشة «استيراد مقرر».
        </p>

        {loading ? (
          <div className="mt-5 flex items-center justify-center gap-2 py-4">
            <Loader2 className="h-5 w-5 animate-spin text-feather-blue" aria-hidden="true" />
            <span className="font-body text-sm text-ink-soft">جارٍ تجهيز الكود…</span>
          </div>
        ) : error ? (
          <p className="mt-4 rounded-xl bg-feather-red/10 px-3 py-2 text-center font-body text-xs text-feather-red-dark">
            {error}
          </p>
        ) : (
          <>
            <div className="mt-4 rounded-2xl border-2 border-dashed border-feather-blue/50 bg-feather-blue/5 px-4 py-4 text-center">
              <p className="font-display text-3xl font-extrabold tracking-[0.2em] text-feather-blue-dark">
                {shareCode}
              </p>
            </div>
            <button
              type="button"
              onClick={() => void copy()}
              className={`mt-3 flex w-full items-center justify-center gap-2 rounded-xl border-2 border-b-4 px-4 py-3 font-display text-sm font-extrabold uppercase shadow-key active:translate-y-[2px] ${
                copied
                  ? "border-feather-green-dark bg-feather-green text-white"
                  : "border-feather-blue-dark bg-feather-blue text-white"
              }`}
            >
              {copied ? (
                <>
                  <Check className="h-4 w-4" aria-hidden="true" />
                  تم النسخ!
                </>
              ) : (
                <>
                  <Copy className="h-4 w-4" aria-hidden="true" />
                  نسخ الكود
                </>
              )}
            </button>
            <p className="mt-2 text-center font-body text-[11px] text-ink-faint">
              الكود يمنح نسخة كاملة من المقرر — شاركه مع من تثق بهم فقط.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

interface ImportCourseModalProps {
  open: boolean;
  /** Optional pre-filled code (e.g. from a /?import=CODE deep link). */
  initialCode?: string;
  onClose: () => void;
  onImported: (result: { courseId: string; name: string }) => void;
}

export function ImportCourseModal({ open, initialCode, onClose, onImported }: ImportCourseModalProps) {
  const userId = useQuizStore((state) => state.userId);
  const [code, setCode] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setCode(initialCode ?? "");
      setError(null);
    }
  }, [open, initialCode]);

  if (!open) return null;

  const submit = async () => {
    const trimmed = code.trim().toUpperCase();
    if (trimmed.replace(/^CL-/, "").length !== 6) {
      setError("الكود يتكوّن من 6 أحرف — مثال: CL-4F8A2B");
      return;
    }
    if (!userId) {
      setError("سجّل الدخول أولاً");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const result = await api.importCourse(userId, trimmed);
      onImported({ courseId: result.course_id, name: result.name });
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
        <div className="flex items-start justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
            <Download className="h-5 w-5 text-feather-green" aria-hidden="true" />
            استيراد مقرر
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-ink-faint hover:bg-surface-muted"
            aria-label="إغلاق"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <p className="mt-1 font-body text-xs text-ink-soft">
          الصق كود المشاركة لاستيراد نسخة كاملة من المقرر مع بطاقات مراجعة جديدة.
        </p>

        <input
          type="text"
          value={code}
          onChange={(event) => {
            setCode(event.target.value.toUpperCase());
            if (error) setError(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !pending) void submit();
          }}
          placeholder="CL-4F8A2B"
          dir="ltr"
          maxLength={12}
          disabled={pending}
          className="mt-4 w-full rounded-xl border-2 border-surface-border bg-surface px-4 py-3 text-center font-display text-lg font-extrabold tracking-[0.2em] outline-none focus:border-feather-green disabled:opacity-60"
          autoFocus
        />

        {error && (
          <p className="mt-2 rounded-xl bg-feather-red/10 px-3 py-2 text-center font-body text-xs text-feather-red-dark">
            {error}
          </p>
        )}

        <button
          type="button"
          onClick={() => void submit()}
          disabled={pending}
          className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] disabled:opacity-50"
        >
          {pending ? (
            <>
              <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
              جارٍ الاستيراد…
            </>
          ) : (
            <>
              <Download className="h-5 w-5" aria-hidden="true" />
              استيراد المقرر
            </>
          )}
        </button>
      </div>
    </div>
  );
}