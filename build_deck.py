# Used to download and actual build the decks from MTGGoldFish and Archidekt

import json
import os
import re
import threading
import time
from contextlib import contextmanager

from deck_source import commander_to_slug  # re-exported; gui.py imports this from here
import numpy as np
import pandas as pd
from collections import Counter
from sklearn.ensemble import RandomForestClassifier
from sklearn.multioutput import MultiOutputClassifier
from tqdm import tqdm
import joblib
import requests

DECK_SIZE        = 99     # cards excluding the commander
N_LAND_TARGET    = 36     # default target land count
MAX_REPLACE_TRIES = 3     # unowned ideal cards to attempt replacement for per missing slot
MAX_REPLACE_SIM  = 50     # similar cards to search per replacement attempt
SYNERGY_WEIGHT       = 0.25   # how much EDHRec synergy scores influence candidate ranking
COMBO_WEIGHT         = 0.20   # how much combo ownership completion boosts card scores
SYNERGY_PHASE_WEIGHT = 0.30   # how much synergy score influences Phase A greedy selection
SWAP_MIN_IMPROVEMENT = 0.05   # minimum synergy delta required to perform a Phase B swap
SWAP_MAX_PASSES      = 20     # maximum number of swaps Phase B will make

DATA_DIR     = "data"
CARD_FILE    = os.path.join(DATA_DIR, "all_cards.json")
FEATURE_CSV  = os.path.join(DATA_DIR, "mtg_cards_features.csv")

MODELS_DIR         = "models"
COMMUNITY_DECKS_DIR = "community_decks"
BUILT_DECKS_DIR    = "built_decks"

# Greedy builder targets
TARGETS = {
    "is_creature": (0.30, 0.7),
    "avg_cmc":     (2.8,  0.5),
}

# Minimum card count per functional role for a balanced deck. This is.... best intent. Really just my preference
ROLE_MINIMUMS = {
    "ramp":        10,
    "draw":         8,
    "removal":      6,
    "interaction":  2,
    "tutor":        2,
}

COLOR_ID_COLS = {
    'W': 'color_identity_W',
    'U': 'color_identity_U',
    'B': 'color_identity_B',
    'R': 'color_identity_R',
    'G': 'color_identity_G',
}

BASIC_LANDS = {
    'W': 'Plains',
    'U': 'Island',
    'B': 'Swamp',
    'R': 'Mountain',
    'G': 'Forest',
}

# Known fetch lands
FETCH_LANDS: frozenset = frozenset({
    'Flooded Strand', 'Polluted Delta', 'Bloodstained Mire',
    'Wooded Foothills', 'Windswept Heath',
    'Marsh Flats', 'Scalding Tarn', 'Verdant Catacombs',
    'Arid Mesa', 'Misty Rainforest',
    'Bad River', 'Flood Plain', 'Mountain Valley',
    'Rocky Tar Pit', 'Grasslands',
    'Evolving Wilds', 'Terramorphic Expanse',
    'Fabled Passage', 'Prismatic Vista', 'Myriad Landscape',
    'Thawing Glaciers', 'Shimmerdrift Vale',
})

_PIP_CAP = 0.70   # max single-color share in the mana profile after capping

# Default nonbasic land counts per category when not overridden by the GUI
_DEFAULT_NONBASIC_COUNTS: dict = {'utility': 4, 'fetch': 4, 'fixing': 8}

_BASIC_LAND_NAMES: frozenset = frozenset(BASIC_LANDS.values()) | {'Wastes'}

def detect_role(oracle_text: str) -> str:
    """
    Attempt to place a card into a "role" based on oracle text.
    
    Needs more work and potentially more roles
    
    Roles:
      ramp        — mana ramping or land fetching
      draw        — card drawing
      removal     — single target or multi-target removales
      interaction — counterspells (ETC. Negate)
      tutor       — Search cards
      token-gen   — Token cards
      
      Everything else becomes value because I am lazy... Will need to revamp this section later
      value       — everything else (threats, utility, synergy pieces)
    """
    import re
    t = oracle_text.lower()

    _ROLE_PATTERNS = [
        ("ramp",        [
            r"add \{",
            r"search your library for a.*\bland\b",
            r"put.*\bland\b.*onto the battlefield",
            r"you may play an? additional land",
        ]),
        ("draw",        [
            r"draw (a|two|three|four|\d+) card",
            r"draw x card",
            r"draw cards equal",
            r"its controller draws",
        ]),
        ("removal",     [
            r"destroy target",
            r"exile target",
            r"destroy all",
            r"exile all",
            r"deals? \d+ damage to (target|any|each)",
            r"deals? x damage",
            r"-\d+/-\d+",
        ]),
        ("interaction", [
            r"counter target (spell|ability|activated|triggered)",
            r"counter that (spell|ability)",
            r"can't be countered",
            r"spells? (your opponents? cast|cast by opponents?) cost \{",
        ]),
        ("tutor",       [
            r"search your library for (an? |up to \d+ )\S+(?: card| cards)",
        ]),
        ("token-gen",   [
            r"create (a|an|\d+|x) \S.*token",
        ]),
    ]

    for role, patterns in _ROLE_PATTERNS:
        for pat in patterns:
            if re.search(pat, t):
                return role
    return "value"

# Laods the users cards
def load_owned_cards(path: str) -> set:
    if not os.path.exists(path):
        print(f"\nERROR: Could not find '{path}'.")
        print(
            "\nCreate a plain text file listing one card per line. All of the following formats are accepted:\n"
            "\n    Command Tower"
            "\n    1 Command Tower"
            "\n    1x Command Tower"
            "\n    4x Lightning Bolt"
            "\n"
            "\nQuantities are stripped automatically — duplicates are ignored."
            "\nYou can also add comments with // at the start of a line:\n"
            "\n    // Lands"
            "\n    Command Tower"
            "\n    1x Reliquary Tower"
            "\n"
            f"\nSave the file as '{path}' in the same folder as build_deck.py, then re-run."
        )
        raise SystemExit(1)

    owned = set()
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('//'):
                continue
            line = re.sub(r'^\d+x?\s+', '', line)
            owned.add(line)
    return owned

# Added so people know the program didn't crash!
@contextmanager
def _heartbeat(message: str, interval: int = 10):
    stop = threading.Event()
    def _beat():
        while not stop.wait(interval):
            print(message)
    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


# This method was largely updated to include the fix for double facing card names (NAME // NAME)
#   And returning the full name if partial was given
def resolve_commander_name(commander_name: str, cards_json_path: str) -> str:
    name_lower = commander_name.lower()
    with open(cards_json_path, 'r', encoding='utf-8') as f:
        cards = json.load(f)
    for card in cards:
        full_name = card.get('name', '')
        if full_name.lower() == name_lower:
            return full_name
    for card in cards:
        full_name = card.get('name', '')
        if full_name.lower().startswith(name_lower + ' //'):
            return full_name
    return commander_name


