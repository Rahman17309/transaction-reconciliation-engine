# Learning notes: Transaction Reconciliation Engine

A running log of what was built each day, the concepts behind it, and interview questions with model answers.

---

## Day 1: Problem, data and data quality

### What I did
- Wrote the business problem the way a finance controller would frame it: prove that sales were paid, that the gateway paid what it owed, and that the money reached the bank.
- Built a seeded generator (`src/generate_data.py`) for four realistic exports: ERP ledger, gateway transactions, gateway payouts and bank statement. It injects 12 kinds of known problems and labels each one in `data/truth/labels.csv`.
- Built a cleaning pipeline (`src/clean.py`) with 18 logged rules, a quarantine table for every removed row, and 19 validation checks. All 19 pass.

### Key concepts
- **Three-way reconciliation.** Ledger ↔ gateway checks *completeness and accuracy of revenue*. Gateway ↔ payout checks *what we were owed*. Payout ↔ bank checks *cash actually received*. Each leg catches different failures.
- **MDR (merchant discount rate).** The gateway's fee as a % of the transaction, plus 18% GST on the fee in India. Rates differ by payment method, so fees have to be checked per method.
- **Settlement / payout.** The gateway nets fees, GST, refunds and chargebacks out of each day's captures and sends one bank transfer (identified by a UTR). One bank line therefore maps to *many* transactions.
- **Paise vs rupees.** Gateways store integer paise to avoid floating-point error. Convert once, at the edge.
- **Time zones.** Indian books are in IST (UTC+5:30), while gateway APIs and exports are often in UTC. A UTC-dated export cuts the month at 05:30 IST.
- **Quarantine, not delete.** Removed rows are kept with a reason, so an auditor can see exactly what was excluded and why.

### Interview Q&A
**Q: Why generate synthetic data instead of using a public dataset?**
A: No public dataset has all three legs (ERP, gateway and bank) for the same transactions, because that data is confidential. A seeded generator with documented assumptions gives realistic messiness *and* a ground truth, so I can measure the engine's precision and recall per exception type. You can't do that with unlabeled real data.

**Q: How do you identify duplicate postings without deleting legitimate repeat orders?**
A: I define a duplicate on the business key, not on the whole row: same order id, same entry type, same amount, different voucher number. A customer re-ordering gets a new order id, so it isn't caught. I keep the first voucher, quarantine the later one, and log the count (72 here).

**Q: What validation checks would you run before trusting a cleaned financial dataset?**
A: I'd check uniqueness of the keys, key-format regexes, sign conventions (receipts positive, credit notes negative), arithmetic identities (GST = 18% of fee; payout net = gross − fee − GST − refunds − chargebacks), cross-source totals (payout gross = sum of that batch's captured transactions), and a recomputed bank running balance. If any check fails, the pipeline exits non-zero.

---

## Day 2: EDA and first findings

### What I did
- First-pass full outer join of ERP receipts to gateway payments on the normalised order id: 97.7% exact matches, with 468 keys (₹4.30 L) left to decide.
- Showed that 112 of the 277 "missing" rows come from the export being pulled by UTC date while the books are in IST.
- Found a one-directional pattern in amount differences (gateway always 5–20% lower), which points to checkout discounts.
- Checked every transaction's fee against the contracted MDR. Wallet moved to 2.25% on 15 Oct, and 87 card payments were charged 2.4%.
- Matched gateway refunds to ERP credit notes (31% missing), and payouts to bank lines by UTR (one held batch, one in transit).
- Ran chi-square tests to see whether breaks concentrate in a payment method or channel. They don't (p = 0.200 and 0.317).

### Key concepts
- **Full outer join with an indicator.** `merge(..., how="outer", indicator=True)` labels each key `both`, `left_only` or `right_only`. It's the fastest way to see completeness gaps on both sides at once.
- **Timing vs break.** A timing difference clears on its own next period (in-transit settlement, cut-off). A break needs action (missing booking, overcharge, unposted refund). Separating the two is the main job of reconciliation.
- **Directional bias as evidence.** Random keying errors go both ways. When every difference has the same sign and a similar size, look for a systematic process.
- **Effective rate vs contract rate.** Effective rate = total fee / total gross. If it sits above contract, drill down by method and by day to find *when* and *where* it changed.
- **Chi-square test of independence.** Compares observed counts in a contingency table (method × break/no-break) with the counts expected if the two were independent. A large p-value means there's no evidence the break rate differs by group.

