#!/usr/bin/env python3
# Figures of one run, from results/<run>/ (aggregate.py is run first if needed):
#   python3 analyze.py results/C2_20261009_120000
#   python3 analyze.py results/C1_... results/C2_... results/C3_...    -> also the comparison
# One figure with 4 panels (left: background TCP, right: V2I; top: delay, bottom: goodput):
#   <run>/plots/iter_N.png         one iteration
#   <run>/plots/average.png        all iterations (thin lines) and their mean (thick line)
#   results/comparison.png, .csv   mean +/- std of each configuration (several runs)
import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from aggregate import aggregate, load_v2i, LATE_MS, OFFERED_MBPS, V2I_MBPS

BACKUP_MBPS = 10
# colour-blind safe palette (Okabe-Ito): (light = packets / single iterations, dark = mean)
V2I_COLORS = ("#56B4E9", "#0072B2")
TCP_COLORS = ("#CC79A7", "#882E72")
LOST_COLOR = "#D55E00"
plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
                     "font.size": 17, "axes.titlesize": 18, "axes.labelsize": 18, "xtick.labelsize": 16,
                     "ytick.labelsize": 16, "legend.fontsize": 17, "figure.titlesize": 22,
                     "lines.linewidth": 3.2, "axes.linewidth": 1.4,
                     "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 200})


def lines(ax, ts_list, mean, col, colors, label):
    """One iteration: its line. Several: a thin line for each + their mean."""
    if len(ts_list) > 1:
        for ts in ts_list:
            ax.plot(ts.t, ts[col], color=colors[0], lw=1.5, alpha=0.6)
    ax.plot(mean.index, mean[col], color=colors[1], label=label)


def loss_marks(ax, t, label):
    """Red ticks along the bottom of a panel at the times packets were lost."""
    ax.plot(t, np.full(len(t), 0.03), "|", color=LOST_COLOR, ms=20, mew=2.5,
            transform=ax.get_xaxis_transform(), label=label)


