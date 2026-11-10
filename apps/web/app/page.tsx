"use client";

import {
  BookOpen,
  CheckCircle2,
  Download,
  Gem,
  GraduationCap,
  Heart,
  Inbox,
  Loader2,
  Lock,
  LogOut,
  PlayCircle,
  Plus,
  Share2,
  ShieldCheck,
  Sparkles,
  Star,
  Swords,
  Trash2,
  UploadCloud,
  X,
  Zap,
} from "lucide-react";
import { motion } from "framer-motion";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import AuthModal from "@/components/AuthModal";
import ChapterFolder, { FolderMenu } from "@/components/ChapterFolder";
import { ClassroomShareModal, CreateClassroomModal } from "@/components/ClassroomModals";
import {
  ImportCourseModal,
  ShareCourseModal,
} from "@/components/CourseShareModals";
import StreakCalendar from "@/components/StreakCalendar";
import ThemeToggle from "@/components/ThemeToggle";
import UploadModal from "@/components/UploadModal";
import WarmupBanner from "@/components/WarmupBanner";
import {
  ApiError,
  CHALLENGE_ICONS,
  CHALLENGE_LABELS,
  DEFAULT_MODEL,
  DEFAULT_MODEL_TIER,
  GENERATED_CHALLENGE_TYPES,
  MODEL_TIERS,
  api,
  awaitIngestion,
  getNodeChallenges,
  groupNodesByLecture,
  startIngestion,
  type ChallengeType,
  type ClassroomSummary,
  type CourseSummary,
  type IngestJobState,
  type LectureGroup,
  type ModelTierId,
  type NodeSummary,
  type ProcessLectureResponse,
} from "@/lib/api";
import { cacheNode, listCachedNodes, removeCachedNode } from "@/lib/offline";
import {
  isStaleIdentity,
  isValidUserId,
  playSfx,
  useQuizStore,
} from "@/stores/quizStore";

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

/** One-shot confetti burst — the "reward is variable" celebration beat. */
function ConfettiBurst({ pieces = 28 }: { pieces?: number }) {
  const confetti = useMemo(
    () =>
      Array.from({ length: pieces }, (_, index) => ({
        id: index,
        left: `${(index * 100) / pieces + Math.random() * 4}%`,
        drift: `${Math.round((Math.random() - 0.5) * 160)}px`,
        spin: `${Math.random() > 0.5 ? "" : "-"}${360 + Math.round(Math.random() * 720)}deg`,
        size: `${7 + Math.round(Math.random() * 7)}px`,
        delay: `${(Math.random() * 0.5).toFixed(2)}s`,
        duration: `${(2 + Math.random() * 1.4).toFixed(2)}s`,
        color: [
          "var(--cl-green)",
          "var(--cl-blue)",
          "var(--cl-yellow)",
          "var(--cl-orange)",
          "var(--cl-purple)",
          "var(--cl-red)",
        ][index % 6],
      })),
    [pieces],
  );
  return (
    <div aria-hidden="true" className="pointer-events-none fixed inset-0 z-50 overflow-hidden">
      {confetti.map((piece) => (
        <span
          key={piece.id}
          className="confetti-piece"
          style={{
            left: piece.left,
            "--confetti-color": piece.color,
            "--confetti-drift": piece.drift,
            "--confetti-spin": piece.spin,
            "--confetti-size": piece.size,
            "--confetti-delay": piece.delay,
            "--confetti-duration": piece.duration,
          } as React.CSSProperties}
        />
      ))}
    </div>
  );
}

/**
 * Pulsing placeholder shown under the chapter tree while an ingestion job is
 * still building parts 2..N. Part 1 is already playable by the time this
 * renders — this just communicates that more is on the way in the background.
 */
function ProgressiveIngestSkeleton({ progress }: { progress: IngestJobState | null }) {
  const done = progress?.done_chunks ?? 0;
  const total = progress?.total_chunks ?? 0;
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div
      className="animate-pulse rounded-3xl border-2 border-dashed border-feather-blue/50 bg-feather-blue/5 p-4"
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin text-feather-blue" aria-hidden="true" />
        <p className="font-display text-sm font-extrabold text-feather-blue-dark">
          جاري تجهيز بقية الأجزاء في الخلفية...
        </p>
      </div>
      <p className="mt-1 font-body text-xs text-ink-soft">
        {done > 0
          ? `الجزء الأول جاهز للّعب الآن — تم تجهيز ${done} من ${total}.`
          : `جارٍ تجهيز ${total} أجزاء…`}
      </p>
      <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-surface-border">
        <div
          className="h-full rounded-full bg-feather-blue transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

/**
 * In-folder skeleton NODE appended below the committed parts of the lecture
 * that is still generating. Shaped like a real pathway node (medallion + card)
 * so the tree reads as "Part N is coming" rather than a detached progress bar.
 * The committed parts above it stay fully clickable and playable.
 */
function PartPreparingNode({
  nextPart,
  done,
  total,
}: {
  nextPart: number;
  done: number;
  total: number;
}) {
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <li
      className="flex w-full items-center gap-3 sm:w-[calc(100%-2.5rem)]"
      role="status"
      aria-live="polite"
    >
      <span
        className="relative flex h-14 w-14 shrink-0 items-center justify-center rounded-full border-2 border-dashed border-feather-blue/60 bg-feather-blue/5 text-feather-blue"
        aria-hidden="true"
      >
        <Loader2 className="h-5 w-5 animate-spin" />
      </span>
      <div className="animate-pulse min-w-0 flex-1 rounded-2xl border-2 border-dashed border-feather-blue/50 bg-feather-blue/5 p-3">
        <p className="font-display text-sm font-extrabold text-feather-blue-dark" dir="auto">
          ⏳ جاري إعداد Part {nextPart} في الخلفية...
        </p>
        <p className="mt-0.5 font-body text-[11px] text-ink-soft">
          {done > 0
            ? `${done} من ${total} أجزاء جاهزة — العب الآن وستُفتح البقية تلقائياً.`
            : `جارٍ تجهيز ${total} أجزاء…`}
        </p>
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-border">
          <div
            className="h-full rounded-full bg-feather-blue transition-all"
            style={{ width: `${percent}%` }}
          />
        </div>
      </div>
    </li>
  );
}

