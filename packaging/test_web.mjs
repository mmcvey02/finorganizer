// Smoke-test a built web app (packaging/build_web.py) under Node: start the bundled
// Pyodide, load the bundled finorganizer package and use it like the page does.
//
//     node packaging/test_web.mjs dist/web
import { createRequire } from "module";
import fs from "fs";
import path from "path";

const site = path.resolve(process.argv[2] || "dist/web");
const require = createRequire(import.meta.url);
const { loadPyodide } = require(path.join(site, "pyodide", "pyodide.js"));

const fail = (msg) => { console.error("FAIL: " + msg); process.exit(1); };
for (const f of ["index.html", "app.js", "finweb.js", "sw.js", "manifest.webmanifest", "icons/apple-touch-icon.png"]) {
  if (!fs.existsSync(path.join(site, f))) fail("missing " + f);
}
const sw = fs.readFileSync(path.join(site, "sw.js"), "utf8");
for (const f of JSON.parse(/const FILES = (\[.*\]);/.exec(sw)[1])) {
  if (f !== "./" && !fs.existsSync(path.join(site, f))) fail("service worker lists missing file " + f);
}

const py = await loadPyodide({ indexURL: path.join(site, "pyodide") + path.sep });
py.unpackArchive(new Uint8Array(fs.readFileSync(path.join(site, "finorganizer.zip"))), "zip", { extractDir: "/app" });
py.runPython(`import sys; sys.path.insert(0, "/app")`);
const webapp = py.pyimport("finorganizer.webapp");
webapp.start("/data");
const call = (method, url, body) => {
  const r = webapp.request(method, url, body ? JSON.stringify(body) : "");
  const [status, type, data] = r.toJs();
  r.destroy();
  return { status, type, text: new TextDecoder().decode(data) };
};
const ok = (r, what) => { if (r.status !== 200) fail(`${what}: ${r.status} ${r.text}`); return r; };

ok(call("POST", "/api/sample"), "load sample data");
const meta = JSON.parse(ok(call("GET", "/api/meta"), "meta").text);
const dash = JSON.parse(ok(call("GET", "/api/dashboard"), "dashboard").text);
if (!dash.accounts.length) fail("dashboard has no accounts");
const acct = JSON.parse(ok(call("POST", "/api/accounts", { name: "Phone test", type: "cash" }), "add account").text);
ok(call("POST", "/api/import", { account_id: acct.id, csv: "Date,Description,Amount\n2026-09-01,NETFLIX.COM,-15.99\n" }), "import");
const txs = JSON.parse(ok(call("GET", `/api/transactions?account_id=${acct.id}`), "transactions").text);
if (txs[0].category_name !== "Subscriptions") fail("CSV import wasn't categorized: " + JSON.stringify(txs[0]));
if (!ok(call("GET", "/summary"), "summary").text.includes("<svg")) fail("summary has no charts");
ok(call("GET", "/api/export.csv"), "export");
const backup = webapp.request("GET", "/api/backup.db", "");
const bytes = backup.toJs()[2];
backup.destroy();
if (new TextDecoder().decode(bytes.slice(0, 15)) !== "SQLite format 3") fail("backup isn't a database");
console.log(`web app OK: FinOrganizer ${meta.version} on Python ${py.runPython("import sys; sys.version.split()[0]")},`,
  `${dash.accounts.length} sample accounts, ${bytes.length} byte backup`);
