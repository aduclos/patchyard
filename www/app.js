// Patchyard web app. Renders data/status.json (written by `python -m patchyard check`)
// and drives the action API (/api/*, patchyard/web.py). No libraries, no build step.
// Layout: sidebar | machines (top) / terminal with one tab per job (bottom).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const short = h => h.split(".")[0];
const store = {
  get(k, d) { try { const v = localStorage.getItem("patchyard." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("patchyard." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

const S = {
  status: null, filter: "all", q: "", drawer: null, selected: new Set(),
  console: false, running: [], jobs: [], logs: {},
  tabs: [], active: null,
  user: "", settings: null,
};

const SUPPORT_URL = "https://ko-fi.com/dridgedukey";     // the project's Ko-fi page, shown in Settings

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
const UPDATES = {patchyard: "All updates, daily", stock: "Security only", "dnf-automatic": "dnf-automatic, daily",
  "windows-update": "Windows Update", macos: "macOS: daily check, security responses", "transactional-update": "transactional-update", none: "Not automatic"};
const STATUS = {ok: "Up to date", reboot: "Reboot waiting", update: "Update available", warn: "Check", fail: "Problem"};
const ACTION_LABEL = {patch: "Patch", reboot: "Reboot", "auto-updates": "Auto-update policy", refresh: "Refresh status",
  enroll: "Add machine", unenroll: "Remove machine", "reboot-controller": "Reboot Patchyard server", "refresh-snooze": "Refresh",
  "release-upgrade": "Release upgrade", "cancel-upgrade": "Cancel upgrade", "watch-upgrade": "Watching upgrade",
  "device-check": "Firmware check", "device-update": "Firmware update", "device-reboot": "Reboot device"};
const VIEW = {all: "All", attention: "Need attention", reboot: "Waiting on you", ok: "Up to date", devices: "Network devices"};
const READ_ONLY_JOBS = ["refresh", "refresh-snooze", "watch-upgrade", "device-check"];
const busyHosts = () => new Set(S.jobs.filter(j => j.rc === null && !READ_ONLY_JOBS.includes(j.action)).flatMap(j => j.hosts));
const jobName = j => (ACTION_LABEL[j.action] || j.action) + (j.hosts.length ? " · " + j.hosts.map(short).join(", ") : "");

/* ---------- api ---------- */
async function api(path, body) {
  const opt = body ? {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-Patchyard": "1"}} : {cache: "no-store"};
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
const isServer = h => !!h.controller;          // the machine Patchyard runs on: a machine like the rest
const isDevice = h => h.role === "device";
const changeable = h => h.role === "managed";
const selectable = h => h.reachable && changeable(h) && !h.release.upgrading;
const hostInfo = name => (S.status?.hosts || []).find(h => h.host === name) || {};
const FILTERS = {
  all: () => true,
  attention: h => h.status === "fail" || h.status === "warn",
  reboot: h => h.status === "reboot" || h.status === "update",
  ok: h => h.status === "ok",
  devices: h => h.role === "device",
};
function matchesSearch(h) {
  if (!S.q) return true;
  const hay = [h.name, h.host, h.os, STATUS[h.status], h.role, ...(h.groups || []), ...h.items.map(i => i.text)]
    .join(" ").toLowerCase();
  return S.q.toLowerCase().split(/\s+/).every(w => hay.includes(w));
}
function hostsShown() {
  return (S.status?.hosts || []).filter(FILTERS[S.filter] || FILTERS.all).filter(matchesSearch);
}

function render() {
  const st = S.status; if (!st) return;
  $("#checked").textContent = `Checked ${ago(st.time)}`;
  const sm = st.summary;
  const counts = {all: st.hosts.length, attention: sm.fail + sm.warn, reboot: sm.reboot + (sm.update || 0), ok: sm.ok,
    devices: st.hosts.filter(isDevice).length};
  $$("[data-count]").forEach(el => el.textContent = counts[el.dataset.count]);
  $$(".nav-item").forEach(b => {
    b.classList.toggle("on", b.dataset.filter === S.filter);
    b.classList.toggle("hot", ["attention", "reboot"].includes(b.dataset.filter) && counts[b.dataset.filter] > 0);
  });
  $("#view-title").textContent = VIEW[S.filter];
  const shown = hostsShown(), pend = st.hosts.reduce((a, h) => a + (h.pending || 0), 0);
  $("#view-sub").textContent = `${shown.length} shown${S.q ? ` for “${S.q}”` : ""} · ${pend} updates pending in total`;
  const busy = busyHosts();
  const groups = [["Machines", shown.filter(h => !isDevice(h))], ["Network devices", shown.filter(isDevice)]]
    .filter(([, hs]) => hs.length);
  $("#cards").innerHTML = groups.map(([title, hs]) =>
    `<div class="section-h">${title}<span class="n">${hs.length}</span></div>
     <div class="cards">${hs.map(h => cardHtml(h, busy)).join("")}</div>`).join("")
    || `<p class="empty-view">Nothing ${S.q ? "matches your search" : "in this view"}.</p>`;
  syncSelection();
  renderDrawer();
}

/* Badge with the OS family (original glyphs; no vendor logos). */
/* Brand logos (www/icons.js): a badge in the brand's colour, or a logo readable on the dark UI. */
function luminance(hex) {
  const n = parseInt(hex, 16);
  const [r, g, b] = [n >> 16 & 255, n >> 8 & 255, n & 255].map(v => (v /= 255) <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
  return .2126 * r + .7152 * g + .0722 * b;
}
function logo(slug, onBadge = true) {
  const b = BRANDS[slug];
  if (!b) return "";
  const l = luminance(b.hex);
  const fill = onBadge ? (l > .45 ? "#14171f" : "#fff") : (l < .06 ? "#e7eaf2" : "#" + b.hex);
  return `<svg class="brand${WORDMARKS.has(slug) ? " wide" : ""}" viewBox="0 0 24 24" role="img" aria-label="${esc(b.title)}"><path fill="${fill}" stroke="none" d="${b.d}"/></svg>`;
}
const WORDMARKS = new Set(["asus", "synology"]);        // wide logos: drawn larger to stay legible
const OS_LOGOS = [["ubuntu", "ubuntu"], ["debian", "debian"], ["raspbian", "raspberrypi"], ["fedora", "fedora"],
  ["red hat", "redhat"], ["rhel", "redhat"], ["rocky", "rockylinux"], ["alma", "almalinux"], ["centos", "centos"],
  ["suse", "opensuse"], ["arch", "archlinux"], ["alpine", "alpinelinux"], ["freebsd", "freebsd"],
  ["macos", "apple"], ["mac os", "apple"], ["windows", "windows"]];
const DRIVER_LOGOS = {opnsense: "opnsense", fortigate: "fortinet", mikrotik: "mikrotik", openwrt: "openwrt",
  asuswrt: "asus", unifi: "ubiquiti", omada_eap: "tplink", truenas: "truenas", synology: "synology",
  homeassistant: "homeassistant"};
function osBadge(h) {
  const os = (h.os || "").toLowerCase();
  const slug = isDevice(h) ? DRIVER_LOGOS[h.device?.driver] : (OS_LOGOS.find(([k]) => os.includes(k)) || [, os ? "linux" : ""])[1];
  if (slug && BRANDS[slug]) {
    const title = isDevice(h) ? `${BRANDS[slug].title} (${h.device?.driver})` : h.os || BRANDS[slug].title;
    return `<span class="os os-logo" style="--osc:#${BRANDS[slug].hex}" title="${esc(title)}">${logo(slug)}</span>`;
  }
  if (isDevice(h))                                  // a driver without a logo (exec)
    return `<span class="os os-router" title="${esc(h.device?.driver || "")}"><svg viewBox="0 0 24 24"><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M8 9a6 6 0 0 1 8 0M10.3 11.2a2.5 2.5 0 0 1 3.4 0"/></svg></span>`;
  return `<span class="os os-unknown" title="${esc(h.os || "unknown")}">${esc((h.name[0] || "?").toUpperCase())}</span>`;
}

function stateOf(h, busy) {
  const stuck = h.items.some(i => i.key.startsWith("relupstuck:"));
  if (!h.reachable) return ["off", "Unreachable"];
  if (h.release.upgrading) return stuck ? ["warn", "Upgrade waiting for input"] : ["busy", "Release upgrade"];
  if (busy.has(h.host)) return ["busy", "Job running"];
  return [h.status, STATUS[h.status]];
}

function cardHtml(h, busy) {
  const [cls, label] = stateOf(h, busy);
  const badges = [];          // [text, highlighted]
  if (isDevice(h)) {
    const d = h.device;
    badges.push([d.update_available === null ? "no online check" : d.latest ? "latest " + d.latest : "", false]);
  } else {
    if (h.pending) badges.push([`${h.pending} pending`, false]);
    if (h.reboot_since) badges.push(["reboot", true]);
    if (h.containers.length) badges.push([`${h.containers.length} container${h.containers.length === 1 ? "" : "s"}`, false]);
    if (h.policy?.reboot_blackout) badges.push(["no-reboot window", false]);
    if (h.policy?.manual_only !== null && h.policy?.manual_only !== undefined) badges.push(["manual only", false]);
    if (h.role === "report_only") badges.push(["report only", false]);
  }
  const sel = selectable(h)
    ? `<input type="checkbox" class="pick" value="${esc(h.host)}" aria-label="Select ${esc(h.name)}" ${S.selected.has(h.host) ? "checked" : ""}>` : "";
  return `<div class="card ${S.drawer === h.host ? "active" : ""} ${S.selected.has(h.host) ? "sel" : ""} ${h.reachable ? "" : "unreachable"}"
      data-open="${esc(h.host)}" tabindex="0" role="button" aria-label="${esc(h.name)}: ${esc(label)}">
    ${sel}${osBadge(h)}
    <div class="card-main"><div class="card-name">${esc(h.name)}</div><div class="card-sub">${esc(h.os || "–")}</div></div>
    <div class="card-foot"><span class="state s-${cls}">${esc(label)}</span>
      ${badges.filter(([t]) => t).map(([t, hot]) => `<span class="badge${hot ? " hot" : ""}">${esc(t)}</span>`).join("")}</div>
  </div>`;
}

/* Actions for one machine or device, shown in the drawer header. */
function actionsHtml(h, busy) {
  const isBusy = busy.has(h.host), off = S.console ? "" : "disabled";
  const can = selectable(h) && S.console && !isBusy;
  if (isDevice(h)) return deviceButtons(h, off, isBusy);
  if (!changeable(h)) return "";
  if (h.release.upgrading)
    return `<button class="btn sm ghost" type="button" data-watch="${esc(h.host)}" ${off}>Watch</button>
      <button class="btn sm danger" type="button" data-one="cancel-upgrade" data-host="${esc(h.host)}" ${off}>Cancel upgrade</button>`;
  if (!h.reachable) return "";
  // The Patchyard server reboots only if reboot_command is set, and keeps its own update schedule
  // when it is patched with patch_command.
  const server = isServer(h), site = S.status.site || {};
  return `<button class="btn sm" type="button" data-one="patch" data-host="${esc(h.host)}" ${can ? "" : "disabled"}>Patch</button>
    ${server && !site.controller_reboot ? "" : `<button class="btn sm ghost" type="button" data-one="reboot" data-host="${esc(h.host)}" ${can ? "" : "disabled"}>Reboot</button>`}
    ${server && site.controller_patch ? "" : `<button class="btn sm ghost" type="button" data-one="auto-updates" data-host="${esc(h.host)}" ${can ? "" : "disabled"}>Auto-update policy</button>`}`;
}

function renderDrawer() {
  const d = $("#drawer"), h = S.drawer ? hostInfo(S.drawer) : null;
  if (!h || !h.host) { d.hidden = true; d.innerHTML = ""; return; }
  const busy = busyHosts(), [cls, label] = stateOf(h, busy), scroll = d.scrollTop;
  d.hidden = false;
  d.innerHTML = `<div class="drawer-head">
      <div class="drawer-title">${osBadge(h)}<div><h3>${esc(h.name)}</h3><p>${esc(h.os || "–")}${h.host !== h.name ? " · " + esc(h.host) : ""}</p></div>
        <button class="icon" type="button" data-drawer-close title="Close (Esc)" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      <div><span class="pill s-${cls}">${esc(label)}</span></div>
      <div class="drawer-acts">${actionsHtml(h, busy)}</div>
    </div>
    <div class="drawer-body">${detailHtml(h)}</div>`;
  d.scrollTop = scroll;
}
function openDrawer(host) { S.drawer = S.drawer === host ? null : host; render(); }
function closeDrawer() { if (S.drawer) { S.drawer = null; render(); } }

function deviceButtons(h, off, busy) {
  const c = h.device.capabilities, dis = off || (busy ? "disabled" : "");
  return [
    c.update && h.device.update_available ? `<button class="btn sm" type="button" data-device="${esc(h.host)}" data-device-action="update" ${dis}>Update…</button>` : "",
    c.reboot ? `<button class="btn sm ghost" type="button" data-device="${esc(h.host)}" data-device-action="reboot" ${dis}>Reboot…</button>` : "",
    `<button class="btn sm ghost" type="button" data-device="${esc(h.host)}" data-device-action="check" ${dis}>Check now</button>`,
  ].join("");
}

function deviceHtml(h) {
  const d = h.device;
  const rows = [`<dt>Model</dt><dd>${esc(d.model || "–")}</dd>`, `<dt>Firmware</dt><dd>${esc(d.version || "–")}</dd>`,
    `<dt>Latest</dt><dd>${d.update_available === null ? "the device cannot check by itself" : esc(d.latest || "unknown")}</dd>`,
    `<dt>Driver</dt><dd><code>${esc(d.driver)}</code></dd>`];
  if (d.disruption) rows.push(`<dt>Update/reboot</dt><dd>${esc(d.disruption)}</dd>`);
  const notes = (d.notes || []).map(n => `<li>${esc(n)}</li>`).join("");
  return `<div><h4>Device</h4><dl class="kv">${rows.join("")}</dl>${notes ? `<ul class="muted">${notes}</ul>` : ""}</div>`;
}

async function deviceAction(name, action) {
  const h = hostInfo(name), d = h.device || {};
  if (action === "check") {
    try { const j = await api("device", {name, action}); await loadState(); openTab(j.job, true); }
    catch (e) { toast(e.message, "fail"); }
    return;
  }
  const what = action === "update"
    ? `Installs firmware ${esc(d.latest || "")} on <b>${esc(name)}</b> (now ${esc(d.version || "?")}). The device reboots by itself.`
    : `Reboots <b>${esc(name)}</b>.`;
  const res = await ask({title: `${action === "update" ? "Update" : "Reboot"} ${name}`,
    desc: `<div class="warnbox">${what}${d.disruption ? `<br>${esc(d.disruption)}` : ""}</div>`,
    confirmWord: "yes", okText: action === "update" ? "Update firmware" : "Reboot", danger: true});
  if (!res) return;
  try {
    const j = await api("device", {name, action, confirm: "yes"});
    await loadState(); openTab(j.job, true);
    toast(`${action === "update" ? "Firmware update" : "Reboot"} started on ${name}`, "ok");
  } catch (e) { toast(e.message, "fail"); }
}

function policyHtml(p) {
  const rows = [];
  if (p.reboot_blackout) rows.push(`<dt>No reboots</dt><dd>${esc(p.reboot_blackout.text)}${p.reboot_blackout.hours_utc.length ? "" : " (its local time)"}${p.reboot_blackout.reason ? " · " + esc(p.reboot_blackout.reason) : ""}</dd>`);
  if (p.manual_only !== null) rows.push(`<dt>Manual only</dt><dd>${esc(p.manual_only || "only patched when picked by name")}</dd>`);
  if (p.hold) rows.push(`<dt>Held back</dt><dd>${p.hold.packages.map(x => `<code>${esc(x)}</code>`).join(" ")}${p.hold.reason ? " · " + esc(p.hold.reason) : ""}</dd>`);
  if (!p.track_containers) rows.push(`<dt>Containers</dt><dd>not watched</dd>`);
  return rows;
}
function settingsHtml(h) {
  const rows = policyHtml(h.policy);
  return `<div><h4>Settings</h4>${rows.length ? `<dl class="kv">${rows.join("")}</dl>` : `<p class="muted">The defaults for every machine.</p>`}
    <button class="btn sm ghost" type="button" data-msettings="${esc(h.host)}">Edit settings…</button></div>`;
}

function detailHtml(h) {
  const issues = h.items.length ? `<ul class="issues">${h.items.map(i => {
    const snoozed = i.text.includes("(snoozed until");
    const btn = ["fail", "warn", "reboot"].includes(i.sev)
      ? `<button class="btn sm ghost" type="button" data-snooze="${esc(i.key)}" data-days="7">Snooze 7 d</button>`
      : snoozed ? `<button class="btn sm ghost" type="button" data-snooze="${esc(i.key)}" data-days="0">Unsnooze</button>` : "";
    return `<li class="s-${i.sev}"><span>${esc(i.text)}</span>${btn}</li>`;
  }).join("")}</ul>` : `<p class="muted">Nothing to report.</p>`;
  let out = `<div><h4>Status</h4>${issues}</div>` + (isDevice(h) ? deviceHtml(h) : settingsHtml(h));
  const r = h.release;
  if (r.available && !r.upgrading && selectable(h))
    out += `<div><h4>New release</h4><p class="muted">${esc(r.available)} is available (optional; the current release keeps getting updates).
      Runs without questions, keeps existing config files, streams its log here and never reboots by itself.</p>
      <button class="btn sm ghost" type="button" data-one="release-upgrade" data-host="${esc(h.host)}" ${S.console && !busyHosts().has(h.host) ? "" : "disabled"}>Upgrade to ${esc(r.available)}…</button></div>`;
  if (r.upgrading)
    out += `<div class="wide"><h4>Release upgrade · last activity ${ago(r.activity)}</h4>
      <p class="muted">Upgrader log, newest line</p><pre class="report">${esc(r.last_line || "–")}</pre>
      <p class="muted">Last package step (dpkg)</p><pre class="report">${esc(r.dpkg_last || "–")}</pre></div>`;
  if (h.pending_pkgs.length && h.pending_titles)               // update titles (Windows, macOS): one per line
    out += `<div><h4>Pending updates (${h.pending})</h4><ul class="plist">${h.pending_pkgs.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>`;
  else if (h.pending_pkgs.length)
    out += `<div><h4>Pending packages (${h.pending})</h4><div class="tags">${h.pending_pkgs.map(p => `<span class="tag">${esc(p)}</span>`).join("")}</div></div>`;
  if (h.left_out?.length)
    out += `<div><h4>Not installed by Patchyard (${h.left_out.length})</h4><ul class="plist muted">${h.left_out.map(p => `<li>${esc(p)}</li>`).join("")}</ul>
      <p class="muted small">Windows Update offers these, but they are outside what Patch installs. Install them from Windows
      Update yourself, or add their category under <b>Patch installs</b> in this machine's settings.</p></div>`;
  if (h.containers.length)
    out += `<div><h4>Containers (${h.containers.length})</h4><div class="tags">${h.containers.map(c => `<span class="tag">${esc(c)}</span>`).join("")}</div></div>`;
  if (changeable(h))
    out += `<div><h4>Dashboard</h4>
      <button class="btn sm ghost" type="button" data-unenroll="${esc(h.host)}" ${S.console ? "" : "disabled"}>Remove from dashboard…</button></div>`;
  if (h.extra?.note) out += `<div><h4>Note</h4><p class="muted">${esc(h.extra.note)}</p></div>`;
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
  $$(".card[data-open]").forEach(c => c.classList.toggle("sel", S.selected.has(c.dataset.open)));
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
function ask({title, desc, opts = "", needPw = false, confirmWord = "", okText = "Run", danger = false, pwLabel = "Password",
             onOpen = null}) {
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
      if (needPw && !pw && !$("#f-pw").hidden) return void ($("#dlg-err").textContent = "Enter the password.");
      if (confirmWord && $("#dlg-confirm").value.trim() !== confirmWord) return void ($("#dlg-err").textContent = `Type ${confirmWord} to confirm.`);
      const o = {};
      $$("#dlg-opts input, #dlg-opts select").forEach(i =>
        o[i.name] = (i.type === "checkbox" || i.type === "radio") ? i.checked : i.value.trim());
      finish({pw, opts: o});
    };
    d.showModal();
    if (onOpen) onOpen();
    setTimeout(() => (needPw && !$("#f-pw").hidden ? $("#dlg-pw") : confirmWord ? $("#dlg-confirm") : ok).focus(), 30);
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
  // Rebooting the machine Patchyard runs on takes the dashboard away until it is back: typed confirmation.
  const server = action === "reboot" && hosts.map(hostInfo).some(isServer);
  const res = await ask({title: `${verb} ${names.join(", ")}`,
    desc: big ? `<div class="warnbox">${what}</div>` : server ? `<div class="warnbox">${esc(names[0])} runs Patchyard: this page
      is unavailable until it is back up.</div>` : esc(what), opts: actionOptions(action, hosts),
    okText: verb, confirmWord: big || server ? "yes" : "", danger: big || server || action === "cancel-upgrade"});
  if (!res) return;
  try {
    const j = await api("run", {action, hosts, reboot: !!res.opts.reboot, force: !!res.opts.force,
                                confirm: server ? "yes" : undefined});
    toast(`${verb} started on ${names.join(", ")}`, "ok");
    S.selected.clear();
    await loadState(); openTab(j.job, true); render();
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

async function unenrollMachine(host) {
  const res = await ask({title: `Remove ${short(host)} from the dashboard`,
    desc: "Takes it out of the inventory. Nothing on the machine changes: its daily auto-updates keep running. Add it back any time.",
    okText: "Remove", danger: true});
  if (!res) return;
  try { const j = await api("unenroll", {host}); await loadState(); openTab(j.job, true); }
  catch (e) { toast(e.message, "fail"); }
}


/* ---------- console state, sidebar activity ---------- */
async function loadState() {
  const el = $("#console-state");
  const wasOnline = S.console;
  try {
    const s = await api("state");
    S.console = true; S.jobs = s.jobs || [];
    if (s.user && s.user !== S.user) { S.user = s.user; $("#who").textContent = s.user; $("#avatar").textContent = s.user[0] || "?"; }
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
  if (!S.jobs.length) { box.innerHTML = `<p class="side-empty">No jobs since Patchyard started.</p>`; return; }
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
  if (focus && phone.matches) setSheet(true);
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
// Phones and short screens show the terminal as a bottom sheet (app.css); it opens and closes.
const phone = matchMedia("(max-width: 760px), (max-height: 520px)");
function setSheet(open) {
  $("#term").classList.toggle("open", open);
  document.body.classList.toggle("sheet-open", open && phone.matches);
  $("#term-min").title = open ? "Close the terminal" : "Open the terminal";
}
phone.addEventListener("change", () => { if (!phone.matches) document.body.classList.remove("sheet-open"); });
$(".term-head").addEventListener("click", e => {          // the bar itself opens and closes the sheet
  if (phone.matches && !e.target.closest("button, input, label")) setSheet(!$("#term").classList.contains("open"));
});

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
  const t = e.target.closest("button, input, [data-open], [data-close]");
  if (!t) return;
  if (t.dataset.close) { e.stopPropagation(); return void closeTab(t.dataset.close); }
  if (t.matches(".pick")) { t.checked ? S.selected.add(t.value) : S.selected.delete(t.value); syncSelection(); return; }
  if (t.id === "sel-clear") { S.selected.clear(); render(); return; }
  if (t.id === "wrap") { $("#log").classList.toggle("wrap", t.checked); store.set("wrap", t.checked); return; }
  if (t.id === "term-max") return void setTermMode($("#main").classList.contains("term-max") ? "" : "max");
  if (t.id === "term-min") {
    if (phone.matches) return void setSheet(!$("#term").classList.contains("open"));
    return void setTermMode($("#main").classList.contains("term-min") ? "" : "min");
  }
  if (t.dataset.filter) { S.filter = t.dataset.filter; render(); return; }
  if (t.dataset.bulk) return void runAction(t.dataset.bulk, [...S.selected]);
  if (t.dataset.one) return void runAction(t.dataset.one, [t.dataset.host]);
  if (t.dataset.msettings) return void MachineSettings.edit(t.dataset.msettings);
  if (t.dataset.snooze) return void snooze(t.dataset.snooze, +t.dataset.days);
  if (t.dataset.openjob) return void openTab(t.dataset.openjob, true);
  if (t.dataset.deviceAction) return void deviceAction(t.dataset.device, t.dataset.deviceAction);
  if (t.id === "btn-enroll") return void enrollMachine();
  if (t.id === "btn-settings") return void Settings.open();
  if (t.id === "btn-logout") return void api("logout", {}).finally(toLogin);
  if (t.dataset.unenroll) return void unenrollMachine(t.dataset.unenroll);
  if (t.dataset.watch) {
    const live = S.jobs.find(j => j.action === "watch-upgrade" && j.rc === null && j.hosts[0] === t.dataset.watch);
    if (live) return void openTab(live.id, true);
    api("watch", {hosts: [t.dataset.watch]}).then(async j => { await loadState(); openTab(j.job, true); })
      .catch(err => toast(err.message, "fail"));
    return;
  }
  if (t.dataset.tab) {
    S.active = t.dataset.tab; saveTabs(); renderTabs(); renderJobList(); renderLog(); pollLogs();
    if (phone.matches) setSheet(true);
    return;
  }
  if (t.id === "btn-refresh") {
    api("refresh", {}).then(async j => { await loadState(); openTab(j.job, true); toast("Checking every machine…"); })
      .catch(err => toast(err.message, "fail"));
    return;
  }
  if (t.hasAttribute("data-drawer-close")) return void closeDrawer();
  if (t.dataset.open) return void openDrawer(t.dataset.open);
});
$("#q").addEventListener("input", e => { S.q = e.target.value.trim(); render(); });
document.addEventListener("keydown", e => {
  if (Settings.isOpen()) return;                            // the settings page handles its own keys
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if (e.key === "/" && !typing && !$("#dlg").open) { e.preventDefault(); $("#q").focus(); }
  else if (e.key === "Escape" && !$("#dlg").open) {
    if (document.activeElement === $("#q") && S.q) { $("#q").value = ""; S.q = ""; render(); }
    else closeDrawer();
  }
  else if ((e.key === "Enter" || e.key === " ") && document.activeElement?.dataset?.open && !typing) {
    e.preventDefault(); openDrawer(document.activeElement.dataset.open);
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
setInterval(() => { if (S.status) $("#checked").textContent = `Checked ${ago(S.status.time)}`; }, 30e3);
(function tick() {
  loadState().then(pollLogs).finally(() => setTimeout(tick, S.running.length ? 2000 : 15000));
})();
