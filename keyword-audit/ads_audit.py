#!/usr/bin/env python3
"""
Google Ads keyword & negative-keyword report auditor.

Reproduces, deterministically and without any LLM, a manual keyword audit:

  1. Loads Google Ads UI exports regardless of encoding (UTF-8 / UTF-16LE) or
     delimiter (comma / tab), and strips the two banner rows the UI prepends.
  2. Cleans the data: drops "Total:" rollup rows, coerces numeric columns that
     arrive as strings (thousands separators, "--" placeholders).
  3. Aggregates duplicate keywords that appear across multiple campaigns so a
     keyword is ranked once, not once per campaign.
  4. Computes CTR and CPC and ranks by intent quality rather than raw spend.
  5. Flags structural problems that silently break a campaign:
       - keyword-level bids overriding the ad group default
       - bids pinned at the cap (paying the ceiling)
       - negative-keyword lists that outnumber positive keywords
       - negatives that block the ad group's own keywords
       - Quality Score / first-page-bid warnings in "Status reasons"

Usage
-----
    python ads_audit.py --keywords Search_keyword_report.csv \
                        --negatives Negative_keyword_report.csv \
                        --campaign "ORG - Remarketing - Search - 90d" \
                        --min-clicks 50 \
                        --out audit_output

Every argument except --keywords is optional.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The Google Ads UI writes two banner rows (report title, date range) above the
# real header row. Change this if you export via the API instead.
BANNER_ROWS = 2

# Columns that are numeric in meaning but arrive as text. Google formats them
# with thousands separators and writes "--" where a value does not apply.
NUMERIC_COLUMNS = [
    "Clicks",
    "Impr.",
    "Cost",
    "Conversions",
    "Interactions",
    "Avg. CPC",
    "Avg. cost",
    "Max. CPC",
    "Conv. rate",
    "Cost / conv.",
]

# Rows describing real keywords always carry one of these statuses. Rollup rows
# ("Total: Your keywords", "Total: Campaign", ...) leave the column blank or
# set it to "--", which is how we identify and drop them.
REAL_ROW_STATUSES = {"Enabled", "Paused", "Removed"}

# Substrings in the "Status reasons" column worth surfacing, mapped to the
# action they imply. Google concatenates several reasons with "; ".
STATUS_REASON_MEANINGS = {
    "low quality": "Low Quality Score - keyword/landing-page relevance problem",
    "below first page bid": "Bid too low to reach page one",
    "rarely served": "Search volume too low to serve",
    "ad group paused": "Ad group is paused",
    "campaign paused": "Campaign is paused",
}


# ---------------------------------------------------------------------------
# Loading & cleaning
# ---------------------------------------------------------------------------


def _sniff_delimiter(sample: str) -> str:
    """Return the delimiter used by a Google Ads export.

    Google writes comma-separated files when the export is UTF-8 and
    tab-separated files when it is UTF-16, but this is not guaranteed, so we
    detect rather than assume.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except csv.Error:
        # Sniffer fails on files with few rows; fall back to whichever
        # candidate appears more often in the sample.
        return "\t" if sample.count("\t") > sample.count(",") else ","


def load_report(path: str | Path, banner_rows: int = BANNER_ROWS) -> pd.DataFrame:
    """Load a Google Ads report into a DataFrame, handling encoding and delimiter.

    Google Ads exports are UTF-16 little-endian in some flows and UTF-8 (often
    with a BOM) in others. Reading with the wrong codec produces either a
    UnicodeDecodeError or a single garbled column, so we try each in turn.
    """
    path = Path(path)
    raw = path.read_bytes()

    # Decide the codec from evidence rather than by trying them in turn:
    # decoding UTF-8 bytes as UTF-16 often *succeeds* and returns silent
    # garbage, so a try/except chain would pick the wrong one.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[1:2] == b"\x00":
        candidates = ("utf-16", "utf-8-sig", "utf-8")
    else:
        candidates = ("utf-8-sig", "utf-8", "latin-1")

    text = None
    for encoding in candidates:
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "\x00" in decoded[:4000]:
            continue  # wrong codec: real text never contains NUL
        text = decoded
        break

    if text is None:
        raise ValueError(f"Could not decode {path} with any known encoding")

    # Drop the UI banner rows before handing the rest to the CSV parser.
    body = "\n".join(text.splitlines()[banner_rows:])
    delimiter = _sniff_delimiter(body[:4000])

    return pd.read_csv(io.StringIO(body), sep=delimiter)


