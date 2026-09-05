# Project Report — PII Detection & Data Quality Validation

## What we built

A program that takes a messy customer file, reports everything wrong with it,
finds the personal information inside, fixes what can be fixed, hides the
personal details, and writes a report at every step. One command, about four
seconds. Every number below comes from the files in `reports/`.

## 1. How we made the test data

We had no real customer file, so we wrote a program to invent one. That was
better than using real data anyway: we decided in advance exactly what was
wrong with it, which gave us an answer key to check our detection against.

The generator makes **3,000 records**, roughly 70% clean, with **1,630 faults**
deliberately injected into the rest. It uses a fixed starting number, so anyone
running it gets an identical file and can reproduce these results exactly.

### What we broke, column by column

| Column | Faults we introduced | Count |
| :--- | :--- | ---: |
| `created_date` | 200 records sharing one import timestamp, US-style dates, timezones, impossible dates like `2023-13-45` | 337 |
| `customer_id` | Missing, text IDs (`AB-1234`), scientific notation, leading zeros, negatives, **duplicates** | 162 |
| `first_name` / `last_name` | Missing, junk (`J0hn`, `XXX`), wrong capitalisation, invisible padding characters | 305 |
| `address` | Missing, 20 records sharing one address, ALL CAPS, too short, too long, no city | 152 |
| `email` | Missing, wrong case, no `@`, no domain, two addresses in one cell | 145 |
| `phone` | Missing, dots/brackets/spaces, letters (`555-CAL-LME`), fake numbers, wrong digit counts | 142 |
| `account_status` | Missing, case variants, typos (`actiive`), codes (`A`/`I`/`S`, `0`/`1`/`2`), unknown states | 134 |
| `income` | Missing, currency symbols, thousands commas, ranges (`24460-34460`), negatives, `fifty thousand` | 121 |
| `date_of_birth` | Missing, US format, impossible (`1985-02-30`), the text `invalid_date`, ages over 150, future dates | 118 |
| The file itself | Byte-order mark, mixed line endings, rows with wrong column counts, an embedded line break, duplicate rows | 14 |

One deliberate trap: we put **valid but awkward names** — `O'Brien`,
`van der Berg`, `Ngũgĩ`, `Mary-Jane` — in the *clean* pile. A careless rule that
only accepts A–Z flags these real customers as dirty. Catching that was the
point.

## 2. What we found

The program read 2,998 rows; five were unreadable and set aside. Checking the
rest against the rules, **845 rows (28%) failed**, breaking 1,011 rules between
them.

It sorted those into **383 the program could repair itself** and **628 needing
someone to go back to the source system**. That split became the cleaning plan.

## 3. The five biggest problems

**1. Account creation dates were rarely dates — 261 failures, a quarter of
everything.** 227 carried the identical timestamp `2021-03-15 09:00:00`, the
fingerprint of a bulk import. We taught the program ten date formats plus
Excel's internal number format, and to strip attached times. Without this the
column was useless — it showed 200 people joining in the same second.
**All 261 fixed.**

**2. The customer ID is not unique — 19 IDs repeated across 44 rows.** We fixed
none, deliberately: you cannot tell which record owns a repeated ID without the
source system, and guessing would silently corrupt every future join. This is
the most serious problem in the file. Until it is resolved, linking this data to
any other system is unreliable and 44 customers may be double-counted.
**This should block the file from going live.**

**3. Phone numbers came in nine shapes — 76 failures.** Brackets, dots, spaces,
country codes, extensions, letters. We stripped punctuation and reformatted to
`555-123-4567`, removing anything that could never make ten digits. **72 fixed.**
You cannot spot a duplicate customer when their number is written five ways.

**4. "Missing" was spelled twelve ways — about 1.8% of every column.** `NULL`,
`N/A`, `-`, `?`, `TBD`, `Unknown`, blank spaces and more. We built one shared
list of everything meaning "no value". Counting only empty cells would have
under-reported the gaps roughly tenfold.

**5. Account status had 26 values for 3 valid states — 45 failures.** We
translated the unambiguous cases: capitalisation, typos, letter codes, and the
legacy word `dormant`. **27 fixed.** The remaining 18 we refused to guess —
`closed`, `pending` and `banned` are real states our system has no slot for, and
codes like `0` and `TRUE` have no documented meaning. Inventing an account
status on a financial record is worse than admitting we don't know it.

## 4. Personal information in the file

**Eight of ten columns hold personal information; five are high-sensitivity.**
23,609 cells contain personal data — 98.5% of everything that could. This is not
a file with some personal data in it; it is almost entirely personal data.

| Type | Columns | Why it's sensitive |
| :--- | :--- | :--- |
| Directly identifies someone | Name, email, phone | A leak hands an attacker a ready-made contact list for scams |
| Identifies in combination | Date of birth, address | A birth date verifies identity — and unlike a password, can never be changed |
| Financially sensitive | Income | Discrimination and predatory targeting |

