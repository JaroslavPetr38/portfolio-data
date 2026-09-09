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
import re
import os
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

PATRIA_FEED_URL = "https://www.patria.cz/rss.html"
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik10}.json"
YAHOO_RSS_TEMPLATE = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}"
FINNHUB_NEWS_TEMPLATE = (
    "https://finnhub.io/api/v1/company-news"
    "?symbol={ticker}&from={date_from}&to={date_to}&token={token}"
)
# API klíč se čte VÝHRADNĚ z prostředí (GitHub Secret), nikdy natvrdo v kódu.
# Pokud proměnná chybí, Finnhub zdroj se čistě přeskočí (ne pád skriptu).
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

# SEC vyžaduje identifikovatelný User-Agent (jméno/kontakt), jinak může
# request odmítnout. UPRAV na svůj kontakt před ostrým nasazením.
SEC_USER_AGENT = "Personal news agent/1.0 (xpetr.xjaroslav@gmail.com)"

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

# ---------------------------------------------------------------------
# ETF/fondy nefilují 8-K stejným způsobem jako operační firmy (mají
# jiné SEC formuláře - N-CEN, N-PORT) a typicky nejsou v CIK mapě
# operačních firem vůbec. Radši je z SEC 8-K kontroly rovnou vynech,
# než aby se to tvářilo jako chyba pokaždé znovu.
# Doplňuj sem podle potřeby, jak narazíš na další ETF v portfoliu.
# ---------------------------------------------------------------------
KNOWN_NON_FILER_TICKERS = {"URA"}


def load_portfolio(tickers_file):
    """Vrátí (name_lookup_dict, us_tickers_list, yahoo_lookup_list).

    yahoo_lookup_list je seznam (ticker, yahoo_ticker) pro VŠECHNY
    portfolio/watchlist položky (ne jen US) - používá pole
    "yahoo_ticker" pokud existuje, jinak spadne zpět na holý ticker.
    """
    with open(tickers_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    name_lookup = {}
    us_tickers = []
    yahoo_lookup = []
    for section in ("portfolio", "watchlist"):
        for item in data.get(section, []):
            ticker = item.get("ticker")
            name = item.get("name")
            exchange = (item.get("exchange") or "").upper()
            yahoo_ticker = item.get("yahoo_ticker") or ticker

            if name:
                name_lookup[name.lower()] = ticker
            if ticker:
                name_lookup[ticker.lower()] = ticker
            if exchange in ("NASDAQ", "NYSE", "US") and ticker not in KNOWN_NON_FILER_TICKERS:
                us_tickers.append(ticker)
            if ticker and yahoo_ticker:
                yahoo_lookup.append((ticker, yahoo_ticker, name))

    return name_lookup, us_tickers, yahoo_lookup


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


def fetch_yahoo_items_for_ticker(ticker, yahoo_ticker, company_name):
    """
    Stáhne Yahoo Finance per-ticker RSS feed. Stejná lean XML struktura
    jako Patria (title/link/pubDate/description v <item>), takže sdílí
    stejný parsing přístup. Funguje mezinárodně (LGEN.L, ALWN.AT, ...),
    ne jen pro US tickery - proto se volá pro VŠECHNY tickery, ne jen
    tu podmnožinu, co jde do SEC EDGAR.

    DŮLEŽITÉ: Yahoo feed je URL-scoped na ticker (?s=NLY), ale to
    NEZNAMENÁ, že každá položka je skutečně "o firmě" - Yahoo tam
    zařazuje i tematicky přilehlý obsah (např. obecný Fed komentář
    do feedu sazbově citlivého REIT). Proto se přidává diagnostický
    příznak "company_mentioned_in_text" - NEFILTRUJE se tím nic,
    jen se dává Step 3 agentovi extra signál k rozhodnutí.
    """
    url = YAHOO_RSS_TEMPLATE.format(ticker=yahoo_ticker)
    try:
        content = http_get(url, user_agent="Mozilla/5.0 (news-fetch-bot)")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[yahoo:{yahoo_ticker}] fetch failed: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"[yahoo:{yahoo_ticker}] XML parse failed: {e}", file=sys.stderr)
        return []

    # Kontrolní vzory - ticker i (pokud existuje) první slovo názvu firmy
    # (celý víceslovný název firmy by byl moc přísný - "Legal & General"
    # se v textu často zkracuje na "Legal & General Group" apod., ale
    # "Legal" samotné jako první slovo je rozumný, benevolentnější signál)
    check_patterns = [_get_word_boundary_pattern(ticker)]
    if company_name:
        first_word = company_name.split()[0] if company_name.split() else None
        if first_word and len(first_word) > 2:  # vynech příliš krátká/obecná slova
            check_patterns.append(_get_word_boundary_pattern(first_word))

    items = []
    for item in root.iter("item"):
        pub_date = (item.findtext("pubDate") or "").strip()
        if not is_recent_enough(pub_date):
            continue
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        haystack = title + " " + description

        mentioned = any(p.search(haystack) for p in check_patterns)

        items.append(
            {
                "source": "yahoo",
                "matched_ticker": ticker,
                "title": title,
                "link": (item.findtext("link") or "").strip(),
                "pubDate": pub_date,
                "description": description,
                "company_mentioned_in_text": mentioned,
            }
        )
    return items


