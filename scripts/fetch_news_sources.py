#!/usr/bin/env python3
"""
scripts/fetch_news_sources.py

Stáhne (1) Patria.cz RSS feed a (2) SEC EDGAR 8-K podání (přes
data.sec.gov/submissions JSON API, ne přes lehký atom feed) pro
US tickery z portfolio_tickers.json.

NOVÉ v této verzi:
- SEC strana teď používá strukturované submissions API, které vrací
  Item kategorii (např. "2.02" = earnings, "5.02" = leadership change)
  přímo, bez nutnosti fetchovat každé podání zvlášť.
- DISCOVERY MÓD: vždy zapíše news/sec_item_types_discovered.json se
  seznamem VŠECH nalezených Item kódů (s počtem výskytů a příklady
  titulů), aby šlo ručně rozhodnout, které kódy do filtru zahrnout.
  Filtr samotný (SEC_ITEM_WHITELIST) je zatím PRÁZDNÝ = nic se
  nevyřazuje podle typu, dokud whitelist ručně nedoplníš.
- Přidán časový filtr (max_age_hours) - položky starší než limit se
  do "matches" vůbec nedostanou.

Určeno pro spuštění v GitHub Actions (NE v Claude Code Routine
sandboxu - ten má blokovaný egress na obě domény).

Používá pouze standardní knihovnu Pythonu - žádný pip install.
"""

import json
import sys
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

PATRIA_FEED_URL = "https://www.patria.cz/rss.html"
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik10}.json"

# SEC vyžaduje identifikovatelný User-Agent (jméno/kontakt), jinak může
# request odmítnout. UPRAV na svůj kontakt před ostrým nasazením.
SEC_USER_AGENT = "PortfolioManager-NewsBot/1.0 (kontakt@example.com)"

# ---------------------------------------------------------------------
# ŘÍDÍCÍ FILTR: zatím prázdný = žádné SEC 8-K se podle typu nevyřazuje.
# Po prostudování news/sec_item_types_discovered.json sem doplň kódy,
# které chceš PONECHAT (whitelist). Příklad běžných "keep-worthy" kódů:
#   "1.01"  - Entry into Material Agreement
#   "2.01"  - Completion of Acquisition/Disposition
#   "2.02"  - Results of Operations (earnings)
#   "5.02"  - Departure/Election of Directors/Officers
#   "8.01"  - Other Events (často tiskové zprávy o čemkoliv)
# Ponech prázdné, dokud si výpis sám neprojdeš.
# ---------------------------------------------------------------------
SEC_ITEM_WHITELIST = []  # <-- doplnit ručně po review discovery souboru

MAX_AGE_HOURS = 72  # položky starší než tohle se do matches nedostanou


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
            if exchange in ("NASDAQ", "NYSE", "US"):
                us_tickers.append(ticker)
    return name_lookup, us_tickers


def http_get(url, user_agent="Mozilla/5.0 (news-fetch-bot)", timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def is_recent_enough(date_str, max_age_hours=MAX_AGE_HOURS):
    """
    Zkusí naparsovat datum (RSS RFC-822 formát nebo ISO 8601).
    Pokud nejde naparsovat, radši NECHÁ projít (fail-safe), než aby
    tiše ztratil data kvůli neznámému formátu.
    """
    if not date_str:
        return True
    dt = None
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return True  # neznámý formát -> nech projít
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() < max_age_hours * 3600


# ---------------------------------------------------------------------
# Patria RSS
# ---------------------------------------------------------------------

def fetch_patria_items():
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
        pub_date = (item.findtext("pubDate") or "").strip()
        if not is_recent_enough(pub_date):
            continue
        items.append(
            {
                "source": "patria",
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "pubDate": pub_date,
                "description": (item.findtext("description") or "").strip(),
            }
        )
    return items


# ---------------------------------------------------------------------
# SEC EDGAR - přes strukturované submissions JSON API
# ---------------------------------------------------------------------

_cik_map_cache = None


def load_sec_ticker_to_cik_map():
    """Stáhne a nakešuje mapu ticker -> 10-místné CIK (jednou za běh)."""
    global _cik_map_cache
    if _cik_map_cache is not None:
        return _cik_map_cache

    try:
        content = http_get(SEC_TICKER_MAP_URL, user_agent=SEC_USER_AGENT)
        raw = json.loads(content)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[sec] ticker map fetch failed: {e}", file=sys.stderr)
        _cik_map_cache = {}
        return _cik_map_cache

    mapping = {}
    # formát: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for entry in raw.values():
        ticker = entry.get("ticker", "").upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            mapping[ticker] = str(cik).zfill(10)

    _cik_map_cache = mapping
    return mapping


def fetch_sec_items_for_ticker(ticker, discovered_items_acc):
    """
    Stáhne posledních N 8-K podání pro daný ticker přes submissions API.
    Zapisuje nalezené Item kódy do discovered_items_acc (shared dict
    napříč všemi tickery) pro diagnostický výstup.
    Vrací seznam matchnutých položek (respektuje SEC_ITEM_WHITELIST,
    pokud je vyplněný; pokud je prázdný, propustí vše).
    """
    cik_map = load_sec_ticker_to_cik_map()
    cik10 = cik_map.get(ticker.upper())
    if not cik10:
        print(f"[sec:{ticker}] CIK not found in SEC ticker map", file=sys.stderr)
        return []

    url = SEC_SUBMISSIONS_TEMPLATE.format(cik10=cik10)
    try:
        content = http_get(url, user_agent=SEC_USER_AGENT)
        data = json.loads(content)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[sec:{ticker}] submissions fetch failed: {e}", file=sys.stderr)
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items_field = recent.get("items", [])
    filing_dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    results = []
    # Omezit na posledních cca 15 podání na ticker, ať se běh nenatahuje donekonečna
    limit = 15
    count_checked = 0

    for form, item_codes, filing_date, accession, primary_doc in zip(
        forms, items_field, filing_dates, accession_numbers, primary_docs
    ):
        if count_checked >= limit:
            break
        if form != "8-K":
            continue
        count_checked += 1

        if not is_recent_enough(filing_date + "T00:00:00Z"):
            continue

        item_codes_list = [c.strip() for c in item_codes.split(",") if c.strip()]

        # Diagnostika: zapsat KAŽDÝ nalezený Item kód do sdíleného souhrnu
        for code in item_codes_list:
            key = code
            if key not in discovered_items_acc:
                discovered_items_acc[key] = {
                    "count": 0,
                    "example_tickers": [],
                    "example_dates": [],
                }
            discovered_items_acc[key]["count"] += 1
            if len(discovered_items_acc[key]["example_tickers"]) < 5:
                discovered_items_acc[key]["example_tickers"].append(ticker)
                discovered_items_acc[key]["example_dates"].append(filing_date)

        # Filtrování podle whitelistu (pokud je prázdný, propustí vše)
        if SEC_ITEM_WHITELIST and not any(
            c in SEC_ITEM_WHITELIST for c in item_codes_list
        ):
            continue

        accession_nodash = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik10)}/{accession_nodash}/{primary_doc}"
        )

        results.append(
            {
                "source": "sec_edgar",
                "matched_ticker": ticker,
                "title": f"8-K ({item_codes}) - {ticker}",
                "link": filing_url,
                "pubDate": filing_date,
                "description": f"SEC 8-K filing, item(s): {item_codes or 'unspecified'}",
                "sec_items": item_codes_list,
            }
        )

    return results


