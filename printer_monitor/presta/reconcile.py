"""Moteur de rapprochement CMDB <-> inventaire client (v2).

Chaîne de traitement (cahier des charges §32) :

    SOURCE CLIENT
      -> TABLES DE RÉFÉRENCE CLIENT (« Sites RMS »)
      -> CORRESPONDANCES HISTORIQUES DE NOTRE CMDB (apprentissage)
      -> NORMALISATION
      -> OBJET CMDB (translation.translate_device)
      -> COMPARAISON
      -> VALIDATION (interface)
      -> MISE À JOUR (cmdb_writer)

Phases :
  1. IDENTIFICATION : tatouage, puis n° de série, puis adresse MAC ;
  2. APPRENTISSAGE  : les paires confirmées alimentent des dictionnaires
     (sites, bâtiments, étages, bureaux, modèles) ;
  3. TRADUCTION     : chaque ligne client devient un objet CMDB avec un
     niveau de confiance par champ ;
  4. COMPARAISON    : champ par champ, uniquement sur les champs traduits ;
  5. CLASSEMENT     : ajouts / modifications fiables / sans changement /
     retraits potentiels / anomalies GROUPÉES.

Ce module n'écrit JAMAIS dans la CMDB : il produit uniquement des
propositions que l'utilisateur validera.

La comparaison ne se fait plus sur des chaînes mais sur les CONVENTIONS de
chaque champ (``conventions.compare``) : une différence de nommage ou de
formatage n'est pas une différence, et un ÉTAT MÉTIER (« RETIRE », « HORS
RESEAU », « COLIMO »…) n'est jamais écrasé par une adresse ni proposé en
retrait — il devient une anomalie NOMMÉE (§3, §4, §29).
"""

from collections import Counter

from . import normalize
from . import conventions
from .catalogue import CmdbCatalogue
from .learning import LearnedMappings, classify_residual, client_after_site
from .mapping_store import MappingStore
from .translation import (
    LEVEL_EXACT,
    LEVEL_FUZZY,
    LEVEL_HIST,
    LEVEL_NORM,
    LEVEL_SITES,
    LEVEL_UNKNOWN,
    FieldValue,
    TranslatedDevice,
    normalize_ip,
    translate_device,
)

# En-têtes de la feuille CMDB (ordre réel du fichier).
CMDB_HEADERS = (
    "Grandsite", "Site", "Categorie", "AdresseIP", "Numeroetiquette",
    "Marque", "Modele", "Numserie", "AdresseMAC", "Bâtiment", "Etage",
    "Bureau", "CONCATENER", "Blocagemonochrome?", "Garantie(AUTO)",
    "Dated'installation", "SPECIFITE", "STATUT", "COMMENTAIRES",
    "Type", "Dated'installationmanuelle", "garantiemanuelle",
)

IDX_GRANDSITE = 0
IDX_SITE = 1
IDX_CATEGORIE = 2
IDX_IP = 3
IDX_TAG = 4
IDX_MARQUE = 5
IDX_MODELE = 6
IDX_NUMSERIE = 7
IDX_MAC = 8
IDX_BATIMENT = 9
IDX_ETAGE = 10
IDX_BUREAU = 11
IDX_CONCAT = 12


class CmdbRow:
    """Ligne brute de la feuille CMDB + clés de rapprochement."""

    def __init__(self, row_index, raw_row):
        self.row_index = row_index
        self.raw = list(raw_row) if raw_row is not None else []
        cells = [normalize.strip_spaces(v) for v in self.raw]

        def cell(idx):
            return cells[idx] if idx < len(cells) else ""

        self.grandsite = cell(IDX_GRANDSITE)
        self.site = cell(IDX_SITE)
        self.categorie = cell(IDX_CATEGORIE)
        self.ip = cell(IDX_IP)
        self.tag = cell(IDX_TAG)
        self.marque = cell(IDX_MARQUE)
        self.modele = cell(IDX_MODELE)
        self.numeroserie = cell(IDX_NUMSERIE)
        self.mac = cell(IDX_MAC)
        self.batiment = cell(IDX_BATIMENT)
        self.etage = cell(IDX_ETAGE)
        self.bureau = cell(IDX_BUREAU)
        self.concatener = cell(IDX_CONCAT)
        self.status = cell(17)
        self.type = cell(19)

        self.tag_key = normalize.norm_tag(self.tag)
        self.serial_key = normalize.norm_serial(self.numeroserie)
        self.mac_key = normalize.norm_mac(self.mac)
        self.applied = False

    def display(self):
        parts = [v for v in (self.tag, self.modele, self.ip) if v]
        return ", ".join(parts) if parts else f"(ligne {self.row_index})"


