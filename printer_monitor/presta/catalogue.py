"""Catalogue de référence construit à partir de la CMDB.

Ce catalogue est la mémoire de la nomenclature interne : sites, bâtiments,
étages, bureaux, marques/modèles réellement présents. Sa fonction est de
permettre des correspondances **normalisées** (niveau 2) et **floues**
(niveau 1) entre les valeurs client et les valeurs CMDB, sans jamais
modifier la base de référence elle-même.

Le catalogue est ré-construit à chaque import : il reflète toujours l'état
ACTUEL de la CMDB (la référence finale).
"""

from difflib import SequenceMatcher

from . import normalize
from .model_map import ModelMapper


def _sequence_score(a, b):
    """Similarité 0..1 entre deux chaînes normalisées / compactes."""
    text_a = normalize.norm_compact(a)
    text_b = normalize.norm_compact(b)
    if not text_a or not text_b:
        return 0.0
    return SequenceMatcher(None, text_a, text_b).ratio()


class CmdbCatalogue:
    """Index de recherche de la nomenclature interne (feuille CMDB)."""

    def __init__(self, cmdb_rows, model_mapper=None):
        self.rows = list(cmdb_rows)
        # (Grandsite, Site) présents dans la CMDB -> nombre de lignes.
        self.site_pairs = []
        # Grandsite seul -> nombre de lignes.
        self.grandsite_values = {}
        # Par champ de localisation : norm_key(valeur) -> Counter(valeur exacte).
        self.field_values = {"batiment": {}, "etage": {}, "bureau": {}}
        self.models = model_mapper
        if self.models is None:
            self.models = ModelMapper()
            models = [
                (r.marque, r.modele)
                for r in self.rows
                if r.marque and r.modele
            ]
            self.models.load_cmdb(models)
        self._build()

    # ------------------------------------------------------------------ #
    # Construction des index
    # ------------------------------------------------------------------ #
    def _build(self):
        pairs = {}
        for r in self.rows:
            g = normalize.strip_spaces(r.grandsite)
            s = normalize.strip_spaces(r.site)
            key = (g, s)
            pairs[key] = pairs.get(key, 0) + 1
            if g:
                self.grandsite_values[g] = self.grandsite_values.get(g, 0) + 1
        self.site_pairs = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))
        self.site_by_compact = {}
        self.site_by_norm = {}
        for (g, s), count in self.site_pairs:
            cg = normalize.norm_compact(g)
            self.site_by_compact.setdefault(cg, []).append(((g, s), count))
            self.site_by_norm.setdefault(normalize.norm_key(s), []).append(((g, s), count))
            self.site_by_norm.setdefault(normalize.norm_key(g), []).append(((g, s), count))
            if s and s != g:
                self.site_by_norm.setdefault(
                    normalize.norm_key(s.replace("/", " ")), []
                ).append(((g, s), count))

        for field, attr in (
            ("batiment", "batiment"),
            ("etage", "etage"),
            ("bureau", "bureau"),
        ):
            values = self.field_values[field]
            for r in self.rows:
                val = normalize.strip_spaces(getattr(r, attr))
                if not val or val.casefold() in ("-", "n.a.", "na"):
                    continue
                key = normalize.norm_key(val)
                bucket = values.setdefault(key, {})
                bucket[val] = bucket.get(val, 0) + 1
