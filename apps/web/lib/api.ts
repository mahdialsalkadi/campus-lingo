/**
 * Typed client for the CampusLingo core-ai service.
 * Field names mirror services/core-ai/schema.py one-to-one.
 */

import { cacheNode, getCachedNode } from "@/lib/offline";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

/** Generic model id: llama-server serves whatever GGUF run.sh loaded. */
export const DEFAULT_MODEL = "default";

/**
 * Qwen 3.5 model tiers, each served by its own llama-server on a fixed port
 * (800M -> 8081 ... 9B -> 8084). GET /health reports per-tier availability
 * under llm.tiers; uploads carry the chosen tier as the model_tier form field.
 */
export const MODEL_TIERS = [
  {
    id: "800M",
    name: "Qwen 3.5 0.8B",
    label: "Ultra Fast",
    description: "High concurrency, instant quizzes",
    port: 8081,
  },
  {
    id: "2B",
    name: "Qwen 3.5 2B",
    label: "Balanced",
    description: "Fast generation, lightweight",
    port: 8082,
  },
  {
    id: "4B",
    name: "Qwen 3.5 4B (DeltaNet)",
    label: "Sweet Spot",
    description: "DeltaNet 3:1, high accuracy",
    port: 8083,
  },
  {
    id: "9B",
    name: "Qwen 3.5 9B",
    label: "Deep Reasoning",
    description: "Heavy academic lectures",
    port: 8084,
  },
] as const;

export type ModelTierId = (typeof MODEL_TIERS)[number]["id"];

export const DEFAULT_MODEL_TIER: ModelTierId = "4B";

export type ChallengeType =
  | "MATCH_MADNESS"
  | "SPOT_MISTAKE"
  | "CONTEXTUAL_CLOZE"
  | "MCQ_QUIZ"
  | "TRUE_FALSE"
  | "SWIPE_BOOLEAN"
  | "SPOT_IMPOSTER"
  | "TAP_TO_FILL"
  | "BOSS_FIGHT"
  | "DIAGRAM_QA";

/**
 * The generation surface: the four classic high-yield variants PLUS the
 * micro-dopamine games built for short attention spans. The retired chat
 * types (INTERACTIVE_CHAT / DIALOGUE) are not part of the type at all, and
 * TRUE_FALSE is legacy / render-only.
 */
export const GENERATED_CHALLENGE_TYPES: ChallengeType[] = [
  "MATCH_MADNESS",
  "SPOT_MISTAKE",
  "CONTEXTUAL_CLOZE",
  "MCQ_QUIZ",
  "SWIPE_BOOLEAN",
  "SPOT_IMPOSTER",
  "TAP_TO_FILL",
];

/** Every renderable type (adds the figure-based and legacy kinds). */
export const CHALLENGE_TYPES: ChallengeType[] = [
  ...GENERATED_CHALLENGE_TYPES,
  "TRUE_FALSE",
  "DIAGRAM_QA",
];

export const CHALLENGE_LABELS: Record<ChallengeType, { en: string; ar: string }> = {
  MATCH_MADNESS: { en: "Match Madness", ar: "مطابقة سريعة" },
  SPOT_MISTAKE: { en: "Spot the Mistake", ar: "اكتشف الخطأ" },
  CONTEXTUAL_CLOZE: { en: "Contextual Cloze", ar: "أكمل الفراغ" },
  MCQ_QUIZ: { en: "Multiple Choice", ar: "اختيار من متعدد" },
  TRUE_FALSE: { en: "True / False", ar: "صح أم خطأ" },
  SWIPE_BOOLEAN: { en: "Swipe Truth", ar: "اسحب: صح أم خطأ" },
  SPOT_IMPOSTER: { en: "Spot the Imposter", ar: "اكتشف الدخيل" },
  TAP_TO_FILL: { en: "Tap to Fill", ar: "اضغط لتكملة" },
  BOSS_FIGHT: { en: "Boss Fight", ar: "معركة الزعيم" },
  DIAGRAM_QA: { en: "Diagram Comprehension", ar: "فهم الرسم" },
};

/** Short icon per challenge type (emoji keeps it dependency-free). */
export const CHALLENGE_ICONS: Record<ChallengeType, string> = {
  MATCH_MADNESS: "🧩",
  SPOT_MISTAKE: "🔍",
  CONTEXTUAL_CLOZE: "✏️",
  MCQ_QUIZ: "📝",
  TRUE_FALSE: "⚖️",
  SWIPE_BOOLEAN: "👉",
  SPOT_IMPOSTER: "🕵️",
  TAP_TO_FILL: "👆",
  BOSS_FIGHT: "🐲",
  DIAGRAM_QA: "🖼️",
};

/** League display names by tier index (shared by dashboard + profile). */
export const LEAGUE_NAMES_FALLBACK = [
  "Bronze",
  "Silver",
  "Gold",
  "Sapphire",
  "Ruby",
  "Emerald",
  "Amethyst",
  "Pearl",
  "Obsidian",
] as const;

/** Rating scale, shared by the FSRS scheduler and the end-of-session modal. */
export const RATINGS = [
  { value: 1, en: "Again", ar: "أعِد", color: "#FF4B4B" },
  { value: 2, en: "Hard", ar: "صعب", color: "#FF9600" },
  { value: 3, en: "Good", ar: "جيد", color: "#1CB0F6" },
  { value: 4, en: "Easy", ar: "سهل", color: "#58CC02" },
] as const;

// --- challenge payloads -----------------------------------------------------

export interface MatchPair {
  term: string;
  definition: string;
}

export interface MatchMadnessPayload {
  type: "MATCH_MADNESS";
  instructions: string;
  pairs: MatchPair[];
  seconds: number;
  one_liner_rationale?: string;
}

