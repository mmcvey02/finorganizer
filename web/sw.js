// Service worker for the iPhone / web version: keeps a copy of every file the app
// needs so it opens without a connection. Each build has its own cache; when a new
// build is published, the browser installs it in the background and removes the old one.
// packaging/build_web.py fills in VERSION and FILES.
"use strict";

const VERSION = "__VERSION__";
const FILES = __FILES__;
const CACHE = "finorganizer-" + VERSION;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: "reload" })))).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("finorganizer-") && k !== CACHE)
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;
  event.respondWith(caches.open(CACHE).then(async (cache) => {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    if (req.mode === "navigate") {  // any page address within the app opens the app
      const page = await cache.match("./");
      if (page) return page;
    }
    return fetch(req);
  }));
});

// Notifications sent by FinOrganizer on your computer (see finorganizer/webpush.py).
self.addEventListener("push", (event) => {
  let m = {};
  try { m = event.data ? event.data.json() : {}; } catch (_) { m = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(m.title || "FinOrganizer", {
    body: m.body || "", tag: m.tag || undefined, icon: "icons/icon-192.png", badge: "icons/icon-192.png",
    data: { url: typeof m.url === "string" && m.url.startsWith("#") ? m.url : "" },
  }));
});

// Tapping one opens the app on the page it's about.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL("./" + ((event.notification.data && event.notification.data.url) || ""), self.registration.scope).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (win) return win.focus().then((w) => (w && w.navigate ? w.navigate(url) : w));
    return self.clients.openWindow(url);
  }));
});
