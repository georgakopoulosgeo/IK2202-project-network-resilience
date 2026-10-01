#!/bin/bash
# C1 baseline: native OSPF (no BFD), no QoS, silent failure of the primary link.
# Put this script in ~/v2i-lab and run it while the lab is up:  ./run_baseline.sh [ITERATIONS]
# Needs shared/v2i_sender.py and shared/v2i_receiver.py (mounted as /shared in the devices).

cd "$(dirname "$0")" || exit 1
N=${1:-2}
WARMUP=20                 # s of normal traffic before the failure
AFTER=70                  # s after the failure (C1 needs up to ~40 s to converge)
DUR=$((WARMUP + AFTER))
RUN=results/C2_$(date +%Y%m%d_%H%M%S)      # inside shared/

# Run a shell command inside a device. The whole command goes to kathara as ONE string
# (kathara splits it itself): the compiled kathara binary refuses a separate '-c' argument.
# ${2//\'/...} escapes single quotes so they survive inside the '...' wrapper.
kx() { kathara exec "$1" "bash -c '${2//\'/\'\\\'\'}'"; }

restore_primary() {
    kx r1 "tc qdisc del dev eth1 ingress; tc filter del dev eth1 parent 1: prio 1" >/dev/null 2>&1
}

# Packets dropped so far on r1: primary link (both directions) + backup queue.
r1_drops() {
    kx r1 'for q in "eth1 root" "eth1 ingress" "eth2 root"; do tc -s qdisc show dev $q; done |
           grep -o "dropped [0-9]*" | awk "{s += \$2} END {print s + 0}"' | tr -d '\r'
}

# On Ctrl-C: stop local jobs and don't leave the primary link broken.
trap 'kill $(jobs -p) 2>/dev/null; restore_primary' EXIT

mkdir -p "shared/$RUN"
kx tms "iperf3 -s -D" >/dev/null 2>&1       # background-traffic server (harmless if already running)

for i in $(seq 1 "$N"); do
    D="$RUN/iter_$i"
    mkdir -p "shared/$D"
    echo "=== iteration $i/$N"

    # 1. clean state: primary link up and used by OSPF
    restore_primary
    until kx r1 "ip route get 10.0.3.2" | grep -q "dev eth1"; do sleep 2; done
    until kx r1 "vtysh -c 'show bfd peers brief'" | grep 10.0.1.2 | grep -q up; do sleep 1; done
    sleep 5
    drops_before=$(r1_drops)
    #read -r a1 b1 c1 d1 <<< "$(counters)"   

    # 2. traffic RSU -> TMS: receiver first, then background TCP and V2I telemetry.
    #    --fq-rate paces TCP per packet (iperf3 -b sends 128 KB bursts that fill the queue).
    kx tms "python3 /root/v2i_receiver.py /shared/$D/v2i.csv $((DUR + 5))" &
    sleep 2
    kx rsu "timeout $((DUR + 30)) iperf3 -c 10.0.3.2 -P 4 --fq-rate 17.5M -t $DUR > /shared/$D/iperf3.txt" &
    kx rsu "python3 /root/v2i_sender.py $DUR" &
    sleep "$WARMUP"

    # 3. silent failure: drop everything on r1-eth1 while the link stays UP, so OSPF
    #    only notices through its dead timer ('ip link set down' is detected instantly).
    #    t_fail is written after the egress drop is active, on the same clock as the packets.
    kx r1 "tc filter add dev eth1 parent 1: protocol all prio 1 matchall action drop
           date +%s%N > /shared/$D/t_fail
           tc qdisc add dev eth1 handle ffff: ingress
           tc filter add dev eth1 parent ffff: protocol all prio 1 matchall action drop"
    echo "primary link failed, observing ${AFTER}s"
    wait                                         # receiver, iperf3 and sender finish

    # 4. total packets dropped on r1 during this iteration (cumulative packet loss)
    #read -r a2 b2 c2 d2 <<< "$(counters)"
    echo $(( $(r1_drops) - drops_before )) > "shared/$D/r1_drops"
    # lost = (RSU sent - TMS received) + (TMS sent - RSU received), all flows
    #echo $(( (a2 - a1) - (d2 - d1) + (c2 - c1) - (b2 - b1) )) > "shared/$D/lost_all"
    restore_primary
done

trap - EXIT
python3 analyze.py "shared/$RUN"