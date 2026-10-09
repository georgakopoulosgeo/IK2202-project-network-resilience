#!/bin/bash
# Stamp every kernel route change with epoch ns (same clock as the packets).
exec > "$1"
# $EPOCHREALTIME is read without forking, so the stamp lags the event by
# microseconds instead of the ~1-3 ms a 'date' call costs.
ip monitor route | while IFS= read -r line; do
    t=${EPOCHREALTIME/[.,]/}
    if [[ -n $t ]]; then printf '%s000 %s\n' "$t" "$line"
    else printf '%s %s\n' "$(date +%s%N)" "$line"; fi
done
