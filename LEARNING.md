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
