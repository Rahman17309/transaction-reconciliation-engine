-- 20 · Money going back out: gateway refunds ↔ ERP credit notes, and chargebacks.
--
--   F1 matched          refund has a credit note for the same order and the same amount
--   F2 amount differs   credit note exists but for a different amount
--   F3 no credit note   refund processed (and netted from a payout) but not in the books -> break
--   F4 next period      refund is 1 Nov in IST -> timing
--   F5 CN pending       no credit note yet, but the refund is so close to month end that finance
--                       normally posts the credit note in the next period -> timing, re-check in November.
--                       "So close" = the 99th percentile of the observed refund -> credit-note lag on
--                       matched pairs (learned from the data, not hard-coded). Added on Day 4 after
--                       scoring showed every refund false positive sat in the last day of October.
-- Credit notes with no gateway refund are reported separately (manual credit notes).

-- How long after a refund does finance usually post the credit note? (hours, matched pairs only)
CREATE OR REPLACE TABLE cn_posting_lag AS
WITH lag AS (
    SELECT EPOCH(c.posting_ts_ist - r.ts_ist) / 3600.0 AS lag_h
    FROM gw_refunds r JOIN erp_credit_notes c USING (order_id)
    WHERE r.in_period_ist AND ROUND(-c.amount_inr - r.amount_inr, 2) = 0)
SELECT COUNT(*) AS pairs, MEDIAN(lag_h) AS median_h, QUANTILE_CONT(lag_h, 0.99) AS p99_h, MAX(lag_h) AS max_h
FROM lag;

CREATE OR REPLACE TABLE refund_match AS
SELECT
    r.txn_id, r.order_id, r.ts_ist, r.settlement_id,
    r.amount_inr                         AS refund_inr,
    c.voucher_no                         AS cn_voucher,
    -c.amount_inr                        AS cn_inr,
    CASE
        WHEN NOT r.in_period_ist                         THEN 'F4_next_period'
        WHEN c.voucher_no IS NULL
             AND r.ts_ist >= p.period_end - TO_MICROSECONDS(CAST(l.p99_h * 3600e6 AS BIGINT))
                                                         THEN 'F5_cn_pending'
        WHEN c.voucher_no IS NULL                        THEN 'F3_no_credit_note'
        WHEN ROUND(-c.amount_inr - r.amount_inr, 2) = 0  THEN 'F1_matched'
        ELSE 'F2_amount_differs'
    END AS rule
FROM gw_refunds r
LEFT JOIN erp_credit_notes c USING (order_id)
CROSS JOIN params p CROSS JOIN cn_posting_lag l;

CREATE OR REPLACE TABLE credit_notes_without_refund AS
SELECT c.* FROM erp_credit_notes c ANTI JOIN gw_refunds r USING (order_id);

-- Chargebacks only exist at payout level (the gateway nets them from the batch).
-- None has an ERP entry, so every one is a break owned by finance.
CREATE OR REPLACE TABLE chargebacks AS
SELECT settlement_id, batch_date, settlement_date, chargeback_inr
FROM payouts WHERE chargeback_inr > 0;
