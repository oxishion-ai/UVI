"""Point d'entrée de l'application mobile UVI.

Le nom `main.py` n'est pas un choix de style : python-for-android (Android) et
kivy-ios (iOS) cherchent tous deux ce fichier précis à la racine du dossier
empaqueté. Le renommer casserait le build.

IMPORT DU MOTEUR, DEUX SITUATIONS :

  - En développement sur ordinateur, uvi_engine.py est dans le dossier PARENT
    (racine du dépôt, partagé avec l'IDE de bureau) -- on l'ajoute au
    sys.path.
  - Dans l'application empaquetée, il a été copié À CÔTÉ de ce fichier par
    prepare_build.py, parce que l'empaqueteur ne prend que le contenu de
    `mobile/`.

Le test d'existence ci-dessous couvre les deux sans configuration : c'est le
même code qui tourne en développement et sur le téléphone.
"""

import os
import sys

_ICI = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(_ICI, "uvi_engine.py")):
    _PARENT = os.path.dirname(_ICI)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
if _ICI not in sys.path:
    sys.path.insert(0, _ICI)


def main():
    from uvi_mobile.app import UVIMobileApp
    UVIMobileApp().run()


if __name__ == "__main__":
    main()
