# Reflection — PII Detection & Data Quality Validation

All figures are from the committed reports for a run over 2,998 loaded rows
(3,003 lines; 5 excluded as ragged before parsing).

---

## 1. Top 5 data quality issues

**1. `created_date` was almost never a date — 261 failures (26% of all
failures), all fixed.** 227 rows carried `2021-03-15 09:00:00`, a single
timestamp repeated across an entire bulk-import block; the rest were US-format
or timezone-stamped. *Fix:* parse the whole value, then fall back to splitting
off a time component, then Excel serials and epoch seconds. *Impact:* the
column was unusable for any cohort or retention analysis, and the repeated
timestamp would have shown ~200 customers joining in the same second. Now 0
failures.

**2. `customer_id` is not a primary key — 19 values repeated across 44 rows,
still unfixed.** *Fix:* none possible. A duplicate key cannot be resolved
without the source system; guessing which row owns the id would silently
corrupt the join. *Impact:* the highest-severity issue in the file. Until it is
resolved, no join to any other system is trustworthy, and 44 rows could be
double-counted in any aggregate. This is the one finding that should block a
production load outright.

**3. Phone numbers arrived in nine different shapes — 76 failures, 72 fixed.**
Parenthesised, dotted, bare digits, `+1`-prefixed, extensions, vanity strings.
*Fix:* strip extension and separators, drop a leading US country code, reformat
to `XXX-XXX-XXXX`; remove what cannot yield ten digits. *Impact:* deduplication
by phone was impossible, since the same subscriber appeared under multiple
spellings. Four valid international numbers remain non-conforming by design.

**4. Missing values hid behind twelve different spellings — ~1.8% per column.**
`NULL`, `N/A`, `n/a`, `-`, `?`, `TBD`, `Unknown`, `Not Provided` and whitespace
all mean absent. *Fix:* one sentinel set in config, applied by a single
`is_null_sentinel()`. *Impact:* counting only empty strings would have
understated the gap by roughly an order of magnitude and produced a
falsely clean completeness report.

**5. `account_status` had 26 distinct values for 3 valid states — 45 failures,
27 fixed.** *Fix:* an unambiguous canonical map handles case, separators, typos
(`actiive`), letter codes and the legacy `dormant`. *Impact:* any segmentation
by account state was wrong. 20 rows remain: `closed`, `pending` and `banned`
are real lifecycle states the schema cannot express, and the numeric/boolean
codes have no codebook — mapping them would have invented account state on
financial records.

Overall: **1,011 failures → 484, pass rate 71.8% → 85.8%**, 98.2% row
retention. The residual is genuinely unfixable in-file: 150 missing required
values, 44 duplicate keys, 79 malformed emails.

---

## 2. Risk assessment — sensitivity of the detected PII

Eight of ten columns carry PII; five are HIGH sensitivity. 23,609 PII cells are
populated across 2,998 records — an exposure score of 59,043 out of a possible
59,960, or **98.5%**. This file is almost entirely personal data.

- **`email`, `phone` (direct, HIGH)** — unique contact identifiers. 2,909
  addresses and 2,925 numbers were recovered by regex. A leak hands an attacker
  a ready-made phishing, smishing and credential-stuffing target list.
- **`date_of_birth` (quasi, HIGH)** — immutable and a standard identity
  verification factor. Unlike a password, a birth date cannot be reset after a
  breach.
- **`address` (quasi, HIGH)** — enables physical-world harm and household
  linkage. The profiler also found one address shared by 21 records, which is a
  fraud signal in its own right.
- **`income` (sensitive, HIGH)** — financial data, carrying discrimination and
  predatory-targeting risk.
- **`first_name`, `last_name` (direct, MEDIUM)** — weak alone, strong in
  combination.

The measured finding matters more than the classification. **Full name alone is
unique for only 4.6% of records, but name plus date of birth is unique for
99.6%.** So is date of birth plus address, with no name at all. Removing names
would not have anonymised this file — which is precisely why the masking spec
covers DOB and address.

Aggravating context: this is a financial services dataset, so a breach is
reportable, and the records support both identity theft and account takeover
without any further enrichment.

---

## 3. Masking trade-offs — utility vs privacy

Masking works where it was applied. `DOB + address` uniqueness collapses from
**99.6% to 0.1%**.

But income is deliberately left unmasked, because the masked extract exists for
analytics and income is the column analysts actually need. Income is **99.2%
unique**, which makes it a quasi-identifier on its own. The consequence is
measurable: `DOB + income` still identifies **99.4%** of records after masking,
essentially unchanged from 99.6% before. Even name-initial plus birth year
leaves 73.7% unique.

The masked extract is therefore **pseudonymous, not anonymous**. Two further
gaps are deliberate and worth stating plainly: `customer_id` is preserved as a
join key, so anyone holding both files can re-link them completely; and
preserving the email domain and the last four phone digits — both required by
the brief's format — retains real signal.

