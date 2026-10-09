#!/bin/bash
# Failover experiment for ONE configuration:
#   c1 = native OSPF,  c2 = OSPF + BFD,  c3 = OSPF + BFD + strict-priority QoS
# Deploy the lab by hand first (cd v2i-lab-c2 && kathara lstart), then from this folder:
#   ./run_failover.sh c2 5            # configuration, iterations (default 5)
# Output: results/C2_<date>/iter_N/ (raw data), aggregate/ (numbers), plots/ (figures)

CONF=${1,,}                         # c1, c2 or c3 (C2 also works)
N=${2:-5}
BASE=$(cd "$(dirname "$0")" && pwd)
LAB=$BASE/v2i-lab-$CONF
if [ -z "$CONF" ] || [ ! -d "$LAB" ]; then
    echo "usage: $0 c1|c2|c3 [iterations]"; exit 1
fi

WARMUP=20                           # s of normal traffic before the failure
AFTER=80                            # s observed after the failure (C1 needs ~40 s to converge)
DUR=$((WARMUP + AFTER))
RUN=${CONF^^}_$(date +%Y%m%d_%H%M%S)
OUT=$BASE/results/$RUN              # final place of the data
STAGE=$LAB/shared/current           # the same folder is /shared/current inside the devices

# kathara commands only work inside the folder of the deployed lab
cd "$LAB" || exit 1

# Run a shell command inside a device. The whole command goes to kathara as ONE string
# (kathara splits it itself): the compiled kathara binary refuses a separate '-c' argument.
# ${2//\'/...} escapes single quotes so they survive inside the '...' wrapper.
kx() { kathara exec "$1" "bash -c '${2//\'/\'\\\'\'}'"; }

restore_primary() {
    kx r1 "tc qdisc del dev eth1 ingress; tc filter del dev eth1 parent 1: prio 1" >/dev/null 2>&1
}

# Packets dropped so far by tc on r1, summed over the qdiscs given as arguments.
# Only root/ingress qdiscs: a root qdisc already counts the drops of its children.
r1_drops() {
    for q in "$@"; do kx r1 "tc -s qdisc show dev $q"; done |
        grep -o "dropped [0-9]*" | awk '{s += $2} END {print s + 0}'
}

# On Ctrl-C: stop local jobs and don't leave the primary link broken.
trap 'kill $(jobs -p) 2>/dev/null; restore_primary' EXIT

mkdir -p "$OUT"
echo "configuration=$CONF iterations=$N warmup=${WARMUP}s after=${AFTER}s" > "$OUT/run_info.txt"

# Background-traffic server, restarted in JSON mode with 0.5 s intervals, so that
# iperf3 -J --get-server-output also returns what the TMS received every 0.5 s.
kx tms "pkill -x iperf3; sleep 1; iperf3 -s -D -J -i 0.5" >/dev/null 2>&1

for i in $(seq 1 "$N"); do
    echo "=== $RUN: iteration $i/$N"

    # 1. clean state: primary link up and used by OSPF (and BFD up, except in C1)
    restore_primary
    echo "waiting for OSPF to use the primary link..."
    until kx r1 "ip route get 10.0.3.2" | grep -q "dev eth1"; do sleep 2; done
    if [ "$CONF" != c1 ]; then
        until kx r1 "vtysh -c 'show bfd peers brief'" | grep 10.0.1.2 | grep -q up; do sleep 1; done
    fi
    sleep 5
    kx tms "rm -rf /shared/current"         # leftovers of an aborted iteration (owned by root)
    mkdir -p "$STAGE"
    p0=$(r1_drops "eth1 root")              # drop counters before the iteration
    b0=$(r1_drops "eth2 root")

    # 2. traffic RSU -> TMS: receiver first, then background TCP and V2I telemetry.
    #    --fq-rate paces TCP per packet (iperf3 -b sends 128 KB bursts that fill the queue).
    #    t_iperf = iperf3 start time, to align its 0.5 s intervals with the failure.
    #    Terminal output of sender/receiver is saved too (the sender prints how many it sent).
    kx tms "python3 /root/v2i_receiver.py /shared/current/v2i.csv $((DUR + 5))" > "$STAGE/receiver.log" &
    sleep 2
    kx rsu "date +%s%N > /shared/current/t_iperf
            timeout $((DUR + 30)) iperf3 -c 10.0.3.2 -P 4 --fq-rate 17.5M -t $DUR -i 0.5 -J --get-server-output > /shared/current/iperf3.json" &
    kx rsu "python3 /root/v2i_sender.py $DUR" > "$STAGE/sender.log" &
    sleep "$WARMUP"

    # 3. silent failure: drop everything on r1-eth1 while the link stays UP, so OSPF
    #    only notices through its dead timer or BFD ('ip link set down' is detected instantly).
    #    t_fail is written after the egress drop is active, on the same clock as the packets.
    kx r1 "tc filter add dev eth1 parent 1: protocol all prio 1 matchall action drop
           date +%s%N > /shared/current/t_fail
           tc qdisc add dev eth1 handle ffff: ingress
           tc filter add dev eth1 parent ffff: protocol all prio 1 matchall action drop"
    echo "primary link failed, observing ${AFTER}s"
    wait                                    # receiver, iperf3 and sender finish

    # 4. packets dropped by tc on r1 in this iteration: "<primary link> <backup queue>"
    p1=$(r1_drops "eth1 root" "eth1 ingress")
    b1=$(r1_drops "eth2 root")
    echo "$((p1 - p0)) $((b1 - b0))" > "$STAGE/r1_drops"
    restore_primary

    # 5. files written inside the devices belong to root: give them to us, move to results/
    kx tms "chown -R $(id -u):$(id -g) /shared/current"
    mv "$STAGE" "$OUT/iter_$i"
done

trap - EXIT
cd "$BASE" || exit 1
python3 aggregate.py "$OUT"                 # numbers -> results/<run>/aggregate/
python3 analyze.py "$OUT"                   # figures -> results/<run>/plots/