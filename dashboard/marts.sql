-- Dashboard marts: small, pre-aggregated tables the dashboard reads.
-- Runs on top of the engine tables (sql/00-60). Every figure on the dashboard comes from one of
-- these, and src/dashboard.py checks that each mart ties back to the engine before writing HTML.

-- Close date for ageing: the last day of the period (IST).
CREATE OR REPLACE TABLE dash_params AS
SELECT (period_end - INTERVAL 1 DAY)::DATE AS close_date FROM params;

-- 1 · Leg-1 status per IST day: what auto-matched, what is timing, what is a break.
CREATE OR REPLACE TABLE dash_daily_status AS
WITH m AS (
    SELECT COALESCE(l.posting_ts_ist, l.gw_ts_ist)::DATE AS day_ist, c.outcome, l.value_inr
    FROM leg1_match l JOIN rule_catalogue c USING (rule)
    WHERE c.outcome <> 'no_action'
)
SELECT day_ist,
       COUNT(*)                                        AS keys,
       COUNT(*) FILTER (WHERE outcome = 'matched')     AS matched_keys,
       COUNT(*) FILTER (WHERE outcome = 'timing')      AS timing_keys,
       COUNT(*) FILTER (WHERE outcome = 'break')       AS break_keys,
       SUM(value_inr) FILTER (WHERE outcome = 'break') AS break_inr,
       COUNT(*) FILTER (WHERE outcome = 'matched') / COUNT(*)::DOUBLE AS auto_match_rate,
       -- 7-day trailing break rate, so one noisy day doesn't look like a trend
       SUM(COUNT(*) FILTER (WHERE outcome = 'break')) OVER w
         / SUM(COUNT(*)) OVER w                        AS break_rate_7d
FROM m
GROUP BY day_ist
WINDOW w AS (ORDER BY day_ist ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
ORDER BY day_ist;

-- 2 · Owner work queue with ageing buckets (age at close, from the register).
CREATE OR REPLACE TABLE dash_owner_queue AS
WITH r AS (
    SELECT *, CASE WHEN age_days < 0  THEN 'not yet due'
                   WHEN age_days <= 7  THEN '0-7 d'
                   WHEN age_days <= 14 THEN '8-14 d'
                   WHEN age_days <= 21 THEN '15-21 d'
                   ELSE '22+ d' END AS age_bucket
    FROM exception_register
)
SELECT owner, class, age_bucket,
       COUNT(*)        AS items,
       SUM(n_txns)     AS txns,
       SUM(impact_inr) AS impact_inr,
       MAX(age_days)   AS oldest_days
FROM r GROUP BY ALL ORDER BY owner, class, age_bucket;

-- 3 · Fee audit by method and day: charged rate vs contract.
CREATE OR REPLACE TABLE dash_fee_daily AS
SELECT f.method, f.day_ist,
       COUNT(*)                                   AS txns,
       SUM(f.amount_inr)                          AS gross_inr,
       SUM(f.fee_inr) / SUM(f.amount_inr)         AS charged_rate,
       ANY_VALUE(f.contract_mdr)                  AS contract_rate,
       -- only flagged overcharges count as excess; sub-tolerance paisa rounding is ignored, as in the register
       COALESCE(SUM(f.excess_incl_gst_inr) FILTER (WHERE f.is_overcharged), 0) AS excess_incl_gst_inr,
       COUNT(*) FILTER (WHERE f.is_overcharged)   AS overcharged_txns,
       SUM(COALESCE(SUM(f.excess_incl_gst_inr) FILTER (WHERE f.is_overcharged), 0))
           OVER (PARTITION BY f.method ORDER BY f.day_ist) AS cum_excess_inr
FROM fee_audit f
GROUP BY f.method, f.day_ist
ORDER BY f.method, f.day_ist;

-- 4 · Settlement tracker: one row per payout with a plain-English status.
CREATE OR REPLACE TABLE dash_settlements AS
SELECT settlement_id, batch_date, due_date, bank_date, net_inr, bank_credit,
       CASE WHEN rule = 'S1_matched' AND bank_date <= due_date THEN 'Credited on time'
            WHEN rule = 'S1_matched'                         THEN 'Credited late'
            WHEN rule = 'S2_amount_differs'                  THEN 'Credited, amount differs'
            WHEN rule = 'S3_in_transit'                      THEN 'In transit (not yet due)'
            ELSE 'Overdue at close' END AS status,
       bank_date - due_date AS days_late,
       rule
FROM leg3_payout_bank
ORDER BY batch_date;

-- 5 · Break rate by payment method (leg 1, cut-off rows excluded, as in the Day 2 test).
CREATE OR REPLACE TABLE dash_method_breaks AS
SELECT COALESCE(l.payment_mode, l.method) AS method,
       COUNT(*)                                        AS keys,
       COUNT(*) FILTER (WHERE c.outcome = 'break')     AS break_keys,
       COUNT(*) FILTER (WHERE c.outcome = 'break') / COUNT(*)::DOUBLE AS break_rate,
       SUM(l.value_inr) FILTER (WHERE c.outcome = 'break') AS break_inr
FROM leg1_match l JOIN rule_catalogue c USING (rule)
WHERE c.outcome IN ('matched', 'break')
GROUP BY 1 ORDER BY break_rate DESC;

-- 6 · Headline KPIs (one row).
CREATE OR REPLACE TABLE dash_kpis AS
SELECT
    (SELECT SUM(amount_inr) FROM cash_bridge WHERE kind = 'start')                                AS erp_net_sales_inr,
    (SELECT amount_inr FROM cash_bridge WHERE kind = 'end')       AS bank_credits_inr,
    (SELECT amount_inr FROM cash_bridge WHERE kind = 'residual')                                  AS unexplained_inr,
    (SELECT SUM(matched_keys) / SUM(keys)::DOUBLE FROM dash_daily_status)                         AS auto_match_rate,
    (SELECT SUM(keys) FROM dash_daily_status)                                                     AS leg1_keys,
    (SELECT COUNT(*) FROM exception_register)                                                     AS open_items,
    (SELECT COUNT(*) FROM exception_register WHERE class = 'break')                               AS break_items,
    (SELECT SUM(impact_inr) FROM exception_register WHERE class = 'break')                        AS break_inr,
    (SELECT SUM(impact_inr) FROM exception_register WHERE class = 'timing')                       AS timing_inr,
    (SELECT SUM(impact_inr) FROM exception_register WHERE class = 'claim')                        AS claim_inr,
    (SELECT COUNT(*) FROM exception_register WHERE class = 'break' AND cum_share_of_breaks <= 0.80)
      + 1                                                                                         AS items_for_80pct,
    (SELECT COUNT(*) FROM dash_settlements WHERE status = 'Credited on time')                           AS payouts_on_time,
    (SELECT COUNT(*) FROM dash_settlements)                                                       AS payouts;
