"""Canevas graphique mobile — rendu des opérations DRAW.* du moteur UVI.

Reproduit à l'identique la sémantique du canevas Tkinter de l'IDE de bureau
(voir UVIIDE.canvas_draw et _draw_sprite_grid dans uvi_core.py), avec deux
adaptations imposées par Kivy :

1. REPÈRE INVERSÉ. Tkinter place l'origine en HAUT à gauche, Y vers le bas ;
   Kivy la place en BAS à gauche, Y vers le haut. Sans conversion, tout
   script dessinerait à l'envers verticalement. La conversion est faite ici,
   au rendu, et non dans l'hôte : elle dépend de la hauteur RÉELLE du widget,
   connue seulement au moment de dessiner.

2. VIEWPORT LOGIQUE. Un script écrit sur bureau suppose un canevas de
   plusieurs centaines de pixels (demo_complet.uvi dessine jusqu'à x=300).
   Sur un téléphone en portrait, la zone de dessin est plus petite : rendre
   les coordonnées telles quelles couperait la moitié du dessin. Le canevas
   présente donc un repère logique fixe (VIEWPORT_L x VIEWPORT_H) mis à
   l'échelle pour tenir dans le widget, proportions préservées (bandes noires
   sur les côtés si besoin). Un même script donne ainsi la même image sur
   bureau et sur mobile, à la taille près.

Les opérations sont ACCUMULÉES (self._operations) plutôt que dessinées à la
volée : Kivy reconstruit son arbre d'instructions à chaque redimensionnement,
et sans historique le dessin disparaîtrait à la première rotation de l'écran.
DRAW.CLEAR vide cette liste, exactement comme c.delete("all") côté Tkinter.
"""

from __future__ import annotations

import math
import threading

from kivy.graphics import Color, Ellipse, Line, Rectangle
from kivy.uix.widget import Widget
from kivy.core.text import Label as CoreLabel

from ..theme import T, rgba

#: Repère logique dans lequel les scripts dessinent (voir docstring).
VIEWPORT_L = 800.0
VIEWPORT_H = 600.0


