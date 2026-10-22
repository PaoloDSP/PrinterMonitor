"""Application principale Printer Monitor (interface graphique Tkinter)."""

import csv
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import BooleanVar, END, IntVar, StringVar, filedialog, messagebox, ttk

from .auth import verify_advanced_password
from ._version import APP_VERSION
from .config import (
    DEFAULT_CONFIG,
    DRUM_LOW_WARNING_THRESHOLD,
    LOW_WARNING_THRESHOLD,
    WARNING_WASTE_TONER_THRESHOLD,
    load_json_config,
    save_json_config,
)
from .ping import _ping_host
from .snmp import (
    SYS_NAME_OID,
    PrinterDevice,
)
from .utils import (
    DRUM_COLOR_NAMES,
    TONER_COLORS,
    is_kyocera_black_toner_ref,
    normalize_ip,
    text_color_for_bg,
)
from .widgets import ModelTagsFrame
from .chips import ChipRenderer, chips_for_device
from .vtable import VirtualTable
from .help import show_help, show_license
from .history import HistoryManager, DEFAULT_RETENTION_DAYS
from .window_state import setup_secondary_window
from .theme import (
    ROW_CRITICAL,
    ROW_DOWN,
    ROW_DOWN_ALT,
    ROW_ERROR,
    ROW_LOW,
    ROW_OFFLINE,
    ROW_TIMEOUT,
    SURFACE,
    TEXT,
    apply_theme,
    style_toplevel,
)

try:
    import openpyxl
except ImportError:
    openpyxl = None

# Chemin du fichier de log (défini dans config.py au démarrage)
LOG_FILE = Path.cwd() / "printer_monitor.log"
# Chemin du fichier de log (défini dans config.py au démarrage)
LOG_FILE = Path.cwd() / "printer_monitor.log"

# Libellé canonique du mode de tri PAR DÉFAUT (« alertes en premier »).
# C'est la valeur initiale de self.sort_mode ET la première entrée de la
# combobox de tri : les deux restent ainsi toujours cohérents (avant une
# correction, la variable était initialisée à l'identifiant technique
# « default », qui n'apparaissait ni dans la liste affichée ni dans aucune
# comparaison explicite). Dans _device_sort_key, tout mode qui n'est pas un
# tri « par niveau » — donc « Défaut (alertes en premier) » ET « default »,
# ainsi que toute valeur inconnue — est traité comme le tri par priorité,
# calculée par _get_device_priority UNIQUEMENT à partir de l'état réel de la
# machine (statut, joignabilité, erreur SNMP) et de l'état FINAL de ses
# consommables (seuils + exceptions par modèle inclus) — JAMAIS des alertes
# machine textuelles ni de la colonne « Messages » :
#   0 = hors ligne, 1 = erreur/DOWN, 2 = consommable au seuil de remplacement,
#   3 = consommable en avertissement, 4 = machine normale.
SORT_MODE_DEFAULT = "Défaut (alertes en premier)"

# Délai maximal (secondes) pour sonder la disponibilité du HTTPS d'un appareil.
_HTTPS_PROBE_TIMEOUT = 2.0

# ── Tableau : moteur de rendu ────────────────────────────────────────────────
# REFONTE DU RENDU (aucun changement de comportement) : la liste est affichée
# par une table VIRTUALISÉE (printer_monitor/vtable.py) qui dessine elle-même
# l'en-tête, les cellules et les chips dans un canevas unique, au lieu du
# couple « ttk.Treeview + overlays tk.Frame/tk.Label » (chips.ChipRenderer).
# L'ancien couple reste disponible pour comparaison/diagnostic via la variable
# d'environnement PRINTERMONITOR_LEGACY_TABLE=1.
_LEGACY_TABLE = os.environ.get("PRINTERMONITOR_LEGACY_TABLE", "").strip().lower() not in (
    "", "0", "false", "non", "no",
)


def _https_available(host, port=443, timeout=_HTTPS_PROBE_TIMEOUT):
    """Détermine si un appareil sert en HTTPS sur le port donné.

    La détection repose sur une sonde réseau fiable côté application (et non sur
    une erreur de navigateur) : on établit une connexion TCP puis un handshake
    TLS, sans valider le certificat car les appareils sont adressés par leur
    adresse IP et non par un nom d'hôte. Retourne True si le handshake réussit.
    """
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:
        return False


#: États métier historiquement rencontrés dans la colonne AdresseIP. Le
#: registre fait autorité (``presta.conventions``), cette liste n'est qu'un
#: filet de sécurité pour les graphies encore inconnues.
_LEGACY_SPECIAL_IP_MARKERS = ("RETIRE", "RETRAIT", "HORS RLE", "HORS RESEAU",
                              "HORS PARC", "COLIMO", "HORS SERVICE")


def _normalize_message_text(text):
    """Normalise un message pour comparaison (insensible casse/accents).

    Retire un éventuel préfixe de gravité ``[Critique]/[Alerte]/[Info]`` en
    tête, supprime les accents, met en minuscules et ne conserve que les
    tokens alphanumériques séparés par un espace. Utilisé UNIQUEMENT pour
    détecter les doublons de présentation : jamais pour calculer un statut.
    """
    import unicodedata

    s = str(text or "")
    s = re.sub(
        r"^\s*\[(?:critique|critical|alerte|alert|warning|"
        r"avertissement|info(?:rmation)?)\]\s*",
        "",
        s,
        flags=re.I,
    )
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _message_tokens(text):
    """Tokens normalisés d'un message (ensemble, éventuellement vide)."""
    norm = _normalize_message_text(text)
    return set(norm.split()) if norm else set()


# Mots de liaison / génériques d'état : seuls, ils n'apportent aucun détail
# (machine, imprimante, statut, erreur...). Un message dont TOUS les tokens
# appartiennent à ces ensembles (+ la famille d'état concernée) est un simple
# reformulation du statut : redondant. Tout autre token (jam, toner, porte,
# bac, fuser, code, timeout, snmp...) rend le message distinct : conservé.
_MESSAGE_FILLER_TOKENS = frozenset({
    "machine", "machines", "imprimante", "imprimantes", "printer",
    "printers", "device", "devices", "peripherique", "statut", "status",
    "etat", "state", "en", "erreur", "erreurs", "error", "errors",
    "critique", "critical", "alerte", "alert", "warning", "avertissement",
    "info", "le", "la", "les", "de", "du", "des", "un", "une", "au", "aux",
    "est", "et", "a", "avec", "son", "sa", "ses", "ce", "cette",
})

# Famille DOWN : uniquement le token « down » s'ajoute aux génériques.
_GENERIC_DOWN_TOKENS = _MESSAGE_FILLER_TOKENS | {"down"}

# Famille hors ligne : variantes FR/EN de « hors ligne / offline ».
_GENERIC_OFFLINE_TOKENS = _MESSAGE_FILLER_TOKENS | {
    "hors", "ligne", "lignes", "offline", "injoignable", "injoignables",
    "unreachable", "non", "joignable", "deconnecte", "deconnectee",
    "disconnected", "inaccessible", "indisponible",
}


def _is_generic_down_message(text):
    """True si le message ne fait que reformuler un état DOWN générique."""
    tokens = _message_tokens(text)
    return bool(tokens) and tokens <= _GENERIC_DOWN_TOKENS


def _is_generic_offline_message(text):
    """True si le message ne fait que reformuler un état hors ligne."""
    tokens = _message_tokens(text)
    return bool(tokens) and tokens <= _GENERIC_OFFLINE_TOKENS


def _is_timeout_error(text):
    """True si le texte est un timeout SNMP générique (conséquence d'état)."""
    norm = _normalize_message_text(text)
    if not norm or "timeout" not in norm:
        return False
    return ("snmp" in norm) or ("response" in norm)


def _status_is_down(status):
    """True si le statut normalisé vaut exactement « down » (hrDeviceStatus=5)."""
    return _normalize_message_text(status) == "down"


def _status_indicates_offline(status):
    """True si le statut affiche déjà un état hors ligne."""
    norm = _normalize_message_text(status)
    if not norm:
        return False
    return ("hors ligne" in norm) or ("offline" in norm) or (
        "injoignable" in norm) or ("unreachable" in norm)


def _status_indicates_timeout(status):
    """True si le statut affiche déjà un timeout SNMP (machine joignable)."""
    norm = _normalize_message_text(status)
    if not norm:
        return False
    return ("snmp" in norm and "timeout" in norm) or (
        "timeout" in norm and "joignable" in norm)


def _is_special_ip(value):
    """True si la colonne AdresseIP contient un ÉTAT MÉTIER, pas une adresse.

    « RETIRE », « HORS RESEAU », « COLIMO », « HORS PARC RMS »… décrivent la
    situation d'un matériel : ces lignes ne sont pas des imprimantes à
    surveiller. La reconnaissance est déléguée au registre partagé
    (``presta.conventions``) qui est LA référence, avec un repli textuel pour
    les graphies non encore répertoriées.
    """
    from .presta import conventions

    if conventions.special_state(value):
        return True
    text = str(value or "").upper()
    return any(marker in text for marker in _LEGACY_SPECIAL_IP_MARKERS)


class PrinterMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Printer Monitor v{APP_VERSION}")
        self.config = load_json_config()
        self.devices = []
        self.device_items = {}
        self.poll_queue = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.snmp_community = StringVar(value=self.config.get("snmp_community", "public"))
        self.snmp_port = IntVar(value=self.config.get("snmp_port", 161))
        self.toner_threshold = IntVar(value=self.config.get("thresholds", {}).get("toner", 4))
        self.drum_threshold = IntVar(value=self.config.get("thresholds", {}).get("drum", 4))
        self.toner_warning_threshold = IntVar(value=self.config.get("thresholds", {}).get("toner_warning", LOW_WARNING_THRESHOLD))
        self.drum_warning_threshold = IntVar(value=self.config.get("thresholds", {}).get("drum_warning", DRUM_LOW_WARNING_THRESHOLD))
        self.waste_toner_threshold = IntVar(value=self.config.get("thresholds", {}).get("waste_toner", WARNING_WASTE_TONER_THRESHOLD))
        # Exceptions par modèle : seuils de remplacement critiques configurables
        # par modèle d'imprimante. Normalisés en minuscules pour une comparaison
        # insensible à la casse.
        self.model_exceptions = self._normalize_model_exceptions(
            self.config.get("model_exceptions", DEFAULT_CONFIG.get("model_exceptions", []))
        )
        self.site_filter = StringVar(value="Tous")
        # Initialisé avec le libellé canonique SORT_MODE_DEFAULT (et non
        # l'ancien identifiant technique « default » qui ne correspondait à
        # aucun élément de la combobox) : l'affichage au démarrage et le tri
        # appliqué sont désormais le même mode par défaut dès la première
        # passe de populate_tree(), sans intervention utilisateur.
        self.sort_mode = StringVar(value=SORT_MODE_DEFAULT)
        # Mode avancé (développeur / debug) : désactivé par défaut, il ne
        # s'active qu'après une authentification par mot de passe réussie.
        # Le mot de passe administrateur est vérifié par le module auth.py
        # (dérivé scrypt salé codé en dur) — il n'est jamais stocké dans le
        # fichier JSON de configuration.
        self.advanced_mode = False
        # Global refresh progress tracking
        self.refresh_total = 0
        self.refresh_completed = 0
        self.refresh_start_time = None
        self.refresh_in_progress = False
        # Arrêt coopératif du rafraîchissement (bouton « Arrêter ») : l'événement
        # est levé à la demande de l'utilisateur et vérifié par chaque ouvrier
        # SNMP entre deux opérations. Aucun thread n'est terminé brutalement.
        self.refresh_stop_event = threading.Event()
        # Rafraîchissement automatique : l'état est chargé depuis la config mais
        # le compte à rebours ne démarre qu'après le chargement initial (CMDB +
        # Grand site) ; il ne se lance jamais directement au démarrage.
        _auto = self.config.get("auto_refresh", {}) or {}
        self.autorefresh_enabled = bool(_auto.get("enabled", True))
        self.autorefresh_interval_minutes = int(_auto.get("interval_minutes", 60))
        self.autorefresh_enabled_var = BooleanVar(value=self.autorefresh_enabled)
        self.autorefresh_interval_var = IntVar(value=self.autorefresh_interval_minutes)
        self._auto_after_id = None
        self._auto_refresh_active = False
        self._cmdb_loaded = False
        # Historique local SQLite : snapshots des imprimeurs après chaque
        # rafraîchissement réussi. Désactivable depuis les paramètres.
        _hist_cfg = self.config.get("history", {}) or {}
        self._history_enabled = bool(_hist_cfg.get("enabled", True))
        _hist_days = max(1, int(_hist_cfg.get("retention_days", DEFAULT_RETENTION_DAYS)))
        self._history = HistoryManager(retention_days=_hist_days) if self._history_enabled else None
        self._history_enabled_var = BooleanVar(value=self._history_enabled)
        self._history_retention_var = IntVar(value=_hist_days)
        self._last_history_cleanup = 0.0
        self.create_ui()
        # Charge automatiquement la CMDB mémorisée dans la configuration, si le
        # fichier existe toujours et est accessible. En cas d'échec, aucune
        # erreur n'est affichée : l'utilisateur pourra sélectionner une CMDB
        # manuellement avec le bouton « Charger CMDB ».
        self._load_saved_cmdb()
        self.root.after(100, self.process_poll_results)
        # Arrêt propre du timer de rafraîchissement automatique à la fermeture.
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _load_app_brand(self, parent_frame):
        """Intègre l'identité de l'application dans l'en-tête : petite icône
        carrée, nom du logiciel et version.

        Le tout reste compact et fait partie intégrante de la structure de la
        fenêtre (aucune bannière étirée posée au-dessus du contenu). Si l'icône
        est introuvable, un monogramme discret est affiché à la place.
        """
        icon_path = Path(__file__).resolve().parent.parent / "PM icone.png"
        if not icon_path.exists():
            icon_path = Path(__file__).resolve().parent / "PM icone.png"
        if not icon_path.exists():
            icon_path = Path(__file__).resolve().parent.parent / "printer_monitor_logo.png"

        use_icon = False
        try:
            if icon_path.exists():
                from PIL import Image, ImageTk

                pil = Image.open(str(icon_path)).convert("RGBA")
                # Recadre au plus grand carré centré pour une icône propre et
                # homogène, puis réduit à une petite taille d'avatar.
                w, h = pil.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                pil = pil.crop((left, top, left + side, top + side))
                icon_size = 30
                pil = pil.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
                self._brand_icon = ImageTk.PhotoImage(pil)
                ttk.Label(parent_frame, image=self._brand_icon, style="Card.TLabel").pack(
                    side="left", padx=(0, 8)
                )
                use_icon = True
        except Exception as e:
            logging.debug("Impossible de charger l'icône de l'en-tête : %s", e)

        if not use_icon:
            # Repli discret : aucun fichier image, un simple monogramme coloré.
            ttk.Label(
                parent_frame, text="◈", style="Brand.Chip.TLabel"
            ).pack(side="left", padx=(0, 8))

        ttk.Label(parent_frame, text="Printer Monitor", style="Brand.Title.TLabel").pack(
            side="left"
        )
        ttk.Label(parent_frame, text=f"v{APP_VERSION}", style="Brand.Chip.TLabel").pack(
            side="left", padx=(6, 0)
        )

    def create_ui(self):
        # Thème moderne : palette, typographie et styles ttk centralisés dans
        # printer_monitor/theme.py (cartes plates sans relief 3D, boutons
        # modernes avec états de survol, tableau aéré, scrollbars discrètes).
        apply_theme(self.root)

        header_bar = ttk.Frame(self.root, style="Card.TFrame")
        header_bar.pack(fill="x", padx=12, pady=(12, 6))
        header_inner = ttk.Frame(header_bar, style="Card.TFrame")
        header_inner.pack(fill="x", padx=8, pady=6)

        # Bloc identité (icône + nom + version) : toujours visible.
        brand_box = ttk.Frame(header_inner, style="Card.TFrame")
        brand_box.pack(side="left")
        self._load_app_brand(brand_box)

        ttk.Separator(header_inner, orient="vertical").pack(
            side="left", fill="y", padx=10, pady=4
        )

        # Actions principales : étendue défilante pour rester accessibles sur
        # écran étroit, sans rien perdre et sans casser la mise en page
        # responsive (une barre de défilement horizontale n'apparaît que si la
        # largeur disponible devient insuffisante).
        actions_box = ttk.Frame(header_inner, style="Card.TFrame")
        actions_box.pack(side="left", fill="x", expand=True)

        self._actions_canvas = tk.Canvas(
            actions_box, height=32, bd=0, highlightthickness=0, bg=SURFACE
        )
        self._actions_inner = ttk.Frame(self._actions_canvas, style="Card.TFrame")
        self._actions_win = self._actions_canvas.create_window(
            (0, 0), window=self._actions_inner, anchor="nw"
        )
        self._actions_hscroll = ttk.Scrollbar(
            actions_box, orient="horizontal", command=self._actions_canvas.xview
        )
        self._actions_canvas.configure(xscrollcommand=self._actions_hscroll.set)
        self._actions_canvas.pack(side="top", fill="x")
        self._actions_hscroll.pack_forget()
        self._actions_inner.bind("<Configure>", self._sync_actions_scroll)
        self._actions_canvas.bind(
            "<Configure>",
            lambda e: self._actions_canvas.itemconfigure(self._actions_win, width=e.width),
        )

        actions = self._actions_inner
        ttk.Button(actions, text="Charger CMDB", command=self.load_cmdb).pack(side="left", padx=(0, 5))
        self.refresh_all_btn = ttk.Button(
            actions, text="Rafraîchir tout", command=self.refresh_all,
            style="Accent.TButton",
        )
        self.refresh_all_btn.pack(side="left")
        self.refresh_selected_btn = ttk.Button(
            actions, text="Rafraîchir sélection", command=self.refresh_selected
        )
        self.refresh_selected_btn.pack(side="left", padx=5)
        self.refresh_stop_btn = ttk.Button(
            actions, text="Arrêter", command=self.stop_refresh,
            style="Danger.TButton",
        )
        self.refresh_stop_btn.pack(side="left", padx=5)
        self.refresh_stop_btn.pack_forget()
        self.export_btn = ttk.Button(actions, text="Exporter CSV", command=self.export_csv)
        self.export_btn.pack(side="left", padx=5)
        ttk.Button(actions, text="Paramètres", command=self.open_settings).pack(side="left", padx=5)
        # Bouton « Historique » : accède à la fenêtre d'historique local (SQLite).
        # Les snapshots sont enregistrés automatiquement à la fin de chaque
        # rafraîchissement réussi — aucune nouvelle interrogation SNMP n'est
        # effectuée depuis la fenêtre d'historique.
        self.history_btn = ttk.Button(actions, text="Historique", command=self.open_history)
        self.history_btn.pack(side="left", padx=5)
        # Bouton « Certificats » : gestion HTTPS des imprimantes (tls_certs).
        # Le compteur signale les cas a examiner (approbation requise, etc.).
        # Le Ctrl+clic et l'ouverture navigateur restent inchanges.
        self._tls_attention = 0
        self.tls_btn = ttk.Button(actions, text="Certificats", command=self.open_tls_certs)
        self.tls_btn.pack(side="left", padx=5)
        # Bouton d'accès au mode avancé (développeur / debug). Toujours visible,
        # mais les outils qu'il déverrouille restent cachés tant que l'on n'a
        # pas saisi le bon mot de passe.
        self.advanced_toggle_btn = ttk.Button(actions, text="Mode avancé", command=self.authenticate_advanced)
        self.advanced_toggle_btn.pack(side="left", padx=5)
        # Bouton « Aide » : ouvre la documentation utilisateur intégrée
        # (printer_monitor/help.py), stylée avec le thème de l'application.
        ttk.Button(actions, text="Aide", command=self.open_help).pack(side="left", padx=5)
        # Bouton « Licence » : affiche le contenu intégral du fichier LICENSE
        # du projet dans une fenêtre dédiée (printer_monitor/help.py).
        ttk.Button(actions, text="Licence", command=self.open_license).pack(side="left", padx=5)

        # Cacher par défaut les options développeur/debug du mode avancé. Elles
        # ne sont créées qu'une fois ici, puis rendues visibles dynamiquement
        # par _apply_advanced_visibility() une fois l'authentification réussie.
        self._create_advanced_frame()

        filter_frame = ttk.Frame(self.root, style="Card.TFrame")
        filter_frame.pack(fill="x", padx=12, pady=4)
        filter_inner = ttk.Frame(filter_frame, style="Card.TFrame")
        filter_inner.pack(fill="x", padx=10, pady=6)
        ttk.Label(filter_inner, text="Filtrer par Grand site :", style="Muted.TLabel").pack(side="left")
        self.site_filter_combo = ttk.Combobox(filter_inner, textvariable=self.site_filter, state="readonly", width=30)
        self.site_filter_combo.pack(side="left", padx=(8, 0))
        self.site_filter_combo.bind("<<ComboboxSelected>>", lambda _: self.apply_site_filter(save=True))

        columns = ["ip", "tag", "location", "csv_model", "status", "toner", "drum", "bottle", "staples", "last_update", "error"]
        self.columns = list(columns)
        tree_container = ttk.Frame(self.root, style="Card.TFrame")
        tree_container.pack(fill="both", expand=True, padx=12, pady=4)
        # Même API que ttk.Treeview (insert/item/delete/move/selection/column/
        # heading/tag_configure/identify_*/bbox/yview/xview/bind…) : tous les
        # appels ci-dessous, ainsi que les interactions, restent identiques.
        if _LEGACY_TABLE:
            self.tree = ttk.Treeview(
                tree_container, columns=columns, show="headings", selectmode="browse"
            )
        else:
            self.tree = VirtualTable(
                tree_container, columns=columns, show="headings", selectmode="browse"
            )
        headings = {
            "ip": "Adresse IP",
            "tag": "Tatouage",
            "location": "Localisation",
            "csv_model": "Modèle",
            "status": "Statut",
            "toner": "Toner/cartouche",
            "drum": "Tambour/photoconducteur",
            "bottle": "Bouteille récup. toner",
            "staples": "Agrafes",
            "last_update": "Dernière maj",
            "error": "Messages",
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            # minwidth : empêche Tk de compresser la colonne sous ce seuil
            # lorsque l'espace manque (sinon les colonnes stretch retombent à
            # 20px dès que le total dépasse la largeur du tableau).
            self.tree.column(
                column, width=140, anchor="center", stretch=False,
                minwidth=self.COLUMN_MIN_WIDTHS.get(column, 70),
            )
        self.tree.column("ip", width=130, stretch=False)
        self.tree.column("tag", width=120, stretch=False)
        self.tree.column("location", width=180, stretch=False)
        self.tree.column("toner", width=230, stretch=False)
        self.tree.column("drum", width=230, stretch=False)
        # ── Alignement des colonnes ──
        # Colonnes structurees (IP, tatouage, modele, statut, consommables) :
        # centrees (anchor="center" defini plus haut). Localisation/Messages :
        # alignees a gauche (textes longs potentiellement tronques).
        for col in ("location", "error"):
            self.tree.column(col, anchor="w")
            self.tree.heading(col, anchor="w")
        self.tree.heading("ip", command=lambda: self.sort_by("ip", False))
        self.tree.column("bottle", width=140, stretch=False)
        self.tree.column("staples", width=100, stretch=False)
        self.tree.column("status", width=120, stretch=True)
        self.tree.column("last_update", width=130, stretch=True)
        self.tree.column("error", width=300, stretch=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        # Ctrl+C : copie une cellule précise (si une cellule a été cliquée) ou
        # l'enregistrement complet de la ligne sélectionnée (comportement par défaut).
        self.tree.bind("<Control-c>", self.copy_selected)
        # Mémorise la dernière cellule cliquée pour la copie individuelle de cellule.
        self._press_row = None
        self._press_selection = None
        self.tree.bind("<Button-1>", self.on_cell_click)
        # Relâchement : désélection au second clic (AVANT le redimensionnement
        # de séparateur ; add="+" pour conserver ce dernier).
        self.tree.bind("<ButtonRelease-1>", self._on_row_release, add="+")
        # Ctrl+clic sur l'adresse IP -> ouvre l'interface Web de l'imprimante.
        self.tree.bind("<Control-Button-1>", self.on_ctrl_click)
        # Double-clic sur la séparation entre deux colonnes -> ajustement auto (type Excel)
        self.tree.bind("<Double-1>", self.on_column_separator_double_click)
        # Redimensionnement manuel par glissement d'un séparateur : mémorisé
        # pour que l'ajustement automatique n'écrase pas le choix utilisateur.
        self._user_sized_cols = set()
        # Colonnes rétrécies automatiquement lors d'un manque de place :
        # elles seront restaurées vers leur largeur de contenu quand la
        # fenêtre s'élargit à nouveau.
        self._shrunk_cols = set()
        self.tree.bind("<ButtonRelease-1>", self._on_tree_separator_release, add="+")
        # Adaptation des colonnes au redimensionnement de la fenêtre : les colonnes
        # stretch=True (status, last_update, error) s'expandent/contraient pour
        # occuper l'espace disponible sans débordement horizontal.
        self.root.bind("<Configure>", self._on_window_resize)
        # Scrollbar verticale pour parcourir la liste des imprimantes.
        # Packée AVANT le Treeview pour que sa place soit réservée et qu'elle
        # reste visible même quand la liste contient plus d'éléments que la zone.
        tree_vscroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_vscroll.set)
        tree_vscroll.pack(side="right", fill="y")

        # Scrollbar horizontale pour les colonnes qui dépassent de la largeur de la fenêtre.
        # Packée AVANT le Treeview pour la même raison. Visible uniquement si le
        # contenu dépasse la largeur de la fenêtre.
        tree_hscroll = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=tree_hscroll.set)
        tree_hscroll.pack(side="bottom", fill="x")

        # Le Treeview occupe l'espace restant après réservation des scrollbars.
        self.tree.pack(side="left", fill="both", expand=True)

        # Sorting by level
        sort_frame = ttk.Frame(self.root, style="Card.TFrame")
        sort_frame.pack(fill="x", padx=12, pady=4)
        sort_inner = ttk.Frame(sort_frame, style="Card.TFrame")
        sort_inner.pack(fill="x", padx=10, pady=6)
        ttk.Label(sort_inner, text="Tri par niveau :", style="Muted.TLabel").pack(side="left")
        self.sort_combo = ttk.Combobox(
            sort_inner,
            textvariable=self.sort_mode,
            state="readonly",
            width=30,
            values=[SORT_MODE_DEFAULT, "Niveau bas → haut", "Niveau haut → bas"],
        )
        self.sort_combo.pack(side="left", padx=(8, 0))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _: self.populate_tree())

        # Couleurs des lignes selon l'état (palette moderne définie dans theme.py)
        self.tree.tag_configure("error", background=ROW_ERROR)
        # Global device alert states (most critical supply alert)
        self.tree.tag_configure("device_critical", background=ROW_CRITICAL)
        self.tree.tag_configure("device_low", background=ROW_LOW)
        # SNMP timeout with reachable machine (orange) and machine offline (grey)
        self.tree.tag_configure("snmp_timeout", background=ROW_TIMEOUT)
        self.tree.tag_configure("snmp_offline", background=ROW_OFFLINE)
        # Imprimante en erreur (« down ») : rouge vif, distinct des simples
        # imprimantes hors ligne. Un effet clignotant est appliqué via
        # _blink_down_rows() sans ralentir le rafraîchissement.
        self.tree.tag_configure("device_down", background=ROW_DOWN, foreground="#ffffff")
        self._down_blink_state = False
        self._down_blink_after_id = None
        # Rendu des chips (pastilles couleur par cellule).
        # • table virtualisée : les chips sont dessinées DIRECTEMENT dans les
        #   cellules ; la table expose elle-même l'API attendue par le reste du
        #   code (redraw / schedule_redraw / repaint_row_backgrounds / clear), il
        #   n'y a donc plus aucun overlay à synchroniser ;
        # • mode historique : surcouche de widgets (chips.ChipRenderer).
        if _LEGACY_TABLE:
            self.chip_renderer = ChipRenderer(self)
        else:
            self.chip_renderer = self.tree
            self.tree.set_chip_provider(self._chips_for_row)

        detail_frame = ttk.LabelFrame(self.root)
        self.detail_frame = detail_frame
        detail_frame.pack(fill="x", padx=8, pady=4)
        # En-tête personnalisé du panneau : titre + bouton de fermeture discret.
        # (Le texte natif du LabelFrame ne peut pas porter de bouton, d'où le
        # labelwidget.) La fermeture masque le panneau et désélectionne la
        # machine, sans toucher à ses données.
        self._details_visible = True
        detail_header = ttk.Frame(detail_frame, style="Card.TFrame")
        ttk.Label(
            detail_header, text="Détails de l'appareil", style="Card.TLabel",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        self.detail_close_btn = ttk.Button(
            detail_header, text="✕", width=2, takefocus=0,
            command=self.clear_selection,
        )
        self.detail_close_btn.pack(side="right", padx=(6, 0))
        detail_frame.configure(labelwidget=detail_header)
        self._detail_header = detail_header
        # Zone scrollable pour les barres de consommables : le canvas suit la
        # hauteur du contenu tant qu'il tient dans la limite (max_height), sinon
        # une barre de défilement verticale apparaît pour consulter le reste.
        detail_canvas_container = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_canvas_container.pack(fill="x", padx=6, pady=(6, 0))
        self.detail_canvas = tk.Canvas(
            detail_canvas_container, height=200, highlightthickness=0, background=SURFACE
        )
        self.detail_canvas_vscroll = ttk.Scrollbar(
            detail_canvas_container, orient="vertical", command=self.detail_canvas.yview
        )
        self.detail_canvas.configure(yscrollcommand=self.detail_canvas_vscroll.set)
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        # Scrollbar masquée par défaut : elle n'apparaît que si le contenu
        # dépasse la hauteur maximale de la zone.
        self.detail_canvas_vscroll.pack_forget()
        # Frame interne contenant les groupes de barres de consommables.
        self.detail_bar_frame = ttk.Frame(self.detail_canvas)
        self._detail_canvas_window = self.detail_canvas.create_window(
            (0, 0), window=self.detail_bar_frame, anchor="nw"
        )
        # Ajuste la hauteur du canvas et la région de défilement quand le
        # contenu (nombre de consommables) change de taille.
        self.detail_bar_frame.bind("<Configure>", self._on_detail_bar_configure)
        # Ajuste la largeur du frame interne à celle du canvas.
        self.detail_canvas.bind(
            "<Configure>",
            lambda e: self.detail_canvas.itemconfigure(self._detail_canvas_window, width=e.width),
        )
        # Texte de détails avec sa propre barre de défilement verticale
        detail_text_container = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_text_container.pack(fill="both", expand=True, padx=6, pady=6)
        self.detail_text = tk.Text(
            detail_text_container,
            height=8,
            wrap="word",
            background=SURFACE,
            foreground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 9),
        )
        detail_vscroll = ttk.Scrollbar(detail_text_container, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_vscroll.set)
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_vscroll.pack(side="right", fill="y")
        self.detail_text.insert(END, "Chargez un fichier CMDB puis rafraîchissez pour afficher les niveaux et états des imprimantes.")
        self.detail_text.config(state="disabled")

        status_bar = ttk.Frame(self.root, style="Card.TFrame")
        self.status_bar = status_bar
        status_bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        self.status_label = ttk.Label(
            status_bar, text="Aucun fichier chargé.", style="Card.Muted.TLabel"
        )
        self.status_label.pack(side="left", padx=(10, 0), pady=6)
        # Progress bar for global refresh : longueur fixe et raisonnable —
        # elle ne doit pas s'étirer sur toute la largeur de la barre d'état.
        self.refresh_progress = ttk.Progressbar(
            status_bar, orient="horizontal", mode="determinate", length=220
        )
        self.refresh_progress.pack(side="left", padx=8)
        self.refresh_percent_label = ttk.Label(
            status_bar, text="0%", width=5, style="Card.Muted.TLabel"
        )
        self.refresh_percent_label.pack(side="left", padx=2, pady=6)
        self.refresh_time_label = ttk.Label(status_bar, text="", style="Card.Muted.TLabel")
        self.refresh_time_label.pack(side="left", padx=4, pady=6)

        # Signature en bas à droite : discret, élégant et cliquable
        self.footer_label = tk.Label(
            status_bar,
            text="Développé avec ❤️ par Paolo DOS SANTOS PINTO",
            font=("Segoe UI", 8),
            fg="#888888",
            cursor="hand2",
            bg=SURFACE,
        )
        self.footer_label.pack(side="right", padx=4)
        self.footer_label.bind(
            "<Enter>",
            lambda e: self.footer_label.config(fg="#1a73e8", font=("Segoe UI", 8, "underline")),
        )
        self.footer_label.bind(
            "<Leave>",
            lambda e: self.footer_label.config(fg="#888888", font=("Segoe UI", 8)),
        )
        self.footer_label.bind(
            "<Button-1>",
            lambda e: webbrowser.open("https://www.pdsp.fr/"),
        )

        # Réordonne les sections pour que la barre de statut, le menu avancé
        # et le panneau Détails restent ancrés en bas de la fenêtre, quelle que
        # soit la hauteur du contenu des consommables affichés.
        self._reorder_bottom_sections()

    def _sync_actions_scroll(self, event=None):
        """Met à jour la zone défilante des actions de l'en-tête : une barre de
        défilement horizontale n'apparaît que si les boutons dépassent la largeur
        disponible, afin que tous les contrôles restent accessibles sur écran
        étroit sans casser la mise en page responsive."""
        try:
            canvas = getattr(self, "_actions_canvas", None)
            inner = getattr(self, "_actions_inner", None)
            hscroll = getattr(self, "_actions_hscroll", None)
            if canvas is None or inner is None or hscroll is None:
                return
            inner.update_idletasks()
            req = inner.winfo_reqwidth()
            avail = max(0, canvas.winfo_width())
            canvas.configure(scrollregion=(0, 0, req, 40))
            if req > avail:
                hscroll.pack(side="bottom", fill="x")
            else:
                hscroll.pack_forget()
        except Exception:
            pass

    def _reorder_bottom_sections(self):
        """Replace l'ordre de packing des sections inférieures de la fenêtre.

        Ordre du bas vers le haut : status_bar, advanced_frame (si déverrouillé),
        detail_frame. Le tree (expand=True) absorbe l'espace restant."""
        try:
            if hasattr(self, "status_bar"):
                self.status_bar.pack_forget()
            if hasattr(self, "advanced_frame"):
                self.advanced_frame.pack_forget()
            if hasattr(self, "detail_frame"):
                self.detail_frame.pack_forget()
            if hasattr(self, "status_bar"):
                self.status_bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
            if self.advanced_mode and hasattr(self, "advanced_frame"):
                self.advanced_frame.pack(side="bottom", fill="x", padx=12, pady=4)
            if hasattr(self, "detail_frame") and getattr(self, "_details_visible", True):
                self.detail_frame.pack(side="bottom", fill="x", padx=12, pady=4)
        except Exception:
            pass

    def _on_window_resize(self, event=None):
        """Adapte la largeur des colonnes du Treeview quand la fenêtre est
        redimensionnée. Les colonnes stretch=True (status, last_update, error)
        s'expandent ou se contractent pour occuper l'espace disponible,
        évitant ainsi tout débordement horizontal inutile."""
        if not hasattr(self, "tree") or not self.tree.winfo_ismapped():
            return
        # Évite de recalculer à chaque micro-événement : on ne traite que les
        # changements de largeur significatifs (différence d'au moins 5px).
        new_w = self.tree.winfo_width()
        if new_w <= 10:
            return
        if hasattr(self, "_last_tree_width") and abs(new_w - self._last_tree_width) < 5:
            return
        self._last_tree_width = new_w
        try:
            self._adjust_stretch_columns()
        except Exception:
            pass

    # Synchronise la zone défilante des actions de l'en-tête quand la
        # fenêtre change de largeur.
        self._sync_actions_scroll()

    # Largeurs minimales par colonne : garantissent la lisibilité du contenu
    # même lorsque la fenêtre est très réduite (l'en-tête reste lisible).
    COLUMN_MIN_WIDTHS = {
        "ip": 105, "tag": 75, "location": 95, "csv_model": 90,
        "status": 85, "toner": 140, "drum": 140, "bottle": 90,
        "staples": 65, "last_update": 95, "error": 110,
    }

    def _min_col_width(self, col):
        """Largeur minimale cohérente pour une colonne : plancher métier +
        portion lisible de l'en-tête mesuré."""
        floor = self.COLUMN_MIN_WIDTHS.get(col, 70)
        try:
            heading_font = tkfont.nametofont("TkHeadingFont")
            head_w = heading_font.measure(self.tree.heading(col, "text") or "")
        except Exception:
            head_w = len(self.tree.heading(col, "text") or "") * 8
        return max(floor, int(head_w * 0.55) + 16)

    def _adjust_stretch_columns(self):
        """Ajuste toutes les colonnes à la largeur disponible du tableau.

        Fenêtre réduite : les colonnes non verrouillées par l'utilisateur
        rétrécissent au prorata de leur marge disponible (largeur actuelle -
        minimum), sans jamais passer sous leur largeur minimale ; une scrollbar
        horizontale n'apparaît que si même les minima ne suffisent plus.
        Fenêtre élargie : les colonnes précédemment rétrécies reprennent
        d'abord leur largeur de contenu, puis le surplus restant est réparti
        entre les colonnes extensibles (status, last_update, error).
        Les colonnes dimensionnées manuellement (glissement ou double-clic)
        ne sont jamais modifiées."""
        tree = self.tree
        if not tree or not tree.winfo_ismapped():
            return
        if tree.winfo_width() < 50:
            return  # Layout pas encore étalonné
        user_sized = getattr(self, "_user_sized_cols", set())
        current = {c: max(1, tree.column(c, "width")) for c in self.columns}
        mins = {c: self._min_col_width(c) for c in self.columns}
        # Synchronise le minwidth Tk avec nos minima : sans cela, Tk compresse
        # lui-même les colonnes stretch jusqu'à 20px quand le total dépasse la
        # largeur du tableau, en ignorant nos largeurs minimales.
        for c in self.columns:
            tree.column(c, minwidth=mins[c])
        # Réserve pour la scrollbar verticale
        avail = max(200, tree.winfo_width() - 20)
        total = sum(current.values())

        if total > avail:
            # ── Manque de place : rétrécit au prorata de la marge disponible ──
            shrinkable = [c for c in self.columns if c not in user_sized]
            slack = {c: max(0, current[c] - mins[c]) for c in shrinkable}
            total_slack = sum(slack.values())
            excess = total - avail
            if total_slack <= 0:
                return
            if excess >= total_slack:
                # Même les minima ne suffisent pas : tout au minimum, une
                # scrollbar horizontale apparaîtra pour le reste.
                for c in shrinkable:
                    tree.column(c, width=mins[c])
                    self._shrunk_cols.add(c)
                return
            for c in shrinkable:
                if slack[c] > 0:
                    reduce_by = max(1, int(excess * slack[c] / total_slack))
                    new_w = max(mins[c], current[c] - reduce_by)
                    tree.column(c, width=new_w)
                    if new_w < current[c]:
                        self._shrunk_cols.add(c)
            return

        # ── Surplus : restaure puis répartit ──
        surplus = avail - total
        # 1) Restaure les colonnes rétrécies vers leur largeur de contenu.
        for c in list(self._shrunk_cols):
            target = self._measure_column(c)
            if target and current[c] < target:
                gain = min(target - current[c], surplus)
                if gain > 0:
                    tree.column(c, width=current[c] + gain)
                    surplus -= gain
                    current[c] += gain
            if current[c] >= (target or current[c]):
                self._shrunk_cols.discard(c)
            if surplus <= 0:
                return
        # 2) Répartit le surplus restant entre les colonnes extensibles.
        stretch_cols = [c for c in ("status", "last_update", "error")
                        if c not in user_sized]
        if not stretch_cols or surplus <= 0:
            return
        total_cur = sum(current[c] for c in stretch_cols)
        for c in stretch_cols:
            extra = int(surplus * current[c] / max(1, total_cur))
            tree.column(c, width=max(mins[c], current[c] + extra))

    def _on_detail_bar_configure(self, event=None, content_height=None):
        """Ajuste la zone scrollable des barres de consommables.

        La hauteur du canvas suit la hauteur du contenu tant qu'il tient dans
        l'espace réellement disponible dans la fenêtre ; au-delà, une barre de
        défilement verticale apparaît pour consulter le reste. La hauteur max
        est calculée dynamiquement afin que le menu avancé et la barre de
        statut restent toujours visibles, quelle que soit la taille de la
        fenêtre ou le nombre de consommables.
        """
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))
        if content_height is None:
            # Priorité au scrollregion bbox (hauteur réelle du contenu) : event.height
            # peut refléter un état intermédiaire de layout (ex. hauteur du canvas
            # initialisée à 200px au démarrage, ou hauteur du frame avant
            # l'étalonnage des nouvelles barres). Le bbox reflète la vraie
            # hauteur demandée par le contenu.
            bbox = self.detail_canvas.bbox("all")
            if bbox:
                content_height = bbox[3]
            elif event is not None:
                content_height = event.height
            else:
                content_height = 0
        # Hauteur maximale dynamique : au-delà, une barre de défilement
        # verticale apparaît. On tient compte de la hauteur de fenêtre pour que
        # le panneau Détails ne pousse jamais le menu avancé ou la barre de
        # statut hors de la fenêtre, tout en donnant assez d'espace aux barres
        # de consommables sur les grands écrans.
        win_h = self.root.winfo_height() if self.root.winfo_ismapped() else 700
        # Sur petits écrans : limite plus petite ; sur grands écrans : plus
        # grande, mais plafonnée pour laisser place aux autres panneaux.
        if win_h < 500:
            max_height = 120
        elif win_h < 800:
            max_height = 200
        else:
            max_height = 240
        new_height = min(content_height, max_height)
        if self.detail_canvas.winfo_height() != new_height:
            self.detail_canvas.configure(height=new_height)
        # Affiche la scrollbar uniquement si le contenu dépasse la hauteur max.
        if content_height > max_height:
            if not self.detail_canvas_vscroll.winfo_ismapped():
                self.detail_canvas_vscroll.pack(side="right", fill="y")
        else:
            if self.detail_canvas_vscroll.winfo_ismapped():
                self.detail_canvas_vscroll.pack_forget()

    def _create_advanced_frame(self):
        """Crée le cadre regroupant les options développeur/debug du mode avancé.

        Aucune fonctionnalité n'est supprimée : ce cadre (et les boutons qu'il
        contient, notamment le Diagnostic SNMP) reste
        masqué tant que le mot de passe n'a pas été saisi. La visibilité est
        pilotée par _apply_advanced_visibility().
        """
        self.advanced_frame = ttk.LabelFrame(self.root, text="Mode avancé (développeur / debug)")
        advanced_inner = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        advanced_inner.pack(fill="x", padx=4, pady=4)

        ttk.Button(advanced_inner, text="Diagnostic SNMP", command=self.diagnostic_snmp).pack(side="left", padx=4)
        ttk.Button(advanced_inner, text="OID bruts", command=self.open_raw_oids).pack(side="left", padx=4)
        ttk.Button(advanced_inner, text="Logs détaillés", command=self.open_logs).pack(side="left", padx=4)
        ttk.Button(advanced_inner, text="Rapprochement de parc",
                   command=self.open_presta_sync).pack(side="left", padx=4)
        ttk.Button(advanced_inner, text="Fonctions de test", command=self.open_test_functions).pack(side="left", padx=4)
        ttk.Separator(advanced_inner, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(advanced_inner, text="Verrouiller", command=self.lock_advanced).pack(side="left", padx=4)


        # Masqué par défaut : seules les fonctions de base sont visibles tant
        # que le mode avancé n'est pas déverrouillé par le mot de passe.
        self.advanced_frame.pack_forget()

    def open_presta_sync(self):
        """Ouvre la fenêtre de comparaison / mise à jour CMDB avec Presta RMS.

        Ce module est accessible uniquement depuis le mode avancé. Il importe
        le fichier mensuel Presta RMS, le rapproche avec la CMDB chargée et
        permet à l'utilisateur de valider précisément les changements à
        appliquer. L'analyse ne modifie JAMAIS la CMDB sans validation.
        """
        try:
            from .presta.dialog import PrestaSyncDialog
        except Exception as exc:
            messagebox.showerror(
                "Module Presta RMS",
                f"Impossible de charger le module de comparaison Presta RMS : {exc}",
            )
            return
        # Vérifie qu'une CMDB est bien disponible avant d'ouvrir la fenêtre.
        if not self.config.get("cmdb_path"):
            messagebox.showwarning(
                "CMDB requise",
                "Aucune CMDB mémorisée. Chargez d'abord votre fichier CMDB "
                "via le bouton « Charger CMDB », puis relancez la comparaison.",
            )
            return
        PrestaSyncDialog(self.root, self)

    def authenticate_advanced(self):
        """Affiche la boîte de dialogue du mot de passe du mode avancé."""
        if self.advanced_mode:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Mode avancé")
        dialog.grab_set()
        dialog.resizable(False, False)
        style_toplevel(dialog)
        ttk.Label(dialog, text="Saisissez le mot de passe du mode avancé :").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(12, 4))
        pw_var = StringVar()
        entry = ttk.Entry(dialog, textvariable=pw_var, show="*", width=28)
        entry.grid(row=1, column=0, columnspan=2, padx=12, pady=4)
        entry.focus_set()

        def submit(_event=None):
            if verify_advanced_password(pw_var.get()):
                dialog.destroy()
                self.advanced_mode = True
                self._apply_advanced_visibility()
                self.log_status("Mode avancé activé : options développeur affichées.")
            else:
                messagebox.showerror("Mode avancé", "Mot de passe incorrect.")
                pw_var.set("")
                entry.focus_set()

        ttk.Button(dialog, text="Valider", command=submit).grid(row=2, column=0, padx=12, pady=(8, 4))
        ttk.Button(dialog, text="Annuler", command=dialog.destroy).grid(row=2, column=1, padx=12, pady=(8, 4))
        dialog.bind("<Return>", submit)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def lock_advanced(self):
        """Verrouille le mode avancé : masque à nouveau les options développeur."""
        self.advanced_mode = False
        self._apply_advanced_visibility()
        self.log_status("Mode avancé verrouillé.")

    def _apply_advanced_visibility(self):
        """Affiche ou masque le cadre du mode avancé selon l'état d'authentification."""
        if self.advanced_mode:
            self.advanced_toggle_btn.config(text="Mode avancé (actif)", command=self.lock_advanced)
        else:
            self.advanced_toggle_btn.config(text="Mode avancé", command=self.authenticate_advanced)
        if hasattr(self, "advanced_frame"):
            self._reorder_bottom_sections()
        # Recalcule la hauteur de la zone scrollable si nécessaire.
        if hasattr(self, "detail_canvas"):
            self.root.after_idle(self._on_detail_bar_configure)

    def _selected_device(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return self.device_items.get(selection[0])

    def _visible_devices(self):
        """Retourne les appareils actuellement affichés dans la liste, c'est-à-dire
        résultant du filtre "Grand site" en vigueur (Tous -> tout le CMDB)."""
        selected_site = self.site_filter.get()
        return [d for d in self.devices if selected_site == "Tous" or d.site == selected_site]

    def open_raw_oids(self):
        """Visualiseur d'OID bruts : effectue un SNMP WALK non filtré
        (1.3.6.1.2.1.43.11 puis 1.3.6.1.4.1.641) sur l'imprimante sélectionnée et
        affiche chaque OID avec son type et sa valeur brute."""
        device = self._selected_device()
        if not device:
            messagebox.showwarning("OID bruts", "Sélectionnez d'abord une imprimante dans la liste.")
            return
        community = self.snmp_community.get()
        try:
            port = int(self.snmp_port.get())
        except Exception:
            port = 161

        dialog = tk.Toplevel(self.root)
        dialog.title(f"OID bruts - {device.ip}")
        dialog.resizable(True, True)
        style_toplevel(dialog)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            dialog, "oid_bruts", default_size=(780, 520), min_size=(520, 320))

        head = ttk.Frame(dialog)
        head.pack(fill="x", padx=6, pady=4)
        status_lbl = ttk.Label(head, text="Collecte des OID en cours...")
        status_lbl.pack(side="left")

        text = tk.Text(dialog, wrap="none", font=("Consolas", 9))
        vs = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        hs = ttk.Scrollbar(dialog, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")

        def worker():
            lines = []
            for base in ("1.3.6.1.2.1.43.11", "1.3.6.1.4.1.641"):
                lines.append(f"### SNMP WALK {base} ###")
                try:
                    rows = device.snmp_walk_raw(community, port, base)
                except Exception as exc:
                    lines.append(f"ERREUR : {exc}")
                    continue
                lines.extend(f"{oid}\t{typ}\t{val}" for oid, typ, val in rows)
                lines.append(f"-- {len(rows)} OID(s) pour {base} --")
                lines.append("")
            content = "\n".join(lines) if lines else "Aucun OID collecté."
            self.root.after(0, lambda: _fill(content))

        def _fill(content):
            text.delete("1.0", END)
            text.insert(END, content)
            total_lines = int(text.index("end-1c").split(".")[0])
            status_lbl.config(text=f"Terminé : {total_lines} lignes")

        threading.Thread(target=worker, daemon=True).start()

    def open_logs(self):
        """Visualiseur des logs détaillés : affiche la fin du fichier
        printer_monitor.log enregistré par l'application."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Logs détaillés")
        dialog.resizable(True, True)
        style_toplevel(dialog)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            dialog, "logs", default_size=(840, 620), min_size=(560, 360))

        head = ttk.Frame(dialog)
        head.pack(fill="x", padx=6, pady=4)
        try:
            size = LOG_FILE.stat().st_size
            info = f"{LOG_FILE.name} ({size} octets) - 4000 dernières lignes"
        except Exception:
            info = "Fichier de log indisponible"
        ttk.Label(head, text=info).pack(side="left")
        ttk.Button(head, text="Actualiser", command=lambda: _load()).pack(side="right", padx=4)

        text = tk.Text(dialog, wrap="none", font=("Consolas", 9))
        vs = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        hs = ttk.Scrollbar(dialog, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")

        def _load():
            try:
                data = LOG_FILE.read_text(encoding="utf-8", errors="replace")
                content = "\n".join(data.splitlines()[-4000:])
            except Exception as exc:
                content = f"Impossible de lire les logs : {exc}"
            text.delete("1.0", END)
            text.insert(END, content)
            text.see("end")

        _load()

    def open_test_functions(self):
        """Fonctions de test : permet de lancer rapidement quelques vérifications
        (ping, SNMP GET) sur l'appareil sélectionné."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Fonctions de test")
        dialog.resizable(True, True)
        style_toplevel(dialog)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            dialog, "fonctions_test", default_size=(700, 420),
            min_size=(480, 300))

        controls = ttk.Frame(dialog)
        controls.pack(fill="x", padx=6, pady=4)
        ttk.Button(controls, text="Tester le ping (appareil sélectionné)",
                   command=lambda: _start("ping")).pack(side="left", padx=4)
        ttk.Button(controls, text="Ping toute la liste",
                   command=lambda: _start("ping_all")).pack(side="left", padx=4)
        ttk.Button(controls, text="Test SNMP GET (sysName)",
                   command=lambda: _start("snmp")).pack(side="left", padx=4)
        ttk.Button(controls, text="Exporter CSV des pings",
                   command=lambda: self.root.after(0, _export_ping_csv)).pack(side="left", padx=4)

        out = tk.Text(dialog, wrap="word", font=("Consolas", 9))
        vs = ttk.Scrollbar(dialog, orient="vertical", command=out.yview)
        out.configure(yscrollcommand=vs.set)
        out.pack(side="left", fill="both", expand=True, padx=6, pady=(0, 6))
        vs.pack(side="right", fill="y")

        def _log(msg):
            out.insert(END, msg + "\n")
            out.see("end")

        def _ping():
            device = self._selected_device()
            if not device:
                self.root.after(0, lambda: _log("Aucune imprimante sélectionnée."))
                return
            ip = device.ip
            tag = device.tag or ""
            self.root.after(0, lambda: _log(f"Ping {ip} ({tag})..."))
            ok = _ping_host(ip)
            status = "OK" if ok else "INJOIGNABLE"
            ping_results[:] = [(ip, tag, status)]
            self.root.after(0, lambda: _log(f"Ping {ip} ({tag}) : {status}"))

        def _ping_all():
            devices = list(self._visible_devices())
            if not devices:
                self.root.after(0, lambda: _log("Aucune imprimante affichée (chargez un CMDB et vérifiez le filtre de Grand site)."))
                return
            self.root.after(0, lambda: _log(
                f"Ping des {len(devices)} appareil(s) affiché(s) "
                f"(filtre : {self.site_filter.get()})..."))

            def do_one(device):
                return device.ip, (device.tag or ""), _ping_host(device.ip)

            try:
                with ThreadPoolExecutor(max_workers=16) as ex:
                    for ip, tag, ok in ex.map(do_one, devices):
                        status = "OK" if ok else "INJOIGNABLE"
                        ping_results.append((ip, tag, status))
                        self.root.after(
                            0,
                            lambda ip=ip, tag=tag, status=status: _log(
                                f"Ping {ip} ({tag}) : {status}"),
                        )
                self.root.after(0, lambda: _log("Ping de toute la liste terminé."))
            except Exception as exc:
                self.root.after(0, lambda: _log(f"Erreur pendant le ping : {exc}"))

        def _export_ping_csv():
            if not ping_results:
                self.root.after(0, lambda: _log("Aucun résultat de ping à exporter."))
                return
            path = filedialog.asksaveasfilename(
                title="Exporter les résultats de ping vers CSV",
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv"), ("Tous les fichiers", "*")],
            )
            if not path:
                return
            try:
                # Même mise en page que l'export du rafraîchissement principal :
                # encodage UTF-8 avec BOM (compatible Excel) et séparateur ';'.
                with open(path, "w", newline="", encoding="utf-8-sig") as csvfile:
                    writer = csv.writer(csvfile, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                    writer.writerow(["Adresse IP", "Tatouage", "Statut"])
                    for ip, tag, status in ping_results:
                        writer.writerow([ip, tag, status])
                self.root.after(
                    0,
                    lambda: _log(f"Résultats de ping exportés vers {os.path.basename(path)}."),
                )
            except Exception as exc:
                self.root.after(0, lambda: _log(f"Erreur export CSV : {exc}"))

        def _snmp_get():
            device = self._selected_device()
            if not device:
                self.root.after(0, lambda: _log("Aucune imprimante sélectionnée."))
                return
            community = self.snmp_community.get()
            try:
                port = int(self.snmp_port.get())
            except Exception:
                port = 161
            ip = device.ip
            self.root.after(0, lambda: _log(f"SNMP GET {ip} sysName ({SYS_NAME_OID})..."))
            try:
                value = device.snmp_get(community, port, SYS_NAME_OID)
                self.root.after(0, lambda: _log(f"sysName de {ip} : {value or '(non renseigné)'}"))
            except Exception as exc:
                self.root.after(0, lambda: _log(f"SNMP GET échoué : {exc}"))

        def _start(which):
            if which == "ping":
                threading.Thread(target=_ping, daemon=True).start()
            elif which == "ping_all":
                threading.Thread(target=_ping_all, daemon=True).start()
            elif which == "snmp":
                threading.Thread(target=_snmp_get, daemon=True).start()

        out.insert(END, "Sélectionnez un test ci-dessus.\n")

        # Résultats des pings (adresse IP, tatouage, statut) collectés pour l'export CSV.
        ping_results = []

    def log_status(self, message):
        self.status_label.config(text=message)

    def open_help(self):
        """Ouvre la fenêtre d'aide intégrée (documentation utilisateur).

        La fenêtre est construite par printer_monitor/help.py avec le thème
        de l'application ; sa section « Licence » indique où consulter les
        conditions complètes (fichier LICENSE du projet) et donne un accès
        direct à la fenêtre dédiée « Licence ».
        """
        show_help(self.root)

    def open_license(self):
        """Ouvre la fenêtre « Licence » qui affiche le contenu intégral du
        fichier LICENSE du projet, sans modification.

        La fenêtre est construite par printer_monitor/help.py avec le thème
        de l'application ; si le fichier LICENSE est absent ou vide, un
        résumé des conditions est affiché à la place.
        """
        show_license(self.root)

    def open_tls_certs(self):
        """Fenetre Certificats HTTPS : suivi observe/gere, diagnostics, actions."""
        try:
            mgr = self._get_tls_manager()
            store = mgr.store if mgr else None
            if store is None:
                from .tls_certs import CertStore as _CS
                store = _CS()
        except Exception as exc:
            messagebox.showerror("Certificats", "Registre inaccessible : %s" % exc)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Certificats HTTPS des imprimantes")
        dialog.resizable(True, True)
        try:
            style_toplevel(dialog)
        except Exception:
            pass
        try:
            setup_secondary_window(dialog, "certificats", default_size=(900, 560),
                                   min_size=(680, 420))
        except Exception:
            dialog.geometry("900x560")
        try:
            dialog.transient(self.root)
        except Exception:
            pass
        info_txt = ("Suivi automatique pendant le rafraichissement. "
                    "Les certificats approuves par Windows sont suivis sans installation. "
                    "Autosignes / autorites inconnues : approbation explicite requise. "
                    "Ctrl+clic sur l'IP ouvre toujours l'interface dans le navigateur.")
        ttk.Label(dialog, text=info_txt, wraplength=860, justify="left").pack(
            fill="x", padx=8, pady=6)
        cols = ("ip", "statut", "sujet", "emetteur", "fin", "empreinte")
        tree = ttk.Treeview(dialog, columns=cols, show="headings", height=16)
        for c, w, head in (("ip", 110, "IP"), ("statut", 150, "Statut"),
                           ("sujet", 170, "Sujet"), ("emetteur", 170, "Emetteur"),
                           ("fin", 150, "Fin validite"), ("empreinte", 200, "Empreinte")):
            tree.heading(c, text=head)
            tree.column(c, width=w, stretch=(c in ("sujet", "emetteur", "empreinte")))
        tree.pack(fill="both", expand=True, padx=8, pady=4)
        try:
            data = store._data
            for ip, entry in sorted((data.get("printers", {}) or {}).items()):
                fp = (entry or {}).get("fingerprint", "")
                obs = (data.get("observations", {}) or {}).get(fp, {})
                m = (data.get("managed", {}) or {}).get(fp)
                if m and m.get("origin") == "printer-monitor":
                    statut = "gere par l'app"
                elif obs.get("is_expired"):
                    statut = "expire"
                elif not obs.get("identity_ok", True):
                    statut = "identite incompatible"
                elif obs.get("is_self_signed"):
                    statut = "autosigne a approuver"
                else:
                    statut = "observe"
                tree.insert("", "end", values=(ip, statut, obs.get("subject_cn", ""),
                                               obs.get("issuer_cn", ""), obs.get("not_after", ""),
                                               (fp[:27] + "...") if fp else ""))
        except Exception as exc:
            tree.insert("", "end", values=("", "erreur lecture : %s" % exc, "", "", "", ""))
        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="Resonder maintenant",
                   command=lambda: self._tls_rescan_now(dialog)).pack(side="left", padx=4)
        ttk.Button(btns, text="Nettoyer les geres orphelins",
                   command=lambda: self._tls_cleanup_managed(dialog)).pack(side="left", padx=4)
        ttk.Button(btns, text="Fermer", command=dialog.destroy).pack(side="right", padx=4)
        try:
            self._tls_attention = 0
            self._update_tls_button()
        except Exception:
            pass

    def _tls_rescan_now(self, dialog=None):
        try:
            mgr = self._get_tls_manager()
            if mgr is None:
                return
            with mgr._lock:
                mgr._cache.clear()
            self._launch_tls_refresh(list(self.devices or []))
            self.log_status("Sondes TLS relancees en arriere-plan.")
        except Exception as exc:
            messagebox.showerror("Certificats", "Rescan impossible : %s" % exc)

    def _tls_cleanup_managed(self, dialog=None):
        try:
            mgr = self._get_tls_manager()
            if mgr is None:
                return
            fps = list((mgr.store._data.get("managed", {}) or {}).keys())
            done = 0
            for fp in fps:
                try:
                    if mgr.cleanup_managed_if_orphan(fp).get("removed"):
                        done += 1
                except Exception:
                    pass
            messagebox.showinfo("Certificats",
                                "%d certificat(s) gere(s) orphelin(s) retire(s). "
                                "Les certificats tiers ne sont jamais touches." % done)
            if dialog is not None:
                try:
                    dialog.destroy()
                except Exception:
                    pass
                self.open_tls_certs()
        except Exception as exc:
            messagebox.showerror("Certificats", "Nettoyage impossible : %s" % exc)

    def load_cmdb(self):
        path = filedialog.askopenfilename(
            title="Sélectionner le fichier CMDB",
            filetypes=[("Fichiers CMDB", ("*.xlsx", "*.csv")), ("Excel", "*.xlsx"), ("CSV", "*.csv"), ("All files", "*")],
        )
        if not path:
            return
        try:
            self.devices = self.parse_file(path)
            self.device_items.clear()
            self.tree.delete(*self.tree.get_children())
            # Reconciliation TLS : oublie les IP qui ne sont plus dans le parc
            # (imprimantes supprimees / IP reaffectees). Les certificats GERES
            # restent proteges : seul le suivi d'observation est oublie, jamais
            # le magasin Windows (nettoyage explicite via Certificats).
            try:
                mgr = self._get_tls_manager()
                if mgr is not None:
                    live = {getattr(d, "ip", "") for d in (self.devices or [])}
                    known = list((mgr.store._data.get("printers", {}) or {}).keys())
                    for old_ip in known:
                        if old_ip not in live:
                            mgr.cleanup_printer(old_ip)
            except Exception:
                pass
            self.update_site_filter_options()
            self.populate_tree()
            self.log_status(f"{len(self.devices)} imprimante(s) chargée(s) depuis {os.path.basename(path)}.")
            # Mémorise le chemin du fichier CMDB dans la configuration pour un
            # rechargement automatique au prochain démarrage.
            self.config["cmdb_path"] = path
            save_json_config(self.config)
            # Chargement initial terminé : (re)démarre le compte à rebours du
            # rafraîchissement automatique (s'il est activé).
            self._restart_autorefresh()
        except Exception as exc:
            messagebox.showerror("Erreur CMDB", f"Impossible de charger le fichier CMDB : {exc}")

    def _load_saved_cmdb(self):
        """Charge automatiquement la CMDB mémorisée dans la configuration, si
        le fichier existe toujours et est accessible. Si le fichier mémorisé
        n'existe plus (supprimé, déplacé, inaccessible) ou est illisible,
        l'utilisateur est invité à sélectionner un nouveau fichier CMDB."""
        saved_path = (self.config or {}).get("cmdb_path", "")
        if not saved_path:
            # Premier lancement (aucune CMDB mémorisée) : l'utilisateur
            # sélectionnera manuellement un fichier via « Charger CMDB ».
            return
        path = Path(saved_path)
        if not path.is_file():
            # Fichier mémorisé introuvable (supprimé, déplacé ou inaccessible) :
            # demande à l'utilisateur d'en sélectionner un nouveau.
            self._prompt_for_missing_cmdb(path)
            return
        try:
            self.devices = self.parse_file(str(path))
            self.device_items.clear()
            self.tree.delete(*self.tree.get_children())
            self.update_site_filter_options()
            self.populate_tree()
            self.log_status(f"{len(self.devices)} imprimante(s) chargée(s) depuis {path.name}.")
            # Chargement initial terminé : (re)démarre le compte à rebours du
            # rafraîchissement automatique (s'il est activé).
            self._restart_autorefresh()
        except Exception:
            # Fichier inaccessible ou illisible : ne pas bloquer le démarrage,
            # mais proposer à l'utilisateur de sélectionner une nouvelle CMDB.
            self._prompt_for_missing_cmdb(path)

    def _prompt_for_missing_cmdb(self, path):
        """Informe l'utilisateur que la CMDB mémorisée est introuvable ou
        illisible et lui propose de sélectionner immédiatement un nouveau
        fichier CMDB (réutilise la fonctionnalité existante load_cmdb)."""
        try:
            answer = messagebox.askyesno(
                "CMDB introuvable",
                f"Le dernier fichier CMDB mémorisé est introuvable ou inaccessible :\n"
                f"{path}\n\n"
                "Voulez-vous sélectionner un nouveau fichier CMDB maintenant ?",
                parent=self.root,
            )
        except Exception:
            # Interface non disponible : ne jamais bloquer le démarrage.
            return
        if answer:
            self.load_cmdb()

    def parse_file(self, path):
        lower = path.lower()
        if lower.endswith(('.xlsx', '.xlsm', '.xltx', '.xltm')):
            return self.parse_xlsx(path)
        return self.parse_csv(path)

    def parse_xlsx(self, path):
        if openpyxl is None:
            raise ImportError("Le support des fichiers Excel nécessite openpyxl. Installez-le depuis requirements.txt.")
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if "CMDB" in workbook.sheetnames:
                sheet_name = "CMDB"
            elif len(workbook.sheetnames) >= 3:
                sheet_name = workbook.sheetnames[2]
            else:
                sheet_name = workbook.sheetnames[0]
            sheet = workbook[sheet_name]
            rows = list(sheet.iter_rows(values_only=True))
            use_cmdb_style = sheet_name.lower() == "cmdb" or (len(workbook.sheetnames) >= 3 and sheet_name == workbook.sheetnames[2])
        finally:
            workbook.close()
        if not rows:
            raise ValueError("Le fichier Excel est vide.")
        if use_cmdb_style:
            return self._parse_cmdb_rows(rows)
        headers = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
        data_rows = rows[1:]
        return self._parse_rows(headers, data_rows)

    def parse_csv(self, path):
        with open(path, newline="", encoding="utf-8-sig") as csvfile:
            sample = csvfile.read(2048)
            csvfile.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            reader = csv.reader(csvfile, dialect)
            headers = next(reader, None)
            rows = list(reader)
        return self._parse_rows(headers, rows)

    def _parse_cmdb_rows(self, rows):
        devices = []
        for row_index, row in enumerate(rows[1:], start=2):
            if not row:
                continue
            cells = [str(cell).strip() if cell is not None else "" for cell in row]
            site = cells[0] if len(cells) > 0 else ""
            ip = normalize_ip(cells[3] if len(cells) > 3 else "")
            tag = cells[4] if len(cells) > 4 else ""
            csv_model = cells[6] if len(cells) > 6 else ""
            location = cells[12] if len(cells) > 12 else ""
            if not ip:
                continue
            if _is_special_ip(ip):
                continue
            devices.append(
                PrinterDevice(
                    ip,
                    tag,
                    brand="",
                    csv_model=csv_model,
                    site=site,
                    location=location,
                    row_info={"row": row_index, "raw": row},
                )
            )
        if not devices:
            raise ValueError("Aucune adresse IP valide trouvée dans le fichier CMDB.")
        devices.sort(key=lambda d: ((d.site or "").lower(), d.ip))
        return devices

    # ── Diagnostic temporaire du TRI PAR DÉFAUT ─────────────────────────────
    # Le diagnostic est activé UNIQUEMENT en mode « par défaut » (libellé
    # canonique « Défaut (alertes en premier) », identifiant historique
    # « default » ou toute valeur inconnue) : jamais dans les modes par niveau.
    # Il écrit, pour chaque machine, la priorité calculée depuis l'état réel
    # (_get_device_priority — jamais la colonne « Messages ») puis l'ordre FINAL
    # juste avant le rendu. Ces logs sont TEMPORAIRES et seront retirés une fois
    # la cause confirmée.
    @staticmethod
    def _default_sort_priority_label(priority):
        return {
            0: "hors ligne",
            1: "down",
            2: "remplacement",
            3: "avertissement",
            4: "normal",
        }.get(priority, "inconnu(%r)" % (priority,))

    def _is_default_sort_active(self):
        try:
            mode = self.sort_mode.get()
        except Exception:
            mode = ""
        # Même détection que _device_sort_key : tout ce qui n'est pas un tri
        # « par niveau » tombe dans la branche par défaut.
        return mode not in ("Niveau bas → haut", "Niveau haut → bas")

    def _log_default_sort(self, devices, stage):
        """Log de diagnostic du tri par défaut (DESACTIVE en production).

        Le diagnostic écrivait en APPEND synchrone dans
        ``printer_monitor_sort_debug.log`` à CHAQUE passe (populate + lots de
        200 ms) : fichier de 69 Mo constaté, I/O disque à chaque
        rafraîchissement, ralentissement visible de la liste des machines.
        Réactivable explicitement via PRINTERMONITOR_SORT_DEBUG=1.
        """
        if os.environ.get("PRINTERMONITOR_SORT_DEBUG", "").strip().lower() not in (
            "1", "true", "yes", "on", "oui",
        ):
            return
        if not self._is_default_sort_active():
            return
        try:
            from pathlib import Path
            path = Path.cwd() / "printer_monitor_sort_debug.log"
            lines = ["[SORT %s] mode=%r" % (stage, self.sort_mode.get())]
            for d in devices:
                try:
                    ip = d.ip
                except Exception:
                    ip = "?"
                # Statut réel affiché entre crochets pour corréler facilement
                # priorité et état machine dans le diagnostic.
                statut = (getattr(d, "status", "") or
                          getattr(d, "reachability", "") or "?")
                p = self._get_device_priority(d)
                lines.append("  Machine %s [%s] -> priorite=%d -> %s" % (
                    ip, statut, p, self._default_sort_priority_label(p)))
            lines.append("  FINAL: " + " -> ".join(
                getattr(d, "ip", "?") for d in devices))
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.device_items.clear()
        selected_site = self.site_filter.get()
        sorted_devices = sorted(self.devices, key=self._device_sort_key)
        self._log_default_sort(sorted_devices, "populate")
        for device in sorted_devices:
            if selected_site != "Tous" and device.site != selected_site:
                continue
            toner = device.get_supply_summary("toner") or "-"
            drum = device.get_supply_summary("drum") or "-"
            # Bottle and staples status
            bottle = device.get_bottle_status(self.waste_toner_threshold.get())
            staples = device.get_staples_status(10)
            tags = []
            # Global device state: the most critical supply alert drives the
            # whole printer row colour. Reachability (SNMP timeout / offline)
            # takes priority over supply-based alerts.
            if device.reachability == "snmp_timeout":
                tags.append("snmp_timeout")
            elif device.reachability == "offline":
                tags.append("snmp_offline")
            elif device.error:
                tags.append("error")
            elif (device.status or "").strip().lower() == "down":
                # Statut « down » (hrDeviceStatus=5) = imprimante en erreur :
                # affichée en rouge vif, distinct des simples imprimantes hors
                # ligne. Un effet clignotant est appliqué par _blink_down_rows().
                tags.append("device_down")
            else:
                global_state = self._get_device_global_state(device)
                if global_state == "critical":
                    tags.append("device_critical")
                elif global_state == "low":
                    tags.append("device_low")
            item = self.tree.insert(
                "",
                "end",
                values=(device.ip, device.tag, device.location, device.csv_model, device.status, toner, drum, bottle, staples, device.last_update, self._device_messages(device)),
                tags=tags,
            )
            self.device_items[item] = device

        # Redimensionnement automatique des colonnes selon le contenu (type Excel)
        self.auto_size_columns()
        # Nouvelle liste (changement de Grand site / filtre / CMDB / tri) :
        # retour en haut APRES les inserts, comme un Treeview standard. Avant
        # les inserts, la table est vide (scrollregion = vue → moveto sans
        # effet) et la vue restait scrollee sur l'ancienne position — au-dela
        # du nouveau contenu si la liste a raccourci → affichage fige + trous
        # jusqu'au prochain scroll manuel.
        # Le moveto declenche un _render() synchrone : on (re)marque les lignes
        # comme remplacees APRES, sinon le flag est consomme par ce rendu
        # intermediaire (table pas encore repeuplee) et le rendu final recycle
        # des slots obsoletes → trous.
        try:
            self.tree.yview_moveto(0.0)
        except Exception:
            pass
        try:
            self.tree._rows_replaced = True
            self.tree._all_dirty = True
            # Rendu SYNCHRONE : le moveto a pu s'executer avec l'ancienne
            # scrollregion (rendu intermediaire coalesce par after_idle),
            # et le flag _rows_replaced pose apres n'aurait ete consomme
            # qu'au prochain scroll → affichage fige + trous. Le redraw
            # force la repeinture complete immediate (lignes visibles).
            self.tree.redraw()
        except Exception:
            pass
        # Recale les chips immédiatement : un redessin SYNCHRONE ici garantit
        # que la peinture écran (effectuée quand Tk reprend la boucle
        # d'événements) voit d'un seul coup lignes ET pastilles recalées — pas
        # de fenêtre où le texte brut du Treeview reste visible pendant les
        # 30 ms du débounce. Coût proportionnel aux seules lignes visibles.
        try:
            self.chip_renderer.redraw()
        except Exception:
            pass
        # Démarre l'effet clignotant pour les imprimantes « down » (léger, ne
        # ralentit pas le rafraîchissement : un seul timer Tkinter global).
        self._start_down_blink()

    def sort_by(self, column, reverse):
        """Ancien tri Treeview par clic sur un en-tête (Adresse IP).

        Vestige de l'ancien système « Treeview » : les commandes d'en-tête
        référencent encore ``self.sort_by`` alors que la méthode avait disparu
        lors du passage au rendu par chips — chaque clic sur
        « Adresse IP » levait donc une AttributeError silencieuse. Surtout, un
        tri par colonne ICI ÉCRASERAIT le tri actif (Default / par niveau) :
        process_poll_results réordonnerait les lignes 200 ms plus tard via
        _reorder_tree_rows, provoquant un va-et-vient visuel. Le clic sur un
        en-tête ré-applique donc simplement le tri métier COURANT
        (populate_tree), sans jamais introduire d'ordre concurrent.
        """
        self.populate_tree()

    def _start_down_blink(self):
        """Démarre (ou relance) le timer de clignotement des imprimantes « down ».
        Un seul timer Tkinter est utilisé pour toute la liste : il alterne la
        couleur de fond des lignes « device_down » entre rouge vif et rouge
        sombre toutes les 500 ms. Le timer s'arrête automatiquement lorsqu'il
        n'y a plus de ligne « down »."""
        if self._down_blink_after_id is not None:
            try:
                self.root.after_cancel(self._down_blink_after_id)
            except Exception:
                pass
            self._down_blink_after_id = None
        # Vérifie s'il existe au moins une ligne « down ».
        has_down = False
        try:
            for item in self.tree.get_children():
                tags = self.tree.item(item, "tags")
                if "device_down" in tags:
                    has_down = True
                    break
        except Exception:
            return
        if not has_down:
            return
        self._down_blink_state = not self._down_blink_state
        # Alterne entre rouge vif et rouge sombre (palette moderne du thème).
        bg = ROW_DOWN if self._down_blink_state else ROW_DOWN_ALT
        try:
            self.tree.tag_configure("device_down", background=bg, foreground="#ffffff")
        except Exception:
            pass
        # Les overlays de chips suivent la couleur clignotante : recoloration
        # ciblée des fonds uniquement (pas de recalcul complet du rendu,
        # qui coûtait un parcours de toute la liste toutes les 500 ms).
        try:
            self.chip_renderer.repaint_row_backgrounds()
        except Exception:
            pass
        self._down_blink_after_id = self.root.after(500, self._start_down_blink)

    def _measure_column(self, col):
        """Largeur en pixels nécessaire pour afficher l'en-tête et le contenu de `col`."""
        try:
            heading_font = tkfont.nametofont("TkHeadingFont")
            cell_font = tkfont.nametofont("TkDefaultFont")
        except Exception:
            heading_font = cell_font = None

        max_width = 0
        header_text = self.tree.heading(col, "text") or ""
        if heading_font is not None:
            max_width = max(max_width, heading_font.measure(header_text))
        else:
            max_width = max(max_width, len(header_text) * 8)

        try:
            idx = self.columns.index(col)
        except ValueError:
            return None

        for item in self.tree.get_children():
            values = self.tree.item(item, "values")
            if idx < len(values):
                cell_text = str(values[idx])
                if cell_font is not None:
                    w = cell_font.measure(cell_text)
                else:
                    w = len(cell_text) * 8
                if w > max_width:
                    max_width = w

        if max_width == 0:
            return None
        # Marge interne (padding), largeur minimale et maximale raisonnable
        return max(70, min(int(max_width) + 30, 400))

    def auto_size_columns(self, cols=None):
        """Redimensionne automatiquement la largeur de chaque colonne au contenu."""
        if not hasattr(self, "tree") or not self.columns:
            return
        for col in (cols or self.columns):
            width = self._measure_column(col)
            if width is not None:
                self.tree.column(col, width=width)

    def auto_size_one_column(self, col_id):
        """Redimensionne une seule colonne à partir de son identifiant interne (ex: \"#2\")."""
        if not hasattr(self, "tree") or not self.columns:
            return
        try:
            n = int(col_id[1:])
        except (ValueError, IndexError):
            return
        idx = n - 1
        if not (0 <= idx < len(self.columns)):
            return
        width = self._measure_column(self.columns[idx])
        if width is not None:
            self.tree.column(self.columns[idx], width=width)
        # Recale les overlays de chips après l'ajustement de largeur :
        # sans cela ils restaient alignés sur les positions précédentes.
        try:
            self.chip_renderer.schedule_redraw()
        except Exception:
            pass

    def on_column_separator_double_click(self, event):
        """Double-clic sur la séparation entre deux colonnes : ajuste la largeur (type Excel)."""
        try:
            if self.tree.identify_region(event.x, event.y) != "separator":
                return
            col_id = self.tree.identify_column(event.x)
        except Exception:
            return
        if col_id:
            self.auto_size_one_column(col_id)
            # La colonne est dimensionnée manuellement : l'ajustement
            # automatique au redimensionnement ne doit pas l'écraser.
            try:
                n = int(col_id[1:])
                if 1 <= n <= len(self.columns):
                    self._user_sized_cols.add(self.columns[n - 1])
            except (ValueError, IndexError, AttributeError):
                pass

    def _on_tree_separator_release(self, event=None):
        """Mémorise la colonne dont le séparateur vient d'être déplacé à la
        souris (redimensionnement manuel) afin que la redistribution
        automatique des colonnes stretch ne modifie pas sa largeur."""
        try:
            if self.tree.identify_region(event.x, event.y) != "separator":
                return None
            col_id = self.tree.identify_column(event.x)
        except Exception:
            return None
        if col_id and col_id.startswith("#"):
            try:
                n = int(col_id[1:])
                if 1 <= n <= len(self.columns):
                    self._user_sized_cols.add(self.columns[n - 1])
            except (ValueError, IndexError, AttributeError):
                pass
        # La largeur d'une colonne vient de changer manuellement : recale les
        # overlays de chips. Sans ce recalage, ils restaient à leur position
        # antérieure et laissaient apparaître le texte du Treeview à côté des
        # pastilles (« running » + fragment « Machine hors… » à l'écran).
        try:
            self.chip_renderer.schedule_redraw()
        except Exception:
            pass
        return None

    def _is_alert_device(self, device):
        return self._get_device_global_state(device) in ("low", "critical")

    def update_site_filter_options(self, restore_saved=True):
        sites = sorted({device.site for device in self.devices if device.site})
        self.site_filter_combo.config(values=["Tous"] + sites)
        saved = (self.config or {}).get("selected_site", "Tous") or "Tous"
        if restore_saved and saved in (["Tous"] + sites):
            self.site_filter.set(saved)
        else:
            if restore_saved and saved != "Tous":
                # Grand site memorise mais indisponible : repli sur "Tous"
                # sans bloquer l'interface, et corrige la preference.
                try:
                    self.config["selected_site"] = "Tous"
                    save_json_config(self.config)
                except Exception:
                    pass
            self.site_filter.set("Tous")

    def apply_site_filter(self, save=True):
        self.populate_tree()
        # Changement de Grand site : le timer est réinitialisé et repart de zéro.
        self._restart_autorefresh()
        if save:
            try:
                current = self.site_filter.get()
            except Exception:
                return
            try:
                if (self.config or {}).get("selected_site") != current:
                    self.config["selected_site"] = current
                    save_json_config(self.config)
            except Exception:
                pass

    def _parse_rows(self, headers, rows):
        devices = []
        header_map = {}
        if headers and any(re.search(r"ip|adresse|address", str(cell), re.I) for cell in headers):
            for index, name in enumerate(headers):
                key = str(name).strip().lower()
                if "ip" in key or "adresse" in key or "address" in key:
                    header_map["ip"] = index
                elif "tatouage" in key or key == "id" or "tag" in key or "asset" in key or "device" in key or ("numero" in key and ("etiquette" in key or "etiq" in key or "ettiq" in key)):
                    header_map["tag"] = index
                elif "marque" in key or "brand" in key or "manufacturer" in key or "fabricant" in key:
                    header_map["brand"] = index
                elif "modèle" in key or "modele" in key or "model" in key:
                    header_map["model"] = index
                elif "grand site" in key or ("site" in key and "grand" in key) or ("site" in key and "address" not in key):
                    header_map["site"] = index
                elif "site" in key or "location" in key or "emplacement" in key or "localisation" in key:
                    if "site" not in header_map:
                        header_map["site"] = index
                    if "location" not in header_map and ("location" in key or "emplacement" in key or "localisation" in key):
                        header_map["location"] = index
                elif "lieu" in key:
                    if "location" not in header_map:
                        header_map["location"] = index
            start_row = 2
        else:
            header_map = {"site": 0, "tag": 1, "brand": 2, "ip": 3, "model": 6, "location": 12}
            start_row = 1
        for row_index, row in enumerate(rows, start=start_row):
            if not row:
                continue
            cells = [str(cell).strip() if cell is not None else "" for cell in row]
            ip = ""
            tag = ""
            brand = ""
            csv_model = ""
            site = ""
            location = ""
            if "ip" in header_map and header_map["ip"] < len(cells):
                ip = normalize_ip(cells[header_map["ip"]])
            if "tag" in header_map and header_map["tag"] < len(cells):
                tag = cells[header_map["tag"]]
            if "brand" in header_map and header_map["brand"] < len(cells):
                brand = cells[header_map["brand"]]
            if "model" in header_map and header_map["model"] < len(cells):
                csv_model = cells[header_map["model"]]
            if "site" in header_map and header_map["site"] < len(cells):
                site = cells[header_map["site"]]
            if "location" in header_map and header_map["location"] < len(cells):
                location = cells[header_map["location"]]
            if not ip:
                for cell in cells:
                    candidate = normalize_ip(cell)
                    if candidate:
                        ip = candidate
                        break
            invalid_ip = not ip or any(term in ip.upper() for term in ("RETIRE", "HORS RLE", "HORS RESEAU", "COLIMO"))
            if not ip or invalid_ip:
                continue
            if not tag and len(cells) > 2 and cells[1] != ip:
                tag = cells[1]
            devices.append(
                PrinterDevice(
                    ip,
                    tag,
                    brand=brand,
                    csv_model=csv_model,
                    site=site,
                    location=location,
                    row_info={"row": row_index, "raw": row},
                )
            )
        if not devices:
            raise ValueError("Aucune adresse IP valide trouvée dans le fichier CMDB.")
        devices.sort(key=lambda d: ((d.site or "").lower(), d.csv_model.lower() if d.csv_model else "", d.ip))
        return devices

    # ------------------------------------------------------------------
    # ARRÊT COOPÉRATIF D'UN RAFRAÎCHISSEMENT EN COURS
    # ------------------------------------------------------------------
    def _begin_refresh(self, devices_to_refresh, status_message=None):
        """Prépare et lance une passe de rafraîchissement (liste d'imprimantes
        donnée) avec suivi de progression. Utilisé par « Rafraîchir sélection » ;
        « Rafraîchir tout » reproduit la même logique en restant compatible avec
        les tests unitaires existants."""
        stop_event = getattr(self, "refresh_stop_event", None)
        if stop_event is None:
            stop_event = threading.Event()
            setattr(self, "refresh_stop_event", stop_event)
        try:
            stop_event.clear()
        except Exception:
            pass
        self.refresh_total = len(devices_to_refresh)
        self.refresh_completed = 0
        self.refresh_start_time = time.time()
        self.refresh_in_progress = True
        self.refresh_progress["maximum"] = self.refresh_total
        self.refresh_progress["value"] = 0
        self.refresh_percent_label.config(text="0%")
        self.refresh_time_label.config(text=f"0/{self.refresh_total} — 0.0s")
        if status_message:
            self.log_status(status_message)
        updater = getattr(self, "_update_refresh_controls", None)
        if callable(updater):
            updater()
        for device in devices_to_refresh:
            self.executor.submit(self.poll_device, device)
        # Gestion automatique des certificats HTTPS en arriere-plan :
        # executee sur un executor dedie (tls_certs), sans bloquer le SNMP.
        try:
            self._launch_tls_refresh(devices_to_refresh)
        except Exception:
            pass

    def _update_refresh_controls(self):
        """Met à jour la barre d'outils selon l'avancement du rafraîchissement :
          * « Rafraîchir tout » et « Rafraîchir sélection » sont désactivés
            pendant une passe (aucun lancement simultané) ;
          * le bouton « Arrêter » n'est visible que pendant une passe."""
        try:
            in_progress = bool(getattr(self, "refresh_in_progress", False))
        except Exception:
            return
        for name in ("refresh_all_btn", "refresh_selected_btn"):
            btn = getattr(self, name, None)
            if btn is None:
                continue
            try:
                btn.config(state="disabled" if in_progress else "normal")
            except Exception:
                pass
        stop_btn = getattr(self, "refresh_stop_btn", None)
        if stop_btn is None:
            return
        try:
            if in_progress:
                if not stop_btn.winfo_ismapped():
                    stop_btn.config(state="normal")
                    export_btn = getattr(self, "export_btn", None)
                    if export_btn is not None and export_btn.winfo_ismapped():
                        stop_btn.pack(side="left", padx=4, before=export_btn)
                    else:
                        stop_btn.pack(side="left", padx=4)
            elif stop_btn.winfo_ismapped():
                stop_btn.pack_forget()
        except Exception:
            pass

    def stop_refresh(self):
        """Demande l'arrêt coopératif du rafraîchissement en cours (manuel ou
        automatique). Le logiciel n'est pas fermé : chaque ouvrier SNMP vérifie
        l'événement entre deux opérations et abandonne dès que possible ; les
        imprimantes non encore traitées ne sont plus interrogées et les données
        déjà récupérées sont conservées."""
        stop_event = getattr(self, "refresh_stop_event", None)
        if stop_event is None:
            return
        if not getattr(self, "refresh_in_progress", False):
            return
        stop_event.set()
        self.log_status("Arrêt du rafraîchissement demandé...")
        stop_btn = getattr(self, "refresh_stop_btn", None)
        if stop_btn is not None:
            try:
                stop_btn.config(state="disabled")
            except Exception:
                pass
        # Si c'était une passe automatique, le prochain compte à rebours est
        # programmé normalement à l'intervalle configuré (jamais déclenché
        # immédiatement après l'arrêt).
        if self._auto_refresh_active:
            self._restart_autorefresh()

    def refresh_all(self):
        if not self.devices:
            messagebox.showwarning("Avertissement", "Aucune imprimante chargée. Chargez d'abord un fichier CMDB.")
            return
        # Un seul rafraîchissement à la fois (manuel ou automatique) : on refuse
        # tout second lancement pendant qu'une passe est en cours.
        if getattr(self, "refresh_in_progress", False):
            return
        selected_site = self.site_filter.get()
        devices_to_refresh = [d for d in self.devices if selected_site == "Tous" or d.site == selected_site]
        # (Ré)initialise le drapeau d'arrêt coopératif pour cette nouvelle passe.
        stop_event = getattr(self, "refresh_stop_event", None)
        if stop_event is None:
            stop_event = threading.Event()
            setattr(self, "refresh_stop_event", stop_event)
        try:
            stop_event.clear()
        except Exception:
            pass
        # Initialize global refresh progress tracking
        self.refresh_total = len(devices_to_refresh)
        self.refresh_completed = 0
        self.refresh_start_time = time.time()
        self.refresh_in_progress = True
        self.refresh_progress["maximum"] = self.refresh_total
        self.refresh_progress["value"] = 0
        self.refresh_percent_label.config(text="0%")
        self.refresh_time_label.config(text=f"0/{self.refresh_total} — 0.0s")
        self.log_status(f"Rafraîchissement de {self.refresh_total} imprimante(s) en cours...")
        updater = getattr(self, "_update_refresh_controls", None)
        if callable(updater):
            updater()
        for device in devices_to_refresh:
            self.executor.submit(self.poll_device, device)
        try:
            self._launch_tls_refresh(devices_to_refresh)
        except Exception:
            pass

    def refresh_selected(self):
        # Un seul rafraîchissement à la fois : un « Rafraîchir sélection » est
        # refusé si une passe (manuelle ou automatique) est déjà en cours.
        if getattr(self, "refresh_in_progress", False):
            return
        item = self.tree.selection()
        if not item:
            messagebox.showwarning("Avertissement", "Sélectionnez une imprimante dans la liste.")
            return
        device = self.device_items.get(item[0])
        if device:
            self._begin_refresh([device], status_message=f"Rafraîchissement de {device.ip}...")

    # ------------------------------------------------------------------
    # CERTIFICATS HTTPS — gestion automatique en arriere-plan (tls_certs.py)
    # ------------------------------------------------------------------
    # Complete le Ctrl+clic existant (on_ctrl_click / _open_printer_web,
    # inchange) : pendant chaque rafraichissement SNMP, les certificats
    # HTTPS sont observes/analyses/suivis sur un executor dedie, sans
    # bloquer le SNMP et sans installation aveugle.

    def _get_tls_manager(self):
        """Retourne le gestionnaire TLS (creation paresseuse, jamais bloquante)."""
        mgr = getattr(self, "_tls_manager", None)
        if mgr is not None:
            return mgr
        try:
            from .tls_certs import CertAutoManager
            mgr = CertAutoManager(config=getattr(self, "config", {}) or {},
                                  on_event=self._on_tls_event)
            self._tls_manager = mgr
            return mgr
        except Exception:
            return None

    def _launch_tls_refresh(self, devices):
        """Lance la gestion TLS en arriere-plan pour *devices* (non bloquant)."""
        try:
            mgr = self._get_tls_manager()
            if mgr is None:
                return
            items = []
            for d in devices or []:
                ip = getattr(d, "ip", "") or ""
                host = getattr(d, "hostname", "") or getattr(d, "name", "") or ""
                if ip:
                    items.append((ip, host))
            if not items:
                return
            tls_exec = getattr(self, "_tls_executor", None)
            if tls_exec is None:
                tls_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tls-certs")
                self._tls_executor = tls_exec
            mgr.handle_printers_async(items, executor=tls_exec)
        except Exception:
            pass

    def _on_tls_event(self, event: dict):
        """Callback TLS (thread d'arriere-plan) : journal sobre + compteur UI."""
        try:
            if not isinstance(event, dict):
                return
            action = event.get("action", "")
            if action in ("needs_approval", "keep", "update_managed"):
                try:
                    self._tls_attention = int(getattr(self, "_tls_attention", 0)) + 1
                except Exception:
                    self._tls_attention = 1
                try:
                    if hasattr(self, "root") and self.root is not None:
                        self.root.after(0, self._update_tls_button)
                except Exception:
                    pass
        except Exception:
            pass

    def _update_tls_button(self):
        try:
            btn = getattr(self, "tls_btn", None)
            if btn is None:
                return
            n = int(getattr(self, "_tls_attention", 0) or 0)
            btn.config(text="Certificats (%d)" % n if n else "Certificats")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # RAFRAÎCHISSEMENT AUTOMATIQUE
    # ------------------------------------------------------------------
    def _autorefresh_interval_ms(self):
        """Intervalle du rafraîchissement automatique en millisecondes
        (0 si désactivé ou intervalle invalide)."""
        if not self.autorefresh_enabled:
            return 0
        minutes = self.autorefresh_interval_minutes
        if not isinstance(minutes, int) or minutes <= 0:
            return 0
        return minutes * 60 * 1000

    def _cancel_autorefresh_timer(self):
        """Annule tout compte à rebours en attente (s'il existe)."""
        if self._auto_after_id is not None:
            try:
                self.root.after_cancel(self._auto_after_id)
            except Exception:
                pass
            self._auto_after_id = None

    def _restart_autorefresh(self):
        """Réinitialise le timer du rafraîchissement automatique. Le compte à
        rebours ne démarre que si la fonction est activée ET qu'une CMDB a
        déjà été chargée (chargement initial terminé) ; sinon il reste arrêté.
        Appelé en fin de chargement initial et à chaque changement de Grand
        site / CMDB."""
        self._cancel_autorefresh_timer()
        self._auto_refresh_active = False
        self._cmdb_loaded = bool(self.devices)
        interval_ms = self._autorefresh_interval_ms()
        if interval_ms > 0 and self._cmdb_loaded:
            self._auto_after_id = self.root.after(interval_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        """Déclenché à l'échéance du compte à rebours. Ne lance jamais deux
        rafraîchissements simultanément : si un rafraîchissement (manuel ou
        automatique) est déjà en cours, on attend sa fin avant de programmer
        le suivant."""
        self._auto_after_id = None
        interval_ms = self._autorefresh_interval_ms()
        if interval_ms <= 0 or not self._cmdb_loaded or not self.devices:
            return
        if self.refresh_in_progress:
            # Un rafraîchissement est déjà en cours : on attend sa fin, puis
            # on retentera le tick (pas de rafraîchissement simultané).
            self._auto_after_id = self.root.after(5000, self._auto_refresh_tick)
            return
        # Même mécanisme que le bouton "Rafraîchir tout" : on conserve ainsi
        # toutes les données, règles SNMP, statuts et catégories existants.
        self._auto_refresh_active = True
        self.log_status(
            f"Rafraîchissement automatique déclenché (toutes les {self.autorefresh_interval_minutes} min)."
        )
        self.refresh_all()
        # Le prochain compte à rebours sera (re)démarré à la fin du
        # rafraîchissement (voir process_poll_results).


    def clean_history(self, dialog=None):
        """Vide entièrement l'historique local (imprimantes, mesures,
        consommables) après confirmation de l'utilisateur.

        Si *dialog* (la fenêtre d'historique) est fourni, la liste des
        imprimantes et des mesures est actualisée après le nettoyage.
        """
        if not self._history:
            messagebox.showwarning("Historique", "L'historique est désactivé.")
            return
        if not messagebox.askyesno(
            "Nettoyer l'historique",
            "Voulez-vous vraiment supprimer TOUT l'historique "
            "(imprimantes, mesures et consommables) ?\n\n"
            "Cette action est irréversible.",
        ):
            return
        try:
            deleted = self._history.clear_all()
            self.log_status(f"Historique vidé : {deleted} mesure(s) supprimée(s).")
            # Actualise la fenêtre d'historique si elle est encore ouverte.
            if dialog is not None and dialog.winfo_exists():
                # Vide les deux Treeview de la fenêtre d'historique.
                for tree in (getattr(self, "_history_results_tree", None),
                             getattr(self, "_history_meas_tree", None)):
                    if tree is not None and tree.winfo_exists():
                        tree.delete(*tree.get_children())
                # Réinitialise la sélection courante.
                if hasattr(self, "_history_current_ip"):
                    self._history_current_ip = None
        except Exception as exc:
            logging.exception("History cleanup failed")
            messagebox.showerror("Historique", f"Erreur lors du nettoyage : {exc}")

    def open_history(self):
        """Ouvre la fenêtre d'historique : recherche d'imprimantes, affichage
        des mesures disponibles, visualisation de timeline et comparaison de
        deux mesures."""
        if not self._history or not self._history_enabled:
            messagebox.showwarning("Historique", "L'historique est désactivé dans les paramètres.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Historique des imprimantes")
        dialog.resizable(True, True)
        style_toplevel(dialog)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            dialog, "historique", default_size=(1000, 700),
            min_size=(720, 460))
        dialog.transient(self.root)
        dialog.grab_set()

        # ── Barre de recherche ──
        search_frame = ttk.Frame(dialog)
        search_frame.pack(fill="x", padx=6, pady=4)
        ttk.Label(search_frame, text="Rechercher :").pack(side="left")
        search_var = StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var, width=30)
        search_entry.pack(side="left", padx=4)
        search_entry.focus_set()
        # La saisie dans le champ déclenche la recherche en direct (à chaque
        # frappe) ; effacer le texte restaure la liste complète.
        search_entry.bind(
            "<KeyRelease>",
            lambda _e: self._history_search(
                search_var.get().strip(), results_tree, timeline_frame, detail_text),
        )
        ttk.Button(search_frame, text="Nettoyer l'historique",
                   command=lambda: self.clean_history(dialog)).pack(side="right", padx=4)

        # ── Corps : deux panneaux ──
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=6, pady=4)

        # Panneau gauche : résultats de recherche + mesures
        left_panel = ttk.Frame(body)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 4))

        ttk.Label(left_panel, text="Imprimantes").pack(anchor="w", padx=4, pady=(0, 2))
        results_tree = ttk.Treeview(left_panel, columns=("num", "ip", "name", "model", "site"), show="headings", height=8)
        self._history_results_tree = results_tree
        results_tree.heading("num", text="#")
        results_tree.heading("ip", text="Adresse IP")
        results_tree.heading("name", text="Nom")
        results_tree.heading("model", text="Modèle")
        results_tree.heading("site", text="Grand site")
        results_tree.column("num", width=40, stretch=False, anchor="center")
        results_tree.column("ip", width=120, stretch=False)
        results_tree.column("name", width=150, stretch=True)
        results_tree.column("model", width=180, stretch=True)
        results_tree.column("site", width=120, stretch=True)
        results_tree.pack(fill="both", expand=True, padx=2, pady=2)
        results_tree.bind("<<TreeviewSelect>>",
                          lambda e: self._history_on_printer_select(e, timeline_frame, detail_text))

        ttk.Label(left_panel, text="Mesures disponibles (Ctrl+clic pour sélectionner 2)").pack(anchor="w", padx=4, pady=(4, 2))
        self._history_meas_tree = ttk.Treeview(left_panel, columns=("num", "timestamp", "status"), show="headings", height=10, selectmode="extended")
        self._history_meas_tree.heading("num", text="#")
        self._history_meas_tree.heading("timestamp", text="Date / Heure")
        self._history_meas_tree.heading("status", text="Statut")
        self._history_meas_tree.column("num", width=40, stretch=False, anchor="center")
        self._history_meas_tree.column("timestamp", width=200, stretch=True)
        self._history_meas_tree.column("status", width=150, stretch=True)
        self._history_meas_tree.pack(fill="both", expand=True, padx=2, pady=2)
        self._history_meas_tree.bind("<<TreeviewSelect>>",
                                      lambda e: self._history_on_measure_select(dialog, detail_text, timeline_frame))

        # Panneau droit : détail + timeline
        right_panel = ttk.Frame(body)
        right_panel.pack(side="right", fill="both", expand=True, padx=(4, 0))

        ttk.Label(right_panel, text="Détails de la mesure").pack(anchor="w", padx=4, pady=(0, 2))
        detail_text = tk.Text(right_panel, height=8, wrap="word", state="disabled", font=("Segoe UI", 9))
        detail_text.pack(fill="x", padx=2, pady=2)

        ttk.Label(right_panel, text="Évolution (timeline)").pack(anchor="w", padx=4, pady=(4, 2))
        timeline_canvas = tk.Canvas(right_panel, height=180, highlightthickness=0)
        timeline_canvas.bind("<Configure>", lambda e: timeline_canvas.configure(scrollregion=timeline_canvas.bbox("all")))
        timeline_vbar = ttk.Scrollbar(right_panel, orient="vertical", command=timeline_canvas.yview)
        timeline_canvas.configure(yscrollcommand=timeline_vbar.set)
        timeline_frame = ttk.Frame(timeline_canvas)
        timeline_canvas.create_window((0, 0), window=timeline_frame, anchor="nw")
        timeline_vbar.pack(side="right", fill="y")
        timeline_canvas.pack(side="left", fill="both", expand=True, padx=2, pady=2)

        def _on_timeline_configure(event):
            timeline_canvas.configure(scrollregion=timeline_canvas.bbox("all"))
        timeline_frame.bind("<Configure>", _on_timeline_configure)

        # Bouton de comparaison
        compare_btn = ttk.Button(right_panel, text="Comparer les mesures sélectionnées",
                                 command=lambda: self._history_compare(dialog))
        compare_btn.pack(pady=(4, 0))

        # Charger les résultats initiaux
        self._history_search(search_var.get().strip(), results_tree, timeline_frame, detail_text)

    def _history_search(self, query, results_tree, timeline_frame, detail_text):
        """Recherche des imprimeurs et met à jour le treeview de résultats."""
        results_tree.delete(*results_tree.get_children())
        self._history_meas_tree.delete(*self._history_meas_tree.get_children())
        # Note: timeline_frame is recreated by the caller on re-search
        if hasattr(self, "_history_current_ip"):
            self._history_current_ip = None
        detail_text.config(state="normal")
        detail_text.delete("1.0", END)
        detail_text.insert(END, "Sélectionnez une imprimante pour afficher ses mesures.")
        detail_text.config(state="disabled")

        if not self._history:
            return

        try:
            printers = self._history.search_printers(query)
            for idx, (ip, name, model, csv_model, site) in enumerate(printers, 1):
                results_tree.insert("", "end",
                                     values=(idx, ip, name or "-", model or "-", site or "-"),
                                     tags=(ip,))
        except Exception as exc:
            logging.error("History search failed: %s", exc)

    def _history_on_printer_select(self, event, timeline_frame, detail_text):
        """Called when a printer is selected in the results tree."""
        selected = event.widget.selection()
        if not selected:
            return
        ip = event.widget.item(selected[0], "tags")
        ip = ip[0] if ip else None
        if not ip:
            return
        self._history_current_ip = ip

        # Update measurements list
        self._history_meas_tree.delete(*self._history_meas_tree.get_children())
        if not self._history:
            return
        try:
            measurements = self._history.get_measurements(ip)
            for i, (meas_id, timestamp, status) in enumerate(measurements, 1):
                self._history_meas_tree.insert("", "end",
                                                values=(i, timestamp, status or "-"),
                                                tags=(str(meas_id),))
        except Exception as exc:
            logging.error("History measurements load failed: %s", exc)

    def _history_on_measure_select(self, dialog, detail_text, timeline_frame):
        """Affiche le détail et la timeline lorsqu'une ou plusieurs mesures sont
        sélectionnées dans la liste."""
        selected = self._history_meas_tree.selection()
        if not selected:
            return

        current_ip = getattr(self, "_history_current_ip", None)
        if not current_ip:
            return

        if not self._history:
            return
        try:
            timeline_data = self._history.get_timeline(current_ip)
            self._history_show_timeline(timeline_frame, timeline_data)

            # Show details of the first selected measurement
            first_sel = selected[0]
            meas_tags = self._history_meas_tree.item(first_sel, "tags")
            meas_id_str = meas_tags[0] if meas_tags else ""
            try:
                meas_id = int(meas_id_str)
            except (ValueError, TypeError):
                return

            meas = self._history.get_measurement(meas_id)
            consumables = self._history.get_consumables(meas_id)
            if meas:
                detail_text.config(state="normal")
                detail_text.delete("1.0", END)
                lines = [
                    f"Imprimante : {meas.get('name', '-')}",
                    f"IP : {meas.get('printer_ip', '-')}",
                    f"Modèle : {meas.get('model', '-') or meas.get('csv_model', '-')}",
                    f"Statut : {meas.get('status', '-')}",
                    f"Horodatage : {meas.get('timestamp', '-')}",
                    f"Site : {meas.get('site', '-')}",
                    f"Emplacement : {meas.get('location', '-')}",
                    "",
                    "Consommables :",
                ]
                for cat, desc, color, pct in consumables:
                    color_str = f" ({color})" if color else ""
                    lines.append(f"  {cat}{color_str} : {desc} - {pct}%")
                detail_text.insert(END, "\n".join(lines))
                detail_text.config(state="disabled")
        except Exception as exc:
            logging.error("History detail display failed: %s", exc)

    def _history_show_timeline(self, timeline_frame, timeline_data):
        """Render a timeline visualization in the canvas frame."""
        for child in timeline_frame.winfo_children():
            child.destroy()

        if not timeline_data:
            ttk.Label(timeline_frame, text="Aucune donnée de timeline.").pack(pady=10)
            return

        # Gather all consumable series
        all_consumables = {}
        for m in timeline_data:
            for c in m.get("consumables", []):
                key = (c["category"], c["color"])
                if key not in all_consumables:
                    all_consumables[key] = []
                all_consumables[key].append((m["timestamp"], c["percentage"]))

        if not all_consumables:
            ttk.Label(timeline_frame, text="Aucun consommable enregistré.").pack(pady=10)
            return

        row = 0
        for (category, color), points in all_consumables.items():
            if not points:
                continue
            label_text = f"{category}"
            if color:
                label_text += f" ({color})"
            ttk.Label(timeline_frame, text=label_text, font=("Segoe UI", 8, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 0))
            row += 1

            try:
                canvas_width = max(150, timeline_frame.master.master.winfo_width() - 20)
            except Exception:
                canvas_width = 300
            bar_height = 14
            spacing = 6
            canvas_height = len(points) * bar_height + (len(points) - 1) * spacing
            canvas = tk.Canvas(timeline_frame, width=canvas_width, height=canvas_height, highlightthickness=0)
            canvas.grid(row=row, column=0, columnspan=2, padx=4, pady=(2, 6))

            for i, (timestamp, pct) in enumerate(points):
                y = i * (bar_height + spacing)
                bar_w = int((pct / 100.0) * canvas_width) if pct is not None else 0
                bar_color = "#9e9e9e"
                if category == "toner" and color in TONER_COLORS:
                    bar_color = TONER_COLORS[color]
                elif category == "drum" and color in TONER_COLORS:
                    bar_color = TONER_COLORS[color]
                else:
                    bar_color = "#607d8b"
                if bar_w > 0:
                    canvas.create_rectangle(0, y, bar_w, y + bar_height, fill=bar_color, outline="")
                canvas.create_text(canvas_width - 4, y + bar_height // 2,
                                   text=f"{pct}% - {timestamp}", anchor="e",
                                   font=("Segoe UI", 7), fill="#333333")
            row += 1

    def _history_compare(self, dialog):
        """Compare deux mesures sélectionnées : affiche un tableau comparatif."""
        selected = self._history_meas_tree.selection()
        if len(selected) < 2:
            messagebox.showwarning("Comparaison", "Sélectionnez deux mesures (Ctrl+clic) pour comparer.")
            return

        meas_ids = []
        for item in selected[:2]:
            try:
                meas_tags = self._history_meas_tree.item(item, "tags")
                meas_id = int(meas_tags[0]) if meas_tags else 0
                meas_ids.append(meas_id)
            except (ValueError, TypeError):
                continue

        if len(meas_ids) < 2:
            messagebox.showwarning("Comparaison", "Impossible de lire les identifiants des mesures sélectionnées.")
            return

        m1 = self._history.get_measurement(meas_ids[0])
        m2 = self._history.get_measurement(meas_ids[1])
        c1 = self._history.get_consumables(meas_ids[0])
        c2 = self._history.get_consumables(meas_ids[1])

        if not m1 or not m2:
            messagebox.showwarning("Comparaison", "Mesure introuvable.")
            return

        cmp_dialog = tk.Toplevel(dialog)
        cmp_dialog.title("Comparaison de mesures")
        cmp_dialog.resizable(True, True)
        style_toplevel(cmp_dialog)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            cmp_dialog, "comparaison_mesures", default_size=(700, 500),
            min_size=(480, 320))
        cmp_dialog.transient(dialog)
        cmp_dialog.grab_set()

        ttk.Label(cmp_dialog, text=f"Comparaison : {m1.get('timestamp', '')} vs {m2.get('timestamp', '')}",
                  font=("Segoe UI", 9, "bold")).pack(pady=4)

        tree = ttk.Treeview(cmp_dialog, columns=("cons", "m1_pct", "m2_pct", "evolution"), show="headings", height=15)
        tree.heading("cons", text="Consommable")
        tree.heading("m1_pct", text=f"{m1.get('timestamp', '')[:16]}")
        tree.heading("m2_pct", text=f"{m2.get('timestamp', '')[:16]}")
        tree.heading("evolution", text="Evolution")
        tree.column("cons", width=280)
        tree.column("m1_pct", width=80, anchor="center")
        tree.column("m2_pct", width=80, anchor="center")
        tree.column("evolution", width=80, anchor="center")

        def build_lookup(consumables):
            lookup = {}
            for cat, desc, color, pct in consumables:
                key = f"{cat}:{color or ''}:{desc}"
                lookup[key] = pct
            return lookup

        lookup1 = build_lookup(c1)
        lookup2 = build_lookup(c2)

        all_keys = set(lookup1.keys()) | set(lookup2.keys())
        for key in sorted(all_keys):
            parts = key.split(":", 2)
            cat = parts[0]
            col = parts[1] if len(parts) > 1 else ""
            desc = parts[2] if len(parts) > 2 else ""
            p1 = lookup1.get(key)
            p2 = lookup2.get(key)
            label = f"{cat} ({col})" if col else cat
            if desc:
                label += f" - {desc}"

            if p1 is not None and p2 is not None:
                diff = p2 - p1
                if diff > 0:
                    evo = f"+{diff}%"
                elif diff < 0:
                    evo = f"{diff}%"
                else:
                    evo = "=0%"
            else:
                evo = "N/A"

            tree.insert("", "end", values=(
                label,
                f"{p1}%" if p1 is not None else "-",
                f"{p2}%" if p2 is not None else "-",
                evo,
            ))

        tree.pack(fill="both", expand=True, padx=6, pady=4)
        vsb = ttk.Scrollbar(cmp_dialog, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        ttk.Button(cmp_dialog, text="Fermer", command=cmp_dialog.destroy).pack(pady=4)

    def on_close(self):
        """Arrête proprement le timer de rafraîchissement automatique et le
        pool de threads avant de fermer la fenêtre."""
        self._cancel_autorefresh_timer()
        self._auto_refresh_active = False
        # Arrête le timer de clignotement des imprimantes « down ».
        if self._down_blink_after_id is not None:
            try:
                self.root.after_cancel(self._down_blink_after_id)
            except Exception:
                pass
            self._down_blink_after_id = None
        # Demande l'arrêt coopératif de tout rafraîchissement encore en cours
        # afin que les ouvriers SNMP s'interrompent dès que possible.
        stop_event = getattr(self, "refresh_stop_event", None)
        if stop_event is not None:
            try:
                stop_event.set()
            except Exception:
                pass
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            tls_exec = getattr(self, "_tls_executor", None)
            if tls_exec is not None:
                tls_exec.shutdown(wait=False)
        except Exception:
            pass
        self.root.destroy()

    def export_csv(self):
        if not self.devices:
            messagebox.showwarning("Avertissement", "Aucune imprimante chargée. Chargez d'abord un fichier CMDB.")
            return
        path = filedialog.asksaveasfilename(
            title="Exporter les résultats vers CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Tous les fichiers", "*")],
        )
        if not path:
            return
        try:
            # Use UTF-8 with BOM so Excel opens the file correctly and use semicolon
            # as delimiter for locales expecting ';' as the CSV separator.
            with open(path, "w", newline="", encoding="utf-8-sig") as csvfile:
                writer = csv.writer(csvfile, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                writer.writerow([
                    "Grand site",
                    "Adresse IP",
                    "Tatouage",
                    "Localisation",
                    "Modèle",
                    "Statut",
                    "Toner/cartouche",
                    "Tambour/photoconducteur",
                    "Bouteille récup. toner",
                    "Agrafes",
                    "Dernière maj",
                    "Messages",
                ])
                for item in self.tree.get_children():
                    values = self.tree.item(item, "values")
                    # Ensure we write a flat row of strings (Treeview may return tuple)
                    if isinstance(values, (list, tuple)):
                        row = [str(v) for v in values]
                    else:
                        row = [str(values)]
                    # La colonne "Grand site" n'est plus affichee mais reste
                    # exportee depuis les donnees internes (device.site).
                    device = self.device_items.get(item)
                    site = getattr(device, "site", "") if device else ""
                    writer.writerow([site] + row)
            self.log_status(f"Résultats exportés vers {os.path.basename(path)}.")
        except Exception as exc:
            messagebox.showerror("Export CSV", f"Impossible d'exporter le fichier CSV : {exc}")

    def poll_device(self, device):
        # Arrêt coopératif : si l'arrêt a été demandé avant que cette imprimante
        # ne démarre, elle n'est plus interrogée du tout (données conservées).
        if self.refresh_stop_event.is_set():
            self.poll_queue.put(device)
            return
        device.refresh(self.snmp_community.get(), self.snmp_port.get(), self.refresh_stop_event)
        # Sauvegarde de l'historique uniquement si le rafraîchissement a réussi
        # complètement (pas interrompu, pas d'erreur, machine joignable).
        self._save_history_if_successful(device)
        self.poll_queue.put(device)

    def _save_history_if_successful(self, device):
        """Enregistre un snapshot dans l'historique SQLite uniquement si le
        rafraîchissement a été complet et réussi.

        Un rafraîchissement est considéré comme réussi si :
          - l'arrêt coopératif n'a pas été demandé (pas interrompu) ;
          - aucune erreur SNMP (device.error vide) ;
          - la machine est joignable (device.reachability vide).
        """
        if not self._history or not self._history_enabled:
            return
        try:
            if self.refresh_stop_event.is_set():
                return
            if device.error:
                return
            if device.reachability:
                return
            self._history.save_measurement(device)
            # Nettoyage périodique (au plus une fois par heure)
            now = time.time()
            if now - self._last_history_cleanup > 3600:
                self._history.cleanup_old_records()
                self._last_history_cleanup = now
        except Exception as exc:
            logging.debug(f"\u00c9chec de l'enregistrement d'historique pour {device.ip}: {exc}")

    def process_poll_results(self):
        selected_ip = None
        selected_items = self.tree.selection()
        if selected_items:
            selected_values = self.tree.item(selected_items[0], "values")
            if selected_values and len(selected_values) > 1:
                selected_ip = selected_values[1]

        updated = False
        completed_count = 0
        while not self.poll_queue.empty():
            device = self.poll_queue.get()
            self.update_device_row(device)
            updated = True
            completed_count += 1

        if updated:
            # update_device_row met déjà les lignes existantes à jour en place
            # (valeurs, tags). Le tri « par niveau » peut faire évoluer l'ordre
            # attendu : on RÉORDONNE alors les lignes existantes (tree.move)
            # au lieu de détruire/recréer toute la liste — plus aucun retour
            # temporaire à l'affichage brut « ancien » pendant la passe, et le
            # scroll/la sélection sont préservés. populate_tree() ne sert que
            # de repli si l'ensemble affiché a réellement changé (filtre/CMDB).
            if not self._reorder_tree_rows():
                self.populate_tree()
                if selected_ip:
                    for item, device in self.device_items.items():
                        if device.ip == selected_ip:
                            self.tree.selection_set(item)
                            self.tree.focus(item)
                            self.show_details(device)
                            break
            else:
                # Un seul redessin SYNCHRONE des overlays pour tout le lot : la
                # peinture écran est ainsi composée en un seul passage (valeurs +
                # pastilles cohérentes), sans fenêtre où le texte brut du Treeview
                # resterait visible avant l'échéance du débounce de 30 ms.
                # NB : le repli populate_tree() redessine déjà lui-même.
                try:
                    self.chip_renderer.redraw()
                except Exception:
                    pass
            # Diagnostic (TEMPORAIRE) : ordre réellement présent dans le Treeview
            # juste avant le rendu, dans l'ordre où les lignes sont affichées.
            try:
                self._log_default_sort(
                    [self.device_items[i] for i in self.tree.get_children()
                     if i in self.device_items],
                    "final",
                )
            except Exception:
                pass

        # Update global refresh progress bar and elapsed time
        if self.refresh_in_progress:
            if completed_count > 0:
                self.refresh_completed += completed_count
                self.refresh_progress["value"] = self.refresh_completed
            elapsed = time.time() - self.refresh_start_time
            pct = int((self.refresh_completed / self.refresh_total) * 100) if self.refresh_total > 0 else 0
            self.refresh_percent_label.config(text=f"{pct}%")
            if self.refresh_completed >= self.refresh_total:
                self.refresh_in_progress = False
                if self.refresh_stop_event.is_set():
                    self.log_status(
                        f"Rafraîchissement arrêté : {self.refresh_completed} imprimante(s) traitée(s) en {elapsed:.1f}s."
                    )
                    self.refresh_time_label.config(text=f"Arrêté après {elapsed:.1f}s")
                else:
                    self.log_status(
                        f"Rafraîchissement terminé : {self.refresh_total} imprimante(s) en {elapsed:.1f}s."
                    )
                    self.refresh_time_label.config(text=f"Terminé en {elapsed:.1f}s")
                # Si c'était un rafraîchissement automatique, on redémarre le
                # compte à rebours à partir de sa fin pour la période suivante.
                # En cas d'arrêt manuel, le prochain compte à rebours a déjà été
                # programmé à l'intervalle configuré par stop_refresh() (jamais
                # déclenché immédiatement après l'arrêt).
                if self._auto_refresh_active:
                    self._restart_autorefresh()
                updater = getattr(self, "_update_refresh_controls", None)
                if callable(updater):
                    updater()
            else:
                # Show elapsed time and estimated remaining time
                if self.refresh_completed > 0 and elapsed > 0:
                    rate = self.refresh_completed / elapsed
                    remaining = (self.refresh_total - self.refresh_completed) / rate if rate > 0 else 0
                    self.refresh_time_label.config(
                        text=f"{self.refresh_completed}/{self.refresh_total} — {elapsed:.1f}s — ~{remaining:.0f}s restant"
                    )
                else:
                    self.refresh_time_label.config(
                        text=f"{self.refresh_completed}/{self.refresh_total} — {elapsed:.1f}s"
                    )

        self.root.after(200, self.process_poll_results)

    def _expected_display_devices(self):
        """Périphériques qui DOIVENT être affichés, triés selon la règle métier
        existante (même clé de tri que populate_tree). Aucune logique de tri
        n'est modifiée ici : seul l'appel est factorisé."""
        selected_site = self.site_filter.get()
        return [
            d for d in sorted(self.devices, key=self._device_sort_key)
            if selected_site == "Tous" or d.site == selected_site
        ]

    def _display_order_changed(self):
        """True si les lignes actuellement affichées divergent de l'ordre ou de
        l'ensemble attendus selon la règle de tri : un rebuild complet
        (populate_tree) devient nécessaire. Sinon la mise à jour en place des
        lignes suffit."""
        try:
            children = self.tree.get_children()
            current_ips = [self.device_items[i].ip for i in children]
        except Exception:
            return True
        expected_ips = [d.ip for d in self._expected_display_devices()]
        if len(current_ips) != len(expected_ips):
            return True
        return current_ips != expected_ips

    def _reorder_tree_rows(self):
        """Réordonne les lignes affichées selon la règle de tri métier
        (_expected_display_devices, inchangée) SANS détruire/recréer les items.

        Pendant un rafraîchissement, le tri « par niveau » fait évoluer l'ordre
        attendu au fil des résultats SNMP. Un rebuild complet (populate_tree)
        provoque à chaque lot : suppression de toutes les lignes (donc perte
        momentanée des overlays de chips -> le texte brut « ancien » réapparaît
        à l'écran), recréation, auto-size et repositionnement différé. En
        déplaçant simplement les items existants (tree.move), les identités
        sont conservées : ni scintillement, ni sélection perdue, ni position
        de scroll réinitialisée, et les overlays restent valides.

        Retourne True si le réordonnancement a pu être appliqué ; False si le
        contenu affiché diverge de l'attendu (changement de filtre/CMDB) —
        dans ce cas l'appelant garde son repli populate_tree().
        """
        try:
            children = list(self.tree.get_children())
        except Exception:
            return False
        expected = self._expected_display_devices()
        current = [self.device_items.get(i) for i in children]
        if any(d is None for d in current):
            return False
        if len(current) != len(expected):
            return False
        # Comparaison exacte par identité des objets affichés (les mêmes
        # instances de PrinterDevice sont partagées entre self.devices et
        # device_items) ; tout écart -> rebuild complet classique.
        from collections import Counter

        if Counter(map(id, current)) != Counter(map(id, expected)):
            return False
        # device -> liste des items correspondants (gère les IP en doublon,
        # qui produisent plusieurs items pour des objets distincts).
        by_dev = {}
        for item, dev in zip(children, current):
            by_dev.setdefault(id(dev), []).append(item)
        try:
            for idx, dev in enumerate(expected):
                item = by_dev[id(dev)].pop(0)
                if children[idx] != item:
                    self.tree.move(item, "", idx)
            return True
        except Exception:
            return False

    def update_device_row(self, device):
        for item, row_device in self.device_items.items():
            if row_device is device:
                toner = device.get_supply_summary("toner") or "-"
                drum = device.get_supply_summary("drum") or "-"
                bottle = device.get_bottle_status(self.waste_toner_threshold.get())
                staples = device.get_staples_status(10)
                self.tree.item(item, values=(device.ip, device.tag, device.location, device.csv_model, device.status, toner, drum, bottle, staples, device.last_update, self._device_messages(device)))
                tags = []
                # Global device state: the most critical supply alert drives the
                # whole printer row colour. Reachability (SNMP timeout / offline)
                # takes priority over supply-based alerts.
                if device.reachability == "snmp_timeout":
                    tags.append("snmp_timeout")
                elif device.reachability == "offline":
                    tags.append("snmp_offline")
                elif device.error:
                    tags.append("error")
                elif (device.status or "").strip().lower() == "down":
                    # Statut « down » (hrDeviceStatus=5) = imprimante en erreur :
                    # affichée en rouge vif, distinct des simples imprimantes hors
                    # ligne. Un effet clignotant est appliqué par _blink_down_rows().
                    tags.append("device_down")
                else:
                    global_state = self._get_device_global_state(device)
                    if global_state == "critical":
                        tags.append("device_critical")
                    elif global_state == "low":
                        tags.append("device_low")
                self.tree.item(item, tags=tags)
                # NB : pas de recale individuel des chips ici — process_poll_results
                # déclenche UN redessin synchrone pour tout le lot (sinon chaque
                # ligne reposait un timer de debounce supplémentaire).
                if item == self.tree.selection()[0] if self.tree.selection() else None:
                    self.show_details(device)
                break
        self.log_status(f"Mise à jour terminée pour {device.ip}.")

    def should_warn(self, device, category):
        threshold = self._get_critical_threshold(device, category)
        for supply in device.supplies:
            if supply.category != category:
                continue
            if supply.percentage is not None and supply.percentage <= threshold:
                return True
        return False

    def _device_messages(self, device):
        """Construit la liste des messages à afficher dans la colonne « Messages »
        (joints par \\" | \\"), en distinguant l'origine de chaque information pour
        ne garder que les messages utiles et éviter les doublons.

        Cause des doublons : les etats systeme etaient re-ajoutes ici alors
        que la colonne Statut les affiche deja, et les alertes machine
        generiques repetaient le meme etat. La dedup repose sur des valeurs
        normalisees (casse/accents/gravite ignores) et sur la nature du
        message, pas sur des chaines exactes.

        1. Etats systeme : emis uniquement si Statut ne les affiche pas deja.
           En pratique PrinterDevice.refresh renseigne deja status quand
           reachability vaut offline/snmp_timeout : pas de doublon ajoute.
           Aucun message generique DOWN : Statut affiche deja down.
        2. Erreurs techniques : conservees si detail distinct (timeout SNMP
           sur machine DOWN joignable, authFailure, ...). Timeouts generiques
           deja couverts et erreurs generiques deja affichees : supprimes.
           Aucune cause non observee n'est deduite.
        3. Alertes machine (prtAlertTable / Lexmark) : conservees avec leur
           gravite, sauf reformulations generiques du statut deja affiche
           (DOWN, hors ligne, timeout couvert). Tout token supplementaire
           (jam, toner, porte, bac, fuser, code, ...) rend l'alerte distincte
           : conservee, meme hors ligne. Inconnus/constructeur preserves.

        4. Consommables (toner/cartouche, tambour, bouteille de récupération,
           agrafes) : ne génère plus de message applicatif (ex. « [Critique]
           Black Cartridge (1%) »), car le niveau est déjà visible visuellement
           et/ou annoncé par un message de l'imprimante. La logique de seuils,
           de statuts et de coloration des lignes reste inchangée.
        """
        parts = []
        seen = set()

        def _push(text):
            txt = (text or "").strip()
            if not txt:
                return
            key = _normalize_message_text(txt)
            if not key or key in seen:
                return
            seen.add(key)
            parts.append(txt)

        status = getattr(device, "status", "") or ""
        reach = getattr(device, "reachability", "") or ""
        is_down = _status_is_down(status)
        status_offline = _status_indicates_offline(status)
        status_timeout = _status_indicates_timeout(status)
        is_offline = (reach == "offline") or status_offline
        is_timeout_state = (reach == "snmp_timeout") or status_timeout
        # 1. Etats systeme : uniquement si le statut ne les affiche pas deja.
        # En pratique PrinterDevice.refresh renseigne deja status quand
        # reachability est offline/snmp_timeout, donc aucun doublon ici.
        if reach == "snmp_timeout" and not status_timeout:
            _push("SNMP Timeout - Machine joignable")
        elif reach == "offline" and not status_offline:
            _push("[Critique] Machine hors ligne")
        # NB : aucun message generique DOWN : la colonne Statut affiche deja
        # down (hrDeviceStatus=5) le cas echeant, sinon ce serait un doublon
        # systematique.
        # 2. Erreur de communication : conservee si detail distinct.
        error = (getattr(device, "error", "") or "").strip()
        if error:
            if _is_timeout_error(error):
                if not (is_offline or is_timeout_state):
                    _push(f"[Critique] Erreur : {error}")
            elif _is_generic_down_message(error):
                if not is_down:
                    _push(f"[Critique] Erreur : {error}")
            elif _is_generic_offline_message(error):
                if not is_offline:
                    _push(f"[Critique] Erreur : {error}")
            else:
                _push(f"[Critique] Erreur : {error}")
        # 3. Alertes machine : dedup des reformulations generiques du statut,
        # preservation de tout le reste (toner/encre, papier, bourrage,
        # porte, materiel, codes constructeur, inconnus).
        for alert in getattr(device, "machine_alerts", []) or []:
            txt = (alert or "").strip()
            if not txt:
                continue
            if is_down and _is_generic_down_message(txt):
                continue
            if is_offline and _is_generic_offline_message(txt):
                continue
            if (is_offline or is_timeout_state) and _is_timeout_error(txt):
                continue
            _push(txt)
        # Consommables (toner/cartouche, tambour, bouteille de récupération,
        # agrafes) : pas de message généré par l'application. Le niveau est déjà
        # affiché visuellement (colonnes + barres de consommables) et/ou annoncé
        # par un message de l'imprimante (machine_alerts). Un message applicatif
        # du type « [Critique] Black Cartridge (1%) » ou « Bouteille presque
        # pleine » ne ferait que répéter une information déjà visible, c'est
        # pourquoi on ne le génère plus. La logique de seuils, de statuts et de
        # coloration des lignes reste inchangée.
        return " | ".join(parts)

    def _set_details_visible(self, visible):
        """Affiche ou masque le panneau « Détails de l'appareil ».

        Ne touche jamais aux données des machines : seul l'état de
        présentation (packing du panneau) change, via
        _reorder_bottom_sections() afin de conserver l'ordre des sections
        basses (statut, mode avancé, détails).
        """
        self._details_visible = bool(visible)
        self._reorder_bottom_sections()

    def clear_selection(self):
        """Désélectionne la machine et masque le panneau de détails.

        Réinitialise proprement l'état de sélection (sélection du tableau +
        dernière cellule mémorisée pour Ctrl+C) sans modifier ni supprimer les
        données de la machine, et sans déclencher de rafraîchissement.
        """
        try:
            selection = self.tree.selection()
            if selection:
                try:
                    self.tree.selection_remove(*selection)
                except Exception:
                    try:
                        self.tree.selection_set()
                    except Exception:
                        pass
        except Exception:
            pass
        self._last_clicked_cell = None
        self._set_details_visible(False)

    def on_tree_select(self, event=None):
        """Show details on simple click (no double-click needed)."""
        selection = self.tree.selection()
        if not selection:
            self._set_details_visible(False)
            return
        item = selection[0]
        device = self.device_items.get(item)
        if device:
            self.show_details(device)

    def on_cell_click(self, event=None):
        """Mémorise la cellule exacte sur laquelle l'utilisateur a cliqué afin que
        Ctrl+C puisse copier le contenu de cette cellule précise. Ne modifie pas la
        sélection de la ligne entière (mode browse) et ne rend pas les cellules
        éditables.

        La détection du second clic (désélection) est mémorisée par le tableau
        lui-même dans ``_on_press`` (sélection AVANT le clic) ; ici on ne fait
        que la recopie pour le mode Treeview historique, sans écraser une
        valeur déjà posée par le tableau.
        """
        try:
            state = getattr(event, "state", 0) or 0
        except Exception:
            state = 0
        # ttk.Treeview historique : identify_row/identify_column/identify_region
        # n'existent pas ; on utilise l'API native identify(). (Le Treeview
        # expose un attribut identify_row non appelable : le test porte donc
        # sur identify_region, qui n'existe QUE sur la table virtualisée.)
        legacy = not self._tree_is_virtual()
        if legacy:
            try:
                row_id = self.tree.identify("row", event.x, event.y) or ""
                col_id = self.tree.identify("column", event.x, event.y) or ""
                region = self.tree.identify("region", event.x, event.y) or ""
                if region == "heading":
                    region = "heading"
                elif region == "cell":
                    region = "cell"
                elif row_id:
                    region = "cell"
                else:
                    region = "nothing"
            except Exception:
                return None
        else:
            try:
                region = self.tree.identify_region(event.x, event.y)
            except Exception:
                return None
            if region != "cell":
                self._press_selection = None
                return None
            row_id = self.tree.identify_row(event.y)
            col_id = self.tree.identify_column(event.x)
        if row_id and col_id:
            self._last_clicked_cell = (row_id, col_id)
            # Mode historique (ttk.Treeview) : pas de _on_press applicatif ;
            # on mémorise ici, exactement comme le fait la table virtualisée.
            # (La table virtualisée a déjà mémorisé dans _on_press via
            # _press_row/_press_selection posés sur elle-même : on ne l'écrase
            # pas — d'où la condition sur legacy.)
            if legacy and region == "cell":
                try:
                    modifiers = int(state) & 0x0005  # Maj (0x1) / Ctrl (0x4)
                except Exception:
                    modifiers = 0
                self._press_selection = None if modifiers else tuple(self.tree.selection() or ())
                self._press_row = row_id
        else:
            self._press_selection = None
        return None

    def _on_row_release(self, event=None):
        """Relâchement du clic : désélectionne si la même ligne, déjà
        sélectionnée AVANT l'appui, vient d'être cliquée sans modificateur.

        L'état « avant » vient du tableau (``_on_press``) ou, en mode
        historique, de ``on_cell_click`` — jamais de l'état courant seul,
        ce qui rend la détection indépendante de l'ordre d'exécution des
        bindings (robuste en usage réel comme en test synthétique).
        """
        row_id = getattr(self.tree, "_press_row", None)
        before = getattr(self.tree, "_press_selection",
                         getattr(self, "_press_selection", None))
        try:
            self.tree._press_row = None
            self.tree._press_selection = None
        except Exception:
            pass
        self._press_row = None
        self._press_selection = None
        if not row_id or not before:
            return None
        try:
            state = int(getattr(event, "state", 0) or 0) & 0x0005
        except Exception:
            state = 0
        if state:
            return None
        try:
            if self._tree_is_virtual():
                row_now = self.tree.identify_row(event.y)
                region = self.tree.identify_region(event.x, event.y)
            else:
                row_now = self.tree.identify("row", event.x, event.y) or ""
                region = self.tree.identify("region", event.x, event.y) or ""
                if region != "cell" and row_now:
                    region = "cell"
        except Exception:
            return None
        if region != "cell" or row_now != row_id:
            return None
        if row_id in before and row_id in (self.tree.selection() or ()):
            self.clear_selection()
        return None

    def _tree_is_virtual(self):
        """True si le tableau est la table virtualisée (vs ttk.Treeview).

        Les deux moteurs exposent ``identify_row``/``identify_region`` :
        on les distingue par ``set_chip_provider`` (méthode propre à
        VirtualTable).
        """
        try:
            return callable(getattr(self.tree, "set_chip_provider", None))
        except Exception:
            return False

    def on_ctrl_click(self, event=None):
        """Ctrl+clic sur une adresse IP : ouvre l'interface Web de l'imprimante
        dans le navigateur par défaut, en privilégiant HTTPS avec repli sur HTTP
        si le HTTPS n'est pas servi. Le clic normal conserve son comportement
        actuel (sélection de la ligne entière)."""
        self.on_cell_click(event)
        try:
            if self._tree_is_virtual():
                region = self.tree.identify_region(event.x, event.y)
                row_id = self.tree.identify_row(event.y)
                col_id = self.tree.identify_column(event.x)
            else:
                row_id = self.tree.identify("row", event.x, event.y) or ""
                col_id = self.tree.identify("column", event.x, event.y) or ""
                region = self.tree.identify("region", event.x, event.y) or ""
                if region != "cell" and row_id:
                    region = "cell"
        except Exception:
            return None
        if region != "cell" or not row_id or not col_id:
            return None
        try:
            col_idx = int(col_id[1:]) - 1
        except ValueError:
            return None
        if col_idx < 0 or col_idx >= len(self.columns) or self.columns[col_idx] != "ip":
            return None
        values = self.tree.item(row_id, "values")
        if not values or col_idx >= len(values):
            return None
        ip = str(values[col_idx]).strip()
        if not ip:
            return None
        # La sonde HTTPS (réseau, potentiellement lente) est déléguée à un thread
        # afin de ne pas bloquer l'interface, puis le navigateur est ouvert depuis
        # ce thread avec le schéma HTTPS (ou HTTP en repli) ainsi déterminé.
        self.executor.submit(self._open_printer_web, ip)
        return None

    def _open_printer_web(self, ip):
        """Ouvre l'interface Web de l'imprimante dans le navigateur par défaut.

        Tente d'abord HTTPS et retombe sur HTTP si l'appareil ne sert pas HTTPS.
        Le choix du schéma repose sur une sonde réseau côté application (et non
        sur une erreur du navigateur). Le clic normal et les autres liens
        conservent leur comportement, et l'affichage de l'adresse IP est intact.
        """
        scheme = "https" if _https_available(ip) else "http"
        try:
            webbrowser.open(f"{scheme}://{ip}")
            self.root.after(
                0,
                lambda: self.log_status(
                    f"Ouverture de l'interface Web de {ip} dans le navigateur ({scheme})…"
                ),
            )
        except Exception:
            self.root.after(0, lambda: self.log_status("Impossible d'ouvrir le navigateur."))

    def copy_selected(self, event=None):
        """Copy the selected row information to the clipboard (read-only copy).

        Si une cellule précise a été cliquée et qu'elle appartient encore à la
        ligne actuellement sélectionnée, seul le contenu de cette cellule est
        copié (exactement la valeur affichée). Sinon, l'enregistrement complet de
        la ligne sélectionnée est copié (comportement historique). La sélection de
        la ligne entière est conservée inchangée et les cellules ne sont jamais
        rendues éditables.
        """
        selection = self.tree.selection()
        if not selection:
            return "break"

        # Copie d'une cellule précise : uniquement si la dernière cellule cliquée
        # appartient encore à la ligne actuellement sélectionnée.
        cell = getattr(self, "_last_clicked_cell", None)
        if cell:
            row_id, col_id = cell
            if row_id in selection:
                try:
                    col_idx = int(col_id[1:]) - 1
                except ValueError:
                    col_idx = -1
                values = self.tree.item(row_id, "values")
                if values and 0 <= col_idx < len(values):
                    text = str(values[col_idx])
                    try:
                        self.root.clipboard_clear()
                        self.root.clipboard_append(text)
                        self.log_status("Contenu de la cellule copié dans le presse-papiers.")
                    except Exception:
                        self.log_status("Impossible de copier dans le presse-papiers.")
                    return "break"

        # Comportement historique : copie de l'enregistrement complet de la ligne.
        # La colonne "Grand site" n'est plus affichee mais reste dans device.site :
        # on l'ajoute en tete pour conserver toute l'information metier.
        item = selection[0]
        values = self.tree.item(item, "values")
        device = self.device_items.get(item)
        lines = []
        headings = {
            "ip": "Adresse IP",
            "tag": "Tatouage",
            "location": "Localisation",
            "csv_model": "Modèle",
            "status": "Statut",
            "toner": "Toner/cartouche",
            "drum": "Tambour/photoconducteur",
            "bottle": "Bouteille récup. toner",
            "staples": "Agrafes",
            "last_update": "Dernière maj",
            "error": "Messages",
        }
        if device and getattr(device, "site", ""):
            lines.append(f"Grand site : {device.site}")
        if values:
            for idx, key in enumerate(("ip", "tag", "location", "csv_model", "status", "toner", "drum", "bottle", "staples", "last_update", "error")):
                if idx < len(values):
                    lines.append(f"{headings.get(key, key)} : {values[idx]}")
        if device:
            lines.append("")
            lines.append(f"Nom de l'appareil : {device.hostname}")
            lines.append(f"Description SNMP : {device.model}")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
            self.log_status("Informations copiées dans le presse-papiers.")
        except Exception:
            self.log_status("Impossible de copier dans le presse-papiers.")
        return "break"

    def diagnostic_snmp(self, event=None):
        """Lance le Diagnostic SNMP (mode Debug Lexmark) sur l'imprimante
        sélectionnée : SNMP WALK sur 1.3.6.1.2.1.43.11 puis 1.3.6.1.4.1.641,
        et écrit tous les OID (avec leur type et leur valeur brute) dans un
        fichier texte, sans aucun filtrage (-2, -3, 0, chaînes, "No Such
        Instance", etc. sont tous conservés)."""
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning(
                "Diagnostic SNMP",
                "Sélectionnez d'abord une imprimante dans la liste.",
            )
            return
        device = self.device_items.get(selection[0])
        if not device:
            return
        community = self.snmp_community.get()
        try:
            port = int(self.snmp_port.get())
        except Exception:
            port = 161
        self.log_status(f"Diagnostic SNMP lancé pour {device.ip}…")
        self.executor.submit(self._run_snmp_diagnostic, device, community, port)

    def _run_snmp_diagnostic(self, device, community, port):
        """Effectue les SNMP WALK (Lexmark + Epson) et ecrit le resultat.

        Conserve le format historique (bases 43.11 + 641) puis ajoute les
        branches Epson 1.3.6.1.4.1.1248.1.2.2.2.1.1 (vendor) et Remote Mode,
        plus le resume [Supplies]/[Fallback] via snmp_diagnostic_dump.
        """
        bases = ("1.3.6.1.2.1.43.11", "1.3.6.1.4.1.641",
                 "1.3.6.1.4.1.1248.1.2.2.2.1.1",
                 "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1")
        try:
            filename = f"snmp_debug_{device.ip}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = Path.cwd() / filename
        except Exception:
            filepath = None

        lines = []
        lines.append("=" * 90)
        lines.append("Diagnostic SNMP (mode Debug Lexmark)")
        lines.append(f"Horodatage : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Adresse IP : {device.ip}")
        lines.append(f"Tatouage   : {device.tag}")
        lines.append(f"Communauté : {community}   Port : {port}")
        lines.append("Aucune valeur n'est filtrée : -2, -3, 0, chaînes vides, "
                     "'No Such Instance', etc. sont toutes conservées.")
        lines.append("=" * 90)

        totals = {}
        all_rows = []
        for base in bases:
            lines.append("")
            lines.append(f"### SNMP WALK {base} ###")
            try:
                rows = device.snmp_walk_raw(community, port, base)
            except Exception as exc:
                logging.exception("Diagnostic SNMP walk failed for %s oid %s", device.ip, base)
                lines.append(f"ERREUR lors du WALK {base} : {exc}")
                totals[base] = 0
                continue
            for oid_name, type_info, value_raw in rows:
                all_rows.append((base, oid_name, type_info, value_raw))
            lines.append(f"-- {len(rows)} OID(s) collecté(s) pour {base} --")
            totals[base] = len(rows)

        for base, oid_name, type_info, value_raw in all_rows:
            line = f"{oid_name}\t{type_info}\t{value_raw}"
            lines.append(line)
            logging.debug("SNMP DEBUG [%s] %s : %s", base, device.ip, line)

        lines.append("")
        lines.append("=" * 90)
        total = sum(totals.values())
        lines.append(f"TOTAL OID collectés : {total}")
        try:
            _dump = device.snmp_diagnostic_dump(community, port)
            lines.append("")
            lines.append("### RESUME DIAGNOSTIC (Printer/sysDescr/Epson/Supplies) ###")
            lines.append(_dump)
        except Exception as exc:
            lines.append(f"RESUME DIAGNOSTIC indisponible : {exc}")
        lines.append(f"Fichier            : {filepath}")
        content = "\n".join(lines) + "\n"

        try:
            if filepath is not None:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                raise OSError("dossier courant indisponible")
        except Exception as exc:
            logging.exception("Impossible d'écrire le fichier de diagnostic SNMP")
            logging.info("Diagnostic SNMP (vers logs uniquement) :\n%s", content)

        dir_label = str(filepath) if filepath is not None else "(logs uniquement)"
        self.root.after(0, lambda: self.log_status(
            f"Diagnostic SNMP terminé pour {device.ip} : {total} OID -> {dir_label}"
        ))
        try:
            self.root.after(0, lambda: messagebox.showinfo(
                "Diagnostic SNMP",
                f"Diagnostic terminé pour {device.ip}.\n\n"
                f"OID collectés : {total}\n"
                f"Fichier :\n{dir_label}",
            ))
        except Exception:
            pass

    def _get_device_priority(self, device):
        """Priorité UNIQUE du tri « Default » (fonction de référence).

        La priorité est calculée au niveau de la MACHINE ENTIÈRE, à partir des
        états réels déterminés par l'application — statut de la machine, état
        FINAL de chaque consommable (seuils + exceptions par modèle déjà
        appliqués par les fonctions existantes) — JAMAIS du texte de la colonne
        « Messages » ni d'un simple pourcentage.

        Ordre obligatoire (plus petit = plus prioritaire) :
          0 = HORS LIGNE        (reachability == "offline")
          1 = DOWN              (statut "down", erreur SNMP, timeout SNMP)
          2 = REMPLACEMENT      (au moins UN consommable au seuil de
                                 remplacement, tous types confondus)
          3 = ALERTE            (aucun en remplacement mais au moins UN
                                 consommable au seuil d'alerte)
          4 = NORMAL            (aucune alerte)

        Point clé (cause du bug historique) : les alertes machine lues dans
        prtAlertTable (ex. « [Critique] Code 72.01 — SMTP server not set up »)
        NE participent PLUS à la priorité. Ces messages textuels existaient sur
        des machines dont le statut réel était « running »/« warning » : la
        règle précédente les classait toutes en priorité 1 (DOWN), ce qui
        aplatissait tout le parc dans un seul bucket départagé par IP — l'ordre
        du tri Default semblait donc inopérant. La catégorie DOWN est
        désormais réservée au statut réel de la machine (« down », erreur SNMP,
        timeout), exactement comme la coloration des lignes.

        Les consommables sont évalués via les fonctions d'état EXISTANTES (aucun
        seuil recalculé, aucun comportement de couleur/modification) :
          - toners/cartouches et tambours : _get_supply_state (qui applique
            _get_critical_threshold, donc les exceptions par modèle) ;
          - bouteille de récupération : get_bottle_status (« Pleine » ou
            « Alerte » = remplacement, sémantique existante) ;
          - agrafes : get_staples_status (« Alerte » = remplacement) ;
          - autres catégories (developer, fuser, kit maintenance...) :
            « normal » au sens de l'application (pas de seuil propre), comme
            dans _get_device_global_state.

        L'enchaînement des tests garantit qu'une machine cumulant plusieurs
        problèmes reçoit la priorité de son état LE PLUS important : une
        machine DOWN avec un toner à 5 % reste dans DOWN, une machine hors
        ligne avec un consommable à 1 % reste dans HORS LIGNE.
        """
        # 1. HORS LIGNE (joignabilité réseau) → priorité maximale.
        if getattr(device, "reachability", None) == "offline":
            return 0
        # 2. DOWN : statut réel de la machine en erreur (hrDeviceStatus=5),
        #    erreur SNMP ou timeout (machine joignable mais pilotable en échec).
        if getattr(device, "error", None):
            return 1
        if getattr(device, "reachability", None) == "snmp_timeout":
            return 1
        if (getattr(device, "status", "") or "").strip().lower() == "down":
            return 1
        # 3/4. Consommables : états FINAUX (seuils + exceptions déjà inclus).
        #      Un SEUL consommable au seuil de remplacement rend toute la
        #      machine « remplacement » ; sinon un seul au seuil d'alerte la
        #      rend « alerte ».
        has_replacement = False
        has_warning = False
        for supply in getattr(device, "supplies", []) or []:
            state = self._get_supply_state(supply, device)
            if state == "critical":
                has_replacement = True
            elif state == "low":
                has_warning = True
        if device.get_bottle_status(self.waste_toner_threshold.get()) in ("Pleine", "Alerte"):
            has_replacement = True
        if device.get_staples_status(10) == "Alerte":
            has_replacement = True
        if has_replacement:
            return 2
        if has_warning:
            return 3
        # 5. Aucune alerte.
        return 4

    def _device_sort_key(self, device):
        """Compute a comparison key for the selected sort mode.

        Pour les modes « par niveau », la machine est représentée par le
        pourcentage le plus bas (le plus critique) parmi TOUS ses
        consommables : toner / cartouche d'encre, tambour / photoconducteur,
        bouteille de récupération (waste toner) et agrafes, ainsi que tout
        autre consommable éventuel. On utilise la même mesure que celle
        affichée dans l'interface (display_percentage) quel que soit le type.
        """
        mode = self.sort_mode.get()

        # Collecte une valeur numérique comparable pour chaque consommable,
        # en préférant la mesure affichée (display_percentage) puis le
        # pourcentage brut. Les valeurs manquantes (niveau inconnu) sont
        # ignorées, comme dans l'ancienne implémentation.
        levels = []
        for supply in getattr(device, "supplies", []) or []:
            pct = supply.display_percentage
            if pct is None:
                pct = supply.percentage
            if pct is None:
                continue
            try:
                levels.append(int(pct))
            except (TypeError, ValueError):
                continue

        # Un faible pourcentage signifie un besoin d'intervention pour
        # chacun des types (ex. une bouteille de récupération pleine
        # s'affiche à un faible pourcentage, des agrafes à 5 % sont quasi
        # épuisées). La machine est donc représentée par son consommable le
        # plus bas (le plus critique), en cohérence avec
        # _get_device_global_state.
        min_pct = min(levels) if levels else None

        def pct_rank(pct):
            # None (inconnue) : trié après les valeurs connues.
            return 0 if pct is not None else 1

        def reversed_string_key(s):
            # Clé lexicographique inversée : trier sur cette clé en ordre
            # croissant équivaut à trier sur `s` en ordre décroissant. Utilisée
            # comme départage (site, IP) du tri décroissant pour que les deux
            # directions soient exactement l'inverse l'une de l'autre, même
            # lorsque plusieurs machines sont au même niveau.
            return tuple(-ord(c) for c in s) + (-1,)

        if mode == "Niveau bas → haut":
            # Pourcentages croissants (les plus bas d'abord) ; inconnus à la fin.
            return (
                pct_rank(min_pct),
                0 if min_pct is None else min_pct,
                (device.site or "").lower(),
                device.ip,
            )
        if mode == "Niveau haut → bas":
            # Pourcentages décroissants (les plus hauts d'abord) ; inconnus à la
            # fin. Le départage (site, IP) est lui aussi inversé pour que ce tri
            # soit le miroir exact du tri ascendant.
            return (
                pct_rank(min_pct),
                0 if min_pct is None else -min_pct,
                reversed_string_key((device.site or "").lower()),
                reversed_string_key(device.ip),
            )

        # Tri par défaut : priorité (hors ligne > erreur/DOWN/al. machine >
        # consommable critique > consommable bas > normal), déterminée par
        # _get_device_priority à partir de l'état réel (jamais du texte de la
        # colonne « Messages »). Le départage intra-priorité est DÉTERMINISTE :
        # site -> modèle (si modèle SNMP connu) / cs_Model -> IP.
        #
        # DÉTECTION DU MODE PAR DÉFAUT : tout mode qui n'est PAS un tri
        # « par niveau » tombe dans cette branche — c'est-à-dire le libellé
        # canonique SORT_MODE_DEFAULT (« Défaut (alertes en premier) »),
        # l'identifiant technique « default » (valeur historique de
        # self.sort_mode) et toute valeur inconnue. Cette clé ne
        # dépend que de l'état des devices et ne garde aucune trace du tri de
        # niveau précédent : lorsqu'on sélectionne « Défaut », chaque ligne est
        # reclassée depuis zéro, sans ordre résiduel hérité.
        #
        # NB : le mode par défaut ne lit jamais min_pct/levels ; il se base
        # uniquement sur les statuts calculés (_get_supply_state et consommables
        # particuliers bouteille/agrafes), avec application des exceptions de
        # seuils par modèle (_get_critical_threshold).
        priority = self._get_device_priority(device)
        return (
            priority,
            (device.site or "").strip().lower(),
            ((device.csv_model or device.model) or "").strip().lower(),
            device.ip,
        )

    @staticmethod
    def _normalize_model_exceptions(raw):
        """Normalise la liste des exceptions par modèle depuis la configuration
        en une liste propre de dictionnaires avec des seuils entiers."""
        result = []
        for exc in raw or []:
            if not isinstance(exc, dict):
                continue
            models = [str(m).strip() for m in exc.get("models", []) if str(m).strip()]
            toner_val = exc.get("toner", 4)
            drum_val = exc.get("drum", 4)
            try:
                toner_val = int(toner_val)
            except (TypeError, ValueError):
                toner_val = 4
            try:
                drum_val = int(drum_val)
            except (TypeError, ValueError):
                drum_val = 4
            result.append({"models": models, "toner": toner_val, "drum": drum_val})
        return result

    def _get_critical_threshold(self, device, category):
        """Return the critical replacement threshold for the given device and
        consumable category.

        Priority:
        1. Exception correspondant au modèle de l'imprimante (case-insensitive)
        2. Seuil global par défaut
        """
        if device is not None:
            model_strings = []
            if device.model:
                model_strings.append(str(device.model))
            if device.csv_model:
                model_strings.append(str(device.csv_model))
            # Correspondance stricte par « token » : une exception ne s'applique
            # que lorsque le modèle de l'imprimante (SNMP ou CMDB) contient
            # exactement l'un des modèles configurés comme mot entier, insensible
            # à la casse. L'ancienne correspondance par sous-chaîne pouvait
            # appliquer le seuil d'un modèle à une autre machine (ex. « X9210 »
            # matcherait « x921 ») et donc déclencher des faux rouges/avertissements.
            dev_tokens = set()
            for dev_model in model_strings:
                for token in re.split(r"[^A-Za-z0-9]+", str(dev_model).lower()):
                    if token:
                        dev_tokens.add(token)
            for exc in self.model_exceptions:
                exc_models = [
                    str(m).strip().lower()
                    for m in exc.get("models", [])
                    if str(m).strip()
                ]
                for exc_model in exc_models:
                    if exc_model and exc_model in dev_tokens:
                        if category == "drum":
                            return exc.get("drum", self.drum_threshold.get())
                        return exc.get("toner", self.toner_threshold.get())
        # Global default
        if category == "drum":
            return self.drum_threshold.get()
        return self.toner_threshold.get()

    def _get_supply_state(self, supply, device=None):
        """Return the alert state for a single supply:
        - 'critical' when at/below the configured replacement threshold
        - 'low' when at/below the warning threshold
        - 'normal' otherwise

        Seuls les toners/cartouches et tambours/photoconducteurs sont évalués
        par rapport aux seuils configurés. Les autres catégories (bouteille de
        récupération, agrafes, developer, fuser, maintenance, etc.) possèdent
        leur propre sémantique (get_bottle_status / get_staples_status) et ne
        doivent pas déclencher de faux « critical/low » avec un seuil de
        toner/tambour qui ne les concerne pas.

        When ``device`` is provided, the critical threshold is resolved through
        the model-specific exception table (if a matching model exists) before
        falling back to the global default.
        """
        if supply.category not in ("toner", "drum"):
            return "normal"
        pct = supply.percentage
        if pct is None:
            return "normal"
        threshold = self._get_critical_threshold(device, supply.category)
        if supply.category == "toner":
            warn = self.toner_warning_threshold.get()
        else:  # drum
            warn = self.drum_warning_threshold.get()
        if pct <= threshold:
            return "critical"
        if pct <= warn:
            return "low"
        return "normal"

    def _get_device_global_state(self, device):
        """Return the most critical supply state across all consumables:
        'critical' > 'low' > 'normal'."""
        state_rank = {"normal": 0, "low": 1, "critical": 2}
        worst = "normal"
        for supply in device.supplies:
            state = self._get_supply_state(supply, device)
            if state_rank.get(state, 0) > state_rank.get(worst, 0):
                worst = state
        # Bottle and staples have their own alert semantics. A waste/collection
        # bottle reaching its alert threshold (by default 5 %) is treated as a
        # CRITICAL condition (the bottle is almost full and must be emptied),
        # shown in red — the same "replacement" treatment as a toner at its
        # replacement threshold. The "Pleine" (0 %) state remains critical.
        bottle = device.get_bottle_status(self.waste_toner_threshold.get())
        if bottle in ("Pleine", "Alerte"):
            worst = "critical"
        if device.get_staples_status(10) == "Alerte":
            worst = "critical"
        return worst

    def _get_other_supply_state(self, device, supply):
        """State for the "Autres consommables" bars (waste bottle, staples, ...).

        The waste/collection bottle and the staples have their own alert
        semantics (get_bottle_status / get_staples_status) and must not be
        evaluated with the toner/drum thresholds applied by _get_supply_state.

        - Bouteille de récupération : au-dessus de son seuil (par défaut 5 %,
          « presque pleine ») => 'critical' (rouge), cohérent avec l'état global.
        - Agrafes : au seuil fixe de 10 % => 'critical' (rouge).
        - Les autres catégories (developer, fuser, maintenance, kit, ...) n'ont
          pas de seuil propre : resta 'normal'.
        """
        if supply.is_receptacle():
            if supply.percentage is not None and supply.percentage <= self.waste_toner_threshold.get():
                return "critical"
            return "normal"
        desc = (supply.description or "").lower()
        if any(k in desc for k in ("agraf", "agrafe", "staple")):
            if supply.percentage is not None and supply.percentage <= 10:
                return "critical"
            return "normal"
        return "normal"

    def _chips_for_row(self, iid, values=None):
        """Pastilles à dessiner pour une ligne (chips des colonnes concernées).

        Appelée par la table virtualisée UNIQUEMENT pour les lignes réellement
        peintes (donc visibles) : le coût du calcul reste proportionnel à
        l'écran, jamais au nombre total d'imprimantes. La logique métier est
        celle, inchangée, de ``chips.chips_for_device``.
        """
        device = self.device_items.get(iid)
        if device is None:
            return {}
        try:
            return chips_for_device(device, self)
        except Exception:
            return {}

    def _get_toner_color(self, supply):
        """Return the real toner color for a supply based on its description."""
        desc = (supply.description or "").lower()
        if 'cyan' in desc or 'bleu' in desc or re.search(r'\bC\b', desc, re.I):
            return TONER_COLORS["C"]
        if 'magenta' in desc or re.search(r'\bM\b', desc, re.I):
            return TONER_COLORS["M"]
        if 'yellow' in desc or 'jaune' in desc or re.search(r'\bY\b', desc, re.I):
            return TONER_COLORS["Y"]
        if 'black' in desc or 'noir' in desc or 'ink' in desc or re.search(r'\bK\b', desc, re.I):
            return TONER_COLORS["K"]
        # Kyocera mono : référence commerciale seule (ex. "TK-3130"), sans
        # mot-couleur. Règle ciblée sur références noires avérées uniquement.
        if is_kyocera_black_toner_ref(getattr(supply, "description", "")):
            return TONER_COLORS["K"]
        return None

    def _make_group(self, title):
        """Create a titled group section inside the details panel. The
        consumable bars of the group are rendered into the returned inner frame
        with a multi-column grid that spans the whole available width."""
        group = ttk.LabelFrame(self.detail_bar_frame, text=title)
        group.pack(fill="x", padx=4, pady=(4, 0))
        bars_frame = ttk.Frame(group, style="Card.TFrame")
        bars_frame.pack(fill="x", padx=4, pady=4)
        bars_frame._bar_count = 0
        # Grille régulière : nombre de colonnes calculé à partir de la largeur
        # réelle de la fenêtre (cellules d'environ 330px, 1 à 4 colonnes).
        # Tous les groupes partagent la même grille pour un alignement
        # vertical cohérent, quel que soit le nombre de consommables.
        bars_frame._bar_columns = self._compute_bar_columns()
        return bars_frame

    def _compute_bar_columns(self):
        """Nombre de colonnes de la grille des consommables : vise des
        cellules d'environ 330px de large, entre 1 et 4 colonnes selon la
        largeur réelle de la fenêtre. La largeur de la fenêtre racine est
        utilisée plutôt que celle du canvas : en cas de débordement horizontal
        du tableau (colonnes fixes), le canvas suit la cavité de packing et
        non la largeur visible."""
        try:
            panel_w = max(300, self.root.winfo_width() - 60)
        except Exception:
            panel_w = 900
        return max(1, min(4, int(panel_w // 330)))

    def _bar_canvas_width(self, parent):
        """Calcule la largeur fluide de la barre de consommable en fonction de
        la largeur disponible du parent. Retourne une largeur minimale absolue
        pour éviter des barres trop étroites sur les petits écrans."""
        try:
            avail = parent.winfo_width()
        except Exception:
            avail = 0
        if avail and avail > 0:
            # On réserve de l'espace pour l'indicateur couleur, l'icône, le
            # label et les marges (~90px), puis on divise par le nombre de
            # colonnes pour obtenir la largeur par barre.
            cols = getattr(parent, "_bar_columns", 2)
            bar_w = int((avail - 100) / max(1, cols))
            return max(120, min(bar_w, 360))
        return 180

    def _render_bar(self, parent, label, pct, color=None, state="normal"):
        row = ttk.Frame(parent, style="Card.TFrame")
        # Lay the consumable bars out in a compact multi-column grid instead of
        # one long vertical list, so the whole width of the details panel is
        # used. Each additional bar fills the next column before wrapping to a
        # new row.
        columns = max(1, getattr(parent, "_bar_columns", 2))
        idx = getattr(parent, "_bar_count", 0)
        parent._bar_count = idx + 1
        row.grid(
            row=idx // columns,
            column=idx % columns,
            sticky="ew",
            padx=4,
            pady=2,
        )
        parent.columnconfigure(idx % columns, weight=1)
        # Color indicator dot: real consumable color when available, otherwise
        # a neutral grey.
        indicator_color = color or "#9e9e9e"
        indicator = tk.Canvas(
            row, width=16, height=16, highlightthickness=0, background=SURFACE
        )
        indicator.pack(side="left", padx=(0, 4), pady=(2, 0))
        indicator.create_oval(2, 2, 14, 14, fill=indicator_color, outline="")
        # State icon (warning triangle) next to the label
        if state == "critical":
            state_icon = tk.Canvas(
                row, width=16, height=16, highlightthickness=0, background=SURFACE
            )
            state_icon.pack(side="left", padx=(0, 2), pady=(2, 0))
            state_icon.create_polygon(8, 2, 14, 14, 2, 14, fill="#d32f2f", outline="")
        elif state == "low":
            state_icon = tk.Canvas(
                row, width=16, height=16, highlightthickness=0, background=SURFACE
            )
            state_icon.pack(side="left", padx=(0, 2), pady=(2, 0))
            state_icon.create_polygon(8, 2, 14, 14, 2, 14, fill="#ff9800", outline="")
        # Label : tronqué à la source (short_description) et sans contrainte de
        # largeur ; la barre absorbe tout l'espace restant de la colonne.
        label_widget = ttk.Label(row, text=label, style="Card.TLabel")
        label_widget.pack(side="left", padx=(0, 4))
        canvas = tk.Canvas(row, height=16, highlightthickness=0, background=SURFACE)
        canvas.pack(side="left", fill="x", expand=True, padx=6, pady=(2, 0))
        width = self._bar_canvas_width(parent)
        canvas.config(width=width, height=16)
        # La barre est redessinée à chaque changement de taille du canvas :
        # elle reste fluide et correctement alignée, de la petite fenêtre au
        # plein écran, sans dépendre d'une largeur calculée une seule fois.
        canvas._bar_pct = pct
        canvas._bar_color = color
        canvas._bar_state = state

        def _draw_bar(event=None, c=canvas):
            full_w = c.winfo_width()
            if full_w < 10:
                # Fallback avant le premier étalonnage du layout.
                full_w = self._bar_canvas_width(parent)
            # Plafonne la largeur dessinée : la barre reste dense et lisible
            # même lorsque la cellule s'élargit sur un grand écran.
            w = min(full_w, 360)
            h = 16
            c.delete("all")
            # Fond gris (niveau manquant)
            c.create_rectangle(0, 0, w, h, fill="#e6e6e6", outline="")
            pct_v = getattr(c, "_bar_pct", None)
            if pct_v is not None:
                p = max(0, min(100, int(pct_v)))
                fill_w = int((p / 100.0) * w)
                # Couleur réelle du consommable ; bleu-gris neutre sinon.
                bar_color = getattr(c, "_bar_color", None) or "#607d8b"
                c.create_rectangle(0, 0, fill_w, h, fill=bar_color, outline="")
                # Bordure d'état : orange (faible), rouge (critique)
                st = getattr(c, "_bar_state", "normal")
                if st == "critical":
                    c.create_rectangle(0, 0, w, h, outline="#d32f2f", width=2)
                elif st == "low":
                    c.create_rectangle(0, 0, w, h, outline="#ff9800", width=2)
                # Texte du pourcentage : noir ou blanc choisi automatiquement
                # (ratio de contraste WCAG) selon la couleur réellement
                # présente sous le texte — couleur de la jauge quand le texte
                # tient dans la zone remplie, fond gris clair sinon. Reste
                # lisible pour toute couleur de consommable, actuelle ou future.
                text = f"{p}%"
                try:
                    import tkinter.font as tkfont
                    _font = tkfont.nametofont("TkDefaultFont")
                    text_w = _font.measure(text)
                except Exception:
                    text_w = len(text) * 7  # estimation prudente en secours
                if text_w + 8 <= fill_w:
                    # Le texte tient sur la jauge remplie : ancré à droite de
                    # la zone remplie, contraste calculé sur bar_color.
                    text_color = text_color_for_bg(bar_color)
                    c.create_text(fill_w - 4, h // 2, text=text,
                                  anchor="e", fill=text_color)
                else:
                    # Remplissage trop faible : texte à droite de la jauge,
                    # sur le fond gris clair, contraste calculé sur ce fond.
                    bg_color = "#e6e6e6"
                    text_color = text_color_for_bg(bg_color)
                    c.create_text(min(w - 4, max(fill_w + 8, 36)), h // 2,
                                  text=text, anchor="w", fill=text_color)
            else:
                c.create_text(w / 2, h // 2, text="Niveau inconnu", fill="#333333")

        canvas.bind("<Configure>", _draw_bar)
        _draw_bar()

    def show_details(self, device):
        # Toute (re)sélection réaffiche le panneau s'il avait été masqué.
        if not getattr(self, "_details_visible", True):
            self._set_details_visible(True)
        # Clear existing bars
        for child in self.detail_bar_frame.winfo_children():
            child.destroy()
        # Consumable bars are rebuilt below as titled multi-column groups.

        lines = [
            f"Adresse IP : {device.ip}",
            f"Tatouage : {device.tag}",
            f"Marque : {device.brand}",
            f"Modèle CMDB : {device.csv_model}",
            f"Statut : {device.status}",
            f"Nom de l'appareil : {device.hostname}",
            f"Description SNMP : {device.model}",
            f"Site/Emplacement : {device.site or device.location}",
            f"Dernière mise à jour : {device.last_update}",
            "",
        ]
        if device.error:
            lines.insert(5, f"Erreur SNMP : {device.error}")

        machine_alerts = getattr(device, "machine_alerts", []) or []
        if machine_alerts:
            lines.append("Alertes machine :")
            for alert in machine_alerts:
                lines.append(f"  - {alert}")
            lines.append("")

        toner_summary = device.get_supply_summary("toner")
        drum_summary = device.get_supply_summary("drum")
        if toner_summary:
            lines.append(f"Résumé toner/cartouche : {toner_summary}")
        if drum_summary:
            lines.append(f"Résumé tambour/photoconducteur : {drum_summary}")
        lines.append("")

        # Render the consumable bars grouped by category: tones/cartridges,
        # drums/photoconductors and the remaining consumables (waste-toner
        # bottle, staples, developer, fuser, maintenance kits, ...). Each group
        # is a titled section whose bars fill the whole available width.
        toners = [s for s in device.supplies if s.category == 'toner']
        if toners:
            toner_group = self._make_group("Toners / Cartouches")
            # Try to map colours by description
            color_map = {}
            for s in toners:
                desc = (s.description or '').lower()
                if 'cyan' in desc or 'bleu' in desc or re.search(r'\bC\b', desc, re.I):
                    color_map['C'] = s
                elif 'magenta' in desc or re.search(r'\bM\b', desc, re.I):
                    color_map['M'] = s
                elif 'yellow' in desc or 'jaune' in desc or re.search(r'\bY\b', desc, re.I):
                    color_map['Y'] = s
                elif 'black' in desc or 'noir' in desc or 'ink' in desc or re.search(r'\bK\b', desc, re.I):
                    color_map['K'] = s
                # Kyocera mono : référence seule (ex. "TK-3130"), sans
                # mot-couleur. Règle ciblée sur références noires avérées.
                elif is_kyocera_black_toner_ref(s.description):
                    color_map['K'] = s
            # Render in C M Y K order
            for key, name in (('C', 'Cyan'), ('M', 'Magenta'), ('Y', 'Yellow'), ('K', 'Black')):
                s = color_map.get(key)
                if s:
                    color = self._get_toner_color(s) or TONER_COLORS.get(key)
                    state = self._get_supply_state(s, device)
                    self._render_bar(toner_group, f"{name}", s.percentage, color=color, state=state)
                else:
                    self._render_bar(toner_group, f"{name}", None)

        # Render drums / photoconductors / imaging units. A single consumable can
        # cover several colours (shared CMY or Black + CMY kit): render one bar
        # per concerned colour, all with the same SNMP level (single common
        # consumable -> no invented separate levels).
        drums = [s for s in device.supplies if s.category == 'drum']
        if drums:
            drum_group = self._make_group("Tambours / Photoconducteurs")
            for d in drums:
                state = self._get_supply_state(d, device)
                pct = d.percentage
                cols = d.drum_colors()
                if len(cols) == 1:
                    self._render_bar(
                        drum_group,
                        d.short_description(),
                        pct,
                        color=TONER_COLORS[cols[0]],
                        state=state,
                    )
                else:
                    base = d.short_description()
                    for c in cols:
                        self._render_bar(
                            drum_group,
                            f"{base} - {DRUM_COLOR_NAMES[c]}",
                            pct,
                            color=TONER_COLORS[c],
                            state=state,
                        )

        # Other consumables: waste-toner bottle, staples and anything remaining
        # (developer, fuser, maintenance kit, ...).
        bottle_status = device.get_bottle_status(self.waste_toner_threshold.get())
        staples_status = device.get_staples_status(10)
        if bottle_status and bottle_status != "None":
            lines.append(f"Bouteille récup. : {bottle_status}")
        if staples_status and staples_status != "None":
            lines.append(f"Agrafes : {staples_status}")
        others = [s for s in device.supplies if s.category not in ('toner', 'drum')]
        if others:
            other_group = self._make_group("Autres consommables")
            for s in others:
                state = self._get_other_supply_state(device, s)
                desc = (s.description or "").lower()
                if s.is_receptacle():
                    self._render_bar(other_group, "Bouteille récup.", s.display_percentage, state=state)
                elif any(k in desc for k in ("agraf", "agrafe", "staple")):
                    self._render_bar(other_group, "Agrafes", s.percentage, state=state)
                else:
                    self._render_bar(other_group, s.short_description(), s.percentage, state=state)

        if not device.supplies:
            lines.append("Aucun consommable détecté ou non supporté par l'imprimante.")

        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", END)
        self.detail_text.insert(END, "\n".join(lines))
        self.detail_text.config(state="disabled")
        # Force l'actualisation du layout Tkinter (traite les événements
        # Configure en attente) puis recalcul explicite de la zone scrollable
        # pour corriger l'état de la scrollbar après la reconstruction des
        # barres de consommables. Cela garantit que la scrollbar est
        # correctement affichée ou masquée selon la hauteur réelle du contenu.
        self.root.update_idletasks()
        self._on_detail_bar_configure()

    def open_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Paramètres SNMP et seuils")
        dialog.grab_set()
        dialog.resizable(True, True)
        style_toplevel(dialog)
        fields = [
            ("Communauté SNMP", self.snmp_community),
            ("Port SNMP", self.snmp_port),
            ("Seuil remplacement toner/cartouche (%)", self.toner_threshold),
            ("Seuil remplacement tambour/photoconducteur (%)", self.drum_threshold),
            ("Seuil alerte toner/cartouche (%)", self.toner_warning_threshold),
            ("Seuil alerte tambour/photoconducteur (%)", self.drum_warning_threshold),
            ("Seuil alerte bouteille récup. (%)", self.waste_toner_threshold),
        ]
        for row, (label_text, variable) in enumerate(fields):
            ttk.Label(dialog, text=label_text).grid(row=row, column=0, sticky="w", padx=8, pady=6)
            widget = ttk.Entry(dialog, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        # ── Section : Exceptions par modèle ──
        row = len(fields)
        ttk.Separator(dialog, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=10)
        row += 1
        ttk.Label(
            dialog,
            text="Exceptions par modèle (seuils critiques par modèle d'imprimante)",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        row += 1
        # Zone d'affichage des exceptions (canvas scrollable si nécessaire)
        self._exception_rows = []
        self._exceptions_canvas = tk.Canvas(
            dialog, height=180,
            highlightthickness=1, highlightbackground="#e2e8f0",
            background=SURFACE, borderwidth=0,
        )
        self._exceptions_vbar = ttk.Scrollbar(
            dialog, orient="vertical", command=self._exceptions_canvas.yview
        )
        self._exceptions_canvas.configure(yscrollcommand=self._exceptions_vbar.set)
        self._exceptions_canvas.grid(
            row=row, column=0, columnspan=2, sticky="nsew", padx=8, pady=4
        )
        self._exceptions_vbar.grid(row=row, column=2, sticky="ns", padx=(0, 8), pady=4)
        dialog.grid_rowconfigure(row, weight=1)
        dialog.grid_columnconfigure(1, weight=1)
        # Frame intérieure du canvas : sa largeur est alignée sur celle du
        # canvas (le débordement vertical est géré par la scrollbar), ce qui
        # donne aux chips/tags une largeur stable pour leur disposition en flux.
        self._exception_container = ttk.Frame(self._exceptions_canvas, style="Card.TFrame")
        self._exception_container_window = self._exceptions_canvas.create_window(
            (0, 0), window=self._exception_container, anchor="nw"
        )
        self._exceptions_canvas.bind(
            "<Configure>",
            lambda e: self._exceptions_canvas.itemconfigure(
                self._exception_container_window, width=e.width
            ),
        )

        def _on_exceptions_configure(event):
            self._exceptions_canvas.configure(
                scrollregion=self._exceptions_canvas.bbox("all")
            )
        self._exception_container.bind("<Configure>", _on_exceptions_configure)

        def _on_exceptions_mousewheel(event):
            if event.num == 5 or event.delta == -120:
                self._exceptions_canvas.yview_scroll(1, "unit")
            elif event.num == 4 or event.delta == 120:
                self._exceptions_canvas.yview_scroll(-1, "unit")

        self._exceptions_canvas.bind(
            "<Enter>",
            lambda e: dialog.bind_all("<MouseWheel>", _on_exceptions_mousewheel),
        )
        self._exceptions_canvas.bind(
            "<Leave>", lambda e: dialog.unbind_all("<MouseWheel>")
        )
        # Construire les rangées d'exceptions existantes
        for exc in self.model_exceptions:
            self._add_exception_row(self._exception_container, exc)
        # Bouton d'ajout d'une nouvelle exception
        row += 1
        ttk.Button(
            dialog, text="+ Ajouter une exception",
            command=self._add_exception_click,
        ).grid(row=row, column=0, columnspan=2, pady=4)
        # Section rafraîchissement automatique
        row += 1
        ttk.Separator(dialog, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        row += 1
        ttk.Checkbutton(
            dialog,
            text="Activer le rafraîchissement automatique",
            variable=self.autorefresh_enabled_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=6)
        row += 1
        ttk.Label(dialog, text="Intervalle de rafraîchissement (minutes)").grid(
            row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(dialog, textvariable=self.autorefresh_interval_var).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6)
        # ── Section : Historique local ──
        row += 1
        ttk.Separator(dialog, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        row += 1
        ttk.Checkbutton(
            dialog,
            text="Activer l'historique local (SQLite)",
            variable=self._history_enabled_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        row += 1
        ttk.Label(dialog, text="Durée de rétention de l'historique (jours)").grid(
            row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(dialog, textvariable=self._history_retention_var).grid(
            row=row, column=1, sticky="ew", padx=8, pady=4)
        row += 1
        ttk.Button(dialog, text="Enregistrer", command=lambda: self.save_settings(dialog)).grid(
            row=row + 1, column=0, columnspan=2, pady=10
        )
        dialog.columnconfigure(1, weight=1)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        # Taille par défaut mesurée d'après le contenu (winfo_req*), bornée
        # par la taille de l'écran si aucun état n'a encore été mémorisé.
        setup_secondary_window(dialog, "parametres", min_size=(560, 440))

    def _add_exception_row(self, parent, exc_data=None):
        """Add a single exception row to the exceptions container."""
        row_idx = len(self._exception_rows)
        if exc_data is None:
            exc_data = {"models": [], "toner": 4, "drum": 4}
        row_frame = ttk.Frame(parent, style="Card.TFrame")
        row_frame.pack(fill="x", pady=4, padx=2)
        # Header: label + bouton suppression
        header = ttk.Frame(row_frame, style="Card.TFrame")
        header.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(
            header, text=f"Exception {row_idx + 1} :",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            header, text="×", width=3,
            command=lambda: self._remove_exception_row(row_frame),
        ).pack(side="right", padx=2)
        # Section modèles (tags/chips)
        model_section = ttk.Frame(row_frame, style="Card.TFrame")
        model_section.pack(fill="x", pady=(0, 2), padx=6)
        ttk.Label(model_section, text="Modèles :").pack(side="left", padx=(4, 4))
        tags_frame = ModelTagsFrame(
            model_section, initial_models=exc_data.get("models", [])
        )
        tags_frame.pack(side="left", fill="x", expand=True, padx=4, pady=2)
        # Seuils toner / tambour
        thresh_row = ttk.Frame(row_frame, style="Card.TFrame")
        thresh_row.pack(fill="x", pady=(0, 6), padx=6)
        ttk.Label(thresh_row, text="Toner/cart. (%) :").pack(side="left", padx=(4, 4))
        toner_var = IntVar(value=exc_data.get("toner", self.toner_threshold.get()))
        ttk.Entry(thresh_row, textvariable=toner_var, width=6).pack(side="left", padx=(0, 12))
        ttk.Label(thresh_row, text="Tambour/photo. (%) :").pack(side="left", padx=(0, 4))
        drum_var = IntVar(value=exc_data.get("drum", self.drum_threshold.get()))
        ttk.Entry(thresh_row, textvariable=drum_var, width=6).pack(side="left", padx=(0, 4))
        self._exception_rows.append({
            "frame": row_frame,
            "tags_frame": tags_frame,
            "toner_var": toner_var,
            "drum_var": drum_var,
        })

    def _update_exception_labels(self):
        """Met à jour les numéros d'exception après ajout/suppression."""
        for i, row_data in enumerate(self._exception_rows):
            header = row_data["frame"].winfo_children()[0]
            label = header.winfo_children()[0]
            label.config(text=f"Exception {i + 1} :")

    def _remove_exception_row(self, row_frame):
        """Supprime une rangée d'exception de l'interface."""
        for i, row_data in enumerate(self._exception_rows):
            if row_data["frame"] is row_frame:
                row_frame.destroy()
                self._exception_rows.pop(i)
                break
        self._update_exception_labels()

    def _add_exception_click(self):
        """Ajoute une nouvelle exception vide lors d'un clic sur '+'."""
        if not hasattr(self, "_exception_container"):
            return
        self._add_exception_row(self._exception_container)

    def save_settings(self, dialog):
        self.config["snmp_community"] = self.snmp_community.get()
        self.config["snmp_port"] = self.snmp_port.get()
        self.config["thresholds"] = {
            "toner": self.toner_threshold.get(),
            "drum": self.drum_threshold.get(),
            "toner_warning": self.toner_warning_threshold.get(),
            "drum_warning": self.drum_warning_threshold.get(),
            "waste_toner": self.waste_toner_threshold.get(),
        }
        # Exceptions par modèle : collecter les données de l'interface
        self.model_exceptions = self._normalize_model_exceptions(self._collect_model_exceptions())
        self.config["model_exceptions"] = self.model_exceptions
        # Rafraîchissement automatique
        self.autorefresh_enabled = bool(self.autorefresh_enabled_var.get())
        try:
            self.autorefresh_interval_minutes = int(self.autorefresh_interval_var.get())
        except (TypeError, ValueError):
            self.autorefresh_interval_minutes = 60
        if self.autorefresh_interval_minutes <= 0:
            self.autorefresh_interval_minutes = 60
            self.autorefresh_interval_var.set(60)
        self.config["auto_refresh"] = {
            "enabled": self.autorefresh_enabled,
            "interval_minutes": self.autorefresh_interval_minutes,
        }
        # Historique : actif / désactivé + durée de rétention
        self._history_enabled = bool(self._history_enabled_var.get())
        _hist_days_val = self._history_retention_var.get()
        try:
            _hist_days_val = max(1, int(_hist_days_val))
        except (TypeError, ValueError):
            _hist_days_val = DEFAULT_RETENTION_DAYS
            self._history_retention_var.set(DEFAULT_RETENTION_DAYS)
        self.config["history"] = {
            "enabled": self._history_enabled,
            "retention_days": _hist_days_val,
        }
        # Recréer le gestionnaire d'historique si l'état a changé
        if self._history_enabled and not self._history:
            self._history = HistoryManager(retention_days=_hist_days_val)
        elif not self._history_enabled and self._history:
            self._history = None
        elif self._history:
            self._history.retention_days = _hist_days_val

        save_json_config(self.config)
        dialog.destroy()
        self.log_status("Paramètres enregistrés.")
        # Application immédiate : le timer est réinitialisé selon les nouveaux
        # réglages (arrêté si désactivé, relancé si activé et CMDB chargée).
        self._restart_autorefresh()

    def _collect_model_exceptions(self):
        """Read model exceptions from the settings dialog UI and return a list
        of dicts with 'models', 'toner', 'drum' keys. Exceptions without any
        model are skipped."""
        result = []
        for row_data in getattr(self, "_exception_rows", []):
            models = row_data["tags_frame"].get_models()
            if not models:
                continue
            result.append({
                "models": models,
                "toner": row_data["toner_var"].get(),
                "drum": row_data["drum_var"].get(),
            })
        return result
