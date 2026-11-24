"""Cleaning pipeline: raw month-end files -> analysis-ready tables.

Every rule is logged to data/clean/dq_log.csv with the number of rows it touched,
and rows that are removed are kept in data/clean/quarantine.parquet with a reason,
so nothing disappears silently. Validation checks run at the end and are written
to data/clean/validation_report.csv; the script exits non-zero if any check fails.

Run:  python src/clean.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
CLEAN = ROOT / "data" / "clean"
PERIOD_START = pd.Timestamp("2026-10-01")
PERIOD_END = pd.Timestamp("2026-11-01")  # exclusive
ORDER_RE = re.compile(r"^ORD-\d{7}$")


class DQLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, table: str, rule: str, n: int, action: str) -> None:
        self.rows.append({"rule_id": f"R{len(self.rows) + 1:02d}", "table": table,
                          "rule": rule, "rows_affected": int(n), "action": action})

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def normalise_ref(x: str) -> str:
    """' ord1000377 ', 'ORD_1000377', 'ord-1000377' -> 'ORD-1000377'."""
    s = str(x).strip().upper().replace("_", "-")
    m = re.match(r"^ORD-?(\d+)$", s)
    return f"ORD-{m.group(1)}" if m else s


def parse_erp_ts(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    for fmt in ("%d/%m/%Y %H:%M", "%d-%b-%Y %I:%M %p"):
        miss = out.isna()
        out[miss] = pd.to_datetime(s[miss], format=fmt, errors="coerce")
    return out


def clean_ledger(log: DQLog, quarantine: list[pd.DataFrame]) -> pd.DataFrame:
    df = pd.read_csv(RAW / "erp_ledger.csv", dtype=str, keep_default_na=False)
    n0 = len(df)

    df["posting_ts_ist"] = parse_erp_ts(df["posting_ts"])
    n_alt = (~df["posting_ts"].str.match(r"^\d{4}-\d{2}-\d{2} ")).sum()
    log.add("ledger", "Parse 3 mixed timestamp formats (ISO, dd/mm/yyyy, dd-Mon-yyyy 12h)", n_alt, "standardised")
    assert df["posting_ts_ist"].notna().all(), "unparsed ERP timestamps"

    has_comma = df["amount_inr"].str.contains(",")
    df["amount_inr"] = df["amount_inr"].str.replace(",", "", regex=False).astype(float)
    log.add("ledger", "Strip thousands separators from text amounts", has_comma.sum(), "standardised")

    blank = df["payment_mode"].eq("")
    df.loc[blank, "payment_mode"] = "Unknown"
    df["payment_mode"] = df["payment_mode"].replace({"Net Banking": "NetBanking"})
    log.add("ledger", "Blank payment mode (manual journal) set to 'Unknown'", blank.sum(), "flagged")

    test = df["order_ref"].str.startswith("TEST") | df["customer_id"].eq("C_TEST")
    quarantine.append(df[test].assign(source="ledger", reason="test_row"))
    log.add("ledger", "Remove test postings (TEST- refs / C_TEST customer)", test.sum(), "quarantined")
    df = df[~test].copy()

    df["order_id"] = df["order_ref"].map(normalise_ref)
    # duplicate receipts: same order, same type, same amount, different voucher numbers
    df = df.sort_values("voucher_no")
    dup = df.duplicated(["order_id", "entry_type", "amount_inr"], keep="first")
    quarantine.append(df[dup].assign(source="ledger", reason="duplicate_posting"))
    log.add("ledger", "Remove duplicate postings (same order, type and amount; later voucher kept out)",
            dup.sum(), "quarantined")
    df = df[~dup].copy()

    out = (df["posting_ts_ist"] < PERIOD_START) | (df["posting_ts_ist"] >= PERIOD_END)
    log.add("ledger", "Postings outside October IST", out.sum(), "checked (none expected)")

    df = df.rename(columns={"sales_channel": "channel"})
    cols = ["voucher_no", "entry_type", "posting_ts_ist", "order_id", "customer_id",
            "payment_mode", "channel", "amount_inr"]
    log.add("ledger", f"Rows in -> out ({n0:,} -> {len(df):,})", n0 - len(df), "summary")
    return df[cols].sort_values("posting_ts_ist").reset_index(drop=True)


def clean_gateway(log: DQLog, quarantine: list[pd.DataFrame]) -> pd.DataFrame:
    df = pd.read_csv(RAW / "gateway_txns.csv", dtype={"settlement_id": str}, keep_default_na=False)
    n0 = len(df)

    dup = df.duplicated(keep="first")
    quarantine.append(df[dup].astype(str).assign(source="gateway", reason="duplicate_export_row"))
    log.add("gateway", "Remove exact duplicate export rows", dup.sum(), "quarantined")
    df = df[~dup].copy()
    assert df["txn_id"].is_unique

    norm = df["merchant_ref"].map(normalise_ref)
    log.add("gateway", "Normalise merchant_ref (case, spaces, '_' and missing '-')",
            (norm != df["merchant_ref"]).sum(), "standardised")
    df["order_id"] = norm

    df["ts_utc"] = pd.to_datetime(df["created_at_utc"], format="%Y-%m-%dT%H:%M:%SZ")
    df["ts_ist"] = df["ts_utc"] + pd.Timedelta(hours=5, minutes=30)
    shifted = df["ts_utc"].dt.date != df["ts_ist"].dt.date
    log.add("gateway", "Convert UTC to IST (rows whose calendar date changes)", shifted.sum(), "standardised")

    df["method"] = df["method"].str.strip().str.lower().map(
        {"upi": "UPI", "card": "Card", "netbanking": "NetBanking", "wallet": "Wallet"})
    for c in ("amount", "fee", "gst"):
        df[f"{c}_inr"] = df[f"{c}_paise"] / 100
    log.add("gateway", "Convert paise to rupees (amount, fee, GST)", len(df), "standardised")

    out = (df["ts_ist"] < PERIOD_START) | (df["ts_ist"] >= PERIOD_END)
    df["in_period_ist"] = ~out
    log.add("gateway", "UTC-dated export rows that fall on 1 Nov IST (out of October period)",
            out.sum(), "flagged, kept for cut-off analysis")

    failed = df["status"].eq("failed")
    log.add("gateway", "Failed payment attempts (no money moved)", failed.sum(), "flagged")
    df["settlement_id"] = df["settlement_id"].replace("", np.nan)

    cols = ["txn_id", "order_id", "merchant_ref", "type", "status", "method", "ts_utc", "ts_ist",
            "amount_inr", "fee_inr", "gst_inr", "settlement_id", "in_period_ist"]
    log.add("gateway", f"Rows in -> out ({n0:,} -> {len(df):,})", n0 - len(df), "summary")
    return df[cols].sort_values("ts_ist").reset_index(drop=True)


def clean_payouts(log: DQLog) -> pd.DataFrame:
    df = pd.read_csv(RAW / "gateway_payouts.csv")
    df["settlement_date"] = pd.to_datetime(df["settlement_date"])
    df["batch_date"] = pd.to_datetime(df["settlement_id"].str[5:], format="%Y%m%d")
    for c in ("gross", "fee", "gst", "refund", "chargeback", "net"):
        df[f"{c}_inr"] = df[f"{c}_paise"] / 100
    log.add("payouts", "Convert paise to rupees; derive batch date from settlement_id", len(df), "standardised")
    cb = df["chargeback_inr"] > 0
    log.add("payouts", "Settlements with chargeback deductions", cb.sum(), "flagged")
    df["lag_days"] = (df["settlement_date"] - df["batch_date"]).dt.days
    return df[["settlement_id", "batch_date", "settlement_date", "lag_days", "utr", "gross_inr", "fee_inr",
               "gst_inr", "refund_inr", "chargeback_inr", "net_inr"]]


def clean_bank(log: DQLog) -> pd.DataFrame:
    df = pd.read_csv(RAW / "bank_statement.csv")
    df["value_date"] = pd.to_datetime(df["value_date"], format="%d-%m-%Y")
    log.add("bank", "Parse dd-mm-yyyy value dates", len(df), "standardised")
    df["utr"] = df["narration"].str.extract(r"(PFLX\d{10})", expand=False)
    cat = np.select(
        [df["narration"].str.contains("PAYFLUX"), df["narration"].str.contains("COD REMIT"),
         df["narration"].str.contains("VENDOR"), df["narration"].str.contains("SALARY"),
         df["narration"].str.contains("GST"), df["narration"].str.contains("RENT")],
        ["gateway_settlement", "cod_remittance", "vendor_payment", "salary", "tax", "rent"], "bank_charges")
    df["category"] = cat
    log.add("bank", "Classify narration and extract PayFlux UTR", df["utr"].notna().sum(), "enriched")
    return df


def validate(ledger, gw, payouts, bank) -> pd.DataFrame:
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    chk("ledger voucher_no unique", ledger["voucher_no"].is_unique)
    chk("ledger order_id matches ORD-nnnnnnn", ledger["order_id"].str.match(ORDER_RE).all())
    chk("ledger postings all in October IST",
        ledger["posting_ts_ist"].between(PERIOD_START, PERIOD_END, inclusive="left").all())
    chk("ledger receipts positive, credit notes negative",
        (ledger.loc[ledger.entry_type == "receipt", "amount_inr"] > 0).all()
        and (ledger.loc[ledger.entry_type == "credit_note", "amount_inr"] < 0).all())
    chk("ledger one receipt per order", not ledger[ledger.entry_type == "receipt"]["order_id"].duplicated().any())
    chk("ledger no nulls in key columns", ledger[["order_id", "amount_inr", "posting_ts_ist"]].notna().all().all())
    chk("gateway txn_id unique", gw["txn_id"].is_unique)
    chk("gateway order_id matches ORD-nnnnnnn", gw["order_id"].str.match(ORDER_RE).all())
    chk("gateway method mapped", gw["method"].notna().all())
    pay = gw[(gw.type == "payment") & (gw.status == "captured")]
    chk("gateway fee between 0% and 3% of amount", (pay.fee_inr / pay.amount_inr).between(0, 0.03).all())
    gst_err = (pay.gst_inr - (pay.fee_inr * 0.18)).abs()
    chk("gateway GST = 18% of fee (+-1 paise)", (gst_err <= 0.011).all(), f"max diff Rs {gst_err.max():.3f}")
    chk("gateway failed rows have no fee", (gw.loc[gw.status == "failed", "fee_inr"] == 0).all())
    chk("gateway failed rows have no settlement", gw.loc[gw.status == "failed", "settlement_id"].isna().all())
    calc = payouts.gross_inr - payouts.fee_inr - payouts.gst_inr - payouts.refund_inr - payouts.chargeback_inr
    chk("payout net = gross - fee - GST - refunds - chargebacks", (calc - payouts.net_inr).abs().max() < 0.01)
    chk("payout settlement_id unique", payouts["settlement_id"].is_unique)
    # batches fully inside the UTC-dated report: 2-31 Oct IST (1 Oct 00:00-05:30 IST sits in 30 Sep UTC)
    inside = payouts[(payouts.batch_date >= "2026-10-02") & (payouts.batch_date <= "2026-10-31")]
    g_sum = (gw[gw.status.isin(["captured"]) & (gw.type == "payment")]
             .groupby("settlement_id")["amount_inr"].sum())
    gross_diff = (inside.set_index("settlement_id")["gross_inr"] - g_sum.reindex(inside.settlement_id)).abs()
    chk("payout gross = sum of captured txns (2-31 Oct batches)", (gross_diff < 0.01).all(),
        f"{len(inside)} batches compared")
    run = 2_500_000 + (bank.credit - bank.debit).cumsum()
    chk("bank running balance recomputes", (run - bank.balance).abs().max() < 0.01)
    chk("bank every PayFlux credit has a UTR",
        bank.loc[bank.category == "gateway_settlement", "utr"].notna().all())
    chk("bank no row has both credit and debit", ((bank.credit > 0) & (bank.debit > 0)).sum() == 0)
    return pd.DataFrame(checks)


def main() -> int:
    CLEAN.mkdir(parents=True, exist_ok=True)
    log = DQLog()
    quarantine: list[pd.DataFrame] = []
    ledger = clean_ledger(log, quarantine)
    gw = clean_gateway(log, quarantine)
    payouts = clean_payouts(log)
    bank = clean_bank(log)

    ledger.to_parquet(CLEAN / "ledger.parquet", index=False)
    gw.to_parquet(CLEAN / "gateway_txns.parquet", index=False)
    payouts.to_parquet(CLEAN / "payouts.parquet", index=False)
    bank.to_parquet(CLEAN / "bank.parquet", index=False)
    q = pd.concat(quarantine, ignore_index=True).astype(str)
    q.to_parquet(CLEAN / "quarantine.parquet", index=False)
    log.frame().to_csv(CLEAN / "dq_log.csv", index=False)

    rep = validate(ledger, gw, payouts, bank)
    rep.to_csv(CLEAN / "validation_report.csv", index=False)
    pd.set_option("display.width", 160, "display.max_colwidth", 90)
    print(log.frame().to_string(index=False))
    print()
    print(rep.to_string(index=False))
    print(f"\n{rep.passed.sum()}/{len(rep)} validation checks passed")
    return 0 if rep.passed.all() else 1


if __name__ == "__main__":
    sys.exit(main())
