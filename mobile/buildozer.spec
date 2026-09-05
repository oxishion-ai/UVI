[app]

# --- Identité ---------------------------------------------------------------
title = UVI
package.name = uvi
package.domain = ai.oxishion

# --- Sources ----------------------------------------------------------------
# source.dir est CE dossier (mobile/) et non la racine du dépôt : empaqueter
# la racine embarquerait models/ (plusieurs Go de poids GGUF), packaging/dist
# (le build Windows) et shion/weights -- l'APK ferait des gigaoctets pour rien.
# uvi_engine.py et uvi_auth.py, qui vivent à la racine et sont PARTAGÉS avec
# l'IDE de bureau, sont copiés ici par prepare_build.py juste avant le build
# (voir sa docstring : une copie automatisée plutôt qu'un doublon versionné,
# qui divergerait au premier correctif appliqué d'un seul côté).
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,uvi,json,txt
source.include_patterns = UVI_Storage/*.uvi
source.exclude_dirs = .venv,__pycache__,.buildozer,bin,tests

version = 9.0.0

# --- Dépendances ------------------------------------------------------------
# Volontairement minimal : le moteur UVI n'utilise que la bibliothèque
# standard (voir la docstring de uvi_engine.py). pygments sert uniquement à
# la coloration syntaxique de l'éditeur, et son absence est déjà gérée
# proprement (repli sur un TextInput simple, voir widgets/editor.py).
requirements = python3,kivy==2.3.1,pygments,certifi

# --- Présentation -----------------------------------------------------------
orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/../assets/uvi_icon_512.png

# --- Android ----------------------------------------------------------------
# INTERNET est nécessaire à SHION.ASK et à la connexion au compte UVI
# (uvi_auth.py). Aucune autre permission : UVI écrit ses scripts dans le
# stockage privé de l'application (App.user_data_dir), ce qui ne demande
# AUCUNE permission depuis Android 10 -- demander l'accès au stockage
# partagé serait à la fois inutile et un motif de rejet sur le Play Store.
android.permissions = android.permission.INTERNET

android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True

# --- iOS --------------------------------------------------------------------
# Le build iOS ne passe PAS par buildozer mais par kivy-ios (toolchain
# séparée), et exige macOS + Xcode : voir mobile/README.md §iOS. Ces deux
# clés servent seulement à ce que la configuration reste complète et
# transposable le jour où un Mac est disponible.
ios.kivy_ios_url = https://github.com/kivy/kivy-ios
ios.kivy_ios_branch = master

[buildozer]
log_level = 2
warn_on_root = 1
