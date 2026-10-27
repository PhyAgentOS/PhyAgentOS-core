#!/usr/bin/env bash
set -euo pipefail
paos_trial_display_dir="/tmp/paos-piper-x-xvfb"
if [ -f "$paos_trial_display_dir/pid" ]; then
  paos_trial_display_pid="$(cat "$paos_trial_display_dir/pid")"
  if [ -r "/proc/$paos_trial_display_pid/cmdline" ] && grep -zq 'Xvfb' "/proc/$paos_trial_display_pid/cmdline" && grep -zq ':92' "/proc/$paos_trial_display_pid/cmdline"; then
    kill "$paos_trial_display_pid"
  fi
  rm -f "$paos_trial_display_dir/pid"
fi
