"""Import du fichier mensuel Presta RMS (source externe).

Ce module lit le classeur Excel fourni par le prestataire, détecte la feuille
« Parc RMS selon contrat », retrouve les colonnes nécessaires **par leur nom**
(résiste aux évolutions mineures du fichier fournisseur) et construit une
structure temporaire en mémoire.

Aucune donnée de la CMDB n'est lue ni modifiée ici : l'import est purement
réversible (simple lecture).
"""

import openpyxl

from . import normalize

# Noms possibles de la feuille principale (recherche insensible à la casse,
# aux accents et aux espaces superflus).
SHEET_NAME_CANDIDATES = ("parc rms selon contrat",)

# Colonnes obligatoires : nom d'en-tête attendu -> clé interne.
REQUIRED_COLUMNS = {
    "tatouage": "tatouage",
    "numero de serie": "num_serie",
}

# Colonnes optionnelles mais exploitées si présentes.
OPTIONAL_COLUMNS = {
    "etat de l'actif": "etat",
    "sous-etat de l'actif": "sous_etat",
    "categorie de modele": "categorie_modele",
    "modele": "modele",
    "type de modele": "type_modele",
    "sous-type de modele": "sous_type_modele",
    "emplacement": "emplacement",
    "cree": "cree",
    "mis a jour": "mis_a_jour",
    "adresse ip": "ip",
    "adresse mac": "mac",
    "commentaires": "commentaires",
    "description": "description",
    "site": "site_presta",
    "grand site": "grandsite_presta",
    "type": "type_client",
}

# Feuille de référence client : table de découpage des sites.
SITES_SHEET = "Sites RMS"


class ClientSite:
    """Une ligne de la feuille « Sites RMS » (table de découpe du client).

    ``site_path`` est le chemin hiérarchique (ex. « /26 - DROME/SAINT PAUL
    TROIS CHATEAUX/TRICASTIN/ ») qui sert de préfixe à « Emplacement ».
    """

    __slots__ = ("grand_site", "nom_site", "site_path")

    def __init__(self, grand_site, nom_site, site_path):
        self.grand_site = normalize.strip_spaces(grand_site)
        self.nom_site = normalize.strip_spaces(nom_site)
        self.site_path = normalize.strip_spaces(site_path)

    def __repr__(self):
        return f"ClientSite({self.grand_site!r} / {self.nom_site!r})"


class PrestaImportError(Exception):
    """Erreur bloquante lors de l'import du fichier Presta RMS."""


class PrestaDevice:
    """Représentation temporaire d'une ligne du fichier Presta RMS.

    Les valeurs brutes sont conservées telles quelles ; les clés normalisées
    (tag_key / serial_key / site_key) servent uniquement au rapprochement.
    """

    __slots__ = (
        "row_index", "raw", "fields",
        "tatouage", "num_serie", "etat", "sous_etat",
        "categorie_modele", "modele", "type_modele", "sous_type_modele",
        "emplacement", "cree", "mis_a_jour", "ip", "mac",
        "commentaires", "description", "site_presta", "grandsite_presta",
        "type_client",
        "tag_key", "serial_key", "mac_key", "site_key",
    )

    def __init__(self, row_index, raw, fields):
        self.row_index = row_index
        self.raw = raw
        self.fields = fields
        for key in (
            "tatouage", "num_serie", "etat", "sous_etat", "categorie_modele",
            "modele", "type_modele", "sous_type_modele", "emplacement", "cree",
            "mis_a_jour", "ip", "mac", "commentaires", "description",
            "site_presta", "grandsite_presta", "type_client",
        ):
            setattr(self, key, normalize.strip_spaces(fields.get(key, "")))
        # Clés de rapprochement (normalisées, jamais affichées).
        self.tag_key = normalize.norm_tag(self.tatouage)
        self.serial_key = normalize.norm_serial(self.num_serie)
        self.mac_key = normalize.norm_mac(self.mac)
        self.site_key = normalize.location_site_key(self.emplacement)


def _norm_header(name):
    """Normalise un nom d'en-tête pour la recherche de colonnes."""
    return normalize.remove_accents(str(name if name is not None else "")).casefold().strip()


def find_sheet(workbook):
    """Retrouve la feuille « Parc RMS selon contrat » dans le classeur.

    Renvoie le nom exact de la feuille ou lève PrestaImportError.
    """
    for sheet_name in workbook.sheetnames:
        if _norm_header(sheet_name) in SHEET_NAME_CANDIDATES:
            return sheet_name
    raise PrestaImportError(
        "Feuille « Parc RMS selon contrat » introuvable dans le classeur.\n"
        f"Feuilles disponibles : {', '.join(workbook.sheetnames)}"
    )


