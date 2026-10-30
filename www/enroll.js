// Add machine: a small wizard, one machine or device at a time.
//   1. What are you adding? Tiles: computers (Linux/BSD, Windows, Mac) and network gear.
//   2. Details: the name, plus (Linux, Mac) an admin's user and password; for network gear the brand,
//      address and login.
//   3. Test and add: a live checklist; when everything passes, Patchyard adds it and follows the
//      job until it is on the dashboard.
// Two ways to add a computer, one the default per kind, the other under Advanced:
//   password  Patchyard signs in once as an admin over SSH (default for Linux and Mac)
//   line      one line run on the machine sets it up and reports in; the wizard waits for it and
//             carries on by itself (default for Windows, which has no SSH server until then).
//             If Patchyard's key login already works there, it is added straight away.
// Settings > Join commands (at the end) lists and revokes join commands.
// Loaded after app.js and settings.js; uses their helpers.
"use strict";

const HOURS = {1: "1 hour", 24: "1 day", 168: "7 days"};

function copyButton(id, text) {
  const b = $(`#${id}`);
  if (b) b.onclick = async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied", "ok"); }
    catch (e) { getSelection().selectAllChildren(b.previousElementSibling); toast("Selected; press Ctrl+C", "ok"); }
  };
}

const Wizard = (() => {
  const svg = d => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const ICON = {
    linux: svg('<path d="M12 3c-2.2 0-3.5 1.9-3.5 4.3 0 1.4.4 2.3-.6 3.6C6.6 12.6 6 14.3 6 16c0 2.8 2.7 5 6 5s6-2.2 6-5c0-1.7-.6-3.4-1.9-5.1-1-1.3-.6-2.2-.6-3.6C15.5 4.9 14.2 3 12 3z"/><path d="M10 15.5c0 1.4.9 2.5 2 2.5s2-1.1 2-2.5-.9-2.5-2-2.5-2 1.1-2 2.5z"/><path d="M10.6 7.6h.01M13.4 7.6h.01M11.2 9.6h1.6l-.8.9z"/>'),
    windows: svg('<rect x="3.5" y="3.5" width="7.5" height="7.5" rx="1.2"/><rect x="13" y="3.5" width="7.5" height="7.5" rx="1.2"/><rect x="3.5" y="13" width="7.5" height="7.5" rx="1.2"/><rect x="13" y="13" width="7.5" height="7.5" rx="1.2"/>'),
    mac: svg('<path d="M9 9V6.8A2.3 2.3 0 1 0 6.8 9H9zm0 0h6M9 9v6m6-6V6.8A2.3 2.3 0 1 1 17.2 9H15zm0 0v6m0 0h2.2a2.3 2.3 0 1 1-2.2 2.2V15zm0 0H9m0 0v2.2A2.3 2.3 0 1 1 6.8 15H9z"/>'),
    firewall: svg('<path d="M12 3l7 3v5.5c0 4.3-3 8-7 9.5-4-1.5-7-5.2-7-9.5V6z"/><path d="M9 12l2 2 4-4"/>'),
    ap: svg('<path d="M4 9.5a12 12 0 0 1 16 0M7 13a7.5 7.5 0 0 1 10 0M10 16.5a3 3 0 0 1 4 0"/><path d="M12 20h.01"/>'),
    nas: svg('<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/><path d="M8 7.5h.01M8 16.5h.01M12 7.5h4M12 16.5h4"/>'),
    home: svg('<path d="M4 11l8-7 8 7v8.5a1.5 1.5 0 0 1-1.5 1.5H15v-6H9v6H5.5A1.5 1.5 0 0 1 4 19.5z"/>'),
  };
  const KINDS = [
    {id: "linux", label: "Linux or BSD", sub: "Ubuntu, Debian, Fedora, RHEL, Raspberry Pi OS, FreeBSD", os: "linux",
     who: "an account that can use sudo", logo: "linux",
     logos: ["ubuntu", "debian", "fedora", "redhat", "raspberrypi"]},
    {id: "windows", label: "Windows", sub: "Windows 10 and 11, Windows Server 2019 and later", os: "windows",
     who: "an administrator", logo: "windows", way: "line"},
    {id: "mac", label: "Mac", sub: "macOS, with Remote Login turned on", os: "macos", who: "an administrator",
     logo: "apple"},
    {id: "firewall", label: "Firewall or router", sub: "OPNsense, FortiGate, MikroTik, OpenWrt, ASUS",
     drivers: ["opnsense", "fortigate", "mikrotik", "openwrt", "asuswrt"]},
    {id: "ap", label: "Wi-Fi access point", sub: "UniFi, ASUS, TP-Link Omada", drivers: ["unifi", "asuswrt", "omada_eap"]},
    {id: "nas", label: "NAS or storage", sub: "TrueNAS, Synology", drivers: ["truenas", "synology"]},
    {id: "home", label: "Smart home", sub: "Home Assistant", drivers: ["homeassistant"]},
  ];
  let w = {};                                   // wizard state
  let devInfo = null;                           // GET /api/devices (driver forms)

  const kind = () => KINDS.find(k => k.id === w.kind);
  const badge = k => k.logo && BRANDS[k.logo]
    ? `<span class="wiz-badge" style="--kc:#${BRANDS[k.logo].hex}">${logo(k.logo)}</span>`
    : `<span class="wiz-badge k-${k.id}">${ICON[k.id]}</span>`;
  const isDevice = () => !!kind()?.drivers;
  const body = () => $("#wiz-body"), foot = () => $("#wiz-foot");

  const defaultWay = k => k.way || "password";
  const shortName = h => /^[\d.:]+$/.test(h || "") ? h : (h || "").split(".")[0];      // an IP stays whole
  function open() {
    w = {step: 1, method: "password", values: {admin: S.user || ""}};
    render();
    $("#wiz").showModal();
  }
  function close() { $("#wiz").close(); w.closed = true; }

  function render() {
    const k = kind();
    $("#wiz-title").innerHTML = w.step === 1 ? "Add a machine or device"
      : `${badge(k)}${esc(k.label)}`;
    $("#wiz-steps").innerHTML = ["What", "Details", "Test and add"].map((t, i) =>
      `<span class="${i + 1 === w.step ? "on" : i + 1 < w.step ? "done" : ""}">${i + 1}. ${t}</span>`).join("");
    if (w.step === 1) renderPick();
    else if (w.step === 2) isDevice() ? renderDevice() : renderMachine();
    else renderRun();
  }

  /* ---------- 1. what ---------- */
  function logoRow(k) {
    const slugs = k.logos || [...new Set((k.drivers || []).map(d => DRIVER_LOGOS[d]))];
    return slugs.length ? `<span class="wiz-logos">${slugs.filter(l => BRANDS[l]).map(l =>
      `<span class="wiz-badge xs" style="--kc:#${BRANDS[l].hex}" title="${esc(BRANDS[l].title)}">${logo(l)}</span>`).join("")}</span>` : "";
  }
  function renderPick() {
    const tile = k => `<button type="button" class="wiz-tile" data-kind="${k.id}">${badge(k)}<span><b>${esc(k.label)}</b>
        <small>${esc(k.sub)}</small>${logoRow(k)}</span></button>`;
    body().innerHTML = `<p class="wiz-h">Computers</p><div class="wiz-tiles">${KINDS.filter(k => !k.drivers).map(tile).join("")}</div>
      <p class="wiz-h">Network gear</p><div class="wiz-tiles">${KINDS.filter(k => k.drivers).map(tile).join("")}</div>`;
    foot().innerHTML = `<button class="btn ghost" type="button" data-w="close">Cancel</button>`;
  }

  /* ---------- 2. details: a computer ---------- */
  function renderMachine() {
    const k = kind(), v = w.values, site = S.status?.site || {};
    const m = w.method, acct = esc(site.service_account || "patchyard"), win = k.os === "windows";
    const field = (name, label, attrs, hint = "") => `<label class="field"><span>${label}</span>
      <input ${attrs} name="${name}" value="${esc(v[name] || "")}" autocomplete="off" spellcheck="false">${hint ? `<small class="help">${hint}</small>` : ""}</label>`;
    const ways = {
      password: [win ? "It already has OpenSSH Server: sign in with an administrator's password"
                     : "Sign in with an admin's password",
                 `Patchyard signs in once and creates its <code>${acct}</code> account. The password is never stored.`],
      line: [win ? "Run one line on it" : "Run one line on it instead",
             win ? "In an administrator PowerShell on the machine. It turns on Microsoft's OpenSSH Server and creates "
                   + `the <code>${acct}</code> account; the wizard adds the machine when it is done.`
                 : "As an admin on the machine (it uses sudo). No password here; the wizard adds it when it is done."],
    };
    const order = defaultWay(k) === "line" ? ["line", "password"] : ["password", "line"];
    body().innerHTML = `
      ${field("host", "Name or address", `type="text" placeholder="${site.domain ? (win ? "pc1" : "web1") : "web1.example.com"}"`,
        site.domain ? `A short name gets <code>${esc(site.domain)}</code> added.` : "")}
      ${m === "password" ? `<div class="dev-grid">${field("admin", "Admin user", 'type="text"', `${esc(k.who)} on it`)}
        ${field("password", "Password", 'type="password"', "Used once to set it up, never stored.")}</div>` : ""}
      ${m === "line" && win ? `<p class="dev-help">Windows has no remote sign-in Patchyard can use until its OpenSSH Server is on.
        Press <b>Test and add</b>: if Patchyard can already sign in, it adds the machine right away; otherwise you get one
        line to run on it in an <b>administrator PowerShell</b>, and the wizard carries on by itself when it finishes.</p>` : ""}
      ${m === "line" && !win ? `<p class="dev-help">Press <b>Test and add</b> to get one line to run on the machine as an admin.
        The wizard carries on by itself when it finishes.</p>` : ""}
      ${m === "password" && win ? `<p class="dev-help">For machines that already run OpenSSH Server. Use a domain administrator
        or the machine's own Administrator: Windows limits what other local accounts can do over a remote sign-in.</p>` : ""}
      ${m === "password" && k.id === "mac" ? `<p class="dev-help">Turn on Remote Login first: System Settings → General → Sharing.</p>` : ""}
      <details class="wiz-adv" ${m !== defaultWay(k) ? "open" : ""}><summary>Advanced</summary>
        <div class="wiz-ways">${order.map(id => `<label class="wiz-way"><input type="radio" name="method" value="${id}" ${m === id ? "checked" : ""}>
          <span><b>${ways[id][0]}</b><small>${ways[id][1]}</small></span></label>`).join("")}</div></details>`;
    foot().innerHTML = `<button class="btn ghost" type="button" data-w="back">Back</button>
      <button class="btn" type="button" data-w="go">Test and add</button>`;
    $$("input[name=method]", body()).forEach(r => r.onchange = () => { keep(); w.method = r.value; render(); });
    setTimeout(() => $("input[name=host]", body())?.focus(), 30);
  }
  function keep() {
    $$("#wiz-body input[name], #wiz-body select[name]").forEach(i => {
      if (i.name !== "method" && i.type !== "password") w.values[i.name] = i.value.trim();
    });
  }

  /* ---------- 2. details: network gear ---------- */
  async function renderDevice() {
    if (!devInfo) {
      body().innerHTML = `<p class="muted">Loading…</p>`;
      try { devInfo = await api("devices"); } catch (e) { return void (body().innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    }
    const k = kind(), v = w.values;
    const drivers = k.drivers.filter(d => devInfo.drivers[d]);
    w.driver = drivers.includes(w.driver) ? w.driver : drivers[0];
    const drv = devInfo.drivers[w.driver];
    const field = f => {
      const name = `${f.option ? "opt" : "cred"}:${f.key}`, val = v[name] ?? f.default;
      const help = f.help ? `<small class="help">${esc(f.help)}</small>` : "";
      if (f.choices.length) return `<label class="field"><span>${esc(f.label)}</span><select name="${esc(name)}">${f.choices.map(c =>
        `<option ${c === val ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>${help}</label>`;
      return `<label class="field"><span>${esc(f.label)}${f.required ? "" : ' <span class="muted">(optional)</span>'}</span>
        <input type="${f.secret ? "password" : "text"}" name="${esc(name)}" value="${f.secret ? "" : esc(val || "")}"
          autocomplete="${f.secret ? "new-password" : "off"}" spellcheck="false">${help}</label>`;
    };
    body().innerHTML = `
      ${drivers.length > 1 ? `<div class="wiz-brands">${drivers.map(d => `<label class="wiz-brand">
        <input type="radio" name="driver" value="${d}" ${d === w.driver ? "checked" : ""}>
        <span class="wiz-badge sm" style="--kc:#${BRANDS[DRIVER_LOGOS[d]]?.hex || "3b4256"}">${logo(DRIVER_LOGOS[d])}</span>
        <span>${esc(devInfo.drivers[d].title)}</span></label>`).join("")}</div>` : ""}
      ${drv.help ? `<p class="dev-help">${esc(drv.help)}</p>` : ""}
      <div class="dev-grid">
        <label class="field"><span>Name</span><input type="text" name="name" value="${esc(v.name || "")}" placeholder="${k.id === "ap" ? "office-ap" : k.id}" spellcheck="false" autocomplete="off"></label>
        <label class="field"><span>Address</span><input type="text" name="url" value="${esc(v.url || "")}" placeholder="https://192.0.2.1" spellcheck="false" autocomplete="off"></label>
      </div>
      ${drv.fields.filter(f => !f.option).map(field).join("")}
      ${drv.fields.filter(f => f.option).map(field).join("")}
      <details class="wiz-adv"><summary>Advanced</summary>
        <label class="check"><input type="checkbox" name="verify_tls" ${v.verify_tls === "off" ? "" : "checked"}> Check its HTTPS certificate
          <span class="muted small">(turn off only for a self-signed one)</span></label>
        <label class="field"><span>What an update or reboot interrupts</span><input type="text" name="disruption" value="${esc(v.disruption || "")}"
          placeholder="Office Wi-Fi drops for about 3 minutes"><small class="help">Shown before anyone confirms an update or reboot.</small></label>
      </details>`;
    foot().innerHTML = `<button class="btn ghost" type="button" data-w="back">Back</button>
      <button class="btn" type="button" data-w="go">Test and add</button>`;
    $$("input[name=driver]", body()).forEach(r => r.onchange = () => { keepDevice(); w.driver = r.value; renderDevice(); });
    setTimeout(() => $("input[name=name]", body())?.focus(), 30);
  }
  function keepDevice() {
    $$("#wiz-body input[name], #wiz-body select[name]").forEach(i => {
      if (i.name === "driver") return;
      if (i.type === "checkbox") w.values[i.name] = i.checked ? "on" : "off";
      else if (i.type !== "password") w.values[i.name] = i.value.trim();
    });
  }
  function deviceRequest() {
    const root = body(), get = n => root.querySelector(`[name="${n}"]`);
    const device = {driver: w.driver, name: get("name").value.trim().toLowerCase(), url: get("url").value.trim(),
                    verify_tls: get("verify_tls").checked, disruption: get("disruption").value.trim(), options: {}};
    const credentials = {};
    $$("[name^='cred:'], [name^='opt:']", root).forEach(el => {
      const [k2, key] = el.name.split(":");
      if (k2 === "opt") device.options[key] = el.value.trim(); else credentials[key] = el.value;
    });
    return {device, credentials};
  }

  /* ---------- 3. test and add ---------- */
  // A row: {step, ok: true | false | null (working) | "todo" (your turn), detail, html (trusted, already escaped)}
  const mark = ok => ok === true ? "ok" : ok === false ? "bad" : ok === "todo" ? "todo" : "run";
  function renderRun() {
    const rows = w.checks.map(c => `<li class="wiz-check ${mark(c.ok)}"><span class="wiz-dot"></span>
        <span><b>${esc(c.step)}</b><small>${esc(c.detail || "")}</small>${c.html || ""}</span></li>`).join("");
    body().innerHTML = `<ul class="wiz-checks">${rows}</ul>`;
    const failed = w.checks.some(c => c.ok === false), busy = w.checks.some(c => c.ok === null || c.ok === "todo");
    const log = w.job ? `<button class="btn ghost" type="button" data-w="log">Show the log</button>` : "";
    const instead = failed && !isDevice() && w.method === "password" && !w.job
      ? `<button class="btn ghost" type="button" data-w="line">Run a line on it instead</button>` : "";
    foot().innerHTML = failed ? `<button class="btn ghost" type="button" data-w="back">Back</button>${instead}${log}
        <button class="btn" type="button" data-w="go">Test again</button>`
      : busy ? `<button class="btn ghost" type="button" data-w="close">Close</button>${log}`
      : `${log}<button class="btn" type="button" data-w="close">Done</button>`;
    $$("[data-copy]", body()).forEach(b => copyButton(b.id, w.cmds[b.dataset.copy]));
  }
  function setChecks(list, run) {
    if (run !== undefined && run !== w.run) return false;       // a newer attempt took over
    w.checks = list;
    if (!w.closed) render();
    return true;
  }
  const pause = ms => new Promise(r => setTimeout(r, ms));

  async function go() {
    if (w.step === 2) {
      if (isDevice()) { keepDevice(); w.req = deviceRequest(); }
      else { keep(); w.req = {host: w.values.host, os: kind().os, method: w.method, admin: w.values.admin,
                              password: $("input[name=password]", body())?.value || ""}; }
      if (!isDevice() && !w.req.host) return void toast("Enter the name or address.", "fail");
      if (!isDevice() && w.method === "password" && (!w.req.admin || !w.req.password))
        return void toast("Enter the admin user and its password.", "fail");
      if (isDevice() && (!w.req.device.name || !w.req.device.url)) return void toast("Enter its name and address.", "fail");
    }
    w.step = 3; w.job = null; w.closed = false;
    const run = w.run = (w.run || 0) + 1;
    if (isDevice()) return runDevice(run);
    if (w.method === "line") return runLine(run);
    return runPassword(run);
  }

  async function runPassword(run) {
    setChecks([{step: "Checking", ok: null, detail: `name, SSH and sign-in on ${w.req.host}`}], run);
    let t;
    try { t = await api("enroll-test", w.req); }
    catch (e) { return setChecks([{step: "Checking", ok: false, detail: e.message}], run); }
    if (!t.ok) return setChecks(t.steps, run);
    await addAndFollow(t.steps, () => api("enroll", w.req), t.host, run);
  }

  // The line: if Patchyard's key login already works (the line or a command ran there before), add it
  // now; otherwise show a one-machine join line and wait for the machine to report in.
  async function runLine(run) {
    const host = w.req.host, short_ = shortName(host), win = kind().os === "windows";
    setChecks([{step: "Checking", ok: null, detail: `is ${short_} in DNS, and already set up for Patchyard?`}], run);
    let t;
    try { t = await api("enroll-test", {host, os: w.req.os, method: "command"}); }
    catch (e) { return setChecks([{step: "Checking", ok: false, detail: e.message}], run); }
    const name = t.steps.find(x => x.step === "Name");
    if (!name?.ok) return setChecks(t.steps, run);
    if (t.ok)
      return addAndFollow([name, {step: "Already set up", ok: true, detail: `Patchyard's key login works on ${short_}`}],
                          () => api("enroll", {host, os: w.req.os, method: "command"}), t.host, run);
    let j;
    try { j = await api("join", {action: "create", hours: 1, uses: 1, name: `${short_} (wizard)`}); }
    catch (e) { return setChecks([name, {step: "Line", ok: false, detail: e.message}], run); }
    const cmd = win ? j.commands.windows : j.commands.linux;
    w.cmds = {line: cmd};
    const todo = {step: `Run this line on ${short_}`, ok: "todo",
      detail: win ? "Open PowerShell as administrator on it (Start, type PowerShell, Run as administrator), paste, press Enter."
                  : "In a terminal on it, as an admin (it asks for your sudo password).",
      html: `<pre class="report cmd">${esc(cmd)}</pre><button class="btn sm ghost" type="button" id="wiz-copy" data-copy="line">Copy</button>`};
    const until = new Date(j.expires * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    const waiting = {step: `Waiting for ${short_}`, ok: null,
      detail: `This carries on by itself when the line finishes. It works until ${until}, for this one machine; ` +
              "you can close this window and the machine still adds itself."};
    if (!setChecks([name, todo, waiting], run)) return;
    for (;;) {                       // until it reports in and its adding starts (the server notes the job)
      await pause(3000);
      if (run !== w.run) return;
      let list;
      try { list = await api("join"); } catch (e) { continue; }
      const tok = list.tokens.find(x => x.id === j.id);
      const got = tok?.used.find(h => tok.jobs?.[h]);
      if (got)
        return addAndFollow([name, {step: "The line ran", ok: true, detail: `${got} set itself up and reported in`}],
                            async () => ({job: tok.jobs[got]}), got, run);
      if (!tok?.active) return setChecks([name, {...todo, ok: false, html: "",
                                                 detail: "the line expired or was revoked; press Test again for a new one"}], run);
    }
  }

  async function addAndFollow(done, start, host, run) {
    const adding = {step: "Adding", ok: null, detail: "starting"};
    if (!setChecks([...done, adding], run)) return;
    let j;
    try { j = await start(); } catch (e) { return setChecks([...done, {step: "Adding", ok: false, detail: e.message}], run); }
    w.job = j.job;
    loadState();
    let offset = 0, text = "";
    for (;;) {
      await pause(1500);
      if (run !== w.run) return;
      let out;
      try { out = await api(`job/${encodeURIComponent(j.job)}?offset=${offset}`); } catch (e) { continue; }
      text += out.text; offset = out.offset;
      const heads = text.split("\n").filter(l => /^--- \d\. /.test(l));
      adding.detail = heads.length ? heads[heads.length - 1].replace(/^--- \d\. /, "") : "working";
      if (out.done) {
        const short_ = shortName(host);
        const result = out.rc === 0 ? {step: "Added", ok: true, detail: `${short_} is on the dashboard`}
          : {step: "Adding", ok: false, detail: (text.split("\n").reverse().find(l => l.startsWith("!!")) || "it stopped; see the log").replace(/^!! /, "")};
        setChecks([...done, result], run);
        loadStatus(); loadState();
        return;
      }
      setChecks([...done, adding], run);
    }
  }

  async function runDevice(run) {
    setChecks([{step: "Signing in", ok: null, detail: `to ${w.req.device.url}`}], run);
    let t;
    try { t = await api("devices", {action: "test", ...w.req}); }
    catch (e) { return setChecks([{step: "Checking", ok: false, detail: e.message}], run); }
    if (!t.ok) return setChecks([{step: "Signing in", ok: false, detail: t.error}], run);
    const seen = {step: "Signed in", ok: true, detail: [t.model, t.version].filter(Boolean).join(" ") +
      (t.update_available ? ` · update ${t.latest || "available"}` : t.update_available === false ? " · up to date" : "")};
    setChecks([seen, {step: "Adding", ok: null, detail: "saving"}], run);
    try { await api("devices", {action: "save", ...w.req}); }
    catch (e) { return setChecks([seen, {step: "Adding", ok: false, detail: e.message}], run); }
    setChecks([seen, {step: "Added", ok: true, detail: `${w.req.device.name} is on the dashboard; checking its firmware now`}], run);
    setTimeout(loadStatus, 4000); loadState();
  }

  /* ---------- wiring ---------- */
  document.addEventListener("click", e => {
    if (!$("#wiz")?.open) return;
    const tile = e.target.closest("[data-kind]");
    if (tile) { w.kind = tile.dataset.kind; w.step = 2; w.method = defaultWay(kind()); return render(); }
    const b = e.target.closest("[data-w]");
    if (!b) return;
    const a = b.dataset.w;
    if (a === "close") return close();
    if (a === "back") {
      if (w.step === 2) isDevice() ? keepDevice() : keep();
      w.run = (w.run || 0) + 1;                                  // stops any wait still running
      w.step = w.step === 3 ? 2 : 1;
      return render();
    }
    if (a === "line") { w.method = "line"; w.req = {...w.req, method: "line", password: ""}; return go(); }
    if (a === "go") return go();
    if (a === "log") { const job = w.job; close(); openTab(job, true); }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Enter" && $("#wiz")?.open && w.step === 2 && e.target.matches("#wiz-body input:not([type=radio]):not([type=checkbox])")) {
      e.preventDefault(); go();
    }
  });
  $("#wiz").addEventListener("close", () => { w.closed = true; w.run = (w.run || 0) + 1; });   // stop waiting
  return {open};
})();

function enrollMachine() { Wizard.open(); }

async function createJoin(opts) {
  let j;
  try { j = await api("join", {action: "create", ...opts}); } catch (e) { return void toast(e.message, "fail"); }
  const until = new Date(j.expires * 1000).toLocaleString();
  await ask({title: "Join command", okText: "Done",
    desc: `<p>Run it on each machine. It works until <b>${esc(until)}</b>${j.max_uses ? ` for up to <b>${j.max_uses}</b>
        machine${j.max_uses === 1 ? "" : "s"}` : ""}; you see it only now.</p>
      <h4 class="set-h">Linux / BSD (as a user who can sudo)</h4>
      <pre class="report cmd">${esc(j.commands.linux)}</pre><button class="btn sm ghost" type="button" id="copy-jl">Copy</button>
      <h4 class="set-h">Windows (administrator PowerShell)</h4>
      <pre class="report cmd">${esc(j.commands.windows)}</pre><button class="btn sm ghost" type="button" id="copy-jw">Copy</button>
      <p class="muted small">Each machine shows up under Activity as it joins. Want to read the script first? Open the
        address without <code>| sudo sh</code>. Revoke it any time in Settings → Join commands.</p>`,
    onOpen: () => { copyButton("copy-jl", j.commands.linux); copyButton("copy-jw", j.commands.windows); }});
}

/* Settings > Join commands */
async function renderJoins(body) {
  let data;
  try { data = await api("join"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
  const state = t => t.revoked ? "revoked" : t.active ? `valid until ${new Date(t.expires * 1000).toLocaleString()}`
    : t.left === 0 ? "used up" : "expired";
  body.innerHTML = `<p class="set-intro">One-line commands that machines run to add themselves. Each works for a limited time
      and number of machines; only a fingerprint of it is kept.</p>
    <ul class="set-list">${data.tokens.map(t => `<li><span><b>${esc(t.name)}</b>
        <span class="muted small">${esc(state(t))} · by ${esc(t.created_by)} ·
        ${t.max_uses ? `${t.used.length} of ${t.max_uses} used` : `${t.used.length} used, no limit`}${t.used.length ? ": " +
          t.used.map(h => esc(short(h))).join(", ") : ""}</span></span>
        ${t.active ? `<button class="btn sm link" type="button" data-join-rm="${esc(t.id)}">Revoke…</button>` : ""}</li>`).join("")
      || '<li class="muted">No join commands in the last week.</li>'}</ul>
    <div><button class="btn sm" type="button" id="join-new">Create join command…</button></div>`;
  $$("[data-join-rm]", body).forEach(b => b.onclick = async () => {
    if (!await ask({title: "Revoke this join command?", desc: "Machines that already joined stay; it cannot add more.",
                    okText: "Revoke", danger: true})) return;
    try { await api("join", {action: "revoke", id: b.dataset.joinRm}); toast("Revoked", "ok"); } catch (e) { toast(e.message, "fail"); }
    renderJoins(body);
  });
  $("#join-new", body).onclick = async () => {
    const r = await ask({title: "Create a join command", okText: "Create",
      desc: "Machines run it to set themselves up and ask Patchyard to add them.",
      opts: `<div class="dev-grid"><label class="field"><span>Valid for</span><select name="hours">${Object.entries(HOURS).map(([h, t]) =>
          `<option value="${h}">${t}</option>`).join("")}</select></label>
        <label class="field"><span>Machines <span class="muted">(0 = no limit)</span></span>
          <input type="number" name="uses" min="0" max="500" value="1"></label></div>
        <label class="field"><span>Name <span class="muted">(optional)</span></span><input type="text" name="name" autocomplete="off"></label>`});
    if (!r) return;
    await createJoin({hours: Number(r.opts.hours), uses: Number(r.opts.uses), name: r.opts.name});
    renderJoins(body);
  };
}
Settings.addSection({id: "join", label: "Join commands", render: renderJoins});
