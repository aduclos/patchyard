# ansiblefleet

Keep a fleet of Linux machines patched with Ansible, from a small web dashboard.
Machines update themselves every day; **nothing reboots unless a person says so**.

- **Daily automatic updates** on every managed machine (Debian/Ubuntu via
  unattended-upgrades; Fedora/RHEL status via dnf-automatic), never auto-rebooting.
- **Dashboard**: what is up to date, what waits for a reboot, what failed, which
  containers stopped, with a live terminal for every job.
- **One-click actions**: patch, reboot (one machine at a time, waiting for its
  containers to come back), apply the update policy, Ubuntu release upgrades
  without a terminal, cancel or watch a running upgrade.
- **Enrollment from the browser**: give a host name and an admin password once; the
  tool creates its own key-only service account there and takes it from there.
- **Alerts** on new problems only (ntfy, webhook, e-mail), plus one daily reminder
  while something still waits on you.
- **Per-machine policy** in the inventory (no-reboot windows, manual-only machines,
  held-back packages), each with a reason the dashboard shows.
- Standard library Python, no build step, no database. MIT licensed.

## How it works

```
                  browser ── HTTPS ──> (reverse proxy, optional)
                                            │
  controller ┌──────────────────────────────▼───────────────────────────┐
             │ ansiblefleet-web   python -m fleet web   (app + API)     │
             │ ansiblefleet-check python -m fleet check (timer)         │
             │   └─ ansible-playbook  ── SSH (key, from= controller) ──────> managed machines
             │ state: inventory, host_vars, logs, data (/var/lib/...)   │      local service account,
             └──────────────────────────────────────────────────────────┘      passwordless sudo
```

- Everything runs as one unprivileged **service account** on the controller. On each
  managed machine the same-named local account has no password, accepts only the
  controller's key, and only from the controller's addresses (`authorized_keys from=`).
- People sign in with their own accounts (Kerberos: Active Directory, FreeIPA, MIT);
  membership in one configured group grants access.
- The **check** (every few minutes, read-only) writes the dashboard data and decides
  what to alert on. **Jobs** run the playbooks in `ansible/playbooks/`.

## Requirements

- Controller: Linux with systemd, Python 3.11+, `ssh`/`ssh-keygen`/`ssh-keyscan`,
  `kinit` (for Kerberos sign-in), and network access to the managed machines.
- Managed machines: SSH, sudo, Python 3; Debian/Ubuntu for full support.

## Install

```sh
git clone https://github.com/aduclos/ansiblefleet.git /srv/ansiblefleet
cd /srv/ansiblefleet
python3 -m venv /tmp/af && /tmp/af/bin/pip install ansible-core   # only to run the installer
sudo /tmp/af/bin/ansible-playbook install/controller.yml
```

The installer creates the service account and its key, the state directory, the
Ansible virtualenv, systemd units and `/etc/ansiblefleet/fleet.toml` (from
`config/fleet.example.toml`, never overwritten). Options are variables at the top of
`install/controller.yml`, e.g. `-e fleet_account=svc_fleet -e fleet_controller_reboot=true`.

Then:

1. Edit `/etc/ansiblefleet/fleet.toml`: at least `site.url`, `auth.realm` and
   `auth.group` (or `auth.method = "none"` for a trusted single-user setup).
2. Validate it: `sudo -u svc_fleet env FLEET_CONFIG=/etc/ansiblefleet/fleet.toml python3 -m fleet config`
3. `sudo systemctl enable --now ansiblefleet-web.service ansiblefleet-check.timer`
4. Open the dashboard and **Add machine**, or list machines under `[managed]` in
   `/var/lib/ansiblefleet/inventory.ini` and run `bootstrap/bootstrap.sh` on them yourself.

The web service binds to `127.0.0.1:8790` by default; put a reverse proxy with TLS in
front of it (any proxy works; the app serves its own files and enforces sign-in), or set
`web.bind` to a LAN address.

## Configuration

All settings live in one TOML file, documented in
[`config/fleet.example.toml`](config/fleet.example.toml). Unknown keys and wrong types
are errors at start-up. A few settings (sign-in group, check interval, reminder hour,
ntfy topic) can be changed from **Settings** in the web UI; those overrides are stored
in the state directory and take precedence over the file.

## Per-machine policy (inventory variables)

Put these in `host_vars/<host>.yml` or `group_vars/<group>.yml` in the state directory:

```yaml
fleet_reboot_blackout:            # no reboots during these UTC hours (overridable in the UI)
  hours_utc: [8, 9, 10, 11]
  reason: "nightly batch jobs"

fleet_manual_only:                # skipped by fleet-wide runs; patched only when picked by name
  reason: "database primary; patch only in a planned window"

fleet_hold_packages:              # never installed by automatic updates
  packages: ["postgresql-", "libpq"]
  reason: "major database upgrades are planned by hand"

fleet_track_containers: false     # do not alert when containers stop (default: true)

fleet_auto_update_extra_origins:  # extra unattended-upgrades origins, e.g. Docker CE
  - "origin=Docker,codename=${distro_codename}"

fleet_release_upgrade_reenable: ["docker"]   # third-party apt sources to re-enable after a release upgrade
```

Groups: machines in `inventory.managed_group` are managed; machines in
`inventory.report_only_group` are shown but never changed; `controller.host` (the
machine running the tool) is never patched by it and can only be rebooted from the UI
if `controller.reboot_command` is set.

## Security model

- The service account's key is the credential that grants root on managed machines.
  Protect the controller accordingly; the key is usable only from the controller's
  addresses.
- Admin passwords typed during enrollment are used once (via `SSH_ASKPASS` and
  `sudo -S`, from a 0600 file in a 0700 tmpfs directory) and never stored or logged.
- Sign-in failures are rate-limited (directories often have no lockout). Sessions are
  in memory, cookies are `HttpOnly; SameSite=Strict` (and `Secure` over HTTPS).
  State-changing requests must be JSON with an `X-Fleet` header and a matching Origin.
- Host keys are recorded on first contact and their fingerprints logged.

## Development

```sh
python3 -m venv .venv-dev && .venv-dev/bin/pip install -e '.[dev]'
python3 -m unittest discover -t . -s tests      # standard library only
.venv-dev/bin/ruff check fleet tests
```

`tests/test_no_site_data.py` fails on any non-documentation IP address or domain in
tracked files: examples use `192.0.2.0/24`, `2001:db8::/32` and `example.com`.

## License

MIT, see [LICENSE](LICENSE).
