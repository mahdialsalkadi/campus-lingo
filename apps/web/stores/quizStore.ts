"use client";

/**
 * Global gamification store: identity (email auth), gems, XP, streak,
 * Mistakes Inbox and the synthesized sound engine, plus the review plumbing
 * that talks to ``/api/reviews`` (FSRS-4.5 lives on the server; this store only
 * mirrors it).
 *
 * Hearts are infinite: nothing in the app ever decrements them, so students can
 * retry and learn continuously.
 *
 * Persistence: zustand/persist -> localStorage (identity + progress). Identity
 * is written only after a successful POST /api/users response. Spaced-repetition
 * truth stays in PostgreSQL.
 */

import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import {
  ApiError,
  api,
  ratingFromResult,
  type AuthUserResponse,
  type ChallengePayload,
  type NodeChallengesResponse,
  type QueueItem,
  type ReviewResponse,
  type UserStats,
} from "@/lib/api";
import { enqueueReview, flushReviewQueue, isOffline } from "@/lib/offline";

export const XP_PER_CORRECT = 10;
export const XP_PERFECT_SESSION_BONUS = 20;

/**
 * Offline XP estimate, mirrors the server's XP_BY_RATING table. Credited
 * optimistically so a lesson never stalls; the authoritative awards land when
 * the queued reviews are replayed on reconnect.
 */
const XP_BY_RATING_FALLBACK: Record<number, number> = { 1: 0, 2: 5, 3: 10, 4: 15 };

export type SfxName =
  | "tap"
  | "correct"
  | "incorrect"
  | "fanfare"
  | "whoosh";

export type ThemeMode = "light" | "dark";

/** Sync the <html data-theme> attribute that app/theme.css keys off. */
export function applyThemeToDom(theme: ThemeMode): void {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", theme);
  }
}

export interface SessionQueueItem {
  challengeId: string;
  nodeId: string;
  nodeTitle: string;
  type: string;
  cardId: string | null;
  payload: ChallengePayload;
}

export interface AttemptResult {
  challengeId: string;
  challengeType: string;
  isCorrect: boolean;
  rating: number;
  xp: number;
  elapsedMs: number;
  at: number;
}

export interface MistakeInboxEntry {
  challengeId: string;
  nodeId: string;
  nodeTitle: string;
  type: string;
  rating: number;
  at: number;
  lapses: number;
  dueDate?: string;
}

interface QuizState {
  // identity & profile
  userId: string | null;
  email: string;
  xp: number;
  gems: number;
  streak: number;
  leagueTier: number;
  soundEnabled: boolean;
  /** UI colour scheme; persisted so the no-flash script can restore it. */
  theme: ThemeMode;

  // session
  queue: SessionQueueItem[];
  index: number;
  /** Challenges answered correctly this session (deduplicated by id). */
  clearedChallengeIds: string[];
  sessionXp: number;
  sessionAttempts: AttemptResult[];
  correctCount: number;
  wrongCount: number;
  /**
   * Questions missed during the initial pass. They seed a SINGLE one-shot
   * remedial round once the main set is cleared (never an infinite loop).
   */
  roundMistakes: SessionQueueItem[];
  /** True while the learner is working through the remedial round. */
  inRemedial: boolean;
  activeNodeId: string | null;
  activeNodeTitle: string;
  startedAt: number | null;
  finishedAt: number | null;
  lastReview: ReviewResponse | null;
  lastXpAward: number;
  completedNodeIds: string[];

  // mistakes inbox: server-side lapsed cards + everything missed this session
  mistakes: QueueItem[];
  sessionMistakes: MistakeInboxEntry[];

  // offline sync
  /** Reviews written to localStorage while the API was unreachable. */
  offlinePending: number;

  // transport
  loading: boolean;
  submitting: boolean;
  error: string | null;

