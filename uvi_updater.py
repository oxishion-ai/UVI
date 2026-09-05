"""uvi_updater.py — auto-mise-à-jour de l'app de bureau UVI (Windows).

STDLIB UNIQUEMENT (urllib, hashlib, subprocess, ctypes) -- même philosophie
que uvi_auth.py : un module minimal, importable sans dépendance lourde, qui
ne doit JAMAIS empêcher UVI de démarrer ou de fonctionner normalement --
toute panne réseau ou manifeste absent se résout en None, jamais en
exception qui remonterait jusqu'à l'appelant Tkinter.

Principe : un petit fichier JSON public (UPDATE_MANIFEST_URL) annonce la
dernière version disponible, l'URL de l'installeur Windows (produit par
packaging/uvi_installer.iss) et son empreinte SHA-256 :

    {"version": "9.1.0", "url": "https://.../uvi_setup.exe",
     "sha256": "...", "notes": "Correctif STR.NUM"}

uvi_core.py compare ce numéro à UVI_VERSION ; si une version plus récente
est annoncée, l'IDE propose de télécharger puis lance l'installeur.
L'installeur (AppMutex + CloseApplications=yes + RestartApplications=yes
dans uvi_installer.iss) se charge lui-même de fermer UVI proprement -- une
fois lancé (launch_installer), ce module n'a plus rien à faire, l'app peut
continuer à tourner jusqu'à ce que l'installeur la ferme.

Publier une mise à jour : voir docs/MISE_A_JOUR.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

# Bucket public Supabase Storage du même projet que uvi_auth.py --
# remplaçable via variable d'environnement pour tester contre un autre
# manifeste sans toucher au code (voir docs/MISE_A_JOUR.md).
_DEFAULT_MANIFEST_URL = (
    "https://mwcucahokitzscjrndek.supabase.co/storage/v1/object/public/"
    "uvi-releases/update.json"
)
UPDATE_MANIFEST_URL = os.environ.get("UVI_UPDATE_MANIFEST_URL", _DEFAULT_MANIFEST_URL).strip()

_TIMEOUT_MANIFEST_S = 6
_TIMEOUT_DOWNLOAD_S = 180

# Doit être EXACTEMENT la valeur de `AppMutex=` dans packaging/uvi_installer.iss
# -- c'est ce nom que Inno Setup surveille pour savoir si UVI tourne encore.
_MUTEX_NAME = "UVI_IDE_RUNNING_MUTEX"
_mutex_handle = None  # référence gardée en vie -- voir hold_app_mutex()


class UviUpdaterError(Exception):
    """Erreur d'auto-mise-à-jour -- message toujours déjà actionable, même
    contrat que UviAuthError (voir uvi_auth.py)."""


# ---------------------------------------------------------------------------
# Mutex nommé -- permet à uvi_installer.iss (CloseApplications=yes) de
# détecter et fermer proprement une instance de UVI en cours avant d'écrire
# les nouveaux fichiers.
# ---------------------------------------------------------------------------

def hold_app_mutex() -> None:
    """Crée et conserve un mutex Windows nommé pendant toute la durée de vie
    du processus. Windows le libère tout seul quand le processus se termine
    (quelle que soit la façon dont il se termine) -- rien à libérer
    explicitement ici, `_mutex_handle` n'existe que pour empêcher le garbage
    collector de fermer le handle prématurément. No-op silencieux hors
    Windows ou si l'appel ctypes échoue : dans ce cas, l'auto-mise-à-jour
    proposera quand même le téléchargement, mais l'installeur silencieux
    pourrait ne pas fermer UVI tout seul (l'utilisateur devra le faire à la
    main) -- jamais une raison d'empêcher le démarrage de l'app."""
    global _mutex_handle
    if sys.platform != "win32":
        return
    try:
        import ctypes
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Comparaison de versions -- tuples d'entiers, tolérant aux suffixes non
# numériques plutôt que de faire planter la comparaison sur un format
# inattendu.
# ---------------------------------------------------------------------------

def _parse_version(texte: str) -> Tuple[int, ...]:
    """'9.1.2' -> (9, 1, 2). '9.1.0-beta' -> (9, 1, 0) : seuls les chiffres
    EN TÊTE de chaque segment sont gardés, un segment sans aucun chiffre au
    début compte comme 0 plutôt que de lever une exception."""
    parties = []
    for segment in str(texte).strip().split("."):
        chiffres = ""
        for c in segment:
            if c.isdigit():
                chiffres += c
            else:
                break
        parties.append(int(chiffres) if chiffres else 0)
    return tuple(parties) if parties else (0,)


def is_newer(remote_version: str, local_version: str) -> bool:
    return _parse_version(remote_version) > _parse_version(local_version)


# ---------------------------------------------------------------------------
# Manifeste distant
# ---------------------------------------------------------------------------

def fetch_manifest() -> Optional[Dict[str, Any]]:
    """Récupère et parse UPDATE_MANIFEST_URL. None (jamais d'exception) si
    injoignable, mal formé, ou si les champs requis ("version", "url")
    manquent -- une vérification de mise à jour ratée ne doit JAMAIS
    empêcher l'IDE de démarrer ou de fonctionner normalement."""
    if not UPDATE_MANIFEST_URL:
        return None
    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_MANIFEST_S) as resp:
            corps = resp.read().decode("utf-8")
        data = json.loads(corps)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("version") or not data.get("url"):
        return None
    return data


