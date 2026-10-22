"""Logique SNMP du logiciel Printer Monitor.

Ce module contient :
- Les imports pysnmp et la détection du backend
- Les constantes OID et les labels de statut
- Les classes de providers (Standard, Brother, Lexmark, Epson, Generic)
- La classe PrinterDevice avec toute la logique d'interrogation SNMP
"""

import asyncio
import inspect
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import _get_snmp_options
from .models import PrinterSupply
from .utils import DRUM_COLOR_CHIPS, DRUM_COLOR_NAMES, DRUM_COLOR_ORDER, is_kyocera_black_toner_ref


def toner_color_key(description):
    """Clé couleur C/M/Y/K d'un toner d'après sa description, ou None.

    Règles génériques inchangées (mots-couleurs + marqueurs mono-lettre),
    plus une règle ciblée Kyocera : les références noires avérées
    (ex. "TK-3130") -> "K". Utilisée par le résumé toner de l'aperçu liste.
    """
    desc = str(description or "").lower()
    for key, kws in (
        ("C", ("cyan", "bleu")),
        ("M", ("magenta",)),
        ("Y", ("yellow", "jaune")),
        ("K", ("black", "noir")),
    ):
        for kw in kws:
            if kw in desc:
                return key
    if re.search(r"\bC\b|[:\-]C\b|\bC[:\-]", desc, re.I):
        return "C"
    if re.search(r"\bM\b|[:\-]M\b|\bM[:\-]", desc, re.I):
        return "M"
    if re.search(r"\bY\b|[:\-]Y\b|\bY[:\-]", desc, re.I):
        return "Y"
    if re.search(r"\bK\b|[:\-]K\b|\bK[:\-]", desc, re.I):
        return "K"
    if is_kyocera_black_toner_ref(description):
        return "K"
    return None


def _ping_host(ip, timeout=2):
    """Résout dynamiquement _ping_host depuis le package.

    Permet aux tests de monkeypatcher ``printer_monitor._ping_host`` tout en
    conservant la compatibilité avec l'ancien module unique ``printer_monitor.py``.
    """
    import printer_monitor as _pm
    return _pm._ping_host(ip, timeout)

# SNMP response exception values (endOfMibView / noSuchObject / noSuchInstance).
# These are the standard "exception values" that an agent may return in a
# var-bind. We detect them explicitly so that a WALK never stops prematurely
# when one appears in the middle of a branch.
try:
    from pysnmp.proto.rfc1905 import (
        EndOfMibView as _EndOfMibView,
        NoSuchInstance as _NoSuchInstance,
        NoSuchObject as _NoSuchObject,
    )
    _SNMP_EXCEPTION_CLASSES = (_EndOfMibView, _NoSuchInstance, _NoSuchObject)
    _END_OF_MIB_CLASS = _EndOfMibView
except Exception:  # keep the WALK working even if imports differ across backends
    # Noms définis à None pour que ``from .snmp import _EndOfMibView`` (package
    # __init__.py, shim printer_monitor.py, tests) reste valide même si pysnmp
    # est absent ou change d'API ; la détection par nom de classe (plus bas)
    # prend alors le relais.
    _EndOfMibView = None
    _NoSuchInstance = None
    _NoSuchObject = None
    _SNMP_EXCEPTION_CLASSES = ()
    _END_OF_MIB_CLASS = None


def _is_snmp_exception(value_obj):
    """Return True if value_obj is any SNMP exception value (endOfMibView,
    noSuchInstance or noSuchObject)."""
    if value_obj is None:
        return False
    if _SNMP_EXCEPTION_CLASSES and isinstance(value_obj, _SNMP_EXCEPTION_CLASSES):
        return True
    try:
        name = type(value_obj).__name__.lower()
        if name in ("endofmibview", "nosuchinstance", "nosuchobject"):
            return True
    except Exception:
        pass
    return False


def _is_end_of_mib(value_obj):
    """Return True only for the genuine endOfMibView terminator."""
    if value_obj is None:
        return False
    if _END_OF_MIB_CLASS and isinstance(value_obj, _END_OF_MIB_CLASS):
        return True
    try:
        return type(value_obj).__name__.lower() == "endofmibview"
    except Exception:
        return False


# Try importing the high-level pysnmp API. Newer pysnmp versions organize HLAPI
# implementations under subpackages (v3arch, v1arch, asyncio). Attempt several
# fallbacks and map names where needed so the rest of the code can call the
# expected symbols (CommunityData, ContextData, ObjectIdentity, ObjectType,
# SnmpEngine, UdpTransportTarget, getCmd, nextCmd).
try:
    from pysnmp.hlapi import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        getCmd,
        nextCmd,
    )
    _pysnmp_backend = 'hlapi'
except Exception:
    _pysnmp_backend = None
    # Try the v3arch asyncio implementation which exports the expected symbols
    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            get_cmd,
            next_cmd,
        )
        # Provide camelCase aliases expected by existing code
        getCmd = get_cmd
        nextCmd = next_cmd
        _pysnmp_backend = 'v3arch.asyncio'
    except Exception:
        # Try v1arch asyncio (may have different names). Import SnmpEngine from
        # pysnmp.entity.engine and alias snake_case functions to camelCase.
        try:
            from pysnmp.hlapi.v1arch.asyncio import (
                CommunityData,
                ObjectIdentity,
                ObjectType,
                UdpTransportTarget,
                get_cmd,
                next_cmd,
            )
            from pysnmp.entity.engine import SnmpEngine
            # v1arch may not expose ContextData (SNMPv3) — provide a simple placeholder
            class ContextData:
                def __init__(self, *args, **kwargs):
                    pass
            getCmd = get_cmd
            nextCmd = next_cmd
            _pysnmp_backend = 'v1arch.asyncio'
        except Exception:
            # If none of the above work, mark SNMP as unavailable
            SnmpEngine = None

# GETBULK support: locate whichever backend callable can issue a GetBulk PDU
# (bulk_cmd under the asyncio backends, getBulkCmd under the legacy hlapi one).
# It lets a whole subtree be walked in a handful of round-trips instead of one
# GETNEXT per row. Kept as a module-level callable so the GETNEXT engine
# (still required for SNMPv1, which has no GETBULK) remains fully functional.
try:
    from pysnmp.hlapi.v3arch.asyncio import bulk_cmd as _BULK_CMD
except Exception:
    try:
        from pysnmp.hlapi.v1arch.asyncio import bulk_cmd as _BULK_CMD
    except Exception:
        try:
            from pysnmp.hlapi import getBulkCmd as _BULK_CMD
        except Exception:
            _BULK_CMD = None

# Log which pysnmp backend (if any) was selected. Logging configured later, so
# use a minimal print if logging isn't ready yet; once logging is configured,
# an info line will be emitted again at startup.
try:
    _backend_msg = f"pysnmp backend: {_pysnmp_backend or 'none'}"
    print(_backend_msg)
except Exception:
    pass

SUPPLY_OIDS = {
    "description": "1.3.6.1.2.1.43.11.1.1.6",
    "type": "1.3.6.1.2.1.43.11.1.1.4",
    "level": "1.3.6.1.2.1.43.11.1.1.9",
    "max_capacity": "1.3.6.1.2.1.43.11.1.1.8",
    # Colonnes standard supplementaires (RFC 3805) utilisees pour la
    # reconstruction generique : jamais de mapping fixe 1=noir, etc.
    # .2 = prtMarkerSuppliesMarkerIndex, .3 = prtMarkerSuppliesColorantIndex,
    # .5 = prtMarkerSuppliesSupplyClass, .7 = prtMarkerSuppliesUnit.
    "marker_index": "1.3.6.1.2.1.43.11.1.1.2",
    "colorant_index": "1.3.6.1.2.1.43.11.1.1.3",
    "supply_class": "1.3.6.1.2.1.43.11.1.1.5",
    "unit": "1.3.6.1.2.1.43.11.1.1.7",
}

# --- Heuristiques Epson generiques (fallbacks proprietaires) ---
# Ces OID sont des REPLIS Epson uniquement, jamais universels.
#  - EPSON_SUPPLY_TABLE_BASE : branche proprietaire documentee par PrintMaster
#    autour de 1.3.6.1.4.1.1248.1.2.2.2.1.1 (table a colonnes dynamiques).
#  - EPSON_REMOTE_ROOT : racine Epson Remote Mode
#    1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1 (commandes di/st/ia/ii, payload binaire).
#  - EPSON_ENTERPRISE_ID : enterprise Epson 1248 (sysObjectID).
EPSON_ENTERPRISE_ID = "1.3.6.1.4.1.1248"
EPSON_SUPPLY_TABLE_BASE = "1.3.6.1.4.1.1248.1.2.2.2.1.1"
EPSON_REMOTE_ROOT = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1"
EPSON_REMOTE_COMMANDS = ("di", "st", "ia", "ii")

SYS_OBJECT_ID_OID = "1.3.6.1.2.1.1.2.0"
HR_DEVICE_DESCR_BASE = "1.3.6.1.2.1.25.3.2.1.3"

# Codes negatifs Epson/standard : -1 = inconnu, -2 = bas/faible, -3 = inconnu.
UNKNOWN_LEVEL_CODES = (-1, -2, -3)


def _coerce_snmp_int(value):
    """Convert any SNMP value to int when plausible, else None.

    Accepte int/int32/uint/uint32/int64/uint64/Counter32/Counter64/Gauge32,
    chaines numeriques, OctetString/bytes contenant du texte numerique ou
    un entier binaire (1/2/4 octets, big et little endian essayes).
    Ne leve jamais. Les valeurs negatives sont conservees (l'appelant
    decide si -1/-2/-3 signifient 'unknown').
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int,)):
        return int(value)
    # Objets pysnmp Integer/Counter/Gauge : int(value) fonctionne, sinon
    # prettyPrint().
    try:
        _cls = type(value).__name__.lower()
    except Exception:
        _cls = ""
    if _cls in (
        "integer", "counter32", "counter64", "gauge32", "gauge",
        "unsigned32", "unsigned", "timeticks",
        "int", "int32", "uint", "uint32", "int64", "uint64",
    ):
        try:
            return int(value)
        except Exception:
            pass
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        # 1) texte numerique éventuel (ex. b"42", b" 80 ").
        try:
            txt = raw.decode("utf-8", errors="ignore").strip().strip("\x00").strip()
            if txt != "":
                try:
                    return int(txt)
                except Exception:
                    pass
                # flottant eventuel -> int
                try:
                    return int(float(txt.replace(",", ".")))
                except Exception:
                    pass
        except Exception:
            pass
        # 2) entier binaire court (1/2/4 octets).
        if len(raw) in (1, 2, 4):
            for _order in ("big", "little"):
                try:
                    return int.from_bytes(raw, _order, signed=True)
                except Exception:
                    continue
        # 3) premier octet plausible ? On ne devine PAS : None.
        return None
    if isinstance(value, str):
        txt = value.strip().strip("\x00").strip()
        if txt == "":
            return None
        try:
            return int(txt)
        except Exception:
            pass
        try:
            return int(float(txt.replace(",", ".")))
        except Exception:
            return None
    # Dernier recours : types exotiques avec prettyPrint / str().
    for _getter in ("prettyPrint", "__str__"):
        try:
            _fn = getattr(value, _getter, None)
            if _fn is None:
                continue
            _txt = str(_fn() if callable(_fn) else _fn).strip()
            if _txt == "":
                continue
            try:
                return int(_txt)
            except Exception:
                continue
        except Exception:
            continue
    try:
        return int(value)
    except Exception:
        return None


def _coerce_snmp_text(value):
    """Convert any SNMP value to displayable text ('' si vide).

    bytes -> decode utf-8/latin-1 ignore, sinon prettyPrint/str.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().strip("\x00")
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for _enc in ("utf-8", "latin-1"):
            try:
                return raw.decode(_enc, errors="ignore").strip().strip("\x00")
            except Exception:
                continue
        return ""
    try:
        _fn = getattr(value, "prettyPrint", None)
        if callable(_fn):
            try:
                return str(_fn()).strip().strip("\x00")
            except Exception:
                pass
    except Exception:
        pass
    try:
        return str(value).strip().strip("\x00")
    except Exception:
        return ""


