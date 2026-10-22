"""Rendu des *chips* (pastilles couleur) dans la liste des machines.

Le ``ttk.Treeview`` ne permet pas de colorer texte ou fond **par cellule** :
les couleurs ne s'appliquent qu'à une ligne entière via les *tags*. De plus,
sous Tk 8.6 les glyphes emoji-couleur (⬛🟦🟥🟨) sont rendus en noir uni,
quelle que soit la police ou la couleur ``fg``.

Pour afficher de vraies couleurs par consommable (toner C/M/Y/K, tambour,
bouteille, agrafes, statut …), un petit ``tk.Frame`` opaque est superposé à
**chaque cellule concernée** des lignes visibles ; il contient les pastilles
(fond / texte / bordure colorés, contraste garanti par calcul de luminance).
Le reste du tableau reste celui du Treeview : tri, sélection, copie, export,
redimensionnement et défilement ne sont pas affectés. Les événements souris
reçus sur les overlays sont retransmis au Treeview.

La logique métier n'est pas modifiée : ce module ne fait que l'affichage.
"""

import tkinter as tk

from .theme import (
    ROW_ERROR,
    ROW_OFFLINE,
    ROW_TIMEOUT,
)

# ── Palette des chips ────────────────────────────────────────────────────────
CHIP_BG_OK = "#dcfce7"       # vert doux : OK / prêt
CHIP_FG_OK = "#166534"
CHIP_BG_WARN = "#fef9c3"     # jaune doux : seuil d'alerte atteint
CHIP_FG_WARN = "#854d0e"
CHIP_BG_CRIT = "#fee2e2"     # rouge doux : critique / pleine
CHIP_FG_CRIT = "#991b1b"
CHIP_BG_UNKNOWN = "#e2e8f0"  # gris : niveau inconnu
CHIP_FG_UNKNOWN = "#334155"
CHIP_BG_DOWN = "#ef4444"     # rouge vif : machine « down »
CHIP_FG_DOWN = "#ffffff"

CHIP_BORDER = "#94a3b8"      # bordure fine neutre autour de chaque chip

# Couleurs réelles des consommables (C/M/Y/K) définies dans utils.py
from .utils import TONER_COLORS as TONER_COLOR_VALUES
from .utils import text_color_for_bg

# Alias « inconnu » utilisés dans chips_for_device()
UNKNOWN_BG = CHIP_BG_UNKNOWN
UNKNOWN_FG = CHIP_FG_UNKNOWN


def _contrast_fg(bg_hex):
    """Texte noir ou blanc selon le fond, pour un contraste garanti.

    Délègue à ``text_color_for_bg`` (utils) : comparaison des ratios de
    contraste WCAG des deux candidats sur la couleur demandée, ce qui reste
    correct quelle que soit la couleur (cyan, magenta, jaune, ...)."""
    return text_color_for_bg(bg_hex)


def _fmt_pct(supply):
    """Formate le pourcentage affiché pour un consommable."""
    dp = getattr(supply, "display_percentage", None)
    if dp is not None:
        return f"{dp}%"
    if supply.is_valid_level():
        return str(supply.level)
    return "-"


def _get_toner_color_safe(app, supply):
    """Couleur réelle du toner via la méthode existante de l'app (sécurisée)."""
    fn = getattr(app, "_get_toner_color", None)
    if callable(fn):
        try:
            return fn(supply)
        except Exception:
            return None
    return None

# === PART2_MARKER ===


def _toner_pills(device, app):
    """Chips de la colonne Toner : une vraie chip par couleur C/M/Y/K."""
    pills = []
    supplies = [s for s in device.supplies if getattr(s, "category", "") == "toner"]
    if not supplies:
        return pills

    color_map = {}
    others = []
    palette = set(TONER_COLOR_VALUES.values())
    for s in supplies:
        color = _get_toner_color_safe(app, s)
        if color in palette:
            color_map.setdefault(color, s)
        else:
            others.append(s)

    # Ordre stable K -> C -> M -> Y (values() préserve cet ordre de utils.py)
    for col, supply in color_map.items():
        text = _fmt_pct(supply)
        fg = _contrast_fg(col)
        pills.append({"bg": col, "fg": fg, "text": text})
    for s in others:
        pills.append({"bg": CHIP_BG_UNKNOWN, "fg": CHIP_FG_UNKNOWN, "text": _fmt_pct(s)})
    return pills


