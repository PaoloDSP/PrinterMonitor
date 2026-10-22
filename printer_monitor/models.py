"""Modèles de données du logiciel Printer Monitor.

Ce module contient les classes de données pures : PrinterSupply (consommable)
et PrinterDevice (imprimante). La logique SNMP elle-même est dans snmp.py.
"""

from .utils import DRUM_COLOR_ORDER, is_kyocera_black_toner_ref


class PrinterSupply:
    def __init__(self, index, description, level, max_capacity, supply_type):
        self.index = index
        self.description = description or ""
        self.level = self._to_int(level)
        self.max_capacity = self._to_int(max_capacity)
        self.supply_type = supply_type
        self._category_cache = None

    @staticmethod
    def _to_int(value):
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:
            return None

    def is_valid_level(self):
        """Return True when the supply level is a plausible non-negative value
        and (if max_capacity is present) does not exceed it."""
        if self.level is None:
            return False
        try:
            if self.level < 0:
                return False
            if self.max_capacity is not None:
                if self.max_capacity <= 0:
                    return False
                if self.level > self.max_capacity:
                    return False
            return True
        except Exception:
            return False

    @property
    def percentage(self):
        # Only compute percentage when level and max_capacity look valid
        if not self.is_valid_level():
            return None
        if self.max_capacity and self.max_capacity > 0:
            try:
                return int(round((self.level / self.max_capacity) * 100))
            except Exception:
                return None
        return None

    def is_receptacle(self):
        """Return True for waste toner bottles / collection receptacles whose
        displayed percentage must be inverted (remaining capacity). Must be
        checked independently of 'toner' since these descriptions often contain
        the word 'toner' (e.g. 'Waste Toner Container')."""
        text = f"{self.description} {self.supply_type or ''}".strip().lower()
        if self.supply_type and "waste" in str(self.supply_type).lower():
            return True
        return any(
            k in text
            for k in (
                "waste", "bottle", "récup", "recup", "collect",
                "récupération", "récupérateur", "receptacle", "réceptacle",
            )
        )

    @property
    def display_percentage(self):
        """Percentage to display for this supply. TEST MODE (retest du sens) :
        l'inversion pour les réceptacles est temporairement désactivée, le
        pourcentage affiché correspond directement au niveau brut SNMP
        (0% = bouteille vide, 100% = bouteille pleine). Toners/cartouches/drums
        inchangés (niveau brut)."""
        return self.percentage

    # Synonymous labels used by Lexmark to designate the drum / photoconductor,
    # depending on the model generation and the printer language. Recent models
    # (CX9xx, CS9xx, MX8xx, MS8xx, etc.) no longer always say "Drum" or
    # "Photoconductor"; they may expose the imaging unit as:
    #   EN: Imaging Unit, Imaging Kit, Image Kit, Image Processing Unit, PC Unit
    #   FR: Unité de traitement image, Kit de traitement d'images, Photoconducteur
    # All of these are treated as drums / photoconductors.
    DRUM_SYNONYMS = (
        "photoconduct", "pc unit",
        "drum", "tambour",
        "imaging", "image processing", "image unit", "image kit",
        # FR: "unité de traitement image" / "kit de traitement d'images"
        "traitement",
    )

    @property
    def category(self):
        # Computed once per supply (description/supply_type are immutable for a
        # given PrinterSupply object); avoids repeated string parsing while the
        # UI recomputes summaries/status for the whole fleet.
        if self._category_cache is None:
            self._category_cache = self._compute_category()
        return self._category_cache

    def _compute_category(self):
        text = f"{self.description} {self.supply_type or ''}".strip().lower()
        if any(k in text for k in self.DRUM_SYNONYMS):
            return "drum"
        # Waste toner bottles and staples must be checked before toner since
        # their descriptions often contain the word "toner" (e.g. "Waste Toner
        # Container"). Categorising them as "other" keeps them out of the toner
        # column while get_bottle_status/get_staples_status still find them by
        # keyword.
        if any(k in text for k in ("waste", "bottle", "récup", "recup", "collect")):
            return "other"
        if any(k in text for k in ("staple", "agraf")):
            return "other"
        if "developer" in text:
            return "developer"
        if "fuser" in text:
            return "fuser"
        if "maintenance" in text or "kit" in text:
            return "maintenance"
        if "cartridge" in text or "toner" in text or "cartouche" in text or "ink" in text:
            return "toner"
        # Kyocera mono : la description SNMP peut être la seule référence
        # commerciale (ex. "TK-3130" sur FS-4200DN), sans mot-type ni
        # mot-couleur. Règle ciblée sur références noires avérées uniquement.
        if is_kyocera_black_toner_ref(self.description):
            return "toner"
        return "other"

    def drum_colors(self):
        """Return the ordered list of colour keys (from DRUM_COLOR_ORDER) that
        this imaging consumable (photoconductor / drum / imaging unit / imaging
        kit) covers, based on the description provided by SNMP.

        Priority is given to the colour information actually reported by SNMP in
        the description ('Black Imaging Unit', 'Yellow Photoconductor', ...),
        then to explicit colour keywords in the description (Black, Cyan,
        Magenta, Yellow, CMY/CMYK, Colour/Color, Couleur, etc.).

        A single supply can cover several colours (e.g. a shared CMY imaging kit
        -> C, M, Y; or a Black + CMY kit -> K, C, M, Y). When no colour can be
        determined, the imaging unit is assumed to be the mono (Black) one.
        """
        text = f"{self.description} {self.supply_type or ''}".strip().lower()
        colors = set()

        # Explicit per-colour mentions
        for key, kws in (
            ("K", ("black", "noir")),
            ("C", ("cyan", "bleu")),
            ("M", ("magenta",)),
            ("Y", ("yellow", "jaune")),
        ):
            if any(k in text for k in kws):
                colors.add(key)

        # Group / colour indicators (CMY, CMYK, Colour/Color, Couleur, ...)
        if "cmyk" in text or "cmjn" in text:
            colors.update(("K", "C", "M", "Y"))
        elif (
            "cmy" in text
            or "colour" in text
            or "color" in text
            or "couleur" in text
            or "couleurs" in text
            or "process" in text
            or "full color" in text
            or "full colour" in text
        ):
            colors.update(("C", "M", "Y"))

        # Fall back to Black (typical mono imaging unit) when nothing detected.
        if not colors:
            colors.add("K")

        return [c for c in DRUM_COLOR_ORDER if c in colors]

    def short_description(self):
        desc = (self.description or "").strip()
        if not desc:
            return self.category.capitalize()
        if len(desc) <= 18:
            return desc
        return desc[:18].rstrip() + "..."

    def label(self):
        pct = self.display_percentage
        if pct is not None:
            return f"{self.description} ({pct}%)"
        if self.is_valid_level():
            return f"{self.description} ({self.level})"
        return self.description