"""Système de design moderne pour Printer Monitor.

Définit la palette de couleurs, la typographie et les styles ttk de
l'application : cartes plates sans relief 3D, boutons modernes avec états
de survol, tableaux aérés et barres de défilement discrètes.

Le style repose sur le thème « clam » (personnalisable sur toutes les
plateformes) plutôt que sur les thèmes natifs rétro (winnative/95).
"""

import tkinter as tk
from tkinter import ttk

# ── Palette ──────────────────────────────────────────────────────────────
BG = "#f1f4f8"            # Fond général de l'application
SURFACE = "#ffffff"       # Fond des cartes / panneaux
BORDER = "#e2e8f0"        # Bordures fines et séparateurs
TEXT = "#1e293b"          # Texte principal
MUTED = "#64748b"         # Texte secondaire / libellés discrets
PRIMARY = "#2563eb"       # Couleur d'action principale (bleu)
PRIMARY_HOVER = "#1d4ed8"
PRIMARY_ACTIVE = "#1e40af"
PRIMARY_SOFT = "#dbeafe"  # Sélection douce (lignes du tableau)
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"

# États d'alerte des lignes du tableau (palette douce et lisible)
ROW_ERROR = "#fee2e2"
ROW_CRITICAL = "#fee2e2"
ROW_LOW = "#ffedd5"
ROW_BOTTLE = "#fef9c3"
ROW_STAPLES = "#fefce8"
ROW_TIMEOUT = "#ffedd5"
ROW_OFFLINE = "#e2e8f0"
ROW_DOWN = "#ef4444"
ROW_DOWN_ALT = "#b91c1c"

FONT_FAMILY = "Segoe UI"


def _font(size=9, weight="normal"):
    return (FONT_FAMILY, size, weight)


