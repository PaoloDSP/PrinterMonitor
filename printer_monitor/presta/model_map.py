"""Conversion des modeles de l'inventaire externe vers (Marque, Modele) CMDB.

La CMDB utilise une nomenclature simplifiee qui separe la Marque du Modele
(ex. LX / X921, LX / MS521). Cette table est construite automatiquement a
partir du fichier CMDB reel : chaque nom de modele externe est rattache au
code CMDB le plus pertinent (regles deterministes, sans IA).
"""

import re

from . import normalize

# Marques externes observees -> marque interne CMDB.
_BRAND_ALIASES = {
    "lexmark": "LX",
    "brother": "BROTHER",
    "epson": "EPSON",
    "canon": "CANON",
    "hp": "HP",
    "kyocera": "KYOCERA",
    "xerox": "XEROX",
    "ricoh": "RICOH",
}

# Suffixes de modele a ignorer (variantes constructeur).
_TAIL_SUFFIXES = (
    "dtex", "dte", "dtn", "dtd", "dtw", "dtf", "dtwf", "dea", "de", "dn",
    "dwf", "twf", "wf", "wt", "st", "tf", "dt", "se", "aic", "gie", "a",
    "b", "c", "d", "e", "f", "g", "h", "n",
)

_WORD_RE = re.compile(r"[a-z0-9]+")


class ModelMapper:
    """Convertit un nom de modele externe vers (marque, modele) CMDB."""

    def __init__(self, cmdb_models=None):
        # index : code normalise -> (marque, modele) premiere rencontre
        self._ref = {}
        self._codes = ()
        if cmdb_models:
            self.load_cmdb(cmdb_models)

    # ------------------------------------------------------------------ #
    # Construction de la reference depuis la CMDB reelle
    # ------------------------------------------------------------------ #
    def load_cmdb(self, models):
        """models : iterable de (marque, modele) presents dans la CMDB.

        La reference retient la graphie la plus frequente (ex. X921 vs x921)
        pour chaque code, afin de ne jamais imposer une casse minoritaire.
        """
        per_code = {}
        for marque, modele in models:
            marque = normalize.strip_spaces(str(marque or ""))
            modele = normalize.strip_spaces(str(modele or ""))
            if not marque or not modele:
                continue
            code = normalize.remove_accents(modele).casefold()
            if not code:
                continue
            bucket = per_code.setdefault(code, {})
            key = (marque.upper(), modele.upper())
            bucket[key] = bucket.get(key, 0) + 1
        self._ref = {}
        for code, bucket in per_code.items():
            best_key = max(bucket, key=bucket.get)
            self._ref[code] = best_key
        self._codes = sorted(self._ref, key=len, reverse=True)

    def resolve(self, external_modele):
        """Retourne (marque, modele) internes ou (None, None).

        Exemples reels :
            "LEXMARK MS521DN"            -> ("LX", "MS521")
            "LEXMARK LEX-CC2-CX921DE"    -> ("LX", "X921")
            "LEXMARK MS823DN"            -> ("LX", "MS823")
        """
        text = normalize.remove_accents(str(external_modele or "")).upper().strip()
        if not text:
            return None, None

        lower = text.casefold()
        matched_brand = False
        for alias, _internal in _BRAND_ALIASES.items():
            if lower.startswith(alias):
                text = text[len(alias):].strip()
                matched_brand = True
                break
        if not matched_brand and lower.startswith("lex"):
            text = text[3:].strip()

        # Comparaison insensible a la casse (le code sera recherche en minuscules)
        text = text.casefold()

        code = self._find_code(text)
        if code:
            candidate = self._ref.get(code)
            if candidate:
                return candidate

        full = self._strip_tails(text)
        if full in self._ref:
            return self._ref[full]

        # Repli : familles Brother MFC-J + 4 chiffres (ex. MFC-J6955DW)
        m = re.search(r'mfc[- ]?j?([0-9]{4})', text)
        if m:
            cand = "mfc-j" + m.group(1)
            if cand in self._ref:
                return self._ref[cand]
        return None, None

    @property
    def nbr_ref(self):
        return len(self._ref)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _find_code(self, text):
        """Recherche le code CMDB le plus long contenu dans le modele externe.

        La recherche travaille sur deux formes :
        * forme nettoyee (suffixes constructeur retires) ;
        * forme compacte (sans separteurs) pour les modeles type
          'MFC-J6955DW' dont le code interne est 'MFC-J6955'.
        """
        cleaned = self._strip_tails(text)
        tokens = [p for p in _WORD_RE.findall(cleaned) if len(p) >= 2]
        compact = "".join(tokens)
        for code in self._codes:
            if len(code) < 2:
                continue
            if code in cleaned or code in tokens:
                return code
            # Code avec separateurs (ex. MFC-J6955) : comparaison compacte
            ccomp = re.sub(r'[^a-z0-9]', '', code)
            if ccomp and len(ccomp) >= 4 and (ccomp in compact or ccomp in cleaned):
                return code
        return None

    @staticmethod
    def _strip_tails(value):
        """Retire les suffixes constructeur de chaque token (MS823DN -> MS823)."""
        pieces = _WORD_RE.findall(value or "")
        out = []
        for piece in pieces:
            p = piece
            changed = True
            while changed and len(p) >= 3:
                changed = False
                for suffix in _TAIL_SUFFIXES:
                    if p.casefold().endswith(suffix) and len(p) - len(suffix) >= 2:
                        p = p[:len(p) - len(suffix)]
                        changed = True
                        break
            if p and p not in out:
                out.append(p)
        return " ".join(out)


__all__ = ("ModelMapper",)