def clean_report(df: pd.DataFrame, status_column: str) -> pd.DataFrame:
    """Drop rollup rows and coerce numeric columns.

    `status_column` is the column that distinguishes real rows from Google's
    "Total:" summaries - "Keyword status" in a keyword report. Reports that
    have no such column (negative keywords) skip the row filter.
    """
    out = df.copy()

    # Remove the "Total: ..." rollup rows. Left in place they pass any
    # clicks/cost threshold and dominate a sort by spend.
    if status_column and status_column in out.columns:
        out = out[out[status_column].isin(REAL_ROW_STATUSES)]

    for column in NUMERIC_COLUMNS:
        if column not in out.columns:
            continue
        out[column] = (
            out[column]
            .astype(str)
            .str.replace(",", "", regex=False)       # thousands separators
            .str.replace("£", "", regex=False)       # currency prefix
            .str.replace("%", "", regex=False)       # percentage suffix
            .str.strip()
            .replace({"--": None, "": None, "nan": None})
        )
        out[column] = pd.to_numeric(out[column], errors="coerce")

    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Keyword analysis
# ---------------------------------------------------------------------------


def safe_ratio(numerator, denominator, decimals: int = 2):
    """Divide two Series, returning NaN where the denominator is zero or missing.

    Uses NaN rather than pandas' NA so the result keeps a plain float dtype;
    NA-backed columns raise on .round() and .astype(float).
    """
    numerator = pd.to_numeric(numerator, errors="coerce")
    denominator = pd.to_numeric(denominator, errors="coerce").replace(0, float("nan"))
    return (numerator / denominator).round(decimals)


def normalise_keyword(keyword: str) -> str:
    """Strip match-type punctuation so the same term compares equal everywhere.

    Google writes exact match as [term], phrase match as "term" and broad match
    bare. Comparing raw strings would treat these as three different keywords.
    """
    return str(keyword).strip().strip('[]"').strip().lower()


def rank_keywords(
    df: pd.DataFrame, min_clicks: int = 50, merge_match_types: bool = False
) -> pd.DataFrame:
    """Aggregate keywords across campaigns and rank them by CTR.

    Ranking by Cost answers "where did the money go", which is a reporting
    question. Ranking by CTR answers "which terms did searchers actually want",
    which is the selection question. When conversion tracking is broken - every
    row showing zero - CTR and CPC are the only usable signals left.

    The same keyword can appear in several campaigns; those rows are summed so
    a term is ranked once. Match type is kept as part of the grouping key by
    default because phrase match catches a wider set of queries than exact and
    therefore dilutes CTR - the two are not comparable on the same scale. Pass
    merge_match_types=True to override.
    """
    working = df[df["Clicks"] > min_clicks].copy()
    if working.empty:
        return working

    working["keyword_normalised"] = working["Keyword"].map(normalise_keyword)

    group_keys = ["keyword_normalised"]
    if not merge_match_types and "Match type" in working.columns:
        group_keys.append("Match type")

    grouped = (
        working.groupby(group_keys)
        .agg(
            clicks=("Clicks", "sum"),
            impressions=("Impr.", "sum"),
            cost=("Cost", "sum"),
            appearances=("Keyword", "size"),  # >1 means duplicated across campaigns
        )
        .reset_index()
    )

    # Guard against division by zero: a keyword can log clicks with impressions
    # missing from the export if columns were deselected.
    grouped["ctr_pct"] = safe_ratio(grouped["clicks"] * 100, grouped["impressions"])
    grouped["cpc"] = safe_ratio(grouped["cost"], grouped["clicks"])

    return grouped.sort_values("ctr_pct", ascending=False).reset_index(drop=True)