def get_commander_colors(commander_name: str, cards_json_path: str) -> list:
    name_lower = commander_name.lower()
    with open(cards_json_path, 'r', encoding='utf-8') as f:
        cards = json.load(f)
    for card in cards:
        if card.get('name', '').lower() == name_lower:
            return card.get('color_identity', [])
    # DFC prefix fallback
    for card in cards:
        full_name = card.get('name', '')
        if full_name.lower().startswith(name_lower + ' //'):
            return card.get('color_identity', [])
    return []


def is_color_legal(card_name: str, card_df: pd.DataFrame, commander_colors: list) -> bool:
    if card_name not in card_df.index:
        return False
    row = card_df.loc[card_name]
    for color, col in COLOR_ID_COLS.items():
        if color not in commander_colors and col in card_df.columns and row[col] == 1:
            return False
    return True


def filter_by_color_identity(card_df: pd.DataFrame, commander_colors: list) -> pd.DataFrame:
    mask = pd.Series(True, index=card_df.index)
    for color, col in COLOR_ID_COLS.items():
        if color not in commander_colors and col in card_df.columns:
            mask &= (card_df[col] == 0)
    return card_df[mask]

# Attempts to download from all DeckSources incrementally
#   downloads are saved every deck in-case the program crashes or you close it. So safe to restart
#   it will create a slug cache based on website first and then output to a single file
def scrape_commander_decks(commander_name: str, output_file: str, n_decks: int = 100, redownload: bool = False) -> list:
    from build_using_goldfish import GoldfishDeckSource
    from build_using_archidekt import ArchidektDeckSource
    from deck_source import DeckSourceRegistry

    registry = DeckSourceRegistry()
    registry.register(GoldfishDeckSource())
    registry.register(ArchidektDeckSource())
    return registry.fetch_decks(commander_name, n_decks, output_file, redownload)

# Grabs inclusion% and synergy% from EDHRec to be used as higher ranking cards during generation
def scrape_edhrec_synergy(commander_name: str, output_file: str, redownload: bool = False) -> dict:
    if not redownload and os.path.exists(output_file):
        print(f"  Loading cached synergy data: {output_file}")
        with open(output_file, encoding='utf-8') as f:
            return json.load(f)

    slug = commander_to_slug(commander_name)
    url  = f"https://json.edhrec.com/pages/commanders/{slug}.json"
    print(f"  Fetching EDHRec synergy from {url} ...")

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Could not reach EDHRec ({e}) — skipping synergy.")
        return {}

    try:
        cardlists = data["container"]["json_dict"]["cardlists"]
    except (KeyError, TypeError) as e:
        print(f"  WARNING: Unexpected EDHRec JSON structure ({e}) — skipping synergy.")
        return {}

    synergy = {}
    for cardlist in cardlists:
        for card in cardlist.get('cardviews', []):
            name = card.get('name', '').strip()
            if not name:
                continue
            syn_score  = float(card.get('synergy', 0.0))
            num_decks  = card.get('num_decks', 0)
            pot_decks  = card.get('potential_decks', 0)
            inclusion  = (num_decks / pot_decks) if pot_decks > 0 else 0.0
            synergy[name] = {"synergy": syn_score, "inclusion": inclusion}

    if not synergy:
        print("  WARNING: No synergy cards parsed from EDHRec — skipping synergy.")
        return {}

    print(f"  EDHRec synergy: {len(synergy)} cards loaded.")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(synergy, f, indent=2)
    return synergy

# Grab potential combos and score them higher. This scores based on what YOU have
#   Meaning, if your deck grabbed 1 card of a 2 card combo, through that second card as high as possible to be grabbed
def scrape_edhrec_combos(commander_name: str, output_file: str, redownload: bool = False) -> list:
    if not redownload and os.path.exists(output_file):
        print(f"  Loading cached combo data: {output_file}")
        with open(output_file, encoding='utf-8') as f:
            return json.load(f)

    slug = commander_to_slug(commander_name)
    url  = f"https://json.edhrec.com/pages/combos/{slug}.json"
    print(f"  Fetching EDHRec combos from {url} ...")

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Could not reach EDHRec combos ({e}) — skipping combo data.")
        return []

    try:
        cardlists = data["container"]["json_dict"]["cardlists"]
    except (KeyError, TypeError) as e:
        print(f"  WARNING: Unexpected EDHRec combos structure ({e}) — skipping combo data.")
        return []

    combos = []
    for entry in cardlists:
        cardviews = entry.get("cardviews", [])
        card_names = [cv.get("name", "").strip() for cv in cardviews if cv.get("name")]
        if len(card_names) < 2:
            continue
        count = entry.get("combo", {}).get("count", 0)
        combos.append({"cards": card_names, "count": count})

    if not combos:
        print("  WARNING: No combos parsed from EDHRec — skipping combo data.")
        return []

    print(f"  EDHRec combos: {len(combos)} combos loaded.")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(combos, f, indent=2)
    return combos

# calculates (owned_pieces / total_pieces)^2 as a combo boost score
def _combo_signal(all_cards: list, owned_set: set, slug: str) -> np.ndarray | None:
    combos_file = os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_combos.json")
    if not os.path.exists(combos_file):
        return None

    with open(combos_file, encoding='utf-8') as f:
        combos = json.load(f)

    if not combos:
        return None

    card_idx = {c: i for i, c in enumerate(all_cards)}
    signal   = np.zeros(len(all_cards))

    for combo in combos:
        cards = combo.get("cards", [])
        if len(cards) < 2:
            continue
        owned_count = sum(1 for c in cards if c in owned_set)
        ratio = owned_count / len(cards)
        boost = ratio ** 2  # 50%→0.25, 75%→0.56, 100%→1.0
        for card in cards:
            if card in card_idx:
                idx = card_idx[card]
                signal[idx] = max(signal[idx], boost)

    if signal.max() > 0:
        signal = signal / signal.max()

    n_boosted = int((signal > 0).sum())
    print(f"      Combo signal: {n_boosted} cards boosted across {len(combos)} combos.")
    return signal

