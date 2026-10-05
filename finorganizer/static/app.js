/* FinOrganizer single-page UI. Talks to the JSON API in server.py; all money is integer cents. */
"use strict";

// ------------------------------------------------------------------ utilities

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (cents, opts = {}) => {
  if (cents === null || cents === undefined) return "";
  const v = Math.abs(cents) / 100;
  const s = v.toLocaleString(undefined, {
    minimumFractionDigits: opts.whole ? 0 : 2, maximumFractionDigits: opts.whole ? 0 : 2 });
  return (cents < 0 ? "-$" : "$") + s;
};
const moneyCell = (cents) => `<td class="num ${cents < 0 ? "neg" : ""}">${money(cents)}</td>`;
const shortMoney = (cents) => {
  const v = Math.abs(cents) / 100, sign = cents < 0 ? "-" : "";
  if (v >= 1e6) return `${sign}$${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${sign}$${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k`;
  return `${sign}$${v.toFixed(0)}`;
};
const toCents = (v) => {
  if (v === null || v === undefined || String(v).trim() === "") return 0;
  const n = parseFloat(String(v).replace(/[$,\s]/g, ""));
  if (Number.isNaN(n)) throw new Error(`"${v}" is not a valid amount`);
  return Math.round(n * 100);
};
const dollars = (cents) => (cents === null || cents === undefined ? "" : (cents / 100).toFixed(2));
const pct = (v) => (v === null || v === undefined ? "–" : `${v.toFixed(1)}%`);

const todayISO = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
const thisMonth = () => todayISO().slice(0, 7);
const shiftMonth = (m, n) => {
  const [y, mo] = m.split("-").map(Number);
  const d = new Date(y, mo - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};
const monthLabel = (m, long = false) => {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleString(undefined,
    long ? { month: "long", year: "numeric" } : { month: "short" });
};
const titleCase = (s) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
const GET = (p) => api("GET", p);

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), isError ? 4000 : 2200);
}

async function attempt(fn, success) {
  try {
    const r = await fn();
    if (success) toast(success);
    return r;
  } catch (e) {
    toast(e.message, true);
    throw e;
  }
}

// ---------------------------------------------------------------- shared data

const store = { meta: null, accounts: [], categories: [] };

async function loadShared() {
  [store.meta, store.accounts, store.categories] = await Promise.all([
    GET("/api/meta"), GET("/api/accounts"), GET("/api/categories")]);
}

const accountOptions = (selected, blank) =>
  (blank ? `<option value="">${esc(blank)}</option>` : "") +
  store.accounts.map((a) => `<option value="${a.id}" ${a.id == selected ? "selected" : ""}>${esc(a.name)}</option>`).join("");

function categoryOptions(selected, blank = "Uncategorized", kinds) {
  const groups = {};
  for (const c of store.categories) {
    if (kinds && !kinds.includes(c.kind)) continue;
    const g = c.group_name || titleCase(c.kind);
    (groups[g] = groups[g] || []).push(c);
  }
  return (blank !== null ? `<option value="">${esc(blank)}</option>` : "") +
    Object.entries(groups).map(([g, cs]) => `<optgroup label="${esc(g)}">${cs.map((c) =>
      `<option value="${c.id}" ${c.id == selected ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</optgroup>`).join("");
}

// --------------------------------------------------------------------- modal

/* fields: [{name, label, type: text|number|money|date|select|textarea|checkbox, options(html), value, required}] */
function formDialog(title, fields, submitLabel = "Save") {
  const dlg = $("#modal"), form = $("#modal-form");
  form.innerHTML = `<h2>${esc(title)}</h2>
    <div class="form-grid">${fields.map((f) => {
      const id = `f-${f.name}`;
      const req = f.required ? "required" : "";
      const val = esc(f.value ?? "");
      let input;
      if (f.type === "select") input = `<select id="${id}" name="${f.name}" ${req}>${f.options}</select>`;
      else if (f.type === "textarea") input = `<textarea id="${id}" name="${f.name}" rows="6">${val}</textarea>`;
      else if (f.type === "checkbox") return `<label class="check"><input type="checkbox" name="${f.name}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>`;
      else if (f.type === "file") input = `<input id="${id}" type="file" name="${f.name}" accept="${f.accept || ""}">`;
      else {
        const type = f.type === "money" ? "text" : (f.type || "text");
        const extra = f.type === "money" ? 'inputmode="decimal" placeholder="0.00"' : (f.type === "number" ? 'step="any"' : "");
        input = `<input id="${id}" name="${f.name}" type="${type}" value="${val}" ${extra} ${req} placeholder="${esc(f.placeholder || "")}">`;
      }
      return `<label class="field" style="${f.wide ? "grid-column: 1 / -1" : ""}">${esc(f.label)}${input}${f.hint ? `<span class="muted small">${esc(f.hint)}</span>` : ""}</label>`;
    }).join("")}</div>
    <div class="buttons"><button type="button" class="ghost" value="cancel">Cancel</button><button value="ok">${esc(submitLabel)}</button></div>`;
  return new Promise((resolve) => {
    $("button[value=cancel]", form).onclick = () => { dlg.close(); };
    form.onsubmit = (e) => {
      e.preventDefault();
      const out = {};
      for (const f of fields) {
        const inp = form.elements[f.name];
        if (f.type === "checkbox") out[f.name] = inp.checked;
        else if (f.type === "file") out[f.name] = inp.files[0] || null;
        else out[f.name] = inp.value;
      }
      dlg.close();
      resolve(out);
    };
    dlg.onclose = () => resolve(null);
    dlg.showModal();
    const first = $("input:not([type=checkbox]), select, textarea", form);
    if (first) first.focus();
  });
}

const confirmDialog = async (msg) => (await formDialog(msg, [], "Confirm")) !== null;

// ------------------------------------------------------------------- tooltip

const tip = $("#tooltip");
function showTip(html, evt) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => (tip.style.display = "none");

// -------------------------------------------------------------------- charts

function niceScale(min, max, ticks = 4) {
  if (min === max) { max = min + 1; }
  const span = max - min;
  const step0 = span / ticks;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const out = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(v);
  return out;
}

/* Grouped vertical bars. series: [{key, label, color}], data: [{label, tip, [key]: cents}] */
function barChart(data, series, { height = 220, width = 640 } = {}) {
  const W = Math.max(280, width), H = height, m = { t: 10, r: 8, b: 26, l: 52 };
  const vals = data.flatMap((d) => series.map((s) => d[s.key]));
  const ticks = niceScale(Math.min(0, ...vals), Math.max(0, ...vals));
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const y = (v) => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
  const band = (W - m.l - m.r) / data.length;
  const gap = 2, bw = Math.min(28, (band * 0.7 - gap * (series.length - 1)) / series.length);
  const groupW = bw * series.length + gap * (series.length - 1);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">`;
  for (const t of ticks) {
    svg += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`;
    svg += `<text x="${m.l - 6}" y="${y(t) + 4}" text-anchor="end">${shortMoney(t)}</text>`;
  }
  data.forEach((d, i) => {
    const x0 = m.l + band * i + (band - groupW) / 2;
    series.forEach((s, j) => {
      const v = d[s.key], x = x0 + j * (bw + gap);
      const top = y(Math.max(v, 0)), bottom = y(Math.min(v, 0));
      const h = Math.max(bottom - top, v ? 1 : 0), r = Math.min(4, bw / 2, h);
      // Rounded at the data end, square at the baseline.
      const path = v >= 0
        ? `M${x},${bottom} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${bottom} Z`
        : `M${x},${top} V${bottom - r} Q${x},${bottom} ${x + r},${bottom} H${x + bw - r} Q${x + bw},${bottom} ${x + bw},${bottom - r} V${top} Z`;
      svg += `<path d="${path}" fill="${s.color}"/>`;
    });
    if (i % Math.ceil(data.length / Math.max(1, Math.floor((W - m.l) / 40))) === 0)
      svg += `<text x="${m.l + band * i + band / 2}" y="${H - 8}" text-anchor="middle">${esc(d.label)}</text>`;
    svg += `<rect class="hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${H - m.t - m.b}" fill="transparent"/>`;
  });
  svg += `<line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/></svg>`;
  const legend = series.length > 1
    ? `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>` : "";
  const wrap = document.createElement("div");
  wrap.innerHTML = legend + svg;
  $$(".hit", wrap).forEach((r) => {
    const d = data[r.dataset.i];
    const html = `<strong>${esc(d.tip || d.label)}</strong><br>` + series.map((s) =>
      `<i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color}"></i> ${esc(s.label)}: ${money(d[s.key])}`).join("<br>");
    r.addEventListener("mousemove", (e) => showTip(html, e));
    r.addEventListener("mouseleave", hideTip);
  });
  return wrap;
}