export interface SpotMistakePayload {
  type: "SPOT_MISTAKE";
  instructions: string;
  code_or_text: string;
  mistake_token: string;
  explanation: string;
  one_liner_rationale?: string;
}

export interface ContextualClozePayload {
  type: "CONTEXTUAL_CLOZE";
  instructions: string;
  sentence_with_blank: string;
  correct_answer: string;
  distractors: string[];
  explanation: string;
  one_liner_rationale?: string;
}

export interface McqOption {
  text: string;
  correct: boolean;
}

export interface McqQuizPayload {
  type: "MCQ_QUIZ";
  instructions: string;
  question: string;
  options: McqOption[];
  explanation: string;
  one_liner_rationale?: string;
}

export interface TrueFalsePayload {
  type: "TRUE_FALSE";
  instructions: string;
  statement: string;
  answer: boolean;
  explanation: string;
  one_liner_rationale?: string;
}

/** "Tinder for study": swipe right = true, left = false (3-second loop). */
export interface SwipeBooleanPayload {
  type: "SWIPE_BOOLEAN";
  instructions: string;
  statement: string;
  answer: boolean;
  one_liner_rationale: string;
}

export interface ImposterStatement {
  text: string;
  is_imposter: boolean;
}

/** One false statement hiding among three truths. */
export interface SpotImposterPayload {
  type: "SPOT_IMPOSTER";
  instructions: string;
  statements: ImposterStatement[];
  one_liner_rationale: string;
}

/** One-touch cloze: one blank, three pills at the bottom. */
export interface TapToFillPayload {
  type: "TAP_TO_FILL";
  instructions: string;
  sentence_with_blank: string;
  correct_answer: string;
  distractors: string[];
  one_liner_rationale: string;
}

export interface BossQuestion {
  question: string;
  options: McqOption[];
}

/** Chapter culmination: rapid-fire questions vs a depleting HP bar. */
export interface BossFightPayload {
  type: "BOSS_FIGHT";
  instructions: string;
  boss_name: string;
  boss_emoji?: string;
  seconds_per_question: number;
  questions: BossQuestion[];
  one_liner_rationale?: string;
}

export type DiagramFocus =
  | "component_identification"
  | "data_flow"
  | "graph_trend"
  | "comparison";

/** One comprehension question about an extracted slide figure. */
export interface DiagramQuestion {
  question: string;
  options: McqOption[];
  focus: DiagramFocus;
}

export interface DiagramQaPayload {
  type: "DIAGRAM_QA";
  instructions: string;
  /** Public URL of the extracted figure (/static/uploads/diagrams/...). */
  image_url: string;
  /** Server-side key of the figure (page-N-slot). */
  diagram_ref: string;
  caption: string;
  questions: DiagramQuestion[];
  explanation: string;
  one_liner_rationale?: string;
}

export type ChallengePayload =
  | MatchMadnessPayload
  | SpotMistakePayload
  | ContextualClozePayload
  | McqQuizPayload
  | TrueFalsePayload
  | SwipeBooleanPayload
  | SpotImposterPayload
  | TapToFillPayload
  | BossFightPayload
  | DiagramQaPayload;

export interface Challenge {
  id: string;
  node_id: string;
  type: ChallengeType;
  payload_json: ChallengePayload;
  difficulty: number;
  /** Extracted assets referenced by the payload (e.g. the diagram URL). */
  assets?: Record<string, unknown>;
}

export interface LearningUnitBundle {
  unit_id: string;
  concept_title: string;
  difficulty_level: number;
  challenges: ChallengePayload[];
}

export interface NodeChallengesResponse {
  node_id: string;
  node_title: string;
  lecture_id: string;
  order_index: number;
  count: number;
  challenges: Challenge[];
}

export interface NodeDetail extends NodeChallengesResponse {
  content_summary: string;
  next_nodes: { id: string; title: string; order_index: number }[];
}

export interface NodeSummary {
  id: string;
  lecture_id: string;
  lecture_title: string;
  title: string;
  content_summary: string;
  order_index: number;
  challenge_count: number;
}

/** One uploaded PDF = one lecture group on the pathway map. */
export interface LectureGroup {
  lectureId: string;
  lectureTitle: string;
  nodes: { node: NodeSummary; index: number }[];
}

/**
 * Group flat node rows by their parent lecture (one PDF) preserving order.
 * The dashboard renders one chapter block per lecture with its nodes inside.
 */
export function groupNodesByLecture(nodes: NodeSummary[]): LectureGroup[] {
  const groups: LectureGroup[] = [];
  const byId = new Map<string, LectureGroup>();
  for (const node of nodes) {
    let group = byId.get(node.lecture_id);
    if (!group) {
      group = { lectureId: node.lecture_id, lectureTitle: node.lecture_title, nodes: [] };
      byId.set(node.lecture_id, group);
      groups.push(group);
    }
    group.nodes.push({ node, index: 0 });
  }
  // Re-number within each group so the pathway badge matches the visible order.
  for (const group of groups) {
    group.nodes.forEach((entry, index) => {
      entry.index = index;
    });
  }
  return groups;
}

export interface CourseSummary {
  id: string;
  name: string;
  lms_type: string;
  created_at: string;
  /** Formatted share code (CL-XXXXXX) — null until the owner shares the course. */
  share_code?: string | null;
  lectures: {
    id: string;
    title: string;
    status: string;
    total_chunks: number;
    created_at: string;
  }[];
}

/** Body of POST /api/courses (mirrors services/core-ai/main.py create_course). */
export interface CreateCourseResponse {
  id: string;
  user_id: string;
  name: string;
  lms_type: string;
  share_code: string;
  lectures: { id: string; title: string; status: string }[];
}

/** Body of GET /api/courses/{course_id}/share-code. */
export interface ShareCodeResponse {
  course_id: string;
  share_code: string;
}

