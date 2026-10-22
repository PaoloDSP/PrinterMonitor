"""Gestion de la géométrie des fenêtres secondaires de Printer Monitor.

Fournit :

* ``enumerate_monitors``      : liste des écrans actuellement disponibles
                                (multi-écrans via l'API Windows, avec repli
                                sur l'écran unique rapporté par Tk) ;
* ``apply_saved_geometry``    : restaure la position/taille mémorisée d'une
                                fenêtre après vérification qu'elle reste bien
                                visible sur un écran encore connecté ;
* ``remember_window``         : mémorise automatiquement position et taille
                                à chaque déplacement/redimensionnement
                                (sauvegarde différée, état maximisé inclus) ;
* ``setup_secondary_window``  : combinaison des trois, à appeler juste après
                                la création d'une fenêtre secondaire.

Chaque fenêtre est identifiée par une clé arbitraire (« parametres »,
« historique », « rapprochement »…) ; les états sont stockés de façon
indépendante dans ``printer_monitor_windows.json``, à côté du fichier de
configuration principale.
"""

import json
import logging
from pathlib import Path

# Fichier de mémorisation (même répertoire de travail que la configuration
# principale, cf. printer_monitor/config.py).
STATE_FILE = Path("printer_monitor_windows.json")

# Visibilité minimale exigée pour considérer qu'une fenêtre est utilisable :
# au moins ces pixels (horizontalement / verticalement) doivent rester sur un
# écran disponible, sinon la position mémorisée est jugée invalide et la
# fenêtre est recentrée.
_MIN_VISIBLE_X = 140
_MIN_VISIBLE_Y = 70

_states_cache = None


