#!/usr/bin/env python3
"""
plot_results.py - figures for the failover experiments (run on the host).

    python3 plot_results.py shared/results/C1_2026...            # one condition
    python3 plot_results.py C1_dir C2_dir C3_dir                 # + comparison

Per run  -> RUN_DIR/plots/
    timeline_iter_NN.png   V2I delay, cumulative V2I loss and link throughput
                           around the failure (one per iteration)
    overview.png           failover time, loss per phase, delay CDF,
                           deadline exceedance across all iterations
Several runs -> ./plots_comparison/ (or --out)
    comparison.png         headline metrics per condition (mean +/- 95% CI,
                           dots = iterations) + post-reroute delay CDF

Options:
    --labels C1,C2,C3   names for the runs (default: prefix of the folder name)
    --iters 1,2         only these timelines (default: all)
    --no-timelines      skip per-iteration timelines
    --xlim=-2,5         zoom the timelines (useful for sub-second failover in C2/C3)
    --format png,pdf    output formats (pdf is handy for LaTeX)

Needs matplotlib + numpy:  sudo apt install python3-matplotlib
"""
import argparse
import glob
import json
import math
import os
import re
import statistics

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

DEADLINES = (20, 100)
BACKUP_MBPS = 10
PALETTE = ["#4c72b0", "#dd8452", "#55a868", "#c44e52", "#8172b3"]
C_V2I, C_LOST = "#4c72b0", "#c44e52"
C_PRIMARY, C_BACKUP = "#4c72b0", "#dd8452"
C_PRE, C_POST = "#7f7f7f", "#dd8452"
T95 = [None, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
       2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
       2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]

plt.rcParams.update({"figure.dpi": 110, "axes.grid": True, "grid.alpha": 0.3,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "font.size": 9})


# ------------------------------------------------------------------ loading --
def _json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def run_windows(run_dir):
    """Pre/post window lengths used by the analysis (from run_config.txt)."""
    pre, post = 10.0, 30.0
    cfg = os.path.join(run_dir, "run_config.txt")
    if os.path.exists(cfg):
        m = re.search(r"pre_window=([\d.]+)s post_window=([\d.]+)s", open(cfg).read())
        if m:
            pre, post = float(m.group(1)), float(m.group(2))
    return pre, post


def load_iteration(d, pre_w, post_w):
    snd = _json(os.path.join(d, "sender_stats.json"))
    fail = _json(os.path.join(d, "failure.json"))
    summ = _json(os.path.join(d, "summary.json"))
    pk_path = os.path.join(d, "packets.csv")
    if not (snd and fail and os.path.exists(pk_path)):
        print(f"  skip {os.path.basename(d)}: missing files")
        return None

    n, iv, s0 = snd["packets_sent"], snd["interval_ns"], snd["start_real_ns"]
    t0, t0_end = fail["t_fail_start_ns"], fail["t_fail_end_ns"]
    rate = 1e9 / iv

    try:
        arr = np.loadtxt(pk_path, delimiter=",", skiprows=1, dtype=np.int64, ndmin=2)
    except ValueError:
        arr = np.zeros((0, 3), dtype=np.int64)
    if arr.size == 0:
        arr = np.zeros((0, 3), dtype=np.int64)
    arr = arr[arr[:, 0] < n]
    seq, tx, rx = arr[:, 0], arr[:, 1], arr[:, 2]

    delay = np.full(n, np.inf)               # lost = infinite delay
    delay[seq] = (rx - tx) / 1e6             # ms
    t_rel = (s0 + np.arange(n) * iv - t0) / 1e9   # scheduled send time vs failure (s)

    post = tx > t0_end
    if post.any():
        seq_restore = int(seq[post].min())
        restore_rel = (rx[post].min() - t0) / 1e9
    else:
        seq_restore, restore_rel = n, None
    seq_fail = min(n, max(0, math.ceil((t0 - s0) / iv)))

    it = {
        "name": os.path.basename(d.rstrip("/")),
        "dir": d,
        "t0": t0,
        "t_rel": t_rel,
        "delay": delay,
        "restore_rel": restore_rel,
        "route_rel": None,
        "pre": delay[max(0, seq_fail - int(pre_w * rate)):seq_fail],
        "post": delay[seq_restore:min(n, seq_restore + int(post_w * rate))],
        "summary": summ or {},
        "link": load_link(d, t0),
    }
    if summ and summ.get("route_switch_ms") is not None:
        it["route_rel"] = summ["route_switch_ms"] / 1e3
    return it