def find_bid_overrides(df: pd.DataFrame, tolerance: float = 0.01) -> pd.DataFrame:
    """List keywords whose own bid differs from the ad group's other keywords.

    A keyword-level Max CPC always overrides the ad group default. Setting the
    ad group to GBP 2.00 while its keywords sit at GBP 0.05 leaves the campaign
    live but effectively unable to enter any auction - the most common cause of
    a "campaign is running but gets zero impressions" report.

    This reports, per ad group, the distinct bids in use so an outlier is
    visible without needing to know the ad group default.
    """
    if "Max. CPC" not in df.columns or "Ad group" not in df.columns:
        return pd.DataFrame()

    bids = df.dropna(subset=["Max. CPC"])
    summary = (
        bids.groupby("Ad group")["Max. CPC"]
        .agg(["min", "max", "nunique", "count"])
        .reset_index()
        .rename(
            columns={
                "min": "lowest_bid",
                "max": "highest_bid",
                "nunique": "distinct_bids",
                "count": "keywords_with_own_bid",
            }
        )
    )

    # Flag ad groups where the bid spread is wide enough to be unintentional.
    summary["inconsistent"] = (
        summary["highest_bid"] - summary["lowest_bid"]
    ) > tolerance
    return summary.sort_values("inconsistent", ascending=False).reset_index(drop=True)


def find_capped_bids(df: pd.DataFrame, threshold: float = 0.9) -> pd.DataFrame:
    """Flag keywords whose average CPC sits near their Max CPC.

    When Avg CPC approaches Max CPC the bid is the binding constraint: the
    keyword pays close to its ceiling on most auctions, which usually means the
    ceiling is above what the position is worth. A large gap in the other
    direction (Max CPC far above Avg CPC) means there is headroom.

    Caveat worth remembering when reading the output: Max CPC reflects the bid
    *today*, while Avg CPC is averaged over the whole reporting window. If the
    bid was changed mid-window the comparison is meaningless, which shows up as
    an Avg CPC far *above* Max CPC.
    """
    needed = {"Max. CPC", "Avg. CPC"}
    if not needed.issubset(df.columns):
        return pd.DataFrame()

    working = df.dropna(subset=["Max. CPC", "Avg. CPC"]).copy()
    working = working[working["Max. CPC"] > 0]
    working["bid_utilisation"] = safe_ratio(working["Avg. CPC"], working["Max. CPC"])

    # Utilisation above 1.0 is impossible in a single auction, so it is proof
    # that the bid changed during the reporting window.
    working["note"] = working["bid_utilisation"].apply(
        lambda ratio: "bid changed mid-window"
        if ratio > 1.0
        else ("paying near cap" if ratio >= threshold else "headroom")
    )

    # Only rows that need a decision are returned; the long tail of keywords
    # with unremarkable headroom would bury them.
    working = working[working["note"] != "headroom"]

    columns = [c for c in ["Keyword", "Ad group", "Campaign"] if c in working.columns]
    return (
        working[columns + ["Max. CPC", "Avg. CPC", "bid_utilisation", "note"]]
        .sort_values("bid_utilisation", ascending=False)
        .reset_index(drop=True)
    )


