"""Package Printer Monitor.

Ré-exporte les symboles publics pour la compatibilité avec les tests existants
qui importent depuis ``printer_monitor``.
"""

from .auth import verify_advanced_password
from .config import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    DRUM_LOW_WARNING_THRESHOLD,
    LOW_WARNING_THRESHOLD,
    WARNING_WASTE_TONER_THRESHOLD,
    _get_snmp_options,
    load_json_config,
    save_json_config,
)
from .models import PrinterSupply
from .ping import _ping_host
from .snmp import (
    EPSON_REMOTE_COMMANDS,
    EPSON_REMOTE_ROOT,
    EPSON_SUPPLY_TABLE_BASE,
    HR_DEVICE_DESCR_BASE,
    HR_DEVICE_STATUS_LABELS,
    LEXMARK_CONSUMABLE_TYPES,
    LEXMARK_SUPPLY_OIDS,
    PRINTER_STATUS_OID,
    STATUS_LABELS,
    SUPPLY_OIDS,
    SYS_DESCR_OID,
    SYS_NAME_OID,
    SYS_OBJECT_ID_OID,
    UNKNOWN_LEVEL_CODES,
    VENDOR_STATUS_FALLBACKS,
    _EndOfMibView,
    _NoSuchInstance,
    _NoSuchObject,
    _coerce_snmp_int,
    _coerce_snmp_raw_bytes,
    _coerce_snmp_text,
    _epson_ii_slot_oid,
    _epson_remote_oid,
    _infer_epson_vendor_roles,
    _is_end_of_mib,
    _is_snmp_exception,
    _parse_epson_di,
    _parse_epson_ia,
    _parse_epson_ii,
    _parse_epson_remote_payload,
    _parse_epson_st2,
    _merge_epson_supplies,
    _standard_supplies_coherent,
    _walk_columns_concurrent,
    BrotherPrinterProvider,
    EpsonPrinterProvider,
    GenericPrinterProvider,
    LexmarkPrinterProvider,
    PrinterDevice,
    PrinterProvider,
    StandardPrinterProvider,
    read_epson_remote_mode,
    read_epson_vendor_table,
    read_standard_supplies,
    toner_color_key,
)
from .utils import (
    DRUM_COLOR_CHIPS,
    DRUM_COLOR_NAMES,
    DRUM_COLOR_ORDER,
    IP_RE,
    KYOCERA_BLACK_TONER_REFS,
    SUPPLY_RE,
    TONER_COLORS,
    is_kyocera_black_toner_ref,
    normalize_ip,
)
from .widgets import ModelTagsFrame
from .splash import show_splash
from .history import HistoryManager, DEFAULT_RETENTION_DAYS
from .app import PrinterMonitorApp
from .main import main

from ._version import APP_VERSION

__version__ = APP_VERSION