def apply_theme(root):
    """Applique le thème moderne complet à la fenêtre racine."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    root.configure(background=BG)

    # Typographie par défaut de l'application : même taille que le thème
    # natif Windows (9 pt) afin de conserver les métriques de mesure des
    # colonnes et l'adaptation responsive existants.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            f = tkfont_named(name)
            f.configure(family=FONT_FAMILY, size=9)
        except Exception:
            pass
    try:
        tkfont_named("TkHeadingFont").configure(family=FONT_FAMILY, size=9, weight="bold")
    except Exception:
        pass

    # ── Frames & cartes ──
    style.configure("TFrame", background=BG)
    style.configure(
        "Card.TFrame",
        background=SURFACE,
        relief="solid",
        borderwidth=1,
        bordercolor=BORDER,
    )
    style.configure("Toolbar.TFrame", background=SURFACE)

    # ── Libellés ──
    style.configure("TLabel", background=BG, foreground=TEXT, font=_font())
    style.configure("Card.TLabel", background=SURFACE, foreground=TEXT, font=_font())
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=_font(9))
    style.configure("Card.Muted.TLabel", background=SURFACE, foreground=MUTED, font=_font(9))
    style.configure(
        "Heading.TLabel", background=BG, foreground=TEXT, font=_font(11, "bold")
    )
    style.configure(
        "Card.Heading.TLabel", background=SURFACE, foreground=TEXT, font=_font(11, "bold")
    )

    # ── Identité de marque (header intégré) ──
    style.configure(
        "Brand.Title.TLabel", background=SURFACE, foreground=PRIMARY, font=_font(12, "bold")
    )
    style.configure(
        "Brand.Chip.TLabel",
        background=PRIMARY_SOFT,
        foreground=PRIMARY,
        font=_font(8, "bold"),
        padding=(4, 1),
    )

    # ── Boutons ──
    style.configure(
        "TButton",
        background=SURFACE,
        foreground=TEXT,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        borderwidth=1,
        focusthickness=0,
        focuscolor=SURFACE,
        padding=(10, 5),
        font=_font(),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("pressed", "#eef2f7"), ("active", "#f5f7fa")],
        bordercolor=[("active", "#cbd5e1")],
    )

    # Bouton d'action principale (bleu plein)
    style.configure(
        "Accent.TButton",
        background=PRIMARY,
        foreground="#ffffff",
        bordercolor=PRIMARY,
        lightcolor=PRIMARY,
        darkcolor=PRIMARY,
        focuscolor=PRIMARY,
        padding=(12, 5),
        font=_font(9, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", "#93c5fd"), ("pressed", PRIMARY_ACTIVE), ("active", PRIMARY_HOVER)],
        bordercolor=[("disabled", "#93c5fd")],
        foreground=[("disabled", "#eff6ff")],
    )

    # Bouton d'action destructive (Arrêter un rafraîchissement)
    style.configure(
        "Danger.TButton",
        background=DANGER,
        foreground="#ffffff",
        bordercolor=DANGER,
        lightcolor=DANGER,
        darkcolor=DANGER,
        focuscolor=DANGER,
        padding=(10, 5),
        font=_font(9, "bold"),
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", "#fca5a5"), ("pressed", DANGER_HOVER), ("active", DANGER_HOVER)],
        bordercolor=[("disabled", "#fca5a5")],
        foreground=[("disabled", "#fef2f2")],
    )

    # ── Champs de saisie & listes déroulantes ──
    style.configure(
        "TEntry",
        fieldbackground=SURFACE,
        foreground=TEXT,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        insertcolor=TEXT,
        padding=(6, 4),
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", PRIMARY)],
        lightcolor=[("focus", PRIMARY)],
        darkcolor=[("focus", PRIMARY)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE,
        foreground=TEXT,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        arrowcolor=MUTED,
        padding=(6, 4),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", SURFACE)],
        bordercolor=[("focus", PRIMARY)],
        lightcolor=[("focus", PRIMARY)],
        darkcolor=[("focus", PRIMARY)],
        arrowcolor=[("active", TEXT)],
    )
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", PRIMARY_SOFT)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", _font())

    # ── Cases à cocher ──
    style.configure(
        "TCheckbutton",
        background=BG,
        foreground=TEXT,
        focuscolor=BG,
        font=_font(),
    )
    style.map(
        "TCheckbutton",
        background=[("active", BG)],
        indicatorcolor=[("selected", PRIMARY), ("pressed", BG)],
    )
    style.configure(
        "Card.TCheckbutton",
        background=SURFACE,
        foreground=TEXT,
        focuscolor=SURFACE,
        font=_font(),
    )
    style.map(
        "Card.TCheckbutton",
        background=[("active", SURFACE)],
        indicatorcolor=[("selected", PRIMARY), ("pressed", SURFACE)],
    )

    # ── Groupes (LabelFrame) façon carte ──
    style.configure(
        "TLabelframe",
        background=SURFACE,
        relief="solid",
        borderwidth=1,
        bordercolor=BORDER,
    )
    style.configure(
        "TLabelframe.Label",
        background=SURFACE,
        foreground=TEXT,
        font=_font(10, "bold"),
    )

    # ── Tableau (Treeview) ──
    # rowheight porté à 40 (au lieu de 26) : la hauteur réelle d'une ligne de
    # chip (°Segoe UI 8 + padding + bordure) est ~17 px. Une seule ligne tient
    # en 26 px, mais aucune place pour un retour à la ligne. À 40 px, deux
    # lignes de pastilles tiennent confortablement, ce qui permet au layout
    # responsive du ChipRenderer de « revenir à la ligne » (et non d'écraser
    # les toners/consommables/statut quand la colonne est étroite) — l'espace
    # vertical supplémentaire sert uniquement la lisibilité, le centrage
    # vertical reste intact (chips centrés verticalement dans la cellule).
    style.configure(
        "Treeview",
        background=SURFACE,
        fieldbackground=SURFACE,
        foreground=TEXT,
        bordercolor=BORDER,
        borderwidth=0,
        rowheight=40,
        font=_font(),
    )
    style.configure(
        "Treeview.Heading",
        background="#f8fafc",
        foreground=MUTED,
        bordercolor=BORDER,
        borderwidth=0,
        relief="flat",
        padding=(6, 5),
        font=_font(9, "bold"),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", "#eef2f7")],
        foreground=[("active", TEXT)],
    )
    style.map(
        "Treeview",
        background=[("selected", PRIMARY_SOFT)],
        foreground=[("selected", TEXT)],
    )

    # ── Barres de défilement plates et discrètes ──
    style.configure(
        "TScrollbar",
        background="#d7dee8",
        troughcolor=SURFACE,
        bordercolor=SURFACE,
        lightcolor="#d7dee8",
        darkcolor="#d7dee8",
        arrowcolor=MUTED,
        relief="flat",
        arrowsize=12,
    )
    style.map(
        "TScrollbar",
        background=[("active", "#b9c3d0"), ("pressed", "#a5b1c2")],
        arrowcolor=[("active", TEXT)],
    )

    # ── Barre de progression ──
    style.configure(
        "Horizontal.TProgressbar",
        troughcolor="#e2e8f0",
        background=PRIMARY,
        bordercolor=BG,
        lightcolor=PRIMARY,
        darkcolor=PRIMARY,
        thickness=8,
    )

    # ── Séparateurs ──
    style.configure("TSeparator", background=BORDER)


def tkfont_named(name):
    """Retourne la police nommée Tk (import local pour éviter les cycles)."""
    import tkinter.font as tkfont

    return tkfont.nametofont(name)


def style_toplevel(win):
    """Applique le fond du thème à une fenêtre de dialogue (Toplevel)."""
    try:
        win.configure(background=BG)
    except Exception:
        pass
    return win


def make_card(parent, **pack_kwargs):
    """Crée une carte (frame blanche bordée) prête à recevoir du contenu."""
    card = ttk.Frame(parent, style="Card.TFrame")
    if pack_kwargs:
        card.pack(**pack_kwargs)
    return card