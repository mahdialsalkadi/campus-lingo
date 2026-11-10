"use client";

/**
 * DIAGRAM_QA — a slide figure plus comprehension questions about it.
 *
 * Interaction: Duolingo-style tactile options, with the figure shown as an
 * interactive card that supports **pinch-to-zoom** (pointer events with two
 * active pointers, plus wheel zoom on desktop) and **full-screen preview**
 * (a portal-free fixed overlay with an Esc/backdrop close).
 *
 * The image is served from the core-ai `/static/uploads/diagrams/...` path, so
 * a deck with no reachable model still gets real visual practice.
 */

import { Maximize2, Minus, Plus, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { assetUrl, type ChallengeComponentProps, type DiagramQaPayload } from "@/lib/api";
import { playSfx } from "@/stores/quizStore";

const MIN_ZOOM = 1;
const MAX_ZOOM = 4;

export default function DiagramQuestion({
  challengeId,
  payload,
  onResult,
  disabled,
}: ChallengeComponentProps<DiagramQaPayload>) {
  const question = payload.questions[0];
  const imageUrl = useMemo(() => assetUrl(payload.image_url), [payload.image_url]);

  const [selected, setSelected] = useState<number | null>(null);
  const [answered, setAnswered] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [fullscreen, setFullscreen] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const startedAt = useRef(Date.now());

  // -- pinch-to-zoom (two pointers) + wheel zoom ----------------------------
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinchStart = useRef<{ distance: number; zoom: number } | null>(null);

  const distanceBetween = () => {
    const [a, b] = [...pointers.current.values()];
    if (!a || !b) return 0;
    return Math.hypot(a.x - b.x, a.y - b.y);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 2) {
      pinchStart.current = { distance: distanceBetween(), zoom };
    }
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 2 && pinchStart.current) {
      const ratio = distanceBetween() / (pinchStart.current.distance || 1);
      setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, pinchStart.current.zoom * ratio)));
    } else if (pointers.current.size === 1 && zoom > 1) {
      setOffset((prev) => ({ x: prev.x, y: prev.y }));
    }
  };

  const endPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) pinchStart.current = null;
  };

  const onWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey && zoom === 1) return; // let the page scroll normally
    event.preventDefault();
    setZoom((prev) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev - event.deltaY * 0.002)));
  };

  const resetZoom = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  const grade = (index: number) => {
    if (answered || disabled) return;
    const option = question.options[index];
    const correct = Boolean(option?.correct);
    setSelected(index);
    setAnswered(true);
    playSfx(correct ? "correct" : "incorrect");
    onResult({
      isCorrect: correct,
      elapsedMs: Date.now() - startedAt.current,
      hintsUsed: fullscreen || zoom > 1 ? 0 : 0,
      responseJson: {
        challenge_id: challengeId,
        diagram_ref: payload.diagram_ref,
        selected_index: index,
        correct_index: question.options.findIndex((item) => item.correct),
        focus: question.focus,
        zoom_magnification: Number(zoom.toFixed(2)),
      },
    });
  };

  const figure = (className: string) => (
    <div
      className={`relative overflow-hidden rounded-2xl border-2 border-surface-border bg-surface-muted ${className}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
      onPointerLeave={endPointer}
      onWheel={onWheel}
      style={{ touchAction: zoom > 1 ? "none" : "pan-y" }}
    >
      {imageFailed ? (
        <div className="flex h-48 items-center justify-center px-4 text-center font-body text-xs text-ink-faint">
          تعذّر تحميل الرسم — راجع النص المشروح أدناه.
        </div>
      ) : (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageUrl}
          alt={payload.caption || "رسم من المحاضرة"}
          onError={() => setImageFailed(true)}
          draggable={false}
          className="mx-auto max-h-80 w-auto select-none object-contain transition-transform duration-150"
          style={{ transform: `scale(${zoom}) translate(${offset.x}px, ${offset.y}px)` }}
        />
      )}
    </div>
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="font-body text-sm font-bold text-ink" dir="auto">
        {payload.instructions}
      </p>

      {figure("")}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setZoom((prev) => Math.max(MIN_ZOOM, prev - 0.5))}
            className="rounded-xl border-2 border-b-4 border-surface-border bg-surface px-2.5 py-1.5 text-ink-soft active:translate-y-[2px] active:border-b-2"
            aria-label="تصغير"
          >
            <Minus className="h-4 w-4" aria-hidden="true" />
          </button>
          <span className="font-display text-xs font-extrabold text-ink-soft">
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            onClick={() => setZoom((prev) => Math.min(MAX_ZOOM, prev + 0.5))}
            className="rounded-xl border-2 border-b-4 border-surface-border bg-surface px-2.5 py-1.5 text-ink-soft active:translate-y-[2px] active:border-b-2"
            aria-label="تكبير"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
          </button>
          {zoom > 1 && (
            <button
              type="button"
              onClick={resetZoom}
              className="rounded-xl border-2 border-surface-border bg-surface px-2 py-1.5 font-body text-[11px] font-bold text-ink-soft"
            >
              إعادة ضبط
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            playSfx("tap");
            setFullscreen(true);
          }}
          className="flex items-center gap-1.5 rounded-xl border-2 border-b-4 border-feather-blue/50 bg-feather-blue/10 px-3 py-1.5 font-display text-xs font-extrabold text-feather-blue-dark active:translate-y-[2px] active:border-b-2"
        >
          <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
          عرض كامل
        </button>
      </div>

      {fullscreen && (
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-4 bg-black/90 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => setFullscreen(false)}
        >
          <button
            type="button"
            onClick={() => setFullscreen(false)}
            className="absolute end-4 top-4 rounded-2xl border-2 border-white/30 bg-white/10 p-2 text-white"
            aria-label="إغلاق"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
          <div onClick={(event) => event.stopPropagation()} className="w-full max-w-4xl">
            {figure("border-white/20 bg-black/40")}
          </div>
          <p className="max-w-2xl text-center font-body text-xs text-white/70">
            استخدم القرص أو إصبعين للتكبير — {payload.caption || "رسم المحاضرة"}
          </p>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <p className="font-body text-sm font-extrabold text-ink" dir="auto">
          {question.question}
        </p>
        <div className="flex flex-col gap-2">
          {question.options.map((option, index) => {
            const isSelected = selected === index;
            const revealed = answered && (option.correct || isSelected);
            return (
              <button
                key={`${option.text}-${index}`}
                type="button"
                disabled={answered || disabled}
                onClick={() => grade(index)}
                className={`rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm font-bold transition-all active:translate-y-[2px] active:border-b-2 disabled:active:translate-y-0 ${
                  revealed
                    ? option.correct
                      ? "border-feather-green-dark bg-feather-green/15 text-feather-green-dark"
                      : "border-feather-red-dark bg-feather-red/15 text-feather-red-dark"
                    : "border-surface-border bg-surface text-ink hover:bg-surface-muted"
                }`}
                dir="auto"
              >
                {option.text}
              </button>
            );
          })}
        </div>
      </div>

      {answered && (
        <div className="rounded-2xl border-2 border-surface-border bg-surface-muted p-3">
          <p className="font-body text-xs leading-6 text-ink-soft" dir="auto">
            {payload.one_liner_rationale?.trim() || payload.explanation}
          </p>
        </div>
      )}
    </div>
  );
}