def _normalize_ip(value):
    """Rétro-compatibilité : voir translation.normalize_ip."""
    return normalize_ip(value)


def _last_segment(path):
    """Dernier segment d'un chemin client (« /26 - DROME/…/TRICASTIN/ »)."""
    parts = [p for p in normalize.split_location_path(path) if p.strip()]
    return parts[-1] if parts else ""


def _alias_observations(dev, row):
    """Correspondances (champ, valeur client, valeur CMDB) CONSTATÉES.

    Une paire identifiée par tatouage est une vérité terrain : quand le client
    écrit « LE BLAYAIS » là où la CMDB écrit « BLAYAIS », l'observation est
    enregistrée pour être appliquée ensuite aux lignes NON appariées (§24).
    Deux valeurs déjà identiques après normalisation ne sont pas enregistrées :
    elles n'apporteraient rien et gonfleraient la table (§8).
    """
    pairs = []
    candidates = [
        ("grandsite", _last_segment(dev.grandsite_presta), row.grandsite),
        ("site", _last_segment(dev.site_presta), row.site),
        ("modele", dev.modele, row.modele),
    ]
    batiment, etage, bureau = classify_residual(client_after_site(dev))
    candidates += [
        ("batiment", batiment, row.batiment),
        ("etage", etage, row.etage),
        ("bureau", bureau, row.bureau),
    ]
    for field, client_value, cmdb_value in candidates:
        if not client_value or not cmdb_value:
            continue
        if normalize.norm_compact(client_value) == \
                normalize.norm_compact(cmdb_value):
            continue
        pairs.append((field, client_value, cmdb_value))
    return pairs
# ---------------------------------------------------------------------- #
#  Objets résultat
# ---------------------------------------------------------------------- #
ADDITION_FIELD_MAP = {
    "Grandsite": "grandsite",
    "Site": "site",
    "Categorie": "categorie",
    "AdresseIP": "adresse_ip",
    "Numeroetiquette": "numeroetiquette",
    "Marque": "marque",
    "Modele": "modele",
    "Numserie": "numserie",
    "AdresseMAC": "adresse_mac",
    "Bâtiment": "batiment",
    "Etage": "etage",
    "Bureau": "bureau",
    "CONCATENER": "concatener",
    "Type": "type",
}


class FieldChange:
    """Proposition de modification d'un champ unique.

    ``field`` est le nom EXACT de la colonne CMDB ; ``level`` est le niveau
    de confiance de la valeur traduite proposée.
    """

    __slots__ = ("field", "cmdb_value", "new_value", "level", "applied")

    def __init__(self, field, cmdb_value, new_value, level=LEVEL_NORM):
        self.field = field
        self.cmdb_value = cmdb_value
        self.new_value = new_value
        self.level = level
        self.applied = False

    @property
    def reliable(self):
        return self.level <= LEVEL_NORM

    def summarize(self):
        old = self.cmdb_value or "(vide)"
        new = self.new_value or "(vide)"
        return f"{old} → {new}"


class Modification:
    """Équipement présent dans les deux référentiels avec des différences."""

    def __init__(self, cmdb_row, client_device, translated):
        self.cmdb_row = cmdb_row
        self.client_device = client_device
        self.translated = translated
        self.field_changes = []
        self.applied = False

    @property
    def reliable(self):
        return bool(self.field_changes) and all(
            c.reliable for c in self.field_changes
        )

    @property
    def tag(self):
        return self.cmdb_row.tag

    def describe(self):
        return ", ".join(
            f"{c.field}: {c.summarize()}" for c in self.field_changes
        )


