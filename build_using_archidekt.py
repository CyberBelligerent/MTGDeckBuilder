import argparse
import json
import os
import random
import re
import time

import requests

BASE_URL     = "https://archidekt.com/api"
SLEEP_BASE   = 1.5   # seconds between every request
SLEEP_JITTER = 1.0   # additional random seconds
MAX_RETRIES  = 3     # retries on rate-limit / server errors
BACKOFF_BASE = 4.0   # seconds for first backoff
PAGE_SIZE    = 100   # max results per search page

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

def commander_to_slug(name: str) -> str:
    # For DFC cards ("Front // Back"), use only the front face for slug generation
    name = name.split(' // ')[0]
    slug = name.lower()
    slug = re.sub(r"[',\.]", '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    return slug

def _sleep():
    time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))

def _get(session: requests.Session, url: str, params: dict = None) -> requests.Response | None:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, params=params, timeout=30)

            if resp.status_code == 200:
                return resp

            if resp.status_code in (401, 403, 429):
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"  Rate limited (HTTP {resp.status_code}). "
                      f"Backing off {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue

            print(f"  HTTP {resp.status_code}: {url}")
            return None

        except requests.RequestException as e:
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"  Request error: {e}. Retrying in {wait:.0f}s...")
            time.sleep(wait)

    print(f"  Giving up after {MAX_RETRIES} retries: {url}")
    return None

def search_deck_ids(session: requests.Session, commander_name: str, n_decks: int) -> list:
    deck_ids = []
    page = 1
    # For DFC cards use only the front face (how users title their decks)
    search_name = commander_name.split(' // ')[0]

    while len(deck_ids) < n_decks:
        print(f"  Search page {page}...")
        resp = _get(session, f"{BASE_URL}/decks/v3/", params={
            "name":       search_name,
            "deckFormat": 3,
            "orderBy":    "-viewCount",
            "pageSize":   PAGE_SIZE,
            "page":       page,
        })
        if not resp:
            break

        try:
            data = resp.json()
        except Exception as e:
            print(f"  Failed to parse search response as JSON: {e}")
            break

        results = data.get("results") or []
        if not results:
            break

        for deck in results:
            deck_id = deck.get("id")
            if deck_id is not None:
                deck_ids.append(deck_id)

        print(f"  +{len(results)} decks  (running total: {len(deck_ids)})")

        if not data.get("next"):
            break

        page += 1
        _sleep()

    return deck_ids[:n_decks]


def fetch_deck(session: requests.Session, deck_id: int, commander_name: str) -> dict | None:
    resp = _get(session, f"{BASE_URL}/decks/{deck_id}/")
    if not resp:
        return None

    try:
        data = resp.json()
    except Exception as e:
        print(f"    Failed to parse deck {deck_id} as JSON: {e}")
        return None

    cards = []
    actual_commander = None

    for entry in data.get("cards") or []:
        try:
            oracle_card = (entry.get("card") or {}).get("oracleCard") or {}
            card_name   = oracle_card.get("name")
            qty         = entry.get("quantity") or 1
            categories  = entry.get("categories") or []

            if not card_name:
                continue

            if "Commander" in categories:
                actual_commander = card_name

            cards.extend([card_name] * qty)
        except Exception as e:
            print(f"    Skipping malformed card entry in deck {deck_id}: {e}")
            continue

    # Verify this is actually a deck for the right commander (Weirdly happens every now and again?)
    # Also handle DFC cards: actual_commander may be "Front // Back" while commander_name is "Front"
    if actual_commander:
        ac_lower  = actual_commander.lower()
        cn_lower  = commander_name.lower()
        cn_front  = cn_lower.split(' // ')[0]
        ac_front  = ac_lower.split(' // ')[0]
        if ac_front != cn_front:
            print(f"    Skipping deck {deck_id}: commander is '{actual_commander}'")
            return None

    if not cards:
        return None

    return {"commander": commander_name, "cards": cards}

def _load_cache(output_file: str) -> list:
    if output_file and os.path.exists(output_file):
        try:
            with open(output_file, encoding='utf-8') as f:
                return json.load(f).get("decks", [])
        except Exception:
            pass
    return []

def _save_cache(output_file: str, decks: list):
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({"decks": decks}, f, indent=2)

def fetch_decks(commander_name: str, n_decks: int = 100, output_file: str = None, redownload: bool = False) -> list:
    if redownload and output_file and os.path.exists(output_file):
        print(f"  --redownload: deleting cache '{output_file}'")
        os.remove(output_file)

    decks = _load_cache(output_file) if output_file else []
    already_have = len(decks)

    if already_have >= n_decks:
        print(f"\n[Archidekt] Cache already has {already_have} decks — skipping download.")
        return decks[:n_decks]

    if already_have:
        print(f"\n[Archidekt] Resuming: {already_have} decks cached, "
              f"need {n_decks - already_have} more for '{commander_name}'...")
    else:
        print(f"\n[Archidekt] Fetching up to {n_decks} decks for '{commander_name}'...")

    session = requests.Session()
    need    = n_decks - already_have

    # Fetches more than needed incase decks are invalid
    deck_ids = search_deck_ids(session, commander_name, (n_decks + already_have) * 2)
    if not deck_ids:
        print("  No decks found on Archidekt.")
        return decks

    # Skip downloaded IDs
    remaining_ids = deck_ids[already_have:]
    print(f"\n  Downloading up to {len(remaining_ids)} deck(s) "
          f"(targeting {need} valid, skipping {already_have} cached)...")
    decks = list(decks)
    for i, deck_id in enumerate(remaining_ids, already_have + 1):
        if len(decks) >= n_decks:
            break
        print(f"  [{i:>3}/{n_decks}] Deck ID {deck_id} - Archidekt")
        try:
            deck = fetch_deck(session, deck_id, commander_name)
            if deck:
                decks.append(deck)
                if output_file:
                    _save_cache(output_file, decks)   # incremental save
        except Exception as e:
            print(f"    Unexpected error on deck {deck_id}: {e}")
        _sleep()

    fetched = len(decks) - already_have
    print(f"\n  Done. {fetched} new deck(s) downloaded  "
          f"({len(decks)} total in cache).")

    return decks