"""Couche de NORMALISATION MÉTIER CMDB.

Le système comprend les différences textuelles ; ce module ajoute la
compréhension des CONVENTIONS DE NOTATION de notre CMDB. Pour chaque champ,
trois valeurs distinctes coexistent et ne doivent jamais être confondues :

    1. VALEUR SOURCE        la chaîne telle que fournie (client ou CMDB) ;
    2. VALEUR NORMALISÉE    clé de comparaison produite par normalize_<champ>() ;
    3. VALEUR CANONIQUE     la valeur à ÉCRIRE dans la CMDB, produite par
                            canonical_<champ>().

        SOURCE CLIENT -> NORMALISATION -> COMPARAISON
                      -> CANONISATION CMDB -> VALEUR À ÉCRIRE

Règle absolue (§16 et §28 de la spécification) : la CMDB est LA référence.
La normalisation ne sert JAMAIS à uniformiser la CMDB aux habitudes du
client ; c'est le fichier client qui est traduit vers notre nomenclature.

Chaque transformation est tracée : le nombre de fois où une convention a
évité une fausse modification est comptabilisé par ``record_convention`` et
remonté dans le rapport (§23), afin d'afficher « LE BLAYAIS → BLAYAIS : 150
équipements, aucune modification » plutôt que 150 anomalies.
"""

import re
import threading
from collections import Counter

from . import normalize

# --------------------------------------------------------------------------- #
#  1. ÉTATS SPÉCIAUX  (§3, §4)
# --------------------------------------------------------------------------- #
# Certaines valeurs de nos colonnes ne sont PAS des données ordinaires mais
# des ÉTATS MÉTIER. Le registre est extensible : il suffit d'ajouter un mot-clé
# dans STATE_ALIASES pour qu'il soit reconnu partout (comparaison, retraits,
# canonisation, affichage).
STATE_RETIRE = "retire"
STATE_HORS_RESEAU = "hors_reseau"
STATE_HORS_PARC = "hors_parc"
STATE_COLIMO = "colimo"
STATE_HORS_SERVICE = "hors_service"

#: Mot-clé (texte accent-insensible, séparateurs normalisés) -> état métier.
STATE_ALIASES = {
    "RETRAIT": STATE_RETIRE,
    "RETRAITES": STATE_RETIRE,
    "MACHINE RETIREE": STATE_RETIRE,
    "RETIRE": STATE_RETIRE,
    "RETIREES": STATE_RETIRE,
    "MATERIEL RETIRE": STATE_RETIRE,
    "HORS RESEAU": STATE_HORS_RESEAU,
    "HORS RESEAUX": STATE_HORS_RESEAU,
    "HORS RLE": STATE_HORS_RESEAU,
    "HORS LAN": STATE_HORS_RESEAU,
    "PAS DE RESEAU": STATE_HORS_RESEAU,
    "SANS ADRESSE": STATE_HORS_RESEAU,
    "HORS PARC RMS": STATE_HORS_PARC,
    "HORS PARC": STATE_HORS_PARC,
    "HORS CMDB": STATE_HORS_PARC,
    "COLIMO": STATE_COLIMO,
    "HORS SERVICE": STATE_HORS_SERVICE,
}

#: Libellés humains (jamais techniques) pour l'interface et les rapports.
STATE_LABELS = {
    STATE_RETIRE: "Matériel retiré",
    STATE_HORS_RESEAU: "Matériel hors réseau (sans IP exploitable)",
    STATE_HORS_PARC: "Matériel hors périmètre du parc",
    STATE_COLIMO: "Matériel sous gestion COLIMO",
    STATE_HORS_SERVICE: "Matériel hors service",
}

#: États signifiant « le matériel n'est plus un équipement actuellement
#: présent » : ces lignes ne doivent JAMAIS être proposées en retrait
#: (elles sont déjà retirées) (§3).
RETIRING_STATES = frozenset({STATE_RETIRE})

#: États « l'équipement existe mais n'a pas d'adresse exploitable » : ils ne
#: sont pas des adresses IP et ne se comparent donc pas comme telles (§2, §3).
NO_ADDRESS_STATES = frozenset(
    {STATE_HORS_RESEAU, STATE_HORS_PARC, STATE_COLIMO, STATE_HORS_SERVICE}
)

#: États dont la valeur n'est pas une adresse MAC exploitable.
NO_MAC_STATES = frozenset({STATE_RETIRE, STATE_HORS_RESEAU, STATE_HORS_PARC,
                           STATE_COLIMO, STATE_HORS_SERVICE})

#: Famille « retiré » : toutes les graphies (RETRAIT, RETRAITE, RETIRÉS,
#: RETIRÉES…) après suppression des accents. Les autres familles s'ajoutent
#: dans ``STATE_ALIASES`` ; une nouvelle valeur métier ne demande qu'une ligne.
_RETIRED_RE = re.compile(r"^(?:RETRAIT|RETIRE)(?:E?S?)$")


def _state_key(value):
    """Forme « clé d'état » : majuscules, sans accents, séparateurs -> espace."""
    text = normalize.remove_accents(value).upper()
    text = text.replace("/", " ").replace("-", " ").replace(".", " ")
    text = normalize.strip_spaces(text)
    return " ".join(text.split())