  // actions
  ensureUser: (email?: string) => Promise<string>;
  login: (email: string, password: string) => Promise<{ id: string; created: boolean }>;
  register: (email: string, password: string) => Promise<{ id: string; created: boolean }>;
  logout: () => void;
  toggleTheme: () => void;
  setTheme: (theme: ThemeMode) => void;
  refreshStats: () => Promise<UserStats | null>;
  loadQueue: (limit?: number) => Promise<void>;
  loadMistakes: () => Promise<void>;
  /**
   * Start a lesson queue; ``onlyTypes`` optionally filters the node's
   * challenges to the kinds the learner picked in the pre-session chooser.
   */
  startNodeSession: (
    node: NodeChallengesResponse & { title?: string },
    onlyTypes?: string[],
  ) => void;
  startQueueSession: (items: QueueItem[]) => void;
  recordAttempt: (input: {
    challengeId: string;
    challengeType: string;
    isCorrect: boolean;
    elapsedMs: number;
    hintsUsed?: number;
    responseJson?: unknown;
  }) => Promise<{ rating: number; xp: number; offline?: boolean } | null>;
  /** Replay offline reviews and adopt the server-authoritative XP/gems. */
  syncOfflineReviews: () => Promise<number>;
  submitSessionRating: (rating: 1 | 2 | 3 | 4) => Promise<ReviewResponse | null>;
  addXp: (amount: number) => void;
  /**
   * Advance the session. At the end of the main set, a missed question opens a
   * single "⚡ جولة تصحيح سريعة" (remedial round) exactly once; at the end of
   * that round (or with no mistakes) the session finishes.
   */
  nextChallenge: () => void;
  prevChallenge: () => void;
  finishSession: () => void;
  resetSession: () => void;
  toggleSound: () => void;
  clearError: () => void;
}

// ---------------------------------------------------------------------------
// Sound effects — synthesized with the Web Audio API, no asset files.
// ---------------------------------------------------------------------------

let audioContext: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  if (!audioContext) {
    try {
      audioContext = new Ctor();
    } catch {
      return null;
    }
  }
  if (audioContext.state === "suspended") void audioContext.resume();
  return audioContext;
}

interface ToneOptions {
  freq: number;
  delay?: number;
  duration?: number;
  type?: OscillatorType;
  gain?: number;
  sweepTo?: number;
}

function tone({
  freq,
  delay = 0,
  duration = 0.16,
  type = "sine",
  gain = 0.14,
  sweepTo,
}: ToneOptions): void {
  const ctx = getAudioContext();
  if (!ctx) return;
  const start = ctx.currentTime + delay;
  const osc = ctx.createOscillator();
  const amp = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, start);
  if (sweepTo) {
    osc.frequency.exponentialRampToValueAtTime(Math.max(40, sweepTo), start + duration);
  }
  amp.gain.setValueAtTime(0.0001, start);
  amp.gain.exponentialRampToValueAtTime(gain, start + 0.012);
  amp.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  osc.connect(amp).connect(ctx.destination);
  osc.start(start);
  osc.stop(start + duration + 0.03);
}