/* Single-series line with crosshair hover. data: [{label, tip, value}] */
function lineChart(data, { color = "var(--series-1)", height = 220, valueLabel = "Value", width = 640 } = {}) {
  const W = Math.max(280, width), H = height, m = { t: 12, r: 12, b: 26, l: 60 };
  const vals = data.map((d) => d.value);
  const ticks = niceScale(Math.min(...vals), Math.max(...vals));
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const x = (i) => m.l + (data.length === 1 ? (W - m.l - m.r) / 2 : (W - m.l - m.r) * i / (data.length - 1));
  const y = (v) => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo || 1));
  const labelEvery = Math.ceil(data.length / Math.max(2, Math.floor((W - m.l) / 60)));
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">`;
  for (const t of ticks) {
    svg += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`;
    svg += `<text x="${m.l - 6}" y="${y(t) + 4}" text-anchor="end">${shortMoney(t)}</text>`;
  }
  data.forEach((d, i) => {
    if (i % labelEvery === 0 || i === data.length - 1)
      svg += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(d.label)}</text>`;
  });
  const pts = data.map((d, i) => `${x(i)},${y(d.value)}`).join(" ");
  svg += `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const last = data[data.length - 1];
  svg += `<circle cx="${x(data.length - 1)}" cy="${y(last.value)}" r="4" fill="${color}" stroke="var(--surface)" stroke-width="2"/>`;
  svg += `<line class="xhair" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--text-2)" stroke-dasharray="3 3" visibility="hidden"/>`;
  svg += `<circle class="xdot" r="5" fill="${color}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  svg += `<rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`;
  const wrap = document.createElement("div");
  wrap.innerHTML = svg;
  const el = $("svg", wrap), hit = $(".hit", wrap), xh = $(".xhair", wrap), xd = $(".xdot", wrap);
  hit.addEventListener("mousemove", (e) => {
    const box = el.getBoundingClientRect();
    const px = (e.clientX - box.left) * (W / box.width);
    const i = Math.max(0, Math.min(data.length - 1,
      Math.round((px - m.l) / ((W - m.l - m.r) / Math.max(1, data.length - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    xd.setAttribute("cx", x(i)); xd.setAttribute("cy", y(data[i].value)); xd.setAttribute("visibility", "visible");
    showTip(`<strong>${esc(data[i].tip || data[i].label)}</strong><br>${esc(valueLabel)}: ${money(data[i].value)}`, e);
  });
  hit.addEventListener("mouseleave", () => {
    hideTip(); xh.setAttribute("visibility", "hidden"); xd.setAttribute("visibility", "hidden");
  });
  return wrap;
}

/* Horizontal bars with direct labels: [{label, value, note}] */
function hBars(items, color = "var(--series-1)") {
  if (!items.length) return `<div class="empty">No spending recorded for this period.</div>`;
  const max = Math.max(...items.map((i) => i.value));
  return items.map((i) => `<div class="bar-row" title="${esc(i.label)}: ${money(i.value)}">
      <div>${esc(i.label)}</div>
      <div class="track"><span style="width:${(100 * i.value / max).toFixed(1)}%;background:${color}"></span></div>
      <div class="num">${money(i.value)}${i.note ? ` <span class="muted small">${esc(i.note)}</span>` : ""}</div>
    </div>`).join("");
}

function progressBar(percent, state = "") {
  return `<div class="progress ${state}" role="progressbar" aria-valuenow="${percent.toFixed(0)}" aria-valuemin="0" aria-valuemax="100"><span style="width:${Math.min(100, Math.max(0, percent)).toFixed(1)}%"></span></div>`;
}

const cashFlowChart = (flows, width) => barChart(
  flows.map((f) => ({ label: monthLabel(f.month), tip: monthLabel(f.month, true),
                      income: f.income_cents, expenses: f.expense_cents })),
  [{ key: "income", label: "Income", color: "var(--series-1)" },
   { key: "expenses", label: "Expenses", color: "var(--series-2)" }], { width });

// --------------------------------------------------------------------- pages

const pages = {};

pages.dashboard = async (params) => {
  const month = params.get("month") || thisMonth();
  const d = await GET(`/api/dashboard?month=${month}`);
  const s = d.summary, nw = d.net_worth;
  if (!d.accounts.length) {
    view.innerHTML = `<div class="card empty">
      <h1>Welcome to FinOrganizer</h1>
      <p>Start by adding your bank accounts, cards, loans and investments.</p>
      <p><a class="btn" href="#accounts">Add an account</a></p>
      <p class="small">Want to explore first? Run <code>python -m finorganizer demo</code> to load sample data.</p></div>`;
    return;
  }
  view.innerHTML = `
    <div class="page-head"><h1>${esc(monthLabel(month, true))}</h1>
      <button class="ghost" id="prev">‹</button><input type="month" id="month" value="${month}"><button class="ghost" id="next">›</button>
      <button id="quick-add">+ Transaction</button></div>
    <div class="tiles">
      <div class="card tile"><div class="label">Net worth</div><div class="value">${money(nw.net_worth_cents, { whole: true })}</div>
        <div class="sub">${money(nw.assets_cents, { whole: true })} assets · ${money(nw.liabilities_cents, { whole: true })} debt</div></div>
      <div class="card tile"><div class="label">Income</div><div class="value">${money(s.income_cents, { whole: true })}</div><div class="sub">this month</div></div>
      <div class="card tile"><div class="label">Spending</div><div class="value">${money(s.expense_cents, { whole: true })}</div><div class="sub">${s.transaction_count} transactions</div></div>
      <div class="card tile"><div class="label">Saved</div><div class="value ${s.net_cents < 0 ? "neg" : ""}">${money(s.net_cents, { whole: true })}</div><div class="sub">savings rate ${pct(s.savings_rate)}</div></div>
    </div>
    <div class="grid cols-2">
      <div class="card"><h2>Insights</h2>${d.insights.length ? d.insights.map((i) => `<div class="insight ${i.level}">
          <span class="icon">${{ good: "✓", warning: "!", info: "i" }[i.level]}</span><span>${esc(i.message)}</span></div>`).join("")
          : `<div class="muted">Add a few transactions to see insights.</div>`}</div>
      <div class="card"><h2>Cash flow, last 6 months</h2><div id="cf"></div></div>
      <div class="card"><h2>Where the money went</h2>${hBars(d.spending_by_category.slice(0, 8).map((c) =>
          ({ label: c.category_name, value: c.spent_cents, note: `${c.percent.toFixed(0)}%` })), "var(--series-2)")}</div>
      <div class="card"><h2>Budgets <a class="small" href="#budgets?month=${month}">manage</a></h2>
        ${d.budgets.filter((b) => b.budget_cents).length ? d.budgets.filter((b) => b.budget_cents).map((b) => budgetRow(b)).join("")
          : `<div class="muted">No budgets for this month yet. <a href="#budgets?month=${month}">Set some up</a>.</div>`}</div>
      <div class="card"><h2>Goals <a class="small" href="#goals">manage</a></h2>
        ${d.goals.length ? d.goals.map((g) => `<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;gap:8px">
          <span>${esc(g.name)}</span><span class="num">${money(g.current_cents, { whole: true })} / ${money(g.target_cents, { whole: true })}</span></div>
          ${progressBar(g.percent)}</div>`).join("") : `<div class="muted">No goals yet. <a href="#goals">Create one</a>.</div>`}</div>
      <div class="card"><h2>Upcoming bills, next 30 days <a class="small" href="#recurring">manage</a></h2>
        ${d.upcoming.length ? `<table><tbody>${d.upcoming.slice(0, 8).map((u) => `<tr><td>${esc(u.date)}${u.overdue ? ' <span class="pill warn">due</span>' : ""}</td>
          <td>${esc(u.name)}</td>${moneyCell(u.amount_cents)}</tr>`).join("")}</tbody></table>` : `<div class="muted">Nothing scheduled.</div>`}</div>
      <div class="card"><h2>Accounts <a class="small" href="#accounts">manage</a></h2><table><tbody>
        ${d.accounts.map((a) => `<tr><td><a href="#transactions?account_id=${a.id}">${esc(a.name)}</a> <span class="muted small">${titleCase(a.type)}</span></td>${moneyCell(a.balance_cents)}</tr>`).join("")}
      </tbody></table></div>
    </div>`;
  $("#cf").append(cashFlowChart(d.cash_flow, $("#cf").clientWidth));
  const go = (m) => (location.hash = `dashboard?month=${m}`);
  $("#month").onchange = (e) => e.target.value && go(e.target.value);
  $("#prev").onclick = () => go(shiftMonth(month, -1));
  $("#next").onclick = () => go(shiftMonth(month, 1));
  $("#quick-add").onclick = () => editTransaction(null).then(route);
};

function budgetRow(b) {
  const p = b.percent_used ?? 0;
  const state = p > 100 ? "over" : p > 85 ? "warn" : "";
  return `<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;gap:8px">
    <span>${esc(b.category_name)}</span>
    <span class="num ${b.remaining_cents < 0 ? "neg" : ""}">${money(b.spent_cents, { whole: true })} of ${money(b.budget_cents, { whole: true })}</span></div>
    ${progressBar(p, state)}</div>`;
}

// ---------------------------------------------------------------- transactions

async function editTransaction(tx, defaults = {}) {
  if (!store.accounts.length) { toast("Add an account first", true); return; }
  const isNew = !tx;
  tx = tx || {};
  const amount = tx.amount_cents ?? null;
  const v = await formDialog(isNew ? "New transaction" : "Edit transaction", [
    { name: "type", label: "Type", type: "select", options:
      `<option value="out" ${amount === null || amount < 0 ? "selected" : ""}>Expense (money out)</option>
       <option value="in" ${amount > 0 ? "selected" : ""}>Income (money in)</option>` },
    { name: "amount", label: "Amount", type: "money", value: amount === null ? "" : dollars(Math.abs(amount)), required: true },
    { name: "date", label: "Date", type: "date", value: tx.date || todayISO(), required: true },
    { name: "account_id", label: "Account", type: "select", options: accountOptions(tx.account_id || defaults.account_id), required: true },
    { name: "payee", label: "Payee", value: tx.payee || "", wide: true },
    { name: "category_id", label: "Category", type: "select", options: categoryOptions(tx.category_id), wide: true },
    { name: "memo", label: "Memo", value: tx.memo || "", wide: true },
    { name: "cleared", label: "Cleared / reconciled", type: "checkbox", value: !!tx.cleared },
  ]);
  if (!v) return;
  const cents = Math.abs(toCents(v.amount)) * (v.type === "out" ? -1 : 1);
  const body = { account_id: Number(v.account_id), date: v.date, amount_cents: cents, payee: v.payee,
                 category_id: v.category_id || null, memo: v.memo, cleared: v.cleared ? 1 : 0 };
  if (isNew) await attempt(() => api("POST", "/api/transactions", body), "Transaction added");
  else await attempt(() => api("PUT", `/api/transactions/${tx.id}`, body), "Transaction updated");
}

async function newTransfer() {
  if (store.accounts.length < 2) { toast("You need at least two accounts", true); return; }
  const v = await formDialog("Transfer between accounts", [
    { name: "from", label: "From", type: "select", options: accountOptions(store.accounts[0].id) },
    { name: "to", label: "To", type: "select", options: accountOptions(store.accounts[1].id) },
    { name: "amount", label: "Amount", type: "money", required: true },
    { name: "date", label: "Date", type: "date", value: todayISO() },
    { name: "memo", label: "Memo", wide: true, hint: "Use transfers for card payments, savings deposits and loan payments so they aren't counted as spending." },
  ], "Transfer");
  if (!v) return;
  await attempt(() => api("POST", "/api/transfers", { from_account_id: Number(v.from), to_account_id: Number(v.to),
    amount_cents: toCents(v.amount), date: v.date, memo: v.memo }), "Transfer recorded");
}

async function importCSV(defaultAccount) {
  const v = await formDialog("Import bank CSV", [
    { name: "account_id", label: "Into account", type: "select", options: accountOptions(defaultAccount), wide: true },
    { name: "file", label: "CSV file", type: "file", accept: ".csv,text/csv", wide: true,
      hint: "Needs a date column and either an amount column or debit/credit columns. Payee, category and memo columns are used when present. Duplicates are skipped." },
    { name: "invert", label: "Purchases are positive numbers (common for credit cards)", type: "checkbox" },
    { name: "day_first", label: "Dates are day-first (DD/MM/YYYY)", type: "checkbox" },
  ], "Import");
  if (!v || !v.file) return;
  const text = await v.file.text();
  const r = await attempt(() => api("POST", "/api/import", { account_id: Number(v.account_id), csv: text,
    invert: v.invert, day_first: v.day_first }));
  toast(`Imported ${r.imported}, skipped ${r.duplicates} duplicates` + (r.errors.length ? `, ${r.errors.length} errors` : ""), r.errors.length > 0);
  if (r.errors.length) console.warn("Import errors:", r.errors);
}

pages.transactions = async (params) => {
  const f = Object.fromEntries(params);
  const qs = new URLSearchParams({ ...f, limit: 500 });
  const [txs, conns] = await Promise.all([GET(`/api/transactions?${qs}`), GET("/api/connections").catch(() => [])]);
  const total = txs.reduce((s, t) => s + t.amount_cents, 0);
  const lastSync = conns.map((c) => c.last_sync).filter(Boolean).sort().pop();
  const syncErrors = conns.filter((c) => c.last_error);
  view.innerHTML = `
    <div class="page-head"><h1>Transactions</h1>
      ${conns.length ? `<span class="muted small">Banks synced ${lastSync ? esc(lastSync.replace("T", " ").slice(0, 16)) : "never"}</span>
        <button class="ghost sync-banks" id="tx-sync">Sync banks</button>` : ""}
      <button class="ghost" id="import">Import CSV</button>
      <a class="btn ghost" style="background:transparent;color:var(--text-2);border-color:var(--border);text-decoration:none" href="/api/export.csv${f.start ? `?start=${f.start}&end=${f.end || ""}` : ""}">Export CSV</a>
      <button class="ghost" id="transfer">Transfer</button>
      <button id="add">+ Transaction</button></div>
    ${syncErrors.map((c) => `<div class="insight warning"><span class="icon">!</span><span>${esc(c.label)}: ${esc(c.last_error)}
      <a href="#accounts">Fix on the Accounts page</a></span></div>`).join("")}
    <div class="card">
      <form class="filters" id="filters">
        <input name="search" placeholder="Search payee or memo" value="${esc(f.search || "")}">
        <select name="account_id">${accountOptions(f.account_id, "All accounts")}</select>
        <select name="category_id">${categoryOptions(f.category_id, "All categories")}</select>
        <label class="field">From<input type="date" name="start" value="${esc(f.start || "")}"></label>
        <label class="field">To<input type="date" name="end" value="${esc(f.end || "")}"></label>
        <label class="check"><input type="checkbox" name="uncategorized" value="1" ${f.uncategorized ? "checked" : ""}> Uncategorized only</label>
        <button class="ghost" type="button" id="clear">Clear</button>
      </form>
      <div class="muted small" style="margin-bottom:6px">${txs.length}${txs.length === 500 ? "+" : ""} transactions · net ${money(total)}</div>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Payee</th><th class="hide-sm">Account</th><th>Category</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${txs.length ? txs.map((t) => `<tr data-id="${t.id}">
          <td>${esc(t.date)}</td>
          <td>${esc(t.payee)}${t.external_id ? ' <span class="pill info" title="Downloaded from your bank">bank</span>' : ""}${t.memo ? `<div class="muted small">${esc(t.memo)}</div>` : ""}</td>
          <td class="hide-sm">${esc(t.account_name)}</td>
          <td>${t.transfer_id ? `<button type="button" class="pill unlink" title="Money moved between your own accounts; not counted as spending or income. Click if this isn't a transfer.">Transfer</button>` : `<select class="cat" aria-label="Category">${categoryOptions(t.category_id)}</select>`}</td>
          ${moneyCell(t.amount_cents)}
          <td class="actions"><button class="link edit">Edit</button><button class="link danger del">Delete</button></td></tr>`).join("")
          : `<tr><td colspan="6" class="empty">No transactions match.</td></tr>`}</tbody></table></div>
    </div>`;
  const form = $("#filters");
  const apply = () => {
    const p = new URLSearchParams();
    for (const [k, v] of new FormData(form)) if (v) p.set(k, v);
    location.hash = `transactions?${p}`;
  };
  form.onchange = apply;
  form.onsubmit = (e) => { e.preventDefault(); apply(); };
  $("#clear").onclick = () => (location.hash = "transactions");
  const txSync = $("#tx-sync");
  if (txSync) txSync.onclick = () => syncBanks().catch(() => {});
  $("#add").onclick = () => editTransaction(null, { account_id: f.account_id }).then(route);
  $("#transfer").onclick = () => newTransfer().then(route);
  $("#import").onclick = () => importCSV(f.account_id).then(route);
  const byId = Object.fromEntries(txs.map((t) => [t.id, t]));
  $$("tbody tr[data-id]").forEach((tr) => {
    const t = byId[tr.dataset.id];
    $(".edit", tr).onclick = () => editTransaction(t).then(route);
    $(".del", tr).onclick = async () => {
      if (await confirmDialog(`Delete "${t.payee || "transaction"}" (${money(t.amount_cents)})?`))
        await attempt(() => api("DELETE", `/api/transactions/${t.id}`), "Deleted").then(route);
    };
    const unlink = $(".unlink", tr);
    if (unlink) unlink.onclick = async () => {
      if (await confirmDialog("Not a transfer? Both sides will become ordinary transactions you can categorize."))
        await attempt(() => api("POST", `/api/transactions/${t.id}/unlink-transfer`), "Split into two transactions").then(route);
    };
    const sel = $(".cat", tr);
    if (sel) sel.onchange = () => attempt(() => api("PUT", `/api/transactions/${t.id}`, { category_id: sel.value || null }), "Category updated");
  });
};

// -------------------------------------------------------------------- accounts

async function editAccount(a) {
  const isNew = !a;
  a = a || {};
  const v = await formDialog(isNew ? "New account" : `Edit ${a.name}`, [
    { name: "name", label: "Name", value: a.name || "", required: true, wide: true },
    { name: "type", label: "Type", type: "select", options: store.meta.account_types.map((t) =>
      `<option value="${t}" ${t === (a.type || "checking") ? "selected" : ""}>${titleCase(t)}</option>`).join("") },
    { name: "institution", label: "Institution", value: a.institution || "" },
    { name: "opening", label: "Opening balance", type: "money", value: dollars(a.opening_balance_cents ?? 0),
      hint: "Enter debts (cards, loans) as negative numbers." },
    { name: "rate", label: "Interest rate / APR %", type: "number", value: a.interest_rate ?? "" },
  ]);
  if (!v) return;
  const body = { name: v.name, type: v.type, institution: v.institution,
                 opening_balance_cents: toCents(v.opening), interest_rate: Number(v.rate) || 0 };
  if (isNew) await attempt(() => api("POST", "/api/accounts", body), "Account created");
  else await attempt(() => api("PUT", `/api/accounts/${a.id}`, body), "Account updated");
}

pages.accounts = async (params) => {
  const showArchived = params.get("archived") === "1";
  const accts = await GET(`/api/accounts${showArchived ? "?archived=1" : ""}`);
  const groups = {};
  for (const a of accts) (groups[a.type] = groups[a.type] || []).push(a);
  const assets = accts.filter((a) => !a.archived && a.balance_cents > 0).reduce((s, a) => s + a.balance_cents, 0);
  const debts = accts.filter((a) => !a.archived && a.balance_cents < 0).reduce((s, a) => s - a.balance_cents, 0);
  view.innerHTML = `
    <div class="page-head"><h1>Accounts</h1>
      <label class="check"><input type="checkbox" id="arch" ${showArchived ? "checked" : ""}> Show archived</label>
      <button id="add">+ Account</button></div>
    <div class="tiles">
      <div class="card tile"><div class="label">Assets</div><div class="value">${money(assets, { whole: true })}</div></div>
      <div class="card tile"><div class="label">Debts</div><div class="value">${money(debts, { whole: true })}</div></div>
      <div class="card tile"><div class="label">Net worth</div><div class="value">${money(assets - debts, { whole: true })}</div></div>
    </div>
    <div class="card">${accts.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Account</th><th>Type</th><th class="hide-sm">Institution</th><th class="num hide-sm">APR</th><th class="num hide-sm">Transactions</th><th class="num">Balance</th><th></th></tr></thead>
      <tbody>${Object.entries(groups).map(([type, list]) => list.map((a) => `<tr data-id="${a.id}">
        <td><a href="#transactions?account_id=${a.id}">${esc(a.name)}</a>${a.archived ? ' <span class="pill">archived</span>' : ""}</td>
        <td>${titleCase(type)}</td><td class="hide-sm">${esc(a.institution)}</td>
        <td class="num hide-sm">${a.interest_rate ? a.interest_rate + "%" : ""}</td>
        <td class="num hide-sm">${a.transaction_count}</td>${moneyCell(a.balance_cents)}
        <td class="actions"><button class="link edit">Edit</button><button class="link arch">${a.archived ? "Restore" : "Archive"}</button><button class="link danger del">Delete</button></td></tr>`).join("")).join("")}
      </tbody></table></div>` : `<div class="empty">No accounts yet. Add checking, savings, credit cards, loans and investments to track your whole picture.</div>`}</div>
    <div id="banks" style="margin-top:16px"></div>`;
  renderBanks($("#banks"));
  $("#arch").onchange = (e) => (location.hash = e.target.checked ? "accounts?archived=1" : "accounts");
  $("#add").onclick = () => editAccount(null).then(loadShared).then(route);
  const byId = Object.fromEntries(accts.map((a) => [a.id, a]));
  $$("tbody tr[data-id]").forEach((tr) => {
    const a = byId[tr.dataset.id];
    $(".edit", tr).onclick = () => editAccount(a).then(loadShared).then(route);
    $(".arch", tr).onclick = () => attempt(() => api("PUT", `/api/accounts/${a.id}`, { archived: a.archived ? 0 : 1 }),
      a.archived ? "Restored" : "Archived").then(loadShared).then(route);
    $(".del", tr).onclick = async () => {
      if (await confirmDialog(`Delete ${a.name} and all ${a.transaction_count} of its transactions? This cannot be undone.`))
        await attempt(() => api("DELETE", `/api/accounts/${a.id}`), "Deleted").then(loadShared).then(route);
    };
  });
};

