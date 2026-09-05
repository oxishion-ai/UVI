"""Allège les dépendances pures que python-for-android tire pour Kivy.

POURQUOI CE PATCH EXISTE. La recette Kivy de p4a déclare :

    python_depends = ['certifi', 'chardet', 'idna', 'requests', 'urllib3',
                      'filetype']

Aucun de ces paquets n'est utilisé par UVI mobile : ils servent à
`kivy.network.urlrequest` et à `kivy.garden`, que l'application n'appelle
jamais (le moteur, lui, n'utilise que `urllib` de la bibliothèque standard).

Ils ne sont pas seulement superflus, ils CASSENT le build. `requests` tire
`charset-normalizer`, pour lequel PyPI publie depuis 2025 des roues au
format Android. Le résolveur de p4a trouve cette roue et écrit son URL dans
requirements.txt :

    charset_normalizer-3.5.1-cp314-cp314-android_24_arm64_v8a.whl

puis l'installe avec le pip du venv de BUILD, qui tourne sur linux-x86_64 et
ne reçoit pas `--platform`. pip refuse la roue :

    ERROR: ... is not a supported wheel on this platform

et le build meurt APRÈS avoir compilé CPython, Kivy et pyjnius.

DEUX PISTES ÉCARTÉES AVANT CELLE-CI :
  - Épingler charset-normalizer dans buildozer.spec : sans effet, le
    résolveur de p4a ajoute l'URL de la roue Android EN PLUS de l'épinglage.
  - Fournir une recette locale : `p4a.local_recipes` n'était jamais
    transmis à p4a par buildozer, et p4a compare les noms de dossier sans
    normaliser les tirets (il cherche « charset-normalizer », un dossier
    Python ne peut pas porter ce nom).

Retirer `requests` de la recette supprime la cause plutôt qu'un symptôme.
`filetype` est conservé : pur Python, sans composant compilé, inoffensif.

Le patch est ré-appliqué à chaque build (p4a est ré-extrait par buildozer),
et il est idempotent : ré-exécuté sur une recette déjà patchée, il ne fait
rien.

Usage :
    python patch_p4a.py <racine du dossier .buildozer>
"""

from __future__ import annotations

import os
import re
import sys

#: Ce que la recette déclare à l'origine, et ce qu'on veut à la place.
MOTIF = re.compile(
    r"^(\s*)python_depends\s*=\s*\[[^\]]*\]\s*$", re.MULTILINE)
REMPLACEMENT = (
    r"\1python_depends = ['filetype']  # requests & co retirés, "
    r"voir mobile/patch_p4a.py"
)


def trouver_recette_kivy(racine: str):
    """Chemin du fichier de recette Kivy sous une racine .buildozer, ou None.

    Recherché plutôt que codé en dur : buildozer place python-for-android à
    des emplacements différents selon qu'il l'a cloné ou installé en paquet.
    """
    for dossier, _sous_dossiers, fichiers in os.walk(racine):
        if (dossier.replace("\\", "/").endswith("pythonforandroid/recipes/kivy")
                and "__init__.py" in fichiers):
            return os.path.join(dossier, "__init__.py")
    return None


def patcher(chemin: str) -> bool:
    """Renvoie True si le fichier a été modifié."""
    with open(chemin, "r", encoding="utf-8") as f:
        source = f.read()

    if "requests" not in source:
        return False  # déjà patché

    nouveau, remplacements = MOTIF.subn(REMPLACEMENT, source)
    if remplacements == 0:
        print("ATTENTION : python_depends introuvable dans %s -- la recette "
              "a peut-être changé de forme, patch NON appliqué." % chemin)
        return False

    with open(chemin, "w", encoding="utf-8") as f:
        f.write(nouveau)
    return True


def main(argv):
    if len(argv) < 2:
        sys.exit("usage: patch_p4a.py <racine .buildozer>")
    racine = argv[1]
    if not os.path.isdir(racine):
        print("racine absente (%s) -- rien à patcher" % racine)
        return 0

    recette = trouver_recette_kivy(racine)
    if recette is None:
        print("recette Kivy introuvable sous %s -- rien à patcher" % racine)
        return 0

    if patcher(recette):
        print("patché : %s" % recette)
    else:
        print("déjà patché : %s" % recette)

    with open(recette, "r", encoding="utf-8") as f:
        for ligne in f:
            if "python_depends" in ligne:
                print("  -> %s" % ligne.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