### Interview Q&A
**Q: Your ledger and gateway don't tie at month end. Where do you look first?**
A: I'd split the gap into timing and breaks before chasing individual rows. First the cut-off: are both sources on the same time zone and date basis? Here, 112 of 277 unmatched rows were just a UTC export against IST books. Then settlements in transit. Whatever is left after that is the real exception list.

**Q: How would you detect a gateway overcharging you?**
A: I'd recompute the expected fee per transaction as amount × contracted rate for that method, then compare it with the fee charged, allowing a paisa of rounding. Then I'd aggregate the excess by method and by day. A step change on a date (like the wallet move to 2.25% on 15 Oct) means a rate change. A small share of transactions at a higher rate (card at 2.4%) suggests misclassification, for example international or corporate cards. Either way you raise a claim with evidence.

**Q: Why did you run a chi-square test here, and what did it tell you?**
A: To decide who owns the fix. If breaks clustered in one payment method, it would be a technical integration issue for engineering. The test didn't reject independence (p = 0.200 by method, 0.317 by channel), so the breaks look like process issues for finance and e-commerce ops. I excluded the cut-off window first so that artefact didn't bias the test.

**Q: A settlement isn't in the bank statement. Is that a problem?**
A: It depends on when it was due. I compute the expected credit date from the contract (T+1 business day, no Sunday credits). If it's due after the period end, it's in transit: a normal reconciling item. If it was due inside the period and hasn't arrived, it's a real exception. Here the 19 Oct batch (₹6.42 L) was due on 20 Oct and arrived on 3 Nov, so it needs a follow-up with the gateway.

---

## Day 3: SQL deep-dive (the matching engine)

### What I did
- Rewrote the matching as seven DuckDB SQL files in `sql/`, run in order by `src/match.py`. The engine reads only the cleaned tables.
- Leg 1: a 9-rule cascade over a full outer join of ERP receipts and gateway payments. 98.05% of 23,976 keys auto-match; 112 cut-off rows become *timing*; 356 breaks (₹3.20 L) remain.
- Searched for mistyped order ids with a mutual-best `ROW_NUMBER()` match (same amount and method, within 10 minutes). None found.
- Fee audit: re-priced every capture at the contracted MDR, and used `LAG()` over daily effective rates to detect the wallet step from 2.00% to 2.25% on 15 Oct without hard-coding the date.
- Leg 2: rebuilt each payout from its transactions (many-to-one). 30 of 32 tie exactly; the two that don't are the batches cut by the UTC export window.
- Leg 3: matched payouts to bank credits by UTR and classified the two missing ones as overdue (19 Oct) and in transit (31 Oct) from the contractual due date.
- Built one exception register (619 items: 501 breaks worth ₹10.07 L, 2 fee claims, 116 timing items) with owner, action, age, rank and Pareto share.
- Built a cash bridge from ERP net sales (₹2,19,91,231.50) to PayFlux bank credits (₹2,04,49,800.31) that leaves ₹0.00 unexplained. The engine has 7 integrity checks; all pass.

### Key concepts
- **Rule cascade.** Each record gets the outcome of the *first* rule it satisfies. Putting it in one ordered `CASE` makes the policy explicit and guarantees no record is counted twice. Strict rules go first (exact), then tolerances, then explanations (timing), and "break" is what's left.
- **Matched / timing / break / claim.** Matched needs nothing. Timing clears by itself next period. A break needs a person. A claim is money to recover from a third party. Separating them is what turns 24k rows into a short work list.
- **Mutual-best matching.** When fuzzy-matching two unmatched sets, rank candidates from *both* sides with `ROW_NUMBER()` and keep a pair only if each is the other's top choice. That stops one record being consumed by two matches.
- **Many-to-one reconciliation.** A settlement is hundreds of transactions. Aggregate the detail by the batch key and compare each component (gross, fee, GST, refunds) with the header, not just the net.
- **Window functions for control checks.** `LAG()` finds step changes, `SUM() OVER (ORDER BY …)` recomputes running balances and cumulative shares, `RANK()` orders work within a type, and `QUALIFY` filters on a window result without a sub-query.
- **Cash bridge with a residual line.** Walk from one control total to the other with each reconciling item computed from the detail, and show the leftover explicitly. A non-zero residual means something isn't understood yet; here it surfaced a ₹256 cut-off item I'd otherwise have missed.

