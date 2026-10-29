#!/usr/bin/env python3
# Final figures and key numbers of the failover experiments.
#
#   python3 final_analysis.py                            reads results_iter10/
#   python3 final_analysis.py results_iter10 results04   several folders (the newest run of each
#                                                        configuration + traffic mode is used)
#   python3 final_analysis.py --no-plots                 only writes key_numbers.txt, leaves the
#                                                        figures in final_plots/ untouched
# Output:
#   final_plots/timeline_<config>_<mode>.png   left: throughput received, right: delay with the
#                                              key numbers as a legend
#   key_numbers.txt                            the key numbers as mean ± standard deviation over
#                                              the runs, for the table in the report
#
# Lines are the median of the runs per 0.5 s, the band shows the middle 50 % of the runs.
# C1 shows one typical run (median convergence time): its runs reroute between ~31 and ~40 s,
# so an average of them would draw a slow ramp that no run actually has.
import glob, json, os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from aggregate import load_v2i, BIN, LATE_MS, MSS, V2I_BYTES

os.chdir(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = "final_plots"
T0, T1 = -20, 80                                  # seconds around the failure
TIME = np.round(np.arange(T0, T1, BIN), 3)        # start of every 0.5 s bin
SURGE_RATE = 1800                                 # V2I messages/s during the surge
FONT = 20                                         # font size (pt); was 13, bigger so it stays readable
                                                  # when the figure is shrunk to the report width

NAMES = {"C1": "OSPF only", "C2": "OSPF + BFD", "C3": "OSPF + BFD + strict priority",
         "C4": "OSPF + BFD + rate share (V2I 6 / TCP 4 Mbit/s)"}
TCP_C, V2I_C, LOST_C, CAP_C, GREY = "#eb6834", "#2a78d6", "#d03b3b", "#b9b8b2", "#52514e"
plt.rcParams.update({"font.size": FONT, "axes.spines.top": False, "axes.spines.right": False,
                     "grid.color": "#e6e5e0", "savefig.dpi": 200})


def load_iteration(folder):
    """One iteration -> time series in 0.5 s bins, key numbers, surge window (or None)."""
    v2i, t_fail = load_v2i(folder)                # one row per V2I message sent (see aggregate.py)
    v2i["bin"] = np.round(np.floor(v2i.t / BIN) * BIN, 3)
    lost = v2i.status == "lost"

    # V2I: throughput received (by arrival time), mean delay and share lost (by send time)
    arrived = v2i[~lost]
    arrival_bin = np.round(np.floor((arrived.t + arrived.delay_ms / 1000) / BIN) * BIN, 3)
    ts = pd.DataFrame(index=TIME)
    ts["v2i_mbps"] = arrived.groupby(arrival_bin).size() * V2I_BYTES * 8 / BIN / 1e6
    ts["v2i_delay_ms"] = v2i.groupby("bin").delay_ms.mean()
    ts["v2i_lost_share"] = lost.groupby(v2i.bin).mean()

    # TCP: one iperf3 interval every 0.5 s (4 flows together)
    iperf = json.load(open(f"{folder}/iperf3.json"))
    t_start = (int(open(f"{folder}/t_iperf").read()) - t_fail) / 1e9
    received = iperf.get("server_output_json", iperf)["intervals"]
    rows = []
    for sent, rcv in zip(iperf["intervals"], received):
        active = [s for s in sent["streams"] if s["bytes"] > 0]    # idle flows report an old RTT
        retrans = sent["sum"]["retransmits"]
        rows.append({"t": round(round((t_start + sent["sum"]["start"]) / BIN) * BIN, 3),
                     "tcp_mbps": rcv["sum"]["bits_per_second"] / 1e6,
                     "tcp_rtt_ms": np.mean([s["rtt"] for s in active]) / 1000 if active else np.nan,
                     "retrans": retrans,
                     "segments": rcv["sum"]["bytes"] / MSS + retrans})
    tcp = pd.DataFrame(rows).groupby("t").agg({"tcp_mbps": "mean", "tcp_rtt_ms": "mean",
                                               "retrans": "sum", "segments": "sum"})
    ts["tcp_mbps"] = tcp.tcp_mbps
    ts["tcp_rtt_ms"] = tcp.tcp_rtt_ms
    ts[["v2i_mbps", "tcp_mbps", "v2i_lost_share"]] = ts[["v2i_mbps", "tcp_mbps", "v2i_lost_share"]].fillna(0)

    # surge = 0.5 s bins in which the sender sent more than 1.4 x the normal 1000 messages/s
    sent_per_s = v2i.groupby("bin").size() / BIN
    surge_bins = sent_per_s[sent_per_s > 1400].index
    surge = (surge_bins.min(), surge_bins.max() + BIN) if len(surge_bins) > 0 else None

    # key numbers, for everything sent after the failure
    after = v2i[v2i.t >= 0]
    tcp_after = tcp[tcp.index >= 0]
    v2i_lost = (after.status == "lost").sum()
    convergence = (after.rx_ns.min() - t_fail) / 1e9              # first V2I message over the backup

    # V2I delay after the reroute: mean one-way delay of the messages sent once both flows run on
    # the backup link. Starts 2 s after the reroute, or 2 s after TCP is back if TCP was silent
    # (C1: TCP backs off after the long outage), so the queue has filled. The surge (+ 3 s for
    # the queue to drain) is left out, so C3 and C4 with the surge are compared outside it.
    tcp_back = ts.index[(ts.index >= convergence) & (ts.tcp_mbps > 0.5)]
    start = max(convergence, tcp_back.min() if len(tcp_back) > 0 else convergence) + 2
    steady = v2i.t >= start
    if surge:
        steady &= (v2i.t < surge[0] - BIN) | (v2i.t >= surge[1] + 3)

    # Background TCP round-trip time on the backup link, over the same window as the V2I delay
    # (0.5 s iperf3 bins, mean of the active flows; bins without an active flow are skipped).
    tcp_steady = (tcp.index >= start) & (tcp.index < T1)
    if surge:
        tcp_steady &= (tcp.index < surge[0] - BIN) | (tcp.index >= surge[1] + 3)

    numbers = {
        "convergence_s": convergence,
        "v2i_on_time_pct": 100 * (after.status == "on time").mean(),
        "v2i_late_pct": 100 * (after.status == "late").mean(),
        "v2i_loss_pct": 100 * v2i_lost / len(after),
        # V2I lost + TCP segments lost (TCP retransmits every segment the network dropped)
        "cumulative_loss_pct": 100 * (v2i_lost + tcp_after.retrans.sum()) / (len(after) + tcp_after.segments.sum()),
        "v2i_delay_after_ms": v2i.delay_ms[steady].mean(),        # lost messages have no delay
        "tcp_rtt_after_ms": tcp.tcp_rtt_ms[tcp_steady].mean(),
    }

    # The same metrics during the surge only (surge runs): V2I messages sent during the surge,
    # and the iperf3 bins that fall inside it.
    if surge:
        v2i_s = v2i[(v2i.t >= surge[0]) & (v2i.t < surge[1])]
        tcp_s = tcp[(tcp.index >= surge[0]) & (tcp.index < surge[1])]
        lost_s = (v2i_s.status == "lost").sum()
        numbers.update({
            "surge_duration_s": surge[1] - surge[0],
            "surge_v2i_on_time_pct": 100 * (v2i_s.status == "on time").mean(),
            "surge_v2i_late_pct": 100 * (v2i_s.status == "late").mean(),
            "surge_v2i_loss_pct": 100 * lost_s / len(v2i_s),
            "surge_v2i_delay_ms": v2i_s.delay_ms.mean(),          # delivered messages only
            "surge_cumulative_loss_pct": 100 * (lost_s + tcp_s.retrans.sum()) / (len(v2i_s) + tcp_s.segments.sum()),
            "surge_tcp_mbps": tcp_s.tcp_mbps.mean(),              # background throughput received
            "surge_tcp_rtt_ms": tcp_s.tcp_rtt_ms.mean(),
        })
    return ts, numbers, surge


def load_run(run_folder):
    series, numbers, surges = [], [], []
    for folder in sorted(glob.glob(f"{run_folder}/iter_*")):
        try:
            ts, nums, surge = load_iteration(folder)
        except Exception as e:                    # e.g. iperf3 killed by its timeout
            print(f"  skipping {folder}: {e}")
            continue
        series.append(ts)
        numbers.append(nums)
        surges.append(surge)
    return series, pd.DataFrame(numbers), surges


def draw(ax, series, column, color, floor=None):
    """median of the runs (line) and the middle 50 % of the runs (band)"""
    data = pd.concat([s[column] for s in series], axis=1)
    if floor is not None:
        data = data.clip(lower=floor)             # log axis: show 0 as "≈0"
    if len(series) > 1:
        ax.fill_between(data.index, data.quantile(0.25, axis=1), data.quantile(0.75, axis=1),
                        step="post", color=color, alpha=0.2, lw=0)
    ax.step(data.index, data.median(axis=1), where="post", color=color, lw=2)


def plot_run(name, series, numbers, surges, path):
    config, mode = name.split("_")
    if config == "C1":                            # one typical run: the one with the median convergence
        i = (numbers.convergence_s - numbers.convergence_s.median()).abs().idxmin()
        series = [series[i]]
        convergence = numbers.convergence_s[i]
    else:
        convergence = numbers.convergence_s.mean()

    fig, (thr, dly) = plt.subplots(1, 2, figsize=(17, 9))
    fig.subplots_adjust(left=0.07, right=0.99, top=0.91, bottom=0.37, wspace=0.18)
    for ax in (thr, dly):
        ax.set_xlim(T0, T1)
        ax.set_xlabel("time since the primary link failed (s)")
        ax.grid(True)
        if mode == "surge":
            start = np.mean([s[0] for s in surges if s])
            end = np.mean([s[1] for s in surges if s])
            ax.axvspan(start, end, color="#8a8984", alpha=0.2, lw=0)
        ax.axvline(0, color="black", lw=1.4)
        ax.axvline(convergence, color=GREY, lw=1.4, ls="--")

    # left: throughput received
    thr.step([T0, 0, T1], [100, 10, 10], where="post", color=CAP_C, lw=3)
    draw(thr, series, "tcp_mbps", TCP_C, floor=0.1)
    draw(thr, series, "v2i_mbps", V2I_C, floor=0.1)
    thr.set_yscale("log")
    thr.set_ylim(0.08, 200)
    thr.set_yticks([0.1, 1, 10, 100], ["≈0", "1", "10", "100"])
    thr.minorticks_off()
    thr.set_ylabel("throughput received (Mbit/s)")

    # right: delay, and a strip at the bottom that gets darker the more V2I messages are lost
    dly.axhline(LATE_MS, color=LOST_C, lw=1.8, ls=":")
    draw(dly, series, "tcp_rtt_ms", TCP_C)
    draw(dly, series, "v2i_delay_ms", V2I_C)
    lost = pd.concat([s.v2i_lost_share for s in series], axis=1).mean(axis=1)
    for t, share in lost.items():
        if share > 0:
            dly.axvspan(t, t + BIN, ymin=0, ymax=0.04, color=LOST_C, alpha=min(1, 0.15 + 2 * share), lw=0)
    dly.set_yscale("log")
    dly.set_ylim(0.15, 1e6)                       # room at the top for the key numbers
    dly.set_yticks([1, 10, 100, 1000], ["1", "10", "100", "1000"])
    dly.minorticks_off()
    dly.set_ylabel("delay (ms)")
    dly.legend(handles=[Patch(alpha=0, label=line) for line in key_numbers(numbers)],
               title=f"After Failure", loc="upper right",
               handlelength=0, handletextpad=0, fontsize=FONT - 4, title_fontsize=FONT - 4, framealpha=0.9)

    fig.suptitle(f"Configuration {config[1:]}: {NAMES[config]} + {mode} V2I traffic", x=0.07, ha="left",
                 fontsize=FONT + 3, fontweight="bold")
    legend = [Line2D([], [], color=TCP_C, lw=2, label="background TCP (throughput / round-trip time)"),
              Line2D([], [], color=V2I_C, lw=2, label="V2I telemetry (throughput / one-way delay)"),
              Patch(color=LOST_C, label="V2I lost messages"),
              Line2D([], [], color=LOST_C, lw=1.8, ls=":", label=f"{LATE_MS} ms V2I budget"),
              Line2D([], [], color=CAP_C, lw=3, label="link capacity (100 → 10 Mbit/s backup)"),
              Line2D([], [], color="black", lw=1.4, label="primary link fails"),
              Line2D([], [], color=GREY, lw=1.4, ls="--", label="traffic rerouted to the backup")]
    if mode == "surge":
        legend.append(Patch(color="#8a8984", alpha=0.35, label=f"V2I surge ({SURGE_RATE} msg/s)"))
    fig.legend(handles=legend, loc="lower center", ncol=2, frameon=False, columnspacing=3)
    fig.savefig(path)
    plt.close(fig)


def key_numbers(numbers):
    """mean over the runs, for the legend in the figures (unchanged)"""
    mean, std = numbers.mean(), numbers.std()
    return [f"Convergence time = {mean.convergence_s:.2f}s",
            f"V2I on time / late / lost = {mean.v2i_on_time_pct:.1f}% / {mean.v2i_late_pct:.1f}% / "
            f"{mean.v2i_loss_pct:.1f}%",
            f"V2I packet loss = {mean.v2i_loss_pct:.2f}%",
            f"Cumulative packet loss = {mean.cumulative_loss_pct:.2f}%"]


def key_numbers_with_std(numbers):
    """mean ± sample standard deviation (n - 1) over the runs, for key_numbers.txt only"""
    mean, std = numbers.mean(), numbers.std()

    def value(column, digits, unit):
        return f"{mean[column]:.{digits}f} ± {std[column]:.{digits}f}{unit}"

    lines = [f"Convergence time = {value('convergence_s', 2, ' s')}",
             f"V2I on time = {value('v2i_on_time_pct', 2, '%')}",
             f"V2I late = {value('v2i_late_pct', 2, '%')}",
             f"V2I lost = {value('v2i_loss_pct', 2, '%')}",
             f"Cumulative packet loss = {value('cumulative_loss_pct', 2, '%')}",
             f"V2I delay after reroute = {value('v2i_delay_after_ms', 2, ' ms')}",
             f"Background traffic mean RTT after reroute = {value('tcp_rtt_after_ms', 1, ' ms')}"]
    if "surge_v2i_loss_pct" in numbers and numbers.surge_v2i_loss_pct.notna().any():    # surge runs only
        lines += [f"During the surge ({value('surge_duration_s', 1, ' s')}):",
                  f"  - V2I on time = {value('surge_v2i_on_time_pct', 2, '%')}",
                  f"  - V2I late = {value('surge_v2i_late_pct', 2, '%')}",
                  f"  - V2I lost = {value('surge_v2i_loss_pct', 2, '%')}",
                  f"  - V2I delay = {value('surge_v2i_delay_ms', 2, ' ms')}",
                  f"  - Cumulative packet loss = {value('surge_cumulative_loss_pct', 2, '%')}",
                  f"  - Background traffic throughput = {value('surge_tcp_mbps', 2, ' Mbit/s')}",
                  f"  - Background traffic mean RTT = {value('surge_tcp_rtt_ms', 1, ' ms')}"]
    return lines


def main():
    args = sys.argv[1:]
    make_plots = "--no-plots" not in args
    folders = [a for a in args if a != "--no-plots"] or ["results_iter10"]
    # every run folder: <folder>/.../C2_20261013_170435/iter_1, iter_2, ...
    runs = sorted({os.path.dirname(p) for f in folders for p in glob.glob(f"{f}/**/C?_*/iter_1", recursive=True)},
                  key=os.path.basename)          # oldest first, so the newest run of a config wins
    loaded = {}
    for run in runs:
        print(run)
        series, numbers, surges = load_run(run)
        if not series:
            continue
        mode = "surge" if surges[0] else "steady"
        loaded[f"{os.path.basename(run)[:2]}_{mode}"] = (series, numbers, surges)

    os.makedirs(OUT_DIR, exist_ok=True)
    text = []
    for name in sorted(loaded):
        series, numbers, surges = loaded[name]
        if make_plots:
            path = f"{OUT_DIR}/timeline_{name}.png"
            plot_run(name, series, numbers, surges, path)
            print(f"  -> {path}")
        config, mode = name.split("_")
        text.append(f"Configuration {config[1:]} {mode}: {NAMES[config]}, {mode} V2I traffic ({len(numbers)} runs)")
        text += ["- " + line for line in key_numbers_with_std(numbers)] + [""]
    with open("key_numbers.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(text))
    print("  -> key_numbers.txt")


if __name__ == "__main__":
    main()