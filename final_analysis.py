#!/usr/bin/env python3
# Final figures for the presentation: ONE figure per run (configuration + traffic mode).
#   python3 final_analysis.py results04
# Every run folder (a folder with iter_*/ inside) under the given results folder is used;
# the steady/surge mode is detected from the V2I send rate. If a configuration was run twice
# in the same mode, the newest run is used.
# Output, in results_plots/ next to this script:
#   C1_steady.png, C2_steady.png, C3_steady.png, C3_surge.png, C4_surge.png, ...
#   C3_vs_C4_surge.png   TCP and V2I during the surge, strict priority vs rate share (bars)
#   summary.csv          all numbers (mean and std over the iterations)
#
# Each run figure has 2 panels on the same time axis, with the same scales in every figure:
#   left:  throughput received by the TMS: background TCP and V2I (Mbit/s)
#   right: V2I one-way delay and TCP round-trip time (ms), 20 ms V2I budget
# Under the title: share of V2I messages sent after the failure that were on time, late
# (arrived after > 20 ms) or lost (never arrived).
import glob, json, os, re, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FixedFormatter, NullLocator
from aggregate import load_v2i, BIN, LATE_MS, V2I_BYTES

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "results_plots")
T0, T1 = -20.0, 80.0                 # time window around the failure (s)
NORMAL_RATE = 1000                   # V2I packets/s (v2i_sender.py RATE)
SURGE_THRESHOLD = 1.4 * NORMAL_RATE  # a 0.5 s bin sending more than this is part of the surge
GRID = np.round(np.arange(T0, T1, BIN), 3)

NAMES = {"C1": "OSPF only",   # shown as "Configuration 1: OSPF only"
         "C2": "OSPF + BFD",
         "C3": "OSPF + BFD + strict priority",
         "C4": "OSPF + BFD + rate share (V2I 6 / TCP 4 Mbit/s)"}

# colours: two series (validated categorical slots 1-2); red = lost V2I packets and the 20 ms budget
V2I_C, TCP_C = "#2a78d6", "#eb6834"
ONTIME_C, LATE_C, LOST_C = "#0ca30c", "#fab219", "#d03b3b"
INK, INK2, GRIDC, BAND, CAP = "#1b1b1a", "#52514e", "#e6e5e0", "#8a8984", "#b9b8b2"
plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
                     "font.size": 13, "axes.titlesize": 15, "axes.labelsize": 13,
                     "xtick.labelsize": 12, "ytick.labelsize": 12, "legend.fontsize": 12,
                     "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
                     "axes.edgecolor": INK2, "axes.linewidth": 0.8, "grid.color": GRIDC, "grid.linewidth": 0.8,
                     "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 200})


# ---------------------------------------------------------------- loading one iteration
def load_tcp(d, t_fail):
    """0.5 s iperf3 intervals: throughput received by the TMS and the RTT of the 4 flows.
    RTT = average of the flows weighted by the bytes they sent in the interval: a flow that
    sends nothing keeps reporting its last (stale) RTT, so it must not count."""
    j = json.load(open(f"{d}/iperf3.json"))
    t0 = (int(open(f"{d}/t_iperf").read()) - t_fail) / 1e9
    received = j.get("server_output_json", j)["intervals"]
    rows = []
    for snd, rcv in zip(j["intervals"], received):
        active = [s for s in snd["streams"] if s["bytes"] > 0]
        sent = sum(s["bytes"] for s in active)
        rows.append({"t": round(round((t0 + snd["sum"]["start"]) / BIN) * BIN, 3),
                     "tcp_mbps": rcv["sum"]["bits_per_second"] / 1e6,
                     "tcp_rtt_ms": sum(s["rtt"] * s["bytes"] for s in active) / sent / 1000 if sent else np.nan})
    return pd.DataFrame(rows).groupby("t").mean().reindex(GRID)


