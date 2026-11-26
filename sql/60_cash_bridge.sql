-- 60 · Cash bridge: October ERP net sales → PayFlux credits in the October bank statement.
-- Each step is computed from the match tables above; the last line is whatever is left over.
-- A good reconciliation leaves "unexplained" at (or within paise of) zero.

CREATE OR REPLACE TABLE cash_bridge AS
WITH
erp AS (SELECT SUM(amount_inr) AS v FROM ledger),
bank_pf AS (SELECT SUM(credit) AS v FROM bank WHERE category = 'gateway_settlement'),
oct_batches AS (SELECT * FROM payouts WHERE batch_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-31'),
steps AS (
    SELECT 1 AS step, 'ERP net sales, October (receipts + credit notes)' AS item, 'start' AS kind, (SELECT v FROM erp) AS amount_inr
    UNION ALL SELECT 2, 'Booked but never captured (no record / failed)', 'break',
        -SUM(erp_amount) FROM leg1_match WHERE rule IN ('R4_payment_failed', 'R7_no_gateway_record')
    UNION ALL SELECT 3, 'Captured but never booked', 'break',
        SUM(gw_amount) FROM leg1_match WHERE rule = 'R8_not_booked'
    UNION ALL SELECT 4, 'Checkout discounts not passed to ERP (amount mismatch)', 'break',
        SUM(diff_inr) FROM leg1_match WHERE rule = 'R3_amount_mismatch'
    UNION ALL SELECT 5, 'Rounding within ±₹1', 'matched',
        SUM(diff_inr) FROM leg1_match WHERE rule = 'R2_tolerance'
    UNION ALL SELECT 6, 'Cut-off: unexported 1 Oct captures not in ERP; 31 Oct bookings captured 1 Nov', 'timing',
        (SELECT gross_inr - gross_txn FROM leg2_payout_rebuild WHERE settlement_id = 'setl_20261001')
        - (SELECT SUM(erp_amount) FROM leg1_match WHERE rule = 'R5_cutoff_erp')
        - (SELECT COALESCE(SUM(erp_amount), 0) FROM leg1_match WHERE rule = 'R0_cross_period')
    UNION ALL SELECT 7, 'Gateway fees (MDR) and 18% GST on fees', 'cost',
        -SUM(fee_inr + gst_inr) FROM oct_batches
    UNION ALL SELECT 8, 'Refunds netted by gateway with no ERP credit note', 'break',
        -SUM(refund_inr) FROM refund_match WHERE rule = 'F3_no_credit_note'
    UNION ALL SELECT 9, 'Credit-note vs refund differences and refund timing', 'timing',
        (SELECT -SUM(amount_inr) FROM erp_credit_notes)
        - (SELECT SUM(refund_inr) FROM oct_batches)
        + (SELECT SUM(refund_inr) FROM refund_match WHERE rule = 'F3_no_credit_note')
    UNION ALL SELECT 10, 'Chargebacks netted from payouts, not booked', 'break',
        -SUM(chargeback_inr) FROM oct_batches
    UNION ALL SELECT 11, '30 Sep batch credited on 1 Oct (September sales)', 'timing',
        SUM(net_inr) FROM payouts WHERE batch_date = DATE '2026-09-30'
    UNION ALL SELECT 12, '19 Oct batch held by gateway (overdue)', 'break',
        -SUM(net_inr) FROM leg3_payout_bank WHERE rule = 'S4_overdue'
    UNION ALL SELECT 13, '31 Oct batch in transit (due 2 Nov)', 'timing',
        -SUM(net_inr) FROM leg3_payout_bank WHERE rule = 'S3_in_transit'
),
running AS (
    SELECT *, SUM(amount_inr) OVER (ORDER BY step) AS running_inr FROM steps
)
SELECT step, item, kind, ROUND(amount_inr, 2) AS amount_inr, ROUND(running_inr, 2) AS running_inr FROM running
UNION ALL
SELECT 14, 'Unexplained', 'residual',
       ROUND((SELECT v FROM bank_pf) - MAX_BY(running_inr, step), 2), ROUND((SELECT v FROM bank_pf), 2) FROM running
UNION ALL
SELECT 15, 'PayFlux credits in October bank statement', 'end', ROUND((SELECT v FROM bank_pf), 2), ROUND((SELECT v FROM bank_pf), 2)
ORDER BY step;
