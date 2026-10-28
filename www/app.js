// ansiblefleet web app. Renders data/status.json (written by `python -m fleet check`)
// and drives the action API (/api/*, fleet/web.py). No libraries, no build step.
// Layout: sidebar | machines (top) / terminal with one tab per job (bottom).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const short = h => h.split(".")[0];
const store = {
  get(k, d) { try { const v = localStorage.getItem("fleet." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("fleet." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

const S = {
  status: null, filter: "all", selected: new Set(), open: new Set(),
  console: false, running: [], jobs: [], logs: {},
  tabs: [], active: null,
  user: "", settings: null,
};

/* ---------- formatting ---------- */
function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}
function dur(sec) {
  if (sec == null) return "–";
  if (sec < 60) return "<1 min";
  if (sec < 3600) return Math.round(sec / 60) + " min";
  if (sec < 86400) return Math.round(sec / 3600) + " h";
  return Math.round(sec / 86400) + " d";
}
const UPDATES = {fleet: "All updates, daily", stock: "Security only", "dnf-automatic": "dnf-automatic", none: "Not automatic"};
const STATUS = {ok: "Up to date", reboot: "Reboot waiting", warn: "Check", fail: "Problem"};
const ACTION_LABEL = {patch: "Patch", reboot: "Reboot", "auto-updates": "Auto-update policy", refresh: "Refresh status",
  enroll: "Add machine", unenroll: "Remove machine", "reboot-controller": "Reboot controller", "refresh-snooze": "Refresh",
  "release-upgrade": "Release upgrade", "cancel-upgrade": "Cancel upgrade", "watch-upgrade": "Watching upgrade"};
const VIEW = {all: "All machines", attention: "Need attention", reboot: "Reboot waiting", ok: "Up to date"};
const READ_ONLY_JOBS = ["refresh", "refresh-snooze", "watch-upgrade"];
const busyHosts = () => new Set(S.jobs.filter(j => j.rc === null && !READ_ONLY_JOBS.includes(j.action)).flatMap(j => j.hosts));
const jobName = j => (ACTION_LABEL[j.action] || j.action) + (j.hosts.length ? " · " + j.hosts.map(short).join(", ") : "");

/* ---------- api ---------- */
async function api(path, body) {
  const opt = body ? {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-Fleet": "1"}} : {cache: "no-store"};
  const r = await fetch("/api/" + path, opt);
  let j = {}; try { j = await r.json(); } catch (e) { /* empty body */ }
  if (r.status === 401 && j.login) toLogin();
  if (!r.ok) { const e = new Error(j.error || "HTTP " + r.status); e.status = r.status; throw e; }
  return j;
}
function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast"; el.style.setProperty("--c", `var(--${kind || "accent"})`); el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "fail" ? 9000 : 5000);
}

function toLogin() { location.href = "/login?next=" + encodeURIComponent(location.pathname); }

/* ---------- status / machines ---------- */
async function loadStatus() {
  try {
    const r = await fetch("data/status.json", {cache: "no-store"});
    if (r.redirected && new URL(r.url).pathname === "/login") return toLogin();   // session expired
    if (!r.ok) throw new Error(`status data: HTTP ${r.status}`);
    S.status = await r.json();
    if (S.status.site?.name) { document.title = S.status.site.name; $("#site-name").textContent = S.status.site.name; }
    render();
  } catch (e) { $("#checked").textContent = "Could not load the status data (" + e.message + ")"; }
}
const isController = h => h.role === "controller";
const changeable = h => h.role === "managed";
const selectable = h => h.reachable && changeable(h) && !h.release.upgrading;
const hostInfo = name => (S.status?.hosts || []).find(h => h.host === name) || {};
function hostsShown() {
  const hs = S.status?.hosts || [];
  if (S.filter === "attention") return hs.filter(h => h.status === "fail" || h.status === "warn");
  if (S.filter === "reboot") return hs.filter(h => h.status === "reboot");
  if (S.filter === "ok") return hs.filter(h => h.status === "ok");
  return hs;
}

function render() {
  const st = S.status; if (!st) return;
  $("#checked").textContent = `Checked ${ago(st.time)}`;
  const sm = st.summary;
  const counts = {all: st.hosts.length, attention: sm.fail + sm.warn, reboot: sm.reboot, ok: sm.ok};
  $$("[data-count]").forEach(el => el.textContent = counts[el.dataset.count]);
  $$(".nav-item").forEach(b => {
    b.classList.toggle("on", b.dataset.filter === S.filter);
    b.classList.toggle("hot", ["attention", "reboot"].includes(b.dataset.filter) && counts[b.dataset.filter] > 0);
  });
  $("#view-title").textContent = VIEW[S.filter];
  const pend = st.hosts.reduce((a, h) => a + (h.pending || 0), 0);
  $("#view-sub").textContent = `${hostsShown().length} shown · ${pend} updates pending fleet-wide`;

  const busy = busyHosts();
  $("#rows").innerHTML = hostsShown().map(h => rowHtml(h, busy)).join("") ||
    `<p class="hint" style="padding:20px">Nothing in this view.</p>`;
  syncSelection();
}

function rowHtml(h, busy) {
  const isBusy = busy.has(h.host);
  const stuck = h.items.some(i => i.key.startsWith("relupstuck:"));
  const pill = !h.reachable ? `<span class="pill s-off">Unreachable</span>`
    : h.release.upgrading && stuck ? `<span class="pill s-warn">Upgrade waiting for input</span>`
    : h.release.upgrading ? `<span class="pill s-busy">Release upgrade</span>`
    : isBusy ? `<span class="pill s-busy">Job running</span>`
    : `<span class="pill s-${h.status}">${STATUS[h.status]}</span>`;
  const u = h.updates;
  const upd = h.reachable
    ? `${esc(UPDATES[u.policy] || u.policy || "–")}<div class="sub">last run ${ago(u.last)}${u.result && u.result !== "success" ? " · " + esc(u.result) : ""}</div>` : "–";
  const can = selectable(h) && S.console && !isBusy;
  const off = S.console ? "" : "disabled";
  const acts = isController(h)
    ? (S.status.site?.controller_reboot ? `<button class="btn sm danger" type="button" data-controller-reboot ${off}>Reboot…</button>` : "")
    : !changeable(h) ? ""
    : h.release.upgrading ? `<button class="btn sm ghost" type="button" data-watch="${esc(h.host)}" ${off}>Watch</button>
       <button class="btn sm danger" type="button" data-one="cancel-upgrade" data-host="${esc(h.host)}" ${off}>Cancel upgrade</button>`
    : h.reachable ? `<button class="btn sm ghost" type="button" data-one="patch" data-host="${esc(h.host)}" ${can ? "" : "disabled"}>Patch</button>
       <button class="btn sm ghost" type="button" data-one="reboot" data-host="${esc(h.host)}" ${can ? "" : "disabled"}>Reboot</button>` : "";
  const sel = selectable(h)
    ? `<input type="checkbox" class="pick" value="${esc(h.host)}" aria-label="Select ${esc(h.name)}" ${S.selected.has(h.host) ? "checked" : ""}>` : "";
  return `<div class="row ${S.open.has(h.host) ? "open" : ""} ${S.selected.has(h.host) ? "sel" : ""} ${h.reachable ? "" : "disabled"}" data-host="${esc(h.host)}">
    <div class="tr" role="row" data-toggle>
      <span class="c-sel">${sel}</span>
      <span class="c-host"><span class="name"><svg class="caret" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>${esc(h.name)}</span>
        <span class="os">${esc(h.os || "")}</span></span>
      <span class="c-status">${pill}</span>
      <span class="c-upd">${upd}</span>
      <span class="c-pend">${h.pending ?? "–"}</span>
      <span class="c-up">${h.reachable ? dur(h.uptime_s) : "–"}</span>
      <span class="c-act">${acts}</span>
    </div>
    <div class="detail">${detailHtml(h)}</div>
  </div>`;
}

function policyHtml(p) {
  const rows = [];
  if (p.reboot_blackout) rows.push(`<dt>No reboots</dt><dd>${p.reboot_blackout.hours_utc.map(x => String(x).padStart(2, "0") + ":00").join(", ")} UTC${p.reboot_blackout.reason ? " · " + esc(p.reboot_blackout.reason) : ""}</dd>`);
  if (p.manual_only !== null) rows.push(`<dt>Manual only</dt><dd>${esc(p.manual_only || "only patched when picked by name")}</dd>`);
  if (p.hold) rows.push(`<dt>Held back</dt><dd>${p.hold.packages.map(x => `<code>${esc(x)}</code>`).join(" ")}${p.hold.reason ? " · " + esc(p.hold.reason) : ""}</dd>`);
  if (!p.track_containers) rows.push(`<dt>Containers</dt><dd>not watched</dd>`);
  return rows.length ? `<div><h4>Policy</h4><dl class="kv">${rows.join("")}</dl></div>` : "";
}

function detailHtml(h) {
  const issues = h.items.length ? `<ul class="issues">${h.items.map(i => {
    const snoozed = i.text.includes("(snoozed until");
    const btn = ["fail", "warn", "reboot"].includes(i.sev)
      ? `<button class="btn sm ghost" type="button" data-snooze="${esc(i.key)}" data-days="7">Snooze 7 d</button>`
      : snoozed ? `<button class="btn sm ghost" type="button" data-snooze="${esc(i.key)}" data-days="0">Unsnooze</button>` : "";
    return `<li class="s-${i.sev}"><span>${esc(i.text)}</span>${btn}</li>`;
  }).join("")}</ul>` : `<p class="muted">Nothing to report.</p>`;
  let out = `<div><h4>Status</h4>${issues}</div>` + policyHtml(h.policy);
  const r = h.release;
  if (r.available && !r.upgrading && selectable(h))
    out += `<div><h4>New release</h4><p class="muted">${esc(r.available)} is available (optional; the current release keeps getting updates).
      Runs without questions, keeps existing config files, streams its log here and never reboots by itself.</p>
      <button class="btn sm ghost" type="button" data-one="release-upgrade" data-host="${esc(h.host)}" ${S.console && !busyHosts().has(h.host) ? "" : "disabled"}>Upgrade to ${esc(r.available)}…</button></div>`;
  if (r.upgrading)
    out += `<div class="wide"><h4>Release upgrade · last activity ${ago(r.activity)}</h4>
      <p class="muted">Upgrader log, newest line</p><pre class="report">${esc(r.last_line || "–")}</pre>
      <p class="muted">Last package step (dpkg)</p><pre class="report">${esc(r.dpkg_last || "–")}</pre></div>`;
  if (h.pending_pkgs.length)
    out += `<div><h4>Pending packages (${h.pending})</h4><div class="tags">${h.pending_pkgs.map(p => `<span class="tag">${esc(p)}</span>`).join("")}</div></div>`;
  if (h.containers.length)
    out += `<div><h4>Containers (${h.containers.length})</h4><div class="tags">${h.containers.map(c => `<span class="tag">${esc(c)}</span>`).join("")}</div></div>`;
  if (changeable(h))
    out += `<div><h4>Dashboard</h4>
      <button class="btn sm ghost" type="button" data-unenroll="${esc(h.host)}" ${S.console ? "" : "disabled"}>Remove from dashboard…</button></div>`;
  if (isController(h))
    out += `<div><h4>Controller</h4><p class="muted">Runs this tool. It is never patched from here${h.extra?.note ? "; " + esc(h.extra.note) : "."}</p></div>`;
  if (h.role === "report_only")
    out += `<div><h4>Report only</h4><p class="muted">Shown here, never changed from here.</p></div>`;
  if (h.extra?.report) out += `<div class="wide"><h4>Report</h4><pre class="report">${esc(h.extra.report)}</pre></div>`;
  return out;
}

function syncSelection() {
  const valid = new Set((S.status?.hosts || []).filter(selectable).map(h => h.host));
  S.selected.forEach(h => { if (!valid.has(h)) S.selected.delete(h); });
  const n = S.selected.size;
  $("#bulk").hidden = n === 0;
  $("#selcount").textContent = n + " selected";
  $$("[data-bulk]").forEach(b => b.disabled = !S.console);
  const shown = hostsShown().filter(selectable);
  const all = $("#sel-all");
  all.checked = shown.length > 0 && shown.every(h => S.selected.has(h.host));
  all.indeterminate = !all.checked && shown.some(h => S.selected.has(h.host));
  $$(".row").forEach(r => r.classList.toggle("sel", S.selected.has(r.dataset.host)));
}

/* ---------- dialog ---------- */
const ACTION_TEXT = {
  patch: ["Patch", "Installs every pending update now."],
  reboot: ["Reboot", "Reboots one machine at a time and waits for its containers to come back."],
  "auto-updates": ["Apply auto-update policy", "Sets the machine to install all updates every morning, never rebooting."],
  "release-upgrade": ["Release upgrade", `Brings the current release up to date, then upgrades to the next release with no questions asked
    (existing config files are kept). Takes 30–90 minutes; it runs on the machine itself, so closing this page does not stop it.
    It does not reboot: press Reboot afterwards when it suits you.`],
  "cancel-upgrade": ["Cancel upgrade", `Stops the upgrader and restores the package sources it rewrote. Safe while it is still waiting or
    downloading; refused once packages are being installed.`],
};
function ask({title, desc, opts = "", needPw = false, confirmWord = "", okText = "Run", danger = false, pwLabel = "Password"}) {
  return new Promise(resolve => {
    const d = $("#dlg");
    $("#dlg-title").textContent = title;
    $("#dlg-desc").innerHTML = desc;
    $("#dlg-opts").innerHTML = opts;
    $("#f-pw").hidden = !needPw;
    $("#f-pw span").textContent = pwLabel;
    $("#f-confirm").hidden = !confirmWord;
    $("#f-confirm b").textContent = confirmWord;
    ["#dlg-pw", "#dlg-confirm"].forEach(s => $(s).value = "");
    $("#dlg-err").textContent = "";
    const ok = $("#dlg-ok");
    ok.textContent = okText; ok.className = "btn" + (danger ? " danger" : "");
    const finish = val => { d.close(); $("#dlg-form").onsubmit = null; $("#dlg-cancel").onclick = null; resolve(val); };
    $("#dlg-cancel").onclick = () => finish(null);
    d.oncancel = () => finish(null);
    $("#dlg-form").onsubmit = e => {
      e.preventDefault();
      const pw = $("#dlg-pw").value;
      if (needPw && !pw) return void ($("#dlg-err").textContent = "Enter the password.");
      if (confirmWord && $("#dlg-confirm").value.trim() !== confirmWord) return void ($("#dlg-err").textContent = `Type ${confirmWord} to confirm.`);
      const o = {};
      $$("#dlg-opts input, #dlg-opts select").forEach(i => o[i.name] = i.type === "checkbox" ? i.checked : i.value.trim());
      finish({pw, opts: o});
    };
    d.showModal();
    setTimeout(() => (needPw ? $("#dlg-pw") : confirmWord ? $("#dlg-confirm") : ok).focus(), 30);
  });
}

/* Options that only make sense for some machines are shown only when one of them is selected,
   with that machine's own reason from its inventory policy. */
function actionOptions(action, hosts) {
  const info = hosts.map(hostInfo);
  if (action === "patch") {
    const manual = info.filter(h => h.policy?.manual_only !== null && h.policy?.manual_only !== undefined);
    return `<label class="check"><input type="checkbox" name="reboot"> Reboot afterwards if an update needs it</label>` +
      (manual.length ? `<p class="muted">Manual-only machines included because you picked them: ${manual.map(h =>
        `<b>${esc(h.name)}</b>${h.policy.manual_only ? " (" + esc(h.policy.manual_only) + ")" : ""}`).join(", ")}</p>` : "");
  }
  if (action === "reboot") {
    const windows = info.filter(h => h.policy?.reboot_blackout);
    if (!windows.length) return "";
    return `<label class="check"><input type="checkbox" name="force"> Ignore the no-reboot window of ${windows.map(h =>
      `<b>${esc(h.name)}</b>${h.policy.reboot_blackout.reason ? " (" + esc(h.policy.reboot_blackout.reason) + ")" : ""}`).join(", ")}</label>`;
  }
  return "";
}

async function runAction(action, hosts) {
  if (!hosts.length) return;
  const names = hosts.map(short);
  const [verb, what] = ACTION_TEXT[action];
  const big = action === "release-upgrade";
  const res = await ask({title: `${verb} ${names.join(", ")}`,
    desc: big ? `<div class="warnbox">${what}</div>` : esc(what), opts: actionOptions(action, hosts),
    okText: verb, confirmWord: big ? "yes" : "", danger: big || action === "cancel-upgrade"});
  if (!res) return;
  try {
    const j = await api("run", {action, hosts, reboot: !!res.opts.reboot, force: !!res.opts.force});
    toast(`${verb} started on ${names.join(", ")}`, "ok");
    S.selected.clear();
    await loadState(); openTab(j.job, true); render();
  } catch (e) { toast(e.message, "fail"); }
}
async function rebootController() {
  const h = (S.status?.hosts || []).find(isController) || {};
  const res = await ask({title: `Reboot ${esc(h.name || "the controller")}`,
    desc: `<div class="warnbox">This machine runs the dashboard itself (and whatever else it hosts); it goes away until it is back up.</div>
      ${h.extra?.note ? `<p>${esc(h.extra.note)}</p>` : ""}`,
    confirmWord: "yes", okText: "Reboot", danger: true});
  if (!res) return;
  try {
    const j = await api("reboot-controller", {confirm: "yes"});
    await loadState(); openTab(j.job, true);
    toast("Rebooting. This page comes back when the machine is up.", "reboot");
  } catch (e) { toast(e.message, "fail"); }
}
async function snooze(key, days) {
  const res = await ask({title: days ? "Snooze for 7 days" : "Unsnooze",
    desc: days ? "The alert stays on the page as information and stops sending notifications." : "The alert becomes active again.",
    okText: days ? "Snooze" : "Unsnooze"});
  if (!res) return;
  try { await api("snooze", {key, days}); toast(days ? "Snoozed" : "Unsnoozed", "ok"); setTimeout(loadStatus, 4000); }
  catch (e) { toast(e.message, "fail"); }
}

async function enrollMachine() {
  const site = S.status?.site || {};
  const res = await ask({title: "Add a machine",
    desc: `Debian or Ubuntu, reachable over SSH. An admin account's password is used <b>once</b>, to create the
      local <code>${esc(site.service_account || "service")}</code> account (key login from the controller only, passwordless sudo).
      It is not stored. Everything after that runs as the service account. The host key is trusted on first use and logged.`,
    opts: `<label class="field"><span>Host name</span><input type="text" name="host" placeholder="${site.domain ? "name or name" + esc(site.domain) : "host name"}" autocomplete="off" spellcheck="false"></label>
      <label class="field"><span>Admin account on that machine (can sudo)</span><input type="text" name="admin" value="${esc(S.user || "")}" autocomplete="off" spellcheck="false"></label>`,
    needPw: true, pwLabel: "Its password", okText: "Add machine"});
  if (!res) return;
  if (!res.opts.host || !res.opts.admin) return void toast("Enter the host name and an admin account.", "fail");
  try {
    const j = await api("enroll", {host: res.opts.host, admin: res.opts.admin, password: res.pw});
    await loadState(); openTab(j.job, true);
    toast(`Enrolling ${res.opts.host}…`, "ok");
  } catch (e) { toast(e.message, "fail"); }
}
async function unenrollMachine(host) {
  const res = await ask({title: `Remove ${short(host)} from the dashboard`,
    desc: "Takes it out of the inventory. Nothing on the machine changes: its daily auto-updates keep running. Add it back any time.",
    okText: "Remove", danger: true});
  if (!res) return;
  try { const j = await api("unenroll", {host}); await loadState(); openTab(j.job, true); }
  catch (e) { toast(e.message, "fail"); }
}

async function openSettings() {
  let st;
  try { st = await api("settings"); } catch (e) { return void toast(e.message, "fail"); }
  const fields = Object.entries(st.editable).map(([key, f]) =>
    `<label class="field"><span>${esc(f.label)}</span><input type="text" name="${esc(key)}" value="${esc(f.value)}" spellcheck="false"></label>`).join("");
  const res = await ask({title: "Settings",
    desc: `<dl class="kv"><dt>Service account</dt><dd><code>${esc(st.service_account)}</code> (local on every machine)</dd>
      <dt>Its SSH key</dt><dd><code>${esc(st.key_fingerprint)}</code></dd>
      <dt>Signed in as</dt><dd>${esc(S.user)}</dd></dl>
      <p class="muted">Everything else is in the site configuration file.</p>`,
    opts: fields, okText: "Save"});
  if (!res) return;
  const changes = {};
  for (const [key, f] of Object.entries(st.editable)) {
    const v = res.opts[key];
    if (v === undefined || String(v) === String(f.value)) continue;
    changes[key] = typeof f.value === "number" ? Number(v) : v;
  }
  if (!Object.keys(changes).length) return void toast("Nothing changed");
  try { await api("settings", changes); toast("Settings saved", "ok"); }
  catch (e) { toast(e.message, "fail"); }
}

/* ---------- console state, sidebar activity ---------- */
async function loadState() {
  const el = $("#console-state");
  const wasOnline = S.console;
  try {
    const s = await api("state");
    S.console = true; S.jobs = s.jobs || [];
    if (s.user && s.user !== S.user) { S.user = s.user; $("#who").textContent = s.user; }
    el.className = "console-state on"; $(".label", el).textContent = "actions ready";
    if (s.locked_s) { el.className = "console-state off"; $(".label", el).textContent = `password locked ${Math.ceil(s.locked_s / 60)} min`; }
    const was = S.running;
    S.running = s.running || [];
    S.running.forEach(id => { if (!S.tabs.includes(id)) openTab(id, !S.active); });
    if (was.some(id => !S.running.includes(id))) setTimeout(loadStatus, 1500);
    // the console forgets jobs when it restarts: drop tabs it no longer knows
    S.tabs = S.tabs.filter(id => S.jobs.some(j => j.id === id));
    if (S.active && !S.tabs.includes(S.active)) S.active = S.tabs[S.tabs.length - 1] || null;
  } catch (e) {
    S.console = false; el.className = "console-state off"; $(".label", el).textContent = "actions offline";
  }
  renderJobList(); renderTabs();
  if (S.console !== wasOnline) render();     // buttons depend on it; the first status render ran before this
}
const jobState = j => j.rc === null ? "run" : j.rc === 0 ? "ok" : "fail";
function renderJobList() {
  const box = $("#joblist");
  if (!S.jobs.length) { box.innerHTML = `<p class="side-empty">No jobs since the console started.</p>`; return; }
  box.innerHTML = S.jobs.map(j => `<button type="button" class="jl ${j.id === S.active ? "on" : ""}" data-openjob="${esc(j.id)}">
      <span class="st ${jobState(j)}"></span><span class="jl-t"><b>${esc(jobName(j))}</b>
      <small>${new Date(j.start * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}${j.end ? " · " + dur(j.end - j.start) : " · running"}${j.user ? " · " + esc(j.user) : ""}</small></span></button>`).join("");
}

/* ---------- terminal (bottom pane) ---------- */
function saveTabs() { store.set("tabs", S.tabs); store.set("active", S.active); }
function openTab(id, focus) {
  if (!S.tabs.includes(id)) S.tabs.push(id);
  if (focus || !S.active) S.active = id;
  if (focus && $("#main").classList.contains("term-min")) setTermMode("");
  saveTabs(); renderTabs(); renderJobList(); renderLog(); pollLogs();
}
function closeTab(id) {
  S.tabs = S.tabs.filter(t => t !== id);
  if (S.active === id) S.active = S.tabs[S.tabs.length - 1] || null;
  saveTabs(); renderTabs(); renderJobList(); renderLog();
}
function renderTabs() {
  const box = $("#tabs");
  if (!S.tabs.length) { box.innerHTML = `<span class="tab-empty">Terminal · job output appears here</span>`; return; }
  box.innerHTML = S.tabs.map(id => {
    const j = S.jobs.find(x => x.id === id);
    return `<button type="button" role="tab" class="tab ${id === S.active ? "on" : ""}" data-tab="${esc(id)}" aria-selected="${id === S.active}">
      <span class="st ${j ? jobState(j) : ""}"></span>${esc(j ? jobName(j) : id)}
      <span class="x" data-close="${esc(id)}" title="Close tab"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></span></button>`;
  }).join("");
}
function colorize(text) {
  return text.split("\n").map(line => {
    const e = esc(line);
    if (/^(PLAY|TASK|RUNNING HANDLER)\b|^\$ /.test(line)) return `<span class="l-head">${e}</span>`;
    if (/^(fatal|failed|ERROR|\[ERROR\])|FAILED!|UNREACHABLE|=== finished rc=[1-9]|^!!/.test(line)) return `<span class="l-bad">${e}</span>`;
    if (/^changed:|REBOOT/.test(line)) return `<span class="l-chg">${e}</span>`;
    if (/^ok:|=== finished rc=0/.test(line)) return `<span class="l-ok">${e}</span>`;
    if (/^(skipping|included):/.test(line)) return `<span class="l-dim">${e}</span>`;
    return e;
  }).join("\n");
}
function renderLog() {
  const pre = $("#log");
  if (!S.active) {
    pre.innerHTML = `<span class="l-dim">Start a job (Patch, Reboot, Apply auto-update policy, Refresh now) or pick one under Activity to see its output here.</span>`;
    pre.dataset.job = ""; return;
  }
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.innerHTML = colorize(S.logs[S.active]?.text || "");
  if (atEnd || pre.dataset.job !== S.active) pre.scrollTop = pre.scrollHeight;
  pre.dataset.job = S.active;
}
async function pollLogs() {
  for (const id of S.tabs) {
    const j = S.jobs.find(x => x.id === id);
    const L = S.logs[id] || (S.logs[id] = {text: "", offset: 0, done: false});
    if (L.done && j && j.rc !== null) continue;
    try {
      const r = await api(`job/${id}?offset=${L.offset}`);
      L.text += r.text; L.offset = r.offset; L.done = r.done;
      if (id === S.active && r.text) renderLog();
    } catch (e) { /* console restarting; next tick */ }
  }
}

/* terminal size: drag the splitter, maximize, collapse (all remembered) */
function setTermMode(mode) {
  const m = $("#main");
  m.classList.toggle("term-max", mode === "max");
  m.classList.toggle("term-min", mode === "min");
  $("#term-max").title = mode === "max" ? "Restore" : "Maximize terminal";
  $("#term-min").title = mode === "min" ? "Expand terminal" : "Collapse terminal";
  store.set("termMode", mode);
}
function setTermHeight(px) {
  const max = $("#main").clientHeight - 140;
  px = Math.max(120, Math.min(px, max));
  $("#main").style.setProperty("--term-h", px + "px");
  store.set("termH", px);
}
(function splitter() {
  const sp = $("#splitter");
  sp.addEventListener("pointerdown", e => {
    e.preventDefault(); sp.setPointerCapture(e.pointerId); sp.classList.add("drag");
    const rect = $("#main").getBoundingClientRect();
    const move = ev => setTermHeight(rect.bottom - ev.clientY);
    const up = () => { sp.classList.remove("drag"); sp.removeEventListener("pointermove", move); sp.removeEventListener("pointerup", up); };
    sp.addEventListener("pointermove", move); sp.addEventListener("pointerup", up);
  });
  sp.addEventListener("dblclick", () => setTermMode("max"));
  sp.addEventListener("keydown", e => {
    const h = $("#term").getBoundingClientRect().height;
    if (e.key === "ArrowUp") { setTermHeight(h + 40); e.preventDefault(); }
    if (e.key === "ArrowDown") { setTermHeight(h - 40); e.preventDefault(); }
  });
})();

/* ---------- events ---------- */
document.addEventListener("click", e => {
  const t = e.target.closest("button, input, [data-toggle], [data-close]");
  if (!t) return;
  if (t.dataset.close) { e.stopPropagation(); return void closeTab(t.dataset.close); }
  if (t.matches(".pick")) { t.checked ? S.selected.add(t.value) : S.selected.delete(t.value); syncSelection(); return; }
  if (t.id === "sel-all") { hostsShown().filter(selectable).forEach(h => t.checked ? S.selected.add(h.host) : S.selected.delete(h.host)); render(); return; }
  if (t.id === "sel-clear") { S.selected.clear(); render(); return; }
  if (t.id === "wrap") { $("#log").classList.toggle("wrap", t.checked); store.set("wrap", t.checked); return; }
  if (t.id === "term-max") return void setTermMode($("#main").classList.contains("term-max") ? "" : "max");
  if (t.id === "term-min") return void setTermMode($("#main").classList.contains("term-min") ? "" : "min");
  if (t.dataset.filter) { S.filter = t.dataset.filter; render(); return; }
  if (t.dataset.bulk) return void runAction(t.dataset.bulk, [...S.selected]);
  if (t.dataset.one) return void runAction(t.dataset.one, [t.dataset.host]);
  if (t.hasAttribute("data-controller-reboot")) return void rebootController();
  if (t.dataset.snooze) return void snooze(t.dataset.snooze, +t.dataset.days);
  if (t.dataset.openjob) return void openTab(t.dataset.openjob, true);
  if (t.id === "btn-enroll") return void enrollMachine();
  if (t.id === "btn-settings") return void openSettings();
  if (t.id === "btn-logout") return void api("logout", {}).finally(toLogin);
  if (t.dataset.unenroll) return void unenrollMachine(t.dataset.unenroll);
  if (t.dataset.watch) {
    const live = S.jobs.find(j => j.action === "watch-upgrade" && j.rc === null && j.hosts[0] === t.dataset.watch);
    if (live) return void openTab(live.id, true);
    api("watch", {hosts: [t.dataset.watch]}).then(async j => { await loadState(); openTab(j.job, true); })
      .catch(err => toast(err.message, "fail"));
    return;
  }
  if (t.dataset.tab) { S.active = t.dataset.tab; saveTabs(); renderTabs(); renderJobList(); renderLog(); pollLogs(); return; }
  if (t.id === "btn-refresh") {
    api("refresh", {}).then(async j => { await loadState(); openTab(j.job, true); toast("Checking every machine…"); })
      .catch(err => toast(err.message, "fail"));
    return;
  }
  if (t.hasAttribute("data-toggle")) {
    const host = t.closest(".row").dataset.host;
    S.open.has(host) ? S.open.delete(host) : S.open.add(host);
    t.closest(".row").classList.toggle("open");
  }
});

/* ---------- start ---------- */
S.tabs = store.get("tabs", []); S.active = store.get("active", null);
setTermMode(store.get("termMode", ""));
const savedH = store.get("termH", null);
if (savedH) $("#main").style.setProperty("--term-h", savedH + "px");
const wrapOn = store.get("wrap", true);
$("#wrap").checked = wrapOn; $("#log").classList.toggle("wrap", wrapOn);
renderTabs(); renderLog();
loadStatus();
setInterval(loadStatus, 60e3);
setInterval(() => { if (S.status) $("#checked").textContent = `Checked ${ago(S.status.time)} · every 15 min`; }, 30e3);
(function tick() {
  loadState().then(pollLogs).finally(() => setTimeout(tick, S.running.length ? 2000 : 15000));
})();
