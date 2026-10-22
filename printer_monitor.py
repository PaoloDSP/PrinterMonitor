#!/usr/bin/env python
"""SHIM DE COMPATIBILITE.

Ce fichier a ete conserve pour la retrocompatibilite. Le code reel a ete
reorganise dans le package ``printer_monitor/`` (modules config.py, models.py,
snmp.py, ping.py, utils.py, app.py, widgets.py, splash.py et main.py).

Ce shim re-exporte tous les symboles publics depuis le package afin que les
anciens imports ``from printer_monitor import ...`` et l'execution directe
``python printer_monitor.py`` continuent de fonctionner a l'identique.
"""

from printer_monitor import (
    # Classes / objets principaux
    PrinterDevice,
    PrinterMonitorApp,
    PrinterSupply,
    show_splash,
    ModelTagsFrame,
    # Constantes
    TONER_COLORS,
    DRUM_COLOR_CHIPS,
    DRUM_COLOR_NAMES,
    DRUM_COLOR_ORDER,
    SUPPLY_OIDS,
    LEXMARK_SUPPLY_OIDS,
    LEXMARK_CONSUMABLE_TYPES,
    PRINTER_STATUS_OID,
    SYS_NAME_OID,
    SYS_DESCR_OID,
    STATUS_LABELS,
    VENDOR_STATUS_FALLBACKS,
    HR_DEVICE_STATUS_LABELS,
    DEFAULT_CONFIG,
    LOW_WARNING_THRESHOLD,
    DRUM_LOW_WARNING_THRESHOLD,
    WARNING_WASTE_TONER_THRESHOLD,
    CONFIG_FILE,
    # Fournisseurs
    PrinterProvider,
    StandardPrinterProvider,
    BrotherPrinterProvider,
    LexmarkPrinterProvider,
    EpsonPrinterProvider,
    GenericPrinterProvider,
    # Helpers
    _ping_host,
    _is_snmp_exception,
    _is_end_of_mib,
    _walk_columns_concurrent,
    _coerce_snmp_int,
    _coerce_snmp_text,
    _coerce_snmp_raw_bytes,
    _standard_supplies_coherent,
    _epson_ii_slot_oid,
    _epson_remote_oid,
    _parse_epson_di,
    _parse_epson_ia,
    _parse_epson_ii,
    _parse_epson_remote_payload,
    _parse_epson_st2,
    _merge_epson_supplies,
    _infer_epson_vendor_roles,
    read_standard_supplies,
    read_epson_vendor_table,
    read_epson_remote_mode,
    EPSON_SUPPLY_TABLE_BASE,
    EPSON_REMOTE_ROOT,
    EPSON_REMOTE_COMMANDS,
    SYS_OBJECT_ID_OID,
    HR_DEVICE_DESCR_BASE,
    UNKNOWN_LEVEL_CODES,
    _get_snmp_options,
    load_json_config,
    save_json_config,
    normalize_ip,
    _EndOfMibView,
    _NoSuchInstance,
    _NoSuchObject,
    # Point d'entree
    main,
    __version__,
)

if __name__ == "__main__":
    main()