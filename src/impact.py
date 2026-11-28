"""₹ impact sizing on top of the reconciliation engine.

Usage:
  python src/impact.py              -> data/output/impact_lines.csv, impact_summary.csv, fee_run_rate.csv
  python src/impact.py --months 20  -> also re-sizes 20 regenerated months (seeds 101-120) and writes
                                       data/output/impact_months.csv + impact_annual_range.csv

The impact model is SQL (impact/impact.sql) run on the engine tables. It reads no truth labels.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import match  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
OUT = ROOT / "data" / "output"
SQL = ROOT / "impact" / "impact.sql"
SEEDS_FROM = 101
N_BOOT = 10_000


def size(clean_dir: Path) -> dict[str, pd.DataFrame]:
    con = match.build(clean_dir=clean_dir)
    con.execute(SQL.read_text())
    res = {t: con.execute(f"SELECT * FROM {t}").df() for t in ("impact_lines", "impact_summary", "fee_run_rate")}
    con.close()
    return res


def month_metrics(res: dict[str, pd.DataFrame]) -> dict[str, float]:
    """One row per month: the quantities the annual range is built from."""
    s, lines, fee = res["impact_summary"].iloc[0], res["impact_lines"], res["fee_run_rate"]
    c = lines[lines.section.isin(["C", "D"])]
    get = lambda pat: float(c.loc[c.line.str.startswith(pat), "month_inr"].sum())
    return {
        "erp_net_sales": s.erp_net_sales,
        "cash_to_recover": s.cash_to_recover,
        "net_revenue_overstatement": s.net_revenue_overstatement,
        "overstatement_share": s.overstatement_share,
        "is_material": bool(s.is_material),
        "fee_leakage_annual": s.fee_leakage_annual,
        "fee_methods_flagged": int(len(fee)),
        "unpaid_receipts_month": get("Receipts with no captured payment"),
        "chargebacks_month": get("Chargebacks"),
        "held_payout_cost": get("Cost of funds"),
        "breaks_over_14d_inr": s.breaks_over_14d_inr,
    }


def months(n: int) -> pd.DataFrame:
    rows = []
    for seed in range(SEEDS_FROM, SEEDS_FROM + n):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "SEED": str(seed), "DATA_DIR": d}
            for script in ("generate_data.py", "clean.py"):
                r = subprocess.run([sys.executable, str(ROOT / "src" / script)], env=env, capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"seed {seed}: {script} failed\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
            rows.append({"seed": seed, **month_metrics(size(Path(d) / "clean"))})
        print(f"seed {seed}: overstatement {rows[-1]['overstatement_share']:.3%}, "
              f"fee leakage/yr ₹{rows[-1]['fee_leakage_annual']:,.0f}", flush=True)
    return pd.DataFrame(rows)


def annual_range(m: pd.DataFrame) -> pd.DataFrame:
    """Annualised exposure of the recurring lines: mean month x 12 (fee leakage is already annual),
    with a bootstrap 95% interval on the mean and the min-max of single months."""
    rng = np.random.default_rng(7)
    spec = [("Fee overcharge at current rates", "fee_leakage_annual", 1),
            ("Receipts with no captured payment (revenue at risk)", "unpaid_receipts_month", 12),
            ("Chargebacks", "chargebacks_month", 12),
            ("Revenue overstated if closed unreconciled (misstatement)", "net_revenue_overstatement", 12)]
    out = []
    for name, col, k in spec:
        x = m[col].to_numpy() * k
        boots = rng.choice(x, size=(N_BOOT, len(x)), replace=True).mean(axis=1)
        out.append({"line": name, "annual_mean_inr": x.mean(),
                    "ci95_lo_inr": np.percentile(boots, 2.5), "ci95_hi_inr": np.percentile(boots, 97.5),
                    "min_month_x12_inr": x.min(), "max_month_x12_inr": x.max(), "months": len(x)})
    return pd.DataFrame(out)


def main() -> int:
    res = size(DATA / "clean")
    OUT.mkdir(parents=True, exist_ok=True)
    for t, df in res.items():
        df.to_csv(OUT / f"{t}.csv", index=False)
    pd.set_option("display.width", 200); pd.set_option("display.max_colwidth", 60)
    print(res["impact_lines"][["section", "line", "month_inr", "annual_inr", "basis"]].round(2).to_string(index=False))
    print(res["impact_summary"].T.to_string(header=False))
    if "--months" in sys.argv:
        n = int(sys.argv[sys.argv.index("--months") + 1])
        m = months(n)
        m.to_csv(OUT / "impact_months.csv", index=False)
        a = annual_range(m)
        a.to_csv(OUT / "impact_annual_range.csv", index=False)
        print(a.round(0).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
