-- 20 · Money going back out: gateway refunds ↔ ERP credit notes, and chargebacks.
--
--   F1 matched          refund has a credit note for the same order and the same amount
--   F2 amount differs   credit note exists but for a different amount
--   F3 no credit note   refund processed (and netted from a payout) but not in the books -> break
--   F4 next period      refund is 1 Nov in IST -> timing
-- Credit notes with no gateway refund are reported separately (manual credit notes).

CREATE OR REPLACE TABLE refund_match AS
SELECT
    r.txn_id, r.order_id, r.ts_ist, r.settlement_id,
    r.amount_inr                         AS refund_inr,
    c.voucher_no                         AS cn_voucher,
    -c.amount_inr                        AS cn_inr,
    CASE
        WHEN NOT r.in_period_ist                         THEN 'F4_next_period'
        WHEN c.voucher_no IS NULL                        THEN 'F3_no_credit_note'
        WHEN ROUND(-c.amount_inr - r.amount_inr, 2) = 0  THEN 'F1_matched'
        ELSE 'F2_amount_differs'
    END AS rule
FROM gw_refunds r
LEFT JOIN erp_credit_notes c USING (order_id);

CREATE OR REPLACE TABLE credit_notes_without_refund AS
SELECT c.* FROM erp_credit_notes c ANTI JOIN gw_refunds r USING (order_id);

-- Chargebacks only exist at payout level (the gateway nets them from the batch).
-- None has an ERP entry, so every one is a break owned by finance.
CREATE OR REPLACE TABLE chargebacks AS
SELECT settlement_id, batch_date, settlement_date, chargeback_inr
FROM payouts WHERE chargeback_inr > 0;
