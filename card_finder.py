import json
import re
import sys
import os
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

TOP_N        = 10
WEIGHT_TEXT  = 0.40
WEIGHT_FEAT  = 0.40
WEIGHT_SYN   = 0.20

CARD_FILE    = os.path.join("data", "all_cards.json")
FEATURE_CSV  = os.path.join("data", "mtg_cards_features.csv")

def load_oracle_texts(path):
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    oracle = {}
    for card in raw:
        name = card.get('name')
        if not name:
            continue

        if 'card_faces' in card:
            parts = []
            for face in card['card_faces']:
                parts.append(face.get('type_line', ''))
                parts.append(face.get('oracle_text', ''))
            text = ' '.join(parts)
        else:
            text = f"{card.get('type_line', '')} {card.get('oracle_text', '')}"

        oracle[name] = text.strip()

    return oracle

def load_features(path):
    df = pd.read_csv(path)
    df.set_index('name', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    return df

def load_deck_sets(path):
    with open(path) as f:
        decks = json.load(f)['decks']
    return [set(d['cards']) for d in decks]

def build_synergy_vector(query_card, card_list, deck_sets):
    card_index = {c: i for i, c in enumerate(card_list)}
    n = len(card_list)
    counts = np.zeros(n, dtype=np.float32)
    deck_count = 0

    for deck in deck_sets:
        if query_card in deck:
            deck_count += 1
            for other in deck:
                if other in card_index and other != query_card:
                    counts[card_index[other]] += 1

    if deck_count == 0:
        return None

    counts /= deck_count
    max_val = counts.max()
    if max_val > 0:
        counts /= max_val

    return counts

class CardFinder:
    def __init__(self, feature_csv=FEATURE_CSV, card_json=CARD_FILE, deck_file=None):
        print("Loading card features...")
        self.card_df = load_features(feature_csv)

        if 'commander_legal' in self.card_df.columns:
            self.card_df = self.card_df[self.card_df['commander_legal'] == True]

        print("Loading oracle text...")
        self.oracle = load_oracle_texts(card_json)

        # only cards that exist in both
        common = [c for c in self.card_df.index if c in self.oracle]
        self.card_df = self.card_df.loc[common]
        self.card_list = list(self.card_df.index)
        self.card_index = {c: i for i, c in enumerate(self.card_list)}
        self.texts = [self.oracle[c] for c in self.card_list]

        print(f"  → {len(self.card_list):,} cards loaded.")

        print("Building TF-IDF matrix...")
        self.vectorizer = TfidfVectorizer(
            stop_words='english',
            ngram_range=(1, 2),
            max_features=8000,
            token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9'\-]{1,}\b"
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(self.texts)

        skip_cols = {'commander_legal', 'legendary', 'rarity_encoded'}
        num_cols = [
            c for c in self.card_df.columns
            if c not in skip_cols and self.card_df[c].dtype in [np.float64, np.int64, float, int]
        ]
        raw = self.card_df[num_cols].fillna(0).values.astype(np.float32)
        self.feat_matrix = normalize(raw, norm='l2')

        self.deck_sets = None
        if deck_file and os.path.exists(deck_file):
            print(f"Loading synergy data from {deck_file}...")
            self.deck_sets = load_deck_sets(deck_file)
            print(f"  → {len(self.deck_sets)} decks loaded.")
        else:
            print("  No deck file — synergy scoring disabled.")
    
    def find_similar(self, query, top_n=TOP_N, commander_legal_only=True):
        query_card = query if query in self.card_index else None

        # Text
        if query_card is not None:
            idx = self.card_index[query_card]
            query_tfidf = self.tfidf_matrix[idx]
        else:
            print(f"  '{query}' not found as a card name — using as text description.")
            query_tfidf = self.vectorizer.transform([query])

        text_sims = cosine_similarity(query_tfidf, self.tfidf_matrix).flatten()

        # Features
        if query_card is not None:
            idx = self.card_index[query_card]
            query_feat = self.feat_matrix[idx].reshape(1, -1)
            feat_sims = cosine_similarity(query_feat, self.feat_matrix).flatten()
        else:
            feat_sims = np.zeros(len(self.card_list), dtype=np.float32)

        # Synergy
        syn_vec = np.zeros(len(self.card_list), dtype=np.float32)
        w_text, w_feat, w_syn = WEIGHT_TEXT, WEIGHT_FEAT, WEIGHT_SYN

        if self.deck_sets and query_card:
            result = build_synergy_vector(query_card, self.card_list, self.deck_sets)
            if result is not None:
                syn_vec = result
            else:
                w_text += w_syn / 2
                w_feat += w_syn / 2
                w_syn = 0.0
        else:
            w_text += w_syn / 2
            w_feat += w_syn / 2
            w_syn = 0.0
            
        combined = w_text * text_sims + w_feat * feat_sims + w_syn * syn_vec

        sorted_indices = np.argsort(combined)[::-1]
        results = []
        for i in sorted_indices:
            name = self.card_list[i]
            if name == query_card:
                continue
            results.append({
                'name':        name,
                'score':       round(float(combined[i]), 4),
                'text_sim':    round(float(text_sims[i]), 4),
                'feat_sim':    round(float(feat_sims[i]), 4),
                'synergy':     round(float(syn_vec[i]), 4),
                'oracle_text': self.oracle.get(name, '')[:100],
            })
            if len(results) >= top_n:
                break

        return pd.DataFrame(results)
