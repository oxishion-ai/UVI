"""Pavé tactile — source des évènements KEY.DOWN / KEY.PRESSED sur mobile.

Un téléphone n'a ni flèches ni barre d'espace : sans ce pavé, tout script UVI
qui lit le clavier (les jeux, essentiellement) serait injouable. Les boutons
produisent exactement les mêmes noms de touches que Tkinter côté bureau
("left", "right", "up", "down", "space", "return"), pour qu'un script écrit
sur ordinateur fonctionne sans modification sur téléphone.

La sémantique est calquée sur UVIIDE.key_is_down / key_was_pressed :

  - `est_enfoncee`  : état instantané, vrai tant que le doigt reste posé.
  - `a_ete_pressee` : front montant, CONSOMMÉ à la lecture -- un appui ne
    déclenche qu'une seule fois, sinon KEY.PRESSED serait indiscernable de
    KEY.DOWN et n'aurait aucune raison d'exister séparément.

Les noms sont normalisés en minuscules sans espaces, comme côté bureau
(`str(key_name).strip().lower()`), pour que "Right", "right" et " RIGHT "
désignent la même touche.
"""

from __future__ import annotations

import threading

from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout

from ..theme import T, rgba


class PaveTactile(GridLayout):
    """Croix directionnelle + touches d'action, affichée sous le canevas."""

    #: (libellé affiché, nom de touche envoyé au moteur), disposé en grille
    #: 3 colonnes pour former une croix directionnelle lisible au pouce.
    #:
    #: Libellés en ASCII et non en flèches Unicode (▲ ◀ ▶ ▼) : la police par
    #: défaut de Kivy ne contient pas les formes géométriques, qui
    #: s'affichent en carrés vides -- constaté à l'écran sur la barre du
    #: haut. Le nom envoyé au moteur, lui, reste celui de Tkinter ("up",
    #: "left"...) pour qu'un script écrit sur ordinateur marche tel quel.
    TOUCHES = [
        ("", None),        ("^", "up"),        ("", None),
        ("<", "left"),     ("ESPACE", "space"), (">", "right"),
        ("", None),        ("v", "down"),      ("ENTREE", "return"),
    ]

    def __init__(self, **kwargs):
        kwargs.setdefault("cols", 3)
        kwargs.setdefault("spacing", 4)
        kwargs.setdefault("padding", 6)
        super().__init__(**kwargs)
        self._verrou = threading.Lock()
        self._enfoncees = set()
        self._fronts = set()

        for libelle, touche in self.TOUCHES:
            if touche is None:
                # Case vide de la croix : un widget neutre plutôt qu'un
                # bouton désactivé, pour ne pas suggérer une action absente.
                from kivy.uix.widget import Widget
                self.add_widget(Widget())
                continue
            bouton = Button(
                # Les flèches d'un caractère méritent d'être grandes (cible
                # au pouce) ; les libellés en toutes lettres déborderaient à
                # cette taille.
                text=libelle, font_size="24sp" if len(libelle) == 1 else "13sp",
                bold=True,
                background_normal="", background_down="",
                background_color=rgba(T.PANNEAU_HAUT),
                color=rgba(T.CYAN),
            )
            bouton.bind(
                on_press=lambda _b, t=touche: self._presser(t),
                on_release=lambda _b, t=touche: self._relacher(t),
            )
            self.add_widget(bouton)

    # ------------------------------------------------------------------
    # Côté interface (thread Kivy)
    # ------------------------------------------------------------------

    def _presser(self, touche: str):
        with self._verrou:
            self._enfoncees.add(touche)
            self._fronts.add(touche)

    def _relacher(self, touche: str):
        with self._verrou:
            self._enfoncees.discard(touche)

    # ------------------------------------------------------------------
    # Côté moteur (thread d'exécution du script)
    # ------------------------------------------------------------------

    def est_enfoncee(self, nom: str) -> bool:
        touche = str(nom).strip().lower()
        with self._verrou:
            return touche in self._enfoncees

    def a_ete_pressee(self, nom: str) -> bool:
        touche = str(nom).strip().lower()
        with self._verrou:
            if touche in self._fronts:
                self._fronts.discard(touche)
                return True
            return False

    def reinitialiser(self):
        """Appelé à l'arrêt d'un script : sans ça, une touche encore tenue au
        moment du STOP resterait « enfoncée » pour l'exécution suivante."""
        with self._verrou:
            self._enfoncees.clear()
            self._fronts.clear()