### Interview Q&A
**Q: Why write the matching engine in SQL rather than pandas?**
A: Finance and audit can read SQL, and the rules are the thing they need to review. Each file is one step with a clear output table, so you can query any intermediate result. It also runs unchanged in a warehouse (DuckDB locally, Snowflake or BigQuery in production) where the data actually lives. I kept Python only for orchestration, checks and charts.

**Q: How do you make sure a record isn't matched twice or dropped?**
A: Two ways. Structurally, each leg is one full outer join with a single `CASE`, so every row gets exactly one outcome. Then I assert it: the count of outcomes equals the count of source rows for receipts, gateway payments and refunds. The runner exits non-zero if any check fails.

**Q: The gateway's fee changed mid-month. How would you catch that automatically?**
A: Compute the effective rate (fee ÷ amount) per method per day, then use `LAG()` to compare each day with the previous one and flag a jump above a threshold (10 bps here). It found Wallet moving from 2.00% to 2.25% on 15 Oct with no date in the code, so it will catch the next change too.

**Q: What is a cash bridge and why is the residual line important?**
A: It walks from book revenue to cash received, one reconciling item at a time: breaks, fees, refunds, chargebacks, timing. Each item is computed from the matched detail, not typed in. The residual is whatever is left, and it should be zero. Showing it explicitly is the honest part: here it was ₹256 at first, and chasing it found captures in the 1 Oct payout that weren't in the export or the ERP.

**Q: You have 501 breaks. How would you prioritise them?**
A: By value and owner. I rank items within each type and compute the cumulative share of break value. Here one overdue payout is 63.7% of the value, and 72 items cover 80%. Each owner gets their top items first with a `QUALIFY ROW_NUMBER() OVER (PARTITION BY owner …)` query, and ageing shows what's at risk of falling past the chargeback or claim window.

---

## Day 4: Validation against ground truth

### What I did
- Wrote `src/score.py`, which compares what the engine flagged with the injected labels for all 12 problem types and reports precision, recall and F1 with Wilson 95% intervals. It keeps two views: **raw** (every label) and **in scope** (drops labels on activity outside October IST or outside the files finance received, each with a written reason).
- Result for October: **precision 100% on every type; recall in scope 99.90%** (2,073 of 2,075). Raw recall is 98.29%, and every raw-only miss is explained.
- Scoring found two engine bugs, both fixed in SQL:
  - **Refund false positives.** All 14 were 31 Oct refunds whose credit note was correctly posted in November. I added **F5 CN pending**, with the window learned from the matched pairs' posting lag (p99 = 29.0 h). Refund precision went from 89.47% to 100%.
  - **Cross-period match.** A sale booked at 23:59 on 31 Oct and captured on 1 Nov matched as "exact", but its cash is in the November payout. I added **R0 cross-period** as timing.
- Swept the two judgement calls. Any CN-pending window from 24 to 36 h gives the same result. Any amount tolerance from ₹1 up to just under ₹13 splits rounding from real discounts perfectly.
- Wrote `src/robustness.py`, which regenerates 20 new months (seeds 101–120) and runs the whole pipeline with the rules frozen. All integrity checks pass, the bridge leaves ₹0.00 unexplained every month, and precision is 100% everywhere. Recall is 99.63–99.90%, and all 106 misses are refunds parked as CN pending.
- Made every engine export sort deterministically, so reruns produce identical files and clean diffs.

### Key concepts
- **Precision vs recall in reconciliation.** Precision is the share of flagged items that are real; low precision wastes reviewer time and teaches people to ignore the list. Recall is the share of real problems flagged; low recall means money leaks silently. In finance ops you want both near 100%, and when you must trade, a "pending, re-check" queue is better than either a false alarm or a silent drop.
- **Wilson score interval.** A confidence interval for a proportion that behaves well near 0% or 100% and with small n. 11/11 chargebacks correct gives a 95% interval of about 74–100%, so a small type still needs more months of evidence.
- **Scope-adjusted scoring.** Some labels describe activity the review shouldn't flag (next-period captures). Counting them as misses punishes the right answer, but hiding them hides process problems. I report both views and list every excluded label with its reason.
- **Learned thresholds.** The CN-pending window is the 99th percentile of the observed refund → credit-note lag, so it adapts if finance gets faster or slower. Hard-coded "2 days" would silently go stale.
- **Sensitivity analysis.** Sweep each judgement parameter and look for a *flat region* around the chosen value. A result that changes a lot when a threshold moves slightly isn't trustworthy.
- **Out-of-sample testing on generated data.** One seed is one month. Rerunning on many seeds with frozen rules is the reconciliation equivalent of a hold-out set. It caught the R0 bug, which never occurs in the published month.

