"""Module d'historique des imprimeurs — persistence SQLite locale.

Ce module est entièrement dédié à l'historique : il ne touche en aucun cas la
logique SNMP, le calcul des seuils ou l'interface principale. Il ne fait que
persister (et lire) des *snapshots* de mesures effectuées par
PrinterMonitorApp après un rafraîchissement **réussi**.

Structure de la base (fichier SQLite sur disque, jamais en RAM) :

    Printer (IP / nom / modèle / statut)
      └── Measurement (horodatage)
            ├── toner / cartouche  (par couleur C/M/Y/K)
            ├── tambour / photoconducteur (par couleur C/M/Y/K)
            └── bouteille de récupération (pourcentage)

Par défaut les enregistrements de plus de 14 jours sont supprimés
automatiquement (réglable via le fichier de configuration).
"""

import logging
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from .utils import DRUM_COLOR_ORDER

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

# Nombre de jours d'historique conservé par défaut.
DEFAULT_RETENTION_DAYS = 14

# Chemin du fichier SQLite (dans le répertoire de travail, comme le fichier
# de configuration JSON).
DB_FILE_NAME = "printer_monitor_history.db"
DB_FILE = Path.cwd() / DB_FILE_NAME

# Mots-clés permettant d'identifier une bouteille de récupération de toner.
_BOTTLE_KEYWORDS = ("bottle", "récup", "recup", "waste", "collect",
                    "récupération", "récupérateur")

# Mots-clés couleur pour les consommables toner / cartouche / encre.
_TONER_COLOR_KEYWORDS = {
    "C": ("cyan", "bleu"),
    "M": ("magenta",),
    "Y": ("yellow", "jaune"),
    "K": ("black", "noir", "ink"),
}


def _supply_color(desc):
    """Déduit la couleur ('C'/'M'/'Y'/'K' ou None) d'un consommable à partir
    de sa description textuelle."""
    text = (desc or "").lower()
    for color, keywords in _TONER_COLOR_KEYWORDS.items():
        if any(k in text for k in keywords):
            return color
    return None