**The key finding is about combinations.** A full name alone identifies only
**4.6%** of people uniquely — there are plenty of Johns and Smiths. But **name
plus date of birth identifies 99.6%**, and so does date of birth plus address
with no name at all.

Removing names would *not* have made this file anonymous. That is why the
masking rules cover birth dates and addresses too.

## 5. Hiding the personal data — and what it costs

`John` becomes `J***`, `john.doe@gmail.com` becomes `j***@gmail.com`, addresses
become `[MASKED ADDRESS]`, `1985-03-15` becomes `1985-**-**`. **17,330 values
masked, none leaked.**

We then measured whether it worked instead of assuming:

| Combination | Identifies uniquely, before | After masking |
| :--- | ---: | ---: |
| Birth date + address | 99.6% | **0.1%** |
| Name + birth date | 99.6% | 73.7% |
| **Birth date + income** | 99.6% | **99.4%** |

Masking the address worked almost perfectly. But we **deliberately left income
unmasked**, because analysis is the point of the shared file and income is what
analysts need. Incomes are nearly all different — **99.2% are unique** — so
income acts like a fingerprint, and birth year plus income still singles out
99.4% of customers.

The masked file is therefore **not anonymous**, only safer. The customer ID is
also kept so the file can be joined to other systems, meaning anyone holding
both files can undo the masking entirely.

That trade is acceptable *if* the file stays inside the company behind access
controls, and unacceptable if published. The real options are: band incomes into
ranges, drop income, or keep it and keep the controls. We chose the third and
wrote down why. What would be wrong is sharing it while calling it anonymous.

## 6. Did the checks work?

We used a library called Pydantic, which reports failures one field at a time —
which record, which column, which rule — rather than just "this file is bad".
Uniqueness had to be checked separately, since you cannot tell a record is a
duplicate by looking at it alone.

What worked: sorting failures into "fixable" and "ask the source system", which
turned 1,011 errors into an actionable list; checking twice, before and after
cleaning, so "we fixed it" is demonstrated rather than claimed; and writing
rules loose enough that `O'Brien` and `van der Berg` pass, since flagging real
customers is its own data quality problem.

What the checks **cannot** do is tell whether data is true. An email can be
perfectly formatted and belong to someone else. They confirm data is
*well-formed*, never that it is *correct*.

One honest weakness: our quality gate passes anything under a 30% failure rate.
The cleaned file passed at 14.2% while still holding 44 duplicate IDs. A better
gate would fail outright on a duplicate key, whatever the overall percentage.

## 7. Running this for real

**Schedule.** Daily, triggered by the source file arriving rather than by the
clock, so a late file delays the run instead of processing nothing. The program
is repeatable — same input, same output — so a retry is always safe.

**When something breaks.** Steps are split into essential (reading, cleaning,
masking) and reporting. An essential failure stops the run and returns an error
code; a failed report is logged but does not block the data. A report is written
even when the run fails, and steps that never ran say "not reached" rather than
zero, so a failed run cannot be mistaken for an empty one.

**Before trusting it in production we would add:**

- Alerts on failure and on the quality gate, not just a log nobody reads.
- Quality tracked over time. 14% means nothing alone; 14% jumping to 40% means
  something upstream broke, and that is worth waking someone for.
- An owner for the rejected records — we currently write them to a file nobody
  reads.
- A check for the source file changing shape, so a renamed column reports "the
  format changed" instead of a confusing wave of errors.
- Restricted access and a deletion schedule for the raw file, the most sensitive
  thing in the project.

## 8. What we learned

**The cleaning rule is more dangerous than the messy data.** Three "obvious"
rules damaged correct records. The worst rewrote `van der Berg` as
`Van Der Berg` across about 330 already-correct surnames. Each time the cause
was identical: a rule written for the common case, applied without asking what
valid data it would also catch.

**Automated tests found what reading the code did not.** Written last, the test
suite immediately exposed a bug five files deep: dates like `March 15, 1985`
were being **deleted** instead of converted. Nobody spotted it because deleted
data leaves no error behind — it just becomes a gap.

**A better-looking number can mean worse data.** Deleting every problem value
would have raised our pass rate while destroying evidence. Fixing formatting but
leaving real problems visible gives a less impressive headline and a more honest
file.

**Privacy has to be measured, not asserted.** "We masked the personal data" felt
finished until we calculated it and found 99.4% of records still identifiable
through income and birth year.

**Reports about sensitive data are themselves sensitive.** Our first PII report
printed real addresses as examples, and our first masking step left the second
address in a two-address cell readable — publishing six real email addresses
into the file we were calling "masked". Both were caught by scanning the outputs
for real values, not by reading the code.

## Results

| | Before | After |
| :--- | ---: | ---: |
| Rows failing at least one rule | 845 | **417** |
| Total rule failures | 1,011 | **484** |
| Pass rate | 71.8% | **85.8%** |
| Records kept | — | 2,943 of 2,998 (98.2%) |
| Personal values masked | — | 17,330, zero leaks |

The 484 remaining failures are not oversights: 150 missing required values, 44
duplicate IDs and 79 malformed emails, all of which need the source system
rather than more clever code.
