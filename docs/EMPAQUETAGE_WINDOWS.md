# Empaqueter UVI en logiciel Windows téléchargeable

Deux étapes : **PyInstaller** transforme `uvi_core.py` + ses dépendances en
un dossier autonome (`UVI.exe` + tout ce qu'il lui faut, plus besoin de
Python installé) ; **Inno Setup** (optionnel mais recommandé) enveloppe ce
dossier dans un vrai installateur `uvi_setup.exe` avec raccourcis Menu
Démarrer/Bureau et désinstallation propre.

## 1. PyInstaller

Déjà configuré : [`packaging/uvi.spec`](../packaging/uvi.spec).

```bash
pip install pyinstaller
pyinstaller packaging/uvi.spec --distpath packaging/dist --workpath packaging/build --noconfirm
```

Résultat : `packaging/dist/UVI/UVI.exe` (+ dossier `_internal/` à côté,
obligatoire, ne pas séparer les deux). Lancer directement `UVI.exe` depuis
là pour vérifier avant d'aller plus loin.

### Pourquoi ONEDIR et pas ONEFILE

Volontaire, documenté dans le spec : UVI écrit à côté de lui-même
(`.uvi_prefs.json`, `UVI_Storage/` — les scripts de l'utilisateur,
`bug_reports/`). Le mode `--onefile` de PyInstaller extrait l'exécutable
dans un dossier **temporaire différent à chaque lancement** — l'app aurait
donc l'air fraîchement réinstallée, scripts et préférences perdus, à
chaque ouverture. Le mode dossier (onedir, celui utilisé ici) n'a pas ce
problème : c'est le même dossier à chaque lancement.

**Conséquence pour la distribution** : le dossier d'installation doit être
**inscriptible sans droits administrateur** (pas `C:\Program Files`, en
lecture seule sans élévation) — voir §2, `DefaultDirName={localappdata}`.

### Ce qui est embarqué, ce qui ne l'est pas

- Inclus : `assets/` (icônes), `shion/weights/` (Shion-Nano/Micro, quelques
  Mo, le moteur natif de secours), `shion/corpus/`, `UVI_Storage/` (bibliothèque
  standard `.uvi`).
- **Volontairement exclu** : `models/*.gguf` (poids DeepSeek 1,5B/7B,
  plusieurs Go) — téléchargés séparément par qui veut le backend DeepSeek
  local, voir `docs/SERVEUR_LOCAL_CPU.md`. Les embarquer aurait fait gonfler
  l'installateur de plusieurs Go pour une fonctionnalité optionnelle.

## 2. Installateur Inno Setup (recommandé)

1. Installer [Inno Setup 6](https://jrsoftware.org/isdl.php) (gratuit,
   ~3 Mo).
2. Compiler [`packaging/uvi_installer.iss`](../packaging/uvi_installer.iss) :

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\uvi_installer.iss
```

3. Résultat : `packaging/dist_installer/uvi_setup.exe` — double-clic,
   installation en un clic (pas de droits admin requis, installe dans
   `%LOCALAPPDATA%\UVI`), raccourcis Menu Démarrer + Bureau (optionnel),
   désinstallation via le Panneau de configuration.

C'est ce fichier `uvi_setup.exe` qui devient le lien "Télécharger UVI" sur
le site (`web/site/index.html`, actuellement un bouton placeholder
« Téléchargement bientôt disponible » — remplacer son `href` une fois
`uvi_setup.exe` hébergé quelque part, ex. un asset de release GitHub ou un
lien Netlify direct).

## 3. Sans Inno Setup : distribution en ZIP

Alternative plus simple, sans outil supplémentaire : zipper directement
`packaging/dist/UVI/` et le proposer en téléchargement — l'utilisateur
dézippe et lance `UVI.exe`. Moins poli (pas de raccourci Menu Démarrer, pas
de désinstalleur) mais fonctionnellement identique.

## 4. Icône

`assets/uvi_icon.ico` (généré par
[`assets/generer_icone_ico.py`](../assets/generer_icone_ico.py) à partir des
PNG déjà produits par `generer_logo.py` — aucune dépendance d'image,
même esprit que le reste du projet) sert à la fois pour l'icône de
l'exécutable (`packaging/uvi.spec`, `icon=...`) et pour l'icône de
l'installateur (`uvi_installer.iss`, `SetupIconFile=...`). Régénérer les
deux scripts dans l'ordre si le logo change :

```bash
python assets/generer_logo.py        # PNG + SVG
python assets/generer_icone_ico.py   # .ico, à partir des PNG ci-dessus
```

## 5. macOS / Linux

Non couvert ici (périmètre de cette première version — voir la discussion
de portée). PyInstaller supporte les deux, mais avec des conventions
différentes (`.app`/`.dmg` pour macOS via `py2app` ou l'option `BUNDLE` de
PyInstaller, `AppImage`/paquet natif pour Linux) qui méritent leur propre
passe plutôt qu'une adaptation rapide du spec Windows.