class HistoryManager:
    """Gestionnaire d'historique SQLite, thread-safe.

    Utilisation typique::

        >>> from printer_monitor.history import HistoryManager
        >>> hm = HistoryManager()
        >>> hm.save_measurement(device)          # après un rafraîchissement réussi
        >>> hm.search_printers("192.168")        # recherche imprimantes
        >>> hm.get_measurements(ip)              # mesures d'une imprimante
        >>> hm.cleanup_old_records()             # nettoyage périodique
    """

    def __init__(self, db_path=None, retention_days=DEFAULT_RETENTION_DAYS):
        self.db_path = str(db_path) if db_path else str(DB_FILE)
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self._init_db()

    # ── Base de données ───────────────────────────────────────────────────────

    def _connect(self):
        """Ouvre une connexion SQLite avec l'activation des clés étrangères."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Crée les tables et index si nécessaire."""
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    CREATE TABLE IF NOT EXISTS printers (
                        ip           TEXT PRIMARY KEY,
                        name         TEXT,
                        model        TEXT,
                        csv_model    TEXT,
                        site         TEXT,
                        location     TEXT
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS measurements (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_ip  TEXT NOT NULL,
                        timestamp   TEXT NOT NULL,
                        status      TEXT,
                        FOREIGN KEY (printer_ip) REFERENCES printers(ip)
                                     ON DELETE CASCADE
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS consumables (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        measurement_id INTEGER NOT NULL,
                        category       TEXT,          -- toner | drum | bottle | other
                        description    TEXT,
                        color          TEXT,          -- C | M | Y | K | NULL
                        percentage     INTEGER,
                        FOREIGN KEY (measurement_id) REFERENCES measurements(id)
                                       ON DELETE CASCADE
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_meas_printer ON measurements(printer_ip)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_meas_ts      ON measurements(timestamp)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_cons_meas   ON consumables(measurement_id)")
                conn.commit()
            finally:
                conn.close()

    # ── Sauvegarde ───────────────────────────────────────────────────────────

    def save_measurement(self, device):
        """Persiste un *snapshot* complet d'après un rafraîchissement réussi.

        Ne stocke que les informations nécessaires (IP, nom, modèle, statut,
        pourcentages toner/encre, tambours, bouteille de récupération).
        Aucune donnée temporaire ou redondante n'est conservée.
        """
        supplies = getattr(device, "supplies", []) or []

        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # 1. En-tête imprimante (upsert)
                c.execute("""
                    INSERT OR IGNORE INTO printers
                        (ip, name, model, csv_model, site, location)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    device.ip,
                    device.hostname or device.tag or "",
                    device.model or "",
                    device.csv_model or "",
                    device.site or "",
                    device.location or "",
                ))
                c.execute("""
                    UPDATE printers
                       SET name      = COALESCE(?, name),
                           model     = COALESCE(?, model),
                           csv_model = COALESCE(?, csv_model),
                           site      = COALESCE(?, site),
                           location  = COALESCE(?, location)
                     WHERE ip = ?
                """, (
                    device.hostname or device.tag or "",
                    device.model or "",
                    device.csv_model or "",
                    device.site or "",
                    device.location or "",
                    device.ip,
                ))

                # 2. Mesure
                status = device.status or "unknown"
                c.execute("""
                    INSERT INTO measurements (printer_ip, timestamp, status)
                    VALUES (?, ?, ?)
                """, (device.ip, timestamp, status))
                measurement_id = c.lastrowid

                # 3. Consommables — on ne garde que toner, drum et bouteille
                for supply in supplies:
                    pct = supply.percentage
                    if pct is None:
                        continue
                    category = supply.category
                    desc = supply.description or ""

                    if category == "toner":
                        color = _supply_color(desc) or "K"
                        c.execute("""
                            INSERT INTO consumables
                                (measurement_id, category, description, color, percentage)
                            VALUES (?, ?, ?, ?, ?)
                        """, (measurement_id, "toner", desc, color, pct))
                    elif category == "drum":
                        # Un tambour peut couvrir plusieurs couleurs
                        colors = supply.drum_colors() or ["K"]
                        for color in colors:
                            c.execute("""
                                INSERT INTO consumables
                                    (measurement_id, category, description, color, percentage)
                                VALUES (?, ?, ?, ?, ?)
                            """, (measurement_id, "drum", desc, color, pct))
                    elif supply.is_receptacle():
                        c.execute("""
                            INSERT INTO consumables
                                (measurement_id, category, description, color, percentage)
                            VALUES (?, ?, ?, ?, ?)
                        """, (measurement_id, "bottle", desc, None, pct))
                    # Les autres catégories (developer, fuser, maintenance,
                    # staples, ...) ne sont pas conservées — données non demandées.

                conn.commit()
                logger.debug("History: saved measurement %s for %s",
                             measurement_id, device.ip)
            except Exception:
                logger.exception("History: failed to save measurement for %s",
                                 device.ip)
            finally:
                conn.close()

    # ── Recherche / lecture ───────────────────────────────────────────────────

    @staticmethod
    def _fold(text):
        """Normalise une chaîne pour une recherche insensible à la casse et aux
        accents : « ÉPSON » == « epson », « ÉtA » == « eta », « PARIS » == « paris ».

        Le LIKE de SQLite n'est insensible à la casse que pour l'ASCII : ce
        filtrage Python (casefold + suppression des diacritiques) garantit le
        même résultat pour les caractères accentués (é, è, ê, ç, …).
        """
        text = unicodedata.normalize("NFKD", str(text or ""))
        return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()

    def search_printers(self, query=""):
        """Recherche des imprimeurs par nom, IP, modèle ou site.

        Retourne une liste de lignes (ip, name, model, csv_model, site).
        Si *query* est vide (ou ne contient que des espaces), retourne tous
        les imprimeurs connus.
        """
        needle = self._fold(query).strip()
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT DISTINCT ip, name, model, csv_model, site
                    FROM printers
                    ORDER BY name, ip
                """)
                rows = c.fetchall()
            finally:
                conn.close()

        if not needle:
            return rows

        # Recherche multi-termes : chaque mot du query doit apparaître dans les
        # champs de l'imprimante (ordre quelconque, ex. « xerox printer »).
        tokens = needle.split()
        matched = []
        for row in rows:
            haystack = " ".join(
                self._fold(v) for v in (row["ip"], row["name"], row["model"],
                                        row["csv_model"], row["site"]))
            if all(token in haystack for token in tokens):
                matched.append(row)
        return matched

    def get_printer_info(self, ip):
        """Retourne les métadonnées d'une imprimante (ou None)."""
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("SELECT * FROM printers WHERE ip = ?", (ip,))
                row = c.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_measurements(self, printer_ip):
        """Retourne les mesures d'une imprimante, triées par date décroissante.

        Chaque élément : (id, timestamp, status)
        """
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT id, timestamp, status
                    FROM measurements
                    WHERE printer_ip = ?
                    ORDER BY id DESC
                """, (printer_ip,))
                return c.fetchall()
            finally:
                conn.close()

    def get_measurement(self, measurement_id):
        """Retourne les métadonnées d'une mesure complète."""
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT m.id, m.printer_ip, m.timestamp, m.status,
                           p.name, p.model, p.csv_model, p.site, p.location
                    FROM measurements m
                    JOIN printers p ON p.ip = m.printer_ip
                    WHERE m.id = ?
                """, (measurement_id,))
                row = c.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_consumables(self, measurement_id):
        """Retourne les consommables d'une mesure.

        Chaque élément : (category, description, color, percentage)
        """
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT category, description, color, percentage
                    FROM consumables
                    WHERE measurement_id = ?
                    ORDER BY
                        CASE category
                            WHEN 'toner'  THEN 1
                            WHEN 'drum'   THEN 2
                            WHEN 'bottle' THEN 3
                            ELSE 4
                        END,
                        color NULLS LAST,
                        description
                """, (measurement_id,))
                return c.fetchall()
            finally:
                conn.close()

    def get_timeline(self, printer_ip):
        """Retourne les données de timeline pour une imprimante.

        Liste ordonnée (plus récente en premier) de dicts::

            {id, timestamp, status, consumables}

        où ``consumables`` est une liste de dicts {category, color, percentage}.
        """
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT m.id AS meas_id, m.timestamp, m.status,
                           c.category, c.color, c.percentage
                    FROM measurements m
                    LEFT JOIN consumables c ON c.measurement_id = m.id
                    WHERE m.printer_ip = ?
                    ORDER BY m.id DESC,
                             CASE c.category
                                 WHEN 'toner'  THEN 1
                                 WHEN 'drum'   THEN 2
                                 WHEN 'bottle' THEN 3
                                 ELSE 4
                             END,
                             c.color,
                             c.description
                """, (printer_ip,))
                rows = c.fetchall()

                timeline = []
                current = None
                for row in rows:
                    meas_id = row['meas_id']
                    if current is None or current['id'] != meas_id:
                        if current is not None:
                            timeline.append(current)
                        current = {
                            'id': meas_id,
                            'timestamp': row['timestamp'],
                            'status': row['status'],
                            'consumables': [],
                        }
                    if row['category'] is not None:
                        current['consumables'].append({
                            'category': row['category'],
                            'color': row['color'],
                            'percentage': row['percentage'],
                        })
                if current is not None:
                    timeline.append(current)
                return timeline
            finally:
                conn.close()

    # ── Nettoyage ─────────────────────────────────────────────────────────────

    def cleanup_old_records(self, days=None):
        """Supprime les mesures (et leurs consommables) de plus de *days* jours.

        Retourne le nombre de mesures supprimées.
        """
        if days is None:
            days = self.retention_days
        cutoff = (datetime.now() - timedelta(days=max(1, days))).strftime(
            "%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                deleted = c.execute(
                    "DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
                conn.commit()
                logger.info("History: cleaned up %d old measurement(s) "
                            "(cutoff %s)", deleted.rowcount, cutoff)
                return deleted.rowcount
            except Exception:
                logger.exception("History: cleanup failed")
                return 0
            finally:
                conn.close()

    def clear_all(self):
        """Vide entièrement la base d'historique (imprimantes, mesures,
        consommables).

        Retourne le nombre de mesures supprimées.
        """
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                # Les consommables sont supprimés en cascade via la clé
                # étrangère ON DELETE CASCADE sur measurements.
                deleted = c.execute("DELETE FROM measurements")
                c.execute("DELETE FROM printers")
                conn.commit()
                logger.info("History: cleared all data (%d measurement(s))",
                            deleted.rowcount)
                return deleted.rowcount
            except Exception:
                logger.exception("History: clear_all failed")
                return 0
            finally:
                conn.close()

    def get_stats(self):
        """Retourne des statistiques rapides sur la base d'historique."""
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                counts = {}
                for table in ("printers", "measurements", "consumables"):
                    c.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = c.fetchone()[0]
                c.execute("SELECT MIN(timestamp) FROM measurements")
                oldest = c.fetchone()[0]
                return {
                    **counts,
                    "oldest_timestamp": oldest,
                    "retention_days": self.retention_days,
                    "db_path": self.db_path,
                }
            finally:
                conn.close()


def load_retention_from_config():
    """Lit la durée de rétention depuis le fichier de configuration JSON.

    Retourne ``DEFAULT_RETENTION_DAYS`` si la clé est absente ou invalide.
    """
    try:
        from .config import load_json_config
        cfg = load_json_config()
        days = cfg.get("history", {}).get("retention_days", DEFAULT_RETENTION_DAYS)
        return max(1, int(days))
    except Exception:
        return DEFAULT_RETENTION_DAYS
