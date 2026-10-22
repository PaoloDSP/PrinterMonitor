"""Historique / traçabilité des synchronisations CMDB <-> Presta RMS.

Chaque application de modifications (après validation humaine) est enregistrée
dans un fichier JSONL horodaté. Pour chaque modification de champ, on conserve
l'équipement, le champ, l'ancienne valeur, la nouvelle valeur, la date et la
source (= Presta RMS).

Ce module préserve l'indépendance avec l'historique SNMP existant (SQLite) :
il s'agit d'un journal dédié à la synchronisation de la CMDB.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

SYNC_HISTORY_FILE = Path("printer_monitor_presta_sync.jsonl")

SOURCE = "Presta RMS"


class SyncHistory:
    """Journal JSONL des synchronisations CMDB <-> Presta RMS."""

    def __init__(self, path=SYNC_HISTORY_FILE):
        self.path = Path(path)

    def _append(self, record):
        """Ajoute un enregistrement JSONL (robuste en cas d'erreur d'écriture)."""
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logging.exception("Impossible d'écrire dans l'historique de synchronisation : %s", exc)

    def record_sync(
        self,
        source_file,
        result,
        applied_additions,
        applied_modifications,
        applied_deletions,
        report,
    ):
        """Enregistre une synchronisation complète après application.

        Paramètres
        ----------
        source_file : str — nom du fichier Presta RMS importé.
        result : ReconcileResult — résultat du rapprochement (pour les compteurs).
        applied_additions : list[Addition] — ajouts réellement appliqués.
        applied_modifications : list[Modification] — modifs réellement appliquées.
        applied_deletions : list[CmdbRow] — retraits réellement appliqués.
        report : dict — rapport renvoyé par cmdb_writer.apply_modifications.
        """
        counts = result.counts if result else {}
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source_file": str(source_file),
            "source": SOURCE,
            "counters": {
                "additions_detected": counts.get("additions", 0),
                "modifications_detected": counts.get("modifications", 0),
                "removals_detected": counts.get("removals", 0),
                "anomalies_detected": counts.get("anomalies", 0),
                "unchanged": counts.get("unchanged", 0),
            },
            "applied": report,
            "field_changes": [],
            "added_devices": [],
            "deleted_devices": [],
        }

        # Détail des modifications appliquées : équipement, champ,
        # ancienne -> nouvelle valeur, date (toutes identiques), source.
        for modif in applied_modifications:
            device_label = (
                modif.cmdb_row.tag
                or modif.cmdb_row.numeroserie
                or modif.cmdb_row.display()
            )
            for change in modif.field_changes:
                if not change.applied:
                    continue
                record["field_changes"].append({
                    "equipment": device_label,
                    "field": change.field,
                    "old_value": change.cmdb_value,
                    "new_value": change.new_value,
                    "source": SOURCE,
                })

        for addition in applied_additions:
            values = addition.values_for_cmdb()
            record["added_devices"].append({
                "tatouage": values.get("Numeroetiquette",
                                       addition.client_device.tatouage),
                "num_serie": values.get("Numserie",
                                        addition.client_device.num_serie),
                "modele": values.get("Modele", ""),
                "site": values.get("Site", ""),
                "grandsite": values.get("Grandsite", ""),
            })

        for deletion in applied_deletions:
            record["deleted_devices"].append({
                "tatouage": deletion.tag,
                "num_serie": deletion.numeroserie,
                "modele": deletion.modele,
            })

        self._append(record)

    def read_all(self):
        """Lit tout l'historique (liste de records, du plus récent au plus ancien)."""
        records = []
        if not self.path.exists():
            return records
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logging.exception("Historique de synchronisation illisible : %s", exc)
        records.reverse()
        return records


__all__ = ("SyncHistory", "SOURCE", "SYNC_HISTORY_FILE")