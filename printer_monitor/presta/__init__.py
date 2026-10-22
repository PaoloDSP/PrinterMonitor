"""Module « Rapprochement de parc » (CMDB <-> inventaire client).

Ce paquet regroupe le rapprochement mensuel entre la CMDB interne
(feuille « CMDB ») et l'inventaire client (fichier Presta RMS, feuille
« Parc RMS selon contrat », table « Sites RMS »). La CMDB reste la
référence maître : toute valeur client est TRADUITE vers la nomenclature
interne avant comparaison, et rien n'est écrit sans validation humaine.

Sous-modules :
    importer      -- lecture du fichier client (équipements + « Sites RMS »)
    catalogue     -- dictionnaire de référence construit depuis la CMDB
    learning      -- apprentissage des correspondances (paires confirmées)
    mapping_store -- correspondances validées persistantes (JSON)
    conventions   -- CONVENTIONS DE NOTATION de la CMDB : états métier
                     (RETIRE, HORS RESEAU, COLIMO…), normaliseurs par champ,
                     canonisation des valeurs à écrire, alias métier et
                     journal des conventions appliquées
    normalize     -- primitives de normalisation (clés de comparaison)
    translation   -- moteur de traduction client -> objet CMDB (confiances)
    reconcile     -- identification + rapprochement + classement + anomalies
                     groupées + rapport de diagnostic
    diagnostic    -- exécution complète sur fichiers réels + rapport texte
    cmdb_writer   -- application transactionnelle des changements validés
    sync_history  -- historique / traçabilité des synchronisations
    dialog        -- interface « Rapprochement de parc » (+ « Voir la
                     traduction »)
"""