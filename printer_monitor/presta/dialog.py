"""Interface « Rapprochement de parc » (mode avancé).

Permet à l'utilisateur de :

  1. sélectionner le fichier client mensuel (« Presta RMS.xlsm ») ;
  2. lancer l'analyse : import + traduction + rapprochement avec la CMDB ;
  3. parcourir les onglets Ajouts / Modifications / Retraits / Anomalies /
     Sans changement, avec filtres (site, grandsite, type, catégorie,
     type de différence, recherche) ;
  4. voir, pour chaque machine, LA TRADUCTION détaillée
     (données client -> valeurs CMDB interprétées) ;
  5. tout sélectionner dans un onglet et appliquer en masse ;
  6. résoudre UNE SEULE FOIS chaque correspondance inconnue groupée.

L'analyse ne modifie JAMAIS la CMDB sans validation explicite.
"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import conventions
from .cmdb_writer import CMDBWriteError, apply_modifications
from .diagnostic import format_report, load_cmdb_rows, run_diagnostic
from .mapping_store import MappingStore
from .sync_history import SyncHistory

try:
    from ..window_state import setup_secondary_window
except Exception:  # pragma: no cover - usage autonome du module
    setup_secondary_window = None

KIND_EMOJI = {
    "additions": "🟢",
    "modifications": "🟠",
    "removals": "🔴",
    "anomalies": "🟣",
    "unchanged": "⚪",
}

TAB_DEFS = (
    ("modifications", "Modifications"),
    ("additions", "Ajouts"),
    ("removals", "Retraits"),
    ("anomalies", "Anomalies"),
    ("unchanged", "Sans changement"),
)


class RapprochementDialog(tk.Toplevel):
    """Fenêtre principale du rapprochement de parc."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Rapprochement de parc")
        try:
            from ..theme import style_toplevel
            style_toplevel(self)
        except Exception:
            pass
        # Position/taille mémorisées + adaptation aux écrans disponibles.
        if setup_secondary_window is not None:
            setup_secondary_window(
                self, "rapprochement", default_size=(1240, 780),
                min_size=(980, 600))
        else:  # repli : comportement d'origine
            self.geometry("1240x780")
            self.minsize(980, 600)

        self.client_path = tk.StringVar(value="")
        self.result = None
        self.engine = None
        self.site_mapping = None  # compat
        self.mappings = MappingStore()
        self.sync_history = SyncHistory()
        self.current_tab = "modifications"
        self._row_items = {}

        self._build_header()
        self._build_toolbar()
        self._build_filters()
        self._build_tree()
        self._build_statusbar()

    # ------------------------------------------------------------------ #
    # Construction de l'interface
    # ------------------------------------------------------------------ #
    def _build_header(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Inventaire :").pack(side="left")
        ttk.Entry(top, textvariable=self.client_path).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(top, text="Parcourir…",
                   command=self.choose_file).pack(side="left", padx=2)
        ttk.Button(top, text="Analyser",
                   command=self.run_import).pack(side="left", padx=6)

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=2)
        self.tab_buttons = {}
        for key, label in TAB_DEFS:
            btn = ttk.Button(bar, text=f"{label} (0)",
                             command=lambda k=key: self.show_tab(k))
            btn.pack(side="left", padx=3)
            self.tab_buttons[key] = btn
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.btn_select_all = ttk.Button(
            bar, text="Tout sélectionner", command=self.select_all,
            state="disabled")
        self.btn_select_all.pack(side="left", padx=3)
        self.btn_apply = ttk.Button(
            bar, text="Appliquer la sélection",
            command=self.apply_selected, state="disabled")
        self.btn_apply.pack(side="left", padx=3)
        self.btn_report = ttk.Button(
            bar, text="Rapport", command=self.show_report)
        self.btn_report.pack(side="right", padx=3)
    def _build_filters(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=2)
        self.filters = {}
        self.filter_boxes = {}
        for name in ("Site", "Grandsite", "Type", "Catégorie",
                     "Différence"):
            ttk.Label(bar, text=f"{name} :").pack(side="left", padx=(6, 1))
            var = tk.StringVar(value="")
            box = ttk.Combobox(bar, textvariable=var, width=18,
                               state="readonly",
                               postcommand=lambda n=name: self._fill_filter(n))
            box.pack(side="left")
            box.bind("<<ComboboxSelected>>",
                     lambda _e: self.populate_tree())
            self.filters[name] = var
            self.filter_boxes[name] = box
        ttk.Label(bar, text="Recherche :").pack(side="left", padx=(10, 1))
        self.search_var = tk.StringVar(value="")
        entry = ttk.Entry(bar, textvariable=self.search_var, width=22)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", lambda _e: self.populate_tree())
        ttk.Button(bar, text="Effacer les filtres",
                   command=self.reset_filters).pack(side="left", padx=8)

    def _build_tree(self):
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        columns = ("cat", "tag", "modele", "loc", "diff")
        self.tree = ttk.Treeview(frame, columns=columns, selectmode="none")
        headings = (
            ("#0", "✓", 42, "center"),
            ("cat", "Catégorie", 90, "w"),
            ("tag", "Étiquette", 105, "w"),
            ("modele", "Modèle CMDB", 130, "w"),
            ("loc", "Localisation CMDB", 260, "w"),
            ("diff", "Différence", 430, "w"),
        )
        for col, text, width, anchor in headings:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # Cases à cocher par clic sur la colonne #0
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)

    def _build_statusbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(2, 8))
        self.status_var = tk.StringVar(
            value="Sélectionnez le fichier inventaire puis « Analyser ».")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        ttk.Button(bar, text="Fermer",
                   command=self.destroy).pack(side="right")

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Sélectionner le fichier client mensuel",
            filetypes=[
                ("Excel", "*.xlsm *.xlsx"),
                ("Tous les fichiers", "*.*"),
            ],
            parent=self,
        )
        if path:
            self.client_path.set(path)

    def run_import(self):
        path = self.client_path.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning(
                "Rapprochement de parc",
                "Sélectionnez d'abord le fichier inventaire client.",
                parent=self,
            )
            return
        cmdb_path = self.app.config.get("cmdb_path")
        if not cmdb_path or not os.path.isfile(cmdb_path):
            messagebox.showerror(
                "CMDB requise",
                "Aucune CMDB chargée. Chargez d'abord votre fichier CMDB.",
                parent=self,
            )
            return
        self.status_var.set("Analyse en cours… (traduction + rapprochement)")
        self.update_idletasks()
        try:
            result, engine = run_diagnostic(
                path, cmdb_path, mappings=self.mappings
            )
        except Exception as exc:
            messagebox.showerror(
                "Rapprochement de parc",
                f"Erreur pendant l'analyse : {exc}", parent=self,
            )
            self.status_var.set("Analyse interrompue.")
            return
        self.result = result
        self.engine = engine
        self.status_var.set(
            f"{result.diagnostics.get('equipements_analyses', 0)} équipements"
            f" analysés — {len(result.modifications)} modification(s), "
            f"{len(result.additions)} ajout(s), "
            f"{len(result.removals)} retrait(s), "
            f"{len(result.anomalies)} groupe(s) d'anomalies."
        )
        for key, _label in TAB_DEFS:
            self._update_tab_button(key)
        self.btn_select_all.configure(state="normal")
        self.btn_apply.configure(state="normal")
        self.show_tab("modifications")
    # ------------------------------------------------------------------ #
    # Affichage des lignes
    # ------------------------------------------------------------------ #
    def _update_tab_button(self, key):
        sizes = {
            "additions": lambda: len(self.result.additions),
            "modifications": lambda: len(self.result.modifications),
            "removals": lambda: len(self.result.removals),
            "anomalies": lambda: len(self.result.anomalies),
            "unchanged": lambda: len(self.result.unchanged),
        }
        if self.result is None:
            return
        label = dict(TAB_DEFS)[key]
        self.tab_buttons[key].configure(
            text=f"{KIND_EMOJI.get(key, '')} {label} ({sizes[key]()})"
        )

    def _fill_filter(self, name):
        if self.engine is None:
            values = []
        elif name == "Site":
            values = sorted({r.site for r in self.engine.cmdb_rows if r.site})
        elif name == "Grandsite":
            values = sorted({
                r.grandsite for r in self.engine.cmdb_rows if r.grandsite
            })
        elif name == "Type":
            values = ["MFP", "IMP"]
        elif name == "Catégorie":
            values = ["COULEUR", "MONOCHROME"]
        else:
            values = ["Ajout", "Modification", "Retrait", "Anomalie",
                      "Sans changement"]
        self.filter_boxes[name].configure(values=values)

    def reset_filters(self):
        """Efface tous les filtres d'un seul coup et actualise la liste.

        Remet chaque liste déroulante (Site, Grandsite, Type, Catégorie,
        Différence… et tout filtre ajouté ultérieurement à self.filters /
        self.filter_boxes) à sa valeur par défaut (vide = « tous »), efface
        la recherche, puis rafraîchit immédiatement les résultats : aucun
        filtre ne reste actif en arrière-plan.
        """
        for name, var in self.filters.items():
            var.set("")
            box = self.filter_boxes.get(name)
            if box is not None:
                try:
                    box.selection_clear()
                except Exception:
                    pass
        self.search_var.set("")
        self.populate_tree()

    def show_tab(self, key):
        self.current_tab = key
        for k, btn in self.tab_buttons.items():
            btn.state(["pressed"] if k == key else ["!pressed"])
        self.populate_tree()

    def _iter_tab_objects(self):
        """Renvoie [(kind, obj)] pour l'onglet courant."""
        r = self.result
        if r is None:
            return []
        if self.current_tab == "additions":
            return [("ajout", a) for a in r.additions]
        if self.current_tab == "modifications":
            return [("modification", m) for m in r.modifications]
        if self.current_tab == "removals":
            return [("retrait", row) for row in r.removals]
        if self.current_tab == "anomalies":
            return [("anomalie", a) for a in r.anomalies]
        return []

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._row_items.clear()
        rows = self._filtered_rows()
        for item in rows:
            iid = self.tree.insert("", "end", values=item["values"])
            self._row_items[iid] = item
            if item["checked"]:
                self.tree.item(iid, text="☑")
            else:
                self.tree.item(iid, text="☐")
        self.status_var.set(f"{len(rows)} ligne(s) affichée(s).")
    # ------------------------------------------------------------------ #
    # Lignes filtrées
    # ------------------------------------------------------------------ #
    def _row_matches(self, ctx):
        """ctx : dict {grandsite, site, type, categorie, tag, modele…}."""
        f = self.filters
        if f["Site"].get() and f["Site"].get() != ctx.get("site", ""):
            return False
        if f["Grandsite"].get() and \
                f["Grandsite"].get() != ctx.get("grandsite", ""):
            return False
        if f["Type"].get() and f["Type"].get() != ctx.get("type", ""):
            return False
        if f["Catégorie"].get() and \
                f["Catégorie"].get() != ctx.get("categorie", ""):
            return False
        needle = self.search_var.get().strip().casefold()
        if needle:
            haystack = " ".join(
                str(v) for v in ctx.values() if v
            ).casefold()
            if needle not in haystack:
                return False
        return True

    def _filtered_rows(self):
        rows = []
        diff_filter = self.filters["Différence"].get()
        if self.result is None:
            return rows
        tab = self.current_tab

        if tab == "modifications":
            for modif in self.result.modifications:
                if diff_filter and diff_filter != "Modification":
                    continue
                row = modif.cmdb_row
                t = modif.translated
                loc = "/".join(
                    v for v in (t.get("grandsite"), t.get("site"),
                                t.get("batiment"), t.get("etage"),
                                t.get("bureau")) if v
                ) or row.concatener
                badge = "" if modif.reliable else "  (à vérifier)"
                ctx = {
                    "grandsite": row.grandsite, "site": row.site,
                    "type": t.get("type"), "categorie": t.get("categorie"),
                    "tag": row.tag, "modele": row.modele,
                    "diff": "; ".join(c.field for c in modif.field_changes),
                }
                if not self._row_matches(ctx):
                    continue
                rows.append({
                    "kind": "modification",
                    "obj": modif,
                    "checked": False,
                    "values": (
                        t.get("categorie") or row.categorie,
                        row.tag,
                        f"{row.marque} {row.modele}".strip(),
                        loc,
                        modif.describe() + badge,
                    ),
                })
            return rows

        if tab == "additions":
            for addition in self.result.additions:
                if diff_filter and diff_filter != "Ajout":
                    continue
                t = addition.translated
                values = addition.values_for_cmdb()
                if not self._row_matches({
                    "grandsite": values.get("Grandsite", ""),
                    "site": values.get("Site", ""),
                    "type": values.get("Type", ""),
                    "categorie": values.get("Categorie", ""),
                    "tag": t.get("numeroetiquette"),
                    "modele": values.get("Modele", ""),
                }):
                    continue
                loc = "/".join(
                    v for v in (values.get("Grandsite", ""),
                                values.get("Site", ""),
                                values.get("Bâtiment", ""),
                                values.get("Etage", ""),
                                values.get("Bureau", "")) if v
                )
                badge = "" if addition.reliable else "  (site à confirmer)"
                rows.append({
                    "kind": "ajout",
                    "obj": addition,
                    "checked": False,
                    "values": (
                        values.get("Categorie", ""),
                        t.get("numeroetiquette"),
                        " ".join(v for v in (values.get("Marque", ""),
                                             values.get("Modele", "")) if v),
                        loc,
                        "AJOUT" + badge,
                    ),
                })
            return rows

        if tab == "removals":
            for row in self.result.removals:
                if diff_filter and diff_filter != "Retrait":
                    continue
                if not self._row_matches({
                    "grandsite": row.grandsite, "site": row.site,
                    "type": row.type, "categorie": row.categorie,
                    "tag": row.tag, "modele": row.modele,
                }):
                    continue
                rows.append({
                    "kind": "retrait",
                    "obj": row,
                    "checked": False,
                    "values": (
                        row.categorie, row.tag,
                        f"{row.marque} {row.modele}".strip(),
                        row.concatener,
                        "RETRAIT POTENTIEL (absent du fichier client)",
                    ),
                })
            return rows

        if tab == "anomalies":
            for anomaly in self.result.anomalies:
                if diff_filter and diff_filter != "Anomalie":
                    continue
                rows.append({
                    "kind": "anomalie",
                    "obj": anomaly,
                    "checked": False,
                    "values": (
                        "",
                        f"{anomaly.count} éq.",
                        anomaly.field or "",
                        anomaly.label[:55],
                        anomaly.message() +
                        ((" -> " + " | ".join(anomaly.candidates[:3]))
                         if anomaly.candidates else ""),
                    ),
                })
            return rows

        if tab == "unchanged":
            for row in self.result.unchanged:
                if not self._row_matches({
                    "grandsite": row.grandsite, "site": row.site,
                    "type": row.type, "categorie": row.categorie,
                    "tag": row.tag, "modele": row.modele,
                }):
                    continue
                rows.append({
                    "kind": "unchanged",
                    "obj": row,
                    "checked": False,
                    "values": (
                        row.categorie, row.tag,
                        f"{row.marque} {row.modele}".strip(),
                        row.concatener, "—",
                    ),
                })
        return rows

    # ------------------------------------------------------------------ #
    # Interaction tableau
    # ------------------------------------------------------------------ #
    def _on_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "tree" and self.tree.identify_column(event.x) != "#0":
            return
        iid = self.tree.identify_row(event.y)
        item = self._row_items.get(iid)
        if item is None:
            return
        if item["kind"] == "anomalie":
            self.resolve_anomaly(item["obj"])
            return
        item["checked"] = not item["checked"]
        self.tree.item(iid, text="☑" if item["checked"] else "☐")

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        item = self._row_items.get(iid)
        if item is None:
            return
        self.show_translation(item)

    def select_all(self):
        for iid, item in self._row_items.items():
            if item["kind"] in ("modification", "ajout", "retrait"):
                item["checked"] = True
                self.tree.item(iid, text="☑")
        n = sum(
            1 for it in self._row_items.values() if it["checked"]
        )
        self.status_var.set(f"{n} ligne(s) sélectionnée(s).")
    # ------------------------------------------------------------------ #
    # Application en masse
    # ------------------------------------------------------------------ #
    def apply_selected(self):
        if self.result is None:
            return
        additions = [
            it["obj"] for it in self._row_items.values()
            if it["kind"] == "ajout" and it["checked"]
        ]
        modifs = []
        for it in self._row_items.values():
            if it["kind"] != "modification" or not it["checked"]:
                continue
            modif = it["obj"]
            modif.applied = True
            for change in modif.field_changes:
                change.applied = True
            modifs.append(modif)
        deletions = [
            it["obj"] for it in self._row_items.values()
            if it["kind"] == "retrait" and it["checked"]
        ]
        if not (additions or modifs or deletions):
            messagebox.showinfo(
                "Rapprochement de parc",
                "Cochez d'abord les lignes à appliquer "
                "(ou utilisez « Tout sélectionner »).",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Confirmation",
            f"Appliquer {len(additions)} ajout(s), {len(modifs)} "
            f"modification(s) et {len(deletions)} retrait(s) ?\n\n"
            "Une sauvegarde .bak de la CMDB sera créée.",
            parent=self,
        ):
            return
        cmdb_path = self.app.config.get("cmdb_path")
        try:
            report = apply_modifications(
                cmdb_path, additions, modifs, deletions
            )
        except CMDBWriteError as exc:
            messagebox.showerror("Application CMDB", str(exc), parent=self)
            return
        try:
            self.sync_history.record_sync(
                self.client_path.get(), self.result,
                additions, modifs, deletions, report,
            )
        except Exception:
            pass
        for obj in additions + modifs + deletions:
            obj.applied = False
        messagebox.showinfo(
            "Application terminée",
            f"{report['added']} ajout(s) • "
            f"{report['modified_cells']} cellule(s) • "
            f"{report['deleted']} retrait(s).",
            parent=self,
        )
        self.run_import()

    # ------------------------------------------------------------------ #
    # Résolution groupée d'une anomalie
    # ------------------------------------------------------------------ #
    def resolve_anomaly(self, anomaly):
        from .learning import client_site_key
        from .normalize import norm_key

        # Un état métier n'est PAS une correspondance à mémoriser : la valeur
        # « RETIRE », « HORS RESEAU » ou « COLIMO » décrit une situation, et le
        # moteur n'a aucune écriture à proposer (§3, §4). On l'explique au lieu
        # de demander une valeur qui serait ignorée.
        if anomaly.kind in (conventions.SPECIAL_STATE,
                            conventions.REAPPEARANCE):
            messagebox.showinfo(
                "État métier dans la CMDB",
                f"{anomaly.message()}\n\n"
                f"{anomaly.count} équipement(s) concerné(s).\n\n"
                "Aucune écriture n'est proposée : cette valeur de la colonne "
                "AdresseIP décrit une situation métier et non une donnée.\n\n"
                "Corrigez la CMDB (ou la fiche du prestataire), puis relancez "
                "l'analyse.",
                parent=self,
            )
            return

        cands = "\n".join(f"  • {c}" for c in anomaly.candidates) or \
            "  (aucune valeur connue dans la CMDB)"
        choice = simpledialog.askstring(
            "Correspondance à mémoriser",
            f"{anomaly.message()}\n\n"
            f"{anomaly.count} équipement(s) concerné(s).\n\n"
            f"Valeurs possibles observées :\n{cands}\n\n"
            "Saisissez la valeur CMDB à utiliser pour TOUS ces équipements "
            "(vide pour ignorer) :",
            parent=self,
        )
        if not choice:
            return
        choice = choice.strip()

        if anomaly.field in ("Bâtiment", "Etage", "Bureau"):
            attr = {
                "Bâtiment": "batiment",
                "Etage": "etage",
                "Bureau": "bureau",
            }[anomaly.field]
            self.mappings.add_location(attr, anomaly.label, choice)
        elif anomaly.kind == "site_inconnu":
            parts = [p.strip() for p in choice.split("/") if p.strip()]
            grand = parts[0] if parts else choice
            site = parts[1] if len(parts) > 1 else grand
            keys = set()
            for dev, _row in anomaly.items:
                if dev is None:
                    continue
                key = (
                    f"{norm_key(dev.grandsite_presta or '')}"
                    f"|{norm_key(dev.site_presta or '')}"
                )
                keys.add(key)
            for key in keys:
                self.mappings.add_site(key, grand, site)
        elif anomaly.kind == "modele_inconnu":
            parts = choice.split(None, 1)
            marque = (parts[0].upper() if parts else "")[:12]
            modele = parts[1] if len(parts) > 1 else ""
            models = {dev.modele for dev, _row in anomaly.items
                      if dev is not None and dev.modele}
            for modele_client in models:
                self.mappings.add_model(modele_client, marque, modele)
        self.run_import()
    # ------------------------------------------------------------------ #
    # Mode « Voir la traduction »
    # ------------------------------------------------------------------ #
    CMDB_ATTR = {
        "Numeroetiquette": "tag",
        "Grandsite": "grandsite",
        "Site": "site",
        "Bâtiment": "batiment",
        "Etage": "etage",
        "Bureau": "bureau",
        "CONCATENER (calculé)": "concatener",
        "Marque": "marque",
        "Modele": "modele",
        "Categorie": "categorie",
        "Type": "type",
        "Numserie": "numeroserie",
        "AdresseIP": "ip",
        "AdresseMAC": "mac",
    }

    def show_translation(self, item):
        kind, obj = item["kind"], item["obj"]
        if kind == "modification":
            translated, row = obj.translated, obj.cmdb_row
        elif kind == "ajout":
            translated, row = obj.translated, None
        else:
            return
        win = tk.Toplevel(self)
        win.title("Voir la traduction")
        if setup_secondary_window is not None:
            setup_secondary_window(
                win, "traduction_rapprochement", default_size=(880, 640),
                min_size=(560, 360))
        else:
            win.geometry("880x640")
        tag = translated.get("numeroetiquette") or "?"
        ttk.Label(win, text=f"Équipement {tag}",
                  font=("TkDefaultFont", 12, "bold")).pack(
                      anchor="w", padx=10, pady=(10, 2))
        text = tk.Text(win, wrap="word", width=110)
        text.pack(fill="both", expand=True, padx=10, pady=8)
        text.tag_configure("h", font=("TkDefaultFont", 11, "bold"))

        def section(title):
            text.insert("end", f"\n{title}\n", ("h",))
            text.insert("end", "-" * 78 + "\n")

        client_labels = {
            "numeroetiquette": "Tatouage",
            "numserie": "Numéro de série",
            "adresse_ip": "Adresse IP",
            "adresse_mac": "Adresse MAC",
            "modele": "Modèle",
            "categorie": "Sous-type de modèle",
            "type": "Type (colonne client)",
            "batiment": "Emplacement (segment bâtiment)",
            "etage": "Emplacement (segment étage)",
            "bureau": "Emplacement (segment bureau)",
            "grandsite": "Grand site / Site (client)",
            "site": "Grand site / Site (client)",
            "marque": "Modèle",
        }
        labels = (
            ("Numeroetiquette", "numeroetiquette"),
            ("Grandsite", "grandsite"),
            ("Site", "site"),
            ("Bâtiment", "batiment"),
            ("Etage", "etage"),
            ("Bureau", "bureau"),
            ("CONCATENER (calculé)", "concatener"),
            ("Marque", "marque"),
            ("Modele", "modele"),
            ("Categorie", "categorie"),
            ("Type", "type"),
            ("Numserie", "numserie"),
            ("AdresseIP", "adresse_ip"),
            ("AdresseMAC", "adresse_mac"),
        )
        section("1. DONNÉES CLIENT (source brute)")
        for _col, attr in labels:
            fv = translated.field(attr)
            raw = fv.client_raw if fv is not None else ""
            if raw:
                text.insert(
                    "end",
                    f"  {client_labels.get(attr, attr):<32}: {raw}\n",
                )

        section("2. VALEURS CMDB INTERPRÉTÉES (après traduction)")
        for col, attr in labels:
            fv = translated.field(attr)
            if fv is None or fv.is_empty:
                continue
            text.insert(
                "end",
                f"  {col:<22}: {fv.value or '(inconnu)'}   [{fv.label}]\n",
            )
            if fv.candidates:
                text.insert(
                    "end",
                    f"      candidats : {' | '.join(fv.candidates[:3])}\n",
                )

        section("3. VALEURS ACTUELLES DE LA CMDB")
        if row is None:
            text.insert("end", "  (équipement absent de la CMDB — AJOUT)\n")
        else:
            for col, _attr in labels:
                attr_row = self.CMDB_ATTR.get(col, "")
                current = getattr(row, attr_row, "") if attr_row else ""
                text.insert(
                    "end", f"  {col:<22}: {current or '(vide)'}\n")

        section("4. CHANGEMENTS PROPOSÉS")
        if kind == "modification":
            for change in obj.field_changes:
                fiabilite = "FIABLE" if change.reliable else "à vérifier"
                text.insert(
                    "end",
                    f"  {change.field:<22}: "
                    f"{change.cmdb_value or '(vide)'} → {change.new_value}"
                    f"   [{fiabilite}]\n",
                )
        elif kind == "ajout":
            text.insert(
                "end",
                "  AJOUT : nouvelle ligne avec les valeurs traduites.\n",
            )
        else:
            text.insert("end", "  Aucun changement.\n")
        text.configure(state="disabled")

    def show_report(self):
        if self.result is None:
            messagebox.showinfo(
                "Rapport", "Lancez d'abord l'analyse.", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("Rapport de diagnostic")
        if setup_secondary_window is not None:
            setup_secondary_window(
                win, "rapport_diagnostic", default_size=(760, 560),
                min_size=(480, 320))
        else:
            win.geometry("760x560")
        text = tk.Text(win, wrap="none")
        text.pack(fill="both", expand=True)
        text.insert("1.0", format_report(self.result))
        text.configure(state="disabled")


# Alias pour compatibilité avec l'appel existant dans app.py
PrestaSyncDialog = RapprochementDialog


def open_rapprochement(master, app):
    """Point d'entrée : ouvre la fenêtre « Rapprochement de parc »."""
    RapprochementDialog(master, app)