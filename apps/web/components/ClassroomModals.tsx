"use client";

/**
 * Classroom creation UI.
 *
 * - CreateClassroomModal — collects name / section / description and opens a
 *   classroom via POST /api/classrooms (which mints a 6-char enrollment code).
 * - ClassroomShareCard — the post-creation share panel: the raw enrollment
 *   code plus the full `/join/{code}` link, each with a 1-click copy button and
 *   an animated green success toast.
 */

import { motion } from "framer-motion";
import { Check, Copy, GraduationCap, Link2, Loader2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { ApiError, api, type ClassroomSummary } from "@/lib/api";

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

/** Clipboard write with a legacy fallback for http / restricted contexts. */
async function copyText(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = document.createElement("textarea");
    area.value = value;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
  }
}

// ---------------------------------------------------------------------------
// Share card
// ---------------------------------------------------------------------------

interface ClassroomShareCardProps {
  classroom: ClassroomSummary;
  onDone?: () => void;
}

export function ClassroomShareCard({ classroom, onDone }: ClassroomShareCardProps) {
  const [toast, setToast] = useState<string | null>(null);
  const [origin, setOrigin] = useState("");

  useEffect(() => {
    setOrigin(typeof window !== "undefined" ? window.location.origin : "");
  }, []);

  const code = classroom.enrollment_code ?? "";
  const joinLink = origin && code ? `${origin}/join/${code}` : "";

  const flash = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(null), 2200);
  };

  const copyCode = async () => {
    await copyText(code);
    flash("تم نسخ الكود إلى الحافظة بنجاح!");
  };

  const copyLink = async () => {
    await copyText(joinLink);
    flash("تم نسخ الرابط إلى الحافظة بنجاح!");
  };

  return (
    <div className="relative">
      <div className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
        <GraduationCap className="h-5 w-5 text-feather-green" aria-hidden="true" />
        «{classroom.name}» جاهز!
      </div>
      <p className="mt-1 font-body text-xs text-ink-soft">
        شارك الكود أو الرابط مع طلابك. كل من ينضم يرى المحاضرات الجديدة تلقائياً.
      </p>

      {/* Raw enrollment code */}
      <div className="mt-4 rounded-2xl border-2 border-dashed border-feather-green/50 bg-feather-green/5 px-4 py-3 text-center">
        <p className="font-body text-[11px] font-bold uppercase tracking-widest text-ink-faint">
          كود الانضمام
        </p>
        <p className="font-display text-3xl font-extrabold tracking-[0.25em] text-feather-green-dark" dir="ltr">
          {code || "—"}
        </p>
        <button
          type="button"
          onClick={() => void copyCode()}
          disabled={!code}
          className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-2.5 font-display text-sm font-extrabold text-white shadow-key active:translate-y-[2px] active:border-b-0 disabled:opacity-50"
        >
          <Copy className="h-4 w-4" aria-hidden="true" />
          📋 نسخ الكود
        </button>
      </div>

      {/* Full join link */}
      <div className="mt-3 rounded-2xl border-2 border-surface-border bg-surface-muted px-4 py-3">
        <p className="font-body text-[11px] font-bold uppercase tracking-widest text-ink-faint">
          رابط الانضمام
        </p>
        <p className="mt-1 truncate font-body text-sm font-bold text-ink" dir="ltr" title={joinLink}>
          {joinLink || "—"}
        </p>
        <button
          type="button"
          onClick={() => void copyLink()}
          disabled={!joinLink}
          className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2.5 font-display text-sm font-extrabold text-white shadow-key active:translate-y-[2px] active:border-b-0 disabled:opacity-50"
        >
          <Link2 className="h-4 w-4" aria-hidden="true" />
          🔗 نسخ رابط الانضمام
        </button>
      </div>

      {onDone && (
        <button
          type="button"
          onClick={onDone}
          className="mt-4 w-full rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-sm font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
        >
          تم
        </button>
      )}

      {toast && (
        <motion.p
          initial={{ opacity: 0, y: 8, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          className="absolute inset-x-0 -bottom-1 mx-auto w-fit rounded-xl border-2 border-feather-green-dark bg-feather-green px-3 py-2 font-display text-xs font-extrabold text-white shadow-card"
          role="status"
        >
          ✅ {toast}
        </motion.p>
      )}
    </div>
  );
}

interface ClassroomShareModalProps {
  classroom: ClassroomSummary | null;
  onClose: () => void;
}

/** Re-open the share card from classroom settings / the taught-classes list. */
export function ClassroomShareModal({ classroom, onClose }: ClassroomShareModalProps) {
  if (!classroom) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
        <div className="flex items-start justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
            <GraduationCap className="h-5 w-5 text-feather-green" aria-hidden="true" />
            مشاركة الفصل
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
        <div className="mt-5">
          <ClassroomShareCard classroom={classroom} onDone={onClose} />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create modal
// ---------------------------------------------------------------------------

interface CreateClassroomModalProps {
  open: boolean;
  instructorId: string | null;
  /** Pre-selects the classroom list refresh once a class is created. */
  onCreated: (classroom: ClassroomSummary) => void;
  onClose: () => void;
}

export function CreateClassroomModal({
  open,
  instructorId,
  onCreated,
  onClose,
}: CreateClassroomModalProps) {
  const [name, setName] = useState("");
  const [section, setSection] = useState("");
  const [description, setDescription] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ClassroomSummary | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setSection("");
      setDescription("");
      setError(null);
      setCreated(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("أدخل اسم الفصل الدراسي (مثال: Data Structures CS202)");
      return;
    }
    if (!instructorId) {
      setError("سجّل الدخول أولاً");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const classroom = await api.createClassroom(instructorId, {
        name: trimmed,
        section: section.trim(),
        description: description.trim(),
      });
      setCreated(classroom);
      onCreated(classroom);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
        <div className="flex items-start justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-lg font-extrabold text-ink">
            <GraduationCap className="h-5 w-5 text-feather-green" aria-hidden="true" />
            🏫 إنشاء فصل دراسي
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

        {created ? (
          <div className="mt-5">
            <ClassroomShareCard classroom={created} onDone={onClose} />
          </div>
        ) : (
          <>
            <p className="mt-1 font-body text-xs text-ink-soft">
              سيحصل الفصل على كود انضمام مكوّن من 6 رموز، ويمكنك مشاركته فوراً.
            </p>

            <label className="mt-4 block font-display text-xs font-extrabold text-ink">
              اسم الفصل
              <input
                type="text"
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                  if (error) setError(null);
                }}
                placeholder="Data Structures CS202"
                maxLength={200}
                disabled={pending}
                className="mt-1 w-full rounded-xl border-2 border-surface-border bg-surface px-3 py-2.5 font-body text-sm outline-none focus:border-feather-green disabled:opacity-60"
                dir="auto"
                autoFocus
              />
            </label>

            <label className="mt-3 block font-display text-xs font-extrabold text-ink">
              الفصل / الشعبة (اختياري)
              <input
                type="text"
                value={section}
                onChange={(event) => setSection(event.target.value)}
                placeholder="Section A · Fall 2026"
                maxLength={120}
                disabled={pending}
                className="mt-1 w-full rounded-xl border-2 border-surface-border bg-surface px-3 py-2.5 font-body text-sm outline-none focus:border-feather-green disabled:opacity-60"
                dir="auto"
              />
            </label>

            <label className="mt-3 block font-display text-xs font-extrabold text-ink">
              الوصف (اختياري)
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="نبذة قصيرة عن المقرر…"
                maxLength={500}
                rows={3}
                disabled={pending}
                className="mt-1 w-full resize-none rounded-xl border-2 border-surface-border bg-surface px-3 py-2.5 font-body text-sm outline-none focus:border-feather-green disabled:opacity-60"
                dir="auto"
              />
            </label>

            {error && (
              <p className="mt-2 rounded-xl bg-feather-red/10 px-3 py-2 text-center font-body text-xs text-feather-red-dark">
                {error}
              </p>
            )}

            <button
              type="button"
              onClick={() => void submit()}
              disabled={pending}
              className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] active:border-b-0 disabled:opacity-50"
            >
              {pending ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
                  جارٍ الإنشاء…
                </>
              ) : (
                <>
                  <Check className="h-5 w-5" aria-hidden="true" />
                  إنشاء الفصل
                </>
              )}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