def _drum_pills(device):
    """Chips de la colonne Tambour : une chip par couleur couverte."""
    pills = []
    for s in device.supplies:
        if getattr(s, "category", "") != "drum":
            continue
        pct = _fmt_pct(s)
        try:
            colors = s.drum_colors()
        except Exception:
            colors = []
        for c in colors or []:
            bg = TONER_COLOR_VALUES.get(c)
            if bg is not None:
                pills.append({"bg": bg, "fg": _contrast_fg(bg), "text": pct})
            else:
                pills.append({"bg": CHIP_BG_UNKNOWN, "fg": CHIP_FG_UNKNOWN, "text": pct})
    return pills


def _status_style(device):
    """Style de la chip Statut : cohérent avec les couleurs de ligne existantes
    (reachability prioritaire, puis erreur/down, sinon OK vert doux)."""
    raw = (getattr(device, "status", "") or "").strip()
    reach = getattr(device, "reachability", "")
    if reach == "offline":
        return ROW_OFFLINE, "#334155"
    if reach == "snmp_timeout":
        return ROW_TIMEOUT, "#7c2d12"
    if getattr(device, "error", None):
        return ROW_ERROR, CHIP_FG_CRIT
    if raw.lower() == "down":
        return CHIP_BG_DOWN, CHIP_FG_DOWN
    return CHIP_BG_OK, CHIP_FG_OK


def chips_for_device(device, app=None):
    """Map colonne -> [chip, ...] pour un device donné.

    Une liste vide signifie « pas de chip » : la cellule du Treeview reste
    telle quelle. Colonnes couvertes : toner, drum, bottle, staples, status.
    """
    pills_by_col = {
        "toner": _toner_pills(device, app),
        "drum": _drum_pills(device),
        "status": [],
    }

    # ── Chip statut ──
    raw_status = (getattr(device, "status", "") or "").strip()
    bg, fg = _status_style(device)
    pills_by_col["status"] = [{"bg": bg, "fg": fg, "text": raw_status or "?"}]

    # ── Chips bouteille & agrafes : via device.get_bottle_status / get_staples_status ──
    def _state_chip(status_text, ok_bg=CHIP_BG_OK, ok_fg=CHIP_FG_OK,
                    warn_bg=CHIP_BG_WARN, warn_fg=CHIP_FG_WARN,
                    crit_bg=CHIP_BG_CRIT, crit_fg=CHIP_FG_CRIT):
        t = str(status_text)
        low = t.lower()
        if t == "None" or not t.strip():
            return None   # pas d'agrafeuse / bouteille -> pas de chip
        if low == "ok":
            return {"bg": ok_bg, "fg": ok_fg, "text": t}
        if low.startswith("alert") or low.startswith("warn"):
            return {"bg": warn_bg, "fg": warn_fg, "text": f"{t}"}
        if low.startswith("pleine") or low.startswith("full"):
            return {"bg": crit_bg, "fg": crit_fg, "text": t}
        # niveaux numériques ou texte libre (« Niveau inconnu » …) : gris neutre
        return {"bg": UNKNOWN_BG, "fg": UNKNOWN_FG, "text": t}

    try:
        chip = _state_chip(device.get_bottle_status(app.waste_toner_threshold.get()))
        pills_by_col["bottle"] = [chip] if chip else []
    except Exception:
        pills_by_col["bottle"] = []

    try:
        chip = _state_chip(device.get_staples_status(10))
        pills_by_col["staples"] = [chip] if chip else []
    except Exception:
        pills_by_col["staples"] = []

    return pills_by_col

# === PART3_MARKER ===

import tkinter as tk

from .theme import PRIMARY_SOFT, SURFACE

# Colonnes du tableau qui affichent des chips (noms logiques de app.columns)
CHIP_COLUMNS = ("status", "toner", "drum", "bottle", "staples")

# Ordre de priorité des couleurs de ligne (le premier tag trouvé gagne ;
# « device_down » doit refléter la couleur courante du clignotement).
_ROW_TAG_PRIORITY = (
    "device_down",
    "error",
    "snmp_timeout",
    "snmp_offline",
    "device_critical",
    "device_low",
)

