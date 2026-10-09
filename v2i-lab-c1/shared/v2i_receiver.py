#!/usr/bin/env python3
"""
v2i_receiver.py - V2I telemetry receiver and analysis (runs on the TMS).

Three modes:

1) Receive (default)
       python3 v2i_receiver.py --port 5005 --duration 110 --run-id 1 --out-dir DIR
   Writes DIR/packets.csv (seq, tx_ns, rx_ns) and DIR/receiver_stats.json.
   Receive times come from kernel socket timestamps (SO_TIMESTAMPNS), so Python
   scheduling delay does not inflate the measured one-way delay.

2) Analyze one iteration
       python3 v2i_receiver.py --analyze DIR
   Needs DIR/packets.csv, DIR/sender_stats.json, DIR/failure.json
   (optional: DIR/route_events.log, DIR/receiver_stats.json).
   Writes DIR/summary.json with the three headline metrics:
     1. Failover convergence time
          failover_ms      failure -> first telemetry packet delivered via backup
          route_switch_ms  failure -> kernel route to the TMS moved to the backup
                           interface (control-plane part, from route_events.log)
     2. V2I packet loss, split by where it happens
          pre_lost         steady state before the failure (should be 0)
          blackout_lost    between the failure and the reroute
          post_lost        on the backup link, in the post-reroute window
          v2i_lost_total   whole run
     3. Delay and deadline exceedance (pre-failure and post-reroute windows)
          *_delay_{mean,p50,p99,max}_ms, *_jitter_ms (mean |IPDV|),
          *_exceed_<D>ms_pct = % of packets that were lost OR arrived later than D

3) Aggregate all iterations of a run
       python3 v2i_receiver.py --aggregate RUN_DIR
   Reads RUN_DIR/iter_*/summary.json, writes RUN_DIR/summary.csv (one row per
   iteration) and RUN_DIR/stats.csv (n, mean, SD, 95% CI, min, max per metric).
"""
import argparse
import csv
import glob
import json
import math
import os
import signal
import socket
import statistics
import struct
import sys
import time

MAGIC = b"V2I1"
HDR = struct.Struct("!4sIQQ")  # magic, run_id, seq, tx_ns (must match sender)
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)  # 35 on Linux

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


# --------------------------------------------------------------------------
# 1) Receive
# --------------------------------------------------------------------------
def _socket_drops(port):
    """Datagrams dropped by the kernel at OUR socket (receiver-side, not network)."""
    try:
        with open("/proc/net/udp") as f:
            next(f)
            for line in f:
                cols = line.split()
                if int(cols[1].split(":")[1], 16) == port:
                    return int(cols[-1])
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return None