# ------------------------------------------------------------------ #
    # Recherche d'un Grand site / Site CMDB
    # ------------------------------------------------------------------ #
    def resolve_site(self, grand_client, site_client, threshold=0.62):
        """Cherche le (Grandsite, Site) CMDB le plus proche.

        Ordre des niveaux de confiance :
          1. équivalence compacte complète (TRICASTIN/TRICASTIN) ;
          2. Grand Site client équivalent à un Grandsite CMDB ;
          3. Site client équivalent (clé normalisée) ;
          4. recherche floue si le score dépasse ``threshold``.

        Renvoie (grandsite, site, score, classe) avec classe ∈
        {'normalise', 'floue', 'inconnu'}.
        """
        # 1. compact intégral (ex. TRICASTIN/TRICASTIN)
        if grand_client and site_client:
            compact = (f"{normalize.norm_compact(grand_client)}/"
                       f"{normalize.norm_compact(site_client)}")
            for (g, s), _count in self.site_pairs:
                combo = f"{normalize.norm_compact(g)}/{normalize.norm_compact(s)}"
                if combo == compact:
                    return g, s, 1.0, "normalise"
        # 2. Grand Site seul
        if grand_client:
            hits = self.site_by_compact.get(normalize.norm_compact(grand_client))
            if hits:
                (g, s), _count = hits[0]
                return g, s, 1.0, "normalise"
        # 3. Site normalisé
        if site_client:
            hits = self.site_by_norm.get(normalize.norm_key(site_client))
            if hits:
                (g, s), _count = hits[0]
                return g, s, 1.0, "normalise"
        # 3bis. Contenance unique (ex. BECQUEREL ⊂ NOGENT/BECQUEREL)
        cands = self._containment_site_candidates(grand_client, site_client)
        if cands is not None and len(cands) == 1:
            g, s = cands[0]
            return g, s, 0.95, "normalise"
        # 4. flou
        candidates = []
        for (g, s), count in self.site_pairs:
            base = g
            if s and s != g:
                base = f"{g} {s}"
            score_g = self._score_piece(g, grand_client or site_client)
            score_s = self._score_piece(s, site_client or grand_client)
            score = max(score_g, score_s)
            if score >= threshold:
                candidates.append((score, (g, s)))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            score, (g, s) = candidates[0]
            return g, s, score, "floue"
        return None, None, 0.0, "inconnu"

    def _containment_site_candidates(self, grand_client, site_client):
        """Paires CMDB dont le Grandsite/Site CONTIENT la valeur client.

        Ne renvoie une liste que si elle est non ambiguë (une seule paire
        distincte). None si aucune contenance trouvée.
        """
        hits_g = []
        hits_s = []
        cg = normalize.norm_compact(grand_client or "")
        cs = normalize.norm_compact(site_client or "")
        if cg:
            for (g, s), _count in self.site_pairs:
                if cg in normalize.norm_compact(g):
                    hits_g.append((g, s))
        if cs:
            for (g, s), _count in self.site_pairs:
                if cs in normalize.norm_compact(s):
                    hits_s.append((g, s))
        if cg and cs and hits_g and hits_s:
            inter = [p for p in hits_g if p in hits_s]
            if inter:
                return sorted(set(inter))
            return None
        if hits_g and len(set(hits_g)) == 1:
            return hits_g
        if hits_s and len(set(hits_s)) == 1:
            return hits_s
        return None

    @staticmethod
    def _score_piece(cmdb_text, *client_texts):
        """Score 0..1 combinant similarité et recouvrement de mots."""
        best = 0.0
        for c in client_texts:
            if not c:
                continue
            tokens_c = set(normalize.norm_compact(c).split(" ")) - {""}
            tokens_m = set(normalize.norm_compact(cmdb_text).split(" ")) - {""}
            inter = 0.0
            if tokens_c and tokens_m:
                inter = len(tokens_c & tokens_m) / max(len(tokens_c), len(tokens_m))
            best = max(best, _sequence_score(cmdb_text, c), inter)
        return best

    # ------------------------------------------------------------------ #
    # Recherche d'une valeur de localisation (bâtiment / étage / bureau)
    # ------------------------------------------------------------------ #
    def resolve_location(self, field, value, threshold=0.55):
        """Cherche la valeur CMDB la plus plausible pour un segment client.

        field : 'batiment' | 'etage' | 'bureau'.
        Renvoie (valeur_cmdb, score, classe) avec
        classe ∈ {'exact', 'normalise', 'floue', 'inconnu'}.
        """
        text = normalize.strip_spaces(value)
        if not text:
            return "", 1.0, "exact"
        values = self.field_values.get(field, {})
        key = normalize.norm_key(text)
        bucket = values.get(key)
        if bucket:
            best_val = max(bucket, key=lambda k: bucket[k])
            return best_val, 1.0, "exact"
        # comparai-son compacte (espaces / ponctuation ignorés)
        comp = normalize.norm_compact(text)
        for k_key, bucket in values.items():
            if comp and normalize.norm_compact(k_key) == comp:
                best_val = max(bucket, key=lambda kk: bucket[kk])
                return best_val, 0.98, "normalise"
        # recouvrement fort, avec GARDE-FOUS (jamais de valeur inventée) :
        #   * même premier caractère alphanumérique ;
        #   * score >= 0.72 ;
        #   * signature numérique compatible (« 19M » ≈ « 19 M », mais
        #     « 6M60 » ≠ « M6.011 »).
        comp = normalize.norm_compact(text)
        first = comp[0] if comp else ""
        digits_src = "".join(ch for ch in comp if ch.isdigit())
        best = (0.0, "", "")
        for k_key, bucket in values.items():
            if not bucket:
                continue
            k_comp = normalize.norm_compact(str(k_key))
            if not k_comp or k_comp[0] != first:
                continue
            digits_k = "".join(ch for ch in k_comp if ch.isdigit())
            if digits_src != digits_k:
                continue
            score = _sequence_score(text, str(k_key))
            if score > best[0]:
                v0 = next(iter(bucket))
                best = (score, k_key, v0)
        if best[0] >= max(threshold, 0.72):
            bucket = values[best[1]]
            best_val = max(bucket, key=lambda kk: bucket[kk])
            return best_val, best[0], "floue"
        return "", 0.0, "inconnu"

    def model_refs(self):
        return self.models._ref


__all__ = ("CmdbCatalogue",)