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

Chemin éprouvé sur cette machine (aucun droit administrateur requis :
`wsl --update` puis `wsl --install` passent en utilisateur normal).

**Prendre Ubuntu 22.04, pas la dernière version.** Ubuntu 26.04 n'embarque
que Python 3.14, avec lequel buildozer et Cython ne fonctionnent pas ; la
22.04 fournit Python 3.10, la configuration sur laquelle buildozer est
réellement éprouvé.

```bash
wsl --update
wsl --install -d Ubuntu-22.04 --no-launch
```

Puis, dans la distribution :

```bash
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip python3-dev \
    autoconf automake libtool libtool-bin libltdl-dev pkg-config \
    zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 \
    cmake libffi-dev libssl-dev build-essential ccache
pip install --user 'cython==0.29.36' buildozer
```

```bash
cd mobile && python3 prepare_build.py && buildozer android debug
```

L'APK sort dans `mobile/bin/`.

#### Quatre pièges, tous rencontrés et tous résolus

Ils coûtent chacun entre dix minutes et deux heures à découvrir, puisque
c'est le temps qu'il faut au build pour y arriver.

1. **`libltdl-dev` n'est pas optionnel.** libffi 3.4.2 réclame la macro
   `LT_SYS_SYMBOL_USCORE`, apparue dans libtool 2.4.7 ; Ubuntu 22.04 livre
   la 2.4.6, et seule `ltdl.m4` (de `libltdl-dev`) la définit. Sans lui,
   `autoreconf` échoue sur *possibly undefined macro* après avoir déjà
   compilé hostpython3.

2. **Les licences du SDK doivent être pré-acceptées.** `sdkmanager` pose la
   question sur l'entrée standard ; un build lancé sans terminal reçoit une
   fin de fichier, comptée comme un refus, et `build-tools` n'est jamais
   installé (*Aidl not found*). Écrire les fichiers d'empreinte dans
   `$ANDROID_SDK/licenses/` règle le problème sans dialogue. Noter aussi
   que `sdkmanager` exige `--sdk_root=` explicitement, faute de quoi il
   échoue en affichant son aide, ce qui masque la vraie cause.

3. **Supprimer le venv de build entre deux essais.** p4a rejoue
   `hostpython -m venv venv` sur le venv existant, ce qui y réinstalle pip
   25.3 par-dessus la 26.2.1 d'un build précédent. La 25.3 range `build_env`
   dans un fichier, la 26.2.1 dans un dossier : l'écrasement laisse les
   deux, et l'import casse (*cannot import name
   `BuildDependencyInstallError`*) — tout à la fin du build. Sur un venv
   neuf, l'ordre est sain.

4. **Alléger les dépendances de la recette Kivy** avec
   [`patch_p4a.py`](patch_p4a.py) : voir sa docstring, qui détaille pourquoi
   `requests` fait échouer le build via `charset-normalizer` et pourquoi ni
   un épinglage ni une recette locale n'y suffisent.

Sur un Pentium N3710 (4 cœurs, 1,6 GHz, 3,8 Go de RAM visibles par WSL), le
build complet demande environ **2 h 30**, dont l'essentiel en compilation de
CPython et de Kivy pour ARM64. Les recompilations suivantes sont bien plus
rapides : `~/.buildozer` et `ccache` conservent tout le travail natif.

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
