import json
import os
import random
import re
import time

import requests
from bs4 import BeautifulSoup

BASE_URL     = "https://www.mtggoldfish.com"
SLEEP_BASE   = 2.0
SLEEP_JITTER = 1.0
MAX_RETRIES  = 3
BACKOFF_BASE = 4.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def commander_to_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[',\.]", '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    return slug

def _sleep():
    time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))

def _get(session: requests.Session, url: str) -> requests.Response | None:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)

            if resp.status_code == 200:
                return resp

            if resp.status_code in (401, 403, 429):
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"  Rate limited (HTTP {resp.status_code}). "
                      f"Backing off {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue

            # Other HTTP errors — don't retry
            print(f"  HTTP {resp.status_code}: {url}")
            return None

        except requests.RequestException as e:
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"  Request error: {e}. Retrying in {wait:.0f}s...")
            time.sleep(wait)

    print(f"  Giving up after {MAX_RETRIES} retries: {url}")
    return None

def get_deck_ids(session: requests.Session, commander_name: str, n_decks: int) -> list:
    slug = commander_to_slug(commander_name)
    deck_ids = []
    page = 1

    while len(deck_ids) < n_decks:
        url = f"{BASE_URL}/archetype/{slug}/decks?page={page}"
        print(f"  Deck list page {page}: {url}")

        resp = _get(session, url)
        if not resp:
            break

        soup = BeautifulSoup(resp.text, 'html.parser')
        links = soup.find_all('a', href=re.compile(r'^/deck/\d+$'))

        ids_on_page = list(dict.fromkeys(
            re.search(r'/deck/(\d+)', a['href']).group(1)
            for a in links
        ))

        if not ids_on_page:
            print(f"  No deck IDs found on page {page} — stopping.")
            break

        deck_ids.extend(ids_on_page)
        print(f"  +{len(ids_on_page)} decks  (running total: {len(deck_ids)})")

        if not soup.find('a', rel='next'):
            break

        page += 1
        _sleep()

    return deck_ids[:n_decks]


def download_deck(session: requests.Session, deck_id: str, commander_name: str) -> dict | None:
    url = f"{BASE_URL}/deck/download/{deck_id}"
    resp = _get(session, url)
    if not resp:
        return None

    cards = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r'^(\d+)\s+(.+)$', line)
        if match:
            qty       = int(match.group(1))
            card_name = match.group(2).strip()
            cards.extend([card_name] * qty)

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
        print(f"\n[MTGGoldfish] Cache already has {already_have} decks — skipping download.")
        return decks[:n_decks]

    if already_have:
        print(f"\n[MTGGoldfish] Resuming: {already_have} decks cached, "
              f"need {n_decks - already_have} more for '{commander_name}'...")
    else:
        print(f"\n[MTGGoldfish] Fetching up to {n_decks} decks for '{commander_name}'...")

    session  = requests.Session()
    need     = n_decks - already_have

    deck_ids = get_deck_ids(session, commander_name, n_decks)
    if not deck_ids:
        print("  No decks found on MTGGoldfish.")
        return decks

    remaining_ids = deck_ids[already_have:]
    print(f"\n  Downloading {len(remaining_ids)} deck(s) "
          f"(skipping {already_have} already cached)...")

    for i, deck_id in enumerate(remaining_ids, already_have + 1):
        if len(decks) - already_have >= need:
            break
        print(f"  [{i:>3}/{n_decks}] Deck ID {deck_id} - MTGGoldFish")
        deck = download_deck(session, deck_id, commander_name)
        if deck:
            decks.append(deck)
            if output_file:
                _save_cache(output_file, decks)   # incremental save
        _sleep()

    fetched = len(decks) - already_have
    print(f"\n  Done. {fetched} new deck(s) downloaded  "
          f"({len(decks)} total in cache).")

    return decks[:n_decks]
