-- 40 · Legs 2 and 3: transactions ↔ payout (many-to-one) and payout ↔ bank (one-to-one on UTR).

-- Leg 2: rebuild every payout from its transactions and compare component by component.
-- The export only covers 1–31 Oct UTC, so the 30 Sep and 1 Oct batches are partly outside it;
-- they are flagged rather than reported as breaks.
CREATE OR REPLACE TABLE leg2_payout_rebuild AS
WITH txn AS (
    SELECT settlement_id,
           SUM(amount_inr) FILTER (WHERE type = 'payment' AND status = 'captured') AS gross_txn,
           SUM(fee_inr)    FILTER (WHERE type = 'payment' AND status = 'captured') AS fee_txn,
           SUM(gst_inr)    FILTER (WHERE type = 'payment' AND status = 'captured') AS gst_txn,
           SUM(amount_inr) FILTER (WHERE type = 'refund')                          AS refund_txn,
           COUNT(*)        FILTER (WHERE type = 'payment' AND status = 'captured') AS n_captures
    FROM gw WHERE settlement_id IS NOT NULL GROUP BY 1)
SELECT p.settlement_id, p.batch_date, p.settlement_date, p.utr, t.n_captures,
       p.gross_inr,  ROUND(COALESCE(t.gross_txn, 0), 2)  AS gross_txn,
       p.fee_inr,    ROUND(COALESCE(t.fee_txn, 0), 2)    AS fee_txn,
       p.gst_inr,    ROUND(COALESCE(t.gst_txn, 0), 2)    AS gst_txn,
       p.refund_inr, ROUND(COALESCE(t.refund_txn, 0), 2) AS refund_txn,
       p.chargeback_inr, p.net_inr,
       p.batch_date IN (DATE '2026-09-30', DATE '2026-10-01') AS partial_export,
       ROUND(p.gross_inr - COALESCE(t.gross_txn, 0), 2) = 0
         AND ROUND(p.fee_inr - COALESCE(t.fee_txn, 0), 2) = 0
         AND ROUND(p.gst_inr - COALESCE(t.gst_txn, 0), 2) = 0
         AND ROUND(p.refund_inr - COALESCE(t.refund_txn, 0), 2) = 0 AS ties
FROM payouts p LEFT JOIN txn t USING (settlement_id);

-- Leg 3: payout ↔ bank credit on UTR, with the contractual due date (T+1, Sunday rolls to Monday).
CREATE OR REPLACE TABLE leg3_payout_bank AS
WITH b AS (SELECT utr, value_date, credit FROM bank WHERE category = 'gateway_settlement'),
     due AS (
        SELECT *, CASE WHEN DAYOFWEEK(batch_date + 1) = 0 THEN batch_date + 2 ELSE batch_date + 1 END AS due_date
        FROM payouts)
SELECT d.settlement_id, d.batch_date, d.due_date, d.settlement_date, d.utr, d.net_inr,
       b.value_date AS bank_date, b.credit AS bank_credit,
       CASE
         WHEN b.utr IS NOT NULL AND ROUND(b.credit - d.net_inr, 2) = 0 THEN 'S1_matched'
         WHEN b.utr IS NOT NULL                                          THEN 'S2_amount_differs'
         WHEN d.due_date >= DATE '2026-11-01'                            THEN 'S3_in_transit'
         ELSE 'S4_overdue'
       END AS rule,
       DATE '2026-10-31' - d.due_date AS days_overdue_at_close
FROM due d LEFT JOIN b USING (utr);

-- Bank lines that look like PayFlux credits but match no payout (should be none).
CREATE OR REPLACE TABLE bank_unmatched_gateway_credits AS
SELECT b.* FROM bank b ANTI JOIN payouts p USING (utr) WHERE b.category = 'gateway_settlement';

-- Integrity check on the statement itself: recompute the running balance with a window sum.
CREATE OR REPLACE TABLE bank_balance_check AS
WITH o AS (SELECT *, file_row_number + 1 AS line_no FROM bank)
SELECT line_no, value_date, balance,
       ROUND(2500000 + SUM(credit - debit) OVER (ORDER BY line_no), 2) AS recomputed,
       ROUND(balance - (2500000 + SUM(credit - debit) OVER (ORDER BY line_no)), 2) AS diff
FROM o;
