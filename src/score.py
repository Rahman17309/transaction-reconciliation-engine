"""Score the reconciliation engine against the injected ground truth.

The engine (src/clean.py + sql/) never reads data/truth/labels.csv. This script is the only
place the labels are used. For every injected problem type it compares the set of keys the
engine flagged with the set of keys that were really affected, and reports precision, recall
and F1 with Wilson 95% intervals.

Two views are reported:
  raw       every label counts, wherever it falls in time
  in_scope  labels on activity outside October in IST (e.g. a wallet overcharge on a 1 Nov
            capture) are dropped, because the engine correctly classes that activity as
            next-period timing. Each dropped label is listed in score_detail with a reason.

Usage:  python src/score.py            -> data/output/scorecard.csv, score_detail.csv
        DATA_DIR=/some/dir python src/score.py   (score another generated run, see robustness.py)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))

# truth issue -> (label shown in reports, engine stage that should catch it)
ISSUES = {
    "duplicate_posting":                 ("Duplicate ERP posting",        "cleaning"),
    "duplicate_export_row":              ("Duplicate gateway export row", "cleaning"),
    "rounding_within_tolerance":         ("Rounding within ±₹1 (auto-clear)", "leg 1"),
    "amount_mismatch":                   ("Amount mismatch",              "leg 1"),
    "ledger_only_payment_failed":        ("Booked, payment failed",       "leg 1"),
    "ledger_only_no_gateway_record":     ("Booked, no gateway record",    "leg 1"),
    "gateway_only_not_booked":           ("Captured, not booked",         "leg 1"),
    "refund_not_in_ledger":              ("Refund with no credit note",   "refunds"),
    "fee_overcharge_card":               ("Card fee above contract",      "fee audit"),
    "fee_overcharge_wallet_rate_change": ("Wallet MDR step-up",           "fee audit"),
    "chargeback_deducted":               ("Chargeback not booked",        "leg 2"),
    "settlement_held_by_gateway":        ("Settlement held by gateway",   "leg 3"),
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n (better than normal approx near 0 or 1)."""
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    den = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def predictions(con: duckdb.DuckDBPyConnection, clean: Path) -> pd.DataFrame:
    """One row per (issue, key) the engine flagged, using only engine outputs and cleaning logs."""
    q = pd.read_parquet(clean / "quarantine.parquet")
    parts = [
        pd.DataFrame({"issue": "duplicate_posting",
                      "key": q.loc[q.reason == "duplicate_posting", "order_id"]}),
        pd.DataFrame({"issue": "duplicate_export_row",
                      "key": q.loc[q.reason == "duplicate_export_row", "txn_id"]}),
    ]
    rule_map = {
        "R2_tolerance": "rounding_within_tolerance", "R3_amount_mismatch": "amount_mismatch",
        "R4_payment_failed": "ledger_only_payment_failed", "R7_no_gateway_record": "ledger_only_no_gateway_record",
        "R8_not_booked": "gateway_only_not_booked",
    }
    l1 = con.sql("SELECT rule, order_id AS key FROM leg1_match").df()
    l1 = l1[l1.rule.isin(rule_map)]
    parts.append(pd.DataFrame({"issue": l1.rule.map(rule_map), "key": l1.key}))
    parts.append(con.sql("SELECT 'refund_not_in_ledger' AS issue, order_id AS key FROM refund_match "
                         "WHERE rule = 'F3_no_credit_note'").df())
    parts.append(con.sql("""
        SELECT CASE method WHEN 'Card' THEN 'fee_overcharge_card'
                           WHEN 'Wallet' THEN 'fee_overcharge_wallet_rate_change'
                           ELSE 'fee_overcharge_' || lower(method) END AS issue,
               order_id AS key
        FROM fee_audit WHERE is_overcharged""").df())
    # Chargebacks are only visible at payout level, so they are scored per settlement.
    parts.append(con.sql("SELECT 'chargeback_deducted' AS issue, settlement_id AS key FROM chargebacks").df())
    parts.append(con.sql("SELECT 'settlement_held_by_gateway' AS issue, settlement_id AS key "
                         "FROM leg3_payout_bank WHERE rule = 'S4_overdue'").df())
    return pd.concat(parts, ignore_index=True).drop_duplicates()


