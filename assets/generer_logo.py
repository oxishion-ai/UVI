"""Génère les assets de l'identité visuelle UVI : le « noyau » -- trois
arcs orbitaux (Univers . Vision . Intelligence) autour d'un point central.

POURQUOI UN SCRIPT SÉPARÉ ET NON UNE GÉNÉRATION AU LANCEMENT : ce sont des
fichiers statiques (icône de fenêtre, image pour le README/GitHub). Les
régénérer à chaque `python uvi_core.py` coûterait du temps de démarrage
pour un résultat identique à chaque fois -- on les génère une fois ici, on
committe le résultat, et uvi_core.py se contente de les charger.

ZÉRO DÉPENDANCE : ni Pillow ni aucune bibliothèque d'image. Les PNG sont
écrits par `tkinter.PhotoImage.write(..., format="png")` (Tk >= 8.6 sait
encoder du PNG nativement), et le SVG est du texte XML généré à la main --
même logique que le reste du projet ("zéro framework, tout maison").

Le même triplet de constantes (angles, épaisseurs, couleurs) alimente les
trois sorties (PNG rasterisés, SVG vectoriel, et -- indépendamment, voir
`dessiner_noyau_uvi` dans uvi_core.py -- le badge Canvas de la barre du
haut) : une seule définition du dessin, trois rendus.
"""
import math
import os
import tkinter as tk

# --- Palette (miroir de `class T` dans uvi_core.py) ------------------------
FOND = "#060B18"
CYAN = "#22D3EE"
BLEU_VIF = "#3B82F6"
VIOLET = "#A855F7"
TEXTE_FORT = "#EAF2FF"

# --- Géométrie du motif (proportions relatives à `rayon`) ------------------
# Mêmes proportions que `dessiner_noyau_uvi` dans uvi_core.py : épaisseur
# d'arc ~28% du rayon, cœur ~44%, reflet ~16% décalé vers le haut-gauche.
ARCS = (
    (90, 100, BLEU_VIF),   # Univers
    (210, 100, CYAN),      # Vision
    (330, 100, VIOLET),    # Intelligence
)


def _point(cx, cy, rayon, angle_deg):
    """Point sur le cercle de rayon `rayon`, convention 0°=droite,
    90°=haut, angle croissant = sens antihoraire à l'écran (cohérent avec
    Tkinter create_arc et avec la fonction _rasteriser ci-dessous)."""
    a = math.radians(angle_deg)
    return cx + rayon * math.cos(a), cy - rayon * math.sin(a)


# --- 1. Rasterisation PNG (pur Tkinter, aucune dépendance image) -----------

def _rasteriser(n: int) -> "tk.PhotoImage":
    """Construit un PhotoImage transparent de n x n pixels contenant le
    motif. Les pixels hors motif ne sont jamais touchés -- une PhotoImage
    fraîchement créée est transparente partout par défaut, donc pas besoin
    de gérer l'alpha explicitement.

    Optimisation : un seul appel .put() par SEGMENT horizontal contigu
    d'une même couleur (pas un appel par pixel), sinon un icône 256x256
    prendrait des dizaines de milliers d'appels Tcl."""
    photo = tk.PhotoImage(width=n, height=n)
    cx = cy = n / 2.0
    rayon = n * 0.44
    epaisseur = max(1.4, rayon * 0.28)
    r_arc = rayon - epaisseur / 2.0
    r_arc_min, r_arc_max = r_arc - epaisseur / 2.0, r_arc + epaisseur / 2.0
    r_coeur = rayon * 0.44
    r_reflet = rayon * 0.16
    dx_reflet = -r_coeur * 0.28
    dy_reflet = -r_coeur * 0.28

    def couleur_en(px, py):
        dx, dy = px - cx, py - cy
        dist = math.hypot(dx, dy)
        # Reflet (petit disque clair, décalé) en premier : il est
        # entièrement contenu dans le cœur, donc prioritaire sur lui.
        if math.hypot(dx - dx_reflet, dy - dy_reflet) <= r_reflet:
            return TEXTE_FORT
        if dist <= r_coeur:
            return CYAN
        if r_arc_min <= dist <= r_arc_max:
            angle = math.degrees(math.atan2(-dy, dx)) % 360.0
            for debut, etendue, couleur in ARCS:
                fin = debut + etendue
                if fin <= 360:
                    dans = debut <= angle < fin
                else:
                    dans = angle >= debut or angle < (fin - 360)
                if dans:
                    return couleur
        return None

    for y in range(n):
        py = y + 0.5
        x = 0
        while x < n:
            c = couleur_en(x + 0.5, py)
            if c is None:
                x += 1
                continue
            x0 = x
            while x < n and couleur_en(x + 0.5, py) == c:
                x += 1
            photo.put(c, to=(x0, y, x, y + 1))
    return photo


