"""Utilitaire de ping ICMP cross-platform."""

import logging
import os
import subprocess


def _ping_host(ip, timeout=2):
    """Effectue un ping ICMP vers l'adresse IP et renvoie True si l'hôte répond.

    Utilise la commande ``ping`` du système (cross-platform) : ``ping -n 1 -w``
    sous Windows, ``ping -c 1 -W`` ailleurs. Renvoie False sur toute erreur
    (hôte injoignable, commande absente, délai dépassé, etc.).
    """
    try:
        if os.name == "nt":
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(timeout)), ip]
        kwargs = {}
        if os.name == "nt":
            # Empêche l'ouverture d'une fenêtre CMD lors de l'exécution du ping.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
            **kwargs,
        )
        return proc.returncode == 0
    except Exception as exc:
        logging.debug("Ping ICMP failed for %s: %s", ip, exc)
        return False