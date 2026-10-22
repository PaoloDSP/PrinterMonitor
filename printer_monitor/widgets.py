"""Widgets réutilisables de l'interface graphique."""

import re
import tkinter as tk
from tkinter import StringVar, ttk

# Couleurs des « chips » (tags de modèles) — palette moderne du thème
CHIP_BG = "#dbeafe"
CHIP_FG = "#1e40af"
CHIP_REMOVE_FG = "#b91c1c"
CHIP_REMOVE_HOVER = "#7f1d1d"
CHIP_GAP = 4


class ModelTagsFrame(ttk.Frame):
    """Widget for entering and displaying printer model strings as removable
    tags/chips.

    Typing a model and pressing Enter (or clicking '+') adds it as a tag.
    Clicking the '×' on an individual tag removes that model only. Multiple
    models may be entered at once by separating them with commas, semicolons
    or spaces.

    Les chips sont disposées en flux : elles passent à la ligne suivante
    lorsque la largeur disponible est insuffisante, au lieu d'être écrasées
    ou tronquées horizontalement.
    """

    def __init__(self, parent, initial_models=None):
        super().__init__(parent)
        self._models = []
        self._chips = []
        self._bg = self._parent_bg()
        self._tags_inner = tk.Frame(self, bg=self._bg)
        self._tags_inner.pack(side="top", fill="x", pady=(2, 2))
        entry_row = tk.Frame(self, bg=self._bg)
        entry_row.pack(side="top", fill="x")
        self._entry_var = StringVar()
        self._entry = ttk.Entry(entry_row, textvariable=self._entry_var, width=20)
        self._entry.pack(side="left", padx=(0, 4))
        self._entry.bind("<Return>", self._add_from_entry)
        ttk.Button(entry_row, text="+", width=3, command=self._add_from_entry).pack(
            side="left", padx=(0, 0)
        )
        # Re-disposition des chips à chaque changement de largeur du conteneur
        self._tags_inner.bind("<Configure>", self._relayout_chips)
        for m in (initial_models or []):
            self.add_tag(str(m))
        self._rebuild()

    def _parent_bg(self):
        """Couleur de fond du parent (carte blanche ou fond du dialogue),
        pour que le widget s'intègre visuellement à son conteneur."""
        try:
            return self.master.cget("background")
        except Exception:
            try:
                return self.winfo_toplevel().cget("background")
            except Exception:
                return "white"

    def add_tag(self, model):
        model = str(model).strip()
        if not model:
            return
        if any(m.lower() == model.lower() for m in self._models):
            return
        self._models.append(model)
        self._rebuild()

    def remove_tag(self, model):
        self._models = [m for m in self._models if m.lower() != str(model).lower()]
        self._rebuild()

    def _rebuild(self):
        for child in self._tags_inner.winfo_children():
            child.destroy()
        self._chips = []
        if not self._models:
            tk.Label(
                self._tags_inner,
                text="(aucun modèle)",
                bg=self._bg,
                fg="#94a3b8",
                font=("Segoe UI", 9),
            ).pack(side="left", anchor="w")
            return
        for model in self._models:
            # Chip moderne : pastille plate sans relief 3D (frame contenant le
            # texte et la croix, afin que la largeur demandée soit fiable pour
            # la disposition en flux).
            tag = tk.Frame(self._tags_inner, bg=CHIP_BG)
            text_lbl = tk.Label(
                tag,
                text=model,
                bg=CHIP_BG,
                fg=CHIP_FG,
                padx=10,
                pady=3,
                font=("Segoe UI", 9),
            )
            text_lbl.pack(side="left")
            x_btn = tk.Label(
                tag,
                text="×",
                bg=CHIP_BG,
                fg=CHIP_REMOVE_FG,
                padx=4,
                cursor="hand2",
                font=("Segoe UI", 9, "bold"),
            )
            x_btn.pack(side="right")
            x_btn.bind("<Button-1>", lambda e, m=model: self.remove_tag(m))
            x_btn.bind("<Enter>", lambda e, b=x_btn: b.config(fg=CHIP_REMOVE_HOVER))
            x_btn.bind("<Leave>", lambda e, b=x_btn: b.config(fg=CHIP_REMOVE_FG))
            self._chips.append(tag)
        self._relayout_chips()
        # Re-disposition différée : une fois le widget réellement mappé, la
        # largeur disponible est connue et les chips se replient correctement.
        try:
            self.after_idle(self._relayout_chips)
        except Exception:
            pass

    def _relayout_chips(self, event=None):
        """Dispose les chips en flux : chaque chip conserve sa largeur
        naturelle (jamais compressée) et passe à la ligne suivante lorsque
        la largeur disponible est dépassée."""
        try:
            avail = self._tags_inner.winfo_width()
        except Exception:
            avail = 0
        if not avail or avail < 40:
            # Conteneur pas encore étalonné : fallback sur le parent.
            try:
                avail = self.master.winfo_width() - 20
            except Exception:
                avail = 0
        if not avail or avail < 40:
            avail = 400
        row = col = 0
        used = 0
        for chip in self._chips:
            w = chip.winfo_reqwidth()
            if col > 0 and used + CHIP_GAP + w > avail:
                row += 1
                col = 0
                used = 0
            chip.grid(
                row=row,
                column=col,
                sticky="w",
                padx=(0, CHIP_GAP),
                pady=2,
            )
            used += (CHIP_GAP if col else 0) + w
            col += 1

    def _add_from_entry(self, event=None):
        text = self._entry_var.get().strip()
        if not text:
            return
        for part in re.split(r"[,;\s]+", text):
            part = part.strip()
            if part:
                self.add_tag(part)
        self._entry_var.set("")
        self._entry.focus()

    def get_models(self):
        return list(self._models)