"""bug_reports.py — Système interne de rapports de bugs pour UVI.

BUT : faire remonter, au fil des mises à jour, des informations
STRUCTURELLES sur les plantages du moteur (type d'exception, quelle
fonction interne, quel type d'instruction UVI était en cours) pour
améliorer la stabilité -- SANS JAMAIS transmettre ni stocker le code
source de l'utilisateur, ses variables, ni le texte de ses messages
d'erreur.

CE QUI EST DÉLIBÉRÉMENT EXCLU DE TOUT RAPPORT -- jamais construit, jamais
sérialisé, pas juste "filtré après coup" :
  - le contenu de l'éditeur ou de tout fichier .uvi
  - le message texte d'une exception (str(exc)) : peut interpoler du code
    utilisateur (ex. SafeEvalError(f"Échec d'évaluation de '{expr}' :
    {e}") dans uvi_core.py inclut l'expression source telle quelle dans
    son message -- str(exc) n'est donc JAMAIS sérialisé ici)
  - toute valeur de variable (aucune capture de locals dans les
    tracebacks -- traceback.extract_tb ne le fait pas par défaut, et ce
    module se garde bien d'utiliser TracebackException(capture_locals=True))
  - le texte source de chaque ligne de traceback (FrameSummary.line) :
    omis même pour les fichiers du projet lui-même, un numéro de ligne
    suffit à la retrouver dans la version correspondante du code
  - tout chemin de fichier hors de ce projet (seul le nom de fichier est
    gardé, jamais le chemin absolu complet -- évite de révéler le nom de
    session Windows de l'utilisateur, ex. C:\\Users\\<nom>\\...)

CE QUI EST INCLUS -- uniquement des faits structurels, jamais du contenu :
  - le nom de classe de l'exception (ex. "ZeroDivisionError")
  - le code d'erreur interne UVI s'il existe (ex. "CRASH", "MAX_RECURSION")
  - le type de nœud AST en cours d'exécution, si connu (ex. "IfNode") --
    un nom de classe interne, jamais le contenu de la ligne UVI
  - le numéro de ligne DANS LE SCRIPT (un entier, jamais son contenu)
  - une traceback restreinte à fichier/ligne/fonction (jamais de source)
  - version courte de UVI (hash git), version de Python, plateforme
  - un identifiant de rapport aléatoire et un horodatage UTC

STOCKAGE : uniquement LOCAL, dans bug_reports/*.json à côté de ce fichier
(dossier gitignoré -- voir .gitignore). Ce module NE FAIT AUCUN APPEL
RÉSEAU : tant qu'aucun serveur de collecte UVI n'existe, "remonter" un
rapport signifie l'écrire sur disque, consultable par qui a accès à cette
machine (voir le panneau PARAMÈTRES de l'IDE, carte RAPPORTS DE BUGS).
Le jour où un tel serveur existera, le point d'extension est
BugReportStore.write() : y ajouter un appel HTTP optionnel (derrière un
opt-in explicite, jamais par défaut) est le seul changement nécessaire --
aucun appelant de record_exception() n'aurait à changer.

DÉSACTIVATION : variable d'environnement UVI_BUG_REPORTS=0 (ou "false")
désactive complètement la capture -- record_exception() devient un no-op
silencieux. Peut être placée dans .shion.local.env comme toute autre
variable de ce projet (voir _charger_config_shion_locale dans
uvi_core.py, qui charge ce fichier avant que quoi que ce soit d'autre ne
soit lu)."""

import json
import os
import platform
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional


_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPORTS_DIR = os.path.join(_PROJECT_DIR, "bug_reports")

# Plafond de rapports conservés -- un moteur qui plante en boucle (ex. une
# régression déclenchée à chaque script) ne doit pas remplir le disque à
# l'infini. Les plus ANCIENS sont supprimés en premier (voir _purger) :
# ce sont les nouveaux qui ont le plus de valeur diagnostique une fois le
# plafond atteint (ils reflètent l'état le plus récent du code).
_MAX_REPORTS = 200


def _est_active() -> bool:
    valeur = os.environ.get("UVI_BUG_REPORTS", "1").strip().lower()
    return valeur not in ("0", "false", "non", "off")


def _version_uvi() -> str:
    """Hash git court du dépôt comme identifiant de version -- honnête et
    sans entretien (pas de constante à penser à incrémenter à chaque
    changement), à la différence d'un numéro de version manuel. "unknown"
    si ce dossier n'est pas (ou plus) un dépôt git chez l'utilisateur final,
    ou si git met trop de temps à répondre -- un rapport reste exploitable
    sans cette info, elle n'est qu'un indice de contexte, jamais une
    donnée requise."""
    try:
        resultat = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_DIR, capture_output=True, text=True, timeout=5,
        )
        if resultat.returncode == 0:
            return resultat.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