def special_state(value):
    """État métier de ``value``, ou None s'il s'agit d'une valeur ordinaire.

    La détection est volontairement stricte (valeur entière reconnue, famille
    « retiré », préfixe « COLIMO… ») : une IP, un nom de site ou un numéro de
    série ne peuvent jamais être pris pour un état.
    """
    key = _state_key(value)
    if not key:
        return None
    state = STATE_ALIASES.get(key)
    if state:
        return state
    if _RETIRED_RE.match(key):
        return STATE_RETIRE
    if key.startswith("COLIMO"):
        return STATE_COLIMO
    if key.startswith("HORS RESEAU"):
        return STATE_HORS_RESEAU
    if key.startswith("HORS PARC"):
        return STATE_HORS_PARC
    return None


def is_special_value(value, state=None):
    """True si ``value`` est un état métier (``RETRAIT``, ``RETIRÉ``…).

    ``state`` permet de tester un état précis : ``is_special_value(v,
    STATE_RETIRE)``. C'est LE point d'entrée demandé par la spécification (§4)
    : la comparaison doit CONNAÎTRE ces états au lieu de les traiter comme des
    données ordinaires.
    """
    found = special_state(value)
    if found is None:
        return False
    return True if state is None else found == state


def is_retired_state(value):
    """True si ``value`` déclare l'équipement comme DÉJÀ RETIRÉ (§3)."""
    return special_state(value) in RETIRING_STATES


def is_no_address_state(value):
    """True si ``value`` est un état « présent mais sans adresse »."""
    return special_state(value) in NO_ADDRESS_STATES


def state_label(state):
    """Libellé lisible d'un état métier."""
    return STATE_LABELS.get(state, state or "")



# --------------------------------------------------------------------------- #
#  2. JOURNAL DES CONVENTIONS APPLIQUÉES  (§23)
# --------------------------------------------------------------------------- #
# Une convention qui empêche une fausse modification doit être VISIBLE et
# GROUPÉE : « LE BLAYAIS -> BLAYAIS — 150 équipements — aucune modification ».
# Le journal est accumulé pendant un rapprochement (``collect_conventions``).

_CONVENTION_LOCK = threading.Lock()
_CONVENTION_SINK = None


def record_convention(field, source, normalized, kind="règle", level="sûre"):
    """Mémorise l'application d'une convention de normalisation.

    Hors d'une collecte active, l'appel est sans effet : la normalisation reste
    utilisable seule (tests, appels isolés) sans coût ni effet de bord.

    Les champs à forte cardinalité (IP, MAC, série) ne sont pas journalisés
    valeur par valeur : seule la convention est nommée
    (``CONVENTION_PLACEHOLDERS``), sinon le rapport compterait des milliers de
    groupes d'une ligne (§23).
    """
    sink = _CONVENTION_SINK
    if sink is None:
        return
    src = normalize.strip_spaces(source)
    dst = normalize.strip_spaces(normalized)
    if not src or src == dst:
        return
    placeholder = CONVENTION_PLACEHOLDERS.get(normalize.norm_compact(field))
    if placeholder:
        src, dst = placeholder
    with _CONVENTION_LOCK:
        key = (field, src, dst, kind, level)
        sink[key] = sink.get(key, 0) + 1


class collect_conventions:
    """Contexte : accumule les conventions appliquées pendant le bloc."""

    def __enter__(self):
        global _CONVENTION_SINK
        self.counts = Counter()
        self._previous = _CONVENTION_SINK
        _CONVENTION_SINK = self.counts
        return self.counts

    def __exit__(self, *exc):
        global _CONVENTION_SINK
        _CONVENTION_SINK = self._previous
        self._previous = None
        return False


def describe_conventions(counts, limit=None):
    """Textes lisibles, les plus fréquents d'abord (§23)."""
    out = []
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for (field, source, normalized, kind, level), n in items[:limit]:
        out.append(
            f"{field} : « {source} » → « {normalized} » — {kind} ({level})"
            f" — {n} équipement(s) — aucune modification"
        )
    return out


# --------------------------------------------------------------------------- #
#  3. DICTIONNAIRE D'ALIAS MÉTIER  (§7, §8, §24)
# --------------------------------------------------------------------------- #
#: Provenances d'une correspondance metier, classees par fiabilite (§24).
PROVENANCE_RULE = "regle"        # transformation sure, toujours applicable
PROVENANCE_HISTORY = "historique"  # constatee sur des equipements apparies
PROVENANCE_USER = "validee"      # choix explicite de l'utilisateur

#: Nombre d'observations minimo d'une correspondance historique pour qu'elle
#: soit appliquee sans validation humaine (§9, §24) : une seule occurrence peut
#: venir d'un appareil mal etique et mettrait deux sites differents en commun.
HISTORY_MIN_OBSERVATIONS = 3

