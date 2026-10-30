// Network devices on the Settings page (patchyard/devices/store.py): add, edit, test and remove
// routers, access points and NAS boxes. A device's login is write-only: the editor only shows
// whether a password or key is stored. Devices from the configuration file are listed read-only.
// Loaded after app.js and settings.js; uses their helpers.
"use strict";

const DeviceSettings = (() => {
  let data = null;               // GET /api/devices

  function fieldHtml(f, value, stored) {
    const name = `${f.option ? "opt" : "cred"}:${f.key}`;
    const help = f.help ? `<small class="help">${esc(f.help)}</small>` : "";
    if (f.choices.length)
      return `<label class="field"><span>${esc(f.label)}</span><select name="${esc(name)}">${f.choices.map(c =>
        `<option ${c === (value || f.default) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>${help}</label>`;
    if (f.secret)
      return `<label class="field"><span>${esc(f.label)}</span><input type="password" name="${esc(name)}" autocomplete="new-password"
        placeholder="${stored ? "Saved. Type a new one to replace it" : ""}">${help}</label>`;
    return `<label class="field"><span>${esc(f.label)}${f.required ? "" : ' <span class="muted">(optional)</span>'}</span>
      <input type="text" name="${esc(name)}" value="${esc(value ?? f.default)}" spellcheck="false" autocomplete="off">${help}</label>`;
  }

  function formHtml(dev, driver, pending) {
    const drv = data.drivers[driver];
    const v = (k, d) => pending?.[k] ?? dev?.[k] ?? d;
    const types = Object.entries(data.drivers).map(([k, d]) =>
      `<option value="${esc(k)}" ${k === driver ? "selected" : ""}>${esc(d.title)}</option>`).join("");
    const login = drv.fields.filter(f => !f.option), opts = drv.fields.filter(f => f.option);
    return `<label class="field"><span>Type</span><select name="driver" id="dev-driver">${types}</select></label>
      ${drv.help ? `<p class="dev-help">${esc(drv.help)}</p>` : ""}
      <div class="dev-grid">
        <label class="field"><span>Name</span><input type="text" name="name" value="${esc(v("name", ""))}" placeholder="core-switch"
          spellcheck="false" autocomplete="off"></label>
        <label class="field"><span>Address</span><input type="text" name="url" value="${esc(v("url", ""))}" placeholder="https://192.0.2.1"
          spellcheck="false" autocomplete="off"></label>
      </div>
      <label class="check"><input type="checkbox" name="verify_tls" ${v("verify_tls", true) ? "checked" : ""}> Check its HTTPS
        certificate <span class="muted small">(turn off only for a self-signed one)</span></label>
      ${login.length ? `<h4 class="set-h">Sign-in</h4>${login.map(f => fieldHtml(f, pending?.["cred:" + f.key] ??
        (f.secret ? "" : dev?.credentials?.[f.key]), dev?.credentials?.[f.key])).join("")}` : ""}
      ${opts.length ? `<h4 class="set-h">Options</h4>${opts.map(f => fieldHtml(f, pending?.["opt:" + f.key] ?? dev?.options?.[f.key])).join("")}` : ""}
      <label class="field"><span>What an update or reboot interrupts <span class="muted">(optional)</span></span>
        <input type="text" name="disruption" value="${esc(v("disruption", ""))}" placeholder="Office Wi-Fi drops for about 3 minutes">
        <small class="help">Shown before anyone confirms an update or reboot of this device.</small></label>
      <div class="dev-test"><button class="btn sm ghost" type="button" id="dev-test">Test connection</button>
        <span class="muted small" id="dev-test-out">Signs in once and reads the firmware version. Nothing is saved.</span></div>`;
  }

  function readForm(root) {
    const get = n => root.querySelector(`[name="${n}"]`);
    const device = {driver: get("driver").value, name: get("name").value.trim(), url: get("url").value.trim(),
                    verify_tls: get("verify_tls").checked, disruption: get("disruption").value.trim(), options: {}};
    const credentials = {}, raw = {};
    $$("[name^='cred:'], [name^='opt:']", root).forEach(el => {
      const [kind, key] = el.name.split(":");
      raw[el.name] = el.value;
      if (kind === "opt") device.options[key] = el.value.trim(); else credentials[key] = el.value;
    });
    return {device, credentials, pending: {...device, ...raw}};
  }

  async function editor(dev, pending) {
    if (!data) data = await api("devices");
    let driver = pending?.driver || dev?.driver || Object.keys(data.drivers)[0];
    const wireForm = () => {
      const root = $("#dev-form");
      $("#dev-driver").onchange = () => {            // another type: other fields, keep what is typed
        const p = readForm(root).pending;
        driver = $("#dev-driver").value;
        root.innerHTML = formHtml(dev, driver, p);
        wireForm();
      };
      $("#dev-test").onclick = async () => {
        const out = $("#dev-test-out"), f = readForm(root);
        out.textContent = "Signing in…"; out.className = "muted small";
        try {
          const r = await api("devices", {action: "test", device: f.device, credentials: f.credentials, original: dev?.name});
          if (r.ok) {
            out.textContent = `Connected: ${[r.model, r.version].filter(Boolean).join(" ") || "no model reported"}` +
              (r.update_available === true ? ` · update ${r.latest || "available"}` : r.update_available === false ? " · up to date" : "");
            out.className = "ok-msg small";
          } else { out.textContent = r.error; out.className = "bad-msg small"; }
        } catch (e) { out.textContent = e.message; out.className = "bad-msg small"; }
      };
    };
    $("#dlg").classList.add("wide");
    let form = null;
    const res = await ask({title: dev ? `Edit ${dev.name}` : "Add a network device", okText: dev ? "Save" : "Add device",
      desc: "", opts: `<div id="dev-form" class="dev-form">${formHtml(dev, driver, pending)}</div>`, onOpen: wireForm});
    if (res) form = readForm($("#dev-form"));
    $("#dlg").classList.remove("wide");
    if (!form) return false;
    try {
      await api("devices", {action: "save", device: form.device, credentials: form.credentials, original: dev?.name});
      toast(`${form.device.name} saved; checking it now`, "ok");
      data = null;
      return true;
    } catch (e) {
      toast(e.message, "fail");
      return editor(dev, form.pending);              // reopen with what was typed
    }
  }

  async function render(body) {
    try { data = await api("devices"); } catch (e) { return void (body.innerHTML = `<p class="err">${esc(e.message)}</p>`); }
    const rows = data.devices.map(d => {
      const login = d.source === "file" ? "" : Object.values(d.credentials).some(Boolean) ? "login saved" : "no login";
      return `<li><span><b>${esc(d.name)}</b> <span class="muted small">${esc(d.title)} · ${esc(d.url || "–")}</span>
        <span class="badge ${d.source === "file" ? "src-inventory" : "src-machine"}">${d.source === "file" ? "configuration file" : "added here"}</span>
        ${login ? `<span class="badge">${login}</span>` : ""}</span>
        <span>${d.source === "file" ? "" : `<button class="btn sm ghost" type="button" data-dev-edit="${esc(d.name)}">Edit…</button>
          <button class="btn sm link" type="button" data-dev-rm="${esc(d.name)}">Remove…</button>`}</span></li>`;
    }).join("");
    body.innerHTML = `<p class="set-intro">Routers, firewalls, access points and NAS boxes that Patchyard checks for new firmware
        and can update or reboot through their own management interface.</p>
      <ul class="set-list">${rows || '<li class="muted">No devices yet.</li>'}</ul>
      <div><button class="btn sm" type="button" id="dev-add">Add device…</button></div>
      ${data.devices.some(d => d.source === "file") ? `<p class="muted small">Devices from the configuration file are changed there.</p>` : ""}`;
    $("#dev-add", body).onclick = async () => { if (await editor(null)) render(body); };
    $$("[data-dev-edit]", body).forEach(b => b.onclick = async () => {
      if (await editor(data.devices.find(d => d.name === b.dataset.devEdit))) render(body);
    });
    $$("[data-dev-rm]", body).forEach(b => b.onclick = async () => {
      const name = b.dataset.devRm;
      if (!await ask({title: `Remove ${name}?`, okText: "Remove", danger: true,
        desc: "Patchyard stops checking it and forgets its login. Nothing changes on the device."})) return;
      try { await api("devices", {action: "remove", name}); toast(`${name} removed`, "ok"); } catch (e) { toast(e.message, "fail"); }
      render(body);
    });
  }

  Settings.addSection({id: "devices", label: "Network devices", render});
  return {render};
})();
