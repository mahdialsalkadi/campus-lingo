/**
 * Typed client for the CampusLingo core-ai service.
 * Field names mirror services/core-ai/schema.py one-to-one.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export const DEFAULT_MODEL = "qwen2.5:7b";

export type ChallengeType =
  | "MATCH_MADNESS"
  | "SPOT_MISTAKE"
  | "CONTEXTUAL_CLOZE"
  | "INTERACTIVE_STORY";

export const CHALLENGE_TYPES: ChallengeType[] = [
  "MATCH_MADNESS",
  "SPOT_MISTAKE",
  "CONTEXTUAL_CLOZE",
  "INTERACTIVE_STORY",
];

export const CHALLENGE_LABELS: Record<ChallengeType, { en: string; ar: string }> = {
  MATCH_MADNESS: { en: "Match Madness", ar: "مطابقة سريعة" },
  SPOT_MISTAKE: { en: "Spot the Mistake", ar: "اكتشف الخطأ" },
  CONTEXTUAL_CLOZE: { en: "Contextual Cloze", ar: "أكمل الفراغ" },
  INTERACTIVE_STORY: { en: "Interactive Story", ar: "قصة تفاعلية" },
};

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
}

export interface SpotMistakePayload {
  type: "SPOT_MISTAKE";
  instructions: string;
  code_or_text: string;
  mistake_token: string;
  explanation: string;
}

export interface ContextualClozePayload {
  type: "CONTEXTUAL_CLOZE";
  instructions: string;
  sentence_with_blank: string;
  correct_answer: string;
  distractors: string[];
  explanation: string;
}

export interface StoryOption {
  text: string;
  correct: boolean;
  feedback: string;
}

export interface StoryNode {
  speaker: string;
  text: string;
  action_prompt: string;
  options: StoryOption[];
}

export interface InteractiveStoryPayload {
  type: "INTERACTIVE_STORY";
  instructions: string;
  scenario_title: string;
  dialogue: StoryNode[];
}

export type ChallengePayload =
  | MatchMadnessPayload
  | SpotMistakePayload
  | ContextualClozePayload
  | InteractiveStoryPayload;

export interface Challenge {
  id: string;
  node_id: string;
  type: ChallengeType;
  payload_json: ChallengePayload;
  difficulty: number;
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

export interface CourseSummary {
  id: string;
  name: string;
  lms_type: string;
  created_at: string;
  lectures: {
    id: string;
    title: string;
    status: string;
    total_chunks: number;
    created_at: string;
  }[];
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
 * Upload a lecture PDF for parsing + Qwen generation + persistence.
 * `formData` must contain: file (the PDF) and course_id.
 */
export function uploadLecture(formData: FormData) {
  return request<ProcessLectureResponse>("/api/process-lecture", {
    method: "POST",
    body: formData,
  });
}

/** Every challenge belonging to one learning node. */
export function getNodeChallenges(nodeId: string) {
  return request<NodeChallengesResponse>(`/api/nodes/${nodeId}/challenges`, {}, 30_000);
}

/** Grade one attempt: recalculates DSR server-side and persists the schedule. */
export function submitCardReview(submission: ReviewSubmission) {
  return request<ReviewResponse>("/api/reviews", {
    method: "POST",
    body: JSON.stringify(submission),
  });
}

export const api = {
  health: () =>
    request<{ status: string; database: { ok: boolean }; ollama: Record<string, unknown> }>(
      "/health",
      {},
      15_000,
    ),

  upsertUser: (email: string) =>
    request<{
      id: string;
      email: string;
      xp_points: number;
      gems: number;
      streak_count: number;
      league_tier: number;
      created: boolean;
    }>("/api/users", { method: "POST", body: JSON.stringify({ email }) }, 20_000),

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
    request<{ id: string; name: string }>("/api/courses", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, name, lms_type: lmsType }),
    }),

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

  mistakes: (userId: string, limit = 50) =>
    request<{ count: number; items: QueueItem[] }>(
      `/api/mistakes?user_id=${userId}&limit=${limit}`,
      {},
      30_000,
    ),

  fsrsPreview: (challengeId: string, userId: string) =>
    request<FsrsPreview>(`/api/fsrs/preview/${challengeId}?user_id=${userId}`, {}, 20_000),
};

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

export function isInteractiveStory(
  payload: ChallengePayload,
): payload is InteractiveStoryPayload {
  return payload.type === "INTERACTIVE_STORY";
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
