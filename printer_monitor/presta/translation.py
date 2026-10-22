"""Moteur de TRaduction : SOURCE client -> OBJET CMDB.

Avant toute comparaison, chaque ligne cliente est transformée en un objet
ayant EXACTEMENT la structure logique de notre CMDB, avec une provenance et
un niveau de confiance par champ :

    Niveau 1 — Exact           : correspondance validée (mapping utilisateur)
    Niveau 2 — Sites RMS       : identité site récupérée de la table « Sites RMS »
    Niveau 3 — Historique      : majorité observée sur des équipements appariés
    Niveau 4 — Normalisé       : correspondance normalisée très forte
    Niveau 5 — Flou            : correspondance approximative à score élevé
    Niveau 6 — Inconnu         : ANOMALIE (aucune traduction fiable)

La CMDB reste LA référence finale : on ne copie jamais une valeur client dans
la CMDB sans passer par cette traduction.
"""

from . import normalize
from .learning import classify_residual, client_after_site
from .normalize import normalize_ip

# Niveaux de confiance (cahier des charges §29)
LEVEL_EXACT = 1
LEVEL_SITES = 2
LEVEL_HIST = 3
LEVEL_NORM = 4
LEVEL_FUZZY = 5
LEVEL_UNKNOWN = 6

LEVEL_LABELS = {
    LEVEL_EXACT: "Exact",
    LEVEL_SITES: "Sites RMS",
    LEVEL_HIST: "Historique (CMDB)",
    LEVEL_NORM: "Normalisé",
    LEVEL_FUZZY: "Flou",
    LEVEL_UNKNOWN: "Inconnu",
}

LOCATION_FIELDS = ("batiment", "etage", "bureau")


class FieldValue:
    """Valeur CMDB interprétée, avec provenance, niveau et candidats."""

    __slots__ = ("value", "level", "client_raw", "candidates", "is_empty")

    def __init__(self, value="", level=LEVEL_UNKNOWN, client_raw="",
                 candidates=None, is_empty=False):
        self.value = value
        self.level = level
        self.client_raw = client_raw
        self.candidates = candidates or []
        self.is_empty = is_empty

    @property
    def label(self):
        return LEVEL_LABELS.get(self.level, "?")

    @property
    def is_unknown(self):
        if self.is_empty:
            return False
        return self.level >= LEVEL_UNKNOWN or not self.value

    def __repr__(self):
        return f"FieldValue({self.value!r}, {self.label})"


class TranslatedDevice:
    """Objet CMDB interprété depuis une ligne client."""

    def __init__(self, client):
        self.client = client
        self.fields = {}

    def set(self, name, value, level, client_raw="", candidates=None):
        self.fields[name] = FieldValue(value, level, client_raw, candidates)

    def get(self, name):
        fv = self.fields.get(name)
        return fv.value if fv and not fv.is_empty else ""

    def level_of(self, name):
        fv = self.fields.get(name)
        return fv.level if fv else None

    def field(self, name):
        return self.fields.get(name)

    def build_concatener(self):
        parts = [
            self.get("grandsite"),
            self.get("site"),
            self.get("batiment"),
            self.get("etage"),
            self.get("bureau"),
        ]
        return "/".join(p for p in parts if p)

    def as_dict(self):
        return {name: fv.value for name, fv in self.fields.items() if not fv.is_empty}

    def is_reliable(self, name):
        fv = self.fields.get(name)
        if fv is None or fv.is_empty:
            return True
        return fv.level <= LEVEL_NORM
