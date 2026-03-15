import os
import re
import json
import numpy as np
import pandas as pd
from sklearn.preprocessing import MultiLabelBinarizer

ALL_SUBTYPES = [
    "Advisor", "Aetherborn", "Ally", "Angel", "Antelope", "Ape", "Archer", "Archon", "Army", "Artificer",
    "Assassin", "Assembly-Worker", "Astartes", "Atog", "Aurochs", "Avatar", "Azra", "Badger", "Balloon",
    "Barbarian", "Basilisk", "Bat", "Bear", "Beast", "Beeble", "Berserker", "Bird", "Blinkmoth", "Boar",
    "Bringer", "Brushwagg", "Camarid", "Camel", "Caribou", "Carrier", "Cat", "Centaur", "Cephalid", "Chimera",
    "Citizen", "Cleric", "Cockatrice", "Construct", "Coward", "Crab", "Crocodile", "Cyclops", "Dauthi",
    "Demon", "Deserter", "Devil", "Dinosaur", "Djinn", "Dragon", "Drake", "Dreadnought", "Drone", "Druid",
    "Dryad", "Dwarf", "Efreet", "Egg", "Elder", "Eldrazi", "Elemental", "Elephant", "Elf", "Elk", "Eye",
    "Faerie", "Ferret", "Fish", "Flagbearer", "Fox", "Frog", "Fungus", "Gargoyle", "Germ", "Giant", "Gnome",
    "Goat", "Goblin", "God", "Golem", "Gorgon", "Graveborn", "Gremlin", "Griffin", "Hag", "Harpy", "Hellion",
    "Hippo", "Hippogriff", "Homarid", "Homunculus", "Horror", "Horse", "Hound", "Human", "Hydra", "Hyena",
    "Illusion", "Imp", "Incarnation", "Insect", "Jackal", "Jellyfish", "Juggernaut", "Kavu", "Kirin",
    "Kithkin", "Knight", "Kobold", "Kor", "Kraken", "Lammasu", "Leech", "Leviathan", "Lhurgoyf", "Licid",
    "Lizard", "Manticore", "Masticore", "Mercenary", "Merfolk", "Metathran", "Minion", "Minotaur",
    "Mole", "Monger", "Mongoose", "Monk", "Monkey", "Moonfolk", "Mouse", "Mutant", "Myr", "Mystic",
    "Naga", "Narwhal", "Nautilus", "Nephilim", "Nightmare", "Nightstalker", "Ninja", "Noble", "Noggle",
    "Nomad", "Nymph", "Octopus", "Ogre", "Ooze", "Orb", "Orc", "Orgg", "Otter", "Ouphe", "Ox", "Oyster",
    "Pangolin", "Pegasus", "Pentavite", "Pest", "Phelddagrif", "Phoenix", "Phyrexian", "Pilot", "Pincher",
    "Pirate", "Plant", "Praetor", "Prism", "Processor", "Rabbit", "Raccoon", "Ranger", "Rat", "Rebel",
    "Reflection", "Rhino", "Rigger", "Rogue", "Sable", "Salamander", "Samurai", "Sand", "Saproling",
    "Satyr", "Scarecrow", "Scion", "Scorpion", "Scout", "Serf", "Serpent", "Servo", "Shade", "Shaman",
    "Shapeshifter", "Shark", "Sheep", "Siren", "Skeleton", "Slith", "Sliver", "Slug", "Snake", "Soldier",
    "Soltari", "Spawn", "Specter", "Spellshaper", "Sphinx", "Spider", "Spike", "Spirit", "Splinter",
    "Sponge", "Squid", "Squirrel", "Starfish", "Surrakar", "Survivor", "Tentacle", "Tetravite", "Thalakos",
    "Thopter", "Thrull", "Treefolk", "Trilobite", "Triskelavite", "Troll", "Turtle", "Unicorn", "Vampire",
    "Vedalken", "Viashino", "Volver", "Wall", "Warlock", "Warrior", "Weird", "Werewolf", "Whale", "Wizard",
    "Wolf", "Wolverine", "Wombat", "Worm", "Wraith", "Wurm", "Yeti", "Zombie", "Zubera",

    "Equipment", "Fortification", "Vehicle",

    "Aura", "Curse", "Rune", "Saga", "Shrine", "Class", "Cartouche", "Background", "Role", "Constellation",

    "Desert", "Forest", "Gate", "Island", "Lair", "Locus", "Mine", "Mountain", "Plains", "Power-Plant",
    "Swamp", "Tower", "Urza's", "Wastes", "Cave", "Sphere", "Cloud", "Crater", "Lair", "Lab", "Library",

    "Ajani", "Ashiok", "Basri", "Chandra", "Dack", "Daretti", "Domri", "Dovin", "Elspeth", "Estrid", "Freyalise",
    "Garruk", "Gideon", "Huatli", "Jace", "Jaya", "Karn", "Kasmina", "Kaya", "Kiora", "Liliana", "Lukka",
    "Nahiri", "Narset", "Nicol Bolas", "Nissa", "Ob Nixilis", "Ral", "Rowan", "Saheeli", "Samut", "Sarkhan",
    "Serra", "Sorin", "Tamiyo", "Teferi", "Tezzeret", "Tibalt", "Ugin", "Venser", "Vivien", "Vraska", "Will",
    "Yanggu", "Yanling",

    "Siege", "Arcane", "Trap"
]

