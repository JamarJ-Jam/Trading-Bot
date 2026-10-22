// Caches the app shell so it opens instantly from the home screen.
// API calls always go to the network: trading data must never be stale.
const CACHE = "trading-bot-v3";
const SHELL = ["/", "/assets/app.css", "/assets/app.js", "/assets/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Network first, cache as the offline fallback, so updates show up straight away.
  e.respondWith(fetch(e.request)
    .then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return resp;
    })
    .catch(() => caches.match(e.request)));
});
