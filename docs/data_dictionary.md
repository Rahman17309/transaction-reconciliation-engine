# Data dictionary

All four raw files describe the **October 2026 close** of a mid-size Indian D2C retailer that collects online payments through a payment gateway (called *PayFlux* here; fictional). The files are produced by `src/generate_data.py` (seed 42) and mimic what each system actually exports, quirks included.

## Raw files (`data/raw/`)

### `erp_ledger.csv` — sales receipts and credit notes booked in the ERP
| Column | Type | Notes |
|---|---|---|
| voucher_no | text | `RV/nnnnnn` receipts, `CN/nnnnn` credit notes. Unique. |
| entry_type | text | `receipt` or `credit_note` |
| posting_ts | text | **IST**, three formats: `2026-10-05 14:22:10`, `05/10/2026 14:22`, `05-Oct-2026 02:22 PM` |
| order_ref | text | `ORD-nnnnnnn`; test rows use `TEST-nnnn` |
| customer_id | text | `Cnnnnnn`; `C_TEST` for test rows |
| payment_mode | text | UPI, Card, Net Banking, Wallet; blank for manual journals |
| sales_channel | text | Website, App, Instagram |
| amount_inr | text | Rupees, 2 dp. ~5% of rows exported with thousands separators (`1,391.00`). Credit notes negative. |

### `gateway_txns.csv` — PayFlux transaction report
| Column | Type | Notes |
|---|---|---|
| txn_id | text | `pf_…` payments, `rf_…` refunds |
| merchant_ref | text | Order id as sent by the checkout integration: `ORD-1000377`, `ord-1000377`, ` ORD1000377 `, `ORD_1000377` |
| type | text | `payment` or `refund` |
| created_at_utc | text | **UTC** ISO-8601. The export is filtered on the UTC date (1–31 Oct UTC). |
| method | text | lower-case: upi, card, netbanking, wallet |
| status | text | `captured`, `failed` (payments), `processed` (refunds) |
| amount_paise, fee_paise, gst_paise | int | **Paise**. Fee = MDR; GST = 18% of fee. Refunds carry no fee. |
| settlement_id | text | `setl_YYYYMMDD` = the IST batch day; blank for failed attempts |

### `gateway_payouts.csv` — PayFlux settlement file
| Column | Notes |
|---|---|
| settlement_id | One batch per IST day of captures and refunds |
| settlement_date | Date the money is sent (T+1, Sundays roll to Monday) |
| gross / fee / gst / refund / chargeback / net _paise | net = gross − fee − GST − refunds − chargebacks |
| utr | Bank transfer reference that appears in the bank narration |

### `bank_statement.csv` — current account statement, 1–31 Oct
| Column | Notes |
|---|---|
| value_date | `dd-mm-yyyy` |
| narration | Free text. PayFlux credits contain the UTR (`PFLXnnnnnnnnnn`). Also COD remittances, vendor payments, salary, rent, tax and bank charges. |
| credit, debit, balance | Rupees. Opening balance ₹25,00,000. |

## Clean tables (`data/clean/`, written by `src/clean.py`)
| Table | Grain | Key additions |
|---|---|---|
| `ledger.parquet` | one ERP entry | `posting_ts_ist` (datetime), `order_id` (normalised), numeric `amount_inr`; test rows and duplicate postings removed |
| `gateway_txns.parquet` | one gateway transaction | `order_id` (normalised), `ts_utc`, `ts_ist`, amounts in ₹, `in_period_ist` flag; duplicate export rows removed |
| `payouts.parquet` | one settlement batch | amounts in ₹, `batch_date`, `lag_days` |
| `bank.parquet` | one statement line | parsed `value_date`, extracted `utr`, `category` |
| `quarantine.parquet` | one removed row | `source`, `reason` — nothing is dropped silently |
| `dq_log.csv` | one cleaning rule | rows affected and action taken |
| `validation_report.csv` | one check | pass/fail |

## Ground truth (`data/truth/labels.csv`)
Every injected problem with its key (order id, txn id or settlement id). Used **only** to score the matching engine later in the week; the engine never reads it.
