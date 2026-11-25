-- 30 · Fee audit: every captured October payment re-priced at the contracted MDR.
-- expected fee = amount × contract rate (rounded to the paisa); GST = 18% of fee.

CREATE OR REPLACE TABLE fee_audit AS
SELECT
    g.txn_id, g.order_id, g.method, g.ts_ist, CAST(g.ts_ist AS DATE) AS day_ist, g.settlement_id,
    g.amount_inr, g.fee_inr, g.gst_inr,
    c.mdr                                           AS contract_mdr,
    ROUND(g.amount_inr * c.mdr, 2)                  AS expected_fee_inr,
    g.fee_inr / g.amount_inr                        AS charged_rate,
    ROUND(g.fee_inr - ROUND(g.amount_inr * c.mdr, 2), 2)              AS excess_fee_inr,
    ROUND((g.fee_inr - ROUND(g.amount_inr * c.mdr, 2)) * (1 + p.gst_rate), 2) AS excess_incl_gst_inr,
    (g.fee_inr - ROUND(g.amount_inr * c.mdr, 2)) > p.fee_tolerance    AS is_overcharged
FROM gw_payments g
JOIN contract_mdr c USING (method)
CROSS JOIN params p
WHERE g.status = 'captured' AND g.in_period_ist;

-- Daily effective rate per method, with LAG to spot the day a rate stepped up.
CREATE OR REPLACE TABLE fee_daily AS
WITH d AS (
    SELECT method, day_ist, COUNT(*) AS txns,
           SUM(fee_inr) / SUM(amount_inr) AS eff_rate,
           ANY_VALUE(contract_mdr) AS contract_mdr,
           SUM(excess_incl_gst_inr) AS excess_incl_gst_inr
    FROM fee_audit GROUP BY ALL)
SELECT *,
       eff_rate - LAG(eff_rate) OVER (PARTITION BY method ORDER BY day_ist) AS dod_change,
       SUM(excess_incl_gst_inr) OVER (PARTITION BY method ORDER BY day_ist) AS cum_excess_inr
FROM d;

-- Rate step changes: day-on-day jump of more than 10 bps in the effective rate.
CREATE OR REPLACE TABLE fee_rate_changes AS
SELECT method, day_ist, eff_rate - dod_change AS rate_before, eff_rate AS rate_after
FROM fee_daily WHERE dod_change > 0.0010;
