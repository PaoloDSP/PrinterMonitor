"""Utilitaires partagés et constantes globales du logiciel Printer Monitor."""

import ipaddress
import re
from urllib.parse import urlparse

# ── Constantes de seuils (re-exportées depuis config) ──────────────────────
from .config import (
    DRUM_LOW_WARNING_THRESHOLD,
    LOW_WARNING_THRESHOLD,
    WARNING_WASTE_TONER_THRESHOLD,
)

# ── Expressions régulières ─────────────────────────────────────────────────
SUPPLY_RE = re.compile(r"(toner|cartouche|drum|photoconduct|tambour|developer|impression|ink|imaging|traitement|image)", re.I)
IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

# ── Consommables Kyocera : correspondances ciblées ──────────────────────────
# Les Kyocera mono (ex. FS-4200DN) exposent en SNMP la seule référence
# commerciale (« TK-3130 ») sans mot-couleur (black/noir) ni mot-type
# (toner/cartridge) : la détection générique par mots-clés ne peut donc pas
# classer ni colorer ce consommable. La table ci-dessous est volontairement
# ciblée et documentée : références noires avérées -> couleur "K".
# Pour l'étendre : ajouter d'autres références MONO avérées (TK-*, jamais de
# préfixe couleur) après vérification sur la machine réelle. Ne pas y mettre
# de références couleur (ex. TK-5xx C/M/Y) sans règle couleur dédiée.
KYOCERA_BLACK_TONER_REFS = frozenset({
    "TK-3130",  # FS-4200DN : toner noir mono, description SNMP = "TK-3130".
})


def is_kyocera_black_toner_ref(description):
    """True si la description est une référence toner noire Kyocera connue.

    Comparaison exacte (insensible à la casse/espaces) contre
    KYOCERA_BLACK_TONER_REFS : aucun préfixe générique « TK- » n'est
    considéré comme noir par défaut, pour ne pas classer à tort des
    références couleur d'autres gammes.
    """
    try:
        text = str(description or "").strip().upper()
    except Exception:
        return False
    return text in KYOCERA_BLACK_TONER_REFS

# ── Constantes d'affichage ─────────────────────────────────────────────────
# Real toner colors (C/M/Y/K) used for the progress bars so each bar keeps the
# actual consumable color instead of a generic level-based color.
TONER_COLORS = {
    "C": "#00bcd4",  # Cyan
    "M": "#e91e63",  # Magenta
    "Y": "#fbc02d",  # Yellow (dark enough for white text)
    "K": "#424242",  # Black / dark grey
}

# Colour chips and labels used to display the colour(s) covered by an imaging
# consumable (photoconductor / drum / imaging unit / imaging kit). A single such
# consumable does not necessarily map to a single colour: some models expose a
# shared CMY (or Black + CMY) imaging kit. The K chip is a black square, the C/M/Y
# chips are filled squares rendered with the corresponding TONER_COLORS in the
# details bars.
DRUM_COLOR_CHIPS = {"K": "⬛", "C": "🟦", "M": "🟥", "Y": "🟨"}
DRUM_COLOR_NAMES = {"K": "Noir", "C": "Cyan", "M": "Magenta", "Y": "Jaune"}
DRUM_COLOR_ORDER = ["K", "C", "M", "Y"]

# ── Contraste texte / fond ──────────────────────────────────────────────────
# Texte clair et texte sombre utilisés par défaut au-dessus des couleurs de
# consommables (jauges, chips). Le sombre est le slate-900 de la palette UI.
TEXT_LIGHT_ON_COLOR = "#ffffff"
TEXT_DARK_ON_COLOR = "#0f172a"


def _srgb_to_linear(channel):
    """Canal sRGB 0..255 -> composante linéaire 0..1 (norme WCAG 2.x)."""
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color):
    """Luminance relative (0..1) d'une couleur '#rrggbb' selon le WCAG.

    Retourne 1.0 en cas de couleur invalide (comportement sûr : le choix
    noir/blanc se rabattra alors sur le texte sombre, lisible sur fond clair).
    """
    try:
        h = str(hex_color).lstrip("#")
        if len(h) == 3:  # forme abrégée #rgb
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (
            0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b)
        )
    except Exception:
        return 1.0


def contrast_ratio(color_a, color_b):
    """Ratio de contraste WCAG entre deux couleurs hexadécimales (1..21)."""
    la = relative_luminance(color_a)
    lb = relative_luminance(color_b)
    lighter, darker = max(la, lb), min(la, lb)
    if lighter <= 0 and darker <= 0:
        return 1.0
    return (lighter + 0.05) / (darker + 0.05)


def text_color_for_bg(bg_hex, light=TEXT_LIGHT_ON_COLOR, dark=TEXT_DARK_ON_COLOR):
    """Choisit automatiquement blanc ou noir selon le fond pour un contraste
    maximal : blanc sur les fonds foncés, noir sur les fonds clairs. Le candidat
    offrant le meilleur ratio de contraste est retenu, ce qui fonctionne pour
    toutes les couleurs actuelles (cyan, magenta, jaune, noir, gris neutre,
    gris-bleu) comme pour toute couleur ajoutée ultérieurement."""
    light_ratio = contrast_ratio(light, bg_hex)
    dark_ratio = contrast_ratio(dark, bg_hex)
    return light if light_ratio >= dark_ratio else dark


def normalize_ip(value):
    if value is None:
        return ""
    candidate = str(value).strip()
    if not candidate:
        return ""

    if "://" in candidate or candidate.startswith("//"):
        parsed = urlparse(candidate, scheme="http")
        if parsed.hostname:
            candidate = parsed.hostname
    elif "/" in candidate and not candidate.startswith("http"):
        parsed = urlparse("http://" + candidate)
        if parsed.hostname:
            candidate = parsed.hostname

    if ":" in candidate and candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0].strip()

    if not candidate:
        return ""

    if IP_RE.match(candidate):
        try:
            ipaddress.IPv4Address(candidate)
            return candidate
        except ipaddress.AddressValueError:
            return ""
    return ""