# Calculé en ARRIÈRE-PLAN, une seule fois au chargement du module -- lancer
# un sous-processus git est mesuré à plusieurs SECONDES sur la machine de
# développement de ce projet (matériel modeste, cf. docs/SERVEUR_LOCAL_CPU.md
# pour un autre exemple de cette contrainte). record_exception() peut être
# appelée SYNCHRONEMENT depuis le thread Tk principal (via
# report_callback_exception, voir son point d'accroche dans uvi_core.py) --
# bloquer ce thread plusieurs secondes pour un simple hash de version
# transformerait l'outil censé améliorer la stabilité en une cause de gel
# de l'IDE au pire moment (pendant qu'un plantage est déjà en cours). Le
# premier rapport écrit avant que ce thread ait fini peut donc porter
# uvi_version="unknown" ; les suivants auront la vraie valeur, déjà en
# cache.
_VERSION_CACHE: Optional[str] = None


def _peupler_version_arriere_plan() -> None:
    global _VERSION_CACHE
    _VERSION_CACHE = _version_uvi()


threading.Thread(target=_peupler_version_arriere_plan, daemon=True).start()


def _version_cachee() -> str:
    return _VERSION_CACHE if _VERSION_CACHE is not None else "unknown"


def _sanitiser_traceback(tb) -> list:
    """Réduit une traceback Python à fichier/ligne/fonction UNIQUEMENT --
    jamais le texte de la ligne source (FrameSummary.line), jamais de
    valeurs de variables locales. Le chemin de fichier est réduit à son
    seul nom (os.path.basename) : un chemin absolu complet peut contenir
    le nom de session Windows de l'utilisateur (C:\\Users\\<nom>\\...),
    qui n'a rien à faire dans un rapport structurel."""
    frames = []
    for frame in traceback.extract_tb(tb):
        frames.append({
            "fichier": os.path.basename(frame.filename),
            "ligne": frame.lineno,
            "fonction": frame.name,
        })
    return frames


def _construire_rapport(exc: BaseException, *, source: str,
                         error_code: Optional[str],
                         ast_node_type: Optional[str],
                         uvi_line: Optional[int]) -> dict:
    return {
        "report_id": str(uuid.uuid4()),
        "horodatage_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uvi_version": _version_cachee(),
        "python_version": sys.version.split()[0],
        "plateforme": platform.platform(),
        # "source" : quel sous-système a intercepté l'exception (ex.
        # "tree_walker", "vm", "tkinter_callback", "thread", "main") --
        # PAS un chemin ni un identifiant utilisateur.
        "source": source,
        "error_code": error_code,
        "exception_type": type(exc).__name__,
        "ast_node_type": ast_node_type,
        "uvi_line": uvi_line,
        "traceback": _sanitiser_traceback(exc.__traceback__),
    }


def _purger_si_necessaire() -> None:
    try:
        fichiers = sorted(
            (f for f in os.listdir(_REPORTS_DIR) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(_REPORTS_DIR, f)),
        )
    except OSError:
        return
    trop = len(fichiers) - _MAX_REPORTS
    for f in fichiers[:max(trop, 0)]:
        try:
            os.remove(os.path.join(_REPORTS_DIR, f))
        except OSError:
            pass


def record_exception(exc: BaseException, *, source: str,
                      error_code: Optional[str] = None,
                      ast_node_type: Optional[str] = None,
                      uvi_line: Optional[int] = None) -> Optional[str]:
    """Point d'entrée principal. Construit un rapport SANS AUCUN contenu
    utilisateur (voir docstring de module) et l'écrit sur disque.

    Ne lève JAMAIS d'exception elle-même : un système de rapport de bugs
    qui plante en essayant de rapporter un plantage serait un bug
    supplémentaire au-dessus du premier, dans le pire moment possible
    (un except déjà en cours de gestion) -- toute erreur ici est avalée
    silencieusement, en dernier recours.

    Retourne le report_id écrit, ou None si la capture est désactivée
    (UVI_BUG_REPORTS=0) ou a échoué."""
    if not _est_active():
        return None
    try:
        rapport = _construire_rapport(
            exc, source=source, error_code=error_code,
            ast_node_type=ast_node_type, uvi_line=uvi_line,
        )
        os.makedirs(_REPORTS_DIR, exist_ok=True)
        chemin = os.path.join(_REPORTS_DIR, "%s.json" % rapport["report_id"])
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(rapport, f, indent=2, ensure_ascii=False)
        _purger_si_necessaire()
        return rapport["report_id"]
    except Exception:
        return None


def report_count() -> int:
    try:
        return len([f for f in os.listdir(_REPORTS_DIR) if f.endswith(".json")])
    except OSError:
        return 0


def reports_dir() -> str:
    return _REPORTS_DIR


def list_reports(limit: int = 50) -> list:
    """Les `limit` rapports les plus récents (le plus récent en premier),
    déjà désérialisés -- pour un futur visualiseur dans l'IDE ou un
    export manuel par l'utilisateur/mainteneur."""
    try:
        fichiers = sorted(
            (f for f in os.listdir(_REPORTS_DIR) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(_REPORTS_DIR, f)),
            reverse=True,
        )
    except OSError:
        return []
    rapports = []
    for nom in fichiers[:limit]:
        try:
            with open(os.path.join(_REPORTS_DIR, nom), "r", encoding="utf-8") as f:
                rapports.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return rapports
