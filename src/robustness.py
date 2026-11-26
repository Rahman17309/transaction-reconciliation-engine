"""Is the engine's score a property of the rules, or of seed 42?

Regenerates the whole month with N different random seeds (same documented injection rates,
different orders, amounts, timings and which rows get broken), then runs the full pipeline on
each (clean -> SQL engine -> integrity checks -> score) in a throw-away directory.
The rules are NOT re-tuned between seeds.

Usage:  python src/robustness.py [n_seeds]     -> data/output/robustness.csv
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output"
SEEDS_FROM = 101   # seed 42 is the published run; robustness uses 101, 102, ...


def run(seed: int, workdir: Path) -> pd.DataFrame:
    env = {**os.environ, "SEED": str(seed), "DATA_DIR": str(workdir)}
    for script in ("generate_data.py", "clean.py", "match.py"):
        r = subprocess.run([sys.executable, str(ROOT / "src" / script)], env=env,
                           capture_output=True, text=True)
        if script == "match.py":
            checks_ok = r.returncode == 0          # match.py exits 1 if any integrity check fails
        elif r.returncode != 0:
            raise RuntimeError(f"seed {seed}: {script} failed\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    r = subprocess.run([sys.executable, str(ROOT / "src" / "score.py")], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"seed {seed}: score.py failed\n{r.stderr[-2000:]}")
    card = pd.read_csv(workdir / "output" / "scorecard.csv")
    bridge = pd.read_csv(workdir / "output" / "cash_bridge.csv")
    card["seed"] = seed
    card["integrity_checks_pass"] = checks_ok
    card["bridge_residual_inr"] = float(bridge.loc[bridge.kind == "residual", "amount_inr"].iloc[0])
    # how many in-scope misses were parked as CN-pending timing (the deliberate trade-off)
    det = pd.read_csv(workdir / "output" / "score_detail.csv", keep_default_na=False)
    fn = det[(det.outcome == "FN") & (det.scope_note == "")]
    card["fn_in_cn_pending"] = 0
    card.loc[card.issue == "refund_not_in_ledger", "fn_in_cn_pending"] = int(
        (fn.issue.eq("refund_not_in_ledger") & fn.engine_rule.str.contains("F5_cn_pending")).sum())
    return card


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    frames = []
    for seed in range(SEEDS_FROM, SEEDS_FROM + n):
        with tempfile.TemporaryDirectory() as d:
            frames.append(run(seed, Path(d)))
        c = frames[-1]
        a = c[(c.issue == "ALL") & (c.view == "in_scope")].iloc[0]
        print(f"seed {seed}: in-scope precision {a.precision:.4f} recall {a.recall:.4f} "
              f"| checks {'pass' if c.integrity_checks_pass.iloc[0] else 'FAIL'} "
              f"| bridge residual ₹{c.bridge_residual_inr.iloc[0]:.2f}", flush=True)
    res = pd.concat(frames, ignore_index=True)
    res.to_csv(OUT / "robustness.csv", index=False)
    s = (res[res.view == "in_scope"].groupby("label", sort=False)
         .agg(min_precision=("precision", "min"), mean_precision=("precision", "mean"),
              min_recall=("recall", "min"), mean_recall=("recall", "mean")))
    print(s.round(4).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