class AliasDict:
    """Correspondances métier « valeur client -> valeur CMDB » par champ.

    Chaque entrée porte sa PROVENANCE et un nombre d'OBSERVATIONS, car un alias
    ne vaut que par la fiabilité de son origine (§24) :

        * ``règle``      : transformation sûre, toujours applicable ;
        * ``historique`` : correspondance constatée sur les équipements
          appariés (elle ne devient fiable que si elle est majoritaire) ;
        * ``validée``    : choix explicite de l'utilisateur (la plus forte).

    Les clés sont normalisées (casse, accents, espaces ignorés) ; les valeurs
    sont la graphie EXACTE de la CMDB, jamais retouchée (§16).
    """

    #: Provenances possibles, du plus fiable au moins fiable.
    RULE = PROVENANCE_RULE
    HISTORY = PROVENANCE_HISTORY
    USER = PROVENANCE_USER
    PROVENANCE_RANK = {PROVENANCE_USER: 0, PROVENANCE_HISTORY: 1,
                       PROVENANCE_RULE: 2}

    __slots__ = ("_by_field",)

    def __init__(self, data=None):
        self._by_field = {}
        if data:
            self.load(data)

    def add(self, field, source, canonical, provenance=PROVENANCE_HISTORY,
             count=1):
        """Ajoute ou renforce une correspondance source -> valeur CMDB."""
        src_key = normalize.norm_compact(source)
        target = normalize.strip_spaces(canonical)
        if not src_key or not target:
            return
        bucket = self._by_field.setdefault(field, {})
        entry = bucket.get(src_key)
        if entry is None:
            bucket[src_key] = {"source": normalize.strip_spaces(source),
                               "canonical": target,
                               "provenance": provenance,
                               "count": count}
            return
        entry["count"] += count
        # Une provenance plus fiable (validation utilisateur) prend le pas ;
        # à provenance égale on garde la graphie la plus observée.
        if self.PROVENANCE_RANK.get(provenance, 9) < \
                self.PROVENANCE_RANK.get(entry["provenance"], 9):
            entry["canonical"] = target
            entry["provenance"] = provenance

    def load(self, data):
        """Fusionne un dictionnaire serialise : {champ: {cle: entree}}."""
        for field, entries in (data or {}).items():
            if not isinstance(entries, dict):
                continue
            bucket = self._by_field.setdefault(field, {})
            for key, entry in entries.items():
                if isinstance(entry, dict) and entry.get("canonical"):
                    bucket.setdefault(str(key), {
                        "source": entry.get("source", key),
                        "canonical": normalize.strip_spaces(entry["canonical"]),
                        "provenance": entry.get("provenance", PROVENANCE_USER),
                        "count": int(entry.get("count", 1) or 1),
                    })

    def to_dict(self):
        """Representation serialisable (stockage local de l'utilisateur)."""
        return {f: dict(e) for f, e in self._by_field.items()}

    def get(self, field, value, min_count=1):
        """Valeur CMDB connue pour ``value``, sinon None.

        ``min_count`` permet d'exiger plusieurs observations avant de se fier
         a une correspondance historique (paragraphe 9).
        """
        entry = self.entry(field, value)
        if not entry or entry.get("count", 1) < min_count:
            return None
        return entry["canonical"]

    def entry(self, field, value):
        """Entree complete (source, canonical, provenance, count) ou None."""
        bucket = self._by_field.get(field)
        if not bucket:
            return None
        return bucket.get(normalize.norm_compact(value))

    def fields(self):
        return tuple(self._by_field)

    def items(self, field):
        return dict(self._by_field.get(field, {}))

    def __bool__(self):
        return any(self._by_field.values())

    def __contains__(self, field):
        return bool(self._by_field.get(field))


# --------------------------------------------------------------------------- #
#  4. ALIAS ACTIFS + NORMALISEURS PAR CHAMP  (paragraphe 17)
# --------------------------------------------------------------------------- #
# Table de correspondance utilisee pendant un rapprochement : elle est alimentee
# par les regles sures, par l'historique des equipements apparies (catalogue)
# puis par les validations de l'utilisateur (mapping_store).
_ALIASES = AliasDict()


def set_aliases(alias_dict):
    """Installe la table d'alias metier utilisee par les normaliseurs."""
    global _ALIASES
    _ALIASES = alias_dict if isinstance(alias_dict, AliasDict) else AliasDict(
        alias_dict)
    return _ALIASES


def active_aliases():
    """Table d'alias courant (lecture)."""
    return _ALIASES


#: Champs DÉRIVÉS, exclus du journal des conventions (§23) : « CONCATENER » est
#: recalculé par le moteur (site/bâtiment/étage/bureau), chaque ligne produit
#: donc une variante unique et illisible — le rapport y perdrait tout intérêt.
CONVENTION_EXCLUDED_FIELDS = frozenset({"concatener", "concatenation"})

#: Champs à FORTE CARDINALITÉ : journaliser chaque valeur produirait autant de
#: groupes que de machines (une adresse IP par ligne !). Seule la CONVENTION
#: est alors nommée, jamais la donnée : le compte reste exact et le rapport
#: reste lisible.
CONVENTION_PLACEHOLDERS = {
    "adresseip": ("une IP écrite sans schéma",
                  "la même IP au format de la CMDB"),
    "ip": ("une IP écrite sans schéma",
           "la même IP au format de la CMDB"),
    "adressemac": ("une MAC écrite autrement",
                   "la même MAC au format de la CMDB"),
    "mac": ("une MAC écrite autrement",
            "la même MAC au format de la CMDB"),
    "numserie": ("un numéro de série espacé/mélangé",
                 "le même numéro au format de la CMDB"),
    "numeroserie": ("un numéro de série espacé/mélangé",
                    "le même numéro au format de la CMDB"),
    "numeroetiquette": ("un tatouage espacé/mélangé",
                        "le même tatouage au format de la CMDB"),
}


