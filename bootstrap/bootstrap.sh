#!/bin/sh
# Make this Linux machine manageable by an ansiblefleet controller. Run as root:
#   FLEET_ACCOUNT=svc_patch \
#   FLEET_PUBKEY='ssh-ed25519 AAAA... svc_patch@controller' \
#   FLEET_ALLOWED_SOURCES='192.0.2.5,192.0.2.6' sh bootstrap.sh
# (Enrollment from the web UI runs it this way through the admin account's sudo.)
# Idempotent. Creates/updates exactly:
#   * a local system user $FLEET_ACCOUNT: no password at all (key login only)
#   * its ~/.ssh/authorized_keys: the one fleet key, usable ONLY from FLEET_ALLOWED_SOURCES
#   * /etc/sudoers.d/90-$FLEET_ACCOUNT: passwordless sudo (validated with visudo first)
# Touches nothing that already exists except its own files above.
set -eu
: "${FLEET_ACCOUNT:?FLEET_ACCOUNT not set}"
: "${FLEET_PUBKEY:?FLEET_PUBKEY not set}"
: "${FLEET_ALLOWED_SOURCES:?FLEET_ALLOWED_SOURCES not set (comma-separated controller addresses)}"
U=$FLEET_ACCOUNT
FROM=$FLEET_ALLOWED_SOURCES
case "$U" in *[!a-z0-9_-]*|"") echo "FLEET_ACCOUNT: lower-case letters, digits, _ and - only" >&2; exit 2 ;; esac
case "$FROM" in *[!0-9a-fA-F.:,/]*) echo "FLEET_ALLOWED_SOURCES: addresses/CIDRs separated by commas" >&2; exit 2 ;; esac
case "$FLEET_PUBKEY" in ssh-ed25519\ *) ;; *) echo "FLEET_PUBKEY is not an ed25519 key" >&2; exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 2; }

if id "$U" >/dev/null 2>&1; then
  echo "user $U exists"
else
  useradd --system --create-home --home-dir /var/lib/$U --shell /bin/bash \
          --comment "ansiblefleet service account" "$U"
  echo "created user $U"
fi
usermod -p '*' "$U"                          # no usable password; '*' (not '!') so sshd still allows key login
H=$(getent passwd "$U" | cut -d: -f6)
install -d -m 0700 -o "$U" -g "$(id -gn "$U")" "$H/.ssh"
printf 'from="%s",no-agent-forwarding,no-port-forwarding,no-X11-forwarding %s\n' "$FROM" "$FLEET_PUBKEY" > "$H/.ssh/authorized_keys.new"
chown "$U:$(id -gn "$U")" "$H/.ssh/authorized_keys.new"; chmod 0600 "$H/.ssh/authorized_keys.new"
mv "$H/.ssh/authorized_keys.new" "$H/.ssh/authorized_keys"
echo "installed the fleet key (from=$FROM)"

T=$(mktemp); printf '# ansiblefleet service account: passwordless root for Ansible.\n%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$U" > "$T"
if command -v visudo >/dev/null 2>&1; then visudo -cf "$T" >/dev/null || { rm -f "$T"; echo "visudo rejected the rule" >&2; exit 1; }; fi
install -m 0440 -o root -g root "$T" "/etc/sudoers.d/90-$U"; rm -f "$T"
echo "sudo rule installed"

# sshd restrictions that would turn svc_patch away (we do not edit sshd_config; report instead)
if command -v sshd >/dev/null 2>&1; then
  R=$(sshd -T 2>/dev/null | grep -iE '^(allowusers|allowgroups|denyusers|denygroups) ' || true)
  if [ -n "$R" ]; then
    echo "WARNING: sshd has access lists; make sure $U is allowed:"; echo "$R"
    echo "$R" | grep -qiE "^allow(users|groups) " && ! echo "$R" | grep -qiE "\b$U\b" && exit 3
  fi
fi
# Prove it the way Ansible will use it (works with classic sudo and sudo-rs alike).
runuser -u "$U" -- sudo -n true && echo "OK: $U ready" || { echo "sudo rule not effective" >&2; exit 1; }