### Interview Q&A
**Q: How do you know your reconciliation engine is right?**
A: Three layers. First, structural integrity checks: every record gets exactly one outcome, payouts rebuild from their transactions, and the cash bridge leaves ₹0 unexplained. Second, I score it against ground truth. I generated the data with labelled problems, so I can measure precision and recall per exception type: 100% precision on all types and 99.90% recall in scope. Third, I check it out of sample: 20 freshly generated months with the rules frozen. A clean bridge alone isn't proof, because two wrong classifications can cancel out.

**Q: Your engine flagged refunds as breaks that weren't. What did you do?**
A: I looked at every false positive. All 14 were refunds on 31 Oct whose credit notes finance posted correctly in November, so it was a timing issue, not a break. Instead of guessing a grace period, I measured the actual refund → credit-note lag on the 294 matched pairs (median 14 h, p99 29 h) and used the p99 as a "pending" window. Refunds inside it go to a timing queue to re-check in early November. Precision went from 89.5% to 100%, at the cost of 2 real breaks that wait a few days in that queue instead of being lost.

**Q: Why report both "raw" and "in-scope" scores?**
A: Raw recall (98.29%) includes labels on activity the October close shouldn't flag, like overcharges on 1 Nov captures, which the engine correctly sends to next period. Counting those as misses would be misleading, but dropping them silently would be worse. So I show both and list every excluded label with its reason. The reasons were useful in themselves: most exclusions exist only because the gateway report is pulled by UTC date, which gives finance a concrete process fix.

**Q: How did you choose the ₹1 amount tolerance, and how would you defend it?**
A: With a sensitivity sweep. The largest rounding difference in the data is ₹1.00 and the smallest real checkout discount is ₹13.00, so any tolerance in that band gives exactly the same split. ₹1 sits at the conservative end. At ₹20, 7 real discounts would be auto-cleared, so I'd defend ₹1 as the tightest value that removes the rounding noise.

**Q: What did testing on 20 other months find that October didn't?**
A: A real bug. A sale booked at 23:59 on 31 Oct and captured seconds later on 1 Nov matched as "exact" in leg 1, but its cash was in the November payout, so the cash bridge had an unexplained line in 7 of the 20 months. October happened to have no such order. I added a cross-period rule that classes these as timing, and every month then reconciled to ₹0.00. It's the reason I don't trust a rule set tested on one sample.

**Q: Precision is 100%. Isn't that suspicious?**
A: It would be on real data, and I'd say so. Here the problems are injected by rules I documented, and the matching rules target those mechanisms, so very high scores are expected. The useful parts are the bugs scoring exposed, the trade-off curves, and stability on unseen months. On real data I'd expect lower numbers and would sample-audit flagged items by hand to estimate precision, because real data has no labels.

---

## Day 5: The reconciliation dashboard

### What I did
- Added a **semantic layer in SQL** (`dashboard/marts.sql`): six small marts on top of the engine tables (daily leg-1 status with a 7-day trailing break rate, owner work queue with ageing buckets, fee audit by method and day with a running overcharge, settlement tracker, break rate by method, headline KPIs).
- Wrote `src/dashboard.py`, which builds the marts, runs **7 tie-out checks** back to the engine (daily keys and break ₹ = leg-1 totals, owner queue = register items and ₹, fee excess = fee claims, every payout once, bridge steps sum to the gap, residual ₹0.00), and only then writes one self-contained HTML file. 7/7 pass.
- Four report pages: **Overview** (8 KPI cards, notes, cash bridge waterfall, daily outcome, items by type), **Exceptions** (slicers for owner, class, type, age and search over all 619 items, ageing matrix, Pareto, largest-first work queue, CSV export of the filtered selection), **Gateway fees** (daily effective MDR vs contract, cumulative overcharge, the claim) and **Settlements** (every payout by status, break rate by method).
- Exported the marts to `data/output/dashboard/*.csv` so the same model loads into Power BI.
- `src/screenshot_dashboard.py` renders each page with headless Chromium for the README; checked the slicers against the register (Finance (AR) breaks: 299 items, ₹2,79,126.50, the same in the page and in pandas) and the page at phone width (no horizontal scroll).
- New findings: daily auto-match is stable (97.67–99.22% on full days, 7-day break rate 1.33–1.60%), and **227 of 485 breaks (46.8%) are over 14 days old at close**.

