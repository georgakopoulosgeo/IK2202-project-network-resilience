#!/usr/bin/env python3
# Numbers of one run, computed from its raw data in results/:
#   python3 aggregate.py results/C2_20261009_120000
# Writes results/<run>/aggregate/:
#   summary.csv     one row of metrics per iteration
#   mean_std.csv    mean, std, min and max of every metric over the iterations
#   timeseries.csv  every iteration in 0.5 s bins around the failure (used by analyze.py)
# Phases: "pre" = sent before the failure (20 s), "post" = sent after it (70 s).
#   V2I        each packet is on time (delay <= 20 ms), late or lost (sequence gap)
#   background goodput received by the TMS, TCP RTT, and retransmitted segments, which
#              is how TCP sees packets dropped by the network (TCP itself never loses data)
#   r1 drops   packets dropped by tc on r1: on the failed primary link / in the backup queue
import glob, json, os, re, sys
import numpy as np
import pandas as pd

BIN = 0.5            # s, same as 'iperf3 -i 0.5' in run_failover.sh
LATE_MS = 20         # V2I delay budget [4]
OFFERED_MBPS = 70    # background load: 4 paced TCP flows x 17.5 Mbit/s
MSS = 1448           # payload bytes per TCP segment (MTU 1500)
V2I_BYTES = 300      # payload bytes per V2I packet (v2i_sender.py)
V2I_MBPS = 500 * V2I_BYTES * 8 / 1e6   # V2I goodput if nothing is lost: 500 pkt/s -> 1.2 Mbit/s
STATUS = ["on time", "late", "lost"]


def load_v2i(d):
    """One row per V2I packet SENT, with its status: on time, late or lost."""
    t_fail = int(open(f"{d}/t_fail").read())
    n_sent = int(re.search(r"sent (\d+)", open(f"{d}/sender.log").read()).group(1))
    rx = pd.read_csv(f"{d}/v2i.csv")
    df = pd.DataFrame({"seq": range(n_sent)}).merge(rx, on="seq", how="left")
    # lost packets have no timestamps: estimate their send time from their neighbours
    df["tx_ns"] = df.tx_ns.interpolate(limit_direction="both")
    df["t"] = (df.tx_ns - t_fail) / 1e9                     # s relative to the failure
    df["status"] = np.select([df.delay_ms.isna(), df.delay_ms > LATE_MS], ["lost", "late"], "on time")
    return df, t_fail


def load_iperf(d, t_fail):
    """One row per 0.5 s iperf3 interval, the 4 TCP flows together."""
    j = json.load(open(f"{d}/iperf3.json"))
    t0 = (int(open(f"{d}/t_iperf").read()) - t_fail) / 1e9  # iperf3 start relative to the failure
    # receiver side = what reached the TMS (the sender side only counts what entered the
    # socket buffer); falls back to the sender side if the server did not answer in JSON
    received = j.get("server_output_json", j)["intervals"]
    rows = []
    for snd, rcv in zip(j["intervals"], received):
        rows.append({"t": round((t0 + snd["sum"]["start"]) / BIN) * BIN,
                     "bg_mbps": rcv["sum"]["bits_per_second"] / 1e6,
                     "bg_rtt_ms": np.mean([s["rtt"] for s in snd["streams"]]) / 1000,
                     "bg_retrans": snd["sum"]["retransmits"],
                     # every segment is delivered once, plus once per retransmission
                     "bg_segments": rcv["sum"]["bytes"] / MSS + snd["sum"]["retransmits"]})
    bg = pd.DataFrame(rows)
    bg.loc[bg.bg_mbps < 0.1, "bg_rtt_ms"] = np.nan          # nothing delivered = no new RTT sample
    return bg


