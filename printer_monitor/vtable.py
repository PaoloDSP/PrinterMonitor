"""Table virtualisée à rendu direct (sans overlay de widgets).

Remplace le couple « ``ttk.Treeview`` + surcouche de ``tk.Frame``/``tk.Label`` »
utilisé jusqu'ici pour afficher les chips (``printer_monitor/chips.py``).

Principe
--------
Un **unique** ``tk.Canvas`` porte le tableau entier :

* l'en-tête est dessiné dans le canevas et reste ancré en haut (il est recalé
  à l'offset vertical courant de la vue à chaque rendu) ;
* seules les lignes **visibles** existent sous forme d'éléments de canevas
  (rectangles + textes + pastilles) ;
* ces éléments sont **recyclés** : un même « slot » de rendu représente la
  ligne 50, puis après défilement la ligne 90, puis la ligne 130 — sans
  ``destroy()`` / ``create()``, uniquement ``coords()`` / ``itemconfigure()`` ;
* les chips sont dessinées **directement dans la cellule** (rectangle bordé +
  texte), à la place exacte qu'occupait l'overlay ; aucun widget superposé,
  donc plus aucune synchronisation à maintenir.

Le défilement du canevas est natif : le canevas translate lui-même les
éléments déjà placés (coordonnées *absolues*), ce qui rend un défilement d'une
ligne quasi gratuit (seul le slot entrant est repeint).

Le tableau est un **remplacement technique** de ``ttk.Treeview`` : il expose la
même API que celle réellement utilisée par l'application (``insert``, ``item``,
``delete``, ``move``, ``selection``, ``focus``, ``column``, ``heading``,
``tag_configure``, ``identify_*``, ``bbox``, ``yview``/``xview``, ``bind``,
``event_generate``…) : aucun appel n'a besoin de changer et aucun comportement
utilisateur n'est modifié.

Ce module est **une couche d'affichage** : les données, la sélection, les tags
et les actions restent la source de vérité ; un slot graphique n'est qu'une vue
temporaire d'une ligne.
"""

import tkinter as tk
import tkinter.font as tkfont

from .chips import CHIP_BORDER, CHIP_COLUMNS, chip_width, plan_chips
from .theme import BORDER, FONT_FAMILY, MUTED, PRIMARY_SOFT, SURFACE, TEXT

# Hauteur de ligne : identique au ``rowheight`` du thème (theme.py) afin de
# conserver la densité visuelle actuelle (deux lignes de pastilles par cellule).
DEFAULT_ROW_HEIGHT = 40

# Marge interne horizontale d'une cellule.
CELL_PAD = 6

# Demi-largeur de la zone sensible d'un séparateur de colonnes (px), en-tête.
SEP_GRAB = 4

# Délai avant affichage de l'infobulle (ms) : la valeur complète d'une cellule
# tronquée reste consultable, comme avec le ttk.Treeview natif.
TOOLTIP_DELAY = 600

_FIT_CACHE = {}


def _cache_put(cache, key, value):
    if len(cache) < 8192:
        cache[key] = value


def _fit_text(font, text, avail):
    """Tronque ``text`` avec « … » pour tenir dans ``avail`` px.

    La valeur COMPLÈTE n'est jamais perdue : elle reste dans le modèle (et est
    proposée en infobulle). Renvoie ``(texte_affiche, tronque)``.
    """
    if avail <= 0:
        return "", bool(text)
    key = (id(font), text, avail)
    cached = _FIT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        width = font.measure(text)
    except Exception:
        return text, False
    if width <= avail:
        _cache_put(_FIT_CACHE, key, (text, False))
        return text, False
    ell = "…"
    ell_w = font.measure(ell)
    if ell_w > avail:
        _cache_put(_FIT_CACHE, key, ("", True))
        return "", True
    # Dichotomie : plus long préfixe tenant avec le « … » final.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) + ell_w <= avail:
            lo = mid
        else:
            hi = mid - 1
    result = (text[:lo].rstrip() + ell, True)
    _cache_put(_FIT_CACHE, key, result)
    return result


def _class_dispatch(method_name):
    """Répartit un binding de classe vers la MÉTHODE de l'instance visée.

    Le tag « VirtualTable » est partagé par toutes les instances : le
    gestionnaire doit retrouver la table réellement concernée par l'événement
    (``event.widget``) au lieu de capturer une instance.
    """
    def handler(event):
        widget = getattr(event, "widget", None)
        fn = getattr(widget, method_name, None)
        if callable(fn):
            return fn(event)
        return None

    return handler