### Key concepts
- **Semantic layer.** A thin set of business-shaped tables between raw outputs and visuals. Every visual reads one mart, so a number on screen traces to one query, definitions live in one place, and the same marts can feed Power BI, a notebook or an email.
- **Tie-out before publish.** A dashboard is a report, and a report that doesn't agree with its source is worse than none. Reconciling each mart back to the engine, and blocking publishing on failure, is the dashboard equivalent of a unit test.
- **Trailing (rolling) windows.** `SUM(x) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)` smooths daily noise so a single bad day doesn't read as a trend. A ratio of rolling sums is better than a rolling mean of daily ratios because busy days get their proper weight.
- **Ageing buckets.** Age = close date − event date, bucketed (0–7, 8–14, 15–21, 22+ days). Ageing turns a list into risk: old refunds and chargebacks drift past dispute windows, old receivables get harder to collect.
- **Page design by question.** One page per question the controller asks (does it tie, what do I work on, are we overcharged, did the cash arrive) rather than one page per table. KPI cards first, then the chart that explains them, then the detail.
- **Power BI equivalents.** Marts ≈ the model's tables; window functions ≈ DAX measures like running totals (`CALCULATE` + `DATESINPERIOD`); slicers ≈ the JS filters; "export data" ≈ the CSV button.

### Interview Q&A
**Q: Walk me through your reconciliation dashboard.**
A: Four pages, one per question. Overview: does it tie? It shows ERP net sales ₹2.20 Cr against bank credits ₹2.04 Cr, a cash bridge that explains the ₹15.41 L gap to ₹0.00, and a 98.05% auto-match rate. Exceptions: what do I work on? Slicers by owner, type and age over all 619 open items, sorted by ₹, with an export for the team tracker; 66 items cover 80% of break value. Gateway fees: are we charged the contract? It shows the wallet rate stepping from 2.00% to 2.25% on 15 Oct and a ₹3,953 claim. Settlements: did the cash arrive? 30 of 32 payouts were on time, one is overdue and one is in transit.

**Q: How do you make sure the dashboard shows the right numbers?**
A: Two things. All logic lives in SQL marts, not in the charting code, so there's one definition per number. Then the build runs seven tie-out checks back to the engine (for example, the owner queue must sum to the register's 619 items and ₹ total, and fee excess must equal the fee claims) and refuses to write the page if any fail. I also checked the interactive filters against pandas for one slice.

**Q: Why build it as HTML instead of Power BI?**
A: For a public portfolio, an HTML file opens for anyone without a licence, and it's diffable in Git. The design is the same as a Power BI report: a model of marts, measures as window functions, slicers and drill to detail. The marts are exported as CSVs, so rebuilding it in Power BI is loading six tables and recreating the visuals. At work I'd use whatever the finance team already has.

**Q: What did the dashboard show that the analysis didn't?**
A: Ageing. Nearly half the breaks (227 of 485) were more than two weeks old on close day, because a once-a-month reconciliation finds a 3 Oct error in November. That turns the recommendation from "fix these breaks" into "run the engine daily", which I'll size tomorrow. It also confirmed the break rate is flat through the month (7-day rate 1.33–1.60%), so there was no outage to chase.

**Q: How would you handle a dashboard user who says a number is wrong?**
A: Trace it. Every card maps to one mart and one SQL query, and the mart ties to the engine, which ties to the source files through the cash bridge. So I can show the path from the card to the rows behind it. If it really is wrong, the fix goes in the SQL with a new tie-out check so it can't come back, and I tell the user what changed.

---

## Day 6: ₹ impact sizing and recommendations

### What I did
- Wrote an impact model in SQL (`impact/impact.sql`) on top of the engine tables. It sizes the October exceptions three ways: **A · cash to recover now**, **B · revenue misstatement if the books closed today**, and **C · recurring cost per year if nothing changes**. Assumptions (cost of funds 10% a year, materiality 0.5% of revenue) are rows in `impact_params`, not literals in the queries.
- Priced fee leakage at the **current** rate, not the October average: `fee_run_rate` takes the step date the Day 3 `LAG()` query found and only uses the days since.
- Re-sized the same model on the 20 regenerated months (`python src/impact.py --months 20`) and put a bootstrap 95% interval on each annual figure.
- Executed `notebooks/06_impact.ipynb` (4 charts) and wrote `reports/recommendations.md`: 8 ranked actions with owner, ₹ at stake, effort and the file that proves it.
- Results: ₹6,70,844.84 to recover (95.7% is the held payout); revenue overstated by ₹2,28,750.50 = 1.040% of sales, 2.08× materiality, and material in 20 of 20 test months; wallet + card overcharge ₹81,299 a year incl. GST at current rates (wallet ₹75,318), about 21× the October claim; ₹1.61 L a month of receipts with no captured payment.

