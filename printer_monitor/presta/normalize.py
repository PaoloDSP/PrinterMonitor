"""Couche de normalisation pour la comparaison CMDB <-> Presta RMS.

IMPORTANT : la normalisation sert UNIQUEMENT à comparer les données.
Elle ne modifie jamais les valeurs originales affichées ou stockées dans la
CMDB. Chaque fonction renvoie une forme « clé de comparaison » dérivée de la
valeur d'origine.
"""

import re
import unicodedata

# Espaces (y compris insécables) et caractères invisibles courants.
_WHITESPACE_RE = re.compile(r"[\s\u00a0\u202f]+")
# Caractères non alphanumériques (pour les identifiants : séries, étiquettes).
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")
# Séparateurs d'adresses MAC.
_MAC_SEP_RE = re.compile(r"[:\-.]")
# Séparateurs de site / localisation (« / », « - », « , », « . »).
_NAME_SEP_RE = re.compile(r"[/,\-.]+")
# Espace intercalé entre un chiffre et une lettre (« 19 M » == « 19M »).
_DIGIT_LETTER_WS_RE = re.compile(
    r"(?<=\d)\s+(?=[A-Za-z])|(?<=[A-Za-z])\s+(?=\d)"
)
# Déterminants français ignorés en tête d'un nom de site / établissement.
_SITE_ARTICLE_RE = re.compile(
    r"^(?:(?:le|la|les|du|de|des|l|d)\s+|(?:l|d)[’']\s*)"
)
# Marqueurs « machine déjà retirée » (colonne AdresseIP de la CMDB).
_RETRAIT_RE = re.compile(r"^retrait(?:e|es|s)?$")
# Niveaux (étage) : RDC, RDJ, SOUS-SOL, SS1, 1 ETG, 2EME, 0 M, 19 M, 24 M…
_FLOOR_RE = re.compile(
    r"^(?:\d{1,3}[.,]?\d*\s*(?:ETG|EME|ER|[Mm])|"
    r"RDC|RDJ|[R]-?1|SOUS-SOL|SOUS SOL|SS\s*\d*|SS)$",
    re.IGNORECASE,
)


def is_floor(value):
    """True si le segment ressemble à un niveau / étage (RDC, 1 ETG, 19 M…)."""
    text = strip_spaces(to_text(value))
    if not text:
        return False
    return bool(_FLOOR_RE.match(text))


def norm_compact(value):
    """Chaîne « compacte » pour comparaison : minuscules, sans accents/espaces.

    Exemple : « LOCAUX ÉLECTRIQUES 1-2 » -> « locauxelectriques1-2 ».
    Sert aux correspondances normalisées fortes.
    """
    return re.sub(r"[^a-z0-9]", "", remove_accents(value).casefold())


def to_text(value):
    """Convertit une cellule Excel (ou toute valeur) en texte propre.

    Les dates/objets sont convertis via str(). None devient chaîne vide.
    """
    if value is None:
        return ""
    return str(value)


def strip_spaces(value):
    """Supprime espaces de début/fin et réduit les espaces multiples."""
    text = to_text(value)
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_ip(value):
    """Normalise une adresse IP pour la comparaison (schéma http(s) retiré).

    Retire le préfixe « http:// » / « https:// » et le slash final :
    « https://192.168.1.10 » et « 192.168.1.10 » deviennent équivalents.
    """
    text = strip_spaces(to_text(value))
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return text.strip().rstrip("/")


def remove_accents(value):
    """Retire les accents/diacritiques (NFD puis suppression des marques)."""
    text = strip_spaces(value)
    decomposed = unicodedata.normalize("NFD", text)
    without = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    # Ligatures courantes du français.
    replacements = {"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"}
    for src, dst in replacements.items():
        without = without.replace(src, dst)
    return without


def _special_state(value):
    """État métier reconnu par la couche de conventions, sinon None.

    L'import est volontairement LOCAL : ``conventions`` s'appuie sur ce module,
    un import en tête de fichier créerait un cycle. La normalisation reste donc
    utilisable seule, et les états métier (« RETRAIT », « RETIRE », « HORS
    RESEAU », « COLIMO »…) sont reconnus partout via un seul registre (§4).
    """
    from . import conventions
    return conventions.special_state(value)


