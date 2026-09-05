# UVI mobile (Android / iOS)

Application mobile de l'IDE UVI, écrite en **Kivy**. Elle réutilise
**exactement le même moteur** que la version bureau : un script produit la
même sortie sur téléphone et sur ordinateur.

## Pourquoi ce n'est pas une « recompilation » du logiciel bureau

Aucun outil ne transforme un programme Tkinter en application mobile :
**Tkinter n'existe ni sur Android ni sur iOS**. Ce qui a rendu le portage
possible, c'est que le moteur du langage n'a jamais dépendé de l'interface.

Le dépôt est donc désormais organisé ainsi :

| Fichier | Rôle | Dépend de Tkinter |
|---|---|---|
| `uvi_engine.py` | lexer, parseur, évaluateur, VM bytecode, VFS, agent | **non** |
| `uvi_core.py` | IDE de bureau | oui |
| `mobile/uvi_mobile/` | IDE mobile (Kivy) | non |

`uvi_core.py` fait `from uvi_engine import *`, si bien que tout le code
existant (et les 743 tests) continue de fonctionner sans modification.

Le moteur ne connaît son environnement qu'à travers un objet « hôte » dont il
n'utilise que **neuf membres** — c'est tout le contrat à implémenter pour une
nouvelle plateforme :

```
log(message, niveau)      request_input(nom)        canvas_draw(op, *args)
key_is_down(touche)       key_was_pressed(touche)   update_vfs_list()
_set_status(message)      _empiler_toast(msg, niv)  pump_events()
```

Trois implémentations coexistent : `HeadlessHost` (tests, dans
`uvi_engine.py`), `UVIIDE` (bureau), `UVIMobileHost`
(`mobile/uvi_mobile/host.py`).

## Lancer l'application sur ordinateur (développement)

Kivy fonctionne aussi sur Windows/macOS/Linux : on développe et on teste
l'application mobile **sans téléphone ni émulateur**.

Kivy n'a pas encore de version compatible Python 3.14 (celui installé sur la
machine de développement) — l'environnement utilise donc Python 3.11 :

```bash
cd mobile
py -V:Astral/CPython3.11.15 -m venv .venv     # ou tout autre Python 3.11/3.12
.venv\Scripts\python.exe -m pip install "kivy[base]" pygments
.venv\Scripts\python.exe main.py
```

## Compiler l'APK Android

`buildozer`, qui produit l'APK, **ne fonctionne que sous Linux**. Deux voies :

### A. GitHub Actions (recommandé — aucun Linux requis)

Le workflow [`.github/workflows/android.yml`](../.github/workflows/android.yml)
compile l'APK sur un runner Linux :

1. Onglet **Actions** du dépôt → *APK Android* → **Run workflow**
2. À la fin, l'APK est dans les **artefacts** du job (`uvi-android-apk`)

Un tag `v*` (`git tag v9.0.0 && git push --tags`) déclenche aussi le build.

Premier build : 45-70 min (téléchargement du SDK/NDK, compilation de CPython
pour ARM). Les suivants, avec le cache : ~10 min.

### B. En local, via WSL

WSL est présent sur la machine mais **sans distribution installée**. À
installer d'abord (`wsl --install -d Ubuntu`, ~500 Mo + redémarrage), puis :

```bash
sudo apt install -y python3-pip openjdk-17-jdk zip unzip autoconf libtool
pip install buildozer cython
cd mobile && python3 prepare_build.py && buildozer android debug
```

L'APK sort dans `mobile/bin/`.

## Compiler pour iOS

**Bloqué en l'état** : Apple n'autorise la compilation et la signature d'une
application iOS que depuis **macOS avec Xcode**, et il faut un compte Apple
Developer (99 $/an) pour l'installer sur un appareil ou la publier.

Le code, lui, est prêt : Kivy cible iOS via
[kivy-ios](https://github.com/kivy/kivy-ios), et **aucune ligne de
`mobile/` n'est spécifique à Android**. Le jour où un Mac est disponible :

```bash
pip install kivy-ios
toolchain build python3 kivy pygments
toolchain create UVI /chemin/vers/mobile
open uvi-ios/UVI.xcodeproj      # puis signer et lancer depuis Xcode
```

Une alternative sans Mac physique : un CI macOS (GitHub Actions
`macos-latest`, Codemagic). Le compte Apple Developer reste nécessaire.

## Ce que fait l'application

- **Éditeur** avec coloration syntaxique UVI (lexer Pygments écrit pour ce
  projet, `widgets/editor.py`) ; repli automatique en texte simple si
  Pygments est absent.
- **Exécution** par les deux moteurs : `▶ EXÉCUTER` (tree-walker, langage
  complet) et `⚡ VM` (bytecode), comme sur bureau.
- **Console** couleur avec les quatre niveaux du moteur, tampon circulaire de
  2000 lignes.
- **Canevas** `DRAW.*` complet (rect, circle, line, text, poly, sprite).
- **Pavé tactile** pour `KEY.DOWN` / `KEY.PRESSED` : un téléphone n'a pas de
  flèches, les scripts de jeu seraient sinon injouables. Les noms de touches
  envoyés au moteur sont ceux de Tkinter (`left`, `space`…), donc un script
  écrit sur ordinateur fonctionne tel quel.
- **Saisie** `?nom` via une barre dédiée qui bloque le script exactement
  comme sur bureau.
- **Explorateur de fichiers** sur le VFS, avec ouverture, enregistrement et
  suppression.

### Repère du canevas

Un script écrit sur bureau suppose un canevas de plusieurs centaines de
pixels. Sur un téléphone en portrait, rendre ces coordonnées telles quelles
couperait la moitié du dessin. Le canevas mobile présente donc un **repère
logique fixe de 800 × 600**, mis à l'échelle pour tenir dans l'écran en
préservant les proportions. Kivy plaçant son origine en bas à gauche (Tkinter
en haut à gauche), l'axe vertical est inversé au rendu — sans quoi tous les
dessins seraient retournés.

### Volontairement absent de cette première version

- **`SHION.ASK`** : l'appel fonctionne et remonte proprement son
  indisponibilité, mais aucun backend n'est embarqué. Le modèle local pèse
  plusieurs gigaoctets (hors de question dans un APK) ; il faudra le brancher
  sur le backend distant, ce qui suppose d'abord la connexion au compte.
- **Débogueur pas-à-pas** et **panneaux Variables/Modules/Tests** : utiles sur
  un grand écran, difficilement lisibles sur un téléphone. À reprendre avec
  une ergonomie pensée pour le tactile plutôt qu'un portage direct.
