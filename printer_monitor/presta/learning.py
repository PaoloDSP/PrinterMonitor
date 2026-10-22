"""Apprentissage des correspondances à partir des équipements réellement
appariés (tatouage CMDB == tatouage client).

Pour chaque paire confirmée, on observe comment le client nomme le site, le
bâtiment, l'étage, le bureau, le modèle… et comment la CMDB les enregistre.
Ces observations alimentent des compteurs : une correspondance devient
« fiable » si elle est majoritaire et non contradictoire, « ambiguë » sinon.
"""

from . import normalize


def split_segments(value):
    """Découpe un chemin (client) en segments propres, en majuscules."""
    return [
        normalize.strip_spaces(s).upper()
        for s in normalize.split_location_path(value)
        if s
    ]


def residual_after_site(emplacement, site_path):
    """Segments de l'emplacement qui suivent le préfixe de site client.

    Ex. emplacement « /26 - DROME/…/TRICASTIN/ATELIER CENTRAL A/RDC/ATA103/ »
    et site_path « /26 - DROME/…/TRICASTIN/ »
    -> [« ATELIER CENTRAL A », « RDC », « ATA103 »]
    """
    full = split_segments(emplacement)
    pref = split_segments(site_path)
    if not pref:
        return full
    n = 0
    for a, b in zip(full, pref):
        if a != b:
            break
        n += 1
    return full[n:] if n else full


def classify_residual(residual):
    """Classe les segments résiduels en (bâtiment, étage, bureau)."""
    seg = [s for s in (residual or []) if s]
    if not seg:
        return ("", "", "")
    if len(seg) >= 3:
        return (" / ".join(seg[:-2]), seg[-2], seg[-1])
    if len(seg) == 2:
        if normalize.is_floor(seg[-1]):
            return (seg[0], seg[-1], "")
        return (seg[0], "", seg[-1])
    if normalize.is_floor(seg[0]):
        return ("", seg[0], "")
    return (seg[0], "", "")


def client_after_site(client):
    """Segments résiduels d'un PrestaDevice (emplacement sans préfixe site)."""
    return residual_after_site(client.emplacement, client.site_presta)


def _client_site_key(client):
    """Clé site client canonique (grand site + nom/chemin site)."""
    g = client.grandsite_presta or ""
    s = client.site_presta or ""
    if not (g or s):
        return ""
    return f"{normalize.norm_key(g)}|{normalize.norm_key(s)}"


class LearnedMappings:
    """Compteurs d'observations client -> CMDB.

    Les localisations sont apprises à DEUX niveaux :
      * contextuel : (clé site client | segment) -> valeur CMDB ;
      * global     : segment -> valeur CMDB (secours).
    """

    def __init__(self):
        self.site = {}           # client_key -> {(grandsite, site): n}
        self.batiment = {}       # ctx|seg -> {cmdb_value: n}
        self.etage = {}
        self.bureau = {}
        self.batiment_glob = {}  # seg -> {cmdb_value: n}
        self.etage_glob = {}
        self.bureau_glob = {}
        self.model = {}          # client_norm -> {(marque, modele): n}
        self.categorie = {}      # client -> {cmdb: n}
        self.type = {}           # client -> {cmdb: n}

    LOCATION_FIELDS = ("batiment", "etage", "bureau")

    @staticmethod
    def _bump(store, key, target):
        counter = store.setdefault(key, {})
        counter[target] = counter.get(target, 0) + 1

    def observe(self, client, cmdb):
        """Enregistre les correspondances d'une paire confirmée."""
        client_key = client_site_key(client)
        if client_key and (cmdb.grandsite or cmdb.site):
            self._bump(self.site, client_key, (cmdb.grandsite, cmdb.site))

        bat, etage, bureau = classify_residual(client_after_site(client))
        for field, seg, val in (
            ("batiment", bat, cmdb.batiment),
            ("etage", etage, cmdb.etage),
            ("bureau", bureau, cmdb.bureau),
        ):
            if seg and val:
                k_seg = normalize.norm_key(seg)
                self._bump(getattr(self, field),
                           f"{client_key}|{k_seg}", val)
                self._bump(getattr(self, field + "_glob"), k_seg, val)

        if client.modele and cmdb.marque and cmdb.modele:
            self._bump(
                self.model, normalize.norm_key(client.modele),
                (cmdb.marque, cmdb.modele),
            )
        if client.sous_type_modele and cmdb.categorie:
            self._bump(
                self.categorie, normalize.norm_key(client.sous_type_modele),
                cmdb.categorie,
            )
        if client.type_client and cmdb.type:
            self._bump(self.type, normalize.norm_key(client.type_client),
                       cmdb.type)

    # ------------------------------------------------------------------ #
    # Interrogation
    # ------------------------------------------------------------------ #
    def majority(self, store, key):
        """Cible majoritaire non ambiguë pour une clé donnée, sinon None.

        La clé est normalisée ici : les appelants peuvent passer une valeur
        brute ou une clé déjà normalisée (idempotent).

        Règle : le meilleur candidat doit coiffer nettement le second
        (≥ 55 % des observations et au moins 2× le second).
        """
        counter = store.get(normalize.norm_key(key))
        if not counter:
            return None
        items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
        top, n_top = items[0]
        total = sum(counter.values())
        if total <= 0:
            return None
        if len(items) == 1:
            return top
        n_second = items[1][1]
        if n_top / total >= 0.55 and n_top >= n_second * 2:
            return top
        return None

    def location_value(self, field, client_key, segment):
        """Valeur CMDB apprise pour un segment de localisation.

        Cherche d'abord dans le contexte du site client, puisglobalement.
        Renvoie (valeur | None, niveau) où niveau ∈ {'contexte','global'}.
        """
        k_seg = normalize.norm_key(segment or "")
        if not k_seg:
            return None, None
        ctx_key = normalize.norm_key(f"{client_key}|{k_seg}")
        val = self.majority(getattr(self, field), ctx_key)
        if val is not None:
            return val, "contexte"
        val = self.majority(getattr(self, field + "_glob"), k_seg)
        if val is not None:
            return val, "global"
        return None, None

    def location_candidates(self, field, client_key, segment, limit=6):
        """Candidats observés pour un segment (affichage groupé)."""
        k_seg = normalize.norm_key(segment or "")
        out = []
        for store, key in (
            (getattr(self, field, {}),
             normalize.norm_key(f"{client_key}|{k_seg}")),
            (getattr(self, field + "_glob", {}), k_seg),
        ):
            counter = store.get(key) or {}
            for value, n in sorted(
                counter.items(), key=lambda kv: (-kv[1], str(kv[0]))
            )[:limit]:
                label = f"{value} ({n})"
                if label not in out:
                    out.append(label)
        return out

    def ambiguous_candidates(self, store, key):
        """Tous les candidats (valeur, n) d'une clé (diagnostic)."""
        counter = store.get(key)
        if not counter:
            return []
        return sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))


def client_site_key(client):
    """Clé site client canonique (grand site + chemin site)."""
    g = client.grandsite_presta or ""
    s = client.site_presta or ""
    if not (g or s):
        return ""
    return f"{normalize.norm_key(g)}|{normalize.norm_key(s)}"


# Alias historique
_client_site_key = client_site_key


def learn_from_pairs(pairs):
    """Construit un LearnedMappings depuis des paires (client, cmdb_row)."""
    learning = LearnedMappings()
    for client, cmdb in pairs:
        if client and cmdb:
            learning.observe(client, cmdb)
    return learning


__all__ = (
    "LearnedMappings", "learn_from_pairs", "residual_after_site",
    "classify_residual", "client_after_site", "norm_key",
)