def out_of_scope(con: duckdb.DuckDBPyConnection, truth: pd.DataFrame) -> pd.Series:
    """Reason a label is outside what the October-IST review can or should flag ('' = in scope).

    Uses only engine tables. A label is out of scope when the activity it describes is not
    October activity in IST, or when the source row is not in the files finance received.
    """
    gw = con.sql("SELECT order_id, type, in_period_ist FROM gw").df()
    pays = gw[gw.type == "payment"]
    refs = gw[gw.type == "refund"]
    pay_out = set(pays.loc[~pays.in_period_ist.astype(bool), "order_id"])
    ref_out = set(refs.loc[~refs.in_period_ist.astype(bool), "order_id"])
    pay_in, ref_in = set(pays.order_id), set(refs.order_id)
    erp = set(con.sql("SELECT order_id FROM erp_receipts").df().order_id)
    cut_erp = set(con.sql("SELECT order_id FROM leg1_match WHERE rule = 'R5_cutoff_erp'").df().order_id)
    gw_side = {"fee_overcharge_card", "fee_overcharge_wallet_rate_change", "gateway_only_not_booked",
               "amount_mismatch", "rounding_within_tolerance", "ledger_only_payment_failed"}
    erp_side = {"ledger_only_no_gateway_record", "ledger_only_payment_failed", "amount_mismatch",
                "rounding_within_tolerance"}

    def why(r):
        if r.issue in erp_side and r.key not in erp:
            return "order dated before 1 Oct IST, not in the October ledger"
        if r.issue == "ledger_only_no_gateway_record" and r.key in cut_erp:
            return "posted before 05:30 IST on 1 Oct (cut-off window, timing)"
        if r.issue in gw_side and r.key in pay_out:
            return "capture is 1 Nov in IST (next period)"
        if r.issue in gw_side and r.key not in pay_in:
            return "capture outside the UTC-dated export (before 05:30 IST 1 Oct or after 05:30 IST 1 Nov)"
        if r.issue == "refund_not_in_ledger" and r.key in ref_out:
            return "refund is 1 Nov in IST (next period)"
        if r.issue == "refund_not_in_ledger" and r.key not in ref_in:
            return "refund processed after the export window (November)"
        return ""
    return truth.apply(why, axis=1)


def score(data_dir: Path = DATA) -> tuple[pd.DataFrame, pd.DataFrame]:
    sys.path.insert(0, str(ROOT / "src"))
    import match  # noqa: E402  (engine builder; reads only data/clean)
    clean = data_dir / "clean"
    con = match.build(":memory:", clean_dir=clean)

    truth = pd.read_csv(data_dir / "truth" / "labels.csv")
    truth = truth[["issue", "key"]].drop_duplicates()   # chargebacks: several per settlement
    truth["scope_note"] = out_of_scope(con, truth)
    pred = predictions(con, clean)
    # what the engine actually did with each key (for explaining misses)
    rules = pd.concat([
        con.sql("SELECT order_id AS key, rule FROM leg1_match").df(),
        con.sql("SELECT order_id AS key, rule FROM refund_match").df(),
    ]).groupby("key").rule.agg(lambda x: " + ".join(sorted(set(x))))
    con.close()

    det = truth.merge(pred.assign(flagged=True), on=["issue", "key"], how="outer")
    det["labelled"] = det.scope_note.notna()
    det["flagged"] = det.flagged.fillna(False).astype(bool)
    det["scope_note"] = det.scope_note.fillna("")
    det["outcome"] = np.select(
        [det.labelled & det.flagged, det.labelled & ~det.flagged, ~det.labelled & det.flagged],
        ["TP", "FN", "FP"], "TN")

    rows = []
    for issue, (label, stage) in ISSUES.items():
        d = det[det.issue == issue]
        for view, dd in (("raw", d), ("in_scope", d[d.scope_note == ""])):
            tp, fn, fp = [(dd.outcome == o).sum() for o in ("TP", "FN", "FP")]
            prec = tp / (tp + fp) if tp + fp else np.nan
            rec = tp / (tp + fn) if tp + fn else np.nan
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else np.nan
            plo, phi = wilson(tp, tp + fp)
            rlo, rhi = wilson(tp, tp + fn)
            rows.append(dict(issue=issue, label=label, stage=stage, view=view,
                             labelled=tp + fn, flagged=tp + fp, tp=tp, fp=fp, fn=fn,
                             precision=prec, precision_lo=plo, precision_hi=phi,
                             recall=rec, recall_lo=rlo, recall_hi=rhi, f1=f1))
    card = pd.DataFrame(rows)
    # Micro-average over all types (each labelled key counts once)
    for view in ("raw", "in_scope"):
        c = card[card.view == view]
        tp, fp, fn = c.tp.sum(), c.fp.sum(), c.fn.sum()
        prec, rec = tp / (tp + fp), tp / (tp + fn)
        plo, phi = wilson(tp, tp + fp)
        rlo, rhi = wilson(tp, tp + fn)
        card.loc[len(card)] = dict(issue="ALL", label="All types (micro-average)", stage="all", view=view,
                                   labelled=tp + fn, flagged=tp + fp, tp=tp, fp=fp, fn=fn,
                                   precision=prec, precision_lo=plo, precision_hi=phi,
                                   recall=rec, recall_lo=rlo, recall_hi=rhi,
                                   f1=2 * prec * rec / (prec + rec))
    det = det.sort_values(["issue", "outcome", "key"]).reset_index(drop=True)
    det["engine_rule"] = det.key.map(rules)
    return card, det


def main() -> int:
    card, det = score(DATA)
    out = DATA / "output"
    card.to_csv(out / "scorecard.csv", index=False)
    det[det.outcome != "TP"].to_csv(out / "score_detail.csv", index=False)
    pd.set_option("display.width", 200)
    show = card[["label", "view", "labelled", "flagged", "tp", "fp", "fn", "precision", "recall", "f1"]]
    print(show.round(4).to_string(index=False))
    print("\nNon-TP labels by reason:")
    print(det[det.outcome != "TP"].fillna({"engine_rule": "-"})
          .groupby(["issue", "outcome", "scope_note", "engine_rule"]).size().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
