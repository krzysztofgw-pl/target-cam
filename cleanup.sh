#!/usr/bin/env bash
# Delete photos older than DAYS, but always keep the KEEP newest ones.
# Env: TARGET_DIR (default ~/target), KEEP (default 100), DAYS (default 14), DRY_RUN=1 to only print.
set -euo pipefail

DIR="${TARGET_DIR:-$HOME/target}"
KEEP="${KEEP:-100}"
DAYS="${DAYS:-14}"

# The Pi has no RTC: do nothing until the clock is synced, otherwise the cutoff would be wrong.
if command -v timedatectl >/dev/null 2>&1; then
  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" != "yes" ]]; then
    echo "clock not synced, skipping"
    exit 0
  fi
fi

cutoff=$(date -d "-$DAYS days" +%s)

# newest first; skip the first KEEP files; delete the rest if older than the cutoff
find "$DIR" -maxdepth 1 -type f -name '2*_*.jpg' -printf '%T@ %f\n' \
  | sort -rn | tail -n +"$((KEEP + 1))" \
  | while read -r ts name; do
      if (( ${ts%.*} < cutoff )); then
        if [[ -n "${DRY_RUN:-}" ]]; then
          echo "would delete $name"
        else
          rm -f -- "$DIR/$name" "$DIR/prev/$name" "$DIR/thumb/$name"
        fi
      fi
    done