def _coerce_snmp_raw_bytes(value):
    """Return raw bytes when the SNMP value is binary, else None."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return None


def _log_snmp_row(queried_oid, reply_oid, value_obj, index, interpreted):
    """Diagnostic fin (DEBUG) : OID interroge, OID repondu, type, brut, interprete."""
    try:
        _typ = type(value_obj).__name__ if value_obj is not None else "None"
    except Exception:
        _typ = "?"
    try:
        _raw = repr(value_obj)
        if len(_raw) > 200:
            _raw = _raw[:200] + "..."
    except Exception:
        _raw = "?"
    logging.debug(
        "SNMP row queried=%s reply=%s type=%s raw=%s index=%s interpreted=%r",
        queried_oid, reply_oid, _typ, _raw, index, interpreted,
    )


def _standard_supplies_coherent(supplies):
    """Verifie qu'une liste PrinterSupply issue de la Printer-MIB est exploitable.

    Criteres : au moins 1 entree, description non vide, niveau >= 0,
    capacite > 0, pourcentage calculable ou niveau brut coherent.
    Une table vide, des niveaux -1/-2/-3 ou des descriptions absentes
    ne doivent PAS conclure a 'aucun consommable' : elles declenchent
    les fallbacks (return False).
    """
    if not supplies:
        return False
    usable = 0
    for s in supplies:
        try:
            _desc = (s.description or "").strip()
            if not _desc:
                continue
            _lvl = s.level
            if _lvl is None or _lvl < 0:
                continue
            _mx = s.max_capacity
            if _mx is not None and _mx <= 0:
                continue
            usable += 1
        except Exception:
            continue
    return usable > 0

# Lexmark enterprise MIB consumable table (1.3.6.1.4.1.641.6.4.4.1)
# Lexmark printers expose toners, drums, waste toner bottles, and staples
# through these enterprise OIDs rather than the standard Printer-MIB supply
# table (which only returns maintenance kits / imaging units / ADF kits).
# Table structure: lexmarkConsumableTable = 1.3.6.1.4.1.641.6.4.4.1
#                  lexmarkConsumableEntry = 1.3.6.1.4.1.641.6.4.4.1.1
# NOTE: column .8 (lexmarkConsumableCapacityUnits) is a capacity-UNITS enum
# (2=percent, 1=items), NOT a real capacity. It is kept below for reference but
# is intentionally never used as max_capacity; percentages are computed from the
# standard Printer-MIB only (prtMarkerSuppliesMaxCapacity = 1.3.6.1.2.1.43.11.1.1.8).
LEXMARK_SUPPLY_OIDS = {
    "name": "1.3.6.1.4.1.641.6.4.4.1.1.2",
    "description": "1.3.6.1.4.1.641.6.4.4.1.1.3",
    "type": "1.3.6.1.4.1.641.6.4.4.1.1.4",
    "max_capacity_units": "1.3.6.1.4.1.641.6.4.4.1.1.8",
    "level": "1.3.6.1.4.1.641.6.4.4.1.1.6",
    "unit": "1.3.6.1.4.1.641.6.4.4.1.1.7",
}

# Lexmark consumable type integer values (lexmarkConsumableType)
LEXMARK_CONSUMABLE_TYPES = {
    1: "toner",
    2: "ink",
    3: "photoconductor",
    4: "waste toner bottle",
    5: "maintenance kit",
    6: "staple",
    7: "fuser",
    8: "transfer unit",
    9: "developer",
    10: "other",
}

PRINTER_STATUS_OID = "1.3.6.1.2.1.43.5.1.1.1"
SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"

STATUS_LABELS = {
    1: "other",
    2: "unknown",
    3: "idle",
    4: "printing",
    5: "warmup",
}

# Some vendors do not implement prtGeneralStatus (1.3.6.1.2.1.43.5.1.1.1) and
# return "No Such Instance" for it (notably Lexmark, observed in production
# logs). hrDeviceStatus from the Host Resources MIB is widely implemented and
# provides a usable fallback for these vendors.
VENDOR_STATUS_FALLBACKS = {
    "lexmark": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "hp": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "epson": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "canon": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "xerox": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "ricoh": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "kyocera": ["1.3.6.1.2.1.25.3.2.1.5.1"],
    "samsung": ["1.3.6.1.2.1.25.3.2.1.5.1"],
}

# hrDeviceStatus has different semantics from prtGeneralStatus:
# 1=unknown, 2=running, 3=warning, 4=testing, 5=down
HR_DEVICE_STATUS_LABELS = {
    1: "unknown",
    2: "running",
    3: "warning",
    4: "testing",
    5: "down",
}


class PrinterProvider:
    """Base class for printer providers/vendors."""
    name = "standard"

    def get_supplies(self, device, community, port):
        """Return a list of PrinterSupply for the device."""
        raise NotImplementedError()


class StandardPrinterProvider(PrinterProvider):
    name = "standard"

    def get_supplies(self, device, community, port):
        # Read standard Printer-MIB supplies. Toutes les colonnes utiles
        # (RFC 3805 : .2/.3/.4/.5/.6/.7/.8/.9) sont WALKEES comme racines de
        # table ; les index sont decouverts dynamiquement (union des index
        # bruts) et jamais supposes fixes (pas de 1=noir, etc.).
        return read_standard_supplies(device, community, port)


def read_standard_supplies(device, community, port):
    """Lit la table standard prtMarkerSupplies (1.3.6.1.2.1.43.11.1.1).

    - Chaque colonne (.2/.3/.4/.5/.6/.7/.8/.9) est WALKEE comme racine.
    - Les index sont decouverts dynamiquement (union des suffixes bruts).
    - Les valeurs sont converties via _coerce_snmp_int/_coerce_snmp_text
      (Integer/Counter/Gauge/OctetString/bytes...).
    - Les niveaux -1/-2/-3 sont conserves tels quels dans PrinterSupply
      (is_valid_level() les invalide) afin que l'appelant puisse detecter
      'partiel' et declencher les fallbacks au lieu de conclure 'vide'.
    """
    try:
        # Perf preservee : 4 colonnes coeurs par defaut (comportement
        # historique). Les 4 colonnes supplementaires (.2/.3/.5/.7) ne
        # sont WALKEES que pour Epson ou si le coeur est partiel
        # (descriptions manquantes) — reconstruction de libelles.
        try:
            _vendor_hint = device._detect_vendor()
        except Exception:
            _vendor_hint = ""
        _core = [
            SUPPLY_OIDS["description"],
            SUPPLY_OIDS["type"],
            SUPPLY_OIDS["level"],
            SUPPLY_OIDS["max_capacity"],
        ]
        description_map, type_map, level_map, max_map = _walk_columns_concurrent(
            device, community, port, _core)
        marker_map, colorant_map, class_map, unit_map = {}, {}, {}, {}
        _need_extra = (_vendor_hint == "epson")
        if not _need_extra:
            try:
                _core_idx = set(description_map) | set(level_map) | set(max_map)
                _nodesc = [i for i in _core_idx if not _coerce_snmp_text(
                    description_map.get(i))]
                _need_extra = bool(_nodesc)
            except Exception:
                _need_extra = False
        if _need_extra:
            try:
                _extra = _walk_columns_concurrent(
                    device, community, port,
                    [SUPPLY_OIDS["marker_index"], SUPPLY_OIDS["colorant_index"],
                     SUPPLY_OIDS["supply_class"], SUPPLY_OIDS["unit"]])
                marker_map, colorant_map, class_map, unit_map = _extra
            except Exception:
                pass
        cols = (description_map, type_map, level_map, max_map,
                marker_map, colorant_map, class_map, unit_map)
        (description_map, type_map, level_map, max_map,
         marker_map, colorant_map, class_map, unit_map) = cols
    except Exception:
        logging.exception("Standard supplies walk failed for %s", getattr(device, "ip", "?"))
        return []
    # Union des index bruts : une Epson partielle peut n'exposer que
    # level+max sans description exploitable, ou l'inverse.
    indexes = set()
    for _m in (description_map, type_map, level_map, max_map,
               marker_map, colorant_map, class_map, unit_map):
        try:
            indexes.update(_m.keys())
        except Exception:
            continue
    supplies = []
    for index in sorted(indexes, key=lambda s: [int(x) for x in str(s).split(".") if x.isdigit()]):
        try:
            _qbase = "1.3.6.1.2.1.43.11.1.1"
            _desc_raw = description_map.get(index)
            _typ_raw = type_map.get(index)
            _lvl_raw = level_map.get(index)
            _max_raw = max_map.get(index)
            description = _coerce_snmp_text(_desc_raw)
            if not description:
                # Repli : reconstruire un libelle depuis colorant/type.
                _ci = _coerce_snmp_int(colorant_map.get(index))
                _tp = _coerce_snmp_int(_typ_raw)
                _cl = _coerce_snmp_int(class_map.get(index))
                description = _fallback_supply_label(_ci, _tp, _cl, index)
            level = _coerce_snmp_int(_lvl_raw)
            max_capacity = _coerce_snmp_int(_max_raw)
            supply_type = _coerce_snmp_text(_typ_raw)
            if not supply_type:
                _tp2 = _coerce_snmp_int(_typ_raw)
                if _tp2 is not None:
                    supply_type = str(_tp2)
            _log_snmp_row(f"{_qbase}.6", f"{_qbase}.6.{index}", _desc_raw, index, description)
            _log_snmp_row(f"{_qbase}.9", f"{_qbase}.9.{index}", _lvl_raw, index, level)
            _log_snmp_row(f"{_qbase}.8", f"{_qbase}.8.{index}", _max_raw, index, max_capacity)
            supply = PrinterSupply(index, description, level, max_capacity, supply_type)
            supplies.append(supply)
        except Exception:
            continue
    return supplies


def _fallback_supply_label(colorant_index, supply_type, supply_class, index):
    """Construit un libelle generique sans jamais supposer 1=noir, etc.

    Le libelle reste neutre ('Supply <index>') sauf si le type indique
    clairement encre/toner ; la couleur precise vient des donnees, pas
    d'un mapping fixe d'index.
    """
    try:
        _t = int(supply_type) if supply_type is not None else None
    except Exception:
        _t = None
    _kind = "Supply"
    if _t == 3:
        _kind = "Toner"
    elif _t == 4:
        _kind = "Ink"
    return f"{_kind} {index}"


class BrotherPrinterProvider(PrinterProvider):
    name = "brother"

    def get_supplies(self, device, community, port):
        # Try standard Printer-MIB first
        std = StandardPrinterProvider().get_supplies(device, community, port)
        # If any valid percentages present, prefer standard
        if any(s.percentage is not None for s in std):
            return std
        # Otherwise, attempt the Brother proprietary OID
        bro = device._read_brother_proprietary(community, port)
        if bro:
            # Keep non-toner supplies discovered by standard provider
            non_toners = [s for s in std if s.category != 'toner']
            return non_toners + bro
        return std


class LexmarkPrinterProvider(PrinterProvider):
    """Provider for Lexmark printers (CX921, CX962, CS943, MS823, etc.).

    Lexmark exposes maintenance kits, imaging units, and ADF kits via the
    standard Printer-MIB supply table, but toners, photoconductors, waste toner
    bottles, and staples are only available through the Lexmark enterprise MIB
    consumable table at 1.3.6.1.4.1.641.6.4.4.
    """
    name = "lexmark"

    def get_supplies(self, device, community, port):
        # Standard Printer-MIB supplies (maintenance kits, imaging units, ADF
        # kits). These may also provide MaxCapacity for consumables exposed by
        # the Lexmark enterprise MIB.
        std = StandardPrinterProvider().get_supplies(device, community, port)
        std_max_by_index = {
            s.index: s.max_capacity
            for s in std
            if s.max_capacity is not None and s.max_capacity > 0
        }

        # Lexmark enterprise MIB consumables (toners, photoconductors, waste
        # toner bottle, staples, etc.).
        lex_supplies = self._read_lexmark_consumables(device, community, port)
        if lex_supplies:
            # Heuristic: when the Lexmark enterprise table does not provide a
            # usable max_capacity, fall back to the standard Printer-MIB
            # MaxCapacity for the same underlying index, so the level can be
            # expressed as a percentage.
            for s in lex_supplies:
                if s.max_capacity is None or s.max_capacity <= 0:
                    raw_index = s.index
                    if isinstance(raw_index, str) and raw_index.startswith("lex-"):
                        raw_index = raw_index.split("-", 1)[1]
                    std_max = std_max_by_index.get(raw_index)
                    if std_max is not None and std_max > 0:
                        s.max_capacity = std_max
            return std + lex_supplies
        return std

    def _read_lexmark_consumables(self, device, community, port):
        """Walk the Lexmark enterprise consumable table and return PrinterSupply
        objects for each consumable found (toners, drums, waste bottle, staples).
        """
        try:
            (name_map, desc_map, type_map, level_map, unit_map) = _walk_columns_concurrent(
                device, community, port,
                [
                    LEXMARK_SUPPLY_OIDS["name"],
                    LEXMARK_SUPPLY_OIDS["description"],
                    LEXMARK_SUPPLY_OIDS["type"],
                    LEXMARK_SUPPLY_OIDS["level"],
                    LEXMARK_SUPPLY_OIDS["unit"],
                ],
            )
            # Use name_map as the primary index source, but fall back to
            # desc_map when the name column is not populated (some Lexmark
            # firmware versions only expose the description column).
            if not name_map and desc_map:
                name_map = desc_map
            if not name_map:
                return []

            supplies = []
            for index, name in name_map.items():
                try:
                    description = desc_map.get(index) or name or f"Consumable {index}"
                    level_raw = level_map.get(index)
                    type_raw = type_map.get(index)
                    unit_raw = unit_map.get(index)

                    # Convert type integer to readable string
                    try:
                        type_int = int(str(type_raw).strip())
                        type_str = LEXMARK_CONSUMABLE_TYPES.get(type_int, str(type_int))
                    except (ValueError, TypeError):
                        type_str = str(type_raw) if type_raw else ""

                    # Parse the level only. There is NO exploitable max capacity in
                    # the Lexmark enterprise consumable table: column .8
                    # (lexmarkConsumableCapacityUnits) is a capacity-units enum
                    # (2=percent, 1=items), NOT a real capacity, so it must never be
                    # used as max_capacity. Percentages are computed exclusively from
                    # the standard Printer-MIB (prtMarkerSuppliesMaxCapacity = .8).
                    level = PrinterSupply._to_int(level_raw)
                    max_capacity = None

                    # Parse unit. In the Lexmark MIB, lexmarkConsumableCapacityUnits:
                    #   1 = pages (tenths of pages)
                    #   2 = percent
                    #   3 = items
                    try:
                        unit_int = int(str(unit_raw).strip())
                    except (ValueError, TypeError):
                        unit_int = 0

                    # Treat negative level (unknown) as None
                    if level is not None and level < 0:
                        level = None

                    # Skip entries with no usable level. Some Lexmark firmware
                    # versions report consumable metadata (name/type) but leave
                    # the level unpopulated, which only creates noise in the UI.
                    if level is None:
                        continue

                    supply = PrinterSupply(
                        f"lex-{index}",
                        description,
                        level,
                        max_capacity,
                        type_str,
                    )
                    supplies.append(supply)
                except Exception:
                    continue

            logging.debug(
                "Lexmark enterprise consumables for %s: %s",
                device.ip,
                ", ".join(f"{s.description}={s.level}/{s.max_capacity}" for s in supplies),
            )
            return supplies
        except Exception:
            logging.exception("Failed to read Lexmark enterprise consumables for %s", device.ip)
            return []


def _epson_vendor_columns(device, community, port, base=EPSON_SUPPLY_TABLE_BASE,
                         max_columns=12):
    """Decouvre dynamiquement les colonnes de la table Epson proprietaire.

    WALK la branche de base puis regroupe par premier sub-identifiant
    (= numero de colonne presume). Retourne {col: {index: raw_value}}.
    Ne suppose AUCUNE signification de colonne.
    """
    try:
        rows = device.snmp_walk_raw(community, port, base)
    except Exception:
        try:
            rows = [(k, "?", v) for k, v in
                    device.snmp_walk(community, port, base).items()]
        except Exception:
            return {}
    columns = {}
    for _entry in rows or []:
        try:
            if isinstance(_entry, (tuple, list)) and len(_entry) == 3:
                _oid, _typ, _val = _entry
            elif isinstance(_entry, (tuple, list)) and len(_entry) == 2:
                _oid, _val = _entry
            else:
                continue
            _oid_s = str(_oid)
            _suffix = _oid_s[len(base):].strip(".") if _oid_s.startswith(base) else ""
            if not _suffix:
                continue
            _parts = _suffix.split(".")
            _col = int(_parts[0])
            if _col < 1 or _col > max_columns:
                continue
            _idx = ".".join(_parts[1:]) if len(_parts) > 1 else ""
            if not _idx:
                continue
            columns.setdefault(_col, {})[_idx] = _val
            _log_snmp_row(base, _oid_s, _val, f"col{_col}.{_idx}", _val)
        except Exception:
            continue
    return columns


def _infer_epson_vendor_roles(columns, model=None):
    """Role colonnes Epson, ordre STRICT (passe 2) : mapping, structure,
    heuristique en dernier recours, doute=UNKNOWN.

    - description : majorite de textes non numeriques exploitables.
    - niveau/max : majorite d'entiers >= 0 (ou codes -1/-2/-3 connus).
    Retourne (desc_col, level_col, max_col), None si incertain.
    Ne code JAMAIS 'colonne 3 = niveau pour toutes les Epson'.
    """
    if not columns:
        return (None, None, None)
    _all_idx = set()
    for _m in columns.values():
        try:
            _all_idx.update((_m or {}).keys())
        except Exception:
            pass
    if len(_all_idx) < 2:
        return (None, None, None)
    _text_score = {}
    _num_score = {}
    _nums = {}
    for _col, _map in columns.items():
        _t = 0
        _n = 0
        _vals = []
        for _v in (_map or {}).values():
            _iv = _coerce_snmp_int(_v)
            _tx = _coerce_snmp_text(_v)
            _is_num = _iv is not None
            if not _is_num and _tx != "":
                try:
                    int(_tx)
                    _is_num = True
                except Exception:
                    _is_num = False
            if _is_num:
                _n += 1
                if _iv is not None:
                    _vals.append(_iv)
            elif _tx != "":
                _t += 1
        _text_score[_col] = _t
        _num_score[_col] = _n
        _nums[_col] = _vals
    _desc_col = None
    _best_t = 0
    for _col, _t in _text_score.items():
        if _t >= 2 and _t > _best_t and _t >= _num_score.get(_col, 0):
            _best_t = _t
            _desc_col = _col

    def _levelness(c):
        _s = 0
        for _v in _nums.get(c, []):
            if 0 <= _v <= 100 or _v in UNKNOWN_LEVEL_CODES:
                _s += 2
            elif _v >= 0:
                _s += 1
        return _s

    _num_cols = [c for c, nn in _num_score.items() if nn >= 2 and c != _desc_col]
    _level_col = None
    _max_col = None
    if _num_cols:
        _num_cols.sort(key=_levelness, reverse=True)
        for _cand in _num_cols:
            if _levelness(_cand) < 2:
                continue
            _idx_c = set((columns.get(_cand) or {}).keys())
            _coupled = False
            if _desc_col is not None:
                _idx_d = set((columns.get(_desc_col) or {}).keys())
                if len(_idx_c & _idx_d) >= 2:
                    _coupled = True
            if not _coupled:
                for _o in _num_cols:
                    if _o == _cand:
                        continue
                    _idx_o = set((columns.get(_o) or {}).keys())
                    if len(_idx_c & _idx_o) >= 2:
                        _coupled = True
                        break
            if _coupled:
                _level_col = _cand
                break
        if _level_col is not None and len(_num_cols) > 1:
            _lvl_vals = _nums.get(_level_col, [])
            _lvl_max = max([v for v in _lvl_vals if v >= 0], default=None)
            for _c in _num_cols:
                if _c == _level_col:
                    continue
                _vals = [v for v in _nums.get(_c, []) if v >= 0]
                if not _vals:
                    continue
                if _lvl_max is None or max(_vals) >= _lvl_max:
                    _idx_l = set((columns.get(_level_col) or {}).keys())
                    _idx_c = set((columns.get(_c) or {}).keys())
                    if len(_idx_l & _idx_c) >= 2:
                        _max_col = _c
                        break
    return (_desc_col, _level_col, _max_col)


def read_epson_vendor_table(device, community, port,
                            base=EPSON_SUPPLY_TABLE_BASE):
    """Fallback Epson : table proprietaire 1.3.6.1.4.1.1248.1.2.2.2.1.1.

    Parcourt la table dynamiquement, infere les colonnes depuis les donnees
    reelles, reconstruit des PrinterSupply depuis les index reels.
    Retourne [] si rien d'exploitable (l'appelant essaie Remote Mode).
    """
    columns = _epson_vendor_columns(device, community, port, base)
    if not columns:
        return []
    desc_col, level_col, max_col = _infer_epson_vendor_roles(columns)
    if level_col is None:
        logging.debug("Epson vendor table %s: no level column (cols=%s)",
                      base, sorted(columns.keys()))
        return []
    level_map = columns.get(level_col, {}) or {}
    desc_map = columns.get(desc_col, {}) or {} if desc_col is not None else {}
    max_map = columns.get(max_col, {}) or {} if max_col is not None else {}
    indexes = set(level_map.keys()) | set(desc_map.keys()) | set(max_map.keys())

    def _sort_key(s):
        try:
            return [int(x) for x in str(s).split(".") if x.isdigit()]
        except Exception:
            return [str(s)]

    supplies = []
    for index in sorted(indexes, key=_sort_key):
        try:
            _lvl_raw = level_map.get(index)
            level = _coerce_snmp_int(_lvl_raw)
            if level in UNKNOWN_LEVEL_CODES:
                level = None
            max_capacity = _coerce_snmp_int(max_map.get(index))
            if max_capacity is not None and max_capacity <= 0:
                max_capacity = None
            description = _coerce_snmp_text(desc_map.get(index))
            if not description:
                description = f"Epson Ink {index}"
            if level is None and max_capacity is None:
                if index not in desc_map:
                    continue
            supplies.append(PrinterSupply(
                f"epson-vendor-{index}", description, level,
                max_capacity, "ink",
            ))
            try:
                supplies[-1]._epson_source = "vendor"
            except Exception:
                pass
        except Exception:
            continue
    return supplies


def _epson_remote_oid(command, payload=1):
    """Construit l'OID Epson Remote Mode EXACT d'apres epson_print_conf.

    Reference (Ircama/epson_print_conf, ``epctrl_snmp_oid``) :
        cmd = command.encode() + struct.pack('<H', len(payload)) + payload
        oid = EPSON_CTRL_TO_OID + "." + ".".join(str(b) for b in cmd)
    avec EPSON_CTRL_TO_OID = 1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1.

    OID exacts verifies (Ircama/epson_print_conf, commentaires source) :
    - di, payload 1      -> ...2.1.100.105.1.0.1  (device identification)
    - st, payload 1      -> ...2.1.115.116.1.0.1  (status ST2)
    - ia, payload 0      -> ...2.1.105.97.1.0.0   (ink actuator list ;
      epctrl_snmp_oid("ia", 0), commentaire ".105.97.1.0.0" = 69 61 01 00 00 :
      l'entier 0 devient l'octet b'\x00', longueur 1)
    - ii, payload [1,i]  -> ...2.1.105.105.2.0.1.<i> (slot i, 1..8)
    NOTE passe 3 : une premiere version affirmait a tort payload vide
    (...105.97.0.0). La reference convertit l'entier 0 en bytes([0]),
    donc suffixe 105.97.1.0.0. Corrige ici et en test.
    """
    try:
        if len(command) != 2:
            return None
        # Reference exacte epctrl_snmp_oid : int -> bytes([payload])
        # (0 -> b'\x00', longueur 1), list -> bytes(list).
        # Seul None (jamais utilise par la reference pour ia) = b''.
        if payload is None:
            payload = b''
        elif isinstance(payload, int):
            payload = bytes([payload])
        elif isinstance(payload, (list, tuple)):
            payload = bytes(payload)
        elif isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        else:
            return None
        import struct as _struct
        cmd = command.encode("ascii") + _struct.pack("<H", len(payload)) + payload
        return EPSON_REMOTE_ROOT + "." + ".".join(str(int(b)) for b in cmd)
    except Exception:
        return None


def _epson_ii_slot_oid(slot):
    """OID exact pour le slot d'encre ``ii`` n° slot (1..8).

    Reference : epctrl_snmp_oid("ii", b"\\x01" + bytes([i])).
    """
    try:
        _i = int(slot)
        if _i < 1 or _i > 8:
            return None
        return _epson_remote_oid("ii", bytes([0x01, _i]))
    except Exception:
        return None


# Tables documentees epson_print_conf (status_parser) : nommage des encres
# pour la reponse ST2 ftype 0x0f. Utilisees UNIQUEMENT pour libeller les
# entrees ST2 reelles, jamais pour deviner des niveaux.
_EPSON_ST2_COLOUR_IDS = {
    0x01: "Black", 0x03: "Cyan", 0x04: "Magenta", 0x05: "Yellow",
    0x06: "Light Cyan", 0x07: "Light Magenta", 0x0A: "Light Black",
    0x0B: "Matte Black", 0x0F: "Light Light Black",
    0x10: "Orange", 0x11: "Green",
}
_EPSON_ST2_INK_COLOR_IDS = {
    0x00: "Black", 0x01: "Cyan", 0x02: "Magenta", 0x03: "Yellow",
    0x04: "Light Cyan", 0x05: "Light Magenta", 0x06: "Dark Yellow",
    0x07: "Grey", 0x08: "Light Black", 0x09: "Red", 0x0A: "Blue",
    0x0B: "Gloss Optimizer", 0x0C: "Light Grey", 0x0D: "Orange",
}

# Mapping documente des reponses par commande (Ircama/epson_print_conf).
# Source exacte : get_device_identification / get_cartridges /
# get_cartridge_information / get_printer_status.
# - di : texte "MFG:...;CMD:...;MDL:...;CLS:...;DES:..." (decode()[10:]).
# - ia : texte "@BDC ... IA:00;<liste separee par virgules>;...".
# - ii : slots 1..8, "@BDC PS\r\n<k:v;...>"" ou "ii:NA;" = slot vide = FIN.
# - st : binaire "@BDC ST2" TLV strict (voir _parse_epson_st2).
_EPSON_REMOTE_RESPONSE_SPEC = {
    "di": {
        "oid": EPSON_REMOTE_ROOT + ".100.105.1.0.1",
        "payload": "b'\\x01' (payload=1)",
        "snmp_type": "OctetString (bytes)",
        "format": "texte 'MFG:..;CMD:..;MDL:..;CLS:..;DES:..' "
                  "apres 10 octets d'en-tete ; decode()[10:].split(';')",
        "parser": "_parse_epson_di (MFG/CMD/MDL/CLS/DES) ; pas de consommable",
    },
    "st": {
        "oid": EPSON_REMOTE_ROOT + ".115.116.1.0.1",
        "payload": "b'\\x01' (payload=1)",
        "snmp_type": "OctetString (bytes, trame @BDC ST2)",
        "format": "b'\\x00@BDC ST2\\r\\n' + len16 LE + TLV "
                  "(ftype, longueur, item) ; encre = ftype 0x0f : "
                  "item[0]=colourlen puis (colour, ink_color, level) par pas",
        "parser": "_parse_epson_st2 strict ; niveaux 0x0f uniquement, "
                  "maintenance box 0x37 informative",
    },
    "ia": {
        "oid": EPSON_REMOTE_ROOT + ".105.97.1.0.0",
        "payload": "0 (entier -> bytes([0]) = b'\\x00', longueur 1)",
        "snmp_type": "OctetString (bytes, texte latin-1)",
        "format": "contient 'IA:00;<liste>;...' ; re.sub(r'.*IA:00;(.*);.*') "
                  "puis split(',') ; refus '||:NA;' = pas de liste",
        "parser": "_parse_epson_ia (liste de cartouches, SANS niveau ; "
                  "niveau UNKNOWN, jamais devine)",
    },
    "ii": {
        "oid": EPSON_REMOTE_ROOT + ".105.105.2.0.1.<slot 1..8>",
        "payload": "b'\\x01<slot>' (payload=[1, slot])",
        "snmp_type": "OctetString (bytes, texte latin-1)",
        "format": "slot : '@BDC PS\\r\\n<k:v;...>' avec IQT (hex, ink_quantity = METADONNEE jamais niveau), "
                  "IC1 (hex, couleur), II=='03' ; 'ii:NA;' = slot vide = FIN",
        "parser": "_parse_epson_ii (IQT hex -> METADONNEE ink_quantity, niveau TOUJOURS UNKNOWN ; niveau (%) = ST2 0x0F) ; "
                  "IC1 hex -> nom via CARTRIDGE_TYPE partiel",
    },
}

# Sous-ensemble documente CARTRIDGE_TYPE (Ircama) pour nommer les slots ii.
# Partiel et affiche tel quel : inconnu -> "unknown color", jamais devine.
_EPSON_CARTRIDGE_TYPE = {
    1811: "Black", 1812: "Cyan", 1813: "Magenta", 1814: "Yellow",
    711: "Black", 712: "Cyan", 713: "Magenta", 714: "Yellow",
    10332: "Black", 10360: "Cyan", 10361: "Magenta", 10362: "Yellow",
}

# Ordre de fallback Epson impose (passe 2) : Vendor -> Remote -> Legacy ->
# partiel UNKNOWN. Ne jamais remplacer une donnee valide par une heuristique
# plus incertaine : les scores ci-dessous garantissent cet ordre.
_EPSON_SOURCE_SCORE = {"standard": 0, "vendor": 1, "remote-st2": 2,
                       "remote-ii": 3, "remote-ia": 4, "legacy": 5,
                       "partial": 6}


def _parse_epson_st2(data):
    """Parse STRICT d'une reponse Epson ST2 (@BDC ST2, TLV).

    Reference : epson_print_conf ``status_parser``.
    Structure reelle : ``\\x00@BDC ST2\\r\\n`` (11 octets), longueur
    little-endian sur data[11:13] (= len(data)-13), puis elements TLV
    ``ftype(1) longueur(1) item[longueur]``.
    Seul ftype 0x0f (Ink information) produit des niveaux :
    item[0]=colourlen (pas), puis par pas : colour, ink_color, level.
    Retourne dict {ink_level:[(colour,ink_color,name,ink_name,level),...],
    maintenance_box_*, status, ...} ou None si trame invalide.
    Ne devine JAMAIS : en-tete/longueur/TLV invalides -> None.
    """
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("latin-1", errors="ignore")
    if not isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data)
        except Exception:
            return None
    data = bytes(data)
    if len(data) < 16:
        return None
    if data[:11] != b"\x00@BDC ST2\r\n":
        _start = data.find(b"BDC ST2\r\n")
        if _start < 0:
            return None
        data = bytes(2) + data[_start:]
        if data[:11] != b"\x00@BDC ST2\r\n":
            return None
    try:
        _len_p = int.from_bytes(data[11:13], byteorder="little")
    except Exception:
        return None
    if len(data) - 13 != _len_p:
        return None
    buf = data[13:]
    out = {}
    while len(buf):
        if len(buf) < 2:
            return None
        _ftype, _length = buf[0], buf[1]
        buf = buf[2:]
        _item = buf[:_length]
        if len(_item) != _length:
            return None
        buf = buf[_length:]
        if _ftype == 0x01 and _length >= 1:  # status code
            out["status_code"] = _item[0]
        elif _ftype == 0x0F:  # Ink information (0x0f)
            if _length < 1:
                return None
            _colourlen = _item[0]
            if _colourlen < 3:
                return None
            _offset = 1
            _inks = []
            while _offset + 2 < _length + 1 and _offset + _colourlen <= _length + 1:
                try:
                    _colour = _item[_offset]
                    _ink_color = _item[_offset + 1]
                    _level = _item[_offset + 2]
                except Exception:
                    break
                _name = _EPSON_ST2_COLOUR_IDS.get(_colour, "0x%X" % _colour)
                _ink_name = _EPSON_ST2_INK_COLOR_IDS.get(_ink_color, "0x%X" % _ink_color)
                _inks.append((_colour, _ink_color, _name, _ink_name, _level))
                _offset += _colourlen
                if _offset >= _length:
                    break
            out["ink_level"] = _inks
        elif _ftype == 0x37:  # Maintenance box information
            if _length >= 1:
                _num = _item[0]
                if 1 <= _num <= 2:
                    _j = 1
                    for _i in range(1, _length, _num):
                        try:
                            out[f"maintenance_box_{_j}"] = _item[_i]
                        except Exception:
                            break
                        _j += 1
        # autres ftype : ignores (statut/papier/cutter...), jamais de niveaux.
    return out


def _parse_epson_di(raw):
    """Parse STRICT di (get_device_identification)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode('latin-1', errors='ignore')
    if not isinstance(raw, (bytes, bytearray)):
        return None
    raw = bytes(raw)
    if len(raw) < 12:
        return None
    try:
        _txt = raw.decode('latin-1', errors='ignore')[10:]
    except Exception:
        return None
    if 'MFG:' not in _txt and 'MDL:' not in _txt and 'CMD:' not in _txt:
        return None
    _km = {'MFG': 'Manufacturer', 'CMD': 'Commands', 'MDL': 'Model',
           'CLS': 'Class', 'DES': 'Description'}
    out = {}
    for _part in _txt.split(';'):
        if not _part or ':' not in _part:
            continue
        _kv = _part.split(':')
        out[_km.get(_kv[0].strip(), _kv[0].strip())] = [v for v in _kv[1:] if v]
    return out or None