def timeseries(v2i, bg):
    """0.5 s bins: V2I status shares and mean delay (by send time) + the iperf3 intervals."""
    t = np.floor(v2i.t / BIN) * BIN
    ts = pd.crosstab(t, v2i.status, normalize="index").reindex(columns=STATUS, fill_value=0) * 100
    ts.columns = ["v2i_ontime_pct", "v2i_late_pct", "v2i_lost_pct"]
    ts["v2i_delay_ms"] = v2i.delay_ms.groupby(t).mean()
    # goodput = payload received per 0.5 s, by ARRIVAL time (lost packets have no arrival time)
    t_rx = np.floor((v2i.t + v2i.delay_ms / 1000) / BIN) * BIN
    ts["v2i_mbps"] = v2i.delay_ms.groupby(t_rx).count() * V2I_BYTES * 8 / BIN / 1e6
    ts["v2i_mbps"] = ts.v2i_mbps.fillna(0)                  # nothing arrived in that 0.5 s
    return ts.join(bg.set_index("t"), how="outer").rename_axis("t").reset_index()


def analyze_iteration(d):
    v2i, t_fail = load_v2i(d)
    bg = load_iperf(d, t_fail)
    after = v2i[v2i.t >= 0]
    arrived = after.dropna(subset=["rx_ns"])
    primary, backup = map(int, open(f"{d}/r1_drops").read().split())
    m = {"iteration": os.path.basename(d),
         # the first packet sent after the failure that arrives went over the backup
         "convergence_s": (arrived.rx_ns.min() - t_fail) / 1e9,
         "v2i_lost": int((v2i.status == "lost").sum()),
         "v2i_lost_after_reroute": int(((after.status == "lost") & (after.seq > arrived.seq.min())).sum()),
         "v2i_delay_p99_ms_post": after.delay_ms.quantile(0.99),
         "bg_retrans_post": int(bg[bg.t >= 0].bg_retrans.sum()),
         "r1_drops_primary": primary,
         "r1_drops_backup": backup}
    for phase, v, b in [("pre", v2i[v2i.t < 0], bg[bg.t < 0]), ("post", after, bg[bg.t >= 0])]:
        shares = v.status.value_counts(normalize=True).reindex(STATUS, fill_value=0) * 100
        m[f"v2i_delay_ms_{phase}"] = v.delay_ms.mean()
        m[f"v2i_mbps_{phase}"] = V2I_MBPS * v.delay_ms.notna().mean()     # share delivered x rate sent
        m[f"v2i_ontime_pct_{phase}"], m[f"v2i_late_pct_{phase}"], m[f"v2i_lost_pct_{phase}"] = shares
        m[f"bg_mbps_{phase}"] = b.bg_mbps.mean()
        m[f"bg_rtt_ms_{phase}"] = b.bg_rtt_ms.mean()
        m[f"bg_drop_pct_{phase}"] = 100 * b.bg_retrans.sum() / b.bg_segments.sum()
    m["bg_delivered_pct_post"] = 100 * m["bg_mbps_post"] / OFFERED_MBPS   # share of the offered load
    return m, timeseries(v2i, bg).assign(iteration=m["iteration"])


def aggregate(run):
    iters = sorted(glob.glob(f"{run}/iter_*"), key=lambda p: int(p.rsplit("_", 1)[1]))
    rows, series = [], []
    for d in iters:
        try:
            m, ts = analyze_iteration(d)
        except Exception as e:                              # e.g. iperf3 killed by timeout
            print(f"skipping {d}: {e}")
            continue
        rows.append(m)
        series.append(ts)
    s = pd.DataFrame(rows)
    s = s[["iteration"] + sorted(s.columns.drop("iteration"))].round(3)  # sorted = grouped by class
    stats = s.drop(columns="iteration").agg(["mean", "std", "min", "max"]).T.round(3)

    os.makedirs(f"{run}/aggregate", exist_ok=True)
    s.to_csv(f"{run}/aggregate/summary.csv", index=False)
    stats.to_csv(f"{run}/aggregate/mean_std.csv")
    pd.concat(series).round(3).to_csv(f"{run}/aggregate/timeseries.csv", index=False)
    print(f"\n{run}: {len(s)} iterations\n{stats.to_string()}")
    return s


if __name__ == "__main__":
    for run in sys.argv[1:]:
        aggregate(run.rstrip("/"))