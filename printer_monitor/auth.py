"""Authentification sécurisée du mode avancé (développeur / debug).

Ce module centralise la vérification du mot de passe administrateur du mode
avancé. Contrairement à l'ancien système (hash SHA-256 à sel fixe stocké dans
le fichier JSON de configuration), le mot de passe administrateur est ici
authentifié par un dérivé de clé **scrypt** avec un **sel aléatoire** codé en
dur dans ce module.

Ce choix garantit :
  * aucun mot de passe en clair ni dans le code ni dans le JSON de config ;
  * aucune valeur dans ``printer_monitor_config.json`` permettant à un
    utilisateur de substituer son propre mot de passe ;
  * une dérivation de clé lente (coût mémoire + CPU) conforme aux bonnes
    pratiques de stockage de mots de passe (scrypt, N=2**14, r=8, p=1).
"""

import base64
import hashlib
import hmac

# ---------------------------------------------------------------------------
# Identifiants administrateur — stockés comme un dérivé de clé scrypt salé.
#
# ATTENTION : ne PAS modifier ces valeurs depuis le fichier JSON de
# configuration ou depuis l'interface. Le mot de passe administrateur est une
# information codée en dur, hors de portée des utilisateurs du logiciel.
#
# Pour remplacer le mot de passe administrateur, ce module doit être mis à
# jour avec un nouveau sel aléatoire et le nouveau dérivé scrypt correspondant
# (générés hors-ligne par le développeur du logiciel).
# ---------------------------------------------------------------------------

# Paramètres scrypt conformes aux recommandations OWASP (période 2023+).
_SCRYPT_N = 2 ** 14          # coût CPU/mémoire
_SCRYPT_R = 8                # bloc size
_SCRYPT_P = 1                # parallélisme
_SCRYPT_DKLEN = 32           # longueur de la clé dérivée (octets)

# Sel aléatoire unique à cette installation du logiciel — généré hors-ligne,
# jamais stocké dans le fichier JSON utilisateur.
_ADMIN_SALT_B64 = "5w+vgTMKLK9zeKAmi3bKLw=="

# Dérivé de clé scrypt du mot de passe administrateur — jamais le mot de passe
# lui-même, jamais un simple hachage réversible par dictionnaire.
_ADMIN_KEY_B64 = "nbRtu9iIGDn+Q4faWpOObUs1jLqjjMhDfCwA22RJzi4="

# Longueur minimale du mot de passe administrateur.
MIN_ADMIN_PASSWORD_LENGTH = 12

_ADMIN_SALT = base64.b64decode(_ADMIN_SALT_B64)
_ADMIN_KEY = base64.b64decode(_ADMIN_KEY_B64)


def _derive_scrypt(password):
    """Dérive la clé scrypt d'un mot de passe avec le sel administrateur.

    Retourne les octets bruts de la clé dérivée (longueur ``_SCRYPT_DKLEN``).
    """
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=_ADMIN_SALT,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def verify_advanced_password(password):
    """Vérifie un mot de passe saisi contre le mot de passe administrateur.

    La comparaison est effectuée en temps constant (``hmac.compare_digest``)
    pour éviter les attaques par canal auxiliaire sur la longueur/champ de bits.
    """
    if not isinstance(password, str) or not password:
        return False
    try:
        candidate = _derive_scrypt(password)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _ADMIN_KEY)