def _parse_epson_ia(raw):
    """Parse STRICT ia (get_cartridges) : exige 'IA:00;' sinon None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode('latin-1', errors='ignore')
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        _txt = bytes(raw).decode('latin-1', errors='replace')
    except Exception:
        return None
    if 'IA:00;' not in _txt:
        return None
    try:
        _m = re.sub(r'.*IA:00;(.*);.*', r'\g<1>', _txt, flags=re.S)
    except Exception:
        return None
    _items = [i.strip() for i in _m.split(',')]
    _items = [i for i in _items if i]
    return _items or None


def _parse_epson_ii(raw):
    """Parse STRICT ii slot : (color_num, color_name, ink_quantity|None, documented).

    Reference : get_cartridge_information + cartridge_parser.
    IQT est decode comme ``ink_quantity`` (quantite d'encre, PAS un
    pourcentage de niveau restant) ; le niveau courant vient du champ
    0x0F de la trame ST2. Donc : IQT est conserve comme METADONNEE
    (hex brut + entier), et le niveau retourne est TOUJOURS UNKNOWN
    (None) — jamais IQT transforme en pourcentage.
    Retourne (color_num, color_name, ink_quantity|None, documented,
    iqt_hex|None) ou None si slot vide / non conforme.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode('latin-1', errors='ignore')
    if not isinstance(raw, (bytes, bytearray)):
        return None
    raw = bytes(raw)
    if not raw:
        return None
    if raw.find(b'ii:NA;') >= 0:
        return None
    if raw.find(b'@BDC PS\r\n') < 0:
        return None
    try:
        _body = raw[raw.find(b'@BDC PS\r\n') + 9:]
        if _body.endswith(b'\x0c'):
            _body = _body[:-1]
        if _body.endswith(b';'):
            _body = _body[:-1]
        _txt = _body.decode('latin-1', errors='ignore')
    except Exception:
        return None
    _map = {}
    try:
        for _pair in _txt.split(';'):
            if ':' not in _pair:
                continue
            _k, _v = _pair.split(':', 1)
            _map[_k.strip()] = _v.strip()
    except Exception:
        return None
    if not _map:
        return None
    _doc = (_map.get('II') == '03')
    _num, _name = None, 'unknown color'
    try:
        if 'IC1' in _map and _map['IC1'] not in ('', 'NAVL'):
            _num = int(_map['IC1'], 16)
            _name = _EPSON_CARTRIDGE_TYPE.get(_num, 'unknown color')
    except Exception:
        _num = None
    # Passe 3 : IQT = ink_quantity (metadonnee), JAMAIS un niveau restant.
    # Le niveau (%) vient exclusivement du champ 0x0F de ST2.
    _qty = None
    _qty_hex = None
    try:
        if _doc and 'IQT' in _map and _map['IQT'] not in ('', 'NAVL'):
            _qty_hex = _map['IQT']
            _qty = int(_map['IQT'], 16)
    except Exception:
        _qty = None
        _qty_hex = None
    return (_num, _name, _qty, _doc, _qty_hex)


