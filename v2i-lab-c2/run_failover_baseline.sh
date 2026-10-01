#!/usr/bin/env bash
# =============================================================================
# run_failover_baseline.sh - C1 baseline
#   native OSPFv2 (default timers, NO BFD), no QoS,
#   silent failure of the primary link r1:eth1 <-> r2:eth0.
#
# Run on the Kathara host while the lab is running:
#     ITERATIONS=1 ./run_failover_baseline.sh    # dry run first
#     ./run_failover_baseline.sh                 # full run (30 iterations)
#
# Needs v2i_sender.py and v2i_receiver.py in $LAB_DIR/shared/, which Kathara
# mounts as /shared in every device.
# Results: $LAB_DIR/shared/results/C1_<timestamp>/iter_NN/ + summary.csv, stats.csv
#
# Per iteration:
#   receiver (tms) -> telemetry 500 pps + iperf3 4x17.5 Mbit/s (rsu -> tms)
#   -> random 15-25 s warm-up -> silent failure on r1 eth1 (t_fail logged in r1)
#   -> observe 80 s -> snapshot counters -> restore -> wait for OSPF -> analyze
# =============================================================================
set -uo pipefail

# ------------------------------------------------------------------ config ---
LAB_DIR="${LAB_DIR:-$HOME/v2i-lab}"
CONDITION="C1"
ITERATIONS="${ITERATIONS:-10}"

WARMUP_MIN=15          # s before the failure; randomised so the failure does not
WARMUP_MAX=25          #   lock onto the phase of the 10 s OSPF hellos
POST_FAIL=80           # s observed after the failure: dead interval (<=40 s)
                       #   + 30 s post-reroute window + margin
SETTLE=10              # s of quiet after the primary is back
CONVERGE_TIMEOUT=120   # s max wait for OSPF to return to the primary

RSU_IP=10.0.0.2
TMS_IP=10.0.3.2

V2I_PORT=5005
V2I_RATE=500           # pkt/s  = 50 vehicles x 10 Hz CAM
V2I_SIZE=300           # B UDP payload
V2I_DSCP=46            # EF: ignored in C1/C2, used for classification in C3

BG_STREAMS=4
BG_RATE=17.5M          # per stream -> 70 Mbit/s total: fits the 100M primary,
                       #   7x oversubscribes the 10M backup
# Kernel (per-packet) pacing. iperf3's '-b' paces per 128 KB write, so 4 streams
# can dump ~512 KB at once = ~40 ms of queue on the 100M primary even though
# the average load is only 70%.
BG_PACING="--fq-rate"  # set to "-b" to go back to iperf3's application pacing
IPERF_PORT=5201

CAPTURE_PCAP="${CAPTURE_PCAP:-1}"   # 1 = telemetry pcap on tms + OSPF/BFD pcap on r1

PRE_WINDOW=10          # s before the failure used for steady-state metrics
POST_WINDOW=30         # s after the reroute used for backup-link metrics

# ------------------------------------------------------------------- paths ---
RUN_NAME="${CONDITION}_$(date +%Y%m%d_%H%M%S)"
H_RUN="$LAB_DIR/shared/results/$RUN_NAME"   # path on the host
C_RUN="/shared/results/$RUN_NAME"           # same directory inside the devices
H_BIN="$LAB_DIR/shared/.bin"
C_BIN="/shared/.bin"

# ----------------------------------------------------------------- helpers ---
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# How 'kathara exec' receives the command differs between versions; preflight
# picks the first style that works:
#   sep    kathara exec -d DIR DEV -- bash -c "CMD"
#   plain  kathara exec -d DIR DEV bash -c "CMD"
#   string kathara exec -d DIR DEV "bash -c 'CMD'"   (Kathara splits it with shlex)
KX_MODE=sep
_squote() { local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }
# kx <device> <shell command>: run a command inside a device through bash -c
kx() {
    local dev=$1; shift
    local cmd="$*"
    case $KX_MODE in
        sep)    kathara exec -d "$LAB_DIR" "$dev" -- bash -c "$cmd" ;;
        plain)  kathara exec -d "$LAB_DIR" "$dev" bash -c "$cmd" ;;
        string) kathara exec -d "$LAB_DIR" "$dev" "bash -c $(_squote "$cmd")" ;;
    esac 2>&1 | tr -d '\r'
}

need() {   # need <device> <tool>...
    local dev=$1 t; shift
    for t in "$@"; do
        kx "$dev" "command -v $t >/dev/null && echo HAVE" | grep -qx HAVE \
            || die "'$t' is missing in $dev"
    done
}