/** Body of POST /api/courses/import (deep-clone of a shared course). */
export interface ImportCourseResponse {
  course_id: string;
  name: string;
  source_course_id: string;
  lectures_cloned: number;
  nodes_cloned: number;
  challenges_cloned: number;
  cards_seeded: number;
}

// --- Ebbinghaus Daily Memory Warm-up (spaced repetition ritual) -------------

/** Pedagogical buckets of the daily review queue (mirrors services/core-ai). */
export type ReviewBucket = "yesterday" | "midterm" | "critical";

/** One due card in GET /api/reviews/daily-queue (with its renderable challenge). */
export interface DailyQueueCard {
  card_id: string;
  challenge_id: string;
  user_id: string;
  last_reviewed_at: string | null;
  due_at: string | null;
  repetition_level: number;
  retrievability_score: number;
  stability: number;
  difficulty: number;
  state: number;
  reps: number;
  lapses: number;
  bucket: ReviewBucket;
  bucket_label: string;
  /** Enriched renderable challenge (joined server-side when available). */
  node_id?: string;
  node_title?: string;
  lecture_id?: string;
  type?: ChallengeType;
  payload_json?: ChallengePayload;
}

/** Body of GET /api/reviews/daily-queue — the Daily Memory Warm-up envelope. */
export interface DailyQueueResponse {
  user_id: string;
  date: string;
  total_due_count: number;
  estimated_minutes: number;
  yesterday_count: number;
  midterm_count: number;
  critical_count: number;
  streak_saver: boolean;
  xp_bonus_multiplier: number;
  cards: DailyQueueCard[];
}

/** Body of GET /api/lectures/{lecture_id}/memory-health — the folder meter. */
export interface LectureMemoryHealth {
  lecture_id: string;
  user_id: string;
  total: number;
  due: number;
  fresh: number;
  decaying: number;
  critical: number;
  /** 0..1 — mean live recall probability of the chapter's cards. */
  score: number;
  /** green (90-100%) / amber (60-89%) / red (<60%). */
  band: "green" | "amber" | "red";
}

export interface ProcessLectureResponse {
  lecture_id: string;
  node: Record<string, unknown>;
  challenges: Challenge[];
  cards_seeded: number;
  embeddings_stored: number;
  challenges_by_type: Record<string, number>;
  generation_errors: string[];
  units?: { chunk_index: number; lecture_id?: string; title?: string; challenges?: number }[];
  elapsed_ms: number;
}

/** Body of POST /api/process-lecture-async and GET /api/ingest-jobs/{id}. */
export interface IngestJobState {
  job_id: string;
  status:
    | "queued"
    | "parsing"
    | "generating"
    | "partially_ready"
    | "saving"
    | "done"
    | "error";
  model_tier?: string;
  total_chunks: number;
  done_chunks: number;
  failed_chunks: number;
  progress: number;
  error: string | null;
  lecture_id: string | null;
  units: {
    chunk_index: number;
    lecture_id?: string;
    title?: string;
    challenges?: number;
    node?: Record<string, unknown>;
    difficulty_level?: number;
  }[];
  generation_errors: string[];
  challenges_by_type: Record<string, number>;
  cards_seeded: number;
  embeddings_stored: number;
  elapsed_ms: number;
  /** Progressive unlocking: node ids already playable while later parts generate. */
  unlocked_nodes?: string[];
}

/** Shape returned by /api/auth/register and /api/auth/login. */
export interface AuthUserResponse {
  id: string;
  email: string;
  xp_points: number;
  gems: number;
  streak_count: number;
  league_tier: number;
  created: boolean;
}

export interface QueueItem {
  card_id: string;
  challenge_id: string;
  node_id: string;
  node_title: string;
  lecture_id: string;
  type: ChallengeType;
  payload_json: ChallengePayload;
  state: number;
  stability: number;
  difficulty: number;
  due_date: string;
  retrievability: number | null;
  is_mistake: boolean;
  lapses: number;
}

export interface UserStats {
  user_id: string;
  email: string;
  xp_points: number;
  gems: number;
  streak_count: number;
  league_tier: number;
  league_name?: string;
  weekly_xp?: number;
  streak_freeze_active?: boolean;
  inventory?: Record<string, number | string>;
  cards_total: number;
  cards_due: number;
  cards_relearning: number;
  accuracy_last_50: number;
}

export interface ReviewSubmission {
  user_id: string;
  challenge_id: string;
  rating: 1 | 2 | 3 | 4;
  elapsed_ms?: number;
  response_json?: unknown;
  /** "warmup" = Daily Memory Warm-up ritual (earns the 1.5x XP bonus). */
  context?: "warmup" | "lesson";
}

export interface ReviewResponse {
  challenge_id: string;
  user_id: string;
  rating: number;
  state: number;
  state_name: string;
  stability: number;
  difficulty: number;
  retrievability: number | null;
  due_date: string;
  interval_days: number;
  reps: number;
  lapses: number;
  xp_awarded: number;
  hearts_lost: number;
  streak_count: number;
  gems: number;
  xp_points: number;
  league_tier: number;
  weekly_xp?: number;
  streak_freeze_active?: boolean;
}

export interface FsrsPreview {
  challenge_id: string;
  current_state: string;
  current_retrievability: number | null;
  options: Record<
    string,
    {
      interval_days: number;
      due_date: string;
      state: number;
      state_name: string;
      stability: number;
      difficulty: number;
    }
  >;
}

// --- error type -------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// --- core request -----------------------------------------------------------

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 1_800_000,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...(init.headers ?? {}),
      },
    });

    if (response.status === 204) return undefined as T;

    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    }

    if (!response.ok) {
      const detail =
        parsed && typeof parsed === "object" && "detail" in (parsed as object)
          ? (parsed as { detail: unknown }).detail
          : parsed;
      throw new ApiError(
        typeof detail === "string" ? detail : `request failed (${response.status})`,
        response.status,
        detail,
      );
    }
    return parsed as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("request timed out", 408);
    }
    throw new ApiError(error instanceof Error ? error.message : "network error", 0);
  } finally {
    clearTimeout(timer);
  }
}