def check_for_update(local_version: str) -> Optional[Dict[str, Any]]:
    """Manifeste distant si sa version est STRICTEMENT plus récente que
    local_version, sinon None (déjà à jour, ou vérification impossible --
    les deux cas se traitent de la même façon côté appelant : ne rien
    montrer)."""
    manifest = fetch_manifest()
    if manifest is None:
        return None
    if not is_newer(manifest["version"], local_version):
        return None
    return manifest


# ---------------------------------------------------------------------------
# Téléchargement + vérification + lancement de l'installeur
# ---------------------------------------------------------------------------

def _sha256_fichier(chemin: str) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def download_installer(manifest: Dict[str, Any],
                        progress_cb: Optional[Callable[[int, int], None]] = None) -> str:
    """Télécharge l'installeur annoncé par le manifeste vers un fichier
    temporaire et vérifie son empreinte SHA-256 si le manifeste en fournit
    une. Renvoie le chemin local. Lève UviUpdaterError (message actionable)
    sur tout échec -- contrairement à fetch_manifest, ici l'utilisateur a
    explicitement demandé la mise à jour, une erreur doit donc lui être
    montrée, pas avalée silencieusement."""
    url = manifest["url"]
    version = manifest.get("version", "0")
    dest = os.path.join(tempfile.gettempdir(), "uvi_setup_%s.exe" % version)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_DOWNLOAD_S) as resp:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            recu = 0
            with open(dest, "wb") as f:
                while True:
                    bloc = resp.read(1 << 16)
                    if not bloc:
                        break
                    f.write(bloc)
                    recu += len(bloc)
                    if progress_cb:
                        progress_cb(recu, total)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        raise UviUpdaterError("Téléchargement de la mise à jour impossible : %s" % e)

    empreinte_attendue = manifest.get("sha256")
    if empreinte_attendue:
        empreinte_recue = _sha256_fichier(dest)
        if empreinte_recue.lower() != str(empreinte_attendue).lower():
            try:
                os.remove(dest)
            except OSError:
                pass
            raise UviUpdaterError(
                "Fichier de mise à jour corrompu ou altéré (empreinte SHA-256 "
                "différente de celle annoncée par le manifeste) -- installation annulée."
            )
    return dest


def launch_installer(chemin_installeur: str) -> None:
    """Lance l'installeur Inno Setup en mode silencieux et rend la main
    IMMÉDIATEMENT (processus détaché) -- NE ferme PAS l'application
    appelante elle-même : uvi_installer.iss porte AppMutex +
    CloseApplications=yes + RestartApplications=yes, c'est l'installeur qui
    ferme UVI proprement (en attendant la libération des fichiers) puis le
    relance une fois la mise à jour posée. L'appelant n'a donc rien d'autre
    à faire après cet appel que de laisser l'utilisateur continuer à
    travailler jusqu'à la fermeture."""
    if sys.platform != "win32":
        raise UviUpdaterError("Auto-mise-à-jour disponible uniquement sur Windows.")
    drapeaux = ["/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    try:
        subprocess.Popen(
            [chemin_installeur] + drapeaux,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        raise UviUpdaterError("Impossible de lancer l'installeur : %s" % e)
