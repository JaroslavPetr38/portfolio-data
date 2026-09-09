#!/usr/bin/env python3
"""
scripts/fetch_news_sources.py

Stáhne (1) Patria.cz RSS feed a (2) SEC EDGAR 8-K feedy pro US tickery
z portfolio_tickers.json, vyfiltruje relevantní položky, a zapíše
výsledek do news/external_feeds.json.

Určeno pro spuštění v GitHub Actions (NE v Claude Code Routine
sandboxu — ten má blokovaný egress na obě domény).

Používá pouze standardní knihovnu Pythonu — žádný pip install.
"""

import json
import sys
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

PATRIA_FEED_URL = "https://www.patria.cz/rss.html"
SEC_EDGAR_TEMPLATE = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={ticker}&type=8-K"
    "&dateb=&owner=include&count=5&output=atom"
)
# SEC vyžaduje identifikovatelný User-Agent (jméno/kontakt), jinak může
# request odmítnout — uprav si podle sebe.
SEC_USER_AGENT = "xpetr.xjaroslav@gmail.com-NewsBot/1.0 (personal-use)"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def load_portfolio(tickers_file):
    """Vrátí (name_lookup_dict, us_tickers_list)."""
    with open(tickers_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    name_lookup = {}
    us_tickers = []
    for section in ("portfolio", "watchlist"):
        for item in data.get(section, []):
            ticker = item.get("ticker")
            name = item.get("name")
            exchange = (item.get("exchange") or "").upper()
            if name:
                name_lookup[name.lower()] = ticker
            if ticker:
                name_lookup[ticker.lower()] = ticker
            # US burzy - uprav seznam podle vlastního exchangeId schématu
            if exchange in ("NASDAQ", "NYSE", "US"):
                us_tickers.append(ticker)
    return name_lookup, us_tickers


def http_get(url, user_agent="Mozilla/5.0 (news-fetch-bot)", timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_patria_items():
    """Stáhne a naparsuje Patria RSS feed. Vrátí seznam položek nebo [] při chybě."""
    try:
        content = http_get(PATRIA_FEED_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[patria] fetch failed: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"[patria] XML parse failed: {e}", file=sys.stderr)
        return []

    items = []
    for item in root.iter("item"):
        items.append(
            {
                "source": "patria",
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "pubDate": (item.findtext("pubDate") or "").strip(),
                "description": (item.findtext("description") or "").strip(),
            }
        )
    return items


def fetch_sec_items(ticker):
    """Stáhne a naparsuje SEC EDGAR Atom feed pro jeden ticker. Vrátí seznam položek."""
    url = SEC_EDGAR_TEMPLATE.format(ticker=ticker)
    try:
        content = http_get(url, user_agent=SEC_USER_AGENT)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[sec:{ticker}] fetch failed: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"[sec:{ticker}] XML parse failed: {e}", file=sys.stderr)
        return []

    items = []
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""
        updated = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)
        items.append(
            {
                "source": "sec_edgar",
                "matched_ticker": ticker,
                "title": title.strip(),
                "link": link.strip(),
                "pubDate": updated.strip(),
                "description": f"SEC 8-K filing for {ticker}",
            }
        )
    return items


def match_portfolio(items, name_lookup):
    """Pro položky bez už přiřazeného matched_ticker (tj. Patria) najde shodu podle jména."""
    matched = []
    for item in items:
        if item.get("matched_ticker"):
            matched.append(item)
            continue
        haystack = (item["title"] + " " + item["description"]).lower()
        for name_key, ticker in name_lookup.items():
            if name_key in haystack:
                enriched = dict(item)
                enriched["matched_ticker"] = ticker
                matched.append(enriched)
                break
    return matched


def check_macro_keywords(items, matched_tickers_set):
    """Z nematchnutých položek (jen Patria) vytáhne kandidáty na makro zprávy."""
    macro_keywords = [
        "fed", "ecb", "úrokov", "sazb", "inflace", "recese",
        "centrální banka", "čnb",
    ]
    macro_candidates = []
    for item in items:
        if item in matched_tickers_set:
            continue
        haystack = (item["title"] + " " + item["description"]).lower()
        if any(kw in haystack for kw in macro_keywords):
            macro_candidates.append(item)
    return macro_candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers-file", default="news/portfolio_tickers.json")
    parser.add_argument("--output-file", default="news/external_feeds.json")
    args = parser.parse_args()

    try:
        name_lookup, us_tickers = load_portfolio(args.tickers_file)
    except FileNotFoundError:
        print(f"tickers file not found: {args.tickers_file}", file=sys.stderr)
        sys.exit(1)

    all_raw_items = []

    # 1) Patria RSS
    patria_items = fetch_patria_items()
    all_raw_items.extend(patria_items)

    # 2) SEC EDGAR per US ticker
    sec_items_all = []
    for ticker in us_tickers:
        sec_items = fetch_sec_items(ticker)
        sec_items_all.extend(sec_items)
    all_raw_items.extend(sec_items_all)

    # Matchování (SEC už má matched_ticker, Patria potřebuje name lookup)
    matched = match_portfolio(all_raw_items, name_lookup)

    # Makro kandidáti jen z nematchnutých Patria položek
    matched_set = {id(m) for m in matched}
    macro_candidates = [
        i for i in patria_items
        if id(i) not in matched_set
        and any(
            kw in (i["title"] + " " + i["description"]).lower()
            for kw in ["fed", "ecb", "úrokov", "sazb", "inflace", "recese", "čnb"]
        )
    ]

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources_attempted": {
            "patria": {"ok": len(patria_items) > 0, "item_count": len(patria_items)},
            "sec_edgar": {"tickers_checked": len(us_tickers), "item_count": len(sec_items_all)},
        },
        "matched_count": len(matched),
        "matches": matched,
        "macro_candidates": macro_candidates,
    }

    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(matched)} matched items, {len(macro_candidates)} macro candidates "
          f"-> {args.output_file}")


if __name__ == "__main__":
    main()
