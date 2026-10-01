#!/bin/bash
# Every 100 ms: epoch_us + tx byte counters of r1 eth1 (primary) and eth2 (backup).
# Same clock as the packet timestamps -> exact throughput per link around the failure.
exec > "$1"
echo "t_us eth1_tx_bytes eth2_tx_bytes"
while :; do
    t=${EPOCHREALTIME/[.,]/}
    [[ -z $t ]] && t=$(( $(date +%s%N) / 1000 ))
    read -r a < /sys/class/net/eth1/statistics/tx_bytes
    read -r b < /sys/class/net/eth2/statistics/tx_bytes
    printf '%s %s %s\n' "$t" "$a" "$b"
    sleep 0.1
done
