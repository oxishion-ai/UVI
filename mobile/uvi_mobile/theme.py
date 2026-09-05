"""Palette de l'application mobile UVI.

Reprise à l'identique de la classe T de l'IDE de bureau (uvi_core.py) pour
que les deux applications se ressemblent : même bleu profond, mêmes accents,
même code couleur de coloration syntaxique. Un utilisateur qui passe du
bureau au téléphone doit reconnaître UVI immédiatement.

Kivy attend des couleurs en (r, g, b, a) normalisés 0..1, pas des chaînes
"#RRGGBB" : `rgba()` fait la conversion une fois pour toutes, et les
constantes sont exposées dans les DEUX formes -- la forme hexadécimale reste
nécessaire pour le balisage `[color=...]` des Label Kivy.
"""

from __future__ import annotations


def rgba(hexa: str, alpha: float = 1.0):
    """"#RRGGBB" (ou "#RGB") -> (r, g, b, a) normalisé pour Kivy.

    Accepte aussi les quelques noms de couleur Tk que les scripts UVI
    existants peuvent employer dans DRAW.* ("black", "white", "red"...) :
    ils viennent de l'API Tkinter d'origine et doivent continuer à
    fonctionner tels quels sur mobile, sinon un script écrit sur le bureau
    dessinerait faux sur le téléphone.
    """
    if not hexa:
        return (0.0, 0.0, 0.0, 0.0)  # chaîne vide = pas de remplissage
    texte = str(hexa).strip()
    nomme = _NOMS_TK.get(texte.lower())
    if nomme:
        texte = nomme
    if not texte.startswith("#"):
        return (0.0, 0.0, 0.0, 0.0)
    texte = texte[1:]
    if len(texte) == 3:
        texte = "".join(c * 2 for c in texte)
    if len(texte) != 6:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        r = int(texte[0:2], 16) / 255.0
        v = int(texte[2:4], 16) / 255.0
        b = int(texte[4:6], 16) / 255.0
    except ValueError:
        return (0.0, 0.0, 0.0, 0.0)
    return (r, v, b, alpha)


#: Noms de couleur Tkinter les plus courants -- voir docstring de rgba().
_NOMS_TK = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
    "green": "#008000", "blue": "#0000FF", "yellow": "#FFFF00",
    "cyan": "#00FFFF", "magenta": "#FF00FF", "gray": "#808080",
    "grey": "#808080", "orange": "#FFA500", "purple": "#800080",
    "pink": "#FFC0CB", "brown": "#A52A2A", "lime": "#00FF00",
    "navy": "#000080", "teal": "#008080", "silver": "#C0C0C0",
}


class T:
    """Mêmes noms que la classe T du bureau, pour que le code se lise pareil."""

    # Fonds, du plus profond au plus clair
    FOND = "#060B18"
    PANNEAU = "#0B1424"
    PANNEAU_HAUT = "#0E1A2E"
    CHAMP = "#0A1220"
    SURVOL = "#152640"

    # Traits
    BORDURE = "#1B2C48"
    BORDURE_VIVE = "#264066"

    # Textes
    TEXTE = "#C3D6F0"
    TEXTE_FORT = "#EAF2FF"
    TEXTE_FAIBLE = "#5D7599"

    # Accents
    CYAN = "#22D3EE"
    BLEU = "#2E7CF6"
    BLEU_VIF = "#3B82F6"
    VIOLET = "#A855F7"
    ROSE = "#EC4899"
    VERT = "#22C55E"
    AMBRE = "#F59E0B"
    ROUGE = "#EF4444"

    # Coloration syntaxique
    SYN_MOTCLE = "#C084FC"
    SYN_CHAINE = "#FBBF24"
    SYN_NOMBRE = "#22D3EE"
    SYN_COMMENT = "#4B6280"
    SYN_FONCTION = "#38BDF8"
    SYN_OPERATEUR = "#F472B6"


#: Couleur associée à chaque niveau de log du moteur (voir HeadlessHost.log :
#: les niveaux sont "info", "err", "sys", "ok").
COULEURS_NIVEAU = {
    "info": T.TEXTE,
    "err": T.ROUGE,
    "sys": T.TEXTE_FAIBLE,
    "ok": T.VERT,
}
