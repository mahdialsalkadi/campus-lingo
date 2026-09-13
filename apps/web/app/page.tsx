"use client";

import {
  BookOpen,
  CheckCircle2,
  Flame,
  Gem,
  Heart,
  Inbox,
  Loader2,
  Lock,
  PlayCircle,
  Sparkles,
  Trash2,
  UploadCloud,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  CHALLENGE_LABELS,
  DEFAULT_MODEL,
  api,
  uploadLecture,
  type CourseSummary,
  type NodeSummary,
  type ProcessLectureResponse,
} from "@/lib/api";
import { HEART_REGEN_MS, MAX_HEARTS, useQuizStore } from "@/stores/quizStore";

const LEAGUE_NAMES = [
  "Bronze",
  "Silver",
  "Gold",
  "Sapphire",
  "Ruby",
  "Emerald",
  "Amethyst",
  "Pearl",
  "Obsidian",
];

type Stage = "idle" | "uploading" | "generating" | "saving" | "done" | "error";

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

export default function DashboardPage() {
  const userId = useQuizStore((state) => state.userId);
  const email = useQuizStore((state) => state.email);
  const hearts = useQuizStore((state) => state.hearts);
  const heartsUpdatedAt = useQuizStore((state) => state.heartsUpdatedAt);
  const gems = useQuizStore((state) => state.gems);
  const xp = useQuizStore((state) => state.xp);
  const streak = useQuizStore((state) => state.streak);
  const leagueTier = useQuizStore((state) => state.leagueTier);
  const soundEnabled = useQuizStore((state) => state.soundEnabled);
  const completedNodeIds = useQuizStore((state) => state.completedNodeIds);
  const mistakes = useQuizStore((state) => state.mistakes);

  const ensureUser = useQuizStore((state) => state.ensureUser);
  const refreshStats = useQuizStore((state) => state.refreshStats);
  const refreshHearts = useQuizStore((state) => state.refreshHearts);
  const refillHearts = useQuizStore((state) => state.refillHearts);
  const toggleSound = useQuizStore((state) => state.toggleSound);
  const loadMistakes = useQuizStore((state) => state.loadMistakes);

  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [selectedCourseId, setSelectedCourseId] = useState<string | null>(null);
  const [nodes, setNodes] = useState<NodeSummary[]>([]);
  const [newCourseName, setNewCourseName] = useState("");
  const [stage, setStage] = useState<Stage>("idle");
  const [dragging, setDragging] = useState(false);
  const [result, setResult] = useState<ProcessLectureResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<{ cards_due: number; cards_total: number; accuracy_last_50: number; cards_relearning: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const fileInput = useRef<HTMLInputElement | null>(null);

  // Login dialog state
  const [showLogin, setShowLogin] = useState(false);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const loadCourses = useCallback(async (id: string) => {
    const list = await api.listCourses(id);
    setCourses(list);
    if (list.length > 0) setSelectedCourseId((prev) => prev ?? list[0].id);
    return list;
  }, []);

  const loadNodes = useCallback(async (courseId: string) => {
    const detail = await api.courseNodes(courseId);
    setNodes(detail.nodes);
  }, []);

  // Check if user has an existing session (persisted email+userId in localStorage via zustand)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!userId || !email) {
        // No session — show login dialog
        if (!cancelled) setShowLogin(true);
        return;
      }
      try {
        const id = await ensureUser(email);
        if (cancelled) return;
        const [userStats] = await Promise.all([api.stats(id), loadCourses(id)]);
        await loadMistakes();
        if (cancelled) return;
        setStats(userStats);
      } catch (err) {
        if (!cancelled) setError(errorText(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId, email, ensureUser, loadCourses, loadMistakes]);

  useEffect(() => {
    if (!selectedCourseId) return;
    let cancelled = false;
    (async () => {
      try {
        await loadNodes(selectedCourseId);
      } catch (err) {
        if (!cancelled) setError(errorText(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedCourseId, loadNodes]);

  useEffect(() => {
    const tick = window.setInterval(() => {
      setNow(Date.now());
      refreshHearts();
    }, 1000);
    const sync = window.setInterval(() => void refreshStats(), 60_000);
    return () => {
      window.clearInterval(tick);
      window.clearInterval(sync);
    };
  }, [refreshHearts, refreshStats]);

  const nextHeartIn = useMemo(() => {
    if (hearts >= MAX_HEARTS) return null;
    const remaining = heartsUpdatedAt + HEART_REGEN_MS - now;
    if (remaining <= 0) return "now";
    return `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000)
      .toString()
      .padStart(2, "0")}s`;
  }, [hearts, heartsUpdatedAt, now]);

  /** Unlocked = first node or the previous node is completed. */
  const nodeStatuses = useMemo(() => {
    return nodes.map((node, index) => {
      if (completedNodeIds.includes(node.id)) return "completed" as const;
      if (index === 0) return "unlocked" as const;
      const previous = nodes[index - 1];
      return completedNodeIds.includes(previous.id) ? ("unlocked" as const) : ("locked" as const);
    });
  }, [nodes, completedNodeIds]);

  // --- Login handler ---
  const handleLogin = async () => {
    const trimmed = loginEmail.trim().toLowerCase();
    if (!trimmed || !trimmed.includes("@")) {
      setLoginError("أدخل بريد إلكتروني صحيح");
      return;
    }
    setLoginLoading(true);
    setLoginError(null);
    try {
      const id = await ensureUser(trimmed);
      setShowLogin(false);
      setLoginEmail("");
      const [userStats] = await Promise.all([api.stats(id), loadCourses(id)]);
      await loadMistakes();
      setStats(userStats);
    } catch (err) {
      setLoginError(errorText(err));
    } finally {
      setLoginLoading(false);
    }
  };

  // --- Delete handlers ---
  const deleteCourse = async (courseId: string, courseName: string) => {
    if (!window.confirm(`هل أنت متأكد من حذف المقرر "${courseName}" وجميع محاضراته؟`)) return;
    try {
      await api.deleteCourse(courseId);
      const id = userId ?? (await ensureUser());
      await loadCourses(id);
      if (selectedCourseId === courseId) {
        setSelectedCourseId(null);
        setNodes([]);
      }
    } catch (err) {
      setError(errorText(err));
    }
  };

  const deleteLecture = async (lectureId: string, lectureTitle: string) => {
    if (!window.confirm(`هل أنت متأكد من حذف المحاضرة "${lectureTitle}"؟`)) return;
    try {
      await api.deleteLecture(lectureId);
      if (selectedCourseId) {
        await loadNodes(selectedCourseId);
      }
      const id = userId ?? (await ensureUser());
      await loadCourses(id);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const createCourse = async () => {
    const name = newCourseName.trim();
    const id = userId ?? (await ensureUser());
    if (!name) {
      setError("اكتب اسم المقرر أولاً");
      return;
    }
    try {
      const course = await api.createCourse(id, name);
      setNewCourseName("");
      await loadCourses(id);
      setSelectedCourseId(course.id);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const processFile = async (file: File) => {
    const id = userId ?? (await ensureUser());
    if (!selectedCourseId) {
      setError("أنشئ مقرراً (Course) أولاً ثم ارفع المحاضرة");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("الملف يجب أن يكون PDF");
      return;
    }
    setError(null);
    setResult(null);
    setStage("uploading");

    const form = new FormData();
    form.append("file", file);
    form.append("course_id", selectedCourseId);
    form.append("model_name", DEFAULT_MODEL);
    form.append("max_nodes", "25");
    form.append("generate", "true");

    const generatingTimer = window.setTimeout(() => setStage("generating"), 1500);
    const savingTimer = window.setTimeout(() => setStage("saving"), 8000);
    try {
      const response = await uploadLecture(form);
      setResult(response);
      setStage("done");
      await loadCourses(id);
      await loadNodes(selectedCourseId);
      await refreshStats();
      await loadMistakes();
      setStats(await api.stats(id));
    } catch (err) {
      setError(errorText(err));
      setStage("error");
    } finally {
      window.clearTimeout(generatingTimer);
      window.clearTimeout(savingTimer);
    }
  };

  const onDrop = (event: React.DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void processFile(file);
  };

  const stageLabel: Record<Stage, string> = {
    idle: "اسحب ملف المحاضرة هنا أو اضغط للاختيار (PDF)",
    uploading: "جارٍ رفع الملف…",
    generating: "Qwen يبني التحديات من نص المحاضرة…",
    saving: "حفظ الوحدات والبطاقات في PostgreSQL…",
    done: "تم! الوحدة جاهزة للتدريب",
    error: "فشل المعالجة",
  };

  const busy = stage === "uploading" || stage === "generating" || stage === "saving";

  // --- Login dialog ---
  if (showLogin) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
          <h1 className="font-display text-2xl font-extrabold text-ink text-center">
            CampusLingo
          </h1>
          <p className="mt-2 font-body text-sm text-ink-soft text-center">
            أدخل بريدك الإلكتروني للمتابعة — بياناتك ومiclassاتك تُحفظ تلقائياً.
          </p>
          {loginError && (
            <p className="mt-2 rounded-xl bg-feather-red/10 px-3 py-2 font-body text-xs text-feather-red-dark text-center">
              {loginError}
            </p>
          )}
          <input
            type="email"
            value={loginEmail}
            onChange={(e) => setLoginEmail(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void handleLogin(); }}
            placeholder="you@university.edu"
            className="mt-4 w-full rounded-xl border-2 border-surface-border bg-surface px-4 py-3 font-body text-sm outline-none focus:border-feather-blue text-center"
            autoFocus
          />
          <button
            type="button"
            onClick={() => void handleLogin()}
            disabled={loginLoading}
            className="mt-3 w-full rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] disabled:opacity-50"
          >
            {loginLoading ? (
              <Loader2 className="mx-auto h-5 w-5 animate-spin" />
            ) : (
              "دخول"
            )}
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col gap-6 px-4 py-6 sm:px-6">
      {/* Top bar: streak / hearts / XP -------------------------------------- */}
      <header className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border-2 border-surface-border bg-surface px-4 py-3 shadow-card">
        <div className="flex items-center gap-2">
          <span className="font-display text-xl font-extrabold text-feather-green">CampusLingo</span>
          <span className="hidden font-body text-xs text-ink-faint sm:inline">{email}</span>
        </div>
        <div className="flex flex-wrap items-center gap-3 font-display text-sm font-bold">
          <span className="flex items-center gap-1 text-feather-orange" title="Streak">
            <Flame className="h-4 w-4" aria-hidden="true" /> {streak}
          </span>
          <span className="flex items-center gap-1 text-feather-red" title="Hearts">
            <Heart className="h-4 w-4 fill-feather-red" aria-hidden="true" /> {hearts}/{MAX_HEARTS}
          </span>
          <span className="flex items-center gap-1 text-feather-purple" title="XP">
            <Zap className="h-4 w-4" aria-hidden="true" /> {xp}
          </span>
          <span className="flex items-center gap-1 text-feather-blue" title="Gems">
            <Gem className="h-4 w-4" aria-hidden="true" /> {gems}
          </span>
          <span className="hidden font-body text-xs font-normal text-ink-faint sm:inline">
            {LEAGUE_NAMES[leagueTier] ?? "Bronze"} league
          </span>
          <button
            type="button"
            onClick={toggleSound}
            className="rounded-xl border-2 border-surface-border px-2 py-1 text-xs"
            title={soundEnabled ? "الصوت مفعّل" : "الصوت مكتوم"}
          >
            {soundEnabled ? "🔊" : "🔇"}
          </button>
        </div>
      </header>

      {nextHeartIn && (
        <p className="-mt-3 font-body text-xs text-ink-faint">
          قلب إضافي خلال {nextHeartIn} · أو أعد التعبئة بـ 350 💎
        </p>
      )}

      {error && (
        <div className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-3">
          <p className="font-body text-sm text-feather-red-dark">{error}</p>
          <button
            type="button"
            onClick={() => setError(null)}
            className="mt-1 font-display text-xs font-bold uppercase text-ink-soft"
          >
            إخفاء
          </button>
        </div>
      )}

      {/* Upload zone ------------------------------------------------------- */}
      <section className="grid gap-4 lg:grid-cols-[1.3fr_1fr]">
        <div className="flex flex-col gap-3 rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h1 className="font-display text-2xl font-extrabold text-ink">
                حوّل محاضراتك إلى تدريب يومي
              </h1>
              <p className="mt-1 font-body text-sm text-ink-soft">
                ارفع شرائح المحاضرة (PDF) — نستخرج النص، نبني وحدة تعلّم بتحديات تفاعلية،
                ونوزّعها على جدول FSRS-4.5.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <select
              value={selectedCourseId ?? ""}
              onChange={(event) => setSelectedCourseId(event.target.value || null)}
              className="rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-body text-sm"
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
              placeholder="اسم مقرر جديد"
              className="rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-body text-sm outline-none focus:border-feather-blue"
            />
            <button
              type="button"
              onClick={createCourse}
              className="rounded-xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key active:translate-y-[2px]"
            >
              إنشاء مقرر
            </button>
            {selectedCourseId && (
              <button
                type="button"
                onClick={() => {
                  const c = courses.find((c) => c.id === selectedCourseId);
                  void deleteCourse(selectedCourseId, c?.name ?? "");
                }}
                className="rounded-xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-3 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key active:translate-y-[2px]"
                title="حذف المقرر"
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>

          <label
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-3xl border-4 border-dashed px-6 py-10 text-center transition-colors ${
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
                if (file) void processFile(file);
              }}
            />
            {busy ? (
              <Loader2 className="h-9 w-9 animate-spin text-feather-blue" aria-hidden="true" />
            ) : (
              <UploadCloud className="h-9 w-9 text-feather-green" aria-hidden="true" />
            )}
            <p className="font-display text-sm font-extrabold text-ink">{stageLabel[stage]}</p>
            <p className="font-body text-xs text-ink-faint">
              PyMuPDF للاستخراج · Qwen عبر Ollama · PostgreSQL + pgvector للحفظ
            </p>
            {!busy && (
              <span className="mt-1 rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key">
                اختر ملف PDF
              </span>
            )}
          </label>

          {result && (
            <div className="animate-pop rounded-2xl border-2 border-feather-green bg-feather-green/10 p-4">
              <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
                <Sparkles className="h-4 w-4 text-feather-green" aria-hidden="true" />
                تم إنشاء الوحدة التعليمية
              </p>
              <p className="mt-1 font-body text-xs text-ink-soft">
                {result.cards_seeded} بطاقة FSRS · {Object.values(result.challenges_by_type).reduce((a, b) => a + b, 0)} تحدٍّ ·
                {" "}{result.embeddings_stored} embedding · {(result.elapsed_ms / 1000).toFixed(1)}s
              </p>
              <ul className="mt-2 flex flex-wrap gap-2">
                {Object.entries(result.challenges_by_type).map(([type, count]) => (
                  <li
                    key={type}
                    className="rounded-lg bg-surface px-2 py-1 font-body text-[11px] text-ink-soft"
                  >
                    {(CHALLENGE_LABELS[type as keyof typeof CHALLENGE_LABELS]?.ar ?? type)}: {count}
                  </li>
                ))}
              </ul>
              {result.generation_errors.length > 0 && (
                <details className="mt-2">
                  <summary className="font-body text-xs text-feather-orange">
                    {result.generation_errors.length} تحذير من التوليد
                  </summary>
                  <ul className="mt-1 flex flex-col gap-1">
                    {result.generation_errors.slice(0, 8).map((warning, index) => (
                      <li key={index} className="font-body text-[11px] text-ink-faint">
                        {warning}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
        </div>

        {/* Mistakes Inbox -------------------------------------------------- */}
        <aside className="flex flex-col gap-3 rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-2 font-display text-base font-extrabold text-ink">
              <Inbox className="h-5 w-5 text-feather-red" aria-hidden="true" />
              Mistakes Inbox
            </h2>
            <button
              type="button"
              onClick={() => void loadMistakes()}
              className="font-display text-[11px] font-bold uppercase text-feather-blue"
            >
              تحديث
            </button>
          </div>
          <p className="font-body text-xs text-ink-faint">
            كل بطاقة أخطأت فيها تعود هنا حتى تتقنها مرتين متتاليتين.
          </p>
          {mistakes.length === 0 ? (
            <p className="rounded-2xl border-2 border-dashed border-surface-border p-3 font-body text-xs text-ink-soft">
              لا أخطاء محفوظة — أداء رائع حتى الآن.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {mistakes.slice(0, 6).map((item) => (
                <li key={item.card_id} className="rounded-2xl border-2 border-feather-red/40 p-2">
                  <p className="font-body text-sm font-bold text-ink" dir="auto">
                    {CHALLENGE_LABELS[item.type]?.ar ?? item.type}
                  </p>
                  <p className="font-body text-[11px] text-ink-faint" dir="auto">
                    {item.node_title} · {item.lapses} خطأ · استقرار {item.stability.toFixed(2)} ·
                    {" "}مستحقة {new Date(item.due_date).toLocaleString()}
                  </p>
                  <Link
                    href={`/play/${item.node_id}`}
                    className="mt-1 inline-block rounded-lg border-2 border-b-4 border-feather-red-dark bg-feather-red px-3 py-1 font-display text-[11px] font-extrabold uppercase text-white"
                  >
                    أعد المحاولة
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-auto grid grid-cols-2 gap-2">
            <div className="rounded-2xl border-2 border-surface-border p-2">
              <p className="font-body text-[11px] uppercase text-ink-faint">بطاقات مستحقة</p>
              <p className="font-display text-lg font-extrabold text-feather-green">
                {stats?.cards_due ?? 0}
              </p>
            </div>
            <div className="rounded-2xl border-2 border-surface-border p-2">
              <p className="font-body text-[11px] uppercase text-ink-faint">دقة آخر 50</p>
              <p className="font-display text-lg font-extrabold text-feather-purple">
                {Math.round((stats?.accuracy_last_50 ?? 0) * 100)}%
              </p>
            </div>
          </div>
          <Link
            href="/play/review"
            className={`flex items-center justify-center gap-2 rounded-2xl border-2 border-b-4 px-4 py-3 font-display text-sm font-extrabold uppercase ${
              hearts > 0
                ? "border-feather-green-dark bg-feather-green text-white shadow-key"
                : "pointer-events-none border-surface-border bg-surface-muted text-ink-faint"
            }`}
          >
            <PlayCircle className="h-4 w-4" aria-hidden="true" />
            جلسة المراجعة اليومية ({(stats?.cards_due ?? 0)} بطاقة)
          </Link>
          {hearts <= 0 && (
            <button
              type="button"
              onClick={() => void refillHearts()}
              className="rounded-2xl border-2 border-b-4 border-feather-orange bg-feather-yellow px-4 py-2 font-display text-xs font-extrabold uppercase text-ink"
            >
              إعادة تعبئة القلوب — 350 💎
            </button>
          )}
        </aside>
      </section>

      {/* Node pathway map -------------------------------------------------- */}
      <section className="flex flex-col gap-3 pb-8">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-xl font-extrabold text-ink">
            <BookOpen className="h-5 w-5 text-feather-blue" aria-hidden="true" />
            خريطة الوحدات
          </h2>
          <span className="font-body text-xs text-ink-faint">
            {completedNodeIds.length}/{nodes.length} مكتملة
          </span>
        </div>

        {nodes.length === 0 ? (
          <p className="rounded-2xl border-2 border-dashed border-surface-border bg-surface p-4 font-body text-sm text-ink-soft">
            لا توجد وحدات بعد — ارفع محاضرة PDF لبناء المسار التعليمي.
          </p>
        ) : (
          <ol className="flex flex-col gap-2">
            {nodes.map((node, index) => {
              const status = nodeStatuses[index];
              const locked = status === "locked";
              return (
                <li
                  key={node.id}
                  className={`flex flex-wrap items-center justify-between gap-3 rounded-2xl border-2 p-3 shadow-card ${
                    status === "completed"
                      ? "border-feather-green bg-feather-green/5"
                      : locked
                        ? "border-surface-border bg-surface-muted opacity-70"
                        : "border-feather-blue/50 bg-surface"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <span
                      className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-2 font-display text-sm font-extrabold ${
                        status === "completed"
                          ? "border-feather-green bg-feather-green text-white"
                          : locked
                            ? "border-surface-border bg-surface text-ink-faint"
                            : "border-feather-blue bg-feather-blue text-white"
                      }`}
                    >
                      {status === "completed" ? (
                        <CheckCircle2 className="h-5 w-5" aria-hidden="true" />
                      ) : locked ? (
                        <Lock className="h-4 w-4" aria-hidden="true" />
                      ) : (
                        index + 1
                      )}
                    </span>
                    <div className="min-w-0">
                      <p className="truncate font-body text-sm font-bold text-ink" dir="auto">
                        {node.title}
                      </p>
                      <p className="truncate font-body text-[11px] text-ink-faint" dir="auto">
                        {node.lecture_title} · {node.challenge_count} تحديات ·{" "}
                        {status === "completed" ? "مكتملة" : locked ? "مقفلة" : "متاحة"}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Link
                      href={locked ? "#" : `/play/${node.id}`}
                      aria-disabled={locked}
                      className={`rounded-xl border-2 border-b-4 px-4 py-2 font-display text-xs font-extrabold uppercase ${
                        locked
                          ? "pointer-events-none border-surface-border bg-surface-muted text-ink-faint"
                          : "border-feather-green-dark bg-feather-green text-white shadow-key"
                      }`}
                    >
                      {status === "completed" ? "أعد التدريب" : "ابدأ"}
                    </Link>
                    <button
                      type="button"
                      onClick={() => void deleteLecture(node.lecture_id, node.lecture_title)}
                      className="rounded-xl border-2 border-surface-border bg-surface px-2 py-2 text-feather-red hover:bg-feather-red/10"
                      title="حذف المحاضرة"
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        )}

        <p className="font-body text-xs text-ink-faint">
          كل التحديات مسجّلة في جدول FSRS-4.5: {stats?.cards_total ?? 0} بطاقة إجمالاً ·{" "}
          {stats?.cards_due ?? 0} مستحقة الآن · {stats?.cards_relearning ?? 0} قيد إعادة التعلّم
        </p>
      </section>
    </main>
  );
}
