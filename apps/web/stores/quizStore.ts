"use client";

/**
 * Global gamification store: hearts, XP, streak, Mistakes Inbox and the
 * synthesized sound engine, plus the review plumbing that talks to
 * ``/api/reviews`` (FSRS-4.5 lives on the server; this store only mirrors it).
 *
 * Persistence: zustand/persist -> localStorage (progress + heart timestamp, so
 * a reload does not hand back free lives). Spaced-repetition truth stays in
 * PostgreSQL.
 */

import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import {
  ApiError,
  api,
  ratingFromResult,
  submitCardReview,
  type ChallengePayload,
  type NodeChallengesResponse,
  type QueueItem,
  type ReviewResponse,
} from "@/lib/api";

export const MAX_HEARTS = 5;
export const HEART_REGEN_MS = 30 * 60 * 1000; // one heart every 30 minutes
export const HEART_REFILL_COST = 350;
export const XP_PER_CORRECT = 10;
export const XP_PERFECT_SESSION_BONUS = 20;

export type SfxName =
  | "tap"
  | "correct"
  | "incorrect"
  | "fanfare"
  | "heartLost"
  | "whoosh";

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
  hearts: number;
  heartsUpdatedAt: number;
  soundEnabled: boolean;

  // session
  queue: SessionQueueItem[];
  index: number;
  sessionXp: number;
  sessionAttempts: AttemptResult[];
  correctCount: number;
  wrongCount: number;
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

  // transport
  loading: boolean;
  submitting: boolean;
  error: string | null;

  // actions
  ensureUser: (email?: string) => Promise<string>;
  refreshStats: () => Promise<void>;
  refreshHearts: () => void;
  loadQueue: (limit?: number) => Promise<void>;
  loadMistakes: () => Promise<void>;
  startNodeSession: (node: NodeChallengesResponse & { title?: string }) => void;
  startQueueSession: (items: QueueItem[]) => void;
  recordAttempt: (input: {
    challengeId: string;
    challengeType: string;
    isCorrect: boolean;
    elapsedMs: number;
    hintsUsed?: number;
    responseJson?: unknown;
  }) => Promise<{ rating: number; xp: number } | null>;
  submitSessionRating: (rating: 1 | 2 | 3 | 4) => Promise<ReviewResponse | null>;
  loseHeart: () => void;
  addXp: (amount: number) => void;
  nextChallenge: () => void;
  prevChallenge: () => void;
  finishSession: () => void;
  resetSession: () => void;
  refillHearts: () => Promise<void>;
  grantHearts: (count?: number) => void;
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
    case "heartLost":
      tone({ freq: 392, duration: 0.18, type: "triangle", gain: 0.1, sweepTo: 196 });
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