# True when both directions use the primary and r1 has both adjacencies Full.
primary_ok() {
    local o1 o2
    o1=$(kx r1 "ip route get $TMS_IP; vtysh -c 'show ip ospf neighbor'")
    o2=$(kx r2 "ip route get $RSU_IP")
    [[ $o1 == *"dev eth1"* && $o2 == *"dev eth0"* ]] && (( $(grep -c Full <<<"$o1") >= 2 ))
}

wait_primary() {
    local t0=$SECONDS
    until primary_ok; do
        (( SECONDS - t0 > CONVERGE_TIMEOUT )) && return 1
        sleep 2
    done
}

# Small scripts executed inside r1 (kept in shared/.bin, regenerated every run).
write_helpers() {
    mkdir -p "$H_BIN"
    cat > "$H_BIN/inject_failure.sh" <<'EOF'
#!/bin/bash
# Silent failure of the primary link (runs inside r1). Every frame on eth1 is
# dropped in both directions while the interface stays UP, so OSPF only notices
# when hellos stop arriving - like a failed microwave hop or intermediate switch.
# ('ip link set eth1 down' would be detected instantly by r1 via carrier loss,
#  making C1 look like C2.)
out=$1
t0=$(date +%s%N)
tc filter add dev eth1 parent 1: protocol all prio 1 matchall action drop || exit 1
t1=$(date +%s%N)          # from here on nothing leaves r1 through eth1
tc qdisc add dev eth1 handle ffff: ingress
tc filter add dev eth1 parent ffff: protocol all prio 1 matchall action drop || exit 1
printf '{"t_fail_start_ns": %s, "t_fail_end_ns": %s, "method": "tc matchall drop on r1 eth1 egress+ingress, link stays up"}\n' "$t0" "$t1" > "$out"
echo FAIL_INJECTED
EOF
    cat > "$H_BIN/restore_primary.sh" <<'EOF'
#!/bin/bash
tc filter del dev eth1 parent 1: prio 1 2>/dev/null
tc qdisc del dev eth1 ingress 2>/dev/null
tc filter show dev eth1 parent 1: | grep -q matchall && exit 1
echo RESTORED
EOF
    cat > "$H_BIN/route_monitor.sh" <<'EOF'
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
EOF
    cat > "$H_BIN/link_sampler.sh" <<'EOF'
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
EOF
}

preflight() {
    local out
    command -v kathara >/dev/null || die "kathara not found on this host"
    [[ $ITERATIONS =~ ^[1-9][0-9]*$ ]] || die "ITERATIONS must be a positive integer"
    for f in v2i_sender.py v2i_receiver.py; do
        [[ -f "$LAB_DIR/shared/$f" ]] || die "put $f in $LAB_DIR/shared/"
    done

    # find a 'kathara exec' calling style that works. The expected output KX_42
    # only appears if bash really ran (an error message would echo 'KX_$((6*7))').
    local mode first=""
    for mode in sep plain string; do
        KX_MODE=$mode
        out=$(kx r1 'echo KX_$((6*7))')
        grep -qx KX_42 <<<"$out" && break
        [[ -z $first ]] && first=$out
        KX_MODE=""
    done
    if [[ -z $KX_MODE ]]; then
        log "'kathara exec -d $LAB_DIR r1 -- bash -c ...' returned:"
        printf '%s\n' "${first:-<no output>}" | sed 's/^/    | /'
        log "hints: if the lab was started with sudo, run this script with sudo too:"
        log "         sudo LAB_DIR=$LAB_DIR $0"
        log "       'kathara list' must show r1 when run as the same user as this script,"
        log "       and LAB_DIR must be the folder the lab was started from."
        die "cannot run commands in r1 via 'kathara exec'"
    fi
    log "kathara exec style: $KX_MODE"

    # /shared must be the same directory everywhere
    local probe=".probe_$$" dev
    touch "$LAB_DIR/shared/$probe"
    for dev in r1 r2 rsu tms; do
        if ! kx "$dev" "test -f /shared/$probe && echo SHARED_OK" | grep -qx SHARED_OK; then
            rm -f "$LAB_DIR/shared/$probe"
            die "/shared is not mounted in $dev (enable 'shared_mount' via 'kathara settings' and restart the lab)"
        fi
    done
    rm -f "$LAB_DIR/shared/$probe"

    need rsu python3 iperf3 pkill timeout
    need tms python3 iperf3 pkill
    need r1 tc vtysh pkill
    if (( CAPTURE_PCAP )); then
        for dev in tms r1; do
            if ! kx "$dev" "command -v tcpdump >/dev/null && echo HAVE" | grep -qx HAVE; then
                log "WARNING: tcpdump missing in $dev - pcap capture disabled"
                CAPTURE_PCAP=0
            fi
        done
    fi

    # C1 means no BFD: refuse to run if any BFD session is up
    for dev in r1 r2; do
        out=$(kx "$dev" "vtysh -c 'show bfd peers'")
        grep -qi "status: up" <<<"$out" \
            && die "BFD session UP on $dev - that is C2, not C1 (disable bfdd or remove 'ip ospf bfd')"
        grep -qi "status:" <<<"$out" \
            && log "WARNING: BFD sessions exist on $dev but are not up - check before trusting C1"
    done

    kx r1 "tc qdisc show dev eth2" | grep -qE 'bfifo|pfifo' \
        || log "WARNING: no explicit leaf queue on r1 eth2 - queue size = txqueuelen (often 1000 pkts = ~1.2 s at 10 Mbit/s)"

    # leftovers from an aborted run would keep the primary dead
    kx r1 "bash $C_BIN/restore_primary.sh" >/dev/null
    log "waiting for OSPF to use the primary path ..."
    wait_primary || die "OSPF is not using the primary (check 'ip route get $TMS_IP' and 'show ip ospf neighbor' on r1)"
}

