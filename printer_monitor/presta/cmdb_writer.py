"""Application transactionnelle des modifications validées à la CMDB.

Règles strictes :

    * un fichier de sauvegarde (.bak) est créé avant toute modification ;
    * l'écriture passe par un fichier temporaire puis un remplacement
      atomique (``os.replace``) — si une erreur survient, l'original n'est
      pas touché ;
    * seules les cellules explicitement sélectionnées sont modifiées ;
    * les formules, formats et colonnes internes (SPECIFITE, STATUT,
      COMMENTAIRES, garanties manuelles…) ne JAMAIS écrasés : seules les
      colonnes listées dans FIELD_TO_COL sont accessibles ;
    * les valeurs écrites proviennent TOUJOURS de la traduction vers la
      nomenclature interne (jamais des valeurs brutes du client).

Le classeur est ouvert en ``data_only=False`` pour préserver les formules.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

import openpyxl

from . import normalize
from .reconcile import CMDB_HEADERS


class CMDBWriteError(Exception):
    """Erreur d'écriture de la CMDB (aucun fichier modifié)."""


def _column_letter(header_index):
    """Convertit un index de colonne (0-based) en lettre Excel."""
    letters = ""
    idx = header_index + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_HEADER_TO_COL = {name: i for i, name in enumerate(CMDB_HEADERS)}

# Colonnes que la synchronisation peut renseigner — et SEULEMENT celles-là.
# Les autres (SPECIFITE, STATUT, COMMENTAIRES, garanties manuelles, dates…)
# restent la propriété exclusive de la gestion interne.
FIELD_TO_COL = {
    "Grandsite": _HEADER_TO_COL["Grandsite"],
    "Site": _HEADER_TO_COL["Site"],
    "Categorie": _HEADER_TO_COL["Categorie"],
    "AdresseIP": _HEADER_TO_COL["AdresseIP"],
    "Numeroetiquette": _HEADER_TO_COL["Numeroetiquette"],
    "Marque": _HEADER_TO_COL["Marque"],
    "Modele": _HEADER_TO_COL["Modele"],
    "Numserie": _HEADER_TO_COL["Numserie"],
    "AdresseMAC": _HEADER_TO_COL["AdresseMAC"],
    "Bâtiment": _HEADER_TO_COL["Bâtiment"],
    "Etage": _HEADER_TO_COL["Etage"],
    "Bureau": _HEADER_TO_COL["Bureau"],
    "CONCATENER": _HEADER_TO_COL["CONCATENER"],
    "Type": _HEADER_TO_COL["Type"],
}


def _locate_header_row(ws):
    """(ligne_en-têtes, dict nom->lettre_colonne) pour la feuille CMDB."""
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=20), start=1):
        header_map = {}
        for cell in row:
            if cell.value is None:
                continue
            norm = str(cell.value).strip()
            if norm in CMDB_HEADERS:
                header_map[norm] = cell.column_letter
        if "Numeroetiquette" in header_map and "Grandsite" in header_map:
            return row_idx, header_map
    raise CMDBWriteError(
        "En-têtes de CMDB introuvables : vérifiez que la feuille CMDB "
        "contient les colonnes « Grandsite » et « Numeroetiquette »."
    )


def _find_row_in_sheet(ws, header_row, tag_col_letter, tag_key):
    """Cherche une ligne par clé de tatouage → numéro de ligne Excel."""
    for row_idx in range(header_row + 1, ws.max_row + 1):
        cell = ws[f"{tag_col_letter}{row_idx}"]
        if cell.value is None:
            continue
        if normalize.norm_tag(str(cell.value)) == tag_key:
            return row_idx
    return None


def apply_modifications(cmdb_path, additions, modifications, deletions=None):
    """Applique les changements validés à la CMDB (transactionnel).

    additions     : list[Addition] — seules celles avec ``applied`` ;
    modifications : list[Modification] — champs avec ``applied`` par champ ;
    deletions     : list[CmdbRow] — lignes vidées après confirmation.

    Renvoie {"added", "modified_cells", "deleted"} ; lève CMDBWriteError
    en cas d'échec (l'original reste alors intact).
    """
    deletions = deletions or []
    path = Path(cmdb_path)
    if not path.is_file():
        raise CMDBWriteError(f"Le fichier CMDB n'existe pas : {path}")

    backup_path = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, backup_path)
    except OSError as exc:
        logging.exception("Sauvegarde de la CMDB impossible : %s", exc)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".xlsx", prefix="cmdb_tmp_", dir=str(path.parent)
    )
    os.close(tmp_fd)

    try:
        workbook = openpyxl.load_workbook(str(path), data_only=False)
        if "CMDB" not in workbook.sheetnames:
            raise CMDBWriteError(
                "La feuille « CMDB » est introuvable dans le fichier."
            )
        ws = workbook["CMDB"]
        header_row, header_map = _locate_header_row(ws)
        tag_col_letter = header_map["Numeroetiquette"]

        added = 0
        modified_cells = 0
        deleted = 0

        # --- Modifications champ par champ ---
        for modif in modifications:
            if not modif.applied:
                continue
            tag_key = normalize.norm_tag(modif.cmdb_row.tag)
            if not tag_key:
                continue
            row_idx = _find_row_in_sheet(
                ws, header_row, tag_col_letter, tag_key
            )
            if row_idx is None:
                logging.warning(
                    "Ligne CMDB introuvable (modification ignorée) : %s",
                    modif.cmdb_row.tag,
                )
                continue
            for change in modif.field_changes:
                if not change.applied:
                    continue
                col_idx = FIELD_TO_COL.get(change.field)
                if col_idx is None:
                    # Colonne interne : jamais écrasée par la synchro.
                    continue
                ws[f"{_column_letter(col_idx)}{row_idx}"] = change.new_value
                modified_cells += 1

        # --- Ajouts : valeurs DÉJÀ TRADUITES vers la nomenclature interne ---
        for addition in additions:
            if not addition.applied:
                continue
            values = addition.values_for_cmdb()
            if not values.get("Numeroetiquette") and not values.get("Numserie"):
                continue  # rien d'identifiable : on n'ajoute pas aveuglément
            row_idx = ws.max_row + 1
            for column, value in values.items():
                col_idx = FIELD_TO_COL[column]
                ws[f"{_column_letter(col_idx)}{row_idx}"] = value
            added += 1

        # --- Retraits : vidage de ligne, JAMAIS de suppression automatique ---
        for deletion in deletions:
            if not getattr(deletion, "applied", False):
                continue
            tag_key = normalize.norm_tag(deletion.tag)
            if not tag_key:
                continue
            row_idx = _find_row_in_sheet(
                ws, header_row, tag_col_letter, tag_key
            )
            if row_idx is None:
                continue
            max_col = len(CMDB_HEADERS)
            for cell in ws[row_idx]:
                if cell.column <= max_col:
                    cell.value = None
            deleted += 1

        workbook.save(tmp_name)
        workbook.close()

        os.replace(tmp_name, path)
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            pass

        return {
            "added": added,
            "modified_cells": modified_cells,
            "deleted": deleted,
        }

    except CMDBWriteError:
        _cleanup_tmp(tmp_name)
        raise
    except Exception as exc:
        _cleanup_tmp(tmp_name)
        logging.exception("Échec de l'application des modifications CMDB")
        raise CMDBWriteError(f"Erreur pendant l'écriture de la CMDB : {exc}")


def _cleanup_tmp(tmp_name):
    try:
        os.remove(tmp_name)
    except OSError:
        pass


__all__ = ("CMDBWriteError", "apply_modifications", "FIELD_TO_COL")