class without_aliases:
    """Contexte : neutralise les correspondances apprises / validées (§24).

    Sert à mesurer la part RÉELLE d'un alias dans une équivalence : si deux
    valeurs sont déjà équivalentes sans lui, la convention appliquée est une
    simple règle de nommage et l'alias ne doit pas se l'attribuer (§23).
    """

    def __enter__(self):
        global _ALIASES
        self._previous = _ALIASES
        _ALIASES = AliasDict()
        return self

    def __exit__(self, *exc):
        global _ALIASES
        _ALIASES = self._previous
        return False


_KIND_LABELS = {
    PROVENANCE_RULE: "convention de nommage",
    PROVENANCE_HISTORY: "alias appris de la CMDB",
    PROVENANCE_USER: "correspondance validée",
}
_LEVEL_LABELS = {
    PROVENANCE_RULE: "règle sûre",
    PROVENANCE_HISTORY: "constatée",
    PROVENANCE_USER: "validée",
}


def _alias_entry(field, value, min_count=1):
    """Entrée de correspondance APPLICABLE pour ``value``, sinon None.

    Une correspondance seulement constatée une fois n'est PAS appliquée : elle
    pourrait confondre deux sites differents. Elle doit avoir ete observee au
    moins ``HISTORY_MIN_OBSERVATIONS`` fois (§9, §24).
    """
    entry = _ALIASES.entry(field, value) if _ALIASES else None
    if not entry or not _trustworthy(entry, min_count):
        return None
    return entry


def _alias_lookup(field, value, min_count=1):
    """Applique une correspondance connue, sinon renvoie la valeur d'origine.

    La valeur CMDB est restituee dans sa graphie EXACTE (jamais retouchee).
    La journalisation (§23) n'est PAS faite ici mais dans ``compare`` : c'est
    seulement là que l'on sait si la correspondance a réellement évité une
    fausse modification (et non constaté une égalité déjà acquise).
    """
    entry = _alias_entry(field, value, min_count)
    return entry["canonical"] if entry else value


def _trustworthy(entry, min_count=1):
    """True si une correspondance est assez documentee pour etre appliquee."""
    count = entry.get("count", 1)
    required = min_count
    if entry.get("provenance") == PROVENANCE_HISTORY:
        required = max(required, HISTORY_MIN_OBSERVATIONS)
    return count >= required


def _text_key(value):
    """Cle generique : casse, accents, ponctuation et espaces ignores."""
    return normalize.norm_text(value)


def normalize_site(value):
    """Cle de comparaison d'un site : nettoyage SUR, sans article perdu.

    Le nettoyage generique (casse, accents, espaces, ponctuation) est applique,
    puis les correspondances metier connues. Le cas « LE BLAYAIS » n'est pas
    resolu par une regle aveugle qui perdrait « LE HAVRE » : il n'est accepte
    que s'il a ete constate (alias historique) ou valide (§5, §8).
    """
    cleaned = _alias_lookup("site", value)
    return _text_key(cleaned)


def normalize_grandsite(value):
    """Cle de comparaison d'un grand site (mêmes regles que le site)."""
    cleaned = _alias_lookup("grandsite", value)
    return _text_key(cleaned)


def normalize_building(value):
    """Cle de comparaison d'un batiment (nettoyage generique + alias)."""
    cleaned = _alias_lookup("batiment", value)
    return _text_key(cleaned)


def normalize_office(value):
    """Cle de comparaison d'un bureau (nettoyage generique + alias).

    Un numero de bureau doit rester comparable tel quel : « L808 » et « 808 »
    ne sont PAS declares equivalents (difference reelle, pas un formatage).
    """
    cleaned = _alias_lookup("bureau", value)
    return _text_key(cleaned)


def normalize_model(value):
    """Cle de comparaison d'un modele : code CMDB + correspondances connues."""
    cleaned = _alias_lookup("modele", value)
    return normalize.norm_compact(cleaned)


def normalize_brand(value):
    cleaned = _alias_lookup("marque", value)
    return normalize.norm_compact(cleaned)


def normalize_category(value):
    """Cle de comparaison de categorie (COULEUR / MONOCHROME / N&B...)."""
    text = normalize.norm_compact(value)
    if text in NB_CATEGORY_SYNONYMS:
        return "monochrome"
    if text in COLOR_CATEGORY_SYNONYMS:
        return "couleur"
    return text


NB_CATEGORY_SYNONYMS = frozenset({
    "nb", "nbc", "nbelanc", "noiretblanc", "noirblanc", "noir", "monochrome",
    "noncouleur", "blackandwhite", "mono", "nampb",
})
COLOR_CATEGORY_SYNONYMS = frozenset({
    "couleur", "color", "colour", "quadri", "bichromie", "couleurrecto",
    "coul",
})


def normalize_device_type(value):
    """Cle de comparaison du type (CMDB : « IMP » / « MFP »).

    Les synonymes metier sont ramenes a la nomenclature CMDB ; un type inconnu
    reste compare tel quel (difference reelle a valider).
    """
    text = normalize.norm_compact(value)
    if text in MFP_TYPE_SYNONYMS:
        return "mfp"
    if text in IMP_TYPE_SYNONYMS:
        return "imp"
    return text


MFP_TYPE_SYNONYMS = frozenset({
    "mfp", "multifonction", "multifonctions", "imprimantemultifonction",
    "imprimantemultifonctions", "mf", "mfc", "toutenun", "copieur",
    "photocopieur",
})
IMP_TYPE_SYNONYMS = frozenset({
    "imp", "imprimante", "printer", "jba",
})


