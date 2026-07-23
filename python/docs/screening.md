# Screening a file of entities

Batch-screen a CSV of people, companies, or other entities against
sanctions and watchlist data, and get a reviewable CSV of scored match
candidates back.

This is the workflow for the recurring compliance question: "here is a
spreadsheet of counterparties — which of them appear in the data?" The
`yente-cli screen` command runs one match query per row with bounded
concurrency and writes one output row per candidate. The same engine is
available in Python as `match_iter` for embedding in a pipeline — see
[the closing section](#the-same-in-python). For one-off queries, use
[`yente-cli match`](cli.md) instead; for the mechanics of matching
itself (thresholds, algorithms, filters), the
[SDK tutorial](tutorial.md#3-matching-in-depth) is the reference.

## A first run

Start from a CSV with one entity per row. The file can carry any
columns; only the ones you map are sent to the API:

```csv
ref,full_name,born,country
a-101,Arkadii Romanovich Rotenberg,1951-12-15,ru
a-102,Jane Ordinary Smith,,nz
a-103,Nicolas Maduro,,ve
```

Screening needs two things: a schema for the rows (`-s Person`) and a
mapping from your columns to [FollowTheMoney](https://followthemoney.tech/)
(FtM) properties (`-i COLUMN=prop`, repeatable):

```bash
yente-cli screen customers.csv \
  -s Person \
  -i full_name=name -i born=birthDate -i country=country \
  -d sanctions -l 2
```

The command reports progress on stderr and writes
`customers.out.csv`:

```csv
ref,full_name,born,country,match_id,match_caption,match_score,match,match_topics,match_error
a-101,Arkadii Romanovich Rotenberg,1951-12-15,ru,Q4398633,Arkady Romanovich Rotenberg,1.000,true,poi;export.control;sanction;debarment;role.oligarch,
a-103,Nicolas Maduro,,ve,Q58132,Nicolás Maduro,1.000,true,role.pol;debarment;role.pep;poi;sanction;role.diplo,
a-103,Nicolas Maduro,,ve,Q22278983,Nicolas Ernesto Maduro Guerra,0.943,true,role.pol;role.pep;sanction;debarment,
```

```
screened 3 rows: 3 candidates on 2 rows, 0 errors
```

Three things to notice:

- **One row per candidate.** Row `a-103` produced two candidates, so it
  appears twice. Rows keep their input order.
- **Input columns pass through.** `ref`, `full_name`, `born`, and
  `country` are copied onto every candidate row, so the output joins
  back to your source data (or stands alone as a review file) without
  bookkeeping.
- **No-hit rows disappear.** `a-102` matched nothing and is absent. Pass
  `--include-empty` to keep such rows with blank result columns — useful
  when the output doubles as evidence that every row was screened.

Empty cells are fine: `a-103` has no `born` value, so no `birthDate` is
sent for that row. Map every column you have — more properties mean
more scoring signal. If a mapped property doesn't exist on the schema,
the command exits with a fuzzy suggestion (`birthdate` → "did you mean
`birthDate`?") before any query is sent.

The output file is the optional second positional argument. It defaults
to `<input>.out.csv`; `-` writes to stdout, and `-` as the input reads
from stdin, so the command drops into shell pipelines.

## Reading the result columns

Result columns carry a `match_` prefix so they never collide with your
input columns (if one of your columns is itself called `match_id`, the
command refuses to run rather than guess):

- **`match_id`** — the candidate's OpenSanctions entity identifier.
  Feed it to `yente-cli fetch` for the full record, or add `--url` for
  a `match_url` column linking to the entity's page.
- **`match_caption`** — the candidate's display name.
- **`match_score`** — the match score, 0 to 1.
- **`match`** — `true` when the score crossed the threshold (server
  default 0.70). This is the server's yes/no verdict; the score is the
  nuance behind it.
- **`match_topics`** — what the candidate is flagged as (`sanction`,
  `role.pep`, `crime.fraud`, …), joined with the `--join` separator
  (default `;`). Run `yente-cli ref topics` for the vocabulary.
- **`match_error`** — empty on success; see
  [failed rows](#failed-rows-dont-abort-the-run).

Pull any candidate property into an extra column with `-o prop=COLUMN`,
e.g. `-o country=candidate_country` to compare the candidate's
territory against your input side by side.

To see *why* a candidate scored as it did, add `--explanation`. The
`match_explanation` column lists the features that contributed, highest
first — `name_match=1.00`, `dob_year_matches=0.90;name_match=0.83`, and
so on. During manual review this answers the most common question about
a mid-score candidate: is the score carried by the name alone, or
corroborated by birth date, territory, or identifiers?

## Threshold, cutoff, and triage

Two flags shape what comes back, and they do different jobs:

- **`--threshold`** sets where the `match` column flips to `true`
  (server default 0.70). It doesn't remove anything.
- **`--cutoff`** drops candidates below the given score from the
  response entirely (server default 0.50).

A practical triage pattern is two-phase. First run wide: default
threshold, default cutoff, `--explanation`, and skim the ranked list —
this is the run where you notice near-misses and calibrate. Then
produce the escalation list with `--match`, which keeps only
`match=true` candidates:

```bash
# Review file: everything the matcher considered plausible
yente-cli screen customers.csv review.csv -s Person \
  -i full_name=name -i born=birthDate --explanation

# Escalation list: only threshold-crossing hits
yente-cli screen customers.csv hits.csv -s Person \
  -i full_name=name -i born=birthDate --match
```

If the escalation list is too noisy, raise `--threshold` rather than
`--cutoff` — the near-misses stay visible in the review file. Reserve
`--cutoff` for trimming output volume on very large runs.

## Mixed files: a schema per row

When one file carries both people and companies, name the schema in a
column and pass `--schema-column`:

```csv
type,name,country
Person,Nicolas Maduro,ve
Company,Bank Rossiya,ru
```

```bash
yente-cli screen parties.csv --schema-column type -s Person -i name=name
```

When both flags are given, the column wins and `-s` fills rows where
the cell is empty. Rows naming an unknown or non-matchable schema
become error rows; they don't stop the run.

If you genuinely can't tell people from organizations — raw payee
strings, unlabeled list entries — screen with `-s LegalEntity`, which
matches both. This costs scoring accuracy: schema-specific features
like `birthDate` comparison only activate for `Person` queries. The
trade-off is covered in
[the tutorial](tutorial.md#querying-a-parent-schema-matches-descendants-too).

## Failed rows don't abort the run

A 50,000-row run should not die at row 49,000. When a row fails — an
invalid value, an unknown schema in `--schema-column`, a request error
that survived retries — the row is written with the `match_error`
column set and blank result columns, and the run continues. Error rows
are always written, whether or not `--include-empty` is set.

The exit code summarizes the run:

| Code | Meaning |
|---|---|
| `0` | Completed; at least one candidate written. |
| `1` | Completed; zero candidates. |
| `2` | Usage error — nothing was screened (bad mapping, column collision, missing file). |
| `3` / `4` | API or network error before streaming started. |
| `5` | Completed, but some rows failed. Check `match_error`. |

On exit `5`, extract the failed rows and re-run just those. The error
rows carry all input columns, so the failed subset is itself a valid
input file — select the rows where `match_error` is non-empty (with
`qsv`, `csvkit`, or a spreadsheet), drop the `match_` columns, and feed
the result back to `screen`.

## Throughput

`--workers` (default 20) bounds how many match requests are in flight
at once; input is read lazily, so memory stays flat regardless of file
size. The output preserves input order even though requests complete
out of order.

If the API starts returning rate-limit errors in `match_error`, lower
`--workers`. Raising it past the default mostly helps on self-hosted
yente instances with capacity to spare.

## The same in Python

The CLI is a wrapper around `Client.match_iter()`, which streams
`(key, entity)` pairs through `/match` with the same bounded
concurrency. Use it when screening is one stage of a larger pipeline —
reading from a database, writing to a queue — rather than CSV-to-CSV:

```python
import csv

from yente_client import Client, MatchError, Person

with Client(api_key="...") as client, open("customers.csv") as fh:
    rows = {
        # `or None` turns empty CSV cells into unset properties.
        row["ref"]: Person(name=row["full_name"], birthDate=row["born"] or None)
        for row in csv.DictReader(fh)
    }
    stream = client.match_iter(
        rows.items(), workers=8, datasets=["sanctions"], on_error="collect"
    )
    for ref, response in stream:
        if isinstance(response, MatchError):
            print(ref, "failed:", response.exception)
        elif response.top is not None:
            print(ref, "->", response.top.caption, f"{response.top.score:.2f}")
```

Results arrive in completion order, not input order — the key (`ref`
here) ties each response back to its row. With `on_error="collect"`,
failures come back in-band as `MatchError` values instead of raising,
mirroring the CLI's `match_error` column. `AsyncClient.match_iter()` is
the `async for` equivalent. Signatures are in the
[API reference](api/client.md).

## Where to go next

- [SDK tutorial](tutorial.md) — matching in depth: thresholds,
  algorithms, filters, and the FtM entity model.
- [CLI overview](cli.md) — the full command surface around `screen`.
- `yente-cli screen --help` — every flag, with worked examples.
