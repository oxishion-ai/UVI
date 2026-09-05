"""Explorateur de fichiers — équivalent mobile du panneau EXPLORATEUR.

Liste le contenu du VFS (VFSManager, voir uvi_engine.py) et permet d'ouvrir,
créer ou supprimer un script. Le moteur appelle `update_vfs_list()` sur
l'hôte après chaque &save / &delete : c'est ce qui déclenche `rafraichir()`
ici, pour que la liste reflète en direct ce que le script vient d'écrire.
"""

from __future__ import annotations

from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

from ..theme import T, rgba


class Explorateur(BoxLayout):
    """Colonne de fichiers. `on_ouvrir` reçoit le nom du fichier choisi."""

    def __init__(self, vfs, on_ouvrir=None, on_supprimer=None, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        super().__init__(**kwargs)
        self.vfs = vfs
        self.on_ouvrir = on_ouvrir
        self.on_supprimer = on_supprimer

        self._defilement = ScrollView()
        self._liste = BoxLayout(orientation="vertical", size_hint_y=None,
                                spacing=2, padding=6)
        self._liste.bind(minimum_height=self._liste.setter("height"))
        self._defilement.add_widget(self._liste)
        self.add_widget(self._defilement)

        self.rafraichir()

    def rafraichir(self, *_args):
        """Thread Kivy uniquement (l'hôte passe par Clock.schedule_once)."""
        self._liste.clear_widgets()
        fichiers = [f for f in self.vfs.list_files() if not f.startswith(".")]
        if not fichiers:
            self._liste.add_widget(Label(
                text="Aucun script.\nUtilise « Nouveau » pour commencer.",
                color=rgba(T.TEXTE_FAIBLE), font_size="13sp",
                size_hint_y=None, height="60dp", halign="center"))
            return

        for nom in fichiers:
            ligne = BoxLayout(orientation="horizontal", size_hint_y=None,
                              height="44dp", spacing=4)
            bouton = Button(
                text=nom, font_size="14sp", halign="left", valign="middle",
                background_normal="", background_down="",
                background_color=rgba(T.PANNEAU_HAUT), color=rgba(T.TEXTE_FORT),
            )
            bouton.bind(size=lambda inst, val: setattr(
                inst, "text_size", (val[0] - 20, val[1])))
            bouton.bind(on_release=lambda _b, n=nom: self._ouvrir(n))
            ligne.add_widget(bouton)

            supprimer = Button(
                text="✕", font_size="16sp", size_hint_x=None, width="48dp",
                background_normal="", background_down="",
                background_color=rgba(T.PANNEAU_HAUT), color=rgba(T.ROUGE),
            )
            supprimer.bind(on_release=lambda _b, n=nom: self._supprimer(n))
            ligne.add_widget(supprimer)

            self._liste.add_widget(ligne)

    def _ouvrir(self, nom: str):
        if self.on_ouvrir is not None:
            self.on_ouvrir(nom)

    def _supprimer(self, nom: str):
        if self.on_supprimer is not None:
            self.on_supprimer(nom)
