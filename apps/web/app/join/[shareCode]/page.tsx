"use client";

/**
 * /join/[shareCode] — viral peer-to-peer course sharing.
 *
 * Shows a public preview of a classmate's course and clones it into the
 * signed-in account in one tap (or enrolls into a classroom when the code is
 * an enrollment code). The clone copies lectures/nodes/challenges and seeds
 * fresh FSRS cards — **no LLM inference is re-triggered**, so joining is
 * instant and free.
 */

import {
  ArrowRight,
  BookOpen,
  Check,
  GraduationCap,
  Layers,
  SearchX,
  Sparkles,
  Users,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, api, type SharedCoursePreview } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

export default function JoinCoursePage({ params }: { params: { shareCode: string } }) {
  const shareCode = params.shareCode;
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);

  const [preview, setPreview] = useState<SharedCoursePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [joining, setJoining] = useState(false);
  const [joined, setJoined] = useState<{
    name: string;
    kind: "classroom" | "clone";
    challenges: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await api.previewSharedCourse(shareCode);
        if (!cancelled) setPreview(body);
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiError && err.status === 404) {
            setNotFound(true);
          } else {
            setError(err instanceof Error ? err.message : "رمز المشاركة غير صحيح");
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [shareCode]);

  const join = async () => {
    if (!userId) {
      setError("سجّل الدخول أولاً لنسخ المقرر إلى حسابك.");
      return;
    }
    setJoining(true);
    setError(null);
    try {
      const result = await api.joinSharedCourse(shareCode, userId);
      setJoined({
        name: result.name,
        kind: result.joined_kind === "classroom" ? "classroom" : "clone",
        challenges: result.challenges_cloned,
      });
      setTimeout(() => router.push("/"), 1200);
    } catch (err) {
      setError(err instanceof Error ? err.message : "تعذّر الانضمام");
    } finally {
      setJoining(false);
    }
  };

  if (notFound) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-10">
        <div className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-6 text-center shadow-card">
          <span className="mx-auto mb-3 flex h-14 w-14 items-center justify-center rounded-2xl bg-feather-red/15">
            <SearchX className="h-7 w-7 text-feather-red" aria-hidden="true" />
          </span>
          <h1 className="mb-1 font-display text-lg font-black text-ink">
            الكورس غير موجود
          </h1>
          <p className="mb-5 font-body text-sm text-ink-faint">
            تأكد من صحة الرمز — قد يكون الرابط منسوخاً بشكل ناقص أو منتهياً.
          </p>
          <p className="mb-5 rounded-2xl bg-surface-muted px-3 py-2 font-body text-[11px] text-ink-faint" dir="ltr">
            {shareCode}
          </p>
          <button
            type="button"
            onClick={() => router.push("/")}
            className="flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold text-white active:translate-y-[2px] active:border-b-2"
          >
            <ArrowRight className="h-4 w-4 rotate-180" aria-hidden="true" />
            العودة إلى الصفحة الرئيسية
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="flex flex-1 items-center justify-center px-4 py-10">
      <div className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
        <div className="mb-4 flex items-center gap-2">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-feather-green font-display text-lg font-black text-white">
            L
          </span>
          <div>
            <p className="font-display text-sm font-black text-ink">دعوة إلى مقرر</p>
            <p className="font-body text-[11px] text-ink-faint" dir="ltr">
              {shareCode}
            </p>
          </div>
        </div>

        {loading ? (
          <p className="py-8 text-center font-body text-xs text-ink-faint">
            جارٍ تحميل المقرر…
          </p>
        ) : preview ? (
          <>
            <h1 className="mb-1 font-display text-lg font-black text-ink" dir="auto">
              {preview.name}
            </h1>
            <p className="mb-4 font-body text-xs text-ink-faint">
              من{" "}
              <span className="font-bold text-ink-soft" dir="ltr">
                @{preview.owner_handle}
              </span>
              {preview.institution ? ` · ${preview.institution}` : ""}
              {preview.major ? ` · ${preview.major}` : ""}
            </p>

            <ul className="mb-4 grid grid-cols-2 gap-2">
              <li className="flex items-center gap-2 rounded-2xl border-2 border-surface-border p-2.5 font-body text-xs font-bold text-ink-soft">
                <BookOpen className="h-4 w-4 text-feather-blue" aria-hidden="true" />
                {preview.lectures} محاضرة
              </li>
              <li className="flex items-center gap-2 rounded-2xl border-2 border-surface-border p-2.5 font-body text-xs font-bold text-ink-soft">
                <Layers className="h-4 w-4 text-feather-purple" aria-hidden="true" />
                {preview.challenges} تحدٍّ
              </li>
              <li className="flex items-center gap-2 rounded-2xl border-2 border-surface-border p-2.5 font-body text-xs font-bold text-ink-soft">
                <Users className="h-4 w-4 text-feather-green" aria-hidden="true" />
                {preview.downloads_count} نسخة
              </li>
              <li className="flex items-center gap-2 rounded-2xl border-2 border-surface-border p-2.5 font-body text-xs font-bold text-ink-soft">
                <GraduationCap className="h-4 w-4 text-feather-orange" aria-hidden="true" />
                {preview.is_classroom ? "فصل دراسي" : "مقرر شخصي"}
              </li>
            </ul>

            {(preview.chapters?.length ?? 0) > 0 && (
              <div className="mb-4">
                <p className="mb-1.5 font-display text-[11px] font-black uppercase text-ink-faint">
                  الفصول
                </p>
                <ul className="max-h-32 space-y-1 overflow-y-auto rounded-2xl border-2 border-surface-border p-2">
                  {preview.chapters!.slice(0, 20).map((chapter, index) => (
                    <li
                      key={`${chapter}-${index}`}
                      className="flex items-center gap-1.5 font-body text-xs text-ink-soft"
                      dir="auto"
                    >
                      <BookOpen className="h-3 w-3 shrink-0 text-feather-blue" aria-hidden="true" />
                      <span className="truncate">{chapter}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {joined ? (
              <p className="flex items-center gap-2 rounded-2xl border-2 border-feather-green bg-feather-green/10 px-3 py-3 font-body text-xs font-bold text-feather-green-dark">
                <Check className="h-4 w-4" aria-hidden="true" />
                {joined.kind === "classroom"
                  ? `تم تسجيلك في فصل «${joined.name}» — ستظهر المحاضرات الجديدة تلقائياً.`
                  : `تم نسخ «${joined.name}» (${joined.challenges} تحدياً) إلى مسارك.`}
              </p>
            ) : (
              <button
                type="button"
                disabled={joining}
                onClick={() => void join()}
                className="flex w-full items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold text-white disabled:opacity-50 active:translate-y-[2px] active:border-b-2"
              >
                <Sparkles className="h-4 w-4" aria-hidden="true" />
                {joining
                  ? "جارٍ الانضمام…"
                  : preview.is_classroom
                    ? "انضم إلى الفصل"
                    : "انسخ المقرر إلى حسابي"}
              </button>
            )}

            {!preview.is_classroom && (
              <p className="mt-3 text-center font-body text-[10px] text-ink-faint">
                يُنسخ المحتوى كما هو بدون إعادة توليد — صفر تكلفة، نتيجة فورية.
              </p>
            )}
          </>
        ) : null}

        {error && (
          <p className="mt-3 rounded-2xl border-2 border-feather-red/40 bg-feather-red/10 px-3 py-2 font-body text-xs font-bold text-feather-red-dark">
            {error}
          </p>
        )}
      </div>
    </main>
  );
}