function regenerateHearts(
  hearts: number,
  heartsUpdatedAt: number,
): { hearts: number; heartsUpdatedAt: number } {
  if (hearts >= MAX_HEARTS) return { hearts: MAX_HEARTS, heartsUpdatedAt: Date.now() };
  const now = Date.now();
  const earned = Math.floor((now - heartsUpdatedAt) / HEART_REGEN_MS);
  if (earned <= 0) return { hearts, heartsUpdatedAt };
  const nextHearts = Math.min(MAX_HEARTS, hearts + earned);
  return {
    hearts: nextHearts,
    heartsUpdatedAt: nextHearts >= MAX_HEARTS ? now : heartsUpdatedAt + earned * HEART_REGEN_MS,
  };
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
    (set, get) => ({
      userId: null,
      email: "",
      xp: 0,
      gems: 500,
      streak: 0,
      leagueTier: 0,
      hearts: MAX_HEARTS,
      heartsUpdatedAt: Date.now(),
      soundEnabled: true,

      queue: [],
      index: 0,
      sessionXp: 0,
      sessionAttempts: [],
      correctCount: 0,
      wrongCount: 0,
      activeNodeId: null,
      activeNodeTitle: "",
      startedAt: null,
      finishedAt: null,
      lastReview: null,
      lastXpAward: 0,
      completedNodeIds: [],

      mistakes: [],
      sessionMistakes: [],

      loading: false,
      submitting: false,
      error: null,

      // -- identity --------------------------------------------------------
      ensureUser: async (email) => {
        const current = get().userId;
        const targetEmail = (email ?? get().email).trim().toLowerCase();
        if (!targetEmail) {
          throw new Error("email required");
        }
        if (current && targetEmail === get().email) return current;
        set({ loading: true, error: null });
        try {
          const user = await api.upsertUser(targetEmail);
          set({
            userId: user.id,
            email: user.email,
            xp: user.xp_points,
            gems: user.gems,
            streak: user.streak_count,
            leagueTier: user.league_tier,
            loading: false,
          });
          return user.id;
        } catch (error) {
          set({ loading: false, error: messageOf(error) });
          throw error;
        }
      },

      refreshStats: async () => {
        const userId = get().userId;
        if (!userId) return;
        try {
          const stats = await api.stats(userId);
          set({
            xp: stats.xp_points,
            gems: stats.gems,
            streak: stats.streak_count,
            leagueTier: stats.league_tier,
          });
        } catch (error) {
          set({ error: messageOf(error) });
        }
      },

      refreshHearts: () => {
        const { hearts, heartsUpdatedAt } = get();
        const next = regenerateHearts(hearts, heartsUpdatedAt);
        if (next.hearts !== hearts || next.heartsUpdatedAt !== heartsUpdatedAt) set(next);
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

      startNodeSession: (node) => {
        const title =
          "node_title" in node && node.node_title
            ? node.node_title
            : (node as { title?: string }).title ?? "Lesson";
        set({
          queue: node.challenges.map((challenge) => ({
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
          sessionXp: 0,
          correctCount: 0,
          wrongCount: 0,
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
          sessionXp: 0,
          correctCount: 0,
          wrongCount: 0,
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
            sessionXp: prev.sessionXp + XP_PER_CORRECT,
            xp: prev.xp + XP_PER_CORRECT,
            submitting: true,
            error: null,
          }));
        } else {
          playSfx("heartLost");
          const heartsNow = Math.max(0, state.hearts - 1);
          set((prev) => ({
            wrongCount: prev.wrongCount + 1,
            hearts: heartsNow,
            heartsUpdatedAt: heartsNow === MAX_HEARTS ? Date.now() : prev.heartsUpdatedAt,
            submitting: true,
            error: null,
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
          const review = await submitCardReview({
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
          set({ submitting: false, error: messageOf(error) });
          return null;
        }
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
          const review = await submitCardReview({
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

      loseHeart: () => {
        const heartsNow = Math.max(0, get().hearts - 1);
        playSfx("heartLost");
        set({ hearts: heartsNow });
      },

      addXp: (amount) => set((prev) => ({ xp: prev.xp + amount, sessionXp: prev.sessionXp + amount })),

      nextChallenge: () => {
        const { index, queue } = get();
        if (index < queue.length - 1) {
          playSfx("whoosh");
          set({ index: index + 1 });
        } else {
          get().finishSession();
        }
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
      },

      resetSession: () =>
        set({
          queue: [],
          index: 0,
          sessionXp: 0,
          sessionAttempts: [],
          sessionMistakes: [],
          correctCount: 0,
          wrongCount: 0,
          activeNodeId: null,
          activeNodeTitle: "",
          startedAt: null,
          finishedAt: null,
          lastReview: null,
          lastXpAward: 0,
        }),

      // -- economy ---------------------------------------------------------
      refillHearts: async () => {
        const { userId, gems } = get();
        if (!userId) {
          set({ error: "sign in first" });
          return;
        }
        if (gems < HEART_REFILL_COST) {
          set({ error: `refill costs ${HEART_REFILL_COST} gems, you have ${gems}` });
          return;
        }
        try {
          const result = await api.refillHearts(userId);
          set({ hearts: result.hearts, gems: result.gems, error: null });
          playSfx("fanfare");
        } catch (error) {
          set({ error: messageOf(error) });
        }
      },

      grantHearts: (count = 1) =>
        set((prev) => {
          const hearts = Math.min(MAX_HEARTS, prev.hearts + count);
          return {
            hearts,
            heartsUpdatedAt: hearts >= MAX_HEARTS ? Date.now() : prev.heartsUpdatedAt,
          };
        }),

      toggleSound: () =>
        set((prev) => {
          const enabled = !prev.soundEnabled;
          if (enabled) playSfx("tap");
          return { soundEnabled: enabled };
        }),

      clearError: () => set({ error: null }),
    }),
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
        hearts: state.hearts,
        heartsUpdatedAt: state.heartsUpdatedAt,
        soundEnabled: state.soundEnabled,
        completedNodeIds: state.completedNodeIds,
      }),
    },
  ),
);

export function useCurrentChallenge(): SessionQueueItem | null {
  return useQuizStore((state) => state.queue[state.index] ?? null);
}

export function useSessionProgress(): number {
  return useQuizStore((state) =>
    state.queue.length === 0 ? 0 : (state.index / state.queue.length) * 100,
  );
}