def plot_run(ts_list, lost_t, m, title, path, packets=None):
    mean = pd.concat(ts_list).groupby("t").mean(numeric_only=True)
    fig, ((tcp_d, v2i_d), (tcp_g, v2i_g)) = plt.subplots(2, 2, figsize=(22, 13))

    # top left: background TCP delay = the kernel's RTT estimate (none while nothing arrives)
    lines(tcp_d, ts_list, mean, "bg_rtt_ms", TCP_COLORS, "background TCP (4 flows)")
    loss_marks(tcp_d, mean.index[mean.bg_retrans > 0], "TCP segment retransmitted (= lost)")
    tcp_d.set_ylabel("round-trip time (ms)")
    tcp_d.set_title(f"Background TCP: delay\nmean RTT {m['bg_rtt_ms_pre']:.1f} ms before, "
                    f"{m['bg_rtt_ms_post']:.1f} ms after the failure")

    # top right: V2I one-way delay of every packet (one iteration) and its mean per 0.5 s
    if packets is not None:
        v2i_d.scatter(packets.t, packets.delay_ms, s=4, color=V2I_COLORS[0], alpha=0.35)
        v2i_d.scatter([], [], s=80, color=V2I_COLORS[0], label="V2I packet")   # bigger dot for the legend
    lines(v2i_d, ts_list, mean, "v2i_delay_ms", V2I_COLORS, "V2I telemetry (mean per 0.5 s)")
    v2i_d.axhline(LATE_MS, color=LOST_COLOR, ls=":", lw=3, label=f"{LATE_MS} ms V2I budget")
    loss_marks(v2i_d, lost_t, "V2I packet lost")
    v2i_d.set_ylabel("one-way delay (ms)")
    v2i_d.set_title(f"V2I telemetry: delay\nafter the failure: on time {m['v2i_ontime_pct_post']:.1f}%, "
                    f"late {m['v2i_late_pct_post']:.1f}%, lost {m['v2i_lost_pct_post']:.1f}%")

    # bottom left: background TCP goodput
    lines(tcp_g, ts_list, mean, "bg_mbps", TCP_COLORS, None)
    tcp_g.axhline(OFFERED_MBPS, color="0.45", ls=":", lw=3, label="offered load")
    tcp_g.axhline(BACKUP_MBPS, color=LOST_COLOR, ls="-.", lw=3, label="backup capacity (10 Mbit/s)")
    tcp_g.set_ylim(0, OFFERED_MBPS * 1.12)
    tcp_g.set_title(f"Background TCP: goodput\n{m['bg_mbps_pre']:.1f} Mbit/s before, {m['bg_mbps_post']:.1f} "
                    f"Mbit/s after ({m['bg_delivered_pct_post']:.0f}% of offered, "
                    f"{m['bg_drop_pct_post']:.2f}% segments lost)")

    # bottom right: V2I goodput
    lines(v2i_g, ts_list, mean, "v2i_mbps", V2I_COLORS, None)
    v2i_g.axhline(V2I_MBPS, color="0.45", ls=":", lw=3, label="offered load")
    v2i_g.set_ylim(0, V2I_MBPS * 1.25)
    v2i_g.set_title(f"V2I telemetry: goodput\n{m['v2i_mbps_pre']:.2f} Mbit/s before, "
                    f"{m['v2i_mbps_post']:.2f} Mbit/s after the failure")

    # same time axis on all panels, from the first to the last full 10 s, no empty margins
    x0, x1 = int(round(mean.index.min() / 10) * 10), int(round(mean.index.max() / 10) * 10)
    for ax in (tcp_d, v2i_d):
        ax.set_yscale("log")
        ax.set_ylim(bottom=ax.get_ylim()[0] / 4)            # room for the loss marks under the data
    for ax in (tcp_g, v2i_g):
        ax.set_ylabel("goodput (Mbit/s)")
    for ax in (tcp_d, v2i_d, tcp_g, v2i_g):
        ax.axvline(0, color="black", lw=2.5, label="primary link fails")
        ax.axvline(m["convergence_s"], color="0.4", ls="--", lw=2.5, label="first packet via backup")
        ax.set_xlim(x0, x1)
        ax.set_xticks(range(x0, x1 + 1, 10))
        ax.set_xlabel("time relative to the failure (s)")
        ax.grid(alpha=0.3)

    # one legend for the whole figure, below it (each label once)
    legend = {}
    for ax in (tcp_d, v2i_d, tcp_g, v2i_g):
        for h, lab in zip(*ax.get_legend_handles_labels()):
            legend.setdefault(lab, h)
    fig.suptitle(title, fontweight="bold")
    fig.tight_layout(h_pad=2.5, w_pad=3)
    fig.legend(legend.values(), legend.keys(), loc="upper center", bbox_to_anchor=(0.5, 0),
               ncol=4, frameon=False)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def compare(runs, folder):
    """Bar per configuration (mean, error bar = std over iterations) + the table for the report."""
    panels = [("convergence_s", "Convergence time (s)"), ("v2i_ontime_pct_post", "V2I on time (%)"),
              ("v2i_late_pct_post", "V2I late (%)"), ("v2i_lost_pct_post", "V2I lost (%)"),
              ("v2i_delay_ms_post", "V2I mean delay (ms)"), ("bg_mbps_post", "TCP goodput (Mbit/s)"),
              ("bg_rtt_ms_post", "TCP RTT (ms)"), ("bg_drop_pct_post", "TCP segments lost (%)")]
    fig, axes = plt.subplots(2, 4, figsize=(22, 10))
    for ax, (col, name) in zip(axes.flat, panels):
        ax.bar(list(runs), [s[col].mean() for s in runs.values()], yerr=[s[col].std() for s in runs.values()],
               capsize=6, color=["0.6", "#0072B2", "#009E73"][:len(runs)])
        ax.set_title(name)
        ax.grid(axis="y", alpha=0.3)
    axes.flat[0].set_yscale("log")                          # C1 ~30 s vs C2/C3 ~0.3 s
    fig.suptitle("After the failure: mean and standard deviation over the iterations", fontweight="bold")
    fig.tight_layout()
    fig.savefig(f"{folder}/comparison.png")
    plt.close(fig)

    table = pd.DataFrame({label: s.mean(numeric_only=True).round(2).astype(str) + " ± "
                          + s.std(numeric_only=True).round(2).astype(str) for label, s in runs.items()})
    table.to_csv(f"{folder}/comparison.csv")
    print(f"\n{table.to_string()}\n\n{folder}/comparison.png and comparison.csv written")


runs = {}
for run in [r.rstrip("/") for r in sys.argv[1:]]:
    if not os.path.isdir(f"{run}/aggregate"):
        aggregate(run)
    label = os.path.basename(run).split("_")[0]             # C1, C2 or C3
    s = pd.read_csv(f"{run}/aggregate/summary.csv")
    ts_all = pd.read_csv(f"{run}/aggregate/timeseries.csv")
    os.makedirs(f"{run}/plots", exist_ok=True)

    ts_list, lost_list = [], []
    for _, m in s.iterrows():                               # one figure per iteration
        ts = ts_all[ts_all.iteration == m.iteration]
        packets, _ = load_v2i(f"{run}/{m.iteration}")
        lost = packets.t[packets.status == "lost"]
        ts_list.append(ts)
        lost_list.append(lost)
        plot_run([ts], lost, m, f"{label}, {m.iteration}: convergence {m.convergence_s:.2f} s",
                 f"{run}/plots/{m.iteration}.png", packets)

    m = s.mean(numeric_only=True)                           # one figure for all iterations together
    n = f"{len(s)} iteration{'s' if len(s) > 1 else ''}"
    plot_run(ts_list, pd.concat(lost_list), m, f"{label}, mean of {n} (thin lines: single iterations): "
             f"convergence {m.convergence_s:.2f} s", f"{run}/plots/average.png")
    print(f"plots written to {run}/plots/")
    runs[label] = s

if len(runs) > 1:
    compare(runs, os.path.dirname(run))