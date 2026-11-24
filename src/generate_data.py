"""Generate a realistic, messy October-2026 month-end close for a D2C retailer.

Four source files, as a finance team would actually receive them:

  data/raw/erp_ledger.csv        sales receipts booked in the ERP (IST, mixed date formats)
  data/raw/gateway_txns.csv      PayFlux transaction report (UTC timestamps, amounts in paise)
  data/raw/gateway_payouts.csv   PayFlux settlement (payout) file
  data/raw/bank_statement.csv    current-account statement (all company credits and debits)

Known problems are injected at documented rates and written to data/truth/labels.csv
so the matching engine built later in the week can be scored. The engine never reads
the labels.

Run:  python src/generate_data.py   (deterministic, seed 42)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
TRUTH = ROOT / "data" / "truth"

IST = timedelta(hours=5, minutes=30)
# Orders are simulated from 30 Sep 18:00 IST to 1 Nov 08:00 IST so that the
# month boundary (IST for the ERP, UTC for the gateway report) has real spill-over.
SIM_START = datetime(2026, 9, 30, 18, 0)
SIM_END = datetime(2026, 11, 1, 8, 0)
N_ORDERS = 25_500

METHODS = ["UPI", "Card", "NetBanking", "Wallet"]
METHOD_P = [0.55, 0.25, 0.10, 0.10]
# Contracted MDR (fee) rates, before 18% GST on the fee.
CONTRACT_MDR = {"UPI": 0.0030, "Card": 0.0190, "NetBanking": 0.0150, "Wallet": 0.0200}
GST_ON_FEE = 0.18

# Injection rates (documented in README "Assumptions")
P_LEDGER_ONLY_ABSENT = 0.004   # booked in ERP, no gateway record at all
P_LEDGER_ONLY_FAILED = 0.003   # booked in ERP, gateway shows the payment FAILED
P_AMOUNT_MISMATCH = 0.005      # gateway captured less than ERP booked (coupon applied at checkout)
P_PAISE_ROUNDING = 0.004       # +-Rs 1 rounding difference (should be tolerated)
P_GATEWAY_ONLY = 0.003         # captured at gateway, never booked in ERP
P_LEDGER_DUP = 0.003           # ERP posted the same receipt twice
P_GATEWAY_DUP = 0.002          # duplicate rows in the gateway export
P_REFUND = 0.020               # refunds processed at the gateway
P_REFUND_NOT_BOOKED = 0.30     # share of refunds with no credit note in the ERP
P_CARD_OVERCHARGE = 0.015      # card txns charged 2.4% instead of 1.9%
WALLET_RATE_CHANGE = date(2026, 10, 15)  # wallet MDR silently moved to 2.25% from this IST date
WALLET_NEW_RATE = 0.0225
N_TEST_ROWS = 40
N_CHARGEBACKS = 15
HELD_SETTLEMENT_DAY = date(2026, 10, 19)  # this batch is held by the gateway (KYC review)


def ist_hours(rng: np.random.Generator, n: int) -> np.ndarray:
    """Hour-of-day weights: evening peak, small but real late-night traffic."""
    w = np.array([3, 2, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 7, 7, 6, 6, 6, 7, 8, 9, 10, 10, 8, 5], float)
    return rng.choice(24, size=n, p=w / w.sum())


def messy_ist(ts: pd.Timestamp, r: float) -> str:
    """ERP export has three date formats depending on which module posted the row."""
    if r < 0.70:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if r < 0.90:
        return ts.strftime("%d/%m/%Y %H:%M")
    return ts.strftime("%d-%b-%Y %I:%M %p")


def messy_ref(order_id: str, r: float) -> str:
    """Merchant reference as the gateway stores it (typed by different integrations)."""
    num = order_id.split("-")[1]
    if r < 0.80:
        return order_id
    if r < 0.90:
        return order_id.lower()
    if r < 0.95:
        return f" ORD{num} "
    return f"ORD_{num}"


def next_business_day(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() == 6:  # Sundays: no NEFT credit, roll to Monday
        d += timedelta(days=1)
    return d


def main() -> None:
    rng = np.random.default_rng(SEED)
    RAW.mkdir(parents=True, exist_ok=True)
    TRUTH.mkdir(parents=True, exist_ok=True)
    labels: list[dict] = []

    # ---------------------------------------------------------------- orders
    span_days = (SIM_END.date() - SIM_START.date()).days + 1
    day_off = rng.integers(0, span_days, N_ORDERS)
    base_day = datetime(SIM_START.year, SIM_START.month, SIM_START.day)
    ts = [base_day + timedelta(days=int(d), hours=int(h), minutes=int(m), seconds=int(s))
          for d, h, m, s in zip(day_off, ist_hours(rng, N_ORDERS),
                                rng.integers(0, 60, N_ORDERS), rng.integers(0, 60, N_ORDERS))]
    orders = pd.DataFrame({"order_ts_ist": ts})
    orders = orders[(orders.order_ts_ist >= SIM_START) & (orders.order_ts_ist < SIM_END)]
    orders = orders.sort_values("order_ts_ist").reset_index(drop=True)
    n = len(orders)
    orders["order_id"] = [f"ORD-{1_000_000 + i}" for i in range(n)]
    orders["customer_id"] = [f"C{x:06d}" for x in rng.integers(1, 60_000, n)]
    orders["method"] = rng.choice(METHODS, n, p=METHOD_P)
    amt = np.exp(rng.normal(6.55, 0.75, n))  # median ~Rs 700
    orders["amount"] = np.clip(np.round(amt, 0), 99, 25_000)
    orders["channel"] = rng.choice(["Website", "App", "Instagram"], n, p=[0.45, 0.45, 0.10])

    # --------------------------------------------------------- gateway txns
    g = orders.copy()
    g["status"] = "captured"
    g["gw_amount"] = g["amount"]
    u = rng.random(n)
    absent = u < P_LEDGER_ONLY_ABSENT
    failed = (u >= P_LEDGER_ONLY_ABSENT) & (u < P_LEDGER_ONLY_ABSENT + P_LEDGER_ONLY_FAILED)
    g.loc[failed, "status"] = "failed"
    v = rng.random(n)
    mism = (v < P_AMOUNT_MISMATCH) & ~absent & ~failed
    g.loc[mism, "gw_amount"] = np.round(g.loc[mism, "amount"] * rng.uniform(0.80, 0.95, mism.sum()), 0)
    rnd = (v >= P_AMOUNT_MISMATCH) & (v < P_AMOUNT_MISMATCH + P_PAISE_ROUNDING) & ~absent & ~failed
    g.loc[rnd, "gw_amount"] = g.loc[rnd, "amount"] + rng.choice([-1, 1], rnd.sum())
    # Gateway timestamp: a few seconds to a couple of minutes after the order
    g["gw_ts_ist"] = g["order_ts_ist"] + pd.to_timedelta(rng.integers(3, 150, n), unit="s")
    for oid in g.loc[absent, "order_id"]:
        labels.append({"source": "ledger", "key": oid, "issue": "ledger_only_no_gateway_record"})
    for oid in g.loc[failed, "order_id"]:
        labels.append({"source": "ledger", "key": oid, "issue": "ledger_only_payment_failed"})
    for oid in g.loc[mism, "order_id"]:
        labels.append({"source": "ledger", "key": oid, "issue": "amount_mismatch"})
    for oid in g.loc[rnd, "order_id"]:
        labels.append({"source": "ledger", "key": oid, "issue": "rounding_within_tolerance"})
    g = g[~absent].copy()

    # gateway-only captures: orders that exist at the gateway but were never booked
    n_go = int(round(n * P_GATEWAY_ONLY))
    go_idx = rng.choice(g.index[g.status == "captured"], n_go, replace=False)
    gateway_only_ids = set(g.loc[go_idx, "order_id"])
    for oid in gateway_only_ids:
        labels.append({"source": "gateway", "key": oid, "issue": "gateway_only_not_booked"})

    # fees
    g["ist_date"] = g["gw_ts_ist"].dt.date
    rate = g["method"].map(CONTRACT_MDR).astype(float)
    over = (g["method"] == "Card") & (rng.random(len(g)) < P_CARD_OVERCHARGE)
    rate[over] = 0.024
    wallet_late = (g["method"] == "Wallet") & (g["ist_date"] >= WALLET_RATE_CHANGE)
    rate[wallet_late] = WALLET_NEW_RATE
    for oid in g.loc[over & (g.status == "captured"), "order_id"]:
        labels.append({"source": "gateway", "key": oid, "issue": "fee_overcharge_card"})
    for oid in g.loc[wallet_late & (g.status == "captured"), "order_id"]:
        labels.append({"source": "gateway", "key": oid, "issue": "fee_overcharge_wallet_rate_change"})
    cap = g["status"] == "captured"
    g["amount_paise"] = (g["gw_amount"] * 100).round().astype("int64")
    g["fee_paise"] = np.where(cap, (g["amount_paise"] * rate).round(), 0).astype("int64")
    g["gst_paise"] = (g["fee_paise"] * GST_ON_FEE).round().astype("int64")
    g["txn_id"] = [f"pf_{x}" for x in rng.integers(10**11, 10**12, len(g))]
    g["merchant_ref"] = [messy_ref(o, r) for o, r in zip(g["order_id"], rng.random(len(g)))]
    g["type"] = "payment"

    # refunds (processed 1-6 days after capture)
    cap_ids = g.index[cap & ~g["order_id"].isin(gateway_only_ids)]
    ref_idx = rng.choice(cap_ids, int(round(len(cap_ids) * P_REFUND)), replace=False)
    refunds = g.loc[ref_idx].copy()
    refunds["type"] = "refund"
    refunds["status"] = "processed"
    refunds["gw_ts_ist"] = refunds["gw_ts_ist"] + pd.to_timedelta(rng.integers(24, 150, len(refunds)), unit="h")
    full = rng.random(len(refunds)) < 0.8
    refunds["amount_paise"] = np.where(full, refunds["amount_paise"],
                                       (refunds["amount_paise"] * 0.5).round()).astype("int64")
    refunds["fee_paise"] = 0  # PayFlux does not return the MDR on refunds
    refunds["gst_paise"] = 0
    refunds["txn_id"] = [f"rf_{x}" for x in rng.integers(10**11, 10**12, len(refunds))]
    refunds["ist_date"] = refunds["gw_ts_ist"].dt.date
    booked_cn = rng.random(len(refunds)) >= P_REFUND_NOT_BOOKED
    refunds["cn_booked"] = booked_cn
    for oid in refunds.loc[~booked_cn, "order_id"]:
        labels.append({"source": "gateway", "key": oid, "issue": "refund_not_in_ledger"})

    gw = pd.concat([g, refunds], ignore_index=True)

    # ---------------------------------------------------------- settlements
    # Gateway settles each IST day's captured payments net of fees, GST and refunds, T+1 business day.
    gw["batch_day"] = gw["ist_date"]
    settle_rows = []
    cb_days = rng.choice(pd.date_range("2026-10-03", "2026-10-30").date, N_CHARGEBACKS)
    cb_amounts = np.round(np.exp(rng.normal(7.4, 0.5, N_CHARGEBACKS)), 0)
    for d, grp in gw[(gw.status.isin(["captured", "processed"]))].groupby("batch_day"):
        pay = grp[grp.type == "payment"]
        rf = grp[grp.type == "refund"]
        cb = int(round(cb_amounts[cb_days == d].sum() * 100))
        gross = int(pay.amount_paise.sum())
        fees = int(pay.fee_paise.sum())
        gst = int(pay.gst_paise.sum())
        refund_amt = int(rf.amount_paise.sum())
        net = gross - fees - gst - refund_amt - cb
        sd = next_business_day(d)
        settle_rows.append({"batch_day": d, "settlement_id": f"setl_{d:%Y%m%d}",
                            "settlement_date": sd, "gross_paise": gross, "fee_paise": fees,
                            "gst_paise": gst, "refund_paise": refund_amt,
                            "chargeback_paise": cb, "net_paise": net,
                            "utr": f"PFLX{rng.integers(10**9, 10**10)}"})
    payouts = pd.DataFrame(settle_rows)
    gw = gw.merge(payouts[["batch_day", "settlement_id"]], on="batch_day", how="left")
    held = payouts["batch_day"] == HELD_SETTLEMENT_DAY
    payouts.loc[held, "settlement_date"] = date(2026, 11, 3)
    labels.append({"source": "payout", "key": payouts.loc[held, "settlement_id"].iloc[0],
                   "issue": "settlement_held_by_gateway"})
    for d, a in zip(cb_days, cb_amounts):
        labels.append({"source": "payout", "key": f"setl_{d:%Y%m%d}", "issue": "chargeback_deducted",
                       "detail": f"{int(a)}"})

    # ------------------------------------------------------ gateway report
    # The PayFlux dashboard export is by UTC date: 1-31 Oct UTC.
    gw["ts_utc"] = gw["gw_ts_ist"] - IST
    in_rep = (gw["ts_utc"] >= datetime(2026, 10, 1)) & (gw["ts_utc"] < datetime(2026, 11, 1))
    rep = gw[in_rep].copy()
    dup_idx = rng.choice(rep.index, int(round(len(rep) * P_GATEWAY_DUP)), replace=False)
    for t in rep.loc[dup_idx, "txn_id"]:
        labels.append({"source": "gateway", "key": t, "issue": "duplicate_export_row"})
    rep = pd.concat([rep, rep.loc[dup_idx]]).sort_values("ts_utc")
    gateway_txns = pd.DataFrame({
        "txn_id": rep["txn_id"],
        "merchant_ref": rep["merchant_ref"],
        "type": rep["type"],
        "created_at_utc": rep["ts_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": rep["method"].str.lower(),
        "status": rep["status"],
        "amount_paise": rep["amount_paise"],
        "fee_paise": rep["fee_paise"],
        "gst_paise": rep["gst_paise"],
        "settlement_id": np.where(rep["status"] == "failed", "", rep["settlement_id"]),
    })
    gateway_txns.to_csv(RAW / "gateway_txns.csv", index=False)

    out_pay = payouts[(payouts.settlement_date >= date(2026, 10, 1))].copy()
    out_pay = out_pay[out_pay.batch_day <= date(2026, 10, 31)]
    gateway_payouts = out_pay.drop(columns="batch_day")
    gateway_payouts.to_csv(RAW / "gateway_payouts.csv", index=False)

    # ---------------------------------------------------------------- ledger
    led = orders[(orders.order_ts_ist >= datetime(2026, 10, 1)) & (orders.order_ts_ist < datetime(2026, 11, 1))]
    led = led[~led.order_id.isin(gateway_only_ids)].copy()
    led["entry_type"] = "receipt"
    led["voucher_no"] = [f"RV/{i:06d}" for i in range(1, len(led) + 1)]
    # credit notes for refunds that finance did book
    cn = refunds[refunds.cn_booked].merge(orders[["order_id", "channel", "customer_id"]], on="order_id",
                                          suffixes=("_x", ""))
    cn["cn_ts"] = cn["gw_ts_ist"] + pd.to_timedelta(rng.integers(1, 30, len(cn)), unit="h")
    # credit notes posted in November belong to next month's books
    cn = cn[(cn.cn_ts >= datetime(2026, 10, 1)) & (cn.cn_ts < datetime(2026, 11, 1))]
    cn_rows = pd.DataFrame({
        "order_ts_ist": cn["cn_ts"],
        "order_id": cn["order_id"], "customer_id": cn["customer_id"], "method": cn["method"],
        "amount": -(cn["amount_paise"] / 100.0), "channel": cn["channel"], "entry_type": "credit_note",
    })
    cn_rows["voucher_no"] = [f"CN/{i:05d}" for i in range(1, len(cn_rows) + 1)]
    led = pd.concat([led, cn_rows], ignore_index=True)
    dups = led[led.entry_type == "receipt"].sample(frac=P_LEDGER_DUP, random_state=SEED)
    for oid in dups["order_id"]:
        labels.append({"source": "ledger", "key": oid, "issue": "duplicate_posting"})
    dups = dups.assign(voucher_no=[f"RV/9{i:05d}" for i in range(len(dups))])
    led = pd.concat([led, dups], ignore_index=True)
    test = pd.DataFrame({
        "order_ts_ist": [datetime(2026, 10, 1) + timedelta(hours=int(h)) for h in rng.integers(0, 720, N_TEST_ROWS)],
        "order_id": [f"TEST-{i:04d}" for i in range(N_TEST_ROWS)],
        "customer_id": "C_TEST", "method": rng.choice(METHODS, N_TEST_ROWS),
        "amount": rng.choice([1.0, 10.0, 0.0], N_TEST_ROWS), "channel": "Website",
        "entry_type": "receipt", "voucher_no": [f"RV/T{i:04d}" for i in range(N_TEST_ROWS)],
    })
    led = pd.concat([led, test], ignore_index=True).sort_values("order_ts_ist").reset_index(drop=True)
    r = rng.random(len(led))
    erp = pd.DataFrame({
        "voucher_no": led["voucher_no"],
        "entry_type": led["entry_type"],
        "posting_ts": [messy_ist(t, x) for t, x in zip(led["order_ts_ist"], r)],
        "order_ref": led["order_id"],
        "customer_id": led["customer_id"],
        "payment_mode": led["method"].replace({"NetBanking": "Net Banking"}),
        "sales_channel": led["channel"],
        # ERP exports ~15% of amounts as text with thousands separators (visible on amounts >= Rs 1,000)
        "amount_inr": [f"{a:,.2f}" if x < 0.15 else f"{a:.2f}" for a, x in zip(led["amount"], rng.random(len(led)))],
    })
    # ~0.5% blank payment mode (manual journal)
    blank = rng.random(len(erp)) < 0.005
    erp.loc[blank, "payment_mode"] = ""
    erp.to_csv(RAW / "erp_ledger.csv", index=False)

    # ------------------------------------------------------------------ bank
    bank_rows = []
    for _, p in out_pay.iterrows():
        if p.settlement_date > date(2026, 10, 31):
            continue  # credited after the statement period
        bank_rows.append({"value_date": p.settlement_date,
                          "narration": f"NEFT CR-PAYFLUX PAYMENTS PVT LTD-{p.utr}-SETL",
                          "credit": p.net_paise / 100, "debit": 0.0})
    # other operating lines on the same account
    for d in pd.date_range("2026-10-01", "2026-10-31").date:
        if d.weekday() == 6:
            continue
        if rng.random() < 0.55:
            bank_rows.append({"value_date": d, "narration": f"NEFT CR-BLUEDART COD REMIT-{rng.integers(10**8, 10**9)}",
                              "credit": float(np.round(rng.uniform(40_000, 160_000), 2)), "debit": 0.0})
        for _ in range(rng.integers(1, 4)):
            bank_rows.append({"value_date": d, "narration": f"IMPS DR-VENDOR PAY-{rng.integers(10**8, 10**9)}",
                              "credit": 0.0, "debit": float(np.round(rng.uniform(15_000, 220_000), 2))})
    bank_rows += [
        {"value_date": date(2026, 10, 1), "narration": "ACH DR-OFFICE RENT OCT", "credit": 0.0, "debit": 185_000.0},
        {"value_date": date(2026, 10, 30), "narration": "SALARY BATCH OCT-2026", "credit": 0.0, "debit": 1_240_000.0},
        {"value_date": date(2026, 10, 31), "narration": "CHRGS-SMS ALERT Q3", "credit": 0.0, "debit": 59.0},
        {"value_date": date(2026, 10, 7), "narration": "GST PMT CHALLAN", "credit": 0.0, "debit": 312_450.0},
    ]
    bank = pd.DataFrame(bank_rows).sort_values(["value_date", "credit"], ascending=[True, False]).reset_index(drop=True)
    bank["balance"] = 2_500_000 + (bank["credit"] - bank["debit"]).cumsum()
    bank["value_date"] = pd.to_datetime(bank["value_date"]).dt.strftime("%d-%m-%Y")
    bank.to_csv(RAW / "bank_statement.csv", index=False)

    pd.DataFrame(labels).to_csv(TRUTH / "labels.csv", index=False)
    print(f"erp_ledger.csv       {len(erp):>7,} rows")
    print(f"gateway_txns.csv     {len(gateway_txns):>7,} rows")
    print(f"gateway_payouts.csv  {len(gateway_payouts):>7,} rows")
    print(f"bank_statement.csv   {len(bank):>7,} rows")
    print(f"labels.csv           {len(labels):>7,} labelled issues")


if __name__ == "__main__":
    main()