def _parse_epson_remote_payload(command, raw):
    """Dispatch STRICT di/st/ia/ii (passe 2, protocole reel).

    - 'di' : identification -> [] (pas de consommable).
    - 'st' : trame @BDC ST2 TLV stricte uniquement (ftype 0x0f ->
      niveaux ; 0x37 maintenance box informative UNKNOWN).
      PAS de scan 'octets 0..100', PAS de 'BK:80'.
    - 'ia' : liste SANS niveau -> UNKNOWN (None), jamais devine.
    - 'ii' : slot k:v (IQT/IC1, II=='03') -> METADONNEE ink_quantity,
      niveau TOUJOURS UNKNOWN (le % vient de ST2 0x0F).
    Dans le doute -> None (une fausse valeur est pire qu'UNKNOWN).
    """
    if command == 'di':
        return []
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.encode('latin-1', errors='ignore')
    if not isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw)
        except Exception:
            return []
    raw = bytes(raw)
    if not raw:
        return []
    if command == 'st':
        _st2 = _parse_epson_st2(raw)
        if not _st2 or not _st2.get('ink_level'):
            return []
        out = []
        for (_c, _ic, _name, _ink_name, _level) in _st2['ink_level']:
            try:
                _lvl = int(_level)
            except Exception:
                continue
            if _lvl < 0 or _lvl > 100:
                out.append({'description': 'Epson %s' % _name,
                            'level': None, 'max_capacity': None, '_src': 'remote-st2'})
                continue
            out.append({'description': 'Epson %s (%s)' % (_name, _ink_name),
                        'level': _lvl, 'max_capacity': 100, '_src': 'remote-st2'})
        for _k in sorted(k for k in _st2 if str(k).startswith('maintenance_box_')):
            try:
                _v = int(_st2[_k])
            except Exception:
                continue
            _label = {0: 'not full', 1: 'near full', 2: 'full'}.get(_v, 'unknown (%d)' % _v)
            out.append({'description': 'Epson Maintenance Box (%s)' % _label,
                        'level': None, 'max_capacity': None, '_src': 'remote-st2'})
        return out
    if command == 'ia':
        _items = _parse_epson_ia(raw)
        if not _items:
            return []
        return [{'description': 'Epson %s' % str(_n)[:40],
                 'level': None, 'max_capacity': None, '_src': 'remote-ia'}
                for _n in _items[:8]]
    if command == 'ii':
        _slot = _parse_epson_ii(raw)
        if not _slot:
            return []
        # Passe 3 : IQT = ink_quantity (metadonnee), niveau TOUJOURS UNKNOWN.
        # Le niveau (%) vient exclusivement du champ 0x0F de ST2.
        _num, _name, _qty, _doc = _slot[0], _slot[1], _slot[2], _slot[3]
        _qty_hex = _slot[4] if len(_slot) > 4 else None
        _desc = 'Epson %s' % _name
        if _qty is not None and _qty_hex:
            _desc = '%s (IQT %s)' % (_desc, _qty_hex)
        return [{'description': _desc,
                 'level': None,
                 'max_capacity': None,
                 'ink_quantity': _qty,
                 'ink_quantity_hex': _qty_hex,
                 '_src': 'remote-ii'}]
    return []


def read_epson_remote_mode(device, community, port):
    """Fallback Epson Remote Mode : di/st/ia/ii sous ...44.1.1.2.1.

    Isole dans le module Epson : aucun impact autres constructeurs.
    Retourne [] si aucune commande ne fournit de niveaux exploitables.
    """
    collected = []
    # Payloads EXACTS (epctrl_snmp_oid) : di=1, st=1, ia=0,
    # ii=[1, slot] slot 1..8. Hors spec = jamais interroge.
    _plan = [('di', _epson_remote_oid('di', 1), None),
             ('st', _epson_remote_oid('st', 1), None),
             ('ia', _epson_remote_oid('ia', 0), None)]
    for _slot in range(1, 9):
        _plan.append(('ii', _epson_ii_slot_oid(_slot), _slot))
    for _cmd, _oid, _slot in _plan:
        if not _oid:
            continue
        try:
            _raw_val = device.snmp_get(community, port, _oid)
        except Exception:
            continue
        if _raw_val is None or _raw_val == "":
            continue
        _raw_bytes = None
        if isinstance(_raw_val, (bytes, bytearray)):
            _raw_bytes = bytes(_raw_val)
        else:
            try:
                _rows = device.snmp_walk_raw(community, port, _oid)
                if _rows:
                    _first = _rows[0][2] if len(_rows[0]) == 3 else _rows[0][1]
                    if isinstance(_first, (bytes, bytearray)):
                        _raw_bytes = bytes(_first)
                    else:
                        _raw_bytes = str(_raw_val).encode("latin-1", errors="ignore")
                else:
                    _raw_bytes = str(_raw_val).encode("latin-1", errors="ignore")
            except Exception:
                try:
                    _raw_bytes = str(_raw_val).encode("latin-1", errors="ignore")
                except Exception:
                    continue
        _parsed = _parse_epson_remote_payload(_cmd, _raw_bytes)
        logging.debug("Epson Remote %s (%s): %d parsed", _cmd, _oid, len(_parsed))
        collected.extend(_parsed)
    if not collected:
        return []
    try:
        collected.sort(key=lambda e: _EPSON_SOURCE_SCORE.get(str(e.get('_src', 'remote')), 2))
    except Exception:
        pass
    _seen = set()
    supplies = []
    for _i, _entry in enumerate(collected):
        try:
            try:
                _lv = _entry.get("level", -1)
                _lv = int(_lv) if _lv is not None else -1
            except Exception:
                _lv = -1
            _key = (str(_entry.get("description", "")).lower(), _lv)
            if _key in _seen:
                continue
            _seen.add(_key)
            _mx = _entry.get("max_capacity")
            supplies.append(PrinterSupply(
                f"epson-remote-{_i + 1}", _entry.get("description", f"Epson Ink {_i + 1}"),
                _entry.get("level"), _mx, "ink",
            ))
            try:
                supplies[-1]._epson_source = _entry.get("_src", "remote")
                if "ink_quantity" in _entry:
                    supplies[-1]._epson_iqt = _entry.get("ink_quantity")
                if "ink_quantity_hex" in _entry:
                    supplies[-1]._epson_iqt_hex = _entry.get("ink_quantity_hex")
            except Exception:
                pass
        except Exception:
            continue
    return supplies


def _merge_epson_supplies(sources):
    """Fusionne les sources Epson (score, [PrinterSupply]) en une liste UI.

    Regles (passe finale, cote Epson uniquement) :
    - une entree avec niveau valide (is_valid_level) prime toujours sur
      une entree UNKNOWN de meme couleur ;
    - une entree UNKNOWN complete une couleur absente (cartouche
      detectee via ia/ii, maintenance box ST2 0x37) au lieu d'etre
      jetee : l'UI affiche 'Niveau inconnu' (chip grise) ;
    - deduplique par couleur normalisee (black/cyan/magenta/yellow/...) ;
      les libelles non-couleur (maintenance, waste, box) sont gardes ;
    - ne fabrique jamais de niveau : UNKNOWN reste UNKNOWN.
    """
    _COLOR_KEYS = (
        ("black", ("black", "noir", "bk", "k ")),
        ("cyan", ("cyan", "bleu", "c ")),
        ("magenta", ("magenta", "m ")),
        ("yellow", ("yellow", "jaune", "y ")),
        ("light cyan", ("light cyan", "lc ")),
        ("light magenta", ("light magenta", "lm ")),
        ("matte black", ("matte black", "mk ")),
        ("maintenance", ("maintenance",)),
        ("waste", ("waste", "box", "bouteille", "recup")),
    )

    def _ckey(description):
        try:
            _t = " %s " % str(description or "").lower()
        except Exception:
            return None
        for _name, _kws in _COLOR_KEYS:
            for _k in _kws:
                if _k in _t:
                    return _name
        return None

    def _has_level(supply):
        try:
            return bool(supply.is_valid_level())
        except Exception:
            return False

    _best = {}   # ckey -> PrinterSupply (avec niveau si possible)
    _misc = []   # sans couleur identifiable
    for _score, _lst in sorted(sources, key=lambda e: e[0]):
        for _s in _lst or []:
            try:
                _k = _ckey(getattr(_s, "description", ""))
                if _k is None:
                    _misc.append(_s)
                    continue
                _cur = _best.get(_k)
                if _cur is None:
                    _best[_k] = _s
                elif _has_level(_s) and not _has_level(_cur):
                    _best[_k] = _s
                # sinon : on garde l'existant (premier = plus confiant).
            except Exception:
                continue
    _order = ["black", "cyan", "magenta", "yellow", "light cyan",
              "light magenta", "matte black", "maintenance", "waste"]
    _out = [_best[k] for k in _order if k in _best]
    _out += [_s for _k, _s in _best.items() if _k not in _order]
    _out += _misc
    return _out


class EpsonPrinterProvider(PrinterProvider):
    """Provider for Epson printers (generique, extensible).

    Cascade :
      1. Printer-MIB standard (WALK complet, index dynamiques).
      2. Verification de coherence : si partielle (vide, -1/-2/-3,
         descriptions absentes), on ne conclut JAMAIS 'sans consommable'.
      3. Fallback table proprietaire Epson 1.3.6.1.4.1.1248.1.2.2.2.1.1
         (colonnes decouvertes dynamiquement, roles infere's).
      4. Fallback Epson Remote Mode 1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1
         (di/st/ia/ii, payload binaire/texte).
      5. Ancienne table EPSON-MIB 1.3.6.1.4.1.1248.1.1.2.1.3.1.x
         (conservee pour compatibilite).
    Les OID Epson sont des fallbacks Epson uniquement, jamais universels.
    """

    name = "epson"

    # Epson enterprise MIB supply table historique (EPSON-MIB, conservee).
    # epsPrinterSupplyTable = 1.3.6.1.4.1.1248.1.1.2.1.3
    # epsPrinterSupplyEntry = 1.3.6.1.4.1.1248.1.1.2.1.3.1
    EPSON_SUPPLY_OIDS = {
        "description": "1.3.6.1.4.1.1248.1.1.2.1.3.1.1",
        "level": "1.3.6.1.4.1.1248.1.1.2.1.3.1.2",
        "max_capacity": "1.3.6.1.4.1.1248.1.1.2.1.3.1.3",
    }

    def get_supplies(self, device, community, port):
        # Passe finale : FUSION multi-sources Epson (au lieu de cascade
        # exclusive). Decision technique : chaque branche exploitable
        # contribue ; l'echec d'une branche n'empeche jamais d'essayer les
        # suivantes ; les consommables partiels (sans niveau) sont transmis
        # a l'UI (Niveau inconnu) au lieu d'etre jetes.
        # Ordre de confiance : standard coherent > vendor > remote-st2 >
        # remote-ii/ia (metadata) > legacy > partiel UNKNOWN.
        # 1. Standard d'abord (WALK complet).
        std = read_standard_supplies(device, community, port)
        # 2. Coherence : exploitable -> on garde tel quel.
        if _standard_supplies_coherent(std):
            return std
        # 3. Collecte TOUTES les sources Epson sans s'arreter au premier
        #    resultat : vendor, remote (st2+ii+ia), legacy, standard partiel.
        _sources = []  # (score, [PrinterSupply])
        try:
            vendor = read_epson_vendor_table(device, community, port)
            if vendor:
                logging.debug(
                    "Epson vendor MIB success for %s: %d supplies",
                    getattr(device, "ip", "?"), len(vendor),
                )
                _sources.append((1, list(vendor)))
            else:
                logging.debug(
                    "Epson vendor MIB: no usable supplies for %s",
                    getattr(device, "ip", "?"),
                )
        except Exception:
            logging.exception(
                "Epson vendor MIB failed for %s", getattr(device, "ip", "?")
            )
        try:
            remote = read_epson_remote_mode(device, community, port)
            if remote:
                logging.debug(
                    "Epson Remote Mode success for %s: %d supplies",
                    getattr(device, "ip", "?"), len(remote),
                )
                _sources.append((2, list(remote)))
            else:
                logging.debug(
                    "Epson Remote Mode: no usable supplies for %s",
                    getattr(device, "ip", "?"),
                )
        except Exception:
            logging.exception(
                "Epson Remote Mode failed for %s", getattr(device, "ip", "?")
            )
        try:
            legacy = self._read_epson_supplies(device, community, port)
            if legacy:
                _sources.append((5, list(legacy)))
        except Exception:
            logging.exception(
                "Epson legacy MIB failed for %s", getattr(device, "ip", "?")
            )
        if std:
            _sources.append((6, list(std)))
        if not _sources:
            return []
        # 4. Fusion : les niveaux ST2/vendor priment ; les metadata
        #    ii/ia (UNKNOWN) completent les couleurs manquantes au lieu
        #    d'etre jetees. Deduplique par couleur normalisee.
        return _merge_epson_supplies(_sources)

    def _read_epson_supplies(self, device, community, port):
        """Walk the Epson enterprise supply table and return PrinterSupply
        objects for each ink/toner found."""
        try:
            desc_map, level_map, max_map = _walk_columns_concurrent(
                device, community, port,
                [
                    self.EPSON_SUPPLY_OIDS["description"],
                    self.EPSON_SUPPLY_OIDS["level"],
                    self.EPSON_SUPPLY_OIDS["max_capacity"],
                ],
            )
            if not desc_map:
                return []

            supplies = []
            for index, description in desc_map.items():
                try:
                    level_raw = level_map.get(index)
                    max_raw = max_map.get(index)

                    # Epson returns negative codes for unknown/low levels:
                    # -2 = low, -3 = unknown. Treat them as None so the
                    # percentage is not computed from a bogus value.
                    level = _coerce_snmp_int(level_raw)
                    max_capacity = _coerce_snmp_int(max_raw)
                    if level in UNKNOWN_LEVEL_CODES:
                        level = None
                    if max_capacity is not None and max_capacity < 0:
                        max_capacity = None

                    supply = PrinterSupply(
                        f"epson-{index}",
                        description,
                        level,
                        max_capacity,
                        "ink",
                    )
                    try:
                        supply._epson_source = "legacy"
                    except Exception:
                        pass
                    supplies.append(supply)
                except Exception:
                    continue

            logging.debug(
                "Epson enterprise supplies for %s: %s",
                device.ip,
                ", ".join(f"{s.description}={s.level}/{s.max_capacity}" for s in supplies),
            )
            return supplies
        except Exception:
            logging.exception("Failed to read Epson enterprise supplies for %s", device.ip)
            return []