_CHIP_FONT = ("Segoe UI", 8)


# ── Planification des chips : logique PURE, sans widget ─────────────────────
# Cette logique a été extraite de ChipRenderer pour être partagée avec la
# table virtualisée (printer_monitor/vtable.py), qui dessine les pastilles
# DIRECTEMENT dans les cellules du canevas (plus aucun overlay de widgets).
# Les fonctions ci-dessous ne dépendent que d'une police Tk (mesure de texte) :
# aucune création de widget, donc utilisables pendant un rendu de canevas.

# Densités d'espacement essayées successivement : (nom du mode, padx
# horizontal des chips, gap entre chips). Les DEUX restent lisibles : la
# variante compacte ne descend jamais sous pad 3 / gap 3. L'ancien
# « compact » (pad 2 / gap 1) collait les pastilles entre elles (« 5% » et
# « +3 » fusionnés) ; il a été remplacé. Quand même la densité compacte ne
# suffit pas, le layout passe à la ligne (voir plan_chips) au lieu de
# resserrer davantage ou de tronquer.
DENSITIES = (
    ("normal", 6, 4),
    ("compact", 3, 3),
)

_FONT_OBJ = None
_MEASURE_CACHE = {}
_LINE_HEIGHT = None


def _chip_font():
    """Police Tk des chips (mise en cache, recréée si l'interpréteur change)."""
    global _FONT_OBJ
    if _FONT_OBJ is None:
        import tkinter.font as tkfont

        _FONT_OBJ = tkfont.Font(family="Segoe UI", size=8)
    return _FONT_OBJ


def chip_text_px(text):
    """Largeur en pixels du texte rendu dans la police des chips."""
    global _FONT_OBJ
    try:
        return _chip_font().measure(text)
    except Exception:
        # Nouveau root Tk (relance de l'app dans le même process) : la police
        # en cache appartient à l'interpréteur détruit.
        _FONT_OBJ = None
        return chip_text_px(text)


def chip_width(text, pad_x):
    """Largeur RÉELLE rendue d'une chip (px) : texte + padding horizontal
    (2×pad_x) + bordure de focus (2 px). Sert au calcul exact du placement :
    mesure et rendu partagent le même modèle, il n'y a plus d'écart
    d'estimation entre le centrage calculé et le centrage affiché."""
    key = (text, pad_x)
    cached = _MEASURE_CACHE.get(key)
    if cached is not None:
        return cached
    width = chip_text_px(text) + 2 * pad_x + 2
    if len(_MEASURE_CACHE) < 4096:
        _MEASURE_CACHE[key] = width
    return width


def chip_line_height():
    """Hauteur RÉELLE (px) d'une ligne de pastille : hauteur de ligne de la
    police + padding vertical (pady 1 × 2) + bordure de focus (1 px × 2), soit
    exactement la géométrie de l'ancien libellé (tk.Label) et du rectangle
    dessiné dans le canevas de la table virtualisée."""
    global _LINE_HEIGHT, _FONT_OBJ
    if _LINE_HEIGHT is not None:
        return _LINE_HEIGHT
    try:
        _LINE_HEIGHT = max(10, int(_chip_font().metrics("linespace")) + 4)
    except Exception:
        _FONT_OBJ = None
        return 17
    return _LINE_HEIGHT

def wrap_pills(items, widths, avail, gap):
    """Répartit ``items`` en lignes : chaque ligne reçoit le nombre maximal de
    pastilles ENTIÈRES qu'elle peut contenir, sans jamais tronquer ni
    chevaucher. Renvoie une liste de lignes sous forme
    ``{"items": [(pill, x_relatif), ...], "total": largeur_ligne}``."""
    lines, cx, cur = [], 0, []
    for it, w in zip(items, widths):
        if cur and cx + gap + w > avail:
            lines.append({"items": cur, "total": max(0, cx - gap)})
            cur, cx = [], 0
        cur.append((it, cx))
        cx += w + gap
    if cur:
        lines.append({"items": cur, "total": max(0, cx - gap)})
    return lines


