"""Configuration du logiciel Printer Monitor.

Ce module centralise la configuration par défaut, le chargement/sauvegarde
du fichier JSON et les options de performance SNMP.
"""

import json
import logging
from pathlib import Path

# Warning thresholds (percentage) above which a supply is considered "low"
# (orange indicator) but not yet critical. The critical/replacement thresholds
# (toner/drum) remain separate and user-configurable.
LOW_WARNING_THRESHOLD = 10
DRUM_LOW_WARNING_THRESHOLD = 15
WARNING_WASTE_TONER_THRESHOLD = 5

CONFIG_FILE = Path("printer_monitor_config.json")
DEFAULT_CONFIG = {
    "snmp_community": "public",
    "snmp_port": 161,
    # Chemin du dernier fichier CMDB chargé. Au démarrage, si ce chemin existe
    # toujours et est accessible, la CMDB est rechargée automatiquement sans
    # demander à l'utilisateur de la sélectionner à nouveau.
    "cmdb_path": "",
    # Dernier grand site sélectionné dans le filtre. Restauré au démarrage si
    # le site existe encore dans la CMDB rechargée ; "Tous" (défaut) affiche
    # tout le parc. Mémorisé automatiquement à chaque changement de filtre.
    "selected_site": "Tous",
    "thresholds": {
        "toner": 4,
        "drum": 4,
        "toner_warning": LOW_WARNING_THRESHOLD,
        "drum_warning": DRUM_LOW_WARNING_THRESHOLD,
        "waste_toner": WARNING_WASTE_TONER_THRESHOLD,
    },
    # Exceptions par modèle : permet de définir des seuils de remplacement critiques
    # différents pour certains modèles d'imprimante. La comparaison est insensible
    # à la casse. Chaque entrée définit une liste de modèles et les seuils
    # toner/cartouche et tambour/photoconducteur à appliquer.
    "model_exceptions": [
        {"models": ["X921", "X922", "CS923"], "toner": 5, "drum": 10},
        {"models": ["CS943"], "toner": 10, "drum": 10},
    ],
    # Rafraîchissement automatique de toute la liste. Le compte à rebours ne
    # démarre qu'une fois la CMDB et le Grand site sélectionnés et le chargement
    # initial terminé ; il ne se lance jamais au démarrage du logiciel.
    "auto_refresh": {"enabled": True, "interval_minutes": 60},
    # Historique local (SQLite) : snapshots des imprimeurs après chaque
    # rafraîchissement réussi. ``retention_days`` définit combien de jours
    # d'historique sont conservés (nettoyage automatique).
    "history": {"enabled": True, "retention_days": 14},
    # Certificats HTTPS des imprimantes (tls_certs.py) : sondes TLS en
    # arriere-plan pendant le SNMP, sans installation aveugle.
    "tls": {"enabled": True, "timeout": 3.0, "ports": [443, 8443],
            "cache_ttl_hours": 24, "auto_install_renewals": False},
    # Paramètres TLS / certificats HTTPS des imprimantes (voir tls_certs.py).
    # Les certificats approuvés par une autorité Windows sont suivis sans
    # installation ; les autosignés exigent une approbation explicite.
    # "auto_install_renewals" reste désactivé par défaut (pas d'écriture
    # aveugle dans ROOT) ; "enabled" permet de couper les sondes TLS.
    # de configuration). Des timeouts/retries réduits accélèrent fortement le
    # cycle complet sur les imprimantes lentes ou injoignables (le coût d'une
    # machine hors-ligne est dominée par les temps d'attente SNMP). Les
    # pourcentages/statuts affichés restent identiques quel que soit ce réglage.
    "snmp": {
        "get_timeout": 3,          # secondes, par essai de GET
        "get_retries": 1,          # nombre de retries après le 1er GET
        "walk_timeout": 3,         # secondes, par requête de walk (GETBULK/GETNEXT)
        "walk_retries": 1,         # nombre de retries après la 1re requête de walk
        "probe_timeout": 1.5,      # sondage rapide de joignabilité (avant le refresh complet)
        "probe_retries": 1,
        "max_repetitions": 30,     # rangées par requête GETBULK
        "parallel_walks": True,    # walks des colonnes d'une imprimante en parallèle
        "walk_workers": 4,         # max de walks simultanés par imprimante
    },
}

def _merge_config(loaded):
    """Merge a loaded configuration over DEFAULT_CONFIG so that newly added
    keys (e.g. the configurable warning/waste thresholds) get their defaults
    on existing user config files."""
    merged = {**DEFAULT_CONFIG, **(loaded or {})}
    merged["thresholds"] = {**DEFAULT_CONFIG["thresholds"], **(loaded or {}).get("thresholds", {})}
    if "auto_refresh" in (loaded or {}):
        merged["auto_refresh"] = {**DEFAULT_CONFIG["auto_refresh"], **loaded["auto_refresh"]}
    if "history" in (loaded or {}):
        merged["history"] = {**DEFAULT_CONFIG["history"], **loaded["history"]}
    if "tls" in (loaded or {}):
        merged["tls"] = {**DEFAULT_CONFIG["tls"], **loaded["tls"]}
    # Migration : l'ancien système stockait un hash SHA-256 du mot de passe du
    # mode avancé dans "advanced.password_hash". Ce mécanisme est obsolète et
    # supprimé : le mot de passe administrateur est désormais authentifié par
    # un dérivé scrypt codé en dur dans printer_monitor/auth.py. On ignore
    # donc toute valeur "advanced" présente dans un ancien fichier de config.
    if "advanced" in (loaded or {}):
        merged.pop("advanced", None)
    return merged


def load_json_config():
    if CONFIG_FILE.exists():
        try:
            return _merge_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return {**DEFAULT_CONFIG, "thresholds": {**DEFAULT_CONFIG["thresholds"]}}


def save_json_config(config):
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")
    except Exception as exc:
        logging.exception("Failed to save configuration")
        from tkinter import messagebox
        messagebox.showerror("Sauvegarde impossible", f"Impossible de sauvegarder la configuration : {exc}")


_SNMP_OPTIONS_CACHE = None


def _get_snmp_options():
    """Return the performance/tuning options used by the SNMP collector.

    Loaded once from the JSON configuration (with safe defaults); a restart of
    the application picks up any hand-edit of printer_monitor_config.json, so
    it can be tuned without touching the functional logic.
    """
    global _SNMP_OPTIONS_CACHE
    if _SNMP_OPTIONS_CACHE is None:
        cfg = load_json_config()
        snmp_cfg = cfg.get("snmp", {}) or {}

        def _f(key, default):
            try:
                return float(snmp_cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key, default):
            try:
                return int(snmp_cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        def _b(key, default):
            val = snmp_cfg.get(key, default)
            if isinstance(val, bool):
                return val
            return str(val).lower() in ("1", "true", "yes", "on")

        _SNMP_OPTIONS_CACHE = {
            "get_timeout": max(0.5, _f("get_timeout", 3)),
            "get_retries": max(0, _i("get_retries", 1)),
            "walk_timeout": max(0.5, _f("walk_timeout", 3)),
            "walk_retries": max(0, _i("walk_retries", 1)),
            "probe_timeout": max(0.25, _f("probe_timeout", 1.5)),
            "probe_retries": max(0, _i("probe_retries", 1)),
            "max_repetitions": min(max(1, _i("max_repetitions", 30)), 1000),
            "parallel_walks": _b("parallel_walks", True),
            "walk_workers": min(max(1, _i("walk_workers", 4)), 16),
        }
    return _SNMP_OPTIONS_CACHE