# ---------------------------------------------------------------------
# Matchování (jen pro Patria - SEC už matched_ticker má)
# ---------------------------------------------------------------------

def match_portfolio(items, name_lookup):
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


def extract_macro_candidates(patria_items, matched_ids):
    macro_keywords = [
        "fed", "ecb", "úrokov", "sazb", "inflace", "recese",
        "centrální banka", "čnb",
    ]
    candidates = []
    for item in patria_items:
        if id(item) in matched_ids:
            continue
        haystack = (item["title"] + " " + item["description"]).lower()
        if any(kw in haystack for kw in macro_keywords):
            candidates.append(item)
    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers-file", default="news/portfolio_tickers.json")
    parser.add_argument("--output-file", default="news/external_feeds.json")
    parser.add_argument(
        "--discovery-file", default="news/sec_item_types_discovered.json"
    )
    args = parser.parse_args()

    try:
        name_lookup, us_tickers = load_portfolio(args.tickers_file)
    except FileNotFoundError:
        print(f"tickers file not found: {args.tickers_file}", file=sys.stderr)
        sys.exit(1)

    # 1) Patria RSS
    patria_items = fetch_patria_items()

    # 2) SEC EDGAR per US ticker + discovery akumulátor
    discovered_items_acc = {}
    sec_items_all = []
    for ticker in us_tickers:
        sec_items_all.extend(fetch_sec_items_for_ticker(ticker, discovered_items_acc))

    # Matchování
    all_items_for_matching = patria_items + sec_items_all
    matched = match_portfolio(all_items_for_matching, name_lookup)
    matched_ids = {id(m) for m in matched}

    macro_candidates = extract_macro_candidates(patria_items, matched_ids)

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "max_age_hours_filter": MAX_AGE_HOURS,
        "sec_item_whitelist_active": bool(SEC_ITEM_WHITELIST),
        "sources_attempted": {
            "patria": {"ok": len(patria_items) >= 0, "item_count": len(patria_items)},
            "sec_edgar": {
                "tickers_checked": len(us_tickers),
                "item_count": len(sec_items_all),
            },
        },
        "matched_count": len(matched),
        "matches": matched,
        "macro_candidates": macro_candidates,
    }

    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Diagnostický výpis Item typů - VŽDY se zapíše, nezávisle na whitelistu
    discovery_output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Seznam všech Item kódů nalezených v 8-K podáních za tento běh. "
            "Doplň SEC_ITEM_WHITELIST ve skriptu podle toho, které kódy chceš "
            "zahrnout do matches. Prázdný whitelist = nic se nefiltruje podle typu."
        ),
        "item_types_found": discovered_items_acc,
    }
    with open(args.discovery_file, "w", encoding="utf-8") as f:
        json.dump(discovery_output, f, ensure_ascii=False, indent=2)

    print(
        f"OK: {len(matched)} matched items, {len(macro_candidates)} macro candidates "
        f"-> {args.output_file}"
    )
    print(
        f"Discovery: {len(discovered_items_acc)} distinct Item types found "
        f"-> {args.discovery_file}"
    )


if __name__ == "__main__":
    main()