class UVICanvas(Widget):
    """Widget de dessin. `empiler()` est appelable depuis le thread
    d'exécution ; le rendu, lui, n'a lieu que dans le thread Kivy."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._operations = []
        self._verrou = threading.Lock()
        self._fond = T.CHAMP
        self.bind(pos=self._redessiner, size=self._redessiner)

    # ------------------------------------------------------------------
    # Réception des opérations (thread d'exécution)
    # ------------------------------------------------------------------

    def empiler(self, op: str, args):
        """Ajoute une opération. Thread-safe : appelée depuis le thread du
        script, alors que _redessiner lit la même liste depuis le thread
        Kivy."""
        with self._verrou:
            if op == "clear":
                self._operations = []
            elif op == "bgcolor":
                self._fond = str(args[0]) if args and str(args[0]).strip() else "#000000"
            else:
                self._operations.append((op, args))
        # Kivy redessine tout seul au prochain cycle via la propriété canvas,
        # mais il faut le lui demander explicitement puisque rien n'a changé
        # dans pos/size : on passe par le Clock du thread principal.
        from kivy.clock import Clock
        Clock.schedule_once(self._redessiner, 0)

    def effacer(self):
        with self._verrou:
            self._operations = []
        self._redessiner()

    # ------------------------------------------------------------------
    # Conversion repère logique -> repère écran
    # ------------------------------------------------------------------

    def _echelle(self):
        """Facteur d'échelle et décalage pour centrer le viewport logique
        dans le widget en préservant les proportions."""
        if self.width <= 0 or self.height <= 0:
            return 1.0, 0.0, 0.0
        k = min(self.width / VIEWPORT_L, self.height / VIEWPORT_H)
        dx = self.x + (self.width - VIEWPORT_L * k) / 2.0
        dy = self.y + (self.height - VIEWPORT_H * k) / 2.0
        return k, dx, dy

    def _pt(self, x, y, k, dx, dy):
        """(x, y) logique en repère Tkinter -> (x, y) écran en repère Kivy.
        L'inversion verticale est ici : y_logique=0 est en HAUT."""
        return dx + float(x) * k, dy + (VIEWPORT_H - float(y)) * k

    # ------------------------------------------------------------------
    # Rendu (thread Kivy uniquement)
    # ------------------------------------------------------------------

    def _redessiner(self, *_args):
        with self._verrou:
            operations = list(self._operations)
            fond = self._fond

        k, dx, dy = self._echelle()
        self.canvas.clear()
        with self.canvas:
            # Fond du viewport logique -- dessiné à sa taille exacte pour que
            # les bandes de letterboxing restent distinctes de la zone de
            # dessin (on voit où s'arrête le canevas du script).
            Color(*rgba(fond))
            Rectangle(pos=(dx, dy), size=(VIEWPORT_L * k, VIEWPORT_H * k))

            for op, args in operations:
                try:
                    self._rendre(op, args, k, dx, dy)
                except Exception:
                    # Une opération malformée (couleur invalide, argument
                    # manquant) ne doit jamais interrompre le rendu des
                    # autres -- même tolérance que le try/except TclError
                    # de UVIIDE._draw_sprite_grid côté bureau.
                    continue

    def _rendre(self, op, args, k, dx, dy):
        if op == "rect":
            x, y, w, h, couleur = args[:5]
            remplissage = args[5] if len(args) >= 6 else ""
            epaisseur = args[6] if len(args) >= 7 else 1
            # Coin bas-gauche en repère Kivy = (x, y + h) en repère Tkinter.
            px, py = self._pt(x, float(y) + float(h), k, dx, dy)
            lx, ly = float(w) * k, float(h) * k
            if str(remplissage).strip():
                Color(*rgba(remplissage))
                Rectangle(pos=(px, py), size=(lx, ly))
            if str(couleur).strip():
                Color(*rgba(couleur))
                Line(rectangle=(px, py, lx, ly), width=max(1.0, float(epaisseur) * k * 0.5))

        elif op == "circle":
            x, y, r, couleur = args[:4]
            remplissage = args[4] if len(args) >= 5 else ""
            epaisseur = args[5] if len(args) >= 6 else 1
            r = float(r)
            px, py = self._pt(float(x) - r, float(y) + r, k, dx, dy)
            d = 2 * r * k
            if str(remplissage).strip():
                Color(*rgba(remplissage))
                Ellipse(pos=(px, py), size=(d, d))
            if str(couleur).strip():
                Color(*rgba(couleur))
                Line(ellipse=(px, py, d, d), width=max(1.0, float(epaisseur) * k * 0.5))

        elif op == "line":
            x1, y1, x2, y2, couleur = args[:5]
            epaisseur = args[5] if len(args) >= 6 else 1
            a = self._pt(x1, y1, k, dx, dy)
            b = self._pt(x2, y2, k, dx, dy)
            Color(*rgba(couleur))
            Line(points=[a[0], a[1], b[0], b[1]],
                 width=max(1.0, float(epaisseur) * k * 0.5))

        elif op == "text":
            x, y, texte, couleur = args[:4]
            taille = args[4] if len(args) >= 5 else 10
            # anchor="nw" côté Tkinter : (x, y) est le coin HAUT-gauche.
            etiquette = CoreLabel(text=str(texte), font_size=max(8.0, float(taille) * k),
                                  color=rgba(couleur))
            etiquette.refresh()
            tex = etiquette.texture
            px, py = self._pt(x, y, k, dx, dy)
            Color(1, 1, 1, 1)  # la couleur est déjà dans la texture
            Rectangle(texture=tex, pos=(px, py - tex.height), size=tex.size)

        elif op == "poly":
            points, couleur = args[0], args[1]
            remplissage = args[2] if len(args) >= 3 else ""
            epaisseur = args[3] if len(args) >= 4 else 1
            plats = []
            for i in range(0, len(points) - 1, 2):
                sx, sy = self._pt(points[i], points[i + 1], k, dx, dy)
                plats.extend([sx, sy])
            if not plats:
                return
            trait = remplissage if str(remplissage).strip() else couleur
            Color(*rgba(trait if str(trait).strip() else couleur))
            # Kivy n'a pas de polygone plein simple pour un contour
            # quelconque : on ferme le tracé, ce qui rend le contour
            # correct dans tous les cas et le remplissage visuellement
            # proche pour les formes convexes (triangles des scripts UVI).
            Color(*rgba(couleur if str(couleur).strip() else trait))
            Line(points=plats + plats[:2],
                 width=max(1.0, float(epaisseur) * k * 0.5), close=True)

        elif op == "sprite":
            x, y, taille, pixels = args
            n = len(pixels)
            if n <= 0:
                return
            cote = int(math.isqrt(n))
            if cote <= 0:
                return
            taille = float(taille)
            for i in range(cote * cote):
                ligne, colonne = divmod(i, cote)
                couleur = pixels[i] if pixels[i] not in (None, "") else self._fond
                px, py = self._pt(float(x) + colonne * taille,
                                  float(y) + (ligne + 1) * taille, k, dx, dy)
                Color(*rgba(str(couleur)))
                Rectangle(pos=(px, py), size=(taille * k, taille * k))