def load_link(d, t0):
    """Throughput per link (Mbit/s) vs time relative to the failure."""
    p = os.path.join(d, "link_samples.txt")
    if os.path.exists(p):
        try:
            a = np.loadtxt(p, skiprows=1, dtype=np.int64, ndmin=2)
        except ValueError:
            a = np.zeros((0, 3), dtype=np.int64)
        if len(a) > 2:
            dt = np.diff(a[:, 0]) / 1e6                     # s
            mid = (a[1:, 0] + a[:-1, 0]) / 2 * 1e3          # ns
            ok = dt > 0
            return {"t": ((mid - t0) / 1e9)[ok],
                    "primary": (np.diff(a[:, 1]) * 8 / dt / 1e6)[ok],
                    "backup": (np.diff(a[:, 2]) * 8 / dt / 1e6)[ok],
                    "source": "r1 link counters"}
    # fallback: iperf3 server-side goodput, aligned only to the second
    j = _json(os.path.join(d, "iperf3.json"))
    try:
        ivs = (j.get("server_output_json") or j)["intervals"]
        start = j["start"]["timestamp"]["timesecs"] * 1e9
        t = np.array([(start + (x["sum"]["start"] + x["sum"]["end"]) / 2 * 1e9 - t0) / 1e9 for x in ivs])
        bw = np.array([x["sum"]["bits_per_second"] / 1e6 for x in ivs])
        return {"t": t, "background": bw, "source": "iperf3 goodput (aligned to +/-1 s)"}
    except (AttributeError, KeyError, TypeError):
        return None


def load_run(run_dir, want_iters=None):
    pre_w, post_w = run_windows(run_dir)
    its = []
    for d in sorted(glob.glob(os.path.join(run_dir, "iter_*"))):
        num = int(re.sub(r"\D", "", os.path.basename(d)) or 0)
        it = load_iteration(d, pre_w, post_w)
        if it:
            it["num"] = num
            it["plot_timeline"] = want_iters is None or num in want_iters
            its.append(it)
    return its, pre_w, post_w


# ------------------------------------------------------------------ helpers --
def mean_ci(vals):
    v = [x for x in vals if x is not None and np.isfinite(x)]
    if not v:
        return float("nan"), 0.0, 0
    m = statistics.fmean(v)
    if len(v) < 2:
        return m, 0.0, 1
    df = len(v) - 1
    t = T95[df] if df < len(T95) else 1.96
    return m, t * statistics.stdev(v) / math.sqrt(len(v)), len(v)


def cdf(ax, values, **kw):
    """Delay CDF; lost packets (inf) keep the curve below 1."""
    v = np.asarray(values, float)
    if v.size == 0:
        return
    f = np.sort(v[np.isfinite(v)])
    if f.size == 0:
        return
    y = np.arange(1, f.size + 1) / v.size
    if f.size > 4000:
        idx = np.unique(np.linspace(0, f.size - 1, 4000).astype(int))
        f, y = f[idx], y[idx]
    ax.step(f, y, where="post", **kw)


def event_lines(ax, it, post_w, label=False):
    ax.axvline(0, color="black", lw=1.2, label="failure" if label else None)
    if it["route_rel"] is not None:
        ax.axvline(it["route_rel"], color="0.4", ls="--", lw=1,
                   label="route switched to backup" if label else None)
    if it["restore_rel"] is not None:
        ax.axvline(it["restore_rel"], color="#55a868", ls="--", lw=1,
                   label="first V2I packet via backup" if label else None)
        ax.axvspan(it["restore_rel"], it["restore_rel"] + post_w, color=C_POST, alpha=0.07,
                   label=f"post-reroute window ({post_w:g} s)" if label else None)