# ---------------------------------------------------------------------
# SEC EDGAR - přes strukturované submissions JSON API
# ---------------------------------------------------------------------

_cik_map_cache = None
SEC_FETCH_ERRORS = []  # sbírá chyby pro zápis do výstupního JSON (ne jen stderr)


def load_sec_ticker_to_cik_map():
    """Stáhne a nakešuje mapu ticker -> 10-místné CIK (jednou za běh)."""
    global _cik_map_cache
    if _cik_map_cache is not None:
        return _cik_map_cache

    try:
        content = http_get(SEC_TICKER_MAP_URL, user_agent=SEC_USER_AGENT)
        raw = json.loads(content)
    except urllib.error.HTTPError as e:
        msg = f"ticker map HTTP {e.code}: {e.reason}"
        print(f"[sec] {msg}", file=sys.stderr)
        SEC_FETCH_ERRORS.append({"stage": "cik_map", "error": msg})
        _cik_map_cache = {}
        return _cik_map_cache
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        msg = f"ticker map fetch failed: {e}"
        print(f"[sec] {msg}", file=sys.stderr)
        SEC_FETCH_ERRORS.append({"stage": "cik_map", "error": msg})
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
        msg = f"CIK not found in SEC ticker map (map size: {len(cik_map)})"
        print(f"[sec:{ticker}] {msg}", file=sys.stderr)
        SEC_FETCH_ERRORS.append({"stage": "cik_lookup", "ticker": ticker, "error": msg})
        return []

    url = SEC_SUBMISSIONS_TEMPLATE.format(cik10=cik10)
    try:
        content = http_get(url, user_agent=SEC_USER_AGENT)
        data = json.loads(content)
    except urllib.error.HTTPError as e:
        msg = f"submissions HTTP {e.code}: {e.reason}"
        print(f"[sec:{ticker}] {msg}", file=sys.stderr)
        SEC_FETCH_ERRORS.append({"stage": "submissions", "ticker": ticker, "error": msg})
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        msg = f"submissions fetch failed: {e}"
        print(f"[sec:{ticker}] {msg}", file=sys.stderr)
        SEC_FETCH_ERRORS.append({"stage": "submissions", "ticker": ticker, "error": msg})
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

_word_boundary_pattern_cache = {}


def _get_word_boundary_pattern(name_key):
    """
    Vrátí (a nakešuje) regex, co matchne name_key jen jako celé,
    samostatné slovo - ne jako substring uvnitř jiného slova
    (např. "ogi" nesmí matchnout uvnitř "technologii").

    \b v Pythonu 3 je Unicode-aware, takže funguje správně i s
    českou diakritikou (ř, š, č, á...) bez dalšího nastavení.
    """
    if name_key not in _word_boundary_pattern_cache:
        _word_boundary_pattern_cache[name_key] = re.compile(
            r"\b" + re.escape(name_key) + r"\b", re.IGNORECASE
        )
    return _word_boundary_pattern_cache[name_key]


