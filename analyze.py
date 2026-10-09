#!/usr/bin/env python3
# Figures of one run (aggregate.py is run first if needed):
#   python3 analyze.py results/raw_data/C2_20261009_120000
#   python3 analyze.py results/raw_data/C1_... results/raw_data/C2_... results/raw_data/C3_...   (+ comparison)
# One figure with 2 panels (left: background TCP, right: V2I): delay over time, and on a
# second y-axis the packets dropped per second.
#   results/plots/<run>/iter_N.png      one iteration
#   results/plots/<run>/average.png     all iterations (thin lines) and their mean (thick line)
#   results/plots/comparison/           mean +/- std of each configuration (several runs)
import os, sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from aggregate import aggregate, load_v2i, BIN, LATE_MS

# colour-blind safe palette (Okabe-Ito): (light = packets / single iterations, dark = mean)
V2I_COLORS = ("#56B4E9", "#0072B2")
TCP_COLORS = ("#CC79A7", "#882E72")
DROP_COLOR = "#D55E00"
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


def drops(ax, t, per_s, label):
    """Second y-axis on the right: packets dropped per second, drawn behind the delay."""
    ax2 = ax.twinx()
    ax2.fill_between(t, per_s, step="post", color=DROP_COLOR, alpha=0.25, lw=0)
    ax2.step(t, per_s, where="post", color=DROP_COLOR, lw=2, label=label)
    ax2.set_ylim(0, max(per_s.max(), 1) * 1.15)
    ax2.set_ylabel("packets dropped per second", color=DROP_COLOR)
    ax2.tick_params(axis="y", colors=DROP_COLOR)
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(DROP_COLOR)
    ax.set_zorder(ax2.get_zorder() + 1)                     # delay on top of the drops
    ax.patch.set_visible(False)
    return ax2


def plot_run(ts_list, m, title, path, packets=None):
    mean = pd.concat(ts_list).groupby("t").mean(numeric_only=True)
    fig, (tcp, v2i) = plt.subplots(1, 2, figsize=(24, 8))

    # left: background TCP delay = the kernel's RTT estimate (none while nothing arrives);
    #       drops = retransmitted segments, counted when TCP notices the loss
    lines(tcp, ts_list, mean, "bg_rtt_ms", TCP_COLORS, "background TCP RTT (4 flows)")
    tcp.set_ylabel("round-trip time (ms)")
    tcp.set_title(f"Background TCP\nmean RTT {m['bg_rtt_ms_pre']:.1f} ms before, {m['bg_rtt_ms_post']:.1f} ms "
                  f"after the failure; {m['bg_drop_pct_post']:.2f}% of segments lost after")
    tcp2 = drops(tcp, mean.index, mean.bg_retrans / BIN, "TCP segments lost (retransmitted)")

    # right: V2I one-way delay of every packet (one iteration) and its mean per 0.5 s;
    #        drops = V2I packets lost, by the time they were sent
    if packets is not None:
        v2i.scatter(packets.t, packets.delay_ms, s=4, color=V2I_COLORS[0], alpha=0.35)
        v2i.scatter([], [], s=80, color=V2I_COLORS[0], label="V2I packet")   # bigger dot for the legend
    lines(v2i, ts_list, mean, "v2i_delay_ms", V2I_COLORS, f"V2I delay (mean per {BIN} s)")
    v2i.axhline(LATE_MS, color="#E69F00", ls=":", lw=3, label=f"{LATE_MS} ms V2I budget")
    v2i.set_ylabel("one-way delay (ms)")
    v2i.set_title(f"V2I telemetry\nafter the failure: on time {m['v2i_ontime_pct_post']:.1f}%, "
                  f"late {m['v2i_late_pct_post']:.1f}%, lost {m['v2i_lost_pct_post']:.1f}%")
    v2i2 = drops(v2i, mean.index, mean.v2i_lost_per_s, "V2I packets lost")

    # same time axis on both panels, from the first to the last full 10 s, no empty margins
    x0, x1 = int(round(mean.index.min() / 10) * 10), int(round(mean.index.max() / 10) * 10)
    for ax in (tcp, v2i):
        ax.set_yscale("log")
        ax.axvline(0, color="black", lw=2.5, label="primary link fails")
        ax.axvline(m["convergence_s"], color="0.4", ls="--", lw=2.5, label="first packet via backup")
        ax.set_xlim(x0, x1)
        ax.set_xticks(range(x0, x1 + 1, 10))
        ax.set_xlabel("time relative to the failure (s)")
        ax.grid(alpha=0.3)

    # one legend for the whole figure, below it (each label once)
    legend = {}
    for ax in (tcp, tcp2, v2i, v2i2):
        for h, lab in zip(*ax.get_legend_handles_labels()):
            legend.setdefault(lab, h)
    fig.suptitle(title, fontweight="bold")
    fig.tight_layout(w_pad=4)
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
    os.makedirs(folder, exist_ok=True)
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
    # results/raw_data/<run>  ->  results/plots/<run>
    plots = os.path.join(os.path.dirname(os.path.dirname(run)), "plots", os.path.basename(run))
    os.makedirs(plots, exist_ok=True)

    ts_list = []
    for _, m in s.iterrows():                               # one figure per iteration
        ts = ts_all[ts_all.iteration == m.iteration]
        packets, _ = load_v2i(f"{run}/{m.iteration}")
        ts_list.append(ts)
        plot_run([ts], m, f"{label}, {m.iteration}: convergence {m.convergence_s:.2f} s",
                 f"{plots}/{m.iteration}.png", packets)

    m = s.mean(numeric_only=True)                           # one figure for all iterations together
    n = f"{len(s)} iteration{'s' if len(s) > 1 else ''}"
    plot_run(ts_list, m, f"{label}, mean of {n} (thin lines: single iterations): "
             f"convergence {m.convergence_s:.2f} s", f"{plots}/average.png")
    print(f"plots written to {plots}/")
    runs[label] = s

if len(runs) > 1:
    compare(runs, os.path.join(os.path.dirname(os.path.dirname(run)), "plots", "comparison"))