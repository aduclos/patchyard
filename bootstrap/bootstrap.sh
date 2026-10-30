#!/bin/sh
# Make this Linux machine manageable by a Patchyard controller. Run as root:
#   PATCHYARD_ACCOUNT=patchyard \
#   PATCHYARD_PUBKEY='ssh-ed25519 AAAA... patchyard@controller' \
#   PATCHYARD_ALLOWED_SOURCES='192.0.2.5,192.0.2.6' sh bootstrap.sh
# (Enrollment from the web UI runs it this way through the admin account's sudo.)
# Idempotent. Creates/updates exactly:
#   * a local system user $PATCHYARD_ACCOUNT: no password at all (key login only)
#   * its ~/.ssh/authorized_keys: the one Patchyard key, usable ONLY from PATCHYARD_ALLOWED_SOURCES
#   * /etc/sudoers.d/90-$PATCHYARD_ACCOUNT: passwordless sudo (validated with visudo first)
# Touches nothing that already exists except its own files above.
set -eu
: "${PATCHYARD_ACCOUNT:?PATCHYARD_ACCOUNT not set}"
: "${PATCHYARD_PUBKEY:?PATCHYARD_PUBKEY not set}"
: "${PATCHYARD_ALLOWED_SOURCES:?PATCHYARD_ALLOWED_SOURCES not set (comma-separated controller addresses)}"
U=$PATCHYARD_ACCOUNT
FROM=$PATCHYARD_ALLOWED_SOURCES
case "$U" in *[!a-z0-9_-]*|"") echo "PATCHYARD_ACCOUNT: lower-case letters, digits, _ and - only" >&2; exit 2 ;; esac
case "$FROM" in *[!0-9a-fA-F.:,/]*) echo "PATCHYARD_ALLOWED_SOURCES: addresses/CIDRs separated by commas" >&2; exit 2 ;; esac
case "$PATCHYARD_PUBKEY" in ssh-ed25519\ *) ;; *) echo "PATCHYARD_PUBKEY is not an ed25519 key" >&2; exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 2; }

home_of() {
  h=$(getent passwd "$1" 2>/dev/null | cut -d: -f6)
  [ -n "$h" ] || h=$(awk -F: -v u="$1" '$1==u{print $6}' /etc/passwd)
  [ -n "$h" ] || h=$(dscl . -read "/Users/$1" NFSHomeDirectory 2>/dev/null | awk '{print $2}')   # macOS
  echo "$h"
}
if [ "$(uname -s)" = Darwin ] && ! launchctl print system/com.openssh.sshd >/dev/null 2>&1; then
  echo "Remote Login is off: turn it on in System Settings > General > Sharing, then run this again" >&2; exit 1
fi
if id "$U" >/dev/null 2>&1; then
  echo "user $U exists"
elif [ "$(uname -s)" = Darwin ]; then                                # macOS: a hidden service user, uid 400-499
  uid=$(dscl . -list /Users UniqueID | awk '$2 >= 400 && $2 < 500 {print $2}' | sort -n | tail -1)
  uid=$(( ${uid:-399} + 1 ))
  dscl . -create "/Users/$U"
  dscl . -create "/Users/$U" UserShell /bin/sh
  dscl . -create "/Users/$U" RealName "Patchyard service account"
  dscl . -create "/Users/$U" UniqueID "$uid"
  dscl . -create "/Users/$U" PrimaryGroupID 20
  dscl . -create "/Users/$U" NFSHomeDirectory "/var/$U"
  dscl . -create "/Users/$U" IsHidden 1
  dscl . -create "/Users/$U" Password '*'
  install -d -m 0750 -o "$U" -g staff "/var/$U"
  echo "created user $U"
elif command -v useradd >/dev/null 2>&1; then                        # most Linux
  useradd --system --create-home --home-dir "/var/lib/$U" --shell /bin/sh --comment "Patchyard service account" "$U"
  echo "created user $U"
elif command -v pw >/dev/null 2>&1; then                             # FreeBSD
  pw useradd "$U" -d "/var/lib/$U" -m -s /bin/sh -c "Patchyard service account"
  echo "created user $U"
elif command -v adduser >/dev/null 2>&1; then                        # Alpine / BusyBox
  adduser -S -D -h "/var/lib/$U" -s /bin/sh -g "Patchyard service account" "$U"
  echo "created user $U"
else
  echo "no useradd, pw or adduser found" >&2; exit 1
fi
# No usable password; '*' (not '!', which some sshd builds treat as locked) keeps key login working.
if command -v usermod >/dev/null 2>&1; then usermod -p '*' "$U"
elif command -v pw >/dev/null 2>&1; then pw usermod "$U" -w no
elif [ -w /etc/shadow ]; then sed -i "s/^$U:[^:]*:/$U:*:/" /etc/shadow; fi
H=$(home_of "$U")
G=$(id -gn "$U")
install -d -m 0700 -o "$U" -g "$G" "$H/.ssh"
printf 'from="%s",no-agent-forwarding,no-port-forwarding,no-X11-forwarding %s\n' "$FROM" "$PATCHYARD_PUBKEY" > "$H/.ssh/authorized_keys.new"
chown "$U:$G" "$H/.ssh/authorized_keys.new"; chmod 0600 "$H/.ssh/authorized_keys.new"
mv "$H/.ssh/authorized_keys.new" "$H/.ssh/authorized_keys"
echo "installed the Patchyard key (from=$FROM)"
# macOS with Remote Login limited to some users: let the account in
if dscl . -read /Groups/com.apple.access_ssh >/dev/null 2>&1; then
  dseditgroup -o edit -a "$U" -t user com.apple.access_ssh && echo "allowed $U to use Remote Login"
fi

SUDOERS_D=/etc/sudoers.d
[ -d /usr/local/etc/sudoers.d ] && SUDOERS_D=/usr/local/etc/sudoers.d       # FreeBSD
[ -d "$SUDOERS_D" ] || { echo "sudo is not installed (no $SUDOERS_D); install sudo first" >&2; exit 1; }
T=$(mktemp); printf '# Patchyard service account: passwordless root for Ansible.\n%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$U" > "$T"
if command -v visudo >/dev/null 2>&1; then visudo -cf "$T" >/dev/null || { rm -f "$T"; echo "visudo rejected the rule" >&2; exit 1; }; fi
install -m 0440 -o root -g "$(id -gn root)" "$T" "$SUDOERS_D/90-$U"; rm -f "$T"
echo "sudo rule installed"

# sshd restrictions that would turn the account away (we do not edit sshd_config; report instead)
if command -v sshd >/dev/null 2>&1; then
  R=$(sshd -T 2>/dev/null | grep -iE '^(allowusers|allowgroups|denyusers|denygroups) ' || true)
  if [ -n "$R" ]; then
    echo "WARNING: sshd has access lists; make sure $U is allowed:"; echo "$R"
    echo "$R" | grep -qiE "^allow(users|groups) " && ! echo "$R" | grep -qiE "\b$U\b" && exit 3
  fi
fi
# Prove it the way Ansible will use it: as the account, sudo without a password
# (sudo is required anyway, so this needs no runuser/su; works with sudo-rs too).
if sudo -u "$U" sudo -n true </dev/null; then echo "OK: $U ready"; else echo "sudo rule not effective" >&2; exit 1; fi