# ---------------------------------------------------------------------- #
# ---------------------------------------------------------------------- #
#  Helpers de site
# ---------------------------------------------------------------------- #
def find_client_site(client, sites_rms):
    """Entrée « Sites RMS » dont le chemin est préfixe du site client.

    Renvoie l'entrée la plus longue qui correspond (la plus précise),
    sinon None.
    """
    if not sites_rms:
        return None
    site_path = client.site_presta or client.emplacement or ""
    segs = [
        s.upper()
        for s in (site_path or "").replace("\\", "/").split("/")
        if s.strip()
    ]
    best = None
    for entry in sites_rms:
        epath = [
            s.upper()
            for s in (entry.site_path or "").replace("\\", "/").split("/")
            if s.strip()
        ]
        if not epath:
            continue
        if len(epath) <= len(segs) and all(a == b for a, b in zip(epath, segs)):
            if best is None or len(epath) > best[0]:
                best = (len(epath), entry)
    return best[1] if best else None


def site_identity(client, site_rms):
    """(grand_site_client, nom_site_client) d'une ligne."""
    if site_rms is not None:
        return site_rms.grand_site, site_rms.nom_site
    g = client.grandsite_presta or ""
    s = client.site_presta or ""
    if g:
        return g, ""
    return "", s



#  Résolution site -> (Grandsite, Site) CMDB
# ---------------------------------------------------------------------- #
def _site_client_key(grand, nom):
    return f"{normalize.norm_key(grand or '')}|{normalize.norm_key(nom or '')}"


def _resolve_site(grand, nom, catalogue, mappings, learned, site_rms_found):
    """Traduit l'identité site client vers (grandsite, site) CMDB.

    Renvoie {'grandsite': FieldValue, 'site': FieldValue}. Ordre :
    validé -> Sites RMS -> historique -> normalisé -> flou -> anomalie.
    """
    key = _site_client_key(grand, nom)
    label = " / ".join(v for v in (grand, nom) if v)

    # 1. Correspondance exacte validée (niveau le plus fort)
    if mappings is not None:
        found = mappings.validated_site(key)
        if found:
            return {
                "grandsite": FieldValue(found[0], LEVEL_EXACT, label),
                "site": FieldValue(found[1], LEVEL_EXACT, label),
            }

    # 2. Historique (majorité observée sur les équipements appariés)
    if learned is not None:
        target = learned.majority(learned.site, key)
        if target:
            return {
                "grandsite": FieldValue(target[0], LEVEL_HIST, label),
                "site": FieldValue(target[1], LEVEL_HIST, label),
            }

    # 3. Recherche catalogue (normalisée forte puis floue)
    cat_g, cat_s, cat_score, cat_kind = catalogue.resolve_site(
        grand or nom, nom or grand
    )

    # 4. Cascade catalogue
    if cat_g and cat_s:
        if cat_kind == "normalise":
            level = LEVEL_SITES if site_rms_found else LEVEL_NORM
            return {
                "grandsite": FieldValue(cat_g, level, label),
                "site": FieldValue(cat_s, level, label),
            }
        if cat_kind == "floue":
            cands = [f"{cat_g} / {cat_s} (score {cat_score:.2f})"]
            return {
                "grandsite": FieldValue(cat_g, LEVEL_FUZZY, label, cands),
                "site": FieldValue(cat_s, LEVEL_FUZZY, label, cands),
            }

    # ANOMALIE — jamais de valeur inventée dans la CMDB
    return {
        "grandsite": FieldValue("", LEVEL_UNKNOWN, label, [label]),
        "site": FieldValue("", LEVEL_UNKNOWN, label, [label]),
    }
