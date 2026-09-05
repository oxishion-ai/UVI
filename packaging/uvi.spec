# -*- mode: python ; coding: utf-8 -*-
"""Spec PyInstaller pour UVI (Windows).

Mode ONEDIR, PAS onefile -- volontaire. UVI écrit à côté de lui-même
(.uvi_prefs.json, UVI_Storage/, bug_reports/, .shion.local.env -- voir
_FICHIER_PREFS et VFSManager dans uvi_core.py) : en mode onefile,
PyInstaller extrait l'exécutable dans un dossier TEMPORAIRE DIFFÉRENT à
chaque lancement, ce qui ferait « perdre » tous les scripts et préférences
d'un lancement à l'autre -- l'app aurait l'air fraîchement réinstallée à
chaque ouverture. Onedir n'a pas ce problème : le dossier de sortie est le
même à chaque lancement, exactement comme `python uvi_core.py` exécuté
depuis un dossier fixe. Voir packaging/build_windows.md pour comment
l'empaqueter ensuite dans un vrai installateur (Inno Setup) qui installe
CE dossier tel quel dans %LOCALAPPDATA% (pas Program Files, pour rester
inscriptible sans droits administrateur).

Utilisation :  pyinstaller packaging/uvi.spec --distpath packaging/dist
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

datas = [
    (os.path.join(REPO, "assets"), "assets"),
    (os.path.join(REPO, "shion", "weights"), "shion/weights"),
    (os.path.join(REPO, "shion", "corpus"), "shion/corpus"),
    (os.path.join(REPO, "UVI_Storage"), "UVI_Storage"),
]
# models/ (poids GGUF DeepSeek, plusieurs Go) volontairement PAS inclus --
# téléchargés séparément par l'utilisateur, voir docs/SERVEUR_LOCAL_CPU.md.

a = Analysis(
    [os.path.join(REPO, "uvi_core.py")],
    pathex=[REPO],
    binaries=[],
    datas=datas,
    # uvi_engine : le moteur du langage, extrait de uvi_core.py pour être
    # partagé avec l'application mobile (voir mobile/README.md). uvi_core
    # l'importe via `from uvi_engine import *` -- une forme que l'analyse
    # statique de PyInstaller suit mal, d'où la mention explicite ici.
    hiddenimports=["numpy", "uvi_auth", "uvi_updater", "uvi_engine"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UVI",
    icon=os.path.join(REPO, "assets", "uvi_icon.ico"),
    console=False,  # app Tkinter -- pas de console qui s'ouvre derrière
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="UVI",
)