class VirtualTable(tk.Canvas):
    """Table virtualisée (en-tête, cellules et chips dessinés dans un canevas).

    API compatible avec l'usage fait de ``ttk.Treeview`` par l'application
    (``show="headings"``, ``selectmode="browse"``).
    """

    def __init__(self, master, columns=(), show="headings", selectmode="browse",
                 rowheight=DEFAULT_ROW_HEIGHT, chip_columns=None, **kw):
        kw.setdefault("background", SURFACE)
        kw.setdefault("borderwidth", 0)
        # Bordure fine identique à celle du Treeview (bordercolor du thème).
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", BORDER)
        kw.setdefault("highlightcolor", BORDER)
        # Le tableau est accessible au clavier (Tab) et prend le focus au clic,
        # comme ttk.Treeview.
        kw.setdefault("takefocus", True)
        super().__init__(master, **kw)

        self.columns = [str(c) for c in columns]
        self._show = show
        self._selectmode = selectmode
        self._row_h = max(16, int(rowheight))

        # ── Modèle de colonnes ──────────────────────────────────────────
        self._col_cfg = {}
        for name in self.columns:
            self._col_cfg[name] = {
                "width": 140, "minwidth": 40, "stretch": False,
                "anchor": "center", "text": name, "command": None,
                "hanchor": "center",
            }

        # ── Modèle de données (source de vérité) ────────────────────────
        self._rows = []          # ordre d'affichage : [iid, ...]
        self._data = {}          # iid -> {"values": tuple, "tags": tuple}
        self._index = {}         # iid -> position
        self._index_ok = True
        self._pending_moves = {}
        self._iid_seq = 0

        # ── État logique de sélection (jamais porté par le graphique) ────
        self._selection = ()
        self._focus_iid = ""
        # Contenu remplace en bloc (delete+insert massif : changement de Grand
        # site, filtre, CMDB). Positionne par delete()/insert() et consomme
        # par _render_rows() : force la repeinture complete des lignes
        # visibles (les index ont change de sens, le recyclage aveugle
        # conserverait des slots obsoletes → trous + affichage fige).
        self._rows_replaced = False
        # Dernier appui simple (sans modificateur) : sélection AVANT le clic,
        # mémorisée par _on_press pour la détection du second clic (désélection
        # gérée par l'application au relâchement).
        self._press_row = None
        self._press_selection = None

        # ── Tags (fond / texte des familles de lignes) ──────────────────
        self._tag_cfg = {}

        # ── Polices et géométrie ────────────────────────────────────────
        self._cell_font = tkfont.Font(family=FONT_FAMILY, size=9)
        self._head_font = tkfont.Font(family=FONT_FAMILY, size=9, weight="bold")
        # Police des chips : identique à celle de l'ancien overlay.
        self._chip_font = tkfont.Font(family=FONT_FAMILY, size=8)
        # Hauteur d'en-tête = ligne de texte + padding vertical (5 px × 2) +
        # bordures, soit la géométrie de ttk::treeview avec le thème de l'app.
        self._header_h = int(self._head_font.metrics("linespace")) + 12

        # ── Rendu ───────────────────────────────────────────────────────
        self._slots = []         # slots de lignes recyclables
        self._row_slot = {}      # index de ligne -> slot
        self._header_items = None
        self._x_layout = {}      # colonne -> (x, largeur)
        self._total_w = 0
        self._render_after = None
        self._all_dirty = True
        self._scrollregion_dirty = True
        self._chip_provider = None
        self._chip_columns = tuple(chip_columns or CHIP_COLUMNS)
        self._tooltip = None
        self._tooltip_after = None
        self._tooltip_pos = None
        self._drag = None

        # Tag de bindings interne : les bindings de l'application (posés sur le
        # widget par ``bind``) restent prioritaires, puis les comportements par
        # défaut du tableau s'enchaînent — exactement comme ttk::treeview
        # (bindtags = widget, classe, toplevel, all).
        self.bindtags((str(self), "VirtualTable",
                       str(self.winfo_toplevel()), "all"))
        self._install_class_bindings()

        self._rebuild_layout()
        self._sync_scrollregion()
        self.bind("<Configure>", self._on_configure, add="+")

    # ══════════════════════════════════════════════════════════════════════
    # Colonnes / en-tête / tags
    # ══════════════════════════════════════════════════════════════════════
    def _col(self, col):
        try:
            return self._col_cfg[col]
        except KeyError:
            raise tk.TclError(f'Column "{col}" not found') from None

    def column(self, col, option=None, **kw):
        """``column(col, width=…)`` (pose) ou ``column(col, "width")`` (lecture)."""
        cfg = self._col(col)
        if kw:
            changed = False
            for key, value in kw.items():
                if key == "width":
                    # ttk.Treeview borne la largeur demandée à ``minwidth`` au
                    # moment de la pose (mais un changement de ``minwidth`` ne
                    # modifie pas rétroactivement la largeur).
                    value = max(1, int(float(value)))
                    value = max(value, int(cfg.get("minwidth", 0)))
                elif key == "minwidth":
                    value = max(0, int(float(value)))
                elif key == "stretch":
                    value = str(value) not in ("0", "false", "False", "")
                if cfg.get(key) != value:
                    cfg[key] = value
                    changed = True
            if changed:
                self._on_layout_changed()
            return None
        if option:
            return cfg.get(option)
        return {
            "width": cfg["width"], "minwidth": cfg["minwidth"],
            "stretch": cfg["stretch"], "anchor": cfg["anchor"], "id": col,
        }

    def heading(self, col, option=None, **kw):
        """``heading(col, text=…, anchor=…, command=…)`` / ``heading(col, "text")``."""
        cfg = self._col(col)
        if kw:
            changed = False
            for key, value in kw.items():
                if cfg.get(key) != value:
                    cfg[key] = value
                    changed = True
            if changed:
                self._invalidate_header()
                self._schedule_render()
            return None
        if option:
            return cfg.get(option)
        return dict(cfg)

    def tag_configure(self, tag, option=None, **kw):
        """Configure (ou lit) le fond / la couleur de texte d'un tag de ligne."""
        tag = str(tag)
        cfg = self._tag_cfg.setdefault(tag, {})
        if kw:
            cfg.update(kw)
            self._all_dirty = True
            self._schedule_render()
            return None
        if option:
            return cfg.get(option, "")
        return dict(cfg)

    # ══════════════════════════════════════════════════════════════════════
    # Données (modèle)
    # ══════════════════════════════════════════════════════════════════════
    def _new_iid(self):
        while True:
            self._iid_seq += 1
            iid = f"I{self._iid_seq:04d}"
            if iid not in self._data:
                return iid

    @staticmethod
    def _stringify(value):
        # ttk.Treeview rend les valeurs sous forme de chaînes (``None`` devient
        # "None") : on reproduit exactement ce comportement (plusieurs logiques
        # métier s'appuient sur la valeur affichée, ex. « None » = pas
        # d'agrafeuse).
        return str(value)

    @staticmethod
    def _as_tuple(value):
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(value)

    def insert(self, parent="", index="end", iid=None, **kw):
        """Insère une ligne (``insert("", "end", values=…, tags=…)``)."""
        self._flush_moves()
        new_iid = str(iid) if iid is not None else self._new_iid()
        if new_iid in self._data:
            raise tk.TclError(f"Item {new_iid} already exists")
        self._data[new_iid] = {
            "values": tuple(self._stringify(v) for v in self._as_tuple(kw.get("values"))),
            "tags": tuple(str(t) for t in self._as_tuple(kw.get("tags"))),
        }
        try:
            pos = int(index)
        except (TypeError, ValueError):
            pos = len(self._rows)
        if pos >= len(self._rows):
            self._rows.append(new_iid)
            if self._index_ok:
                self._index[new_iid] = len(self._rows) - 1
        else:
            self._rows.insert(max(0, pos), new_iid)
            self._reindex()
        self._after_data_change()
        return new_iid

    def delete(self, *items):
        """Supprime une ou plusieurs lignes (le reste du modèle est préservé).

        Exception : le tag d'items CANEVAS ``"vthead"`` (en-tête graphique,
        pas une ligne) est transmis au delete natif de tk.Canvas au lieu
        d'être silencieusement ignoré — sinon l'ancien en-tête restait
        affiché et un second était créé (en-tête fantôme au milieu de la
        liste après changement de Grand site / resize).
        """
        drop = set()
        canvas_tags = []
        for it in items:
            if isinstance(it, (list, tuple)):
                drop.update(str(i) for i in it)
            elif str(it) == "vthead":
                canvas_tags.append(it)
            else:
                drop.add(str(it))
        for tag in canvas_tags:
            try:
                tk.Canvas.delete(self, tag)
            except Exception:
                pass
        found = [iid for iid in drop if iid in self._data]
        if not found:
            if canvas_tags:
                self._after_data_change()
            return
        self._flush_moves()
        for iid in found:
            del self._data[iid]
        self._rows = [iid for iid in self._rows if iid not in drop]
        # Le sens des index change a chaque suppression : les slots recycles
        # portent des index obsoletes. Marque pour repeinture complete au
        # prochain rendu (cout borne aux lignes visibles, pas au volume).
        self._rows_replaced = True
        for slot in self._slots:
            # Les slots ne référencent plus que des lignes existantes.
            if slot["row"] is not None and slot["row"] >= len(self._rows):
                slot["row"] = None
        if self._selection:
            self._selection = tuple(i for i in self._selection if i in self._data)
        self._reindex()
        self._after_data_change()

    def get_children(self, *args):
        """Retourne les lignes dans l'ordre d'affichage courant."""
        self._flush_moves()
        return tuple(self._rows)

    def item(self, iid, option=None, **kw):
        """Lecture (``item(iid, "values"|"tags")``) ou mise à jour d'une ligne."""
        iid = str(iid)
        rec = self._data.get(iid)
        if rec is None:
            raise tk.TclError(f"Item {iid} not found")
        if kw:
            changed = False
            if "values" in kw:
                new_values = tuple(self._stringify(v)
                                   for v in self._as_tuple(kw["values"]))
                if new_values != rec["values"]:
                    rec["values"] = new_values
                    changed = True
            if "tags" in kw:
                new_tags = tuple(str(t) for t in self._as_tuple(kw["tags"]))
                if new_tags != rec["tags"]:
                    rec["tags"] = new_tags
                    changed = True
            if changed:
                self._after_data_change()
            return None
        if option:
            if option == "values":
                return rec["values"]
            if option == "tags":
                return rec["tags"]
            return ""
        return {"values": rec["values"], "tags": rec["tags"]}

    def move(self, iid, parent, index):
        """Déplace une ligne (batching : coût linéaire par passe de rendu)."""
        iid = str(iid)
        if iid not in self._data:
            raise tk.TclError(f"Item {iid} not found")
        try:
            target = int(index)
        except (TypeError, ValueError):
            target = len(self._rows)
        self._pending_moves[iid] = target
        self._after_data_change(flush=False)

    def _flush_moves(self):
        """Applique les ``move()`` en attente en une seule passe linéaire."""
        if not self._pending_moves:
            return
        moves = self._pending_moves
        self._pending_moves = {}
        n = len(self._rows)
        if not n:
            return
        out = [None] * n
        for iid in moves:
            if iid not in self._data:
                continue
            pos = max(0, min(int(moves[iid]), n - 1))
            # Premier emplacement libre à partir de la cible : les cibles d'un
            # même réordonnancement sont cohérentes (elles reproduisent l'ordre
            # attendu), l'ordre final est donc exact.
            while pos < n and out[pos] is not None:
                pos += 1
            if pos >= n:
                pos = next((i for i in range(n) if out[i] is None), 0)
            out[pos] = iid
        remaining = (iid for iid in self._rows if iid not in moves)
        for i in range(n):
            if out[i] is None:
                out[i] = next(remaining, None)
        self._rows = [iid for iid in out if iid is not None]
        self._reindex()

    def _reindex(self):
        self._index = {iid: i for i, iid in enumerate(self._rows)}
        self._index_ok = True

    def _index_of(self, iid):
        if not self._index_ok:
            self._reindex()
        return self._index.get(iid)

    # ══════════════════════════════════════════════════════════════════════
    # Sélection / focus — état LOGIQUE, indépendant du rendu
    # ══════════════════════════════════════════════════════════════════════
    def selection(self):
        return tuple(self._selection)

    def _normalize_items(self, items):
        flat = []
        for it in items:
            if isinstance(it, (list, tuple)):
                flat.extend(it)
            else:
                flat.append(it)
        out = []
        for iid in flat:
            iid = str(iid)
            if iid not in self._data:
                raise tk.TclError(f"Item {iid} not found")
            if iid not in out:
                out.append(iid)
        return out

    def selection_set(self, *items):
        """Définit la sélection (en mode ``browse`` : une seule ligne)."""
        new = self._normalize_items(items)
        if self._selectmode != "extended":
            new = new[:1]
        self._set_selection(new)
        if new:
            self._focus_iid = new[0]

    def selection_add(self, *items):
        new = list(self._selection)
        for iid in self._normalize_items(items):
            if iid not in new:
                new.append(iid)
        if self._selectmode != "extended":
            new = new[-1:]
        self._set_selection(new)

    def selection_remove(self, *items):
        drop = set(self._normalize_items(items))
        self._set_selection([i for i in self._selection if i not in drop])

    def selection_toggle(self, *items):
        for iid in self._normalize_items(items):
            if iid in self._selection:
                self.selection_remove(iid)
            else:
                self.selection_add(iid)

    def selection_clear(self):
        self._set_selection([])

    def focus(self, iid=None):
        """Lit (``focus()``) ou pose (``focus(iid)``) la ligne focus."""
        if iid is None:
            return self._focus_iid
        iid = str(iid)
        if iid not in self._data:
            raise tk.TclError(f"Item {iid} not found")
        self._focus_iid = iid
        return None

    def _set_selection(self, items):
        new = tuple(items)
        if new == self._selection:
            return
        self._selection = new
        self._all_dirty = True
        self._schedule_render()
        # Même événement que ttk.Treeview : l'application y est abonnée pour
        # rafraîchir le panneau de détails. Il est généré avec la MÊME
        # temporisation que ttk (when="tail") : l'événement est traité par la
        # boucle d'événements, pas pendant l'appel — le panneau de détails se
        # met donc à jour exactement comme avant la refonte.
        try:
            self.event_generate("<<TreeviewSelect>>", when="tail")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    # Défilement : le canevas pilote directement la virtualisation
    # ══════════════════════════════════════════════════════════════════════
    def _content_h(self):
        return self._header_h + len(self._rows) * self._row_h

    def _view_h(self):
        try:
            return max(1, int(self.winfo_height()))
        except Exception:
            return 1

    def _view_w(self):
        try:
            return max(1, int(self.winfo_width()))
        except Exception:
            return 1

    def yview(self, *args):
        """Défilement vertical (``yview()`` renvoie les fractions de vue)."""
        self._sync_scrollregion()
        if args and args[0] == "scroll":
            try:
                n = float(args[1])
            except (IndexError, TypeError, ValueError):
                n = 0.0
            mode = args[2] if len(args) > 2 else "units"
            if mode == "units":
                # 1 unité = 1 ligne, comme le Treeview (flèches, molette).
                self._scroll_lines(n)
            else:
                self._scroll_lines(n * max(1, int(self._view_h() // self._row_h) - 1))
            return None
        result = tk.Canvas.yview(self, *args)
        self._on_view_changed()
        return result

    def xview(self, *args):
        """Défilement horizontal (en-tête et cellules restent synchronisés)."""
        self._sync_scrollregion()
        result = tk.Canvas.xview(self, *args)
        self._on_view_changed()
        return result

    def _scroll_lines(self, n):
        if not n:
            return
        cam = self.canvasy(0)
        limit = max(0.0, self._content_h() - self._view_h())
        target = max(0.0, min(cam + n * self._row_h, limit))
        tk.Canvas.yview_moveto(self, target / max(1.0, float(self._content_h())))
        self._on_view_changed()

    def _on_view_changed(self):
        """La vue a bougé : on recalcule les lignes à afficher (pas la table)."""
        self._hide_tooltip()
        self._render()

    def _ensure_visible(self, index):
        """Fait défiler juste ce qu'il faut pour rendre ``index`` visible."""
        if index is None:
            return
        top = self._header_h + index * self._row_h
        cam = self.canvasy(0)
        view = self._view_h()
        if top < cam + self._header_h:
            self._scroll_lines(-((cam + self._header_h - top) / self._row_h))
        elif top + self._row_h > cam + view:
            self._scroll_lines(((top + self._row_h) - (cam + view)) / self._row_h)

    # ══════════════════════════════════════════════════════════════════════
    # Identification (coordonnées écran -> ligne / colonne / région)
    # ══════════════════════════════════════════════════════════════════════
    def _row_index_at(self, y):
        """Index de la ligne sous ``y`` (coordonnée widget) ou ``None``."""
        if y < self._header_h:
            return None
        idx = int((self.canvasy(0) + y - self._header_h) // self._row_h)
        if idx < 0 or idx >= len(self._rows):
            return None
        return idx

    def identify_row(self, y):
        idx = self._row_index_at(y)
        return "" if idx is None else self._rows[idx]

    def _column_index_at(self, x):
        if x < 0:
            return None
        cx = self.canvasx(x)
        for i, col in enumerate(self.columns):
            x0, w = self._x_layout.get(col, (None, 0))
            if x0 is None:
                continue
            if x0 <= cx < x0 + w:
                return i
        return None

    def _column_index(self, column):
        """Accepte un nom, un index numérique ou « #N » (comme Tk)."""
        if column is None:
            return None
        if isinstance(column, int):
            return column if 0 <= column < len(self.columns) else None
        text = str(column)
        if text.startswith("#"):
            try:
                n = int(text[1:]) - 1
            except ValueError:
                return None
            return n if 0 <= n < len(self.columns) else None
        if text in self._col_cfg:
            return self.columns.index(text)
        return None

    def identify_column(self, x):
        idx = self._column_index_at(x)
        return "" if idx is None else f"#{idx + 1}"

    def identify_region(self, x, y):
        """Région sous le curseur : ``heading``/``separator``/``cell``/``nothing``."""
        if x < 0 or y < 0:
            return "nothing"
        if x > self._view_w() or y > self._view_h():
            return "nothing"
        if x > self._total_w:
            return "nothing"  # au-delà de la dernière colonne
        if y < self._header_h:
            for col in self.columns[:-1]:
                x0, w = self._x_layout.get(col, (0, 0))
                if abs(x - (x0 + w)) <= SEP_GRAB:
                    return "separator"
            return "heading"
        if self._row_index_at(y) is None:
            return "nothing"
        return "cell"

    def bbox(self, iid, column=None):
        """Rectangle d'une cellule visible (coordonnées widget), sinon ``""``."""
        iid = str(iid)
        self._flush_moves()
        idx = self._index_of(iid)
        if idx is None:
            return ""
        cam = self.canvasy(0)
        top = self._header_h + idx * self._row_h
        if top + self._row_h <= cam or top >= cam + self._view_h():
            return ""
        if column is None:
            x, w = 0, self._total_w
        else:
            ci = self._column_index(column)
            if ci is None:
                return ""
            x, w = self._x_layout.get(self.columns[ci], (0, 0))
        return (int(x), int(top - cam), int(w), self._row_h)

    # ══════════════════════════════════════════════════════════════════════
    # Mise en page (colonnes) et invalidations
    # ══════════════════════════════════════════════════════════════════════
    def _rebuild_layout(self):
        """Recalcule la position horizontale de chaque colonne."""
        x = 0
        layout = {}
        for col in self.columns:
            cfg = self._col_cfg[col]
            w = max(1, max(int(cfg["width"]), int(cfg["minwidth"])))
            layout[col] = (x, w)
            x += w
        self._x_layout = layout
        self._total_w = x

    def _sync_scrollregion(self, force=False):
        """Région de défilement = table entière (en-tête compris, comme Tk).

        La synchronisation est paresseuse : les insertions en masse ne coûtent
        pas un appel Tcl par ligne, la région est recalculée au rendu (ou dès
        qu'une lecture de défilement est demandée).
        """
        if not (force or self._scrollregion_dirty):
            return
        width = max(self._total_w, self._view_w())
        height = max(self._content_h(), self._view_h())
        try:
            self.configure(scrollregion=(0, 0, width, height))
            self._scrollregion_dirty = False
        except Exception:
            pass

    def _on_layout_changed(self):
        """Largeur de colonne modifiée : nouvelle mise en page + rendu."""
        self._rebuild_layout()
        self._invalidate_header()
        self._all_dirty = True
        self._scrollregion_dirty = True
        self._sync_scrollregion()
        self._schedule_render()

    def _invalidate_header(self):
        if self._header_items is not None:
            try:
                # Suppression CANEVAS (pas le modèle de lignes !) : ``self.delete``
                # est surchargé pour les iid de lignes — ici c'est le tag
                # d'items graphiques « vthead », il faut le delete natif de
                # tk.Canvas. Sans ça, l'ancien en-tête restait affiché et un
                # second était créé au rendu suivant → en-tête fantôme au
                # milieu de la liste après changement de Grand site / resize.
                tk.Canvas.delete(self, self._header_items["root"])
            except Exception:
                pass
        self._header_items = None

    def _after_data_change(self, flush=True):
        """Le modèle a changé : région de défilement et rendu suivent."""
        if flush:
            self._flush_moves()
        self._all_dirty = True
        self._scrollregion_dirty = True
        self._schedule_render()

    def _schedule_render(self):
        if self._render_after is not None:
            return
        try:
            self._render_after = self.after_idle(self._render)
        except Exception:
            self._render_after = None

    def _cancel_pending_render(self):
        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except Exception:
                pass
            self._render_after = None

    def _on_configure(self, event=None):
        """Redimensionnement : on ne reconstruit RIEN, on recale la vue."""
        self._scrollregion_dirty = True
        self._sync_scrollregion(force=True)
        self._render()

    # ── Façade compatible avec l'ancien ChipRenderer ─────────────────────
    # L'application appelle ``chip_renderer.redraw()`` / ``schedule_redraw()`` /
    # ``repaint_row_backgrounds()`` / ``clear()`` : en mode virtualisé ces
    # appels pilotent directement le rendu de la table (aucun overlay à gérer).
    def redraw(self):
        """Rend immédiatement les lignes visibles."""
        self._cancel_pending_render()
        self._render()

    def schedule_redraw(self):
        self._all_dirty = True
        self._schedule_render()

    def repaint_row_backgrounds(self):
        """Recolore uniquement les fonds des lignes visibles (clignotement des
        imprimantes « down ») : aucun recalcul de contenu ni de géométrie."""
        self._repaint_visible_backgrounds()

    def clear(self):
        """Rien à libérer : aucun overlay n'est conservé en mémoire."""
        return

    def set_chip_provider(self, provider):
        """Fonction ``provider(iid, values) -> {colonne: [pill, ...]}``.

        Appelée UNIQUEMENT pour les lignes réellement peintes (donc visibles) :
        le coût du calcul des chips reste proportionnel à l'écran, jamais au
        nombre total d'imprimantes.
        """
        self._chip_provider = provider
        self._all_dirty = True
        self._schedule_render()

    # ══════════════════════════════════════════════════════════════════════
    # Rendu virtualisé : en-tête fixe + lignes visibles recyclées
    # ══════════════════════════════════════════════════════════════════════
    def _render(self):
        """Rendu (recyclage + repositionnement) — jamais de reconstruction."""
        self._render_after = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._flush_moves()
        self._sync_scrollregion(force=True)
        # Le contenu a pu RACCOURCIR (changement de Grand site, filtre) alors
        # que la vue est restee scrollee bas : on est alors au-dela du contenu
        # (visible=∅ → affichage fige jusqu'au prochain scroll). On re-ancre
        # la vue dans le contenu AVANT de peindre : retour en haut si le haut
        # du contenu est hors vue, ou clamp sur la fin sinon. Invisible quand
        # la vue est deja valide (cas du scroll normal / refresh en place).
        try:
            cam = float(self.canvasy(0))
            view_h = float(self._view_h())
            content_h = float(self._content_h())
            limit = max(0.0, content_h - view_h)
            if cam > limit + 1.0:
                tk.Canvas.yview_moveto(self, limit / max(1.0, content_h))
                cam = limit
        except Exception:
            pass
        self._render_header()
        self._render_rows()

    def _render_header(self):
        """Dessine l'en-tête, ancré en haut de la vue (il ne défile pas)."""
        cam_y = int(round(self.canvasy(0)))
        cam_x = self.canvasx(0)
        view_w = self._view_w()
        h = self._header_h
        items = self._header_items

        if items is None:
            tag = "vthead"
            bg = self.create_rectangle(0, 0, 0, 0, fill=SURFACE, outline="",
                                       tags=(tag,))
            cells = []
            for col in self.columns:
                rect = self.create_rectangle(0, 0, 0, 0, fill="#f8fafc",
                                             outline=BORDER, tags=(tag,))
                text = self.create_text(0, 0, anchor="center", text="",
                                        fill=MUTED, font=self._head_font,
                                        tags=(tag,))
                cells.append((col, rect, text))
            bottom = self.create_line(0, 0, 0, 0, fill=BORDER, tags=(tag,))
            items = {"root": tag, "bg": bg, "cells": cells, "bottom": bottom}
            self._header_items = items

        # Fond opaque : les lignes qui passent dessous sont masquées.
        self.coords(items["bg"], cam_x - 4, cam_y - 2,
                    cam_x + view_w + 4, cam_y + h)
        for col, rect, text in items["cells"]:
            x0, w = self._x_layout.get(col, (0, 0))
            self.coords(rect, x0, cam_y, x0 + w, cam_y + h)
            cfg = self._col_cfg[col]
            label = str(cfg.get("text", col) or "")
            display, _trunc = _fit_text(self._head_font, label,
                                        max(0, w - 2 * CELL_PAD))
            if str(cfg.get("hanchor") or cfg.get("anchor")) == "w":
                tx, anchor = x0 + CELL_PAD, "w"
            else:
                tx, anchor = x0 + w / 2, "center"
            self.coords(text, tx, cam_y + h / 2)
            self.itemconfigure(text, text=display, anchor=anchor, fill=MUTED,
                               state="normal")
        self.coords(items["bottom"], cam_x - 4, cam_y + h,
                    cam_x + view_w + 4, cam_y + h)

    def _render_rows(self):
        """N'affiche que les lignes visibles, en recyclant les mêmes éléments."""
        n = len(self._rows)
        row_h = self._row_h
        force = self._all_dirty
        if not n:
            for slot in self._slots:
                if slot["row"] is not None:
                    self._row_slot.pop(slot["row"], None)
                    slot["row"] = None
                self._hide_slot(slot)
            self._all_dirty = False
            return
        cam = self.canvasy(0)
        view_h = self._view_h()
        # cam peut etre legerement NEGATIF apres un yview_moveto(0.0) (rebond
        # Tk : la geometrie n'est effective qu'au prochain idle). Un cam
        # negatif n'est jamais une position valide : on le normalise a 0 pour
        # le calcul des lignes visibles (sinon la derniere ligne visible est
        # sous-evaluee d'1 → trou en bas de vue).
        if cam < 0.0:
            cam = 0.0
        # Lignes 0..n-1 peintes en coordonnees canevas y = header_h + r*row_h.
        # La fenetre visible commence SOUS l'en-tete fixe, soit en coordonnees
        # canevas a (cam + header_h) : l'index de la premiere ligne visible est
        # donc ((cam + header_h) - header_h) // row_h = cam // row_h.
        # L'ancienne formule ajoutait header_h au lieu de le retrancher, ce qui
        # decalait la fenetre d'une hauteur d'en-tete (~1 ligne manquante =
        # « trous » qui se comblaient au scroll suivant). ``first`` est borne
        # a n-1 : sans ca, une vue au-dela du contenu (typique apres un
        # filtrage qui raccourcit la liste) donnait visible=∅ tout en
        # conservant les anciens slots → affichage fige.
        first = max(0, min(n - 1, int(max(0.0, cam) // row_h)))
        last = min(n - 1, int(max(0.0, cam + view_h - self._header_h) // row_h))
        visible = range(first, last + 1) if last >= first else range(0, 0)
        needed = set(visible)

        # Contenu REMPLACE (changement de Grand site = delete+insert massif) :
        # les index de lignes ont change de sens (l'ancienne ligne 5 n'est plus
        # la nouvelle ligne 5) mais les slots portent encore les anciens index.
        # Sans invalidation, le recyclage « conserve » des slots obsoletes et
        # repeint les autres au mauvais contenu → trous + lignes figees qui ne
        # se resolvent qu'au scroll suivant. Apres un delete/insert, TOUT est
        # repeint (force), le cout reste borne aux lignes visibles.
        if getattr(self, "_rows_replaced", False):
            self._rows_replaced = False
            force = True
            self._row_slot.clear()
            for slot in self._slots:
                slot["row"] = None

        # Un slot qui affiche encore une ligne visible est CONSERVÉ tel quel :
        # le canevas a déjà translaté ses éléments avec la vue, il n'y a donc
        # rien à repositionner — c'est ce qui rend le défilement quasi gratuit.
        # (En mode force — repeinture complete — on libere tout et on repeint.)
        free = []
        if not force:
            for slot in self._slots:
                row = slot["row"]
                if row is not None and row in needed:
                    continue
                if row is not None:
                    self._row_slot.pop(row, None)
                    slot["row"] = None
                free.append(slot)
        else:
            for slot in self._slots:
                if slot["row"] is not None:
                    slot["row"] = None
                free.append(slot)
            self._row_slot.clear()

        for r in visible:
            slot = self._row_slot.get(r)
            if slot is not None and not force:
                continue
            if slot is None:
                slot = free.pop() if free else self._new_slot()
            self._paint_slot(slot, r)

        for slot in free:
            self._hide_slot(slot)
        self._all_dirty = False

    def _new_slot(self):
        """Crée (une seule fois) les éléments d'une ligne réutilisable."""
        bg = self.create_rectangle(0, 0, 0, 0, fill=SURFACE, outline="",
                                   state="hidden", tags=("vtrow",))
        cells = []
        for _col in self.columns:
            text = self.create_text(0, 0, anchor="w", text="", fill=TEXT,
                                    font=self._cell_font, state="hidden",
                                    tags=("vtrow",))
            cells.append({"text": text, "pills": [], "truncated": False,
                          "full": None})
        slot = {"bg": bg, "cells": cells, "row": None, "fg": None,
                "hidden": False}
        self._slots.append(slot)
        # Les éléments créés après l'en-tête le recouvriraient : on le remonte.
        self.tag_raise("vthead")
        return slot

    def _hide_slot(self, slot):
        if slot["row"] is None and slot.get("hidden"):
            return
        self.itemconfigure(slot["bg"], state="hidden")
        for cell in slot["cells"]:
            self.itemconfigure(cell["text"], state="hidden")
            for rect, text in cell["pills"]:
                self.itemconfigure(rect, state="hidden")
                self.itemconfigure(text, state="hidden")
        slot["hidden"] = True

    def _paint_slot(self, slot, row_index):
        """(Re)configure un slot pour la ligne ``row_index`` (aucun widget créé)."""
        iid = self._rows[row_index]
        rec = self._data.get(iid)
        if rec is None:
            return
        row_h = self._row_h
        y = self._header_h + row_index * row_h
        bg, fg = self._row_colors(iid, rec)
        self.coords(slot["bg"], 0, y,
                    max(self._total_w, self._view_w()), y + row_h)
        self.itemconfigure(slot["bg"], fill=bg, state="normal")
        slot["fg"] = fg
        slot["hidden"] = False

        # Les chips ne sont calculées que pour les lignes réellement peintes.
        pills_by_col = {}
        provider = self._chip_provider
        if provider is not None:
            try:
                pills_by_col = provider(iid, rec["values"]) or {}
            except Exception:
                pills_by_col = {}

        values = rec["values"]
        for idx, col in enumerate(self.columns):
            cell = slot["cells"][idx]
            x0, w = self._x_layout.get(col, (0, 0))
            value = values[idx] if idx < len(values) else ""
            pills = pills_by_col.get(col) if col in self._chip_columns else None
            if pills:
                # Une cellule à chips n'affiche PAS le texte brut (comme
                # auparavant, où l'overlay le recouvrait).
                self.itemconfigure(cell["text"], state="hidden")
                cell["truncated"] = False
                cell["full"] = None
                self._paint_pills(cell, pills, x0, y, w, row_h)
            else:
                self._hide_pills(cell)
                self._paint_cell_text(cell, col, value, x0, y, w, row_h, fg)
        slot["row"] = row_index
        slot["hidden"] = False
        self._row_slot[row_index] = slot

    def _paint_cell_text(self, cell, col, value, x0, y, w, h, fg):
        """Affiche la valeur d'une cellule (tronquée avec « … » si besoin)."""
        text = str(value)
        avail = max(0, w - 2 * CELL_PAD)
        display, truncated = _fit_text(self._cell_font, text, avail)
        cell["truncated"] = truncated
        cell["full"] = text if truncated else None
        if not display:
            self.itemconfigure(cell["text"], state="hidden")
            return
        cfg = self._col_cfg.get(col) or {}
        if str(cfg.get("anchor")) == "w":
            tx, anchor = x0 + CELL_PAD, "w"
        else:
            tx, anchor = x0 + w / 2, "center"
        self.coords(cell["text"], tx, y + h / 2)
        self.itemconfigure(cell["text"], text=display, anchor=anchor,
                           fill=fg, state="normal")

    def _paint_pills(self, cell, pills, x0, y, w, h):
        """Dessine les chips d'une cellule DIRECTEMENT dans le canevas.

        La géométrie (padding, retour à la ligne, résumé « +N », centrage) est
        celle de l'ancien overlay : seule la technique change (rectangles et
        textes de canevas recyclés au lieu de tk.Label superposés).
        """
        avail = max(0, w - 4)
        plan = plan_chips(pills, avail, h)
        lines = plan["lines"]
        if not lines:
            self._hide_pills(cell)
            return
        total_w = plan["total"]
        line_h = plan["line_h"]
        line_gap = plan["line_gap"]
        pad_x = plan["pad_x"]
        x_off = x0 + 2 + ((avail - total_w) // 2 if total_w <= avail else 0)
        block_h = len(lines) * line_h + line_gap * max(0, len(lines) - 1)
        y0 = y + max(0, (h - block_h) // 2)

        need = sum(len(line["items"]) for line in lines)
        self._ensure_pills(cell, need)
        used = 0
        for li, line in enumerate(lines):
            lx = max(0, total_w - line["total"]) // 2
            y_line = y0 + li * (line_h + line_gap)
            for pill, rel_x in line["items"]:
                rect, label = cell["pills"][used]
                px = x_off + lx + rel_x
                cw = chip_width(pill["text"], pad_x)
                self.coords(rect, px + 0.5, y_line + 0.5,
                            px + cw - 0.5, y_line + line_h - 0.5)
                self.itemconfigure(rect, fill=pill["bg"], outline=CHIP_BORDER,
                                   width=1, state="normal")
                self.coords(label, px + cw / 2, y_line + line_h / 2)
                self.itemconfigure(label, text=pill["text"], fill=pill["fg"],
                                   font=self._chip_font, state="normal")
                used += 1
        for i in range(used, len(cell["pills"])):
            rect, label = cell["pills"][i]
            self.itemconfigure(rect, state="hidden")
            self.itemconfigure(label, state="hidden")

    def _ensure_pills(self, cell, need):
        """Crée à la demande les paires (rectangle, texte) d'une cellule."""
        pills = cell["pills"]
        while len(pills) < need:
            rect = self.create_rectangle(0, 0, 0, 0, fill=SURFACE,
                                         outline=CHIP_BORDER, state="hidden",
                                         tags=("vtrow",))
            label = self.create_text(0, 0, anchor="center", text="",
                                     fill=TEXT, font=self._chip_font,
                                     state="hidden", tags=("vtrow",))
            pills.append((rect, label))
        self.tag_raise("vthead")

    def _hide_pills(self, cell):
        for rect, label in cell["pills"]:
            self.itemconfigure(rect, state="hidden")
            self.itemconfigure(label, state="hidden")

    def _row_colors(self, iid, rec):
        """Fond et couleur de texte d'une ligne.

        La sélection est prioritaire (elle doit rester visible même sur une
        ligne colorée par une alerte), puis viennent les tags — exactement la
        règle déjà appliquée par le rendu des chips
        (``ChipRenderer._row_background``).
        """
        if iid in self._selection:
            return PRIMARY_SOFT, TEXT
        bg = None
        fg = None
        for tag in rec.get("tags") or ():
            cfg = self._tag_cfg.get(tag)
            if not cfg:
                continue
            if cfg.get("background"):
                bg = cfg["background"]
            if cfg.get("foreground"):
                fg = cfg["foreground"]
        return bg or SURFACE, fg or TEXT

    def _repaint_visible_backgrounds(self):
        """Recolore les fonds (et couleurs de texte) des lignes visibles."""
        for row_index, slot in list(self._row_slot.items()):
            if row_index >= len(self._rows):
                continue
            rec = self._data.get(self._rows[row_index])
            if rec is None:
                continue
            bg, fg = self._row_colors(self._rows[row_index], rec)
            try:
                self.itemconfigure(slot["bg"], fill=bg)
            except Exception:
                continue
            if slot.get("fg") != fg:
                slot["fg"] = fg
                for cell in slot["cells"]:
                    if self.itemcget(cell["text"], "state") != "hidden":
                        self.itemconfigure(cell["text"], fill=fg)

    # ══════════════════════════════════════════════════════════════════════
    # Comportements par défaut (tag de bindings interne « VirtualTable »)
    # ══════════════════════════════════════════════════════════════════════
    def _install_class_bindings(self):
        """Installe les comportements par défaut, APRÈS ceux de l'application.

        Le tag « VirtualTable » est placé juste après le widget dans les
        bindtags : les gestionnaires posés par l'application (clic cellule,
        Ctrl+clic, Ctrl+C, double-clic…) s'exécutent d'abord, puis le
        comportement par défaut du tableau — exactement comme ttk.Treeview.
        """
        bindings = {
            "<Button-1>": "_on_press",
            "<Control-Button-1>": "_on_press",
            "<Shift-Button-1>": "_on_shift_press",
            "<B1-Motion>": "_on_drag_motion",
            "<ButtonRelease-1>": "_on_release",
            "<MouseWheel>": "_on_wheel",
            "<Shift-MouseWheel>": "_on_shift_wheel",
            "<Motion>": "_on_pointer_motion",
            "<Leave>": "_on_pointer_leave",
            "<Up>": "_on_key_up",
            "<Down>": "_on_key_down",
            "<Prior>": "_on_key_page_up",
            "<Next>": "_on_key_page_down",
            "<Home>": "_on_key_home",
            "<End>": "_on_key_end",
        }
        for seq, method in bindings.items():
            self.bind_class("VirtualTable", seq, _class_dispatch(method))
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ── Souris ────────────────────────────────────────────────────────────
    def _separator_column(self, x):
        for col in self.columns[:-1]:
            x0, w = self._x_layout.get(col, (0, 0))
            if abs(x - (x0 + w)) <= SEP_GRAB:
                return col
        return None

    def _on_press(self, event):
        try:
            self.focus_set()
        except Exception:
            pass
        if self.identify_region(event.x, event.y) == "separator":
            col = self._separator_column(event.x)
            if col is not None:
                cfg = self._col_cfg[col]
                self._drag = {"col": col, "x": event.x,
                              "width": cfg["width"], "minwidth": cfg["minwidth"]}
            return "break"
        self._hide_tooltip()
        row = self.identify_row(event.y)
        if row:
            # Mémorise la sélection AVANT ce clic : l'application s'en sert
            # pour détecter un second clic sur la même ligne (désélection +
            # masquage du panneau de détails). Les clics avec modificateur
            # (Ctrl/Maj) sont exclus.
            try:
                state = int(getattr(event, "state", 0) or 0) & 0x0005
            except Exception:
                state = 0
            if not state:
                self._press_selection = tuple(self._selection)
                self._press_row = row
            else:
                self._press_selection = None
                self._press_row = None
            self.selection_set(row)
            self._focus_iid = row
        return None

    def _on_shift_press(self, event):
        # En mode « browse », Maj+clic sélectionne la ligne cliquée (aucune
        # sélection multiple : le comportement actuel est conservé).
        return self._on_press(event)

    def _on_drag_motion(self, event):
        """Glissement d'un séparateur : redimensionnement en direct."""
        drag = self._drag
        if not drag:
            return None
        delta = event.x - drag["x"]
        new_width = max(int(drag["minwidth"]), int(drag["width"]) + int(delta))
        self.column(drag["col"], width=new_width)
        return "break"

    def _on_release(self, event):
        self._drag = None
        return None

    def _on_wheel(self, event):
        delta = getattr(event, "delta", 0) or 0
        if not delta:
            return None
        self._hide_tooltip()
        self.yview("scroll", -delta / 120.0, "units")
        return "break"

    def _on_shift_wheel(self, event):
        delta = getattr(event, "delta", 0) or 0
        if not delta:
            return None
        self.xview("scroll", -delta / 120.0, "units")
        return "break"

    # ── Clavier (navigation du tableau, comme le Treeview) ───────────────
    def _page_rows(self):
        return max(1, int(self._view_h() // self._row_h) - 1)

    def _current_index(self):
        if self._selection:
            idx = self._index_of(self._selection[0])
            if idx is not None:
                return idx
        if self._focus_iid:
            return self._index_of(self._focus_iid)
        return None

    def _select_index(self, index):
        n = len(self._rows)
        if not n:
            return "break"
        index = max(0, min(n - 1, int(index)))
        iid = self._rows[index]
        self.selection_set(iid)
        self._focus_iid = iid
        self._ensure_visible(index)
        return "break"

    def _move_selection(self, delta):
        current = self._current_index()
        if current is None:
            return self._select_index(0 if delta >= 0 else len(self._rows) - 1)
        return self._select_index(current + delta)

    def _on_key_up(self, event):
        return self._move_selection(-1)

    def _on_key_down(self, event):
        return self._move_selection(1)

    def _on_key_page_up(self, event):
        return self._move_selection(-self._page_rows())

    def _on_key_page_down(self, event):
        return self._move_selection(self._page_rows())

    def _on_key_home(self, event):
        return self._select_index(0)

    def _on_key_end(self, event):
        return self._select_index(len(self._rows) - 1)

    def _on_destroy(self, event=None):
        if event is not None and getattr(event, "widget", self) is not self:
            return None
        self._hide_tooltip()
        self._cancel_pending_render()
        return None

    # ══════════════════════════════════════════════════════════════════════
    # Infobulle : valeur complète d'une cellule tronquée
    # ══════════════════════════════════════════════════════════════════════
    def _on_pointer_motion(self, event):
        if self._drag:
            return None
        self._hide_tooltip()
        if self.identify_region(event.x, event.y) != "cell":
            return None
        self._tooltip_pos = (event.x, event.y)
        try:
            self._tooltip_after = self.after(TOOLTIP_DELAY, self._show_tooltip)
        except Exception:
            self._tooltip_after = None
        return None

    def _on_pointer_leave(self, event=None):
        self._hide_tooltip()
        return None

    def _show_tooltip(self):
        """Affiche la valeur COMPLÈTE de la cellule sous le curseur.

        La cellule est ré-identifiée à cet instant (et non au moment du
        survol) : un défilement, un tri ou une recherche entre-temps ne peut
        donc jamais faire apparaître l'infobulle d'une autre ligne.
        """
        self._tooltip_after = None
        pos = self._tooltip_pos
        if not pos or self._tooltip is not None:
            return
        x, y = pos
        row = self.identify_row(y)
        col_idx = self._column_index_at(x)
        if not row or col_idx is None:
            return
        col = self.columns[col_idx]
        if col in self._chip_columns:
            return  # la cellule affiche des pastilles, pas du texte brut
        rec = self._data.get(row)
        if rec is None:
            return
        value = str(rec["values"][col_idx]) if col_idx < len(rec["values"]) else ""
        if not value:
            return
        _x0, w = self._x_layout.get(col, (0, 0))
        _display, truncated = _fit_text(self._cell_font, value,
                                        max(0, w - 2 * CELL_PAD))
        if not truncated:
            return
        self._tooltip = self._build_tooltip(value)

    def _build_tooltip(self, text):
        try:
            top = tk.Toplevel(self)
            top.wm_overrideredirect(True)
            try:
                top.attributes("-topmost", True)
            except Exception:
                pass
            frame = tk.Frame(top, background=BORDER, bd=0, highlightthickness=0)
            frame.pack()
            tk.Label(frame, text=text, background=SURFACE, foreground=TEXT,
                     font=self._cell_font, justify="left", wraplength=640,
                     padx=8, pady=4, bd=0, highlightthickness=0).pack(padx=1, pady=1)
            top.update_idletasks()
            top.wm_geometry(
                f"+{self.winfo_pointerx() + 14}+{self.winfo_pointery() + 20}")
            return top
        except Exception:
            return None

    def _hide_tooltip(self):
        if self._tooltip_after is not None:
            try:
                self.after_cancel(self._tooltip_after)
            except Exception:
                pass
            self._tooltip_after = None
        top = self._tooltip
        self._tooltip = None
        self._tooltip_pos = None
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass
