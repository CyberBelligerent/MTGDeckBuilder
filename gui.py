import io
import os
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk, scrolledtext

# (feature_col, display_label, default_fraction, optimizer_weight)
SLIDER_TYPES = [
    ("is_creature",     "Creatures",     0.30, 0.7),
    ("is_artifact",     "Artifacts",     0.10, 0.5),
    ("is_enchantment",  "Enchantments",  0.08, 0.5),
    ("is_instant",      "Instants",      0.08, 0.5),
    ("is_sorcery",      "Sorceries",     0.06, 0.5),
    ("is_planeswalker", "Planeswalkers", 0.02, 0.5),
]
DEFAULT_CMC    = 2.8
CMC_WEIGHT     = 0.5

# Role minimum defaults — must match ROLE_MINIMUMS in build_deck.py
# (role_key, display_label, default_min)
ROLE_MIN_DEFS = [
    ("ramp",        "Ramp",        10),
    ("draw",        "Draw",         8),
    ("removal",     "Removal",      6),
    ("interaction", "Interaction",  2),
    ("tutor",       "Tutor",        2),
]

# Redirects Standard out to my logger instead
class _StreamRedirect:
    """Forwards write() calls to a tkinter Text widget (thread-safe)."""

    def __init__(self, widget: tk.Text):
        self._widget = widget
        self._lock   = threading.Lock()

    def write(self, text: str):
        with self._lock:
            self._widget.after(0, self._append, text)

    def _append(self, text: str):
        self._widget.configure(state="normal")
        self._widget.insert(tk.END, text)
        self._widget.see(tk.END)
        self._widget.configure(state="disabled")

    def flush(self):
        pass

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