// --------------------------------------------------------------- linked banks

async function connectBank() {
  const v = await formDialog("Connect a bank (SimpleFIN)", [
    { name: "token", label: "SimpleFIN setup token", type: "textarea", required: true, wide: true,
      hint: "1) Sign up at beta-bridge.simplefin.org and connect your banks there. 2) Under Apps, click 'New connection' to create a setup token. 3) Copy the whole token and paste it here. Each token works once. FinOrganizer only gets read-only access and can never move money." },
    { name: "label", label: "Name for this connection (optional)", wide: true },
  ], "Connect");
  if (!v) return false;
  toast("Connecting and downloading your accounts…");
  const r = await attempt(() => api("POST", "/api/connections", { setup_token: v.token, label: v.label }));
  if (r.warning) toast(r.warning, true);
  else toast(`Connected. Downloaded ${r.sync ? r.sync.imported : 0} transactions into ` +
    `${r.sync ? r.sync.added_accounts.join(", ") : "your accounts"}. You can change where each bank account goes below.`);
  return true;
}

let syncing = null;

// Download from every linked bank, then redraw whatever page is open so new
// transactions and balances appear immediately. Concurrent calls share one sync.
async function syncBanks({ quiet = false } = {}) {
  if (syncing) return syncing;
  syncing = (async () => {
    if (!quiet) toast("Syncing with your banks…");
    $$(".sync-banks").forEach((b) => { b.disabled = true; b.textContent = "Syncing…"; });
    let changed = !quiet;  // a background sync only redraws if something new arrived
    try {
      const results = await api("POST", "/api/connections/sync");
      const imported = results.reduce((s, r) => s + r.imported, 0);
      const matched = results.reduce((s, r) => s + r.matched, 0);
      const transfers = results.reduce((s, r) => s + (r.transfers_linked || 0), 0);
      const added = results.flatMap((r) => r.added_accounts || []);
      const errors = results.flatMap((r) => r.errors);
      changed = changed || imported > 0 || matched > 0 || transfers > 0 || added.length > 0 || errors.length > 0;
      if (!quiet || imported || errors.length)
        toast(`Imported ${imported} new transaction${imported === 1 ? "" : "s"} from your banks` +
          (matched ? `, matched ${matched} you'd already entered` : "") +
          (transfers ? `, recognised ${transfers} transfer${transfers === 1 ? "" : "s"} between your accounts` : "") +
          (added.length ? `; now syncing ${added.join(", ")}` : "") +
          (errors.length ? `. Problem: ${errors[0]}` : ""), errors.length > 0);
      return results;
    } catch (e) {
      changed = true;
      toast(e.message, true);
      throw e;
    } finally {
      syncing = null;
      if (changed) {
        await loadShared();
        await route();
      } else {
        $$(".sync-banks").forEach((b) => { b.disabled = false; b.textContent = b.id === "tx-sync" ? "Sync banks" : "Sync now"; });
      }
    }
  })();
  return syncing;
}

