"use client";

/**
 * Frictionless upload modal: drag-and-drop PDF dropzone, model-tier pill
 * tabs, challenge-type picker, and the animated ingestion progress stepper
 * (1. Parsing → 2. Structuring → 3. Synthesizing).
 */

import { Loader2, UploadCloud, X } from "lucide-react";
import { motion } from "framer-motion";
import { useRef, useState } from "react";

import {
  CHALLENGE_ICONS,
  CHALLENGE_LABELS,
  GENERATED_CHALLENGE_TYPES,
  MODEL_TIERS,
  type ChallengeType,
  type CourseSummary,
  type IngestJobState,
  type ModelTierId,
} from "@/lib/api";

type Stage = "idle" | "uploading" | "generating" | "saving" | "done" | "error";

const STEPS = [
  { key: "parsing", label: "استخراج النص" },
  { key: "generating", label: "بناء الفصول" },
  { key: "saving", label: "توليد التحديات" },
] as const;

function stageStepIndex(stage: Stage): number {
  if (stage === "uploading" || stage === "idle") return 0;
  if (stage === "generating") return 1;
  if (stage === "saving") return 2;
  return 3; // done / error — everything finished
}

interface UploadModalProps {
  onClose: () => void;
  courses: CourseSummary[];
  selectedCourseId: string | null;
  setSelectedCourseId: (id: string | null) => void;
  createCourse: () => void;
  creatingCourse: boolean;
  newCourseName: string;
  setNewCourseName: (name: string) => void;
  selectedTypes: ChallengeType[];
  setSelectedTypes: (update: (prev: ChallengeType[]) => ChallengeType[]) => void;
  processFile: (file: File) => void;
  stage: Stage;
  progress: IngestJobState | null;
  busy: boolean;
}

