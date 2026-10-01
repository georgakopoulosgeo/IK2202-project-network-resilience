#!/usr/bin/env python3
# Metrics and plots for one or more runs.
#   python3 analyze.py shared/results/C1_...                    -> per-iteration plots + summary
#   python3 analyze.py shared/results/C1_... shared/results/C2_...   -> also comparison.png
# Metrics per iteration:
#   convergence_s      failure -> first V2I packet delivered over the backup
#   delay_*_ms         V2I one-way delay before the failure / after the reroute
#   v2i_lost           V2I packets lost (sequence gaps), and how many after the reroute
#   cumulative_lost    all packets dropped on r1 (both flows, both directions)
import glob, os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def analyze_iteration(d):
    df = pd.read_csv(f"{d}/v2i.csv").sort_values("seq")
    t_fail = int(open(f"{d}/t_fail").read())

    # Packets sent after t_fail cannot use the primary, so the first one to arrive
    # marks the moment traffic flows over the backup.
    before, after = df[df.tx_ns < t_fail], df[df.tx_ns > t_fail]
    t_back = after.rx_ns.min()

    lost = np.setdiff1d(np.arange(df.seq.max() + 1), df.seq)
    m = {
        "iteration": os.path.basename(d),
        "convergence_s": round((t_back - t_fail) / 1e9, 3),
        "delay_pre_ms": round(before.delay_ms.mean(), 2),
        "delay_post_ms": round(after.delay_ms.mean(), 2),
        "delay_post_p99_ms": round(after.delay_ms.quantile(0.99), 2),
        "v2i_lost": len(lost),
        "v2i_loss_pct": round(100 * len(lost) / (df.seq.max() + 1), 2),
        "v2i_lost_after_reroute": int((lost > after.seq.min()).sum()),
        "cumulative_lost": int(open(f"{d}/r1_drops").read()),
    }
    plot_timeline(d, df, lost, t_fail, t_back, m)
    return m


def plot_timeline(d, df, lost, t_fail, t_back, m):
    t = (df.tx_ns - t_fail) / 1e9
    # send time of lost packets, interpolated from their neighbours' sequence numbers
    t_lost = (np.interp(lost, df.seq, df.tx_ns) - t_fail) / 1e9

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.scatter(t, df.delay_ms, s=2, label="V2I packet delay")
    ax.scatter(t_lost, np.full(len(t_lost), 0.02), marker="|", color="red",
               transform=ax.get_xaxis_transform(), label="V2I packet lost")
    ax.axvline(0, color="black", label="primary link failure")
    ax.axvline((t_back - t_fail) / 1e9, color="green", ls="--", label="first packet via backup")
    ax.axhline(20, color="gray", ls=":", label="20 ms budget")
    ax.set_yscale("log")
    ax.set_ylim(bottom=df.delay_ms.min() / 5)   # room for the loss marks under the data
    ax.set_xlabel("time relative to the failure (s)")
    ax.set_ylabel("one-way delay (ms)")
    ax.set_title(f"{m['iteration']}: convergence {m['convergence_s']} s, "
                 f"V2I lost {m['v2i_lost']}, all packets dropped {m['cumulative_lost']}")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)
    fig.savefig(f"{d}/timeline.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


runs = {}
for run in sys.argv[1:]:
    iters = sorted(glob.glob(f"{run}/iter_*"), key=lambda p: int(p.rsplit("_", 1)[1]))
    s = pd.DataFrame([analyze_iteration(d) for d in iters])
    s.to_csv(f"{run}/summary.csv", index=False)
    print(f"\n{run}\n{s.to_string(index=False)}")
    print("\nmean +/- std over", len(s), "iterations")
    print(s.drop(columns="iteration").agg(["mean", "std"]).T.round(2).to_string())
    runs[os.path.basename(run.rstrip("/")).split("_")[0]] = s

if len(runs) > 1:
    panels = [("convergence_s", "Convergence time (s)"),
              ("delay_post_ms", "V2I delay after reroute (ms)"),
              ("v2i_lost", "V2I packets lost"),
              ("cumulative_lost", "All packets dropped on r1")]
    fig, axes = plt.subplots(1, 4, figsize=(16, 4))
    for ax, (col, title) in zip(axes, panels):
        ax.boxplot([s[col].dropna() for s in runs.values()])
        ax.set_xticks(range(1, len(runs) + 1), list(runs))
        if col == "convergence_s":
            ax.set_yscale("log")      # C1 ~40 s vs C2/C3 well under 1 s
        ax.set_title(title)
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig("comparison.png", dpi=150)
    print("\ncomparison.png written")