def fallback_lines(pills, avail, pad_x, gap, max_lines, line_h, line_gap):
    """Ne conserve que le plus grand nombre de pastilles ENTIÈRES tenant sur
    ``max_lines`` lignes, puis résume les dernières par une chip grise « +N ».
    Chaque pastille conservée reste complète et l'espacement du mode compact
    lisible est préservé (jamais de fusion « 5%+3 »)."""
    n = len(pills)
    # On veut le PLUS GRAND préfixe de pastilles qui reste (i) entier dans la
    # largeur de la cellule, et (ii) empilable en max_lines lignes. On parcourt
    # donc les « drop » croissants ; le premier qui rend le groupe lisible
    # (largeur tenue) est retenu. Cela garantit qu'aucune pastille n'est
    # tronquée et que rien ne déborde — pas de « 5% +3 » collé.
    for drop in range(0, n):
        items = list(pills[: n - drop])
        if drop:
            items.append({"bg": UNKNOWN_BG, "fg": UNKNOWN_FG, "text": f"+{drop}"})
        widths = [chip_width(it["text"], pad_x) for it in items]
        lines = wrap_pills(items, widths, avail, gap)
        total_w = max(l["total"] for l in lines) or 0
        if len(lines) <= max_lines and total_w <= avail:
            return {"mode": "fallback", "pad_x": pad_x, "gap": gap,
                    "lines": lines, "total": total_w,
                    "line_h": line_h, "line_gap": line_gap}
    # Aucune combinaison ne tient horizontalement : on retient le plus grand
    # préfixe qui tient verticalement (max_lines) — chaque pastille restant
    # complète, jamais tronquée.
    for drop in range(0, n):
        items = list(pills[: n - drop])
        if drop:
            items.append({"bg": UNKNOWN_BG, "fg": UNKNOWN_FG, "text": f"+{drop}"})
        widths = [chip_width(it["text"], pad_x) for it in items]
        lines = wrap_pills(items, widths, avail, gap)
        if len(lines) <= max_lines:
            total_w = max(l["total"] for l in lines) or 0
            return {"mode": "fallback", "pad_x": pad_x, "gap": gap,
                    "lines": lines, "total": total_w,
                    "line_h": line_h, "line_gap": line_gap}
    # Filet de sécurité : au moins une pastille tenue (texte entier, même si
    # elle déborde d'un pixel rarement).
    w0 = chip_width(pills[0]["text"], pad_x)
    lines = [{"items": [(pills[0], 0)], "total": w0}]
    return {"mode": "fallback", "pad_x": pad_x, "gap": gap,
            "lines": lines, "total": w0,
            "line_h": line_h, "line_gap": line_gap}