async function renderBanks(box) {
  const conns = await GET("/api/connections");
  const linkedTo = new Set(conns.flatMap((c) => c.accounts.map((r) => r.account_id)).filter(Boolean));
  const feedOptions = (r) => {
    const opts = [];
    if (r.status === "new") opts.push(`<option value="" selected>Choose…</option>`);
    opts.push(`<option value="new">Create a new account</option>`);
    opts.push(`<option value="ignore" ${r.status === "ignored" ? "selected" : ""}>Don't import</option>`);
    opts.push(`<optgroup label="Feed an existing account">${store.accounts
      .filter((a) => a.id === r.account_id || !linkedTo.has(a.id))
      .map((a) => `<option value="${a.id}" ${a.id === r.account_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</optgroup>`);
    return opts.join("");
  };
  box.innerHTML = `<div class="card">
    <div class="filters"><h2 style="margin:0 auto 0 0">Linked banks</h2>
      ${conns.length ? '<button class="ghost sync-banks" id="sync-banks">Sync now</button>' : ""}
      <button id="connect-bank">+ Connect bank</button></div>
    ${conns.length ? conns.map((c) => `<div class="conn" data-conn="${c.id}" style="margin-top:10px">
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:baseline">
        <strong>${esc(c.label)}</strong>
        <span class="muted small">via ${esc(c.host)} · last synced ${c.last_sync ? esc(c.last_sync.replace("T", " ")) : "never"}</span>
        ${c.last_error ? `<span class="pill warn">${esc(c.last_error)}</span>` : ""}
        <button class="link danger rm-conn" style="margin-left:auto">Disconnect</button></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Bank account</th><th class="num">Bank balance</th><th>Feeds into</th><th class="num hide-sm">Difference</th></tr></thead>
        <tbody>${c.accounts.map((r) => `<tr data-remote="${r.id}">
          <td>${esc(r.name)}<div class="muted small">${esc(r.institution)}${r.balance_date ? " · as of " + esc(r.balance_date) : ""}</div></td>
          ${moneyCell(r.balance_cents)}
          <td><select class="feed" aria-label="What ${esc(r.name)} feeds">${feedOptions(r)}</select>
            ${r.status === "new" ? ' <span class="pill info">new</span>' : ""}</td>
          <td class="num hide-sm">${r.difference_cents ? `<span class="neg">${money(r.difference_cents)}</span> <button class="link match">Match bank</button>`
            : (r.status === "linked" ? '<span class="pill good">in sync</span>' : "")}</td></tr>`).join("")}</tbody></table></div></div>`).join("")
    : `<p class="muted">Link your bank, credit card and loan accounts to download balances and transactions automatically instead of typing them in.
       This uses <strong>SimpleFIN Bridge</strong>, an inexpensive read-only service that connects to thousands of US banks. Click <em>Connect bank</em> for steps.</p>`}
  </div>`;
  $("#connect-bank", box).onclick = async () => { if (await connectBank()) { await loadShared(); route(); } };
  const syncBtn = $("#sync-banks", box);
  if (syncBtn) syncBtn.onclick = () => syncBanks().catch(() => {});
  $$(".conn", box).forEach((el) => {
    const c = conns.find((x) => x.id == el.dataset.conn);
    $(".rm-conn", el).onclick = async () => {
      if (await confirmDialog(`Disconnect ${c.label}? Accounts and transactions already imported are kept.`))
        await attempt(() => api("DELETE", `/api/connections/${c.id}`), "Disconnected").then(route);
    };
  });
  $$("tr[data-remote]", box).forEach((tr) => {
    const id = tr.dataset.remote;
    $(".feed", tr).onchange = async (e) => {
      const v = e.target.value;
      if (!v) return;
      const body = v === "new" || v === "ignore" ? { action: v } : { action: "link", account_id: Number(v) };
      await attempt(() => api("PUT", `/api/remote-accounts/${id}`, body),
        v === "ignore" ? "Won't import this account" : "Linked. Sync to download its history.");
      await loadShared();
      route();
    };
    const m = $(".match", tr);
    if (m) m.onclick = () => attempt(() => api("POST", `/api/remote-accounts/${id}/match-balance`),
      "Balance adjusted to match the bank").then(loadShared).then(route);
  });
}

// -------------------------------------------------------------------- profiles

let profileState = { current: "default", profiles: [] };

async function loadProfiles() {
  profileState = await GET("/api/profiles");
  const sel = $("#profile-select");
  sel.innerHTML = profileState.profiles.map((p) =>
    `<option value="${esc(p.id)}" ${p.id === profileState.current ? "selected" : ""}>${esc(p.name)}</option>`).join("") +
    `<option disabled>──────────</option><option value="__new">+ New profile…</option><option value="__manage">Manage profiles…</option>`;
}

async function switchProfile(id) {
  await attempt(() => api("POST", "/api/profiles/switch", { id }));
  await Promise.all([loadShared(), loadProfiles()]);
  const name = profileState.profiles.find((p) => p.id === id)?.name || id;
  toast(`Switched to ${name}`);
  if (location.hash === "#dashboard" || !location.hash) await route(); else location.hash = "dashboard";
  autoSync();
}

async function newProfile() {
  const v = await formDialog("New profile", [{ name: "name", label: "Whose finances? (e.g. a name)", required: true, wide: true,
    hint: "Each profile has its own accounts, budgets, goals and bank connections." }], "Create");
  if (!v) return;
  const r = await attempt(() => api("POST", "/api/profiles", { name: v.name }), "Profile created");
  await switchProfile(r.id);
}

// --------------------------------------------------------------------- updates

let updateInfo = null;
const pref = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; } };
const setPref = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* ignore */ } };

async function checkForUpdates() {
  updateInfo = await GET("/api/update").catch((e) => ({ error: e.message, available: false }));
  renderUpdateBanner();
  return updateInfo;
}

function renderUpdateBanner() {
  const b = $("#update-banner");
  const u = updateInfo;
  if (!u || !u.available || pref("dismissedUpdate", "") === u.latest) { b.hidden = true; return; }
  b.hidden = false;
  b.innerHTML = `<span>FinOrganizer <strong>${esc(u.latest)}</strong> is available (you have ${esc(u.current)}).</span>
    <a class="btn" id="upd-now" href="${esc(u.page)}" target="_blank" rel="noopener">${u.can_install && desktop() ? "Update now" : "Download"}</a>
    <a href="#settings">What's new</a>
    <button class="ghost" id="upd-later">Not now</button>`;
  wireUpdateLink($("#upd-now", b), u);
  $("#upd-later", b).onclick = () => { setPref("dismissedUpdate", u.latest); renderUpdateBanner(); };
}

// Update buttons are real links to the release page, so they work even if the desktop
// bridge isn't available; when it is, the app installs (Windows) or opens the page itself.
function wireUpdateLink(el, u) {
  el.onclick = (e) => {
    if (u.can_install && desktop()) { e.preventDefault(); installUpdate(); }
    else if (desktop() && desktop().open_url) { e.preventDefault(); openExternal(u.page); }
    // otherwise let the link open normally (browser tab / the window's external-link handling)
  };
}

async function installUpdate() {
  const u = updateInfo;
  if (!u) return;
  if (!(u.can_install && desktop())) {  // browser / source installs: open the download page
    openExternal(u.page);
    return;
  }
  $$("#upd-now, #settings-update").forEach((x) => { x.style.pointerEvents = "none"; x.textContent = "Downloading…"; });
  toast("Downloading the update. FinOrganizer will restart by itself.");
  const r = await desktop().install_update();
  toast(r.message, !r.ok);
  if (!r.ok) {
    $$("#upd-now, #settings-update").forEach((x) => { x.style.pointerEvents = ""; x.textContent = "Try again"; });
    if (r.page) openExternal(r.page);
  }
}

pages.settings = async () => {
  await loadProfiles();
  const auto = pref("autoUpdateCheck", "on") === "on";
  view.innerHTML = `<div class="page-head"><h1>Settings</h1></div>
    <div class="card" id="updates-card"><h2>Updates</h2>
      <dl class="kv" style="max-width:420px"><dt>Installed version</dt><dd>${esc(store.meta.version)}</dd>
        <dt>Latest version</dt><dd id="latest-version">${updateInfo && updateInfo.latest ? esc(updateInfo.latest) : "–"}</dd></dl>
      <div id="update-status" class="muted" style="margin:10px 0"></div>
      <div class="filters">
        <button class="ghost" id="check-now">Check for updates</button>
        <a class="btn" id="settings-update" target="_blank" rel="noopener" hidden>Update now</a>
        <label class="check"><input type="checkbox" id="auto-check" ${auto ? "checked" : ""}> Check automatically when FinOrganizer opens</label>
      </div>
      <div id="update-notes"></div>
      <p class="muted small">Updates replace only the program. Your data, profiles and bank connections are kept.</p>
    </div>
    <div style="height:16px"></div>
    <div id="profiles-section"></div>`;
  const show = (u) => {
    $("#latest-version").textContent = u.latest || "–";
    const btn = $("#settings-update");
    btn.hidden = !u.available;
    btn.textContent = u.can_install && desktop() ? `Update to ${u.latest}` : "Download the new version";
    btn.href = u.page;
    wireUpdateLink(btn, u);
    $("#update-status").innerHTML = u.error ? `<span class="neg">Couldn't check: ${esc(u.error)}</span>`
      : u.available ? `<strong>Version ${esc(u.latest)} is available.</strong>` : "You have the latest version.";
    $("#update-notes").innerHTML = u.available && u.notes ? `<h2 style="margin-top:12px">What's new</h2><div class="notes">${esc(u.notes)}</div>` : "";
  };
  if (updateInfo) show(updateInfo);
  $("#check-now").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Checking…";
    try { setPref("dismissedUpdate", ""); show(await checkForUpdates()); }
    finally { e.target.disabled = false; e.target.textContent = "Check for updates"; }
  };
  $("#auto-check").onchange = (e) => setPref("autoUpdateCheck", e.target.checked ? "on" : "off");
  await renderProfiles($("#profiles-section"));
};

pages.profiles = pages.settings;

async function renderProfiles(box) {
  box.innerHTML = `<div class="card"><div class="filters"><h2 style="margin:0 auto 0 0">Profiles</h2><button id="add">+ New profile</button></div>
    <p class="muted">Profiles keep separate finances for different people, each in its own data file. Switch profiles from the menu at the top.</p>
    <table><tbody>${profileState.profiles.map((p) => `<tr data-pid="${esc(p.id)}">
      <td><strong>${esc(p.name)}</strong> ${p.id === profileState.current ? '<span class="pill good">current</span>' : ""}</td>
      <td class="actions">${p.id === profileState.current ? "" : '<button class="link use">Switch to</button>'}
        <button class="link ren">Rename</button>${p.id === "default" ? "" : '<button class="link danger del">Delete</button>'}</td></tr>`).join("")}
    </tbody></table></div>`;
  $("#add", box).onclick = newProfile;
  $$("tr[data-pid]", box).forEach((tr) => {
    const p = profileState.profiles.find((x) => x.id === tr.dataset.pid);
    const use = $(".use", tr);
    if (use) use.onclick = () => switchProfile(p.id);
    $(".ren", tr).onclick = async () => {
      const v = await formDialog(`Rename ${p.name}`, [{ name: "name", label: "Name", value: p.name, required: true, wide: true }]);
      if (v) await attempt(() => api("PUT", `/api/profiles/${p.id}`, { name: v.name }), "Renamed").then(route);
    };
    const del = $(".del", tr);
    if (del) del.onclick = async () => {
      if (await confirmDialog(`Permanently delete ${p.name} and all of its data? This cannot be undone.`)) {
        await attempt(() => api("DELETE", `/api/profiles/${p.id}`), "Profile deleted");
        await loadShared();
        route();
      }
    };
  });
};

// --------------------------------------------------------------------- budgets

pages.budgets = async (params) => {
  const month = params.get("month") || thisMonth();
  const [status, prevStatus] = await Promise.all([
    GET(`/api/budgets?month=${month}`), GET(`/api/budgets?month=${shiftMonth(month, -1)}`)]);
  const byCat = Object.fromEntries(status.map((s) => [s.category_id, s]));
  const prevByCat = Object.fromEntries(prevStatus.map((s) => [s.category_id, s]));
  const expenseCats = store.categories.filter((c) => c.kind === "expense");
  const totalBudget = status.reduce((s, b) => s + b.budget_cents, 0);
  const totalSpent = status.reduce((s, b) => s + b.spent_cents, 0);
  const groups = {};
  for (const c of expenseCats) (groups[c.group_name || "Other"] = groups[c.group_name || "Other"] || []).push(c);
  view.innerHTML = `
    <div class="page-head"><h1>Budget · ${esc(monthLabel(month, true))}</h1>
      <button class="ghost" id="prev">‹</button><input type="month" id="month" value="${month}"><button class="ghost" id="next">›</button>
      <button class="ghost" id="copy">Copy last month</button><button class="ghost" id="addcat">+ Category</button></div>
    <div class="tiles">
      <div class="card tile"><div class="label">Budgeted</div><div class="value">${money(totalBudget, { whole: true })}</div></div>
      <div class="card tile"><div class="label">Spent</div><div class="value">${money(totalSpent, { whole: true })}</div></div>
      <div class="card tile"><div class="label">Remaining</div><div class="value ${totalBudget - totalSpent < 0 ? "neg" : ""}">${money(totalBudget - totalSpent, { whole: true })}</div>
        <div class="sub">${totalBudget ? pct(100 * totalSpent / totalBudget) + " used" : "set budgets below"}</div></div>
    </div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Category</th><th class="num">Budget</th><th class="num hide-sm">Last month spent</th><th class="num">Spent</th><th class="num">Left</th><th style="width:22%">Progress</th></tr></thead>
      <tbody>${Object.entries(groups).map(([g, cats]) => `<tr><th colspan="6" style="padding-top:14px">${esc(g)}</th></tr>` + cats.map((c) => {
        const b = byCat[c.id] || { budget_cents: 0, spent_cents: 0, remaining_cents: 0, percent_used: null };
        const p = b.percent_used ?? (b.spent_cents > 0 ? 101 : 0);
        return `<tr><td>${esc(c.name)}</td>
          <td class="num"><input class="budget" data-cat="${c.id}" inputmode="decimal" style="width:100px;text-align:right" value="${b.budget_cents ? dollars(b.budget_cents) : ""}" placeholder="0.00" aria-label="Budget for ${esc(c.name)}"></td>
          <td class="num hide-sm muted">${prevByCat[c.id] ? money(prevByCat[c.id].spent_cents) : ""}</td>
          <td class="num">${money(b.spent_cents)}</td>
          <td class="num ${b.remaining_cents < 0 ? "neg" : ""}">${b.budget_cents ? money(b.remaining_cents) : ""}</td>
          <td>${b.budget_cents || b.spent_cents ? progressBar(p, p > 100 ? "over" : p > 85 ? "warn" : "") : ""}</td></tr>`;
      }).join("")).join("")}</tbody></table></div>
      <p class="muted small">Type an amount and press Enter or click away to save. Clear the field to remove a budget.</p></div>`;
  const go = (m) => (location.hash = `budgets?month=${m}`);
  $("#month").onchange = (e) => e.target.value && go(e.target.value);
  $("#prev").onclick = () => go(shiftMonth(month, -1));
  $("#next").onclick = () => go(shiftMonth(month, 1));
  $("#copy").onclick = async () => {
    const r = await attempt(() => api("POST", "/api/budgets/copy", { from_month: shiftMonth(month, -1), to_month: month }));
    toast(`Copied ${r.copied} budget line(s)`);
    route();
  };
  $("#addcat").onclick = async () => {
    const v = await formDialog("New category", [
      { name: "name", label: "Name", required: true },
      { name: "kind", label: "Kind", type: "select", options: store.meta.category_kinds.map((k) => `<option ${k === "expense" ? "selected" : ""}>${k}</option>`).join("") },
      { name: "group_name", label: "Group", placeholder: "e.g. Housing" },
    ]);
    if (!v) return;
    await attempt(() => api("POST", "/api/categories", v), "Category added");
    await loadShared();
    route();
  };
  $$("input.budget").forEach((inp) => {
    const save = async () => {
      if (inp.dataset.saved === inp.value) return;
      try {
        await attempt(() => api("PUT", "/api/budgets", { category_id: Number(inp.dataset.cat), month, amount_cents: toCents(inp.value) }), "Budget saved");
        inp.dataset.saved = inp.value;
        // Re-render totals, keeping focus on whichever budget field the user moved to.
        await new Promise((r) => setTimeout(r));
        const next = document.activeElement?.dataset?.cat;
        await route();
        if (next) $(`input.budget[data-cat="${next}"]`)?.focus();
      } catch (_) { /* toast already shown */ }
    };
    inp.dataset.saved = inp.value;
    inp.addEventListener("change", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
  });
};

// ----------------------------------------------------------------------- goals

async function editGoal(g) {
  const isNew = !g;
  g = g || {};
  const v = await formDialog(isNew ? "New savings goal" : `Edit ${g.name}`, [
    { name: "name", label: "Goal", value: g.name || "", required: true, wide: true, placeholder: "e.g. Emergency fund" },
    { name: "target", label: "Target amount", type: "money", value: dollars(g.target_cents), required: true },
    { name: "target_date", label: "Target date", type: "date", value: g.target_date || "" },
    { name: "account_id", label: "Track an account's balance (optional)", type: "select", options: accountOptions(g.account_id, "No, track manually"), wide: true },
    { name: "saved", label: "Already saved (manual tracking)", type: "money", value: dollars(g.saved_cents ?? 0) },
    { name: "notes", label: "Notes", value: g.notes || "", wide: true },
  ]);
  if (!v) return;
  const body = { name: v.name, target_cents: toCents(v.target), target_date: v.target_date || null,
                 account_id: v.account_id ? Number(v.account_id) : null, saved_cents: toCents(v.saved), notes: v.notes };
  if (isNew) await attempt(() => api("POST", "/api/goals", body), "Goal created");
  else await attempt(() => api("PUT", `/api/goals/${g.id}`, body), "Goal updated");
}

pages.goals = async () => {
  const goals = await GET("/api/goals");
  view.innerHTML = `
    <div class="page-head"><h1>Savings goals</h1><button id="add">+ Goal</button></div>
    ${goals.length ? `<div class="grid cols-3">${goals.map((g) => `<div class="card stack" data-id="${g.id}">
      <div style="display:flex;justify-content:space-between;gap:8px"><h2 style="margin:0">${esc(g.name)}</h2>
        ${g.complete ? '<span class="pill">reached</span>' : ""}</div>
      <div><span class="tile"><span class="value" style="font-size:20px">${money(g.current_cents, { whole: true })}</span></span>
        <span class="muted"> of ${money(g.target_cents, { whole: true })}</span></div>
      ${progressBar(g.percent)}
      <dl class="kv small">
        <dt>Progress</dt><dd>${g.percent.toFixed(0)}%</dd>
        <dt>Remaining</dt><dd>${money(g.remaining_cents)}</dd>
        ${g.target_date ? `<dt>Target date</dt><dd>${esc(g.target_date)}</dd>` : ""}
        ${g.monthly_needed_cents ? `<dt>Save per month</dt><dd>${money(g.monthly_needed_cents)}</dd>` : ""}
        ${g.account_name ? `<dt>Tracks</dt><dd>${esc(g.account_name)}</dd>` : ""}
      </dl>
      ${g.notes ? `<div class="muted small">${esc(g.notes)}</div>` : ""}
      <div>${g.account_id ? "" : '<button class="ghost contrib">+ Add money</button>'}
        <button class="link edit">Edit</button><button class="link danger del">Delete</button></div>
    </div>`).join("")}</div>`
    : `<div class="card empty">No goals yet. Set a target, like an emergency fund, a trip or a down payment, and track your progress.</div>`}`;
  $("#add").onclick = () => editGoal(null).then(route);
  const byId = Object.fromEntries(goals.map((g) => [g.id, g]));
  $$("[data-id]").forEach((card) => {
    const g = byId[card.dataset.id];
    $(".edit", card).onclick = () => editGoal(g).then(route);
    $(".del", card).onclick = async () => {
      if (await confirmDialog(`Delete goal "${g.name}"?`)) await attempt(() => api("DELETE", `/api/goals/${g.id}`), "Deleted").then(route);
    };
    const c = $(".contrib", card);
    if (c) c.onclick = async () => {
      const v = await formDialog(`Add to ${g.name}`, [{ name: "amount", label: "Amount (negative to withdraw)", type: "money", required: true }], "Add");
      if (v) await attempt(() => api("POST", `/api/goals/${g.id}/contribute`, { amount_cents: toCents(v.amount) }), "Saved").then(route);
    };
  });
};

// ------------------------------------------------------------------- recurring

async function editRecurring(r) {
  if (!store.accounts.length) { toast("Add an account first", true); return; }
  const isNew = !r;
  r = r || {};
  const amount = r.amount_cents ?? null;
  const v = await formDialog(isNew ? "New bill or recurring item" : `Edit ${r.name}`, [
    { name: "name", label: "Name", value: r.name || "", required: true, wide: true, placeholder: "e.g. Rent, Netflix, Paycheck" },
    { name: "type", label: "Type", type: "select", options:
      `<option value="out" ${amount === null || amount < 0 ? "selected" : ""}>Bill / expense</option><option value="in" ${amount > 0 ? "selected" : ""}>Income</option>` },
    { name: "amount", label: "Amount", type: "money", value: amount === null ? "" : dollars(Math.abs(amount)), required: true },
    { name: "frequency", label: "Repeats", type: "select", options: store.meta.frequencies.map((f) =>
      `<option value="${f}" ${f === (r.frequency || "monthly") ? "selected" : ""}>${titleCase(f)}</option>`).join("") },
    { name: "next_date", label: "Next due", type: "date", value: r.next_date || todayISO(), required: true },
    { name: "account_id", label: "Account", type: "select", options: accountOptions(r.account_id) },
    { name: "category_id", label: "Category", type: "select", options: categoryOptions(r.category_id) },
    { name: "payee", label: "Payee", value: r.payee || "", wide: true },
  ]);
  if (!v) return;
  const body = { name: v.name, amount_cents: Math.abs(toCents(v.amount)) * (v.type === "out" ? -1 : 1),
    frequency: v.frequency, next_date: v.next_date, account_id: Number(v.account_id),
    category_id: v.category_id || null, payee: v.payee };
  if (isNew) await attempt(() => api("POST", "/api/recurring", body), "Saved");
  else await attempt(() => api("PUT", `/api/recurring/${r.id}`, body), "Updated");
}

const PER_MONTH = { weekly: 52 / 12, biweekly: 26 / 12, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12 };

pages.recurring = async () => {
  const [items, upcoming] = await Promise.all([GET("/api/recurring"), GET("/api/upcoming?days=60")]);
  const active = items.filter((i) => i.active);
  const monthlyOut = active.filter((i) => i.amount_cents < 0).reduce((s, i) => s - i.amount_cents * PER_MONTH[i.frequency], 0);
  const monthlyIn = active.filter((i) => i.amount_cents > 0).reduce((s, i) => s + i.amount_cents * PER_MONTH[i.frequency], 0);
  const due = upcoming.filter((u) => u.overdue || u.date <= todayISO());
  view.innerHTML = `
    <div class="page-head"><h1>Bills &amp; recurring</h1>
      <button class="ghost" id="post" ${due.length ? "" : "disabled"}>Record ${due.length} due item${due.length === 1 ? "" : "s"}</button>
      <button id="add">+ Recurring item</button></div>
    <div class="tiles">
      <div class="card tile"><div class="label">Recurring bills / month</div><div class="value">${money(Math.round(monthlyOut), { whole: true })}</div><div class="sub">${money(Math.round(monthlyOut * 12), { whole: true })} per year</div></div>
      <div class="card tile"><div class="label">Recurring income / month</div><div class="value">${money(Math.round(monthlyIn), { whole: true })}</div></div>
      <div class="card tile"><div class="label">Due in next 60 days</div><div class="value">${upcoming.length}</div><div class="sub">net ${money(upcoming.reduce((s, u) => s + u.amount_cents, 0))}</div></div>
    </div>
    <div class="grid cols-2">
      <div class="card"><h2>All recurring items</h2>${items.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Every</th><th>Next</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${items.map((i) => `<tr data-id="${i.id}" style="${i.active ? "" : "opacity:.55"}">
          <td>${esc(i.name)}<div class="muted small">${esc(i.account_name)}${i.category_name ? " · " + esc(i.category_name) : ""}</div></td>
          <td>${titleCase(i.frequency)}</td><td>${esc(i.next_date)}</td>${moneyCell(i.amount_cents)}
          <td class="actions"><button class="link edit">Edit</button><button class="link toggle">${i.active ? "Pause" : "Resume"}</button><button class="link danger del">Delete</button></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">Track rent, utilities, subscriptions and paychecks to see what's coming.</div>`}</div>
      <div class="card"><h2>Calendar, next 60 days</h2>${upcoming.length ? `<table><tbody>${upcoming.map((u) => `<tr>
        <td>${esc(u.date)}${u.overdue ? ' <span class="pill warn">overdue</span>' : ""}</td><td>${esc(u.name)}</td>${moneyCell(u.amount_cents)}</tr>`).join("")}</tbody></table>`
        : `<div class="muted">Nothing scheduled.</div>`}</div>
    </div>`;
  $("#add").onclick = () => editRecurring(null).then(route);
  $("#post").onclick = async () => {
    const r = await attempt(() => api("POST", "/api/recurring/post-due"));
    toast(`Recorded ${r.created.length} transaction(s)`);
    route();
  };
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  $$("tbody tr[data-id]").forEach((tr) => {
    const i = byId[tr.dataset.id];
    $(".edit", tr).onclick = () => editRecurring(i).then(route);
    $(".toggle", tr).onclick = () => attempt(() => api("PUT", `/api/recurring/${i.id}`, { active: i.active ? 0 : 1 })).then(route);
    $(".del", tr).onclick = async () => {
      if (await confirmDialog(`Delete "${i.name}"?`)) await attempt(() => api("DELETE", `/api/recurring/${i.id}`), "Deleted").then(route);
    };
  });
};

// --------------------------------------------------------------------- reports

pages.reports = async (params) => {
  const tab = params.get("tab") || "cashflow";
  const tabs = { cashflow: "Cash flow", spending: "Spending", networth: "Net worth", year: "Year in review" };
  view.innerHTML = `<div class="page-head"><h1>Reports</h1></div>
    <div class="tabs">${Object.entries(tabs).map(([k, v]) => `<button data-tab="${k}" class="${k === tab ? "active" : ""}">${v}</button>`).join("")}</div>
    <div id="report"></div>`;
  $$(".tabs button").forEach((b) => (b.onclick = () => (location.hash = `reports?tab=${b.dataset.tab}`)));
  const box = $("#report");

  if (tab === "cashflow") {
    const months = Number(params.get("months") || 12);
    const flows = await GET(`/api/reports/cashflow?months=${months}`);
    const tot = flows.reduce((a, f) => ({ i: a.i + f.income_cents, e: a.e + f.expense_cents }), { i: 0, e: 0 });
    box.innerHTML = `<div class="card"><div class="filters"><h2 style="margin:0 auto 0 0">Income vs expenses</h2>
        <select id="range">${[6, 12, 24].map((n) => `<option value="${n}" ${n === months ? "selected" : ""}>Last ${n} months</option>`).join("")}</select></div>
        <div id="chart"></div></div>
      <div class="card" style="margin-top:16px"><div class="table-wrap"><table>
        <thead><tr><th>Month</th><th class="num">Income</th><th class="num">Expenses</th><th class="num">Net</th><th class="num">Savings rate</th></tr></thead>
        <tbody>${flows.slice().reverse().map((f) => `<tr><td>${esc(monthLabel(f.month, true))}</td>${moneyCell(f.income_cents)}${moneyCell(f.expense_cents)}${moneyCell(f.net_cents)}<td class="num">${pct(f.savings_rate)}</td></tr>`).join("")}
        <tr><th>Total</th><th class="num">${money(tot.i)}</th><th class="num">${money(tot.e)}</th><th class="num">${money(tot.i - tot.e)}</th><th class="num">${tot.i ? pct(100 * (tot.i - tot.e) / tot.i) : "–"}</th></tr>
        </tbody></table></div></div>`;
    $("#chart").append(cashFlowChart(flows, $("#chart").clientWidth));
    $("#range").onchange = (e) => (location.hash = `reports?tab=cashflow&months=${e.target.value}`);
  }

  if (tab === "spending") {
    const month = params.get("month") || thisMonth();
    const start = params.get("start"), end = params.get("end");
    const q = start && end ? `start=${start}&end=${end}` : `month=${month}`;
    const r = await GET(`/api/reports/spending?${q}`);
    box.innerHTML = `<div class="card"><form class="filters" id="pf">
        <label class="field">Month<input type="month" name="month" value="${start ? "" : month}"></label>
        <span class="muted">or</span>
        <label class="field">From<input type="date" name="start" value="${esc(start || "")}"></label>
        <label class="field">To<input type="date" name="end" value="${esc(end || "")}"></label>
        <button>Apply</button></form>
        <div class="muted">${esc(r.start)} to ${esc(r.end)} · income ${money(r.summary.income_cents)} · spent ${money(r.summary.expense_cents)} · net ${money(r.summary.net_cents)}</div></div>
      <div class="grid cols-2" style="margin-top:16px">
        <div class="card"><h2>Spending by category</h2>${hBars(r.spending.map((c) => ({ label: c.category_name, value: c.spent_cents, note: `${c.percent.toFixed(0)}%` })), "var(--series-2)")}</div>
        <div class="stack">
          <div class="card"><h2>Income sources</h2>${r.income.length ? hBars(r.income.map((c) => ({ label: c.category_name, value: c.amount_cents }))) : '<div class="muted">No income in this period.</div>'}</div>
          <div class="card"><h2>Top payees</h2>${r.top_payees.length ? `<table><tbody>${r.top_payees.map((p) => `<tr><td>${esc(p.payee || "(no payee)")}</td><td class="num muted">${p.count}×</td><td class="num">${money(p.spent_cents)}</td></tr>`).join("")}</tbody></table>` : '<div class="muted">No spending.</div>'}</div>
        </div></div>`;
    $("#pf").onsubmit = (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      location.hash = fd.get("start") && fd.get("end")
        ? `reports?tab=spending&start=${fd.get("start")}&end=${fd.get("end")}`
        : `reports?tab=spending&month=${fd.get("month") || thisMonth()}`;
    };
  }

  if (tab === "networth") {
    const months = Number(params.get("months") || 12);
    const hist = await GET(`/api/reports/networth?months=${months}`);
    const first = hist[0].net_worth_cents, last = hist[hist.length - 1].net_worth_cents;
    box.innerHTML = `<div class="card"><div class="filters"><h2 style="margin:0 auto 0 0">Net worth over time</h2>
        <select id="range">${[6, 12, 24, 60].map((n) => `<option value="${n}" ${n === months ? "selected" : ""}>Last ${n} months</option>`).join("")}</select></div>
        <div class="tile" style="margin-bottom:10px"><span class="value">${money(last, { whole: true })}</span>
          <span class="${last - first < 0 ? "neg" : "pos"}"> ${last - first >= 0 ? "+" : ""}${money(last - first, { whole: true })}</span> <span class="muted">over ${months} months</span></div>
        <div id="chart"></div>
        <p class="muted small">Based on opening balances plus recorded transactions. Investment market gains are only included if you record them.</p></div>`;
    $("#chart").append(lineChart(hist.map((h) => ({ label: monthLabel(h.month), tip: monthLabel(h.month, true), value: h.net_worth_cents })), { valueLabel: "Net worth", width: $("#chart").clientWidth }));
    $("#range").onchange = (e) => (location.hash = `reports?tab=networth&months=${e.target.value}`);
  }

  if (tab === "year") {
    const year = Number(params.get("year") || new Date().getFullYear());
    const y = await GET(`/api/reports/year?year=${year}`);
    const s = y.summary;
    box.innerHTML = `<div class="filters"><button class="ghost" id="py">‹ ${year - 1}</button><strong>${year}</strong><button class="ghost" id="ny">${year + 1} ›</button></div>
      <div class="tiles">
        <div class="card tile"><div class="label">Income</div><div class="value">${money(s.income_cents, { whole: true })}</div></div>
        <div class="card tile"><div class="label">Spending</div><div class="value">${money(s.expense_cents, { whole: true })}</div></div>
        <div class="card tile"><div class="label">Saved</div><div class="value">${money(s.net_cents, { whole: true })}</div><div class="sub">savings rate ${pct(s.savings_rate)}</div></div>
        <div class="card tile"><div class="label">Avg monthly spend</div><div class="value">${money(Math.round(s.expense_cents / 12), { whole: true })}</div></div>
      </div>
      <div class="grid cols-2">
        <div class="card"><h2>Month by month</h2><div id="chart"></div></div>
        <div class="card"><h2>Spending by category</h2>${hBars(y.spending_by_category.slice(0, 12).map((c) => ({ label: c.category_name, value: c.spent_cents, note: `${c.percent.toFixed(0)}%` })), "var(--series-2)")}</div>
        <div class="card"><h2>Top payees</h2><table><tbody>${y.top_payees.map((p) => `<tr><td>${esc(p.payee || "(no payee)")}</td><td class="num muted">${p.count}×</td><td class="num">${money(p.spent_cents)}</td></tr>`).join("")}</tbody></table></div>
        <div class="card"><h2>Income sources</h2>${y.income_by_category.length ? hBars(y.income_by_category.map((c) => ({ label: c.category_name, value: c.amount_cents }))) : '<div class="muted">No income recorded.</div>'}</div>
      </div>`;
    $("#chart").append(cashFlowChart(y.by_month, $("#chart").clientWidth));
    $("#py").onclick = () => (location.hash = `reports?tab=year&year=${year - 1}`);
    $("#ny").onclick = () => (location.hash = `reports?tab=year&year=${year + 1}`);
  }
};

// -------------------------------------------------------------------- planning

function calcForm(id, fields, button = "Calculate") {
  return `<form id="${id}" class="form-grid">${fields.map((f) => `<label class="field">${esc(f.label)}
    <input name="${f.name}" value="${esc(f.value ?? "")}" inputmode="decimal" ${f.type === "money" ? 'placeholder="0.00"' : ""} required></label>`).join("")}
    <button>${esc(button)}</button></form>`;
}
const formVals = (form) => Object.fromEntries(new FormData(form));

pages.planning = async (params) => {
  const tab = params.get("tab") || "debt";
  const tabs = { debt: "Debt payoff", loan: "Loan calculator", retirement: "Retirement", savings: "Savings goal", emergency: "Emergency fund & 50/30/20" };
  view.innerHTML = `<div class="page-head"><h1>Planning</h1></div>
    <div class="tabs">${Object.entries(tabs).map(([k, v]) => `<button data-tab="${k}" class="${k === tab ? "active" : ""}">${v}</button>`).join("")}</div>
    <div id="plan"></div>`;
  $$(".tabs button").forEach((b) => (b.onclick = () => (location.hash = `planning?tab=${b.dataset.tab}`)));
  const box = $("#plan");

  if (tab === "loan") {
    box.innerHTML = `<div class="card">${calcForm("lf", [
      { name: "principal", label: "Loan amount", type: "money", value: "25000" },
      { name: "rate", label: "Interest rate (APR %)", value: "6.5" },
      { name: "months", label: "Term (months)", value: "60" },
      { name: "extra", label: "Extra payment / month", type: "money", value: "0" }])}</div><div id="out"></div>`;
    $("#lf").onsubmit = async (e) => {
      e.preventDefault();
      const v = formVals(e.target);
      const r = await attempt(() => api("POST", "/api/planning/loan", { principal_cents: toCents(v.principal), rate: Number(v.rate), months: Number(v.months), extra_cents: toCents(v.extra) }));
      const yearly = [];
      r.schedule.forEach((m, i) => {
        const y = Math.floor(i / 12);
        yearly[y] = yearly[y] || { year: y + 1, principal: 0, interest: 0, balance: 0 };
        yearly[y].principal += m.principal_cents; yearly[y].interest += m.interest_cents; yearly[y].balance = m.balance_cents;
      });
      $("#out").innerHTML = `<div class="tiles" style="margin-top:16px">
          <div class="card tile"><div class="label">Monthly payment</div><div class="value">${money(r.payment_cents + toCents(v.extra))}</div>${toCents(v.extra) ? `<div class="sub">${money(r.payment_cents)} + ${money(toCents(v.extra))} extra</div>` : ""}</div>
          <div class="card tile"><div class="label">Paid off in</div><div class="value">${r.months} mo</div><div class="sub">${(r.months / 12).toFixed(1)} years</div></div>
          <div class="card tile"><div class="label">Total interest</div><div class="value">${money(r.total_interest_cents, { whole: true })}</div></div>
          ${r.interest_saved_cents ? `<div class="card tile"><div class="label">Interest saved by extra</div><div class="value pos">${money(r.interest_saved_cents, { whole: true })}</div></div>` : ""}
        </div>
        <div class="card"><h2>Balance over time</h2><div id="lc"></div>
        <div class="table-wrap"><table><thead><tr><th>Year</th><th class="num">Principal paid</th><th class="num">Interest paid</th><th class="num">Remaining balance</th></tr></thead>
        <tbody>${yearly.map((y) => `<tr><td>${y.year}</td>${moneyCell(y.principal)}${moneyCell(y.interest)}${moneyCell(y.balance)}</tr>`).join("")}</tbody></table></div></div>`;
      $("#lc").append(lineChart([{ label: "0", tip: "Start", value: toCents(v.principal) },
        ...r.schedule.map((m) => ({ label: `${m.month}`, tip: `Month ${m.month}`, value: m.balance_cents }))], { valueLabel: "Balance", width: $("#lc").clientWidth }));
    };
  }

  if (tab === "debt") {
    const debts = store.accounts.filter((a) => ["credit_card", "loan", "mortgage"].includes(a.type) && a.balance_cents < 0)
      .map((a) => ({ name: a.name, balance: dollars(-a.balance_cents), rate: a.interest_rate || 0, min: dollars(Math.max(2500, Math.round(-a.balance_cents * 0.02))) }));
    if (!debts.length) debts.push({ name: "Credit card", balance: "5000.00", rate: 22.9, min: "100.00" });
    const row = (d) => `<tr><td><input name="name" value="${esc(d.name)}"></td><td><input name="balance" inputmode="decimal" value="${esc(d.balance)}" style="width:110px"></td>
      <td><input name="rate" inputmode="decimal" value="${esc(d.rate)}" style="width:70px"></td><td><input name="min" inputmode="decimal" value="${esc(d.min)}" style="width:100px"></td>
      <td><button type="button" class="link danger rm">Remove</button></td></tr>`;
    const minTotal = debts.reduce((s, d) => s + toCents(d.min), 0);
    box.innerHTML = `<div class="card"><p class="muted">Compare the <strong>avalanche</strong> method (pay the highest interest rate first: least interest) with the <strong>snowball</strong> method (pay the smallest balance first: quick wins). Prefilled from your debt accounts; minimum payments are estimated at 2% and can be edited.</p>
      <form id="df"><div class="table-wrap"><table><thead><tr><th>Debt</th><th>Balance</th><th>APR %</th><th>Min payment</th><th></th></tr></thead>
      <tbody id="debts">${debts.map(row).join("")}</tbody></table></div>
      <div class="filters" style="margin-top:10px"><button type="button" class="ghost" id="addd">+ Add debt</button>
        <label class="field">Total monthly payment<input name="budget" id="budget" inputmode="decimal" value="${dollars(Math.round(minTotal * 1.5))}"></label>
        <button>Compare plans</button></div></form></div><div id="out"></div>`;
    const wire = () => $$(".rm", box).forEach((b) => (b.onclick = () => b.closest("tr").remove()));
    wire();
    $("#addd").onclick = () => { $("#debts").insertAdjacentHTML("beforeend", row({ name: "", balance: "", rate: "", min: "" })); wire(); };
    $("#df").onsubmit = async (e) => {
      e.preventDefault();
      const list = $$("#debts tr").map((tr) => ({
        name: $("[name=name]", tr).value || "Debt", balance_cents: toCents($("[name=balance]", tr).value),
        rate: Number($("[name=rate]", tr).value) || 0, min_payment_cents: toCents($("[name=min]", tr).value) }));
      const r = await attempt(() => api("POST", "/api/planning/debt", { debts: list, budget_cents: toCents($("#budget").value) }));
      const card = (k, label) => {
        const p = r[k];
        return `<div class="card"><h2>${label}</h2><dl class="kv"><dt>Debt-free in</dt><dd>${p.months} months (${(p.months / 12).toFixed(1)} yrs)</dd>
          <dt>Total interest</dt><dd>${money(p.total_interest_cents)}</dd></dl>
          <h2 style="margin-top:14px">Payoff order</h2><table><tbody>${p.payoff_order.map((d, i) => `<tr><td>${i + 1}. ${esc(d.name)}</td><td class="num">month ${d.paid_off_month}</td><td class="num muted">${money(d.interest_cents)} interest</td></tr>`).join("")}</tbody></table></div>`;
      };
      const saved = r.snowball.total_interest_cents - r.avalanche.total_interest_cents;
      $("#out").innerHTML = `<div class="card" style="margin-top:16px">${saved > 0
          ? `Avalanche saves <strong>${money(saved)}</strong> in interest compared with snowball.`
          : "Both methods cost the same here, so pick whichever keeps you motivated."}</div>
        <div class="grid cols-2" style="margin-top:16px">${card("avalanche", "Avalanche (highest rate first)")}${card("snowball", "Snowball (smallest balance first)")}</div>
        <div class="card" style="margin-top:16px"><h2>Total balance, avalanche plan</h2><div id="dc"></div></div>`;
      $("#dc").append(lineChart(r.avalanche.timeline.map((t) => ({ label: `${t.month}`, tip: `Month ${t.month}`, value: t.total_balance_cents })), { valueLabel: "Remaining debt", width: $("#dc").clientWidth }));
    };
  }

  if (tab === "retirement") {
    const retirementBal = store.accounts.filter((a) => ["retirement", "investment"].includes(a.type)).reduce((s, a) => s + a.balance_cents, 0);
    box.innerHTML = `<div class="card">${calcForm("rf", [
      { name: "current_age", label: "Current age", value: "35" },
      { name: "retirement_age", label: "Retirement age", value: "65" },
      { name: "savings", label: "Current retirement savings", type: "money", value: dollars(retirementBal) },
      { name: "monthly", label: "Monthly contribution", type: "money", value: "500" },
      { name: "income", label: "Desired yearly income (today's $)", type: "money", value: "60000" },
      { name: "annual_return", label: "Expected return %", value: "7" },
      { name: "inflation", label: "Inflation %", value: "2.5" },
      { name: "withdrawal_rate", label: "Withdrawal rate %", value: "4" },
      { name: "contribution_growth", label: "Raise contributions % / yr", value: "0" }])}</div><div id="out"></div>`;
    $("#rf").onsubmit = async (e) => {
      e.preventDefault();
      const v = formVals(e.target);
      const r = await attempt(() => api("POST", "/api/planning/retirement", {
        current_age: Number(v.current_age), retirement_age: Number(v.retirement_age), current_savings_cents: toCents(v.savings),
        monthly_contribution_cents: toCents(v.monthly), desired_annual_income_cents: toCents(v.income),
        annual_return: Number(v.annual_return), inflation: Number(v.inflation), withdrawal_rate: Number(v.withdrawal_rate),
        contribution_growth: Number(v.contribution_growth) }));
      $("#out").innerHTML = `<div class="card insight ${r.on_track ? "good" : "warning"}" style="margin-top:16px">
          <span class="icon">${r.on_track ? "✓" : "!"}</span><span>${r.on_track
            ? "You're on track to fund your desired retirement income."
            : `You're projected to fall short by ${money(r.shortfall_cents, { whole: true })} (today's money). Saving about <strong>${money(r.extra_monthly_needed_cents)}</strong> more per month would close the gap.`}</span></div>
        <div class="tiles" style="margin-top:12px">
          <div class="card tile"><div class="label">Balance at ${esc(v.retirement_age)}</div><div class="value">${money(r.projected_balance_cents, { whole: true })}</div><div class="sub">${money(r.projected_real_balance_cents, { whole: true })} in today's money</div></div>
          <div class="card tile"><div class="label">Sustainable income</div><div class="value">${money(r.sustainable_annual_income_cents, { whole: true })}/yr</div><div class="sub">today's money, at ${esc(v.withdrawal_rate)}% withdrawal</div></div>
          <div class="card tile"><div class="label">Target nest egg</div><div class="value">${money(r.target_nest_egg_cents, { whole: true })}</div><div class="sub">today's money</div></div>
        </div>
        <div class="card"><h2>Projected balance (today's money)</h2><div id="rc"></div></div>`;
      $("#rc").append(lineChart(r.projection.map((p) => ({ label: `${Number(v.current_age) + p.year}`, tip: `Age ${Number(v.current_age) + p.year}`, value: p.real_balance_cents })), { valueLabel: "Balance (today's $)", width: $("#rc").clientWidth }));
    };
  }

  if (tab === "savings") {
    box.innerHTML = `<div class="card">${calcForm("sf", [
      { name: "target", label: "Target amount", type: "money", value: "10000" },
      { name: "current", label: "Already saved", type: "money", value: "0" },
      { name: "months", label: "Months to reach it", value: "24" },
      { name: "rate", label: "Savings interest rate %", value: "4" }])}</div><div id="out"></div>`;
    $("#sf").onsubmit = async (e) => {
      e.preventDefault();
      const v = formVals(e.target);
      const r = await attempt(() => api("POST", "/api/planning/savings", { target_cents: toCents(v.target), current_cents: toCents(v.current), months: Number(v.months), rate: Number(v.rate) }));
      $("#out").innerHTML = `<div class="tiles" style="margin-top:16px">
        <div class="card tile"><div class="label">Save each month</div><div class="value">${money(r.monthly_cents)}</div></div>
        <div class="card tile"><div class="label">Total deposits</div><div class="value">${money(r.total_deposits_cents, { whole: true })}</div></div>
        <div class="card tile"><div class="label">Interest earned</div><div class="value">${money(r.interest_earned_cents, { whole: true })}</div></div></div>
        <div class="card"><button id="mkgoal">Create this as a goal</button></div>`;
      $("#mkgoal").onclick = async () => {
        const d = new Date(); d.setMonth(d.getMonth() + Number(v.months));
        await attempt(() => api("POST", "/api/goals", { name: "Savings goal", target_cents: toCents(v.target), saved_cents: toCents(v.current), target_date: d.toISOString().slice(0, 10) }), "Goal created");
        location.hash = "goals";
      };
    };
  }

  if (tab === "emergency") {
    const r = await GET("/api/reports/averages?months=3");
    box.innerHTML = `<div class="card">${calcForm("ef", [
      { name: "expenses", label: "Monthly expenses (3-month avg from your data)", type: "money", value: dollars(r.expense_cents) },
      { name: "months_target", label: "Months of cover", value: "6" }], "Check")}</div><div id="out"></div>`;
    $("#ef").onsubmit = async (e) => {
      e.preventDefault();
      const v = formVals(e.target);
      const x = await attempt(() => api("POST", "/api/planning/emergency", { monthly_expenses_cents: toCents(v.expenses), months_target: Number(v.months_target) }));
      const p = x.target_cents ? 100 * x.current_cents / x.target_cents : 0;
      $("#out").innerHTML = `<div class="grid cols-2" style="margin-top:16px">
        <div class="card"><h2>Emergency fund</h2>
          <dl class="kv"><dt>Target (${esc(v.months_target)} months)</dt><dd>${money(x.target_cents)}</dd>
          <dt>Cash in checking, savings &amp; cash accounts</dt><dd>${money(x.current_cents)}</dd>
          <dt>Months covered</dt><dd>${x.months_covered ?? "–"}</dd>
          <dt>Still needed</dt><dd>${money(x.gap_cents)}</dd></dl>
          <div style="margin-top:12px">${progressBar(p, p < 50 ? "warn" : "")}</div></div>
        <div class="card"><h2>50/30/20 guideline</h2>${x.rule ? `<p class="muted">Based on your average monthly income of ${money(x.avg_income_cents)}:</p>
          <dl class="kv"><dt>Needs (50%): housing, food, bills</dt><dd>${money(x.rule.needs_cents)}</dd>
          <dt>Wants (30%): dining, fun, shopping</dt><dd>${money(x.rule.wants_cents)}</dd>
          <dt>Savings &amp; debt (20%)</dt><dd>${money(x.rule.savings_cents)}</dd></dl>` : '<p class="muted">Record some income to see a suggested split.</p>'}</div></div>`;
    };
    $("#ef").requestSubmit();
  }
};

// ---------------------------------------------------------------------- router

async function route() {
  hideTip();
  const [name, query] = (location.hash.slice(1) || "dashboard").split("?");
  const page = pages[name] ? name : "dashboard";
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${page}`));
  try {
    await pages[page](new URLSearchParams(query || ""));
  } catch (e) {
    view.innerHTML = `<div class="card empty">Something went wrong: ${esc(e.message)}</div>`;
  }
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (_) { /* storage unavailable */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").onclick = () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (_) { /* ignore */ }
  };
}

// The pywebview desktop shell exposes native helpers here; absent in a normal browser.
const desktop = () => (window.pywebview && window.pywebview.api) || null;

// App windows ignore window.open for outside sites, so the desktop app hands them
// to the system browser instead.
function openExternal(url) {
  if (desktop() && desktop().open_url) desktop().open_url(url);
  else window.open(url, "_blank");
}

function initToolbar() {
  const sel = $("#profile-select");
  sel.onchange = async () => {
    const v = sel.value;
    sel.value = profileState.current;  // keep showing the active profile until a switch succeeds
    if (v === "__new") newProfile();
    else if (v === "__manage") location.hash = "settings";
    else if (v && v !== profileState.current) switchProfile(v);
  };
  $("#print-btn").onclick = () => {
    const month = new URLSearchParams(location.hash.split("?")[1] || "").get("month") || thisMonth();
    // In the desktop app the summary opens in its own native window.
    if (desktop()) desktop().open_summary(month);
    else window.open(`/summary?print=1&month=${encodeURIComponent(month)}`, "_blank");
  };
  // Desktop app: save exports through a native "Save as" dialog instead of a download.
  document.addEventListener("click", async (e) => {
    const a = e.target.closest("a[href^='/api/export.csv']");
    if (!a || !desktop()) return;
    e.preventDefault();
    const q = new URLSearchParams(a.getAttribute("href").split("?")[1] || "");
    const saved = await desktop().export_csv(q.get("start") || "", q.get("end") || "");
    if (saved) toast(`Saved ${saved}`);
  });
}

window.addEventListener("hashchange", route);
// The desktop bridge can become ready after the first render: refresh update buttons then.
window.addEventListener("pywebviewready", () => {
  renderUpdateBanner();
  if (location.hash.startsWith("#settings")) route();
});
// Failed actions already show their error in a toast (see attempt()); don't also
// surface them as uncaught errors.
window.addEventListener("unhandledrejection", (e) => {
  console.warn("Handled:", e.reason && e.reason.message ? e.reason.message : e.reason);
  e.preventDefault();
});
initTheme();
initToolbar();
// Keep bank data fresh: sync on open and every so often while the app stays open.
const AUTO_SYNC_HOURS = 6;
async function autoSync() {
  const conns = await GET("/api/connections").catch(() => []);
  const stale = conns.some((c) => !c.last_sync ||
    Date.now() - new Date(c.last_sync).getTime() > AUTO_SYNC_HOURS * 3600e3);
  if (conns.length && stale) await syncBanks({ quiet: true }).catch(() => {});
}
setInterval(autoSync, 30 * 60e3);

// Show the installed version, and say so once after an update has been installed.
function announceVersion() {
  const v = store.meta && store.meta.version;
  if (!v) return;
  $("#app-version").textContent = "v" + v;
  const previous = pref("lastVersion", "");
  if (previous && previous !== v) toast(`FinOrganizer was updated to ${v}`);
  setPref("lastVersion", v);
}

Promise.all([loadShared(), loadProfiles()]).then(route).then(() => {
  announceVersion();
  autoSync();
  if (pref("autoUpdateCheck", "on") === "on") checkForUpdates();
}).catch((e) => {
  view.innerHTML = `<div class="card empty">Could not reach the FinOrganizer server: ${esc(e.message)}</div>`;
});