# -- Adresses --------------------------------------------------------------- #
#: Tout préfixe de schéma susceptible d'être collé devant une IP par les
#: outils de gestion (« https://IP », « ip://IP », « smb://IP »…).
_SCHEME_RE = re.compile(
    r"^(?:https?|s?ftp|smb|sdp|ipps?|ipp|ldaps?|telnet|ssh|wmi|rpc|mailto|tel)"
    r"://+",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_MAC_HEX_RE = re.compile(r"[^0-9A-Fa-f]")

#: Les clés de comparaison des états spéciaux sont préfixées pour ne JAMAIS
#: entrer en collision avec une adresse ou un nom ordinaire.
SPECIAL_KEY_PREFIX = "état:"
OTHER_KEY_PREFIX = "autre:"


def extract_ipv4(value):
    """Adresse IPv4 lisible dans ``value`` (préfixe, port et chemin ignorés).

    None si aucune adresse valide n'est trouvée : « 192.168.1.256 » ou
    « 10.0.0.1.4 » ne sont pas acceptées.
    """
    text = _SCHEME_RE.sub("", normalize.strip_spaces(normalize.to_text(value)))
    for candidate in _IPV4_RE.findall(text):
        octets = candidate.split(".")
        if all(int(o) <= 255 for o in octets):
            return candidate
    return None


def normalize_ip(value):
    """Cle de comparaison d'une adresse IP (§17, §2).

    La CMDB stocke « https://10.0.0.1 » alors que le fichier client fournit
    « 10.0.0.1 » : ces deux valeurs designent la MEME machine. Les états
    métier (« RETRAIT », « HORS RESEAU »…) ne sont pas des adresses : ils
    produisent une clé d'état, jamais comparée à une adresse.
    """
    raw = normalize.strip_spaces(normalize.to_text(value))
    if not raw:
        return ""
    state = special_state(raw)
    if state:
        return SPECIAL_KEY_PREFIX + state
    address = extract_ipv4(raw)
    if address:
        return address
    return OTHER_KEY_PREFIX + normalize.norm_compact(raw)


def is_ip_key(key):
    """True si une cle de comparaison designe une reelle adresse IPv4."""
    return bool(key) and not key.startswith((SPECIAL_KEY_PREFIX,
                                            OTHER_KEY_PREFIX))


def normalize_mac(value):
    """Cle de comparaison d'adresse MAC : « XX:XX:XX:XX:XX:XX » (§17).

    Les separateurs (: - . ou aucun) et la casse sont ignores ; une valeur qui
    n'est pas une MAC exploitable (etat special, texte, adresse tronquee) rend
    une cle non comparative plutot qu'une fausse equivalence.
    """
    raw = normalize.strip_spaces(normalize.to_text(value))
    if not raw:
        return ""
    state = special_state(raw)
    if state:
        return SPECIAL_KEY_PREFIX + state
    hexpart = _MAC_HEX_RE.sub("", raw).upper()
    if len(hexpart) == 12 and all(c in "0123456789ABCDEF" for c in hexpart):
        return ":".join(hexpart[i:i + 2] for i in range(0, 12, 2))
    return OTHER_KEY_PREFIX + normalize.norm_compact(raw)


def normalize_serial(value):
    """Cle de comparaison de numero de serie (§27 : espace en trop ignore)."""
    return normalize.norm_serial(value)


def normalize_tag(value):
    """Cle de comparaison de tatouage / numero d'etiquette."""
    return normalize.norm_tag(value)


def normalize_concatener(value):
    """Cle de comparaison de la cle concatenee de la CMDB.

    Cette colonne combine site / batiment / niveau / bureau : elle herite donc
    des memes conventions (accents, espaces, ponctuation) sans regle propre.
    """
    return normalize.norm_compact(_alias_lookup("concatener", value))


# -- Niveaux / étages -------------------------------------------------------#
#: Graphies du niveau zéro. Elles restent volontairement distinctes de la
#: notation altimétrique « 0 M » employée sur certains sites : les deux
#: conventions ne sont pas fusionnées par une règle aveugle, une équivalence
#: propre à un site devant passer par la table d'alias (§8).
FLOOR_GROUND_WORDS = frozenset({
    "RDC", "RDJ", "RD CHAUSSEE", "RDC CHAUSSEE", "RC", "REZ DE CHAUSSEE",
    "REZ DE CHAUSSE", "REZ DE CHAUSSES", "REZ", "PLAIN PIED", "NIVEAU 0",
    "ETAGE 0", "0 ETAGE", "ETAGE TERRE", "TERRE", "GROUND",
})

_FLOOR_UNDERGROUND_RE = re.compile(
    r"^(?:SS|SST|SOUS SOL|SOL|S)[\s.]*([0-9]{1,2})?$")
_FLOOR_PREFIXED_RE = re.compile(
    r"^(?:ETAGE|ETAGES|ETG|ET)[\s.]*"
    r"([0-9]{1,3}(?:[.,][0-9]{1,2})?)[\s]*([A-Z]?)$")
_FLOOR_NUMBERED_RE = re.compile(
    r"^([0-9]{1,3}(?:[.,][0-9]{1,2})?)"
    r"[\s]*(?:EME|ER|ERE|IEME|E|TH|ST|ND)?"
    r"[\s]*(?:ETAGE|ETAGES|ETG|ET|METRE|METRES|M)?"
    r"[\s]*([A-Z]?)$")
_FLOOR_LEVEL_RE = re.compile(r"^([0-9]{1,3})[\s]*M[\s]*([0-9]{1,2})$")


def _floor_number(text):
    """Rend un nombre de niveau comparable : « 13,50 » -> « 13.5 », « 01 » -> 1."""
    if "." in text:
        whole, _, frac = text.partition(".")
        frac = frac.rstrip("0")
        return f"{int(whole)}.{frac}" if frac else str(int(whole))
    return str(int(text))


def normalize_floor(value):
    """Cle de comparaison d'un niveau (§17).

    « 19 M », « 19M », « 19ème étage » et « ETAGE 19 » designent le MEME niveau
    et produisent la meme cle, ainsi que « 13,50 M » et « 13.50 M ». Casse,
    accents, ponctuation et espaces intercales chiffre/lettre sont ignores,
    mais deux niveaux numeriquement differents restent toujours differents.
    """
    raw = normalize.strip_spaces(normalize.to_text(value))
    if not raw:
        return ""
    state = special_state(raw)
    if state:
        return SPECIAL_KEY_PREFIX + state
    text = normalize.remove_accents(raw).upper()
    text = " ".join(text.replace("/", " ").replace("-", " ").replace(",", ".")
                    .split()).strip(".").strip()
    if not text:
        return ""
    if text in FLOOR_GROUND_WORDS:
        return "RDC"
    match = _FLOOR_UNDERGROUND_RE.match(text)
    if match:
        return "-" + str(int(match.group(1) or "1"))
    match = _FLOOR_LEVEL_RE.match(text)
    if match:                      # niveau intermediaire type « 15M02 »
        return "%sN%s" % (match.group(1), int(match.group(2)))
    for pattern in (_FLOOR_PREFIXED_RE, _FLOOR_NUMBERED_RE):
        match = pattern.match(text)
        if match:
            return _floor_number(match.group(1)) + (match.group(2) or "")
    return normalize.norm_text(raw)


# --------------------------------------------------------------------------- #
#  5. CANONISATION : LA VALEUR A ECRIRE DANS LA CMDB
# --------------------------------------------------------------------------- #
# Normaliser sert a COMPARER, canoniser sert a ECRIRE. La canonisation ne
# s'inspire jamais des habitudes du fichier client : elle remet la valeur dans
# LA convention de la CMDB, en prenant appui sur la valeur CMDB existante quand
# il y en a une (une nouvelle ligne recoit, elle, la convention de reference).

#: Prefixe place par la CMDB devant une adresse IP (convention interne).
IP_SCHEME = "https://"


def canonical_ip(value, reference=""):
    """Valeur d'IP au format CMDB : « https://10.0.0.1 » (§2).

    La CMDB est la reference : c'est son prefixe qui est applique, jamais
    l'inverse. Une valeur qui n'est pas une adresse (etat special « RETRAIT »,
    texte libre) n'est JAMAIS prefixee : ecrire « https://HORS RESEAU » serait
    une erreur, pas une mise en forme.

    ``reference`` (valeur CMDB existante) fixe la convention : sans schema dans
    la reference, l'adresse est ecrite nue.
    """
    raw = normalize.strip_spaces(normalize.to_text(value))
    address = extract_ipv4(raw) if raw else None
    if not raw or address is None or special_state(raw):
        return raw
    ref = normalize.strip_spaces(normalize.to_text(reference)).lower()
    if "://" in ref:
        return ref.split("://", 1)[0] + "://" + address
    if ref and not ref.startswith("http"):
        return address              # cette CMDB n'utilise pas de schema
    return IP_SCHEME + address


def canonical_mac(value):
    """Adresse MAC au format CMDB « XX:XX:XX:XX:XX:XX »."""
    key = normalize_mac(value)
    if not key or key.startswith((SPECIAL_KEY_PREFIX, OTHER_KEY_PREFIX)):
        return normalize.strip_spaces(normalize.to_text(value))
    return key


def canonical_floor(value, reference=""):
    """Niveau rendu dans la convention de la CMDB (« 19 M », « 1 ETG »...).

    La comparaison ignore l'unite ; l'ecriture, elle, doit la respecter : si la
    ligne CMDB porte « 19 M », la valeur client « 19M » s'ecrit « 19 M ».
    """
    raw = normalize.strip_spaces(normalize.to_text(value))
    if not raw or special_state(raw):
        return raw
    key = normalize_floor(raw)
    for sample in (normalize.strip_spaces(normalize.to_text(reference)), raw):
        if sample and not special_state(sample) and normalize_floor(sample) == key:
            return sample
    return raw


CANONICALIZERS = {
    "adresseip": canonical_ip,
    "ip": canonical_ip,
    "adressemac": canonical_mac,
    "mac": canonical_mac,
    "etage": canonical_floor,
    "floor": canonical_floor,
}


def canonical_for_field(field, value, reference=""):
    """Valeur a ecrire dans la CMDB pour ``field`` (colonne ou cle interne)."""
    canonical = CANONICALIZERS.get(normalize.norm_compact(field))
    if canonical is None:
        return normalize.strip_spaces(normalize.to_text(value))
    if canonical in (canonical_ip, canonical_floor):
        return canonical(value, reference)
    return canonical(value)


# --------------------------------------------------------------------------- #
#  6. COMPARAISON METIER : « compare(field, client, cmdb) »
# --------------------------------------------------------------------------- #
#: Champs pour lesquels une valeur peut être un état métier et non une donnée.
STATE_FIELDS = frozenset({
    "adresseip", "ip", "adressemac", "mac", "etage", "floor", "niveau",
    "statut", "etat",
})

#: Issues possibles d'une comparaison métier (§4, §29).
EQUIVALENT = "equivalent"        # différence de nommage/format uniquement
DIFFERENT = "different"          # vraie différence de valeur
SPECIAL_STATE = "etat_special"   # un état métier empêche la comparaison
REAPPEARANCE = "reapparition"    # CMDB « retiré » face à un équipement présent
COMPARISON_OUTCOMES = (EQUIVALENT, DIFFERENT, SPECIAL_STATE, REAPPEARANCE)


def normalizer_for(field):
    """Fonction de normalisation associée à une colonne CMDB (None sinon)."""
    return NORMALIZERS.get(normalize.norm_compact(field))


def normalize_value(field, value):
    """Cle de comparaison de ``value`` pour le champ ``field``.

    Un champ sans normaliseur propre retombe sur la cle generique (accents,
    casse, espaces) : la comparaison reste possible sans regle métier.
    """
    normalizer = normalizer_for(field)
    if normalizer is None:
        return normalize.norm_text(value)
    return normalizer(value)


class Comparison:
    """Resultat d'une comparaison metier : équivalence + explication."""

    __slots__ = ("field", "status", "cmdb_value", "client_value",
                 "cmdb_key", "client_key", "cmdb_state", "client_state")

    def __init__(self, field, status, cmdb_value, client_value,
                 cmdb_key="", client_key="", cmdb_state=None,
                 client_state=None):
        self.field = field
        self.status = status
        self.cmdb_value = cmdb_value
        self.client_value = client_value
        self.cmdb_key = cmdb_key
        self.client_key = client_key
        self.cmdb_state = cmdb_state
        self.client_state = client_state

    @property
    def equivalent(self):
        """True si la différence n'est que du ressort du nommage/formatage."""
        return self.status == EQUIVALENT

    @property
    def comparable(self):
        """False quand un état métier empêche toute comparaison ordinaire."""
        return self.status != SPECIAL_STATE

    @property
    def special(self):
        """True si la comparaison bute sur un état métier (§3)."""
        return self.status in (SPECIAL_STATE, REAPPEARANCE)

    @property
    def kind(self):
        """Type d'anomalie à remonter, ou None (aucune decision humaine)."""
        if self.status == REAPPEARANCE:
            return REAPPEARANCE
        if self.status == SPECIAL_STATE:
            return SPECIAL_STATE
        return None

    def explain(self):
        """Phrase courte expliquant la décision (journal, rapport, UI)."""
        if self.status == REAPPEARANCE:
            return ("déjà %s dans la CMDB alors que le fichier client le "
                    "signale présent" % state_label(self.cmdb_state).lower())
        if self.status == SPECIAL_STATE:
            left = state_label(self.cmdb_state) or "valeur ordinaire"
            right = state_label(self.client_state) or "valeur ordinaire"
            return "état CMDB (%s) non comparable avec l'état client (%s)" % (
                left, right)
        if self.status == EQUIVALENT:
            return "équivalent après application des conventions CMDB"
        return "vraie différence de valeur"


def _record_equivalence(field, client_value, cmdb_value):
    """Journalise une convention qui a évité une fausse modification (§23).

    Appelée UNIQUEMENT lorsque les deux valeurs diffèrent BRUTES mais que la
    convention du champ les déclare équivalentes. Le rapport affiche alors
    « LE BLAYAIS → BLAYAIS : 150 équipements, aucune modification » au lieu de
    150 anomalies — et l'affirmation « aucune modification » est exacte,
    puisqu'aucune modification n'est effectivement proposée.

    La nature de la convention est reprise de la correspondance la plus fiable
    qui a servi (validée > constatée > règle), pour que l'utilisateur sache
    distinguer un simple formatage d'un alias appris (§24).
    """
    source = normalize.strip_spaces(normalize.to_text(client_value))
    target = normalize.strip_spaces(normalize.to_text(cmdb_value))
    if not source or not target or source == target:
        return
    key = normalize.norm_compact(field)
    if key in CONVENTION_EXCLUDED_FIELDS:
        return
    entries = [e for e in (_alias_entry(key, client_value),
                           _alias_entry(key, cmdb_value)) if e]
    # La correspondance n'est créditée que si elle a ÉTÉ DÉCISIVE : deux valeurs
    # déjà équivalentes sans elle relèvent de la règle de nommage du champ.
    if entries:
        with without_aliases():
            rule_cmdb = normalize_value(field, cmdb_value)
            rule_client = normalize_value(field, client_value)
        if rule_cmdb and rule_client and rule_cmdb == rule_client:
            entries = []
    if entries:
        best = min(entries, key=lambda e: AliasDict.PROVENANCE_RANK.get(
            e.get("provenance"), 9))
        provenance = best.get("provenance")
    else:
        provenance = PROVENANCE_RULE
    record_convention(
        field, source, target,
        kind=_KIND_LABELS.get(provenance, "convention de champ"),
        level=_LEVEL_LABELS.get(provenance, "sûre"),
    )


def compare(field, client_value, cmdb_value):
    """Compare une valeur client à une valeur CMDB selon les conventions.

    Point d'entrée unique demandé par la spécification (§29) : le moteur ne
    compare plus des chaînes entre elles, il interroge la convention du champ.
    Renvoie une ``Comparison`` (jamais un simple booléen) pour que l'appelant
    puisse distinguer « identique », « vraiment différent » et « état spécial ».
    """
    key_field = normalize.norm_compact(field)
    cmdb_key = normalize_value(field, cmdb_value)
    client_key = normalize_value(field, client_value)
    if cmdb_key and client_key and cmdb_key == client_key:
        _record_equivalence(field, client_value, cmdb_value)
        return Comparison(field, EQUIVALENT, cmdb_value, client_value,
                          cmdb_key, client_key)
    if key_field not in STATE_FIELDS:
        if not cmdb_key and not client_key:
            return Comparison(field, EQUIVALENT, cmdb_value, client_value,
                              cmdb_key, client_key)
        return Comparison(field, DIFFERENT, cmdb_value, client_value,
                          cmdb_key, client_key)
    cmdb_state = special_state(cmdb_value)
    client_state = special_state(client_value)
    if cmdb_state and client_state and cmdb_state == client_state:
        _record_equivalence(field, client_value, cmdb_value)
        return Comparison(field, EQUIVALENT, cmdb_value, client_value,
                          cmdb_key, client_key, cmdb_state, client_state)
    if cmdb_state and cmdb_state in RETIRING_STATES:
        # La CMDB déclare l'équipement retiré : toute présence client est une
        # réapparition, jamais une mise à jour (« RETRAIT » n'est pas une IP).
        return Comparison(field, REAPPEARANCE, cmdb_value, client_value,
                          cmdb_key, client_key, cmdb_state, client_state)
    if cmdb_state or client_state:
        # Un état métier (HORS RESEAU, COLIMO, HORS PARC…) en face d'une
        # adresse réelle n'est pas une donnée à écraser : décision humaine.
        return Comparison(field, SPECIAL_STATE, cmdb_value, client_value,
                          cmdb_key, client_key, cmdb_state, client_state)
    if not cmdb_key and not client_key:
        return Comparison(field, EQUIVALENT, cmdb_value, client_value,
                          cmdb_key, client_key, cmdb_state, client_state)
    return Comparison(field, DIFFERENT, cmdb_value, client_value,
                      cmdb_key, client_key, cmdb_state, client_state)


# --------------------------------------------------------------------------- #
#  7. TABLE DE CORRESPONDANCE CHAMP -> NORMALISEUR
# --------------------------------------------------------------------------- #
#: Une regle par champ, et seulement la ou notre metier a une convention.
NORMALIZERS = {
    "grandsite": normalize_grandsite,
    "site": normalize_site,
    "batiment": normalize_building,
    "etage": normalize_floor,
    "bureau": normalize_office,
    "concatener": normalize_concatener,
    "categorie": normalize_category,
    "type": normalize_device_type,
    "typecol": normalize_device_type,
    "marque": normalize_brand,
    "modele": normalize_model,
    "numserie": normalize_serial,
    "numeroserie": normalize_serial,
    "numeroetiquette": normalize_tag,
    "adresseip": normalize_ip,
    "ip": normalize_ip,
    "adressemac": normalize_mac,
    "mac": normalize_mac,
}

#: Champs susceptible de porter une correspondance métier apprenable.
ALIAS_FIELDS = ("site", "grandsite", "batiment", "bureau", "modele",
                "categorie", "marque", "etage")

__all__ = (
    "AliasDict", "Comparison", "COMPARISON_OUTCOMES", "CANONICALIZERS",
    "EQUIVALENT", "DIFFERENT", "SPECIAL_STATE", "REAPPEARANCE",
    "PROVENANCE_RULE", "PROVENANCE_HISTORY", "PROVENANCE_USER",
    "HISTORY_MIN_OBSERVATIONS", "STATE_FIELDS",
    "STATE_RETIRE", "STATE_HORS_RESEAU", "STATE_HORS_PARC", "STATE_COLIMO",
    "STATE_HORS_SERVICE", "STATE_ALIASES", "STATE_LABELS",
    "SPECIAL_KEY_PREFIX", "OTHER_KEY_PREFIX",
    "NORMALIZERS", "ALIAS_FIELDS",
    "CONVENTION_EXCLUDED_FIELDS", "CONVENTION_PLACEHOLDERS",
    "IP_SCHEME", "special_state", "is_special_value", "is_retired_state",
    "is_no_address_state", "state_label", "set_aliases", "active_aliases",
    "without_aliases",
    "record_convention", "collect_conventions", "describe_conventions",
    "normalize_value", "normalizer_for", "compare",
    "normalize_ip", "normalize_mac", "normalize_floor", "normalize_site",
    "normalize_grandsite", "normalize_building", "normalize_office",
    "normalize_model", "normalize_brand", "normalize_category",
    "normalize_device_type", "normalize_serial", "normalize_tag",
    "normalize_concatener", "extract_ipv4", "is_ip_key",
    "canonical_ip", "canonical_mac", "canonical_floor", "canonical_for_field",
)