class GenericPrinterProvider(PrinterProvider):
    name = "generic"

    def get_supplies(self, device, community, port):
        return StandardPrinterProvider().get_supplies(device, community, port)


def _walk_columns_concurrent(device, community, port, oid_list):
    """Walk several column OIDs for one device, running the independent walks
    in parallel when the collector is configured to do so and the device is a
    real PrinterDevice (mocks stay serial for determinism).

    Returns a list of dicts ordered exactly as ``oid_list``. The functional
    result of every walk is identical to calling ``device.snmp_walk`` on each
    OID sequentially: each map is keyed by the same instance indexes and holds
    the same raw values.
    """
    opts = _get_snmp_options()
    if (
        not isinstance(device, PrinterDevice)
        or len(oid_list) < 2
        or not opts.get("parallel_walks", True)
    ):
        return [device.snmp_walk(community, port, oid) for oid in oid_list]
    workers = min(opts.get("walk_workers", 4), len(oid_list))
    results = {}

    def _walk(oid):
        return oid, device.snmp_walk(community, port, oid)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pmwalk") as ex:
        futures = [ex.submit(_walk, oid) for oid in oid_list]
        for future in futures:
            oid, walked = future.result()
            results[oid] = walked
    return [results[oid] for oid in oid_list]


class PrinterDevice:
    def __init__(self, ip, tag="", brand="", csv_model="", site="", location="", row_info=None):
        self.ip = ip
        self.tag = tag
        self.brand = brand
        self.csv_model = csv_model
        self.site = site
        self.location = location
        self.row_info = row_info or {}
        self.hostname = ""
        self.model = ""
        self.status = "unknown"
        self.status_code = None
        self.error = ""
        self.reachability = ""
        self.supplies = []
        self.last_update = "-"
        # Per-thread SNMP session state used during a device refresh: one event
        # loop + one SnmpEngine + cached transports per worker thread, so the
        # whole refresh reuses resources instead of rebuilding them per request.
        self._snmp_refresh_state = {}
        self._snmp_session_open = False
        self.machine_alerts = []
        # Événement d'arrêt coopératif du rafraîchissement (bouton « Arrêter »).
        # S'il est défini, la passe s'interrompt proprement entre deux opérations
        # SNMP ; s'il est None, aucun contrôle d'arrêt n'est effectué.
        self._refresh_stop_event = None

    def get_bottle_status(self, alert_threshold=5, full_threshold=0):
        """Return status for the waste/collection bottle based on its *remaining
        capacity* (the percentage displayed by the printer, where 0% = bottle
        full and 100% = bottle empty):
        - pct <= full_threshold (0)   -> "Pleine"  (bottle full, must be emptied)
        - pct <= alert_threshold (5)  -> "Alerte"  (almost full)
        - otherwise                   -> "OK"      (bottle empty / not full)
        """
        keywords = ("bottle", "récup", "recup", "waste", "collect", "récupération", "récupérateur")
        for s in self.supplies:
            desc = (s.description or "").lower()
            if any(k in desc for k in keywords) or (s.supply_type and "waste" in str(s.supply_type).lower()):
                if s.percentage is not None:
                    try:
                        pct = int(s.percentage)
                    except Exception:
                        pct = None
                    if pct is not None:
                        if pct <= full_threshold:
                            return "Pleine"
                        if pct <= alert_threshold:
                            return "Alerte"
                        return "OK"
                if s.is_valid_level():
                    return f"{s.level}"
                return "Niveau inconnu"
        return "None"

    def get_staples_status(self, alert_threshold=10):
        keywords = ("agraf", "agrafe", "staple", "agrafeuse", "agraphe")
        for s in self.supplies:
            desc = (s.description or "").lower()
            if any(k in desc for k in keywords):
                if s.percentage is not None:
                    try:
                        pct = int(s.percentage)
                    except Exception:
                        pct = None
                    if pct is not None:
                        if pct <= alert_threshold:
                            return "Alerte"
                        return "OK"
                if s.is_valid_level():
                    return f"{s.level}"
                return "Niveau inconnu"
        return "None"

    def get_supply_summary(self, category):
        items = [s for s in self.supplies if s.category == category]
        if not items:
            return None

        def _fmt(s):
            if s.display_percentage is not None:
                return f"{s.display_percentage}%"
            if s.is_valid_level():
                return str(s.level)
            return None

        # Special handling for toner: try to produce a color breakdown (C/M/Y/K) when
        # multiple colored toner supplies are detected. Falls back to the previous
        # behaviour (minimum percentage or joined levels) when colours cannot be
        # determined.
        if category == "toner":
            found = {}
            for s in items:
                matched = toner_color_key(s.description)
                if not matched:
                    continue
                val = _fmt(s)
                if val is None:
                    continue
                found[matched] = val
            if found:
                parts = []
                for k in DRUM_COLOR_ORDER:
                    if k in found:
                        parts.append(f"{DRUM_COLOR_CHIPS[k]} {found[k]}")
                if parts:
                    return " ".join(parts)

        # Imaging consumables (photoconductor / drum / imaging unit / imaging
        # kit): a single supply often covers several colours (e.g. a shared CMY
        # imaging kit, or a Black + CMY kit). Show one colour chip per concerned
        # colour. When one SNMP level exists for a shared kit, the SAME percentage
        # is displayed for each of its colours (one common consumable). Single
        # colour consumables display a single chip (e.g. "⬛ 68%").
        if category == "drum":
            chunks = []
            for s in items:
                fmt = _fmt(s)
                if fmt is None:
                    continue
                cols = s.drum_colors()
                chunks.append(" ".join(f"{DRUM_COLOR_CHIPS[c]} {fmt}" for c in cols))
            if chunks:
                return "; ".join(chunks)

        # For non-toner categories, show the value(s) for each supply.
        if category != "toner":
            vals = [(_fmt(s), s) for s in items if _fmt(s) is not None]
            if vals:
                if len(vals) == 1:
                    return vals[0][0]
                return "; ".join(f"{s.short_description()}:{v}" for v, s in vals)
            return "Niveau inconnu"

        # Fallback behaviour for toner: keep the existing (minimum value or joined valid levels)
        vals = [_fmt(s) for s in items if _fmt(s) is not None]
        if vals:
            pcts = [s.percentage for s in items if s.percentage is not None]
            if pcts:
                return f"{min(pcts)}%"
            return "; ".join(v for v in vals if not v.endswith("%"))
        return "Niveau inconnu"

    def refresh(self, community, port, stop_event=None):
        """Interroge l'imprimante en SNMP. ``stop_event`` (facultatif) permet un
        arrêt coopératif : lorsqu'il est levé, la passe s'interrompt proprement
        entre deux opérations et les données déjà récupérées sont conservées.
        Aucune fin de thread brutale n'est utilisée."""
        self._refresh_stop_event = stop_event
        try:
            if not SnmpEngine:
                self.error = "pysnmp introuvable"
                return
            if stop_event is not None and stop_event.is_set():
                # Arrêt demandé avant le moindre interrogatoire : l'imprimante
                # n'est pas interrogée et ses données existantes restent intactes.
                return
            try:
                self.error = ""
                self.supplies = []
                self._begin_snmp_session()
                try:
                    self._do_refresh(community, port)
                finally:
                    self._end_snmp_session()
            except Exception as exc:
                self.error = str(exc)
                exc_text = str(exc).lower()
                if "timeout" in exc_text or "no snmp response" in exc_text:
                    # SNMP timeout: check whether the machine is still reachable via
                    # ICMP ping to give a more accurate status.
                    if _ping_host(self.ip):
                        self.status = "SNMP Timeout - Machine joignable"
                        self.reachability = "snmp_timeout"
                    else:
                        self.status = "Machine hors ligne"
                        self.reachability = "offline"
                else:
                    self.status = "erreur"
                    self.reachability = ""
                self.last_update = time.strftime("%Y-%m-%d %H:%M:%S")
                logging.exception("Exception while refreshing device %s", self.ip)
        finally:
            self._refresh_stop_event = None

    def _refresh_stop_requested(self):
        """True si un arrêt coopératif a été demandé pendant le rafraîchissement
        en cours (bouton « Arrêter »). La logique SNMP existante n'est pas
        modifiée : la passe ne s'interrompt qu'entre deux opérations."""
        ev = getattr(self, "_refresh_stop_event", None)
        return ev is not None and ev.is_set()

    def _quick_probe(self, community, port):
        """Fast reachability check with a very short timeout, run before the
        (heavy) polling of a device so dead machines no longer burn minutes of
        SNMP timeouts.

        Returns (reachable, detail):
          * (True, None)       -> an SNMP response was received;
          * (False, str)       -> no SNMP response (timeout / unreachable);
          * (True, Exception)  -> a mock raised a non-timeout error that must be
                                  re-raised to keep the historic handling.
        """
        # When snmp_get is overridden on the instance (tests / mocks), follow
        # the override so the behaviour stays deterministic.
        if self._snmp_get_is_mocked():
            try:
                self.snmp_get(community, port, SYS_NAME_OID)
                return True, None
            except Exception as exc:
                text = str(exc).lower()
                if "timeout" in text or "no snmp response" in text:
                    return False, str(exc)
                return True, exc
        opts = _get_snmp_options()
        last_error = "No SNMP response received before timeout"
        for mp_model in (1, 0):
            try:
                responses = self._run_getCmd_sync(
                    self._make_transport_factory(port, opts["probe_timeout"], opts["probe_retries"]),
                    self._get_snmp_engine(),
                    CommunityData(community, mpModel=mp_model),
                    ContextData(),
                    ObjectType(ObjectIdentity(SYS_NAME_OID)),
                    lookupMib=False,
                )
            except Exception as exc:
                last_error = str(exc)
                continue
            if not responses:
                continue
            error_indication, error_status, error_index, var_binds = responses[0]
            if error_indication:
                last_error = str(error_indication)
                continue
            return True, None
        return False, last_error

    def _mark_snmp_unreachable(self, detail):
        """Apply the historic SNMP-timeout statuses without running the long
        walks (the printer did not answer the fast probe)."""
        self.error = detail or "No SNMP response received before timeout"
        if _ping_host(self.ip):
            self.status = "SNMP Timeout - Machine joignable"
            self.reachability = "snmp_timeout"
        else:
            self.status = "Machine hors ligne"
            self.reachability = "offline"
        self.last_update = time.strftime("%Y-%m-%d %H:%M:%S")
        logging.warning("SNMP unreachable for %s (fast probe): %s", self.ip, self.error)

    def _do_refresh(self, community, port):
        reachable, probe_detail = self._quick_probe(community, port)
        if not reachable:
            self._mark_snmp_unreachable(probe_detail)
            return
        if isinstance(probe_detail, Exception):
            # Non-timeout error surfaced by a mocked snmp_get: re-raise so the
            # historic exception handling applies unchanged.
            raise probe_detail
        if self._refresh_stop_requested():
            return
        # Read identity first.
        if not self._snmp_get_is_mocked():
            try:
                identity = self.snmp_get_batch(community, port, [SYS_NAME_OID, SYS_DESCR_OID])
            except Exception:
                identity = None
            if identity is not None:
                self.hostname = identity.get(SYS_NAME_OID) or ""
                self.model = identity.get(SYS_DESCR_OID) or ""
            else:
                self.hostname = self.snmp_get(community, port, SYS_NAME_OID) or ""
                self.model = self.snmp_get(community, port, SYS_DESCR_OID) or ""
        else:
            self.hostname = self.snmp_get(community, port, SYS_NAME_OID) or ""
            self.model = self.snmp_get(community, port, SYS_DESCR_OID) or ""
        if self._refresh_stop_requested():
            # Arrêt coopératif : les données déjà récupérées (hostname/modèle)
            # sont conservées telles quelles.
            return
        # Choose provider and fetch supplies
        provider = self._select_provider()
        supplies = provider.get_supplies(self, community, port)
        self.supplies = supplies or []
        if self._refresh_stop_requested():
            # Arrêt coopératif : les consommables déjà récupérés sont conservés.
            return
        # Apply non-destructive vendor adjustments
        try:
            self._adjust_supplies_for_vendor()
        except Exception:
            logging.exception("Vendor adjustment failed")
        if self._refresh_stop_requested():
            return
        status_code, status_label = self._read_status(community, port)
        self.status_code = status_code
        self.status = status_label if status_label is not None else "non disponible"
        self.reachability = ""
        self.last_update = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._refresh_stop_requested():
            # Arrêt coopératif : le statut vient d'être rafraîchi, on s'arrête
            # avant le dernier WALK (alertes machine) pour réagir rapidement.
            return
        self.machine_alerts = self._read_machine_alerts(community, port)

    # Lexmark enterprise alert table (1.3.6.1.4.1.641.6.5.1.1)
    # lexmarkAlertTable: each row describes a machine alert/error.
    #   .6 = description (may contain the error code in brackets, e.g.
    #        "Paper jam, open door C and clear all jammed paper. [230.93]")
    #   .7 = extended info (contains SrvCode=230.93)
    #   .9 = severity — valeurs observées en production : 3 = erreur machine
    #        réelle (ex. bourrage papier), 4 = avertissement de consommable
    #        (ex. photoconducteur bas). Voir le mapping dans _read_machine_alerts.
    LEXMARK_ALERT_OIDS = {
        "description": "1.3.6.1.4.1.641.6.5.1.1.6",
        "info": "1.3.6.1.4.1.641.6.5.1.1.7",
        "severity": "1.3.6.1.4.1.641.6.5.1.1.9",
    }

    # Messages d'attente banals qui ne doivent pas être considérés comme une
    # erreur critique (ex: "Load Tray 1", "Load Tray X", "Load paper", etc.)
    BANAL_WAIT_PATTERNS = (
        r"load\s+tray\s*\d*",
        r"load\s+paper",
        r"load\s+envelope",
        r"load\s+manual",
        r"add\s+paper",
        r"add\s+toner",
        r"replace\s+toner",
        r"install\s+toner",
        r"close\s+door",
        r"close\s+cover",
        r"close\s+top",
        r"close\s+front",
        r"close\s+rear",
        r"close\s+left",
        r"close\s+right",
        r"open\s+door",
        r"open\s+cover",
        r"open\s+top",
        r"open\s+front",
        r"open\s+rear",
        r"open\s+left",
        r"open\s+right",
        r"waiting",
        r"please\s+wait",
        r"warming\s+up",
        r"initializing",
        r"processing",
        r"printing",
        r"copying",
        r"scanning",
        r"faxing",
        r"ready",
        r"standby",
        r"sleep",
        r"power\s+save",
        r"power\s+saving",
        r"low\s+power",
        r"idle",
        r"offline",
        r"online",
        r"connecting",
        r"connected",
        r"disconnected",
        r"network",
        r"ethernet",
        r"wifi",
        r"wireless",
        r"bluetooth",
        r"usb",
        r"parallel",
        r"serial",
        r"firmware",
        r"software",
        r"update",
        r"upgrade",
        r"download",
        r"upload",
        r"install",
        r"uninstall",
        r"configure",
        r"configuration",
        r"settings",
        r"setup",
        r"maintenance",
        r"service",
        r"cleaning",
        r"calibrat",
        r"test\s+page",
        r"demo",
        r"sample",
        r"report",
        r"log",
        r"history",
        r"counter",
        r"page\s+count",
        r"total\s+pages",
        r"total\s+prints",
        r"total\s+copies",
        r"total\s+scans",
        r"total\s+faxes",
    )

    # Mots-clés d'erreur forts : si l'un d'eux est présent dans le message,
    # l'alerte est considérée comme une VRAIE erreur (NON banal), même si elle
    # contient aussi des instructions anodines ("open door C and clear all
    # jammed paper" contient à la fois "open door" ET "jam" -> erreur réelle).
    BANAL_STRONG_ERROR_PATTERNS = (
        r"jam",
        r"misfeed",
        r"stuck",
        r"error",
        r"failure",
        r"fail",
        r"fuser",
        r"overheat",
        r"overheat",
        r"waste\s+toner\s+(full|plein)",
        r"toner\s+low",
        r"toner\s+empty",
        r"low\s+toner",
        r"empty",
        r"paper\s+out",
        r"out\s+of\s+paper",
        r"replace\s+(cartridge|cartouche|drum|fuser|kit|bottle)",
        r"install\s+(cartridge|cartouche|drum|fuser|kit|bottle)",
        r"not\s+detected",
        r"not\s+recognized",
        r"not\s+recognised",
        r"cannot\s+(print|scan|copy|fax)",
        r"can't\s+(print|scan|copy|fax)",
        r"cannot\s+connect",
        r"cannot\s+communicate",
        r"unable\s+to",
        r"unauthorized",
        r"authentication\s+failure",
        r"auth\s+failure",
        r"invalid",
        r"corrupt",
        r"damaged",
        r"overflow",
        r"underflow",
        r"comm\s+error",
        r"hardware\s+error",
        r"firmware\s+error",
        r"software\s+error",
        r"service\s+error",
        r"engine\s+error",
        r"scanner\s+error",
        r"fax\s+error",
        r"memory\s+error",
        r"disk\s+error",
        r"printhead",
        r"carriage",
        r"alignment",
        r"code\s+[0-9]",
        r"srvcode",
        r"\[[0-9]+\.[0-9]+\]",
    )

    def _is_banal_wait_message(self, text):
        """Return True when the alert text is a banal waiting/loading message
        (e.g. "Load Tray 1", "Load paper", "Close door", ...) that should not
        be treated as a critical error.

        Un message contenant des mots-clés d'erreur forts (jam, error, failure,
        toner low, etc.) est toujours considéré comme NON banal, même s'il
        contient aussi des instructions anodines (ex: "Paper jam, open door C
        and clear all jammed paper.")."""
        lower = (text or "").lower()
        # D'abord vérifier si le message contient un mot-clé d'erreur fort.
        for pattern in self.BANAL_STRONG_ERROR_PATTERNS:
            if re.search(pattern, lower):
                return False
        # Ensuite vérifier les patterns d'attente banals.
        for pattern in self.BANAL_WAIT_PATTERNS:
            if re.search(pattern, lower):
                return True
        return False

    def _extract_error_code(self, text, info_text=""):
        """Extract the error code from a Lexmark alert description or info.
        Returns (code, description) where code is the formatted error code
        (e.g. "230.93") and description is the cleaned text without the code.
        """
        code = None
        desc = (text or "").strip()
        # Try to extract from description: "Paper jam ... [230.93]"
        m = re.search(r"\[([0-9]+(?:\.[0-9]+)?)\]", desc)
        if m:
            code = m.group(1)
            desc = re.sub(r"\s*\[[0-9]+(?:\.[0-9]+)?\]\s*$", "", desc).strip()
        # Try to extract from info: "SrvCode=230.93"
        if not code and info_text:
            m = re.search(r"SrvCode=([0-9]+(?:\.[0-9]+)?)", info_text)
            if m:
                code = m.group(1)
        return code, desc

    def _read_machine_alerts(self, community, port):
        """Parse prtAlertTable (1.3.6.1.2.1.43.18) and the Lexmark enterprise
        alert table (1.3.6.1.4.1.641.6.5.1.1) and return machine-generated
        error/warning descriptions as a list of strings.

        For Lexmark printers, the enterprise table provides the error code and
        description (e.g. "Code 230.93 — Paper jam, open door C and clear all
        jammed paper."). Banal waiting messages (Load Tray X, Close door, ...)
        are excluded so they are not treated as critical errors.
        """
        alerts = []
        # --- Standard prtAlertTable ---
        try:
            alert_base = "1.3.6.1.2.1.43.18.1.8.1"
            rows = self.snmp_walk(community, port, alert_base)
            if rows:
                sev_base = "1.3.6.1.2.1.43.18.1.2.1"
                sev_rows = self.snmp_walk(community, port, sev_base)
                # Map index -> severity
                sev_map = {}
                for idx, val in sev_rows.items():
                    try:
                        sev_map[idx] = int(str(val).strip())
                    except Exception:
                        sev_map[idx] = None
                # Sort indices to present alerts in deterministic order
                indices = sorted(rows.keys())
                for idx in indices:
                    text = str(rows[idx]).strip()
                    if not text or text.lower().startswith("no such"):
                        continue
                    if self._is_banal_wait_message(text):
                        continue
                    sev = sev_map.get(idx)
                    sev_text = ""
                    # Sévérité prtAlertSeverity (RFC 3805) :
                    # 1 = other, 2 = informational, 3 = warning, 4 = critical.
                    # Seule une alerte réellement critique (4) reçoit le préfixe
                    # [Critique] ; un avertissement (3) ou une information (2)
                    # ne doit pas être traité comme critique. Une valeur inconnue
                    # ou « other » (1) reste sans préfixe.
                    if sev == 4:
                        sev_text = "Critique"
                    elif sev == 3:
                        sev_text = "Alerte"
                    elif sev == 2:
                        sev_text = "Info"
                    if sev_text:
                        alerts.append(f"[{sev_text}] {text}")
                    else:
                        alerts.append(text)
        except Exception:
            logging.debug("Machine alert read failed for %s", self.ip, exc_info=True)

        # --- Lexmark enterprise alert table ---
        try:
            desc_map = self.snmp_walk(community, port, self.LEXMARK_ALERT_OIDS["description"])
            if desc_map:
                info_map = self.snmp_walk(community, port, self.LEXMARK_ALERT_OIDS["info"])
                sev_map = {}
                try:
                    sev_rows = self.snmp_walk(community, port, self.LEXMARK_ALERT_OIDS["severity"])
                    for idx, val in sev_rows.items():
                        try:
                            sev_map[idx] = int(str(val).strip())
                        except Exception:
                            sev_map[idx] = None
                except Exception:
                    pass
                for idx in sorted(desc_map.keys()):
                    text = str(desc_map[idx]).strip()
                    if not text or text.lower().startswith("no such"):
                        continue
                    if self._is_banal_wait_message(text):
                        continue
                    info_text = str(info_map.get(idx, "") or "")
                    code, desc = self._extract_error_code(text, info_text)
                    sev = sev_map.get(idx)
                    sev_text = ""
                    # Sévérité de la table d'alertes Lexmark (OID entreprise).
                    # Valeurs observées en production sur des appareils réels :
                    #   3 = erreur machine réelle (ex. bourrage papier)
                    #   4 = avertissement de consommable (ex. photoconducteur bas)
                    # On mappe donc 3 -> [Critique] et 4 -> [Alerte], de sorte que
                    # [Critique] ne soit pas attribué automatiquement à de simples
                    # avertissements de consommable. Valeur inconnue : pas de
                    # préfixe de gravité.
                    if sev == 3:
                        sev_text = "Critique"
                    elif sev == 4:
                        sev_text = "Alerte"
                    if code:
                        formatted = f"Code {code} — {desc}"
                    else:
                        formatted = desc
                    if sev_text:
                        alerts.append(f"[{sev_text}] {formatted}")
                    else:
                        alerts.append(formatted)
        except Exception:
            logging.debug("Lexmark machine alert read failed for %s", self.ip, exc_info=True)

        return alerts

    def _begin_snmp_session(self):
        """Open per-thread SNMP sessions (loop + engine + transport cache) so a
        whole device refresh reuses one event loop and one SnmpEngine instead
        of rebuilding them for every single request."""
        self._snmp_refresh_state = {}
        self._snmp_session_open = True

    def _build_session_state(self):
        loop = None
        if inspect.iscoroutinefunction(getCmd):
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            except Exception:
                loop = None
        engine = None
        try:
            engine = SnmpEngine()
        except Exception:
            engine = None
        return {"loop": loop, "engine": engine, "transports": {}}

    def _session_state(self, create=False):
        # The session is always-on and lazily created per thread: the first
        # SNMP operation in a thread builds one event loop + one SnmpEngine +
        # a transport cache that are reused for the whole refresh (pysnmp's
        # documented reuse pattern). This eliminates the ~100ms cost of
        # rebuilding a SnmpEngine for every single request.
        tid = threading.get_ident()
        state = self._snmp_refresh_state
        if tid not in state:
            if not create:
                return None
            state[tid] = self._build_session_state()
        return state[tid]

    def _get_snmp_engine(self):
        st = self._session_state(create=True)
        if st is not None and st.get("engine") is not None:
            return st["engine"]
        return SnmpEngine()

    def _get_snmp_loop(self):
        st = self._session_state(create=True)
        if st is not None:
            return st.get("loop")
        return None

    def _snmp_get_is_mocked(self):
        """Return True when ``snmp_get`` has been overridden on the instance
        (tests / mocks), so the collector follows the mocked behaviour instead
        of doing real network calls."""
        base = PrinterDevice.snmp_get
        try:
            resolved = getattr(self, "snmp_get", None)
        except Exception:
            return True
        try:
            return getattr(resolved, "__func__", resolved) is not base
        except Exception:
            return True

    def _end_snmp_session(self):
        states, self._snmp_refresh_state = self._snmp_refresh_state, {}
        self._snmp_session_open = False
        my_tid = threading.get_ident()
        for tid, st in states.items():
            loop = st.get("loop")
            if loop is None:
                continue
            if tid == my_tid:
                # Cancel pysnmp's lingering dispatcher tasks before closing so
                # the loop shuts down cleanly (no "Task was destroyed" noise).
                self._close_loop(loop)
            else:
                try:
                    loop.close()
                except Exception:
                    pass
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass

    @staticmethod
    def _close_loop(loop):
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    def _detect_vendor(self, _sys_object_id=None, _hr_descr=None):
        """Return the lowercase vendor key from sysDescr/brand/CSV + indices.

        Combine : sysDescr/marque/modele (historique), sysObjectID
        (enterprise 1248 -> epson, sans en dependre exclusivement),
        hrDeviceDescr et OID proprietaires eventuels.
        """
        text = " ".join(
            filter(None, [self.model or "", self.brand or "", self.csv_model or ""])
        ).lower()
        # Epson models often appear as "EPSON WF-7840", "Seiko Epson XP-15000",
        # or just "WF-7840" / "XP-15000" / "ET-4850" without the brand name.
        # Detect the brand first, then fall back to model-prefix heuristics.
        for name in ("brother", "hp", "canon", "epson", "xerox", "ricoh", "kyocera", "lexmark", "samsung"):
            if name in text:
                return name
        # Epson model prefixes (WorkForce, Expression, EcoTank, Stylus, etc.)
        if re.search(r"\b(wf|xp|et|ep|pm|px|stylus|workforce|expression|ecotank|surecolor|sc-)\b", text):
            return "epson"
        # Indices supplementaires : sysObjectID enterprise + hrDeviceDescr.
        try:
            _extra = " ".join(filter(None, [str(_sys_object_id or ""), str(_hr_descr or "")])).lower()
            if _extra:
                for name in ("brother", "hp", "canon", "epson", "xerox", "ricoh", "kyocera", "lexmark", "samsung"):
                    if name in _extra:
                        return name
                if "1.3.6.1.4.1.1248" in _extra or ".1248." in _extra:
                    return "epson"
                if re.search(r"\b(wf|xp|et|ep|pm|px|stylus|workforce|expression|ecotank|surecolor|sc-)\b", _extra):
                    return "epson"
        except Exception:
            pass
        return ""

    def _select_provider(self):
        # Choose provider using sysDescr or brand heuristics. Default to generic.
        vendor = self._detect_vendor()
        if vendor == "brother":
            return BrotherPrinterProvider()
        if vendor == "lexmark":
            return LexmarkPrinterProvider()
        if vendor == "epson":
            return EpsonPrinterProvider()
        if vendor:
            return GenericPrinterProvider()
        return StandardPrinterProvider()

    def _read_status(self, community, port):
        """Read printer status using vendor-aware OID fallback.

        Returns (status_code, status_label). Returns (None, None) when the
        status cannot be determined.

        Some vendors (notably Lexmark, observed in production logs) do not
        implement prtGeneralStatus (1.3.6.1.2.1.43.5.1.1.1) and return
        "No Such Instance" for it. For these vendors, hrDeviceStatus from the
        Host Resources MIB is used as a fallback.
        """
        # Standard Printer-MIB status first (prtGeneralStatus)
        try:
            value = self.snmp_get(community, port, PRINTER_STATUS_OID)
            if value is not None and "no such" not in str(value).lower():
                try:
                    code = int(str(value).strip())
                    return code, STATUS_LABELS.get(code, f"{code}")
                except ValueError:
                    return None, str(value)
        except Exception:
            pass

        # Vendor-specific fallbacks for vendors that do not implement
        # prtGeneralStatus.
        vendor = self._detect_vendor()
        for oid in VENDOR_STATUS_FALLBACKS.get(vendor, []):
            try:
                value = self.snmp_get(community, port, oid)
            except Exception:
                continue
            if value is None or "no such" in str(value).lower():
                continue
            try:
                code = int(str(value).strip())
            except ValueError:
                return None, str(value)
            return code, HR_DEVICE_STATUS_LABELS.get(code, f"{code}")
        return None, None

    def _adjust_supplies_for_vendor(self):
        # Vendor hook for non-destructive adjustments. Keep this method light and
        # avoid synthesizing values. Brother proprietary parsing is performed in
        # refresh() only when standard MIB data is invalid.
        try:
            model_lc = (self.model or "").lower()
            if "brother" in model_lc:
                # As a minimal safety step, if any supply has a max_capacity that
                # is nonsensical (<=0), clear it so it won't produce invalid
                # percentages later. Do NOT invent values here.
                for s in self.supplies:
                    if s.max_capacity is not None and s.max_capacity <= 0:
                        s.max_capacity = None
        except Exception:
            logging.exception("Error in vendor adjustment hook")

    def _read_brother_proprietary(self, community, port):
        """
        Read the Brother proprietary OID and parse the hex-encoded string into
        toner percentages using the documented mapping.

        Returns a list of PrinterSupply objects for toners (C/M/Y/K) or None on
        failure/not present.
        """
        try:
            bro_oid = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0"
            raw = self.snmp_get(community, port, bro_oid)
            if not raw:
                return None
            # raw may be e.g. "81 01 04 00 00 00 64 82 01 04 00 00 00 64 ..."
            # Normalize and split tokens
            tokens = re.findall(r"[0-9A-Fa-f]{2}", str(raw))
            if not tokens:
                # Sometimes prettyPrint returns space-separated hex bytes with
                # 0x or other formatting - try to extract hex pairs nonetheless
                tokens = re.findall(r"\b0x([0-9A-Fa-f]{2})\b", str(raw))
            if not tokens:
                logging.debug("Brother proprietary OID returned non-hex data: %s", raw)
                return None
            logging.debug("Brother proprietary raw tokens: %s", ",".join(tokens))
            # The structure repeats per supply; look for leading supply identifiers
            # 0x81 -> Black (K), 0x82 -> Cyan (C), 0x83 -> Magenta (M), 0x84 -> Yellow (Y)
            code_map = {"81": "K", "82": "C", "83": "M", "84": "Y"}
            supplies = []
            i = 0
            # Walk tokens collecting groups: when a token matches a code in code_map,
            # attempt to take the last byte of that group's fixed-length record as
            # the percentage. The exact group lengths may vary; a robust approach
            # is to use the documented example and take the token that follows a
            # short header until the final byte of that small block. We'll scan for
            # code tokens and, if possible, interpret the token 6 positions later
            # as in the example (this matches 81 ... 64 where 64 is 100).
            while i < len(tokens):
                t = tokens[i].lower()
                if t in code_map:
                    color = code_map[t]
                    # Choose candidate byte: try offset +6 like in example; if out
                    # of range, take the last available token in a small window.
                    candidate_index = i + 6
                    if candidate_index < len(tokens):
                        pct_hex = tokens[candidate_index]
                    else:
                        # fallback: scan next up to 8 tokens and take final one
                        end = min(i + 8, len(tokens) - 1)
                        pct_hex = tokens[end]
                    try:
                        pct = int(pct_hex, 16)
                    except Exception:
                        pct = None
                    if pct is not None:
                        # Clamp to 0..100
                        if pct < 0:
                            pct = 0
                        if pct > 100:
                            pct = 100
                        # Use a descriptive text containing 'Toner' so category detection
                        # picks this up as toner. Set supply_type to 'toner' as well.
                        color_name = {"C": "Cyan", "M": "Magenta", "Y": "Yellow", "K": "Black"}.get(color, color)
                        supplies.append(PrinterSupply(f"bro-{color}", f"{color_name} Toner (Brother)", pct, 100, 'toner'))
                    i += 1
                else:
                    i += 1
            if supplies:
                logging.debug("Parsed Brother proprietary supplies: %s", ",".join(f"{s.description}={s.level}" for s in supplies))
                # Ensure the order is C,M,Y,K for summary display convenience
                order = {"C": 0, "M": 1, "Y": 2, "K": 3}
                supplies_sorted = sorted(supplies, key=lambda s: order.get(s.description.split()[0], 99))
                return supplies_sorted
        except Exception:
            logging.exception("Failed to read or parse Brother proprietary OID")
        return None

    def _make_transport(self, port, timeout, retries):
        # Construct a UdpTransportTarget in a way that's compatible with multiple
        # pysnmp backends/versions. Some versions provide a factory method
        # `create(...)` while others expect direct construction.
        try:
            create = getattr(UdpTransportTarget, "create", None)
            if create and callable(create) and not inspect.iscoroutinefunction(create):
                return create((self.ip, port), timeout, retries)
        except Exception:
            # Fall back to direct construction if factory is not available or fails
            pass
        return UdpTransportTarget((self.ip, port), timeout, retries)

    async def _make_transport_target(self, port, timeout, retries):
        # Async factory for pysnmp 7.x where UdpTransportTarget.create is a
        # coroutine. Must be awaited inside the same event loop as the command
        # so the transport is bound to a live loop.
        try:
            create = getattr(UdpTransportTarget, "create", None)
            if create and callable(create) and inspect.iscoroutinefunction(create):
                return await create((self.ip, port), timeout, retries)
        except Exception:
            pass
        return UdpTransportTarget((self.ip, port), timeout, retries)

    def _run_getCmd_sync(self, transport_factory, *args, **kwargs):
        # Run getCmd synchronously even if the underlying implementation is async.
        # The transport is created by transport_factory inside the same event
        # loop as the command so it is bound to a live loop (pysnmp 7.x).
        # The transport must be inserted as the 3rd positional argument
        # (after SnmpEngine and authData, before contextData).
        if inspect.iscoroutinefunction(getCmd):
            async def _runner():
                transport = transport_factory()
                if inspect.iscoroutine(transport):
                    transport = await transport
                full_args = list(args)
                full_args.insert(2, transport)
                results = []
                gen = getCmd(*full_args, **kwargs)
                if inspect.iscoroutine(gen):
                    gen = await gen
                try:
                    async for item in gen:
                        results.append(item)
                except TypeError:
                    # Not an async iterable; treat gen as a single result
                    results.append(gen)
                return results
            loop = self._get_snmp_loop()
            if loop is not None:
                return loop.run_until_complete(_runner())
            return asyncio.run(_runner())
        else:
            transport = transport_factory()
            if inspect.iscoroutine(transport):
                raise RuntimeError("Async transport factory used with sync getCmd")
            full_args = list(args)
            full_args.insert(2, transport)
            iterator = getCmd(*full_args, **kwargs)
            try:
                return [next(iterator)]
            except StopIteration:
                return []

    def _run_nextCmd_sync(self, transport_factory, *args, **kwargs):
        # Run nextCmd synchronously even if the underlying implementation is async.
        # The transport is created inside the same event loop as the command.
        # The transport must be inserted as the 3rd positional argument.
        if inspect.iscoroutinefunction(nextCmd):
            async def _runner():
                transport = transport_factory()
                if inspect.iscoroutine(transport):
                    transport = await transport
                full_args = list(args)
                full_args.insert(2, transport)
                results = []
                gen = nextCmd(*full_args, **kwargs)
                if inspect.iscoroutine(gen):
                    gen = await gen
                try:
                    async for item in gen:
                        results.append(item)
                except TypeError:
                    # Not an async iterable; treat gen as a single result
                    results.append(gen)
                return results
            loop = self._get_snmp_loop()
            if loop is not None:
                return loop.run_until_complete(_runner())
            return asyncio.run(_runner())
        else:
            transport = transport_factory()
            if inspect.iscoroutine(transport):
                raise RuntimeError("Async transport factory used with sync nextCmd")
            full_args = list(args)
            full_args.insert(2, transport)
            iterator = nextCmd(*full_args, **kwargs)
            return list(iterator)

    def _run_bulkCmd_sync(self, transport_factory, *args, **kwargs):
        # Run a GETBULK request synchronously regardless of the backend.
        # ``args`` layout before transport insertion:
        #   (SnmpEngine, authData, contextData, nonRepeaters, maxRepetitions, *ObjectType)
        # The transport must be inserted as the 3rd positional argument.
        if inspect.iscoroutinefunction(_BULK_CMD):
            async def _runner():
                transport = transport_factory()
                if inspect.iscoroutine(transport):
                    transport = await transport
                full_args = list(args)
                full_args.insert(2, transport)
                results = []
                gen = _BULK_CMD(*full_args, **kwargs)
                if inspect.iscoroutine(gen):
                    gen = await gen
                try:
                    async for item in gen:
                        results.append(item)
                except TypeError:
                    # Not an async iterable; treat gen as a single result
                    results.append(gen)
                return results
            loop = self._get_snmp_loop()
            if loop is not None:
                return loop.run_until_complete(_runner())
            return asyncio.run(_runner())
        else:
            transport = transport_factory()
            if inspect.iscoroutine(transport):
                raise RuntimeError("Async transport factory used with sync bulkCmd")
            full_args = list(args)
            full_args.insert(2, transport)
            iterator = _BULK_CMD(*full_args, **kwargs)
            try:
                return [next(iterator)]
            except StopIteration:
                return []

    def _make_transport_factory(self, port, timeout, retries):
        """Return a factory that creates a transport either synchronously or
        asynchronously depending on the pysnmp backend. During a refresh
        session the transport is cached per thread so the same UDP socket is
        reused across the device's requests (standard pysnmp reuse pattern)."""
        st = self._session_state(create=True)
        if st is not None:
            key = (port, timeout, retries)

            def _factory():
                cached = st["transports"].get(key)
                if cached is not None:
                    return cached
                transport = self._make_transport(port, timeout, retries)
                st["transports"][key] = transport
                return transport

            if inspect.iscoroutinefunction(getCmd):
                async def _afactory():
                    cached = st["transports"].get(key)
                    if cached is not None:
                        return cached
                    transport = await self._make_transport_target(port, timeout, retries)
                    st["transports"][key] = transport
                    return transport
                return _afactory
            return _factory
        if inspect.iscoroutinefunction(getCmd):
            # Async backend: the factory must return a coroutine so the transport
            # is created inside the live event loop.
            return lambda: self._make_transport_target(port, timeout, retries)
        return lambda: self._make_transport(port, timeout, retries)

    def _snmp_get_impl(self, community, port, oid, mp_model):
        """Core SNMP GET implementation for a given SNMP version (mp_model)."""
        opts = _get_snmp_options()
        results = self._run_getCmd_sync(
            self._make_transport_factory(port, opts["get_timeout"], opts["get_retries"]),
            self._get_snmp_engine(),
            CommunityData(community, mpModel=mp_model),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lookupMib=False,
        )
        if not results:
            return None
        error_indication, error_status, error_index, var_binds = results[0]
        if error_indication:
            raise RuntimeError(str(error_indication))
        if error_status:
            raise RuntimeError(f"{error_status.prettyPrint()} at {error_index}")
        if var_binds:
            try:
                pretty = "; ".join(f"{vb[0].prettyPrint()}={vb[1].prettyPrint()}" for vb in var_binds)
            except Exception:
                pretty = str(var_binds)
            logging.debug("SNMP get var_binds for %s oid %s: %s", self.ip, oid, pretty)
            try:
                _vo = var_binds[0][1]
                if isinstance(_vo, (bytes, bytearray)):
                    return bytes(_vo)
                try:
                    _raw_try = bytes(_vo.asOctets()) if hasattr(_vo, "asOctets") else None
                    _typ_try = type(_vo).__name__
                    if _raw_try is not None and _typ_try in ("OctetString", "Opaque"):
                        try:
                            _raw_try.decode("utf-8")
                        except Exception:
                            return bytes(_raw_try)
                except Exception:
                    pass
            except Exception:
                pass
            return var_binds[0][1].prettyPrint()
        return None

    def snmp_diagnostic_dump(self, community, port):
        """Rapport diagnostic (Printer/sysDescr/sysObjectID, MIB brute,
        Epson brute, Supplies, Fallbacks). Ne leve jamais."""
        _lines = []
        try:
            _sys_descr = self.snmp_get(community, port, SYS_DESCR_OID)
        except Exception:
            _sys_descr = None
        try:
            _sys_oid = self.snmp_get(community, port, SYS_OBJECT_ID_OID)
        except Exception:
            _sys_oid = None
        _lines.append(f"Printer: {self.model or self.hostname or self.ip}")
        _lines.append(f"sysObjectID: {_sys_oid}")
        _lines.append(f"sysDescr: {_sys_descr}")
        _lines.append("")
        _std_status = "empty"
        _eps_status = "skipped"
        _rem_status = "skipped"
        try:
            _lines.append("[Printer-MIB]")
            for _col in ("1.3.6.1.2.1.43.11.1.1.6",
                         "1.3.6.1.2.1.43.11.1.1.8",
                         "1.3.6.1.2.1.43.11.1.1.9"):
                try:
                    _rows = self.snmp_walk_raw(community, port, _col)
                except Exception as _e:
                    _lines.append(f"OID {_col} ERROR {_e}")
                    continue
                if not _rows:
                    _lines.append(f"OID {_col} (no rows)")
                    continue
                for _r in _rows:
                    try:
                        _o, _t, _v = _r if len(_r) == 3 else (_r[0], "?", _r[1])
                    except Exception:
                        continue
                    _lines.append(f"OID {_o} TYPE {_t} VALUE {_v!r}")
            _lines.append("")
        except Exception:
            pass
        try:
            _vendor = self._detect_vendor(_sys_oid, None)
            _lines.append("[Epson]")
            _plan = [('di', _epson_remote_oid('di', 1)),
                     ('st', _epson_remote_oid('st', 1)),
                     ('ia', _epson_remote_oid('ia', 0))]
            for _slot in range(1, 9):
                _plan.append(('ii%d' % _slot, _epson_ii_slot_oid(_slot)))
            for _cmd, _oid in _plan:
                try:
                    _rv = self.snmp_get(community, port, _oid)
                except Exception as _e:
                    _rv = f"ERROR {_e}"
                _lines.append(f"REMOTE {_cmd} OID {_oid} VALUE {_rv!r}")
            try:
                _erows = self.snmp_walk_raw(community, port,
                                            EPSON_SUPPLY_TABLE_BASE)
                _eps_status = f"{len(_erows)} rows" if _erows else "empty"
                for _r in (_erows or [])[:50]:
                    try:
                        _o, _t, _v = _r if len(_r) == 3 else (_r[0], "?", _r[1])
                    except Exception:
                        continue
                    _lines.append(f"OID {_o} TYPE {_t} VALUE {_v!r}")
            except Exception as _e:
                _lines.append(f"OID {EPSON_SUPPLY_TABLE_BASE} ERROR {_e}")
            _lines.append("")
        except Exception:
            pass
        try:
            _prov = self._select_provider()
            _sup = _prov.get_supplies(self, community, port)
            _lines.append("[Supplies]")
            for _s in _sup or []:
                _lines.append(
                    f"index={_s.index} description={_s.description} "
                    f"type={_s.supply_type} level={_s.level} "
                    f"max={_s.max_capacity}")
            if not _sup:
                _lines.append("(no supplies)")
            else:
                _ok = _standard_supplies_coherent(_sup)
                _std_status = "success" if _ok else "partial"
            _lines.append("")
            _lines.append("[Fallback]")
            _lines.append(f"standard Printer-MIB: {_std_status}")
            _lines.append(f"Epson vendor MIB: {_eps_status}")
            _lines.append(f"Remote Mode ST2: {_rem_status}")
        except Exception as _e:
            _lines.append(f"[Supplies] ERROR {_e}")
        return "\n".join(_lines)

    def epson_dump_raw_rows(self, community, port):
        """Lignes brutes WF-7620 : OID/type/bytes/texte/index/parse/source."""
        rows = []
        def _row(oid, typ, val, source, parsed=None, index=None):
            try:
                _b = bytes(val) if isinstance(val, (bytes, bytearray)) else None
            except Exception:
                _b = None
            try:
                _t = _coerce_snmp_text(val)
            except Exception:
                _t = ''
            rows.append({'oid': str(oid), 'type': str(typ),
                         'raw_bytes': _b,
                         'raw_hex': (_b.hex(' ') if _b is not None else ''),
                         'raw_text': _t, 'index': index,
                         'parsed': parsed, 'source': source})
            return rows[-1]
        for _col, _src in (('1.3.6.1.2.1.43.11.1.1.6', 'Printer-MIB'),
                            ('1.3.6.1.2.1.43.11.1.1.8', 'Printer-MIB'),
                            ('1.3.6.1.2.1.43.11.1.1.9', 'Printer-MIB')):
            try:
                _rs = self.snmp_walk_raw(community, port, _col)
            except Exception:
                continue
            for _r in _rs or []:
                try:
                    _o, _t, _v = _r if len(_r) == 3 else (_r[0], '?', _r[1])
                except Exception:
                    continue
                _idx = str(_o)[len(_col):].strip('.') if str(_o).startswith(_col) else ''
                _row(_o, _t, _v, _src, _coerce_snmp_int(_v), _idx)
        try:
            _rs = self.snmp_walk_raw(community, port, EPSON_SUPPLY_TABLE_BASE)
        except Exception:
            _rs = []
        for _r in _rs or []:
            try:
                _o, _t, _v = _r if len(_r) == 3 else (_r[0], '?', _r[1])
            except Exception:
                continue
            _row(_o, _t, _v, 'Epson Vendor', _coerce_snmp_int(_v), str(_o))
        _plan = [('di', _epson_remote_oid('di', 1)),
                 ('st', _epson_remote_oid('st', 1)),
                 ('ia', _epson_remote_oid('ia', 0))]
        for _slot in range(1, 9):
            _plan.append(('ii%d' % _slot, _epson_ii_slot_oid(_slot)))
        for _cmd, _oid in _plan:
            try:
                _v = self.snmp_get(community, port, _oid)
            except Exception as _e:
                _row(_oid, 'ERROR', str(_e), 'Epson Remote', None, _cmd)
                continue
            _typ = type(_v).__name__ if _v is not None else 'None'
            if _cmd == 'di':
                _row(_oid, _typ, _v, 'Epson Remote', _parse_epson_di(_v), _cmd)
            elif _cmd == 'st':
                _row(_oid, _typ, _v, 'Epson Remote', _parse_epson_st2(_v), _cmd)
            elif _cmd == 'ia':
                _row(_oid, _typ, _v, 'Epson Remote', _parse_epson_ia(_v), _cmd)
            else:
                _row(_oid, _typ, _v, 'Epson Remote', _parse_epson_ii(_v), _cmd)
        return rows


    def snmp_get(self, community, port, oid):
        # Try SNMPv2c first, then fall back to SNMPv1. Some printers (notably
        # older Epson models) only respond to SNMPv1.
        try:
            return self._snmp_get_impl(community, port, oid, mp_model=1)
        except Exception as exc:
            logging.debug("SNMPv2c GET failed for %s oid %s: %s", self.ip, oid, exc)
            try:
                return self._snmp_get_impl(community, port, oid, mp_model=0)
            except Exception:
                raise

    def snmp_get_batch(self, community, port, oids):
        """Fetch several scalar OIDs in a single SNMP GET (SNMPv2c then SNMPv1).

        Returns a ``{requested_oid: value}`` dict (values are the same strings
        ``snmp_get`` would return). Raises on total transport failure so the
        caller can fall back to individual GETs: some legacy agents reject
        multi-OID GET PDUs.
        """
        opts = _get_snmp_options()
        object_types = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        for mp_model in (1, 0):
            try:
                responses = self._run_getCmd_sync(
                    self._make_transport_factory(port, opts["get_timeout"], opts["get_retries"]),
                    self._get_snmp_engine(),
                    CommunityData(community, mpModel=mp_model),
                    ContextData(),
                    *object_types,
                    lookupMib=False,
                )
            except Exception:
                continue
            if not responses:
                continue
            error_indication, error_status, error_index, var_binds = responses[0]
            if error_indication:
                continue
            if error_status:
                # Agent refused the combined GET -> caller falls back to
                # individual GETs (identical to the previous per-OID behaviour).
                raise RuntimeError(f"{error_status.prettyPrint()} at {error_index}")
            values = []
            for var_bind in var_binds:
                try:
                    values.append(var_bind[1].prettyPrint())
                except Exception:
                    values.append(str(var_bind[1]) if len(var_bind) > 1 else "")
            return {
                oid: (values[i] if i < len(values) else None)
                for i, oid in enumerate(oids)
            }
        raise RuntimeError("No SNMP response received before timeout")

    def _iter_snmp_walk(self, community, port, root_oid, mp_model):
        """Dispatch to the fastest available walk engine.

        GETBULK (SNMPv2c+) fetches up to ``max_repetitions`` rows per request
        and is the preferred engine; the GETNEXT engine below is kept for
        SNMPv1 (which has no GETBULK) and as a transparent fallback whenever an
        agent mishandles GETBULK. Both engines emit exactly the same per-row
        tuples ``(oid_name, type_info, value_text, is_exception)``.
        """
        if self._can_use_bulk() and mp_model == 1:
            yield from self._iter_snmp_walk_bulk(community, port, root_oid, mp_model)
        else:
            yield from self._iter_snmp_walk_getnext(community, port, root_oid, mp_model)

    def _iter_snmp_walk_getnext(self, community, port, root_oid, mp_model):
        """Real SNMP GETNEXT WALK generator.

        Emits one tuple per received var-bind:
            (oid_name, type_info, value_text, is_exception)

        pysnmp's next_cmd only performs a *single* GETNEXT, so on its own it can
        never walk a whole branch (it returns just the first OID). This method
        therefore loops: it issues one GETNEXT per OID, starts from root_oid and
        always resumes from the last OID returned, and stops only when:

          * the returned OID leaves the requested subtree (end of that branch);
          * the agent reports endOfMibView (genuine end of MIB);
          * an SNMPv1 noSuchName error is received (end of MIB for v1);
          * a transport / request error occurs (logged, walk ends cleanly);
          * an internal safety guard (max iterations) trips.

        Exception values in the middle of a branch do NOT stop the walk
        prematurely: endOfMibView is the real end, while noSuchObject /
        noSuchInstance rows are emitted (with is_exception=True) and the walk
        resumes from that OID.
        """
        opts = _get_snmp_options()
        oid_prefix = str(root_oid).strip()
        if not oid_prefix.endswith("."):
            oid_prefix += "."
        current = str(root_oid).strip()
        guard = 0
        max_iter = 20000  # hard cap to avoid an infinite loop against a faulty agent
        while guard < max_iter:
            guard += 1
            if self._refresh_stop_requested():
                # Arrêt coopératif (bouton « Arrêter ») : on coupe proprement le
                # walk entre deux requêtes SNMP. Les lignes déjà collectées sont
                # conservées par l'appelant.
                logging.debug("SNMP walk for %s oid %s: stop requested, ending walk", self.ip, root_oid)
                return
            logging.debug("SNMP walk request for %s oid %s", self.ip, current)
            try:
                responses = self._run_nextCmd_sync(
                    self._make_transport_factory(port, opts["walk_timeout"], opts["walk_retries"]),
                    self._get_snmp_engine(),
                    CommunityData(community, mpModel=mp_model),
                    ContextData(),
                    ObjectType(ObjectIdentity(current)),
                    lookupMib=False,
                )
            except Exception as exc:
                logging.warning("SNMP walk for %s oid %s: request error %s -> stopping walk",
                                self.ip, root_oid, exc)
                return
            if not responses:
                logging.debug("SNMP walk for %s oid %s: no response, ending walk", self.ip, root_oid)
                return
            error_indication, error_status, error_index, var_binds = responses[0]
            if error_indication:
                # A mid-walk transport error must not discard rows already
                # collected: log it and stop cleanly rather than raising.
                logging.warning("SNMP walk for %s oid %s: error %s at %s -> stopping walk",
                                self.ip, root_oid, error_indication, current)
                return
            try:
                status = int(error_status) if error_status else 0
            except Exception:
                status = 0
            if status != 0:
                if status == 2:
                    # SNMPv1 noSuchName marks the end of the MIB for that agent.
                    logging.debug("SNMP walk for %s oid %s: SNMPv1 noSuchName (end of MIB)",
                                  self.ip, root_oid)
                    return
                logging.warning("SNMP walk for %s oid %s: errorStatus %s -> stopping walk",
                                self.ip, root_oid,
                                getattr(error_status, "prettyPrint", lambda: str(error_status))())
                return
            if not var_binds:
                logging.debug("SNMP walk for %s oid %s: empty var-binds, ending walk", self.ip, root_oid)
                return
            # A GETNEXT response normally carries a single var-bind, but loop
            # defensively over every one and resume from the furthest OID seen.
            for var_bind in var_binds:
                try:
                    oid_name = var_bind[0].prettyPrint()
                except Exception:
                    oid_name = str(var_bind[0])
                try:
                    value_obj = var_bind[1]
                except Exception:
                    value_obj = None
                if value_obj is None:
                    value_text = ""
                    type_info = "?"
                    is_exc = False
                else:
                    try:
                        value_text = value_obj.prettyPrint()
                    except Exception:
                        value_text = str(value_obj)
                    type_info = type(value_obj).__name__
                    is_exc = _is_snmp_exception(value_obj)
                logging.debug("SNMP walk received for %s oid %s : %s", self.ip, oid_name, value_text)
                if _is_end_of_mib(value_obj):
                    # Genuine end of MIB/branch - this is the real end of the walk.
                    logging.debug("SNMP walk for %s oid %s : endOfMibView, real end of walk",
                                  self.ip, root_oid)
                    return
                if not oid_name.startswith(oid_prefix):
                    # The returned OID left the requested branch: the subtree is done.
                    logging.debug("SNMP walk for %s oid %s : got %s outside branch, ending walk",
                                  self.ip, root_oid, oid_name)
                    return
                yield oid_name, type_info, value_text, is_exc
                current = oid_name
        logging.warning("SNMP walk for %s oid %s: exceeded %d GETNEXT, aborting walk",
                        self.ip, root_oid, max_iter)

    def _can_use_bulk(self):
        """True when GETBULK is available on the backend and the device is not
        a test mock (a subclassed device with overridden transport/next-cmd
        helpers)."""
        if _BULK_CMD is None:
            return False
        try:
            if type(self)._run_nextCmd_sync is not PrinterDevice._run_nextCmd_sync:
                return False
            if type(self)._make_transport_factory is not PrinterDevice._make_transport_factory:
                return False
        except Exception:
            return False
        return True

    def _iter_snmp_walk_bulk(self, community, port, root_oid, mp_model):
        """GETBULK-based subtree walk.

        Issues one GETBULK per chunk of up to ``max_repetitions`` rows and
        yields the same ``(oid_name, type_info, value_text, is_exception)``
        tuples as the GETNEXT engine. On any agent/transport anomaly the whole
        walk transparently falls back to the GETNEXT engine so no data is ever
        lost.
        """
        oid_prefix = str(root_oid).strip()
        if not oid_prefix.endswith("."):
            oid_prefix += "."
        current = str(root_oid).strip()
        guard = 0
        max_iter = 20000  # hard cap to avoid an infinite loop against a faulty agent
        opts = _get_snmp_options()
        max_rep = max(1, opts.get("max_repetitions", 30))
        while guard < max_iter:
            guard += 1
            if self._refresh_stop_requested():
                # Arrêt coopératif (bouton « Arrêter ») : on coupe proprement le
                # walk getbulk entre deux requêtes SNMP. Les lignes déjà
                # collectées sont conservées par l'appelant.
                logging.debug("SNMP bulk walk for %s oid %s: stop requested, ending walk", self.ip, root_oid)
                return
            logging.debug("SNMP bulk walk request for %s oid %s", self.ip, current)
            try:
                responses = self._run_bulkCmd_sync(
                    self._make_transport_factory(port, opts["walk_timeout"], opts["walk_retries"]),
                    self._get_snmp_engine(),
                    CommunityData(community, mpModel=mp_model),
                    ContextData(),
                    0,
                    max_rep,
                    ObjectType(ObjectIdentity(current)),
                    lookupMib=False,
                )
            except Exception as exc:
                logging.warning("SNMP bulk walk for %s oid %s: request error %s -> GETNEXT fallback",
                                self.ip, root_oid, exc)
                yield from self._iter_snmp_walk_getnext(community, port, root_oid, mp_model)
                return
            if not responses:
                logging.debug("SNMP bulk walk for %s oid %s: no response, ending walk", self.ip, root_oid)
                return
            error_indication, error_status, error_index, var_binds = responses[0]
            if error_indication:
                logging.warning("SNMP bulk walk for %s oid %s: error %s -> GETNEXT fallback",
                                self.ip, root_oid, error_indication)
                yield from self._iter_snmp_walk_getnext(community, port, root_oid, mp_model)
                return
            try:
                status = int(error_status) if error_status else 0
            except Exception:
                status = 0
            if status != 0:
                logging.warning("SNMP bulk walk for %s oid %s: errorStatus %s -> GETNEXT fallback",
                                self.ip, root_oid,
                                getattr(error_status, "prettyPrint", lambda: str(error_status))())
                yield from self._iter_snmp_walk_getnext(community, port, root_oid, mp_model)
                return
            if not var_binds:
                logging.debug("SNMP bulk walk for %s oid %s: empty var-binds, ending walk", self.ip, root_oid)
                return
            last_oid = None
            for var_bind in var_binds:
                try:
                    oid_name = var_bind[0].prettyPrint()
                except Exception:
                    oid_name = str(var_bind[0])
                try:
                    value_obj = var_bind[1]
                except Exception:
                    value_obj = None
                if value_obj is None:
                    value_text = ""
                    type_info = "?"
                    is_exc = False
                else:
                    try:
                        value_text = value_obj.prettyPrint()
                    except Exception:
                        value_text = str(value_obj)
                    type_info = type(value_obj).__name__
                    is_exc = _is_snmp_exception(value_obj)
                logging.debug("SNMP bulk walk received for %s oid %s : %s", self.ip, oid_name, value_text)
                if _is_end_of_mib(value_obj):
                    logging.debug("SNMP bulk walk for %s oid %s : endOfMibView, real end of walk",
                                  self.ip, root_oid)
                    return
                if not oid_name.startswith(oid_prefix):
                    logging.debug("SNMP bulk walk for %s oid %s : got %s outside branch, ending walk",
                                  self.ip, root_oid, oid_name)
                    return
                yield oid_name, type_info, value_text, is_exc
                last_oid = oid_name
            if last_oid is None:
                return
            current = last_oid
        logging.warning("SNMP bulk walk for %s oid %s: exceeded %d GETBULK, aborting walk",
                        self.ip, root_oid, max_iter)

    def _snmp_walk_impl(self, community, port, oid, mp_model):
        """Core SNMP WALK implementation for a given SNMP version (mpModel)."""
        results = {}
        oid_prefix = str(oid).strip()
        if not oid_prefix.endswith("."):
            oid_prefix += "."
        for oid_name, type_info, value_text, is_exc in self._iter_snmp_walk(community, port, oid, mp_model):
            if is_exc:
                # noSuchInstance / noSuchObject rows are skipped (no usable value),
                # but the walk itself has already continued past them.
                continue
            # Extract the complete instance index by taking everything after
            # the column's base OID. This correctly handles multi-sub-id
            # indexes (e.g. "1.1", "2.1") used by some enterprise tables
            # (notably Lexmark consumables).
            if oid_name.startswith(oid_prefix):
                index = oid_name[len(oid_prefix):]
            else:
                index = oid_name.split(".")[-1]
            results[index] = value_text
        return results

    def snmp_walk(self, community, port, oid):
        # Try SNMPv2c first, then fall back to SNMPv1.
        try:
            return self._snmp_walk_impl(community, port, oid, mp_model=1)
        except Exception as exc:
            logging.debug("SNMPv2c WALK failed for %s oid %s: %s", self.ip, oid, exc)
            try:
                return self._snmp_walk_impl(community, port, oid, mp_model=0)
            except Exception:
                raise

    def snmp_walk_raw(self, community, port, oid):
        """Walk the given OID and return every var-bind completely unfiltered as
        a list of (oid_name, type, raw_value) tuples.

        This is a debug helper used by the SNMP diagnostic (Lexmark Debug mode):
        every value is deliberately kept as-is -2, -3, 0, empty strings,
        "No Such Instance" indicators, etc. - and each OID's SNMP type is
        captured so the whole subtree can be inspected (no filtering).
        """
        try:
            return self._snmp_walk_impl_raw(community, port, oid, mp_model=1)
        except Exception as exc:
            logging.debug("SNMPv2c RAW WALK failed for %s oid %s: %s", self.ip, oid, exc)
            try:
                return self._snmp_walk_impl_raw(community, port, oid, mp_model=0)
            except Exception:
                raise

    def _snmp_walk_impl_raw(self, community, port, oid, mp_model):
        """Core raw SNMP WALK returning (oid, type, value) triples without any
        filtering of the returned values."""
        results = []
        # Consume the full GETNEXT walk; noSuchObject / noSuchInstance rows are
        # deliberately kept (is_exception not dropped) so the whole subtree is
        # visible in the debug report. endOfMibView / leaving the branch simply
        # ends the generator, which is the real end of the walk.
        for oid_name, type_info, value_text, is_exc in self._iter_snmp_walk(community, port, oid, mp_model):
            _ = is_exc  # kept: the raw walk preserves every row unfiltered
            results.append((oid_name, type_info, value_text))
        return results