# ---------------------------------------------------------------------- #
#  Résolution bâtiment / étage / bureau
# ---------------------------------------------------------------------- #
def _resolve_location_field(field, src, catalogue, mappings, learned,
                            client_key=""):
    """Traduit un segment de localisation client vers une valeur CMDB."""
    fv = FieldValue("", LEVEL_UNKNOWN, src)
    if not src:
        fv.is_empty = True
        return fv

    # 1. Correspondance validée
    if mappings is not None:
        val = mappings.validated_location(field, src)
        if val:
            return FieldValue(val, LEVEL_EXACT, src)

    # 2. Historique majoritaire (dictionnaire appris, d'abord par site)
    if learned is not None:
        val, _scope = learned.location_value(field, client_key, src)
        if val:
            cands = learned.location_candidates(field, client_key, src)
            return FieldValue(val, LEVEL_HIST, src, cands)

    # 3. Catalogue CMDB (exact / normalisé / flou avec garde-fous)
    val, score, kind = catalogue.resolve_location(field, src)
    if kind == "inconnu" or not val:
        fv.candidates = learned.location_candidates(
            field, client_key, src
        ) if learned else _location_candidates(catalogue, field, src)
        return fv
    level = LEVEL_NORM if kind in ("exact", "normalise") else LEVEL_FUZZY
    fv = FieldValue(val, level, src)
    if kind == "floue":
        fv.candidates.append(f"{val} (score {score:.2f})")
    return fv


def _location_candidates(catalogue, field, src):
    out = []
    bucket = catalogue.field_values.get(field)
    if not bucket:
        return out
    for key, vals in bucket.items():
        v = max(vals, key=lambda k: vals[k])
        out.append(f"{v} ({sum(vals.values())})")
    return out


def _translate_location(client, catalogue, mappings, learned):
    from .learning import client_site_key
    ctx = client_site_key(client)
    bat, etage, bureau = classify_residual(client_after_site(client))
    return {
        "batiment": _resolve_location_field(
            "batiment", bat, catalogue, mappings, learned, ctx
        ),
        "etage": _resolve_location_field(
            "etage", etage, catalogue, mappings, learned, ctx
        ),
        "bureau": _resolve_location_field(
            "bureau", bureau, catalogue, mappings, learned, ctx
        ),
    }


def classify_remaining(client):
    """Alias public : segments résiduels classés (bât, étage, bureau)."""
    return classify_residual(client_after_site(client))
# ---------------------------------------------------------------------- #
#  Résolution modèle -> (marque, modele)
# ---------------------------------------------------------------------- #
def _resolve_model(client, catalogue, mappings, learned):
    marque = FieldValue("", LEVEL_UNKNOWN, client.modele)
    modele = FieldValue("", LEVEL_UNKNOWN, client.modele)
    if not client.modele:
        marque.is_empty = modele.is_empty = True
        return marque, modele

    # 1. Correspondance validée
    if mappings is not None:
        found = mappings.validated_model(client.modele)
        if found:
            return (FieldValue(found[0], LEVEL_EXACT, client.modele),
                    FieldValue(found[1], LEVEL_EXACT, client.modele))

    # 2. Historique (majoritaire et non ambigu)
    if learned is not None:
        target = learned.majority(learned.model, client.modele)
        if target:
            return (FieldValue(target[0], LEVEL_HIST, client.modele),
                    FieldValue(target[1], LEVEL_HIST, client.modele))

    # 3. Référentiel CMDB (normalisation forte)
    mq, ml = catalogue.models.resolve(client.modele)
    if mq and ml:
        return (FieldValue(mq, LEVEL_NORM, client.modele),
                FieldValue(ml, LEVEL_NORM, client.modele))

    marque.value = _derive_brand(client.modele)
    marque.level = LEVEL_NORM if marque.value else LEVEL_UNKNOWN
    return marque, modele


def _derive_brand(model):
    text = normalize.remove_accents(model or "").upper()
    for brand in ("LEXMARK", "BROTHER", "HP", "CANON", "EPSON", "RICOH",
                  "XEROX", "OKI", "KYOCERA"):
        if text.startswith(brand):
            return "LX" if brand == "LEXMARK" else brand
    if text.startswith("LEX"):
        return "LX"
    return ""


# ---------------------------------------------------------------------- #
#  Résolution catégorie / type
# ---------------------------------------------------------------------- #
def _derive_categorie(client):
    text = normalize.remove_accents(
        f"{client.sous_type_modele} {client.type_modele}"
    ).upper()
    if any(k in text for k in ("COULEUR", "COLOR", "COUL")):
        return "COULEUR"
    if any(k in text for k in ("NOIR", "MONO", "BLANC")):
        return "MONOCHROME"
    return ""


