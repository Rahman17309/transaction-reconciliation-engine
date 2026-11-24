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