export default function DashboardPage() {
  // --- identity (from the persisted store, verified server-side on boot) -----
  const userId = useQuizStore((state) => state.userId);
  const email = useQuizStore((state) => state.email);
  const gems = useQuizStore((state) => state.gems);
  const xp = useQuizStore((state) => state.xp);
  const streak = useQuizStore((state) => state.streak);
  const leagueTier = useQuizStore((state) => state.leagueTier);
  const soundEnabled = useQuizStore((state) => state.soundEnabled);
  const completedNodeIds = useQuizStore((state) => state.completedNodeIds);
  const mistakes = useQuizStore((state) => state.mistakes);

  const refreshStats = useQuizStore((state) => state.refreshStats);
  const logout = useQuizStore((state) => state.logout);
  const toggleSound = useQuizStore((state) => state.toggleSound);
  const loadMistakes = useQuizStore((state) => state.loadMistakes);

  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [selectedCourseId, setSelectedCourseId] = useState<string | null>(null);
  const [nodes, setNodes] = useState<NodeSummary[]>([]);
  const [nodesCourseId, setNodesCourseId] = useState<string | null>(null);
  const [nodesLoading, setNodesLoading] = useState(false);
  const [newCourseName, setNewCourseName] = useState("");
  const [creatingCourse, setCreatingCourse] = useState(false);
  const [stage, setStage] = useState<Stage>("idle");
  const [progress, setProgress] = useState<IngestJobState | null>(null);
  /** Qwen 3.5 tier sent with every upload (each tier = its own llama-server). */
  const [selectedUploadTier, setSelectedUploadTier] = useState<ModelTierId>(DEFAULT_MODEL_TIER);
  /** Challenge kinds generated per upload (all six pre-checked). */
  const [selectedTypes, setSelectedTypes] = useState<ChallengeType[]>([
    ...GENERATED_CHALLENGE_TYPES,
  ]);
  /** Collapsed chapter folders on the pathway map (Set of lecture ids). */
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set());
  /** Which folder's options dropdown is open (lecture id, or null). */
  const [folderMenuId, setFolderMenuId] = useState<string | null>(null);
  /** Pending deletion shown in the confirmation modal (id + title). */
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string } | null>(null);
  /** Node whose 1v1 duel room is being created (disabled while in flight). */
  const [duelBusy, setDuelBusy] = useState<string | null>(null);
  /** Lecture currently being cached to IndexedDB (offline download in flight). */
  const [offlineBusyId, setOfflineBusyId] = useState<string | null>(null);
  /** Lecture id -> number of its nodes cached in IndexedDB for offline play. */
  const [offlineSaved, setOfflineSaved] = useState<Record<string, number>>({});
  /** Exam Crunch build in flight (cheatsheet + mock exam). */
  const [crunchBusy, setCrunchBusy] = useState(false);
  /** Classrooms this account teaches / is enrolled in, plus the join form. */
  const [classrooms, setClassrooms] = useState<{
    taught: ClassroomSummary[];
    enrolled: ClassroomSummary[];
  }>({ taught: [], enrolled: [] });
  const [classroomCode, setClassroomCode] = useState("");
  const [classroomBusy, setClassroomBusy] = useState(false);
  const [classroomNotice, setClassroomNotice] = useState<string | null>(null);
  /** 🏫 Create-classroom modal + the share card for an existing classroom. */
  const [classroomCreateOpen, setClassroomCreateOpen] = useState(false);
  const [classroomShare, setClassroomShare] = useState<ClassroomSummary | null>(null);
  const router = useRouter();

  /** Exam Crunch: build the cheatsheet + mock exam, then open the runner. */
  const startExamCrunch = useCallback(async () => {
    if (!selectedCourseId || !userId || crunchBusy) return;
    setCrunchBusy(true);
    setError(null);
    setNotice(null);
    try {
      const exam = await api.examCrunch(selectedCourseId, userId);
      router.push(`/exam/${exam.exam_session_id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        // Graceful beat: the course simply has no gradable material yet.
        const detail = err.detail;
        const message =
          detail && typeof detail === "object" && "message" in detail
            ? String((detail as { message?: unknown }).message)
            : null;
        setNotice(
          message || "المقرر يحتاج إلى محاضرة واحدة مكتملة على الأقل لتوليد الامتحان الشامل",
        );
      } else {
        setError(errorText(err));
      }
    } finally {
      setCrunchBusy(false);
    }
  }, [selectedCourseId, userId, crunchBusy, router]);

  /** Refresh the taught/enrolled classroom lists. */
  const loadClassrooms = useCallback(async (id: string) => {
    try {
      const body = await api.classrooms(id);
      setClassrooms({ taught: body.taught, enrolled: body.enrolled });
    } catch {
      /* classroom list is auxiliary — never block the dashboard on it */
    }
  }, []);

  /** Enroll in a classroom with the 6-char class code. */
  const joinClassroom = async () => {
    if (!userId || !classroomCode.trim() || classroomBusy) return;
    setClassroomBusy(true);
    setError(null);
    try {
      const joined = await api.joinClassroom(classroomCode.trim(), userId);
      setClassroomNotice(
        joined.already_enrolled
          ? `أنت منضم بالفعل إلى «${joined.name}»`
          : `انضممت إلى «${joined.name}» — ستظهر المحاضرات الجديدة تلقائياً`,
      );
      setClassroomCode("");
      await loadClassrooms(userId);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setClassroomBusy(false);
    }
  };

  /** Open a battle-arena room from a node and jump straight into the duel. */
  const startDuel = useCallback(
    async (nodeId: string) => {
      if (duelBusy) return;
      setDuelBusy(nodeId);
      try {
        const room = await api.createArenaRoom(nodeId, 8);
        router.push(`/arena/${room.room_code}`);
      } catch (error) {
        setError(errorText(error));
      } finally {
        setDuelBusy(null);
      }
    },
    [duelBusy, router],
  );
  /** Upload modal open state. */
  const [uploadOpen, setUploadOpen] = useState(false);
  /** Standalone "Create Course" modal open state (top action button). */
  const [createCourseOpen, setCreateCourseOpen] = useState(false);
  /** Tiers whose llama-server answered /health (reported by the backend). */
  const [onlineTiers, setOnlineTiers] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<ProcessLectureResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Friendly toast/banner (400 insufficient_content & co.) — not an error. */
  const [notice, setNotice] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);
  const [shareCourseId, setShareCourseId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importPrefill, setImportPrefill] = useState<string | undefined>(undefined);
  const [stats, setStats] = useState<{
    cards_due: number;
    cards_total: number;
    accuracy_last_50: number;
    cards_relearning: number;
  } | null>(null);

  /** Confetti: one 3s burst whenever a lecture finishes generating. */
  const [celebrate, setCelebrate] = useState(0);
  useEffect(() => {
    if (stage !== "done") return;
    setCelebrate((count) => count + 1);
    const timer = setTimeout(() => setCelebrate(0), 3200);
    return () => clearTimeout(timer);
  }, [stage]);

  /** Golden "+N XP" float whenever the header XP counter increases. */
  const [xpFloat, setXpFloat] = useState<{ id: number; amount: number } | null>(null);
  const prevXp = useRef(xp);
  useEffect(() => {
    if (xp > prevXp.current) {
      setXpFloat({ id: Date.now(), amount: xp - prevXp.current });
      const timer = setTimeout(() => setXpFloat(null), 1200);
      prevXp.current = xp;
      return () => clearTimeout(timer);
    }
    prevXp.current = xp;
  }, [xp]);

  /**
   * The verified session this workspace is rendered for. `booting` stays true
   * until the very first bootstrap decision has been made, which prevents the
   * auth modal from flashing on every keystroke of a re-render ("switch
   * switch" glitch).
   */
  const authenticated = Boolean(userId && email);
  const showAuth = !booting && !authenticated;
  const bootstrappedFor = useRef<string | null>(null);

  const nodesRequestSeq = useRef(0);
  /**
   * Synchronous re-entry lock for uploads. `stage` only flips to "uploading" on
   * the next render, so a fast double-click / double-drop could otherwise start
   * two ingestion jobs and create duplicate lectures. The ref is set in the
   * same tick as the first click, so the second one is a no-op.
   */
  const uploadInFlight = useRef(false);
  /**
   * Progressive-unlocking trigger: how many nodes the polled ingest job has
   * unlocked so far. The polling loop is the primary reactive mechanism (the
   * classroom SSE hub had 0 subscribers for personal courses), so every time
   * this counter grows the chapter tree is re-fetched immediately — that is
   * what makes Part 1 appear on the dashboard within seconds of the upload.
   */
  const prevUnlockedCount = useRef(0);

  const loadNodes = useCallback(async (courseId: string) => {
    // Race guard: only the latest requested course may write into state, so a
    // slow response for an older selection can never clobber the current one.
    setNodesLoading(true);
    const requestId = ++nodesRequestSeq.current;
    try {
      const detail = await api.courseNodes(courseId);
      if (requestId !== nodesRequestSeq.current) return;
      setNodes(detail.nodes);
      setNodesCourseId(courseId);
    } finally {
      if (requestId === nodesRequestSeq.current) setNodesLoading(false);
    }
  }, []);

  const loadCourses = useCallback(async (id: string, selectId?: string) => {
    const list = await api.listCourses(id);
    setCourses(list);
    if (selectId) {
      setSelectedCourseId(selectId);
    } else if (list.length > 0) {
      setSelectedCourseId((prev) => prev ?? list[0].id);
    }
    return list;
  }, []);

  /**
   * Full workspace load for one verified user: stats + courses + mistakes.
   * Runs once per identity — re-renders never re-trigger it.
   */
  const bootstrapFor = useCallback(
    async (id: string) => {
      setBooting(true);
      setError(null);
      try {
        const [userStats] = await Promise.all([api.stats(id), loadCourses(id)]);
        setStats(userStats);
        await loadMistakes();
        // Informational: which Qwen 3.5 tiers have a llama-server up.
        try {
          const healthBody = await api.health();
          const tiers = healthBody.llm?.tiers ?? {};
          setOnlineTiers(
            new Set(
              Object.entries(tiers)
                .filter(([, status]) => status.ok)
                .map(([id]) => id),
            ),
          );
        } catch {
          /* tier availability is informational; never block the dashboard */
        }
      } catch (err) {
        setError(errorText(err));
      } finally {
        setBooting(false);
      }
    },
    [loadCourses, loadMistakes],
  );

  // --- one-time hydration gate ---------------------------------------------
  // zustand/persist rehydrates asynchronously; on first paint `userId` is still
  // null even for a returning user. Wait for rehydration before deciding
  // whether to show the auth modal, otherwise the modal flickers on reload.
  useEffect(() => {
    let cancelled = false;
    const rehydrated = useQuizStore.persist.hasHydrated()
      ? Promise.resolve()
      : new Promise<void>((resolve) => {
          const unsub = useQuizStore.persist.onFinishHydration(() => {
            unsub();
            resolve();
          });
        });
    void rehydrated.then(() => {
      if (!cancelled) setBooting(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // --- single bootstrap per verified identity --------------------------------
  // Fires exactly once per userId (guarded by bootstrappedFor), so switching
  // courses, typing in the creation form or any other re-render never re-runs
  // the boot sequence and never re-shows the auth modal.
  useEffect(() => {
    if (booting) return;
    // Purge stale cached credentials left over from the old hardcoded account.
    if (isStaleIdentity(email)) {
      logout();
      return;
    }
    if (!userId || !email) return;
    if (!isValidUserId(userId)) {
      // Stale/corrupt identity: drop it and let the auth modal take over.
      logout();
      return;
    }
    if (bootstrappedFor.current === userId) return;
    bootstrappedFor.current = userId;
    void bootstrapFor(userId);
  }, [booting, userId, email, bootstrapFor, logout]);

  // --- deep link: /?import=CODE pre-fills and opens the import dialog -------
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const code = params.get("import");
    if (code) {
      setImportPrefill(code.toUpperCase());
      setImportOpen(true);
      // Strip the query so a later navigation does not re-open the dialog.
      window.history.replaceState(null, "", window.location.pathname);
    }
  }, []);

  // --- refresh stats whenever the dashboard (re)loads ------------------------
  useEffect(() => {
    if (!authenticated) return;
    void refreshStats();
  }, [authenticated, refreshStats]);

  /** Nodes grouped under their parent lecture (one PDF = one chapter block). */
  const lectureGroups = useMemo(() => groupNodesByLecture(nodes), [nodes]);

  /** The course the share link points at: the selected one, else the first. */
  const activeCourse = useMemo(
    () => courses.find((course) => course.id === selectedCourseId) ?? courses[0] ?? null,
    [courses, selectedCourseId],
  );

  /**
   * Share code behind the "رابط مشاركة" button. Older courses may not have a
   * code yet, so it is minted lazily via GET /api/courses/{id}/share-code — the
   * link is NEVER the placeholder "CL-SHARE" that shipped by mistake.
   */
  const [shareJoinCode, setShareJoinCode] = useState<string | null>(null);
  useEffect(() => {
    if (!activeCourse) {
      setShareJoinCode(null);
      return;
    }
    if (activeCourse.share_code) {
      setShareJoinCode(activeCourse.share_code);
      return;
    }
    let cancelled = false;
    setShareJoinCode(null);
    api
      .getShareCode(activeCourse.id)
      .then((response) => {
        if (!cancelled) setShareJoinCode(response.share_code);
      })
      .catch(() => {
        if (!cancelled) setShareJoinCode(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeCourse]);

  // Load the classrooms once the account is known.
  useEffect(() => {
    if (!userId) return;
    void loadClassrooms(userId);
  }, [userId, loadClassrooms]);

  /**
   * Live classroom sync: the dashboard keeps an SSE stream open per class it is
   * enrolled in OR teaches, so `node_unlocked` (progressive ingestion) drops a
   * freshly committed Part 1 straight into the tree with no refresh. Deduped and
   * capped at three concurrent streams. This is the real-time companion to the
   * 1.5s ingest-job polling, which stays the robust fallback for personal
   * courses where the classroom hub has no listeners.
   */
  useEffect(() => {
    if (!userId || typeof window === "undefined") return;
    const live = [...classrooms.taught, ...classrooms.enrolled]
      .filter((course, index, list) => list.findIndex((c) => c.id === course.id) === index)
      .slice(0, 3);
    if (live.length === 0) return;
    const sources = live.map((course) => {
      const source = new EventSource(api.classroomUpdatesUrl(course.id, userId));
      source.addEventListener("lecture_added", (event) => {
        let title = "";
        try {
          title = (JSON.parse((event as MessageEvent).data) as { lecture_title?: string })
            .lecture_title ?? "";
        } catch {
          /* malformed frame: still refresh below */
        }
        setClassroomNotice(`📢 مادة جديدة: ${title || course.name}`);
        void loadCourses(userId, course.id);
        if (selectedCourseId === course.id) void loadNodes(course.id);
      });
      // Progressive ingestion: each finished chunk fires node_unlocked while
      // the lecture is still generating — unlock the new part live.
      source.addEventListener("node_unlocked", (event) => {
        let part = "";
        try {
          part = (JSON.parse((event as MessageEvent).data) as { node_title?: string })
            .node_title ?? "";
        } catch {
          /* malformed frame: still refresh below */
        }
        setClassroomNotice(`🔓 فُتح جزء جديد: ${part || course.name}`);
        void loadCourses(userId, course.id);
        if (selectedCourseId === course.id) void loadNodes(course.id);
      });
      return source;
    });
    return () => sources.forEach((source) => source.close());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, classrooms.taught.length, classrooms.enrolled.length, selectedCourseId, loadNodes, loadCourses]);

  /**
   * Reflect what is already downloadable: map every cached node back to its
   * lecture and count it, so the "تحميل بدون اتصال" badge survives a reload.
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const cached = await listCachedNodes();
      if (cancelled) return;
      const cachedIds = new Set(cached.map((entry) => entry.nodeId));
      const counts: Record<string, number> = {};
      for (const group of lectureGroups) {
        const saved = group.nodes.filter(({ node }) => cachedIds.has(node.id)).length;
        if (saved > 0) counts[group.lectureId] = saved;
      }
      setOfflineSaved(counts);
    })();
    return () => {
      cancelled = true;
    };
  }, [lectureGroups]);

  // Real-time sync: poll stats every 60s so passively regenerated gems and
  // heart state land in the UI without a manual refresh.
  useEffect(() => {
    if (!authenticated) return;
    const sync = window.setInterval(() => {
      void refreshStats();
      void loadMistakes();
    }, 60_000);
    return () => window.clearInterval(sync);
  }, [authenticated, refreshStats, loadMistakes]);

  // --- load nodes when the selected course changes ---------------------------
  // Single load per selection: skipped when this course's nodes are already
  // shown, and race-guarded inside loadNodes against out-of-order responses.
  useEffect(() => {
    if (!selectedCourseId || selectedCourseId === nodesCourseId) return;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCourseId, loadNodes]);

  // --- course creation -------------------------------------------------------
  const createCourse = async (): Promise<boolean> => {
    const name = newCourseName.trim();
    if (!name) {
      setError("اكتب اسم المقرر أولاً");
      return false;
    }
    // Guard: course creation needs a valid user UUID. Without one, show the
    // auth modal instead of firing a doomed request.
    if (!isValidUserId(userId)) {
      setError("سجّل الدخول أولاً لإنشاء مقرر");
      logout(); // clears storage + resets stores + re-shows the auth modal
      return false;
    }
    setCreatingCourse(true);
    setError(null);
    try {
      // Single optimistic pass: append to state, select it, load its nodes —
      // no full list reload, no cascading re-mounts.
      const course = await api.createCourse(userId as string, name);
      setNewCourseName("");
      setCourses((prev) => {
        if (prev.some((existing) => existing.id === course.id)) return prev;
        const summary: CourseSummary = {
          id: course.id,
          name: course.name,
          lms_type: course.lms_type ?? "manual",
          created_at: new Date().toISOString(),
          lectures: [],
        };
        return [...prev, summary];
      });
      setSelectedCourseId(course.id);
      // The selectedCourseId effect performs the single nodes load for the new
      // course (deduped via nodesCourseId), so no extra fetch is triggered here.
      void refreshStats();
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setCreatingCourse(false);
    }
  };

  const processFile = async (file: File) => {
    // Lock first: a duplicate submission must never start a second job.
    if (uploadInFlight.current) return;
    if (!isValidUserId(userId)) {
      setError("سجّل الدخول أولاً قبل رفع المحاضرات");
      logout();
      return;
    }
    if (!selectedCourseId) {
      setError("أنشئ مقرراً (Course) أولاً ثم ارفع المحاضرة");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("الملف يجب أن يكون PDF");
      return;
    }
    if (selectedTypes.length === 0) {
      setError("اختر نوعاً واحداً على الأقل من التحديات قبل الرفع");
      return;
    }
    uploadInFlight.current = true;
    setError(null);
    setResult(null);
    setProgress(null);
    setStage("uploading");
    // Progressive unlocking starts from zero for every new job.
    prevUnlockedCount.current = 0;
    // The nodes known BEFORE this job: the newly uploaded lecture is the one
    // folder that gains nodes during polling, so it is the one auto-expanded.
    const knownLectureIds = new Set(nodes.map((node) => node.lecture_id));

    const form = new FormData();
    form.append("file", file);
    form.append("course_id", selectedCourseId);
    form.append("model_name", DEFAULT_MODEL);
    form.append("model_tier", selectedUploadTier);
    // Always send the chosen generatable types so the backend never falls back
    // to a broader set than the learner picked.
    if (selectedTypes.length > 0) {
      form.append("challenge_types", selectedTypes.join(","));
    }

    try {
      // 202 immediately — parsing, Qwen generation and persistence all run
      // server-side; we poll real per-chunk progress instead of guessing.
      setStage("generating");
      const accepted = await startIngestion(form);
      // PROGRESSIVE UNLOCKING (zero-wait start): the job is polled every 1.5s
      // and every time a new part commits server-side (``unlocked_nodes``
      // grows past prevUnlockedCount) the chapter tree is re-fetched at once —
      // the freshly committed Part 1 shows up as a playable green node within
      // seconds, while parts 2..N keep generating in the background. The
      // newly created lecture's folder is auto-expanded so the learner SEES
      // the part appear without any manual refresh or collapsing.
      const final = await awaitIngestion(accepted.job_id, (state) => {
        setProgress(state);
        if (state.status === "saving") setStage("saving");
        const unlockedCount = state.unlocked_nodes?.length ?? 0;
        if (unlockedCount > prevUnlockedCount.current) {
          prevUnlockedCount.current = unlockedCount;
          void loadNodes(selectedCourseId).then(() => {
            // Expand the lecture this job created (the one not present before
            // the upload) so Part 1 is visible the moment it commits.
            setNodes((latest) => {
              const freshLectureId = latest.find(
                (node) => !knownLectureIds.has(node.lecture_id),
              )?.lecture_id;
              if (freshLectureId) {
                setCollapsedFolders((prev) => {
                  const next = new Set(prev);
                  next.delete(freshLectureId);
                  return next;
                });
              }
              // Read-only updater: return the same array untouched.
              return latest;
            });
          });
        }
      });
      setResult({
        lecture_id: final.lecture_id ?? "",
        node: final.units[0]?.node ?? {},
        challenges: [],
        cards_seeded: final.cards_seeded,
        embeddings_stored: final.embeddings_stored,
        challenges_by_type: final.challenges_by_type,
        generation_errors: final.generation_errors,
        units: final.units,
        elapsed_ms: final.elapsed_ms,
      });
      setStage("done");
      await loadNodes(selectedCourseId);
      await loadCourses(userId as string, selectedCourseId);
      setStats(await api.stats(userId as string));
      await loadMistakes();
    } catch (err) {
      setError(errorText(err));
      setStage("error");
    } finally {
      uploadInFlight.current = false;
    }
  };

  const deleteCourse = async (courseId: string, courseName: string) => {
    if (!window.confirm(`هل أنت متأكد من حذف المقرر "${courseName}" وجميع محاضراته؟`)) return;
    try {
      await api.deleteCourse(courseId);
      setCourses((prev) => prev.filter((course) => course.id !== courseId));
      if (selectedCourseId === courseId) {
        setSelectedCourseId(null);
        setNodes([]);
        setNodesCourseId(null);
      }
    } catch (err) {
      setError(errorText(err));
    }
  };

  /**
   * Delete ONE lecture and surgically remove exactly that lecture from local
   * state — no reload that could wipe sibling lectures if a refetch races.
   */
  const deleteLecture = async (lectureId: string) => {
    try {
      await api.deleteLecture(lectureId);
      // Nodes + the chapter folder keyed by this lecture vanish; every other
      // lecture, node and course row stays untouched in state.
      const orphaned = nodes.filter((node) => node.lecture_id === lectureId);
      setNodes((prev) => prev.filter((node) => node.lecture_id !== lectureId));
      setOfflineSaved((prev) => {
        const next = { ...prev };
        delete next[lectureId];
        return next;
      });
      // Drop any offline copies of this lecture's nodes (best-effort).
      for (const node of orphaned) await removeCachedNode(node.id).catch(() => undefined);
      setCourses((prev) =>
        prev.map((course) =>
          course.id === selectedCourseId
            ? {
                ...course,
                lectures: course.lectures.filter((lecture) => lecture.id !== lectureId),
              }
            : course,
        ),
      );
      // Refresh the review counters: the deleted folder's cards no longer count.
      if (userId && isValidUserId(userId)) {
        setStats(await api.stats(userId));
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setDeleteTarget(null);
    }
  };

  /**
   * Cache every node of one chapter folder into IndexedDB so the whole lecture
   * can be played in airplane mode. Challenges are fetched through the normal
   * API (already offline-aware), then written to the cache store.
   */
  const saveLectureOffline = async (group: LectureGroup) => {
    if (offlineBusyId) return;
    setFolderMenuId(null);
    setOfflineBusyId(group.lectureId);
    setError(null);
    try {
      let saved = 0;
      for (const { node } of group.nodes) {
        const payload = await getNodeChallenges(node.id);
        await cacheNode(node.id, payload);
        saved += 1;
      }
      setOfflineSaved((prev) => ({ ...prev, [group.lectureId]: saved }));
      playSfx("fanfare");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setOfflineBusyId(null);
    }
  };

  /** Drop a lecture's cached nodes (frees IndexedDB; never touches the DB). */
  const forgetLectureOffline = async (group: LectureGroup) => {
    setFolderMenuId(null);
    try {
      for (const { node } of group.nodes) await removeCachedNode(node.id);
      setOfflineSaved((prev) => {
        const next = { ...prev };
        delete next[group.lectureId];
        return next;
      });
    } catch (err) {
      setError(errorText(err));
    }
  };

  /**
   * Import a shared course: append it, select it, and let the deduped
   * selectedCourseId effect load its nodes onto the pathway map.
   */
  const handleImported = async (result: { courseId: string; name: string }) => {
    setError(null);
    setCourses((prev) =>
      prev.some((course) => course.id === result.courseId)
        ? prev
        : [
            ...prev,
            {
              id: result.courseId,
              name: result.name,
              lms_type: "shared",
              created_at: new Date().toISOString(),
              lectures: [],
            },
          ],
    );
    setSelectedCourseId(result.courseId);
    if (userId && isValidUserId(userId)) {
      try {
        await loadCourses(userId, result.courseId);
      } catch (err) {
        setError(errorText(err));
      }
    }
  };

  /** Log out: wipe storage + stores, then the auth modal takes over. */
  const handleLogout = () => {
    bootstrappedFor.current = null;
    setCourses([]);
    setNodes([]);
    setNodesCourseId(null);
    setSelectedCourseId(null);
    setStats(null);
    setResult(null);
    setError(null);
    logout();
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

  /** Parts of the in-flight deck still generating server-side. */
  const remainingParts = progress
    ? Math.max(0, progress.total_chunks - progress.done_chunks)
    : 0;
  /** True while parts 2..N of the just-uploaded deck are still building. */
  const showIngestSkeleton = busy && remainingParts > 0;
  /**
   * The lecture folder that is actively generating right now (the job's
   * lecture if known, else the newest one on the tree that had no nodes before
   * this upload). Its folder gets the inline "⏳ جاري إعداد Part N" node.
   */
  const generatingLectureId = useMemo(() => {
    if (!showIngestSkeleton) return null;
    if (progress?.lecture_id) return progress.lecture_id;
    return nodes[0]?.lecture_id ?? null;
  }, [showIngestSkeleton, progress?.lecture_id, nodes]);

  /** Unlocked = first node or the previous node is completed. */
  const nodeStatuses = useMemo(() => {
    return nodes.map((node, index) => {
      if (completedNodeIds.includes(node.id)) return "completed" as const;
      if (index === 0) return "unlocked" as const;
      const previous = nodes[index - 1];
      return completedNodeIds.includes(previous.id) ? ("unlocked" as const) : ("locked" as const);
    });
  }, [nodes, completedNodeIds]);

  // --- auth gate -------------------------------------------------------------
  if (showAuth) {
    return (
      <AuthModal
        onAuthenticated={({ id }) => {
          // The boot effect may have already started for this id — boot once.
          if (bootstrappedFor.current !== id) {
            bootstrappedFor.current = id;
            void bootstrapFor(id);
          }
        }}
      />
    );
  }

  if (booting || !authenticated) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-12">
        <Loader2 className="h-8 w-8 animate-spin text-feather-blue" aria-hidden="true" />
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col gap-6 px-4 py-6 sm:px-6">
      {/* Top bar: identity / streak calendar / hearts / XP ------------------ */}
      <header className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border-2 border-surface-border bg-surface px-4 py-3 shadow-card">
        <div className="flex items-center gap-2">
          <span className="font-display text-xl font-extrabold text-feather-green">CampusLingo</span>
          <span className="hidden font-body text-xs text-ink-faint sm:inline">{email}</span>
        </div>
        <div className="flex flex-wrap items-center gap-3 font-display text-sm font-bold">
          {/* Streak: flame + Sat..Fri dot strip (Duolingo's loss-aversion loop) */}
          <StreakCalendar />
          {/* Stat chips: icon-in-ring counters, like the app's top bar */}
          <span
            className="flex items-center gap-1.5 rounded-2xl px-1 py-0.5 text-feather-red"
            title="قلوب لا نهائية — تدريب بلا قيود"
          >
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-feather-red/15">
              <Heart className="h-4 w-4 fill-feather-red text-feather-red" aria-hidden="true" />
            </span>
            ∞
          </span>
          <span className="relative flex items-center gap-1.5 rounded-2xl px-1 py-0.5 text-feather-purple" title="XP">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-feather-purple/15">
              <Zap className="h-4 w-4 fill-feather-purple text-feather-purple" aria-hidden="true" />
            </span>
            {xp}
            {xpFloat && (
              <span
                key={xpFloat.id}
                className="xp-float absolute -top-1 right-0 font-display text-xs font-extrabold text-feather-yellow"
              >
                +{xpFloat.amount} XP
              </span>
            )}
          </span>
          <span className="flex items-center gap-1.5 rounded-2xl px-1 py-0.5 text-feather-blue" title="Gems">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-feather-blue/15">
              <Gem className="h-4 w-4 fill-feather-blue text-feather-blue" aria-hidden="true" />
            </span>
            {gems}
          </span>
          <span className="hidden rounded-xl bg-feather-purple/15 px-2 py-1 font-display text-[11px] font-extrabold uppercase text-feather-purple sm:inline">
            {LEAGUE_NAMES[leagueTier] ?? "Bronze"}
          </span>
          <ThemeToggle />
          <button
            type="button"
            onClick={toggleSound}
            className="rounded-xl border-2 border-surface-border px-2 py-1 text-xs"
            title={soundEnabled ? "الصوت مفعّل" : "الصوت مكتوم"}
          >
            {soundEnabled ? "🔊" : "🔇"}
          </button>
          <button
            type="button"
            onClick={handleLogout}
            className="flex items-center gap-1 rounded-xl border-2 border-surface-border px-2 py-1 text-xs text-ink-soft hover:border-feather-red hover:text-feather-red"
            title="تسجيل الخروج / تبديل الحساب"
          >
            <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
            <span className="hidden sm:inline">تبديل الحساب</span>
          </button>
        </div>
      </header>

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

      {notice && (
        <div className="rounded-2xl border-2 border-feather-orange/70 bg-feather-orange/10 px-4 py-3">
          <p className="font-body text-sm font-bold text-ink-soft">{notice}</p>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="mt-1 font-display text-xs font-bold uppercase text-ink-faint"
          >
            إخفاء
          </button>
        </div>
      )}

      {/* Daily Memory Warm-up ritual banner (milestone §2): the inescapable
          review beat, rendered above everything else on the dashboard. */}
      <WarmupBanner />

      {/* Celebration: confetti rains for ~3s after a successful generation. */}
      {celebrate > 0 && <ConfettiBurst />}

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

          {/* Dual primary entry points: create a course, or upload a lecture
              straight into the active course. Both stay always visible; the
              floating button below remains a mobile quick-access shortcut. */}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setCreateCourseOpen(true)}
              className="flex flex-1 items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-3 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0 sm:flex-none"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              إنشاء مقرر جديد
            </button>
            <button
              type="button"
              onClick={() => setUploadOpen(true)}
              disabled={busy}
              className="flex flex-1 items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase tracking-wide text-white shadow-key transition-transform active:translate-y-[2px] active:border-b-0 disabled:opacity-60 sm:flex-none"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <UploadCloud className="h-4 w-4" aria-hidden="true" />
              )}
              {busy ? "جارٍ الرفع…" : "رفع محاضرة"}
            </button>
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
            <button
              type="button"
              onClick={() => setImportOpen(true)}
              className="flex items-center gap-1 rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-3 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key active:translate-y-[2px]"
              title="استيراد مقرر عبر كود مشاركة"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              استيراد
            </button>
            <button
              type="button"
              onClick={() => setShareCourseId(selectedCourseId)}
              disabled={!selectedCourseId}
              className="flex items-center gap-1 rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold uppercase text-ink-soft hover:border-feather-blue hover:text-feather-blue disabled:opacity-50"
              title="مشاركة المقرر المحدد عبر كود"
            >
              <Share2 className="h-4 w-4" aria-hidden="true" />
              مشاركة
            </button>
            {/* Exam Crunch: cheatsheet + diagnostic timed mock exam ---------- */}
            <button
              type="button"
              disabled={!selectedCourseId || crunchBusy}
              onClick={() => void startExamCrunch()}
              className="flex items-center gap-1 rounded-xl border-2 border-b-4 border-feather-purple bg-feather-purple px-3 py-2 font-display text-xs font-extrabold uppercase text-white shadow-key disabled:opacity-50 active:translate-y-[2px]"
              title="ورقة مراجعة عالية العائد + امتحان تجريبي مؤقّت"
            >
              {crunchBusy ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Zap className="h-4 w-4" aria-hidden="true" />
              )}
              {crunchBusy ? "جارٍ التحضير…" : "Exam Crunch"}
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

          {/* Qwen 3.5 tier selector — travels with the upload to the backend */}
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="model-tier" className="font-display text-xs font-extrabold uppercase text-ink-soft">
              نموذج التوليد
            </label>
            <select
              id="model-tier"
              value={selectedUploadTier}
              disabled={busy}
              onChange={(event) => setSelectedUploadTier(event.target.value as ModelTierId)}
              className="rounded-xl border-2 border-surface-border bg-surface-muted px-3 py-1.5 font-display text-xs font-extrabold text-ink focus:border-feather-blue focus:outline-none disabled:opacity-50"
            >
              {MODEL_TIERS.map((tier) => (
                <option key={tier.id} value={tier.id}>
                  {tier.name} — {tier.label}
                </option>
              ))}
            </select>
            {onlineTiers.size > 0 && (
              <span className="font-body text-[11px] text-ink-faint">
                متاح: {[...onlineTiers].join(" · ")}
              </span>
            )}
          </div>

          {/* Live progress while a job runs (full stepper lives in the modal) */}
          {busy && (
            <div className="rounded-2xl border-2 border-feather-blue bg-feather-blue/5 p-3">
              <p className="flex items-center gap-2 font-display text-xs font-extrabold text-feather-blue-dark">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                {stageLabel[stage]}
              </p>
              {progress && (
                <p className="mt-1 font-body text-xs text-ink-soft" aria-live="polite">
                  الجزء {progress.done_chunks} من {progress.total_chunks} —{" "}
                  {Math.round(progress.progress * 100)}%
                  {progress.failed_chunks > 0 ? ` · ${progress.failed_chunks} فشل` : ""}
                </p>
              )}
            </div>
          )}

          {result && !busy && (
            <div className="victory-bounce rounded-2xl border-2 border-feather-green bg-feather-green/10 p-4">
              <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
                <Sparkles className="h-5 w-5 text-feather-yellow" fill="currentColor" aria-hidden="true" />
                أحسنت! وحدة تعليمية جديدة جاهزة 🎉
              </p>
              <p className="mt-1 font-body text-xs text-ink-soft">
                {result.cards_seeded} بطاقة FSRS · {Object.values(result.challenges_by_type).reduce((a, b) => a + b, 0)} تحدٍّ ·
                {" "}{result.embeddings_stored} embedding · {(result.elapsed_ms / 1000).toFixed(1)}s
              </p>
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
            className="flex items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key"
          >
            <PlayCircle className="h-4 w-4" aria-hidden="true" />
            جلسة المراجعة اليومية ({(stats?.cards_due ?? 0)} بطاقة)
          </Link>
        </aside>
      </section>

      {/* Classrooms: join with a 6-char code, or open the instructor hub --- */}
      <section className="flex flex-col gap-3 rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-base font-extrabold text-ink">
            <GraduationCap className="h-4 w-4 text-feather-orange" aria-hidden="true" />
            الفصول الدراسية
          </h2>
          <span className="font-body text-[11px] text-ink-faint">
            {classrooms.enrolled.length} منضم · {classrooms.taught.length} أُدرّسها
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            value={classroomCode}
            onChange={(event) => setClassroomCode(event.target.value.toUpperCase())}
            placeholder="كود الفصل (مثال CS301X)"
            maxLength={8}
            dir="ltr"
            className="w-44 rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-sm font-extrabold tracking-wider outline-none focus:border-feather-orange"
          />
          <button
            type="button"
            disabled={!classroomCode.trim() || classroomBusy}
            onClick={() => void joinClassroom()}
            className="rounded-2xl border-2 border-b-4 border-feather-orange bg-feather-orange px-4 py-2 font-display text-xs font-extrabold uppercase text-white disabled:opacity-50 active:translate-y-[2px] active:border-b-2"
          >
            {classroomBusy ? "جارٍ الانضمام…" : "الانضمام بكود"}
          </button>
          <button
            type="button"
            onClick={() => setClassroomCreateOpen(true)}
            className="rounded-2xl border-2 border-b-4 border-feather-green bg-feather-green/10 px-4 py-2 font-display text-xs font-extrabold uppercase text-feather-green-dark active:translate-y-[2px] active:border-b-2"
          >
            🏫 إنشاء فصل دراسي
          </button>
          <Link
            href={shareJoinCode ? `/join/${shareJoinCode}` : "#"}
            aria-disabled={!shareJoinCode}
            onClick={(event) => {
              if (!shareJoinCode) event.preventDefault();
            }}
            className={`rounded-2xl border-2 border-surface-border bg-surface px-3 py-2 font-display text-xs font-extrabold uppercase ${
              shareJoinCode ? "text-ink-soft" : "pointer-events-none text-ink-faint opacity-60"
            }`}
            title={
              shareJoinCode
                ? `رابط مشاركة «${activeCourse?.name ?? ""}»`
                : "أنشئ مقرراً أولاً لإنشاء رابط مشاركة"
            }
          >
            رابط مشاركة
          </Link>
        </div>

        {classroomNotice && (
          <p className="rounded-2xl border-2 border-feather-blue/40 bg-feather-blue/10 px-3 py-2 font-body text-xs font-bold text-feather-blue-dark">
            {classroomNotice}
          </p>
        )}

        {classrooms.taught.length > 0 && (
          <ul className="flex flex-wrap gap-2">
            {classrooms.taught.map((course) => (
              <li key={course.id} className="flex items-center gap-1">
                <Link
                  href={`/instructor/${course.id}`}
                  className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green/10 px-3 py-2 font-display text-xs font-extrabold text-feather-green-dark active:translate-y-[2px] active:border-b-2"
                >
                  <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  {course.name}
                  <span className="rounded-lg bg-surface px-1.5 py-0.5 font-body text-[10px] text-ink-faint" dir="ltr">
                    {course.enrollment_code ?? "—"}
                  </span>
                </Link>
                <button
                  type="button"
                  onClick={() => setClassroomShare(course)}
                  title="مشاركة الفصل"
                  aria-label={`مشاركة ${course.name}`}
                  className="rounded-xl border-2 border-surface-border bg-surface p-2 text-ink-faint transition-colors hover:text-feather-green"
                >
                  <Share2 className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Node pathway map: one collapsible folder per uploaded PDF ---------- */}
      <section className="flex flex-col gap-3 pb-8">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-xl font-extrabold text-ink">
            <BookOpen className="h-5 w-5 text-feather-blue" aria-hidden="true" />
            الفصول
          </h2>
          <span className="font-body text-xs text-ink-faint">
            {completedNodeIds.length}/{nodes.length} مكتملة
          </span>
        </div>

        {nodes.length === 0 ? (
          showIngestSkeleton ? (
            <ProgressiveIngestSkeleton progress={progress} />
          ) : (
            <p className="rounded-2xl border-2 border-dashed border-surface-border bg-surface p-4 font-body text-sm text-ink-soft">
              لا توجد وحدات بعد — ارفع محاضرة PDF لبناء المسار التعليمي.
            </p>
          )
        ) : (
          <div className="flex flex-col gap-4">
            {lectureGroups.map((group) => {
              const groupDoneCount = group.nodes.filter((entry) =>
                completedNodeIds.includes(entry.node.id)
              ).length;
              // The winding path: like Duolingo's skill tree, nodes alternate
              // left / centre / right, and the first unlocked node gets the
              // pulsing START bubble instead of a plain button.
              const offsets = ["sm:me-auto", "sm:mx-auto", "sm:ms-auto"];
              const nextNode = group.nodes.find(
                ({ node }, index) => nodeStatuses[nodes.indexOf(node)] === "unlocked" && index === group.nodes.findIndex(
                  ({ node: inner }) => nodeStatuses[nodes.indexOf(inner)] === "unlocked",
                ),
              );
              const collapsed = collapsedFolders.has(group.lectureId);
              return (
                <ChapterFolder
                  key={group.lectureId}
                  group={group}
                  doneCount={groupDoneCount}
                  collapsed={collapsed}
                  onToggle={() =>
                    setCollapsedFolders((prev) => {
                      const next = new Set(prev);
                      if (next.has(group.lectureId)) next.delete(group.lectureId);
                      else next.add(group.lectureId);
                      return next;
                    })
                  }
                  menu={
                    <FolderMenu
                      lectureId={group.lectureId}
                      lectureTitle={group.lectureTitle}
                      open={folderMenuId === group.lectureId}
                      onToggleOpen={() =>
                        setFolderMenuId((prev) =>
                          prev === group.lectureId ? null : group.lectureId,
                        )
                      }
                      onClose={() => setFolderMenuId(null)}
                      offlineBusy={offlineBusyId === group.lectureId}
                      offlineSaved={offlineSaved[group.lectureId] ?? 0}
                      onSaveOffline={() => void saveLectureOffline(group)}
                      onForgetOffline={() => void forgetLectureOffline(group)}
                      onDelete={() =>
                        setDeleteTarget({ id: group.lectureId, title: group.lectureTitle })
                      }
                    />
                  }
                >
                  <ol className="flex flex-col gap-4 px-4 pb-4">
                    {group.nodes.map(({ node, index }) => {
                      const status = nodeStatuses[nodes.indexOf(node)];
                      const locked = status === "locked";
                      const isNext = nextNode?.node.id === node.id;
                      return (
                        <li
                          key={node.id}
                          className={`flex items-center gap-3 ${offsets[index % offsets.length]} w-full sm:w-[calc(100%-2.5rem)]`}
                        >
                          {/* Node medallion */}
                          <span
                            className={`relative flex h-14 w-14 shrink-0 items-center justify-center rounded-full border-2 border-b-[6px] font-display text-lg font-extrabold transition-transform ${
                              status === "completed"
                                ? "border-feather-green-dark bg-feather-green text-white"
                                : locked
                                  ? "border-surface-border bg-surface-muted text-ink-faint"
                                  : isNext
                                    ? "start-pulse border-feather-green-dark bg-feather-green text-white"
                                    : "border-feather-blue-dark bg-feather-blue text-white"
                            } ${!locked ? "hover:scale-105" : ""} ${isNext ? "animate-bounce-slow" : ""}`}
                            title={node.title}
                          >
                            {status === "completed" ? (
                              <CheckCircle2 className="h-6 w-6" aria-hidden="true" />
                            ) : locked ? (
                              <Lock className="h-5 w-5" aria-hidden="true" />
                            ) : isNext ? (
                              <Star className="h-6 w-6 fill-white" aria-hidden="true" />
                            ) : (
                              index + 1
                            )}
                          </span>
                          <div
                            className={`min-w-0 flex-1 rounded-2xl border-2 bg-surface p-3 shadow-card ${
                              status === "completed"
                                ? "border-feather-green/60"
                                : locked
                                  ? "border-surface-border opacity-75"
                                  : isNext
                                    ? "border-feather-green"
                                    : "border-feather-blue/40"
                            }`}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div className="min-w-0">
                                <p className="truncate font-body text-sm font-bold text-ink" dir="auto">
                                  {node.title}
                                </p>
                                <p className="font-body text-[11px] text-ink-faint">
                                  {node.challenge_count} تحديات ·{" "}
                                  {status === "completed" ? "مكتملة" : locked ? "مقفلة" : isNext ? "ابدأ الآن" : "متاحة"}
                                </p>
                              </div>
                              <div className="flex items-center gap-2">
                                <Link
                                  href={locked ? "#" : `/play/${node.id}`}
                                  aria-disabled={locked}
                                  className={`rounded-xl border-2 border-b-4 px-4 py-2 font-display text-xs font-extrabold uppercase transition-transform ${
                                    locked
                                      ? "pointer-events-none border-surface-border bg-surface-muted text-ink-faint"
                                      : isNext
                                        ? "border-feather-green-dark bg-feather-green text-white shadow-key active:translate-y-[2px]"
                                        : "border-feather-blue-dark bg-feather-blue text-white shadow-key active:translate-y-[2px]"
                                  }`}
                                >
                                  {isNext ? "START" : status === "completed" ? "أعد التدريب" : "ابدأ"}
                                </Link>
                                {/* 1v1 battle arena: opens a duel room for this node */}
                                <button
                                  type="button"
                                  disabled={locked || duelBusy === node.id}
                                  onClick={() => void startDuel(node.id)}
                                  title="تحدٍّ مباشر 1 ضد 1 في ساحة المعركة"
                                  className="flex items-center gap-1 rounded-xl border-2 border-b-4 border-feather-purple/60 bg-feather-purple/10 px-2.5 py-2 font-display text-xs font-extrabold text-feather-purple transition-transform active:translate-y-[2px] active:border-b-0 disabled:opacity-40"
                                >
                                  {duelBusy === node.id ? (
                                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                                  ) : (
                                    <Swords className="h-3.5 w-3.5" aria-hidden="true" />
                                  )}
                                </button>
                              </div>
                            </div>
                          </div>
                        </li>
                      );
                    })}
                    {/* Live progressive beat INSIDE the generating folder: the
                        committed parts above stay clickable while the next one
                        pulses below as "being prepared in the background". */}
                    {group.lectureId === generatingLectureId && (
                      <PartPreparingNode
                        nextPart={group.nodes.length + 1}
                        done={progress?.done_chunks ?? 0}
                        total={progress?.total_chunks ?? 0}
                      />
                    )}
                  </ol>
                </ChapterFolder>
              );
            })}

            {/* Live progressive beat: parts 2..N are still generating while the
                already-committed Part 1 sits unlocked above. */}
            {showIngestSkeleton && <ProgressiveIngestSkeleton progress={progress} />}
          </div>
        )}

        <p className="font-body text-xs text-ink-faint">
          كل التحديات مسجّلة في جدول FSRS-4.5: {stats?.cards_total ?? 0} بطاقة إجمالاً ·{" "}
          {stats?.cards_due ?? 0} مستحقة الآن · {stats?.cards_relearning ?? 0} قيد إعادة التعلّم
        </p>
      </section>

      {/* Standalone course creation modal (top "Create Course" action) ------- */}
      {createCourseOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            <div className="flex items-center gap-2">
              <span className="flex h-10 w-10 items-center justify-center rounded-full bg-feather-blue/15">
                <Plus className="h-5 w-5 text-feather-blue" aria-hidden="true" />
              </span>
              <h2 className="font-display text-lg font-black text-ink">إنشاء مقرر جديد</h2>
            </div>
            <p className="mt-2 font-body text-sm text-ink-soft">
              اختر اسماً واضحاً للمقرر، ثم ارفع محاضراته إليه.
            </p>
            <input
              autoFocus
              value={newCourseName}
              onChange={(event) => setNewCourseName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && newCourseName.trim() && !creatingCourse) {
                  void createCourse().then((ok) => ok && setCreateCourseOpen(false));
                }
              }}
              placeholder="مثال: نظم التشغيل"
              disabled={creatingCourse}
              className="mt-3 w-full rounded-xl border-2 border-surface-border bg-surface px-3 py-2 font-body text-sm outline-none focus:border-feather-blue disabled:opacity-60"
            />
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setCreateCourseOpen(false)}
                className="flex-1 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-sm font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
              >
                إلغاء
              </button>
              <button
                type="button"
                onClick={() => void createCourse().then((ok) => ok && setCreateCourseOpen(false))}
                disabled={creatingCourse || !newCourseName.trim()}
                className="flex flex-1 items-center justify-center gap-2 rounded-2xl border-2 border-b-4 border-feather-blue-dark bg-feather-blue px-4 py-2.5 font-display text-sm font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0 disabled:opacity-60"
              >
                {creatingCourse && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                إنشاء المقرر
              </button>
            </div>
          </motion.div>
        </div>
      )}

      {/* Delete confirmation modal ----------------------------------------- */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            <div className="flex items-center gap-2">
              <span className="flex h-10 w-10 items-center justify-center rounded-full bg-feather-red/15">
                <Trash2 className="h-5 w-5 text-feather-red" aria-hidden="true" />
              </span>
              <h2 className="font-display text-lg font-black text-ink">حذف الفصل؟</h2>
            </div>
            <p className="mt-2 font-body text-sm text-ink-soft" dir="auto">
              سيُحذف "{deleteTarget.title}" بكل عُقده وتحدياته وبطاقات FSRS. المحاضرات الأخرى
              لن تتأثر.
            </p>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                className="flex-1 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-sm font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
              >
                إلغاء
              </button>
              <button
                type="button"
                onClick={() => void deleteLecture(deleteTarget.id)}
                className="flex-1 rounded-2xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-4 py-2.5 font-display text-sm font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0"
              >
                حذف نهائي
              </button>
            </div>
          </motion.div>
        </div>
      )}

      {/* Floating upload button (frictionless modal entry) ------------------ */}
      <motion.button
        type="button"
        whileHover={{ scale: 1.04 }}
        whileTap={{ scale: 0.96 }}
        onClick={() => setUploadOpen(true)}
        className="fixed bottom-20 end-4 z-40 flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-xs font-black uppercase tracking-wider text-white shadow-key lg:bottom-6 lg:end-6"
        aria-label="رفع محاضرة جديدة"
      >
        <UploadCloud className="h-5 w-5" aria-hidden="true" />
        <span className="hidden sm:inline">رفع محاضرة</span>
      </motion.button>

      {/* Upload modal: dropzone + tier pills + type picker + progress stepper */}
      {uploadOpen && (
        <UploadModal
          onClose={() => setUploadOpen(false)}
          courses={courses}
          selectedCourseId={selectedCourseId}
          setSelectedCourseId={setSelectedCourseId}
          createCourse={createCourse}
          creatingCourse={creatingCourse}
          newCourseName={newCourseName}
          setNewCourseName={setNewCourseName}
          selectedTypes={selectedTypes}
          setSelectedTypes={setSelectedTypes}
          processFile={processFile}
          stage={stage}
          progress={progress}
          busy={busy}
        />
      )}

      {/* Course sharing dialogs -------------------------------------------- */}
      <ShareCourseModal
        courseId={shareCourseId}
        courseName={courses.find((course) => course.id === shareCourseId)?.name ?? ""}
        onClose={() => setShareCourseId(null)}
      />
      <ImportCourseModal
        open={importOpen}
        initialCode={importPrefill}
        onClose={() => setImportOpen(false)}
        onImported={(result) => void handleImported(result)}
      />

      {/* Classroom creation + share dialogs ------------------------------- */}
      <CreateClassroomModal
        open={classroomCreateOpen}
        instructorId={userId}
        onCreated={(classroom) => {
          setClassroomNotice(`تم إنشاء «${classroom.name}» — انسخ كود الانضمام وشاركه`);
          if (userId) void loadClassrooms(userId);
        }}
        onClose={() => setClassroomCreateOpen(false)}
      />
      <ClassroomShareModal classroom={classroomShare} onClose={() => setClassroomShare(null)} />
    </main>
  );
}
