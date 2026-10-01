#!/usr/bin/env python3
"""
v2i_sender.py - V2I telemetry generator (runs on the RSU).

Models an RSU forwarding aggregated Cooperative Awareness Messages (CAMs)
to the traffic management server:
    50 vehicles x 10 Hz = 500 packets/s, 300-byte UDP payload, DSCP EF (46).

Every packet carries:
    magic   4 B   b"V2I1"      (lets the receiver ignore foreign packets)
    run_id  4 B   uint32       (ignores stray packets from a previous iteration)
    seq     8 B   uint64       (loss detection)
    tx_ns   8 B   uint64       (CLOCK_REALTIME in ns, taken just before send)
    padding to --size bytes

All Kathara containers share the host kernel clock, so the receiver can
compute true one-way delay as rx_ns - tx_ns without clock synchronisation.

Sequence numbers are never skipped: if the sender falls behind schedule
(CPU hiccup) it catches up by sending immediately, so every gap seen at the
receiver is real network loss.

Example:
    python3 v2i_sender.py --dst 10.0.3.2 --duration 100 --run-id 1 \
        --stats /shared/results/run/iter_01/sender_stats.json
"""
import argparse
import json
import signal
import socket
import struct
import sys
import time

MAGIC = b"V2I1"
HDR = struct.Struct("!4sIQQ")  # magic, run_id, seq, tx_ns -> 24 bytes

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


def main():
    p = argparse.ArgumentParser(description="V2I telemetry UDP sender")
    p.add_argument("--dst", required=True, help="receiver IP (TMS)")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=500.0,
                   help="packets/s (default 500 = 50 vehicles x 10 Hz)")
    p.add_argument("--size", type=int, default=300,
                   help="UDP payload bytes (default 300)")
    p.add_argument("--duration", type=float, required=True, help="seconds")
    p.add_argument("--dscp", type=int, default=46, help="DSCP value (default 46 = EF)")
    p.add_argument("--run-id", type=int, default=0)
    p.add_argument("--stats", help="write sender statistics JSON to this path")
    args = p.parse_args()

    if args.size < HDR.size:
        sys.exit(f"--size must be >= {HDR.size} bytes")
    if args.rate <= 0 or args.duration <= 0:
        sys.exit("--rate and --duration must be positive")

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    interval_ns = int(round(1e9 / args.rate))
    n_total = int(args.duration * args.rate)
    dst = (args.dst, args.port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (args.dscp & 0x3F) << 2)
    buf = bytearray(args.size)

    # Anchor the schedule on the monotonic clock (immune to clock steps) and
    # remember the matching realtime instant, so the analysis can compute the
    # scheduled send time of packets that never arrived.
    lead_ns = 50_000_000
    m0 = time.monotonic_ns()
    r0 = time.time_ns()
    start_mono = m0 + lead_ns
    start_real = r0 + lead_ns

    seq = 0
    send_errors = 0
    max_lag_ns = 0
    late_sends = 0  # packets sent more than one interval behind schedule

    while seq < n_total and not _stop:
        target = start_mono + seq * interval_ns
        wait = target - time.monotonic_ns()
        if wait > 0:
            time.sleep(wait / 1e9)
        lag = time.monotonic_ns() - target
        if lag > max_lag_ns:
            max_lag_ns = lag
        if lag > interval_ns:
            late_sends += 1

        HDR.pack_into(buf, 0, MAGIC, args.run_id, seq, time.time_ns())
        try:
            sock.sendto(buf, dst)
        except OSError:
            send_errors += 1  # local error (e.g. ENOBUFS), not network loss
        seq += 1

    end_real = time.time_ns()
    sock.close()

    stats = {
        "run_id": args.run_id,
        "dst": args.dst,
        "port": args.port,
        "rate_pps": args.rate,
        "interval_ns": interval_ns,
        "payload_bytes": args.size,
        "dscp": args.dscp,
        "packets_sent": seq,          # sequence numbers 0 .. packets_sent-1
        "packets_planned": n_total,
        "send_errors": send_errors,
        "start_real_ns": start_real,  # scheduled realtime of seq 0
        "end_real_ns": end_real,
        "max_lag_us": round(max_lag_ns / 1e3, 1),
        "late_sends": late_sends,
        "stopped_early": _stop,
    }
    if args.stats:
        with open(args.stats, "w") as f:
            json.dump(stats, f, indent=2)

    print(f"[sender] run {args.run_id}: sent {seq} pkts to {args.dst}:{args.port} "
          f"({args.rate:g} pps, {args.size} B, DSCP {args.dscp}), "
          f"errors={send_errors}, max_lag={stats['max_lag_us']} us, late={late_sends}")


if __name__ == "__main__":
    main()