### Key concepts
- **Cash vs accuracy vs run rate.** One rupee can mean different things. A held payout is cash we're owed (recover it). An unbooked refund is a misstatement (fix the books). A wrong fee rate is a recurring loss (fix the cause). Mixing them in one total double-counts and hides the decision each needs.
- **Materiality.** Auditors set a threshold below which errors don't change a reader's decision; performance materiality is often 0.5–1% of revenue or 5% of profit. Testing the net misstatement against it turns "lots of small breaks" into "the October accounts would be wrong".
- **Net vs gross misstatement.** Errors in opposite directions (unbooked captures understate, unpaid receipts overstate) partly cancel in the net (₹2.29 L) but not in the gross (₹3.53 L). Report both: the net decides materiality, the gross shows how much cleanup is needed.
- **Run rate after a regime change.** If a rate changes mid-period, the period average understates the future. Find the change date (here with `LAG()` over daily effective rates), measure only after it, then annualise on average daily volume.
- **Revenue at risk vs loss.** A receipt with no payment is a loss only if the goods shipped. Saying "at risk", keeping it out of the cost total, and naming the data that would confirm it (the dispatch log) is more honest than calling it a loss. A chart has to follow the same rule: putting it in a "cost" panel would say the opposite of the text.
- **Bootstrap interval for an annual figure.** Resample the 20 monthly values with replacement 10,000 times, take the mean each time, and read the 2.5th and 97.5th percentiles. It shows how stable the figure is to monthly noise, but not to the rates changing.
- **₹-days (value × days unseen).** A simple exposure measure for detection speed: it weights big, old breaks most and makes the case for running the reconciliation daily.

### Interview Q&A
**Q: How did you put a rupee value on the reconciliation findings?**
A: I split it by the decision each needs. Cash to recover now: ₹6.71 L, almost all a payout PayFlux held for 11 days. Books wrong if closed today: revenue overstated by ₹2.29 L, 1.04% of sales, which is twice a 0.5% materiality threshold. Recurring cost: ₹3.83 L a year, fees above contract (₹81k incl. GST) plus chargebacks. And I kept ₹1.61 L a month of receipts with no payment in a separate revenue-at-risk bucket, because it isn't a proven loss. Every figure comes from a SQL model on the engine output, with the two assumptions held as parameters.

**Q: The October fee claim is only ₹3,953. Why did you rank it third?**
A: Because the claim isn't the point, the rate is. The wallet MDR went up on 15 October, so October only had half a month of it. Priced at the new rate on normal wallet volume it's about ₹75k a year including GST, about 22 times October's ₹3,445 wallet overcharge; with cards it's ₹81k, about 21 times the full claim. It's also cheap to fix: one email with the per-transaction extract.

**Q: How do you know the materiality conclusion isn't a one-month fluke?**
A: I ran the same impact model on 20 months generated with different seeds. The overstatement ranged from 0.85% to 1.21% of sales and was above 0.5% in all 20. I'm careful about what that proves, though: those months share the same error rates, so it shows the conclusion survives normal month-to-month noise, not that real error rates won't change.

**Q: ₹19 lakh a year of "receipts with no payment" sounds big. Would you present it as a loss?**
A: No. It's revenue at risk: we booked a sale and the gateway has no captured payment. It only becomes a loss if the order shipped. So the recommendation is to check those 171 October orders against the dispatch log to size the real loss, and to post receipts from the payment-capture event instead of order creation so it stops happening.

**Q: How did you rank the recommendations?**
A: Cash first, because it's owed now and gets harder to collect with time. Then fixes that stop a recurring loss, ordered by annual ₹ and effort. Then process changes that keep the books right, like auto-creating credit notes and pulling the gateway report by IST date. Each recommendation has an owner, a ₹ figure and the output file that proves it, so nobody has to take my word for it.

**Q: What would you change if this were real data?**
A: Three things. Replace the synthetic rates with 12 months of actuals, so the annual figures come from observed variation. Get the dispatch log to turn revenue at risk into a confirmed loss. And agree the materiality threshold with the auditors instead of assuming 0.5%. The SQL wouldn't change: the parameters live in one table.