def main(base_dir=None):
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")

    print("Loading card data…")
    with open(os.path.join(data_dir, 'all_cards.json'), 'r', encoding='utf-8') as f:
        cards = json.load(f)

    with open(os.path.join(data_dir, 'keyword_abilities.json'), 'r') as f:
        keywords_abilities = json.load(f)['data']

    with open(os.path.join(data_dir, 'keyword_actions.json'), 'r') as f:
        keywords_actions = json.load(f)['data']

    with open(os.path.join(data_dir, 'ability_words.json'), 'r') as f:
        ability_words = json.load(f)['data']

    all_keywords = keywords_abilities + keywords_actions + ability_words

    print(f"Building feature rows for {len(cards):,} cards…")
    rows = []
    colors = ['W', 'U', 'B', 'R', 'G']
    types_to_check = ['Creature', 'Artifact', 'Enchantment', 'Instant', 'Sorcery', 'Planeswalker', 'Land']

    for card in cards:
        card_data = {
            'name': card['name'],
            'cmc': card['cmc'],
            'rarity': card['rarity'],
            'commander_legal': card['legalities']['commander'] == 'legal',
            'legendary': 'Legendary' in card['type_line'],
        }

        for t in types_to_check:
            card_data[f'is_{t.lower()}'] = 1 if t in card['type_line'] else 0

        if card_data['is_creature']:
            card_data['power']     = int(card.get('power',     0)) if card.get('power',     '').isdigit() else 0
            card_data['toughness'] = int(card.get('toughness', 0)) if card.get('toughness', '').isdigit() else 0
        else:
            card_data['power']     = 0
            card_data['toughness'] = 0

        for color in colors:
            card_data[f'color_{color}']          = 1 if color in card.get('colors', [])          else 0
            card_data[f'color_identity_{color}'] = 1 if color in card.get('color_identity', []) else 0

        mana_cost_str = card.get('mana_cost', '')
        for color in colors:
            card_data[f'mana_pips_{color}'] = mana_cost_str.count(f'{{{color}}}')

        generic_mana = re.findall(r'\{(\d+)\}', mana_cost_str)
        card_data['mana_generic'] = int(generic_mana[0]) if generic_mana else 0

        subtypes = card['type_line'].split('—')[1].strip().split(' ') if '—' in card['type_line'] else []
        for subtype in ALL_SUBTYPES:
            card_data[f'subtype_{subtype}'] = 1 if subtype in subtypes else 0

        card_data['keywords'] = card.get('keywords', [])
        rows.append(card_data)

    print("Encoding keywords and writing CSV…")
    df = pd.DataFrame(rows)

    mlb = MultiLabelBinarizer(classes=all_keywords)
    keyword_matrix = mlb.fit_transform(df['keywords'])
    keyword_df = pd.DataFrame(keyword_matrix, columns=[f'keyword_{kw}' for kw in mlb.classes_])
    df = pd.concat([df.drop(columns=['keywords']), keyword_df], axis=1)

    rarity_mapping = {'common': 1, 'uncommon': 2, 'rare': 3, 'mythic rare': 4}
    df['rarity'] = df['rarity'].str.lower().map(rarity_mapping)
    df.rename(columns={'rarity': 'rarity_encoded'}, inplace=True)

    out_path = os.path.join(data_dir, 'mtg_cards_features.csv')
    df.to_csv(out_path, index=False)
    print(f"Done — {len(df):,} cards written to {out_path}")


if __name__ == "__main__":
    main()
