# IK2202 project: network resilience (Team 12)

Failover of a V2I telemetry stream from a 100 Mbit/s primary link to a 10 Mbit/s backup,
emulated with Kathará + FRRouting, in three configurations:

| folder        | configuration                              |
|---------------|--------------------------------------------|
| `v2i-lab-c1/` | C1: native OSPF                            |
| `v2i-lab-c2/` | C2: OSPF + BFD (3 x 100 ms)                |
| `v2i-lab-c3/` | C3: OSPF + BFD + strict-priority QoS       |

## Lab setup

All three labs use the same `r1.startup` / `r2.startup`; the table at the top of each file
says which blocks ([BFD], [FIFO], [QoS]) to comment out for C1, C2 and C3. The FRR files are
also the same everywhere: `ospfd.conf` and `bfdd.conf` from `v2i-lab-c2`, `bfdd=no` in
`daemons` (the [BFD] line in the startup files switches it on).

## Running

```bash
cd v2i-lab-c2 && kathara lstart && cd ..   # deploy one lab by hand
./run_failover.sh c2 30                    # configuration, iterations
python3 analyze.py results/raw_data/C1_* results/raw_data/C2_* results/raw_data/C3_*   # + comparison
```

`run_failover.sh` collects the raw data, then runs `aggregate.py` (numbers) and `analyze.py` (figures).

```
results/
  raw_data/C2_<date>/
    run_info.txt
    iter_N/       v2i.csv, sender.log, receiver.log, iperf3.json, t_fail, t_iperf, r1_drops
    aggregate/    summary.csv (per iteration), mean_std.csv, timeseries.csv (0.5 s bins)
  plots/
    C2_<date>/    iter_N.png (one iteration), average.png (all iterations)
                  left: TCP RTT, right: V2I one-way delay; right axis: packets dropped per second
    comparison/   comparison.png, comparison.csv (when analyze.py gets several runs)
```

## Metrics ("pre" = before the failure, "post" = after it)

- **Convergence time**: failure until the first V2I packet arrives over the backup.
- **V2I**: one-way delay from the timestamps in each packet; every packet sent is
  *on time* (delay <= 20 ms), *late* or *lost* (sequence gap). Success = on time.
- **Background (4 paced TCP flows, 70 Mbit/s offered)**: goodput received by the TMS,
  TCP RTT (kernel estimate), and retransmitted segments, which is how TCP sees the
  packets the network dropped. Drop rate = retransmissions / segments sent;
  delivered share = goodput / offered load.
- **r1 drops**: packets dropped by tc on r1, on the failed primary link and in the backup queue.