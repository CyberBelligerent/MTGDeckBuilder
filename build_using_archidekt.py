import requests

from deck_source import WebScraperDeckSource


class ArchidektDeckSource(WebScraperDeckSource):
    BASE_URL   = "https://archidekt.com/api"
    PAGE_SIZE  = 100

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    SLEEP_BASE   = 1.5
    SLEEP_JITTER = 1.0

    @property
    def name(self) -> str:
        return "Archidekt"

    @property
    def cache_key(self) -> str:
        return "archidekt"

    def _get_deck_ids(self, session: requests.Session, commander_name: str, n_decks: int, already_have: int) -> list:
        fetch_target = (n_decks + already_have) * 2
        search_name  = commander_name.split(' // ')[0]  # Only first name of a commander
        deck_ids     = []
        page         = 1

        while len(deck_ids) < fetch_target:
            print(f"  Search page {page}...")
            resp = self._get(session, f"{self.BASE_URL}/decks/v3/", params={
                "name":       search_name,
                "deckFormat": 3,
                "orderBy":    "-viewCount",
                "pageSize":   self.PAGE_SIZE,
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
            self._sleep()

        return deck_ids

    def _download_deck(self, session: requests.Session, deck_id: int, commander_name: str) -> "dict | None":
        resp = self._get(session, f"{self.BASE_URL}/decks/{deck_id}/")
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

        # Make sure it's actually the correct commander (Had issues with Archidekt sometimes)
        if actual_commander:
            ac_front = actual_commander.lower().split(' // ')[0]
            cn_front = commander_name.lower().split(' // ')[0]
            if ac_front != cn_front:
                print(f"    Skipping deck {deck_id}: commander is '{actual_commander}'")
                return None

        return {"commander": commander_name, "cards": cards} if cards else None
