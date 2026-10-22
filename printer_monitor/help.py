"""Fenêtre d'aide intégrée de Printer Monitor.

Affiche une documentation utilisateur complète dans une fenêtre Toplevel
stylée avec le thème de l'application (theme.py) : mêmes couleurs, même
typographie Segoe UI, barre de défilement discrète.

La section finale « Licence » affiche le contenu INTEGRAL du fichier
LICENSE du projet, tel quel, sans aucune modification.
"""

import re
import sys
import tkinter as tk
import webbrowser
from pathlib import Path

from .theme import BG, BORDER, MUTED, PRIMARY, SURFACE, TEXT, style_toplevel
from ._version import APP_VERSION
from .window_state import setup_secondary_window


def _license_candidates():
    """Retourne les emplacements possibles du fichier LICENSE.

    Même mécanisme que les autres ressources embarquées (splash.py, logos) :
    - racine des ressources extraites par PyInstaller (``sys._MEIPASS``),
      où le spec place le fichier LICENSE spécifié dans ``datas`` ;
    - répertoire de l'exécutable (portable : LICENSE éventuellement posée
      à côté du .exe, sinon sans effet) ;
    - répertoire courant lors de l'exécution ;
    - racine du projet relativement à ce module (mode développement).
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "LICENSE")
    try:
        candidates.append(Path(sys.executable).resolve().parent / "LICENSE")
    except Exception:
        pass
    candidates.append(Path.cwd() / "LICENSE")
    try:
        candidates.append(Path(__file__).resolve().parent.parent / "LICENSE")
    except Exception:
        pass
    return candidates


def _license_text():
    """Retourne le texte intégral du fichier LICENSE s'il est disponible."""
    for candidate in _license_candidates():
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


# Une ligne d'image Markdown de type bouclier (shields.io), telle qu'écrite
# dans le fichier LICENSE. Le widget tk.Text ne sait pas rendre ces images
# distantes : ces lignes sont donc repérées pour être remplacées par des
# badges natifs (aucune dépendance réseau, aucun HTML exécuté).
_BADGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\s*\(\s*\S+\)\s*$")

# Badges affichés en tête de la fenêtre « Licence ». Ils restituent l'
# information des images shields.io sans jamais afficher la syntaxe Markdown
# brute, et sans charger de ressource distante.
LICENCE_BADGES = (
    ("Licence", "Source Available", "#d97706"),      # orange (bouclier licence)
    ("Utilisation commerciale", "autorisation requise", "#b91c1c"),  # rouge
)


def _split_licence_badges(text):
    """Sépare les lignes d'images Markdown en bouclier du reste du texte.

    Retourne ``(badges, corps)`` où ``badges`` liste les lignes ``![]()``
    détectées et ``corps`` est le texte privé de ces lignes. Le texte légal
    lui-même n'est pas modifié.
    """
    badge_lines = []
    body_lines = []
    for line in text.splitlines():
        if _BADGE_LINE_RE.match(line):
            badge_lines.append(line)
        else:
            body_lines.append(line)
    return badge_lines, "\n".join(body_lines)


# Résumé de la licence affiché lorsque le fichier LICENSE est introuvable
# ou vide (même texte que la section « Licence » de la documentation).
_LICENSE_FALLBACK_TEXT = (
    "Printer Monitor est distribué sous la Printer Monitoring "
    "Community License v1.0. Le code source est accessible et peut être "
    "utilisé, étudié et modifié pour un usage personnel ou interne en "
    "entreprise. La vente, la redistribution commerciale, l'intégration "
    "dans un produit commercial et les services SaaS payants ne sont pas "
    "autorisés sans l'accord préalable du détenteur des droits ; une "
    "licence commerciale distincte doit alors être obtenue. Consultez le "
    "fichier LICENSE du projet pour l'intégralité des conditions."
)


