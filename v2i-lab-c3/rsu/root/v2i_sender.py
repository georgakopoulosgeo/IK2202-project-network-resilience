#!/usr/bin/env python3
# V2I telemetry sender (runs on the RSU).
# 1000 pkt/s = 100 vehicles x 10 Hz, 600-byte payload: seq (8 B) + send time (8 B) + padding.
# in mbps = 1000 * 600 * 8 / 1e6 = 4.8 Mbit/s (5.14 Mbit/s on the wire)
# Optional surge: BURST_RATE pkt/s between BURST_START and BURST_START + BURST_LEN
# (seconds after the sender starts), e.g. an incident where more vehicles report.
# Usage: python3 v2i_sender.py DURATION_S [BURST_START_S BURST_LEN_S]
import socket, struct, sys, time

DEST = ("10.0.3.2", 5555)
RATE = 1000           # packets per second, normal
BURST_RATE = 1800     # packets per second during the surge (~9.25 Mbit/s on the wire)
PAYLOAD_SIZE = 600    # bytes
DSCP_EF = 46          # not used in C1/C2, lets tc classify the telemetry in C3/C4

duration = float(sys.argv[1])
burst_start = float(sys.argv[2]) if len(sys.argv) > 2 else -1   # -1 = no surge
burst_len = float(sys.argv[3]) if len(sys.argv) > 3 else 0

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_EF << 2)
padding = b"V" * (PAYLOAD_SIZE - 16)

start = time.monotonic()
t = 0.0               # scheduled send time of the next packet (s after start)
seq = 0
while t < duration:
    # sleep until this packet's slot, so small sleep errors don't add up over the run
    wait = start + t - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    # wall-clock time: all containers share the host clock, so rx - tx = one-way delay
    sock.sendto(struct.pack("!Qq", seq, time.time_ns()) + padding, DEST)
    seq += 1
    in_burst = burst_start <= t < burst_start + burst_len
    t += 1.0 / (BURST_RATE if in_burst else RATE)

print(f"sent {seq} packets")