That is a defensible trade if the extract stays inside an access-controlled
environment, and indefensible if it is treated as safe to share widely. The
honest options are to band income into ranges, drop it, or keep it and keep the
access controls. We kept it, and wrote down why. What would be wrong is
shipping it while *calling* the file anonymised.

---

## 4. Validation strategy — effectiveness

Pydantic over Great Expectations: per-row model validation produces structured
per-field errors that map directly onto "document specific row/column
failures", without a large dependency tree for a 3k-row file. Uniqueness does
not fit a per-row model, so it is checked separately across the frame — a
reminder that row-level and dataset-level constraints are different kinds of
rule.

What worked well:

- **Splitting failures into auto-fixable and needs-source.** 1,011 failures
  became a work order of 383 repairable and 628 requiring the source system.
  That distinction drove the entire cleaning stage.
- **Validating twice.** Pre-clean is the assessment; post-clean is the proof.
  Without the second pass, "we fixed it" would be an assertion.
- **Rules deliberately narrowed.** `NAME_RE` accepts `O'Brien`, `Ngũgĩ` and
  `van der Berg`. A naive `[A-Za-z]+` rule would have flagged real customers as
  dirty — a false positive is a data quality defect too.

What the rules do not catch: anything requiring outside knowledge. An email
that is well-formed but belongs to someone else, an address that parses but
does not exist, an income that is plausible but wrong. Format validation
establishes that data is *well-formed*, never that it is *true*. Three rows in
this file are field-shifted by an embedded comma — every cell parses, and the
values are simply in the wrong columns.

The 30% failure-rate gate is also a blunt instrument. It passed at 14.2% while
44 duplicate primary keys remained, which is arguably a blocking defect. A
better gate would be per-rule: any duplicate key fails the run regardless of
the aggregate rate.

---

## 5. Production operations

**Scheduling.** Daily, triggered by arrival of the source extract rather than
by wall-clock time, so a late upstream file delays the run instead of producing
an empty one. The pipeline is idempotent — same input, same output — so a
retry is always safe.

**Failure handling.** Stages are classified critical (`load`, `clean`, `mask`)
or reporting. A critical failure aborts and returns a non-zero exit code; a
failing report is recorded and the run continues, because a broken report
should not block the data products. Everything is logged to `logs/pipeline.log`
with per-stage timings, and the execution report is written even on abort.
Stages that never ran report `not reached` rather than zero, so an aborted run
cannot be misread as having produced empty output.

**What I would add before trusting this in production:**

- Alerting on exit code and on the quality gate, not just a log file.
- Trend tracking. A one-off 14.2% failure rate is context-free; a jump from 14%
  to 40% is an upstream incident and is the signal actually worth paging on.
- Quarantine review with an owner and an SLA. `customers_quarantined.csv` is
  currently written and never read, which makes it a queue with no consumer.
- Schema-change detection at load. Today a renamed column would surface as a
  wave of confusing validation failures rather than a clear "the contract
  changed".
- Retention and access control on the raw file, which is the highest-risk
  artifact in the repository.

---

## 6. Lessons learned

**The cleaning rule is more dangerous than the dirty data.** Three separate
rules corrupted correct records before being caught: `.title()` rewrote
`van der Berg` to `Van Der Berg` across ~330 already-valid surnames, a naive
alphabetic check would have rejected `O'Brien`, and blanket phone normalization
would have deleted valid international numbers. Each looked obviously right.
The pattern is the same every time — a rule written for the common case,
applied without asking what legitimate data it also matches.

**Tests find what review does not.** The suite was written last and immediately
caught a bug five modules deep: `clean_date` split on whitespace before trying
the format list, so `March 15, 1985` became `March`, matched nothing and was
silently *nulled*. Long-form dates were being destroyed rather than converted,
and the same bug in the validator reported them as unrecoverable. Neither the
reports nor manual inspection had revealed it, because destroyed data leaves no
failure behind — it just becomes a gap.

**A metric that looks better may mean data got worse.** Nulling every
problematic value would have raised the pass rate substantially while
discarding evidence. Deciding early to repair format but leave semantic
violations visible kept the numbers honest, at the cost of a less impressive
headline. Related: the validator initially reported `PASS` on the raw data
because a release gate meant for cleaned output was being applied pre-clean.

**Measure privacy claims; do not assert them.** "We masked the PII" felt
complete until re-identification was actually computed and showed 99.4% of
records still unique via DOB and income. The masking was not wrong — it was
sound, and still insufficient on its own.

**Reports about sensitive data are themselves sensitive.** The first PII report
printed real addresses as examples, and the first masker left the second
address in a two-address cell fully readable, publishing six real email
addresses into the "masked" output. Both were caught by scanning the outputs
for source values rather than by reading the code.

**The unglamorous decisions carried the most weight.** Centralising every
threshold in one config file prevented a class of bug that would have been
almost impossible to find later — the validator and the cleaner quietly
disagreeing about what "valid" means, with each report internally consistent
and the pair contradictory.