def fetch_finnhub_items_for_ticker(ticker, company_name):
    """
    Stáhne firemní zprávy z Finnhub company-news endpointu pro daný
    ticker. Na rozdíl od RSS zdrojů vyžaduje explicitní from/to datum
    v URL (odvozeno z MAX_AGE_HOURS), ne "posledních N položek".

    Vyžaduje FINNHUB_API_KEY v prostředí (GitHub Secret) - pokud
    chybí, tiše se přeskočí, ne pád skriptu.

    "datetime" pole v odpovědi je Unix timestamp, ne RFC-822 string
    jako u ostatních zdrojů - proto vlastní recency kontrola místo
    sdílené is_recent_enough().
    """
    if not FINNHUB_API_KEY:
        return []

    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(hours=MAX_AGE_HOURS)
    url = FINNHUB_NEWS_TEMPLATE.format(
        ticker=ticker,
        date_from=date_from.strftime("%Y-%m-%d"),
        date_to=date_to.strftime("%Y-%m-%d"),
        token=FINNHUB_API_KEY,
    )

    try:
        content = http_get(url, user_agent="Mozilla/5.0 (news-fetch-bot)")
        raw_items = json.loads(content)
    except urllib.error.HTTPError as e:
        print(f"[finnhub:{ticker}] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[finnhub:{ticker}] fetch failed: {e}", file=sys.stderr)
        return []

    check_patterns = [_get_word_boundary_pattern(ticker)]
    if company_name:
        first_word = company_name.split()[0] if company_name.split() else None
        if first_word and len(first_word) > 2:
            check_patterns.append(_get_word_boundary_pattern(first_word))

    cutoff_ts = (date_to - date_from).total_seconds()
    now_ts = date_to.timestamp()

    items = []
    for entry in raw_items:
        item_ts = entry.get("datetime", 0)
        if now_ts - item_ts > cutoff_ts:
            continue  # mimo MAX_AGE_HOURS okno

        headline = entry.get("headline", "")
        summary = entry.get("summary", "")
        haystack = headline + " " + summary
        mentioned = any(p.search(haystack) for p in check_patterns)

        pub_dt = datetime.fromtimestamp(item_ts, tz=timezone.utc)

        items.append(
            {
                "source": "finnhub",
                "matched_ticker": ticker,
                "title": headline,
                "link": entry.get("url", ""),
                "pubDate": pub_dt.isoformat(),
                "description": summary,
                "company_mentioned_in_text": mentioned,
            }
        )
    return items


def match_portfolio(items, name_lookup):
    matched = []
    for item in items:
        if item.get("matched_ticker"):
            matched.append(item)
            continue
        haystack = item["title"] + " " + item["description"]
        for name_key, ticker in name_lookup.items():
            pattern = _get_word_boundary_pattern(name_key)
            if pattern.search(haystack):
                enriched = dict(item)
                enriched["matched_ticker"] = ticker
                enriched["matched_on"] = name_key  # diagnostika: co přesně matchlo
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
        name_lookup, us_tickers, yahoo_lookup = load_portfolio(args.tickers_file)
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

    # 3) Yahoo Finance RSS per ticker (VŠECHNY, ne jen US - mezinárodní pokrytí)
    yahoo_items_all = []
    for ticker, yahoo_ticker, company_name in yahoo_lookup:
        yahoo_items_all.extend(
            fetch_yahoo_items_for_ticker(ticker, yahoo_ticker, company_name)
        )

    # 4) Finnhub company-news per ticker (jen pokud je API klíč v prostředí)
    finnhub_items_all = []
    finnhub_skipped_no_key = not FINNHUB_API_KEY
    if not finnhub_skipped_no_key:
        for ticker, _yahoo_ticker, company_name in yahoo_lookup:
            finnhub_items_all.extend(
                fetch_finnhub_items_for_ticker(ticker, company_name)
            )

    # Matchování
    all_items_for_matching = (
        patria_items + sec_items_all + yahoo_items_all + finnhub_items_all
    )
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
                "errors": SEC_FETCH_ERRORS,
            },
            "yahoo": {
                "tickers_checked": len(yahoo_lookup),
                "item_count": len(yahoo_items_all),
            },
            "finnhub": {
                "skipped_no_api_key": finnhub_skipped_no_key,
                "tickers_checked": 0 if finnhub_skipped_no_key else len(yahoo_lookup),
                "item_count": len(finnhub_items_all),
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