def is_retrait(value):
    """True si ``value`` est un marqueur « machine déjà retirée » (RETRAIT).

    Toutes les graphies réelles sont reconnues (« RETRAIT », « RETIRE »,
    « RETIRÉ », « MATERIEL RETIRE »…) via le registre d'états de
    ``conventions`` : la CMDB contient « RETIRE » bien plus souvent que
    « RETRAIT », et un marqueur manqué se transforme en fausse modification
    d'adresse IP (§3).
    """
    from . import conventions
    if conventions.is_retired_state(value):
        return True
    # Repli : le format historique « retrait(e|es|s) » reste accepté même si le
    # registre évolue (compatibilité stricte avec les versions précédentes).
    return bool(
        _RETRAIT_RE.match(remove_accents(to_text(value)).strip().casefold())
    )


def norm_key(value):
    """Clé générique de comparaison : sans accents, minuscules, espaces réduits."""
    return remove_accents(value).casefold()


def norm_text(value):
    """Clé de comparaison pour les champs descriptifs / localisation.

    Ignore les différences de formatage qui ne changent pas la
    signification : casse, accents, espaces superflus, espaces intercalés
    chiffre-lettre (« 19 M » == « 19M ») et séparateurs de site
    (« / », « - », « , », « . ») traités comme de simples espaces.
    """
    text = remove_accents(value).casefold()
    text = _NAME_SEP_RE.sub(" ", text)
    text = _DIGIT_LETTER_WS_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def norm_name(value):
    """Clé de comparaison des NOMS DE SITES / ÉTABLISSEMENTS.

    Reprend ``norm_text`` et ignore en plus les déterminants français en
    tête (« LE BLAYAIS » == « BLAYAIS », « L'ISLE » == « ISLE ») : une
    approche générale pour toutes les variations de nommage connues, sans
    règle spécifique par site.
    """
    text = norm_text(value)
    while True:
        stripped = _SITE_ARTICLE_RE.sub("", text, count=1)
        if stripped == text:
            return stripped
        text = stripped


def norm_id(value):
    """Normalise un identifiant matériel (n° de série, n° d'étiquette/tatouage).

    Conserve uniquement les caractères alphanumériques en majuscules :
    supprime tirets, espaces, points, etc. Permet de rapprocher par exemple
    « 50303495114-DN » et « 50303495114DN ».
    """
    text = remove_accents(to_text(value)).upper()
    return _NON_ALNUM_RE.sub("", text)


def norm_tag(value):
    """Normalise un numéro d'étiquette / tatouage (ex. DSP0942345)."""
    return norm_id(value)


def norm_serial(value):
    """Normalise un numéro de série constructeur."""
    return norm_id(value)


def norm_mac(value):
    """Normalise une adresse MAC vers la forme canonique AA:BB:CC:DD:EE:FF.

    Renvoie une chaîne vide si l'adresse est absente ou manifestement invalide.
    """
    hexpart = _MAC_SEP_RE.sub("", strip_spaces(to_text(value))).upper()
    if len(hexpart) == 12 and all(c in "0123456789ABCDEF" for c in hexpart):
        return ":".join(hexpart[i:i + 2] for i in range(0, 12, 2))
    return ""


def is_empty(value):
    """True si la valeur est vide / ne contient que des espaces / placeholders."""
    text = strip_spaces(to_text(value))
    if not text:
        return True
    return text.casefold() in ("-", "--", "?", "n/a", "na", "none", "null", "#n/a")


def split_location_path(location):
    """Découpe un chemin de localisation Presta RMS en segments propres.

    Exemple :
        "/26 - DROME/SAINT PAUL TROIS CHATEAUX/TRICASTIN/BAT A/RDC/"
        -> ["26 - DROME", "SAINT PAUL TROIS CHATEAUX", "TRICASTIN", "BAT A", "RDC"]

    Gère aussi les localisations sans slash (« SITE1/BAT2 ») et les séparateurs
    antislash éventuels.
    """
    text = strip_spaces(to_text(location))
    if not text:
        return []
    text = text.replace("\\", "/")
    segments = [strip_spaces(seg) for seg in text.split("/")]
    return [seg for seg in segments if seg]