def load_iteration(d):
    v2i, t_fail = load_v2i(d)                       # one row per V2I packet sent (aggregate.py)
    b = np.round(np.floor(v2i.t / BIN) * BIN, 3)    # 0.5 s bin of the send time
    sent_per_s = b.value_counts().sort_index() / BIN
    surge_bins = sent_per_s[sent_per_s > SURGE_THRESHOLD].index
    surge = (surge_bins.min(), surge_bins.max() + BIN) if len(surge_bins) else None

    counts = pd.crosstab(b, v2i.status).reindex(columns=["on time", "late", "lost"], fill_value=0)
    ts = (counts.div(counts.sum(axis=1), axis=0) * 100).reindex(GRID)
    ts.columns = ["ontime", "late", "lost"]
    ts["v2i_delay_ms"] = v2i.delay_ms.groupby(b).mean().reindex(GRID)
    arrived = v2i.dropna(subset=["delay_ms"])
    b_rx = np.round(np.floor((arrived.t + arrived.delay_ms / 1000) / BIN) * BIN, 3)
    ts["v2i_mbps"] = (arrived.groupby(b_rx).size() * V2I_BYTES * 8 / BIN / 1e6).reindex(GRID).fillna(0)
    ts = ts.join(load_tcp(d, t_fail))

    after = v2i[v2i.t >= 0]
    conv = (after.rx_ns.min() - t_fail) / 1e9       # first packet over the backup
    in_surge = (lambda t: (t >= surge[0]) & (t < surge[1])) if surge else (lambda t: t != t)
    tcp_back = ts.index[(ts.index >= conv) & (ts.tcp_mbps > 0.5)]

    def v2i_shares(v):
        s = v.status.value_counts(normalize=True).reindex(["on time", "late", "lost"], fill_value=0) * 100
        return dict(zip(["ontime_pct", "late_pct", "lost_pct"], s))

    m = {"convergence_s": conv, "tcp_back_s": tcp_back.min() if len(tcp_back) else np.nan,
         "v2i_sent_after": int((v2i.t >= 0).sum()),
         "v2i_lost_after": int(((v2i.t >= 0) & (v2i.status == "lost")).sum()),
         "v2i_late_after": int(((v2i.t >= 0) & (v2i.status == "late")).sum())}
    t = ts.index.to_series()
    phases = {"pre": (v2i.t < 0, t < 0),
              "post": ((v2i.t >= 0) & ~in_surge(v2i.t), (t >= conv) & ~in_surge(t))}
    if surge:
        phases["surge"] = (in_surge(v2i.t), in_surge(t))
    for ph, (vmask, tmask) in phases.items():
        # V2I shares count every message sent in the phase (after the failure: including the outage);
        # delays and throughputs only after the traffic was rerouted
        m.update({f"v2i_{k}_{ph}": x for k, x in v2i_shares(v2i[vmask]).items()})
        sel = ts[tmask.values]
        m[f"v2i_delay_ms_{ph}"] = sel.v2i_delay_ms.mean()
        m[f"v2i_mbps_{ph}"] = sel.v2i_mbps.mean()
        m[f"tcp_mbps_{ph}"] = sel.tcp_mbps.mean()
        m[f"tcp_rtt_ms_{ph}"] = sel.tcp_rtt_ms.mean()
    lost_t = v2i.t[(v2i.status == "lost") & (v2i.t >= T0) & (v2i.t < T1)].values
    return ts, m, surge, lost_t


def load_run(run):
    iters = sorted(glob.glob(f"{run}/iter_*"), key=lambda p: int(p.rsplit("_", 1)[1]))
    series, rows, surges, lost = [], [], [], []
    for d in iters:
        try:
            ts, m, surge, lost_t = load_iteration(d)
        except Exception as e:
            print(f"  skipping {d}: {e}")
            continue
        series.append(ts)
        rows.append(m)
        surges.append(surge)
        lost.append(lost_t)
    return series, pd.DataFrame(rows), surges, lost


# ---------------------------------------------------------------- plotting
def fmt(df, col, unit="", dec=1):
    """mean (± std when there are several iterations)"""
    x = df[col].dropna()
    if x.empty:
        return "–"
    s = f"{x.mean():.{dec}f}"
    if len(x) > 1 and round(x.std(), dec) > 0:      # "± 0.0" is noise on a slide
        s += f" ± {x.std():.{dec}f}"
    return s + unit


