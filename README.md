# IK2202 project: Network Resilience (Team 12)

Failover of a V2I telemetry stream from a 100 Mbit/s primary link to a 10 Mbit/s backup,
emulated with Kathará + FRRouting, in three configurations:

| folder        | configuration                              |
|---------------|--------------------------------------------|
| `v2i-lab-c1/` | C1: native OSPF                            |
| `v2i-lab-c2/` | C2: OSPF + BFD (3 x 100 ms)                |
| `v2i-lab-c3/` | C3: OSPF + BFD + strict-priority QoS       |

## Running

```bash
cd v2i-lab-c2 && kathara lstart && cd ..   # deploy one lab by hand
./run_failover.sh c2 30                    # configuration, iterations
python3 analyze.py results/C1_* results/C2_* results/C3_*   # comparison of the three
```

`run_failover.sh` collects the raw data, then runs `aggregate.py` (numbers) and `analyze.py` (figures).

```
results/C2_<date>/
  run_info.txt
  iter_N/       v2i.csv, sender.log, receiver.log, iperf3.json, t_fail, t_iperf, r1_drops
  aggregate/    summary.csv (per iteration), mean_std.csv, timeseries.csv (0.5 s bins)
  plots/        iter_N/v2i.png, iter_N/background.png, average/v2i.png, average/background.png
results/comparison.csv, comparison.png      (when analyze.py gets several runs)
```

## Metrics ("pre" = 20 s before the failure, "post" = 70 s after it)

- **Convergence time**: failure until the first V2I packet arrives over the backup.
- **V2I**: one-way delay from the timestamps in each packet; every packet sent is
  *on time* (delay <= 20 ms), *late* or *lost* (sequence gap). Success = on time.
- **Background (4 paced TCP flows, 70 Mbit/s offered)**: goodput received by the TMS,
  TCP RTT (kernel estimate), and retransmitted segments, which is how TCP sees the
  packets the network dropped. Drop rate = retransmissions / segments sent;
  delivered share = goodput / offered load.
- **r1 drops**: packets dropped by tc on r1, on the failed primary link and in the backup queue.