def _resolve_categorie(client, mappings, learned):
    src = client.sous_type_modele or client.type_modele or ""
    if mappings is not None:
        val = mappings.validated_derived("categorie", src)
        if val:
            return FieldValue(val, LEVEL_EXACT, src)
    if learned is not None:
        val = learned.majority(learned.categorie, src)
        if val:
            return FieldValue(val, LEVEL_HIST, src)
    val = _derive_categorie(client)
    if val:
        return FieldValue(val, LEVEL_NORM, src)
    return FieldValue("", LEVEL_UNKNOWN, src)


def _resolve_type(client, mappings, learned):
    src = client.type_client or client.type_modele or ""
    if mappings is not None:
        val = mappings.validated_derived("type", src)
        if val:
            return FieldValue(val, LEVEL_EXACT, src)
    if learned is not None:
        val = learned.majority(learned.type, src)
        if val:
            return FieldValue(val, LEVEL_HIST, src)
    text = normalize.remove_accents(src).upper()
    if text.startswith("MFP"):
        return FieldValue("MFP", LEVEL_NORM, src)
    if text.startswith("IMP"):
        return FieldValue("IMP", LEVEL_NORM, src)
    return FieldValue("", LEVEL_UNKNOWN, src)


# ---------------------------------------------------------------------- #
#  Point d'entrée public
# ---------------------------------------------------------------------- #
def translate_device(client, catalogue, mappings=None, learned=None,
                     sites_rms=None):
    """Traduit une PrestaDevice en un objet structuré CMDB.

    Chaque champ est un FieldValue porteur de la valeur CMDB interprétée,
    de sa provenance et de son niveau de confiance.
    """
    t = TranslatedDevice(client)

    # ---- Identité (transparence) ----
    t.set("numeroetiquette", client.tatouage, LEVEL_EXACT, client.tatouage)
    t.set("numserie", client.num_serie, LEVEL_EXACT, client.num_serie)
    t.set("adresse_ip", normalize_ip(client.ip), LEVEL_EXACT, client.ip)
    t.set(
        "adresse_mac",
        normalize.norm_mac(client.mac) or client.mac,
        LEVEL_EXACT,
        client.mac,
    )

    site_rms = find_client_site(client, sites_rms)
    grand, nom = site_identity(client, site_rms)
    for name, fv in _resolve_site(
        grand, nom, catalogue, mappings, learned, site_rms is not None
    ).items():
        t.set(name, fv.value, fv.level, fv.client_raw, fv.candidates)

    for name, fv in _translate_location(
        client, catalogue, mappings, learned
    ).items():
        t.set(name, fv.value, fv.level, fv.client_raw, fv.candidates)

    marque, modele = _resolve_model(client, catalogue, mappings, learned)
    t.set("marque", marque.value, marque.level, marque.client_raw, marque.candidates)
    t.set("modele", modele.value, modele.level, modele.client_raw, modele.candidates)

    cat = _resolve_categorie(client, mappings, learned)
    t.set("categorie", cat.value, cat.level, cat.client_raw, cat.candidates)
    typ = _resolve_type(client, mappings, learned)
    t.set("type", typ.value, typ.level, typ.client_raw, typ.candidates)

    t.set("concatener", t.build_concatener(), LEVEL_NORM)
    return t


__all__ = (
    "TranslatedDevice", "FieldValue", "translate_device", "normalize_ip",
    "LEVEL_LABELS", "LEVEL_EXACT", "LEVEL_SITES", "LEVEL_HIST", "LEVEL_NORM",
    "LEVEL_FUZZY", "LEVEL_UNKNOWN", "LOCATION_FIELDS", "classify_remaining",
    "find_client_site",
)