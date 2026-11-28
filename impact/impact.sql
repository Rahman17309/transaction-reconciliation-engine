-- Impact model: what the October exceptions are worth, in ₹, and what they cost per year if
-- nothing changes. Runs on top of the engine tables (sql/*.sql); reads no truth labels.
--
-- Three questions, three sections:
--   A. Cash to recover now        one-off amounts we can claim back this close
--   B. Books wrong if closed now  revenue misstatement if October closed without the engine
--   C. Recurring cost per year    money that leaves every month until the cause is fixed
--   D. Revenue at risk per year   exposure that is a loss only if goods shipped unpaid (kept out of C)
--
-- Every assumption is a row in impact_params, so finance can change it and re-run.

CREATE OR REPLACE TABLE impact_params AS
SELECT 0.10  AS cost_of_funds_pa,      -- assumption: short-term borrowing cost, used for cash held by the gateway
       0.005 AS materiality_share,     -- assumption: performance materiality = 0.5% of revenue (common audit rule of thumb)
       365   AS days_per_year,
       12    AS months_per_year,
       DATE '2026-10-31' AS close_date;

-- Month facts the lines are built from.
CREATE OR REPLACE TABLE impact_facts AS
WITH reg AS (
    SELECT exception_type, SUM(impact_inr) AS inr, COUNT(*) AS items
    FROM exception_register GROUP BY 1
),
days AS (SELECT COUNT(DISTINCT day_ist) AS n_days FROM fee_audit)
SELECT
    (SELECT amount_inr FROM cash_bridge WHERE kind = 'start')                         AS erp_net_sales,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'SETTLEMENT_OVERDUE'), 0)     AS held_settlement,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'FEE_ABOVE_CONTRACT'), 0)     AS fee_claim,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'CHARGEBACK_NOT_BOOKED'), 0)  AS chargebacks,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'NO_GATEWAY_RECORD'), 0)      AS no_gateway_record,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'BOOKED_PAYMENT_FAILED'), 0)  AS payment_failed,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'REFUND_NO_CREDIT_NOTE'), 0)  AS refund_no_cn,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'GATEWAY_NOT_BOOKED'), 0)     AS captured_not_booked,
    COALESCE((SELECT inr FROM reg WHERE exception_type = 'AMOUNT_MISMATCH'), 0)        AS checkout_discount,
    (SELECT MAX(days_overdue_at_close) FROM leg3_payout_bank WHERE rule = 'S4_overdue') AS held_days_at_close,
    (SELECT n_days FROM days)                                                           AS n_days;

-- Fee leakage at the CURRENT rate, per method. If a method's effective rate stepped up during
-- the month (fee_rate_changes, found with LAG in sql/30), only the days since the step count:
-- that is what PayFlux will keep charging. Otherwise the whole month is the run rate.
CREATE OR REPLACE TABLE fee_run_rate AS
WITH regime AS (
    SELECT f.method, COALESCE(MAX(c.day_ist), MIN(f.day_ist)) AS regime_start
    FROM fee_audit f LEFT JOIN fee_rate_changes c USING (method)
    GROUP BY 1
),
by_method AS (
    SELECT f.method, r.regime_start,
           SUM(f.amount_inr)                                                     AS month_volume,
           SUM(f.amount_inr)          FILTER (WHERE f.day_ist >= r.regime_start) AS regime_volume,
           SUM(f.excess_incl_gst_inr) FILTER (WHERE f.day_ist >= r.regime_start AND f.is_overcharged) AS regime_excess,
           SUM(f.excess_incl_gst_inr) FILTER (WHERE f.is_overcharged)                   AS month_excess
    FROM fee_audit f JOIN regime r USING (method)
    GROUP BY ALL
)
SELECT b.*,
       regime_excess / regime_volume                                   AS excess_per_rupee,
       month_volume / x.n_days                                         AS avg_daily_volume,
       regime_excess / regime_volume * month_volume / x.n_days * p.days_per_year AS annual_leakage
FROM by_method b CROSS JOIN impact_facts x CROSS JOIN impact_params p
WHERE month_excess > 0;   -- only rows above the paisa tolerance count (same rule as the claim)

CREATE OR REPLACE TABLE impact_lines AS
WITH f AS (SELECT * FROM impact_facts), p AS (SELECT * FROM impact_params),
lines AS (
    -- A · cash to recover now (one-off)
    SELECT 'A' AS section, 1 AS ord, 'Held settlement (19 Oct batch)' AS line, 'Treasury' AS owner,
           'Escalate to PayFlux; book a receivable from the gateway' AS action,
           f.held_settlement AS month_inr, NULL::DOUBLE AS annual_inr, 'one-off' AS basis FROM f
    UNION ALL SELECT 'A', 2, 'Fee overcharge refund claim', 'Treasury / vendor manager',
           'Claim the October excess with the fee_audit extract', f.fee_claim, NULL, 'one-off' FROM f
    UNION ALL SELECT 'A', 3, 'Chargebacks to contest (upper bound)', 'Finance (AR)',
           'Contest with delivery proof inside the gateway window', f.chargebacks, NULL, 'one-off, upper bound' FROM f

    -- B · revenue misstatement if October closed without reconciling (+ = overstated)
    UNION ALL SELECT 'B', 1, 'Receipts with no captured payment (no record + failed)', 'Finance (AR)',
           'Collect or reverse the receipts', f.no_gateway_record + f.payment_failed, NULL, 'overstated' FROM f
    UNION ALL SELECT 'B', 2, 'Refunds paid out, no credit note', 'Finance (AR)',
           'Raise the credit notes', f.refund_no_cn, NULL, 'overstated' FROM f
    UNION ALL SELECT 'B', 3, 'Chargebacks netted, not booked', 'Finance (AR)',
           'Book the chargebacks', f.chargebacks, NULL, 'overstated' FROM f
    UNION ALL SELECT 'B', 4, 'Checkout discounts not in the ERP', 'E-commerce ops',
           'Send the final checkout amount to the ERP', f.checkout_discount, NULL, 'overstated' FROM f
    UNION ALL SELECT 'B', 5, 'Captured payments never booked', 'Finance (GL)',
           'Book the receipts', -f.captured_not_booked, NULL, 'understated' FROM f

    -- C · recurring cost per year if nothing changes
    UNION ALL SELECT 'C', 1, 'Fee overcharge at current rates: ' || method, 'Treasury / vendor manager',
           'Get the contract rate reinstated; add the fee audit to every close',
           regime_excess / regime_volume * month_volume, annual_leakage, 'run rate x 365 days'
    FROM fee_run_rate
    -- D · revenue at risk: a receipt with no captured payment is a loss only if the goods shipped,
    -- which the ERP extract can't show, so it is kept out of the cost total in C
    UNION ALL SELECT 'D', 1, 'Receipts with no captured payment', 'Finance (AR) + E-commerce ops',
           'Post receipts from the capture event; check these orders against the dispatch log',
           f.no_gateway_record + f.payment_failed, (f.no_gateway_record + f.payment_failed) * p.months_per_year,
           'month x 12; at risk if goods ship unpaid' FROM f, p
    UNION ALL SELECT 'C', 3, 'Chargebacks', 'Finance (AR)',
           'Track chargeback rate by method; contest with evidence',
           f.chargebacks, f.chargebacks * p.months_per_year, 'month x 12' FROM f, p
    UNION ALL SELECT 'C', 4, 'Cost of funds on a held payout (per occurrence)', 'Treasury',
           'Daily payout-to-bank check; escalate on day 1 overdue',
           f.held_settlement * f.held_days_at_close / p.days_per_year * p.cost_of_funds_pa, NULL,
           'per occurrence, days overdue at close x cost of funds' FROM f, p
)
SELECT * FROM lines ORDER BY section, ord, line;

-- Headline numbers.
CREATE OR REPLACE TABLE impact_summary AS
SELECT
    f.erp_net_sales,
    (SELECT SUM(month_inr) FROM impact_lines WHERE section = 'A')                              AS cash_to_recover,
    (SELECT SUM(month_inr) FROM impact_lines WHERE section = 'B')                              AS net_revenue_overstatement,
    (SELECT SUM(ABS(month_inr)) FROM impact_lines WHERE section = 'B')                         AS gross_revenue_misstatement,
    (SELECT SUM(month_inr) FROM impact_lines WHERE section = 'B') / f.erp_net_sales            AS overstatement_share,
    f.erp_net_sales * p.materiality_share                                                       AS materiality_threshold,
    (SELECT SUM(month_inr) FROM impact_lines WHERE section = 'B') > f.erp_net_sales * p.materiality_share AS is_material,
    (SELECT SUM(annual_inr) FROM impact_lines WHERE section = 'C' AND line LIKE 'Fee overcharge%') AS fee_leakage_annual,
    (SELECT SUM(annual_inr) FROM impact_lines WHERE section = 'C')                             AS recurring_cost_annual,
    (SELECT SUM(annual_inr) FROM impact_lines WHERE section = 'D')                             AS revenue_at_risk_annual,
    (SELECT SUM(ABS(impact_inr)) FILTER (WHERE class = 'break' AND age_days > 14) FROM exception_register) AS breaks_over_14d_inr,
    (SELECT COUNT(*) FILTER (WHERE class = 'break' AND age_days > 14) FROM exception_register) AS breaks_over_14d,
    (SELECT MEDIAN(age_days) FILTER (WHERE class = 'break') FROM exception_register)          AS median_break_age_days
FROM impact_facts f, impact_params p;
