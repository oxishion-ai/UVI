"""Assemble assets/uvi_icon.ico à partir des PNG déjà générés par
generer_logo.py (uvi_icon_16/24/32/48/64/128/256.png).

ZÉRO DÉPENDANCE, même esprit que generer_logo.py : le format ICO accepte,
depuis Windows Vista, des images au format PNG telles quelles comme entrée
(pas besoin de les ré-encoder en bitmap brut) -- il suffit d'un en-tête
ICONDIR + une ICONDIRENTRY par taille, décrits ici à la main plutôt que via
Pillow. Nécessaire pour donner une icône .exe à l'empaquetage PyInstaller
(voir packaging/uvi.spec) : PyInstaller --icon exige un .ico sur Windows,
un .png ne suffit pas.
"""
import os
import struct

TAILLES = (16, 24, 32, 48, 64, 128, 256)


def generer_ico(dossier: str) -> None:
    images = []
    for taille in TAILLES:
        chemin = os.path.join(dossier, f"uvi_icon_{taille}.png")
        with open(chemin, "rb") as f:
            images.append((taille, f.read()))

    # ICONDIR : reserved=0, type=1 (icone), count=N
    entete = struct.pack("<HHH", 0, 1, len(images))

    entrees = b""
    donnees = b""
    offset = 6 + 16 * len(images)  # après ICONDIR + toutes les ICONDIRENTRY
    for taille, contenu_png in images:
        octet_taille = 0 if taille >= 256 else taille  # 0 encode 256 en ICO
        entrees += struct.pack(
            "<BBBBHHII",
            octet_taille, octet_taille,  # largeur, hauteur
            0, 0,                          # palette (0 = pas de palette), réservé
            1, 32,                         # plans couleur, bits par pixel
            len(contenu_png), offset,      # taille des données, offset
        )
        donnees += contenu_png
        offset += len(contenu_png)

    chemin_ico = os.path.join(dossier, "uvi_icon.ico")
    with open(chemin_ico, "wb") as f:
        f.write(entete + entrees + donnees)
    print(f"  écrit {chemin_ico} ({len(images)} tailles : {TAILLES})")


if __name__ == "__main__":
    generer_ico(os.path.dirname(os.path.abspath(__file__)))
    print("Terminé.")