class HelpDialog(tk.Toplevel):
    """Fenêtre d'aide : documentation utilisateur consultable en lecture seule."""

    def __init__(self, master=None):
        super().__init__(master)
        self.title(f"Aide — Printer Monitor v{APP_VERSION}")
        style_toplevel(self)
        self.configure(background=BG)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            self, "aide", default_size=(920, 720), min_size=(680, 480))
        self.transient(master)

        # ── En-tête ──
        header = tk.Frame(self, background=BG)
        header.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(
            header,
            text="Aide de Printer Monitor",
            background=BG,
            foreground=TEXT,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text=f"Documentation utilisateur · v{APP_VERSION}",
            background=BG,
            foreground=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(10, 0), pady=(4, 0))

        # ── Carte contenant le texte ──
        card = tk.Frame(
            self, background=SURFACE,
            highlightbackground=BORDER, highlightthickness=1,
        )
        card.pack(fill="both", expand=True, padx=14, pady=(4, 8))

        self.text = tk.Text(
            card,
            wrap="word",
            background=SURFACE,
            foreground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=12,
            font=("Segoe UI", 9),
            state="disabled",
            cursor="arrow",
        )
        vscroll = tk.Scrollbar(card, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        # Styles de paragraphes
        self.text.tag_configure(
            "h1", font=("Segoe UI", 13, "bold"),
            foreground=PRIMARY, spacing1=10, spacing3=4,
        )
        self.text.tag_configure(
            "h2", font=("Segoe UI", 10, "bold"),
            foreground=TEXT, spacing1=10, spacing3=3,
        )
        self.text.tag_configure("body", font=("Segoe UI", 9), spacing3=3)
        self.text.tag_configure(
            "bullet", font=("Segoe UI", 9), lmargin1=16, spacing3=2,
        )
        self.text.tag_configure(
            "mono", font=("Consolas", 9), background="#f1f5f9",
            lmargin1=16, spacing3=2,
        )
        self.text.tag_configure(
            "muted", font=("Segoe UI", 8), foreground=MUTED, spacing1=8,
        )
        # Liens cliquables de la section « Licence » (sous forme de texte,
        # le widget tk.Text ne gère pas le HTML).
        self.text.tag_configure(
            "licence_link",
            font=("Segoe UI", 9, "underline"),
            foreground=PRIMARY,
        )
        self.text.tag_configure(
            "repo_link",
            font=("Segoe UI", 9, "underline"),
            foreground=PRIMARY,
        )
        self.text.tag_bind(
            "licence_link",
            "<Button-1>",
            lambda _e: self._ouvrir_licence(),
        )
        self.text.tag_bind(
            "repo_link",
            "<Button-1>",
            lambda _e: webbrowser.open("https://github.com/PaoloDSP/PrinterMonitor"),
        )
        for _tag in ("licence_link", "repo_link"):
            self.text.tag_bind(
                _tag, "<Enter>", lambda _e: self.text.config(cursor="hand2")
            )
            self.text.tag_bind(
                _tag, "<Leave>", lambda _e: self.text.config(cursor="")
            )
        self.text.tag_configure(
            "license", font=("Consolas", 8), background="#f8fafc",
            lmargin1=8, lmargin2=8, spacing3=1,
        )

        self._build_content()

        # Molette de la souris (Windows)
        self.text.bind(
            "<MouseWheel>",
            lambda e: self.text.yview_scroll(-1 if e.delta > 0 else 1, "units"),
        )

        # ── Pied : bouton Fermer ──
        footer = tk.Frame(self, background=BG)
        footer.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(
            footer,
            text="Fermer",
            command=self.destroy,
            background=SURFACE,
            foreground=TEXT,
            activebackground="#eef2f7",
            activeforeground=TEXT,
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=14,
            pady=3,
            cursor="hand2",
        ).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------------
    # Petites aides à l'écriture
    # ------------------------------------------------------------------
    def _title(self, txt):
        self.text.insert("end", txt + "\n", "h1")

    def _section(self, txt):
        self.text.insert("end", txt + "\n", "h2")

    def _p(self, txt):
        self.text.insert("end", txt + "\n", "body")

    def _li(self, txt):
        self.text.insert("end", "•  " + txt + "\n", "bullet")

    def _code(self, txt):
        self.text.insert("end", txt + "\n", "mono")

    def _muted(self, txt):
        self.text.insert("end", txt + "\n", "muted")

    def _ouvrir_licence(self):
        """Ouvre la fenêtre dédiée « Licence » (mécanisme déjà existant)."""
        try:
            show_license(parent=self)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Construction du contenu
    # ------------------------------------------------------------------
    def _build_content(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.__content__()
        self.text.configure(state="disabled")
        self.text.mark_set("insert", "1.0")

    def __content__(self):
        self._title("Printer Monitor — Aide")
        self._p(
            "Printer Monitor est une application de surveillance de parc "
            "d'imprimantes. Elle interroge chaque imprimante en SNMP pour "
            "afficher ses niveaux de consommables (toners/cartouches, "
            "tambours/photoconducteurs, bouteille de récupération de toner, "
            "agrafes…), son statut et ses éventuelles erreurs, puis met en "
            "évidence les machines qui demandent une intervention."
        )

        # -------------------------------------------------- démarrage
        self._section("Démarrage rapide")
        self._li("1. Cliquez sur « Charger CMDB » et sélectionnez votre fichier "
                 "d'inventaire (Excel .xlsx/.xlsm ou CSV) contenant les adresses IP.")
        self._li("2. Si besoin, choisissez un Grand site dans le filtre pour "
                 "n'afficher qu'un sous-ensemble du parc.")
        self._li("3. Cliquez sur « Rafraîchir tout » : l'application interroge "
                 "toutes les imprimantes visibles en SNMP.")
        self._li("4. Cliquez sur une ligne pour afficher le détail des "
                 "consommables dans le panneau « Détails de l'appareil ».")

        # -------------------------------------------------- CMDB
        self._section("Chargement de la CMDB et mémorisation")
        self._p(
            "Le bouton « Charger CMDB » accepte un fichier Excel (.xlsx, .xlsm, "
            ".xltx, .xltm) ou CSV (.csv, séparateurs , ; ou tabulation détectés "
            "automatiquement)."
        )
        self._li("Pour un classeur Excel : la feuille nommée « CMDB » est lue en "
                 "priorité, sinon la troisième feuille si elle existe, sinon la "
                 "première. Dans le style « CMDB », les colonnes sont lues à "
                 "position fixe : Grand site (colonne 1), adresse IP (colonne 4), "
                 "tatouage (colonne 5), modèle (colonne 7), localisation "
                 "(colonne 13).")
        self._li("Sinon, les colonnes sont reconnues par leur en-tête : IP / "
                 "Adresse, Tatouage / Tag, Marque / Brand, Modèle / Model, "
                 "Grand site / Site, Localisation / Location / Emplacement.")
        self._li("Une URL dont l'hôte est une IPv4 (ex. http://10.0.0.15/) est "
                 "acceptée comme adresse.")
        self._li("Les lignes dont l'adresse est un ÉTAT MÉTIER (RETIRE, "
                 "RETRAIT, HORS RESEAU, HORS PARC RMS, COLIMO, HORS SERVICE) "
                 "sont ignorées automatiquement : ce ne sont pas des adresses.")
        self._p(
            "Mémorisation : le chemin du dernier fichier chargé est enregistré "
            "dans la configuration. Au prochain démarrage, la CMDB est "
            "rechargée automatiquement. Si le fichier mémorisé est introuvable "
            "ou illisible, l'application vous propose d'en sélectionner un "
            "nouveau."
        )
        # -------------------------------------------------- filtre / tri
        self._section("Filtre « Grand site » et tri")
        self._p(
            "La liste déroulante « Filtrer par Grand site » limite l'affichage "
            "aux imprimantes du site choisi (« Tous » réaffiche tout). Le tri "
            "s'applique aux imprimantes visibles."
        )
        self._li("« Défaut (alertes en premier) » : hors ligne, puis machines en "
                 "erreur / alerte critique, consommable critique, consommable "
                 "bas, enfin les machines normales ; à priorité égale, tri par "
                 "site puis modèle puis IP.")
        self._li("« Niveau bas → haut » : les machines sont classées selon leur "
                 "consommable le plus bas (le plus critique d'abord) ; niveaux "
                 "inconnus en fin de liste.")
        self._li("« Niveau haut → bas » : ordre inverse exact du précédent.")

        # -------------------------------------------------- refresh
        self._section("Rafraîchissement")
        self._li("« Rafraîchir tout » : interroge toutes les imprimantes "
                 "affichées (en respectant le filtre en vigueur). Une barre de "
                 "progression, le pourcentage et le temps restant estimé "
                 "apparaissent dans la barre d'état.")
        self._li("« Rafraîchir sélection » : n'interroge que l'imprimante "
                 "sélectionnée.")
        self._li("« Arrêter » : visible uniquement pendant une passe ; demande "
                 "un arrêt coopératif, les données déjà récupérées sont "
                 "conservées.")
        self._p(
            "Rafraîchissement automatique : activable et réglable (intervalle "
            "en minutes, 60 min par défaut) dans les Paramètres. Le compte à "
            "rebours ne démarre qu'une fois la CMDB chargée, jamais au simple "
            "lancement du logiciel, et ne provoque jamais deux "
            "rafraîchissements simultanés."
        )

        # -------------------------------------------------- statuts
        self._section("Statuts et couleurs des lignes")
        self._p(
            "Chaque ligne du tableau prend la couleur de son état le plus "
            "critique (la joignabilité prime sur les consommables) :"
        )
        self._li("Gris : machine hors ligne (non joignable au réseau).")
        self._li("Rouge vif clignotant : imprimante en erreur (statut SNMP "
                 "DOWN, hrDeviceStatus=5).")
        self._li("Rouge clair : erreur SNMP/code machine, ou consommable au "
                 "seuil de remplacement (critique).")
        self._li("Orange : machine joignable mais SNMP en timeout, ou "
                 "consommable au seuil d'avertissement.")
        self._li("Bouteille de récupération de toner presque pleine ou pleine "
                 "(à son seuil d'alerte, par défaut 5 %) : rouge, traitée comme "
                 "un consommable à remplacer.")
        self._li("Agrafes quasi épuisées (sous le seuil fixe de 10 %) : rouge, "
                 "traitées comme un consommable à remplacer.")
        self._p(
            "La colonne « Messages » détaille la cause de chaque alerte. Les "
            "messages réellement critiques sont préfixés « [Critique] » ; les "
            "avertissements (SNMP timeout, seuil d'avertissement) n'ont pas de "
            "préfixe. Les alertes "
            "matérielles remontées par l'imprimante (prtAlertTable) y "
            "apparaissent également. Pour rester lisible, un état déjà "
            "affiché dans la colonne « Statut » n'est pas répété dans "
            "« Messages » : seul un détail supplémentaire (cause technique, "
            "alerte matérielle, code constructeur) y figure."
        )
        self._p(
            "Statuts possibles dans la colonne « Statut » : other, unknown, "
            "idle (inactive), printing (imprime), warmup (chauffe) — issus de "
            "prtGeneralStatus, ou running/warning/testing/down via "
            "hrDeviceStatus quand le constructeur ne fournit pas "
            "prtGeneralStatus."
        )
        # -------------------------------------------------- seuils
        self._section("Seuils et alertes")
        self._p(
            "Deux niveaux d'alerte par catégorie (réglables dans les "
            "Paramètres) :"
        )
        self._li("Seuil de remplacement (critique, rouge) : toner/cartouche 4 %, "
                 "tambour/photoconducteur 4 % par défaut.")
        self._li("Seuil d'avertissement (orange) : toner/cartouche 10 %, "
                 "tambour/photoconducteur 15 % par défaut.")
        self._li("Bouteille de récupération de toner : alerte à partir de 5 % (« presque "
                 "pleine ») traitée comme critique/rouge (à remplacer), jusqu'à "
                 "pleine ;"
                 )
        self._li("Agrafes : alerte fixe en dessous de 10 %, traitée comme "
                 "critique/rouge (à remplacer).")
        self._p(
            "Seuls les toners/cartouches et tambours/photoconducteurs sont "
            "évalués avec ces seuils ; les autres consommables (kits, "
            "développeur, fours…) ont leur propre sémantique."
        )

        # -------------------------------------------------- exceptions
        self._section("Exceptions par modèle")
        self._p(
            "Certains modèles nécessitent des seuils de remplacement différents "
            "de la valeur globale. Dans les Paramètres, la section « Exceptions "
            "par modèle » permet de créer des règles : une liste de modèles "
            "(ajoutés comme des étiquettes) et les seuils toner/tambour "
            "correspondants."
        )
        self._li("La correspondance se fait par mot entier, insensible à la "
                 "casse, sur le modèle SNMP ou le modèle de la CMDB : "
                 "l'exception « X921 » s'applique à « Lexmark X921 » mais pas "
                 "à « X9210 ».")
        self._li("Priorité : exception correspondant au modèle, sinon seuil "
                 "global.")
        self._li("Des exceptions d'exemple existent par défaut (X921/X922/CS923, "
                 "CS943) ; elles sont modifiables ou supprimables.")

        # -------------------------------------------------- détails
        self._section("Panneau « Détails de l'appareil »")
        self._p(
            "Un clic sur une ligne affiche les barres de niveau de tous les "
            "consommables de la machine, groupées par catégorie (toners/"
            "cartouches par couleur ⬛🟦🟥🟨, tambours/photoconducteurs, "
            "bouteille de récupération, agrafes, kits…) avec leur description "
            "SNMP et leur pourcentage. La zone défile verticalement si la "
            "machine possède beaucoup de consommables."
        )
        self._p(
            "Astuce : Ctrl+clic sur l'adresse IP ouvre l'interface Web de "
            "l'imprimante dans votre navigateur (HTTPS utilisé si l'appareil "
            "le propose, HTTP sinon)."
        )
        # -------------------------------------------------- tableau astuces
        self._section("Actions sur le tableau")
        self._li("Ctrl+C : copie la cellule cliquée, ou la ligne entière "
                 "s'il n'y a pas de cellule mémorisée (nom de l'appareil et "
                 "description SNMP inclus pour une ligne).")
        self._li("Double-clic entre deux colonnes : ajuste automatiquement la "
                 "largeur de la colonne de gauche (comme dans Excel). Un "
                 "glissement manuel d'un séparateur est mémorisé et respecté.")
        self._li("« Exporter CSV » : enregistre les lignes affichées (filtre et "
                 "tri en vigueur) dans un fichier CSV UTF-8 avec BOM, "
                 "séparateur point-virgule — directement ouvrable dans Excel.")
        self._li("Les colonnes Statut, Dernière maj et Messages s'adaptent à la "
                 "largeur de la fenêtre ; une barre de défilement horizontale "
                 "apparaît si nécessaire.")

        # -------------------------------------------------- historique
        self._section("Historique")
        self._p(
            "Le bouton « Historique » ouvre la fenêtre de l'historique local "
            "(base SQLite printer_monitor_history.db). Après chaque "
            "rafraîchissement réussi, un instantané (statut + niveaux de "
            "consommables par couleur) est enregistré automatiquement — "
            "aucune interrogation SNMP supplémentaire n'est effectuée depuis "
            "cette fenêtre."
        )
        self._li("Recherche en direct d'une imprimante (IP, nom, modèle, site).")
        self._li("Liste des mesures disponibles ; Ctrl+clic pour en "
                 "sélectionner deux et les comparer (évolution des niveaux).")
        self._li("Timeline graphique de l'évolution des consommables.")
        self._li("« Nettoyer l'historique » supprime tout, après confirmation "
                 "(irréversible).")
        self._li("Rétention : les mesures de plus de N jours sont purgées "
                 "automatiquement (14 jours par défaut, réglable dans les "
                 "Paramètres ; l'historique peut aussi être désactivé).")

        # -------------------------------------------------- paramètres
        self._section("Paramètres")
        self._li("Communauté et port SNMP (public / 161 par défaut).")
        self._li("Seuils de remplacement et d'avertissement toner/tambour, "
                 "seuil bouteille de récupération.")
        self._li("Exceptions par modèle (voir la section dédiée).")
        self._li("Rafraîchissement automatique : activation et intervalle.")
        self._li("Historique : activation et durée de rétention en jours.")
        self._p(
            "Réglages avancés (fichier printer_monitor_config.json, section "
            "« snmp ») : timeouts/retries SNMP, sondage rapide de "
            "joignabilité, nombre de rangées GETBULK, walks parallèles… Ces "
            "valeurs accélèrent la collecte sans changer les résultats "
            "affichés ; un redémarrage est nécessaire pour les prendre en "
            "compte."
        )
        # -------------------------------------------------- mode avancé
        self._section("Mode avancé et Rapprochement de parc")
        self._p(
            "Le bouton « Mode avancé » déverrouille (après mot de passe "
            "administrateur) les outils développeur/debug, masqués par défaut :"
        )
        self._li("Diagnostic SNMP : effectue un WALK complet de "
                 "1.3.6.1.2.1.43.11 puis 1.3.6.1.4.1.641 sur l'imprimante "
                 "sélectionnée et écrit TOUS les OID (type + valeur brute, "
                 "sans filtrage) dans un fichier texte.")
        self._li("OID bruts : visualise à l'écran les OID bruts d'une "
                 "imprimante sélectionnée.")
        self._li("Logs détaillés : affiche la fin du fichier "
                 "printer_monitor.log.")
        self._li("Rapprochement de parc : compare la CMDB interne avec "
                 "l'inventaire client mensuel (Presta RMS) — voir ci-dessous.")
        self._li("Fonctions de test : ping de l'appareil sélectionné ou de "
                 "toute la liste affichée, export CSV des résultats de ping, "
                 "test SNMP GET (sysName).")
        self._li("Verrouiller : masque à nouveau ces outils.")
        self._p(
            "Rapprochement de parc : sélectionnez le fichier client (feuille "
            "« Parc RMS selon contrat », table « Sites RMS ») puis lancez "
            "l'analyse. Chaque ligne client est identifiée (tatouage, puis n° "
            "de série, puis MAC), TRADUITE vers la nomenclature interne "
            "(sites, bâtiments, étages, bureaux, modèles — grâce à "
            "l'apprentissage et aux correspondances que vous validez, "
            "mémorisées d'un mois sur l'autre dans "
            "printer_monitor_reconcile_map.json), puis comparée champ par "
            "champ à la CMDB. Les résultats sont classés en onglets Ajouts / "
            "Modifications / Retraits / Anomalies / Sans changement. Rien "
            "n'est jamais écrit sans votre validation explicite : "
            "l'application crée une sauvegarde .bak avant toute modification "
            "et tient un journal horodaté des synchronisations."
        )

        # -------------------------------------------------- licence
        self._section("Licence")
        self._p(
            "Les conditions complètes de la licence (fichier LICENSE du "
            "projet) sont consultables dans la rubrique « Licence » de "
            "l'application, accessible depuis la barre d'outils de la fenêtre "
            "principale."
        )
        self.text.insert("end", "\n", "body")
        self.text.insert("end", "Accès direct : ", "body")
        self.text.insert(
            "end", "ouvrir la fenêtre « Licence »", "licence_link"
        )
        self.text.insert("end", ".\n", "body")
        self.text.insert("end", "Code source du projet : ", "body")
        self.text.insert(
            "end", "https://github.com/PaoloDSP/PrinterMonitor", "repo_link"
        )
        self.text.insert("end", "\n", "body")
        self._muted("Printer Monitor — développé avec ❤️ par Paolo DOS SANTOS "
                    "PINTO (www.pdsp.fr)")



def show_help(parent=None):
    """Ouvre la fenêtre d'aide (non modale)."""
    dlg = HelpDialog(master=parent)
    try:
        dlg.grab_set()
    except Exception:
        pass
    return dlg


class LicenseDialog(tk.Toplevel):
    """Fenêtre « Licence » : affiche le contenu intégral du fichier LICENSE
    du projet, tel quel, stylée avec le thème de l'application."""

    def __init__(self, master=None):
        super().__init__(master)
        self.title("Licence — Printer Monitor")
        style_toplevel(self)
        self.configure(background=BG)
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        setup_secondary_window(
            self, "licence", default_size=(820, 640), min_size=(560, 380))
        self.transient(master)

        # ── En-tête ──
        header = tk.Frame(self, background=BG)
        header.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(
            header,
            text="Licence de Printer Monitor",
            background=BG,
            foreground=TEXT,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        # ── Barre de badges de licence (rendu visuel natif ; aucune image distante) ──
        lic = _license_text()
        _badges, _ = _split_licence_badges(lic) if lic else ([], None)
        if _badges:
            badge_bar = tk.Frame(self, background=BG)
            badge_bar.pack(fill="x", padx=14, pady=(0, 6))
            for _label, _value, _color in LICENCE_BADGES:
                _badge = tk.Frame(badge_bar, background=BG)
                tk.Label(
                    _badge,
                    text=_label,
                    background="#64748a",
                    foreground="white",
                    font=("Segoe UI", 8, "bold"),
                    padx=7,
                    pady=3,
                ).pack(side="left")
                tk.Label(
                    _badge,
                    text=_value,
                    background=_color,
                    foreground="white",
                    font=("Segoe UI", 8, "bold"),
                    padx=9,
                    pady=3,
                ).pack(side="left")
                _badge.pack(side="left", padx=(0, 10))

        # ── Carte contenant le texte ──
        card = tk.Frame(
            self, background=SURFACE,
            highlightbackground=BORDER, highlightthickness=1,
        )
        card.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        self.text = tk.Text(
            card,
            wrap="word",
            background=SURFACE,
            foreground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=12,
            font=("Segoe UI", 9),
            state="disabled",
            cursor="arrow",
        )
        vscroll = tk.Scrollbar(card, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure(
            "h1", font=("Segoe UI", 13, "bold"),
            foreground=PRIMARY, spacing1=10, spacing3=4,
        )
        self.text.tag_configure(
            "body", font=("Segoe UI", 9), spacing3=4,
        )

        self.text.configure(state="normal")
        if lic:
            _, corps = _split_licence_badges(lic)
            self.text.insert("end", "Printer Monitor — Licence\n", "h1")
            self.text.insert("end", (corps or "").strip() + "\n", "body")
        else:
            self.text.insert("end", "Licence introuvable\n", "h1")
            self.text.insert("end",
                "Le fichier LICENSE n'a pas été trouvé à côté de "
                "l'application.\n\n" + _LICENSE_FALLBACK_TEXT,
                "body",
            )
        self.text.configure(state="disabled")


def show_license(parent=None):
    """Ouvre la fenêtre « Licence » (non modale)."""
    dlg = LicenseDialog(master=parent)
    try:
        dlg.grab_set()
    except Exception:
        pass
    return dlg