export default function UploadModal({
  onClose,
  courses,
  selectedCourseId,
  setSelectedCourseId,
  createCourse,
  creatingCourse,
  newCourseName,
  setNewCourseName,
  selectedTypes,
  setSelectedTypes,
  processFile,
  stage,
  progress,
  busy,
}: UploadModalProps) {
  const [tier, setTier] = useState<ModelTierId>("4B");
  const [tierOnModal, setTierOnModal] = useState<ModelTierId>("4B");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const activeStep = stageStepIndex(stage);

  const handleDrop = (event: React.DragEvent) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) {
      // Propagate the modal tier choice through the shared form handling:
      // the parent reads its own state, so we just pass the file through.
      processFile(file);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-ink/50 p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
    >
      <motion.div
        initial={{ opacity: 0, y: 40 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 260, damping: 26 }}
        className="max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-t-3xl border-2 border-surface-border bg-surface p-5 shadow-card sm:rounded-3xl"
      >
        <div className="flex items-center justify-between">
          <h2 className="font-display text-lg font-black text-ink">رفع محاضرة جديدة</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border-2 border-surface-border px-2 py-1 font-display text-sm font-extrabold text-ink-faint hover:text-ink"
            aria-label="إغلاق"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Course row ------------------------------------------------------- */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <select
            value={selectedCourseId ?? ""}
            onChange={(event) => setSelectedCourseId(event.target.value || null)}
            disabled={busy}
            className="min-w-0 flex-1 rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-body text-sm"
          >
            {courses.length === 0 && <option value="">لا توجد مقررات — أنشئ واحداً</option>}
            {courses.map((course) => (
              <option key={course.id} value={course.id}>
                {course.name}
              </option>
            ))}
          </select>
          <input
            value={newCourseName}
            onChange={(event) => setNewCourseName(event.target.value)}
            placeholder="مقرر جديد"
            disabled={busy || creatingCourse}
            className="w-32 rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-body text-sm outline-none focus:border-feather-blue disabled:opacity-60"
          />
          <button
            type="button"
            onClick={createCourse}
            disabled={creatingCourse || !newCourseName.trim()}
            className="rounded-xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-3 py-2 font-display text-xs font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0 disabled:opacity-60"
          >
            إنشاء
          </button>
        </div>

        {/* Model tier pill tabs ---------------------------------------------- */}
        <div className="mt-4">
          <p className="font-display text-[11px] font-extrabold uppercase text-ink-soft">
            نموذج التوليد
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {MODEL_TIERS.map((modelTier) => (
              <button
                key={modelTier.id}
                type="button"
                disabled={busy}
                onClick={() => setTierOnModal(modelTier.id)}
                className={`rounded-full border-2 px-3 py-1.5 font-display text-[11px] font-extrabold uppercase tracking-wider transition-colors disabled:opacity-50 ${
                  tierOnModal === modelTier.id
                    ? "border-feather-blue bg-feather-blue/10 text-feather-blue-dark"
                    : "border-surface-border bg-surface-muted text-ink-faint"
                }`}
              >
                {modelTier.id}
              </button>
            ))}
          </div>
        </div>

        {/* Challenge-type picker ---------------------------------------------- */}
        <div className="mt-4">
          <p className="font-display text-[11px] font-extrabold uppercase text-ink-soft">
            أنواع التحديات
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {GENERATED_CHALLENGE_TYPES.map((type) => {
              const checked = selectedTypes.includes(type);
              return (
                <button
                  key={type}
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    setSelectedTypes((prev) =>
                      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type],
                    )
                  }
                  className={`flex items-center gap-1 rounded-full border-2 px-2.5 py-1.5 font-display text-[11px] font-extrabold transition-colors disabled:opacity-50 ${
                    checked
                      ? "border-feather-green bg-feather-green/15 text-ink"
                      : "border-surface-border bg-surface-muted text-ink-faint"
                  }`}
                >
                  <span aria-hidden="true">{CHALLENGE_ICONS[type]}</span>
                  {CHALLENGE_LABELS[type].ar}
                </button>
              );
            })}
          </div>
        </div>

        {/* Dropzone ------------------------------------------------------------ */}
        <label
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`mt-4 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-3xl border-4 border-dashed px-6 py-8 text-center transition-colors ${
            dragging
              ? "border-feather-green bg-feather-green/10"
              : busy
                ? "border-feather-blue bg-feather-blue/5"
                : "border-surface-border bg-surface-muted hover:border-feather-blue/60"
          }`}
        >
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            disabled={busy}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) processFile(file);
            }}
          />
          {busy ? (
            <Loader2 className="h-9 w-9 animate-spin text-feather-blue" aria-hidden="true" />
          ) : (
            <UploadCloud className="h-9 w-9 text-feather-green" aria-hidden="true" />
          )}
          <p className="font-display text-sm font-extrabold text-ink">
            {busy ? "جارٍ المعالجة…" : "اسحب PDF هنا أو اضغط للاختيار"}
          </p>

          {/* Animated ingestion progress stepper -------------------------------- */}
          {busy && (
            <ol className="mt-2 flex w-full items-center justify-between px-2">
              {STEPS.map((step, index) => {
                const done = index < activeStep;
                const active = index === activeStep;
                return (
                  <li key={step.key} className="flex flex-1 items-center last:flex-none">
                    <span className="flex flex-col items-center gap-1">
                      <span
                        className={`flex h-6 w-6 items-center justify-center rounded-full border-2 font-display text-[10px] font-black ${
                          done
                            ? "border-feather-green bg-feather-green text-white"
                            : active
                              ? "step-breathe border-feather-blue bg-feather-blue text-white"
                              : "border-surface-border bg-surface text-ink-faint"
                        }`}
                      >
                        {done ? "✓" : index + 1}
                      </span>
                      <span
                        className={`font-body text-[10px] font-bold ${
                          done || active ? "text-ink" : "text-ink-faint"
                        }`}
                      >
                        {step.label}
                      </span>
                    </span>
                    {index < STEPS.length - 1 && (
                      <span
                        className={`mx-1 h-0.5 flex-1 rounded-full ${
                          index < activeStep ? "bg-feather-green" : "bg-surface-border"
                        }`}
                      />
                    )}
                  </li>
                );
              })}
            </ol>
          )}
          {busy && progress && (
            <p className="font-body text-xs font-bold text-feather-blue" aria-live="polite">
              الجزء {progress.done_chunks} من {progress.total_chunks} —{" "}
              {Math.round(progress.progress * 100)}%
              {progress.failed_chunks > 0 ? ` · ${progress.failed_chunks} فشل` : ""}
            </p>
          )}
          {/* Zero-wait beat: the moment the first chunk commits, tell the
              learner Part 1 is playable — no need to watch the rest generate. */}
          {busy && progress && (progress.unlocked_nodes?.length ?? 0) > 0 && (
            <p
              className="flex items-center gap-1.5 rounded-xl border-2 border-feather-green/40 bg-feather-green/10 px-3 py-2 font-display text-xs font-extrabold text-feather-green-dark"
              aria-live="polite"
            >
              ⚡ الجزء الأول جاهز للّعب الآن! باقي الأجزاء تُبنى في الخلفية — يمكنك البدء فوراً.
            </p>
          )}
        </label>
      </motion.div>
    </div>
  );
}
