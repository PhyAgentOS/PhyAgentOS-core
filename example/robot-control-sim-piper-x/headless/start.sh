#!/usr/bin/env bash
set -euo pipefail
command -v Xvfb >/dev/null || { echo "Xvfb is required for this server profile" >&2; exit 1; }
paos_trial_display_dir="/tmp/paos-piper-x-xvfb"
mkdir -p "$paos_trial_display_dir"
chmod 700 "$paos_trial_display_dir"
if [ -f "$paos_trial_display_dir/pid" ]; then
  paos_trial_display_pid="$(cat "$paos_trial_display_dir/pid")"
  if kill -0 "$paos_trial_display_pid" 2>/dev/null && grep -zq 'Xvfb' "/proc/$paos_trial_display_pid/cmdline" && grep -zq ':92' "/proc/$paos_trial_display_pid/cmdline"; then
    exit 0
  fi
fi
if [ -S /tmp/.X11-unix/X92 ]; then
  echo "Display :92 is occupied by another X server" >&2
  exit 1
fi
nohup Xvfb :92 -screen 0 1280x800x24 -nolisten tcp >"$paos_trial_display_dir/xvfb.log" 2>&1 </dev/null &
echo "$!" > "$paos_trial_display_dir/pid"
for paos_trial_attempt in $(seq 1 30); do
  [ ! -S /tmp/.X11-unix/X92 ] || exit 0
  sleep 0.1
done
echo "Dedicated Xvfb did not become ready" >&2
exit 1
