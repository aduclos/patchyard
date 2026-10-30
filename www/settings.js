// Settings page: every setting the web UI may change (the schema comes from the server,
// patchyard/config.py SETTINGS), grouped in sections, plus users, API tokens, the change
// history and version information. Loaded after app.js; uses its helpers
// ($, $$, esc, api, ask, toast, ago, S, SUPPORT_URL).
"use strict";

const Settings = (() => {
  const CHOICE_LABEL = {ntfy: "ntfy push", webhook: "Webhook", smtp: "E-mail"};
  const AUTH_LABEL = {local: "Patchyard accounts", kerberos: "Domain accounts", none: "No sign-in"};
  const INTRO = {
    general: "How Patchyard names itself and the machines you add.",
    schedule: "How often Patchyard looks and when it reminds you. Checks only read; they never change a machine.",
    notifications: "Where alerts go. Patchyard alerts only on new problems and waiting reboots, plus one daily reminder.",
    signin: "Who can sign in, and how long a session lasts.",
  };
  const LOCK = `<span class="lock" title="Set in the configuration file (web.settings_locked)"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>configuration file</span>`;
  const extra = [];         // sections other parts of the app add: {id, label, render(body)}
  let view = null;          // GET /api/settings
  let current = "general";
  let dirty = false;

  const root = () => $("#settings");
  const isOpen = () => !root().hidden;

  async function open(section) {
    try { view = await api("settings"); } catch (e) { return void toast(e.message, "fail"); }
    root().hidden = false;
    dirty = false;
    show(section || current);
  }
  async function close() {
    if (await leaveOk()) root().hidden = true;
  }
  async function leaveOk() {
    if (!dirty) return true;
    const r = await ask({title: "Discard unsaved changes?", desc: "This section has changes that are not saved yet.",
                         okText: "Discard", danger: true});
    if (r) dirty = false;
    return !!r;
  }
  async function reload(section) {
    view = await api("settings");
    dirty = false;
    show(section);
  }

  function sections() {
    const list = Object.entries(view.sections).map(([id, label]) => ({id, label, render: renderFields}));
    list.push(...extra);
    if (view.auth_method === "local") list.push({id: "users", label: "Users", render: renderUsers});
    list.push({id: "tokens", label: "API tokens", render: renderTokens},
              {id: "history", label: "Change history", render: renderHistory}, {id: "about", label: "About", render: renderAbout});
    return list;
  }
  function show(id) {
    const list = sections();
    const sec = list.find(s => s.id === id) || list[0];
    current = sec.id;
    $("#set-nav").innerHTML = list.map(s =>
      `<button type="button" class="nav-item ${s.id === current ? "on" : ""}" data-sec="${esc(s.id)}">${esc(s.label)}</button>`).join("");
    $("#set-title").textContent = sec.label;
    const body = $("#set-body");
    body.innerHTML = "";
    sec.render(body, sec.id);
    body.scrollTop = 0;
  }

  /* ---------- fields from the schema ---------- */
  const fieldsOf = id => view.fields.filter(f => f.section === id);
  const fid = key => "s-" + key.replace(/\W/g, "-");

  function fieldHtml(f) {
    const id = fid(f.key), dis = f.locked ? "disabled" : "", lock = f.locked ? LOCK : "";
    const help = f.help ? `<small class="help">${esc(f.help)}</small>` : "";
    const attrs = `id="${id}" ${dis}`;
    let input;
    switch (f.kind) {
      case "readonly":
        input = `<div class="ro">${esc(f.key === "auth.method" ? AUTH_LABEL[f.value] || f.value : f.value || "–")}</div>`; break;
      case "bool":
        return `<div class="set-field" data-field="${esc(f.key)}" data-show-if="${esc(f.show_if)}">
          <label class="check"><input type="checkbox" ${attrs} ${f.value ? "checked" : ""}> <span>${esc(f.label)}</span> ${lock}</label>${help}</div>`;
      case "int":
        input = `<input type="number" ${attrs} value="${esc(f.value)}" ${f.lo !== null ? `min="${f.lo}"` : ""} ${f.hi !== null ? `max="${f.hi}"` : ""}>`; break;
      case "hour":
        input = `<select ${attrs}><option value="-1">Off</option>${Array.from({length: 24}, (_, h) =>
          `<option value="${h}" ${h === f.value ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select>`; break;
      case "list":
        input = `<textarea ${attrs} rows="3" spellcheck="false">${esc((f.value || []).join("\n"))}</textarea>`; break;
      case "choices":
        input = `<div class="choice-row" id="${id}">${f.choices.map(c => `<label class="check"><input type="checkbox" value="${esc(c)}"
          ${(f.value || []).includes(c) ? "checked" : ""} ${dis}> ${esc(CHOICE_LABEL[c] || c)}</label>`).join("")}</div>`; break;
      case "secret":
        input = `<div class="secret"><input type="password" ${attrs} autocomplete="new-password"
          placeholder="${f.value ? "Saved. Type a new one to replace it" : "Not set"}">${f.value && !f.locked
          ? `<button type="button" class="btn sm link" data-clear>Remove</button>` : ""}</div>`; break;
      default:
        input = `<input type="text" ${attrs} value="${esc(f.value)}" spellcheck="false">`;
    }
    return `<div class="set-field" data-field="${esc(f.key)}" data-show-if="${esc(f.show_if)}">
      <label for="${id}">${esc(f.label)} ${lock}</label>${input}${help}</div>`;
  }

  function readValue(f) {
    const box = $(`#set-form [data-field="${CSS.escape(f.key)}"]`);
    if (!box) return f.value;
    switch (f.kind) {
      case "bool": return $("input", box).checked;
      case "int": case "hour": return Number($("input, select", box).value);
      case "list": return $("textarea", box).value.split("\n").map(s => s.trim()).filter(Boolean);
      case "choices": return $$("input:checked", box).map(i => i.value);
      case "secret": return box.dataset.clear === "1" ? null : $("input", box).value;
      case "readonly": return f.value;
      default: return $("input", box).value.trim();
    }
  }
  function pending(fs) {
    const out = {};
    for (const f of fs) {
      if (f.locked || f.kind === "readonly") continue;
      const v = readValue(f);
      if (f.kind === "secret") { if (v === null || v) out[f.key] = v; }
      else if (JSON.stringify(v) !== JSON.stringify(f.value)) out[f.key] = v;
    }
    return out;
  }
  function valueOf(key) {
    const f = view.fields.find(x => x.key === key);
    return f ? readValue(f) : undefined;
  }
  function visible(cond) {
    let m;
    if (!cond) return true;
    if ((m = cond.match(/^([\w.]+)~(.*)$/))) return (valueOf(m[1]) || []).includes(m[2]);
    if ((m = cond.match(/^([\w.]+)!=(.*)$/))) return String(valueOf(m[1])) !== m[2];
    if ((m = cond.match(/^([\w.]+)=(.*)$/))) return String(valueOf(m[1])) === m[2];
    return true;
  }

  function renderFields(body, id) {
    const fs = fieldsOf(id);
    body.innerHTML = `${INTRO[id] ? `<p class="set-intro">${esc(INTRO[id])}</p>` : ""}
      <form class="set-form" id="set-form" novalidate>
        <p class="err" id="set-err" role="alert"></p>
        ${fs.map(fieldHtml).join("")}
        <div class="set-foot">
          <button class="btn" type="submit" id="set-save" disabled>Save changes</button>
          <button class="btn ghost" type="button" id="set-discard" disabled>Discard</button>
          ${id === "notifications" ? `<button class="btn ghost" type="button" id="set-test">Send a test alert</button>` : ""}
          <span class="muted small" id="set-note">${fs.some(f => f.locked) ? "Locked settings are changed in the configuration file." : ""}</span>
        </div>
      </form>`;
    const form = $("#set-form");
    const update = () => {
      dirty = Object.keys(pending(fs)).length > 0;
      $("#set-save").disabled = $("#set-discard").disabled = !dirty;
      $$("[data-show-if]", form).forEach(b => b.hidden = !visible(b.dataset.showIf));
    };
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    $$("[data-clear]", form).forEach(b => b.onclick = () => {
      const box = b.closest("[data-field]");
      box.dataset.clear = "1";
      $("input", box).value = ""; $("input", box).placeholder = "Will be removed when you save";
      b.remove(); update();
    });
    $("#set-discard").onclick = () => { dirty = false; show(id); };
    form.onsubmit = async e => {
      e.preventDefault();
      const changes = pending(fs);
      if (!Object.keys(changes).length) return;
      $("#set-save").disabled = true;
      try {
        const r = await api("settings", changes);
        toast(r.changed.length ? "Saved. It applies now." : "Nothing changed", "ok");
        await reload(id);
        if (view.site) $("#site-name").textContent = view.site;
      } catch (err) { $("#set-err").textContent = err.message; update(); }
    };
    const test = $("#set-test");
    if (test) test.onclick = async () => {
      if (dirty) return void ($("#set-err").textContent = "Save first: the test uses the saved settings.");
      test.disabled = true;
      try {
        const r = await api("notify-test", {});
        const parts = Object.entries(r.results).map(([b, err]) => `${CHOICE_LABEL[b] || b}: ${err ? "failed (" + err + ")" : "sent"}`);
        const bad = Object.values(r.results).some(Boolean);
        $("#set-note").textContent = parts.join(" · ");
        toast(bad ? "A test alert failed" : "Test alert sent", bad ? "fail" : "ok");
      } catch (err) { $("#set-err").textContent = err.message; }
      test.disabled = false;
    };
    update();
  }

  /* ---------- users, tokens ---------- */
  async function renderUsers(body) {
    let users;
    try { users = await api("users"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    body.innerHTML = `<p class="set-intro">People who sign in to this dashboard with a Patchyard account. Everyone here can do everything.</p>
      <ul class="set-list">${users.users.map(u => `<li><span>${esc(u)}${u === S.user ? ' <span class="muted">(you)</span>' : ""}</span><span>
        <button class="btn sm ghost" type="button" data-user-pw="${esc(u)}">Change password…</button>
        ${u === S.user ? "" : `<button class="btn sm link" type="button" data-user-rm="${esc(u)}">Remove…</button>`}</span></li>`).join("")}</ul>
      <div><button class="btn sm" type="button" id="user-add">Add user…</button></div>`;
    $$("[data-user-pw]", body).forEach(b => b.onclick = async () => {
      const r = await ask({title: `New password for ${b.dataset.userPw}`, desc: "At least 10 characters.", needPw: true,
                           pwLabel: "New password", okText: "Save"});
      if (!r) return;
      try { await api("users", {action: "password", name: b.dataset.userPw, password: r.pw}); toast("Password changed", "ok"); }
      catch (e) { toast(e.message, "fail"); }
    });
    $$("[data-user-rm]", body).forEach(b => b.onclick = async () => {
      if (!await ask({title: `Remove ${b.dataset.userRm}?`, desc: "They can no longer sign in. Their API tokens stop working.",
                      okText: "Remove", danger: true})) return;
      try { await api("users", {action: "remove", name: b.dataset.userRm}); toast("Removed", "ok"); } catch (e) { toast(e.message, "fail"); }
      show("users");
    });
    $("#user-add", body).onclick = async () => {
      const r = await ask({title: "Add a user", desc: "They sign in with this name and password.", needPw: true,
        pwLabel: "Password (10+ characters)", okText: "Add",
        opts: `<label class="field"><span>User name</span><input type="text" name="name" autocomplete="off" spellcheck="false"></label>`});
      if (!r) return;
      try { await api("users", {action: "add", name: r.opts.name, password: r.pw}); toast(`Added ${r.opts.name}`, "ok"); }
      catch (e) { toast(e.message, "fail"); }
      show("users");
    };
  }

  async function renderTokens(body) {
    let tokens;
    try { tokens = await api("tokens"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    body.innerHTML = `<p class="set-intro">Tokens let scripts and AI assistants (the MCP server) use Patchyard as the person who
        created them. A token is shown once; only its fingerprint is kept.</p>
      <ul class="set-list">${tokens.tokens.map(t => `<li><span>${esc(t.name)} <span class="muted small">${esc(t.user)} ·
        ${t.last_used ? "used " + ago(t.last_used) : "never used"}</span></span>
        <button class="btn sm link" type="button" data-token-rm="${esc(t.id)}">Revoke…</button></li>`).join("") || '<li class="muted">No tokens yet.</li>'}</ul>
      <div><button class="btn sm" type="button" id="token-add">Create token…</button></div>`;
    $$("[data-token-rm]", body).forEach(b => b.onclick = async () => {
      if (!await ask({title: "Revoke this token?", desc: "Whatever uses it stops working at once.", okText: "Revoke", danger: true})) return;
      try { await api("tokens", {action: "revoke", id: b.dataset.tokenRm}); toast("Token revoked", "ok"); } catch (e) { toast(e.message, "fail"); }
      show("tokens");
    });
    $("#token-add", body).onclick = async () => {
      const r = await ask({title: "Create an API token", okText: "Create",
        desc: "It acts as you. You see it once; store it in your MCP client or script.",
        opts: `<label class="field"><span>What is it for?</span><input type="text" name="name" placeholder="laptop MCP" autocomplete="off"></label>`});
      if (!r) return;
      try {
        const t = await api("tokens", {action: "create", name: r.opts.name});
        await ask({title: "Your new token", okText: "Done",
          desc: `<p>Copy it now; it is not shown again.</p><pre class="report cmd">${esc(t.token)}</pre>
            <p class="muted small">MCP client: command <code>patchyard mcp</code>, environment
            <code>PATCHYARD_URL=${esc(location.origin)}</code> and <code>PATCHYARD_TOKEN</code> (or <code>PATCHYARD_TOKEN_FILE</code>).</p>`});
      } catch (e) { toast(e.message, "fail"); }
      show("tokens");
    };
  }

  /* ---------- history, about ---------- */
  const shown = v => v === null || v === undefined || v === "" ? "–" : Array.isArray(v) ? (v.length ? v.join(", ") : "–")
    : typeof v === "boolean" ? (v ? "on" : "off") : typeof v === "object" ? JSON.stringify(v) : String(v);

  async function renderHistory(body) {
    let log;
    try { log = await api("audit"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    await MachineSettings.schema().catch(() => null);             // labels for machine settings
    const label = k => view.fields.find(f => f.key === k)?.label || MachineSettings.label(k) || k;
    const fmt = (k, v) => k.startsWith("patchyard_") ? MachineSettings.describe(k, v) : shown(v);
    body.innerHTML = `<p class="set-intro">Every change made in Settings and in machine settings, newest first.</p>
      <ul class="hist">${log.entries.map(e => `<li><div class="when">${esc(new Date(e.time * 1000).toLocaleString())} ·
        ${esc(e.user)} · ${esc(e.what)}</div><ul>${Object.entries(e.changes).map(([k, v]) => `<li><b>${esc(label(k))}</b>:
        ${Array.isArray(v) && v.length === 2 ? `${esc(fmt(k, v[0]))} → ${esc(fmt(k, v[1]))}` : esc(shown(v))}</li>`).join("")}</ul></li>`).join("")
        || '<li class="muted">Nothing has been changed yet.</li>'}</ul>`;
  }

  function renderAbout(body) {
    body.innerHTML = `<dl class="kv">
        <dt>Version</dt><dd>${esc(view.version)}</dd>
        <dt>Configuration file</dt><dd><code>${esc(view.config_file)}</code></dd>
        <dt>Changes made here</dt><dd>saved as <code>settings.json</code> in the state directory; they take precedence over the file</dd>
        <dt>Locked to the file</dt><dd>${view.locked.length ? view.locked.map(k => `<code>${esc(k)}</code>`).join(" ") : "nothing"}
          <span class="muted">(<code>web.settings_locked</code>)</span></dd>
        <dt>Sign-in</dt><dd>${esc(AUTH_LABEL[view.auth_method] || view.auth_method)} · you are <b>${esc(S.user)}</b></dd>
        <dt>Service account</dt><dd><code>${esc(view.service_account)}</code> · key <code>${esc(view.key_fingerprint)}</code></dd>
      </dl>
      <p class="muted">Patchyard is free and open source (MIT).${SUPPORT_URL ? ` If it saves you time, you can
        <a href="${SUPPORT_URL}" target="_blank" rel="noopener">support the project</a>.` : ""}</p>`;
  }

  /* ---------- wiring ---------- */
  document.addEventListener("click", async e => {
    const nav = e.target.closest("[data-sec]");
    if (nav && isOpen()) { if (nav.dataset.sec !== current && await leaveOk()) show(nav.dataset.sec); return; }
    if (e.target.closest("#set-close")) close();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && isOpen() && !$("#dlg").open) { e.preventDefault(); close(); }
  });
  window.addEventListener("beforeunload", e => { if (isOpen() && dirty) e.preventDefault(); });

  return {open, close, isOpen, addSection: s => extra.push(s), reload: () => reload(current), view: () => view,
          setDirty: d => { dirty = d; }};
})();