class Addition:
    """Équipement client absent de la CMDB (valeurs déjà traduites)."""

    def __init__(self, client_device, translated):
        self.client_device = client_device
        self.translated = translated
        self.applied = False

    @property
    def reliable(self):
        for name in ("grandsite", "site"):
            fv = self.translated.field(name)
            if fv is not None and fv.is_unknown:
                return False
        return True

    @property
    def tag(self):
        return self.client_device.tatouage

    def values_for_cmdb(self):
        """Valeurs traduites prêtes à écrire (nomenclature interne).

        Chaque valeur passe par la CANONISATION de son champ (§2) : une adresse
        IP ajoutée respecte la convention d'écriture de la CMDB (« https://IP »)
        exactement comme une adresse modifiée — l'ancien code n'habillait que
        les mises à jour, si bien qu'un ajout écrivait une IP nue.
        """
        out = {}
        for col, attr in ADDITION_FIELD_MAP.items():
            fv = self.translated.field(attr)
            if fv is not None and not fv.is_empty and not fv.is_unknown:
                out[col] = conventions.canonical_for_field(col, fv.value)
        return out


class Anomaly:
    """Anomalie GROUPÉE : plusieurs équipements, une seule décision.

    ``key`` identifie le groupe (champ + valeur client + candidats) ;
    ``items`` contient les (device, row|None) concernés.
    """

    def __init__(self, kind, field, label, candidates=None):
        self.kind = kind
        self.field = field
        self.label = label
        self.candidates = list(candidates or [])
        self.items = []
        self.resolved_value = None

    @property
    def count(self):
        return len(self.items)

    def message(self):
        base = {
            "site_inconnu": "Correspondance de site inconnue",
            "correspondance_ambigue": "Correspondance ambiguë",
            "modele_inconnu": "Modèle inconnu",
            "localisation_inconnue": "Correspondance de localisation inconnue",
            "donnees_incompletes": "Données incomplètes",
            "doublon_cmdb": "Doublon dans la CMDB",
            "doublon_client": "Doublon dans le fichier client",
            conventions.SPECIAL_STATE:
                "État métier de la CMDB (non comparable)",
            conventions.REAPPEARANCE:
                "Matériel déjà retiré dans la CMDB, signalé présent",
        }.get(self.kind, self.kind)
        return f"{base} : {self.label}"
class ReconcileResult:
    """Résultat complet du rapprochement (aucune écriture)."""

    def __init__(self):
        self.additions = []
        self.modifications = []
        self.removals = []
        self.anomalies = []          # list[Anomaly] groupées
        self.unchanged = []
        self.translations = {}       # id(device) -> TranslatedDevice
        self.diagnostics = {}
        #: Conventions appliquées pendant l'analyse (§23) :
        #: (champ, source, valeur CMDB, nature, niveau) -> nombre d'équipements.
        self.conventions = Counter()
        #: Lignes CMDB écartées des « retraits potentiels » parce que leur
        #: adresse est un état métier : liste de (CmdbRow, état).
        self.skipped_removals = []

    @property
    def counts(self):
        d = self.diagnostics
        return {
            "additions": len(self.additions),
            "modifications": len(self.modifications),
            "removals": len(self.removals),
            "anomalies": sum(a.count for a in self.anomalies),
            "unchanged": len(self.unchanged),
            **{k: v for k, v in d.items() if isinstance(v, int)},
        }

    def reliable_modifications(self):
        return [m for m in self.modifications if m.reliable]

    def reliable_additions(self):
        return [a for a in self.additions if a.reliable]


# Comparaisons champ par champ.
# (colonne CMDB, attribut traduit, attribut CmdbRow, mode de comparaison)
# Chaque mode reste disponible pour normalize.values_equal, mais la
# comparaison réelle passe par conventions.compare : c'est la CONVENTION du
# champ (colonne CMDB) qui décide, y compris pour les états métier (§29).
COMPARISONS = (
    # Grandsite / Site : normalisation « nommage » (déterminants ignorés,
    # ex. « LE BLAYAIS » == « BLAYAIS »).
    ("Grandsite", "grandsite", "grandsite", "name"),
    ("Site", "site", "site", "name"),
    # Localisation : normalisation « formatage » (« 19 M » == « 19M »).
    ("Bâtiment", "batiment", "batiment", "text"),
    ("Etage", "etage", "etage", "text"),
    ("Bureau", "bureau", "bureau", "text"),
    ("CONCATENER", "concatener", "concatener", "compact"),
    ("Categorie", "categorie", "categorie", "norm"),
    ("Type", "type", "type", "norm"),
    ("Marque", "marque", "marque", "norm"),
    ("Modele", "modele", "modele", "norm"),
    ("Numserie", "numserie", "numeroserie", "serial"),
    ("AdresseIP", "adresse_ip", "ip", "ip"),
    ("AdresseMAC", "adresse_mac", "mac", "mac"),
)


