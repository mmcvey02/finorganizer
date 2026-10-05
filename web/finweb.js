// FinOrganizer for iPhone (and any modern browser), with no server.
//
// The normal app talks to a small Python web server on the computer. Here the same
// Python code runs inside the page on Pyodide (Python compiled to WebAssembly), and
// app.js hands its requests to window.FinWeb.request() instead of the network.
// Data lives in the browser's IndexedDB storage via Emscripten's IDBFS, mounted at
// /data, and is written back after every change.
//
// Built into the web app by packaging/build_web.py.
"use strict";

(function () {
  const DATA_DIR = "/data";
  const base = new URL(".", location.href).href;
  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone === true || matchMedia("(display-mode: standalone)").matches;

  // ------------------------------------------------------------ loading screen
  const boot = document.createElement("div");
  boot.id = "boot";
  boot.innerHTML = `<div class="boot-card"><div class="boot-logo">FinOrganizer</div>
    <div class="boot-msg">Starting…</div><div class="boot-bar"><i></i></div></div>`;
  document.body.appendChild(boot);
  const status = (msg) => { boot.querySelector(".boot-msg").textContent = msg; };
  const firstRun = (() => { try { return !localStorage.getItem("lastVersion"); } catch (_) { return true; } })();

  // -------------------------------------------------------------- persistence
  let py = null, handle = null;
  let saving = Promise.resolve(), saveTimer = null, dirty = false;

  function syncfs(populate) {
    return new Promise((resolve, reject) =>
      py.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
  }

  // Writes are queued so two syncs never overlap.
  function saveNow() {
    clearTimeout(saveTimer);
    if (!dirty || !py) return saving;
    dirty = false;
    saving = saving.then(() => syncfs(false)).catch((e) => {
      dirty = true;
      console.error("Saving failed", e);
      showToast("Couldn't save your changes on this device: " + (e.message || e), true);
    });
    return saving;
  }

  function scheduleSave() {
    dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 200);
  }

  // iOS may suspend or close the app at any moment once it's in the background.
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") saveNow(); });
  window.addEventListener("pagehide", saveNow);

  // ------------------------------------------------------------------ startup
  async function start() {
    status(firstRun ? "Getting things ready (first start only takes a little longer)…" : "Starting…");
    py = await loadPyodide({ indexURL: base + "pyodide/" });
    status("Opening your data…");
    py.FS.mkdirTree(DATA_DIR);
    py.FS.mount(py.FS.filesystems.IDBFS, {}, DATA_DIR);
    await syncfs(true);
    const res = await fetch(base + "finorganizer.zip");
    if (!res.ok) throw new Error("couldn't load the app files (" + res.status + ")");
    py.unpackArchive(await res.arrayBuffer(), "zip", { extractDir: "/app" });
    py.runPython(`import sys; sys.path.insert(0, "/app")`);
    const webapp = py.pyimport("finorganizer.webapp");
    webapp.start(DATA_DIR);
    handle = webapp.request;
    scheduleSave();  // first start creates the database
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
    boot.remove();
  }

  const ready = start().catch((e) => {
    console.error(e);
    status("FinOrganizer couldn't start: " + (e.message || e));
    const retry = document.createElement("button");
    retry.textContent = "Try again";
    retry.onclick = () => location.reload();
    boot.querySelector(".boot-card").appendChild(retry);
    boot.querySelector(".boot-bar").remove();
    throw e;
  });

  // ----------------------------------------------------------------- requests
  const decoder = new TextDecoder();

  async function call(method, path, body) {
    await ready;
    const res = handle(method, path, body === undefined ? "" : JSON.stringify(body));
    let out;
    try {
      const [status, type, data, disposition] = res.toJs();
      out = { status, type, data, disposition };
    } finally {
      res.destroy();
    }
    if (method !== "GET") scheduleSave();
    return out;
  }

  async function request(method, path, body) {
    const r = await call(method, path, body);
    let data = {};
    try { data = JSON.parse(decoder.decode(r.data)); } catch (_) { /* not JSON */ }
    return { status: r.status, data };
  }

  // ---------------------------------------------------------------- downloads
  async function saveFile(name, bytes, type) {
    const file = new File([bytes], name, { type });
    // On iPhone the share sheet is the dependable way to keep a file ("Save to Files").
    if (isIOS && navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file] });
        return;
      } catch (e) {
        if (e.name === "AbortError") return;  // closed the share sheet
      }
    }
    const url = URL.createObjectURL(file);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  async function download(path) {
    const r = await call("GET", path);
    if (r.status >= 400) throw new Error("Download failed (" + r.status + ")");
    const m = /filename="([^"]+)"/.exec(r.disposition || "");
    const name = m ? m[1] : "download";
    await saveFile(name, r.data, name.endsWith(".csv") ? "text/csv" : "application/octet-stream");
  }

  // The printable summary opens over the app (a phone app has no second window).
  async function openSummary(path) {
    const r = await call("GET", path);
    const html = decoder.decode(r.data);
    const box = document.createElement("div");
    box.id = "summary-overlay";
    box.innerHTML = `<div class="summary-bar"><button class="ghost" data-act="close">Done</button>
      <span>Financial summary</span><button data-act="print">Print / PDF</button></div><iframe title="Financial summary"></iframe>`;
    document.body.appendChild(box);
    const frame = box.querySelector("iframe");
    frame.srcdoc = html;
    box.querySelector("[data-act=close]").onclick = () => box.remove();
    box.querySelector("[data-act=print]").onclick = () => {
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch (e) { showToast(e.message, true); }
    };
  }

  function showToast(msg, isError) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "show" + (isError ? " error" : "");
    setTimeout(() => (t.className = ""), isError ? 5000 : 2500);
  }

  // ------------------------------------------------------- offline & updates
  // The service worker keeps a copy of the app so it opens without a connection,
  // and fetches new versions in the background.
  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    const hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.register(base + "sw.js").catch((e) => console.warn("Offline support unavailable", e));
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!hadController) return;  // first install, nothing new to load
      const b = document.getElementById("update-banner");
      if (!b) return;
      b.hidden = false;
      b.innerHTML = `<span>A new version of FinOrganizer is ready.</span><button id="reload-app">Reload</button>`;
      b.querySelector("#reload-app").onclick = async () => { await saveNow(); location.reload(); };
    });
  }

  window.FinWeb = { request, download, openSummary, ready, standalone, isIOS, save: saveNow };
})();
