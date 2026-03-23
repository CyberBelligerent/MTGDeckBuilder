from abc import ABC, abstractmethod
import json
import os
import random
import re
import threading
import time

import requests


def commander_to_slug(name: str) -> str:
    # For DFC cards ("Front // Back"), use only the front face
    name = name.split(' // ')[0]
    slug = name.lower()
    slug = re.sub(r"[',\.]", '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    return slug

# Decided to merge all deck sources into one class. Let's see if this works and allows for better
#   better community support on adding ways to get decks
class DeckSource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        # Name that'll be shown in log
        pass

    @property
    def cache_key(self) -> str:
        # The short name that will show as base_{source_slug}.json
        return re.sub(r'[^a-z0-9]', '_', self.name.lower())

    @abstractmethod
    def fetch_decks(self, commander_name: str, n_decks: int = 100, output_file: str = None, redownload: bool = False) -> list:
        # How decks are ACTUALLY pulled
        pass

# Looking into custom JSON decks for more flexability on custom decks. Not for sure how this will be
#   implemented just yet... But will allow for custom decks to be built and used to go out of the meta
class JsonDeckSource(DeckSource):

    def __init__(self, json_file: str):
        self._json_file = json_file

    @property
    def name(self) -> str:
        return "JSON"

    def fetch_decks(self, commander_name: str, n_decks: int = 100, output_file: str = None, redownload: bool = False) -> list:
        if not os.path.exists(self._json_file):
            print(f"  [{self.name}] File not found: {self._json_file}")
            return []
        try:
            with open(self._json_file, encoding='utf-8') as f:
                decks = json.load(f).get("decks", [])
            print(f"  [{self.name}] Loaded {len(decks)} decks from {self._json_file}")
            return decks[:n_decks]
        except Exception as e:
            print(f"  [{self.name}] Failed to load {self._json_file}: {e}")
            return []

# Class for pulling from websites (Either scraping or API calling)
#   Must create _get_deck_ids() - Recommended to pull extra.
#   Must create _download_deck() - How you actually download and parse a deck
#   Already handles things like sleeping between pulling, caching, and incrementally saving decks
class WebScraperDeckSource(DeckSource, ABC):

    SLEEP_BASE   = 2.0
    SLEEP_JITTER = 1.0
    MAX_RETRIES  = 4
    BACKOFF_BASE = 4.0

    HEADERS: dict = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _sleep(self):
        time.sleep(self.SLEEP_BASE + random.uniform(0, self.SLEEP_JITTER))

    # Helper function for getting decks from the URL. Already has builtin method for rate-limiting
    def _get(self, session: requests.Session, url: str, params: dict = None) -> "requests.Response | None":
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = session.get(url, headers=self.HEADERS, params=params, timeout=30)

                if resp.status_code == 200:
                    return resp

                if resp.status_code in (401, 403, 429):
                    wait = self.BACKOFF_BASE * (2 ** attempt)
                    print(
                        f"  Rate limited (HTTP {resp.status_code}). "
                        f"Backing off {wait:.0f}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})..."
                    )
                    time.sleep(wait)
                    continue

                print(f"  HTTP {resp.status_code}: {url}")
                return None

            except requests.RequestException as e:
                wait = self.BACKOFF_BASE * (2 ** attempt)
                print(f"  Request error: {e}. Retrying in {wait:.0f}s...")
                time.sleep(wait)

        print(f"  Giving up after {self.MAX_RETRIES} retries: {url}")
        return None

    # Pulls cache from decks already downloaded in-case someone decides to run again
    def _load_cache(self, output_file: str) -> list:
        if output_file and os.path.exists(output_file):
            try:
                with open(output_file, encoding='utf-8') as f:
                    return json.load(f).get("decks", [])
            except Exception:
                pass
        return []

    def _save_cache(self, output_file: str, decks: list):
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({"decks": decks}, f, indent=2)

    @abstractmethod
    def _get_deck_ids(self, session: requests.Session, commander_name: str, n_decks: int, already_have: int) -> list:
        # Should return a list of decks ids to download later! already_have can be used to pull from caching
        #   To help with not downloading again or overly using internet
        #   all variables are already passed to function!
        pass

    @abstractmethod
    def _download_deck(self, session: requests.Session, deck_id, commander_name: str) -> "dict | None":
        # Actually downloads the decks based on deck_id. This should be used to parse the deck
        #   Return None if no parsing or issues come up
        #   all variables are already passed to function!
        pass

    def fetch_decks(self, commander_name: str, n_decks: int = 100, output_file: str = None, redownload: bool = False) -> list:
        if redownload and output_file and os.path.exists(output_file):
            print(f"  --redownload: deleting cache '{output_file}'")
            os.remove(output_file)

        decks        = self._load_cache(output_file) if output_file else []
        already_have = len(decks)

        if already_have >= n_decks:
            print(f"\n[{self.name}] Cache already has {already_have} decks — skipping download.")
            return decks[:n_decks]

        if already_have:
            print(
                f"\n[{self.name}] Resuming: {already_have} cached, "
                f"need {n_decks - already_have} more for '{commander_name}'..."
            )
        else:
            print(f"\n[{self.name}] Fetching up to {n_decks} decks for '{commander_name}'...")

        session  = requests.Session()
        deck_ids = self._get_deck_ids(session, commander_name, n_decks, already_have)

        if not deck_ids:
            print(f"  No decks found on {self.name}.")
            return decks

        remaining_ids = deck_ids[already_have:]
        print(
            f"\n  Downloading up to {len(remaining_ids)} deck(s) "
            f"(targeting {n_decks - already_have} valid, skipping {already_have} cached)..."
        )

        decks = list(decks)
        for i, deck_id in enumerate(remaining_ids, already_have + 1):
            if len(decks) >= n_decks:
                break
            print(f"  [{i:>3}/{n_decks}] Deck ID {deck_id} — {self.name}")
            try:
                deck = self._download_deck(session, deck_id, commander_name)
                if deck:
                    decks.append(deck)
                    if output_file:
                        self._save_cache(output_file, decks)
            except Exception as e:
                print(f"    Unexpected error on deck {deck_id}: {e}")
            self._sleep()

        fetched = len(decks) - already_have
        print(f"\n  Done. {fetched} new deck(s) downloaded ({len(decks)} total in cache).")
        return decks[:n_decks]

# Deck downloading orchestrator. This is how the different DeckSources are registered to one system
#   Should already split the target amount of decks between all sources and run them in parallel
#   After everything, it'll merge all downloaded decks into 1 json file
#   Automatic retry to different DeckSources if any couldn't meet their quota
# EXAMPLE:
#   registry.register(GoldfishDeckSource())
#   decks = registry.fetch_decks("Edgar Markov", n_decks=100, output_file="community_decks/edgar-markov_decks.json")
class DeckSourceRegistry:
    
    def __init__(self):
        self._sources: list[DeckSource] = []

    def register(self, source: DeckSource) -> "DeckSourceRegistry":
        self._sources.append(source)
        return self

    def fetch_decks(self, commander_name: str, n_decks: int, output_file: str, redownload: bool = False) -> list:
        if not self._sources:
            raise RuntimeError("No deck sources registered.")

        # Checked the final merged output if it exists first and delete if doing a full rescrape
        if redownload and os.path.exists(output_file):
            os.remove(output_file)

        if not redownload and os.path.exists(output_file):
            try:
                with open(output_file, encoding='utf-8') as f:
                    cached = json.load(f).get("decks", [])
                if len(cached) >= n_decks:
                    print(f"\n[Registry] Merged cache already has {len(cached)} decks — skipping download.")
                    return cached[:n_decks]
            except Exception:
                pass

        # Build per-source targets and cache paths
        n = len(self._sources)
        targets = [n_decks // n] * n
        for i in range(n_decks % n):
            targets[i] += 1

        base = output_file.rsplit('.', 1)[0] if '.' in os.path.basename(output_file) else output_file
        cache_files = [f"{base}_{src.cache_key}.json" for src in self._sources]

        label = " + ".join(s.name for s in self._sources)
        print(f"\n[Registry] Fetching {n_decks} decks via [{label}] in parallel...")
        for src, tgt in zip(self._sources, targets):
            print(f"  {src.name}: targeting {tgt}")

        results: list[list] = [[] for _ in self._sources]

        def _run(idx: int):
            results[idx] = self._sources[idx].fetch_decks(commander_name, n_decks=targets[idx], output_file=cache_files[idx], redownload=redownload)

        threads = [
            threading.Thread(target=_run, args=(i,), name=self._sources[i].name)
            for i in range(n)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"\n[Registry] Parallel phase done:")
        for src, tgt, res in zip(self._sources, targets, results):
            print(f"  {src.name}: {len(res)}/{tgt}")

        # Redistribution of sources that couldn't meet their quota
        short_indices   = [i for i in range(n) if len(results[i]) < targets[i]]
        cover_indices   = [i for i in range(n) if i not in short_indices]
        total_shortfall = sum(targets[i] - len(results[i]) for i in short_indices)

        if total_shortfall > 0 and cover_indices:
            extra_base = total_shortfall // len(cover_indices)
            extra_rem  = total_shortfall % len(cover_indices)
            for j, i in enumerate(cover_indices):
                extra = extra_base + (1 if j < extra_rem else 0)
                if extra == 0:
                    continue
                new_target = targets[i] + extra
                print(
                    f"\n  {self._sources[i].name} covering {extra} shortfall deck(s) "
                    f"(new target: {new_target})..."
                )
                results[i] = self._sources[i].fetch_decks(commander_name, n_decks=new_target, output_file=cache_files[i], redownload=False)

        # Merge all results
        decks = [deck for res in results for deck in res]

        if len(decks) < n_decks:
            print(f"\n  Warning: couldn't pull {n_decks} decks — only found {len(decks)}.")
        else:
            decks = decks[:n_decks]

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({"decks": decks}, f, indent=2)

        print(f"\n  {len(decks)} decks ready.")
        return decks
