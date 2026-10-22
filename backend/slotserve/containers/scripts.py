"""The shell scripts and files SlotServe copies into every dev environment container (SSH, accounts)."""

LIB_SCRIPT = r"""# SlotServe helpers
set_hash() {
  # $1 = account, $2 = SHA-512-crypt hash, "*" (no password) or "!" (locked)
  if command -v chpasswd >/dev/null 2>&1 && printf '%s:%s\n' "$1" "$2" | chpasswd -e >/dev/null 2>&1; then
    return 0
  fi
  if command -v usermod >/dev/null 2>&1 && usermod -p "$2" "$1" >/dev/null 2>&1; then
    return 0
  fi
  for f in /etc/shadow /etc/passwd; do
    if [ -f "$f" ] && grep -q "^$1:" "$f"; then
      awk -F: -v OFS=: -v u="$1" -v h="$2" '$1 == u { $2 = h } { print }' "$f" > "$f.slotserve" \
        && cat "$f.slotserve" > "$f"
      rm -f "$f.slotserve"
      return 0
    fi
  done
  echo "slotserve: could not set the password of $1" >&2
  return 1
}
"""

# The SSH server of every container, set up per container: "--setup" only prepares the logins (root's password,
# or the accounts of a shared environment), "--run" only runs Dropbear, no argument does both (docker exec).
SSH_SCRIPT = """#!/bin/sh
# Started by SlotServe: sets up the logins, then runs the SSH server (Dropbear).
S=/opt/slotserve
. $S/lib.sh
setup() {{
{setup}
}}
case "$1" in
  --setup) setup; exit $? ;;
  --run) ;;
  *) setup || exit 1 ;;
esac
exec $S/dropbearmulti dropbear -F -e {flags} -K 60 -T {tries} -p 22 \\
  -r $S/ssh_host_key {key_dir}-P /tmp/.slotserve-dropbear.pid
"""
ROOT_SETUP = """  if [ -f $S/root.hash ]; then
    set_hash root "$(cat $S/root.hash)" || true
    rm -f $S/root.hash
  fi
  return 0"""
SHARED_SETUP = """  if ! /bin/sh $S/accounts.sh; then
    echo "slotserve: could not create the user accounts, see above" >&2
    return 1
  fi"""

START_SCRIPT = """#!/bin/sh
# Started by SlotServe: an SSH server (dropbear) next to the environment's own main process.
/bin/sh /opt/slotserve/ssh.sh --setup || exit 1
/bin/sh /opt/slotserve/ssh.sh --run &
{cd}
if [ "$#" -gt 0 ]; then exec "$@"; fi
while :; do sleep 3600; done
"""

ACCOUNTS_SCRIPT = r"""#!/bin/sh
# Written by SlotServe: one account per SlotServe user (name:uid:hash lines in /opt/slotserve/accounts).
S=/opt/slotserve
. $S/lib.sh
if command -v useradd >/dev/null 2>&1; then
  ADD=useradd
elif command -v adduser >/dev/null 2>&1; then
  ADD=adduser
else
  echo "slotserve: this image has neither useradd nor adduser, so it cannot host a shared environment" >&2
  exit 2
fi
SH=/bin/sh
[ -x /bin/bash ] && SH=/bin/bash
mkdir -p /home
status=0
while IFS=: read -r name uid hash; do
  [ -n "$name" ] || continue
  if id "$name" >/dev/null 2>&1; then
    if [ "$(id -u "$name")" != "$uid" ]; then
      echo "slotserve: the image already has an account $name; skipped" >&2
      continue
    fi
  elif [ "$ADD" = useradd ]; then
    useradd -M -d "/home/$name" -s "$SH" -u "$uid" "$name" || { status=1; continue; }
  else
    adduser -D -H -h "/home/$name" -s "$SH" -u "$uid" "$name" || { status=1; continue; }
  fi
  h="/home/$name"
  mkdir -p "$h/.ssh"
  cat "$S/keys/$name" > "$h/.ssh/authorized_keys" 2>/dev/null || : > "$h/.ssh/authorized_keys"
  chown "$uid:$(id -g "$name")" "$h" "$h/.ssh" "$h/.ssh/authorized_keys"
  chmod 700 "$h" "$h/.ssh"
  chmod 600 "$h/.ssh/authorized_keys"
  set_hash "$name" "$hash" || status=1
done < $S/accounts
# People who are no longer listed (disabled) lose access.
if [ -f $S/accounts.done ]; then
  for name in $(cat $S/accounts.done); do
    if ! grep -q "^$name:" $S/accounts; then
      rm -f "/home/$name/.ssh/authorized_keys"
      set_hash "$name" '!' || true
    fi
  done
fi
cut -d: -f1 $S/accounts > $S/accounts.done
rm -rf $S/accounts $S/keys
exit $status
"""

PROFILE = """# Added by SlotServe
[ -d /workspace ] && [ "$PWD" = "$HOME" ] && cd /workspace
"""
