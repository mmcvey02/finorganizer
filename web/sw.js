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