def location_site_key(location):
    """Extrait la « clé de site » d'une localisation Presta RMS.

    La clé correspond aux premiers segments significatifs du chemin
    (département + ville + site), c'est-à-dire tout ce qui précède le bâtiment,
    l'étage ou le bureau. C'est cette clé qui alimente la table de
    correspondance des sites : elle est stable même quand la partie fine
    (bâtiment/bureau) change.

    Heuristique : on s'arrête au premier segment qui ressemble à un bâtiment /
    étage / bureau (préfixes BAT, BÂT, ATA, BAN, étage type « RDC », « 1 ETG »,
    bureau type « CA223 », etc.). Si aucun segment de ce type n'est trouvé, la
    clé est constituée des 3 premiers segments (département/ville/site).
    """
    segments = split_location_path(location)
    if not segments:
        return ""
    stop_prefixes = ("BAT", "BÂT", "ATA", "BAN", "BUREAU", "LOCAUX")
    key_segments = []
    for seg in segments:
        upper = remove_accents(seg).upper()
        first_token = upper.split(" ")[0] if upper else ""
        if (
            first_token.startswith(stop_prefixes)
            or re.fullmatch(r"(?:RDC|RDJ|\d+\s*ETG|ETG\s*\d+|SOL)", first_token or "")
            or re.fullmatch(r"[A-Z]{1,3}\d{2,}", first_token or "")
        ):
            break
        key_segments.append(seg)
        if len(key_segments) >= 5:
            break
    if not key_segments:
        # Cas dégénéré : le premier segment ressemblait déjà à un bâtiment ;
        # on garde au moins le département pour éviter une clé vide.
        key_segments = segments[:1]
    return "/".join(key_segments)


def values_equal(cmdb_value, presta_value, mode="norm"):
    """Compare deux valeurs (CMDB vs fichier client) APRÈS normalisation.

    C'est le point d'entrée UNIQUE de la comparaison champ par champ : tous
    les modes de comparaison (texte, nom de site, compact, série, IP, MAC…)
    passent par ici avec une norme cohérente. Les valeurs originales des
    deux fichiers ne sont jamais modifiées — la normalisation sert
    uniquement à décider si deux valeurs sont équivalentes.

    Renvoie True si la différence n'est que de formatage / nommage
    (y compris deux valeurs vides), False pour une vraie différence.
    """
    if mode in ("ip", "mac"):
        # Les états métier (« RETRAIT », « RETIRE », « HORS RESEAU », « COLIMO »)
        # ne sont PAS des données : les comparer comme des adresses produit des
        # écarts fantômes. Aucune écriture ne doit jamais remplacer un état par
        # une valeur, ni l'inverse : la décision revient à
        # ``conventions.compare``, qui nomme explicitement l'anomalie (§3, §4).
        if _special_state(cmdb_value) or _special_state(presta_value):
            return True
    if mode == "ip":
        key = normalize_ip
    elif mode == "name":
        key = norm_name
    elif mode == "text":
        key = norm_text
    elif mode == "compact":
        key = norm_compact
    elif mode == "serial":
        key = norm_serial
    elif mode == "mac":
        key = norm_mac
    else:
        key = norm_key
    a = key(cmdb_value)
    b = key(presta_value)
    if not a and not b:
        return True
    return a == b


def format_ip_like(cmdb_value, new_value):
    """Conserve le format d'IP de la CMDB pour une valeur réellement modifiée.

    La CMDB stocke « https://IP » alors que le fichier client fournit
    souvent « IP » sans schéma. Lors d'une vraie modification, la
    proposition doit garder le préfixe de la valeur CMDB existante pour ne
    pas déformer la norme interne. La valeur brute du fichier client n'est
    jamais modifiée (elle sert uniquement à la comparaison et au calcul).

    La règle elle-même vit dans ``conventions.canonical_ip`` (§2) : elle y est
    partagée avec les AJOUTS, qui doivent eux aussi respecter la convention
    d'écriture de la CMDB — l'ancien code n'habillait que les mises à jour.
    """
    from . import conventions
    return conventions.canonical_ip(new_value, reference=cmdb_value)