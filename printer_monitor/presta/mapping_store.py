"""Table de correspondances persistante du moteur de rapprochement.

Cette table mémorise, entre deux imports, les correspondances validées par
l'utilisateur (une fois) ainsi que les correspondances apprises (cumul) :

    * site           : clé client (grand site + nom site) -> (grandsite, site)
    * batiment/étage : segment client -> valeur CMDB
    * modèle         : nom de modèle client -> (marque, modele)
    * catégorie/type : dérivations constatées

La CMDB reste toujours la référence finale : cette table ne fait que traduire
les valeurs client vers la nomenclature interne.
"""

import json
import logging
from pathlib import Path

from . import normalize

MAPPINGS_FILE = Path("printer_monitor_reconcile_map.json")


def _norm(value):
    return normalize.norm_compact(value)


class MappingStore:
    """Stockage persistant (JSON) des correspondances validées et apprises."""

    def __init__(self, path=MAPPINGS_FILE):
        self.path = Path(path)
        self.validated = {
            "site": {},       # client_key -> {"grandsite": …, "site": …}
            "batiment": {},   # client_norm -> cmdb value
            "etage": {},
            "bureau": {},
            "model": {},      # client_norm -> (marque, modele)
            "categorie": {},
            "type": {},
        }
        self.learned = {}     # compteurs cumulés (précision uniquement)
        self.load()

    # ------------------------------------------------------------------ #
    # Persistance
    # ------------------------------------------------------------------ #
    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.warning("Mappings illisibles (%s) : ignorés", exc)
            return
        if not isinstance(data, dict):
            return
        val = data.get("validated")
        if isinstance(val, dict):
            for sect in self.validated:
                if isinstance(val.get(sect), dict):
                    self.validated[sect] = val[sect]
        learned = data.get("learned")
        if isinstance(learned, dict):
            self.learned = learned

    def save(self):
        payload = {"validated": self.validated, "learned": self.learned}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logging.warning("Écriture des mappings impossible : %s", exc)

    # ------------------------------------------------------------------ #
    # Accès sites
    # ------------------------------------------------------------------ #
    def site_key(self, grand_site, nom_site):
        """Clé client canonique pour la table des sites."""
        return f"{_norm(grand_site or '')}|{_norm(nom_site or '')}"

    def validated_site(self, client_key):
        entry = self.validated["site"].get(client_key)
        if entry:
            return entry.get("grandsite", ""), entry.get("site", "")
        return None

    def add_site(self, client_key, grandsite, site):
        self.validated["site"][client_key] = {
            "site": normalize.strip_spaces(site),
            "grandsite": normalize.strip_spaces(grandsite),
        }
        self.save()

    # ------------------------------------------------------------------ #
    # Accès localisation
    # ------------------------------------------------------------------ #
    def validated_location(self, field, client_value):
        return self.validated.get(field, {}).get(_norm(client_value))

    def add_location(self, field, client_value, cmdb_value):
        self.validated.setdefault(field, {})[_norm(client_value)] = \
            normalize.strip_spaces(cmdb_value)
        self.save()

    # ------------------------------------------------------------------ #
    # Accès modèles / dérivés
    # ------------------------------------------------------------------ #
    def validated_model(self, client_model):
        entry = self.validated["model"].get(_norm(client_model))
        if entry:
            return entry.get("marque", ""), entry.get("modele", "")
        return None

    def add_model(self, client_model, marque, modele):
        self.validated["model"][_norm(client_model)] = {
            "marque": normalize.strip_spaces(marque),
            "modele": normalize.strip_spaces(modele),
        }
        self.save()

    def validated_derived(self, kind, value):
        return self.validated.get(kind, {}).get(_norm(value))

    def add_derived(self, kind, client_value, cmdb_value):
        self.validated.setdefault(kind, {})[_norm(client_value)] = \
            normalize.strip_spaces(cmdb_value)
        self.save()

    def add_learned_counts(self, learned):
        """Fusionne des compteurs d'apprentissage dans le cumul persistant."""
        if not learned:
            return
        for sect, sub in learned.items():
            bucket = self.learned.setdefault(sect, {})
            for key, targets in sub.items():
                tgt = bucket.setdefault(key, {})
                for t, n in targets.items():
                    tgt[t] = tgt.get(t, 0) + n
        self.save()


__all__ = ("MappingStore", "MAPPINGS_FILE")