record_meta() {
    {
        echo "condition=$CONDITION iterations=$ITERATIONS started=$(date -Is)"
        echo "warmup=${WARMUP_MIN}-${WARMUP_MAX}s post_fail=${POST_FAIL}s settle=${SETTLE}s"
        echo "v2i: ${V2I_RATE} pps, ${V2I_SIZE} B, DSCP ${V2I_DSCP}, port ${V2I_PORT}, ${RSU_IP} -> ${TMS_IP}"
        echo "background: iperf3 TCP ${BG_STREAMS} x ${BG_RATE} (${BG_PACING}), ${RSU_IP} -> ${TMS_IP}"
        echo "analysis: pre_window=${PRE_WINDOW}s post_window=${POST_WINDOW}s deadlines=20,100 ms"
        echo "failure: silent, tc matchall drop on r1 eth1 (egress+ingress)"
        echo "host: $(uname -a)"
        kathara --version 2>/dev/null | head -1
    } > "$H_RUN/run_config.txt"
    local r
    for r in r1 r2; do
        kx "$r" "vtysh -c 'show version'; vtysh -c 'show running-config'; \
                 vtysh -c 'show ip ospf interface'; vtysh -c 'show bfd peers'; \
                 for i in eth0 eth1 eth2; do echo \"== \$i\"; tc -s qdisc show dev \$i; tc class show dev \$i; done" \
            > "$H_RUN/${r}_state.txt"
    done
}

cleanup() {
    local rc=$?
    trap - EXIT
    log "cleanup: stopping traffic and restoring the primary link"
    kx rsu "pkill -f '^python3 /shared/v2i_sender.py'; pkill -f '^iperf3 -c'; true" >/dev/null
    kx tms "pkill -f '^python3 /shared/v2i_receiver.py'; pkill -f '^tcpdump'; pkill -f '^iperf3 -s'; true" >/dev/null
    kx r1  "pkill -f '^ip monitor route'; pkill -f '^bash $C_BIN/link_sampler'; pkill -f '^tcpdump'; bash $C_BIN/restore_primary.sh" >/dev/null
    jobs -p | xargs -r kill 2>/dev/null
    exit "$rc"
}

