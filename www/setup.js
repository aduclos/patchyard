// Patchyard first-run setup wizard. Talks to /api/setup/* (patchyard/web.py, setup mode).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const A = {                                    // the answers, sent to /api/setup/finish
  site_name: "Patchyard", site_url: location.origin, domain: "",
  auth_method: "local", admin_user: "admin", admin_password: "", realm: "", group: "patchyard-admins",
  notify: "none", ntfy_url: "https://ntfy.sh", ntfy_topic: "", ntfy_token_file: "", webhook_url: "",
  smtp_host: "", smtp_port: "587", smtp_user: "", smtp_password_file: "", smtp_sender: "", smtp_to: "",
  bind: "0.0.0.0", port: location.port || "8790", controller_host: "",
};
let code = "", info = {}, step = 0;

async function post(path, body) {
  const r = await fetch("/api/setup/" + path, {method: "POST", headers: {"Content-Type": "application/json", "X-Patchyard": "1"},
                                               body: JSON.stringify({code, ...body})});
  let j = {}; try { j = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
const field = (name, label, attrs = "") =>
  `<label class="field"><span>${label}</span><input name="${name}" value="${esc(A[name])}" ${attrs} autocomplete="off" spellcheck="false"></label>`;
function collect() { for (const i of document.querySelectorAll("#card input[name]")) {
  if (i.type === "radio") { if (i.checked) A[i.name] = i.value; } else if (i.type !== "button") A[i.name] = i.value.trim(); } }
function msg(text, ok) { const m = $("#msg"); if (m) { m.className = ok ? "ok-msg" : "bad-msg"; m.textContent = text; } }

const STEPS = [
  { title: "Setup code", render: () => `
      <h2>Welcome</h2>
      <p class="lead">Enter the setup code. The installer printed it; it is also in
        <code>${esc((info.config_path || "").replace(/[^/]+$/, ""))}setup-token</code> on this machine.</p>
      <input class="field code-in" name="code" value="${esc(code)}" placeholder="xxxxxx-xxxxxx-xxxxxx" autofocus>
      <p id="msg"></p>`,
    next: async () => { code = $("[name=code]").value.trim().toLowerCase(); await post("code", {}); } },

  { title: "Your site", render: () => `
      <h2>Your site</h2>
      <p class="lead">How people reach this dashboard.</p>
      ${field("site_name", "Name shown on the dashboard")}
      ${field("site_url", "Dashboard address (as people type it, https:// behind a proxy)", 'inputmode="url"')}
      ${field("domain", "Your network's domain, optional (so “web1” means web1.corp.example.com)", 'placeholder="corp.example.com"')}
      <p id="msg"></p>` },

  { title: "Sign-in", render: () => `
      <h2>Who may sign in?</h2>
      <div class="choice">
        <label><input type="radio" name="auth_method" value="local" ${A.auth_method === "local" ? "checked" : ""}>
          <b>Patchyard accounts</b><small>Simplest. Works anywhere, no domain needed. You create the first admin now and add people later in Settings.</small></label>
        <label><input type="radio" name="auth_method" value="kerberos" ${A.auth_method === "kerberos" ? "checked" : ""}>
          <b>Domain accounts</b><small>Active Directory, FreeIPA or another Kerberos realm. This machine must be joined to it (realm/SSSD). Members of one group may sign in.</small></label>
        <label><input type="radio" name="auth_method" value="none" ${A.auth_method === "none" ? "checked" : ""}>
          <b>No sign-in</b><small>Only for a single-user lab where nobody else can reach this page.</small></label>
      </div>
      <div class="sub" id="sub-local">${field("admin_user", "First admin's user name")}
        <div class="grid2">${field("admin_password", "Password (10+ characters)", 'type="password"')}
          <label class="field"><span>Again</span><input name="admin_password2" type="password" autocomplete="off"></label></div></div>
      <div class="sub" id="sub-kerberos"><div class="grid2">${field("realm", "Realm", 'placeholder="CORP.EXAMPLE.COM"')}
          ${field("group", "Group allowed to sign in")}</div>
        <div class="grid2"><label class="field"><span>Test with a user</span><input name="t_user" autocomplete="off"></label>
          <label class="field"><span>Their password</span><input name="t_pw" type="password" autocomplete="off"></label></div>
        <p><button class="btn sm ghost" type="button" id="test-krb">Test sign-in</button></p></div>
      <p id="msg"></p>`,
    after: () => {
      const sync = () => { const m = document.querySelector("[name=auth_method]:checked").value;
        $("#sub-local").classList.toggle("on", m === "local"); $("#sub-kerberos").classList.toggle("on", m === "kerberos"); };
      document.querySelectorAll("[name=auth_method]").forEach(r => r.addEventListener("change", sync)); sync();
      $("#test-krb").onclick = async () => { collect();
        try { const j = await post("test-kerberos", {answers: A, username: $("[name=t_user]").value, password: $("[name=t_pw]").value});
              msg(`Signed in as ${j.user}; the group check passed.`, true); }
        catch (e) { msg(e.message); } };
    },
    check: () => {
      if (A.auth_method === "local") {
        if (!A.admin_user) return "Choose the first admin's user name.";
        if (A.admin_password.length < 10) return "Use at least 10 characters for the password.";
        if (A.admin_password !== $("[name=admin_password2]").value) return "The two passwords differ.";
      }
      if (A.auth_method === "kerberos" && (!A.realm || !A.group)) return "Enter the realm and the group.";
      return "";
    } },

  { title: "Alerts", render: () => `
      <h2>Where should alerts go?</h2>
      <p class="lead">Only new problems and waiting reboots, plus one daily reminder. Never noise.</p>
      <div class="choice">
        ${[["none", "No alerts", "Just the dashboard."], ["ntfy", "ntfy push", "Phone/desktop push via ntfy.sh or your own ntfy server."],
           ["webhook", "Webhook", "POSTs JSON to a URL (Slack/Discord/Teams bridges, home automation, ...)."],
           ["smtp", "E-mail", "Through any SMTP server."]].map(([v, b, s]) =>
          `<label><input type="radio" name="notify" value="${v}" ${A.notify === v ? "checked" : ""}><b>${b}</b><small>${s}</small></label>`).join("")}
      </div>
      <div class="sub" id="sub-ntfy"><div class="grid2">${field("ntfy_url", "ntfy server")}${field("ntfy_topic", "Topic", 'placeholder="something-hard-to-guess"')}</div>
        ${field("ntfy_token_file", "Access token file, optional (path on this machine)")}</div>
      <div class="sub" id="sub-webhook">${field("webhook_url", "Webhook URL")}</div>
      <div class="sub" id="sub-smtp"><div class="grid2">${field("smtp_host", "SMTP server")}${field("smtp_port", "Port")}</div>
        <div class="grid2">${field("smtp_user", "User, optional")}${field("smtp_password_file", "Password file, optional (path)")}</div>
        <div class="grid2">${field("smtp_sender", "From")}${field("smtp_to", "To (comma-separated)")}</div></div>
      <p><button class="btn sm ghost" type="button" id="test-notify">Send a test</button></p>
      <p id="msg"></p>`,
    after: () => {
      const sync = () => { const m = document.querySelector("[name=notify]:checked").value;
        ["ntfy", "webhook", "smtp"].forEach(k => $("#sub-" + k).classList.toggle("on", m === k));
        $("#test-notify").hidden = m === "none"; };
      document.querySelectorAll("[name=notify]").forEach(r => r.addEventListener("change", sync)); sync();
      $("#test-notify").onclick = async () => { collect();
        try { await post("test-notify", {answers: A}); msg("Sent. Check that it arrived.", true); }
        catch (e) { msg(e.message); } };
    } },

  { title: "Network", render: () => `
      <h2>Where should Patchyard listen?</h2>
      <div class="choice">
        <label><input type="radio" name="bind" value="0.0.0.0" ${A.bind === "0.0.0.0" ? "checked" : ""}>
          <b>On the network</b><small>Reachable from other computers on port ${esc(A.port)} (plain HTTP; fine on a trusted LAN).</small></label>
        <label><input type="radio" name="bind" value="127.0.0.1" ${A.bind === "127.0.0.1" ? "checked" : ""}>
          <b>Only this machine</b><small>Put a reverse proxy with HTTPS (Caddy, nginx, Traefik) in front. Recommended for anything exposed.</small></label>
      </div>
      ${field("port", "Port")}
      ${field("controller_host", "This machine's name in your inventory, optional (it is never rebooted in the middle of a job)",
              `placeholder="${esc(info.hostname || "")}"`)}
      <p class="note">Patchyard's key will only be accepted from this machine's addresses: ${esc((info.addresses || []).join(", ") || "detected when you add machines")}.</p>
      <p id="msg"></p>` },

  { title: "Finish", render: () => `
      <h2>Ready</h2>
      <dl class="review">
        <dt>Name</dt><dd>${esc(A.site_name)}</dd><dt>Address</dt><dd>${esc(A.site_url)}</dd>
        <dt>Domain</dt><dd>${esc(A.domain || "–")}</dd>
        <dt>Sign-in</dt><dd>${{local: "Patchyard accounts (admin: " + esc(A.admin_user) + ")", kerberos: "Domain " + esc(A.realm) + ", group " + esc(A.group), none: "none"}[A.auth_method]}</dd>
        <dt>Alerts</dt><dd>${{none: "none", ntfy: "ntfy " + esc(A.ntfy_topic), webhook: "webhook", smtp: "e-mail to " + esc(A.smtp_to)}[A.notify]}</dd>
        <dt>Listens on</dt><dd>${esc(A.bind)}:${esc(A.port)}</dd>
      </dl>
      <p class="note">Everything can be changed later in the configuration file; a few settings also in Settings.</p>
      <p id="msg"></p>`,
    next: async () => {
      const j = await post("finish", {answers: A});
      $("#card").innerHTML = `<h2>Done</h2>
        <p class="lead">Configuration saved to <code>${esc(j.config)}</code>.</p>
        ${j.restart_needed ? `<p class="bad-msg">Restart the service to listen on ${esc(j.listen)}: <code>sudo systemctl restart patchyard-web</code></p>` : ""}
        <p>Next: sign in and press <b>Add machine</b>. That's the last time you think about patching it.</p>
        <div class="nav"><span></span><a class="btn" href="/login">Go to the dashboard</a></div>`;
      $("#steps").innerHTML = STEPS.map(() => "<span class=done></span>").join("");
      return "done";
    } },
];

function show() {
  const s = STEPS[step];
  $("#steps").innerHTML = STEPS.map((_, i) => `<span class="${i < step ? "done" : i === step ? "now" : ""}"></span>`).join("");
  $("#card").innerHTML = s.render() + `<div class="nav">
      ${step ? '<button class="btn ghost" type="button" id="back">Back</button>' : "<span></span>"}
      <button class="btn" type="submit" id="next">${step === STEPS.length - 1 ? "Finish setup" : "Next"}</button></div>`;
  if (s.after) s.after();
  if ($("#back")) $("#back").onclick = () => { collect(); step--; show(); };
  $("#card").onsubmit = async e => {
    e.preventDefault(); collect();
    const problem = s.check ? s.check() : "";
    if (problem) return msg(problem);
    $("#next").disabled = true;
    try { const r = s.next ? await s.next() : null; if (r !== "done") { step++; show(); } }
    catch (err) { msg(err.message); $("#next").disabled = false; }
  };
  const first = $("#card input:not([type=radio])"); if (first) first.focus();
}

fetch("/api/setup/info").then(r => r.json()).then(j => { info = j; A.controller_host = ""; show(); }).catch(() => show());