// --- endpoints --------------------------------------------------------------

/**
 * How long an upload request may take before the client aborts it. Large
 * slide decks (80+ MB) take a while to stream over a slow link, and aborting
 * the request mid-flight is what surfaced as an opaque "NetworkError" toast.
 */
export const UPLOAD_TIMEOUT_MS = 120_000;

/**
 * Upload a lecture PDF for parsing + Qwen generation + persistence.
 * `formData` must contain: file (the PDF) and course_id.
 */
export function uploadLecture(formData: FormData) {
  return request<ProcessLectureResponse>(
    "/api/process-lecture",
    {
      method: "POST",
      body: formData,
    },
    UPLOAD_TIMEOUT_MS,
  );
}

/**
 * Start the same ingestion pipeline as a background job; resolves as soon as
 * the server accepted the PDF (HTTP 202) with the initial job state.
 */
export function startIngestion(formData: FormData) {
  return request<IngestJobState>(
    "/api/process-lecture-async",
    {
      method: "POST",
      body: formData,
    },
    UPLOAD_TIMEOUT_MS,
  );
}

/** Poll one ingestion job. Terminal states: "done" | "error". */
export function pollIngestionJob(jobId: string) {
  return request<IngestJobState>(`/api/ingest-jobs/${jobId}`);
}

/**
 * Poll an ingestion job until it finishes, reporting progress after every
 * tick. Resolves with the final state; throws on "error" or if polling is
 * aborted (timeout). `onProgress` also fires for the terminal state.
 */
export async function awaitIngestion(
  jobId: string,
  onProgress?: (state: IngestJobState) => void,
  pollMs = 1500,
): Promise<IngestJobState> {
  for (;;) {
    const state = await pollIngestionJob(jobId);
    onProgress?.(state);
    if (state.status === "done" || state.status === "error") {
      if (state.status === "error") throw new ApiError(state.error || "ingestion failed", 502);
      return state;
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
}

/**
 * Every challenge belonging to one learning node.
 *
 * Falls back to the IndexedDB copy when the API is unreachable (airplane
 * mode). A node that was explicitly saved offline is refreshed on every
 * successful fetch, so an online session keeps its offline copy current.
 */
export async function getNodeChallenges(nodeId: string): Promise<NodeChallengesResponse> {
  try {
    const response = await request<NodeChallengesResponse>(
      `/api/nodes/${nodeId}/challenges`,
      {},
      30_000,
    );
    if (await getCachedNode(nodeId)) await cacheNode(nodeId, response);
    return response;
  } catch (error) {
    const cached = await getCachedNode(nodeId);
    if (cached) return cached;
    throw error;
  }
}

// --- enterprise features: tutor, arena, shop, leagues -----------------------

/** Payload for POST /api/tutor/hint (streamed with SSE). */
export interface TutorHintRequest {
  challenge_id?: string;
  question: string;
  student_choice?: string;
  context?: string;
  user_id?: string;
}

/** One parsed event from the tutor's SSE stream. */
export interface TutorHintEvent {
  type: "open" | "delta" | "done" | "error";
  text?: string;
  tier?: string;
  cost?: number;
  message?: string;
}

/**
 * Stream a Socratic hint (Tier 1 / 0.8B, temperature 0.3) token by token.
 *
 * Uses `fetch` + a `ReadableStream` reader rather than `EventSource` because
 * the endpoint is a POST with a JSON body. Resolves once the stream closes;
 * `signal` aborts mid-stream when the drawer is dismissed.
 */
export async function streamTutorHint(
  payload: TutorHintRequest,
  onEvent: (event: TutorHintEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/tutor/hint`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `tutor request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, response.status);
  }
  if (!response.body) throw new ApiError("tutor stream unavailable", 0);

  /** Decode one SSE frame block into typed events (shared by both loops). */
  const handleFrame = (frame: string) => {
    for (const line of frame.split("\n")) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        onEvent(JSON.parse(data) as TutorHintEvent);
      } catch {
        /* ignore malformed frame */
      }
    }
  };

  // Defensive fallback: a proxy or a non-streaming deployment can collapse the
  // SSE response into a single JSON body ({"hint": "..."}). Parse that
  // directly instead of waiting for `data:` frames that never arrive — the
  // drawer's 8s watchdog would otherwise fire on a perfectly good hint.
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("text/event-stream")) {
    const body = (await response.json().catch(() => null)) as
      | { hint?: string; text?: string; message?: string }
      | null;
    const text = body?.hint ?? body?.text ?? body?.message;
    if (text) onEvent({ type: "delta", text: String(text) });
    onEvent({ type: "done" });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Tolerate both LF and CRLF frame separators.
    buffer = buffer.replace(/\r\n/g, "\n");
    // SSE frames are separated by a blank line.
    let split = buffer.indexOf("\n\n");
    while (split !== -1) {
      handleFrame(buffer.slice(0, split));
      buffer = buffer.slice(split + 2);
      split = buffer.indexOf("\n\n");
    }
  }
  // Flush a final frame that arrived without its closing blank line.
  if (buffer.trim()) handleFrame(buffer);
}

export interface ArenaRoom {
  room_code: string;
  node_id: string;
  node_title: string;
  questions: number;
  question_seconds: number;
  status: string;
}

/** A question as delivered by the arena socket (options pre-shuffled). */
export interface ArenaQuestion {
  type: ChallengeType;
  prompt: string;
  options: string[];
}

export interface ArenaPlayerState {
  player_id: string;
  name: string;
  score: number;
  streak: number;
  answered: boolean;
}