def line_with_band(ax, series, col, color, floor=None):
    """mean over the iterations (line) and their min-max range (light band)"""
    data = pd.concat([s[col] for s in series], axis=1)
    if floor is not None:
        data = data.clip(lower=floor)
    x = np.append(data.index.values, T1)
    def step(y):
        return np.append(y.values, y.values[-1])
    if len(series) > 1:
        ax.fill_between(x, step(data.min(axis=1)), step(data.max(axis=1)), step="post",
                        color=color, alpha=0.15, lw=0)
    ax.step(x, step(data.mean(axis=1)), where="post", color=color, lw=2.2)


def log_axis(ax, lo, hi, ticks, labels):
    ax.set_yscale("log")
    ax.set_ylim(lo, hi)
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(FixedFormatter(labels))
    ax.yaxis.set_minor_locator(NullLocator())


def plot_run(config, mode, series, m, surges, path):
    surge = None
    if any(surges):
        valid = [s for s in surges if s]
        surge = (np.mean([s[0] for s in valid]), np.mean([s[1] for s in valid]))
    conv = m.convergence_s.mean()
    fig, (thr, dly) = plt.subplots(1, 2, figsize=(17, 6.8))
    fig.subplots_adjust(left=0.06, right=0.99, top=0.82, bottom=0.22, wspace=0.18)

    for ax in (thr, dly):
        ax.set_xlim(T0, T1)
        ax.set_xlabel("time since the primary link failed (s)")
        ax.grid(True)
        ax.set_axisbelow(True)
        if surge:
            ax.axvspan(*surge, color=BAND, alpha=0.18, lw=0, zorder=0)
        ax.axvline(0, color=INK, lw=1.4, zorder=5)
        ax.axvline(conv, color=INK2, lw=1.4, ls="--", zorder=5)

    # left: throughput
    thr.step([T0, 0, T1], [100, 10, 10], where="post", color=CAP, lw=3, zorder=1)
    line_with_band(thr, series, "tcp_mbps", TCP_C, floor=0.1)
    line_with_band(thr, series, "v2i_mbps", V2I_C, floor=0.1)
    log_axis(thr, 0.08, 200, [0.1, 1, 10, 100], ["≈0", "1", "10", "100"])
    thr.set_ylabel("throughput received (Mbit/s)")

    # right: delay
    dly.axhline(LATE_MS, color=LOST_C, lw=1.8, ls=":")
    line_with_band(dly, series, "tcp_rtt_ms", TCP_C)
    line_with_band(dly, series, "v2i_delay_ms", V2I_C)
    log_axis(dly, 0.3, 3000, [1, 10, 100, 1000], ["1", "10", "100", "1000"])
    dly.set_ylabel("delay (ms)")

    conv_txt = f"{conv:.2f} s" if conv < 1 else f"{conv:.1f} s"
    fig.suptitle(f"Configuration {config[1:]}: {NAMES.get(config, '')}  ·  {mode} V2I traffic  ·  "
                 f"average convergence {conv_txt}", fontsize=17, fontweight="bold", x=0.06, ha="left", y=0.975)

    # V2I messages sent after the failure: coloured pills, placed one after the other
    sent = m.v2i_sent_after.mean()
    lost = 100 * m.v2i_lost_after.mean() / sent
    late = 100 * m.v2i_late_after.mean() / sent
    renderer = fig.canvas.get_renderer()
    inv = fig.transFigure.inverted()
    x, y = 0.06, 0.885
    t = fig.text(x, y, "V2I messages after the failure:", fontsize=14, color=INK, va="center")
    x = inv.transform(t.get_window_extent(renderer))[1][0] + 0.012
    for label, val, fc, tc in (("on time", 100 - lost - late, ONTIME_C, "white"),
                               ("late", late, LATE_C, INK), ("lost", lost, LOST_C, "white")):
        t = fig.text(x, y, f"{label}  {val:.1f}%", fontsize=14, fontweight="bold", color=tc, va="center",
                     bbox=dict(boxstyle="round,pad=0.4", fc=fc, ec="none"))
        x = inv.transform(t.get_window_extent(renderer))[1][0] + 0.022

    handles = [Line2D([], [], color=TCP_C, lw=2.2, label="background TCP (throughput / round-trip time)"),
               Line2D([], [], color=V2I_C, lw=2.2, label="V2I telemetry (throughput / one-way delay)"),
               Line2D([], [], color=LOST_C, lw=1.8, ls=":", label=f"{LATE_MS} ms V2I budget"),
               Line2D([], [], color=CAP, lw=3, label="link capacity (100 → 10 Mbit/s backup)"),
               Line2D([], [], color=INK, lw=1.4, label="primary link fails"),
               Line2D([], [], color=INK2, lw=1.4, ls="--", label="traffic rerouted (average)")]
    if surge:
        handles.append(Patch(color=BAND, alpha=0.35, label="V2I surge (1800 msg/s)"))
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False, bbox_to_anchor=(0.5, 0.0),
               handlelength=2.4, columnspacing=1.8)
    fig.savefig(path)
    plt.close(fig)


