"""Copie les modules partagés du dépôt dans mobile/ avant un build mobile.

POURQUOI UNE COPIE PLUTÔT QU'UN FICHIER VERSIONNÉ EN DOUBLE : uvi_engine.py
est le MÊME interpréteur que celui de l'IDE de bureau. En garder deux copies
dans le dépôt garantirait qu'elles divergent au premier correctif appliqué
d'un seul côté -- exactement le problème que la séparation moteur/interface
cherchait à éviter. La copie est donc générée à la demande, jamais commitée
(voir .gitignore).

POURQUOI UNE COPIE PLUTÔT QU'UN sys.path VERS LE PARENT : python-for-android
et kivy-ios n'empaquettent QUE le contenu de `source.dir` (mobile/). Un
import remontant d'un dossier fonctionne en développement sur ordinateur et
échoue sur le téléphone, ce qui est le pire des deux mondes -- une panne qui
n'apparaît qu'après le build.

Usage :
    python prepare_build.py          # copie les modules
    python prepare_build.py --clean  # les retire
"""

from __future__ import annotations

import os
import shutil
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)

#: Modules de la racine dont l'application mobile a besoin. uvi_auth est
#: inclus parce que uvi_engine l'importe (gestion du compte et du niveau
#: d'abonnement pour SHION.ASK) ; uvi_core (IDE Tkinter) ne l'est PAS et ne
#: doit jamais l'être : Tkinter n'existe pas sur mobile.
MODULES = ("uvi_engine.py", "uvi_auth.py")


def copier():
    for nom in MODULES:
        source = os.path.join(RACINE, nom)
        if not os.path.exists(source):
            sys.exit("Introuvable : %s" % source)
        shutil.copy2(source, os.path.join(ICI, nom))
        print("copié  %s" % nom)


def nettoyer():
    for nom in MODULES:
        cible = os.path.join(ICI, nom)
        if os.path.exists(cible):
            os.remove(cible)
            print("retiré %s" % nom)


if __name__ == "__main__":
    if "--clean" in sys.argv:
        nettoyer()
    else:
        copier()