def generer_png(dossier: str) -> None:
    # 512 ajouté pour l'application mobile : c'est la taille attendue par
    # buildozer pour l'icône Android (mobile/buildozer.spec, icon.filename)
    # et le minimum exigé par le Play Store pour la fiche de l'application.
    for taille in (16, 24, 32, 48, 64, 128, 256, 512):
        img = _rasteriser(taille)
        chemin = os.path.join(dossier, f"uvi_icon_{taille}.png")
        img.write(chemin, format="png")
        print(f"  écrit {chemin} ({taille}x{taille})")


# --- 2. Asset vectoriel SVG (texte XML écrit à la main, pour README/docs) --

def generer_svg(dossier: str) -> None:
    cx = cy = 50.0
    rayon = 40.0
    epaisseur = rayon * 0.28
    r_arc = rayon - epaisseur / 2.0
    r_coeur = rayon * 0.44
    r_reflet = rayon * 0.16
    dx_reflet = -r_coeur * 0.28
    dy_reflet = -r_coeur * 0.28

    morceaux_arcs = []
    for debut, etendue, couleur in ARCS:
        x0, y0 = _point(cx, cy, r_arc, debut)
        x1, y1 = _point(cx, cy, r_arc, debut + etendue)
        # large-arc-flag=0 (etendue < 180°), sweep-flag=0 (notre angle
        # croissant va dans le sens antihoraire à l'écran -- voir _point).
        morceaux_arcs.append(
            f'  <path d="M {x0:.2f} {y0:.2f} A {r_arc:.2f} {r_arc:.2f} 0 0 0 '
            f'{x1:.2f} {y1:.2f}" fill="none" stroke="{couleur}" '
            f'stroke-width="{epaisseur:.2f}" stroke-linecap="round"/>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <!-- Identité visuelle UVI : trois arcs orbitaux (Univers . Vision .
       Intelligence, dans l'ordre bleu -> cyan -> violet) autour d'un
       noyau central -- généré par assets/generer_logo.py, NE PAS éditer
       ce fichier à la main (régénérer le script à la place). -->
  <circle cx="{cx}" cy="{cy}" r="48" fill="{FOND}"/>
{chr(10).join(morceaux_arcs)}
  <circle cx="{cx}" cy="{cy}" r="{r_coeur:.2f}" fill="{CYAN}"/>
  <circle cx="{cx + dx_reflet:.2f}" cy="{cy + dy_reflet:.2f}" r="{r_reflet:.2f}" fill="{TEXTE_FORT}"/>
</svg>
"""
    chemin = os.path.join(dossier, "uvi_logo.svg")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  écrit {chemin}")


if __name__ == "__main__":
    dossier = os.path.dirname(os.path.abspath(__file__))
    racine = tk.Tk()
    racine.withdraw()
    print("Génération des icônes PNG...")
    generer_png(dossier)
    print("Génération du logo SVG...")
    generer_svg(dossier)
    racine.destroy()
    print("Terminé.")
