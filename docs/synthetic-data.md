# Synthetic Data Defect Catalog
### Specification for generating `customers_raw.csv`

**Purpose:** Complete inventory of defects to inject into the synthetic dataset for the PII Detection & Data Quality Validation Pipeline project.

**Target schema:** `customer_id, first_name, last_name, email, phone, date_of_birth, address, income, account_status, created_date`

---

## 1. File & Encoding Level

Defects that break the loader before any column validator runs.

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Unquoted embedded comma | `123 Main St, Apt 4, Springfield` | Ragged row, column shift |
| Embedded newline in field | Multi-line address cell | Row count mismatch |
| Unescaped quote | `John "Johnny" Smith` | Parser error |
| Mojibake | `JosÃ©` (UTF-8 read as latin-1) | Silent corruption |
| BOM on header | `\ufeffcustomer_id` | Column name lookup fails |
| Non-breaking space | `John\u00a0Smith` | `.strip()` does not catch it |
| Zero-width character | `John\u200bSmith` | Invisible mismatch |
| Mixed line endings | `\r\n` vs `\n` | Trailing `\r` on last column |
| Extra field in row | 11 fields instead of 10 | Ragged row |
| Missing field in row | 9 fields instead of 10 | Ragged row |

---

## 2. Null Sentinels (applies to every column)

Scatter these across all columns so completeness analysis must normalize before counting.

| Type | Examples | Problem it creates |
| :--- | :--- | :--- |
| Empty | `""` | Baseline missing |
| Explicit null keywords | `NULL`, `null`, `None`, `NaN` | Counted as present |
| Text abbreviations | `NA`, `N/A`, `n/a`, `#N/A` | Counted as present |
| Symbols | `-`, `--`, `?` | Counted as present |
| Placeholders | `TBD`, `Unknown`, `Not Provided` | Counted as present |
| Whitespace only | `"   "` | Counted as present |

---

## 3. `customer_id` (Integer — unique, positive)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Normal | `1042` | Baseline |
| Missing | `""` / `NULL` | Completeness issue |
| Duplicate (exact row) | Same ID, identical data | Uniqueness violation — dedupe |
| Duplicate (conflicting) | Same ID, different email | Uniqueness violation — reconcile |
| Negative | `-12` | Invalid value |
| Zero | `0` | Invalid value |
| Non-numeric | `AB-1042` | Type mismatch |
| Float round-trip | `1042.0` | `astype(int)` fails if any NaN present |
| Leading zeros | `0001042` | Lost on numeric cast |
| Scientific notation | `1.042E+03` | Type mismatch |

---

## 4. `first_name` / `last_name` (String — 2–50 chars, alphabetic)

### Invalid variations

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Normal | `John`, `Smith` | Baseline |
| lowercase | `john` | Normalization needed |
| UPPERCASE | `JOHN` | Normalization needed |
| MiXeD case | `jOhN` | Normalization needed |
| Leading/trailing spaces | `"  John "` | Trim needed |
| Too short | `J` | Length rule violation |
| Too long | 60-char string | Length rule violation |
| Digits embedded | `J0hn` | Alphabetic rule violation |
| Punctuation | `John!!` | Alphabetic rule violation |
| Emoji | `John 🙂` | Encoding + validation |
| Junk values | `Test`, `asdf`, `XXX` | Passes format, fails sense |
| Missing | `""` | Completeness issue |
| Full name in `first_name` | `John Smith` + empty `last_name` | Field misuse |
| First/last swapped | `Smith` / `John` | Undetectable without reference data |

### Valid-but-often-rejected variations

Include these deliberately — a naive `isalpha()` check will produce false positives.

| Variation | Example | Why the validator fails it |
| :--- | :--- | :--- |
| Non-ASCII letters | `José`, `Müller`, `Ngũgĩ` | `isalpha()` passes but ASCII regex fails |
| Apostrophe | `O'Brien`, `D'Angelo` | Non-alphabetic character |
| Hyphen | `Mary-Jane` | Non-alphabetic character |
| Lowercase particles | `van der Berg`, `de la Cruz` | Internal spaces; title-case corrupts them |
| Suffix | `Jr.`, `III` | Period and numerals |
| Initial only | `J.` | Length + period |

