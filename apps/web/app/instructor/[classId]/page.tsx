"use client";

/**
 * /instructor/[classId] — the instructor analytics dashboard.
 *
 * Three panels, all read-only and instructor-guarded server-side:
 *   1. Class roster — enrolled students with completion %, accuracy and
 *      last-active, refreshed live over the classroom SSE stream.
 *   2. Lecture insights — per-chapter accuracy plus the top struggling concepts.
 *   3. Export — one-click CSV of the roster's scores and milestones.
 */

import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Download,
  RefreshCw,
  ShieldCheck,
  Users,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  type ClassroomInsights,
  type ClassroomRoster,
  type ClassroomRosterEntry,
} from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatWhen(value: string | null): string {
  if (!value) return "لم يبدأ بعد";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "الآن";
  if (minutes < 60) return `قبل ${minutes} دقيقة`;
  if (minutes < 60 * 24) return `قبل ${Math.round(minutes / 60)} ساعة`;
  return `قبل ${Math.round(minutes / (60 * 24))} يوم`;
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-border">
      <div
        className="h-full rounded-full bg-feather-green transition-[width] duration-500"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

export default function InstructorDashboard({ params }: { params: { classId: string } }) {
  const classId = params.classId;
  const userId = useQuizStore((state) => state.userId);

  const [roster, setRoster] = useState<ClassroomRoster | null>(null);
  const [insights, setInsights] = useState<ClassroomInsights | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [lastEvent, setLastEvent] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!userId) return;
    try {
      const [rosterBody, insightBody] = await Promise.all([
        api.classroomRoster(classId, userId),
        api.classroomInsights(classId, userId),
      ]);
      setRoster(rosterBody);
      setInsights(insightBody);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "تعذّر تحميل لوحة الفصل");
    } finally {
      setLoading(false);
    }
  }, [classId, userId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Live roster: the same SSE channel the students listen on updates the
  // instructor's view when somebody joins or new material is posted.
  useEffect(() => {
    if (!userId || typeof window === "undefined") return;
    const source = new EventSource(api.classroomUpdatesUrl(classId, userId));
    source.addEventListener("connected", () => setLive(true));
    const refresh = (event: MessageEvent) => {
      try {
        const payload = JSON.parse(event.data) as { type?: string; lecture_title?: string };
        setLastEvent(
          payload.type === "student_joined"
            ? "انضم طالب جديد للفصل"
            : payload.lecture_title
              ? `مادة جديدة: ${payload.lecture_title}`
              : "تحديث على الفصل",
        );
      } catch {
        setLastEvent("تحديث على الفصل");
      }
      void load();
    };
    source.addEventListener("lecture_added", refresh);
    source.addEventListener("student_joined", refresh);
    source.onerror = () => setLive(false);
    return () => source.close();
  }, [classId, userId, load]);

  const sortedRoster = useMemo<ClassroomRosterEntry[]>(
    () => roster?.roster ?? [],
    [roster],
  );

  if (!userId) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="font-display text-lg font-extrabold text-ink-soft">
          سجّل الدخول لعرض لوحة المدرّس.
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col gap-5 px-4 py-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="font-display text-2xl font-black text-ink">
            {roster?.name ?? "لوحة الفصل"}
          </h1>
          <p className="font-body text-xs text-ink-faint">
            {live ? "متزامن مباشر ●" : "غير متصل بالبث المباشر"}
            {roster?.enrollment_code ? ` · كود الفصل: ${roster.enrollment_code}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void load()}
            className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft active:translate-y-[2px] active:border-b-2"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            تحديث
          </button>
          <a
            href={api.classroomGradesCsvUrl(classId, userId)}
            className="flex items-center gap-1.5 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-3 py-2 font-display text-xs font-extrabold text-white active:translate-y-[2px] active:border-b-2"
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            تصدير الدرجات CSV
          </a>
        </div>
      </header>

      {lastEvent && (
        <p className="rounded-2xl border-2 border-feather-blue/40 bg-feather-blue/10 px-3 py-2 font-body text-xs font-bold text-feather-blue-dark">
          {lastEvent}
        </p>
      )}

      {error && (
        <p className="rounded-2xl border-2 border-feather-red/40 bg-feather-red/10 px-3 py-2 font-body text-xs font-bold text-feather-red-dark">
          {error}
        </p>
      )}

      {/* Roster ------------------------------------------------------------ */}
      <section className="rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <Users className="h-4 w-4 text-feather-green" aria-hidden="true" />
            قائمة الطلاب
          </h2>
          <span className="font-body text-[11px] text-ink-faint">
            {roster?.student_count ?? 0} طالب · {roster?.challenges_total ?? 0} تحدٍّ
          </span>
        </div>

        {loading ? (
          <p className="py-6 text-center font-body text-xs text-ink-faint">جارٍ التحميل…</p>
        ) : sortedRoster.length === 0 ? (
          <p className="py-6 text-center font-body text-xs text-ink-faint">
            لا طلاب بعد — شارك كود الفصل{" "}
            <span className="font-display font-black text-ink">
              {roster?.enrollment_code ?? ""}
            </span>
          </p>
        ) : (
          <ul className="flex flex-col divide-y-2 divide-surface-border">
            {sortedRoster.map((student) => (
              <li key={student.student_id} className="flex flex-col gap-2 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="truncate font-body text-sm font-bold text-ink" dir="ltr">
                    {student.email}
                  </span>
                  <span className="flex items-center gap-3 font-body text-[11px] text-ink-faint">
                    <span>{student.xp_points} XP</span>
                    <span>🔥 {student.streak_count}</span>
                    <span>{formatWhen(student.last_active_at)}</span>
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <ProgressBar value={student.completion_percent} />
                  <span className="shrink-0 font-display text-[11px] font-extrabold text-ink-soft">
                    {student.completion_percent}% · دقة {formatPercent(student.accuracy)}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Lecture insights --------------------------------------------------- */}
      <section className="rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <BarChart3 className="h-4 w-4 text-feather-purple" aria-hidden="true" />
            أداء المحاضرات
          </h2>
          <span className="font-body text-[11px] text-ink-faint">
            {insights?.attempts ?? 0} محاولة · دقة عامة {formatPercent(insights?.accuracy ?? 0)}
          </span>
        </div>

        {!insights || insights.lectures.length === 0 ? (
          <p className="py-6 text-center font-body text-xs text-ink-faint">
            لا بيانات بعد — تُحسب النتائج تلقائياً مع بدء تدريب الطلاب.
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {insights.lectures.map((lecture) => (
              <li
                key={lecture.lecture_id}
                className="rounded-2xl border-2 border-surface-border p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="truncate font-body text-sm font-bold text-ink" dir="auto">
                    {lecture.lecture_title}
                  </span>
                  <span
                    className={`rounded-lg px-2 py-0.5 font-display text-[10px] font-extrabold uppercase ${
                      lecture.accuracy < 0.6
                        ? "bg-feather-red/15 text-feather-red-dark"
                        : lecture.accuracy < 0.8
                          ? "bg-feather-yellow/20 text-ink"
                          : "bg-feather-green/15 text-feather-green-dark"
                    }`}
                  >
                    {formatPercent(lecture.accuracy)}
                  </span>
                </div>
                {lecture.top_struggling.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {lecture.top_struggling.map((concept) => (
                      <span
                        key={concept.concept}
                        className="flex items-center gap-1 rounded-lg bg-feather-red/10 px-2 py-0.5 font-body text-[10px] font-bold text-feather-red-dark"
                        dir="auto"
                      >
                        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                        {concept.concept} · {concept.failures}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <Link
          href="/"
          className="flex items-center gap-1.5 rounded-2xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold text-ink-soft"
        >
          <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          عودة إلى مساري
        </Link>
        <span className="flex items-center gap-1.5 font-body text-[11px] text-ink-faint">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          بيانات الطلاب مرئية للمدرّس فقط
        </span>
      </div>
    </main>
  );
}