def bar_points(ax, labels, groups, title, ylabel, scale=None):
    """Bars = mean +/- 95% CI, dots = iterations. Log scales fall back to linear
    when all values are positive and within a factor of 20 of each other."""
    rng = np.random.default_rng(0)
    allv = np.array([x for g in groups for x in g if x is not None and np.isfinite(x)], float)
    if scale and allv.size and (allv > 0).all() and allv.max() / allv.min() < 20:
        scale = None
        ylabel = ylabel.replace(" (log)", "").replace(" (symlog)", "")
    for k, vals in enumerate(groups):
        v = np.array([x for x in vals if x is not None and np.isfinite(x)], float)
        m, ci, n = mean_ci(v)
        c = PALETTE[k % len(PALETTE)]
        ax.bar(k, m, yerr=ci, color=c, alpha=0.35, edgecolor=c, capsize=5, width=0.6)
        ax.scatter(k + (rng.random(v.size) - 0.5) * 0.35, v, s=9, color=c, zorder=3)
    ax.set_xticks(range(len(labels)), labels)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    if scale == "log":
        ax.set_yscale("log")
    elif scale == "symlog":
        ax.set_yscale("symlog", linthresh=1)


def save(fig, base, formats):
    for ext in formats:
        fig.savefig(f"{base}.{ext}", bbox_inches="tight", dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------- figures --
def plot_timeline(it, label, post_w, out_base, formats, xlim=None):
    fig, ax = plt.subplots(3, 1, figsize=(10, 8), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1.4, 2]})
    x, d = it["t_rel"], it["delay"]
    ok = np.isfinite(d)
    s = it["summary"]

    # (1) one-way delay of every V2I packet, lost packets as red ticks
    ax[0].scatter(x[ok], d[ok], s=2, color=C_V2I, rasterized=True, label="V2I packet delay")
    if (~ok).any():
        ax[0].scatter(x[~ok], np.full((~ok).sum(), 0.03), marker="|", s=30, color=C_LOST,
                      transform=ax[0].get_xaxis_transform(), rasterized=True, label="V2I packet lost")
    for dl in DEADLINES:
        ax[0].axhline(dl, color="0.3", ls=":", lw=1)
        ax[0].text(0.995, dl * 1.1, f"{dl} ms budget", fontsize=8, color="0.3", ha="right",
                   transform=ax[0].get_yaxis_transform())
    ax[0].set_yscale("log")
    if ok.any():  # leave room under the data for the loss ticks
        ax[0].set_ylim(bottom=np.min(d[ok]) / 8, top=max(np.max(d[ok]), max(DEADLINES)) * 2)
    ax[0].set_ylabel("one-way delay (ms, log)")
    event_lines(ax[0], it, post_w, label=True)
    ax[0].legend(loc="lower left", bbox_to_anchor=(0, 1.01), fontsize=7, ncol=6, frameon=False)

    # (2) cumulative V2I loss
    ax[1].plot(x, np.cumsum(~ok), color=C_LOST, lw=1.5)
    ax[1].set_ylabel("cumulative\nV2I lost (pkts)")
    event_lines(ax[1], it, post_w)

    # (3) throughput
    lk = it["link"]
    if lk and "primary" in lk:
        ax[2].plot(lk["t"], lk["primary"], color=C_PRIMARY, lw=1, label="primary r1:eth1")
        ax[2].plot(lk["t"], lk["backup"], color=C_BACKUP, lw=1, label="backup r1:eth2")
        ax[2].axhline(BACKUP_MBPS, color=C_BACKUP, ls=":", lw=1, label=f"backup capacity {BACKUP_MBPS} Mbit/s")
    elif lk:
        ax[2].plot(lk["t"], lk["background"], color="0.3", lw=1, label="background goodput")
    else:
        ax[2].text(0.5, 0.5, "no throughput data", transform=ax[2].transAxes, ha="center")
    if lk:
        ax[2].legend(loc="upper right", fontsize=7, title=lk["source"], title_fontsize=7)
    ax[2].set_ylabel("throughput (Mbit/s)")
    ax[2].set_xlabel("time relative to the primary-link failure (s)")
    event_lines(ax[2], it, post_w)
    if xlim:
        ax[2].set_xlim(*xlim)

    fo = s.get("failover_ms")
    fig.suptitle(f"{label} {it['name']}: failover "
                 f"{'n/a' if fo is None else f'{fo / 1e3:.2f} s'}, "
                 f"V2I lost {s.get('v2i_lost_total', int((~ok).sum()))} "
                 f"(blackout {s.get('blackout_lost', '?')}, post {s.get('post_lost', '?')})",
                 fontsize=10)
    fig.tight_layout()
    save(fig, out_base, formats)


