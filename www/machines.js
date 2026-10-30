// Per-machine settings (patchyard/machines.py): the editor for one machine, opened from its
// panel, and the defaults for every machine, a section of the Settings page. Each setting is
// either inherited (the default, or the system's own behaviour) or set at this level; a value
// written by hand in host_vars/ or group_vars/ wins and is shown locked.
// Loaded after app.js and settings.js; uses their helpers.
"use strict";

const MachineSettings = (() => {
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const DAY = {mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun"};
  const MODE = {install: "Install automatically", download_only: "Download only, wait for Patch"};
  const SOURCE = {machine: "this machine", default: "default", inventory: "inventory file", builtin: "system"};
  const APPLIES = {linux: "Linux", apt: "Debian and Ubuntu", windows: "Windows"};
  let schemaCache = null;

  async function schema() {
    if (!schemaCache) schemaCache = (await api("machine-settings")).schema;
    return schemaCache;
  }

  function daysText(days) {
    const idx = DAYS.map((d, i) => days.includes(d) ? i : -1).filter(i => i >= 0);
    if (idx.length === 7) return "every day";
    if (idx.length > 2 && idx[idx.length - 1] - idx[0] === idx.length - 1) return `${DAY[DAYS[idx[0]]]}–${DAY[DAYS[idx[idx.length - 1]]]}`;
    return idx.map(i => DAY[DAYS[i]]).join(", ");
  }

  /* a value in words */
  function text(st, v) {
    if (v === undefined || v === null) return st.builtin || "not set";
    switch (st.kind) {
      case "bool": return v ? "on" : "off";
      case "int": return `${v} min`;
      case "blackout":
        if (v.hours_utc) return `UTC hours ${v.hours_utc.map(h => String(h).padStart(2, "0") + ":00").join(", ")}`;
        return `${daysText(v.days || DAYS)} ${v.start}–${v.end}${v.reason ? " · " + v.reason : ""}`;
      case "manual": return v === false ? "off" : v === true || !v.reason ? "on" : `on · ${v.reason}`;
      case "packages": return (v.packages || []).join(", ") + (v.reason ? ` · ${v.reason}` : "");
      case "list": case "categories": return v.length ? v.join(", ") : "none";
      case "choice": return MODE[v] || v;
      default: return String(v);
    }
  }
  function describe(key, v) {
    const st = (schemaCache || []).find(s => s.key === key);
    return st ? text(st, v) : (v === null || v === undefined ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v));
  }
  const label = key => (schemaCache || []).find(s => s.key === key)?.label;

  /* controls for one kind, prefilled with v (or a sensible start) */
  function editorHtml(st, v, winDefault) {
    const val = s => esc(s ?? "");
    switch (st.kind) {
      case "time": return `<input type="time" data-part="v" value="${val(v || "03:00")}">`;
      case "int": return `<input type="number" data-part="v" min="${st.lo ?? ""}" max="${st.hi ?? ""}" value="${val(v ?? parseInt(st.builtin, 10))}">`;
      case "bool": return `<label class="check"><input type="checkbox" data-part="v" ${v === false ? "" : "checked"}> On</label>`;
      case "blackout": {
        const b = v && !v.hours_utc ? v : {days: ["mon", "tue", "wed", "thu", "fri"], start: "08:00", end: "18:00", reason: ""};
        return `<div class="ms-days">${DAYS.map(d => `<label class="check"><input type="checkbox" data-part="day" value="${d}"
            ${(b.days || DAYS).includes(d) ? "checked" : ""}> ${DAY[d]}</label>`).join("")}</div>
          <div class="ms-inline"><input type="time" data-part="start" value="${val(b.start)}"> to
            <input type="time" data-part="end" value="${val(b.end)}"> <span class="muted small">its local time</span></div>
          <input type="text" data-part="reason" placeholder="Why (shown on the dashboard)" value="${val(b.reason)}">
          ${v && v.hours_utc ? `<small class="help">Replaces the older UTC-hours window (${esc(text(st, v))}).</small>` : ""}`;
      }
      case "manual":
        return `<label class="check"><input type="checkbox" data-part="on" ${v === false ? "" : "checked"}> On</label>
          <input type="text" data-part="reason" placeholder="Why (shown on the dashboard)" value="${val(v && v.reason)}">`;
      case "packages":
        return `<textarea data-part="packages" rows="2" placeholder="one per line">${esc((v?.packages || []).join("\n"))}</textarea>
          <input type="text" data-part="reason" placeholder="Why" value="${val(v && v.reason)}">`;
      case "list": return `<textarea data-part="v" rows="2" placeholder="one per line">${esc((v || []).join("\n"))}</textarea>`;
      case "categories":
        return `<div class="ms-days">${st.choices.map(c => `<label class="check"><input type="checkbox" data-part="cat" value="${esc(c)}"
          ${(v || winDefault).includes(c) ? "checked" : ""}> ${esc(c)}</label>`).join("")}</div>`;
      case "choice":
        return `<select data-part="v">${st.choices.map(c => `<option value="${esc(c)}" ${c === (v || st.choices[0]) ? "selected" : ""}>
          ${esc(MODE[c] || c)}</option>`).join("")}</select>`;
    }
    return "";
  }
  function readEditor(st, box) {
    const part = p => $(`[data-part="${p}"]`, box);
    const lines = el => el.value.split("\n").map(s => s.trim()).filter(Boolean);
    switch (st.kind) {
      case "int": return Number(part("v").value);
      case "bool": return part("v").checked;
      case "blackout": {
        const b = {days: $$('[data-part="day"]:checked', box).map(i => i.value), start: part("start").value, end: part("end").value};
        if (part("reason").value.trim()) b.reason = part("reason").value.trim();
        return b;
      }
      case "manual": return part("on").checked ? (part("reason").value.trim() ? {reason: part("reason").value.trim()} : true) : false;
      case "packages": {
        const p = {packages: lines(part("packages"))};
        if (part("reason").value.trim()) p.reason = part("reason").value.trim();
        return p;
      }
      case "list": return lines(part("v"));
      case "categories": return $$('[data-part="cat"]:checked', box).map(i => i.value);
      default: return part("v").value;
    }
  }

  /* one setting: inherit or set here */
  function rowHtml(st, {mine, inherited, source, level, effective, winDefault, pending}) {
    const has = pending ? pending.mode === "set" : mine !== undefined;
    const value = pending ? pending.value : mine;
    const applies = level === "defaults" && APPLIES[st.applies] ? `<span class="badge">${APPLIES[st.applies]}</span>` : "";
    const src = source ? `<span class="badge src-${source}" title="Where the value in effect comes from">${SOURCE[source]}</span>` : "";
    const help = st.help ? `<small class="help">${esc(st.help)}</small>` : "";
    if (source === "inventory")
      return `<div class="ms-row" data-key="${esc(st.key)}" data-locked="1"><div class="ms-head"><b>${esc(st.label)}</b> ${src}</div>
        <p class="ro">${esc(text(st, effective))}</p>
        <small class="help">Set by hand in an inventory file (host_vars/ or group_vars/), which takes precedence; change it there.</small></div>`;
    const inheritLabel = level === "defaults" ? `Not set: ${text(st, undefined)}` : `Use the default: ${text(st, inherited)}`;
    return `<div class="ms-row" data-key="${esc(st.key)}"><div class="ms-head"><b>${esc(st.label)}</b> ${applies}${src}</div>
      <select class="ms-mode" aria-label="${esc(st.label)}">
        <option value="inherit" ${has ? "" : "selected"}>${esc(inheritLabel)}</option>
        <option value="set" ${has ? "selected" : ""}>${level === "defaults" ? "Set for every machine" : "Set for this machine"}</option>
      </select>
      <div class="ms-edit" ${has ? "" : "hidden"}>${editorHtml(st, value, winDefault)}</div>${help}</div>`;
  }
  function wire(root, onChange) {
    $$(".ms-mode", root).forEach(sel => sel.onchange = () => {
      $(".ms-edit", sel.closest(".ms-row")).hidden = sel.value !== "set";
      onChange && onChange();
    });
    if (onChange) { root.addEventListener("input", onChange); root.addEventListener("change", onChange); }
  }
  /* {key: value or null} for what differs from `stored` */
  function collect(root, sts, stored) {
    const out = {};
    for (const st of sts) {
      const row = $(`.ms-row[data-key="${CSS.escape(st.key)}"]`, root);
      if (!row || row.dataset.locked) continue;
      const set = $(".ms-mode", row).value === "set";
      const v = set ? readEditor(st, row) : null;
      const before = stored[st.key] === undefined ? null : stored[st.key];
      if (JSON.stringify(v) !== JSON.stringify(before)) out[st.key] = v;
    }
    return out;
  }
  function snapshot(root, sts) {
    const p = {};
    for (const st of sts) {
      const row = $(`.ms-row[data-key="${CSS.escape(st.key)}"]`, root);
      if (row && !row.dataset.locked) p[st.key] = {mode: $(".ms-mode", row).value, value: readEditor(st, row)};
    }
    return p;
  }

  async function offerPolicy(hosts) {
    if (!hosts || !hosts.length) return;
    const n = hosts.length === 1 ? short(hosts[0]) : `${hosts.length} machines`;
    const r = await ask({title: "Apply the new update settings?", okText: "Apply now",
      desc: `Machines pick up their install time and automatic-update settings when the auto-update policy runs.
        Run it on <b>${esc(n)}</b> now? (Nothing is installed or restarted.)`});
    if (!r) return;
    try { const j = await api("run", {action: "auto-updates", hosts}); await loadState(); openTab(j.job, true); }
    catch (e) { toast(e.message, "fail"); }
  }

  /* ---------- one machine: a dialog from its panel ---------- */
  async function edit(host, pending) {
    let v;
    try { v = await api("machine-settings?host=" + encodeURIComponent(host)); } catch (e) { return void toast(e.message, "fail"); }
    schemaCache = v.schema;
    const sts = v.schema.filter(s => v.applies.includes(s.key));
    const rows = sts.map(st => rowHtml(st, {mine: v.values[st.key], inherited: v.defaults[st.key], source: v.source[st.key],
      level: "machine", effective: v.effective[st.key], winDefault: v.windows_default, pending: pending && pending[st.key]})).join("");
    $("#dlg").classList.add("wide");
    let state = null;
    const res = await ask({title: `Settings for ${short(host)}`, okText: "Save",
      desc: `<p class="muted">Each setting uses the default for every machine (Settings → Machine defaults) unless you set it
        here.${v.family === "unknown" ? " Every setting is listed because the machine has not been checked yet." : ""}</p>`,
      opts: `<div class="ms" id="ms-dlg">${rows}</div>`,
      onOpen: () => wire($("#ms-dlg"))});
    if (res) state = {values: collect($("#ms-dlg"), sts, v.values), snap: snapshot($("#ms-dlg"), sts)};
    $("#dlg").classList.remove("wide");
    if (!state) return;
    if (!Object.keys(state.values).length) return void toast("Nothing changed");
    try {
      const r = await api("machine-settings", {host, values: state.values});
      toast(`Saved for ${short(host)}`, "ok");
      await offerPolicy(r.apply_policy_to);
    } catch (e) {
      toast(e.message, "fail");
      edit(host, state.snap);                      // reopen with what was typed
    }
  }

  /* ---------- every machine: a section of the Settings page ---------- */
  async function renderDefaults(body) {
    let v;
    try { v = await api("machine-settings"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    schemaCache = v.schema;
    body.innerHTML = `<p class="set-intro">What every machine does unless its own settings say otherwise (open a machine and
        press Edit settings). Times are each machine's local time.</p>
      <form class="set-form" id="ms-form" novalidate>
        <p class="err" id="ms-err" role="alert"></p>
        <div class="ms ms-page" id="ms-page">${v.schema.map(st => rowHtml(st, {mine: v.defaults[st.key], level: "defaults",
          winDefault: v.windows_default})).join("")}</div>
        <div class="set-foot">
          <button class="btn" type="submit" id="ms-save" disabled>Save changes</button>
          <button class="btn ghost" type="button" id="ms-discard" disabled>Discard</button>
        </div>
      </form>`;
    const root = $("#ms-page", body);       // the dialog keeps its own editor (#ms-dlg) after closing
    const update = () => {
      const dirty = Object.keys(collect(root, v.schema, v.defaults)).length > 0;
      Settings.setDirty(dirty);
      $("#ms-save").disabled = $("#ms-discard").disabled = !dirty;
    };
    wire(root, update);
    $("#ms-discard").onclick = () => { Settings.setDirty(false); renderDefaults(body); };
    $("#ms-form").onsubmit = async e => {
      e.preventDefault();
      const values = collect(root, v.schema, v.defaults);
      if (!Object.keys(values).length) return;
      try {
        const r = await api("machine-settings", {host: null, values});
        Settings.setDirty(false);
        toast("Saved for every machine", "ok");
        await renderDefaults(body);
        await offerPolicy(r.apply_policy_to);
      } catch (err) { $("#ms-err").textContent = err.message; }
    };
  }

  Settings.addSection({id: "machines", label: "Machine defaults", render: renderDefaults});
  return {edit, schema, describe, label, text};
})();