def load_card_features(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.set_index('name', inplace=True)
    return df[~df.index.duplicated(keep='first')]

# Attempt to get or created a new model based on decks downloaded
def get_or_train_model(model_path: str, decks: list, card_df: pd.DataFrame, all_cards: list):
    type_cols = [c for c in card_df.columns if c.startswith("is_")]

    if os.path.exists(model_path):
        print(f"  Loading cached model: {model_path}")
        return joblib.load(model_path), type_cols

    print(f"  Training on {len(decks)} decks, {len(all_cards)} candidate cards...")
    with _heartbeat("  Still training..."):
        X, Y = [], []

        for deck in decks:
            deck_cards = [c for c in deck["cards"] if c in card_df.index]
            deck_cards_set = set(deck_cards)
            if not deck_cards:
                continue
            try:
                deck_df = card_df.loc[deck_cards]
                vec          = np.mean(deck_df.values, axis=0)
                avg_cmc      = deck_df['cmc'].mean()
                std_cmc      = deck_df['cmc'].std()
                type_pcts    = deck_df[type_cols].sum() / len(deck_df)
                feature_vec  = np.concatenate([vec, [avg_cmc, std_cmc], type_pcts.values])
                label        = [1 if c in deck_cards_set else 0 for c in all_cards]
                X.append(feature_vec)
                Y.append(label)
            except Exception:
                continue

        X, Y = np.array(X), np.array(Y)
        model = MultiOutputClassifier(RandomForestClassifier(n_estimators=100, n_jobs=-1))
        model.fit(X, Y)
    joblib.dump(model, model_path)
    print(f"  Model saved: {model_path}")
    return model, type_cols

# Score the deck based on how much it differs from targets
def score_deck(sample_df: pd.DataFrame, targets: dict) -> float:
    if len(sample_df) == 0:
        return float('inf')

    score = 0.0
    total = len(sample_df)

    if "avg_cmc" in targets:
        target_val, weight = targets["avg_cmc"]
        score += weight * abs(sample_df["cmc"].mean() - target_val)

    for col in ["is_creature", "is_artifact", "is_enchantment",
                "is_planeswalker", "is_sorcery", "is_instant"]:
        if col in targets and col in sample_df.columns:
            target_val, weight = targets[col]
            score += weight * abs(sample_df[col].sum() / total - target_val)

    return score

# The meat and potatoes. The base
#   Attemps to fill decks for targets and highest scored cards.
#   This may be either phased out or drastically amped up, since it can grab cards you
#   don't really want....
def greedy_deck_builder(card_df: pd.DataFrame, candidates: list, targets: dict, max_cards: int, synergy_scores: dict | None = None, synergy_factor: float = 0.0) -> list:
    deck = []
    remaining = set(candidates)
    _syn = synergy_scores or {}

    with tqdm(total=max_cards, desc="  Greedy build") as pbar:
        while len(deck) < max_cards and remaining:
            best_card, best_score = None, float('inf')
            for card in remaining:
                penalty = score_deck(card_df.loc[deck + [card]], targets)
                if synergy_factor > 0:
                    penalty -= synergy_factor * _syn.get(card, 0.0)
                if penalty < best_score:
                    best_score, best_card = penalty, card
            if not best_card:
                break
            deck.append(best_card)
            remaining.remove(best_card)
            pbar.update(1)

    return deck

# Helper for the swap phase (B) to HELP not change a creature for an instant. Trying to stay close to targets
def _primary_type(name: str, card_df: pd.DataFrame) -> str:
    if name not in card_df.index:
        return 'other'
    row = card_df.loc[name]
    for t in ('creature', 'planeswalker', 'instant', 'sorcery', 'artifact', 'enchantment'):
        if row.get(f'is_{t}', 0) == 1:
            return t
    return 'other'

# Phase B - Swap
#   Attemps to look at bad value or bad synergy cards and swap them with something that might help your deck more
#   This phase does attempt to keep CMC at target... but may need more work
def synergy_swap_pass(deck: list, owned_set: set, card_df: pd.DataFrame, commander_colors: list, synergy_fn, targets: dict, commander_name: str = "", max_swaps: int = SWAP_MAX_PASSES, min_improvement: float = SWAP_MIN_IMPROVEMENT,) -> tuple[list, list]:
    from collections import defaultdict

    deck = list(deck)
    deck_set = set(deck)
    swap_log: list[tuple] = []
    used_candidates: set = set()

    # Pre-group owned non-deck non-land candidates by primary type,
    # sorted descending by synergy so the inner loop can break early.
    commander_lower = commander_name.lower()
    pool_by_type: dict[str, list] = defaultdict(list)
    for name in owned_set:
        if name in deck_set or name not in card_df.index:
            continue
        if commander_lower and name.lower() == commander_lower:
            continue
        if card_df.loc[name, 'is_land'] == 1:
            continue
        if not is_color_legal(name, card_df, commander_colors):
            continue
        pool_by_type[_primary_type(name, card_df)].append(name)

    for ptype, lst in pool_by_type.items():
        lst.sort(key=lambda n: -synergy_fn(n))

    # Sort deck cards worst-synergy-first so the most replaceable go first.
    deck_scored = sorted(
        [(c, synergy_fn(c)) for c in deck if c in card_df.index],
        key=lambda x: x[1],
    )

    target_cmc = targets.get('avg_cmc', (2.8, 0.5))[0]

    for card_out, score_out in deck_scored:
        if len(swap_log) >= max_swaps:
            break

        ptype_out  = _primary_type(card_out, card_df)
        candidates = pool_by_type.get(ptype_out, [])

        for cand in candidates:
            if cand in deck_set or cand in used_candidates:
                continue

            improvement = synergy_fn(cand) - score_out
            if improvement < min_improvement:
                break  # pool is sorted and nothing better follows

            # Attempt to keep CMC on target
            trial_names = [cand if c == card_out else c for c in deck]
            trial_df    = card_df.loc[[c for c in trial_names if c in card_df.index]]
            if 'cmc' in trial_df.columns and abs(trial_df['cmc'].mean() - target_cmc) > 0.8:
                continue

            # Apply swap
            deck[deck.index(card_out)] = cand
            deck_set.discard(card_out)
            deck_set.add(cand)
            used_candidates.add(cand)
            swap_log.append((card_out, cand, improvement))
            print(f"  ↑ Swap: '{card_out}' (syn={score_out:.3f})"
                  f" → '{cand}' (+{improvement:.3f})")
            break

    return deck, swap_log

# Helper function to calculate how much DEFAULT/PLAIN mana would be required if you don't have any other lands
def calculate_mana_profile(spell_list: list, card_df: pd.DataFrame, commander_colors: list) -> dict:
    if not commander_colors:
        return {}
    known  = [c for c in spell_list if c in card_df.index]
    sub_df = card_df.loc[known] if known else pd.DataFrame()

    raw = {}
    for color in commander_colors:
        col = f'mana_pips_{color}'
        raw[color] = float(sub_df[col].sum()) if (
            not sub_df.empty and col in sub_df.columns
        ) else 1.0

    total = sum(raw.values()) or 1.0
    capped = {c: min(v / total, _PIP_CAP) for c, v in raw.items()}
    cap_total = sum(capped.values()) or 1.0
    return {c: w / cap_total for c, w in capped.items()}

# Helper to attemp to put lands into specific buckets so you don't get like 34/36 of your mana as utility mana (Unless you want that)
def _classify_land(name: str, card_df: pd.DataFrame, commander_colors: list) -> str:
    """
    Lands currently fall into one of three buckets:
      'fetch'   — searches for other lands
      'fixing'  — produces 2+ of the commander's colors
      'utility' — everything else
    """
    if name in FETCH_LANDS:
        return 'fetch'
    if name not in card_df.index:
        return 'utility'
    row = card_df.loc[name]
    coverage = sum(
        1 for c in commander_colors
        if COLOR_ID_COLS.get(c) in card_df.columns and row.get(COLOR_ID_COLS[c], 0) == 1
    )
    return 'fixing' if coverage >= 2 else 'utility'

# Ranks land based on how much the community uses that card
def _load_land_freq(deck_json: str) -> dict:
    if not os.path.exists(deck_json):
        return {}
    try:
        import json as _json
        with open(deck_json, encoding='utf-8') as fh:
            data = _json.load(fh)
        decks = data.get('decks', [])
        if not decks:
            return {}
        counts: dict = {}
        for deck in decks:
            for card in set(deck.get('cards', [])):
                counts[card] = counts.get(card, 0) + 1
        n = len(decks)
        return {card: cnt / n for card, cnt in counts.items()}
    except Exception:
        return {}


def build_mana_base(non_land_deck: list, card_df_full: pd.DataFrame, owned_set: set, commander_colors: list, n_lands: int, nonbasic_counts: dict | None = None, land_freq: dict | None = None) -> tuple[list, dict]:
    """
    Three-phase mana base builder.

    Phase C1 — Mana profile
        Calls calculate_mana_profile() on the final spell list.
        Derives how many basic land slots to protect from nonbasic allocation.

    Phase C2 — Nonbasic allocation
        Classifies owned legal lands into utility / fetch / fixing.
        Ranks each category by community-deck inclusivity (land_freq).
        Fills up to nonbasic_counts[category] slots per category.
        Order: fixing → fetch → utility (color fixing is highest priority).

    Phase C3 — Basic backfill
        Fills all remaining slots (reserved basics + any unfilled C2 slots)
        with basic lands split proportionally by the mana profile weights.

    Returns (land_deck, mana_stats) where mana_stats holds per-category counts
    and the computed mana profile.
    """
    _empty_stats = {'mana_profile': {}, 'utility': 0, 'fetch': 0, 'fixing': 0, 'basic': n_lands}
    if not commander_colors:
        return ['Wastes'] * n_lands, _empty_stats

    counts = {**_DEFAULT_NONBASIC_COUNTS, **(nonbasic_counts or {})}
    freq   = land_freq or {}

    # ── Phase C1: Mana profile ─────────────────────────────────────────────────
    profile      = calculate_mana_profile(non_land_deck, card_df_full, commander_colors)
    total_nb     = sum(counts[k] for k in ('utility', 'fetch', 'fixing'))
    basics_target = max(0, n_lands - total_nb)

    if total_nb > n_lands:
        print(f"  WARNING: Nonbasic target ({total_nb}) exceeds land count ({n_lands}). "
              f"Running {n_lands - min(total_nb, n_lands)} basics.")

    print(f"  Mana profile: "
          + ', '.join(f"{c}={w*100:.0f}%" for c, w in sorted(profile.items())))
    print(f"  Nonbasic target: {total_nb}  |  Basic target: {basics_target}")

    # ── Phase C2: Nonbasic allocation ──────────────────────────────────────────
    deck_set = set(non_land_deck)
    categorized: dict[str, list] = {'utility': [], 'fetch': [], 'fixing': []}

    for name in owned_set:
        if name in deck_set or name not in card_df_full.index:
            continue
        row = card_df_full.loc[name]
        if row.get('is_land', 0) != 1:
            continue
        if 'commander_legal' in card_df_full.columns and not row.get('commander_legal', True):
            continue
        if not is_color_legal(name, card_df_full, commander_colors):
            continue
        cat = _classify_land(name, card_df_full, commander_colors)
        categorized[cat].append(name)

    # Rank each category by community inclusivity (highest first)
    for lst in categorized.values():
        lst.sort(key=lambda n: -freq.get(n, 0.0))

    # Fill categories: fixing → fetch → utility (color coverage first)
    land_deck: list  = []
    used_set         = set(deck_set)
    cat_counts       = {'utility': 0, 'fetch': 0, 'fixing': 0}

    for cat in ('fixing', 'fetch', 'utility'):
        target = min(counts[cat], n_lands - len(land_deck))
        for name in categorized[cat]:
            if cat_counts[cat] >= target:
                break
            if name not in used_set:
                land_deck.append(name)
                used_set.add(name)
                cat_counts[cat] += 1

    # ── Phase C3: Basic backfill ───────────────────────────────────────────────
    remaining = n_lands - len(land_deck)
    if remaining > 0 and profile:
        alloc = {c: max(0, round(remaining * w)) for c, w in profile.items()}
        diff  = remaining - sum(alloc.values())
        if diff != 0:
            top = max(profile, key=profile.get)
            alloc[top] += diff
        for color, cnt in alloc.items():
            basic = BASIC_LANDS.get(color)
            if basic and cnt > 0:
                land_deck.extend([basic] * cnt)
    elif remaining > 0:
        land_deck.extend(['Wastes'] * remaining)

    n_basic = sum(1 for c in land_deck if c in _BASIC_LAND_NAMES)
    mana_stats = {
        'mana_profile': profile,
        'utility': cat_counts['utility'],
        'fetch':   cat_counts['fetch'],
        'fixing':  cat_counts['fixing'],
        'basic':   n_basic,
    }
    return land_deck[:n_lands], mana_stats

# 
def find_owned_replacement(card_name: str, owned_set: set, deck_set: set,
                           card_finder, card_df_full: pd.DataFrame,
                           commander_colors: list,
                           preferred_role: str = None,
                           oracle_index: dict = None) -> str | None:
    """
    Search for the most similar owned, color-legal card not already in the deck.

    If preferred_role and oracle_index are provided, tries role-matched replacements
    first (same functional role as the candidate), then falls back to the best
    NLP-similar owned card regardless of role.

    Always passes gates:
      1. Owned by the user
      2. Not already in the deck
      3. Within the commander's color identity

    Returns the card name, or None if no valid replacement is found.
    """
    try:
        similar = card_finder.find_similar(card_name, top_n=MAX_REPLACE_SIM)
    except Exception as e:
        print(f"    CardFinder error for '{card_name}': {e}")
        return None

    def _passes_gates(name):
        return (name not in deck_set
                and name in owned_set
                and is_color_legal(name, card_df_full, commander_colors))

    # Pass 1 — role-matched replacements (if a preferred role was requested)
    if preferred_role and oracle_index:
        for _, row in similar.iterrows():
            name = row['name']
            if not _passes_gates(name):
                continue
            if detect_role(oracle_index.get(name, "")) == preferred_role:
                print(f"    ↳ '{card_name}' → '{name}'  "
                      f"(role={preferred_role}, similarity {row['score']:.3f})")
                return name

    # Pass 2 — fallback: best NLP-similar owned card regardless of role
    for _, row in similar.iterrows():
        name = row['name']
        if not _passes_gates(name):
            continue
        role_tag = f", role={detect_role(oracle_index.get(name, ''))}" if oracle_index else ""
        print(f"    ↳ '{card_name}' → '{name}'  (similarity {row['score']:.3f}{role_tag})")
        return name

    print(f"    ✗ No owned replacement found for '{card_name}'")
    return None

# Primary type precedence for display (first match wins)
_TYPE_LABELS = [
    ("is_creature",     "Creature"),
    ("is_instant",      "Instant"),
    ("is_sorcery",      "Sorcery"),
    ("is_artifact",     "Artifact"),
    ("is_enchantment",  "Enchantment"),
    ("is_planeswalker", "Planeswalker"),
    ("is_land",         "Land"),
]

# THIS SHOULD NOT BE TRUSTED YET
# Attemps to build out a suggestion of what cards you COULD add to your deck
#   So you know potentially what to buy to make your deck stronger
def get_upgrade_suggestions(commander_name: str, owned_path: str, current_deck: list, feature_csv: str = FEATURE_CSV, cards_json: str = CARD_FILE, n: int = 60) -> list:
    slug       = commander_to_slug(commander_name)
    model_path = os.path.join(MODELS_DIR,          f"{slug}_model.joblib")
    deck_json  = os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_decks.json")

    if not os.path.exists(model_path) or not os.path.exists(deck_json):
        return []

    owned_set        = load_owned_cards(owned_path)
    card_df_full     = load_card_features(feature_csv)
    commander_colors = get_commander_colors(commander_name, cards_json)

    card_df = card_df_full.copy()
    if 'commander_legal' in card_df.columns:
        card_df = card_df[card_df['commander_legal'] == True]
    if commander_colors:
        card_df = filter_by_color_identity(card_df, commander_colors)

    with open(deck_json, encoding='utf-8') as f:
        decks = json.load(f).get("decks", [])

    all_cards = list(dict.fromkeys(
        c for deck in decks for c in deck["cards"] if c in card_df.index
    ))

    type_cols          = [c for c in card_df.columns if c.startswith("is_")]
    model, _type_cols  = get_or_train_model(model_path, decks, card_df, all_cards)

    # Guard against stale model/card-pool mismatch
    if len(model.estimators_) != len(all_cards):
        all_cards = all_cards[:len(model.estimators_)]

    n_features = card_df.shape[1] + 2 + len(type_cols)
    starter    = np.zeros((1, n_features))

    probabilities = model.predict_proba(starter)
    probs = np.array([
        p[0][1] if p.shape[1] > 1 else float(model.estimators_[i].classes_[0])
        for i, p in enumerate(probabilities)
    ])

    deck_set        = set(current_deck)
    commander_lower = commander_name.lower()
    suggestions     = []

    for i in np.argsort(probs)[::-1]:
        card = all_cards[i]
        if card in owned_set or card in deck_set:
            continue
        if card.lower() == commander_lower:
            continue
        if card not in card_df.index:
            continue

        row       = card_df.loc[card]
        card_type = "Other"
        for col, label in _TYPE_LABELS:
            if col in card_df.columns and row.get(col, 0) == 1:
                card_type = label
                break

        suggestions.append({
            "name":  card,
            "type":  card_type,
            "cmc":   float(row.get("cmc", 0)),
            "score": float(probs[i]),
        })

        if len(suggestions) >= n:
            break

    return suggestions

def _find_deck_combo_helper(card_name: str) -> dict:
    return {'card': card_name, 'quantity': 1}

# Finds combos you have in the deck
#   included isn't... working like the API suggests, so currently seems to only use almostIncluded with card matching rules
#   For example, Niv-Mizzet, Parun and Tandem Lookout. It gives me an almostIncluded not included. I think it's because requirements must be met
#   Potentially need to look at requirements later
def find_deck_combos(deck_cards: list, commander_name: str) -> list[dict]:
    import requests

    commander_norm = commander_name.strip().lower()

    payload = {
        "commanders": [_find_deck_combo_helper(commander_name.strip())],
        "cards": [
            _find_deck_combo_helper(c.strip())
            for c in deck_cards
            if c and c.strip().lower() != commander_norm
        ],
    }
    
    try:
        resp = requests.post(
            "https://backend.commanderspellbook.com/find-my-combos?format=json&ordering=-popularity%2Cidentity_count%2Ccard_count%2C-created&q=legal%3Acommander",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Commander Spellbook query failed: {e}")
        return []

    results = []
    for combo in data.get("results", {}).get("included", []):
        combo_id = str(combo.get("id", ""))
        uses = [b for b in combo.get("uses", [])]
        cards = [u["card"]["name"] for u in uses]
        
        for card in cards:
            if card not in deck_cards:
                continue
        
        produces = [p['feature']["name"] for p in combo.get("produces", [])]

        combo_object = {
            "id": combo_id,
            "cards": cards,
            "produces": produces,
            "url": f"https://commanderspellbook.com/combo/{combo_id}/",
        }
        
        results.append(combo_object)
        
    for combo in data.get("results", {}).get("almostIncluded", []):
        combo_id = str(combo.get("id", ""))
        uses = [b for b in combo.get("uses", [])]
        cards = [u["card"]["name"] for u in uses]
        
        if not all(card in deck_cards for card in cards):
            continue
        
        produces = [p['feature']["name"] for p in combo.get("produces", [])]

        combo_object = {
            "id": combo_id,
            "cards": cards,
            "produces": produces,
            "url": f"https://commanderspellbook.com/combo/{combo_id}/",
        }
        
        results.append(combo_object)

    results.sort(key=lambda x: len(x["cards"]))
    return results


# Pulls percentages of what the community is doing so you have an idea
#   of potentially what to set your targets to
#   Might change this later to a range. Since some decks might be outliers causing weird averages
#   Maybe also lock this to 100%? Since... right now it does not add to that
def compute_community_averages(deck_json_path: str, feature_csv: str) -> dict:
    if not os.path.exists(deck_json_path) or not os.path.exists(feature_csv):
        return {}

    with open(deck_json_path, encoding='utf-8') as f:
        decks = json.load(f).get("decks", [])

    card_df = load_card_features(feature_csv)
    type_cols = ["is_creature", "is_artifact", "is_enchantment",
                 "is_planeswalker", "is_sorcery", "is_instant"]

    type_sums = {col: 0.0 for col in type_cols}
    cmc_sum   = 0.0
    count     = 0

    for deck in decks:
        cards = [c for c in deck.get("cards", []) if c in card_df.index]
        if not cards:
            continue
        df    = card_df.loc[cards]
        total = len(df)
        for col in type_cols:
            if col in df.columns:
                type_sums[col] += df[col].sum() / total
        if 'cmc' in df.columns:
            non_land = df[df['is_land'] == 0] if 'is_land' in df.columns else df
            if len(non_land) > 0:
                cmc_sum += non_land['cmc'].mean()
        count += 1

    if count == 0:
        return {}

    result = {col: type_sums[col] / count for col in type_cols}
    result['avg_cmc'] = cmc_sum / count
    result['n_decks'] = count
    return result

# Trains an actual Panda model and saves it as a .joblib file to be used later when making decks
def create_model(commander_name: str, deck_json: str = None, model_path: str = None, redownload: bool = False, cards_json: str = CARD_FILE, feature_csv: str = FEATURE_CSV, n_decks: int = 100) -> str:
    commander_name = resolve_commander_name(commander_name, cards_json)
    slug       = commander_to_slug(commander_name)
    os.makedirs(COMMUNITY_DECKS_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR,          exist_ok=True)
    deck_json  = deck_json  or os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_decks.json")
    model_path = model_path or os.path.join(MODELS_DIR,          f"{slug}_model.joblib")

    divider = '─' * 60
    print(f"\n{divider}")
    print(f"  Creating model for: {commander_name}")
    print(f"{divider}")

    print(f"\n[1/4] Looking up '{commander_name}'...")
    commander_colors = get_commander_colors(commander_name, cards_json)
    if commander_colors:
        print(f"      Color identity: {' '.join(commander_colors) or 'Colorless'}")
    else:
        print(f"      WARNING: Commander not found. Aborting...")
        return False

    print(f"\n[2/4] Fetching community decks...")
    decks = scrape_commander_decks(commander_name, deck_json, n_decks,
                                   redownload=redownload)
    print(f"      {len(decks)} decks ready.")

    print(f"\n[3/4] Fetching EDHRec synergy + combo data...")
    synergy_file = os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_synergy.json")
    scrape_edhrec_synergy(commander_name, synergy_file, redownload=redownload)
    combos_file = os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_combos.json")
    scrape_edhrec_combos(commander_name, combos_file, redownload=redownload)

    print(f"\n[4/4] Training model...")
    card_df_full = load_card_features(feature_csv)
    card_df = card_df_full.copy()
    if 'commander_legal' in card_df.columns:
        card_df = card_df[card_df['commander_legal'] == True]
    if commander_colors:
        card_df = filter_by_color_identity(card_df, commander_colors)

    all_cards = list(dict.fromkeys(
        c for deck in decks for c in deck["cards"] if c in card_df.index
    ))
    print(f"      {len(all_cards)} unique candidate cards in the pool.")

    # Always retrain so the model reflects the current deck pool
    if os.path.exists(model_path):
        print(f"      Removing stale model to force retrain...")
        os.remove(model_path)

    get_or_train_model(model_path, decks, card_df, all_cards)
    print(f"\n  Model ready: {model_path}")
    return True
    #return model_path


def build_deck(commander_name: str, owned_path: str, deck_json: str = None, model_path: str = None, cards_json: str = CARD_FILE, feature_csv: str = FEATURE_CSV, n_lands: int = N_LAND_TARGET, targets: dict = None, strategy: str = "default", role_minimums: dict = None, nonbasic_counts: dict | None = None) -> list:
    commander_name = resolve_commander_name(commander_name, cards_json)
    slug         = commander_to_slug(commander_name)
    deck_json    = deck_json  or os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_decks.json")
    model_path   = model_path or os.path.join(MODELS_DIR,          f"{slug}_model.joblib")
    _role_mins   = role_minimums if role_minimums is not None else ROLE_MINIMUMS
    _targets   = targets if targets is not None else TARGETS

    # Make sure everything is present
    missing = []
    if not os.path.exists(deck_json):
        missing.append(f"  • Community decks : {deck_json}")
    if not os.path.exists(model_path):
        missing.append(f"  • Trained model   : {model_path}")
    if missing:
        print(f"\nERROR: Cannot build deck for '{commander_name}' — missing files:")
        for m in missing:
            print(m)
        print(f"\n  Run 'Create Model' first to download community decks and train the model.")
        raise SystemExit(1)

    print(f"\n{'─' * 60}")
    print(f"[1/7] Loading owned cards from '{owned_path}'...")
    owned_set = load_owned_cards(owned_path)
    print(f"      {len(owned_set):,} unique cards owned.")

    print(f"\n[2/7] Looking up '{commander_name}'...")
    commander_colors = get_commander_colors(commander_name, cards_json)
    if commander_colors:
        color_str = ' '.join(commander_colors) or 'Colorless'
        print(f"      Color identity: {color_str}")
    else:
        print(f"      WARNING: Commander not found — no color filter applied.")

    print(f"\n[3/7] Loading community decks...")
    with open(deck_json, encoding='utf-8') as f:
        decks = json.load(f).get("decks", [])
    print(f"      {len(decks)} decks loaded.")

    print(f"\n[4/7] Loading card features...")
    card_df_full = load_card_features(feature_csv)

    # Filter out cards that much the commanders colors
    card_df = card_df_full.copy()
    if 'commander_legal' in card_df.columns:
        card_df = card_df[card_df['commander_legal'] == True]
    if commander_colors:
        card_df = filter_by_color_identity(card_df, commander_colors)

    print(f"      {len(card_df):,} cards after legal + color filter.")

    print(f"\n[5/7] Preparing model...")

    # Pulls UNIQUE cards from the community decks
    all_cards = list(dict.fromkeys(
        c for deck in decks for c in deck["cards"] if c in card_df.index
    ))

    model, type_cols = get_or_train_model(model_path, decks, card_df, all_cards)

    print(f"\n[6/7] Scoring candidates...")

    # Blank starter vector
    n_features = card_df.shape[1] + 2 + len(type_cols)
    starter    = np.zeros((1, n_features))

    # Only ran if you delete the cached decks and put in a new one without changing the model
    if len(model.estimators_) != len(all_cards):
        print(f"\n  WARNING: Cached model has {len(model.estimators_)} outputs "
              f"but current deck pool has {len(all_cards)} unique cards. "
              f"The model is stale — delete '{model_path}' and re-run to retrain.")
        all_cards = all_cards[:len(model.estimators_)]

    probabilities = model.predict_proba(starter)
    probs = np.array([
        p[0][1] if p.shape[1] > 1 else float(model.estimators_[i].classes_[0])
        for i, p in enumerate(probabilities)
    ])

    # Blend EDHRec synergy and inclusion scores into candidate ranking
    _edhrec_get_syn  = None
    _edhrec_get_inc  = None
    _edhrec_card_set: frozenset = frozenset()
    synergy_file = os.path.join(COMMUNITY_DECKS_DIR, f"{slug}_synergy.json")
    if os.path.exists(synergy_file):
        with open(synergy_file, encoding='utf-8') as f:
            synergy_scores = json.load(f)
        def _get_syn(c):
            v = synergy_scores.get(c, 0.0)
            return v.get("synergy", 0.0) if isinstance(v, dict) else float(v)
        def _get_inc(c):
            v = synergy_scores.get(c, 0.0)
            return v.get("inclusion", 0.0) if isinstance(v, dict) else 0.0

        syn_vec = np.array([max(0.0, _get_syn(c)) for c in all_cards])
        inc_vec = np.array([max(0.0, _get_inc(c)) for c in all_cards])

        if syn_vec.max() > 0:
            syn_vec = syn_vec / syn_vec.max()
        if inc_vec.max() > 0:
            inc_vec = inc_vec / inc_vec.max()

        edhrec_signal = 0.7 * syn_vec + 0.3 * inc_vec
        probs = (1 - SYNERGY_WEIGHT) * probs + SYNERGY_WEIGHT * edhrec_signal
        _edhrec_get_syn  = _get_syn
        _edhrec_get_inc  = _get_inc
        _edhrec_card_set = frozenset(synergy_scores.keys())
        n_syn = sum(1 for c in all_cards if _get_syn(c) > 0)
        n_inc = sum(1 for c in all_cards if _get_inc(c) > 0)
        print(f"      EDHRec signal applied (weight={SYNERGY_WEIGHT}): "
              f"{n_syn} cards with synergy, {n_inc} with inclusion data.")
    else:
        print(f"      No synergy data found — using model scores only.")

    # Checks for combos from the method above
    if strategy == "combo":
        print(f"      Strategy: combo-aware (COMBO_WEIGHT={COMBO_WEIGHT})")
        combo_vec = _combo_signal(all_cards, owned_set, slug)
        if combo_vec is not None:
            probs = (1 - COMBO_WEIGHT) * probs + COMBO_WEIGHT * combo_vec
        else:
            print(f"      No combo data found — run 'Create Model' to fetch it.")
    else:
        print(f"      Strategy: default")

    card_score = {all_cards[i]: float(probs[i]) for i in range(len(all_cards))}

    # All owned + color-legal cards ranked by model scores
    commander_lower = commander_name.lower()
    all_owned_legal = sorted(
        [c for c in owned_set if c in card_df.index and c.lower() != commander_lower],
        key=lambda c: -card_score.get(c, 0.0)
    )

    # Separate top unowned candidates for use in the replacement subroutine
    sorted_all_idx     = np.argsort(probs)[::-1]
    top_unowned        = [
        all_cards[i] for i in sorted_all_idx
        if all_cards[i] not in owned_set
        and all_cards[i] in card_df.index
        and all_cards[i].lower() != commander_lower
    ]

    owned_non_land  = [c for c in all_owned_legal if card_df.loc[c, 'is_land'] == 0]
    print(f"      {len(all_owned_legal)} owned legal cards  "
          f"({len(owned_non_land)} non-land)")

    print(f"\n[7/7] Building deck...")

    n_non_land = DECK_SIZE - n_lands

    # Phase A
    print(f"\n  Phase A: Greedy build "
          f"({n_non_land} non-land slots, {len(owned_non_land)} candidates, "
          f"synergy_factor={SYNERGY_PHASE_WEIGHT})")
    with _heartbeat("  Still building..."):
        deck_non_land = greedy_deck_builder(
            card_df, owned_non_land, _targets, n_non_land,
            synergy_scores=card_score,
            synergy_factor=SYNERGY_PHASE_WEIGHT,
        )
    deck_set       = set(deck_non_land)
    n_owned_direct = len(deck_non_land)

    # Phase B
    print(f"\n  Phase B: Synergy swap pass (max {SWAP_MAX_PASSES} swaps, "
          f"min improvement {SWAP_MIN_IMPROVEMENT:.2f})...")

    def _synergy_score(name: str) -> float:
        return card_score.get(name, 0.0)

    deck_non_land, swap_log = synergy_swap_pass(
        deck_non_land, owned_set, card_df, commander_colors,
        synergy_fn=_synergy_score,
        targets=_targets,
        commander_name=commander_name,
    )
    n_swapped = len(swap_log)
    if swap_log:
        print(f"  Phase B complete: {n_swapped} swap(s) made.")
    else:
        print(f"  Phase B complete: no beneficial swaps found.")
    deck_set = set(deck_non_land)

    # Phase C
    shortfall = n_non_land - len(deck_non_land)
    if shortfall > 0:
        print(f"\n  Phase C: NLP replacement ({shortfall} missing non-land slots)...")
        print(f"  Initializing card similarity engine (this may take a moment)...")
        from card_finder import CardFinder
        finder = CardFinder(
            feature_csv=feature_csv,
            card_json=cards_json,
            deck_file=deck_json,
        )

        oracle_index = finder.oracle

        # Count roles already covered by Phases A+B
        deck_roles = Counter(detect_role(oracle_index.get(c, "")) for c in deck_non_land)
        print(f"  Role coverage after Phases A+B: "
              + ", ".join(f"{r}={deck_roles.get(r, 0)}/{_role_mins.get(r, 0)}"
                          for r in _role_mins))

        # NLP replacement for cards
        replacement_attempts = 0
        for candidate in top_unowned:
            if len(deck_non_land) >= n_non_land:
                break
            if card_df.loc[candidate, 'is_land'] == 1:
                continue

            replacement_attempts += 1
            if replacement_attempts > shortfall * MAX_REPLACE_TRIES:
                break

            # Prefer a same-role replacement when that role is under its minimum
            candidate_role = detect_role(oracle_index.get(candidate, ""))
            role_needed    = deck_roles.get(candidate_role, 0) < _role_mins.get(candidate_role, 0)
            preferred_role = candidate_role if role_needed else None

            print(f"  Seeking replacement for unowned card: '{candidate}' "
                  f"(role={candidate_role}"
                  + (", prioritising role match" if preferred_role else "") + ")")
            replacement = find_owned_replacement(
                candidate, owned_set, deck_set, finder, card_df_full, commander_colors,
                preferred_role=preferred_role, oracle_index=oracle_index,
            )
            if replacement:
                deck_non_land.append(replacement)
                deck_set.add(replacement)
                repl_role = detect_role(oracle_index.get(replacement, ""))
                deck_roles[repl_role] += 1
    else:
        finder = None

    n_owned_nlp = len(deck_non_land) - n_owned_direct
    print(f"\n  Non-land cards in deck: {len(deck_non_land)}")

    # Phase D
    print(f"\n  Phase D: Building mana base ({n_lands} land slots)...")
    land_freq = _load_land_freq(deck_json)
    land_deck, mana_stats = build_mana_base(
        deck_non_land, card_df_full, owned_set, commander_colors, n_lands,
        nonbasic_counts=nonbasic_counts,
        land_freq=land_freq,
    )
    print(f"  Land cards: {len(land_deck)}  "
          f"(fixing={mana_stats['fixing']}, fetch={mana_stats['fetch']}, "
          f"utility={mana_stats['utility']}, basic={mana_stats['basic']})")

    n_basic_lands = mana_stats['basic']
    n_owned_lands = len(land_deck) - n_basic_lands

    final_deck = deck_non_land + land_deck

    # SOMEHOW if the deck was too small to fill cards, just fill with land
    n_filler = 0
    if len(final_deck) < DECK_SIZE:
        gap      = DECK_SIZE - len(final_deck)
        n_filler = gap
        basic    = BASIC_LANDS.get(commander_colors[0], 'Plains') if commander_colors else 'Plains'
        print(f"\n  WARNING: Deck is {gap} cards short — filling with {basic}.")
        final_deck.extend([basic] * gap)

    # Deck quality metrics
    _all_non_basics    = [c for c in final_deck if c not in _BASIC_LAND_NAMES]
    _model_scored      = [c for c in _all_non_basics if c in card_score]
    _freq_scored       = [c for c in _all_non_basics if c in land_freq]
    _edhrec_scored     = [c for c in _all_non_basics if c in _edhrec_card_set]

    avg_model_score = (
        sum(card_score[c] for c in _model_scored) / len(_model_scored)
        if _model_scored else 0.0
    )
    avg_inclusivity = (
        sum(land_freq[c] for c in _freq_scored) / len(_freq_scored)
        if _freq_scored else 0.0
    )
    n_model_scored  = len(_model_scored)
    n_freq_scored   = len(_freq_scored)

    # EDHRec synergy / inclusion averages
    if _edhrec_scored:
        avg_edhrec_synergy   = sum(_edhrec_get_syn(c) for c in _edhrec_scored) / len(_edhrec_scored)
        avg_edhrec_inclusion = sum(_edhrec_get_inc(c) for c in _edhrec_scored) / len(_edhrec_scored)
        n_edhrec_scored      = len(_edhrec_scored)
    else:
        avg_edhrec_synergy   = None
        avg_edhrec_inclusion = None
        n_edhrec_scored      = 0

    stats = {
        "owned_direct":    n_owned_direct,
        "owned_swapped":   n_swapped,
        "owned_nlp":       n_owned_nlp,
        "owned_lands":     n_owned_lands,
        "basic_lands":     n_basic_lands,
        "filler":          n_filler,
        "total":           DECK_SIZE,
        "mana_profile":    mana_stats['mana_profile'],
        "land_utility":    mana_stats['utility'],
        "land_fetch":      mana_stats['fetch'],
        "land_fixing":     mana_stats['fixing'],
        "avg_inclusivity":       avg_inclusivity,
        "avg_model_score":       avg_model_score,
        "avg_edhrec_synergy":    avg_edhrec_synergy,
        "avg_edhrec_inclusion":  avg_edhrec_inclusion,
        "n_model_scored":        n_model_scored,
        "n_freq_scored":         n_freq_scored,
        "n_edhrec_scored":       n_edhrec_scored,
        "n_non_basics":          len(_all_non_basics),
    }
    return final_deck[:DECK_SIZE], stats


def print_deck_report(commander_name: str, deck: list, card_df_full: pd.DataFrame, output_file: str = "deck_output.txt", stats: dict = None):
    known    = [c for c in deck if c in card_df_full.index]
    deck_df  = card_df_full.loc[known]
    total    = len(deck) + 1

    divider = '═' * 60
    print(f"\n{divider}")
    print(f"  Commander : {commander_name}")
    print(f"  Deck size : {len(deck)} cards  (+1 commander = {total})")
    print(f"{divider}\n")

    # Type distribution
    type_cols = [c for c in card_df_full.columns if c.startswith('is_')]
    print("Card Type Distribution:")
    for col in type_cols:
        if col not in deck_df.columns:
            continue
        pct = deck_df[col].sum() / total * 100
        bar = '█' * int(pct / 2)
        print(f"  {col[3:].capitalize():<15} {pct:5.1f}%  {bar}")

    if 'cmc' in deck_df.columns:
        print(f"\n  Avg CMC: {deck_df['cmc'].mean():.2f}")

    # Card list by section
    sections = [
        ('is_creature',     'Creatures'),
        ('is_planeswalker', 'Planeswalkers'),
        ('is_artifact',     'Artifacts'),
        ('is_enchantment',  'Enchantments'),
        ('is_instant',      'Instants'),
        ('is_sorcery',      'Sorceries'),
        ('is_land',         'Lands'),
    ]

    output_lines = [f"// Commander", f"1 {commander_name}", ""]
    basic_counts = Counter(c for c in deck if c in BASIC_LANDS.values())

    printed_cards: set = set()
    print(f"\n{'─' * 60}")
    for col, label in sections:
        if col not in deck_df.columns:
            continue
        # Skips lands, added below
        if col == 'is_land':
            cards = sorted(
                c for c in deck_df[deck_df[col] == 1].index
                if c not in BASIC_LANDS.values() and c not in printed_cards
            )
        else:
            cards = sorted(
                c for c in deck_df[deck_df[col] == 1].index
                if c not in printed_cards
            )

        if not cards:
            continue

        print(f"\n{label} ({len(cards)}):")
        output_lines.append(f"// {label}")
        for card in cards:
            print(f"  1 {card}")
            output_lines.append(f"1 {card}")
        output_lines.append("")
        printed_cards.update(cards)

    if basic_counts:
        print(f"\nBasic Lands:")
        output_lines.append("// Basic Lands")
        for name, count in sorted(basic_counts.items()):
            print(f"  {count} {name}")
            output_lines.append(f"{count} {name}")
        output_lines.append("")

    # Collection coverage summary
    coverage_lines = []
    if stats:
        t = stats["total"]
        owned_total = stats["owned_direct"] + stats["owned_nlp"] + stats["owned_lands"]

        rows = [
            ("Owned — model selected",  stats["owned_direct"],   "cards chosen directly from your collection"),
            ("Owned — synergy swapped", stats["owned_swapped"],  "low-synergy cards replaced by Phase B swap pass"),
            ("Owned — NLP matched",     stats["owned_nlp"],      "owned cards added via Phase C NLP replacement"),
            ("Owned — utility lands",   stats["owned_lands"],  "owned lands chosen for the mana base"),
            ("Basic lands",             stats["basic_lands"],  "procedurally added to fill the mana base"),
        ]
        if stats["filler"]:
            rows.append(("Filler (small collection)", stats["filler"], "added because collection ran out of cards"))

        print(f"\n{'─' * 60}")
        print("Collection Coverage:")
        for label, count, note in rows:
            pct = count / t * 100
            print(f"  {label:<30} {count:>3} cards  ({pct:4.1f}%)  {note}")
        print(f"  {'─' * 56}")
        print(f"  {'Total from your collection':<30} {owned_total:>3} cards  ({owned_total / t * 100:4.1f}%)")

        avg_inc      = stats.get("avg_inclusivity", 0.0)
        avg_mod      = stats.get("avg_model_score", 0.0)
        avg_esyn     = stats.get("avg_edhrec_synergy")
        avg_einc     = stats.get("avg_edhrec_inclusion")
        n_nb         = stats.get("n_non_basics", 1)
        n_mod_sc     = stats.get("n_model_scored", 0)
        n_freq_sc    = stats.get("n_freq_scored", 0)
        n_edhrec_sc  = stats.get("n_edhrec_scored", 0)

        print(f"\n  Deck Quality (scored non-basic cards only):")
        print(f"    Avg community inclusivity : {avg_inc*100:5.1f}%  "
              f"({n_freq_sc}/{n_nb} cards have data)")
        print(f"    Avg model score           : {avg_mod*100:5.1f}%  "
              f"({n_mod_sc}/{n_nb} cards have data)")
        if avg_esyn is not None:
            print(f"    Avg EDHRec synergy        : {avg_esyn:+.4f}  "
                  f"({n_edhrec_sc}/{n_nb} cards have data)")
            print(f"    Avg EDHRec inclusion      : {avg_einc*100:5.1f}%  "
                  f"({n_edhrec_sc}/{n_nb} cards have data)")

        edhrec_lines = []
        if avg_esyn is not None:
            edhrec_lines = [
                f"// {'Avg EDHRec synergy':<30} {avg_esyn:+.4f}  ({n_edhrec_sc}/{n_nb} cards)",
                f"// {'Avg EDHRec inclusion':<30} {avg_einc*100:5.1f}%  ({n_edhrec_sc}/{n_nb} cards)",
            ]
        coverage_lines = [
            "// ── Collection Coverage ─────────────────────────────────────────",
            *[f"// {label:<30} {count:>3} cards  ({count / t * 100:4.1f}%)" for label, count, _ in rows],
            f"// {'Total from your collection':<30} {owned_total:>3} cards  ({owned_total / t * 100:4.1f}%)",
            f"// {'Avg community inclusivity':<30} {avg_inc*100:5.1f}%  ({n_freq_sc}/{n_nb} cards)",
            f"// {'Avg model score':<30} {avg_mod*100:5.1f}%  ({n_mod_sc}/{n_nb} cards)",
            *edhrec_lines,
            "//",
        ]

    print(f"\n{divider}")

    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(coverage_lines + output_lines))
    print(f"  Deck saved → '{output_file}'")