# Main window
class DeckBuilderApp(tk.Tk):
    if getattr(sys, 'frozen', False):
        BASE_DIR = os.path.dirname(sys.executable)
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    def __init__(self):
        super().__init__()
        self.title("MTG Commander Deck Builder - v1.3 - Combo Finder")
        self.resizable(True, True)
        self.minsize(820, 680)

        self._build_thread: threading.Thread | None = None
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

        # Per-slider state: {col: {"target": DoubleVar, "comm_label": Label}}
        self._slider_state: dict[str, dict] = {}
        self._cmc_var       = tk.DoubleVar(value=DEFAULT_CMC)

        self._all_card_names: list[str] = []
        self._legendary_creature_names: list[str] = []
        self._commander_name_set: dict[str, str] = {}
        self._dfc_front_face_map: dict[str, str] = {}

        # Mana base nonbasic counts (mirrors _DEFAULT_NONBASIC_COUNTS in build_deck.py)
        self._mana_utility_var = tk.IntVar(value=4)
        self._mana_fetch_var   = tk.IntVar(value=4)
        self._mana_fixing_var  = tk.IntVar(value=8)

        self._build_ui()
        self._build_menu()
        self._load_card_names_background()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        cfg = ttk.LabelFrame(self, text="Configuration", padding=10)
        cfg.pack(fill=tk.X, padx=10, pady=(10, 4))
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="Commander:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self._commander_var = tk.StringVar()
        self._commander_var.trace_add("write", self._on_commander_changed)
        self._commander_entry = ttk.Entry(cfg, textvariable=self._commander_var, width=36)
        self._commander_entry.grid(row=0, column=1, sticky=tk.EW, pady=3)

        ttk.Label(cfg, text="Owned cards:").grid(row=1, column=0, sticky=tk.W, padx=(0, 6))
        self._owned_var = tk.StringVar(value=os.path.join(self.BASE_DIR, "owned_cards.txt"))
        ttk.Entry(cfg, textvariable=self._owned_var).grid(
            row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Button(cfg, text="Browse…", command=self._browse_owned).grid(
            row=1, column=2, padx=(6, 0))

        ttk.Label(cfg, text="Output file:").grid(row=2, column=0, sticky=tk.W, padx=(0, 6))
        self._output_var = tk.StringVar(value="")
        ttk.Entry(cfg, textvariable=self._output_var).grid(
            row=2, column=1, sticky=tk.EW, pady=3)
        ttk.Button(cfg, text="Browse…", command=self._browse_output).grid(
            row=2, column=2, padx=(6, 0))

        opts = ttk.Frame(cfg)
        opts.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        ttk.Label(opts, text="# decks to scrape:").pack(side=tk.LEFT)
        self._n_decks_var = tk.IntVar(value=100)
        ttk.Spinbox(opts, from_=10, to=500, increment=10,
                    textvariable=self._n_decks_var, width=6).pack(side=tk.LEFT, padx=(4, 16))
        self._rescrape_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Force re-scrape",
                        variable=self._rescrape_var).pack(side=tk.LEFT)

        # Model Row
        model_row = ttk.Frame(cfg)
        model_row.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(10, 2))
        self._create_model_btn = ttk.Button(model_row, text="⚙  Create Model",
                                            command=self._start_create_model)
        self._create_model_btn.pack(side=tk.LEFT)
        self._model_status_lbl = ttk.Label(model_row, text="", foreground="gray")
        self._model_status_lbl.pack(side=tk.LEFT, padx=(10, 0))

        # Targets frame
        tgt = ttk.LabelFrame(self, text="Deck Targets", padding=10)
        tgt.pack(fill=tk.X, padx=10, pady=4)
        tgt.columnconfigure(1, weight=1)

        # Column headers
        ttk.Label(tgt, text="Card type",  font=("", 9, "bold")).grid(
            row=0, column=0, sticky=tk.W)
        ttk.Label(tgt, text="Target %",   font=("", 9, "bold")).grid(
            row=0, column=1, sticky=tk.W, padx=(8, 0))
        ttk.Label(tgt, text="Value",      font=("", 9, "bold")).grid(
            row=0, column=2, padx=(4, 0))
        ttk.Label(tgt, text="Community avg", font=("", 9, "bold")).grid(
            row=0, column=3, padx=(12, 0))

        for i, (col, label, default, _weight) in enumerate(SLIDER_TYPES, start=1):
            var = tk.DoubleVar(value=round(default * 100, 1))
            self._slider_state[col] = {"target": var, "comm_label": None}

            ttk.Label(tgt, text=f"{label}:").grid(
                row=i, column=0, sticky=tk.W, pady=2)

            slider = ttk.Scale(tgt, from_=0, to=100, orient=tk.HORIZONTAL,
                               variable=var, command=lambda _v, v=var, c=col: self._on_slider(v, c))
            slider.grid(row=i, column=1, sticky=tk.EW, padx=(8, 4), pady=2)

            val_lbl = ttk.Label(tgt, text=f"{default*100:.0f}%", width=5, anchor=tk.E)
            val_lbl.grid(row=i, column=2, pady=2)
            self._slider_state[col]["val_label"] = val_lbl

            comm_lbl = ttk.Label(tgt, text="—", foreground="gray", width=14)
            comm_lbl.grid(row=i, column=3, padx=(12, 0), pady=2, sticky=tk.W)
            self._slider_state[col]["comm_label"] = comm_lbl

        # CMC row
        cmc_row = len(SLIDER_TYPES) + 1
        ttk.Label(tgt, text="Avg CMC:").grid(row=cmc_row, column=0, sticky=tk.W, pady=(6, 2))
        cmc_inner = ttk.Frame(tgt)
        cmc_inner.grid(row=cmc_row, column=1, sticky=tk.W, padx=(8, 4), pady=(6, 2))
        ttk.Spinbox(cmc_inner, from_=0.5, to=10.0, increment=0.1,
                    textvariable=self._cmc_var, width=6,
                    format="%.1f").pack(side=tk.LEFT)

        self._cmc_val_lbl = ttk.Label(tgt, text="", width=5)
        self._cmc_val_lbl.grid(row=cmc_row, column=2, pady=(6, 2))

        self._cmc_comm_lbl = ttk.Label(tgt, text="—", foreground="gray", width=14)
        self._cmc_comm_lbl.grid(row=cmc_row, column=3, padx=(12, 0), pady=(6, 2), sticky=tk.W)

        # Lands row
        lands_row = cmc_row + 1
        ttk.Label(tgt, text="Lands:").grid(row=lands_row, column=0, sticky=tk.W, pady=(2, 2))
        self._n_lands_var = tk.IntVar(value=36)
        self._n_lands_var.trace_add("write", lambda *_: self._on_mana_changed()
                                    if hasattr(self, '_mana_basics_lbl') else None)
        ttk.Spinbox(tgt, from_=0, to=99, increment=1,
                    textvariable=self._n_lands_var, width=6).grid(
            row=lands_row, column=1, sticky=tk.W, padx=(8, 4), pady=(2, 2))

        # Load community averages button
        avg_row = lands_row + 1
        self._avg_btn = ttk.Button(tgt, text="Load community averages from cache",
                                   command=self._load_community_averages)
        self._avg_btn.grid(row=avg_row, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        self._avg_status_lbl = ttk.Label(tgt, text="", foreground="gray")
        self._avg_status_lbl.grid(row=avg_row, column=2, columnspan=2,
                                   sticky=tk.W, padx=(8, 0), pady=(8, 0))

        # Mana base row
        mana_outer = ttk.Frame(self)
        mana_outer.pack(fill=tk.X, padx=10, pady=(0, 2))

        self._mana_expanded = False
        self._mana_toggle_btn = ttk.Button(
            mana_outer, text="▶ Mana Base Settings",
            command=self._toggle_mana,
        )
        self._mana_toggle_btn.pack(fill=tk.X)

        self._mana_inner = ttk.Frame(mana_outer, padding=(10, 6, 10, 6),
                                     relief="groove", borderwidth=1)

        self._mana_inner.columnconfigure(1, weight=0)

        # Header row
        ttk.Label(self._mana_inner, text="Nonbasic Allocation",
                  font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 4))
        ttk.Label(self._mana_inner,
                  text="Slots reserved for each land category (remainder = basics)",
                  foreground="gray").grid(
            row=0, column=4, columnspan=3, sticky=tk.W, padx=(12, 0), pady=(0, 4))

        # Category rows
        _mana_rows = [
            ("Fixing Duals:",  self._mana_fixing_var,  "Lands producing 2+ of your commander's colors"),
            ("Fetch Lands:",   self._mana_fetch_var,   "Search / tutor lands"),
            ("Utility Lands:", self._mana_utility_var, "Commander-synergy lands"),
        ]
        for i, (label, var, tip) in enumerate(_mana_rows, start=1):
            ttk.Label(self._mana_inner, text=label).grid(
                row=i, column=0, sticky=tk.W, pady=2)
            ttk.Spinbox(self._mana_inner, from_=0, to=30, increment=1,
                        textvariable=var, width=5,
                        command=self._on_mana_changed).grid(
                row=i, column=1, sticky=tk.W, padx=(8, 0), pady=2)
            ttk.Label(self._mana_inner, text=tip, foreground="gray").grid(
                row=i, column=2, sticky=tk.W, padx=(12, 0), pady=2)

        # Derived basics row
        ttk.Label(self._mana_inner, text="Basics (derived):").grid(
            row=4, column=0, sticky=tk.W, pady=(6, 2))
        self._mana_basics_lbl = ttk.Label(self._mana_inner, text="20", foreground="gray")
        self._mana_basics_lbl.grid(row=4, column=1, sticky=tk.W, padx=(8, 0), pady=(6, 2))

        # Warning row
        self._mana_warn_lbl = ttk.Label(self._mana_inner, text="", foreground="orange")
        self._mana_warn_lbl.grid(row=5, column=0, columnspan=5, sticky=tk.W, pady=(2, 0))

        # Bind spinbox <Return>/<FocusOut> changes (command= handles button clicks)
        for var in (self._mana_fixing_var, self._mana_fetch_var, self._mana_utility_var):
            var.trace_add("write", lambda *_: self._on_mana_changed())

        self._on_mana_changed()  # initialise derived label

        # Advanced deck config
        adv_outer = ttk.Frame(self)
        adv_outer.pack(fill=tk.X, padx=10, pady=(0, 2))

        self._adv_expanded = False
        self._adv_toggle_btn = ttk.Button(
            adv_outer, text="▶ Advanced Deck Configuration",
            command=self._toggle_advanced,
        )
        self._adv_toggle_btn.pack(fill=tk.X)

        self._adv_inner = ttk.Frame(adv_outer, padding=(10, 6, 10, 6),
                                    relief="groove", borderwidth=1)

        self._adv_inner.columnconfigure(1, weight=0)
        self._adv_inner.columnconfigure(3, weight=0)

        # Strategy row
        ttk.Label(self._adv_inner, text="Strategy:").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 6))
        self._strategy_var = tk.StringVar(value="Default")
        ttk.Combobox(
            self._adv_inner, textvariable=self._strategy_var,
            values=["Default", "Combo-Aware"],
            state="readonly", width=14,
        ).grid(row=0, column=1, sticky=tk.W, padx=(8, 4), pady=(0, 6))
        ttk.Label(self._adv_inner,
                  text="Combo-Aware boosts cards completing owned combos",
                  foreground="gray").grid(
            row=0, column=2, columnspan=4, sticky=tk.W, padx=(8, 0), pady=(0, 6))

        # Role minimums header
        ttk.Label(self._adv_inner, text="Role minimums:",
                  font=("", 9, "bold")).grid(
            row=1, column=0, sticky=tk.W, pady=(0, 4))
        ttk.Label(self._adv_inner,
                  text="Minimum card count per role Phase B tries to fill",
                  foreground="gray").grid(
            row=1, column=1, columnspan=5, sticky=tk.W, padx=(8, 0), pady=(0, 4))

        # One spinbox per role, laid out in a single row
        self._role_min_vars = {}
        role_row = ttk.Frame(self._adv_inner)
        role_row.grid(row=2, column=0, columnspan=6, sticky=tk.W)
        for i, (role, label, default) in enumerate(ROLE_MIN_DEFS):
            ttk.Label(role_row, text=f"{label}:").grid(
                row=0, column=i * 2, sticky=tk.W, padx=(0 if i == 0 else 16, 0))
            var = tk.IntVar(value=default)
            self._role_min_vars[role] = var
            ttk.Spinbox(role_row, from_=0, to=30, increment=1,
                        textvariable=var, width=4).grid(
                row=0, column=i * 2 + 1, sticky=tk.W, padx=(4, 0))

        # Action buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=4)

        self._build_btn = ttk.Button(btn_frame, text="▶  Build Deck",
                                     command=self._start_build)
        self._build_btn.pack(side=tk.LEFT)
        
        self._load_btn = ttk.Button(btn_frame, text="Load Deck", command=self._load_deck)
        self._load_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._cancel_btn = ttk.Button(btn_frame, text="■  Cancel",
                                      command=self._cancel_build, state=tk.DISABLED)
        self._cancel_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._clear_btn = ttk.Button(btn_frame, text="Clear log",
                                     command=self._clear_log)
        self._clear_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._reset_btn = ttk.Button(btn_frame, text="Reset targets",
                                     command=self._reset_targets)
        self._reset_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._update_btn = ttk.Button(btn_frame, text="⬇  Update Card Data",
                                      command=self._start_update_card_data)
        self._update_btn.pack(side=tk.LEFT, padx=(24, 0))

        self._status_var = tk.StringVar(value="Ready.")
        ttk.Label(btn_frame, textvariable=self._status_var,
                  foreground="gray").pack(side=tk.LEFT, padx=12)

        # Output area
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Log
        log_frame = ttk.Frame(nb)
        nb.add(log_frame, text="Log")
        self._log = scrolledtext.ScrolledText(
            log_frame, state="disabled", font=("Consolas", 9),
            background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="white", wrap=tk.WORD)
        self._log.pack(fill=tk.BOTH, expand=True)

        # Deck Output
        deck_frame = ttk.Frame(nb)
        nb.add(deck_frame, text="Deck Output")
        deck_toolbar = ttk.Frame(deck_frame)
        deck_toolbar.pack(fill=tk.X, pady=(4, 0), padx=4)
        ttk.Button(deck_toolbar, text="Copy to clipboard",
                   command=self._copy_deck).pack(side=tk.LEFT)
        ttk.Button(deck_toolbar, text="Save as…",
                   command=self._save_deck).pack(side=tk.LEFT, padx=(6, 0))

        self._coverage_lbl = ttk.Label(deck_frame, text="", foreground="gray",
                                       font=("Consolas", 9), anchor=tk.W)
        self._coverage_lbl.pack(fill=tk.X, padx=6, pady=(4, 0))

        self._deck_text = scrolledtext.ScrolledText(
            deck_frame, font=("Consolas", 9),
            background="#fafafa", foreground="#1a1a1a", wrap=tk.NONE)
        self._deck_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Upgrade Suggestions
        upg_frame = ttk.Frame(nb)
        nb.add(upg_frame, text="Upgrade Suggestions")
        self._build_upgrades_tab(upg_frame)

        # Combos
        combos_frame = ttk.Frame(nb)
        nb.add(combos_frame, text="Combos")
        self._build_combos_tab(combos_frame)

        self._nb = nb
        self._wire_commander_autocomplete()

    # Upgrades tabs
    def _build_upgrades_tab(self, parent: ttk.Frame):
        # Toolbar
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 0))

        self._upg_btn = ttk.Button(toolbar, text="Get upgrade suggestions",
                                   command=self._run_upgrades)
        self._upg_btn.pack(side=tk.LEFT)

        ttk.Label(toolbar, text="  Show top:").pack(side=tk.LEFT)
        self._upg_n_var = tk.IntVar(value=60)
        ttk.Spinbox(toolbar, from_=10, to=200, increment=10,
                    textvariable=self._upg_n_var, width=5).pack(side=tk.LEFT, padx=(2, 12))

        ttk.Label(toolbar, text="Filter type:").pack(side=tk.LEFT)
        self._upg_type_var = tk.StringVar(value="All")
        self._upg_type_combo = ttk.Combobox(
            toolbar, textvariable=self._upg_type_var, state="readonly", width=14,
            values=["All", "Creature", "Instant", "Sorcery",
                    "Artifact", "Enchantment", "Planeswalker", "Land", "Other"])
        self._upg_type_combo.pack(side=tk.LEFT, padx=(2, 0))
        self._upg_type_combo.bind("<<ComboboxSelected>>", lambda _: self._apply_upg_filter())

        self._upg_status = ttk.Label(toolbar, text="", foreground="gray")
        self._upg_status.pack(side=tk.LEFT, padx=12)

        # Description
        desc = (
            "Cards you don't own that community decks for this commander include most often.\n"
            "Score = % of scraped decks that contain the card.  Requires a completed build."
        )
        ttk.Label(parent, text=desc, foreground="gray",
                  justify=tk.LEFT).pack(anchor=tk.W, padx=8, pady=(4, 2))

        # Treeview
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))

        cols = ("rank", "name", "type", "cmc", "score")
        self._upg_tree = ttk.Treeview(tree_frame, columns=cols,
                                       show="headings", selectmode="browse")

        self._upg_tree.heading("rank",  text="#",     anchor=tk.E)
        self._upg_tree.heading("name",  text="Card",  anchor=tk.W)
        self._upg_tree.heading("type",  text="Type",  anchor=tk.W)
        self._upg_tree.heading("cmc",   text="CMC",   anchor=tk.E)
        self._upg_tree.heading("score", text="Score", anchor=tk.E)

        self._upg_tree.column("rank",  width=40,  stretch=False, anchor=tk.E)
        self._upg_tree.column("name",  width=280, stretch=True,  anchor=tk.W)
        self._upg_tree.column("type",  width=110, stretch=False, anchor=tk.W)
        self._upg_tree.column("cmc",   width=50,  stretch=False, anchor=tk.E)
        self._upg_tree.column("score", width=70,  stretch=False, anchor=tk.E)

        # Alternating row colours
        self._upg_tree.tag_configure("odd",  background="#f5f5f5")
        self._upg_tree.tag_configure("even", background="#ffffff")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                             command=self._upg_tree.yview)
        self._upg_tree.configure(yscrollcommand=vsb.set)
        self._upg_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Store full results for filtering
        self._upg_all_rows: list[dict] = []

    def _run_upgrades(self):
        commander = self._commander_var.get().strip()
        if not commander:
            self._upg_status.configure(text="Enter a commander name first.")
            return

        self._upg_btn.configure(state=tk.DISABLED)
        self._upg_status.configure(text="Scanning…")
        self._upg_tree.delete(*self._upg_tree.get_children())

        n = self._upg_n_var.get()

        # Parse the current built deck from the output tab so already-included
        # cards are excluded from suggestions.
        import re as _re
        raw          = self._deck_text.get("1.0", tk.END)
        current_deck = []
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                m = _re.match(r'^\d+x?\s+(.+)$', line)
                current_deck.append(m.group(1) if m else line)

        def _worker():
            try:
                from build_deck import get_upgrade_suggestions, FEATURE_CSV, CARD_FILE
                rows = get_upgrade_suggestions(
                    commander_name=commander,
                    owned_path=self._owned_var.get(),
                    current_deck=current_deck,
                    feature_csv=FEATURE_CSV,
                    cards_json=CARD_FILE,
                    n=n,
                )
                self.after(0, self._populate_upgrades, rows)
            except Exception as e:
                import traceback
                self.after(0, self._upg_done, f"Error: {e}")
                print(traceback.format_exc())

        threading.Thread(target=_worker, daemon=True).start()

    def _populate_upgrades(self, rows: list[dict]):
        self._upg_all_rows = rows
        self._apply_upg_filter()
        self._upg_done(f"{len(rows)} suggestions loaded.")

    def _apply_upg_filter(self):
        self._upg_tree.delete(*self._upg_tree.get_children())
        type_filter = self._upg_type_var.get()
        rank = 0
        for row in self._upg_all_rows:
            if type_filter != "All" and row["type"] != type_filter:
                continue
            rank += 1
            tag = "odd" if rank % 2 else "even"
            self._upg_tree.insert("", tk.END, values=(
                rank,
                row["name"],
                row["type"],
                f"{row['cmc']:.0f}",
                f"{row['score']*100:.1f}%",
            ), tags=(tag,))

    def _upg_done(self, message: str):
        self._upg_btn.configure(state=tk.NORMAL)
        self._upg_status.configure(text=message)

    # Combos tab
    def _build_combos_tab(self, parent: ttk.Frame):
        # Toolbar
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 0))

        self._combo_btn = ttk.Button(toolbar, text="Find Combos",
                                     command=self._run_combo_finder)
        self._combo_btn.pack(side=tk.LEFT)

        self._combo_status = ttk.Label(toolbar, text="", foreground="gray")
        self._combo_status.pack(side=tk.LEFT, padx=12)

        desc = (
            "Combos present in your built deck, powered by Commander Spellbook.\n"
            "Double-click a row to open the combo page in your browser."
        )
        ttk.Label(parent, text=desc, foreground="gray",
                  justify=tk.LEFT).pack(anchor=tk.W, padx=8, pady=(4, 2))

        # Treeview
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))

        cols = ("rank", "cards", "produces", "link")
        self._combo_tree = ttk.Treeview(tree_frame, columns=cols,
                                        show="headings", selectmode="browse")

        self._combo_tree.heading("rank",     text="#",        anchor=tk.E)
        self._combo_tree.heading("cards",    text="Cards",    anchor=tk.W)
        self._combo_tree.heading("produces", text="Produces", anchor=tk.W)
        self._combo_tree.heading("link",     text="Link",     anchor=tk.W)

        self._combo_tree.column("rank",     width=40,  stretch=False, anchor=tk.E)
        self._combo_tree.column("cards",    width=340, stretch=True,  anchor=tk.W)
        self._combo_tree.column("produces", width=220, stretch=True,  anchor=tk.W)
        self._combo_tree.column("link",     width=300, stretch=False, anchor=tk.W)

        self._combo_tree.tag_configure("odd",  background="#f5f5f5")
        self._combo_tree.tag_configure("even", background="#ffffff")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                             command=self._combo_tree.yview)
        self._combo_tree.configure(yscrollcommand=vsb.set)
        self._combo_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._combo_tree.bind("<Double-1>", self._on_combo_double_click)
        self._combo_rows: list[dict] = []

    def _run_combo_finder(self):
        import re as _re
        raw = self._deck_text.get("1.0", tk.END)
        deck_cards = []
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                m = _re.match(r'^\d+x?\s+(.+)$', line)
                deck_cards.append(m.group(1) if m else line)

        if not deck_cards:
            self._combo_status.configure(text="Build a deck first.")
            return

        commander = self._commander_var.get().strip()
        if not commander:
            self._combo_status.configure(text="Enter a commander name first.")
            return

        self._combo_btn.configure(state=tk.DISABLED)
        self._combo_status.configure(text="Querying Commander Spellbook…")
        self._combo_tree.delete(*self._combo_tree.get_children())

        def _worker():
            try:
                from build_deck import find_deck_combos
                results = find_deck_combos(deck_cards, commander)
                self.after(0, self._populate_combos, results)
            except Exception as e:
                import traceback
                self.after(0, self._combo_done, f"Error: {e}")
                print(traceback.format_exc())

        threading.Thread(target=_worker, daemon=True).start()

    def _populate_combos(self, results: list[dict]):
        self._combo_rows = results
        self._combo_tree.delete(*self._combo_tree.get_children())
        for i, combo in enumerate(results, start=1):
            tag = "odd" if i % 2 else "even"
            self._combo_tree.insert("", tk.END, iid=str(i - 1), values=(
                i,
                ", ".join(combo["cards"]),
                ", ".join(combo["produces"]),
                combo["url"],
            ), tags=(tag,))
        n = len(results)
        self._combo_done(f"{n} combo{'s' if n != 1 else ''} found")

    def _combo_done(self, message: str):
        self._combo_btn.configure(state=tk.NORMAL)
        self._combo_status.configure(text=message)

    def _on_combo_double_click(self, _event):
        sel = self._combo_tree.selection()
        if not sel:
            return
        iid = int(sel[0])
        if 0 <= iid < len(self._combo_rows):
            webbrowser.open(self._combo_rows[iid]["url"])

    # Commander field watcher
    def _on_commander_changed(self, *_):
        """Auto-fill the output path and refresh model status when commander changes."""
        from build_deck import commander_to_slug, BUILT_DECKS_DIR
        name = self._commander_var.get().strip()
        if name:
            slug = commander_to_slug(name)
            path = os.path.join(self.BASE_DIR, BUILT_DECKS_DIR, f"{slug}_build.txt")
        else:
            path = ""
        self._output_var.set(path)
        self._refresh_model_status()

    def _refresh_model_status(self):
        """Update the model status label based on whether the joblib file exists."""
        from build_deck import commander_to_slug, MODELS_DIR, COMMUNITY_DECKS_DIR
        name = self._commander_var.get().strip()
        if not name:
            self._model_status_lbl.configure(text="", foreground="gray")
            return
        slug       = commander_to_slug(name)
        model_path = os.path.join(self.BASE_DIR, MODELS_DIR, f"{slug}_model.joblib")
        decks_path = os.path.join(self.BASE_DIR, COMMUNITY_DECKS_DIR, f"{slug}_decks.json")
        if os.path.exists(model_path) and os.path.exists(decks_path):
            self._model_status_lbl.configure(text="✓ Model ready", foreground="green")
        else:
            missing = []
            if not os.path.exists(decks_path):
                missing.append("community decks")
            if not os.path.exists(model_path):
                missing.append("trained model")
            self._model_status_lbl.configure(
                text=f"✗ Missing: {', '.join(missing)}", foreground="red"
            )

    # Slide stuff
    def _on_slider(self, var: tk.DoubleVar, col: str):
        pct = round(var.get(), 1)
        var.set(pct)
        lbl = self._slider_state[col].get("val_label")
        if lbl:
            lbl.configure(text=f"{pct:.0f}%")

    def _toggle_mana(self):
        if self._mana_expanded:
            self._mana_inner.pack_forget()
            self._mana_toggle_btn.configure(text="▶ Mana Base Settings")
        else:
            self._mana_inner.pack(fill=tk.X, pady=(2, 0))
            self._mana_toggle_btn.configure(text="▼ Mana Base Settings")
        self._mana_expanded = not self._mana_expanded

    def _on_mana_changed(self):
        """Update derived basics label and warn if nonbasics exceed land count."""
        try:
            total_nb = (self._mana_fixing_var.get()
                        + self._mana_fetch_var.get()
                        + self._mana_utility_var.get())
        except tk.TclError:
            return
        n_lands  = self._n_lands_var.get()
        basics   = max(0, n_lands - total_nb)
        self._mana_basics_lbl.configure(text=str(basics))
        if total_nb > n_lands:
            self._mana_warn_lbl.configure(
                text=f"⚠ Nonbasic total ({total_nb}) exceeds land count ({n_lands}). "
                     f"Running 0 basics — valid for cEDH but unusual.")
        else:
            self._mana_warn_lbl.configure(text="")

    def _build_nonbasic_counts(self) -> dict:
        return {
            'utility': self._mana_utility_var.get(),
            'fetch':   self._mana_fetch_var.get(),
            'fixing':  self._mana_fixing_var.get(),
        }

    def _toggle_advanced(self):
        if self._adv_expanded:
            self._adv_inner.pack_forget()
            self._adv_toggle_btn.configure(text="▶ Advanced Deck Configuration")
        else:
            self._adv_inner.pack(fill=tk.X, pady=(2, 0))
            self._adv_toggle_btn.configure(text="▼ Advanced Deck Configuration")
        self._adv_expanded = not self._adv_expanded

    def _reset_targets(self):
        for col, label, default, _w in SLIDER_TYPES:
            self._slider_state[col]["target"].set(round(default * 100, 1))
            self._slider_state[col]["val_label"].configure(text=f"{default*100:.0f}%")
        self._cmc_var.set(DEFAULT_CMC)
        for role, _label, default in ROLE_MIN_DEFS:
            self._role_min_vars[role].set(default)

    def _build_targets_dict(self) -> dict:
        targets = {}
        for col, _label, _default, weight in SLIDER_TYPES:
            frac = self._slider_state[col]["target"].get() / 100.0
            if frac > 0:
                targets[col] = (frac, weight)
        targets["avg_cmc"] = (self._cmc_var.get(), CMC_WEIGHT)
        return targets

    def _build_role_minimums_dict(self) -> dict:
        return {role: var.get() for role, var in self._role_min_vars.items()}

    # Community averages
    def _load_community_averages(self):
        commander = self._commander_var.get().strip()
        if not commander:
            self._avg_status_lbl.configure(text="Enter a commander name first.")
            return

        from build_deck import commander_to_slug, COMMUNITY_DECKS_DIR
        slug      = commander_to_slug(commander)
        deck_json = os.path.join(self.BASE_DIR, COMMUNITY_DECKS_DIR, f"{slug}_decks.json")

        if not os.path.exists(deck_json):
            self._avg_status_lbl.configure(
                text=f"No cache found (community_decks/{slug}_decks.json). Build a deck first.")
            return

        self._avg_status_lbl.configure(text="Loading…")
        self._avg_btn.configure(state=tk.DISABLED)

        def _worker():
            from build_deck import compute_community_averages, FEATURE_CSV
            avgs = compute_community_averages(deck_json, FEATURE_CSV)
            self.after(0, self._apply_community_averages, avgs)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_community_averages(self, avgs: dict):
        self._avg_btn.configure(state=tk.NORMAL)
        if not avgs:
            self._avg_status_lbl.configure(text="Could not compute averages.")
            return

        n = int(avgs.get("n_decks", 0))
        self._avg_status_lbl.configure(
            text=f"Loaded from {n} decks.")

        for col, _label, _default, _w in SLIDER_TYPES:
            if col in avgs:
                pct = avgs[col] * 100
                lbl = self._slider_state[col]["comm_label"]
                lbl.configure(text=f"avg {pct:.1f}%")

        if "avg_cmc" in avgs:
            self._cmc_comm_lbl.configure(text=f"avg {avgs['avg_cmc']:.2f}")

    # File browser for selecting cards
    def _browse_owned(self):
        path = filedialog.askopenfilename(
            title="Select owned cards file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=self.BASE_DIR)
        if path:
            self._owned_var.set(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save deck output as",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=self.BASE_DIR)
        if path:
            self._output_var.set(path)

    def _validate_and_resolve_commander(self, name: str) -> tuple:
        if not self._commander_name_set:
            return (name, None)  # data not loaded yet, skip validation
        name_lower = name.lower()
        if name_lower in self._commander_name_set:
            return (self._commander_name_set[name_lower], None)
        if name_lower in self._dfc_front_face_map:
            return (self._dfc_front_face_map[name_lower], None)
        
        # New last result check for ensuring you don't build on a false commander
        return (None, f"'{name}' is not a recognized legendary commander. Check spelling or use autocomplete.")

    # Actually build the damn deck
    def _start_build(self):
        commander = self._commander_var.get().strip()
        if not commander:
            self._status_var.set("Error: enter a commander name.")
            return

        # New resolved error start if trying to build a deck for a false commander
        resolved, err = self._validate_and_resolve_commander(commander)
        if err:
            self._status_var.set(f"Error: {err}")
            return
        if resolved and resolved != commander:
            self._commander_var.set(resolved)
            commander = resolved

        self._build_btn.configure(state=tk.DISABLED)
        self._cancel_btn.configure(state=tk.NORMAL)
        self._status_var.set("Running…")
        self._nb.select(0)

        redirect = _StreamRedirect(self._log)
        sys.stdout = redirect
        sys.stderr = redirect

        targets          = self._build_targets_dict()
        strategy         = "combo" if self._strategy_var.get() == "Combo-Aware" else "default"
        role_minimums    = self._build_role_minimums_dict()
        nonbasic_counts  = self._build_nonbasic_counts()

        self._build_thread = threading.Thread(
            target=self._run_pipeline,
            args=(targets, strategy, role_minimums, nonbasic_counts),
            daemon=True)
        self._build_thread.start()

    def _load_deck(self):
        import re as _re
        path = filedialog.askopenfilename(
            title="Load built deck",
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            initialdir=os.path.join(self.BASE_DIR, 'built_decks'),
        )
        
        if not path:
            return
        with open(path, encoding='utf-8') as fh:
            content = fh.read()
            
        lines = content.splitlines()
        commander = ""
        for i, line in enumerate(lines):
            if line.strip() == "// Commander":
                for j in range(i + 1, len(lines)):
                    m = _re.match(r'^\d+x?\s+(.+)$', lines[j].strip())
                    if m:
                        commander = m.group(1)
                        break
                break
        
        self._populate_deck_tab(content)
        if commander:
            self._commander_var.set(commander)
        
        self._nb.select(1)
        self._status_var.set(f"Loaded: {os.path.basename(path)}")

    def _run_pipeline(self, targets: dict, strategy: str = "default",
                      role_minimums: dict = None, nonbasic_counts: dict = None):
        try:
            from build_deck import (
                build_deck, load_card_features, print_deck_report,
                CARD_FILE, FEATURE_CSV, BUILT_DECKS_DIR, commander_to_slug,
                resolve_commander_name,
            )
            commander    = resolve_commander_name(
                self._commander_var.get().strip(), CARD_FILE)
            self.after(0, lambda n=commander: self._commander_var.set(n))
            deck, stats = build_deck(
                commander_name=commander,
                owned_path=self._owned_var.get(),
                n_lands=self._n_lands_var.get(),
                targets=targets,
                strategy=strategy,
                role_minimums=role_minimums,
                nonbasic_counts=nonbasic_counts,
            )
            card_df      = load_card_features(FEATURE_CSV)
            output_path  = self._output_var.get().strip()
            if not output_path:
                slug        = commander_to_slug(commander)
                output_path = os.path.join(self.BASE_DIR, BUILT_DECKS_DIR, f"{slug}_build.txt")
            print_deck_report(commander, deck, card_df, output_path, stats)

            if os.path.exists(output_path):
                with open(output_path, encoding="utf-8") as fh:
                    deck_text = fh.read()
                self.after(0, self._populate_deck_tab, deck_text)

            self.after(0, self._on_build_done, True, "Done!", deck, stats)
        except SystemExit as e:
            self.after(0, self._on_build_done, False, f"Exited: {e}", [], None)
        except Exception as e:
            import traceback
            print(f"\nERROR: {e}\n{traceback.format_exc()}")
            self.after(0, self._on_build_done, False, f"Error: {e}", [], None)

    def _on_build_done(self, success: bool, message: str, deck: list, stats: dict):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._build_btn.configure(state=tk.NORMAL)
        self._cancel_btn.configure(state=tk.DISABLED)
        self._status_var.set(message)
        if success:
            self._populate_coverage_label(stats)
            self._nb.select(1)
            # Kick off upgrade suggestions automatically in the background
            self.after(100, self._run_upgrades)

    def _populate_coverage_label(self, stats: dict):
        if not stats:
            self._coverage_lbl.configure(text="")
            return
        t = stats["total"]

        land_parts = (
            f"fixing {stats.get('land_fixing','?')}  "
            f"fetch {stats.get('land_fetch','?')}  "
            f"utility {stats.get('land_utility','?')}  "
            f"basic {stats['basic_lands']}"
        )
        owned = stats["owned_direct"] + stats.get("owned_swapped", 0) + stats["owned_nlp"] + stats["owned_lands"]
        parts = [
            f"Owned: {owned}/{t} ({owned/t*100:.0f}%)",
            f"Spells: {stats['owned_direct']} direct"
            f" + {stats.get('owned_swapped', 0)} swapped"
            f" + {stats['owned_nlp']} NLP",
            f"Lands: {land_parts}",
        ]

        self._coverage_lbl.configure(text="    ".join(parts))

    def _cancel_build(self):
        self._status_var.set("Cancellation requested — stops after the current step.")
        self._cancel_btn.configure(state=tk.DISABLED)
        
        
    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.configure(state="disabled")

    def _populate_deck_tab(self, text: str):
        self._deck_text.delete("1.0", tk.END)
        self._deck_text.insert(tk.END, text)

    def _copy_deck(self):
        self.clipboard_clear()
        self.clipboard_append(self._deck_text.get("1.0", tk.END))
        self._status_var.set("Copied to clipboard.")

    def _save_deck(self):
        path = filedialog.asksaveasfilename(
            title="Save deck as",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=self.BASE_DIR)
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._deck_text.get("1.0", tk.END))
            self._status_var.set(f"Saved → {path}")

    # Model creation
    def _start_create_model(self):
        """Download community decks and train the model for the current commander."""
        commander = self._commander_var.get().strip()
        if not commander:
            self._model_status_lbl.configure(text="Enter a commander name first.", foreground="red")
            return

        # Both data files must exist before we can do anything
        from build_deck import CARD_FILE, FEATURE_CSV
        missing = []
        if not os.path.exists(os.path.join(self.BASE_DIR, CARD_FILE)):
            missing.append("all_cards.json")
        if not os.path.exists(os.path.join(self.BASE_DIR, FEATURE_CSV)):
            missing.append("mtg_cards_features.csv")
        if missing:
            self._model_status_lbl.configure(
                text="✗ Card data not found — click 'Update Card Data' first.",
                foreground="red",
            )
            self._status_var.set("Please update card data before creating a model.")
            return

        self._create_model_btn.configure(state=tk.DISABLED)
        self._build_btn.configure(state=tk.DISABLED)
        self._model_status_lbl.configure(text="Working…", foreground="gray")
        self._status_var.set("Creating model…")
        self._nb.select(0)

        redirect = _StreamRedirect(self._log)
        sys.stdout = redirect
        sys.stderr = redirect

        threading.Thread(target=self._run_create_model, daemon=True).start()

    def _run_create_model(self):
        try:
            from build_deck import create_model, CARD_FILE, FEATURE_CSV
            successful = create_model(
                commander_name=self._commander_var.get().strip(),
                redownload=self._rescrape_var.get(),
                n_decks=self._n_decks_var.get(),
                cards_json=CARD_FILE,
                feature_csv=FEATURE_CSV,
            )
            if not successful:
                print(f"Commander not found, aborting")
            self.after(0, self._on_create_model_done, bool(successful))
        except SystemExit as e:
            self.after(0, self._on_create_model_done, False, f"Exited: {e}")
        except Exception as e:
            import traceback
            print(f"\nERROR: {e}\n{traceback.format_exc()}")
            self.after(0, self._on_create_model_done, False, str(e))

    def _on_create_model_done(self, success: bool, error: str = ""):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._create_model_btn.configure(state=tk.NORMAL)
        self._build_btn.configure(state=tk.NORMAL)
        self._refresh_model_status()
        self._status_var.set("Model created." if success else f"Model creation failed — see log.")

    # Update card data, also regenerates all_cards
    def _start_update_card_data(self):
        self._update_btn.configure(state=tk.DISABLED)
        self._build_btn.configure(state=tk.DISABLED)
        self._status_var.set("Downloading card data…")
        self._nb.select(0)

        redirect = _StreamRedirect(self._log)
        sys.stdout = redirect
        sys.stderr = redirect

        threading.Thread(target=self._run_update_card_data, daemon=True).start()

    def _run_update_card_data(self):
        import requests

        try:
            # Check bulk-data is fine
            print("Fetching Scryfall bulk-data index…")
            resp = requests.get("https://api.scryfall.com/bulk-data", timeout=30)
            resp.raise_for_status()

            download_url = None
            for entry in resp.json().get("data", []):
                if entry.get("type") == "oracle_cards":
                    download_url = entry["download_uri"]
                    size_mb = entry.get("size", 0) / 1_000_000
                    print(f"  oracle_cards  ({size_mb:.0f} MB compressed)")
                    break

            if not download_url:
                print("ERROR: Could not find oracle_cards entry in Scryfall bulk-data response.")
                self.after(0, self._on_update_done, False)
                return

            # Stream the download of all cards
            data_dir = os.path.join(self.BASE_DIR, "data")
            os.makedirs(data_dir, exist_ok=True)
            out_path = os.path.join(data_dir, "all_cards.json")
            print(f"Downloading to {out_path} …")

            with requests.get(download_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                last_pct = -1
                chunk_size = 1024 * 1024  # 1 MB chunks

                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded / total * 10) * 10
                            if pct != last_pct:
                                print(f"  {downloaded / 1_000_000:.0f} / "
                                      f"{total / 1_000_000:.0f} MB  ({pct}%)")
                                last_pct = pct

            print(f"Download complete ({downloaded / 1_000_000:.1f} MB).\n")

            # Download additional json files to be used with the model
            catalogs = [
                ("keyword_abilities.json", "https://api.scryfall.com/catalog/keyword-abilities"),
                ("keyword_actions.json",   "https://api.scryfall.com/catalog/keyword-actions"),
                ("ability_words.json",     "https://api.scryfall.com/catalog/ability-words"),
            ]
            print("Downloading keyword catalogs from Scryfall…")
            for filename, url in catalogs:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                dest = os.path.join(data_dir, filename)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  ✓ {filename}  ({r.json().get('total_values', '?')} entries)")
            print()

            # Create card features
            print("Regenerating mtg_cards_features.csv…")
            from make_card_features import main as _build_features
            _build_features(self.BASE_DIR)
            self.after(0, self._on_update_done, True)

        except Exception as e:
            import traceback
            print(f"\nERROR: {e}\n{traceback.format_exc()}")
            self.after(0, self._on_update_done, False)

    def _on_update_done(self, success: bool):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._update_btn.configure(state=tk.NORMAL)
        self._build_btn.configure(state=tk.NORMAL)
        self._status_var.set(
            "Card data updated successfully." if success else "Card data update failed — see log."
        )

    # Background task for loading card names
    def _load_card_names_background(self):
        if self._all_card_names:
            return

        def _loader():
            try:
                import pandas as pd
                from build_deck import FEATURE_CSV
                df = pd.read_csv(
                    os.path.join(self.BASE_DIR, FEATURE_CSV),
                    usecols=["name", "legendary", "is_creature", "is_planeswalker"])
                self._all_card_names = sorted(df["name"].tolist())
                mask = (df["legendary"] == 1) & ((df["is_creature"] == 1))
                commanders = df.loc[mask, "name"].tolist()
                self._legendary_creature_names = sorted(commanders)
                self._commander_name_set = {n.lower(): n for n in commanders}
                dfc_map = {}
                for name in commanders:
                    if " // " in name:
                        front = name.split(" // ")[0].lower()
                        dfc_map[front] = name
                self._dfc_front_face_map = dfc_map
            except Exception:
                pass  # silently fail

        threading.Thread(target=_loader, daemon=True).start()

    # Used to auto-complete commanders when typing
    def _wire_commander_autocomplete(self):
        drop = tk.Toplevel(self)
        drop.overrideredirect(True)
        drop.withdraw()

        frame = ttk.Frame(drop, relief="solid", borderwidth=1)
        frame.pack(fill=tk.BOTH, expand=True)
        lb = tk.Listbox(frame, height=8, selectmode=tk.SINGLE,
                        activestyle="dotbox", relief="flat", borderwidth=0)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=lb.yview)
        lb.configure(yscrollcommand=vsb.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._cmd_drop = drop
        self._cmd_selecting = False

        def _show():
            e = self._commander_entry
            x, y = e.winfo_rootx(), e.winfo_rooty() + e.winfo_height()
            w = max(e.winfo_width(), 300)
            drop.geometry(f"{w}x160+{x}+{y}")
            drop.deiconify()
            drop.lift()

        def _hide():
            drop.withdraw()

        def _update(*_):
            if self._cmd_selecting:
                return
            query = self._commander_var.get().strip().lower()
            lb.delete(0, tk.END)
            if len(query) < 2 or not self._legendary_creature_names:
                _hide()
                return
            matches = [n for n in self._legendary_creature_names if query in n.lower()][:50]
            if not matches:
                _hide()
                return
            for name in matches:
                lb.insert(tk.END, name)
            lb.selection_set(0)
            _show()

        def _select(name: str):
            self._cmd_selecting = True
            self._commander_var.set(name)
            self._cmd_selecting = False
            _hide()
            self._commander_entry.icursor(tk.END)

        def _on_down(_):
            if not lb.size() or not drop.winfo_viewable():
                return
            cur = lb.curselection()
            idx = min((cur[0] + 1) if cur else 0, lb.size() - 1)
            lb.selection_clear(0, tk.END)
            lb.selection_set(idx)
            lb.see(idx)
            return "break"

        def _on_up(_):
            if not lb.size() or not drop.winfo_viewable():
                return
            cur = lb.curselection()
            if cur:
                idx = max(cur[0] - 1, 0)
                lb.selection_clear(0, tk.END)
                lb.selection_set(idx)
                lb.see(idx)
            return "break"

        def _on_return(_):
            cur = lb.curselection()
            if cur and drop.winfo_viewable():
                _select(lb.get(cur[0]))
                return "break"

        self._commander_var.trace_add("write", _update)
        self._commander_entry.bind("<Down>",   _on_down)
        self._commander_entry.bind("<Up>",     _on_up)
        self._commander_entry.bind("<Return>", _on_return)
        self._commander_entry.bind("<Escape>", lambda _: _hide())
        self._commander_entry.bind("<FocusOut>", lambda _: self.after(150, _hide))
        lb.bind("<ButtonRelease-1>",
                lambda e: _select(lb.get(lb.nearest(e.y))) if lb.size() else None)

    def _build_menu(self):
        menu_bar = tk.Menu(self)
        self.config(menu=menu_bar)

        coll_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Collection", menu=coll_menu)

        imp_menu = tk.Menu(coll_menu, tearoff=0)
        coll_menu.add_cascade(label="Import…", menu=imp_menu)
        for platform, label in [("archidekt", "Archidekt"),
                                 ("goldfish",   "MTGGoldfish"),
                                 ("moxfield",   "Moxfield"),
                                 ("tappedout",  "TappedOut")]:
            imp_menu.add_command(
                label=label,
                command=lambda p=platform: self._open_import_dialog(p))

        exp_menu = tk.Menu(coll_menu, tearoff=0)
        coll_menu.add_cascade(label="Export…", menu=exp_menu)
        for platform, label in [("archidekt", "Archidekt"),
                                 ("goldfish",   "MTGGoldfish"),
                                 ("moxfield",   "Moxfield"),
                                 ("tappedout",  "TappedOut")]:
            exp_menu.add_command(
                label=label,
                command=lambda p=platform: self._open_export_dialog(p))

        coll_menu.add_separator()
        coll_menu.add_command(label="Add Card…", command=self._open_add_card_dialog)
        coll_menu.add_command(label="Bulk Insert…", command=self._open_bulk_insert_dialog)

    _PLATFORM_LABELS = {
        "archidekt": "Archidekt",
        "goldfish":  "MTGGoldfish",
        "moxfield":  "Moxfield",
        "tappedout": "TappedOut",
    }

    def _open_import_dialog(self, platform: str):
        label = self._PLATFORM_LABELS[platform]
        dlg = tk.Toplevel(self)
        dlg.title(f"Import from {label}")
        dlg.geometry("500x160")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Paste your {label} deck URL:").pack(
            anchor=tk.W, padx=12, pady=(12, 4))
        url_var = tk.StringVar()
        url_entry = ttk.Entry(dlg, textvariable=url_var, width=64)
        url_entry.pack(fill=tk.X, padx=12)
        url_entry.focus_set()

        status = ttk.Label(dlg, text="Cards will be appended to your owned cards file.",
                           foreground="gray")
        status.pack(anchor=tk.W, padx=12, pady=(8, 0))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(anchor=tk.E, padx=12, pady=10)
        import_btn = ttk.Button(btn_frame, text="Import",
                                command=lambda: self._do_import(
                                    platform, url_var.get().strip(), status, import_btn, dlg))
        import_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.bind("<Return>", lambda _: import_btn.invoke())

    def _do_import(self, platform: str, url: str, status_lbl, btn, dlg):
        if not url:
            status_lbl.configure(text="Please enter a URL.", foreground="red")
            return
        btn.configure(state=tk.DISABLED)
        status_lbl.configure(text="Fetching deck…", foreground="gray")

        def _worker():
            try:
                cards = self._fetch_deck(platform, url)
                self.after(0, self._on_import_done, cards, status_lbl, btn, dlg)
            except Exception as e:
                self.after(0, lambda err=str(e): (
                    status_lbl.configure(text=f"Error: {err}", foreground="red"),
                    btn.configure(state=tk.NORMAL),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _fetch_deck(self, platform: str, url: str) -> list[str]:
        import re
        import requests

        if platform == "archidekt":
            m = re.search(r'/decks/(\d+)', url)
            if not m:
                raise ValueError("Could not find deck ID in URL.")
            r = requests.get(f"https://archidekt.com/api/decks/{m.group(1)}/",
                             timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return [
                entry.get("card", {}).get("oracleCard", {}).get("name", "")
                for entry in r.json().get("cards", [])
                if entry.get("card", {}).get("oracleCard", {}).get("name")
            ]

        elif platform == "goldfish":
            m = re.search(r'/deck[s]?/(\d+)', url)
            if not m:
                raise ValueError("Could not find deck ID in URL.")
            r = requests.get(f"https://www.mtggoldfish.com/deck/download/{m.group(1)}",
                             timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            cards = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("//") or line.lower().startswith("about"):
                    continue
                m2 = re.match(r'^(\d+)\s+(.+)$', line)
                cards.append(m2.group(2) if m2 else line)
            return cards

        elif platform == "moxfield":
            m = re.search(r'/decks/([A-Za-z0-9_-]+)', url)
            if not m:
                raise ValueError("Could not find deck ID in URL.")
            r = requests.get(f"https://api.moxfield.com/v2/decks/all/{m.group(1)}",
                             timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
            cards = []
            for section in ("mainboard", "commanders", "companions"):
                cards.extend(data.get(section, {}).keys())
            return cards

        elif platform == "tappedout":
            m = re.search(r'mtg-decks/([^/?#]+)', url)
            if not m:
                raise ValueError("Could not find deck slug in URL.")
            r = requests.get(
                f"https://tappedout.net/mtg-decks/{m.group(1)}/?fmt=txt",
                timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            cards = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("//") or line.upper().startswith("SB:"):
                    continue
                m2 = re.match(r'^(\d+)x?\s+(.+)$', line)
                cards.append(m2.group(2) if m2 else line)
            return cards

        else:
            raise ValueError(f"Unknown platform: {platform}")

    def _on_import_done(self, cards: list[str], status_lbl, btn, dlg):
        if not cards:
            status_lbl.configure(text="No cards found in that deck.", foreground="red")
            btn.configure(state=tk.NORMAL)
            return
        owned_path = self._owned_var.get()
        try:
            with open(owned_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n// Imported {len(cards)} cards\n")
                for card in cards:
                    fh.write(f"{card}\n")
            status_lbl.configure(
                text=f"✓ Added {len(cards)} cards to {os.path.basename(owned_path)}",
                foreground="green")
            btn.configure(state=tk.NORMAL)
            self.after(2500, dlg.destroy)
        except Exception as e:
            status_lbl.configure(text=f"Error writing file: {e}", foreground="red")
            btn.configure(state=tk.NORMAL)

    def _open_export_dialog(self, platform: str):
        label = self._PLATFORM_LABELS[platform]
        dlg = tk.Toplevel(self)
        dlg.title(f"Export for {label}")
        dlg.geometry("340x160")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="What would you like to export?").pack(
            anchor=tk.W, padx=12, pady=(12, 6))

        choice = tk.StringVar(value="collection")
        ttk.Radiobutton(dlg, text="My card collection  (owned_cards.txt)",
                        variable=choice, value="collection").pack(anchor=tk.W, padx=24)
        ttk.Radiobutton(dlg, text="Currently built deck  (Deck Output tab)",
                        variable=choice, value="deck").pack(anchor=tk.W, padx=24)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(anchor=tk.E, padx=12, pady=14)
        ttk.Button(btn_frame, text="Export…",
                   command=lambda: self._do_export(platform, choice.get(), dlg)
                   ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _do_export(self, platform: str, choice: str, dlg):
        import re
        label = self._PLATFORM_LABELS[platform]

        if choice == "collection":
            owned_path = self._owned_var.get()
            if not os.path.exists(owned_path):
                self._status_var.set("Owned cards file not found.")
                dlg.destroy()
                return
            with open(owned_path, encoding="utf-8") as fh:
                raw = fh.read()
            # Normalise to "1 Card Name" format (strip quantities, skip comments)
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r'^(\d+)x?\s+(.+)$', line)
                lines.append(f"1 {m.group(2) if m else line}")
            content = "\n".join(lines)
            default_name = f"collection_for_{label.lower()}.txt"
        else:
            raw = self._deck_text.get("1.0", tk.END).strip()
            if not raw:
                self._status_var.set("No deck built yet — build a deck first.")
                dlg.destroy()
                return
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r'^(\d+)x?\s+(.+)$', line)
                lines.append(f"{m.group(1)} {m.group(2)}" if m else f"1 {line}")
            content = "\n".join(lines)
            default_name = f"deck_for_{label.lower()}.txt"

        dlg.destroy()
        path = filedialog.asksaveasfilename(
            title=f"Export for {label}",
            initialfile=default_name,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=self.BASE_DIR)
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._status_var.set(f"Exported {len(lines)} cards → {os.path.basename(path)}")

    # Add single card
    def _open_add_card_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add Card to Collection")
        dlg.geometry("420x300")
        dlg.resizable(False, True)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Start typing a card name:").pack(
            anchor=tk.W, padx=12, pady=(12, 4))

        search_var = tk.StringVar()
        entry = ttk.Entry(dlg, textvariable=search_var, width=52)
        entry.pack(fill=tk.X, padx=12)
        entry.focus_set()

        list_frame = ttk.Frame(dlg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 0))
        suggestion_lb = tk.Listbox(list_frame, height=8, selectmode=tk.SINGLE,
                                   activestyle="dotbox")
        suggestion_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=suggestion_lb.yview)
        suggestion_lb.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        status = ttk.Label(dlg, text="", foreground="gray")
        status.pack(anchor=tk.W, padx=12, pady=(4, 0))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(anchor=tk.E, padx=12, pady=8)

        def _add():
            cur = suggestion_lb.curselection()
            name = suggestion_lb.get(cur[0]) if cur else search_var.get().strip()
            if not name:
                status.configure(text="Enter a card name.", foreground="red")
                return
            try:
                with open(self._owned_var.get(), "a", encoding="utf-8") as fh:
                    fh.write(f"{name}\n")
                status.configure(text=f"✓ Added: {name}", foreground="green")
                search_var.set("")
                entry.focus_set()
            except Exception as e:
                status.configure(text=f"Error: {e}", foreground="red")

        add_btn = ttk.Button(btn_frame, text="Add Card", command=_add)
        add_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side=tk.LEFT)

        # Wire up autocomplete (loads card names in background if not yet cached)
        self._wire_autocomplete(entry, search_var, suggestion_lb, status, _add)

    def _wire_autocomplete(self, entry, search_var, lb, status, add_fn):
        def _update_list(*_):
            query = search_var.get().strip().lower()
            lb.delete(0, tk.END)
            if len(query) < 2 or not self._all_card_names:
                return
            matches = [n for n in self._all_card_names if query in n.lower()][:50]
            for name in matches:
                lb.insert(tk.END, name)
            if matches:
                lb.selection_set(0)

        search_var.trace_add("write", _update_list)

        def _on_down(_):
            if not lb.size():
                return "break"
            cur = lb.curselection()
            idx = min((cur[0] + 1) if cur else 0, lb.size() - 1)
            lb.selection_clear(0, tk.END)
            lb.selection_set(idx)
            lb.see(idx)
            return "break"

        def _on_up(_):
            if not lb.size():
                return "break"
            cur = lb.curselection()
            if cur:
                idx = max(cur[0] - 1, 0)
                lb.selection_clear(0, tk.END)
                lb.selection_set(idx)
                lb.see(idx)
            return "break"

        entry.bind("<Down>", _on_down)
        entry.bind("<Up>",   _on_up)
        entry.bind("<Return>", lambda _: add_fn())
        lb.bind("<Double-Button-1>", lambda _: add_fn())

        if self._all_card_names:
            return  # Already loaded from a previous dialog or startup

        status.configure(text="Loading card names…")

        def _loader():
            try:
                import pandas as pd
                from build_deck import FEATURE_CSV
                df = pd.read_csv(
                    os.path.join(self.BASE_DIR, FEATURE_CSV),
                    usecols=["name", "legendary", "is_creature"])
                self._all_card_names = sorted(df["name"].tolist())
                if not self._legendary_creature_names:
                    mask = (df["legendary"] == 1) & ((df["is_creature"] == 1))
                    commanders = df.loc[mask, "name"].tolist()
                    self._legendary_creature_names = sorted(commanders)
                    self._commander_name_set = {n.lower(): n for n in commanders}
                    dfc_map = {}
                    for name in commanders:
                        if " // " in name:
                            front = name.split(" // ")[0].lower()
                            dfc_map[front] = name
                    self._dfc_front_face_map = dfc_map
                self.after(0, lambda: status.configure(
                    text=f"{len(self._all_card_names):,} cards available.",
                    foreground="gray"))
            except Exception as e:
                self.after(0, lambda err=str(e): status.configure(
                    text=f"Could not load card names: {err}", foreground="red"))

        threading.Thread(target=_loader, daemon=True).start()

    # Add lots of cards
    # Thanks to comment (https://www.reddit.com/r/EDH/comments/1ruq2h5/comment/oapc23m/?context=1) for bringing this up
    #   hopefully this solves some issues with adding cards
    def _parse_card_name_from_line(self, line: str) -> tuple[str | None, str]:
        import re

        original = line.strip()
        if not original or original.startswith("//"):
            return None, original

        s = original

        # Strips quantity
        s = re.sub(r'^\d+[xX]?\s+', '', s)

        # Strips set codes in [123] or (123)
        s = re.sub(r'\s*[\(\[][A-Za-z0-9]{2,6}[\)\]]\s*\d*', '', s)

        # Remove collectors numbers
        s = re.sub(r'\s+\d{1,4}(?:\s|$)', ' ', s)

        # Remove prices
        s = re.sub(r'\$[\d.]+', '', s)
        s = re.sub(r'[\d.]+\s*\$', '', s)
        s = re.sub(r'\b(?:USD|EUR|GBP|usd|eur)\b', '', s)
        s = re.sub(r'\b\d+\.\d{2}\b', '', s)   # bare price like 1.23

        # Strips additional tags like foil
        s = re.sub(r'\*[A-Za-z]+\*', '', s)
        s = re.sub(r'\b(?:Foil|foil|NM|LP|MP|HP|DMG|NM-M|SP)\b', '', s)

        # Get rid of all white space
        candidate = re.sub(r'\s{2,}', ' ', s).strip()

        if not candidate:
            return None, candidate

        # Attempts to load from already loaded cards
        if self._all_card_names:
            candidate_lower = candidate.lower()

            for name in self._all_card_names:
                if name.lower() == candidate_lower:
                    return name, candidate

            # Try to remove trailing characters until a match is found
            parts = candidate.split()
            for end in range(len(parts), 0, -1):
                attempt = " ".join(parts[:end]).strip().lower()
                for name in self._all_card_names:
                    if name.lower() == attempt:
                        return name, candidate

            # Final attempt, do a substring match to see if its a valid card
            for name in self._all_card_names:
                if name.lower() in candidate_lower:
                    return name, candidate
            return None, candidate

        return candidate, candidate

    def _open_bulk_insert_dialog(self):
        import re as _re

        dlg = tk.Toplevel(self)
        dlg.title("Bulk Insert Cards")
        dlg.geometry("680x560")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text=(
                "Paste any card list below — one card per line.\n"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=12, pady=(12, 4))

        input_frame = ttk.Frame(dlg)
        input_frame.pack(fill=tk.BOTH, expand=True, padx=12)

        input_text = scrolledtext.ScrolledText(
            input_frame, font=("Consolas", 9), height=10,
            background="#fafafa", foreground="#1a1a1a", wrap=tk.NONE)
        input_text.pack(fill=tk.BOTH, expand=True)

        # Preview
        sep = ttk.Separator(dlg, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, padx=12, pady=(8, 4))

        ttk.Label(dlg, text="Preview (green = matched, orange = unmatched):",
                  font=("", 9, "bold")).pack(anchor=tk.W, padx=12)

        preview_frame = ttk.Frame(dlg)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 0))

        preview_text = scrolledtext.ScrolledText(
            preview_frame, font=("Consolas", 9), height=8, state="disabled",
            background="#1e1e1e", foreground="#d4d4d4", wrap=tk.NONE)
        preview_text.pack(fill=tk.BOTH, expand=True)
        preview_text.tag_configure("matched",   foreground="#6fcf97")
        preview_text.tag_configure("unmatched", foreground="#f2994a")
        preview_text.tag_configure("skipped",   foreground="#888888")

        status_lbl = ttk.Label(dlg, text="", foreground="gray")
        status_lbl.pack(anchor=tk.W, padx=12, pady=(4, 0))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(anchor=tk.E, padx=12, pady=8)

        # State shared between parse and commit
        _parsed: list[str] = []   # canonical names to add

        def _run_parse():
            nonlocal _parsed
            _parsed = []
            raw_lines = input_text.get("1.0", tk.END).splitlines()

            preview_text.configure(state="normal")
            preview_text.delete("1.0", tk.END)

            matched_count = 0
            unmatched_count = 0
            skipped_count = 0

            for raw in raw_lines:
                stripped = raw.strip()
                if not stripped or stripped.startswith("//"):
                    skipped_count += 1
                    preview_text.insert(tk.END, f"  (skipped)  {stripped}\n", "skipped")
                    continue

                name, candidate = self._parse_card_name_from_line(stripped)
                if name:
                    _parsed.append(name)
                    matched_count += 1
                    preview_text.insert(
                        tk.END,
                        f"✓  {name}  ←  {stripped}\n",
                        "matched",
                    )
                else:
                    unmatched_count += 1
                    preview_text.insert(
                        tk.END,
                        f"?  {candidate or stripped}\n",
                        "unmatched",
                    )

            preview_text.configure(state="disabled")

            parts = [f"{matched_count} matched"]
            if unmatched_count:
                parts.append(f"{unmatched_count} unmatched (orange lines won't be added)")
            if skipped_count:
                parts.append(f"{skipped_count} skipped")
            status_lbl.configure(
                text="  |  ".join(parts),
                foreground="gray" if not unmatched_count else "orange",
            )
            add_btn.configure(
                state=tk.NORMAL if _parsed else tk.DISABLED,
                text=f"Add {len(_parsed)} Cards" if _parsed else "Add Cards",
            )

        def _commit():
            if not _parsed:
                return
            owned_path = self._owned_var.get()
            try:
                with open(owned_path, "a", encoding="utf-8") as fh:
                    for name in _parsed:
                        fh.write(f"{name}\n")
                status_lbl.configure(
                    text=f"✓ Added {len(_parsed)} cards to collection.",
                    foreground="green",
                )
                add_btn.configure(state=tk.DISABLED)
                input_text.delete("1.0", tk.END)
                preview_text.configure(state="normal")
                preview_text.delete("1.0", tk.END)
                preview_text.configure(state="disabled")
            except Exception as e:
                status_lbl.configure(text=f"Error: {e}", foreground="red")

        ttk.Button(btn_frame, text="Parse", command=_run_parse).pack(
            side=tk.LEFT, padx=(0, 6))
        add_btn = ttk.Button(btn_frame, text="Add Cards",
                             command=_commit, state=tk.DISABLED)
        add_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side=tk.LEFT)

        input_text.focus_set()

    def _on_close(self):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self.destroy()

if __name__ == "__main__":
    app = DeckBuilderApp()
    app.mainloop()