def plot_overview(its, label, post_w, out_base, formats):
    S = [it["summary"] for it in its]
    nums = [it["num"] for it in its]
    fig, ax = plt.subplots(2, 2, figsize=(11, 7.5))

    # (a) failover time per iteration
    a = ax[0, 0]
    fo = [s.get("failover_ms") for s in S]
    rs = [s.get("route_switch_ms") for s in S]
    a.scatter(nums, [v / 1e3 if v is not None else np.nan for v in fo], color=PALETTE[0],
              label="first V2I packet via backup (data plane)", zorder=3)
    a.scatter(nums, [v / 1e3 if v is not None else np.nan for v in rs], color="0.5", marker="x",
              label="route switched (control plane)", zorder=3)
    m, ci, _ = mean_ci(fo)
    title = "(a) Failover convergence time"
    if np.isfinite(m):
        a.axhline(m / 1e3, color=PALETTE[0], ls="--", lw=1)
        title += f": mean {m / 1e3:.2f} s ± {ci / 1e3:.2f} (95% CI)"
    a.set_title(title)
    a.set_xlabel("iteration")
    a.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    a.set_ylabel("time after failure (s)")
    a.legend(fontsize=7, loc="best")

    # (b) V2I loss by phase
    a = ax[0, 1]
    phases = [("pre", "pre_lost", C_PRE), ("blackout", "blackout_lost", C_LOST),
              ("post", "post_lost", C_POST)]
    bar_points(a, [p[0] for p in phases], [[s.get(p[1]) for s in S] for p in phases],
               "(b) V2I packets lost, by phase", "packets (symlog)", scale="symlog")

    # (c) delay CDF, pooled over iterations
    a = ax[1, 0]
    cdf(a, np.concatenate([it["pre"] for it in its]), color=C_PRE, label="pre-failure")
    cdf(a, np.concatenate([it["post"] for it in its]), color=C_POST, label=f"post-reroute ({post_w:g} s)")
    for dl in DEADLINES:
        a.axvline(dl, color="0.3", ls=":", lw=1)
    a.set_xscale("log")
    a.set_ylim(0, 1.02)
    a.set_title("(c) V2I delay CDF (lost packets = never arrive)")
    a.set_xlabel("one-way delay (ms, log)")
    a.set_ylabel("fraction of packets")
    a.legend(fontsize=8)

    # (d) deadline exceedance
    a = ax[1, 1]
    w = 0.35
    for j, (ph, col) in enumerate([("pre", C_PRE), ("post", C_POST)]):
        for k, dl in enumerate(DEADLINES):
            m, ci, _ = mean_ci([s.get(f"{ph}_exceed_{dl}ms_pct") for s in S])
            a.bar(k + (j - 0.5) * w, m, w, yerr=ci, capsize=4, color=col, alpha=0.8,
                  label=ph if k == 0 else None)
    a.set_xticks(range(len(DEADLINES)), [f"> {dl} ms or lost" for dl in DEADLINES])
    a.set_ylabel("% of V2I packets")
    a.set_title("(d) Deadline exceedance (mean ± 95% CI)")
    a.legend(fontsize=8)

    fig.suptitle(f"{label}: {len(its)} iterations", fontsize=11)
    fig.tight_layout()
    save(fig, out_base, formats)


