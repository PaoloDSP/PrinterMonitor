"""Écran de démarrage (splash screen) du logiciel Printer Monitor."""

import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def show_splash(root):
    """Affiche un splash screen professionnel centré avec l'image 'Ban printermonitor.png'.

    L'image est redimensionnée pour occuper au maximum 50% de l'écran tout en
    conservant ses proportions originales. Le splash screen disparaît automatiquement
    lorsque l'application principale est prête.
    """
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.configure(background="white")

    # Liste des chemins possibles pour l'image de splash (Ban printermonitor.png)
    splash_image_name = "Ban printermonitor.png"
    splash_paths = []
    try:
        # Depuis le package printer_monitor/, remonter d'un niveau pour trouver
        # les ressources à la racine du projet.
        splash_paths.append(Path(__file__).resolve().parent.parent / splash_image_name)
    except Exception:
        pass
    try:
        splash_paths.append(Path(__file__).resolve().parent / splash_image_name)
    except Exception:
        pass
    try:
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            splash_paths.append(Path(meipass) / splash_image_name)
        splash_paths.append(Path(sys.executable).resolve().parent / splash_image_name)
    except Exception:
        pass

    image_loaded = False
    for splash_path in splash_paths:
        try:
            if splash_path and splash_path.exists():
                from PIL import Image, ImageTk
                pil_img = Image.open(str(splash_path))

                # Convertir en RGBA si nécessaire pour compatibilité Tkinter
                if pil_img.mode not in ("RGB", "RGBA"):
                    pil_img = pil_img.convert("RGBA")

                orig_w, orig_h = pil_img.size

                # Taille cible : au maximum 50% de l'écran, avec des bornes raisonnables
                max_w = max(400, int(root.winfo_screenwidth() * 0.5))
                max_h = max(300, int(root.winfo_screenheight() * 0.5))

                # Calcul du ratio de redimensionnement pour conserver les proportions
                ratio = min(max_w / orig_w, max_h / orig_h)
                if ratio < 1.0:
                    new_w = int(orig_w * ratio)
                    new_h = int(orig_h * ratio)
                    pil_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                splash.splash_image = ImageTk.PhotoImage(pil_img)
                # Label centré avec fond blanc pour un rendu propre
                ttk.Label(
                    splash,
                    image=splash.splash_image,
                    background="white",
                ).pack(padx=40, pady=40)
                image_loaded = True
                break
        except Exception:
            logging.debug("Impossible de charger le splash screen depuis %s", splash_path)

    if not image_loaded:
        # Fallback : utiliser printer_monitor_logo.png
        try:
            logo_path = Path(__file__).resolve().parent.parent / "printer_monitor_logo.png"
            if not logo_path.exists():
                logo_path = Path(__file__).resolve().parent / "printer_monitor_logo.png"
            if logo_path.exists():
                from PIL import Image, ImageTk
                pil_img = Image.open(str(logo_path))
                if pil_img.mode not in ("RGB", "RGBA"):
                    pil_img = pil_img.convert("RGBA")
                orig_w, orig_h = pil_img.size
                max_w = max(400, int(root.winfo_screenwidth() * 0.5))
                max_h = max(300, int(root.winfo_screenheight() * 0.5))
                ratio = min(max_w / orig_w, max_h / orig_h)
                if ratio < 1.0:
                    new_w = int(orig_w * ratio)
                    new_h = int(orig_h * ratio)
                    pil_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                splash.splash_image = ImageTk.PhotoImage(pil_img)
                ttk.Label(splash, image=splash.splash_image, background="white").pack(padx=40, pady=40)
                image_loaded = True
        except Exception:
            pass

    if not image_loaded:
        # Dernier recours : texte simple
        ttk.Label(
            splash,
            text="Printer Monitor",
            font=("Segoe UI", 16, "bold"),
            background="white",
        ).pack(padx=10, pady=10)

    # Centrer le splash screen sur l'écran
    splash.update_idletasks()
    width = splash.winfo_width()
    height = splash.winfo_height()
    x = (splash.winfo_screenwidth() - width) // 2
    y = (splash.winfo_screenheight() - height) // 2
    splash.geometry(f"{width}x{height}+{x}+{y}")

    # Toujours au premier plan
    try:
        splash.attributes("-topmost", True)
        splash.lift()
    except Exception:
        pass

    return splash