def _state_anomaly(verdict):
    """Libellé + explication d'une anomalie d'ÉTAT MÉTIER (§4, §23).

    Le regroupement s'appuie sur le LIBELLÉ HUMAIN de l'état, jamais sur la
    valeur brute : « HORS RESEAU » resterait sinon éclaté en autant de groupes
    que d'adresses proposées par le fichier client, ce qui noierait le
    signalement au lieu de le rendre lisible.
    """
    label = conventions.state_label(
        verdict.cmdb_state or verdict.client_state
    ) or "(état inconnu)"
    detail = []
    if verdict.cmdb_state and verdict.client_state:
        detail.append("CMDB : %s / client : %s" % (
            conventions.state_label(verdict.cmdb_state),
            conventions.state_label(verdict.client_state),
        ))
    return label, detail


# La normalisation de comparaison est centralisée dans
# normalize.values_equal (modes « norm », « name », « text », « compact »,
# « serial », « ip », « mac ») : voir COMPARISONS ci-dessus.


class ReconcileEngine:
    """Moteur complet : identification -> apprentissage -> traduction ->
    comparaison -> classement."""

    def __init__(self, cmdb_rows, devices, sites_rms=None, mappings=None):
        self.cmdb_rows = list(cmdb_rows)
        self.devices = list(devices)
        self.sites_rms = sites_rms
        self.mappings = mappings
        self.catalogue = CmdbCatalogue(self.cmdb_rows)
        self.learned = LearnedMappings()
        # Remplis par run()
        self.pairs = []
        self.result = ReconcileResult()
        #: Correspondances métier actives (§8) et lignes écartées des retraits.
        self.aliases = conventions.active_aliases()
        self.skipped_removals = []

    # ------------------------------------------------------------------ #
    # Phase 1 : identification
    # ------------------------------------------------------------------ #
    def _identify(self):
        """Apparie les équipements client aux lignes CMDB."""
        by_tag = {}
        by_serial = {}
        by_mac = {}
        for row in self.cmdb_rows:
            if row.tag_key:
                by_tag.setdefault(row.tag_key, []).append(row)
            if row.serial_key:
                by_serial.setdefault(row.serial_key, []).append(row)
            if row.mac_key:
                by_mac.setdefault(row.mac_key, []).append(row)

        pairs = []
        matched_rows = set()
        anomalies = []
        unmatched = []
        for dev in self.devices:
            pair = None
            # 1. Tatouage
            if dev.tag_key and len(by_tag.get(dev.tag_key, ())) == 1:
                pair = by_tag[dev.tag_key][0]
                how = "tatouage"
            elif dev.tag_key and len(by_tag.get(dev.tag_key, ())) > 1:
                anomalies.append(("doublon_cmdb", None,
                                  f"tatouage {dev.tatouage}",
                                  [r.display() for r in by_tag[dev.tag_key]]))
                continue
            # 2. Numéro de série
            if pair is None and dev.serial_key:
                cands = [
                    r for r in by_serial.get(dev.serial_key, ())
                    if r.row_index not in matched_rows
                ]
                if len(cands) == 1:
                    pair, how = cands[0], "numero de serie"
                elif len(cands) > 1:
                    anomalies.append((
                        "correspondance_ambigue", None,
                        f"n° série {dev.num_serie}",
                        [r.display() for r in cands],
                    ))
                    continue
            # 3. Adresse MAC (+ cohérence de modèle)
            if pair is None and dev.mac_key:
                cands = [
                    r for r in by_mac.get(dev.mac_key, ())
                    if r.row_index not in matched_rows
                ]
                if len(cands) == 1:
                    pair, how = cands[0], "adresse MAC"
                elif len(cands) > 1:
                    anomalies.append((
                        "correspondance_ambigue", None,
                        f"MAC {dev.mac}",
                        [r.display() for r in cands],
                    ))
                    continue
            if pair is None:
                unmatched.append(dev)
                continue
            if pair.row_index in matched_rows:
                anomalies.append((
                    "doublon_client", None,
                    f"ligne {dev.row_index} -> {pair.tag}", [],
                ))
                continue
            matched_rows.add(pair.row_index)
            pairs.append((dev, pair, how))

        for kind, field, label, candidates in anomalies:
            self._add_anomaly(kind, field or "", label, candidates, None, None)
        return pairs, unmatched
    # ------------------------------------------------------------------ #
    # Phases 2-3 : apprentissage puis traduction
    # ------------------------------------------------------------------ #
    def _learn_and_translate(self, pairs):
        for dev, row, _how in pairs:
            self.learned.observe(dev, row)
        # Les correspondances constatées / validées sont installées AVANT la
        # traduction : la comparaison qui suit doit déjà en bénéficier (§8).
        self._build_aliases(pairs)
        for dev in self.devices:
            translated = translate_device(
                dev, self.catalogue, self.mappings, self.learned,
                sites_rms=self.sites_rms,
            )
            self.result.translations[id(dev)] = translated

    # ------------------------------------------------------------------ #
    # Correspondances métier (§7, §8, §17)
    # ------------------------------------------------------------------ #
    def _build_aliases(self, pairs):
        """Installe les correspondances métier utilisées par la comparaison.

        Deux sources, classées par fiabilité (§24) :

          * les validations de l'utilisateur (``mapping_store``) : choix
            explicite, appliqué immédiatement ;
          * les correspondances CONSTATÉES sur les équipements appariés par
            tatouage : elles ne sont appliquées qu'après plusieurs
            observations (``HISTORY_MIN_OBSERVATIONS``), une seule occurrence
            pouvant venir d'une saisie erronée et confondre deux sites.

        La table est reconstruite à chaque analyse : rien n'est hérité d'un
        import précédent, afin qu'une correction de la CMDB soit toujours
        visible (§16 : la CMDB reste la référence).
        """
        aliases = conventions.AliasDict()
        if self.mappings is not None:
            for field in conventions.ALIAS_FIELDS:
                for source, target in \
                        self.mappings.validated.get(field, {}).items():
                    if isinstance(target, str):
                        aliases.add(field, source, target,
                                    provenance=conventions.PROVENANCE_USER)
        for dev, row, _how in pairs:
            for field, source, target in _alias_observations(dev, row):
                aliases.add(field, source, target,
                            provenance=conventions.PROVENANCE_HISTORY)
        conventions.set_aliases(aliases)
        self.aliases = aliases
        return aliases

    # ------------------------------------------------------------------ #
    # Phase 4-5 : comparaison et classement
    # ------------------------------------------------------------------ #
    def _compare_pair(self, dev, row, translated):
        changes = []
        for column, t_attr, r_attr, _mode in COMPARISONS:
            fv = translated.field(t_attr)
            if fv is None or fv.is_empty:
                # Absence de donnée client : on n'efface JAMAIS la CMDB.
                continue
            if fv.is_unknown and str(fv.client_raw or "").strip():
                if t_attr == "modele":
                    kind = "modele_inconnu"
                elif t_attr in ("grandsite", "site"):
                    kind = "site_inconnu"
                else:
                    kind = "localisation_inconnue"
                self._add_anomaly(
                    kind, column, fv.client_raw or "(vide)",
                    list(fv.candidates), dev, row,
                )
                continue
            if not str(fv.value or "").strip():
                # Traduction vide (ex. CONCATENER sans localisation) :
                # rien à proposer, jamais de nettoyage automatique.
                continue
            old_val = getattr(row, r_attr)
            # COMPARAISON PAR CONVENTION (§29) : le moteur n'oppose plus deux
            # chaînes, il interroge la convention du champ. Trois issues :
            #   * équivalent  -> différence de nommage/formatage seulement,
            #                    donc AUCUNE modification proposée ;
            #   * état métier -> anomalie NOMMÉE (jamais un écrasement) ;
            #   * différent   -> vraie modification, valeur canonisée.
            # Les valeurs originales (CMDB et client) restent intactes pour
            # l'affichage, la traçabilité et les éventuelles mises à jour.
            verdict = conventions.compare(column, fv.value, old_val)
            if verdict.status == conventions.EQUIVALENT:
                continue
            if verdict.status in (conventions.SPECIAL_STATE,
                                  conventions.REAPPEARANCE):
                label, detail = _state_anomaly(verdict)
                self._add_anomaly(verdict.kind, column, label, detail, dev, row)
                continue
            # Une vraie modification d'IP conserve le format interne de la
            # CMDB (« https://IP »), sans modifier les données du fichier
            # client (la normalisation ne sert qu'à la comparaison).
            new_val = conventions.canonical_for_field(column, fv.value, old_val)
            changes.append(FieldChange(column, old_val, new_val, fv.level))
        if not changes:
            self.result.unchanged.append(row)
            return
        modif = Modification(row, dev, translated)
        modif.field_changes = changes
        self.result.modifications.append(modif)

    def _classify_unmatched(self, unmatched):
        for dev in unmatched:
            if not dev.tag_key and not dev.serial_key:
                self._add_anomaly(
                    "donnees_incompletes", "",
                    f"ligne {dev.row_index} sans tatouage ni n° série",
                    [], dev, None,
                )
            translated = self.result.translations.get(id(dev))
            if translated is None:
                continue
            addition = Addition(dev, translated)
            if not addition.reliable:
                fv = (translated.field("grandsite")
                      or translated.field("site"))
                self._add_anomaly(
                    "site_inconnu", "Grandsite",
                    fv.client_raw or "(vide)", list(fv.candidates),
                    dev, None,
                )
            self.result.additions.append(addition)

    def _compute_removals(self, matched_rows):
        """Retraits POTENTIELS : lignes CMDB absentes du fichier client (§3).

        Une ligne dont l'adresse est un ÉTAT MÉTIER n'est JAMAIS proposée en
        retrait, pour deux raisons distinctes :

          * « RETIRE » / « MATERIEL RETIRE » : le matériel est DÉJÀ retiré ;
            proposer sa suppression viderait une ligne déjà à jour ;
          * « HORS RESEAU », « COLIMO », « HORS PARC RMS » : le matériel EXISTE
            (il est seulement sans adresse exploitable) ; vider la ligne
            détruirait une machine réelle.

        Ces lignes sont comptées séparément (``retraits_ignores_etat_special``)
        et regroupées dans le rapport : elles restent VISIBLES, jamais
        silencieuses.
        """
        for row in self.cmdb_rows:
            if row.row_index in matched_rows or not row.tag_key:
                continue
            state = conventions.special_state(row.ip)
            if state:
                self.skipped_removals.append((row, state))
                continue
            self.result.removals.append(row)

    # ------------------------------------------------------------------ #
    # Anomalies groupées
    # ------------------------------------------------------------------ #
    def _add_anomaly(self, kind, field, label, candidates, device, row):
        norm_label = normalize.norm_key(label or "")
        cand_sig = tuple(sorted(
            c for c in set(candidates or ()) if str(c).strip()
        ))
        group_key = (kind, field, norm_label, cand_sig)
        cache = getattr(self, "_anomaly_index", None)
        if cache is None:
            cache = self._anomaly_index = {}
        anomaly = cache.get(group_key)
        if anomaly is None:
            anomaly = Anomaly(kind, field, label, candidates)
            cache[group_key] = anomaly
            self.result.anomalies.append(anomaly)
        anomaly.items.append((device, row))
    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def run(self):
        result = self.result
        pairs, unmatched = self._identify()
        self.pairs = pairs

        # Toute la chaîne traduction -> comparaison -> classement est
        # journalisée : les conventions qui ont évité une fausse modification
        # sont comptées et regroupées (§23), au lieu de disparaître
        # silencieusement dans la normalisation.
        with conventions.collect_conventions() as journal:
            self._learn_and_translate(pairs)

            matched_rows = set()
            how_counts = {"tatouage": 0, "numero de serie": 0, "adresse MAC": 0}
            for dev, row, how in pairs:
                matched_rows.add(row.row_index)
                how_counts[how] = how_counts.get(how, 0) + 1
                translated = result.translations.get(id(dev))
                if translated is not None:
                    self._compare_pair(dev, row, translated)

            self._classify_unmatched(unmatched)
            self._compute_removals(matched_rows)
        result.conventions = journal
        self._finalize_diagnostics(how_counts)
        return result

    # ------------------------------------------------------------------ #
    # Rapport de diagnostic interne (§30)
    # ------------------------------------------------------------------ #
    def _finalize_diagnostics(self, how_counts):
        d = self.result.diagnostics
        d["equipements_analyses"] = len(self.devices)
        d["lignes_cmdb"] = len(self.cmdb_rows)
        d["correspondances_exactes"] = how_counts.get("tatouage", 0)
        d["correspondances_historiques"] = (
            how_counts.get("numero de serie", 0)
            + how_counts.get("adresse MAC", 0)
        )
        d["correspondances_ambigues"] = sum(
            a.count for a in self.result.anomalies
            if a.kind == "correspondance_ambigue"
        )

        unknown = {"modele": 0, "site": 0, "batiment": 0, "etage": 0,
                   "bureau": 0}
        levels = {}
        for translated in self.result.translations.values():
            fv = translated.field("modele")
            if fv is not None and fv.is_unknown and not fv.is_empty:
                unknown["modele"] += 1
            gs = translated.field("grandsite")
            st = translated.field("site")
            if ((gs is not None and gs.is_unknown) or
                    (st is not None and st.is_unknown)):
                unknown["site"] += 1
            for name in ("batiment", "etage", "bureau"):
                fv = translated.field(name)
                if fv is not None and fv.is_unknown and not fv.is_empty:
                    unknown[name] += 1
            for name, fv in translated.fields.items():
                levels[fv.label] = levels.get(fv.label, 0) + 1

        d["modeles_inconnus"] = unknown["modele"]
        d["sites_inconnus"] = unknown["site"]
        d["batiments_inconnus"] = unknown["batiment"]
        d["etages_inconnus"] = unknown["etage"]
        d["bureaux_inconnus"] = unknown["bureau"]
        d["niveaux_confiance"] = levels
        d["vraies_modifications"] = len(self.result.modifications)
        d["sans_changement"] = len(self.result.unchanged)
        d["ajouts"] = len(self.result.additions)
        d["retraits_potentiels"] = len(self.result.removals)

        # --- États métier et conventions (§3, §4, §23) ----------------- #
        # Ces compteurs rendent VISIBLE ce que la normalisation a absorbé :
        # le nombre de fausses anomalies évitées et le nombre de lignes
        # volontairement écartées des retraits.
        states = {}
        for _row, state in self.skipped_removals:
            states[state] = states.get(state, 0) + 1
        d["etats_speciaux_cmdb"] = states
        d["retraits_ignores_etat_special"] = len(self.skipped_removals)
        d["anomalies_etat_special"] = sum(
            a.count for a in self.result.anomalies
            if a.kind == conventions.SPECIAL_STATE
        )
        d["anomalies_reapparition"] = sum(
            a.count for a in self.result.anomalies
            if a.kind == conventions.REAPPEARANCE
        )
        d["conventions_appliquees"] = sum(self.result.conventions.values())


def reconcile(cmdb_rows, devices, site_mapping=None, sites_rms=None,
              mappings=None):
    """Point d'entrée : lance le moteur complet et renvoie le résultat.

    ``site_mapping`` est conservé pour compatibilité des appels existants.
    """
    engine = ReconcileEngine(cmdb_rows, devices, sites_rms=sites_rms,
                             mappings=mappings)
    result = engine.run()
    result.engine = engine
    return result


__all__ = (
    "CmdbRow", "FieldChange", "Modification", "Addition", "Anomaly",
    "ReconcileResult", "ReconcileEngine", "reconcile", "CMDB_HEADERS",
)