def plan_chips(pills, avail, height=None):
    """Calcule le placement responsive des pastilles dans ``avail`` px de
    largeur et jusqu'à ``height`` px de hauteur de cellule.

    Stratégie IDENTIQUE pour toutes les colonnes à chips (statut,
    toners/cartouches, tambours/photoconducteurs, bouteille, agrafes) :

      1. « normal » : tout sur une seule ligne, espacement confortable ;
      2. « compact » : tout sur une seule ligne, espacement réduit mais
         TOUJOURS lisible (jamais moins de pad 3 / gap 3) ;
      3. « wrap » : retour à la ligne tant que la hauteur le permet — on
         conserve TOUTES les pastilles ENTIÈRES (aucune troncature) ;
      4. « fallback » : résumé « +N » lisible, uniquement si même le retour à
         la ligne ne suffit pas.

    Renvoie ``{"lines": [{"items": [(pill, x_relatif), ...], "total"}],
    "total": total_w, "line_h", "line_gap"}``.
    """
    if not pills:
        return {"mode": "empty", "pad_x": DENSITIES[0][1],
                "gap": DENSITIES[0][2], "lines": [], "total": 0,
                "line_h": 1, "line_gap": 0}

    line_h = chip_line_height()
    line_gap = 3
    h = int(height) if height and height > 0 else line_h
    vroom = max(line_h, h) - 2
    # Nombre de lignes de pastilles réellement accueillies verticalement.
    max_lines = max(1, min(2, (vroom + line_gap) // (line_h + line_gap)))

    # ── 1 & 2 : tout sur une seule ligne, pastilles jamais coupées.
    for name, pad_x, gap in DENSITIES:
        widths = [chip_width(p["text"], pad_x) for p in pills]
        total = sum(widths) + gap * (len(widths) - 1)
        if total <= avail:
            items, cx = [], 0
            for pill, w in zip(pills, widths):
                items.append((pill, cx))
                cx += w + gap
            return {"mode": name, "pad_x": pad_x, "gap": gap,
                    "lines": [{"items": items, "total": total}],
                    "total": total, "line_h": line_h, "line_gap": 0}

    # ── 3 : retour à la ligne —— on garde TOUTES les pastilles.
    name, pad_x, gap = DENSITIES[-1]
    widths = [chip_width(p["text"], pad_x) for p in pills]
    lines = wrap_pills(pills, widths, avail, gap)
    if len(lines) <= max_lines:
        total_w = max(l["total"] for l in lines) or 0
        return {"mode": "wrap", "pad_x": pad_x, "gap": gap,
                "lines": lines, "total": total_w,
                "line_h": line_h, "line_gap": line_gap}

    # ── 4 : dernier recours — résumé « +N » toujours lisible.
    return fallback_lines(pills, avail, pad_x, gap,
                          max_lines, line_h, line_gap)

class ChipRenderer:
    """Superpose de vraies chips colorées aux cellules du Treeview.

    Pour chaque ligne **visible** et chaque colonne à chip, un ``tk.Frame``
    opaque est placé (``place(in_=tree)``) exactement sur la cellule : il masque
    le texte brut du Treeview et affiche les pastilles (fond/texte/bordure).
    Les frames sont réutilisés entre deux rendus pour rester léger ; les
    événements souris sont retransmis au Treeview avec les bonnes coordonnées,
    afin que sélection, détails, menu contextuel et molette continuent de
    fonctionner au-dessus des chips.
    """

    def __init__(self, app):
        self.app = app
        self.tree = app.tree
        # Overlays actifs par clé (item_id, column_name) et pool de frames libres
        self._active = {}
        self._free = []
        self._redraw_after = None
        # Sélection mise en cache le temps d'une passe de rendu : évite
        # d'appeler tree.selection() une fois par cellule à chaque redessin.
        self._selection_cache = None
        # Hauteur réelle mesurée d'une ligne de pastille (mise en cache).
        self._probe_h = None
        self._install_bindings()

    # ── Public ────────────────────────────────────────────────────────────
    def schedule_redraw(self):
        """Redessin débounce : plusieurs sources peuvent demander un rendu."""
        if self._redraw_after is not None:
            return
        self._redraw_after = self.app.root.after(30, self._do_redraw)

    def redraw(self):
        """Redessin immédiat."""
        if self._redraw_after is not None:
            try:
                self.app.root.after_cancel(self._redraw_after)
            except Exception:
                pass
            self._redraw_after = None
        self._do_redraw()

    def clear(self):
        """Libère tous les overlays (liste vidée / filtre changé)."""
        for key in list(self._active):
            self._release(key)

    def repaint_row_backgrounds(self):
        """Recolore uniquement les fonds des overlays actifs (clignotement des
        imprimantes « down »). Ni le contenu ni la géométrie ne sont
        recalculés : quelques ``configure()`` au lieu d'un parcours complet.
        Cela rend le clignotement quasi gratuit même avec des centaines de
        lignes, là où un ``redraw()`` complet était déclenché toutes les
        500 ms."""
        if not self.tree.winfo_exists():
            return
        self._selection_cache = None
        for key, entry in list(self._active.items()):
            item = key[0]
            try:
                bg = self._row_background(item)
            except Exception:
                continue
            if entry.get("row_bg") == bg:
                continue
            entry["row_bg"] = bg
            try:
                entry["frame"].configure(background=bg)
            except Exception:
                pass
            if entry.get("inner") is not None:
                try:
                    entry["inner"].configure(background=bg)
                except Exception:
                    pass

# === PART4_MARKER ===

    # ── Interne : placement ───────────────────────────────────────────────
    def _do_redraw(self):
        self._redraw_after = None
        tree = self.tree
        if not tree.winfo_exists():
            return
        try:
            col_num = {name: f"#{i + 1}" for i, name in enumerate(self.app.columns)}
        except Exception:
            return
        # Synchronise la géométrie Tk en attente (insertions/suppressions de
        # lignes, changements de largeur de colonnes, mise à jour de valeurs)
        # AVANT de mesurer les bboxes. Sans ce flush, les overlays étaient
        # calculés sur des coordonnées intermédiaires : ils restaient décalés
        # jusqu'à ce qu'un défilement ou une sélection force une recomposition
        # complète (les « bugs d'affichage corrigés en faisant défiler »).
        try:
            tree.update_idletasks()
        except Exception:
            pass
        self._selection_cache = None

        # 1ʳᵉ passe : collecte des cellules visibles et de leurs chips, sans
        # encore toucher aux overlays. Cela permet de libérer les overlays
        # obsolètes AVANT d'acquérir les nouveaux : leurs frames retournent
        # dans le pool et sont immédiatement réutilisés (sinon une seconde
        # vague de frames était créée à chaque rafraîchissement complet de la
        # liste, doublant transitoirement le pool).
        wanted = {}
        device_items = self.app.device_items
        for item in tree.get_children():
            device = device_items.get(item)
            if device is None:
                continue
            # D'abord la visibilité (bbox, quasi gratuite), puis seulement le
            # calcul des chips : le coût devient proportionnel au nombre de
            # lignes VISIBLES, plus au nombre total d'imprimantes.
            bboxes = {}
            for col_name in CHIP_COLUMNS:
                num = col_num.get(col_name)
                if num is None:
                    continue
                bbox = tree.bbox(item, num)
                if not bbox:
                    continue  # ligne hors zone visible
                bboxes[col_name] = bbox
            if not bboxes:
                continue
            try:
                pills_by_col = chips_for_device(device, self.app)
            except Exception:
                continue
            for col_name, bbox in bboxes.items():
                pills = pills_by_col.get(col_name) or []
                if not pills:
                    continue
                wanted[(item, col_name)] = (bbox, pills)

        # 2ᵉ passe : libère les overlays devenus inutiles (ligne cachée,
        # item supprimé, plus de chip…) — AVANT toute acquisition.
        for key in list(self._active):
            if key not in wanted:
                self._release(key)

        # 3ᵉ passe : place/rafraîchit les overlays wanted (réutilisant les
        # frames fraîchement libérés).
        for (item, col_name), (bbox, pills) in wanted.items():
            self._ensure_overlay(item, col_name, bbox, pills)

    def _row_background(self, item):
        """Fond de la ligne pour harmoniser l'overlay (alertes/sélection).

        La sélection est mise en cache pour toute la passe de rendu :
        ``tree.selection()`` est appelé une seule fois par redessin au lieu
        d'une fois par cellule (gain notable lors du clignotement)."""
        tree = self.tree
        try:
            if self._selection_cache is None:
                self._selection_cache = set(tree.selection())
            if item in self._selection_cache:
                return PRIMARY_SOFT
            tags = tree.item(item, "tags")
        except Exception:
            tags = []
        for tag in _ROW_TAG_PRIORITY:
            if tag in tags:
                bg = str(tree.tag_configure(tag, "background") or "")
                if bg:
                    return bg
        return SURFACE

# === PART5_MARKER ===

    _MOUSE_SEQS = (
        "<Button-1>", "<Double-Button-1>", "<ButtonRelease-1>",
        "<Button-3>", "<ButtonRelease-3>", "<MouseWheel>",
        "<Shift-MouseWheel>", "<B1-Motion>",
    )

    def _acquire_frame(self):
        if self._free:
            return self._free.pop()
        # Conteneur de cellule : fond opaque harmonisé à la ligne, sans relief
        frame = tk.Frame(
            self.tree,
            bd=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._bind_forward(frame)
        return frame

    def _bind_forward(self, widget):
        """Retransmet tous les événements souris au Treeview."""
        for seq in self._MOUSE_SEQS:
            widget.bind(seq, lambda e, s=seq: self._forward(e, s))

    def _make_pill_label(self, parent, pill, pad_x=6):
        lbl = tk.Label(
            parent,
            text=pill["text"],
            background=pill["bg"],
            foreground=pill["fg"],
            font=_CHIP_FONT,
            padx=pad_x,
            pady=1,
            bd=0,
            highlightthickness=1,
            highlightbackground=CHIP_BORDER,
            highlightcolor=CHIP_BORDER,
            takefocus=False,
        )
        self._bind_forward(lbl)
        return lbl

    def _ensure_overlay(self, item, col_name, bbox, pills):
        key = (item, col_name)
        entry = self._active.get(key)
        if entry is None:
            frame = self._acquire_frame()
            entry = {"frame": frame, "inner": None, "pills": [],
                     "sig": None, "row_bg": None}
            self._active[key] = entry
        frame = entry["frame"]

        x, y, w, h = bbox
        row_bg = self._row_background(item)
        if entry.get("row_bg") != row_bg:
            # Reconfigure uniquement quand la couleur change (clignotement,
            # sélection) : évite un configure() par cellule à chaque tick.
            frame.configure(background=row_bg)
            if entry["inner"] is not None:
                entry["inner"].configure(background=row_bg)
            entry["row_bg"] = row_bg
        frame.place(in_=self.tree, x=x, y=y, width=w, height=h, anchor="nw")

        # ── Placement responsive des pastilles dans la cellule ───────────────
        # Le planneur renvoie une ou plusieurs LIGNES de pastilles, chacune
        # recentrée horiz. dans la largeur de la ligne la plus large. Rien
        # n'est jamais tronqué : si une seule ligne ne tient pas, on passe à
        # la ligne suivante (dans la hauteur disponible) au lieu de coller ou
        # de troncater (plus de « 5% +3 »). Le groupe entier est centré dans
        # la cellule — horiz. et verticalement — comme le texte Treeview.
        avail = max(0, int(w) - 4)
        plan = self._plan_chips(pills, avail, h)
        lines = plan["lines"]
        total_w = plan["total"]            # largeur de la ligne la plus large
        line_h = plan["line_h"]
        line_gap = plan["line_gap"]
        n_lines = len(lines)

        x_off = 2 + ((avail - total_w) // 2 if total_w <= avail else 0)
        block_h = n_lines * line_h + line_gap * max(0, n_lines - 1)
        y0 = max(0, (int(h) - block_h) // 2)

        sig = (avail, h, plan["mode"], n_lines, plan["pad_x"]) + tuple(
            (p["bg"], p["fg"], p["text"])
            for line in lines for p, _rx in line["items"]
        )
        if entry.get("sig") == sig:
            return
        # Reconstruit UNIQUEMENT quand le contenu, la largeur, la hauteur ou
        # la densité ont réellement changé. L'ancien conteneur entier est
        # détruit : les labels partent avec lui, éliminant les fuites de
        # frames invisibles des versions précédentes.
        if entry["inner"] is not None:
            try:
                entry["inner"].destroy()
            except Exception:
                pass
        entry["pills"] = []
        entry["sig"] = sig
        inner = tk.Frame(frame, background=row_bg, bd=0,
                         highlightthickness=0, takefocus=False)
        # Largeur = largeur de la ligne la plus large ; hauteur = pleine
        # cellule pour laisser la marge verticale de centrage. Les labels sont
        # placés en coordonnées absolues (place ne propage pas la taille au
        # parent), d'où la dimension explicite de ``inner``.
        inner.place(x=x_off, y=0, width=int(total_w) if total_w else 1,
                    height=int(h), anchor="nw")
        entry["inner"] = inner
        labels = []
        for li, line in enumerate(lines):
            line_items = line["items"]
            line_w = line["total"]
            lx = (max(0, total_w - line_w)) // 2  # centre la ligne dans le bloc
            y_line = y0 + li * (line_h + line_gap)
            for p, rel_x in line_items:
                lbl = self._make_pill_label(inner, p, plan["pad_x"])
                lbl.place(x=int(lx + rel_x), y=int(y_line), anchor="nw")
                labels.append(lbl)
        entry["pills"] = labels

    # ── Ajustement des chips à la largeur disponible ──────────────────────
    # La logique de planification (densités, mesure, retour à la ligne,
    # résumé « +N ») est PURE et vit désormais au niveau module (plan_chips,
    # chip_width, chip_line_height, wrap_pills, fallback_lines) : elle est
    # partagée avec la table virtualisée (vtable.py). Les méthodes ci-dessous
    # ne sont plus qu'une façade compatible pour le code existant.
    _DENSITIES = DENSITIES

    def _text_px(self, text):
        """Largeur en pixels du texte rendu dans la police des chips."""
        return chip_text_px(text)

    def _chip_width(self, text, pad_x):
        """Largeur RÉELLE rendue d'une chip (px) : texte + padding horizontal
        (2×pad_x) + bordure de focus (2 px)."""
        return chip_width(text, pad_x)

    def _chip_line_height(self):
        """Hauteur RÉELLE (px) d'une ligne de pastille (voir chip_line_height)."""
        return chip_line_height()

    def _wrap(self, items, widths, avail, gap):
        """Répartit ``items`` en lignes de pastilles ENTIÈRES (voir wrap_pills)."""
        return wrap_pills(items, widths, avail, gap)

    def _plan_chips(self, pills, avail, height=None):
        """Calcule le placement responsive des pastilles (voir plan_chips).

        Stratégie IDENTIQUE pour toutes les colonnes à chips (statut,
        toners/cartouches, tambours/photoconducteurs, bouteille, agrafes) :
        normal → compact → retour à la ligne → résumé « +N ».
        """
        return plan_chips(pills, avail, height)

    def _fallback_line(self, pills, avail, pad_x, gap, max_lines,
                       line_h, line_gap):
        """Résumé « +N » lisible (voir fallback_lines)."""
        return fallback_lines(pills, avail, pad_x, gap,
                              max_lines, line_h, line_gap)

    def _release(self, key):
        entry = self._active.pop(key, None)
        if entry is None:
            return
        frame = entry["frame"]
        try:
            frame.place_forget()
        except Exception:
            pass
        # Détruit le conteneur de pastilles AVANT la remise en pool : sans
        # cela il réapparaissait dès la prochaine réutilisation du frame
        # (accumulation d'anciennes chips derrière les nouvelles).
        inner = entry.get("inner")
        if inner is not None:
            try:
                inner.destroy()
            except Exception:
                pass
        self._free.append(frame)

# === PART6_MARKER ===

    # ── Interne : événements ──────────────────────────────────────────────
    def _forward(self, event, seq):
        """Retransmet l'évènement au Treeview en convertissant les coordonnées
        (l'overlay est placé dans le repère du Treeview via ``in_=``)."""
        try:
            ox = event.widget.winfo_rootx() - self.tree.winfo_rootx()
            oy = event.widget.winfo_rooty() - self.tree.winfo_rooty()
        except Exception:
            return "break"
        kwargs = {}
        if getattr(event, "delta", 0):
            kwargs["delta"] = event.delta
        self.tree.event_generate(seq, x=event.x + ox, y=event.y + oy, **kwargs)
        if seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<B1-Motion>"):
            # Défilement / glissement : les lignes visibles changent
            self.schedule_redraw()
        return "break"

    def _install_bindings(self):
        """Recale les overlays dès que la vue du Treeview change.

        La synchronisation s'appuie sur les callbacks *scrollcommand* : Tk les
        invoque à CHAQUE mouvement de vue (molette, clavier, traction des
        barres, appel programmatique). C'est fiable là où l'ancienne approche
        par bindings souris (root MouseWheel + scrollbars) laissait des
        overlays à une position obsolète — le texte du Treeview devenait alors
        visible à côté des chips (« affichage doublé »)."""
        tree = self.tree
        tree.bind("<Configure>", lambda e: self.schedule_redraw(), add="+")
        for opt in ("xscrollcommand", "yscrollcommand"):
            try:
                original = tree.cget(opt)
                if not original:
                    continue

                def make_handler(orig_cb):
                    def handler(first, last):
                        self.schedule_redraw()
                        try:
                            tree.tk.call(orig_cb, first, last)
                        except Exception:
                            pass
                    return handler

                tree.configure(opt=make_handler(original))
            except Exception:
                pass
        # Filet de sécurité limité au tableau (plus de binding global racine,
        # qui captait la molette de tous les dialogues pour rien).
        tree.bind("<MouseWheel>", lambda e: self.schedule_redraw(), add="+")
