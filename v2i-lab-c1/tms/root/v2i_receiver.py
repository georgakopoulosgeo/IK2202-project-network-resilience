#!/usr/bin/env python3
# V2I telemetry receiver (runs on the TMS). Logs one CSV row per received packet.
# Usage: python3 v2i_receiver.py OUTPUT_CSV DURATION_S
import csv, socket, struct, sys, time

out_csv, duration = sys.argv[1], float(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 5555))
sock.settimeout(0.5)      # so the loop can notice the end time during a blackout

end = time.monotonic() + duration
count = 0
with open(out_csv, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["seq", "tx_ns", "rx_ns", "delay_ms"])
    while time.monotonic() < end:
        try:
            data = sock.recv(2048)
        except socket.timeout:
            continue
        rx_ns = time.time_ns()
        seq, tx_ns = struct.unpack("!Qq", data[:16])
        writer.writerow([seq, tx_ns, rx_ns, round((rx_ns - tx_ns) / 1e6, 3)])
        count += 1

print(f"received {count} packets -> {out_csv}")