def detect_header_row(rows, max_scan=20):
    """Détecte la ligne d'en-têtes contenant au minimum Tatouage + Numéro de série.

    Renvoie (index_ligne_0_based, mapping_colonne->clé_interne).
    """
    for idx, row in enumerate(rows[:max_scan]):
        colmap = {}
        for col_idx, cell in enumerate(row):
            header = _norm_header(cell)
            if not header:
                continue
            if header in REQUIRED_COLUMNS:
                colmap[col_idx] = REQUIRED_COLUMNS[header]
            elif header in OPTIONAL_COLUMNS:
                colmap[col_idx] = OPTIONAL_COLUMNS[header]
        required_keys = set(REQUIRED_COLUMNS.values())
        if required_keys.issubset(set(colmap.values())):
            return idx, colmap
    return None, None


def parse_presta_workbook(path):
    """Analyse le classeur Presta RMS et renvoie la liste des équipements.

    Lève PrestaImportError avec un message clair si le fichier n'est pas
    conforme (feuille absente, colonnes manquantes, aucune donnée exploitable).
    """
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise PrestaImportError(f"Impossible d'ouvrir le fichier : {exc}")
    try:
        sheet_name = find_sheet(workbook)
        rows = list(workbook[sheet_name].iter_rows(values_only=True))
    except PrestaImportError:
        raise
    except Exception as exc:
        raise PrestaImportError(f"Erreur de lecture de la feuille : {exc}")
    finally:
        workbook.close()

    if not rows:
        raise PrestaImportError("La feuille « Parc RMS selon contrat » est vide.")

    header_idx, colmap = detect_header_row(rows)
    if header_idx is None:
        missing = ", ".join(sorted(REQUIRED_COLUMNS))
        raise PrestaImportError(
            "Colonnes obligatoires introuvables dans la feuille "
            "« Parc RMS selon contrat ». Vérifiez que les colonnes "
            f"suivantes existent : {missing}."
        )

    devices = []
    skipped = 0
    for row_index, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        fields = {}
        for col_idx, key in colmap.items():
            value = row[col_idx] if col_idx < len(row) else None
            fields[key] = "" if value is None else str(value)
        device = PrestaDevice(row_index, row, fields)
        # Une ligne sans tatouage ET sans numéro de série est inexploitable :
        # elle ne peut être rattachée à aucun équipement de manière fiable.
        if not device.tag_key and not device.serial_key:
            skipped += 1
            continue
        devices.append(device)

    if not devices:
        raise PrestaImportError(
            "Aucune ligne exploitable trouvée (tatouage ou numéro de série requis)."
        )
    return devices, {
        "sheet_name": sheet_name,
        "header_row": header_idx + 1,
        "total_rows": len(devices),
        "skipped_rows": skipped,
    }


def load_presta_file(path):
    """Point d'entrée : importe le fichier Presta RMS.

    Renvoie (devices, infos). Ne touche jamais à la CMDB.
    """
    return parse_presta_workbook(path)


def load_sites_rms(path):
    """Lit la feuille « Sites RMS » du classeur client.

    Renvoie une list[ClientSite] (table de découpage : Grand Site / Nom
    Site / Site). Ne lève jamais : si la feuille manque, la liste est vide.
    """
    sites = []
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return sites
    try:
        if SITES_SHEET not in workbook.sheetnames:
            return sites
        ws = workbook[SITES_SHEET]
        for row in ws.iter_rows(values_only=True):
            if not row or not row[0]:
                continue
            grand = normalize.strip_spaces(row[0])
            nom = normalize.strip_spaces(row[1] if len(row) > 1 else "")
            chemin = normalize.strip_spaces(row[2] if len(row) > 2 else "")
            if not grand and not nom:
                continue
            # Saute la ligne d'en-têtes éventuelle
            if grand.casefold() in ("grand site", "grandsite"):
                continue
            sites.append(ClientSite(grand, nom, chemin))
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return sites


def load_client_file(path):
    """Charge l'intégralité du fichier client.

    Renvoie (devices, sites_rms, infos) :
      * devices   — list[PrestaDevice] (feuille « Parc RMS selon contrat ») ;
      * sites_rms — list[ClientSite] (feuille « Sites RMS », table de découpe).

    C'est LA source utilisée par le moteur de rapprochement mensuel.
    """
    devices, infos = parse_presta_workbook(path)
    sites = load_sites_rms(path)
    infos["sites_rms"] = len(sites)
    return devices, sites, infos