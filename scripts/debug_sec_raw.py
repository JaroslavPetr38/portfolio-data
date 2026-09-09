#!/usr/bin/env python3
"""
scripts/debug_sec_raw.py

DIAGNOSTICKÝ skript - vypíše syrová data z SEC submissions API pro
JEDEN konkrétní ticker, BEZ jakékoli filtrace (žádný recency filter,
žádný Item whitelist, žádný limit). Určeno k jednorázovému spuštění
v GitHub Actions, ne k pravidelnému provozu.

Použití:
    python3 scripts/debug_sec_raw.py --ticker MU

Vypíše posledních 20 podání (jakéhokoli typu) s formulářem, datem
a Item kódem (pokud existuje), ať je vidět, co SEC pro ten ticker
skutečně eviduje jako nejnovější.
"""

import json
import sys
import argparse
import urllib.request
import urllib.error

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_USER_AGENT = "PortfolioManager-DebugBot/1.0 (kontakt@example.com)"  # uprav na svůj kontakt


def http_get(url, user_agent, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    ticker = args.ticker.upper()

    print(f"=== Diagnostika pro ticker: {ticker} ===\n")

    # 1) CIK lookup
    try:
        content = http_get(SEC_TICKER_MAP_URL, SEC_USER_AGENT)
        raw_map = json.loads(content)
    except Exception as e:
        print(f"CHYBA při stažení CIK mapy: {e}")
        sys.exit(1)

    cik10 = None
    company_title = None
    for entry in raw_map.values():
        if entry.get("ticker", "").upper() == ticker:
            cik10 = str(entry["cik_str"]).zfill(10)
            company_title = entry.get("title")
            break

    if not cik10:
        print(f"Ticker {ticker} NENALEZEN v SEC CIK mapě.")
        print("(To je očekávané pro ETF/fondy - viz KNOWN_NON_FILER_TICKERS.)")
        sys.exit(0)

    print(f"Nalezeno: CIK={cik10}, název dle SEC: '{company_title}'\n")

    # 2) Submissions fetch
    url = SEC_SUBMISSIONS_TEMPLATE.format(cik10=cik10)
    print(f"Fetchuji: {url}\n")
    try:
        content = http_get(url, SEC_USER_AGENT)
        data = json.loads(content)
    except urllib.error.HTTPError as e:
        print(f"CHYBA HTTP {e.code}: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"CHYBA při fetchování submissions: {e}")
        sys.exit(1)

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items = recent.get("items", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    print(f"Celkem podání v 'recent' poli: {len(forms)}\n")
    print(f"--- Posledních {args.count} podání (JAKÝKOLI typ, BEZ filtrace) ---")
    print(f"{'#':<4}{'Form':<10}{'Date':<14}{'Items':<20}{'Accession'}")
    print("-" * 70)
    for i in range(min(args.count, len(forms))):
        item_str = items[i] if i < len(items) else ""
        print(f"{i:<4}{forms[i]:<10}{dates[i]:<14}{item_str:<20}{accessions[i]}")

    # Souhrn: kolik 8-K je v CELÉM 'recent' poli, a jaké je nejnovější
    eight_k_indices = [i for i, f in enumerate(forms) if f == "8-K"]
    print(f"\n--- Souhrn 8-K podání ---")
    print(f"Celkem 8-K v 'recent' poli: {len(eight_k_indices)}")
    if eight_k_indices:
        newest_idx = eight_k_indices[0]
        print(f"Nejnovější 8-K: datum={dates[newest_idx]}, items={items[newest_idx]}")
    else:
        print("Žádné 8-K v celém 'recent' poli (neobvyklé pro aktivní firmu).")


if __name__ == "__main__":
    main()
