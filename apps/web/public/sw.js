/* CampusLingo service worker — hand-written, zero build plugins.
 *
 * Strategy
 *   • navigations  -> network-first, fall back to the cached shell (offline)
 *   • static assets -> stale-while-revalidate (fast repeat loads)
 *   • API calls    -> never cached here; the app uses IndexedDB + a
 *     localStorage review queue (lib/offline.ts) for offline study data.
 *   • Web Push     -> `push` renders the retention alert (new lecture, streak
 *     saver, FSRS decay); `notificationclick` focuses or opens the target URL.
 */

const VERSION = "campuslingo-v1";
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;

const SHELL_ASSETS = ["/", "/manifest.json", "/icon.svg", "/icon-maskable.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => !key.startsWith(VERSION))
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

function isApiRequest(url) {
  return url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/");
}

// --- Web Push ---------------------------------------------------------------
// Payload contract (see services/core-ai/retention.py:_payload):
//   { title, body, url, kind, notification_id }

self.addEventListener("push", (event) => {
  let payload = {
    title: "CampusLingo",
    body: "لديك تحديث جديد في مسارك.",
    url: "/",
    kind: "classroom",
  };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch (error) {
    if (event.data) payload.body = event.data.text();
  }

  const options = {
    body: payload.body,
    icon: "/icon.svg",
    badge: "/icon.svg",
    dir: "rtl",
    lang: "ar",
    tag: payload.kind || "campuslingo",
    renotify: true,
    data: { url: payload.url || "/", kind: payload.kind },
  };

  event.waitUntil(
    Promise.all([
      self.registration.showNotification(payload.title || "CampusLingo", options),
      // Browsers deliver push only to a registered SW, so the in-app copy is
      // refreshed by the app itself on next open; nothing else to persist here.
      self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
        for (const client of clients) {
          client.postMessage({ type: "push-received", payload });
        }
      }),
    ]),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          client.postMessage({ type: "notification-click", url: target });
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isApiRequest(url)) return;

  // Navigations: network-first so students always get the freshest page, with
  // the cached shell as the airplane-mode fallback.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() =>
          caches
            .match(request)
            .then((cached) => cached || caches.match("/"))
            .then(
              (cached) =>
                cached ||
                new Response("Offline", { status: 503, statusText: "Offline" }),
            ),
        ),
    );
    return;
  }

  // Static assets: stale-while-revalidate.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.status === 200 && response.type === "basic") {
            const copy = response.clone();
            caches.open(ASSET_CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