def plot_comparison(runs, out_base, formats):
    labels = [r["label"] for r in runs]
    get = lambda key: [[it["summary"].get(key) for it in r["its"]] for r in runs]  # noqa: E731
    fig, ax = plt.subplots(2, 3, figsize=(13, 7.5))
    bar_points(ax[0, 0], labels, [[v / 1e3 if v is not None else None for v in g] for g in get("failover_ms")],
               "(a) Failover convergence time", "s (log)", scale="log")
    bar_points(ax[0, 1], labels, get("blackout_lost"), "(b) V2I lost during blackout",
               "packets (symlog)", scale="symlog")
    bar_points(ax[0, 2], labels, get("post_lost"), "(c) V2I lost on backup (post-reroute)",
               "packets (symlog)", scale="symlog")
    bar_points(ax[1, 0], labels, get("post_delay_p99_ms"), "(d) V2I p99 delay post-reroute",
               "ms (log)", scale="log")
    bar_points(ax[1, 1], labels, get(f"post_exceed_{DEADLINES[0]}ms_pct"),
               f"(e) V2I lost or > {DEADLINES[0]} ms, post-reroute", "% of packets")
    a = ax[1, 2]
    for k, r in enumerate(runs):
        cdf(a, np.concatenate([it["post"] for it in r["its"]]), color=PALETTE[k % len(PALETTE)],
            label=r["label"])
    for dl in DEADLINES:
        a.axvline(dl, color="0.3", ls=":", lw=1)
    a.set_xscale("log")
    a.set_ylim(0, 1.02)
    a.set_title("(f) V2I delay CDF post-reroute")
    a.set_xlabel("one-way delay (ms, log)")
    a.legend(fontsize=8)
    fig.suptitle("Comparison across conditions (bars: mean ± 95% CI, dots: iterations)", fontsize=11)
    fig.tight_layout()
    save(fig, out_base, formats)


# ---------------------------------------------------------------------- main --
def main():
    p = argparse.ArgumentParser(description="Plot failover experiment results")
    p.add_argument("runs", nargs="+", help="run directories (e.g. shared/results/C1_...)")
    p.add_argument("--labels", help="comma-separated names for the runs")
    p.add_argument("--iters", help="comma-separated iteration numbers for timelines")
    p.add_argument("--no-timelines", action="store_true")
    p.add_argument("--xlim", help="zoom timelines, e.g. --xlim=-2,5 (seconds around the failure)")
    p.add_argument("--format", default="png", help="png, pdf or png,pdf")
    p.add_argument("--out", default="plots_comparison", help="folder for the comparison figure")
    args = p.parse_args()

    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    labels = args.labels.split(",") if args.labels else \
        [os.path.basename(r.rstrip("/")).split("_")[0] for r in args.runs]
    if len(labels) != len(args.runs):
        p.error("--labels needs one name per run")
    want = {int(x) for x in args.iters.split(",")} if args.iters else None
    xlim = tuple(float(v) for v in args.xlim.split(",")) if args.xlim else None

    runs = []
    for run_dir, label in zip(args.runs, labels):
        print(f"{label}: {run_dir}")
        its, _pre_w, post_w = load_run(run_dir, want)
        if not its:
            print("  no usable iterations")
            continue
        out = os.path.join(run_dir, "plots")
        os.makedirs(out, exist_ok=True)
        if not args.no_timelines:
            for it in its:
                if it["plot_timeline"]:
                    name = f"timeline_{it['name']}" + ("_zoom" if xlim else "")
                    plot_timeline(it, label, post_w, os.path.join(out, name), formats, xlim)
        plot_overview(its, label, post_w, os.path.join(out, "overview"), formats)
        print(f"  {len(its)} iterations -> {out}/")
        runs.append({"label": label, "its": its})

    if len(runs) > 1:
        os.makedirs(args.out, exist_ok=True)
        plot_comparison(runs, os.path.join(args.out, "comparison"), formats)
        print(f"comparison -> {args.out}/")


if __name__ == "__main__":
    main()