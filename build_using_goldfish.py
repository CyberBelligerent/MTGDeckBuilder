import argparse
import re

import requests
from bs4 import BeautifulSoup

from deck_source import WebScraperDeckSource, commander_to_slug


class GoldfishDeckSource(WebScraperDeckSource):
    BASE_URL = "https://www.mtggoldfish.com"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    @property
    def name(self) -> str:
        return "MTGGoldfish"

    @property
    def cache_key(self) -> str:
        return "goldfish"

    def _resolve_archetype(self, session: requests.Session, slug: str) -> str:
        # MTGGoldfish sometimes uses /archetype/commander-{slug}, sometimes /archetype/{slug}
        candidate = f"commander-{slug}"
        resp = self._get(session, f"{self.BASE_URL}/archetype/{candidate}/decks?page=1")
        if resp:
            soup = BeautifulSoup(resp.text, 'html.parser')
            if soup.find_all('a', href=re.compile(r'^/deck/\d+$')):
                print(f"  Using Goldfish archetype path: {candidate}")
                return candidate
        return slug

    def _get_deck_ids(self, session: requests.Session, commander_name: str, n_decks: int, already_have: int) -> list:
        slug      = commander_to_slug(commander_name)
        archetype = self._resolve_archetype(session, slug)
        deck_ids  = []
        page      = 1

        while len(deck_ids) < n_decks:
            url = f"{self.BASE_URL}/archetype/{archetype}/decks?page={page}"
            print(f"  Deck list page {page}: {url}")
            resp = self._get(session, url)
            if not resp:
                break

            soup     = BeautifulSoup(resp.text, 'html.parser')
            links    = soup.find_all('a', href=re.compile(r'^/deck/\d+$'))
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
            self._sleep()

        return deck_ids[:n_decks]

    def _download_deck(self, session: requests.Session, deck_id: str, commander_name: str) -> "dict | None":
        resp = self._get(session, f"{self.BASE_URL}/deck/download/{deck_id}")
        if not resp:
            return None

        cards = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\d+)\s+(.+)$', line)
            if m:
                cards.extend([m.group(2).strip()] * int(m.group(1)))

        return {"commander": commander_name, "cards": cards} if cards else None

# MTGGoldFish backward compatability call
def fetch_decks(commander_name: str, n_decks: int = 100, output_file: str = None, redownload: bool = False) -> list:
    return GoldfishDeckSource().fetch_decks(commander_name, n_decks=n_decks, output_file=output_file, redownload=redownload)