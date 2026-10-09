#!/usr/bin/env python3
# V2I telemetry sender (runs on the RSU).
# 1000 pkt/s = 100 vehicles x 10 Hz, 600-byte payload: seq (8 B) + send time (8 B) + padding.
# in mbps = 1000 * 600 * 8 / 1e6 = 4.8 Mbit/s
# Usage: python3 v2i_sender.py DURATION_S
import socket, struct, sys, time
 
DEST = ("10.0.3.2", 5555)
RATE = 1000            # packets per second
PAYLOAD_SIZE = 600    # bytes
DSCP_EF = 46          # not used in C1/C2, lets tc classify the telemetry in C3
 
duration = float(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_EF << 2)
padding = b"V" * (PAYLOAD_SIZE - 16)
 
interval = 1.0 / RATE
n_packets = int(duration * RATE)
start = time.monotonic()
for seq in range(n_packets):
    # sleep until this packet's slot, so small sleep errors don't add up over the run
    wait = start + seq * interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    # wall-clock time: all containers share the host clock, so rx - tx = one-way delay
    sock.sendto(struct.pack("!Qq", seq, time.time_ns()) + padding, DEST)
 
print(f"sent {n_packets} packets")
 
