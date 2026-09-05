# Auto-mise-à-jour de UVI

Depuis cette version, UVI vérifie au démarrage (2,5s après l'ouverture de la
fenêtre, en arrière-plan, jamais bloquant) si une version plus récente est
disponible, et peut la télécharger + l'installer elle-même. Voir
[`uvi_updater.py`](../uvi_updater.py) pour l'implémentation, et l'entrée
« Vérifier les mises à jour » du Portail (Ctrl+K) pour le déclenchement
manuel.

## Comment ça marche

1. `uvi_updater.check_for_update(UVI_VERSION)` télécharge un petit fichier
   JSON public (`UPDATE_MANIFEST_URL`) et compare le numéro de version
   qu'il annonce à `UVI_VERSION` (défini en tête de
   [`uvi_core.py`](../uvi_core.py)).
2. Si une version plus récente est annoncée, une fenêtre non bloquante
   propose « Mettre à jour » ou « Plus tard ».
3. En cas d'acceptation : téléchargement de l'installeur vers un fichier
   temporaire, vérification de son empreinte SHA-256 (si le manifeste en
   fournit une -- fortement recommandé, voir plus bas), puis lancement de
   `uvi_setup.exe /SILENT /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`.
4. L'installeur (Inno Setup, [`uvi_installer.iss`](../packaging/uvi_installer.iss))
   fait le reste tout seul : `AppMutex=UVI_IDE_RUNNING_MUTEX` + un mutex
   Windows du même nom tenu par UVI.exe pendant toute son exécution (voir
   `uvi_updater.hold_app_mutex()`, appelé avant `UVIIDE()` dans le bloc
   `__main__`) lui permettent de détecter l'instance en cours, de la fermer
   proprement (`CloseApplications=yes`), d'écrire les nouveaux fichiers,
   puis de relancer UVI (`RestartApplications=yes`). Rien côté Python n'a
   besoin de fermer l'application explicitement.

Toute panne réseau ou manifeste absent/mal formé se résout en `None` côté
`check_for_update` -- jamais une exception, jamais un message d'erreur
imposé à l'utilisateur au démarrage.

## Format de `update.json`

```json
{
  "version": "9.1.0",
  "url": "https://mwcucahokitzscjrndek.supabase.co/storage/v1/object/public/uvi-releases/uvi_setup_9.1.0.exe",
  "sha256": "<empreinte SHA-256 hexadécimale de uvi_setup_9.1.0.exe>",
  "notes": "Résumé court des changements, affiché dans la fenêtre de mise à jour."
}
```

`sha256` est optionnel mais fortement recommandé : sans lui, le fichier
téléchargé est installé sans vérification d'intégrité. Avec lui, tout
fichier corrompu ou altéré en transit/à la source est rejeté (`installation
annulée`) avant d'être exécuté.

## Hébergement du manifeste et de l'installeur

`uvi_updater.UPDATE_MANIFEST_URL` pointe par défaut vers un bucket
**Supabase Storage public** du même projet que l'authentification
([`uvi_auth.py`](../uvi_auth.py), réf. `mwcucahokitzscjrndek`) :

```
https://mwcucahokitzscjrndek.supabase.co/storage/v1/object/public/uvi-releases/update.json
```

Choisi plutôt que GitHub Releases car le dépôt est privé (les assets de
release privée nécessitent une authentification que les utilisateurs
finaux n'ont pas), et plutôt que le site Netlify pour ne pas alourdir le
dépôt git avec des binaires de plusieurs dizaines/centaines de Mo à chaque
publication.

**Mise en place ponctuelle (une seule fois)** -- nécessite le tableau de
bord Supabase, que je n'ai pas :
1. Dashboard Supabase → Storage → *New bucket*
2. Nom : `uvi-releases`
3. Activer **Public bucket**

Sans cette étape, `check_for_update` échoue silencieusement (404) et UVI
se comporte exactement comme avant -- aucun risque à laisser le système
en place avant que le bucket existe.

## Checklist de publication d'une nouvelle version

1. Bump la version à DEUX endroits (volontairement non synchronisés
   automatiquement -- Inno Setup ne peut pas lire une constante Python) :
   - `UVI_VERSION` en tête de [`uvi_core.py`](../uvi_core.py)
   - `MyAppVersion` dans [`uvi_installer.iss`](../packaging/uvi_installer.iss)
2. Recompiler :
   ```bash
   pyinstaller packaging/uvi.spec --distpath packaging/dist --workpath packaging/build --noconfirm
   ```
3. Générer l'installeur (Inno Setup 6 requis, voir
   [EMPAQUETAGE_WINDOWS.md](EMPAQUETAGE_WINDOWS.md)) :
   ```bash
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\uvi_installer.iss
   ```
   → produit `packaging\dist_installer\uvi_setup.exe`
4. Calculer son empreinte :
   ```powershell
   Get-FileHash packaging\dist_installer\uvi_setup.exe -Algorithm SHA256
   ```
5. Écrire `update.json` (voir format ci-dessus) avec le nouveau numéro de
   version, l'URL publique où l'exécutable sera hébergé, et l'empreinte
   calculée à l'étape 4.
6. Uploader `uvi_setup.exe` (idéalement renommé `uvi_setup_<version>.exe`
   pour ne jamais écraser une version encore en cours de déploiement) ET
   `update.json` dans le bucket `uvi-releases`.
7. `update.json` doit être uploadé EN DERNIER -- c'est lui qui annonce
   la disponibilité de la mise à jour ; le publier avant que l'exécutable
   soit en place ferait pointer les utilisateurs vers un lien mort.

## Tester sans toucher au bucket réel

```bash
set UVI_UPDATE_MANIFEST_URL=http://127.0.0.1:8000/update.json
python uvi_core.py
```

(servir un `update.json` de test avec `python -m http.server 8000` depuis
un dossier contenant un faux manifeste + un installeur factice) --
`UVI_UPDATE_MANIFEST_URL` prime toujours sur la valeur par défaut, aucune
modification de code nécessaire pour tester.

## Automatiser l'upload

Les étapes 6-7 sont manuelles (dashboard Supabase) faute d'un jeton
`service_role` -- avec un jeton fourni ponctuellement (jamais persisté,
même règle que pour le jeton Supabase CLI utilisé pour les Edge Functions),
l'upload vers Storage pourrait être scripté à la place.