export function playSfx(name: SfxName): void {
  switch (name) {
    case "tap":
      tone({ freq: 660, duration: 0.06, type: "triangle", gain: 0.06 });
      break;
    case "correct":
      tone({ freq: 659.25, duration: 0.12, type: "sine" });
      tone({ freq: 987.77, delay: 0.09, duration: 0.18, type: "sine" });
      break;
    case "incorrect":
      tone({ freq: 220, duration: 0.22, type: "sawtooth", gain: 0.1, sweepTo: 110 });
      tone({ freq: 174.61, delay: 0.1, duration: 0.26, type: "square", gain: 0.07 });
      break;
    case "fanfare":
      [523.25, 659.25, 783.99, 1046.5, 1318.5].forEach((freq, index) =>
        tone({ freq, delay: index * 0.1, duration: 0.26, type: "triangle", gain: 0.12 }),
      );
      break;
    case "whoosh":
      tone({ freq: 1200, duration: 0.28, type: "sine", gain: 0.05, sweepTo: 220 });
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Identity helpers
// ---------------------------------------------------------------------------

export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Credentials cached before real email auth existed — purged on startup. */
export const LEGACY_DEFAULT_EMAIL = "student@campuslingo.local";

export function isStaleIdentity(email: string | null | undefined): boolean {
  if (!email) return false;
  return email.trim().toLowerCase() === LEGACY_DEFAULT_EMAIL;
}

export function isValidEmail(email: string | null | undefined): boolean {
  return !!email && EMAIL_PATTERN.test(email.trim().toLowerCase());
}

export function isValidUserId(userId: string | null | undefined): boolean {
  return !!userId && UUID_PATTERN.test(userId);
}

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "unexpected error";
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useQuizStore = create<QuizState>()(
  persist(
    (set, get) => {
      /** Adopt a server-verified account into the store and reset the session. */
      const adoptAccount = (user: AuthUserResponse) =>
        set({
          userId: user.id,
          email: user.email,
          xp: user.xp_points,
          gems: user.gems,
          streak: user.streak_count,
          leagueTier: user.league_tier,
          loading: false,
          // a fresh identity starts with an empty workspace
          queue: [],
          index: 0,
          clearedChallengeIds: [],
          sessionXp: 0,
          sessionAttempts: [],
          sessionMistakes: [],
          correctCount: 0,
          wrongCount: 0,
          roundMistakes: [],
          inRemedial: false,
          activeNodeId: null,
          activeNodeTitle: "",
          startedAt: null,
          finishedAt: null,
          lastReview: null,
          lastXpAward: 0,
          completedNodeIds: [],
          mistakes: [],
          offlinePending: 0,
        });

      return {
      userId: null,
      email: "",
      xp: 0,
      gems: 500,
      streak: 0,
      leagueTier: 0,
      soundEnabled: true,
      theme: "light",

      queue: [],
      index: 0,
      clearedChallengeIds: [],
      sessionXp: 0,
      sessionAttempts: [],
      correctCount: 0,
      wrongCount: 0,
      roundMistakes: [],
      inRemedial: false,
      activeNodeId: null,
      activeNodeTitle: "",
      startedAt: null,
      finishedAt: null,
      lastReview: null,
      lastXpAward: 0,
      completedNodeIds: [],

      mistakes: [],
      sessionMistakes: [],

      offlinePending: 0,

      loading: false,
      submitting: false,
      error: null,

      // -- identity --------------------------------------------------------
      ensureUser: async (email) => {
        const targetEmail = (email ?? get().email).trim().toLowerCase();
        if (!isValidEmail(targetEmail) || isStaleIdentity(targetEmail)) {
          throw new Error("sign in with a valid email first");
        }
        if (get().userId && targetEmail === get().email) return get().userId as string;
        // No cached identity: password auth means we cannot silently create
        // or restore an account here — the caller must go through the modal.
        throw new Error("انتهت الجلسة — سجّل الدخول من جديد");
      },

      /**
       * Email + password sign-in via POST /api/auth/login. Identity is saved
       * to the store (and localStorage via persist) only after the server
       * verifies the credentials, restoring gems/XP/streak for the account.
       */
      login: async (rawEmail, password) => {
        const email = rawEmail.trim().toLowerCase();
        if (!isValidEmail(email) || isStaleIdentity(email)) {
          throw new Error("أدخل بريد إلكتروني صحيح");
        }
        if (!password) throw new Error("أدخل كلمة المرور");
        set({ loading: true, error: null });
        try {
          const user = await api.loginWithPassword(email, password);
          adoptAccount(user);
          return { id: user.id, created: user.created };
        } catch (error) {
          set({ loading: false, error: messageOf(error) });
          throw error;
        }
      },

      /**
       * Create an account with email + password via POST /api/auth/register,
       * then adopt it (500 gems, clean workspace).
       */
      register: async (rawEmail, password) => {
        const email = rawEmail.trim().toLowerCase();
        if (!isValidEmail(email) || isStaleIdentity(email)) {
          throw new Error("أدخل بريد إلكتروني صحيح");
        }
        if (password.length < 8) {
          throw new Error("كلمة المرور يجب أن تكون 8 أحرف على الأقل");
        }
        set({ loading: true, error: null });
        try {
          const user = await api.register(email, password);
          adoptAccount(user);
          return { id: user.id, created: user.created };
        } catch (error) {
          set({ loading: false, error: messageOf(error) });
          throw error;
        }
      },

      /** Log out / switch account: wipe storage, reset all state, show auth. */
      logout: () => {
        try {
          if (typeof window !== "undefined") {
            useQuizStore.persist.clearStorage();
            window.localStorage.removeItem("campuslingo-progress"); // legacy v1 key
          }
        } catch {
          /* storage unavailable — still reset the in-memory state below */
        }
        set({
          userId: null,
          email: "",
          xp: 0,
          gems: 500,
          streak: 0,
          leagueTier: 0,
          queue: [],
          index: 0,
          clearedChallengeIds: [],
          sessionXp: 0,
          sessionAttempts: [],
          sessionMistakes: [],
          correctCount: 0,
          wrongCount: 0,
          roundMistakes: [],
          inRemedial: false,
          activeNodeId: null,
          activeNodeTitle: "",
          startedAt: null,
          finishedAt: null,
          lastReview: null,
          lastXpAward: 0,
          completedNodeIds: [],
          mistakes: [],
          offlinePending: 0,
          loading: false,
          submitting: false,
          error: null,
        });
      },

      /**
       * Mirror the server-authoritative profile (including passively
       * regenerated gems from GET /api/users/{id}/stats) into the store.
       */
      refreshStats: async () => {
        const userId = get().userId;
        if (!userId) return null;
        try {
          const stats = await api.stats(userId);
          set({
            xp: stats.xp_points,
            gems: stats.gems,
            streak: stats.streak_count,
            leagueTier: stats.league_tier,
          });
          return stats;
        } catch (error) {
          set({ error: messageOf(error) });
          return null;
        }
      },

      // -- queues ----------------------------------------------------------
      loadQueue: async (limit = 20) => {
        const userId = get().userId ?? (await get().ensureUser());
        set({ loading: true, error: null });
        try {
          const { items } = await api.queue(userId, limit, true);
          set({
            loading: false,
            queue: items.map((item) => ({
              challengeId: item.challenge_id,
              nodeId: item.node_id,
              nodeTitle: item.node_title,
              type: item.type,
              cardId: item.card_id,
              payload: item.payload_json,
            })),
            index: 0,
            activeNodeId: null,
            activeNodeTitle: "Daily Review",
            startedAt: Date.now(),
            finishedAt: null,
            sessionAttempts: [],
            sessionMistakes: [],
            clearedChallengeIds: [],
            sessionXp: 0,
            correctCount: 0,
            wrongCount: 0,
            lastReview: null,
          });
        } catch (error) {
          set({ loading: false, error: messageOf(error) });
        }
      },

      loadMistakes: async () => {
        const userId = get().userId ?? (await get().ensureUser());
        try {
          const { items } = await api.mistakes(userId, 50);
          set({ mistakes: items });
        } catch (error) {
          set({ error: messageOf(error) });
        }
      },

      startNodeSession: (node, onlyTypes) => {
        const title =
          "node_title" in node && node.node_title
            ? node.node_title
            : (node as { title?: string }).title ?? "Lesson";
        const filtered = onlyTypes?.length
          ? node.challenges.filter((challenge) => onlyTypes.includes(challenge.type))
          : node.challenges;
        set({
          queue: filtered.map((challenge) => ({
            challengeId: challenge.id,
            nodeId: node.node_id,
            nodeTitle: title,
            type: challenge.type,
            cardId: null,
            payload: challenge.payload_json,
          })),
          index: 0,
          activeNodeId: node.node_id,
          activeNodeTitle: title,
          startedAt: Date.now(),
          finishedAt: null,
          sessionAttempts: [],
          sessionMistakes: [],
          clearedChallengeIds: [],
          sessionXp: 0,
          correctCount: 0,
          wrongCount: 0,
          roundMistakes: [],
          inRemedial: false,
          error: null,
          lastReview: null,
        });
      },

      startQueueSession: (items) => {
        set({
          queue: items.map((item) => ({
            challengeId: item.challenge_id,
            nodeId: item.node_id,
            nodeTitle: item.node_title,
            type: item.type,
            cardId: item.card_id,
            payload: item.payload_json,
          })),
          index: 0,
          activeNodeId: null,
          activeNodeTitle: "Review Session",
          startedAt: Date.now(),
          finishedAt: null,
          sessionAttempts: [],
          sessionMistakes: [],
          clearedChallengeIds: [] as string[],
          sessionXp: 0,
          correctCount: 0,
          wrongCount: 0,
          roundMistakes: [],
          inRemedial: false,
          error: null,
          lastReview: null,
        });
      },

      // -- grading ---------------------------------------------------------
      recordAttempt: async ({
        challengeId,
        challengeType,
        isCorrect,
        elapsedMs,
        hintsUsed = 0,
        responseJson,
      }) => {
        const state = get();
        const rating = ratingFromResult(isCorrect, elapsedMs, hintsUsed);
        playSfx(isCorrect ? "correct" : "incorrect");

        const current = state.queue[state.index];
        if (isCorrect) {
          set((prev) => ({
            correctCount: prev.correctCount + 1,
            // Mark this unique challenge as cleared for the progress bar (the
            // recycled clone keeps the same id, so dedupe is automatic).
            clearedChallengeIds: prev.clearedChallengeIds.includes(challengeId)
              ? prev.clearedChallengeIds
              : [...prev.clearedChallengeIds, challengeId],
            sessionXp: prev.sessionXp + XP_PER_CORRECT,
            xp: prev.xp + XP_PER_CORRECT,
            submitting: true,
            error: null,
          }));
        } else {
          // Infinite hearts: a wrong answer never costs a life. The card is NOT
          // discarded — `nextChallenge()` folds it into a single one-shot
          // remedial round after the main set (see `roundMistakes`), while the
          // FSRS/Mistakes Inbox side-effect below schedules the long-term review.
          set((prev) => ({
            wrongCount: prev.wrongCount + 1,
            submitting: true,
            error: null,
            // Missed questions seed a single one-shot remedial round. Only
            // the main pass feeds it — a miss during the remedial round is
            // final and never re-queued (no infinite retry loop).
            roundMistakes:
              prev.inRemedial ||
              !current ||
              prev.roundMistakes.some((item) => item.challengeId === challengeId)
                ? prev.roundMistakes
                : [...prev.roundMistakes, { ...current }],
            sessionMistakes: [
              ...prev.sessionMistakes,
              {
                challengeId,
                nodeId: current?.nodeId ?? "",
                nodeTitle: current?.nodeTitle ?? "Session",
                type: challengeType,
                rating,
                at: Date.now(),
                lapses: 1,
              },
            ],
          }));
        }

        try {
          const userId = state.userId ?? (await get().ensureUser());
          const review = await api.submitReview({
            user_id: userId,
            challenge_id: challengeId,
            rating,
            elapsed_ms: elapsedMs,
            response_json: responseJson,
          });
          const attempt: AttemptResult = {
            challengeId,
            challengeType,
            isCorrect,
            rating,
            xp: review.xp_awarded,
            elapsedMs,
            at: Date.now(),
          };
          set((prev) => ({
            submitting: false,
            lastReview: review,
            lastXpAward: review.xp_awarded,
            gems: review.gems,
            streak: review.streak_count,
            leagueTier: review.league_tier,
            sessionXp: prev.sessionXp + review.xp_awarded,
            xp: Math.max(prev.xp, review.xp_points),
            sessionAttempts: [...prev.sessionAttempts, attempt],
          }));
          return { rating, xp: review.xp_awarded };
        } catch (error) {
          // Offline play: the attempt is queued in localStorage and credited
          // optimistically so a lesson never stalls behind connectivity. The
          // pending reviews are replayed (and the server's XP/gems adopted) as
          // soon as the connection returns.
          if (isOffline() || (error instanceof ApiError && error.status === 0)) {
            enqueueReview({
              user_id: state.userId ?? "",
              challenge_id: challengeId,
              rating,
              elapsed_ms: elapsedMs,
              response_json: responseJson,
            });
            const estimate = XP_BY_RATING_FALLBACK[rating] ?? 0;
            const attempt: AttemptResult = {
              challengeId,
              challengeType,
              isCorrect,
              rating,
              xp: estimate,
              elapsedMs,
              at: Date.now(),
            };
            set((prev) => ({
              submitting: false,
              error: null,
              offlinePending: prev.offlinePending + 1,
              sessionXp: prev.sessionXp + estimate,
              xp: prev.xp + estimate,
              sessionAttempts: [...prev.sessionAttempts, attempt],
            }));
            return { rating, xp: estimate, offline: true };
          }
          set({ submitting: false, error: messageOf(error) });
          return null;
        }
      },

      /** Replay every queued offline review; returns how many were credited. */
      syncOfflineReviews: async () => {
        const { userId } = get();
        if (!userId) return 0;
        const sent = await flushReviewQueue((submission) => api.submitReview(submission));
        if (sent > 0) {
          set({ offlinePending: 0 });
          await get().refreshStats();
        }
        return sent;
      },

      /**
       * End-of-session self-assessment: the four FSRS buttons re-grade the last
       * card of the session. FSRS handles a re-grade natively (it recomputes
       * stability from the card's current state), so this is a real scheduling
       * decision, not a cosmetic one.
       */
      submitSessionRating: async (rating) => {
        const { queue, userId, ensureUser, lastReview } = get();
        const target =
          lastReview?.challenge_id ?? queue[queue.length - 1]?.challengeId ?? null;
        if (!target) {
          set({ error: "no challenge to grade" });
          return null;
        }
        set({ submitting: true, error: null });
        try {
          const id = userId ?? (await ensureUser());
          const review = await api.submitReview({
            user_id: id,
            challenge_id: target,
            rating,
            elapsed_ms: 0,
            response_json: { source: "session_modal" },
          });
          playSfx(review.rating >= 3 ? "fanfare" : "whoosh");
          set((prev) => ({
            submitting: false,
            lastReview: review,
            lastXpAward: review.xp_awarded,
            gems: review.gems,
            streak: review.streak_count,
            leagueTier: review.league_tier,
            sessionXp: prev.sessionXp + review.xp_awarded,
            xp: Math.max(prev.xp, review.xp_points),
          }));
          return review;
        } catch (error) {
          set({ submitting: false, error: messageOf(error) });
          return null;
        }
      },

      addXp: (amount) => set((prev) => ({ xp: prev.xp + amount, sessionXp: prev.sessionXp + amount })),

      nextChallenge: () => {
        const { index, queue, inRemedial, roundMistakes } = get();
        if (index < queue.length - 1) {
          playSfx("whoosh");
          set({ index: index + 1 });
          return;
        }
        // End of the main set: open the ONE-SHOT remedial round with every
        // missed question, exactly once. Misses during it never extend it.
        if (!inRemedial && roundMistakes.length > 0) {
          playSfx("whoosh");
          set({
            queue: [...queue, ...roundMistakes.map((item) => ({ ...item }))],
            inRemedial: true,
            index: index + 1,
          });
          return;
        }
        get().finishSession();
      },

      prevChallenge: () => {
        const { index } = get();
        if (index > 0) set({ index: index - 1 });
      },

      finishSession: () => {
        const { activeNodeId, wrongCount, completedNodeIds } = get();
        playSfx("fanfare");
        set((prev) => ({
          finishedAt: Date.now(),
          sessionXp: prev.sessionXp + (wrongCount === 0 ? XP_PERFECT_SESSION_BONUS : 0),
          completedNodeIds:
            activeNodeId && !completedNodeIds.includes(activeNodeId)
              ? [...completedNodeIds, activeNodeId]
              : completedNodeIds,
        }));
        // Pull server-authoritative gems/XP (passive gem regen included).
        void get().refreshStats();
      },

      resetSession: () =>
        set({
          queue: [],
          index: 0,
          clearedChallengeIds: [],
          sessionXp: 0,
          sessionAttempts: [],
          sessionMistakes: [],
          correctCount: 0,
          wrongCount: 0,
          roundMistakes: [],
          inRemedial: false,
          activeNodeId: null,
          activeNodeTitle: "",
          startedAt: null,
          finishedAt: null,
          lastReview: null,
          lastXpAward: 0,
        }),

      toggleSound: () =>
        set((prev) => {
          const enabled = !prev.soundEnabled;
          if (enabled) playSfx("tap");
          return { soundEnabled: enabled };
        }),

      /** Flip light/dark and sync <html data-theme> (theme.css reacts). */
      toggleTheme: () =>
        set((prev) => {
          const theme: ThemeMode = prev.theme === "dark" ? "light" : "dark";
          applyThemeToDom(theme);
          return { theme };
        }),

      setTheme: (theme: ThemeMode) => {
        applyThemeToDom(theme);
        set({ theme });
      },

      clearError: () => set({ error: null }),
      };
    },
    {
      name: "campuslingo-progress-v2",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        userId: state.userId,
        email: state.email,
        xp: state.xp,
        gems: state.gems,
        streak: state.streak,
        leagueTier: state.leagueTier,
        soundEnabled: state.soundEnabled,
        theme: state.theme,
        completedNodeIds: state.completedNodeIds,
      }),
    },
  ),
);

export function useCurrentChallenge(): SessionQueueItem | null {
  return useQuizStore((state) => state.queue[state.index] ?? null);
}

/**
 * Progress = cleared UNIQUE challenges / total UNIQUE challenges. Recycled
 * mistake clones share their original's id, so re-attempting them neither
 * inflates the numerator nor the denominator.
 */
export function useSessionProgress(): number {
  return useQuizStore((state) => {
    const totalUnique = new Set(state.queue.map((item) => item.challengeId)).size;
    if (totalUnique === 0) return 0;
    return (state.clearedChallengeIds.length / totalUnique) * 100;
  });
}
