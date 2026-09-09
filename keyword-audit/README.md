# Google Ads keyword & negative-keyword auditor

Reproduces a manual Google Ads keyword audit deterministically, without an LLM, from the raw CSV exports.

**You're probably here because:**
- You're staring at a `Search keyword report.csv` export and need it cleaned, deduplicated, and ranked instead of eyeballing a few thousand rows.
- A campaign is "running" but getting almost no impressions, and you suspect a keyword-level bid is silently overriding the ad group default.
- You want to know whether your negative keyword list is blocking your own keywords — something the Google Ads UI never tells you directly.
- Conversion tracking is broken (every row shows 0 conversions) and you need a ranking signal that still means something.

## Usage

```bash
python ads_audit.py --keywords Search_keyword_report.csv \
                    --negatives Negative_keyword_report.csv \
                    --campaign "ORG - Remarketing - Search - 90d" \
                    --min-clicks 50 \
                    --out audit_output
```

Only `--keywords` is required. Everything else narrows or exports the results.

| Flag | What it does |
|---|---|
| `--negatives` | Also runs the negative-keyword volume and conflict checks |
| `--campaign` | Restricts the audit to one campaign |
| `--min-clicks` | Click floor for the ranked table (default 50) |
| `--merge-match-types` | Ranks `[term]` and `"term"` as one row instead of separately |
| `--out` | Writes each result table to CSV in this directory |

## What it automates

| Function | Manual equivalent |
|---|---|
| `load_report` | Detecting UTF-16 vs UTF-8 and tab vs comma, stripping the two banner rows |
| `clean_report` | Dropping `Total:` rows, stripping `,` / `£` / `--` from numeric columns |
| `rank_keywords` | Merging the same keyword across campaigns, computing CTR/CPC, sorting |
| `find_bid_overrides` | Finding ad groups with inconsistent keyword-level bids |
| `find_capped_bids` | Ratio of Avg CPC to Max CPC, flagging bids near or past their cap |
| `summarise_status_reasons` | Separating `low quality` from `below first page bid` |
| `audit_negatives` | Negative volume vs. positive keyword count, plus conflict detection |

## Three design decisions worth knowing before you trust the output

**Encoding is detected from evidence, not a try/except chain.** Decoding UTF-8 bytes as UTF-16 frequently *succeeds* and produces garbage — no exception is raised, so a naive try/except loop can silently pick the wrong codec. The script checks for a BOM and for NUL bytes (which only ever appear in a wrongly-decoded UTF-16 file) before choosing.

**Match type stays a separate grouping key by default.** `[keyword]` (exact) and `"keyword"` (phrase) are kept as two rows, because their CTR isn't comparable — phrase match catches a wider set of queries and structurally dilutes CTR against exact. Pass `--merge-match-types` if you want them combined anyway.

**Bid utilisation above 1.0 is flagged as a data artefact, not a real ratio.** Avg CPC can't exceed Max CPC within a single auction — a ratio over 1.0 proves the bid was changed partway through the reporting window, which makes the comparison meaningless rather than alarming. Example from a real account: a keyword sitting at Max CPC £0.05 with an Avg CPC of £6.19 — the bid had been raised mid-window and the export still showed the old ceiling.

## Negative-keyword conflict logic

Match-type blocking rules are implemented literally, not approximated:
- **Exact** negative blocks only the identical query.
- **Phrase** negative blocks any contiguous sub-sequence match.
- **Broad** negative blocks if every one of its words appears anywhere in the query, in any order.

Covered by unit tests for all three types (`negative_blocks_keyword`). A large negative-to-keyword ratio in one ad group isn't automatically a conflict — it usually means the negatives are suppressing traffic meant for a *sibling* ad group, not blocking their own. The two checks (volume vs. conflicts) are reported separately for that reason.

## A note on what the script won't tell you

If every row shows 0 conversions, that's a tracking problem, not a keyword-quality signal — the script detects and warns about this case rather than silently ranking on a broken column. Rank on CTR/CPC until tracking is fixed.
