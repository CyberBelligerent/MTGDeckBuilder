import argparse
import os
import re
import sys

FORGE_COMMANDER_DIR = os.path.expanduser("~/AppData/Roaming/Forge/decks/commander")

def parse_deck_txt(path: str) -> tuple[str, list[tuple[int, str]]]:
    commander = ""
    cards = []
    in_commander_section = False

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip()

            if line.startswith("//"):
                section = line.lstrip("/ ").strip()
                in_commander_section = (section == "Commander")
                continue

            if not line.strip():
                continue

            m = re.match(r'^(\d+)\s+(.+)$', line.strip())
            if not m:
                continue

            qty = int(m.group(1))
            name = m.group(2).strip()

            if in_commander_section:
                commander = name
            else:
                cards.append((qty, name))

    return commander, cards

def convert(txt_path: str, name: str = None, out_path: str = None) -> str:
    commander, cards = parse_deck_txt(txt_path)

    if not commander:
        sys.exit(f"Error: could not find '// Commander' section in {txt_path}")

    deck_name = name or os.path.splitext(os.path.basename(txt_path))[0]

    if out_path is None:
        os.makedirs(FORGE_COMMANDER_DIR, exist_ok=True)
        out_path = os.path.join(FORGE_COMMANDER_DIR, f"{deck_name}.dck")

    lines = [
        "[metadata]",
        f"Name={deck_name}",
        "[Main]",
    ]
    for qty, card in cards:
        lines.append(f"{qty} {card}")
    lines.append("[Commander]")
    lines.append(f"1 {commander}")
    lines.append("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    return out_path

def main():
    parser = argparse.ArgumentParser(
        description="Convert a MTGDeckBuilder .txt deck to Forge .dck format"
    )
    parser.add_argument("txt", help="Path to the built deck .txt file")
    parser.add_argument("--name", help="Custom deck name (default: filename stem)")
    parser.add_argument(
        "--out", help=f"Output .dck path (default: {FORGE_COMMANDER_DIR})"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.txt):
        sys.exit(f"Error: file not found — {args.txt}")

    out = convert(args.txt, name=args.name, out_path=args.out)
    print(f"Written: {out}")

if __name__ == "__main__":
    main()