# --------------------------------------------------------------- iteration ---
run_iteration() {
    local i=$1 it H C warm dur rx tx bg
    local -a mon=()
    it=$(printf 'iter_%02d' "$i")
    H="$H_RUN/$it"
    C="$C_RUN/$it"
    mkdir -p "$H"
    warm=$(( WARMUP_MIN + RANDOM % (WARMUP_MAX - WARMUP_MIN + 1) ))
    dur=$(( warm + POST_FAIL ))
    log "[$it] start: warm-up ${warm}s, traffic ${dur}s"

    # 0. clean starting state
    kx rsu "pkill -f '^python3 /shared/v2i_sender.py'; pkill -f '^iperf3 -c'; true" >/dev/null
    kx tms "pkill -f '^python3 /shared/v2i_receiver.py'; pkill -f '^tcpdump'; true" >/dev/null
    wait_primary || die "[$it] OSPF is not on the primary path"
    kx r1 "tc -s qdisc show dev eth1; tc -s qdisc show dev eth2" > "$H/tc_r1_before.txt"

    # 1. monitors
    kx r1 "bash $C_BIN/route_monitor.sh $C/route_events.log" >/dev/null &
    mon+=($!)
    kx r1 "bash $C_BIN/link_sampler.sh $C/link_samples.txt" >/dev/null &
    mon+=($!)
    if (( CAPTURE_PCAP )); then
        kx tms "tcpdump -i eth0 -s 96 -Z root -w $C/v2i_tms.pcap udp port $V2I_PORT" > "$H/tcpdump_tms.log" &
        mon+=($!)
        kx r1 "tcpdump -i any -s 128 -Z root -w $C/ctrl_r1.pcap 'proto ospf or udp port 3784 or udp port 3785'" > "$H/tcpdump_r1.log" &
        mon+=($!)
    fi

    # 2. receiver first, so no telemetry packet is missed
    kx tms "python3 /shared/v2i_receiver.py --port $V2I_PORT --duration $(( dur + 8 )) \
            --run-id $i --out-dir $C" > "$H/receiver.log" &
    rx=$!
    sleep 3

    # 3. telemetry + background
    kx rsu "python3 /shared/v2i_sender.py --dst $TMS_IP --port $V2I_PORT --rate $V2I_RATE \
            --size $V2I_SIZE --dscp $V2I_DSCP --duration $dur --run-id $i \
            --stats $C/sender_stats.json" > "$H/sender.log" &
    tx=$!
    kx rsu "timeout $(( dur + 30 )) iperf3 -c $TMS_IP -p $IPERF_PORT -P $BG_STREAMS $BG_PACING $BG_RATE \
            -t $dur -i 0.5 -J --get-server-output > $C/iperf3.json 2>&1" >/dev/null &
    bg=$!

    # 4. failure (t_fail is logged inside r1, on the same clock as the packets)
    sleep "$warm"
    kx r1 "bash $C_BIN/inject_failure.sh $C/failure.json" | grep -qx FAIL_INJECTED \
        || die "[$it] failure injection failed"
    log "[$it] primary failed silently - observing ${POST_FAIL}s"

    # 5. let the traffic run out
    wait "$tx" "$bg" "$rx"

    # 6. counters BEFORE restoring (black-hole counter = drops of all flows during the outage)
    kx r1 "tc -s qdisc show dev eth1; tc -s qdisc show dev eth2; \
           echo '--- black-hole counters eth1 egress / ingress'; \
           tc -s filter show dev eth1 parent 1:; tc -s filter show dev eth1 parent ffff:" \
        > "$H/tc_r1_after.txt"
    kx r1  "pkill -f '^ip monitor route'; pkill -f '^bash $C_BIN/link_sampler'; pkill -f '^tcpdump'; true" >/dev/null
    kx tms "pkill -f '^tcpdump'; true" >/dev/null
    wait "${mon[@]}" 2>/dev/null

    # 7. restore and wait for OSPF to move back to the primary
    kx r1 "bash $C_BIN/restore_primary.sh" | grep -qx RESTORED \
        || die "[$it] could not remove the drop filters on r1 eth1"
    log "[$it] primary restored, waiting for OSPF ..."
    wait_primary || die "[$it] OSPF did not return to the primary within ${CONVERGE_TIMEOUT}s"

    # 8. per-iteration metrics
    kx tms "python3 /shared/v2i_receiver.py --analyze $C \
            --pre-window $PRE_WINDOW --post-window $POST_WINDOW" | tee "$H/analysis.txt"
    sleep "$SETTLE"
}

# -------------------------------------------------------------------- main ---
main() {
    [[ -d "$LAB_DIR/shared" ]] || die "$LAB_DIR/shared does not exist"
    write_helpers
    preflight

    mkdir -p "$H_RUN"
    exec > >(tee -a "$H_RUN/run.log") 2>&1
    trap cleanup EXIT
    trap 'exit 130' INT TERM

    record_meta
    kx tms "pkill -f '^iperf3 -s'; iperf3 -s -D -p $IPERF_PORT" >/dev/null
    sleep 1

    local per=$(( (WARMUP_MIN + WARMUP_MAX) / 2 + POST_FAIL + SETTLE + 40 ))
    log "C1 baseline: $ITERATIONS iteration(s), roughly $(( per * ITERATIONS / 60 + 1 )) min"
    log "results: $H_RUN"

    local i
    for (( i = 1; i <= ITERATIONS; i++ )); do
        run_iteration "$i"
    done

    kx tms "python3 /shared/v2i_receiver.py --aggregate $C_RUN" | tee "$H_RUN/aggregate.txt"
    log "done: $H_RUN"
}

main "$@"