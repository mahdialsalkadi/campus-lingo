"use client";

/**
 * ChapterFolder — the header chrome of one collapsible chapter folder (one
 * uploaded PDF = one chapter) with the milestone's "Memory Health" visual
 * indicator:
 *
 * - The folder header keeps the collapse/expand + progress-bar behaviour from
 *   the dashboard; the node pathway itself is passed in as `children` so the
 *   dashboard keeps owning per-node actions (duel, offline, tutor).
 * - Next to the challenge count sits a mini "صحة الذاكرة" retention pill —
 *   green 90-100% (fresh), amber 60-89% (decaying) and red < 60% (critical) —
 *   driven by GET /api/lectures/{id}/memory-health.
 * - Clicking the pill launches a mini-review of only that chapter's expired
 *   cards via /review/warmup?lecture=<id> (the warm-up arena filters by
 *   lecture when the query param is present).
 */

import { CheckCircle2, ChevronDown, Download, Loader2, MoreVertical, ShieldCheck, Trash2, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { api, type LectureGroup, type LectureMemoryHealth } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

/** Retention pill styling per memory-health band (spec's green/amber/red). */
const BAND_STYLES: Record<
  LectureMemoryHealth["band"],
  { pill: string; title: string }
> = {
  green: {
    pill: "border-feather-green/50 bg-feather-green/10 text-feather-green-dark",
    title: "طازجة في الذاكرة (90-100%) — اضغط لمراجعة المستحقة من هذا الفصل",
  },
  amber: {
    pill: "border-feather-orange/60 bg-feather-orange/10 text-feather-orange",
    title: "الذاكرة تتلاشى — راجع قريباً (60-89%) — اضغط لمراجعة المستحقة من هذا الفصل",
  },
  red: {
    pill: "border-feather-red/60 bg-feather-red/10 text-feather-red",
    title: "تآكل حرج — يحتاج تثبيتاً فورياً (أقل من 60%) — اضغط لمراجعة المستحقة من هذا الفصل",
  },
};

/**
 * FolderMenu — the ⋮ options dropdown (offline download / delete), extracted
 * from the dashboard so the folder header chrome stays in one place.
 */
export interface FolderMenuProps {
  lectureId: string;
  lectureTitle: string;
  open: boolean;
  onToggleOpen: () => void;
  onClose: () => void;
  /** Offline download state: busy spinner / saved count. */
  offlineBusy: boolean;
  offlineSaved: number;
  onSaveOffline: () => void;
  onForgetOffline: () => void;
  onDelete: () => void;
}

export function FolderMenu({
  lectureId,
  lectureTitle,
  open,
  onToggleOpen,
  onClose,
  offlineBusy,
  offlineSaved,
  onSaveOffline,
  onForgetOffline,
  onDelete,
}: FolderMenuProps) {
  return (
    <div className="absolute left-3 top-3">
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onToggleOpen();
        }}
        className="rounded-xl border-2 border-surface-border bg-surface px-2 py-2 text-ink-soft hover:bg-surface-muted"
        title={`خيارات الفصل "${lectureTitle}"`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <MoreVertical className="h-4 w-4" aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute left-0 top-full z-20 mt-1 w-44 overflow-hidden rounded-xl border-2 border-surface-border bg-surface shadow-card"
        >
          <button
            type="button"
            role="menuitem"
            disabled={offlineBusy}
            onClick={(event) => {
              event.stopPropagation();
              onSaveOffline();
            }}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-start font-body text-xs font-bold text-ink-soft hover:bg-surface-muted disabled:opacity-50"
          >
            {offlineSaved > 0 ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-feather-green" aria-hidden="true" />
            ) : (
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {offlineBusy
              ? "جاري التحميل..."
              : offlineSaved > 0
                ? `محفوظة (${offlineSaved})`
                : "تحميل بدون اتصال"}
          </button>
          {offlineSaved > 0 ? (
            <button
              type="button"
              role="menuitem"
              onClick={(event) => {
                event.stopPropagation();
                onForgetOffline();
              }}
              className="flex w-full items-center gap-2 px-3 py-2.5 text-start font-body text-xs font-bold text-ink-soft hover:bg-surface-muted"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
              حذف النسخة المحلية
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            onClick={(event) => {
              event.stopPropagation();
              onClose();
              onDelete();
            }}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-start font-body text-xs font-bold text-feather-red hover:bg-feather-red/10"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            حذف المحاضرة
          </button>
        </div>
      )}
    </div>
  );
}

interface ChapterFolderProps {
  group: LectureGroup;
  /** Number of this folder's nodes the learner has completed. */
  doneCount: number;
  /** Collapse state is owned by the dashboard (survives course switches). */
  collapsed: boolean;
  onToggle: () => void;
  /** Folder options dropdown (offline download / delete), positioned by us. */
  menu?: React.ReactNode;
  /** The node pathway (<ol>), rendered below the header when expanded. */
  children: React.ReactNode;
}

export default function ChapterFolder({
  group,
  doneCount,
  collapsed,
  onToggle,
  menu,
  children,
}: ChapterFolderProps) {
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);

  const [health, setHealth] = useState<LectureMemoryHealth | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const allDone = group.nodes.length > 0 && doneCount === group.nodes.length;

  /** Fetch the retention meter once per mounted folder (signed-in users). */
  const loadHealth = useCallback(async () => {
    if (!userId) return;
    setHealthLoading(true);
    try {
      const body = await api.lectureMemoryHealth(group.lectureId, userId);
      setHealth(body);
    } catch {
      /* informational: the pill simply stays hidden when the API is down */
    } finally {
      setHealthLoading(false);
    }
  }, [group.lectureId, userId]);

  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  /** Memory Health pill click = mini-review of only this chapter's expired cards. */
  const startMiniReview = () => {
    router.push(
      `/review/warmup?lecture=${encodeURIComponent(group.lectureId)}&chapter=${encodeURIComponent(
        group.lectureTitle,
      )}`,
    );
  };

  const band = health?.band ?? null;
  const bandStyle = band ? BAND_STYLES[band] : null;
  const healthPercent = health ? Math.round(health.score * 100) : null;

  return (
    <div className="relative rounded-3xl border-2 border-surface-border bg-surface-muted/60">
      {/* Folder options dropdown slot (offline download / delete) ------------- */}
      {menu}

      {/* Chapter folder header — click to collapse/expand --------------------- */}
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center justify-between gap-2 py-3 pe-4 ps-16 text-start"
        aria-expanded={!collapsed}
      >
        <h3
          className="flex min-w-0 items-center gap-2 font-display text-sm font-extrabold text-ink"
          dir="auto"
        >
          <span className="text-base" aria-hidden="true">📁</span>
          <span className="truncate">{group.lectureTitle}</span>
          <ChevronDown
            className={`h-4 w-4 shrink-0 text-ink-faint transition-transform ${
              collapsed ? "-rotate-90" : ""
            }`}
            aria-hidden="true"
          />
        </h3>
        <span className="flex items-center gap-2 font-body text-[11px] text-ink-faint">
          {allDone && (
            <span className="rounded-lg bg-feather-green/15 px-2 py-0.5 font-display text-[10px] font-extrabold uppercase text-feather-green-dark">
              مكتمل ✓
            </span>
          )}
          {doneCount}/{group.nodes.length} مكتملة
          {/* --- Mini "صحة الذاكرة" retention pill (milestone §4) --- */}
          {healthLoading ? (
            <span className="flex items-center gap-1 text-ink-faint">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            </span>
          ) : (
            bandStyle &&
            health &&
            healthPercent !== null && (
              <span
                role="button"
                tabIndex={0}
                onClick={(event) => {
                  event.stopPropagation();
                  startMiniReview();
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    event.stopPropagation();
                    startMiniReview();
                  }
                }}
                className={`flex cursor-pointer items-center gap-1 rounded-lg border-2 px-2 py-0.5 font-display text-[10px] font-extrabold uppercase transition-transform hover:scale-105 ${bandStyle.pill}`}
                title={bandStyle.title}
                aria-label={`صحة الذاكرة ${healthPercent}% — ابدأ مراجعة مصغّرة لهذا الفصل`}
              >
                <ShieldCheck className="h-3 w-3" aria-hidden="true" />
                صحة الذاكرة {healthPercent}%
                {health.due > 0 && (
                  <span className="rounded-md bg-ink/10 px-1 py-0.5 font-body text-[9px]">
                    {health.due} مستحقة
                  </span>
                )}
              </span>
            )
          )}
        </span>
      </button>

      {/* Chapter progress bar: % of parts completed --------------------------- */}
      <div className="mx-4 mb-3 h-1.5 overflow-hidden rounded-full bg-surface-border">
        <div
          className="h-full rounded-full bg-feather-green transition-all duration-500"
          style={{
            width: `${group.nodes.length ? (doneCount / group.nodes.length) * 100 : 0}%`,
          }}
        />
      </div>

      {!collapsed && children}
    </div>
  );
}
