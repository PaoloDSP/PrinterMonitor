# Printer Monitor

> Application de bureau **Python / Tkinter** pour surveiller un parc d'imprimantes en **SNMP**.

[![License](https://img.shields.io/badge/license-Source--Available-orange)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![Tkinter](https://img.shields.io/badge/gui-tkinter-green.svg)](https://docs.python.org/3/library/tkinter.html)

**Version :** 1.5 · **Auteur :** Paolo DOS SANTOS PINTO ([www.pdsp.fr](https://www.pdsp.fr/)) · **Plateforme :** Windows (testé), toute plateforme supportant Python/Tk.

---

## Présentation

Printer Monitor est un logiciel de supervision et de gestion d'un parc d'imprimantes. Il interroge chaque appareil en **SNMP** pour afficher les niveaux de consommables (toners/cartouches par couleur, tambours/photoconducteurs, bouteilles de récupération de toner, agrafes…), le statut et les erreurs de chaque machine, ainsi que les imprimantes hors ligne (« DOWN »).

Il enregistre un **historique local** (SQLite), permet le **rapprochement de parc** entre une CMDB interne et l'inventaire d'un prestataire, et propose une **table virtualisée** fluide jusqu'à plusieurs milliers d'imprimantes.

## Fonctionnalités principales

| Domaine | Fonctionnalités |
|---------|-----------------|
| **CMDB** | Chargement XLSX/XLSM/XLTX/XLTM et CSV avec détection automatique des colonnes ; chemin mémorisé et rechargement automatique au démarrage. |
| **SNMP** | Lecture des niveaux de consommables via la MIB `Printer-MIB` standard, complétée par les MIB constructeurs (Lexmark, Brother, Epson). Seuils configurables et exceptions par modèle. |
| **Interface** | Lignes colorées selon l'état, alertes critiques, filtre par grand site, tri multiple, copie Ctrl+C, ouverture de l'interface Web par Ctrl+clic sur l'IP, aide intégrée. |
| **Table virtualisée** | Seules les lignes visibles sont instanciées (≈ 330 objets pour 100 lignes, 486 pour 10 000). Fluide sur de gros parcs. |
| **Historique** | Instantanés SQLite, timeline graphique, comparaison de deux mesures, purge automatique (14 jours par défaut). |
| **Rafraîchissement** | Manuel (tout / sélection), arrêt coopératif, auto-rafraîchissement programmable, export CSV (UTF-8 BOM, séparateur `;`). |
| **Rapprochement de parc** | Comparaison CMDB ↔ inventaire client Presta RMS, traduction de nomenclature, validation humaine avant écriture. |

---

## Démarrage rapide

### 1. Prérequis

* Python **3.8+**
* `pip`

### 2. Installation

```bash
# Cloner le dépôt
git clone https://github.com/VOTRE_COMPTE/PrinterMonitor.git
cd PrinterMonitor

# Installer les dépendances
python -m pip install -r requirements.txt
```

Dépendances (`requirements.txt`) :
* `pysnmp>=7.1.27,<8.0.0` — collecte SNMP
* `openpyxl>=3.1.0,<4.0.0` — lecture des fichiers CMDB Excel
* `Pillow>=10.0.0,<11.0.0` — gestion des images (splash, logo, icônes)

### 3. Lancement

```bash
python printer_monitor.py
```

L'application ouvre sa fenêtre principale directement.

### 4. Créer un exécutable Windows (PyInstaller)

Un fichier `PrinterMonitor.spec` est fourni pour générer un exécutable mono-fichier avec icône et dépendances embarquées :

```bash
python -m pip install pyinstaller
pyinstaller PrinterMonitor.spec
```

Le binaire est produit dans `dist/PrinterMonitor.exe`.

## Configuration

À la première exécution, l'application crée les fichiers de données à côté du script (ignorés par Git via `.gitignore`) :

* `printer_monitor_config.json` — configuration utilisateur (communauté SNMP, seuils, sites, auto-rafraîchissement…)
* `printer_monitor_history.db` — historique SQLite des mesures
* `printer_monitor_windows.json` — positions et dimensions des fenêtres

Un exemple de fichier d'imprimantes est fourni : `example_printers.csv`.

---

## Structure du projet

```
PrinterMonitor/
├── printer_monitor.py            # Point d'entrée (shim de compatibilité)
├── pyproject.toml                # Métadonnées du projet (setuptools)
├── requirements.txt              # Dépendances Python
├── PrinterMonitor.spec           # Spécification de build PyInstaller
├── PrinterMonitor.ico            # Icône de l'application Windows
├── printer_monitor_logo.png      # Logo affiché dans l'interface
├── example_printers.csv          # Exemple de fichier d'imprimantes
├── OIDS.md                       # Documentation technique des OID SNMP utilisés
├── LICENSE                       # Licence Printer Monitoring Community License v1.0
├── README.md                     # Ce fichier
└── printer_monitor/              # Package principal
    ├── __init__.py               # Export des symboles publics
    ├── _version.py               # Numéro de version (1.5)
    ├── main.py                   # Fonction main() et création de la fenêtre Tk
    ├── app.py                    # Interface principale (tableau, détails, réglages…)
    ├── vtable.py                 # Table virtualisée (en-tête, cellules, chips)
    ├── chips.py                  # Disposition des chips de statut
    ├── snmp.py                   # Collecte SNMP + fournisseurs constructeurs
    ├── models.py                 # PrinterDevice / PrinterSupply
    ├── config.py                 # Configuration JSON + valeurs par défaut
    ├── history.py                # Historique SQLite
    ├── auth.py                   # Mot de passe du mode avancé (scrypt)
    ├── help.py                   # Fenêtre d'aide intégrée
    ├── theme.py                  # Thème visuel (palette, styles ttk)
    ├── widgets.py                # Widgets personnalisés
    ├── splash.py                 # Écran de démarrage
    ├── ping.py                   # Vérification de reachabilité réseau
    ├── tls_certs.py              # Gestion des certificats TLS
    ├── utils.py                  # Fonctions utilitaires
    └── window_state.py           # Sauvegarde/restauration des états de fenêtres
    └── presta/                   # Rapprochement de parc CMDB ↔ Presta RMS
        ├── __init__.py
        ├── catalogue.py
        ├── cmdb_writer.py
        ├── conventions.py
        ├── diagnostic.py
        ├── dialog.py
        ├── importer.py
        ├── learning.py
        ├── mapping_store.py
        ├── model_map.py
        ├── normalize.py
        ├── reconcile.py
        ├── sync_history.py
        └── translation.py
```

---

## 📜 Licence

Printer Monitor est distribué sous la **Printer Monitoring Community License v1.0**.

Le code source est accessible et peut être utilisé, étudié et modifié pour un usage personnel, éducatif ou interne en entreprise.

**La vente, la redistribution commerciale, l'intégration dans un produit commercial et les services SaaS payants ne sont pas autorisés sans l'accord préalable du détenteur des droits.**

Voir le fichier [`LICENSE`](./LICENSE) pour consulter l'intégralité des conditions.

> Ce projet est **source-available** et **non Open Source** au sens officiel de l'Open Source Initiative (OSI).

---

## Contact

Pour toute question concernant le projet, les contributions ou une licence commerciale :

**contact@pdsp.fr**  
[www.pdsp.fr](https://www.pdsp.fr/)

Copyright © 2026 Paolo DOS SANTOS PINTO — Tous droits réservés.