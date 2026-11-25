-- 00 · Staging: typed views over the cleaned parquet tables.
-- {CLEAN} is replaced by src/match.py with the path to data/clean.
-- Period under review: October 2026, IST calendar (the ERP's basis).

CREATE OR REPLACE TABLE params AS
SELECT TIMESTAMP '2026-10-01 00:00:00' AS period_start,
       TIMESTAMP '2026-11-01 00:00:00' AS period_end,
       INTERVAL 330 MINUTE              AS utc_offset,      -- IST = UTC + 05:30
       1.00                              AS amount_tolerance, -- ₹; rounding noise we accept
       0.02                              AS fee_tolerance,    -- ₹; paisa rounding on fees
       0.18                              AS gst_rate;

-- Contracted MDR per payment method (the merchant agreement).
CREATE OR REPLACE TABLE contract_mdr (method VARCHAR, mdr DOUBLE);
INSERT INTO contract_mdr VALUES ('UPI', 0.0030), ('Card', 0.0190), ('NetBanking', 0.0150), ('Wallet', 0.0200);

CREATE OR REPLACE VIEW ledger AS
SELECT * FROM read_parquet('{CLEAN}/ledger.parquet');

CREATE OR REPLACE VIEW erp_receipts AS
SELECT voucher_no, order_id, customer_id, payment_mode, channel, posting_ts_ist, amount_inr
FROM ledger WHERE entry_type = 'receipt';

CREATE OR REPLACE VIEW erp_credit_notes AS
SELECT voucher_no, order_id, posting_ts_ist, amount_inr          -- negative amounts
FROM ledger WHERE entry_type = 'credit_note';

CREATE OR REPLACE VIEW gw AS
SELECT * FROM read_parquet('{CLEAN}/gateway_txns.parquet');

CREATE OR REPLACE VIEW gw_payments AS
SELECT txn_id, order_id, method, status, ts_utc, ts_ist, in_period_ist,
       amount_inr, fee_inr, gst_inr, settlement_id
FROM gw WHERE type = 'payment';

CREATE OR REPLACE VIEW gw_refunds AS
SELECT txn_id, order_id, ts_ist, in_period_ist, amount_inr, settlement_id
FROM gw WHERE type = 'refund';

CREATE OR REPLACE VIEW payouts AS
SELECT * REPLACE (CAST(batch_date AS DATE) AS batch_date, CAST(settlement_date AS DATE) AS settlement_date)
FROM read_parquet('{CLEAN}/payouts.parquet');

CREATE OR REPLACE VIEW bank AS
SELECT * REPLACE (CAST(value_date AS DATE) AS value_date)
FROM read_parquet('{CLEAN}/bank.parquet', file_row_number = true);  -- keeps statement line order