def receive(args):
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    os.makedirs(args.out_dir, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    except OSError:
        print("[receiver] WARNING: SO_TIMESTAMPNS unavailable, using user-space timestamps")
    sock.bind((args.bind, args.port))
    sock.settimeout(0.2)
    anc_size = socket.CMSG_SPACE(16)

    records = []
    seen = set()
    duplicates = foreign = kernel_ts = 0
    end = time.monotonic() + args.duration

    while not _stop and time.monotonic() < end:
        try:
            data, anc, _flags, _addr = sock.recvmsg(4096, anc_size)
        except (socket.timeout, InterruptedError):
            continue
        rx_ns = None
        for level, ctype, cdata in anc:
            if level == socket.SOL_SOCKET and ctype == SO_TIMESTAMPNS and len(cdata) >= 16:
                sec, nsec = struct.unpack("qq", cdata[:16])
                rx_ns = sec * 1_000_000_000 + nsec
        if rx_ns is None:
            rx_ns = time.time_ns()
        else:
            kernel_ts += 1

        if len(data) < HDR.size:
            foreign += 1
            continue
        magic, run_id, seq, tx_ns = HDR.unpack_from(data)
        if magic != MAGIC or (args.run_id is not None and run_id != args.run_id):
            foreign += 1
            continue
        if seq in seen:
            duplicates += 1
            continue
        seen.add(seq)
        records.append((seq, tx_ns, rx_ns))

    drops = _socket_drops(args.port)
    sock.close()
    records.sort()

    with open(os.path.join(args.out_dir, "packets.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["seq", "tx_ns", "rx_ns"])
        w.writerows(records)

    stats = {
        "run_id": args.run_id,
        "received": len(records),
        "duplicates": duplicates,
        "foreign_or_stale": foreign,
        "kernel_rx_timestamps": kernel_ts,
        "socket_drops": drops,
    }
    with open(os.path.join(args.out_dir, "receiver_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(f"[receiver] run {args.run_id}: received {len(records)} pkts, dup={duplicates}, "
          f"foreign={foreign}, socket_drops={drops}, kernel_ts={kernel_ts}")


# --------------------------------------------------------------------------
# 2) Analyze one iteration
# --------------------------------------------------------------------------
def _load_json(path, required=True):
    if not os.path.exists(path):
        if required:
            sys.exit(f"missing {path}")
        return None
    with open(path) as f:
        return json.load(f)


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, math.ceil(q / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def _r(x, nd=3):
    return None if x is None else round(x, nd)


def _window(pk, lo, hi, deadlines, prefix):
    """Stats for sequence numbers [lo, hi). Lost packets count as deadline misses."""
    sent = max(0, hi - lo)
    delays = {s: (pk[s][1] - pk[s][0]) / 1e6 for s in range(lo, hi) if s in pk}
    lost = sent - len(delays)
    vals = sorted(delays.values())
    ipdv = [abs(delays[s] - delays[s - 1]) for s in delays if s - 1 in delays]

    out = {
        f"{prefix}_sent": sent,
        f"{prefix}_lost": lost,
        f"{prefix}_loss_pct": _r(100 * lost / sent if sent else None),
        f"{prefix}_delay_mean_ms": _r(statistics.fmean(vals) if vals else None),
        f"{prefix}_delay_p50_ms": _r(_pct(vals, 50)),
        f"{prefix}_delay_p99_ms": _r(_pct(vals, 99)),
        f"{prefix}_delay_max_ms": _r(vals[-1] if vals else None),
        f"{prefix}_jitter_ms": _r(statistics.fmean(ipdv) if ipdv else None),
    }
    for d in deadlines:
        late = sum(1 for v in vals if v > d)
        out[f"{prefix}_exceed_{d:g}ms_pct"] = _r(100 * (lost + late) / sent if sent else None)
    return out


def analyze(d, args):
    snd = _load_json(os.path.join(d, "sender_stats.json"))
    fail = _load_json(os.path.join(d, "failure.json"))
    rcv = _load_json(os.path.join(d, "receiver_stats.json"), required=False) or {}

    n = snd["packets_sent"]
    iv = snd["interval_ns"]
    start = snd["start_real_ns"]
    rate = 1e9 / iv

    pk = {}
    with open(os.path.join(d, "packets.csv")) as f:
        rows = csv.reader(f)
        next(rows)
        for row in rows:
            s, tx, rx = int(row[0]), int(row[1]), int(row[2])
            if s < n:
                pk[s] = (tx, rx)

    t_fail = fail["t_fail_start_ns"]
    t_fail_end = fail["t_fail_end_ns"]
    warnings = []

    # First sequence number scheduled at/after the failure.
    seq_fail = min(n, max(0, math.ceil((t_fail - start) / iv)))

    # A packet sent after the drop filter was active can only arrive via the backup.
    post = [(s, rx) for s, (tx, rx) in pk.items() if tx > t_fail_end]
    if post:
        seq_restore = min(s for s, _ in post)
        t_restore = min(rx for _, rx in post)
        failover_ms = (t_restore - t_fail) / 1e6
        recovered = True
    else:
        seq_restore, failover_ms, recovered = n, None, False
        warnings.append("no telemetry delivered after the failure (path never recovered)")

    # Control-plane part: when did the kernel route move to the backup interface?
    route_switch_ms = None
    rl = os.path.join(d, "route_events.log")
    if os.path.exists(rl):
        with open(rl) as f:
            for line in f:
                ts, _, rest = line.strip().partition(" ")
                if not ts.isdigit() or int(ts) < t_fail or rest.startswith("Deleted"):
                    continue
                if args.route_prefix in rest and f"dev {args.backup_dev}" in rest:
                    route_switch_ms = (int(ts) - t_fail) / 1e6
                    break
        if route_switch_ms is None:
            warnings.append("route switch not found in route_events.log")

    pre_lo = max(0, seq_fail - int(args.pre_window * rate))
    post_hi = min(n, seq_restore + int(args.post_window * rate))
    post_eff = (post_hi - seq_restore) / rate
    if recovered and post_eff < args.post_window - 0.5:
        warnings.append(f"post-reroute window truncated to {post_eff:.1f}s (increase POST_FAIL)")
    if seq_fail - pre_lo < 0.9 * args.pre_window * rate:
        warnings.append("pre-failure window shorter than requested (sender started late?)")

    blackout_sent = seq_restore - seq_fail
    blackout_lost = blackout_sent - sum(1 for s in range(seq_fail, seq_restore) if s in pk)

    summary = {
        "iteration": os.path.basename(os.path.normpath(d)),
        "recovered": recovered,
        # --- metric 1: failover convergence ---
        "failover_ms": _r(failover_ms),
        "route_switch_ms": _r(route_switch_ms),
        # --- metric 2: V2I loss ---
        "v2i_sent": n,
        "v2i_delivered": len(pk),
        "v2i_lost_total": n - len(pk),
        "blackout_sent": blackout_sent,
        "blackout_lost": blackout_lost,
        "post_window_s": _r(post_eff, 2),
    }
    deadlines = [float(x) for x in args.deadlines.split(",") if x.strip()]
    pre = _window(pk, pre_lo, seq_fail, deadlines, "pre")
    postw = _window(pk, seq_restore, post_hi, deadlines, "post")
    summary.update(pre)
    summary.update(postw)
    if pre["pre_lost"]:
        warnings.append(f"{pre['pre_lost']} telemetry packets lost BEFORE the failure "
                        "(steady state is not clean: check background rate / queue)")

    # --- sanity / provenance ---
    summary.update({
        "duplicates": rcv.get("duplicates"),
        "receiver_socket_drops": rcv.get("socket_drops"),
        "sender_send_errors": snd.get("send_errors"),
        "sender_max_lag_us": snd.get("max_lag_us"),
        "sender_late_sends": snd.get("late_sends"),
        "warnings": warnings,
    })
    with open(os.path.join(d, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    def fmt(x, unit=""):
        return "n/a" if x is None else f"{x}{unit}"

    d0 = f"{deadlines[0]:g}" if deadlines else None
    print(f"[analyze] {summary['iteration']}: "
          f"failover={fmt(summary['failover_ms'], ' ms')} "
          f"(route switch {fmt(summary['route_switch_ms'], ' ms')}) | "
          f"V2I lost total={summary['v2i_lost_total']} "
          f"[pre {pre['pre_lost']}, blackout {blackout_lost}, post {postw['post_lost']}] | "
          f"delay p99 pre={fmt(pre['pre_delay_p99_ms'], ' ms')} "
          f"post={fmt(postw['post_delay_p99_ms'], ' ms')}"
          + (f" | post >{d0}ms or lost={fmt(postw.get(f'post_exceed_{d0}ms_pct'), '%')}" if d0 else ""))
    for w in warnings:
        print(f"[analyze]   WARNING: {w}")


# --------------------------------------------------------------------------
# 3) Aggregate iterations
# --------------------------------------------------------------------------
_T95 = [None, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]

HEADLINE = ["failover_ms", "route_switch_ms", "v2i_lost_total", "blackout_lost",
            "post_lost", "pre_delay_p99_ms", "post_delay_p99_ms", "post_jitter_ms",
            "post_exceed_20ms_pct", "post_exceed_100ms_pct"]


def aggregate(root):
    files = sorted(glob.glob(os.path.join(root, "iter_*", "summary.json")))
    if not files:
        sys.exit(f"no iter_*/summary.json under {root}")
    rows = []
    for fp in files:
        with open(fp) as f:
            rows.append(json.load(f))

    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]

    with open(os.path.join(root, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(v) if isinstance(v, list) else v) for k, v in r.items()})

    stats = []
    for k in keys:
        vals = [r.get(k) for r in rows]
        vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals:
            continue
        m = statistics.fmean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        df = len(vals) - 1
        t = _T95[df] if 0 < df < len(_T95) else 1.96
        ci = t * sd / math.sqrt(len(vals)) if len(vals) > 1 else None
        stats.append({"metric": k, "n": len(vals), "mean": _r(m), "sd": _r(sd),
                      "ci95_halfwidth": _r(ci), "min": _r(min(vals)), "max": _r(max(vals))})

    with open(os.path.join(root, "stats.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, lineterminator="\n", fieldnames=["metric", "n", "mean", "sd", "ci95_halfwidth", "min", "max"])
        w.writeheader()
        w.writerows(stats)

    n_warn = sum(1 for r in rows if r.get("warnings"))
    print(f"[aggregate] {len(rows)} iterations ({n_warn} with warnings) -> summary.csv, stats.csv")
    print(f"{'metric':<24}{'n':>4}{'mean':>12}{'sd':>12}{'95% CI +/-':>12}")
    by = {s["metric"]: s for s in stats}
    for k in HEADLINE:
        s = by.get(k)
        if s:
            ci = "n/a" if s["ci95_halfwidth"] is None else s["ci95_halfwidth"]
            print(f"{k:<24}{s['n']:>4}{s['mean']:>12}{s['sd']:>12}{ci:>12}")


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="V2I telemetry receiver / analysis")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--analyze", metavar="ITER_DIR", help="analyze one iteration directory")
    mode.add_argument("--aggregate", metavar="RUN_DIR", help="aggregate iter_*/summary.json")
    # receive mode
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--duration", type=float, help="receive for this many seconds")
    p.add_argument("--run-id", type=int, default=None, help="accept only this run id")
    p.add_argument("--out-dir", help="where packets.csv / receiver_stats.json go")
    # analysis parameters
    p.add_argument("--deadlines", default="20,100", help="delay budgets in ms (default 20,100)")
    p.add_argument("--pre-window", type=float, default=10.0, help="s before failure (default 10)")
    p.add_argument("--post-window", type=float, default=30.0, help="s after reroute (default 30)")
    p.add_argument("--route-prefix", default="10.0.3.0/24", help="prefix watched in route_events.log")
    p.add_argument("--backup-dev", default="eth2", help="backup interface on r1")
    args = p.parse_args()

    if args.analyze:
        analyze(args.analyze, args)
    elif args.aggregate:
        aggregate(args.aggregate)
    else:
        if args.duration is None or not args.out_dir:
            p.error("receive mode needs --duration and --out-dir")
        receive(args)


if __name__ == "__main__":
    main()