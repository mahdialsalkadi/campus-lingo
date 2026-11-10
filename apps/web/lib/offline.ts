/**
 * Offline engine: IndexedDB node cache + a localStorage review queue.
 *
 * Zero dependencies — a plain IndexedDB wrapper (~60 lines) is all the app
 * needs to cache a node's challenges for airplane-mode practice, and the
 * pending-review queue is small enough that localStorage is the simplest
 * durable store (it never exceeds a few hundred short JSON objects).
 *
 * Flow:
 *   1. "⬇️ تحميل بدون اتصال" on a chapter folder calls ``cacheNode``.
 *   2. Offline, ``getNodeChallenges`` reads the cache when the network fails.
 *   3. Every finished attempt posts to /api/reviews; when that fails the store
 *      calls ``enqueueReview`` instead.
 *   4. ``flushReviewQueue`` replays them in order and the HUD resyncs.
 */

import type { NodeChallengesResponse, ReviewSubmission } from "@/lib/api";

const DB_NAME = "campuslingo-offline";
const DB_VERSION = 1;
const NODE_STORE = "nodes";
const QUEUE_KEY = "campuslingo-review-queue-v1";

export interface CachedNode {
  nodeId: string;
  payload: NodeChallengesResponse;
  cachedAt: number;
}

function hasIndexedDb(): boolean {
  return typeof window !== "undefined" && typeof window.indexedDB !== "undefined";
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(NODE_STORE)) {
        db.createObjectStore(NODE_STORE, { keyPath: "nodeId" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("indexedDB open failed"));
  });
}

function tx<T>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const transaction = db.transaction(NODE_STORE, mode);
        const request = run(transaction.objectStore(NODE_STORE));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error ?? new Error("indexedDB request failed"));
        transaction.oncomplete = () => db.close();
      }),
  );
}

/** Persist a node's challenges (and their payloads) for offline play. */
export async function cacheNode(nodeId: string, payload: NodeChallengesResponse): Promise<void> {
  if (!hasIndexedDb()) return;
  const record: CachedNode = { nodeId, payload, cachedAt: Date.now() };
  await tx("readwrite", (store) => store.put(record) as IDBRequest<IDBValidKey>);
}

export async function getCachedNode(nodeId: string): Promise<NodeChallengesResponse | null> {
  if (!hasIndexedDb()) return null;
  try {
    const record = await tx<CachedNode | undefined>("readonly", (store) =>
      store.get(nodeId) as IDBRequest<CachedNode | undefined>,
    );
    return record?.payload ?? null;
  } catch {
    return null;
  }
}

export async function removeCachedNode(nodeId: string): Promise<void> {
  if (!hasIndexedDb()) return;
  await tx("readwrite", (store) => store.delete(nodeId) as IDBRequest<undefined>);
}

export async function listCachedNodes(): Promise<CachedNode[]> {
  if (!hasIndexedDb()) return [];
  try {
    return await tx<CachedNode[]>("readonly", (store) => store.getAll() as IDBRequest<CachedNode[]>);
  } catch {
    return [];
  }
}

// --- pending review queue ---------------------------------------------------

/** True when the browser reports no connectivity. */
export function isOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

export function readReviewQueue(): ReviewSubmission[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(QUEUE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as ReviewSubmission[]) : [];
  } catch {
    return [];
  }
}

/** Append an unsent review (used when POST /api/reviews cannot reach the API). */
export function enqueueReview(submission: ReviewSubmission): number {
  if (typeof window === "undefined") return 0;
  const queue = readReviewQueue();
  queue.push(submission);
  try {
    window.localStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
  } catch {
    /* quota exhausted — the attempt is simply lost, never the session */
  }
  return queue.length;
}

export function clearReviewQueue(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(QUEUE_KEY);
}

/**
 * Replay the queue, oldest first. Stops at the first failure so ordering is
 * preserved; returns how many reviews were credited.
 */
export async function flushReviewQueue(
  send: (submission: ReviewSubmission) => Promise<unknown>,
): Promise<number> {
  const queue = readReviewQueue();
  if (queue.length === 0) return 0;
  let sent = 0;
  const remaining: ReviewSubmission[] = [];
  for (let index = 0; index < queue.length; index += 1) {
    const submission = queue[index];
    try {
      await send(submission);
      sent += 1;
    } catch {
      remaining.push(...queue.slice(index));
      break;
    }
  }
  if (remaining.length > 0) {
    window.localStorage.setItem(QUEUE_KEY, JSON.stringify(remaining));
  } else {
    clearReviewQueue();
  }
  return sent;
}