---

## 5. `email` (String — valid format)

### Invalid variations

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Valid | `john.doe@gmail.com` | Baseline |
| Missing `@` | `john.doe_gmail.com` | Format violation |
| Missing domain | `john.doe@` | Format violation |
| Double `@` | `john@@gmail.com` | Format violation |
| No TLD | `john.doe@gmail` | Format violation |
| Spaces inside | `john.doe @gmail.com` | Format violation |
| Wrong order | `@gmail.com.john` | Format violation |
| Consecutive dots | `john..doe@x.com` | Invalid, often passes loose regex |
| Leading/trailing dot in local part | `.john@x.com` | Invalid, often passes |
| `mailto:` prefix | `mailto:j@x.com` | Prefix strip needed |
| Two addresses in one cell | `a@x.com; b@y.com` | Split required |
| Missing | `""` | Completeness issue |

### Valid-but-often-rejected variations

| Variation | Example | Why the validator fails it |
| :--- | :--- | :--- |
| Plus-addressing | `john+newsletter@gmail.com` | `+` excluded from naive regex |
| Subdomain + multi-part TLD | `j@mail.company.co.uk` | Multiple dots in domain |
| Mixed case | `John.Doe@GMAIL.COM` | Requires normalization decision |

---

## 6. `phone` (String — normalize to `XXX-XXX-XXXX`)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Clean dashes | `555-123-4567` | Baseline |
| Parentheses | `(555) 123-4567` | Format normalization |
| Dots | `555.123.4567` | Format normalization |
| No separators | `5551234567` | Format normalization |
| Mixed separators | `555-123.4567` | Regex brittleness |
| Country code | `+1 555-123-4567` | Strip or retain decision |
| International | `+44 20 7946 0958`, `+250 788 123 456` | Format rule too narrow |
| Extension | `555-123-4567 x89` | Normalization ambiguity |
| Too short | `555-1234` | Length violation |
| Too long | 11+ digits, no `+` | Length violation |
| Contains letters | `555-CAL-LME` | Vanity number, needs mapping or reject |
| Excel scientific notation | `5.55123E+09` | Silent corruption |
| Dropped leading zero | `788123456` (was `0788123456`) | Unrecoverable digit loss |
| Stored as numeric type | `5551234567` as int64 | Type coercion on read |
| Placeholder junk | `000-000-0000`, `111-111-1111`, `123-456-7890` | Passes format, meaningless |
| Missing | `""` | Completeness issue |

---

## 7. `date_of_birth` (Date — `YYYY-MM-DD`)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| ISO (correct) | `1985-03-15` | Baseline |
| US format | `03/15/1985` | Parse variant |
| Slash ISO | `1985/03/15` | Parse variant |
| Swapped parts | `15-03-1985` | Parse variant |
| Long form | `March 15, 1985` | Parse variant |
| Abbreviated month | `15-Mar-1985`, `Mar 15 1985` | Parse variant |
| With time | `1985-03-15 00:00:00` | Truncation needed |
| Timezone-aware | `1985-03-15T00:00:00+02:00` | Offset can shift the calendar day |
| Ambiguous D/M vs M/D | `03/04/1985` | Genuinely undecidable |
| Two-digit year | `03/15/85` | Y2K windowing (`29` → 1929 or 2029?) |
| Excel serial | `31121` | Needs origin-based conversion |
| Unix epoch | `479692800` | Needs epoch conversion |
| Literal garbage | `invalid_date`, `N/A` | Parse failure |
| Impossible date | `1985-02-30` | Parse failure |
| Zero date | `0000-00-00` | MySQL export artifact |
| Epoch sentinel | `1900-01-01`, `1970-01-01` | Parses as valid, is not real |
| Max-date placeholder | `9999-12-31`, `1111-11-11` | Parses as valid, is not real |
| Age > 150 | `1820-05-01` | Business rule violation |
| Future date | `2030-01-01` | Business rule violation |
| Under 18 | `2010-06-01` | KYC failure, not a parse error |
| Valid leap day | `2000-02-29` | Must **pass** (contrast: `1900-02-29` must fail) |
| Missing | `""` | Completeness issue |

