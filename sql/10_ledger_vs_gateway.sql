-- 10 · Leg 1: ERP receipts ↔ gateway payments.
-- A rule cascade. Each key gets exactly one outcome, decided by the FIRST rule it satisfies,
-- so the order of the CASE branches below is the order of the rules.
--
--   R0 cross-period     booked in October, but the capture is 1 Nov in IST -> timing (cash is in the
--                       November batch). Added on Day 4: a 20-seed robustness run found orders booked
--                       at 23:59 on 31 Oct and captured seconds later matching as "exact", which left
--                       an unexplained line in the cash bridge.
--   R1 exact            same order id, same amount
--   R2 tolerance        same order id, |Δ| ≤ ₹1            -> auto-cleared
--   R3 amount mismatch  same order id, |Δ| > ₹1            -> break
--   R4 payment failed   ERP receipt, gateway attempt failed -> break
--   R5 cut-off (ERP)    ERP-only, posted before 05:30 IST on 1 Oct (sits in 30 Sep UTC) -> timing
--   R6 cut-off (GW)     gateway-only, capture is 1 Nov in IST                          -> timing
--   R7 no gateway       ERP-only, anything else             -> break
--   R8 not booked       gateway-only captured in October    -> break
--   R9 failed, unbooked gateway-only failed attempt         -> no action (no money moved)

CREATE OR REPLACE TABLE leg1_match AS
WITH joined AS (
    SELECT
        COALESCE(e.order_id, g.order_id)            AS order_id,
        e.voucher_no, e.posting_ts_ist, e.payment_mode, e.channel,
        e.amount_inr                                AS erp_amount,
        g.txn_id, g.status, g.method, g.ts_ist      AS gw_ts_ist, g.in_period_ist,
        g.amount_inr                                AS gw_amount,
        g.settlement_id,
        ROUND(g.amount_inr - e.amount_inr, 2)       AS diff_inr
    FROM erp_receipts e
    FULL OUTER JOIN gw_payments g USING (order_id)
)
SELECT j.*,
    CASE
        WHEN voucher_no IS NOT NULL AND status = 'captured' AND NOT in_period_ist                 THEN 'R0_cross_period'
        WHEN voucher_no IS NOT NULL AND status = 'captured' AND diff_inr = 0                     THEN 'R1_exact'
        WHEN voucher_no IS NOT NULL AND status = 'captured' AND ABS(diff_inr) <= p.amount_tolerance THEN 'R2_tolerance'
        WHEN voucher_no IS NOT NULL AND status = 'captured'                                       THEN 'R3_amount_mismatch'
        WHEN voucher_no IS NOT NULL AND status = 'failed'                                         THEN 'R4_payment_failed'
        WHEN voucher_no IS NOT NULL AND posting_ts_ist < p.period_start + p.utc_offset            THEN 'R5_cutoff_erp'
        WHEN txn_id IS NOT NULL AND status = 'captured' AND NOT in_period_ist                     THEN 'R6_cutoff_gateway'
        WHEN voucher_no IS NOT NULL                                                               THEN 'R7_no_gateway_record'
        WHEN status = 'captured'                                                                  THEN 'R8_not_booked'
        ELSE 'R9_failed_unbooked'
    END AS rule,
    COALESCE(erp_amount, gw_amount) AS value_inr
FROM joined j CROSS JOIN params p;

-- Outcome class per rule: matched / timing / break / no_action
CREATE OR REPLACE TABLE rule_catalogue (rule VARCHAR, outcome VARCHAR, exception_type VARCHAR, owner VARCHAR, action VARCHAR);
INSERT INTO rule_catalogue VALUES
 ('R0_cross_period',      'timing',    'CUTOFF_TIMING',         'Finance (GL)',    'Booked 31 Oct, captured 1 Nov: cash comes in the November batch; no entry'),
 ('R1_exact',             'matched',   NULL,                    NULL,              NULL),
 ('R2_tolerance',         'matched',   NULL,                    NULL,              'Auto-clear; post rounding to a rounding-off account'),
 ('R3_amount_mismatch',   'break',     'AMOUNT_MISMATCH',       'E-commerce ops',  'Pass checkout discount to ERP; raise credit note for the difference'),
 ('R4_payment_failed',    'break',     'BOOKED_PAYMENT_FAILED', 'Finance (AR)',    'Reverse the receipt or collect from customer'),
 ('R5_cutoff_erp',        'timing',    'CUTOFF_TIMING',         'Finance (GL)',    'Re-pull gateway report by IST date; no entry'),
 ('R6_cutoff_gateway',    'timing',    'CUTOFF_TIMING',         'Finance (GL)',    'November revenue; no October entry'),
 ('R7_no_gateway_record', 'break',     'NO_GATEWAY_RECORD',     'Finance (AR)',    'Find proof of payment or reverse the receipt'),
 ('R8_not_booked',        'break',     'GATEWAY_NOT_BOOKED',    'Finance (GL)',    'Book the missing sale'),
 ('R9_failed_unbooked',   'no_action', NULL,                    NULL,              NULL);

-- Diagnostic: could any ERP-only and gateway-only row be the SAME sale under a mistyped order id?
-- Candidate = same amount, same method, within 10 minutes. ROW_NUMBER keeps the closest candidate
-- from each side, and a pair counts only if each is the other's best (mutual best match).
CREATE OR REPLACE TABLE leg1_probable_pairs AS
WITH erp_only AS (SELECT * FROM leg1_match WHERE rule = 'R7_no_gateway_record'),
     gw_only  AS (SELECT * FROM leg1_match WHERE rule = 'R8_not_booked'),
     cand AS (
        SELECT e.order_id AS erp_order_id, g.order_id AS gw_order_id, e.erp_amount AS amount_inr,
               ABS(EPOCH(g.gw_ts_ist) - EPOCH(e.posting_ts_ist)) / 60 AS gap_min
        FROM erp_only e JOIN gw_only g
          ON e.erp_amount = g.gw_amount AND e.payment_mode = g.method
         AND ABS(EPOCH(g.gw_ts_ist) - EPOCH(e.posting_ts_ist)) <= 600),
     ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY erp_order_id ORDER BY gap_min) AS rn_e,
                  ROW_NUMBER() OVER (PARTITION BY gw_order_id  ORDER BY gap_min) AS rn_g
        FROM cand)
SELECT erp_order_id, gw_order_id, amount_inr, ROUND(gap_min, 1) AS gap_min
FROM ranked WHERE rn_e = 1 AND rn_g = 1;
