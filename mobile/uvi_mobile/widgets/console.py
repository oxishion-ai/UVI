"""Console de sortie mobile — équivalent du dock CONSOLE de l'IDE de bureau.

Les quatre niveaux du moteur ("info", "err", "sys", "ok" — voir
HeadlessHost.log) sont rendus avec le même code couleur que sur bureau, via
le balisage `[color=...]` des Label Kivy plutôt qu'avec des tags de widget
Text (qui n'existent pas ici).

PLAFOND DE LIGNES : un script en boucle peut journaliser des dizaines de
milliers de lignes. Un Label Kivy contenant tout l'historique devient une
seule texture géante et finit par échouer à s'allouer sur mobile. La console
ne garde donc que les MAX_LIGNES dernières, comme un terminal à tampon
circulaire -- ce qui est de toute façon ce dont on a besoin en débogage.
"""

from __future__ import annotations

from collections import deque

from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

from ..theme import COULEURS_NIVEAU, T, rgba


def _echapper(texte: str) -> str:
    """Neutralise le balisage Kivy dans la sortie d'un script : sans ça, un
    script qui affiche "[b]" ferait basculer toute la console en gras, et un
    "[color=" malformé lèverait une exception de rendu. La sortie utilisateur
    est une DONNÉE, jamais du balisage."""
    return (str(texte).replace("&", "&amp;")
                      .replace("[", "&bl;")
                      .replace("]", "&br;"))


class Console(ScrollView):
    """Zone de sortie défilante."""

    MAX_LIGNES = 2000

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lignes = deque(maxlen=self.MAX_LIGNES)
        self.etiquette = Label(
            text="", markup=True, halign="left", valign="top",
            font_name="RobotoMono-Regular", font_size="13sp",
            color=rgba(T.TEXTE), size_hint_y=None, padding=(10, 10),
        )
        self.etiquette.bind(
            width=lambda inst, val: setattr(inst, "text_size", (val - 20, None)),
            texture_size=lambda inst, val: setattr(inst, "height", val[1]),
        )
        self.add_widget(self.etiquette)

    def ajouter_lignes(self, paquet):
        """`paquet` : liste de (texte, niveau). Thread Kivy uniquement --
        l'hôte groupe les lignes avant d'appeler ici (voir
        UVIMobileHost._vider_tampon)."""
        for texte, niveau in paquet:
            couleur = COULEURS_NIVEAU.get(niveau, T.TEXTE)
            prefixe = {"err": "[ERREUR] ", "sys": "", "ok": ""}.get(niveau, "")
            self._lignes.append(
                "[color=%s]%s%s[/color]" % (couleur.lstrip("#"),
                                            _echapper(prefixe), _echapper(texte))
            )
        self.etiquette.text = "\n".join(self._lignes)
        # Défilement automatique vers le bas : on veut voir la DERNIÈRE
        # ligne produite, comme dans un terminal.
        self.scroll_y = 0

    def effacer(self):
        self._lignes.clear()
        self.etiquette.text = ""