---

## 8. `address` (String — 10–200 chars)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Normal | `123 Main Street, Springfield` | Baseline |
| Too short | `123 Main` | Length violation |
| Too long | 250-char rambling string | Length violation |
| Exactly 10 chars | `123 Main S` | Boundary test (inclusive?) |
| Exactly 200 chars | — | Boundary test (inclusive?) |
| Only numbers | `12345` | Passes length, fails sense |
| Junk | `.`, `-`, `TBD`, `same as above` | Passes or fails length, meaningless |
| ALL CAPS | `123 MAIN STREET` | Normalization |
| Shared by many rows | Same address ×20 | Possible fraud signal |
| Missing | `""` | Completeness issue |

---

## 9. `income` (Numeric — non-negative, ≤ $10M)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Normal | `52000.50` | Baseline |
| Negative | `-5000` | Business rule violation |
| Over $10M | `25000000` | Business rule violation |
| Exactly 10,000,000 | `10000000` | Boundary test (≤ vs <) |
| Zero | `0` | Valid value or sentinel? Ambiguous |
| Currency symbol | `$52,000`, `€52000` | Type mismatch |
| Currency suffix | `52000 USD` | Type mismatch |
| Comma thousands | `52,000.50` | Type mismatch |
| European decimal | `52.000,50` | Ambiguous with comma-thousands |
| Accounting negative | `(5000)` | Parsed as text |
| Suffix notation | `52k` | Parse failure |
| Range | `50000-60000` | Not a scalar |
| Spelled out | `fifty thousand` | Parse failure |
| Numeric sentinel | `-1`, `999999999` | Passes type, fails sense |
| Excess precision | `52000.5555` | Rounding decision |
| Missing | `""` | Completeness issue |

---

## 10. `account_status` (String — `active`, `inactive`, `suspended`)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| Valid | `active`, `inactive`, `suspended` | Baseline |
| Wrong case | `Active`, `ACTIVE` | Normalization |
| Extra spaces | `" active "` | Trim needed |
| Hyphen/space variants | `in-active`, `in active` | Normalization |
| Typo | `actve` | Fuzzy match or reject |
| Invalid value | `closed`, `pending`, `banned` | Category violation |
| Legacy real values | `dormant` | Business decision, not a typo |
| Letter codes | `A`, `I`, `S` | Mapping needed |
| Numeric codes | `0`, `1`, `2` | Mapping needed |
| Boolean | `TRUE` / `FALSE` | Lossy — no suspended state |
| Missing | `""` | Completeness issue |

---

## 11. `created_date` (Date — `YYYY-MM-DD`)

| Variation | Example | Problem it creates |
| :--- | :--- | :--- |
| ISO (correct) | `2023-06-01` | Baseline |
| US format | `06/01/2023` | Parse variant |
| Datetime | `2023-06-01 14:32:10` | Truncation needed |
| Timezone offset | `2023-06-01T14:32:10+02:00` | Day-shift risk |
| Two-digit year | `06/01/23` | Y2K windowing |
| Excel serial | `45078` | Needs conversion |
| Garbage | `N/A`, `2023-13-45` | Parse failure |
| Future date | `2027-01-01` | Impossible record |
| Before company founding | `1990-01-01` | Impossible record |
| Bulk-import block | 200 rows, identical timestamp | Suspicious uniformity |
| Missing | `""` | Completeness issue |

---

## 12. Cross-Field Rules

Column-level validation cannot catch these. Implement as DataFrame-level checks.

| Rule violated | Example | Problem it creates |
| :--- | :--- | :--- |
| `created_date` before `date_of_birth` | Born 1995, created 1990 | Logical impossibility |
| Age < 18 at `created_date` | DOB 2010, created 2023 | Regulatory / KYC failure |
| Duplicate email across IDs | Two rows share `j@x.com` | Duplicate identity |
| Duplicate phone across IDs | Two rows share `555-123-4567` | Duplicate identity |
| Duplicate name + DOB across IDs | Same person, different ID | Entity resolution needed |
| `income = 0` with `account_status = active` | — | Logical contradiction |
| Many rows share one address | 20 accounts, one address | Fraud signal |

---