/** Every server -> client arena frame, discriminated by `type`. */
export type ArenaEvent =
  | { type: "welcome"; player_id: string; room_code: string }
  | { type: "start"; total: number }
  | { type: "question"; index: number; total: number; question: ArenaQuestion; seconds: number }
  | {
      type: "state";
      room_code: string;
      status: string;
      index: number;
      total: number;
      seconds_left: number;
      players: ArenaPlayerState[];
    }
  | { type: "answer_ack"; player_id: string; correct: boolean; gained: number; seconds_left: number }
  | { type: "reveal"; index: number; answer_index: number }
  | {
      type: "finish";
      winner: string | null;
      standings: { player_id: string; name: string; score: number }[];
    }
  | { type: "error"; message: string }
  | { type: "pong" };

/** WebSocket URL for a duel room (ws:// or wss:// follows the page protocol). */
export function arenaSocketUrl(roomCode: string, playerId: string, name: string): string {
  const base = API_BASE.replace(/^http/, "ws");
  const params = new URLSearchParams({ player_id: playerId, name });
  return `${base}/ws/arena/${roomCode}?${params.toString()}`;
}

export interface ShopItem {
  id: string;
  name_ar: string;
  name_en: string;
  icon: string;
  price: number;
  description_ar: string;
  effect: "streak_freeze" | "double_xp" | "badge";
  duration_seconds?: number;
}

export interface ShopPurchaseResponse {
  user_id: string;
  item: ShopItem;
  gems: number;
  inventory: Record<string, number | string>;
  streak_freeze_active: boolean;
}

export interface LeaderboardRow {
  rank: number;
  user_id: string;
  email: string;
  weekly_xp: number;
  is_me: boolean;
}

export interface Leaderboard {
  league_tier: number;
  league_name: string;
  league_names: string[];
  weekly_xp: number;
  my_rank: number | null;
  cohort_size: number;
  zone: "promotion" | "retention" | "relegation";
  promotion_zone: number;
  relegation_zone: number;
  week_key: string;
  rollover_at: string;
  rows: LeaderboardRow[];
}

