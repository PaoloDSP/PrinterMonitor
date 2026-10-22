"""Point d'entrée du logiciel Printer Monitor."""

import sys
import tkinter as tk
from pathlib import Path

from .app import PrinterMonitorApp

# Fichier d'icône Windows embarqué par le .spec (section ``datas``) et utilisé
# pour la fenêtre principale / la barre des tâches (sinon icône Tk générique).
APP_ICON = "PrinterMonitor.ico"


def _icon_candidates():
    """Retourne les emplacements possibles de l'icône de fenêtre.

    Même mécanisme que les autres ressources embarquées (splash.py, help.py) :
    - racine des ressources extraites par PyInstaller (``sys._MEIPASS``),
      où le .spec place l'icône déclarée dans ``datas`` ;
    - répertoire de l'exécutable (mode portable : icône éventuellement posée
      à côté du .exe, sinon sans effet) ;
    - racine du projet relativement à ce module (mode développement).
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / APP_ICON)
    try:
        candidates.append(Path(sys.executable).resolve().parent / APP_ICON)
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parent.parent / APP_ICON)
    return candidates


def _apply_window_icon(root):
    """Applique l'icône Windows de l'application à la fenêtre principale.

    L'échec est silencieux : l'icône de l'exécutable (ressource Windows
    définie dans le .spec) reste alors utilisée par l'Explorateur.
    """
    for candidate in _icon_candidates():
        try:
            if candidate.is_file():
                root.iconbitmap(str(candidate))
                return True
        except Exception:
            continue
    return False


def main():
    root = tk.Tk()
    # Icône de fenêtre / barre des tâches : PrinterMonitor.ico (et non
    # l'icône générique Tk). Doit être fait avant de construire l'interface.
    _apply_window_icon(root)
    # Fenêtre masquée le temps de construire l'interface, pour éviter tout
    # flash de disposition au démarrage. Aucun écran de chargement séparé :
    # l'application démarre directement sur sa fenêtre principale, dont
    # l'identité (logo + nom + version) est intégrée au header.
    root.withdraw()
    app = PrinterMonitorApp(root)
    # Dimensions de démarrage : une taille initiale confortable qui s'adapte
    # à la résolution de l'écran. La fenêtre reste redimensionnable pour
    # s'adapter aux petits écrans (mobile/tablette) comme aux grands (4K).
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    start_w = min(max(1024, int(screen_w * 0.8)), 1400)
    start_h = min(max(640, int(screen_h * 0.7)), 900)
    root.geometry(f"{start_w}x{start_h}")
    # Taille minimale pour garantir que tous les éléments restent visibles
    # et utilisables, même sur des écrans très petits.
    root.minsize(760, 480)
    # On autorise le redimensionnement naturel de la fenêtre : le layout
    # (Treeview, panneau Détails, barre d'outils) s'adapte grâce aux
    # gestionnaires de paquets et aux largeurs fluides des colonnes.
    root.pack_propagate(True)

    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()