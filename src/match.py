"""Run the DuckDB reconciliation engine: sql/*.sql in order, then export the outputs.

Usage:  python src/match.py            -> data/output/*.csv|parquet and recon.duckdb
The engine reads only data/clean/. It never reads data/truth/ (that is for scoring on Day 4).
"""
from pathlib import Path
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SQL, CLEAN, OUT = ROOT / "sql", ROOT / "data" / "clean", ROOT / "data" / "output"

EXPORTS = {  # table -> format
    "leg1_match": "parquet", "refund_match": "parquet", "fee_audit": "parquet",
    "leg2_payout_rebuild": "csv", "leg3_payout_bank": "csv",
    "exception_register": "csv", "exception_summary": "csv", "cash_bridge": "csv",
}


def build(db_path: Path | str = ":memory:") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))
    for f in sorted(SQL.glob("*.sql")):
        con.execute(f.read_text().replace("{CLEAN}", CLEAN.as_posix()))
    return con


def checks(con) -> list[tuple[str, bool]]:
    q = lambda s: con.execute(s).fetchall()[0][0]
    return [
        ("every ERP receipt gets exactly one leg-1 outcome",
         q("SELECT COUNT(*) = COUNT(DISTINCT voucher_no) FROM leg1_match WHERE voucher_no IS NOT NULL")
         and q("SELECT COUNT(*) FROM leg1_match WHERE voucher_no IS NOT NULL") == q("SELECT COUNT(*) FROM erp_receipts")),
        ("every gateway payment gets exactly one leg-1 outcome",
         q("SELECT COUNT(*) FROM leg1_match WHERE txn_id IS NOT NULL") == q("SELECT COUNT(*) FROM gw_payments")),
        ("every refund gets exactly one outcome",
         q("SELECT COUNT(*) FROM refund_match") == q("SELECT COUNT(*) FROM gw_refunds")),
        ("payouts rebuilt from txns tie for all complete batches",
         q("SELECT BOOL_AND(ties) FROM leg2_payout_rebuild WHERE NOT partial_export")),
        ("no PayFlux bank credit without a payout",
         q("SELECT COUNT(*) FROM bank_unmatched_gateway_credits") == 0),
        ("bank running balance recomputes exactly",
         q("SELECT MAX(ABS(diff)) FROM bank_balance_check") == 0),
        ("cash bridge unexplained within ₹1",
         abs(q("SELECT amount_inr FROM cash_bridge WHERE kind = 'residual'")) <= 1),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db = OUT / "recon.duckdb"
    db.unlink(missing_ok=True)
    con = build(db)
    for t, fmt in EXPORTS.items():
        target = OUT / f"{t}.{fmt}"
        opts = "(FORMAT parquet)" if fmt == "parquet" else "(HEADER, DELIMITER ',')"
        con.execute(f"COPY (SELECT * FROM {t}) TO '{target.as_posix()}' {opts}")
    results = checks(con)
    for name, ok in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(con.sql("SELECT rule, COUNT(*) AS keys, ROUND(SUM(value_inr), 2) AS value_inr FROM leg1_match GROUP BY 1 ORDER BY 1"))
    print(con.sql("SELECT * FROM exception_summary"))
    print(con.sql("SELECT step, item, amount_inr, running_inr FROM cash_bridge"))
    con.close()
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