def summarise_status_reasons(df: pd.DataFrame) -> pd.DataFrame:
    """Count the diagnostic reasons Google attaches to each keyword.

    "low quality" and "below first page bid" have different root causes and
    different fixes - landing-page relevance versus bidding - so they are worth
    separating rather than reading as one undifferentiated warning.
    """
    if "Status reasons" not in df.columns:
        return pd.DataFrame()

    counts: dict[str, int] = {}
    for raw in df["Status reasons"].dropna():
        for reason in str(raw).split(";"):
            reason = reason.strip().lower()
            if reason:
                counts[reason] = counts.get(reason, 0) + 1

    if not counts:
        return pd.DataFrame()

    return (
        pd.DataFrame(
            [
                {
                    "reason": reason,
                    "keywords": count,
                    "meaning": STATUS_REASON_MEANINGS.get(reason, ""),
                }
                for reason, count in counts.items()
            ]
        )
        .sort_values("keywords", ascending=False)
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Negative keyword analysis
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Split a keyword into comparable word tokens, discarding punctuation."""
    return re.findall(r"[a-z0-9]+", text.lower())


def negative_blocks_keyword(negative: str, match_type: str, keyword: str) -> bool:
    """Return True if a negative keyword would block a positive keyword.

    Google's blocking rules, simplified to the case that matters here (the
    positive keyword's own text, ignoring close variants Google may also match):

      - Exact negative  [x]  blocks only the identical query.
      - Phrase negative "x" blocks any query containing x as a contiguous
        sequence of words.
      - Broad negative   x   blocks any query containing all of x's words in
        any order.
    """
    negative_tokens = _tokens(normalise_keyword(negative))
    keyword_tokens = _tokens(normalise_keyword(keyword))
    if not negative_tokens:
        return False

    match_type = str(match_type).lower()

    if "exact" in match_type:
        return negative_tokens == keyword_tokens

    if "phrase" in match_type:
        # Contiguous sub-sequence test.
        span = len(negative_tokens)
        return any(
            keyword_tokens[i : i + span] == negative_tokens
            for i in range(len(keyword_tokens) - span + 1)
        )

    # Broad: every negative token present somewhere in the keyword.
    return set(negative_tokens).issubset(set(keyword_tokens))


def audit_negatives(
    negatives: pd.DataFrame, keywords: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (volume summary, conflicts) for a negative keyword list.

    Two distinct problems are checked.

    Volume: a large ad-group-level negative list is normal in a tightly
    sculpted account where sibling ad groups catch the redirected traffic. Once
    those siblings are removed or paused, the same list simply suppresses
    traffic with nothing left to catch it - so negatives far outnumbering
    positives in one ad group is a signal, not a detail.

    Conflicts: negatives that block the ad group's own keywords. These are
    always a mistake and are invisible in the UI.
    """
    level_column = "Level" if "Level" in negatives.columns else None
    group_column = "Ad group" if "Ad group" in negatives.columns else None

    # --- volume summary -------------------------------------------------
    group_keys = [c for c in [level_column, group_column, "Match type"] if c]
    volume = (
        negatives.groupby(group_keys).size().reset_index(name="negatives")
        if group_keys
        else pd.DataFrame([{"negatives": len(negatives)}])
    )

    if group_column and "Ad group" in keywords.columns:
        positives = (
            keywords.groupby("Ad group").size().reset_index(name="positive_keywords")
        )
        volume = volume.merge(positives, on="Ad group", how="left")
        volume["positive_keywords"] = volume["positive_keywords"].fillna(0).astype(int)
        # Ratio makes the imbalance legible at a glance.
        volume["negatives_per_keyword"] = safe_ratio(
            volume["negatives"], volume["positive_keywords"], decimals=1
        )

    # --- conflict detection ---------------------------------------------
    conflicts: list[dict] = []
    negative_text_column = (
        "Negative keyword" if "Negative keyword" in negatives.columns else "Keyword"
    )

    if group_column and "Ad group" in keywords.columns:
        for ad_group, group_negatives in negatives.groupby(group_column):
            group_keywords = keywords[keywords["Ad group"] == ad_group]
            if group_keywords.empty:
                continue

            for _, negative_row in group_negatives.iterrows():
                negative_text = negative_row[negative_text_column]
                negative_match = negative_row.get("Match type", "broad")

                for _, keyword_row in group_keywords.iterrows():
                    if negative_blocks_keyword(
                        negative_text, negative_match, keyword_row["Keyword"]
                    ):
                        conflicts.append(
                            {
                                "ad_group": ad_group,
                                "negative": negative_text,
                                "negative_match": negative_match,
                                "blocked_keyword": keyword_row["Keyword"],
                            }
                        )

    return volume.reset_index(drop=True), pd.DataFrame(conflicts)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _show(frame: pd.DataFrame, empty_message: str, limit: int = 25) -> None:
    if frame is None or frame.empty:
        print(empty_message)
        return
    print(frame.head(limit).to_string(index=False))
    if len(frame) > limit:
        print(f"... {len(frame) - limit} more rows")


def run_audit(
    keywords_path: str,
    negatives_path: str | None = None,
    campaign: str | None = None,
    min_clicks: int = 50,
    merge_match_types: bool = False,
    output_dir: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Run the full audit and print a report. Returns the frames it produced."""
    keywords = clean_report(load_report(keywords_path), "Keyword status")

    if campaign and "Campaign" in keywords.columns:
        keywords = keywords[keywords["Campaign"] == campaign]
        if keywords.empty:
            sys.exit(f"No keyword rows found for campaign: {campaign}")

    results: dict[str, pd.DataFrame] = {}

    # --- coverage -------------------------------------------------------
    _section("Coverage")
    total = len(keywords)
    with_clicks = int((keywords["Clicks"] > 0).sum())
    above_threshold = int((keywords["Clicks"] > min_clicks).sum())
    total_cost = keywords["Cost"].sum()
    threshold_cost = keywords.loc[keywords["Clicks"] > min_clicks, "Cost"].sum()

    print(f"{'Keywords in scope':<25}: {total:,}")
    print(f"{'With at least one click':<25}: {with_clicks:,}")
    print(f"{'Above ' + str(min_clicks) + ' clicks':<25}: {above_threshold:,}")
    if total_cost:
        share = threshold_cost / total_cost * 100
        print(f"{'Spend held by those rows':<25}: {share:.1f}% of {total_cost:,.2f}")

    # A conversion column that is uniformly zero measures broken tracking, not
    # keyword quality, so filtering on it would be a no-op dressed as a signal.
    if "Conversions" in keywords.columns and keywords["Conversions"].fillna(0).eq(0).all():
        print(
            "\nWARNING: every row reports zero conversions. Treat this as absent "
            "tracking, not as evidence about keyword quality. Rank on CTR and CPC."
        )

    # --- ranked keywords ------------------------------------------------
    _section(f"Keywords above {min_clicks} clicks, ranked by CTR")
    ranked = rank_keywords(
        keywords, min_clicks=min_clicks, merge_match_types=merge_match_types
    )
    results["ranked_keywords"] = ranked
    _show(ranked, "No keyword cleared the click threshold.")

    duplicated = ranked[ranked["appearances"] > 1] if not ranked.empty else pd.DataFrame()
    if not duplicated.empty:
        print(
            f"\nNote: {len(duplicated)} keyword(s) appeared in more than one campaign "
            "and were merged. Inspect them unmerged to compare bid effects."
        )

    # --- bid consistency ------------------------------------------------
    _section("Bid consistency by ad group")
    bid_summary = find_bid_overrides(keywords)
    results["bid_summary"] = bid_summary
    _show(bid_summary, "No keyword-level bids found.")

    _section("Bid utilisation")
    capped = find_capped_bids(keywords)
    results["bid_utilisation"] = capped
    _show(capped, "Not enough bid data to compare.", limit=15)

    # --- status reasons -------------------------------------------------
    _section("Status reasons")
    reasons = summarise_status_reasons(keywords)
    results["status_reasons"] = reasons
    _show(reasons, "No status reasons in this export.")

    # --- negatives ------------------------------------------------------
    if negatives_path:
        negatives = clean_report(load_report(negatives_path), status_column="")
        if campaign and "Campaign" in negatives.columns:
            negatives = negatives[negatives["Campaign"] == campaign]

        volume, conflicts = audit_negatives(negatives, keywords)
        results["negative_volume"] = volume
        results["negative_conflicts"] = conflicts

        _section("Negative keyword volume")
        _show(volume, "No negatives found.")

        _section("Negatives blocking this campaign's own keywords")
        _show(conflicts, "No conflicts detected.", limit=40)

    # --- optional export ------------------------------------------------
    if output_dir:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        for name, frame in results.items():
            if frame is not None and not frame.empty:
                frame.to_csv(destination / f"{name}.csv", index=False)
        print(f"\nWrote {len(results)} file(s) to {destination}/")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a Google Ads keyword export without an LLM."
    )
    parser.add_argument("--keywords", required=True, help="Search keyword report CSV")
    parser.add_argument("--negatives", help="Negative keyword report CSV")
    parser.add_argument("--campaign", help="Restrict the audit to one campaign name")
    parser.add_argument(
        "--min-clicks",
        type=int,
        default=50,
        help="Click threshold for the ranked table (default: 50)",
    )
    parser.add_argument(
        "--merge-match-types",
        action="store_true",
        help='Rank [term] and "term" as one row (default: keep separate)',
    )
    parser.add_argument("--out", help="Directory to write CSV results into")
    args = parser.parse_args()

    run_audit(
        keywords_path=args.keywords,
        negatives_path=args.negatives,
        campaign=args.campaign,
        min_clicks=args.min_clicks,
        merge_match_types=args.merge_match_types,
        output_dir=args.out,
    )


if __name__ == "__main__":
    main()