def plot_surge_comparison(c3, c4, path):
    """During the surge: what strict priority (C3) and rate share (C4) give TCP and V2I."""
    panels = [("tcp_mbps_surge", "TCP throughput (Mbit/s)", TCP_C, 1),
              ("tcp_rtt_ms_surge", "TCP round-trip time (ms)", TCP_C, 0),
              ("v2i_lost_pct_surge", "V2I messages lost (%)", V2I_C, 1)]
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.2))
    fig.subplots_adjust(left=0.05, right=0.99, top=0.8, bottom=0.14, wspace=0.3)
    for ax, (col, label, color, dec) in zip(axes, panels):
        vals = [c3[col].mean(), c4[col].mean()]
        errs = [c3[col].std(), c4[col].std()]
        ax.bar([0, 1], vals, width=0.5, color=color, yerr=errs, capsize=5,
               error_kw=dict(ecolor=INK2, lw=1.2))
        top = max(vals) * 1.22 or 1
        for x, v in zip([0, 1], vals):
            ax.text(x, v + top * 0.03, f"{v:.{dec}f}", ha="center", va="bottom", fontsize=14, fontweight="bold")
        ax.set_ylim(0, top)
        ax.set_xticks([0, 1], ["Configuration 3\nstrict priority", "Configuration 4\nrate share"])
        ax.set_title(label, loc="left", fontsize=14)
        ax.grid(True, axis="y")
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", length=0)
    fig.suptitle("During the 10 s V2I surge", fontsize=17, fontweight="bold", x=0.05, ha="left", y=0.97)
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- main
def find_runs(root):
    """every folder with iter_*/ inside, newest last"""
    runs = {os.path.dirname(p) for p in glob.glob(f"{root}/**/iter_*", recursive=True) if os.path.isdir(p)}
    return sorted(runs, key=lambda r: os.path.basename(r))


def main(root):
    os.makedirs(OUT_DIR, exist_ok=True)
    chosen = {}
    for run in find_runs(root.rstrip("/")):
        config = re.match(r"(C\d)", os.path.basename(run))
        if not config:
            continue
        print(f"{run}")
        series, m, surges, _ = load_run(run)
        if not series:
            continue
        mode = "surge" if sum(bool(s) for s in surges) > len(surges) / 2 else "steady"
        key = f"{config.group(1)}_{mode}"
        if key in chosen:
            print(f"  {key}: replaces the older run {chosen[key][0]}")
        chosen[key] = (run, series, m, surges)

    rows = []
    for key in sorted(chosen):
        run, series, m, surges = chosen[key]
        config, mode = key.split("_")
        path = os.path.join(OUT_DIR, f"{key}.png")
        plot_run(config, mode, series, m, surges, path)
        stats = m.agg(["mean", "std"]).T
        row = {"figure": key, "run": os.path.relpath(run, BASE), "iterations": len(m)}
        for col in stats.index:
            row[f"{col}_mean"], row[f"{col}_std"] = stats.loc[col, "mean"], stats.loc[col, "std"]
        rows.append(row)
        print(f"  -> {os.path.relpath(path, BASE)}")
    if "C3_surge" in chosen and "C4_surge" in chosen:
        path = os.path.join(OUT_DIR, "C3_vs_C4_surge.png")
        plot_surge_comparison(chosen["C3_surge"][2], chosen["C4_surge"][2], path)
        print(f"  -> {os.path.relpath(path, BASE)}")
    pd.DataFrame(rows).round(3).to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False)
    print(f"summary -> {os.path.relpath(os.path.join(OUT_DIR, 'summary.csv'), BASE)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 final_analysis.py <results folder>   e.g. results04")
        sys.exit(1)
    main(sys.argv[1])