def _resolve_placement(width, height, x, y, monitors):
    """Valide une position mémorisée vis-à-vis des écrans disponibles.

    Si la fenêtre reste suffisamment visible sur un écran, la position est
    simplement contrainte pour rester utilisable ; sinon (écran déconnecté,
    résolution réduite…) la fenêtre est recentrée sur l'écran le plus proche
    de sa position d'origine.
    """
    best = None  # (intersection valide ?, aire, moniteur)
    for mon in monitors:
        mx, my, mw, mh = mon
        iw = min(x + width, mx + mw) - max(x, mx)
        ih = min(y + height, my + mh) - max(y, my)
        usable = iw >= _MIN_VISIBLE_X and ih >= _MIN_VISIBLE_Y
        area = max(0, iw) * max(0, ih)
        if best is None or (usable, area) > best[:2]:
            best = (usable, area, mon)

    usable, _area, mon = best
    width, height = _fit_size(width, height, mon)
    if usable:
        nx, ny = _place_on_monitor(width, height, x, y, mon)
        return nx, ny, width, height
    # Position invalide : recentrage sur l'écran retenu.
    mx, my, mw, mh = mon
    return (
        mx + max(0, (mw - width) // 2),
        my + max(0, (mh - height) // 2),
        width,
        height,
    )


def _default_size(window, default_size):
    """Taille par défaut : fournie, ou mesurée depuis le contenu demandé."""
    if default_size:
        return int(default_size[0]), int(default_size[1])
    try:
        window.update_idletasks()
        return int(window.winfo_reqwidth()), int(window.winfo_reqheight())
    except Exception:
        return 800, 600


def apply_saved_geometry(window, key, default_size=None):
    """Restaure (ou initialise) la position/taille de ``window`` (clé ``key``).

    Vérifie que la position mémorisée correspond toujours à un écran
    connecté ; dans le cas contraire, replace la fenêtre de façon visible.
    """
    parent = _parent_widget(window)
    monitors = enumerate_monitors(parent or window)
    dw, dh = _default_size(window, default_size)

    entry = _states().get(key) or {}

    def _int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    width = _int(entry.get("width"), dw)
    height = _int(entry.get("height"), dh)

    if "x" in entry and "y" in entry:
        x = _int(entry.get("x"), 0)
        y = _int(entry.get("y"), 0)
        x, y, width, height = _resolve_placement(
            width, height, x, y, monitors)
    else:
        # Première ouverture : centrage sur le premier écran (le principal).
        mx, my, mw, mh = monitors[0]
        width, height = _fit_size(width, height, monitors[0])
        x, y = mx + max(0, (mw - width) // 2), my + max(0, (mh - height) // 2)

    try:
        window.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
    except Exception:
        logging.exception("Application de la géométrie impossible (%s)", key)

    # État maximisé (Windows uniquement) : appliqué une fois la fenêtre
    # affichable, silencieusement ignoré ailleurs.
    if entry.get("maximized"):
        def _zoom():
            try:
                if window.state() != "zoomed":
                    window.state("zoomed")
            except Exception:
                pass
        window.after_idle(_zoom)


def remember_window(window, key):
    """Mémorise automatiquement la géométrie de ``window`` (clé ``key``).

    La sauvegarde est différée (~500 ms) pour éviter d'écrire le fichier JSON
    en continu pendant un déplacement/redimensionnement ; elle est finalisée
    à la destruction de la fenêtre. Les états iconifié (position fantôme
    -32000) et maximisé ne corrompent jamais les coordonnées normales.
    """
    states = _states()
    timer = {"id": None}

    def _capture():
        try:
            state = window.state()
        except Exception:
            return
        if state in ("iconic", "withdrawn"):
            return
        entry = dict(states.get(key) or {})
        if state == "zoomed":
            entry["maximized"] = True
        else:
            try:
                wh, _sep, pos = window.geometry().partition("+")
                w, _sep, h = wh.partition("x")
                coords = pos.split("+")
                entry.update({
                    "width": int(w),
                    "height": int(h),
                    "x": int(coords[0]),
                    "y": int(coords[1]),
                    "maximized": False,
                })
            except Exception:
                return
        states[key] = entry
        _save_states()

    def _flush(_event=None):
        if timer["id"] is not None:
            try:
                window.after_cancel(timer["id"])
            except Exception:
                pass
            timer["id"] = None
        _capture()

    def _schedule(event):
        if event.widget is not window:
            return
        if timer["id"] is not None:
            try:
                window.after_cancel(timer["id"])
            except Exception:
                pass
        timer["id"] = window.after(500, _flush)

    def _on_destroy(event):
        if event.widget is window:
            _flush()

    window.bind("<Configure>", _schedule, add="+")
    window.bind("<Destroy>", _on_destroy, add="+")


def setup_secondary_window(window, key, default_size=None, min_size=None):
    """À appeler après création d'une fenêtre secondaire.

    Applique une taille minimale raisonnable, restaure la géométrie mémorisée
    (avec validation multi-écrans) puis active la mémorisation automatique.
    """
    if min_size:
        try:
            window.minsize(int(min_size[0]), int(min_size[1]))
        except Exception:
            pass
    apply_saved_geometry(window, key, default_size=default_size)
    remember_window(window, key)


def _states():
    """Charge (une seule fois) le dictionnaire des états mémorisés."""
    global _states_cache
    if _states_cache is None:
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _states_cache = raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            _states_cache = {}
        except Exception:
            logging.exception("Lecture de %s impossible", STATE_FILE.name)
            _states_cache = {}
    return _states_cache


def _save_states():
    try:
        STATE_FILE.write_text(
            json.dumps(_states(), indent=2), encoding="utf-8"
        )
    except Exception:
        logging.exception("Écriture de %s impossible", STATE_FILE.name)


def enumerate_monitors(fallback_widget=None):
    """Retourne la liste des écrans actifs sous forme [(x, y, w, h), …].

    Utilise EnumDisplayMonitors (API Windows) pour couvrir les configurations
    multi-écrans ; en cas d'échec, se rabat sur les dimensions de l'écran
    virtuel rapportées par Tk (``fallback_widget``).
    """
    monitors = []
    try:
        import ctypes
        from ctypes import wintypes

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def _callback(_hmonitor, _hdc, rect, _data):
            r = rect.contents
            monitors.append(
                (r.left, r.top, r.right - r.left, r.bottom - r.top)
            )
            return 1

        if ctypes.windll.user32.EnumDisplayMonitors(
            0, None, MonitorEnumProc(_callback), 0
        ) and monitors:
            return monitors
    except Exception:
        pass

    # Repli : écran unique via Tk.
    if fallback_widget is not None:
        try:
            return [(
                0, 0,
                int(fallback_widget.winfo_screenwidth()),
                int(fallback_widget.winfo_screenheight()),
            )]
        except Exception:
            pass
    return [(0, 0, 1280, 1024)]


def _parent_widget(window):
    """Retourne un widget « ancêtre » exploitable (pour winfo_screen*)."""
    master = getattr(window, "master", None)
    if master is not None:
        return master
    try:
        return window.nametowidget(window.winfo_parent())
    except Exception:
        return None


def _fit_size(width, height, monitor):
    """Borne une taille pour qu'elle tienne dans l'écran donné."""
    _mx, _my, mw, mh = monitor
    return max(200, min(width, mw - 20)), max(160, min(height, mh - 60))


def _place_on_monitor(width, height, x, y, monitor):
    """Contraint (x, y) pour garder la fenêtre visible sur cet écran.

    Garantit qu'au moins ``_MIN_VISIBLE_X``/``_MIN_VISIBLE_Y`` pixels de la
    fenêtre restent dans les limites horizontales/verticales de l'écran.
    """
    mx, my, mw, mh = monitor
    lo_x = mx + min(_MIN_VISIBLE_X, mw) - width
    hi_x = mx + mw - min(_MIN_VISIBLE_X, mw)
    lo_y = my + min(_MIN_VISIBLE_Y, mh) - height
    hi_y = my + mh - min(_MIN_VISIBLE_Y, mh)
    return (
        int(min(max(int(x), lo_x), hi_x)),
        int(min(max(int(y), lo_y), hi_y)),
    )