export const api = {
  health: () =>
    request<{
      status: string;
      database: { ok: boolean };
      llm: Record<string, unknown> & {
        selected_tier?: string;
        tiers?: Record<
          string,
          {
            ok: boolean;
            port: number;
            name: string;
            label: string;
            description: string;
            model_path: string;
            parallel_slots: number;
          }
        >;
        tier_catalog?: Record<string, { name: string; label: string; description: string; port: number }>;
      };
    }>(
      "/health",
      {},
      15_000,
    ),

  upsertUser: (email: string, password?: string) =>
    request<{
      id: string;
      email: string;
      xp_points: number;
      gems: number;
      streak_count: number;
      league_tier: number;
      created: boolean;
    }>(
      "/api/users",
      {
        method: "POST",
        body: JSON.stringify(password ? { email, password } : { email }),
      },
      20_000,
    ),

  /** Create a new account with email + password (409 if it already exists). */
  register: (email: string, password: string) =>
    request<AuthUserResponse>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  /** Verify email + password and load the existing account. */
  loginWithPassword: (email: string, password: string) =>
    request<AuthUserResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  stats: (userId: string) =>
    request<UserStats>(`/api/users/${userId}/stats`, {}, 20_000),

  refillHearts: (userId: string) =>
    request<{ hearts: number; gems: number; cost: number }>(
      `/api/users/${userId}/hearts/refill`,
      { method: "POST" },
      20_000,
    ),

  spendGems: (userId: string, amount: number, reason = "spend") =>
    request<{ gems: number; spent: number }>(
      `/api/users/${userId}/gems/spend`,
      { method: "POST", body: JSON.stringify({ amount, reason }) },
      20_000,
    ),

  listCourses: (userId: string) =>
    request<CourseSummary[]>(`/api/courses?user_id=${userId}`, {}, 30_000),

  createCourse: (userId: string, name: string, lmsType = "pdf_upload") =>
    request<CreateCourseResponse>("/api/courses", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, name, lms_type: lmsType }),
    }),

  /** Owner: return (or lazily create) the course's share code. */
  getShareCode: (courseId: string) =>
    request<ShareCodeResponse>(`/api/courses/${courseId}/share-code`, {}, 20_000),

  /** Importer: deep-clone the course behind a share code into their account. */
  importCourse: (userId: string, shareCode: string) =>
    request<ImportCourseResponse>("/api/courses/import", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, share_code: shareCode }),
    }, 60_000),

  courseNodes: (courseId: string) =>
    request<{ course_id: string; name: string; nodes: NodeSummary[] }>(
      `/api/courses/${courseId}/nodes`,
      {},
      30_000,
    ),

  deleteCourse: (courseId: string) =>
    request<void>(`/api/courses/${courseId}`, { method: "DELETE" }),

  deleteLecture: (lectureId: string) =>
    request<void>(`/api/lectures/${lectureId}`, { method: "DELETE" }),

  getNode: (nodeId: string) =>
    request<NodeDetail & { challenges: Challenge[] }>(`/api/nodes/${nodeId}`, {}, 30_000),

  queue: (userId: string, limit = 20, onlyDue = true) =>
    request<{ count: number; items: QueueItem[] }>(
      `/api/queue?user_id=${userId}&limit=${limit}&only_due=${onlyDue}`,
      {},
      30_000,
    ),

  /** Grade one attempt: recalculates DSR server-side and persists the schedule. */
  submitReview(submission: ReviewSubmission) {
    return request<ReviewResponse>("/api/reviews", {
      method: "POST",
      body: JSON.stringify({ ...submission, context: submission.context ?? "lesson" }),
    }, 20_000);
  },

  /**
   * The Daily Memory Warm-up queue: cards overdue or decayed below the 0.85
   * retention threshold, bucketed into Yesterday's Recap / Mid-Term Memory /
   * Critical Decay, with the streak-saver + 1.5x XP envelope for the banner.
   */
  dailyQueue: (userId: string, limit = 50) =>
    request<DailyQueueResponse>(
      `/api/reviews/daily-queue?user_id=${userId}&limit=${limit}`,
      {},
      30_000,
    ),

  /** Chapter-folder "صحة الذاكرة" meter for one lecture's cards. */
  lectureMemoryHealth: (lectureId: string, userId: string) =>
    request<LectureMemoryHealth>(
      `/api/lectures/${lectureId}/memory-health?user_id=${userId}`,
      {},
      20_000,
    ),

  mistakes: (userId: string, limit = 50) =>
    request<{ count: number; items: QueueItem[] }>(
      `/api/mistakes?user_id=${userId}&limit=${limit}`,
      {},
      30_000,
    ),

  fsrsPreview: (challengeId: string, userId: string) =>
    request<FsrsPreview>(`/api/fsrs/preview/${challengeId}?user_id=${userId}`, {}, 20_000),

  // --- battle arena --------------------------------------------------------
  /** Open a duel room from a learning node; returns the 6-char room code. */
  createArenaRoom: (nodeId: string, limit = 8) =>
    request<ArenaRoom>("/api/arena/rooms", {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId, limit }),
    }, 30_000),

  /** Claim the victor's +100 XP / 25 gems (paid once per room, server-side). */
  arenaReward: (roomCode: string, playerId: string) =>
    request<{
      awarded: boolean;
      reason?: string;
      /** Award amounts (100 XP / 25 gems), not balances. */
      xp?: number;
      gems?: number;
      xp_points?: number;
      gems_balance?: number;
      weekly_xp?: number;
      league_tier?: number;
    }>(`/api/arena/rooms/${roomCode}/reward`, {
      method: "POST",
      body: JSON.stringify({ player_id: playerId }),
    }, 20_000),

  arenaRoom: (roomCode: string) =>
    request<{ room_code: string; status: string; total: number; players: ArenaPlayerState[] }>(
      `/api/arena/rooms/${roomCode}`,
      {},
      15_000,
    ),

  // --- gem shop & weekly leagues ------------------------------------------
  shopItems: () => request<{ items: ShopItem[] }>("/api/shop/items", {}, 15_000),

  purchaseItem: (userId: string, itemId: string) =>
    request<ShopPurchaseResponse>(`/api/users/${userId}/shop/purchase`, {
      method: "POST",
      body: JSON.stringify({ item_id: itemId }),
    }, 20_000),

  leaderboard: (userId: string, cohortSize = 30) =>
    request<Leaderboard>(
      `/api/leaderboards/${userId}?cohort_size=${cohortSize}`,
      {},
      20_000,
    ),

  // --- Google-Classroom instructor hub ------------------------------------
  /** Open a classroom; the creator is promoted to `role: instructor`. */
  createClassroom: (
    instructorId: string,
    payload: { name: string; section?: string; description?: string },
  ) =>
    request<ClassroomSummary>("/api/classrooms", {
      method: "POST",
      body: JSON.stringify({
        instructor_id: instructorId,
        name: payload.name,
        section: payload.section ?? "",
        description: payload.description ?? "",
      }),
    }, 30_000),

  /** Classrooms this account teaches plus the ones it is enrolled in. */
  classrooms: (userId: string) =>
    request<{ user_id: string; taught: ClassroomSummary[]; enrolled: ClassroomSummary[] }>(
      `/api/classrooms?user_id=${userId}`,
      {},
      20_000,
    ),

  /** Enroll with the 6-char class code (idempotent). */
  joinClassroom: (code: string, studentId: string) =>
    request<ClassroomSummary & { already_enrolled: boolean }>("/api/classrooms/join", {
      method: "POST",
      body: JSON.stringify({ code, student_id: studentId }),
    }, 30_000),

  classroomRoster: (classId: string, instructorId: string) =>
    request<ClassroomRoster>(
      `/api/classrooms/${classId}/roster?instructor_id=${instructorId}`,
      {},
      30_000,
    ),

  classroomInsights: (classId: string, instructorId: string) =>
    request<ClassroomInsights>(
      `/api/classrooms/${classId}/insights?instructor_id=${instructorId}`,
      {},
      30_000,
    ),

  /** 1-click CSV export URL (opened directly by the browser). */
  classroomGradesCsvUrl: (classId: string, instructorId: string) =>
    `${API_BASE}/api/classrooms/${classId}/grades.csv?instructor_id=${instructorId}`,

  /** SSE endpoint for live chapter-tree updates. */
  classroomUpdatesUrl: (classId: string, studentId?: string) =>
    `${API_BASE}/api/classrooms/${classId}/updates${
      studentId ? `?student_id=${studentId}` : ""
    }`,

  publishClassroomUpdate: (classId: string, instructorId: string, message?: string) =>
    request<{ course_id: string; delivered: number }>(
      `/api/classrooms/${classId}/publish?instructor_id=${instructorId}`,
      { method: "POST", body: JSON.stringify({ type: "classroom_updated", message }) },
      20_000,
    ),

  // --- viral share links + notifications ----------------------------------
  /** Public preview of a shared course (the /join/{code} landing page). */
  previewSharedCourse: (shareCode: string) =>
    request<SharedCoursePreview>(`/api/join/${shareCode}`, {}, 20_000),

  /** Clone a shared peer course instantly (no LLM re-run) — or enroll into
   *  a classroom when the code is an enrollment code. */
  joinSharedCourse: (shareCode: string, userId: string) =>
    request<{
      course_id: string;
      name: string;
      lectures_cloned: number;
      nodes_cloned: number;
      challenges_cloned: number;
      cards_seeded: number;
      source_course_id: string;
      // Classroom-enrollment path (no clone happens).
      joined?: boolean;
      joined_kind?: "classroom" | undefined;
      already_enrolled?: boolean;
    }>(`/api/join/${shareCode}`, {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }, 60_000),

  notifications: (userId: string, limit = 30) =>
    request<NotificationInbox>(
      `/api/users/${userId}/notifications?limit=${limit}`,
      {},
      20_000,
    ),

  markNotificationRead: (notificationId: string) =>
    request<{ id: string; read: boolean }>(`/api/notifications/${notificationId}/read`, {
      method: "POST",
    }, 15_000),

  // --- Web Push ------------------------------------------------------------
  pushPublicKey: () =>
    request<{ public_key: string | null; push_available: boolean; configured: boolean }>(
      "/api/push/public-key",
      {},
      15_000,
    ),

  pushSubscribe: (userId: string, subscription: unknown, userAgent?: string) =>
    request<{ subscription_id: string; user_id: string; push_available: boolean }>(
      "/api/push/subscribe",
      {
        method: "POST",
        body: JSON.stringify({ user_id: userId, subscription, user_agent: userAgent }),
      },
      20_000,
    ),

  pushUnsubscribe: (endpoint: string) =>
    request<{ removed: boolean }>("/api/push/unsubscribe", {
      method: "POST",
      body: JSON.stringify({ endpoint }),
    }, 15_000),

  // --- Exam Crunch ---------------------------------------------------------
  /** Build the cheatsheet + diagnostic timed mock exam for a course. */
  examCrunch: (courseId: string, userId: string, target?: number) =>
    request<ExamCrunchResponse>(
      `/api/courses/${courseId}/exam-crunch`,
      {
        method: "POST",
        body: JSON.stringify({ user_id: userId, target: target ?? null }),
      },
      180_000,
    ),

  examSession: (examId: string, userId: string) =>
    request<ExamSessionResponse>(
      `/api/exam-sessions/${examId}?user_id=${userId}`,
      {},
      30_000,
    ),

  submitExam: (examId: string, userId: string, answers: Record<string, number>) =>
    request<ExamResult>(
      `/api/exam-sessions/${examId}/submit`,
      { method: "POST", body: JSON.stringify({ user_id: userId, answers }) },
      60_000,
    ),

  examSummary: (examId: string, userId: string) =>
    request<ExamSummary>(
      `/api/exam-sessions/${examId}/summary?user_id=${userId}`,
      {},
      30_000,
    ),

  // --- monetization --------------------------------------------------------
  entitlements: (userId: string) =>
    request<Entitlements>(`/api/users/${userId}/entitlements`, {}, 15_000),

  /** Entitlement hook: a checkout provider's webhook calls this after payment. */
  activatePro: (userId: string, months = 1, provider = "manual") =>
    request<Entitlements & { activated: boolean; months: number }>(
      `/api/users/${userId}/pro`,
      { method: "POST", body: JSON.stringify({ months, provider }) },
      30_000,
    ),

  /** Inject a streak freeze/decay sweep now (dev + ops trigger). */
  runRetentionSweep: () =>
    request<{ ok: boolean; streak_saver: number; fsrs_decay: number }>(
      "/api/retention/sweep",
      { method: "POST" },
      30_000,
    ),
};

// --- classrooms / virality / notifications types ----------------------------

export interface ClassroomSummary {
  id: string;
  name: string;
  is_classroom: boolean;
  enrollment_code: string | null;
  instructor_id: string | null;
  instructor_email?: string | null;
  share_code: string | null;
  section?: string | null;
  description?: string | null;
  enrolled_count: number;
  created_at: string;
  role?: string;
}

export interface ClassroomRosterEntry {
  student_id: string;
  email: string;
  xp_points: number;
  streak_count: number;
  last_active_at: string | null;
  completion_percent: number;
  accuracy: number;
  answered: number;
}

export interface ClassroomRoster {
  course_id: string;
  name: string;
  enrollment_code: string | null;
  challenges_total: number;
  student_count: number;
  roster: ClassroomRosterEntry[];
}

export interface ClassroomInsights {
  course_id: string;
  attempts: number;
  accuracy: number;
  student_count: number;
  lectures: {
    lecture_id: string;
    lecture_title: string;
    attempts: number;
    correct: number;
    accuracy: number;
    top_struggling: { concept: string; failures: number }[];
  }[];
}

export interface SharedCoursePreview {
  course_id: string;
  name: string;
  category: string;
  institution: string | null;
  major: string | null;
  owner_handle: string;
  creator_earnings: number;
  instructor_handle?: string | null;
  enrollment_code?: string | null;
  chapters?: string[];
  lectures: number;
  challenges: number;
  downloads_count: number;
  is_classroom: boolean;
}

export interface NotificationItem {
  id: string;
  kind: string;
  title: string;
  body: string;
  url: string | null;
  push_status: string;
  read: boolean;
  created_at: string;
}

export interface NotificationInbox {
  user_id: string;
  unread: number;
  push_available: boolean;
  items: NotificationItem[];
}

// --- exam crunch types ------------------------------------------------------

export interface ExamQuestionPublic {
  index: number;
  challenge_id: string;
  chapter: string;
  node_title: string;
  type: string;
  prompt: string;
  options: string[];
}

export interface ExamChapterHeat {
  chapter: string;
  total: number;
  correct: number;
  accuracy: number;
  risk: "high" | "medium" | "low";
  weak_concepts: string[];
}

export interface ExamWeakSpot {
  challenge_id: string;
  node_id: string | null;
  node_title: string | null;
  chapter: string;
  type: string;
  prompt: string;
  correct_index: number;
  selected_index: number | null;
  explanation: string;
}

export interface ExamResult {
  exam_session_id: string;
  course_id: string;
  score: number;
  total: number;
  percent: number;
  grade: string;
  readiness: number;
  chapters: ExamChapterHeat[];
  weak_spots: ExamWeakSpot[];
  review: { challenge_id: string; chapter: string; correct: boolean }[];
  xp_awarded?: number;
  gems_awarded?: number;
}

export interface ExamCrunchResponse {
  exam_session_id: string;
  course_id: string;
  course_name: string;
  duration_seconds: number;
  question_count: number;
  chapters: string[];
  questions: ExamQuestionPublic[];
  cheatsheet: string;
  // "cached": an already-active exam was returned as-is (200 OK).
  cheatsheet_source: "llm" | "fallback" | "cached";
  cheatsheet_error: string | null;
  target_range: [number, number];
  started_at: string;
  /** Present on the cached-exam resume path (200 OK). */
  cached?: boolean;
}

export interface ExamSessionResponse {
  exam_session_id: string;
  course_id: string;
  status: string;
  total: number;
  duration_seconds: number;
  seconds_remaining: number;
  started_at: string;
  finished_at: string | null;
  questions: ExamQuestionPublic[];
  cheatsheet: string;
  score: number | null;
  grade: string | null;
  readiness: number | null;
}

export interface ExamSummary extends Omit<ExamResult, "xp_awarded" | "gems_awarded"> {
  course_name: string;
  cheatsheet: string;
  duration_seconds: number;
  started_at: string;
  finished_at: string | null;
  weak_spot_count: number;
}

// --- entitlement types ------------------------------------------------------

export interface Entitlements {
  user_id: string;
  role: string;
  is_pro: boolean;
  pro_active: boolean;
  pro_expires_at: string | null;
  show_ads: boolean;
  unlimited_uploads: boolean;
  free_daily_uploads: number;
  creator_gems: number;
  price_usd_cents: number;
  period_days: number;
  perks: string[];
}

// --- challenge helpers ------------------------------------------------------

/** Whitespace-preserving tokenisation of a Spot-the-Mistake text. */
export function tokenize(text: string): string[] {
  return text.match(/\S+/g) ?? [];
}

/**
 * Every token index covered by `mistake_token`.
 * The mistake is usually one word, but a model may flag a phrase ("20 knots"),
 * so tokens are matched by character-span overlap rather than substring.
 */
export function findMistakeTokenIndices(payload: SpotMistakePayload): number[] {
  const needle = payload.mistake_token.trim();
  if (!needle || !payload.code_or_text) return [];
  const start = payload.code_or_text.toLowerCase().indexOf(needle.toLowerCase());
  if (start < 0) return [];
  const end = start + needle.length;
  const indices: number[] = [];
  const pattern = /\S+/g;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = pattern.exec(payload.code_or_text)) !== null) {
    if (match.index < end && match.index + match[0].length > start) indices.push(index);
    index += 1;
  }
  return indices;
}

/** First clickable token holding the mistake, or -1 when the payload is malformed. */
export function findMistakeTokenIndex(payload: SpotMistakePayload): number {
  const indices = findMistakeTokenIndices(payload);
  return indices.length > 0 ? indices[0] : -1;
}

export function isMatchMadness(payload: ChallengePayload): payload is MatchMadnessPayload {
  return payload.type === "MATCH_MADNESS";
}

export function isSpotMistake(payload: ChallengePayload): payload is SpotMistakePayload {
  return payload.type === "SPOT_MISTAKE";
}

export function isContextualCloze(
  payload: ChallengePayload,
): payload is ContextualClozePayload {
  return payload.type === "CONTEXTUAL_CLOZE";
}


export function isDiagramQa(payload: ChallengePayload): payload is DiagramQaPayload {
  return payload.type === "DIAGRAM_QA";
}

/** Absolute URL for a server-relative asset path (diagrams, icons). */
export function assetUrl(path: string | null | undefined): string {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
}

/** What every challenge component reports back to the play page. */
export interface ChallengeOutcome {
  isCorrect: boolean;
  elapsedMs: number;
  /** Wrong taps / wrong picks / hints used — drives the Hard rating. */
  hintsUsed: number;
  /** Raw answer trace, stored with the review for analytics. */
  responseJson: unknown;
}

export interface ChallengeComponentProps<P> {
  challengeId: string;
  payload: P;
  onResult: (outcome: ChallengeOutcome) => void;
  /** Called when the learner loses a heart, so the HUD reacts immediately. */
  onHeartLoss?: () => void;
  onXp?: (amount: number) => void;
  disabled?: boolean;
}

// --- misc helpers -----------------------------------------------------------

/** Maps a finished attempt onto the FSRS 1..4 rating scale. */
export function ratingFromResult(
  isCorrect: boolean,
  elapsedMs: number,
  hintsUsed = 0,
): 1 | 2 | 3 | 4 {
  if (!isCorrect) return 1;
  if (hintsUsed > 0) return 2;
  if (elapsedMs > 0 && elapsedMs <= 8_000) return 4;
  if (elapsedMs >= 45_000) return 2;
  return 3;
}

export function ratingLabel(rating: number, lang: "en" | "ar" = "ar"): string {
  const entry = RATINGS.find((item) => item.value === rating);
  if (!entry) return "—";
  return lang === "ar" ? entry.ar : entry.en;
}

export function formatInterval(days: number | null | undefined): string {
  if (days === null || days === undefined || !Number.isFinite(days) || days <= 0) return "<1m";
  if (days < 1 / 24) return `${Math.max(1, Math.round(days * 1440))}m`;
  if (days < 1) return `${Math.round(days * 24)}h`;
  if (days < 30) return `${Math.round(days)}d`;
  if (days < 365) return `${(days / 30).toFixed(1)}mo`;
  return `${(days / 365).toFixed(1)}y`;
}

export function shuffle<T>(items: T[]): T[] {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const a = copy[i];
    const b = copy[j];
    copy[i] = b;
    copy[j] = a;
  }
  return copy;
}
