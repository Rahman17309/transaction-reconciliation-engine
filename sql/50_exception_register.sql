-- 50 · One exception register across all three legs.
-- Grain: one item a person has to look at. Fee overcharges are rolled up to one claim per
-- method (the per-transaction evidence stays in fee_audit).

CREATE OR REPLACE TABLE exception_register AS
WITH items AS (
    -- Leg 1: ledger ↔ gateway
    SELECT 'L1' AS leg, c.exception_type, c.outcome AS class, c.owner, c.action,
           m.order_id AS ref, m.value_inr AS amount_inr,
           CASE WHEN m.rule = 'R3_amount_mismatch' THEN -m.diff_inr ELSE m.value_inr END AS impact_inr,
           COALESCE(m.posting_ts_ist, m.gw_ts_ist)::DATE AS event_date, 1 AS n_txns
    FROM leg1_match m JOIN rule_catalogue c USING (rule)
    WHERE c.outcome IN ('break', 'timing')

    UNION ALL  -- refunds
    SELECT 'L1', CASE rule WHEN 'F3_no_credit_note' THEN 'REFUND_NO_CREDIT_NOTE'
                           WHEN 'F2_amount_differs' THEN 'REFUND_CN_AMOUNT_DIFFERS'
                           ELSE 'CUTOFF_TIMING' END,
           CASE WHEN rule = 'F4_next_period' THEN 'timing' ELSE 'break' END,
           'Finance (AR)', 'Raise the credit note against the order', order_id, refund_inr,
           CASE WHEN rule = 'F2_amount_differs' THEN refund_inr - cn_inr ELSE refund_inr END,
           ts_ist::DATE, 1
    FROM refund_match WHERE rule <> 'F1_matched'

    UNION ALL  -- chargebacks
    SELECT 'L2', 'CHARGEBACK_NOT_BOOKED', 'break', 'Finance (AR)',
           'Book the chargeback; contest with evidence within the gateway window',
           settlement_id, chargeback_inr, chargeback_inr, batch_date, 1
    FROM chargebacks

    UNION ALL  -- fee overcharge claims, one per method
    SELECT 'L2', 'FEE_ABOVE_CONTRACT', 'claim', 'Treasury / vendor manager',
           'Raise a refund claim with PayFlux, attach fee_audit extract',
           method, SUM(excess_incl_gst_inr), SUM(excess_incl_gst_inr), MIN(day_ist), COUNT(*)
    FROM fee_audit WHERE is_overcharged GROUP BY method

    UNION ALL  -- payout ↔ bank
    SELECT 'L3', CASE rule WHEN 'S3_in_transit' THEN 'SETTLEMENT_IN_TRANSIT'
                           WHEN 'S4_overdue'    THEN 'SETTLEMENT_OVERDUE'
                           ELSE 'SETTLEMENT_AMOUNT_DIFFERS' END,
           CASE WHEN rule = 'S3_in_transit' THEN 'timing' ELSE 'break' END,
           'Treasury', CASE WHEN rule = 'S4_overdue' THEN 'Escalate to PayFlux; book as receivable from gateway'
                            ELSE 'Confirm credit in November statement' END,
           settlement_id, net_inr, net_inr, due_date, 1
    FROM leg3_payout_bank WHERE rule <> 'S1_matched'
)
SELECT
    ROW_NUMBER() OVER (ORDER BY CASE class WHEN 'break' THEN 1 WHEN 'claim' THEN 2 ELSE 3 END,
                                ABS(impact_inr) DESC, ref) AS exception_id,
    *,
    DATE '2026-10-31' - event_date AS age_days,
    -- rank within type, so a reviewer can work the biggest items of each type first
    RANK() OVER (PARTITION BY exception_type ORDER BY ABS(impact_inr) DESC) AS rank_in_type,
    -- share of the break value covered by items up to this one (Pareto view)
    SUM(ABS(impact_inr)) FILTER (WHERE class = 'break') OVER (ORDER BY CASE class WHEN 'break' THEN 1 WHEN 'claim' THEN 2 ELSE 3 END,
                                ABS(impact_inr) DESC, ref ROWS UNBOUNDED PRECEDING)
      / SUM(ABS(impact_inr)) FILTER (WHERE class = 'break') OVER () AS cum_share_of_breaks
FROM items;

CREATE OR REPLACE TABLE exception_summary AS
SELECT class, exception_type, ANY_VALUE(owner) AS owner, COUNT(*) AS items, SUM(n_txns) AS txns,
       ROUND(SUM(impact_inr), 2) AS impact_inr, MAX(age_days) AS oldest_days
FROM exception_register GROUP BY class, exception_type
ORDER BY CASE ANY_VALUE(class) WHEN 'break' THEN 1 WHEN 'claim' THEN 2 ELSE 3 END, impact_inr DESC;
