# OID interrogés par Printer Monitor — référence SNMP

Ce document récapitule **tous les OID** que l'application interroge (lecture
compréhensible du code : `printer_monitor/snmp.py`) ainsi que **les heuristiques
constructeur** qui choisissent, pour chaque machine, quelles tables lire.

## Types de requêtes utilisés

- **GET** : lecture d'une instance précise (identité, statut).
- **GET batch** : plusieurs GET regroupés (identité sysName + sysDescr).
- **GETNEXT / GETBULK (WALK)** : parcours d'une table entière avec
  `lookupMib=False` (les OID sont numériques en dur, aucune MIB n'est compilée).
- **Sondage rapide** (`_quick_probe`) : un simple GET sur `sysName` avant toute
  collecte, pour écarter immédiatement les machines silencieuses.

---

## 1. OID communs (toutes machines)

| OID | Nom MIB | Usage | Valeurs / notes |
|---|---|---|---|
| `1.3.6.1.2.1.1.1.0` | `sysDescr` (SNMPv2-MIB) | Modèle / description + détection constructeur | Texte libre ; nourrit `_detect_vendor()` |
| `1.3.6.1.2.1.1.5.0` | `sysName` (SNMPv2-MIB) | Hostname + **sondage de joignabilité** | GET seul |
| `1.3.6.1.2.1.43.5.1.1.1` | `prtGeneralStatus` (Printer-MIB RFC 3805) | Statut standard | 1=other, 2=unknown, 3=idle, 4=printing, 5=warmup |
| `1.3.6.1.2.1.25.3.2.1.5.1` | `hrDeviceStatus.1` (Host Resources MIB) | **Fallback statut** (constructeurs qui ignorent prtGeneralStatus) | 1=unknown, 2=running, 3=warning, 4=testing, 5=down |

### Table des alertes machine standard (`prtAlertTable`)

| OID | Nom | Usage |
|---|---|---|
| `1.3.6.1.2.1.43.18.1.8.1` | `prtAlertDescription` | Libellé de l'alerte |
| `1.3.6.1.2.1.43.18.1.2.1` | `prtAlertSeverity` | 1=other, 2=info, 3=warning, 4=critical → `[Critique]`/`[Alerte]`/`[Info]` |

Les messages banals ("Load Tray 1", "Close door", "Warming up"…) sont filtrés
(`BANAL_WAIT_PATTERNS`) sauf s'ils contiennent un mot-clé d'erreur fort
(`BANAL_STRONG_ERROR_PATTERNS`, ex. "jam", "error", "toner low").

---

## 2. Consommables — MIB standard (Printer-MIB, table `prtMarkerSupplies`)

Base `1.3.6.1.2.1.43.11.1.1`, instance par consommable (`…X.Y`). Les 4 colonnes
sont WALKées en parallèle (`_walk_columns_concurrent`).

| Colonne | Nom | Contenu |
|---|---|---|
| `1.3.6.1.2.1.43.11.1.1.6` | `prtMarkerSuppliesDescription` | Libellé du consommable |
| `1.3.6.1.2.1.43.11.1.1.4` | `prtMarkerSuppliesType` | Type (toner, drum, …) |
| `1.3.6.1.2.1.43.11.1.1.9` | `prtMarkerSuppliesLevel` | Niveau actuel |
| `1.3.6.1.2.1.43.11.1.1.8` | `prtMarkerSuppliesMaxCapacity` | **Capacité max — base du calcul des %** |

---

## 3. MIB entreprise Lexmark (`lexmarkConsumableTable`)

Base `1.3.6.1.4.1.641.6.4.4.1.1` (5 colonnes WALKées en parallèle ; la table
standard est lue d'abord, puis fusionnée avec celle-ci).

| Colonne | Nom | Contenu |
|---|---|---|
| `1.3.6.1.4.1.641.6.4.4.1.1.2` | `lexmarkConsumableName` | Nom du consommable |
| `1.3.6.1.4.1.641.6.4.4.1.1.3` | `lexmarkConsumableDescription` | Description |
| `1.3.6.1.4.1.641.6.4.4.1.1.4` | `lexmarkConsumableType` | Type — voir mapping ci-dessous |
| `1.3.6.1.4.1.641.6.4.4.1.1.6` | `lexmarkConsumableLevel` | Niveau |
| `1.3.6.1.4.1.641.6.4.4.1.1.7` | `lexmarkConsumableUnit` | Unité : 1=pages, 2=%, 3=items |
| `1.3.6.1.4.1.641.6.4.4.1.1.8` | `lexmarkConsumableCapacityUnits` | ⚠️ **Enum d'unités, PAS une capacité** : jamais utilisée comme max |

> Piège relevé dans le code : la colonne `.8` Lexmark porte l'unité (2=percent,
> 1=items), pas une vraie capacité. Les % Lexmark sont donc calculés avec la
> **MaxCapacity de la table standard** `1.3.6.1.2.1.43.11.1.1.8` quand elle existe.

`LEXMARK_CONSUMABLE_TYPES` : 1=toner, 2=ink, 3=photoconducteur,
4=bouteille de récupération, 5=kit maintenance, 6=agrafe, 7=four, 8=unité de
transfert, 9=développeur, 10=other.

### Alertes Lexmark (table entreprise `lexmarkAlertTable`)

Base `1.3.6.1.4.1.641.6.5.1.1`.

| Colonne | Nom | Contenu / note |
|---|---|---|
| `1.3.6.1.4.1.641.6.5.1.1.6` | description | Libellé (peut contenir le code ex. `[230.93]`) |
| `1.3.6.1.4.1.641.6.5.1.1.7` | info | Détails (`SrvCode=230.93`), aide à l'extraction du code |
| `1.3.6.1.4.1.641.6.5.1.1.9` | severity | 3=erreur machine → `[Critique]` ; 4=avertissement consommable → `[Alerte]` (observé production) |

---

## 4. MIB Brother (propriétaire, uniquement en repli)

| OID | Usage |
|---|---|
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0` | Chaîne hex encodée des niveaux toner : marqueur `81`=Noir, `82`=Cyan, `83`=Magenta, `84`=Jaune ; l'octet ~6 positions plus loin = pourcentage (normalisé 0-100). |

L'heuristique Brother : la table **standard** est d'abord essayée ; elle n'est
conservée telle quelle que si au moins un pourcentage valide est trouvé, sinon
repli sur cet OID propriétaire (les consommables non-toner standard sont
conservés).

---

## 5. MIB entreprise Epson (`EPSON-MIB` + fallbacks generiques)

Base historique `1.3.6.1.4.1.1248.1.1.2.1.3.1` (`epsPrinterSupplyTable`),
utilisee **en dernier** (compatibilite).

| Colonne | Nom | Contenu / note |
|---|---|---|
| `1.3.6.1.4.1.1248.1.1.2.1.3.1.1` | `epsPrinterSupplyDescription` | Description |
| `1.3.6.1.4.1.1248.1.1.2.1.3.1.2` | `epsPrinterSupplyLevel` | Niveau — ⚠️ **codes négatifs** `-1/-2/-3`=unknown/low → fallback, jamais « sans consommable » |
| `1.3.6.1.4.1.1248.1.1.2.1.3.1.3` | `epsPrinterSupplyMaxCapacity` | Capacité max |

### 5b. Fallbacks Epson (passe finale : FUSION multi-sources)

Principe : standard coherent conserve ; sinon TOUTES les branches
exploitables contribuent (vendor + remote st2/ii/ia + legacy + partiel),
fusionnees par `_merge_epson_supplies` : un niveau valide prime sur
UNKNOWN de meme couleur, un UNKNOWN complete une couleur absente
(affiche 'Niveau inconnu', chip grise), jamais de niveau invente.

1. **Printer-MIB standard** : `1.3.6.1.2.1.43.11.1.1` — colonnes
   WALKEES, index decouverts dynamiquement, conversion robuste
   Integer/Counter/Gauge/OctetString/bytes. Coherence : vide,
   `-1/-2/-3` ou descriptions absentes → **fallback, jamais
   « aucun consommable »**.
2. **Table proprietaire Epson** : `1.3.6.1.4.1.1248.1.2.2.2.1.1`
   (documentee par PrintMaster). `_epson_vendor_columns` + inference
   en 3 temps (`_infer_epson_vendor_roles`) : (1) mapping
   connu/documente, (2) structure (texte majoritaire >= 2, numeriques
   >= 2 avec couplage d'index >= 2 entre niveau+description ou
   niveau+max), (3) heuristique en dernier recours ; doute = None.
   Un entier 0..100 isole ne prouve RIEN. Jamais de `max_capacity`
   invente (None si absent).
3. **Epson Remote Mode** — protocole REEL `epson_print_conf`
   (`epctrl_snmp_oid` : `cmd + len16 LE + payload`, racine
   `1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1`), type SNMP `OctetString`
   (bytes). Isole dans le module Epson :

   | Cmd | OID exact | Payload | Reponse reelle | Parser | Normalise |
   |---|---|---|---|---|---|
   | `di` | `...2.1.100.105.1.0.1` | `b'\x01'` | texte `MFG:..;CMD:..;MDL:..;CLS:..;DES:..` apres 10 octets (`decode()[10:]`) | `_parse_epson_di` (MFG/CMD/MDL/CLS/DES) | identification, pas de consommable |
   | `st` | `...2.1.115.116.1.0.1` | `b'\x01'` | binaire `@BDC ST2` : `\x00@BDC ST2\r\n` + len16 LE (= len-13) + TLV `(ftype, longueur, item)` ; encre = ftype `0x0f` (`item[0]`=colourlen puis `(colour, ink_color, level)` par pas) ; maintenance box = `0x37` informative | `_parse_epson_st2` STRICT (en-tete/longueur/TLV invalides → None ; hors 0..100 → UNKNOWN) | niveaux 0x0f + maintenance UNKNOWN |
   | `ia` | `...2.1.105.97.1.0.0` | `0` (entier -> bytes([0]), longueur 1) | texte latin-1 contenant `IA:00;<liste,virgules>;` ; refus `\|\|:NA;` = pas de liste | `_parse_epson_ia` (exige `IA:00;`, sinon None) | liste SANS niveau → UNKNOWN, jamais devine |
   | `ii` slot 1..8 | `...2.1.105.105.2.0.1.<slot>` | `b'\x01<slot>'` | `@BDC PS\r\n<k:v;...>` (`IQT` hex = `ink_quantity`, METADONNEE jamais niveau ; `IC1` hex couleur, `II=='03'` documente) ; `ii:NA;` = slot vide = FIN de liste | `_parse_epson_ii` (`II!='03'` → UNKNOWN ; `IQT` absent/`NAVL` → UNKNOWN) | IQT en metadata, niveau UNKNOWN ; nom via `CARTRIDGE_TYPE` partiel sinon `unknown color` |
4. **Ancienne table EPSON-MIB** `...1.1.2.1.3.1.x` (compatibilite,
   coercition robuste des types SNMP).

Limites documentees : la table `...2.2.2.1.1` et le Remote Mode ne sont
pas universels — certaines Epson n'y repondent pas (voir `OIDS.md` § Epson
encore non supportees : modeles sans SNMP, SNMP desactive, MIB vide ET
Remote ferme → affichage « Niveau inconnu », jamais 0 %).

### 5c. Cas Epson encore non supportes (ne pas masquer)

- **SNMP desactive / communaute non `public`** : aucun fallback ne repond ;
  l'imprimante reste « injoignable », pas « sans consommable ».
- **Modeles grand format / SureColor avec comptabilite par tetes** : la
  table vendor peut exposer des compteurs de gouttes/pages au lieu de
  niveaux 0..100 → l'inference ne retient que les colonnes 0..100
  plausibles ; sinon affichage « Niveau inconnu ».
- **Maintenance Box / Waste Ink sur certains EcoTank** : exposes
  uniquement via EEPROM (`||`, mot de passe par modele, cf.
  Zedeldi/`epson-printer-snmp`) — lecture seule SNMP possible via
  `...44.1.1.2.1.124.124.7...` mais mot de passe specifique au modele :
  NON tente automatiquement (risque d'ecriture accidentelle exclu —
  le moteur ne fait que des GET/WALK).
- **Ancienne EPSON-MIB `...1.1.2.1.3.1.x`** : conservee mais certains
  firmwares recents ne l'exposent plus → la cascade la teste en dernier
  sans l'exiger.
- **Niveaux UNKNOWN affiches tels quels** : `ia` (liste sans niveau),
  maintenance box ST2 `0x37`, slots `ii` non documentes (`II!='03'`) ou
  `IQT` absent/`NAVL`, ST2 hors 0..100, vendor sans couplage structurel.
  Une fausse valeur est pire qu'un UNKNOWN : ces cas restent UNKNOWN,
  jamais 0 % ni 100 % inventes.
- **Preuve vs hypothetique** : `st` ST2 TLV, `di`, `ia`, `ii` prouves par
  fixtures de structure exacte `epson_print_conf` (tests 3/4/13/14) ;
  la table `...2.2.2.1.1` reste hypothetique sur WF-7620 (PrintMaster
  la documente, mais aucun dump reel WF-7620 ne confirme ses colonnes :
  utiliser `epson_dump_raw_rows()` sur la vraie machine).

---

## 6. Heuristiques constructeur

`_detect_vendor()` combine `sysDescr + marque CMDB + modèle CSV` (minuscules) :
une marque trouvée dans le texte → `brother|hp|canon|epson|xerox|ricoh|kyocera|
lexmark|samsung` ; sinon préfixes/régex Epson (`WF`, `XP`, `ET`, `EP`, `PM`,
`PX`, `stylus`, `workforce`, `expression`, `ecotank`, `surecolor`, `sc-`) →
`epson` ; sinon constructeur inconnu.

Puis `_select_provider()` :

| Constructeur | Provider | Comportement |
|---|---|---|
| **(par défaut)** | `StandardPrinterProvider` | Table standard seule. |
| **lexmark** | `LexmarkPrinterProvider` | Table standard **+** table entreprise Lexmark fusionnée ; la max-cap standard est réutilisée pour les % si la table entreprise n'en fournit pas. |
| **brother** | `BrotherPrinterProvider` | Standard en priorité (si % valides) → sinon OID hex propriétaire + conserve les non-toner standard. |
| **epson** | `EpsonPrinterProvider` | Standard WALK complet (8 colonnes, index dynamiques) ; si partiel (vide/-1/-2/-3) → table Epson `...2.2.2.1.1` (colonnes inferees) → Remote Mode `...44.1.1.2.1` (di/st/ia/ii) → ancienne EPSON-MIB ; jamais « sans consommable ». |
| **autre constructeur détecté** | `GenericPrinterProvider` | Standard uniquement. |

**Statut** : `prtGeneralStatus` d'abord, sinon fallback `hrDeviceStatus.1` pour
les vendeurs connus (lexmark, hp, epson, canon, xerox, ricoh, kyocera, samsung).

**Alertes machine** : `prtAlertTable` standard + table d'alertes entreprise
Lexmark exclusivement.

> Le README contient un tableau équivalent en section « Surveillance des
> imprimantes et récupération SNMP » ; ce fichier est la référence détaillée des OID.