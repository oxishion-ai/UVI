"""uvi_engine.py — moteur du langage UVI, SANS AUCUNE dépendance graphique.

Extrait de uvi_core.py (qui ne contient plus que l'IDE de bureau Tkinter et
importe tout ce fichier via `from uvi_engine import *`). La séparation existe
parce que Tkinter n'existe NI sur Android NI sur iOS : l'application mobile
(voir mobile/) réutilise ce moteur tel quel et fournit sa propre interface
Kivy, au lieu de dupliquer 9000 lignes d'interpréteur qui divergeraient
aussitôt.

Le moteur ne connaît son environnement qu'à travers un objet « hôte »
(self.ide) dont il n'utilise que 9 membres — c'est le contrat complet à
implémenter pour porter UVI sur une nouvelle plateforme :

    log(message, kind)        écrit une ligne dans la console
    request_input(...)        demande une saisie à l'utilisateur (?nom)
    canvas_draw(...)          dessine sur le canevas (DRAW.*)
    key_is_down(touche)       état clavier instantané (KEY.DOWN)
    key_was_pressed(touche)   front montant clavier (KEY.PRESSED)
    update_vfs_list()         rafraîchit l'explorateur de fichiers
    _set_status(...)          met à jour la barre d'état
    _empiler_toast(msg, niv)  notification éphémère
    pump_events()             rend la main à l'interface pendant une boucle

HeadlessHost (dans ce fichier) en est l'implémentation minimale de référence,
utilisée par la suite de tests ; UVIIDE (uvi_core.py) l'implémentation bureau ;
UVIMobileHost (mobile/uvi_mobile/host.py) l'implémentation Kivy.
"""
import ast
import ast as pyast
import operator
import time
import json
import math
import os
import random
import sys
import re
import threading
import queue
import subprocess
import tempfile
import urllib.request
import urllib.error
import uvi_auth
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Dict, Callable, Union

# Source UNIQUE du numéro de version -- badge topbar (_build_topbar), titre
# de fenêtre (_retraduire_cas_speciaux) et uvi_updater.check_for_update()
# le lisent tous ici plutôt que de répéter le littéral. Doit être bumpée en
# même temps que MyAppVersion dans packaging/uvi_installer.iss à chaque
# publication (voir docs/MISE_A_JOUR.md) -- les deux restent indépendants
# volontairement, Inno Setup ne peut pas importer une constante Python.
UVI_VERSION = "9.0.0"

try:
    # Système interne de rapports de bugs (voir bug_reports.py, à côté de
    # ce fichier) : capture les plantages inattendus SANS JAMAIS y inclure
    # de code utilisateur (voir la docstring du module pour la garantie
    # complète). try/except ici pour la même raison que HAS_TTS/HAS_SHION
    # plus bas -- un module optionnel absent (ex. quelqu'un copie
    # uvi_core.py seul, sans bug_reports.py) ne doit jamais empêcher UVI
    # de démarrer, seulement désactiver silencieusement cette capture.
    import bug_reports
    HAS_BUG_REPORTS = True
except ImportError:
    HAS_BUG_REPORTS = False


def _signaler_crash(exc: BaseException, *, source: str,
                     error_code: Optional[str] = None,
                     ast_node_type: Optional[str] = None,
                     uvi_line: Optional[int] = None) -> None:
    """Enveloppe fine autour de bug_reports.record_exception -- un seul
    endroit qui gère HAS_BUG_REPORTS ET avale toute exception secondaire,
    pour que chaque site d'appel (une douzaine à travers ce fichier) n'ait
    pas à répéter ces deux gardes individuellement."""
    if not HAS_BUG_REPORTS:
        return
    try:
        bug_reports.record_exception(
            exc, source=source, error_code=error_code,
            ast_node_type=ast_node_type, uvi_line=uvi_line,
        )
    except Exception:
        pass


def _charger_config_shion_locale() -> None:
    """Charge .shion.local.env (à côté de ce fichier) dans les variables
    d'environnement du PROCESSUS, AVANT que SHION_BACKEND et le reste de
    la config SHION/DeepSeek (plus bas dans ce fichier) ne soient lus --
    permet de configurer SHION.ASK UNE FOIS pour toutes (ex. après avoir
    monté un serveur DeepSeek sur Colab) sans avoir à redéfinir des
    variables d'environnement à chaque lancement.

    Ce fichier n'est JAMAIS versionné (voir .gitignore) : il contient
    typiquement une URL de tunnel et une clé d'API propres à UNE session
    de calcul de SON auteur. Partager ce dossier (VS Code, zip, dépôt
    cloné) ne le partage donc PAS -- quiconque n'a pas ce fichier local
    retombe simplement sur SHION_BACKEND=shion (le moteur natif embarqué,
    fonctionne sans aucune configuration, voir _build_shion_client). Pour
    qui l'a, os.environ.setdefault ne fait JAMAIS taire une variable
    d'environnement RÉELLEMENT exportée par l'utilisateur : ce fichier ne
    fournit que des valeurs par défaut, jamais une priorité.

    Absent chez tout le monde tant que personne n'en crée un -- aucun
    changement de comportement pour qui n'utilise pas cette fonctionnalité."""
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".shion.local.env")
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            lignes = f.readlines()
    except OSError:
        return  # fichier absent (cas normal) ou illisible : rien à charger
    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        os.environ.setdefault(cle.strip(), valeur.strip())


_charger_config_shion_locale()


# ==============================================================================
# FICHIER UNIQUE — modules fusionnés ci-dessous, dans cet ordre :
#   compiler_v2.py -> uvi_vm.py -> tools.py -> self_correction.py
#   -> agent_loop.py -> uvi_agent_extension.py
# Tous les imports croisés entre ces modules ont été retirés (même
# espace de noms module-level désormais) ; seuls les imports externes
# (stdlib) ont été remontés dans le bloc d'imports du tout début de ce
# fichier, dédupliqués.
# ==============================================================================

"""
UVIBytecodeCompilerV2 — Phase 1 du plan validé.

Produit un bytecode 100% conforme à UVI_BYTECODE_SPEC_v1.md :
  - désucrage AND/OR/NOT en JUMP_IF_FALSE / JUMP_ABS (§1, note BOOL_AND/OR)
  - décomposition complète TAB en primitives INDEX/APPEND/LEN/... (§2)
  - CALL_BUILTIN uniforme pour STR/DISK/SHION, exception SHION_ASK
    documentée : aucun STORE émis après (§3)
  - résolution finale des labels en indices absolus (§5)
  - en-tête de version bytecode_version: "1.0" (§7)

Ce module est autonome : il importe uniquement les classes AST et Token
depuis le script principal (pas de dépendance sur l'ancien
UVIBytecodeCompiler ni sur UVIEvaluator). Il ne modifie rien à
l'existant — c'est un consommateur d'AST parallèle, pas un patch en place.

Portée des fonctions (validée) : pile de frames, lecture local -> parent ->
global, écriture toujours locale. Pas de copie de mémoire globale à l'appel.
Ce fichier ne fait QUE compiler — le modèle de frame est exécuté côté VM
(Phase 3), mais le compilateur doit déjà émettre CALL_FUNC/RET conformes à
ce modèle (§4), donc aucune primitive de "sauvegarde/restauration mémoire
globale" n'est émise ici, contrairement à l'ancien compilateur.
"""



# ---------------------------------------------------------------------------
# §0 — Opcodes v1 (classe séparée de l'ancien Opcode, pour ne pas toucher au
# tree-walker existant pendant que la v2 se stabilise — cf. décision prise
# avec l'utilisateur : pas de modification en place du compilateur v1).
# ---------------------------------------------------------------------------

class OpcodeV1:
    """Conforme à UVI_BYTECODE_SPEC_v1.md — v1.1.
    BOOL_AND/BOOL_OR de la v1.0 sont retirés (jamais implémentés) :
    le désucrage AND/OR passe entièrement par JUMP_IF_FALSE/JUMP_IF_TRUE,
    porté par le compilateur (voir _compile_boolop)."""
    # 1.a — Pile / données
    PUSH_CONST    = "PUSH_CONST"
    LOAD          = "LOAD"
    STORE         = "STORE"
    BINOP         = "BINOP"
    CMP           = "CMP"
    UNARY_NOT     = "UNARY_NOT"
    UNARY_NEG     = "UNARY_NEG"
    MAKE_ARRAY    = "MAKE_ARRAY"
    POP           = "POP"             # découvert Phase 1A : nécessaire pour
                                       # un CallNode-statement (f(x) sans
                                       # capture) — sinon la pile fuit à
                                       # chaque appel non capturé
    # 1.b — Contrôle de flux
    JUMP_ABS      = "JUMP_ABS"
    JUMP_IF_FALSE = "JUMP_IF_FALSE"
    JUMP_IF_TRUE  = "JUMP_IF_TRUE"    # v1.1
    LABEL         = "LABEL"
    CALL_FUNC     = "CALL_FUNC"
    RET           = "RET"
    CALL_ROUTINE  = "CALL_ROUTINE"    # v1.4 -- *nom (routines) : distinct de
                                       # CALL_FUNC/RET, n'empile JAMAIS de
                                       # nouvelle Frame -- voir _compile_node
                                       # RoutineDefNode/RoutineCallNode et la
                                       # boucle d'exécution (recherche
                                       # "CALL_ROUTINE") pour la justification
                                       # complète : une routine doit pouvoir
                                       # lire ET écrire les mêmes variables
                                       # que son appelant, ce que CALL_FUNC/
                                       # RET (STORE toujours dans
                                       # current_frame.locals) ne permettent
                                       # pas -- confirmé empiriquement avant
                                       # ce correctif (le tree-walker donnait
                                       # un résultat différent de la VM pour
                                       # le même script).
    RET_ROUTINE   = "RET_ROUTINE"     # v1.4 -- symétrique de CALL_ROUTINE.
    ROUTINE_REG   = "ROUTINE_REG"     # v1.4 -- enregistre (nom -> entry_idx)
                                       # pour une routine, distinct de
                                       # DEF_REG : pas de params, pas de
                                       # meta/n_locals (une routine n'a
                                       # jamais sa propre Frame).
    DEF_REG       = "DEF_REG"
    HALT          = "HALT"
    TRAP          = "TRAP"            # v1.3 — erreur runtime générique
    LOCAL_STORE   = "LOCAL_STORE"     # v1.3 — mémoire de frame indexée
    LOCAL_LOAD    = "LOCAL_LOAD"      # v1.3
    # 1.c — I/O minimale
    PRINT         = "PRINT"           # v1.1, primitive noyau (pas CALL_BUILTIN)
    # §2 — TAB (décomposition complète)
    INDEX      = "INDEX"
    INDEX_SET  = "INDEX_SET"
    APPEND     = "APPEND"
    POP_ARRAY  = "POP_ARRAY"
    DEL_INDEX  = "DEL_INDEX"
    LEN        = "LEN"
    # §3 — Services runtime
    CALL_BUILTIN = "CALL_BUILTIN"
    # §4 — Système de modules (MODULE/EXPORT/alias::nom) -- AJOUT DE
    # CETTE SESSION, non présent dans UVI_BYTECODE_SPEC_v1.md — v1.1
    # (le fichier de spec externe a disparu avec la réinitialisation du
    # sandbox, donc pas de vérification formelle de conformité possible
    # ici -- style de nommage aligné sur DEF_REG/ROUTINE_REG/CALL_ROUTINE
    # existants par cohérence, mais une future session devrait intégrer
    # ces trois opcodes à la spec v1.1 si/quand elle est retrouvée ou
    # reconstruite, plutôt que de les laisser non documentés côté spec).
    # Sémantique volontairement DISTINCTE de CALL_BUILTIN/CALL_FUNC : un
    # MODULE_LOAD lit un fichier et compile/exécute une VM ENFANT
    # complète (accès au compilateur lui-même), ce qu'aucun CALL_BUILTIN
    # existant ne fait -- nouvel opcode dédié plutôt que détourner un
    # mécanisme dont la sémantique diffère, même légèrement (même
    # principe déjà appliqué à ROUTINE_REG/CALL_ROUTINE vs DEF_REG/
    # CALL_FUNC pour les routines).
    MODULE_LOAD      = "MODULE_LOAD"      # charge fichier -> alias, VM enfant
    EXPORT_NAME       = "EXPORT_NAME"      # marque un nom comme exporté
    CALL_MODULE_FUNC = "CALL_MODULE_FUNC"  # CALL alias::nom(args)
    MODULE_GET       = "MODULE_GET"       # lecture alias::nom en expression


BINOP_MAP = {
    pyast.Add: "ADD", pyast.Sub: "SUB", pyast.Mult: "MUL",
    pyast.Div: "DIV", pyast.Mod: "MOD", pyast.Pow: "POW",
}
CMP_MAP = {
    pyast.Eq: "EQ", pyast.NotEq: "NEQ", pyast.Lt: "LT",
    pyast.LtE: "LTE", pyast.Gt: "GT", pyast.GtE: "GTE",
}


@dataclass
class Instr:
    opcode: str
    operands: list = field(default_factory=list)
    # Ajout de cette session (débogueur) : ligne source UVI d'où cette
    # instruction provient. 0 = non renseignée (valeur par défaut,
    # rétrocompatible avec tout Instr(opcode, operands) déjà écrit
    # ailleurs dans ce fichier avant cette session -- aucun site
    # existant n'a besoin d'être modifié individuellement, voir
    # UVIBytecodeCompilerV2._compile_node qui tague automatiquement
    # après coup, sans toucher aux dizaines de méthodes
    # _compile_*_node existantes qui créent des Instr directement).
    line: int = 0

    def to_dict(self):
        return {"opcode": self.opcode, "operands": self.operands, "line": self.line}


class BytecodeError(Exception):
    """Levée à la compilation si une construction n'est pas représentable
    en bytecode conforme (ex: expression non parseable par ast)."""
    pass


@dataclass
class UVIDebugFrame:
    """Une entrée de pile d'appel capturée par le débogueur (ajout de
    cette session). Structure de données PURE, indépendante du moteur
    (tree-walker ou VM) -- chaque moteur la construit à sa façon
    (UVIEvaluator._debug_call_stack maintenue manuellement en parallèle
    de son swap de self.memoire ; UVIVirtualMachine la dérive
    directement de self.frame_stack, déjà une vraie pile), mais la
    présente de façon identique à qui inspecte la pause."""
    nom: str
    ligne_appel: int
    parametres: Dict[str, Any] = field(default_factory=dict)


class UVIDebugPause(BaseException):
    """Exception de contrôle interne (jamais une erreur UVI, jamais vue
    par un script utilisateur) levée par UVIDebugger.check_line quand un
    point d'arrêt est atteint.

    Hérite DÉLIBÉRÉMENT de BaseException, PAS Exception -- ce fichier
    contient plus d'une dizaine de blocs `except Exception as e:` à
    travers les deux moteurs (_exec_call, _exec_module_call, etc), et
    modifier chacun individuellement pour exclure UVIDebugPause serait
    fragile (risque d'en oublier un, ou qu'un site futur soit ajouté
    sans cette exclusion). Hériter de BaseException garantit qu'AUCUN
    except Exception existant ou futur ne capture cette pause par
    accident, quel que soit le niveau d'imbrication où elle survient --
    même principe que SystemExit/KeyboardInterrupt en Python natif (déjà
    noté ailleurs dans ce projet : KeyboardInterrupt n'hérite pas
    d'Exception, précédent direct et pertinent ici).

    Capturée au niveau du point d'entrée top-level de l'exécution
    (run_sequence côté tree-walker, run() côté VM), qui construit un
    UVIDebugPauseResult à partir de l'état présent au moment de la
    levée.

    LIMITE ASSUMÉE ET DOCUMENTÉE (pas une continuation transparente) :
    l'état est capturé au moment de l'arrêt (pile d'appels, variables
    visibles), mais la PILE D'APPEL PYTHON elle-même (les frames Python
    natives de _exec_call empilées par récursion) est déjà dépilée une
    fois l'exception remontée jusqu'à run_sequence -- une "reprise" ne
    peut donc pas continuer exactement où l'exécution s'était arrêtée à
    l'intérieur d'un appel imbriqué profond ; elle relance le script
    depuis son point d'entrée, avec ce point d'arrêt précis désactivé
    pour ce passage (voir UVIDebugger.resume_after_pause). Un vrai
    pas-à-pas avec continuation transparente demanderait de réécrire le
    tree-walker en coroutines/générateurs, un changement d'architecture
    bien plus large que ce chantier, hors de portée ici -- ce compromis
    reste honnête et utile : il donne une vraie visibilité sur l'état à
    l'arrêt, sans prétendre à une continuation qu'il ne peut pas
    offrir."""
    def __init__(self, pause_result: "UVIDebugPauseResult"):
        self.pause_result = pause_result
        super().__init__(f"Pause de débogage à la ligne {pause_result.line}")


@dataclass
class UVIDebugPauseResult:
    """Résultat d'une pause -- ce que UVIDebugger.run_until_pause /
    l'appelant du débogueur reçoit pour inspection. line = ligne UVI où
    l'arrêt a eu lieu ; variables = copie de self.memoire (tree-walker)
    ou frame.locals (VM) au moment de l'arrêt -- une COPIE, jamais une
    référence, pour que l'appelant puisse inspecter sans risque de
    modifier l'état réel de l'évaluateur par erreur ; call_stack = liste
    de UVIDebugFrame, la plus récente en dernier (cohérent avec l'ordre
    naturel d'empilement)."""
    line: int
    variables: Dict[str, Any]
    call_stack: List[UVIDebugFrame] = field(default_factory=list)
    breakpoint_condition: Optional[str] = None


class UVIDebugger:
    """Points d'arrêt conditionnels + pile d'appels navigable (ajout de
    cette session). UNE instance par moteur (tree-walker OU VM, jamais
    partagée entre les deux -- ils ont des mécanismes d'exécution trop
    différents pour un état de débogage commun), attachée via
    self.debugger sur UVIEvaluator ou UVIVirtualMachine. None par défaut
    -- un script normal, sans débogueur attaché, n'a AUCUN changement de
    comportement ni de coût (check_line n'est même pas appelée, voir le
    site d'appel dans run_sequence / run())."""

    def __init__(self):
        # ligne -> condition (expression UVI en texte, None = arrêt
        # inconditionnel dès que la ligne est atteinte).
        self.breakpoints: Dict[int, Optional[str]] = {}
        # ligne -> nombre de passages restants à ignorer -- mécanisme de
        # reprise après une pause (voir UVIDebugPause, limite documentée
        # là-bas). Un COMPTEUR (pas juste un flag booléen) : une même
        # ligne source peut être traversée plusieurs fois avant d'
        # atteindre le passage qui intéresse réellement l'appelant (ex.
        # le chargement initial d'un fichier MODULE traverse sa ligne 1
        # une ou plusieurs fois AVANT qu'un CALL alias::nom(...)
        # ultérieur n'exécute cette même ligne pour de bon -- un simple
        # flag "ignorer une fois" ne suffit pas dans ce cas, découvert
        # empiriquement en testant un point d'arrêt posé sur la ligne de
        # définition d'une fonction de module).
        self._skip_counts: Dict[int, int] = {}
        self.enabled: bool = True

    def set_breakpoint(self, line: int, condition: Optional[str] = None):
        self.breakpoints[line] = condition

    def remove_breakpoint(self, line: int):
        self.breakpoints.pop(line, None)
        self._skip_counts.pop(line, None)

    def clear_all(self):
        self.breakpoints.clear()
        self._skip_counts.clear()

    def resume_after_pause(self, line: int, count: int = 1):
        """À appeler par l'utilisateur du débogueur avant de relancer
        l'exécution après une pause à `line`, pour dépasser ce point
        d'arrêt précis lors des `count` prochains passages sur cette
        ligne (voir limite documentée sur UVIDebugPause -- ceci ne
        reprend PAS l'exécution en cours, ça prépare un NOUVEAU
        lancement à ignorer ce nombre de passages). count=1 par défaut
        (cas le plus courant : une ligne normale, traversée une seule
        fois par passage de script) ; augmenter count pour une ligne
        traversée plusieurs fois avant le passage réellement visé (voir
        docstring de _skip_counts).

        OBSERVATION EMPIRIQUE (VM) : un point d'arrêt posé sur la ligne
        de définition d'une fonction EXPORTÉE d'un module traverse
        généralement cette ligne 2 fois lors du CHARGEMENT initial du
        module (avant même le premier appel réel via CALL alias::nom) --
        mesuré directement sur un module minimal à une seule fonction ;
        peut varier avec la complexité du fichier module. Un appelant
        qui ne connaît pas ce nombre à l'avance peut interroger l'état
        par tâtonnement (resume avec count=1, observer si la pause
        suivante a une call_stack non vide -- si oui c'est le bon
        passage, sinon incrémenter count et relancer depuis le début) ;
        ce n'est pas ergonomique pour un humain au clavier, mais
        parfaitement fiable en usage programmatique (scripts de test,
        automatisation)."""
        self._skip_counts[line] = self._skip_counts.get(line, 0) + count

    def check_line(self, line: int, variables: Dict[str, Any], evaluate_condition_fn, call_stack: List[UVIDebugFrame]):
        """Appelée avant l'exécution de CHAQUE ligne (voir site d'appel
        dans run_sequence côté tree-walker / la boucle run() côté VM).
        evaluate_condition_fn(expr_str) -> valeur legacy-truthy UVI
        (0/""/liste vide = faux, tout le reste = vrai, cohérent avec le
        reste du langage -- voir evaluate()/legacy_truthy déjà en place)
        -- fourni par l'appelant plutôt que codé ici, pour que
        UVIDebugger reste indépendant du moteur (tree-walker évalue via
        self.evaluate, VM via une expression compilée à la volée).

        Lève UVIDebugPause si ce point d'arrêt doit interrompre
        l'exécution MAINTENANT ; ne retourne rien sinon (aucun
        changement de flux, l'appelant continue normalement)."""
        if not self.enabled or line not in self.breakpoints:
            return
        if self._skip_counts.get(line, 0) > 0:
            self._skip_counts[line] -= 1
            return
        condition = self.breakpoints[line]
        if condition:
            try:
                cond_val = evaluate_condition_fn(condition)
            except Exception:
                # Une condition de point d'arrêt mal formée ne doit
                # JAMAIS faire planter le script qu'elle observe --
                # traité comme "condition non remplie" plutôt que
                # remonter une erreur, cohérent avec la philosophie
                # tolérante déjà en place ailleurs dans ce langage
                # (_draw_num retombe sur 0.0 plutôt que lever, etc).
                return
            if not self._legacy_truthy(cond_val):
                return
        raise UVIDebugPause(UVIDebugPauseResult(
            line=line,
            variables=dict(variables),
            call_stack=list(call_stack),
            breakpoint_condition=condition,
        ))

    @staticmethod
    def _legacy_truthy(val) -> bool:
        """Même politique que le reste du langage UVI (0, "", liste
        vide = faux) -- dupliquée ici en miniature plutôt que
        d'importer la logique legacy_truthy de la VM/tree-walker, pour
        que UVIDebugger reste une classe autonome sans dépendance sur
        l'un ou l'autre moteur."""
        if val is None:
            return False
        if isinstance(val, (int, float)):
            return val != 0
        if isinstance(val, str):
            return val != ""
        if isinstance(val, list):
            return len(val) > 0
        return bool(val)


class UVIBytecodeCompilerV2:
    def __init__(self):
        self._label_counter = 0
        self._local_slot_counter = 0  # v1.3 : compteur de slots LOCAL_STORE/
        # LOCAL_LOAD, remis à zéro par fonction compilée (chaque frame a son
        # propre espace de slots -- cf. reset dans compile_function, à venir
        # en Phase 1B pour DefNode). Pour le top-level, jamais remis à zéro
        # au sein de ce compilateur -- correspond à la frame top-level
        # implicite unique (§4).

    def _new_local_slot(self) -> int:
        slot = self._local_slot_counter
        self._local_slot_counter += 1
        return slot

    def _new_label(self, prefix="L"):
        self._label_counter += 1
        return f"__{prefix}_{self._label_counter}"

    # -----------------------------------------------------------------
    # Point d'entrée public
    # -----------------------------------------------------------------

    def compile_symbolic(self, ast_nodes: list) -> List[Instr]:
        """Étape intermédiaire extraite de compile_program() (comportement
        inchangé pour compile_program lui-même, qui délègue maintenant ici
        avant de résoudre) : produit le flux d'instructions AVANT résolution
        des labels (§5), c'est-à-dire avec des sauts qui référencent encore
        des noms de labels symboliques plutôt que des indices entiers.

        RAISON D'ÊTRE : UVIBytecodeOptimizer (plus bas) a besoin de ce flux
        PRÉ-résolution pour pouvoir supprimer ou réordonner des
        instructions sans avoir à recalculer un index absolu à chaque
        modification -- tant qu'aucun label n'est renommé ou supprimé, les
        sauts symboliques restent valides quel que soit le nombre
        d'instructions NON-LABEL retirées autour d'eux. Optimiser après
        résolution serait possible mais bien plus fragile : la moindre
        suppression d'instruction désynchroniserait silencieusement tous
        les index de saut qui la dépassent, sans qu'aucune exception ne le
        signale -- exactement le type de bug déjà corrigé ailleurs dans ce
        fichier (cf. SafeEval.eval_expr)."""
        symbolic = []
        for node in ast_nodes:
            symbolic.extend(self._compile_node(node))
        symbolic.append(Instr(OpcodeV1.HALT))
        return symbolic

    def compile_program(self, ast_nodes: list) -> dict:
        """Retourne le dict conforme à l'en-tête §7 :
        {"bytecode_version": "1.0", "instructions": [...]}
        avec labels déjà résolus en indices absolus (§5)."""
        symbolic = self.compile_symbolic(ast_nodes)
        resolved = self._resolve_labels(symbolic)
        return {
            "bytecode_version": "1.0",
            "instructions": [i.to_dict() for i in resolved],
        }

    def compile_program_json(self, ast_nodes: list) -> str:
        return json.dumps(self.compile_program(ast_nodes), indent=2, ensure_ascii=False)

    # -----------------------------------------------------------------
    # §5 — Résolution finale des labels en indices absolus.
    # La VM v1 n'accepte QUE des JUMP_* à opérande entier (contrat spec).
    # LABEL reste dans le flux (no-op) pour lisibilité en désassemblage.
    # -----------------------------------------------------------------

    def _resolve_labels(self, instrs: List[Instr]) -> List[Instr]:
        label_to_idx = {}
        for idx, instr in enumerate(instrs):
            if instr.opcode == OpcodeV1.LABEL:
                label_to_idx[instr.operands[0]] = idx

        resolved = []
        for instr in instrs:
            if instr.opcode in (OpcodeV1.JUMP_ABS, OpcodeV1.JUMP_IF_FALSE, OpcodeV1.JUMP_IF_TRUE):
                label = instr.operands[0]
                if label not in label_to_idx:
                    raise BytecodeError(f"Label non résolu : '{label}'")
                resolved.append(Instr(instr.opcode, [label_to_idx[label]]))
            elif instr.opcode == OpcodeV1.DEF_REG:
                # BUG trouvé au premier test bout-en-bout de DefNode (Phase
                # 1B) : entry_idx (3e opérande) est un label symbolique au
                # même titre qu'un JUMP_*, mais cette branche ne le
                # résolvait pas -- DEF_REG restait avec un nom de label
                # textuel après la passe de résolution, alors que §5 exige
                # que la VM v1 n'accepte QUE des opérandes entiers pour
                # toute référence de saut, y compris le point d'entrée
                # d'une fonction enregistrée.
                nom, params, entry_label, meta = instr.operands
                if entry_label not in label_to_idx:
                    raise BytecodeError(f"Label d'entrée non résolu pour DEF_REG '{nom}' : '{entry_label}'")
                resolved.append(Instr(instr.opcode, [nom, params, label_to_idx[entry_label], meta]))
            elif instr.opcode == OpcodeV1.ROUTINE_REG:
                # Même bug que DEF_REG ci-dessus, retrouvé en écrivant
                # CALL_ROUTINE/RET_ROUTINE/ROUTINE_REG (famille dédiée aux
                # routines *nom, ajoutée pour leur donner un scope global
                # au lieu du scope isolé de CALL_FUNC) : entry_idx (2e
                # opérande) est un label symbolique comme pour DEF_REG,
                # et cette passe ne le résolvait pas avant ce correctif.
                nom, entry_label = instr.operands
                if entry_label not in label_to_idx:
                    raise BytecodeError(f"Label d'entrée non résolu pour ROUTINE_REG '{nom}' : '{entry_label}'")
                resolved.append(Instr(instr.opcode, [nom, label_to_idx[entry_label]]))
            else:
                resolved.append(instr)
        return resolved

    # -----------------------------------------------------------------
    # Compilation d'expressions (chaînes UVI -> instructions empilant une
    # valeur). C'est ici que le désucrage AND/OR/NOT a lieu (§1 noyau,
    # note BOOL_AND/BOOL_OR de la spec), en s'appuyant sur ast comme le
    # fait déjà SafeEval côté tree-walker — mais ici on COMPILE l'ast
    # plutôt que de l'évaluer immédiatement.
    # -----------------------------------------------------------------

    def _replace_bools_for_parsing(self, s: str) -> str:
        # Même logique texte que SafeEval._replace_bools (UVI utilise
        # AND/OR/NOT en majuscules, ast.parse attend and/or/not) — reproduite
        # ici plutôt qu'importée pour garder ce module sans dépendance sur
        # le tree-walker.
        parts = str(s).split('"')
        for i in range(0, len(parts), 2):
            parts[i] = (parts[i]
                        .replace(" AND ", " and ")
                        .replace(" OR ", " or ")
                        .replace(" NOT ", " not "))
            if parts[i].strip().startswith("NOT "):
                parts[i] = parts[i].replace("NOT ", "not ", 1)
        return '"'.join(parts)

    def _preprocess_nested_tab_access(self, expr_str: str) -> str:
        """
        STATUT : implémentation provisoirement validée par LECTURE de code
        (UVIEvaluator.evaluate, script source lignes 971-1001), PAS encore
        confirmée par audit de scripts .uvi réels. Voir spec §3, note sur
        la compatibilité TAB.GET/TAB.LEN imbriqués -- à valider contre
        CHAMBRE 217 / La Maison des Ombres avant de considérer cette prise
        en charge comme un contrat figé du langage.

        Découverte : self.evaluate() traite TAB.GET/TAB.LEN comme des
        préfixes de chaîne spéciaux AVANT tout appel à SafeEval/ast.parse
        -- ce ne sont pas des expressions Python valides. Un opérande
        StrNode/DiskNode/IfNode/etc. peut donc légitimement contenir
        "TAB.GET liste -> i" comme sous-expression, ce que ast.parse
        rejetterait immédiatement (SyntaxError).

        Ce prétraitement substitue TAB.GET/TAB.LEN par un appel de fonction
        fictif AVANT ast.parse, pour que _compile_expr_node puisse le
        reconnaître et le compiler vers les primitives INDEX/LEN déjà
        décomposées (spec §2) -- pas une nouvelle sémantique, juste une
        étape de normalisation syntaxique en amont du parsing Python.

        Limite connue : ne gère que TAB.GET/TAB.LEN en position simple
        (un seul niveau d'imbrication, correspondant exactement aux 2 cas
        gérés par evaluate() -- confirmé qu'il n'y en a pas d'autres par
        grep exhaustif sur 'startswith' dans cette méthode). Si l'audit
        futur révèle d'autres motifs spéciaux dans evaluate(), ce
        prétraitement devra être étendu -- réversible par construction,
        n'affecte pas le modèle de VM lui-même.
        """
        import re
        # TAB.GET nom -> idx_expr  ->  __tab_get__(nom, idx_expr)
        # idx_expr peut lui-même être une expression -- on capture tout
        # jusqu'à la fin de chaîne ou un délimiteur de niveau supérieur
        # (ce prétraitement reste volontairement simple : un seul niveau,
        # pas de TAB.GET imbriqué dans un autre TAB.GET, cas non rencontré
        # dans le tree-walker lui-même donc hors périmètre ici).
        pattern_get = re.compile(r'TAB\.GET\s+([A-Za-z_][A-Za-z0-9_]*)\s*->\s*(.+)')
        m = pattern_get.search(expr_str)
        if m:
            nom, idx_expr = m.group(1), m.group(2).strip()
            replacement = f"__tab_get__({nom}, {idx_expr})"
            expr_str = pattern_get.sub(replacement, expr_str, count=1)

        pattern_len = re.compile(r'TAB\.LEN\s+([A-Za-z_][A-Za-z0-9_]*)')
        m2 = pattern_len.search(expr_str)
        if m2:
            nom = m2.group(1)
            replacement = f"__tab_len__({nom})"
            expr_str = pattern_len.sub(replacement, expr_str, count=1)

        # alias::nom -> __module_get__("alias", "nom") : même
        # justification que TAB.GET/TAB.LEN ci-dessus -- '::' n'est pas
        # une syntaxe Python valide (vérifié empiriquement : SyntaxError
        # immédiate), donc pyast.parse rejetterait toute expression le
        # contenant sans ce prétraitement. alias et nom sont passés
        # comme littéraux de CHAÎNE (pas des Name Python), puisque ce
        # sont des identifiants UVI à résoudre dans self.modules au
        # runtime, jamais des variables Python existantes. Un seul '::'
        # attendu par expression -- cohérent avec la même contrainte
        # côté tree-walker (UVIEvaluator.evaluate).
        pattern_module = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)\b')
        m3 = pattern_module.search(expr_str)
        if m3:
            alias, nom = m3.group(1), m3.group(2)
            replacement = f'__module_get__("{alias}", "{nom}")'
            expr_str = pattern_module.sub(replacement, expr_str, count=1)

        return expr_str

    # Même regex, même justification que SafeEval._NUM_PREFIX_RE (tree-
    # walker) -- dupliquée plutôt que partagée, cohérent avec la
    # convention déjà en place dans cette classe pour
    # _replace_bools_for_parsing (qui duplique délibérément
    # SafeEval._replace_bools "pour garder ce module sans dépendance sur
    # le tree-walker").
    _NUM_PREFIX_RE = re.compile(r'\b([ER])(-?\d+(?:\.\d+)?)\b')

    def compile_expr(self, expr_str: str) -> List[Instr]:
        """Compile une expression UVI (déjà reconstruite en chaîne par le
        parser) en instructions qui, exécutées, laissent le résultat au
        sommet de la pile. Gère le désucrage booléen avec court-circuit —
        c'est la seule voie de génération de JUMP_IF_FALSE pour AND/OR,
        le noyau BINOP/CMP ne connaît pas la logique booléenne paresseuse."""
        expr_str = self._preprocess_nested_tab_access(expr_str)
        prepared = self._replace_bools_for_parsing(expr_str)
        prepared = self._NUM_PREFIX_RE.sub(r'\2', prepared)
        try:
            tree = pyast.parse(prepared, mode="eval")
        except Exception as e:
            raise BytecodeError(f"Expression non compilable : '{expr_str}' ({e})")
        return self._compile_expr_node(tree.body)

    # Même regex, même justification que UVIEvaluator._INTERP_RE (tree-
    # walker) -- dupliquée par la même convention que _NUM_PREFIX_RE
    # ci-dessus (garder ce module sans dépendance sur le tree-walker).
    _INTERP_RE = re.compile(r'\{([^{}]+)\}')

    def _compile_interpolated_string(self, template: str) -> List[Instr]:
        """Découpe le template en segments texte/{expression} (même
        logique que UVIEvaluator._interpolate_string côté tree-walker,
        adaptée à la compilation plutôt qu'à l'exécution directe :
        chaque segment expression doit devenir du bytecode, pas une
        valeur immédiate, puisqu'elle dépend de variables résolues à
        l'exécution). Empile le premier segment, puis BINOP ADD pour
        chaque segment suivant -- une simple concaténation de chaînes
        en cascade, dans l'ordre du template."""
        segments: List[tuple] = []
        pos = 0
        for m in self._INTERP_RE.finditer(template):
            if m.start() > pos:
                segments.append(("texte", template[pos:m.start()]))
            segments.append(("expr", m.group(1)))
            pos = m.end()
        if pos < len(template):
            segments.append(("texte", template[pos:]))
        if not segments:
            # Ne devrait pas arriver (appelée seulement si _INTERP_RE a
            # matché au moins une fois), filet de sécurité tout de même.
            return [Instr(OpcodeV1.PUSH_CONST, [template])]

        def _compile_segment(seg):
            kind, valeur = seg
            if kind == "texte":
                return [Instr(OpcodeV1.PUSH_CONST, [valeur])]
            # "expr" : compilation récursive -- hérite automatiquement de
            # tout compile_expr (retrait de préfixe E/R, TAB.GET imbriqué,
            # etc), puis TO_STR car BINOP ADD sur str+non-str lève
            # TypeError (déjà vérifié pour le bug d'origine de cette
            # session).
            instrs = self.compile_expr(valeur)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["TO_STR", 1]))
            return instrs

        instrs = _compile_segment(segments[0])
        for seg in segments[1:]:
            instrs += _compile_segment(seg)
            instrs.append(Instr(OpcodeV1.BINOP, ["ADD"]))
        return instrs

    def _compile_expr_node(self, node) -> List[Instr]:
        if isinstance(node, pyast.Constant):
            if isinstance(node.value, str) and self._INTERP_RE.search(node.value):
                return self._compile_interpolated_string(node.value)
            return [Instr(OpcodeV1.PUSH_CONST, [node.value])]

        if isinstance(node, pyast.Name):
            return [Instr(OpcodeV1.LOAD, [node.id])]

        if isinstance(node, pyast.Call):
            # Reconnaissance des appels fictifs __tab_get__/__tab_len__
            # introduits par _preprocess_nested_tab_access -- traduits vers
            # les VRAIES primitives INDEX/LEN déjà décomposées (spec §2),
            # jamais vers un CALL_FUNC ou CALL_BUILTIN. Ce n'est PAS un
            # appel de fonction UVI réel, juste un artefact syntaxique de
            # cette étape de compilation.
            func_name = getattr(node.func, "id", None)
            if func_name == "__tab_get__":
                nom_node, idx_node = node.args
                instrs = [Instr(OpcodeV1.LOAD, [nom_node.id])]
                instrs += self._compile_expr_node(idx_node)
                instrs.append(Instr(OpcodeV1.INDEX))
                return instrs
            if func_name == "__tab_len__":
                nom_node = node.args[0]
                return [Instr(OpcodeV1.LOAD, [nom_node.id]), Instr(OpcodeV1.LEN)]
            if func_name == "__module_get__":
                # __module_get__("alias", "nom") : les deux arguments
                # sont des pyast.Constant (chaînes littérales), pas des
                # Name -- voir _preprocess_nested_tab_access pour la
                # justification. Compilé vers MODULE_GET, résolu au
                # runtime dans self.modules (voir boucle d'exécution).
                alias_node, nom_node = node.args
                return [Instr(OpcodeV1.MODULE_GET, [alias_node.value, nom_node.value])]
            raise BytecodeError(f"Appel de fonction non supporté dans une expression : {func_name}")

        if isinstance(node, pyast.BinOp):
            op = BINOP_MAP.get(type(node.op))
            if op is None:
                raise BytecodeError(f"Opérateur binaire non supporté : {type(node.op).__name__}")
            instrs = self._compile_expr_node(node.left)
            instrs += self._compile_expr_node(node.right)
            instrs.append(Instr(OpcodeV1.BINOP, [op]))
            return instrs

        if isinstance(node, pyast.Compare):
            # UVI/SafeEval limite aux comparaisons chaînées simples ; on
            # compile de la même manière (chaque paire évaluée en AND
            # implicite), mais en préservant le court-circuit : si un
            # maillon est faux, on ne calcule pas les suivants.
            if len(node.ops) == 1:
                instrs = self._compile_expr_node(node.left)
                instrs += self._compile_expr_node(node.comparators[0])
                cmp_op = CMP_MAP.get(type(node.ops[0]))
                if cmp_op is None:
                    raise BytecodeError(f"Comparateur non supporté : {type(node.ops[0]).__name__}")
                instrs.append(Instr(OpcodeV1.CMP, [cmp_op]))
                return instrs
            # Comparaison chaînée a < b < c : désucrée en (a<b) AND (b<c).
            # Réutilise le désucrage AND ci-dessous pour garder un seul
            # point de vérité sur le court-circuit.
            and_node = self._chained_compare_to_boolop(node)
            return self._compile_expr_node(and_node)

        if isinstance(node, pyast.BoolOp):
            return self._compile_boolop(node)

        if isinstance(node, pyast.UnaryOp):
            if isinstance(node.op, pyast.Not):
                instrs = self._compile_expr_node(node.operand)
                instrs.append(Instr(OpcodeV1.UNARY_NOT))
                return instrs
            if isinstance(node.op, pyast.USub):
                instrs = self._compile_expr_node(node.operand)
                instrs.append(Instr(OpcodeV1.UNARY_NEG))
                return instrs
            raise BytecodeError(f"Opérateur unaire non supporté : {type(node.op).__name__}")

        raise BytecodeError(f"Nœud d'expression non supporté : {pyast.dump(node)}")

    # -----------------------------------------------------------------
    # Phase 1A — compilation des statements. Chaque branche cite la ligne
    # exacte du tree-walker (Uvi_08-1-1.py) vérifiée avant écriture — pas
    # de supposition de "comment ferait un langage normal".
    # -----------------------------------------------------------------

    def _compile_node(self, node) -> List[Instr]:
        """Enveloppe fine autour de _compile_node_inner (ajout de cette
        session, débogueur) : délègue tout le VRAI travail de
        compilation à _compile_node_inner (renommage pur, aucune des
        dizaines de branches internes n'a été modifiée), puis tague
        chaque Instr produite avec node.line -- MAIS seulement celles
        dont .line vaut encore 0, jamais celles déjà taguées par un
        appel récursif interne (ex. les enfants d'un IfNode/WhileNode
        ont chacun leur propre .line, plus précis que celui du nœud
        composé parent -- écraser cette précision serait une
        régression, pas une amélioration).

        Chaque type de nœud AST hérite de ASTNode.line (un seul champ,
        contrainte syntaxique du langage : IF/WHILE/FOR/DEF tiennent
        sur UNE ligne physique, donc leur .line est déjà la bonne unité
        de granularité pour un point d'arrêt -- pas besoin de plus fin
        que "la ligne")."""
        instrs = self._compile_node_inner(node)
        node_line = getattr(node, "line", 0)
        if node_line:
            for instr in instrs:
                if not instr.line:
                    instr.line = node_line
        return instrs

    def _compile_node_inner(self, node) -> List[Instr]:
        cls = type(node).__name__

        if cls == "AssignNode":
            # exec_node ligne 1074-1080 : deux chemins distincts selon
            # is_call. Le is_call=True (x = f(y)) doit laisser exactement
            # UNE valeur sur la pile avant STORE — donc appel DIRECT à
            # _compile_call_node (pas de POP), PAS _compile_node(call_node)
            # qui router routerait vers le chemin CallNode-statement
            # (POP inclus, cf. plus bas) -- BUG réel trouvé et corrigé ici :
            # la version précédente appelait _compile_node(node.call_node),
            # ce qui aurait ajouté un POP erroné et fait planter le STORE
            # suivant sur une pile vide. Détecté par le premier test bout-
            # en-bout de AssignNode(is_call=True), jamais exercé avant
            # l'écriture des tests DefNode.
            if getattr(node, "is_call", False) and getattr(node, "call_node", None):
                instrs = self._compile_call_node(node.call_node)
            else:
                instrs = self.compile_expr(node.expr)
            instrs.append(Instr(OpcodeV1.STORE, [node.var]))
            return instrs

        if cls == "PrintNode":
            # exec_node ligne 1082-1084 : self.ide.log(str(evaluate(expr)))
            # — pas de logique supplémentaire, juste évaluer puis afficher.
            instrs = self.compile_expr(node.expr)
            instrs.append(Instr(OpcodeV1.PRINT))
            return instrs

        if cls == "CallNode":
            # CallNode en position STATEMENT (pas dans un AssignNode) :
            # exec_node ligne 1056-1057 route vers _exec_call mais JETTE
            # la valeur de retour (return "" à la ligne 1039 du top-level,
            # rien ne capture _exec_call(node)). CALL_FUNC pousse toujours
            # son retour (§1.b) -> POP obligatoire ici pour ne pas faire
            # fuir la pile à chaque appel-statement non capturé. C'est LA
            # différence avec le is_call=True de AssignNode ci-dessus.
            instrs = self._compile_call_node(node)
            # « CALL f(x) -> dest » : STORE au lieu de POP. Les deux
            # consomment la valeur poussée par CALL_FUNC, donc la pile
            # reste équilibrée dans les deux cas -- c'est la condition à
            # ne pas rater ici, un POP oublié ferait fuir la pile à chaque
            # appel. Symétrique du _safe_store ajouté dans
            # UVIEvaluator._exec_call : les deux moteurs doivent rendre le
            # même résultat observable (cf. tests d'équivalence).
            destination = getattr(node, "dest", None)
            if destination:
                instrs.append(Instr(OpcodeV1.STORE, [destination]))
            else:
                instrs.append(Instr(OpcodeV1.POP))
            return instrs

        if cls == "ModuleNode":
            # MODULE fichier -> alias : effet de bord pur (lit un
            # fichier, compile/exécute une VM enfant, enregistre dans
            # self.modules), aucune valeur ne reste sur la pile --
            # cohérent avec DRAW.CLEAR/DISK_SAVE (CALL_BUILTIN qui
            # renvoie _NO_PUSH), mais ici un opcode dédié plutôt que
            # CALL_BUILTIN (voir justification complète sur OpcodeV1.
            # MODULE_LOAD). node.filename est une EXPRESSION (peut être
            # une variable contenant le nom de fichier), compilée
            # normalement ; node.alias est un IDENTIFIANT littéral (ou
            # vide), jamais évalué -- même convention que ops[0] de
            # DRAW.SPRITE/DRAW.POLY.
            instrs = self.compile_expr(node.filename)
            instrs.append(Instr(OpcodeV1.MODULE_LOAD, [node.alias]))
            return instrs

        if cls == "ExportNode":
            # EXPORT nom : nom est un identifiant littéral (jamais
            # évalué), aucune valeur sur la pile -- effet de bord pur
            # sur le module en cours de construction (résolu au runtime
            # par la VM, symétrique de _module_en_construction côté
            # tree-walker).
            return [Instr(OpcodeV1.EXPORT_NAME, [node.name])]

        if cls == "IfNode":
            # exec_node ligne 1086-1098. Pas de elif natif dans le langage
            # (vérifié : IfNode n'a que then_block/else_block ; un "elif"
            # UVI est un IfNode imbriqué dans else_block, produit par le
            # parser lui-même, ligne 588-597 -- aucun traitement spécial
            # requis ici, la récursion sur else_block le gère naturellement).
            # cond_val in (True, "True", 1) -> JUMP_IF_FALSE, avec la
            # sémantique du VMCompatibilityProfile (§7), pas Python natif.
            label_else = self._new_label("if_else")
            label_end = self._new_label("if_end")
            instrs = self.compile_expr(node.condition)
            instrs.append(Instr(OpcodeV1.JUMP_IF_FALSE, [label_else]))
            for n in node.then_block:
                instrs += self._compile_node(n)
            instrs.append(Instr(OpcodeV1.JUMP_ABS, [label_end]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_else]))
            for n in node.else_block:
                instrs += self._compile_node(n)
            instrs.append(Instr(OpcodeV1.LABEL, [label_end]))
            return instrs

        if cls == "WhileNode":
            # exec_node ligne 1100-1115. ASYMÉTRIE VÉRIFIÉE avec ForNode :
            # dépassement de MAX_LOOP_ITERATIONS = 10000 -> TRAP explicite
            # (tours > MAX, donc le 10001e tour déclenche). Compteur en
            # LOCAL_STORE/LOCAL_LOAD (v1.3) -- pas de variable memoire
            # nommée, aucun risque de collision avec l'espace utilisateur.
            # Note : self.ide.root.update() tous les 20 tours (ligne 1113-
            # 1114) est un détail d'intégration Tkinter, PAS une primitive
            # de langage -- volontairement omis du bytecode (question
            # ouverte séparée, non bloquante pour la sémantique du langage).
            label_start = self._new_label("while_start")
            label_end = self._new_label("while_end")
            counter_slot = self._new_local_slot()

            instrs = [Instr(OpcodeV1.PUSH_CONST, [0]),
                      Instr(OpcodeV1.LOCAL_STORE, [counter_slot])]
            instrs.append(Instr(OpcodeV1.LABEL, [label_start]))
            instrs += self.compile_expr(node.condition)
            instrs.append(Instr(OpcodeV1.JUMP_IF_FALSE, [label_end]))
            # Incrément + garde AVANT le corps, comme le tree-walker
            # (tours += 1 puis test, ligne 1105-1108, avant d'exécuter body).
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [counter_slot]))
            instrs.append(Instr(OpcodeV1.PUSH_CONST, [1]))
            instrs.append(Instr(OpcodeV1.BINOP, ["ADD"]))
            instrs.append(Instr(OpcodeV1.LOCAL_STORE, [counter_slot]))
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [counter_slot]))
            instrs.append(Instr(OpcodeV1.PUSH_CONST, [10000]))  # MAX_LOOP_ITERATIONS
            instrs.append(Instr(OpcodeV1.CMP, ["GT"]))
            label_no_trap = self._new_label("while_no_trap")
            instrs.append(Instr(OpcodeV1.JUMP_IF_FALSE, [label_no_trap]))
            instrs.append(Instr(OpcodeV1.TRAP, ["INFINITE_LOOP"]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_no_trap]))
            for n in node.body:
                instrs += self._compile_node(n)
            instrs.append(Instr(OpcodeV1.JUMP_ABS, [label_start]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_end]))
            return instrs

        if cls == "ForNode":
            # exec_node ligne 1117-1133. ASYMÉTRIE VÉRIFIÉE avec WhileNode :
            # count = min(count, MAX_LOOP_ITERATIONS) -- clamp SILENCIEUX,
            # aucun TRAP émis, contrairement à WhileNode. Reproduit tel
            # quel : pas d'erreur INFINITE_LOOP ici, juste un clamp muet.
            # count_expr peut être dynamique -> clamp à L'EXECUTION.
            # Bornes et compteur en LOCAL_STORE/LOCAL_LOAD (v1.3).
            label_start = self._new_label("for_start")
            label_end = self._new_label("for_end")
            i_slot = self._new_local_slot()
            count_slot = self._new_local_slot()

            instrs = self.compile_expr(node.count_expr)
            instrs.append(Instr(OpcodeV1.LOCAL_STORE, [count_slot]))
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [count_slot]))
            instrs.append(Instr(OpcodeV1.PUSH_CONST, [10000]))  # MAX_LOOP_ITERATIONS
            instrs.append(Instr(OpcodeV1.CMP, ["GT"]))
            label_no_clamp = self._new_label("for_no_clamp")
            instrs.append(Instr(OpcodeV1.JUMP_IF_FALSE, [label_no_clamp]))
            instrs.append(Instr(OpcodeV1.PUSH_CONST, [10000]))
            instrs.append(Instr(OpcodeV1.LOCAL_STORE, [count_slot]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_no_clamp]))

            instrs.append(Instr(OpcodeV1.PUSH_CONST, [0]))
            instrs.append(Instr(OpcodeV1.LOCAL_STORE, [i_slot]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_start]))
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [i_slot]))
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [count_slot]))
            instrs.append(Instr(OpcodeV1.CMP, ["LT"]))
            instrs.append(Instr(OpcodeV1.JUMP_IF_FALSE, [label_end]))
            for n in node.body:
                instrs += self._compile_node(n)
            instrs.append(Instr(OpcodeV1.LOCAL_LOAD, [i_slot]))
            instrs.append(Instr(OpcodeV1.PUSH_CONST, [1]))
            instrs.append(Instr(OpcodeV1.BINOP, ["ADD"]))
            instrs.append(Instr(OpcodeV1.LOCAL_STORE, [i_slot]))
            instrs.append(Instr(OpcodeV1.JUMP_ABS, [label_start]))
            instrs.append(Instr(OpcodeV1.LABEL, [label_end]))
            return instrs

        if cls == "DefNode":
            # BUG trouvé pendant l'intégration VM (absent du fichier fourni) :
            # _compile_def_node existait déjà mais n'était jamais appelé
            # depuis _compile_node -- toute déclaration DEF faisait donc
            # échouer compile_program avec "nœud non encore supporté",
            # jamais exercé auparavant faute de test bout-en-bout incluant
            # une fonction. exec_node (ligne 1054-1057) traite DefNode comme
            # un enregistrement pur (aucun effet de bord au moment de la
            # déclaration) -- cohérent avec DEF_REG qui est lui aussi un
            # simple enregistrement exécuté séquentiellement au chargement.
            return self._compile_def_node(node)

        if cls == "StrNode":
            return self._compile_str_node(node)

        if cls == "DiskNode":
            return self._compile_disk_node(node)

        if cls == "ShionNode":
            return self._compile_shion_node(node)

        if cls == "TabNode":
            return self._compile_tab_node(node)

        if cls == "DrawNode":
            return self._compile_draw_node(node)

        if cls == "PackageNode":
            # PACKAGE.PUBLISH nom -> fichier -> version : 3 opérandes
            # TOUJOURS obligatoires (contrairement à DRAW.* qui a des
            # arités variables) -- compile_val direct plutôt qu'une
            # méthode _compile_package_node séparée, la logique est
            # trop simple pour justifier l'indirection.
            if node.cmd == "PACKAGE.PUBLISH" and len(node.operands) >= 3:
                instrs = self.compile_expr(node.operands[0]) + self.compile_expr(node.operands[1]) + self.compile_expr(node.operands[2])
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["PACKAGE_PUBLISH", 3]))
                return instrs
            raise BytecodeError(f"Commande PACKAGE inconnue ou arité insuffisante : {node.cmd}")

        if cls == "KeyNode":
            return self._compile_key_node(node)

        if cls == "SignatureNode":
            # exec_node : aucun traitement (la signature @UVI est consommée
            # avant l'appel au compilateur/évaluateur, ligne 2588-2592).
            return []

        if cls == "StopNode":
            # @STOP : un unique HALT suffit, SANS opcode dédié. La VM
            # (voir la boucle principale, `elif op == "HALT": break`)
            # exécute un flux d'instructions PLAT et partagé entre le
            # top-level et tous les corps de fonction (CALL_FUNC/RET n'y
            # sautent QUE des adresses de retour, ils ne lancent jamais un
            # run() imbriqué séparé) -- un HALT rencontré n'importe où
            # dans ce flux, même au fond d'un appel de fonction récursif,
            # sort donc immédiatement de TOUTE la boucle `while pc <
            # len(instrs)`, pas juste de la frame courante. C'est
            # exactement la sémantique demandée pour @STOP (arrêt total,
            # pas un simple retour de fonction), obtenue sans toucher à la
            # VM elle-même. UVIBytecodeOptimizer traite déjà HALT comme
            # un terminateur inconditionnel (UNCONDITIONAL_TERMINATORS) --
            # le code mort après un @STOP intra-bloc est donc élagué
            # correctement si l'optimiseur est utilisé, sans changement
            # nécessaire là non plus.
            #
            # LIMITE ASSUMÉE (cohérente avec le tree-walker, voir
            # _exec_module_call) : un @STOP à l'intérieur d'un fichier
            # chargé via MODULE arrête seulement la VM ENFANT de ce
            # module (son propre run()), pas le script appelant -- le
            # système de modules exécute le fichier importé dans une VM
            # séparée, hors du flux plat décrit ci-dessus.
            return [Instr(OpcodeV1.HALT)]

        if cls == "SleepNode":
            # exec_node ligne 1142-1144 : time.sleep(min(duration, 10.0)).
            # Le clamp à 10s est reproduit CÔTÉ VM (builtin SLEEP), pas ici,
            # pour que la valeur clampée soit visible au désassemblage.
            return [Instr(OpcodeV1.PUSH_CONST, [node.duration]),
                    Instr(OpcodeV1.CALL_BUILTIN, ["SLEEP", 1])]

        if cls == "InputNode":
            # exec_node ligne 1146-1148 : ide.request_input(var) puis
            # _safe_store(var, valeur). CALL_BUILTIN INPUT pousse la valeur
            # saisie, STORE l'assigne -- symétrique à DISK_LOAD.
            # ?E.nom / ?R.nom (type_hint) : insère TO_INT_STRICT/
            # TO_REAL_STRICT entre INPUT et STORE, pour aligner la VM sur
            # le comportement déjà validé côté tree-walker (conversion
            # stricte, échoue explicitement sur une saisie non conforme
            # plutôt que de stocker une chaîne ou de convertir en
            # silence). type_hint vide (cas historique) : séquence
            # inchangée, INPUT -> STORE direct.
            instrs = [Instr(OpcodeV1.PUSH_CONST, [node.var]),
                      Instr(OpcodeV1.CALL_BUILTIN, ["INPUT", 1])]
            if node.type_hint == "E":
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["TO_INT_STRICT", 1]))
            elif node.type_hint == "R":
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["TO_REAL_STRICT", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [node.var]))
            return instrs

        if cls == "RoutineDefNode":
            # AVANT ce correctif : réutilisait DEF_REG/CALL_FUNC/RET, le
            # même mécanisme qu'un DefNode classique -- donc la MÊME
            # isolation de scope (chaque appel crée une nouvelle Frame,
            # STORE écrit toujours dans current_frame.locals, jamais dans
            # le frame parent). Résultat vérifié empiriquement AVANT ce
            # correctif : pour 'joueur_x = 5 | *avancer -> joueur_x =
            # joueur_x + 1 | *avancer | !joueur_x', le tree-walker
            # (UVIEvaluator._exec_routine* -- pas de nouvelle Frame côté
            # tree-walker, exec_node direct sur self.memoire) donnait 7,
            # la VM donnait 5 (routine sans effet) -- divergence SILENCIEUSE
            # entre les deux moteurs, aucune erreur levée dans les deux cas.
            # ROUTINE_REG/CALL_ROUTINE/RET_ROUTINE (famille dédiée,
            # n'existaient pas avant ce correctif) n'empilent JAMAIS de
            # Frame : le corps s'exécute dans la Frame de l'APPELANT, donc
            # LOAD et STORE portent tous les deux sur les mêmes variables
            # que l'appelant -- alignant enfin la VM sur le tree-walker.
            label_entry = self._new_label(f"routine_{node.name}")
            label_skip = self._new_label(f"routine_{node.name}_skip")
            body_instrs: List[Instr] = [Instr(OpcodeV1.LABEL, [label_entry])]
            for stmt in node.body:
                body_instrs += self._compile_node(stmt)
            body_instrs.append(Instr(OpcodeV1.RET_ROUTINE))
            reg = Instr(OpcodeV1.ROUTINE_REG, [node.name, label_entry])
            return ([reg, Instr(OpcodeV1.JUMP_ABS, [label_skip])]
                    + body_instrs
                    + [Instr(OpcodeV1.LABEL, [label_skip])])

        if cls == "RoutineCallNode":
            # CALL_ROUTINE : pas d'argc (une routine n'a jamais de
            # paramètres, cf. RoutineCallNode qui ne porte que 'name'),
            # pas de POP après (contrairement à CALL_FUNC : RET_ROUTINE ne
            # pousse RIEN sur la pile, une routine n'a pas de _retour --
            # aucune syntaxe UVI ne capture le résultat d'un appel de
            # routine, côté tree-walker comme ici).
            return [Instr(OpcodeV1.CALL_ROUTINE, [node.name])]

        raise BytecodeError(f"_compile_node : nœud non encore supporté en Phase 1A : {cls}")

    def _compile_def_like(self, name: str, params: list, body: list) -> List[Instr]:
        """Factorisation de _compile_def_node pour un nom de fonction déjà
        décidé par l'appelant (permet de réutiliser DEF_REG/CALL_FUNC pour
        les routines '*nom', qui n'ont pas de DefNode propre)."""
        saved_slot_counter = self._local_slot_counter
        self._local_slot_counter = 0

        label_entry = self._new_label(f"func_{name}")
        label_skip = self._new_label(f"func_{name}_skip")

        body_instrs: List[Instr] = [Instr(OpcodeV1.LABEL, [label_entry])]
        body_instrs.append(Instr(OpcodeV1.PUSH_CONST, [""]))
        body_instrs.append(Instr(OpcodeV1.STORE, ["_retour"]))
        for stmt in body:
            body_instrs += self._compile_node(stmt)
        body_instrs.append(Instr(OpcodeV1.RET))

        n_locals_this_func = self._local_slot_counter
        self._local_slot_counter = saved_slot_counter

        def_reg = Instr(OpcodeV1.DEF_REG, [
            name, list(params), label_entry,
            {"n_params": len(params), "n_locals": n_locals_this_func},
        ])
        return ([def_reg, Instr(OpcodeV1.JUMP_ABS, [label_skip])]
                + body_instrs
                + [Instr(OpcodeV1.LABEL, [label_skip])])

    def _compile_tab_node(self, node) -> List[Instr]:
        """TabNode -> primitives tableau décomposées (§2). Vérifié contre
        _exec_tab (script source, ligne 1255-1397) AVANT écriture pour
        chaque sous-commande. Les tableaux UVI étant des list Python
        mutées EN PLACE par le tree-walker (self.memoire[nom].append(...)),
        la VM applique la même stratégie : LOAD pousse la RÉFÉRENCE de
        liste, APPEND/INDEX_SET/DEL_INDEX/POP_ARRAY la mutent en place --
        aucun STORE de retour n'est nécessaire après ces opérations,
        exactement comme côté tree-walker."""
        cmd = node.cmd
        ops = node.operands

        def compile_val(i, default=""):
            return self.compile_expr(ops[i]) if i < len(ops) and ops[i].strip() != "" \
                else [Instr(OpcodeV1.PUSH_CONST, [default])]

        if cmd in ("TAB.CREATE", "TAB_EMPTY"):
            nom = ops[0].strip() if ops else ""
            if not nom:
                return []
            return [Instr(OpcodeV1.MAKE_ARRAY, [0]), Instr(OpcodeV1.STORE, [nom])]

        if cmd == "TAB_INIT":
            nom = ops[0].strip() if ops else ""
            vals_str = ops[1] if len(ops) > 1 else ""
            # Découpage simple par virgule de haut niveau (hors guillemets)
            # -- suffisant pour des littéraux TAB_INIT usuels (nombres,
            # chaînes, variables), pas un vrai parseur d'expressions
            # imbriquées. Limite documentée : pas de virgule à l'intérieur
            # d'un appel de fonction dans un élément de TAB_INIT.
            raw_items = self._split_top_level_commas(vals_str)
            instrs: List[Instr] = []
            for item in raw_items:
                item = item.strip()
                if item:
                    instrs += self.compile_expr(item)
            instrs.append(Instr(OpcodeV1.MAKE_ARRAY, [len(raw_items)]))
            instrs.append(Instr(OpcodeV1.STORE, [nom]))
            return instrs

        if cmd == "TAB.PUSH":
            nom = ops[0].strip() if ops else ""
            instrs = [Instr(OpcodeV1.LOAD, [nom])]
            instrs += compile_val(1)
            instrs.append(Instr(OpcodeV1.APPEND))
            return instrs

        if cmd == "TAB.POP":
            nom = ops[0].strip() if ops else ""
            var_dest = ops[1].strip() if len(ops) >= 2 else None
            instrs = [Instr(OpcodeV1.LOAD, [nom]), Instr(OpcodeV1.POP_ARRAY)]
            if var_dest:
                instrs.append(Instr(OpcodeV1.STORE, [var_dest]))
            else:
                instrs.append(Instr(OpcodeV1.POP))
            return instrs

        if cmd == "TAB.GET":
            nom = ops[0].strip() if ops else ""
            var_dest = ops[2].strip() if len(ops) > 2 else ""
            instrs = [Instr(OpcodeV1.LOAD, [nom])]
            instrs += compile_val(1, default=0)
            instrs.append(Instr(OpcodeV1.INDEX))
            instrs.append(Instr(OpcodeV1.STORE, [var_dest]))
            return instrs

        if cmd == "TAB.SET":
            nom = ops[0].strip() if ops else ""
            instrs = [Instr(OpcodeV1.LOAD, [nom])]
            instrs += compile_val(1, default=0)
            instrs += compile_val(2)
            instrs.append(Instr(OpcodeV1.INDEX_SET))
            return instrs

        if cmd == "TAB.DEL":
            nom = ops[0].strip() if ops else ""
            instrs = [Instr(OpcodeV1.LOAD, [nom])]
            instrs += compile_val(1, default=0)
            instrs.append(Instr(OpcodeV1.DEL_INDEX))
            return instrs

        if cmd == "TAB.LEN":
            nom = ops[0].strip() if ops else ""
            var_dest = ops[1].strip() if len(ops) >= 2 else ""
            return [Instr(OpcodeV1.LOAD, [nom]), Instr(OpcodeV1.LEN),
                    Instr(OpcodeV1.STORE, [var_dest])]

        if cmd == "TAB.PRINT":
            nom = ops[0].strip() if ops else ""
            return [Instr(OpcodeV1.PUSH_CONST, [nom]), Instr(OpcodeV1.LOAD, [nom]),
                    Instr(OpcodeV1.CALL_BUILTIN, ["TAB_PRINT", 2])]

        raise BytecodeError(f"TabNode : commande non supportée : {cmd}")

    @staticmethod
    def _split_top_level_commas(s: str) -> List[str]:
        parts, buf, depth, in_str = [], [], 0, False
        for ch in s:
            if ch == '"':
                in_str = not in_str
                buf.append(ch)
            elif ch in "([" and not in_str:
                depth += 1
                buf.append(ch)
            elif ch in ")]" and not in_str:
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0 and not in_str:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if buf or parts:
            parts.append("".join(buf))
        return [p for p in parts if p.strip() != ""]

    def _compile_str_node(self, node) -> List[Instr]:
        """StrNode -> CALL_BUILTIN STR_*. Vérifié contre _exec_str (script
        source, ligne 1392-1443) AVANT écriture : le tree-walker ne pousse
        PAS un résultat sur une pile générique -- chaque instruction porte
        sa PROPRE cible de stockage en dernier opérande textuel (ops[-1],
        jamais évalué, utilisé tel quel comme nom de variable via
        _safe_store). Désucrage nécessaire : compiler les opérandes-VALEUR
        (tout sauf le dernier) comme expressions réelles via compile_expr
        (qui gère maintenant aussi TAB.GET/TAB.LEN imbriqué -- cf.
        _preprocess_nested_tab_access, Option A actée), CALL_BUILTIN,
        puis STORE explicite vers le nom extrait du dernier opérande.

        STR.JOIN est un cas à part : son PREMIER opérande (pas un opérande
        de valeur générique) est un NOM DE TABLEAU non évalué -- vérifié
        ligne 1413 (tab_nom = ops[0].strip(), jamais ev(0)) -- donc compilé
        comme LOAD direct, pas compile_expr sur une expression arbitraire.

        STR.SLICE : erreur INVALID_SLICE couvre un try/except Python
        GLOBAL (ligne 1435-1440) -- toute exception pendant le cast en
        int, pas seulement une ValueError de cast. Le CALL_BUILTIN
        correspondant devra reproduire cette largeur de capture côté VM
        Phase 3, pas seulement une vérification de type étroite.
        """
        cmd = node.cmd
        ops = node.operands

        def compile_val(i):
            return self.compile_expr(ops[i]) if i < len(ops) else [Instr(OpcodeV1.PUSH_CONST, [""])]

        if cmd == "STR.LEN" and len(ops) >= 2:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_LEN", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "STR.NUM" and len(ops) >= 2:
            # STR.NUM src -> dest : convertit une chaîne (typiquement issue
            # d'un ?INPUT, qui renvoie toujours str -- voir InputNode plus
            # haut, aucune coercition n'existe ailleurs dans le langage) en
            # int ou float Python selon la forme du texte. Nécessaire pour
            # tout calcul arithmétique sur une saisie utilisateur : sans
            # cela, 'res = a + b' sur deux ?INPUT fait une CONCATENATION
            # silencieuse ('7'+'6' -> '76', aucune erreur, aucun TRAP) au
            # lieu d'une addition -- pire que planter, un résultat faux qui
            # a l'air correct. Même famille STR_* que STR_LEN côté VM.
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_NUM", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "STR.UPPER" and len(ops) >= 2:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_UPPER", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "STR.LOWER" and len(ops) >= 2:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_LOWER", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "STR.TRIM" and len(ops) >= 2:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_TRIM", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "STR.SPLIT" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_SPLIT", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs
        if cmd == "STR.JOIN" and len(ops) >= 3:
            # Cas à part : ops[0] est un NOM DE TABLEAU non évalué (vérifié
            # ligne 1413), pas une expression -- LOAD direct.
            instrs = [Instr(OpcodeV1.LOAD, [ops[0].strip()])]
            instrs += compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_JOIN", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs
        if cmd == "STR.REPLACE" and len(ops) >= 4:
            instrs = compile_val(0) + compile_val(1) + compile_val(2)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_REPLACE", 3]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[3].strip()]))
            return instrs
        if cmd == "STR.CONTAINS" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_CONTAINS", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs
        if cmd == "STR.STARTS" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_STARTS", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs
        if cmd == "STR.ENDS" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_ENDS", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs
        if cmd == "STR.SLICE" and len(ops) >= 4:
            instrs = compile_val(0) + compile_val(1) + compile_val(2)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_SLICE", 3]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[3].strip()]))
            return instrs
        if cmd == "STR.INDEX" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["STR_INDEX", 2]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[2].strip()]))
            return instrs

        raise BytecodeError(f"StrNode : commande non supportée ou arité insuffisante : {cmd}")

    def _compile_disk_node(self, node) -> List[Instr]:
        """DiskNode -> CALL_BUILTIN DISK_*. Vérifié contre _exec_disk
        (ligne 1532-1562+) AVANT écriture. Même désucrage opérande-cible-
        incluse que STR. DISK_SAVE/DISK_DEL n'ont pas de STORE en aval
        (pas de valeur de retour utile côté langage -- effets de bord
        purs), contrairement à DISK_LOAD qui stocke le contenu lu."""
        cmd = node.cmd
        ops = node.operands

        def compile_val(i):
            return self.compile_expr(ops[i]) if i < len(ops) else [Instr(OpcodeV1.PUSH_CONST, [""])]

        if cmd == "&save" and len(ops) >= 2:
            instrs = compile_val(0) + compile_val(1)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DISK_SAVE", 2]))
            return instrs  # pas de STORE : DISK_SAVE ne pousse rien (§3)
        if cmd == "&load" and len(ops) >= 2:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DISK_LOAD", 1]))
            instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
            return instrs
        if cmd == "&list":
            return [Instr(OpcodeV1.CALL_BUILTIN, ["DISK_LIST", 0])]
        if cmd == "&delete" and len(ops) >= 1:
            instrs = compile_val(0)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DISK_DEL", 1]))
            return instrs

        raise BytecodeError(f"DiskNode : commande non supportée ou arité insuffisante : {cmd}")

    def _compile_shion_node(self, node) -> List[Instr]:
        """ShionNode -> CALL_BUILTIN SHION_ASK. Exception documentée (spec
        §3) : AUCUN STORE généré après -- nom_var_dest est un ARGUMENT du
        builtin, pas une cible de store en aval (le builtin écrit
        lui-même, en synchrone puis en différé via thread, cf. _exec_shion
        ligne 1445-1505). Erreurs de syntaxe/disponibilité (SHION_SYNTAX,
        SHION_UNAVAILABLE, SHION_EMPTY_PROMPT) découvertes en auditant
        _exec_shion en détail cette session -- absentes du mapping initial
        de session #2, qui n'avait documenté que le happy path. Ces
        erreurs sont désormais portées par le builtin SHION_ASK lui-même
        côté VM Phase 3, pas par un opcode de compilation distinct --
        rien à changer dans _compile_shion_node pour ça, la responsabilité
        de les lever reste dans l'implémentation Python du builtin."""
        cmd = node.cmd
        ops = node.operands
        if cmd != "SHION.ASK" or len(ops) < 2:
            raise BytecodeError(f"ShionNode : syntaxe invalide (attendu SHION.ASK \"prompt\" -> var) : {cmd}")
        instrs = self.compile_expr(ops[0])
        # nom_var_dest passé comme argument littéral (PUSH_CONST), jamais
        # LOAD -- c'est un NOM, pas une valeur à résoudre.
        instrs.append(Instr(OpcodeV1.PUSH_CONST, [ops[1].strip()]))
        instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["SHION_ASK", 2]))
        return instrs  # pas de STORE -- exception documentée

    def _compile_draw_node(self, node) -> List[Instr]:
        """DrawNode -> CALL_BUILTIN DRAW_*. Vérifié contre _exec_draw
        (ci-dessus) AVANT écriture, même méthodologie que pour STR/DISK/
        SHION : chaque sous-commande DRAW.* est un effet de bord pur côté
        VM (dessine sur un canevas géré par l'hôte), donc AUCUN STORE en
        aval -- même famille que DISK_SAVE, pas que TAB.GET/STR.LEN.

        DRAW.SPRITE fait exception au sein même de DrawNode : son PREMIER
        opérande est un NOM DE TABLEAU non évalué (même convention que
        STR.JOIN ops[0], §StrNode ci-dessus) -- compilé en LOAD direct,
        qui pousse la RÉFÉRENCE de liste comme valeur unique sur la pile
        (cf. TAB.PUSH/TAB.SET : LOAD ne décompose jamais la liste en ses
        éléments), pas une expression arbitraire passée à compile_expr."""
        cmd = node.cmd
        ops = node.operands

        def compile_val(i, default=0):
            return self.compile_expr(ops[i]) if i < len(ops) and ops[i].strip() != "" \
                else [Instr(OpcodeV1.PUSH_CONST, [default])]

        if cmd == "DRAW.CLEAR":
            return [Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_CLEAR", 0])]

        if cmd == "DRAW.COLOR" and len(ops) >= 1:
            instrs = compile_val(0, default="")
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_BGCOLOR", 1]))
            return instrs

        if cmd == "DRAW.RECT" and len(ops) >= 4:
            instrs = compile_val(0) + compile_val(1) + compile_val(2) + compile_val(3)
            instrs += compile_val(4, default="#00FF00")
            if len(ops) >= 7:
                # 6e = remplissage, 7e = épaisseur explicite.
                instrs += compile_val(5, default="") + compile_val(6, default=1)
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_RECT", 7]))
            elif len(ops) >= 6:
                # 6e opérande = remplissage explicite.
                instrs += compile_val(5, default="")
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_RECT", 6]))
            else:
                # Arité 5 (comportement historique) -- DRAW_RECT côté VM
                # applique alors le défaut fill="" lui-même.
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_RECT", 5]))
            return instrs

        if cmd == "DRAW.CIRCLE" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1) + compile_val(2)
            instrs += compile_val(3, default="#00FF00")
            if len(ops) >= 6:
                instrs += compile_val(4, default="") + compile_val(5, default=1)
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_CIRCLE", 6]))
            elif len(ops) >= 5:
                instrs += compile_val(4, default="")
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_CIRCLE", 5]))
            else:
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_CIRCLE", 4]))
            return instrs

        if cmd == "DRAW.LINE" and len(ops) >= 4:
            instrs = compile_val(0) + compile_val(1) + compile_val(2) + compile_val(3)
            instrs += compile_val(4, default="#00FF00")
            if len(ops) >= 6:
                instrs += compile_val(5, default=1)
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_LINE", 6]))
            else:
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_LINE", 5]))
            return instrs

        if cmd == "DRAW.TEXT" and len(ops) >= 3:
            instrs = compile_val(0) + compile_val(1)
            instrs += compile_val(2, default="")
            instrs += compile_val(3, default="#00FF00")
            if len(ops) >= 5:
                instrs += compile_val(4, default=10)
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_TEXT", 5]))
            else:
                instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_TEXT", 4]))
            return instrs

        if cmd == "DRAW.SPRITE" and len(ops) >= 3:
            nom_tab = ops[0].strip()
            instrs = [Instr(OpcodeV1.LOAD, [nom_tab])]
            instrs += compile_val(1) + compile_val(2)
            instrs += compile_val(3, default=16)
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_SPRITE", 4]))
            return instrs

        if cmd == "DRAW.POLY" and len(ops) >= 1:
            # Même convention que DRAW.SPRITE : ops[0] est un nom de
            # tableau, compilé en LOAD direct (pousse la référence de
            # liste entière), jamais compile_expr. Arité 1/2/3/4 selon
            # présence couleur/remplissage/épaisseur, défauts appliqués
            # côté VM si absents (même politique que DRAW_RECT/CIRCLE).
            nom_tab = ops[0].strip()
            instrs = [Instr(OpcodeV1.LOAD, [nom_tab])]
            n = 1
            if len(ops) >= 2:
                instrs += compile_val(1, default="#00FF00")
                n = 2
            if len(ops) >= 3:
                instrs += compile_val(2, default="")
                n = 3
            if len(ops) >= 4:
                instrs += compile_val(3, default=1)
                n = 4
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["DRAW_POLY", n]))
            return instrs

        raise BytecodeError(f"DrawNode : commande non supportée ou arité insuffisante : {cmd}")

    def _compile_key_node(self, node) -> List[Instr]:
        """KeyNode -> CALL_BUILTIN KEY_*. Même patron que STR.LEN dans
        _compile_str_node (voir plus haut) : le PREMIER opérande (nom de
        touche) est une expression compilée normalement via compile_expr
        -- peut être une STRING littérale ("Right") ou une variable
        contenant le nom de touche -- et le SECOND (ops[1]) est le nom de
        la variable destination, jamais évalué, compilé directement en
        STORE. AUCUN CALL_BUILTIN 'sans STORE' ici contrairement à
        DrawNode : KEY.* retourne toujours une valeur (1/0), il n'a pas
        d'équivalent DRAW.CLEAR (effet de bord pur sans retour)."""
        cmd = node.cmd
        ops = node.operands
        if len(ops) < 2:
            raise BytecodeError(f"KeyNode : {cmd} nécessite une touche et une variable destination.")

        instrs = self.compile_expr(ops[0])
        if cmd == "KEY.DOWN":
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["KEY_DOWN", 1]))
        elif cmd == "KEY.PRESSED":
            instrs.append(Instr(OpcodeV1.CALL_BUILTIN, ["KEY_PRESSED", 1]))
        else:
            raise BytecodeError(f"KeyNode : commande non supportée : {cmd}")
        instrs.append(Instr(OpcodeV1.STORE, [ops[1].strip()]))
        return instrs

    def _compile_call_node(self, node) -> List[Instr]:
        """CALL_FUNC pour un CallNode -- utilisé par AssignNode(is_call=True)
        ET par CallNode-statement (qui ajoute son propre POP ensuite, cf.
        _compile_node). Args évalués dans le scope APPELANT avant tout push
        de frame, conforme à _exec_call (ligne 1228 précédant 1229).

        BUG trouvé pendant l'intégration VM (absent du fichier fourni) :
        cette méthode était référencée à trois endroits (_compile_node pour
        AssignNode.is_call et pour CallNode-statement) mais n'existait nulle
        part dans le fichier -- seule sa logique traînait, orpheline, dans
        du texte mort après un `return` plus bas dans le fichier. Jamais
        détecté avant faute de test bout-en-bout incluant un appel de
        fonction (DEF/CALL).

        CALL alias::nom(args) (ajout système de modules, cette session) :
        même contrat d'arguments évalués dans le scope appelant, mais
        opcode CALL_MODULE_FUNC dédié plutôt que CALL_FUNC -- la VM doit
        savoir router vers self.modules[alias] plutôt que self.functions,
        une distinction que CALL_FUNC (qui ne connaît que self.functions)
        ne peut pas exprimer sans ambiguïté."""
        instrs = []
        for arg_expr in node.args:
            instrs += self.compile_expr(arg_expr)
        if '::' in node.name:
            alias, _, nom_func = node.name.partition('::')
            instrs.append(Instr(OpcodeV1.CALL_MODULE_FUNC, [alias.strip(), nom_func.strip(), len(node.args)]))
        else:
            instrs.append(Instr(OpcodeV1.CALL_FUNC, [node.name, len(node.args)]))
        return instrs

    def _compile_def_node(self, node) -> List[Instr]:
        """DefNode -> DEF_REG + corps compilé séparément, adressé par
        entry_idx résolu en §5 comme tout autre label.

        Vérifié contre _exec_call (ligne 1206-1246) avant écriture :
        - WRONG_ARG_COUNT est vérifié AVANT l'évaluation des arguments
          (ligne 1225 précède 1228) -> CALL_FUNC doit checker argc avant
          de dépiler quoi que ce soit vers les paramètres.
        - Aucun ReturnNode/mot-clé RETURN n'existe dans le langage (confirmé
          par grep exhaustif) : _retour est une variable ordinaire assignée
          via AssignNode standard. Le corps exécute TOUS ses statements
          sans jamais s'arrêter sur un retour volontaire -> RET implicite
          systématique en fin de corps compilé, jamais de JUMP anticipé
          pour un retour explicite (il n'existe pas).
        - _retour initialisé à "" avant le corps (ligne 1232) -> le
          compilateur émet PUSH_CONST "" ; STORE "_retour" avant de
          compiler le corps, pour que RET ait toujours quelque chose à
          charger même si la fonction ne l'écrit jamais.

        Remise à zéro de _local_slot_counter (surveillance demandée) :
        chaque fonction a sa PROPRE frame avec son PROPRE espace
        local_slots (§4) -- les compteurs de boucle internes à CETTE
        fonction doivent recommencer à 0, sinon les slots d'une fonction
        empièteraient sur la numérotation d'une autre alors qu'ils vivent
        dans des frames disjointes à l'exécution. Sauvegarde/restauration
        du compteur plutôt que simple reset, pour supporter des défs
        imbriquées si le langage venait à les permettre plus tard.
        """
        saved_slot_counter = self._local_slot_counter
        self._local_slot_counter = 0

        label_entry = self._new_label(f"func_{node.name}")
        label_skip = self._new_label(f"func_{node.name}_skip")

        # DEF_REG est un no-op d'enregistrement exécuté au chargement
        # (avant tout CALL_FUNC, §1.b) -- mais le CORPS compilé, lui, doit
        # être physiquement présent dans le flux d'instructions pour que
        # la résolution de labels (§5) lui attribue un index. On l'entoure
        # donc d'un JUMP_ABS qui saute par-dessus au chargement séquentiel
        # normal du programme, pour que le corps ne s'exécute jamais tant
        # qu'un CALL_FUNC n'y saute pas explicitement.
        body_instrs: List[Instr] = [Instr(OpcodeV1.LABEL, [label_entry])]
        body_instrs.append(Instr(OpcodeV1.PUSH_CONST, [""]))
        body_instrs.append(Instr(OpcodeV1.STORE, ["_retour"]))
        for param in node.params:
            # Le binding param -> valeur n'est PAS généré ici en bytecode
            # explicite. Vérifié contre le contrat déjà figé (spec §1.b,
            # ligne CALL_FUNC) : "Pop argc valeurs, push nouvelle frame,
            # saute au corps" -- le binding est une responsabilité de la
            # VM au moment de CALL_FUNC, qui consulte DEF_REG[nom].params
            # (disponible car DEF_REG enregistre explicitement la liste
            # params, pas seulement un entry_idx). Aucune instruction de
            # prologue à émettre dans le corps pour cette raison.
            pass
        for stmt in node.body:
            body_instrs += self._compile_node(stmt)
        body_instrs.append(Instr(OpcodeV1.RET))

        n_locals_this_func = self._local_slot_counter
        self._local_slot_counter = saved_slot_counter  # restauration

        # DEF_REG extensible (surveillance demandée) : métadonnées
        # n_params/n_locals incluses dès maintenant même si la VM Phase 3
        # ne les exploite pas toutes immédiatement -- évite de changer le
        # format de l'opérande dans une future révision juste pour ça.
        def_reg = Instr(OpcodeV1.DEF_REG, [
            node.name,
            list(node.params),
            label_entry,          # résolu en index absolu par §5, comme
                                   # n'importe quel JUMP_*
            {
                "n_params": len(node.params),
                "n_locals": n_locals_this_func,
            },
        ])

        # DEF_REG + jump par-dessus le corps, PUIS le corps lui-même.
        # C'est ce qui permet à compile_program de faire une seule passe
        # linéaire (pas deux passes séparées déclarations/corps) tout en
        # garantissant que le corps ne s'exécute jamais par accident lors
        # du déroulement séquentiel initial du programme.
        return ([def_reg,
                 Instr(OpcodeV1.JUMP_ABS, [label_skip])]
                + body_instrs
                + [Instr(OpcodeV1.LABEL, [label_skip])])

    def _chained_compare_to_boolop(self, node: pyast.Compare) -> pyast.BoolOp:
        """a < b < c (pyast.Compare avec len(ops) > 1, cf. site d'appel
        dans _compile_expr_node) -> (a < b) AND (b < c) : un pyast.BoolOp
        équivalent, pour réutiliser TEL QUEL le désucrage AND déjà écrit
        dans _compile_boolop (court-circuit inclus), plutôt que dupliquer
        cette logique pour ce cas.

        BUG CORRIGÉ (crash, pas juste un écart de comportement) : cette
        méthode était APPELÉE par _compile_expr_node mais n'avait jamais
        été écrite — toute comparaison chaînée (`a < b < c`, `0 <= x <
        10`, etc.) faisait lever un AttributeError brut hors de
        compile_expr/compile_and_run dès qu'un script en contenait une,
        alors que le tree-walker (SafeEval._eval, ast.Compare) la gère
        très bien. Trouvé par la suite de tests tree-walker/VM de ce
        projet (tests/test_compiler.py), qui compare justement les deux
        moteurs sur le même script pour détecter ce genre d'écart."""
        valeurs = []
        gauche = node.left
        for op, comparateur in zip(node.ops, node.comparators):
            valeurs.append(pyast.Compare(left=gauche, ops=[op], comparators=[comparateur]))
            gauche = comparateur
        return pyast.BoolOp(op=pyast.And(), values=valeurs)

    def _compile_boolop(self, node: pyast.BoolOp) -> List[Instr]:
        """
        Désucrage avec court-circuit réel, conforme v1.1 (JUMP_IF_TRUE
        disponible — plus de contournement UNARY_NOT pour le OR).

        ATTENTION — divergence détectée et corrigée en auto-vérification
        contre le tree-walker existant (SafeEval._eval, BoolOp, script
        source lignes 348-358) : contrairement à Python natif (où
        `5 and "x"` vaut `"x"`, pas `True`), UVI **normalise toujours en
        bool strict**. `_eval` retourne explicitement `True`/`False`,
        jamais la valeur brute du dernier opérande. Le désucrage ci-dessous
        reproduit CE comportement (UVI), pas la sémantique Python — sinon
        `score AND nom` avec score=5, nom="Luffy" changerait de résultat
        entre l'ancien tree-walker et la VM (True vs "Luffy"), ce qui
        serait une régression silencieuse inacceptable pour du code UVI
        existant migrant vers la VM.

        AND  a AND b AND c (tous vrais -> True, sinon False dès le 1er faux) :
            compile(a) ; JUMP_IF_FALSE -> L_false
            compile(b) ; JUMP_IF_FALSE -> L_false
            compile(c) ; JUMP_IF_FALSE -> L_false   <- dernier opérande AUSSI testé
            PUSH_CONST True
            JUMP_ABS -> L_end
            LABEL L_false ; PUSH_CONST False
            LABEL L_end

        OR symétrique : premier vrai -> True, sinon (tous faux) -> False.
        """
        is_and = isinstance(node.op, pyast.And)
        short_circuit_op = OpcodeV1.JUMP_IF_FALSE if is_and else OpcodeV1.JUMP_IF_TRUE
        label_short = self._new_label("sc_false" if is_and else "sc_true")
        label_end = self._new_label("sc_end")

        out: List[Instr] = []
        # Différence clé avec la version précédente : TOUS les opérandes,
        # y compris le dernier, passent par le test de court-circuit —
        # il n'y a plus de "dernier opérande laissé tel quel sur la pile".
        for val_node in node.values:
            out += self._compile_expr_node(val_node)
            out.append(Instr(short_circuit_op, [label_short]))
        # Atteint seulement si AUCUN opérande n'a déclenché le court-circuit
        # -> AND: tous vrais -> True. OR: tous faux -> False.
        out.append(Instr(OpcodeV1.PUSH_CONST, [is_and]))
        out.append(Instr(OpcodeV1.JUMP_ABS, [label_end]))
        out.append(Instr(OpcodeV1.LABEL, [label_short]))
        out.append(Instr(OpcodeV1.PUSH_CONST, [not is_and]))
        out.append(Instr(OpcodeV1.LABEL, [label_end]))
        return out


"""
uvi_vm.py — Machine virtuelle UVI, Phase 3.

Consomme le bytecode produit par UVIBytecodeCompilerV2.compile_program()
(compiler_v2.py), conforme à UVI_BYTECODE_SPEC_v1-6.md. Reprend le modèle
de pile de frames validé par ToyVM2 (toy_vm2-4.py) mais :

  - se branche sur les VRAIS services du projet (VFSManager, disque réel,
    SHION.ASK réel) au lieu des simulations en mémoire du jouet ;
  - couvre les opcodes tableau (MAKE_ARRAY/APPEND/POP_ARRAY/INDEX_SET/
    DEL_INDEX) laissés de côté par ToyVM2 ;
  - remonte les erreurs (TRAP) comme des exceptions UVIRuntimeTrap plutôt
    que par un simple attribut à vérifier après coup, pour s'intégrer
    proprement avec le style try/except déjà utilisé ailleurs dans le
    projet (UVIError).

Ce module est autonome : il ne dépend PAS de UVIEvaluator (le
tree-walker). Il attend en entrée un dict bytecode {"bytecode_version":
"1.0", "instructions": [...]}, tel que produit par compiler_v2.py, et des
callbacks optionnels pour les services externes (VFS, SHION, INPUT).

Limites connues (documentées plutôt que masquées) :
  - ImportNode / PipeNode ne sont pas encore compilés par
    UVIBytecodeCompilerV2 -> un script qui les utilise doit encore passer
    par le tree-walker (UVIEvaluator), pas par cette VM.
  - SHION.ASK reste synchrone du point de vue de la VM : le callback
    shion_ask_fn est appelé et DOIT retourner la valeur finale (ou lever
    UVIRuntimeTrap) avant que l'instruction suivante ne s'exécute. Le mode
    non-bloquant (thread + SHION_PENDING) du tree-walker est un détail
    d'intégration IDE/Tkinter, hors périmètre d'un exécuteur bytecode
    linéaire — un appelant qui veut ce comportement doit le construire par
    dessus (cf. uvi_agent_extension.py pour un exemple avec un callback
    qui bloque jusqu'à réponse).
"""



# ---------------------------------------------------------------------------
# Constantes par défaut (alignées sur Uvi_08-1-2.py). Un appelant qui a
# accès aux vraies constantes du moteur principal doit les passer au
# constructeur plutôt que de dépendre de ces valeurs par défaut.
# ---------------------------------------------------------------------------
DEFAULT_MAX_ARRAY_SIZE = 10_000
DEFAULT_MAX_STRING_LENGTH = 1_000_000
DEFAULT_MAX_LOOP_ITERATIONS = 10_000
DEFAULT_MAX_STEPS = 2_000_000


# ---------------------------------------------------------------------------
# Désassembleur — lit la sortie de UVIBytecodeCompilerV2.compile_program()
# (labels déjà résolus en indices absolus, §5) et produit un texte lisible
# façon objdump : un index de ligne, l'opcode, les opérandes formatés selon
# leur nature, et — point important — la cible en clair pour tout saut
# (ex: "JUMP_ABS      -> 12  # HALT"), plutôt que l'entier brut qui, seul,
# ne dit rien du contenu réel visé.
#
# CE QUE CE MODULE NE FAIT PAS : il ne modifie ni ne réinterprète le
# bytecode, il le LIT. Aucune dépendance sur la VM ni sur le compilateur
# au-delà du dict {"bytecode_version", "instructions": [{"opcode",
# "operands"}]} déjà produit par compile_program() -- un désassembleur qui
# aurait besoin d'importer UVIVirtualMachine pour fonctionner serait un
# signe que la responsabilité est mal séparée.
#
# POURQUOI APRÈS résolution des labels et pas avant : contrairement à
# UVIBytecodeOptimizer (plus bas), qui DOIT opérer sur le flux symbolique
# (labels non résolus) pour ne jamais avoir à recalculer un index absolu
# après suppression/réordonnancement d'instructions, le désassembleur n'a
# besoin d'aucune garantie de ce type -- il ne fait qu'afficher, jamais
# réécrire. Lire le bytecode déjà résolu (le format que la VM exécute
# réellement) est donc plus fidèle à ce qui tourne effectivement, plutôt
# que de désassembler une représentation intermédiaire que la VM ne voit
# jamais.
# ---------------------------------------------------------------------------

class UVIDisassembler:
    """Désassemble le dict produit par UVIBytecodeCompilerV2.compile_program()
    en texte lisible. Usage :
        bytecode = UVIBytecodeCompilerV2().compile_program(ast_nodes)
        print(UVIDisassembler().disassemble(bytecode))
    """

    # Opcodes dont le premier opérande est un index de saut résolu (§5) --
    # ce sont ceux, et uniquement ceux, que _resolve_labels réécrit avec un
    # entier. Réutiliser cette liste ailleurs (ex: un futur vérificateur de
    # bytecode) plutôt que de la redupliquer : c'est la même énumération
    # que celle testée dans UVIBytecodeCompilerV2._resolve_labels (§5).
    JUMP_OPCODES = (OpcodeV1.JUMP_ABS, OpcodeV1.JUMP_IF_FALSE, OpcodeV1.JUMP_IF_TRUE)

    def disassemble(self, bytecode: dict) -> str:
        instrs = bytecode.get("instructions", [])
        version = bytecode.get("bytecode_version", "?")
        lines = [f"; UVI bytecode v{version} -- {len(instrs)} instruction(s)"]
        for idx, instr in enumerate(instrs):
            lines.append(self._format_instr(idx, instr, instrs))
        return "\n".join(lines)

    def _format_instr(self, idx: int, instr: dict, all_instrs: list) -> str:
        op = instr.get("opcode", "?")
        ops = instr.get("operands", [])

        # Cas spéciaux d'affichage -- tout le reste tombe dans le format
        # générique plus bas (opérandes séparés par des virgules, sans
        # interprétation). Ordonné pour que les branches les plus
        # fréquentes (BINOP/CMP/CALL_BUILTIN, sauts) soient vérifiées tôt.
        if op in self.JUMP_OPCODES:
            target = ops[0] if ops else "?"
            target_desc = self._describe_target(target, all_instrs)
            return f"{idx:5d}  {op:<15} -> {target}{target_desc}"

        if op == "LABEL":
            # Ne devrait normalement plus apparaître après résolution (une
            # LABEL reste dans le flux comme no-op, cf. commentaire §5 dans
            # le compilateur), mais un bytecode symbolique non résolu
            # pourrait quand même arriver ici si quelqu'un appelle
            # disassemble() directement sur `symbolic` avant
            # _resolve_labels -- traité proprement plutôt que planter sur
            # un accès ops[0] hors limites si operands est vide.
            name = ops[0] if ops else "?"
            return f"{idx:5d}  {op:<15} {name}:"

        if op == "DEF_REG":
            if len(ops) >= 4:
                nom, params, entry_idx, meta = ops[0], ops[1], ops[2], ops[3]
                n_locals = meta.get("n_locals", "?") if isinstance(meta, dict) else "?"
                params_str = ", ".join(params) if params else ""
                entry_desc = self._describe_target(entry_idx, all_instrs)
                return (f"{idx:5d}  {op:<15} {nom}({params_str}) "
                        f"-> entry={entry_idx}{entry_desc}  [n_locals={n_locals}]")
            return f"{idx:5d}  {op:<15} {ops!r}  ; DEF_REG malformé (attendu 4 opérandes)"

        if op == "ROUTINE_REG":
            if len(ops) >= 2:
                nom, entry_idx = ops[0], ops[1]
                entry_desc = self._describe_target(entry_idx, all_instrs)
                return f"{idx:5d}  {op:<15} {nom}() -> entry={entry_idx}{entry_desc}"
            return f"{idx:5d}  {op:<15} {ops!r}  ; ROUTINE_REG malformé (attendu 2 opérandes)"

        if op == "CALL_ROUTINE":
            nom = ops[0] if ops else "?"
            return f"{idx:5d}  {op:<15} {nom}()"

        if op == "CALL_BUILTIN":
            name = ops[0] if ops else "?"
            argc = ops[1] if len(ops) > 1 else "?"
            return f"{idx:5d}  {op:<15} {name}  ({argc} arg(s))"

        if op == "CALL_FUNC":
            name = ops[0] if ops else "?"
            argc = ops[1] if len(ops) > 1 else "?"
            return f"{idx:5d}  {op:<15} {name}  ({argc} arg(s))"

        if op == "PUSH_CONST":
            val = ops[0] if ops else None
            return f"{idx:5d}  {op:<15} {val!r}"

        if not ops:
            return f"{idx:5d}  {op}"

        return f"{idx:5d}  {op:<15} {', '.join(repr(o) for o in ops)}"

    def _describe_target(self, target, all_instrs: list) -> str:
        """Annote une cible de saut avec l'opcode qui s'y trouve, pour ne
        pas laisser un entier nu sans contexte (ex: '-> 12' seul ne dit
        rien ; '-> 12  # HALT' dit ce qui s'exécute à cet endroit).
        Défensif face à un index hors limites (bytecode corrompu ou
        généré à la main) : annote l'anomalie au lieu de lever IndexError
        depuis un outil de lecture seule qui doit rester utilisable même
        sur du bytecode invalide -- c'est justement pour DIAGNOSTIQUER un
        bytecode potentiellement cassé que cet outil existe."""
        if not isinstance(target, int):
            return "  ; ATTENTION : cible non résolue (label symbolique encore présent)"
        if not (0 <= target < len(all_instrs)):
            return f"  ; ATTENTION : cible hors limites (bytecode invalide, {len(all_instrs)} instruction(s) au total)"
        return f"  # {all_instrs[target].get('opcode', '?')}"


class UVIRuntimeTrap(Exception):
    """Levée quand la VM rencontre une erreur runtime correspondant à un
    TRAP explicite du bytecode ou à une condition détectée par la VM
    elle-même (division par zéro, index hors limites, etc.). `code` est le
    code d'erreur stable (ex: "DIVISION_BY_ZERO"), utilisable tel quel
    pour se brancher sur le système d'erreurs UVIError existant."""

    def __init__(self, code: str, message: Optional[str] = None):
        self.code = code
        self.message = message or code
        super().__init__(f"[{code}] {self.message}")


@dataclass
class Frame:
    locals: Dict[str, Any] = field(default_factory=dict)
    local_slots: Dict[int, Any] = field(default_factory=dict)
    parent: Optional["Frame"] = None
    return_pc: int = 0
    # Ajout de cette session (débogueur) : nom de la fonction qui a créé
    # cette frame, "" pour global_frame ou toute frame qui n'a pas cette
    # notion. Défaut rétrocompatible avec tout Frame(...) déjà écrit
    # avant cette session (aucun site existant n'est cassé) -- seuls les
    # DEUX sites qui créent une frame pour un appel de fonction (CALL_FUNC
    # dans run(), _call_module_function) la renseignent (voir ces deux
    # sites), pour permettre à UVIVirtualMachine.debug_call_stack de
    # dériver une pile d'appels lisible directement depuis frame_stack.
    func_name: str = ""


@dataclass
class VMResult:
    output: List[str]
    variables: Dict[str, Any]
    trapped: Optional[str]
    trap_message: Optional[str]
    steps: int
    profile_data: Optional[dict] = None


class InMemoryVFS:
    """Repli utilisé quand aucun VFSManager réel n'est fourni (tests,
    exécution hors IDE). Interface volontairement identique à
    VFSManager (Uvi_08-1-2.py) pour être substituable sans adaptation."""

    def __init__(self):
        self._files: Dict[str, str] = {}

    def list_files(self):
        return sorted(self._files.keys())

    def read_file(self, filename):
        return self._files.get(filename)

    def write_file(self, filename, content):
        self._files[filename] = content

    def delete_file(self, filename):
        return self._files.pop(filename, None) is not None

    def exists(self, filename):
        return filename in self._files


class UVIVirtualMachine:
    def __init__(
        self,
        vfs=None,
        shion_ask_fn: Optional[Callable[[str], str]] = None,
        input_fn: Optional[Callable[[str], str]] = None,
        print_fn: Optional[Callable[[Any], None]] = None,
        draw_fn: Optional[Callable[..., None]] = None,
        key_down_fn: Optional[Callable[[str], bool]] = None,
        key_pressed_fn: Optional[Callable[[str], bool]] = None,
        legacy_truthy: bool = True,
        max_call_depth: int = 100,
        max_array_size: int = DEFAULT_MAX_ARRAY_SIZE,
        max_string_length: int = DEFAULT_MAX_STRING_LENGTH,
        max_steps: int = DEFAULT_MAX_STEPS,
        profile: bool = False,
    ):
        self.vfs = vfs if vfs is not None else InMemoryVFS()
        self.shion_ask_fn = shion_ask_fn
        self.input_fn = input_fn
        self._print_fn = print_fn
        self._draw_fn = draw_fn
        self._key_down_fn = key_down_fn
        self._key_pressed_fn = key_pressed_fn
        self.legacy_truthy = legacy_truthy
        self.max_call_depth = max_call_depth
        self.max_array_size = max_array_size
        self.max_string_length = max_string_length
        self.max_steps = max_steps
        self._profile_enabled = profile
        # Comptage par opcode (nom -> nombre d'exécutions) et par appel
        # nommé (DEF/*routine -> nombre d'invocations) -- initialisés
        # seulement si profile=True, pour n'ajouter AUCUN coût (mémoire
        # ou CPU) quand le profiling n'est pas demandé. Vérifié
        # empiriquement avant ce correctif : même un simple comptage par
        # dict coûte ~80% de surcoût sur la boucle chaude de run().
        self._profile_opcode_counts: Dict[str, int] = {} if profile else None
        self._profile_call_counts: Dict[str, int] = {} if profile else None

        self.output: List[str] = []
        self.functions: Dict[str, tuple] = {}  # nom -> (params, entry_idx, meta)
        self.routines: Dict[str, int] = {}  # nom -> entry_idx (voir ROUTINE_REG/CALL_ROUTINE)
        self.global_frame = Frame()
        self.frame_stack: List[Frame] = [self.global_frame]
        self.stack: List[Any] = []
        # Registre des modules chargés via MODULE (alias -> VMModule),
        # et fichiers modules actuellement en cours de chargement (pour
        # détecter un cycle A charge B qui charge A) -- même politique
        # que UVIEvaluator.modules/loading_modules côté tree-walker,
        # volontairement dupliquée plutôt que partagée entre les deux
        # moteurs (objets Python de nature différente : ceux-ci portent
        # des VMModule, ceux du tree-walker des UVIModule).
        self.modules: Dict[str, "VMModule"] = {}
        self.loading_modules: set = set()
        # Noms marqués EXPORT pendant l'exécution de CETTE VM en tant
        # que fichier module (rempli par l'opcode EXPORT_NAME dans la
        # boucle run(), lu par _load_module de la VM PARENTE une fois
        # run() terminé). Vide et sans effet si cette VM exécute un
        # script normal plutôt qu'un fichier chargé via MODULE -- une
        # VM enfant est une VM comme une autre, EXPORT_NAME fonctionne
        # de la même façon dans les deux cas, seul l'appelant
        # (_load_module) décide quoi faire du résultat après coup.
        self._pending_exports: set = set()
        # Débogueur (ajout de cette session) : None par défaut, aucun
        # coût ni changement de comportement tant qu'aucun UVIDebugger
        # n'est explicitement attaché -- même politique que
        # UVIEvaluator.debugger côté tree-walker. Contrairement au
        # tree-walker (qui doit maintenir _debug_call_stack séparément,
        # voir sa docstring), la VM n'a besoin d'AUCUNE structure
        # supplémentaire : self.frame_stack est déjà une vraie pile
        # persistante, debug_call_stack (property plus bas) la dérive
        # directement à la demande, sans rien maintenir en parallèle.
        self.debugger: Optional["UVIDebugger"] = None

    @property
    def debug_call_stack(self) -> List[UVIDebugFrame]:
        """Dérive une liste de UVIDebugFrame directement depuis
        self.frame_stack -- la frame la plus ANCIENNE (global_frame,
        jamais un appel) est exclue, et l'ordre est du plus ancien
        appel au plus récent (cohérent avec UVIEvaluator._debug_call_stack
        côté tree-walker, où l'appel le plus récent est en dernier)."""
        return [
            UVIDebugFrame(nom=f.func_name, ligne_appel=0, parametres=dict(f.locals))
            for f in self.frame_stack[1:]
            if f.func_name
        ]

    def _evaluate_debug_condition(self, expr: str):
        """Évalue une condition de point d'arrêt (voir UVIDebugger.
        check_line) côté VM. Contrairement au tree-walker (qui a une
        méthode evaluate(str) directe), la VM ne travaille qu'avec du
        bytecode compilé -- donc : compile_expr(expr) produit une petite
        suite d'instructions PURES (pas de STORE, juste PUSH_CONST/LOAD/
        BINOP/CMP/etc, vérifié empiriquement avant d'écrire cette
        méthode), qu'on exécute via self.run(...) sur un bytecode
        AUTONOME (HALT en fin) -- ce nouveau run() partage
        self.frame_stack/self.global_frame de LA VM COURANTE (donc LOAD
        voit les vraies variables visibles à cet instant), mais PAS
        self.stack (sauvegardée/restaurée explicitement ici, pour ne
        laisser AUCUN résidu sur la pile de la vraie exécution en
        cours). La valeur de la condition reste au sommet de self.stack
        après un tel run() (compile_expr seule, sans STORE derrière, ne
        fait que pousser sa valeur).

        Toute erreur de compilation (BytecodeError) ou d'exécution
        (UVIRuntimeTrap via result.trapped) est absorbée silencieusement
        vers "" (traité comme condition fausse par UVIDebugger.
        _legacy_truthy) -- une condition de point d'arrêt malformée ne
        doit JAMAIS faire échouer le script observé, même contrat que
        UVIEvaluator._evaluate_debug_condition côté tree-walker."""
        saved_stack = self.stack
        self.stack = []
        try:
            instrs = self.compile_expr_for_debug(expr)
        except BytecodeError:
            self.stack = saved_stack
            return ""
        try:
            bytecode = {"instructions": [i.to_dict() for i in instrs] + [{"opcode": "HALT", "operands": [], "line": 0}]}
            result = self.run(bytecode)
            if result.trapped:
                return ""
            return self.stack[-1] if self.stack else ""
        except Exception:
            return ""
        finally:
            self.stack = saved_stack

    @staticmethod
    def compile_expr_for_debug(expr: str) -> List[Instr]:
        """Isolé en méthode statique séparée uniquement pour que
        _evaluate_debug_condition puisse distinguer proprement une
        BytecodeError de compilation (traitée silencieusement) d'une
        erreur d'exécution (result.trapped, traitée séparément juste
        après) -- UVIBytecodeCompilerV2 est instancié à la demande,
        jamais conservé entre deux évaluations de condition (coût
        négligeable pour une petite expression, pas de raison de le
        garder en cache)."""
        return UVIBytecodeCompilerV2().compile_expr(expr)

    # -- utilitaires -------------------------------------------------

    @property
    def current_frame(self) -> Frame:
        return self.frame_stack[-1]

    def truthy(self, v) -> bool:
        if self.legacy_truthy:
            return v in (True, "True", 1)
        return bool(v)

    def _lookup(self, name: str):
        frame = self.current_frame
        while frame is not None:
            if name in frame.locals:
                return frame.locals[name]
            frame = frame.parent
        return ""  # variable non définie -> chaîne vide, mode legacy UVI

    def _emit(self, text: str):
        self.output.append(text)
        if self._print_fn is not None:
            self._print_fn(text)

    def _clamp_string(self, val):
        if isinstance(val, str) and len(val) > self.max_string_length:
            return val[: self.max_string_length]
        return val

    def _default_module_alias(self, nom_fic: str) -> str:
        """Dérive l'alias par défaut d'un MODULE fichier (sans -> alias
        explicite) -- retire d'abord un préfixe std:/pkg: et un
        éventuel @version AVANT de dériver le nom, sinon l'alias
        calculé contiendrait ':' ou '@', des caractères invalides dans
        un identifiant UVI (alias::nom échouerait silencieusement à
        matcher son propre alias). 'std:math' -> 'math',
        'pkg:outils@2' -> 'outils', 'fichier.uvi' -> 'fichier'
        (comportement historique inchangé pour un nom sans préfixe)."""
        nom = nom_fic
        if nom.startswith("std:"):
            nom = nom[len("std:"):]
        elif nom.startswith("pkg:"):
            nom = nom[len("pkg:"):].partition("@")[0]
        return os.path.splitext(os.path.basename(nom))[0]

    def _resolve_module_path(self, nom_fic: str):
        """Même résolution que UVIEvaluator._resolve_module_path côté
        tree-walker (VFS puis chemin direct sur disque) -- dupliquée
        plutôt que partagée, cohérent avec la convention déjà en place
        dans ce fichier pour garder VM et tree-walker sans dépendance
        croisée (cf. _NUM_PREFIX_RE, _replace_bools_for_parsing, etc,
        commentés "garder ce module sans dépendance sur le tree-walker").

        Préfixes std:/pkg: (système de paquets, ajout de cette session)
        reconnus AVANT toute autre résolution -- ':' n'apparaît jamais
        dans un nom de fichier VFS légitime (convention réservée
        délibérément), donc aucune ambiguïté avec un vrai nom de
        fichier utilisateur. package_registry peut être absent (ex.
        InMemoryVFS en test, ou tout objet vfs minimal ne portant pas
        cet attribut) -- dans ce cas std:/pkg: échouent proprement
        (retournent None, traité comme FILE_NOT_FOUND par l'appelant),
        jamais une AttributeError qui remonterait comme un crash Python
        brut plutôt qu'une erreur UVI normale."""
        if nom_fic.startswith("std:"):
            registry = getattr(self.vfs, "package_registry", None)
            return registry.resolve_std(nom_fic[len("std:"):]) if registry else None
        if nom_fic.startswith("pkg:"):
            registry = getattr(self.vfs, "package_registry", None)
            if not registry:
                return None
            reste = nom_fic[len("pkg:"):]
            nom, _, version = reste.partition("@")
            return registry.resolve_pkg(nom, version if version else None)
        chemin_vfs = self.vfs.get_path(nom_fic) if hasattr(self.vfs, "get_path") else None
        if chemin_vfs and os.path.exists(chemin_vfs):
            return chemin_vfs
        if os.path.exists(nom_fic):
            return nom_fic
        return None

    def _load_module(self, nom_fic: str, alias: str) -> Optional["VMModule"]:
        """Charge nom_fic dans un VMModule nommé alias, en compilant et
        exécutant son contenu dans une VM ENFANT complètement séparée
        (voir docstring de VMModule pour la justification de ce choix
        architectural plutôt qu'un swap d'attributs comme côté
        tree-walker). Retourne None ET lève UVIRuntimeTrap en cas
        d'erreur (fichier introuvable, cycle, ou toute erreur survenue
        DANS le fichier module lui-même -- une erreur de compilation ou
        d'exécution du module remonte comme une vraie erreur du script
        appelant, jamais avalée en silence).

        Alias déjà pris pour le MÊME fichier physique : no-op, retourne
        le VMModule déjà enregistré (même politique que _exec_module
        côté tree-walker -- un script qui recharge le même module par
        accident, ex. dans deux branches d'un IF, ne doit pas planter)."""
        if not nom_fic:
            raise UVIRuntimeTrap("INVALID_MODULE", "Nom de fichier de module invalide.")
        alias = alias.strip() if alias else self._default_module_alias(nom_fic)
        if not alias:
            raise UVIRuntimeTrap("INVALID_MODULE", "Alias de module invalide.")
        chemin_final = self._resolve_module_path(nom_fic)
        if not chemin_final:
            raise UVIRuntimeTrap("FILE_NOT_FOUND", f"Module '{nom_fic}' introuvable.")
        abs_path = os.path.abspath(chemin_final)
        if abs_path in self.loading_modules:
            raise UVIRuntimeTrap("CIRCULAR_MODULE", f"Chargement de module circulaire bloqué : '{nom_fic}'.")
        if alias in self.modules and self.modules[alias].source_path == abs_path:
            return self.modules[alias]

        with open(chemin_final, 'r', encoding='utf-8') as f:
            content = f.read()

        self.loading_modules.add(abs_path)
        try:
            lexer = UVILexer()
            parser = UVIParser(lexer)
            ast_nodes = parser.parse_program(content.splitlines())
            compiler = UVIBytecodeCompilerV2()
            bytecode = compiler.compile_program(ast_nodes)
        except BytecodeError as e:
            self.loading_modules.discard(abs_path)
            raise UVIRuntimeTrap("MODULE_COMPILE_ERROR", f"Erreur de compilation dans '{nom_fic}' : {e}")

        # Budget de pas RESTANT de la VM parente, pas un nouveau budget
        # complet -- sinon un module (ou une chaîne de modules imbriqués)
        # pourrait consommer plusieurs fois max_steps au total et
        # contourner la protection RUNAWAY. steps_restants n'est connu
        # qu'À L'APPELANT (dans run(), pas ici) -- voir MODULE_LOAD dans
        # la boucle d'exécution, qui calcule ce budget et le passe en
        # paramètre plutôt que de le recalculer ici.
        vm_enfant = UVIVirtualMachine(
            vfs=self.vfs,
            shion_ask_fn=self.shion_ask_fn,
            input_fn=self.input_fn,
            print_fn=self._print_fn,
            draw_fn=self._draw_fn,
            key_down_fn=self._key_down_fn,
            key_pressed_fn=self._key_pressed_fn,
            legacy_truthy=self.legacy_truthy,
            max_call_depth=self.max_call_depth,
            max_array_size=self.max_array_size,
            max_string_length=self.max_string_length,
            max_steps=max(1, self.max_steps - getattr(self, "_current_steps_hint", 0)),
        )
        # modules/loading_modules PARTAGÉS par référence (pas de copie)
        # entre parent et enfant : un module B chargé PAR un module A
        # doit voir le même registre self.modules que la VM racine, pour
        # que CALL c::fn(...) plus tard dans le script principal puisse
        # retrouver un module chargé transitivement -- et pour que la
        # détection de cycle (loading_modules) fonctionne à travers
        # plusieurs niveaux d'imbrication, pas seulement au niveau direct.
        vm_enfant.modules = self.modules
        vm_enfant.loading_modules = self.loading_modules
        # debugger PROPAGÉ (même instance, pas une copie) -- sans ça, un
        # point d'arrêt posé sur une ligne DU FICHIER MODULE lui-même ne
        # se déclencherait jamais lors de son chargement initial (la
        # ligne 1 de calcmod.uvi et la ligne 1 du script appelant sont
        # deux lignes DIFFÉRENTES malgré le même numéro -- chaque fichier
        # a sa propre numérotation, aucun lien entre elles ; propager le
        # même UVIDebugger permet de poser des points d'arrêt dans
        # n'importe quel fichier chargé, module ou script principal,
        # avec la garantie que UVIDebugPause remonte correctement jusqu'à
        # l'appel racine puisqu'elle hérite de BaseException, jamais
        # interceptée en chemin par un except Exception).
        vm_enfant.debugger = self.debugger

        # try/finally (pas juste un discard après l'appel) : si
        # UVIDebugPause interrompt vm_enfant.run(bytecode) en cours de
        # route (hérite de BaseException, remonte donc à travers ce
        # bloc sans passer par le code normal qui suit), abs_path
        # resterait bloqué dans loading_modules pour TOUJOURS sans ce
        # nettoyage garanti -- tout MODULE ultérieur sur ce même fichier
        # échouerait alors en CIRCULAR_MODULE, même après la fin de la
        # session de débogage. Détecté empiriquement en testant
        # resume_after_pause à travers plusieurs tentatives successives.
        try:
            result = vm_enfant.run(bytecode)
        finally:
            self.loading_modules.discard(abs_path)

        if result.trapped:
            raise UVIRuntimeTrap(result.trapped, result.trap_message or f"Erreur dans le module '{nom_fic}'.")

        module = VMModule(alias=alias, source_path=abs_path, vm=vm_enfant, bytecode=bytecode)
        module.exports = vm_enfant._pending_exports
        self.modules[alias] = module
        return module

    def _call_module_function(self, module: "VMModule", nom_func: str, arg_values: list):
        """Appelle la fonction DEF nom_func, exportée par module, avec
        arg_values déjà évalués (dans le scope de L'APPELANT, avant
        cette méthode -- voir CALL_MODULE_FUNC dans run()).

        Exécute DANS module.vm (la VM enfant déjà chargée), jamais dans
        self : une fonction de module doit voir les globales de SON
        module (module.vm.global_frame) comme parent de frame, jamais
        celles de l'appelant -- cohérent avec l'isolation que tout ce
        chantier construit. Utilise le mécanisme start_pc/
        call_stack_pcs_init de run() (voir sa docstring) plutôt qu'une
        boucle d'exécution dupliquée, pour ne jamais risquer de diverger
        du comportement de CALL_FUNC normal au fil de futures évolutions
        de run().

        UNIQUEMENT pour une fonction DEF -- l'appelant (CALL_MODULE_FUNC
        dans run()) est responsable de vérifier que nom_func est bien
        dans module.functions (pas une routine) avant d'appeler cette
        méthode ; même restriction que _exec_module_call côté
        tree-walker, pour la même raison (scope partagé d'une routine,
        incompatible avec une frontière de module -- voir NOT_A_FUNCTION
        côté tree-walker)."""
        vm_module = module.vm
        params, entry_idx, meta = vm_module.functions[nom_func]
        if len(arg_values) != len(params):
            raise UVIRuntimeTrap("WRONG_ARG_COUNT", f"'{module.alias}::{nom_func}' attend {len(params)} argument(s).")

        instrs = module.bytecode["instructions"]
        sentinel_pc = len(instrs)  # hors bornes -> sort proprement de `while pc < len(instrs)`

        # stack/frame_stack de vm_module DEVRAIENT déjà être à leur état
        # de repos après le chargement initial (pile vide, frame_stack
        # = [global_frame] seul) -- sauvegardés et restaurés ici par
        # précaution seulement, pour ne jamais laisser un appel corrompre
        # l'état de repos du module pour un appel suivant, même si cette
        # hypothèse s'avérait fausse dans un cas non encore rencontré.
        saved_stack = vm_module.stack
        saved_frame_stack = vm_module.frame_stack
        vm_module.stack = []
        new_frame = Frame(parent=vm_module.global_frame, return_pc=sentinel_pc, func_name=nom_func)
        for p, v in zip(params, arg_values):
            new_frame.locals[p] = v
        vm_module.frame_stack = list(saved_frame_stack) + [new_frame]

        try:
            result = vm_module.run(module.bytecode, start_pc=entry_idx, call_stack_pcs_init=[sentinel_pc])
            if result.trapped:
                raise UVIRuntimeTrap(result.trapped, result.trap_message or f"Erreur dans '{module.alias}::{nom_func}'.")
            # RET a fait self.stack.append(ret_val) juste avant de
            # dépiler sentinel_pc -- la valeur de retour est donc au
            # sommet de vm_module.stack à ce point précis.
            return vm_module.stack[-1] if vm_module.stack else ""
        finally:
            vm_module.stack = saved_stack
            vm_module.frame_stack = saved_frame_stack

    # -- builtins ------------------------------------------------------

    def _call_builtin(self, name: str, args: list):
        """Retourne la valeur à empiler, ou la sentinelle _NO_PUSH si le
        builtin ne pousse rien. Lève UVIRuntimeTrap en cas d'erreur."""
        if name == "STR_LEN":
            return len(str(args[0]))
        if name == "STR_NUM":
            texte = str(args[0]).strip()
            try:
                return int(texte)
            except ValueError:
                pass
            try:
                return float(texte)
            except ValueError:
                raise UVIRuntimeTrap("INVALID_NUMBER", f"'{texte}' n'est pas un nombre valide.")
        if name == "TO_INT_STRICT":
            # Distinct de STR_NUM : ?E.nom force un ENTIER explicitement
            # choisi par l'utilisateur -- "4.5" doit échouer, pas se
            # convertir silencieusement en float comme STR_NUM le
            # ferait. Même comportement strict (int() direct, pas
            # int(float())) que celui déjà validé côté tree-walker pour
            # ?E.nom (UVIEvaluator, isinstance(node, InputNode)).
            try:
                return int(str(args[0]))
            except (ValueError, TypeError):
                raise UVIRuntimeTrap(
                    "INVALID_INPUT_TYPE", f"Entrée entière attendue, reçu : '{args[0]}'"
                )
        if name == "TO_REAL_STRICT":
            try:
                return float(str(args[0]))
            except (ValueError, TypeError):
                raise UVIRuntimeTrap(
                    "INVALID_INPUT_TYPE", f"Entrée réelle attendue, reçu : '{args[0]}'"
                )
        if name == "TO_STR":
            # Nécessaire pour l'interpolation de chaîne ("texte {expr}")
            # compilée en une série de PUSH_CONST (segments fixes) +
            # sous-expressions compilées (segments {expr}) + BINOP ADD
            # (concaténation) -- chaque valeur d'expression doit d'abord
            # passer par TO_STR avant de pouvoir être concaténée à du
            # texte (BINOP ADD sur str+int lève TypeError, déjà vérifié
            # empiriquement pour le tout premier bug de cette session).
            return str(args[0])
        if name == "STR_UPPER":
            return str(args[0]).upper()
        if name == "STR_LOWER":
            return str(args[0]).lower()
        if name == "STR_TRIM":
            return str(args[0]).strip()
        if name == "STR_SPLIT":
            texte, sep = str(args[0]), str(args[1])
            return texte.split(sep) if sep else list(texte)
        if name == "STR_JOIN":
            arr, sep = args[0], str(args[1])
            if not isinstance(arr, list):
                raise UVIRuntimeTrap("NOT_A_ARRAY")
            return sep.join(str(x) for x in arr)
        if name == "STR_REPLACE":
            return str(args[0]).replace(str(args[1]), str(args[2]))
        if name == "STR_CONTAINS":
            return str(args[1]) in str(args[0])
        if name == "STR_STARTS":
            return str(args[0]).startswith(str(args[1]))
        if name == "STR_ENDS":
            return str(args[0]).endswith(str(args[1]))
        if name == "STR_SLICE":
            try:
                texte = str(args[0])
                debut = int(float(args[1]))
                fin = int(float(args[2]))
                return texte[debut:fin]
            except (ValueError, TypeError):
                # Les deux seules exceptions vérifiées possibles ici :
                # int(float(args[1 ou 2])) échoue avec ValueError sur une
                # chaîne non numérique ("abc") et TypeError sur un type
                # incompatible (None, une liste...). Le slicing Python
                # lui-même (texte[debut:fin]) ne lève JAMAIS, même avec
                # des indices hors limites ou négatifs extrêmes -- testé
                # empiriquement avant ce correctif. Un except Exception:
                # plus large masquerait à tort un bug interne réel (une
                # faute de frappe dans ce bloc Python, par exemple) sous
                # le même code INVALID_SLICE qu'une vraie erreur
                # utilisateur -- désormais un tel bug remonte bruyamment.
                raise UVIRuntimeTrap("INVALID_SLICE")
        if name == "STR_INDEX":
            return str(args[0]).find(str(args[1]))

        if name == "DISK_SAVE":
            nom_fic, contenu = str(args[0]).strip(), str(args[1])
            if not nom_fic:
                raise UVIRuntimeTrap("MISSING_FILENAME")
            self.vfs.write_file(nom_fic, contenu)
            return _NO_PUSH
        if name == "DISK_LOAD":
            nom_fic = str(args[0]).strip()
            if not nom_fic:
                raise UVIRuntimeTrap("MISSING_FILENAME")
            if not self.vfs.exists(nom_fic):
                raise UVIRuntimeTrap("FILE_NOT_FOUND")
            content = self.vfs.read_file(nom_fic) or ""
            return content.rstrip("\r\n")
        if name == "DISK_LIST":
            files = self.vfs.list_files()
            self._emit(", ".join(files) if files else "vide")
            return _NO_PUSH
        if name == "DISK_DEL":
            nom_fic = str(args[0]).strip()
            if not nom_fic:
                raise UVIRuntimeTrap("MISSING_FILENAME")
            if not self.vfs.exists(nom_fic):
                raise UVIRuntimeTrap("FILE_NOT_FOUND")
            self.vfs.delete_file(nom_fic)
            return _NO_PUSH

        if name == "SHION_ASK":
            # _clamp_string (pas juste str()) : args[0] peut venir d'une
            # expression évaluée à la volée (ex. concaténation), qui n'a
            # pas forcément transité par un STORE/LOCAL_STORE déjà clampé
            # -- même raisonnement et même correctif que côté tree-walker
            # (_exec_shion), pour que les deux moteurs restent alignés.
            prompt, var_dest = self._clamp_string(str(args[0])), str(args[1])
            if not prompt.strip():
                raise UVIRuntimeTrap("SHION_EMPTY_PROMPT")
            if self.shion_ask_fn is None:
                raise UVIRuntimeTrap(
                    "SHION_UNAVAILABLE", "SHION.ASK ne peut pas s'exécuter ici."
                )
            try:
                result = self.shion_ask_fn(prompt)
            except Exception as e:
                raise UVIRuntimeTrap("SHION_REQUEST_FAILED", str(e))
            self.current_frame.locals[var_dest] = result
            return _NO_PUSH

        if name == "INPUT":
            var_name = str(args[0])
            if self.input_fn is None:
                raise UVIRuntimeTrap(
                    "INPUT_UNAVAILABLE", "Aucune source d'entrée fournie à la VM."
                )
            return self.input_fn(var_name)

        if name == "SLEEP":
            duration = min(float(args[0]), 10.0)
            time.sleep(duration)
            return _NO_PUSH

        if name == "TAB_PRINT":
            nom, arr = str(args[0]), args[1]
            if not isinstance(arr, list):
                raise UVIRuntimeTrap("NOT_A_ARRAY")
            if not arr:
                self._emit(f"[{nom}] vide")
            else:
                for idx, val in enumerate(arr):
                    self._emit(f"[{nom}][{idx}] = {val}")
            return _NO_PUSH

        if name in ("DRAW_CLEAR", "DRAW_BGCOLOR", "DRAW_RECT", "DRAW_CIRCLE",
                     "DRAW_LINE", "DRAW_TEXT", "DRAW_SPRITE", "DRAW_POLY"):
            # Même famille que DISK_SAVE/SLEEP : effet de bord pur, aucune
            # valeur poussée. Si l'hôte n'a fourni aucun draw_fn (VM utilisée
            # hors contexte graphique -- tests, compile_and_run en ligne de
            # commande, etc.), DRAW_UNAVAILABLE plutôt qu'un crash silencieux,
            # même politique que SHION_UNAVAILABLE/INPUT_UNAVAILABLE.
            if self._draw_fn is None:
                raise UVIRuntimeTrap(
                    "DRAW_UNAVAILABLE", "Le canevas graphique n'est pas disponible ici."
                )
            if name == "DRAW_CLEAR":
                self._draw_fn("clear")
            elif name == "DRAW_BGCOLOR":
                self._draw_fn("bgcolor", str(args[0]))
            elif name == "DRAW_RECT":
                # Arité 5 (historique), 6 (remplissage), ou 7
                # (+épaisseur) -- compilée en conditionnel dans
                # _compile_draw_node, défauts fill="" / width=1
                # appliqués ici si l'appelant n'a pas tout poussé.
                if len(args) >= 7:
                    x, y, w, h, couleur, remplissage, epaisseur = args
                elif len(args) >= 6:
                    x, y, w, h, couleur, remplissage = args
                    epaisseur = 1
                else:
                    x, y, w, h, couleur = args
                    remplissage = ""
                    epaisseur = 1
                self._draw_fn("rect", self._to_float(x), self._to_float(y),
                              self._to_float(w), self._to_float(h), str(couleur),
                              str(remplissage), int(self._to_float(epaisseur)))
            elif name == "DRAW_CIRCLE":
                if len(args) >= 6:
                    x, y, r, couleur, remplissage, epaisseur = args
                elif len(args) >= 5:
                    x, y, r, couleur, remplissage = args
                    epaisseur = 1
                else:
                    x, y, r, couleur = args
                    remplissage = ""
                    epaisseur = 1
                self._draw_fn("circle", self._to_float(x), self._to_float(y),
                              self._to_float(r), str(couleur), str(remplissage),
                              int(self._to_float(epaisseur)))
            elif name == "DRAW_LINE":
                if len(args) >= 6:
                    x1, y1, x2, y2, couleur, epaisseur = args
                else:
                    x1, y1, x2, y2, couleur = args
                    epaisseur = 1
                self._draw_fn("line", self._to_float(x1), self._to_float(y1),
                              self._to_float(x2), self._to_float(y2), str(couleur),
                              int(self._to_float(epaisseur)))
            elif name == "DRAW_TEXT":
                if len(args) >= 5:
                    x, y, texte, couleur, taille_police = args
                else:
                    x, y, texte, couleur = args
                    taille_police = 10
                self._draw_fn("text", self._to_float(x), self._to_float(y), str(texte),
                              str(couleur), int(self._to_float(taille_police)))
            elif name == "DRAW_SPRITE":
                pixels, x, y, taille = args
                if not isinstance(pixels, list):
                    raise UVIRuntimeTrap("NOT_A_ARRAY")
                self._draw_fn("sprite", self._to_float(x), self._to_float(y),
                              self._to_float(taille), list(pixels))
            elif name == "DRAW_POLY":
                # Arité 1/2/3/4 (nom_tab seul, +couleur, +remplissage,
                # +épaisseur) -- compilée en conditionnel dans
                # _compile_draw_node, même politique de défauts que
                # DRAW_RECT/CIRCLE ci-dessus.
                coords = args[0]
                couleur = str(args[1]) if len(args) >= 2 else "#00FF00"
                remplissage = str(args[2]) if len(args) >= 3 else ""
                epaisseur = args[3] if len(args) >= 4 else 1
                if not isinstance(coords, list):
                    raise UVIRuntimeTrap("NOT_A_ARRAY")
                if len(coords) % 2 != 0 or len(coords) < 6:
                    raise UVIRuntimeTrap(
                        "INVALID_POLYGON",
                        f"le tableau doit contenir un nombre pair de coordonnées "
                        f"et au moins 3 points (6 valeurs), trouvé {len(coords)}."
                    )
                points = [self._to_float(v) for v in coords]
                self._draw_fn("poly", points, couleur, remplissage, int(self._to_float(epaisseur)))
            return _NO_PUSH

        if name == "PACKAGE_PUBLISH":
            # PACKAGE.PUBLISH nom -> fichier -> version : même contrat
            # d'erreurs que _exec_package_publish côté tree-walker
            # (INVALID_PACKAGE, FILE_NOT_FOUND, PACKAGE_UNAVAILABLE).
            # Args déjà évalués (3, arité fixe compilée dans
            # _compile_node PackageNode) -- aucune valeur poussée en
            # retour (effet de bord pur), cohérent avec DRAW_*/DISK_SAVE.
            nom, fichier, version = str(args[0]).strip(), str(args[1]).strip(), str(args[2]).strip()
            registry = getattr(self.vfs, "package_registry", None)
            if registry is None:
                raise UVIRuntimeTrap("PACKAGE_UNAVAILABLE", "Le système de paquets n'est pas disponible ici.")
            if not nom or not version:
                raise UVIRuntimeTrap("INVALID_PACKAGE", "Nom ou version de paquet invalide.")
            if fichier.startswith("std:") or fichier.startswith("pkg:"):
                raise UVIRuntimeTrap("INVALID_PACKAGE", "Impossible de publier un paquet ou la bibliothèque standard eux-mêmes.")
            chemin_final = self._resolve_module_path(fichier)
            if not chemin_final:
                raise UVIRuntimeTrap("FILE_NOT_FOUND", f"Fichier à publier introuvable : '{fichier}'.")
            registry.publish(nom, chemin_final, version)
            return _NO_PUSH

        if name in ("KEY_DOWN", "KEY_PRESSED"):
            # Retourne une valeur (1/0), contrairement au bloc DRAW_*
            # ci-dessus (_NO_PUSH) : _compile_key_node (voir plus haut)
            # génère un STORE juste après ce CALL_BUILTIN, qui s'attend à
            # dépiler quelque chose -- même famille que STR_LEN/TAB_LEN,
            # pas DISK_SAVE. KEY_UNAVAILABLE si l'hôte n'a fourni ni
            # key_down_fn ni key_pressed_fn (VM hors contexte graphique),
            # même politique que DRAW_UNAVAILABLE ci-dessus.
            touche = str(args[0])
            if name == "KEY_DOWN":
                if self._key_down_fn is None:
                    raise UVIRuntimeTrap(
                        "KEY_UNAVAILABLE", "La lecture clavier n'est pas disponible ici."
                    )
                return 1 if self._key_down_fn(touche) else 0
            else:
                if self._key_pressed_fn is None:
                    raise UVIRuntimeTrap(
                        "KEY_UNAVAILABLE", "La lecture clavier n'est pas disponible ici."
                    )
                return 1 if self._key_pressed_fn(touche) else 0

        raise UVIRuntimeTrap("UNKNOWN_BUILTIN", name)

    # -- boucle principale ----------------------------------------------

    def run(self, bytecode: dict, start_pc: int = 0, call_stack_pcs_init: Optional[List[int]] = None) -> VMResult:
        """start_pc/call_stack_pcs_init : AJOUT de cette session, pour le
        système de modules (CALL_MODULE_FUNC) -- une fonction exportée
        d'un module doit reprendre l'exécution à son entry_idx dans LE
        BYTECODE DE CE MODULE (self ici est déjà la VM enfant du
        module, self.functions/entry_idx lui appartiennent), sans
        relancer tout le corps du fichier module depuis pc=0. Par
        défaut (start_pc=0, call_stack_pcs_init=None) le comportement
        est IDENTIQUE à avant cet ajout -- un appel run(bytecode) normal
        ne voit aucune différence.

        Convention pour un appel de fonction de module (voir
        UVIVirtualMachine._call_module_function) : l'appelant pousse
        d'abord la frame de paramètres sur self.frame_stack, passe
        start_pc=entry_idx, et call_stack_pcs_init=[len(instrs)] (une
        sentinelle HORS BORNES) -- le RET normal de la fonction fait
        alors pc = call_stack_pcs.pop() = len(instrs), qui sort
        naturellement de la boucle `while pc < len(instrs)` sans qu'il
        faille changer quoi que ce soit à la logique de RET elle-même.
        La valeur de retour reste au sommet de self.stack après un tel
        appel -- à l'appelant de la lire là, VMResult ne la porte pas
        (elle n'a de sens que pour cet usage précis, pas pour un run()
        de script normal)."""
        instrs = bytecode["instructions"]
        pc = start_pc
        steps = 0
        call_stack_pcs: List[int] = list(call_stack_pcs_init) if call_stack_pcs_init else []
        trapped_code: Optional[str] = None
        trapped_msg: Optional[str] = None
        # Débogueur (ajout de cette session) : -1 = valeur impossible
        # (aucune vraie ligne source ne peut être négative), garantit
        # que la toute première ligne rencontrée déclenche bien un
        # premier appel à check_line.
        last_debug_line = -1

        try:
            while pc < len(instrs):
                steps += 1
                if steps > self.max_steps:
                    raise UVIRuntimeTrap("RUNAWAY", "Trop d'instructions exécutées.")
                instr = instrs[pc]
                op = instr["opcode"]
                ops = instr.get("operands", [])
                if self._profile_enabled:
                    self._profile_opcode_counts[op] = self._profile_opcode_counts.get(op, 0) + 1

                if self.debugger is not None:
                    # Une ligne source produit PLUSIEURS instructions
                    # bytecode (ex. "x = 5" -> PUSH_CONST puis STORE,
                    # même ligne) -- check_line ne doit se déclencher
                    # QU'AU CHANGEMENT de ligne, jamais à chaque
                    # instruction, sinon la même pause se répéterait
                    # plusieurs fois pour une seule ligne source et
                    # l'arrêt pourrait survenir au MILIEU d'une ligne
                    # (ex. entre PUSH_CONST et STORE) plutôt qu'avant
                    # qu'elle commence. instr.get("line", 0) : 0 pour
                    # les instructions de contrôle de flux internes
                    # (JUMP_*, LABEL, HALT -- jamais issues directement
                    # d'un nœud AST, voir _compile_node), ignorées
                    # (aucune ligne UVI réelle n'y correspond).
                    instr_line = instr.get("line", 0)
                    if instr_line and instr_line != last_debug_line:
                        last_debug_line = instr_line
                        self.debugger.check_line(
                            instr_line,
                            self.current_frame.locals,
                            self._evaluate_debug_condition,
                            self.debug_call_stack,
                        )

                if op == "PUSH_CONST":
                    self.stack.append(self._clamp_string(ops[0]))
                elif op == "LOAD":
                    self.stack.append(self._lookup(ops[0]))
                elif op == "STORE":
                    self.current_frame.locals[ops[0]] = self._clamp_string(self.stack.pop())
                elif op == "LOCAL_LOAD":
                    self.stack.append(self.current_frame.local_slots.get(ops[0], 0))
                elif op == "LOCAL_STORE":
                    self.current_frame.local_slots[ops[0]] = self.stack.pop()
                elif op == "CMP":
                    b = self.stack.pop(); a = self.stack.pop()
                    try:
                        self.stack.append(_CMP_OPS[ops[0]](a, b))
                    except TypeError:
                        # Même défaut que BINOP ci-dessous, mêmes types en
                        # cause (ex. comparer une chaîne issue de ?INPUT à
                        # un nombre sans passer par STR.NUM) -- sans ce
                        # garde-fou, une TypeError brute remontait jusqu'au
                        # callback Tk, alors que ce fichier s'interdit
                        # partout ailleurs d'afficher une trace Python nue.
                        raise UVIRuntimeTrap("INVALID_OPERATION", f"{ops[0]}({a!r}, {b!r})")
                elif op == "BINOP":
                    b = self.stack.pop(); a = self.stack.pop()
                    code = ops[0]
                    if code in ("DIV", "MOD") and b == 0:
                        raise UVIRuntimeTrap("DIVISION_BY_ZERO")
                    if code == "POW":
                        # Vérifié empiriquement avant ce correctif : POW
                        # avec un grand exposant ENTIER (pas flottant,
                        # OverflowError ne s'applique qu'aux flottants) ne
                        # lève AUCUNE exception mais peut bloquer
                        # l'exécution plusieurs secondes (10**10000000 a
                        # dépassé un timeout de 5s) -- risque de déni de
                        # service qui rend max_steps inopérant, puisque le
                        # blocage survient À L'INTÉRIEUR d'une seule
                        # instruction Python, jamais comptée par le
                        # compteur de pas de la boucle run(). Et pas
                        # seulement l'exposant : une BASE déjà énorme
                        # (obtenue par une suite de multiplications
                        # légitimes) avec un exposant modeste cause le
                        # même risque -- vérifié : (10**100000)**100 a
                        # pris 9.5s. D'où une estimation sur les DEUX
                        # opérandes, pas seulement b.
                        #
                        # log10(|a|)*|b| estime le nombre de chiffres du
                        # résultat SANS le calculer -- vérifié rapide
                        # (<1ms) même sur un entier a déjà plusieurs
                        # millions de chiffres, Python extrait l'ordre de
                        # grandeur sans manipuler chaque chiffre. a=0
                        # traité à part (0**b vaut 0 ou 1, jamais gros).
                        # Seuil = self.max_string_length : le même que
                        # _clamp_string applique déjà aux chaînes -- un
                        # résultat qui serait de toute façon tronqué à
                        # l'affichage n'a aucune raison d'être calculé en
                        # entier avant troncature.
                        if a != 0:
                            import math
                            chiffres_estimes = abs(b) * math.log10(abs(a))
                            if chiffres_estimes > self.max_string_length:
                                raise UVIRuntimeTrap(
                                    "NUMBER_TOO_LARGE",
                                    f"Résultat de POW trop grand (~{int(chiffres_estimes)} chiffres estimés)."
                                )
                    try:
                        self.stack.append(self._clamp_string(_BIN_OPS[code](a, b)))
                    except UVIRuntimeTrap:
                        raise
                    except (TypeError, OverflowError):
                        # Les deux seules exceptions vérifiées possibles
                        # ici, sur toute combinaison de types plausible en
                        # UVI (typage dynamique : nombre, chaîne, liste,
                        # booléen, None) -- TypeError sur une combinaison
                        # incompatible ("5"+3, [1]+3, "5"**3...), OverflowError
                        # sur POW avec un exposant flottant extrême. Pas de
                        # ValueError/IndexError trouvés empiriquement pour
                        # aucun opérateur de _BIN_OPS. Un except Exception:
                        # plus large masquerait à tort un bug interne réel
                        # sous le même code INVALID_OPERATION.
                        raise UVIRuntimeTrap("INVALID_OPERATION", f"{code}({a!r}, {b!r})")
                elif op == "UNARY_NOT":
                    self.stack.append(not self.stack.pop())
                elif op == "UNARY_NEG":
                    val = self.stack.pop()
                    try:
                        self.stack.append(-val)
                    except TypeError:
                        raise UVIRuntimeTrap("INVALID_OPERATION", f"NEG({val!r})")
                elif op == "MAKE_ARRAY":
                    n = ops[0]
                    if n > self.max_array_size:
                        raise UVIRuntimeTrap("ARRAY_FULL", "Tableau trop grand.")
                    items = [self.stack.pop() for _ in range(n)][::-1]
                    self.stack.append(items)
                elif op == "APPEND":
                    val = self.stack.pop(); arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    if len(arr) >= self.max_array_size:
                        raise UVIRuntimeTrap("ARRAY_FULL", "Tableau plein.")
                    arr.append(self._clamp_string(val))
                elif op == "POP_ARRAY":
                    arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    if not arr:
                        raise UVIRuntimeTrap("EMPTY_ARRAY")
                    self.stack.append(arr.pop())
                elif op == "INDEX":
                    idx = self.stack.pop(); arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    idx_int = self._to_index(idx)
                    if not (0 <= idx_int < len(arr)):
                        raise UVIRuntimeTrap("INDEX_OUT_OF_BOUNDS")
                    self.stack.append(arr[idx_int])
                elif op == "INDEX_SET":
                    val = self.stack.pop(); idx = self.stack.pop(); arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    idx_int = self._to_index(idx)
                    if not (0 <= idx_int < len(arr)):
                        raise UVIRuntimeTrap("INDEX_OUT_OF_BOUNDS")
                    arr[idx_int] = self._clamp_string(val)
                elif op == "DEL_INDEX":
                    idx = self.stack.pop(); arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    idx_int = self._to_index(idx)
                    if not (0 <= idx_int < len(arr)):
                        raise UVIRuntimeTrap("INDEX_OUT_OF_BOUNDS")
                    arr.pop(idx_int)
                elif op == "LEN":
                    arr = self.stack.pop()
                    if not isinstance(arr, list):
                        raise UVIRuntimeTrap("NOT_A_ARRAY")
                    self.stack.append(len(arr))
                elif op == "JUMP_IF_FALSE":
                    cond = self.stack.pop()
                    if not self.truthy(cond):
                        pc = ops[0]; continue
                elif op == "JUMP_IF_TRUE":
                    cond = self.stack.pop()
                    if self.truthy(cond):
                        pc = ops[0]; continue
                elif op == "JUMP_ABS":
                    pc = ops[0]; continue
                elif op == "LABEL":
                    pass
                elif op == "PRINT":
                    self._emit(self.stack.pop())
                elif op == "POP":
                    self.stack.pop()
                elif op == "CALL_BUILTIN":
                    name, argc = ops
                    args = [self.stack.pop() for _ in range(argc)][::-1]
                    result = self._call_builtin(name, args)
                    if result is not _NO_PUSH:
                        self.stack.append(result)
                elif op == "DEF_REG":
                    nom, params, entry_idx, meta = ops
                    self.functions[nom] = (params, entry_idx, meta)
                elif op == "CALL_FUNC":
                    nom, argc = ops
                    if nom not in self.functions:
                        raise UVIRuntimeTrap("UNKNOWN_FUNCTION", nom)
                    params, entry_idx, meta = self.functions[nom]
                    if argc != len(params):
                        raise UVIRuntimeTrap("WRONG_ARG_COUNT", nom)
                    if len(call_stack_pcs) > self.max_call_depth:
                        raise UVIRuntimeTrap("MAX_RECURSION")
                    if self._profile_enabled:
                        cle = f"DEF:{nom}"
                        self._profile_call_counts[cle] = self._profile_call_counts.get(cle, 0) + 1
                    arg_values = [self.stack.pop() for _ in range(argc)][::-1]
                    new_frame = Frame(parent=self.global_frame, return_pc=pc + 1, func_name=nom)
                    for p, v in zip(params, arg_values):
                        new_frame.locals[p] = v
                    self.frame_stack.append(new_frame)
                    call_stack_pcs.append(pc + 1)
                    pc = entry_idx
                    continue
                elif op == "RET":
                    ret_val = self.current_frame.locals.get("_retour", "")
                    self.frame_stack.pop()
                    self.stack.append(ret_val)
                    pc = call_stack_pcs.pop()
                    continue
                elif op == "ROUTINE_REG":
                    nom, entry_idx = ops
                    self.routines[nom] = entry_idx
                elif op == "CALL_ROUTINE":
                    nom = ops[0]
                    if nom not in self.routines:
                        raise UVIRuntimeTrap("UNKNOWN_ROUTINE", nom)
                    # PAS de nouvelle Frame (contrairement à CALL_FUNC) :
                    # le corps de la routine doit lire ET écrire les mêmes
                    # variables que l'appelant -- voir la justification
                    # complète dans OpcodeV1.CALL_ROUTINE et
                    # _compile_node/RoutineDefNode ci-dessus. On empile
                    # seulement le PC de retour, jamais frame_stack.
                    if len(call_stack_pcs) > self.max_call_depth:
                        raise UVIRuntimeTrap("MAX_RECURSION")
                    if self._profile_enabled:
                        cle = f"ROUTINE:{nom}"
                        self._profile_call_counts[cle] = self._profile_call_counts.get(cle, 0) + 1
                    call_stack_pcs.append(pc + 1)
                    pc = self.routines[nom]
                    continue
                elif op == "RET_ROUTINE":
                    # Symétrique de CALL_ROUTINE : ne dépile JAMAIS
                    # frame_stack (rien n'y a été empilé), et ne pousse
                    # rien sur self.stack -- une routine n'a pas de
                    # _retour, aucune syntaxe UVI ne capture le résultat
                    # d'un appel de routine (cf. RoutineCallNode côté AST,
                    # qui ne porte que 'name', jamais de cible d'affectation).
                    pc = call_stack_pcs.pop()
                    continue
                elif op == "MODULE_LOAD":
                    # MODULE fichier -> alias : node.filename a déjà été
                    # compilé/évalué (compile_expr) et poussé sur la
                    # pile par _compile_node(ModuleNode) -- on le dépile
                    # ici. alias (ops[0]) est un identifiant littéral,
                    # jamais évalué (voir _compile_node ModuleNode).
                    # Budget de pas transmis à la VM enfant = reliquat
                    # de CE run() (self.max_steps - steps déjà
                    # consommés), jamais un budget neuf complet -- sinon
                    # un module contournerait RUNAWAY (voir docstring de
                    # _load_module).
                    nom_fic = str(self.stack.pop()).strip()
                    alias = ops[0]
                    self._current_steps_hint = steps
                    self._load_module(nom_fic, alias)
                elif op == "EXPORT_NAME":
                    # EXPORT nom : nom (ops[0]) doit exister comme
                    # variable (current_frame.locals -- au niveau racine
                    # d'un fichier module, current_frame EST
                    # global_frame, donc c'est bien la bonne frame à
                    # vérifier), fonction, ou routine à cet instant --
                    # même contrat que _exec_export côté tree-walker.
                    nom = ops[0]
                    if nom in self.current_frame.locals or nom in self.functions or nom in self.routines:
                        self._pending_exports.add(nom)
                    else:
                        raise UVIRuntimeTrap(
                            "UNKNOWN_EXPORT_TARGET",
                            f"'{nom}' n'est ni une variable ni une routine/fonction définie à ce point."
                        )
                elif op == "MODULE_GET":
                    # Lecture alias::nom en expression -- ops = [alias,
                    # nom], tous deux des littéraux (voir compilation de
                    # __module_get__). Même contrat d'erreurs que
                    # UVIEvaluator.evaluate côté tree-walker :
                    # UNKNOWN_MODULE si alias jamais chargé, NOT_EXPORTED
                    # si nom existe en privé OU n'existe pas du tout
                    # (volontairement le même code pour les deux cas,
                    # voir VMModule.get_exported).
                    alias, nom = ops
                    if alias not in self.modules:
                        raise UVIRuntimeTrap("UNKNOWN_MODULE", f"Module '{alias}' non chargé (utiliser MODULE d'abord).")
                    trouve, val = self.modules[alias].get_exported(nom)
                    if not trouve:
                        raise UVIRuntimeTrap("NOT_EXPORTED", f"'{nom}' n'est pas exporté par le module '{alias}'.")
                    self.stack.append(val)
                elif op == "CALL_MODULE_FUNC":
                    # CALL alias::nom(args) -- ops = [alias, nom_func,
                    # argc]. Les argc valeurs sont déjà sur la pile
                    # (compilées par _compile_call_node avant cet
                    # opcode), dans le scope de L'APPELANT -- c'est la
                    # frontière de données qui traverse le module, avant
                    # que l'exécution ne bascule dans module.vm. Même
                    # contrat d'erreurs que _exec_module_call côté
                    # tree-walker (UNKNOWN_MODULE, NOT_EXPORTED,
                    # NOT_A_FUNCTION pour une routine exportée appelée
                    # via CALL, WRONG_ARG_COUNT délégué à
                    # _call_module_function).
                    alias, nom_func, argc = ops
                    arg_values = [self.stack.pop() for _ in range(argc)][::-1]
                    if alias not in self.modules:
                        raise UVIRuntimeTrap("UNKNOWN_MODULE", f"Module '{alias}' non chargé (utiliser MODULE d'abord).")
                    module = self.modules[alias]
                    if not module.has_exported_callable(nom_func):
                        raise UVIRuntimeTrap("NOT_EXPORTED", f"'{nom_func}' n'est pas exporté par le module '{alias}'.")
                    if nom_func not in module.functions:
                        raise UVIRuntimeTrap(
                            "NOT_A_FUNCTION",
                            f"'{alias}::{nom_func}' est une routine, pas une fonction (CALL attend une fonction DEF)."
                        )
                    if len(call_stack_pcs) > self.max_call_depth:
                        raise UVIRuntimeTrap("MAX_RECURSION")
                    if self._profile_enabled:
                        cle = f"DEF:{alias}::{nom_func}"
                        self._profile_call_counts[cle] = self._profile_call_counts.get(cle, 0) + 1
                    ret_val = self._call_module_function(module, nom_func, arg_values)
                    self.stack.append(ret_val)
                elif op == "TRAP":
                    raise UVIRuntimeTrap(ops[0])
                elif op == "HALT":
                    break
                else:
                    raise UVIRuntimeTrap("UNKNOWN_OPCODE", op)

                pc += 1
        except UVIRuntimeTrap as trap:
            trapped_code, trapped_msg = trap.code, trap.message

        profile_data = None
        if self._profile_enabled:
            profile_data = {
                "opcode_counts": dict(self._profile_opcode_counts),
                "call_counts": dict(self._profile_call_counts),
                "top_opcodes": sorted(
                    self._profile_opcode_counts.items(), key=lambda kv: kv[1], reverse=True
                ),
                "total_steps": steps,
            }

        return VMResult(
            output=self.output,
            variables=dict(self.global_frame.locals),
            trapped=trapped_code,
            trap_message=trapped_msg,
            steps=steps,
            profile_data=profile_data,
        )

    @staticmethod
    def _to_index(idx) -> int:
        try:
            return int(float(idx))
        except (TypeError, ValueError):
            raise UVIRuntimeTrap("INVALID_INDEX")

    @staticmethod
    def _to_float(v) -> float:
        """Coercition tolérante utilisée par les builtins DRAW_* pour les
        paramètres géométriques -- même politique que _draw_num côté
        tree-walker (UVIEvaluator) : jamais de TRAP pour une coordonnée
        mal formée, retombe sur 0.0. Les deux moteurs doivent produire le
        même comportement observable pour un même script DRAW.*."""
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


_NO_PUSH = object()

_CMP_OPS = {
    "EQ": lambda a, b: a == b, "NEQ": lambda a, b: a != b,
    "LT": lambda a, b: a < b, "LTE": lambda a, b: a <= b,
    "GT": lambda a, b: a > b, "GTE": lambda a, b: a >= b,
}
_BIN_OPS = {
    "ADD": lambda a, b: a + b, "SUB": lambda a, b: a - b,
    "MUL": lambda a, b: a * b, "DIV": lambda a, b: a / b,
    "MOD": lambda a, b: a % b, "POW": lambda a, b: a ** b,
}


# ---------------------------------------------------------------------------
# Fonctions de commodité : compiler puis exécuter du code source UVI en un
# seul appel, sans passer par l'IDE Tkinter ni par le tree-walker.
# ---------------------------------------------------------------------------

def compile_and_run(
    source_lines: List[str],
    parser,
    compiler=None,
    **vm_kwargs,
) -> VMResult:
    """parser : instance de UVIParser (Uvi_08-1-2.py) déjà construite,
    réutilisée pour bénéficier du VRAI lexer/parser du projet plutôt que
    d'en réimplémenter un ici. compiler : instance de
    UVIBytecodeCompilerV2 (compiler_v2.py) ; en créé une si omis."""
    ast_nodes = parser.parse_program(source_lines)
    bytecode = compiler.compile_program(ast_nodes)
    vm = UVIVirtualMachine(**vm_kwargs)
    return vm.run(bytecode)



# ---------------------------------------------------------------------------
# Optimiseur — opère sur le flux SYMBOLIQUE (UVIBytecodeCompilerV2.
# compile_symbolic(), AVANT _resolve_labels). Raison détaillée dans la
# docstring de compile_symbolic() : supprimer ou réordonner des
# instructions sur un flux où les sauts sont déjà résolus en indices
# entiers désynchroniserait silencieusement toute cible qui dépasse le
# point de modification, sans qu'aucune exception ne le signale. Sur le
# flux symbolique, un saut référence un NOM de label, qui reste valide
# tant que l'instruction LABEL correspondante n'est pas elle-même
# supprimée -- ce que les deux passes ci-dessous ne font jamais.
#
# Usage :
#   symbolic = compiler.compile_symbolic(ast_nodes)
#   optimized = UVIBytecodeOptimizer().optimize(symbolic)
#   resolved = compiler._resolve_labels(optimized)   # même passe qu'avant
#   # ou directement : UVIBytecodeOptimizer().optimize_program(compiler, ast_nodes)
#
# DEUX PASSES, VOLONTAIREMENT PRUDENTES :
#
#   1. Repliement de constantes (constant folding) sur BINOP/CMP : quand
#      deux PUSH_CONST consécutifs alimentent un BINOP/CMP, le résultat
#      est calculable À LA COMPILATION plutôt qu'à chaque exécution de
#      cette ligne (utile en particulier à l'intérieur d'une boucle, où le
#      calcul serait refait à chaque itération sinon). Réutilise
#      EXACTEMENT _BIN_OPS/_CMP_OPS de la VM (mêmes lambdas, même
#      dict) -- PAS une réimplémentation séparée -- pour qu'un repliement
#      donne, par construction, le même résultat que l'exécution non
#      optimisée aurait donné. Les erreurs runtime légitimes (division par
#      zéro, opération invalide) sont volontairement NON repliées : le
#      code original est laissé tel quel pour que la VM lève le même
#      UVIRuntimeTrap qu'elle aurait levé sans optimisation, plutôt que
#      l'optimiseur ne décide lui-même du comportement d'erreur.
#
#   2. Élimination de code mort (dead code elimination) : toute
#      instruction strictement entre un saut inconditionnel/RET/HALT et le
#      PROCHAIN LABEL est inatteignable, PARCE QU'aucune instruction avant
#      ce label ne peut plus jamais s'exécuter (le flux vient de sauter
#      ailleurs ou de s'arrêter) ET qu'aucun saut venu d'ailleurs dans le
#      programme ne peut cibler une position qui n'est pas un LABEL (tous
#      les sauts du compilateur ciblent des labels générés par
#      _new_label, jamais un index arbitraire). Supprimer ces instructions
#      est donc sûr SANS avoir besoin de vérifier tout le programme pour
#      des références externes -- la garantie vient de l'invariant du
#      compilateur, pas d'une analyse globale. Cette passe s'arrête PILE
#      au prochain LABEL (jamais au-delà) : au-delà d'un LABEL, on ne sait
#      plus si le code est mort sans une vraie analyse de flux de
#      contrôle globale, que cette passe ne fait délibérément pas.
# ---------------------------------------------------------------------------

class _FoldFailedSentinel:
    """Sentinelle distincte de None -- un repliement peut légitimement
    produire None-like ou False comme résultat (ex: CMP renvoie un bool),
    donc `is _FOLD_FAILED` doit être testable sans ambiguïté avec un
    résultat replié valide."""
    def __repr__(self):
        return "<FOLD_FAILED>"


_FOLD_FAILED = _FoldFailedSentinel()


class UVIBytecodeOptimizer:
    """Optimise le flux symbolique produit par
    UVIBytecodeCompilerV2.compile_symbolic(), avant résolution des labels."""

    # Opcodes qui interrompent le flux de façon inconditionnelle : tout ce
    # qui suit, jusqu'au prochain LABEL, est mort (cf. justification ci-
    # dessus). JUMP_IF_FALSE/JUMP_IF_TRUE sont volontairement EXCLUS de
    # cette liste : ce sont des sauts CONDITIONNELS, l'instruction suivante
    # reste atteignable si la condition ne déclenche pas le saut.
    UNCONDITIONAL_TERMINATORS = (OpcodeV1.JUMP_ABS, OpcodeV1.RET, OpcodeV1.RET_ROUTINE, OpcodeV1.HALT)

    def optimize(self, symbolic: List[Instr]) -> List[Instr]:
        instrs = self._fold_constants(symbolic)
        instrs = self._eliminate_dead_code(instrs)
        return instrs

    def optimize_program(self, compiler: "UVIBytecodeCompilerV2", ast_nodes: list) -> dict:
        """Confort : reproduit compile_program() mais avec optimisation
        intercalée avant résolution des labels. Ne duplique pas la logique
        de compile_program -- appelle compile_symbolic() et
        _resolve_labels() exactement comme compile_program le fait, avec
        self.optimize() entre les deux."""
        symbolic = compiler.compile_symbolic(ast_nodes)
        optimized = self.optimize(symbolic)
        resolved = compiler._resolve_labels(optimized)
        return {
            "bytecode_version": "1.0",
            "instructions": [i.to_dict() for i in resolved],
        }

    # -- Passe 1 : repliement de constantes -------------------------------

    def _fold_constants(self, instrs: List[Instr]) -> List[Instr]:
        out: List[Instr] = []
        i = 0
        n = len(instrs)
        while i < n:
            # Motif : PUSH_CONST a, PUSH_CONST b, (BINOP|CMP) code
            if (i + 2 < n
                    and instrs[i].opcode == OpcodeV1.PUSH_CONST
                    and instrs[i + 1].opcode == OpcodeV1.PUSH_CONST
                    and instrs[i + 2].opcode in (OpcodeV1.BINOP, OpcodeV1.CMP)):
                a = instrs[i].operands[0]
                b = instrs[i + 1].operands[0]
                code = instrs[i + 2].operands[0]
                table = _BIN_OPS if instrs[i + 2].opcode == OpcodeV1.BINOP else _CMP_OPS
                folded = self._try_fold(table, code, a, b)
                if folded is not _FOLD_FAILED:
                    out.append(Instr(OpcodeV1.PUSH_CONST, [folded]))
                    i += 3
                    continue
                # Échec de repliement (ex: division par zéro à la
                # compilation) -- ne PAS optimiser cette ligne, la laisser
                # telle quelle pour que la VM lève l'erreur runtime
                # normale au bon moment plutôt qu'à la compilation.
            out.append(instrs[i])
            i += 1
        return out

    def _try_fold(self, table: dict, code: str, a, b):
        if code not in table:
            return _FOLD_FAILED
        try:
            return table[code](a, b)
        except Exception:
            # Division par zéro, comparaison de types incompatibles, etc.
            # -- comportement d'erreur laissé à la VM au runtime, cf.
            # justification dans le commentaire de tête de cette classe.
            return _FOLD_FAILED

    # -- Passe 2 : élimination de code mort --------------------------------

    def _eliminate_dead_code(self, instrs: List[Instr]) -> List[Instr]:
        out: List[Instr] = []
        i = 0
        n = len(instrs)
        while i < n:
            out.append(instrs[i])
            if instrs[i].opcode in self.UNCONDITIONAL_TERMINATORS:
                # Tout ce qui suit jusqu'au prochain LABEL (exclu de la
                # suppression : c'est une cible de saut valide) est mort.
                j = i + 1
                while j < n and instrs[j].opcode != OpcodeV1.LABEL:
                    j += 1
                i = j  # reprend PILE au LABEL (ou à n si aucun LABEL après)
                continue
            i += 1
        return out


"""
tools.py — Parsing et exécution d'appels d'outils au format
<|tool_call|>{...}<|/tool_call|>, adapté au projet UVI.

Ce module n'existait pas dans les fichiers fournis (agent_loop.py
l'importe mais ne le définit pas) -- il est recréé ici, à l'identique de
l'interface attendue par agent_loop.py (ParseError, ParsedToolCall,
ToolCallParser, ToolExecutionResult, ToolExecutor), avec un registre
d'outils orienté UVI plutôt que générique : les "outils" exposés sont des
actions sur le moteur UVI (exécuter une ligne de script, lister le VFS,
lire un fichier VFS), pas des outils système arbitraires.

Format d'appel attendu dans le texte généré :
    <|tool_call|>{"name": "uvi_exec", "arguments": {"line": "! 1 + 1"}}<|/tool_call|>
"""



@dataclass
class ParseError:
    error_type: str
    error_message: str


@dataclass
class ParsedToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecutionResult:
    success: bool
    output: Any = None
    error: Optional[str] = None


_TOOL_CALL_PATTERN = re.compile(r"<\|tool_call\|>(.*?)<\|/tool_call\|>", re.DOTALL)


class ToolCallParser:
    """Extrait puis parse les appels d'outils du texte généré par un
    modèle. La réparation JSON (_try_repair_json) reste volontairement
    simple : elle ne corrige que les deux erreurs les plus fréquentes
    observées en pratique sur des sorties de petits modèles (guillemets
    simples au lieu de doubles, virgule finale trainante) -- pas un
    parseur JSON tolérant complet, qui masquerait des erreurs de format
    plus profondes en les "réparant" silencieusement à tort."""

    def extract_calls(self, text: str) -> List[str]:
        return _TOOL_CALL_PATTERN.findall(text)

    def parse_call(self, raw: str) -> Union[ParsedToolCall, ParseError]:
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            repaired = self._try_repair_json(raw)
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError as e:
                return ParseError("INVALID_JSON", str(e))

        if not isinstance(data, dict) or "name" not in data:
            return ParseError(
                "MISSING_FIELD", "Le tool_call doit être un objet JSON avec au moins 'name'."
            )
        return ParsedToolCall(name=data["name"], arguments=data.get("arguments", {}) or {})

    @staticmethod
    def _try_repair_json(raw: str) -> str:
        repaired = raw.replace("'", '"')
        repaired = re.sub(r",\s*}", "}", repaired)
        repaired = re.sub(r",\s*]", "]", repaired)
        return repaired


class ToolExecutor:
    """Registre d'outils UVI + dispatch. Les outils eux-mêmes ne sont PAS
    câblés en dur ici : ils sont fournis par l'appelant (typiquement
    IAAgent/UVIEngine, cf. uvi_agent_extension.py) pour que ce module reste
    indépendant du reste du projet, comme agent_loop.py le demande."""

    def __init__(self, tools: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None):
        self.tools = tools or {}

    def register(self, name: str, fn: Callable[[Dict[str, Any]], Any]):
        self.tools[name] = fn

    def execute(self, call: ParsedToolCall) -> ToolExecutionResult:
        fn = self.tools.get(call.name)
        if fn is None:
            return ToolExecutionResult(
                success=False, error=f"Outil inconnu : '{call.name}'."
            )
        try:
            result = fn(call.arguments)
            return ToolExecutionResult(success=True, output=result)
        except Exception as e:
            return ToolExecutionResult(success=False, error=str(e))


"""
self_correction.py — Exécution de code avec capture d'erreur, adapté à
UVI.

Comme tools.py, ce module n'existait pas dans les fichiers fournis
(agent_loop.py l'importe pour ExecutionResult mais ne le définit pas).

Différence assumée avec un self_correction.py "générique" pour un projet
LLM classique : ici, le "code" que la boucle d'agent doit exécuter et
corriger est du CODE SOURCE UVI (le langage du projet), pas du Python
arbitraire. Exécuter du Python généré par un modèle dans un sous-processus
non isolé serait exactement le risque de sécurité documenté dans
agent_loop.py (voir son en-tête) -- UVICodeExecutor l'évite structurellement
en n'exécutant JAMAIS de Python : seule la VM UVI (uvi_vm.py), qui n'a pas
d'accès disque/réseau hors des primitives explicites &save/&load/SHION.ASK,
interprète le code généré.
"""



@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    trapped: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None


class UVICodeExecutor:
    """Exécute un extrait de code source UVI (une ou plusieurs lignes,
    SANS la signature @UVI) via UVIParser + UVIBytecodeCompilerV2 +
    UVIVirtualMachine, et retourne un ExecutionResult exploitable par
    AgentLoop/IssueDiagnosisLoop (self_correction.py contract).

    parser : instance de UVIParser (Uvi_08-1-2.py) déjà construite --
        réutilisée pour ne pas dupliquer le lexer/parser réel du langage.
    vfs : VFSManager réel ou compatible (facultatif -- InMemoryVFS de
        uvi_vm.py si omis, pour ne jamais faire écrire un agent autonome
        sur le VRAI stockage disque sans que l'appelant l'ait explicitement
        demandé).
    """

    def __init__(self, parser, vfs=None, shion_ask_fn=None, input_fn=None,
                 compiler=None, vm_kwargs: Optional[Dict[str, Any]] = None):
        self.parser = parser
        self.vfs = vfs
        self.shion_ask_fn = shion_ask_fn
        self.input_fn = input_fn
        self.compiler = compiler
        self.vm_kwargs = vm_kwargs or {}

    def execute(self, code: str) -> ExecutionResult:
        if self.compiler is None:
            self.compiler = UVIBytecodeCompilerV2()

        lines = code.splitlines()
        try:
            ast_nodes = self.parser.parse_program(lines)
            bytecode = self.compiler.compile_program(ast_nodes)
        except Exception as e:
            # Erreur de compilation -- traitée comme un échec d'exécution
            # normal (pas une exception qui remonterait jusqu'à AgentLoop),
            # pour que IssueDiagnosisLoop puisse la diagnostiquer comme
            # n'importe quelle autre erreur runtime.
            return ExecutionResult(success=False, stderr=f"Erreur de compilation : {e}")

        vm = UVIVirtualMachine(
            vfs=self.vfs, shion_ask_fn=self.shion_ask_fn,
            input_fn=self.input_fn, **self.vm_kwargs,
        )
        result = vm.run(bytecode)
        stdout = "\n".join(str(x) for x in result.output)
        if result.trapped is not None:
            return ExecutionResult(
                success=False, stdout=stdout,
                stderr=f"[{result.trapped}] {result.trap_message}",
                trapped=result.trapped, variables=result.variables,
            )
        return ExecutionResult(success=True, stdout=stdout, variables=result.variables)


"""
shion/agent_loop.py
Boucle d'agent générique (observer -> décider -> exécuter -> vérifier ->
continuer/arrêter) + processus de diagnostic structuré pour les bugs détectés
en cours d'exécution (issue -> diagnose -> fix -> retest -> resolved/blocked).

================================================================================
CE QUE CE MODULE AJOUTE, ET CE QU'IL RÉUTILISE DÉLIBÉRÉMENT SANS DUPLIQUER
================================================================================
Deux pièces de la logique demandée existent déjà ailleurs dans le projet,
sous une forme plus étroite :

  - tools.py (ToolCallParser / ToolExecutor) sait déjà parser un appel
    d'action et l'exécuter. C'est l'étape 3 ("Exécuter") du premier schéma.
    Ce module NE réimplémente PAS le parsing/exécution d'outil : AgentLoop
    délègue à un ToolExecutor existant.
  - self_correction.py (SelfCorrectionLoop) sait déjà faire exécuter du code
    -> capturer l'erreur -> réinjecter le feedback -> régénérer, mais
    UNIQUEMENT pour un unique bloc de code Python généré une fois, sans
    notion d'observation d'état plus large ni de plusieurs types d'action.
    Ce module réutilise CodeExecutor/ExecutionResult de self_correction.py
    comme MÉCANISME D'EXÉCUTION pour la branche "issue détectée" (deuxième
    schéma), sans redéfinir un troisième format d'ExecutionResult.

Ce qui est réellement NOUVEAU ici :
  1. AgentState / AgentLoop : la boucle générale à 6 étapes du premier schéma
     (Observer, Choisir, Exécuter, Vérifier, Que faire ensuite, Stop), qui
     n'existait pas — self_correction.py régénère du code mais n'a pas de
     notion de garde-fou (nombre max d'itérations, budget), d'objectif
     vérifiable explicite, ni de plusieurs types d'action (outil generic vs
     code Python).
  2. IssueDiagnosisLoop : le sous-processus du deuxième schéma (une seule
     cause proposée par tentative, correction atomique, verrouillage des
     autres corrections pendant l'investigation, test de régression avant de
     déclarer "resolved", état BLOCKED distinct d'un échec silencieux).
     SelfCorrectionLoop régénère un bloc de code à partir de zéro à chaque
     tentative ; IssueDiagnosisLoop diagnostique explicitement une cause
     depuis un état existant avant de proposer un correctif — les deux sont
     des stratégies différentes, pas des doublons l'une de l'autre.

================================================================================
CE QUE CE MODULE NE FAIT PAS AUTOMATIQUEMENT (garde-fous volontairement
explicites, dans le même esprit que config.py et self_correction.py)
================================================================================
  - AgentLoop ne choisit PAS le modèle d'exécution du code (SubprocessCodeExecutor
    vs DockerCodeExecutor) : ce choix reste, comme dans self_correction.py, à
    la charge de l'appelant. Un agent qui boucle sans supervision humaine sur
    du code généré par le modèle est précisément le cas où l'isolation légère
    (sous-processus) est la moins appropriée — voir l'avertissement de
    sécurité en tête de self_correction.py, qui s'applique ici avec plus de
    force encore puisque AgentLoop peut exécuter PLUSIEURS actions en série
    sans validation humaine entre chacune si max_iterations est grand.
  - AgentLoop n'invente PAS de nouveaux tokens spéciaux (pas de <|action|>,
    <|observation|>...). Les tokens spéciaux existants (<|tool_call|>,
    <|tool_result|>, <|think|>, <|answer|>) sont ceux que le tokenizer connaît
    réellement (voir tokenizer.py:SPECIAL_TOKENS) et donc ceux sur lesquels un
    modèle entraîné avec ce package peut avoir appris un comportement. Un
    format d'action générique supplémentaire n'aurait aucun support
    d'entraînement et ne ferait que fragmenter le format déjà utilisé par
    tools.py.
  - AgentLoop ne décide pas seul de la "réussite" d'un objectif au sens du
    schéma ("test réussi, brouillon prêt, ticket résolu..."). La vérification
    (étape 4) est déléguée à une fonction verify_fn fournie par l'appelant,
    dans le même esprit de découplage que generate_fn dans SelfCorrectionLoop.
    Un objectif "vérifiable" au sens du schéma est vérifiable PAR UN CODE QUE
    TU ÉCRIS (test unitaire, comparaison de sortie, validation de schéma) —
    ni ce module ni SelfEvalHead (reasoning.py) ne remplacent cette
    vérification externe : SelfEvalHead prédit une CONFIANCE apprise à partir
    des hidden states du modèle, ce qui suppose déjà un signal d'entraînement
    externe (voir sa docstring) et n'est de toute façon pas branchable comme
    oracle texte->booléen depuis une boucle d'orchestration côté CPU.
================================================================================
"""



# ==============================================================================
# PARTIE 1 — Boucle d'agent générique (premier schéma)
#   Observer l'état actuel -> Choisir l'action suivante -> Exécuter ->
#   Vérifier le résultat -> Que faire ensuite ? -> Stop
# ==============================================================================


class NextStepDecision(str, Enum):
    """Les 5 issues possibles de l'étape "Que faire ensuite ?" du schéma,
    plus STOP qui correspond à l'étape 6 séparée. Une Enum plutôt que des
    chaînes libres pour que decide_next_step ne puisse pas retourner une
    valeur que le reste de la boucle ne sait pas gérer — l'erreur, si le
    modèle produit une décision hors de cette liste, doit apparaître au
    moment du parsing de la décision (voir _parse_decision), pas être
    découverte silencieusement plus loin dans la boucle."""

    CONTINUE = "continuer"
    FIX = "corriger"
    RETRY = "réessayer"
    ROLLBACK = "revenir en arrière"
    STOP = "arrêter"


@dataclass
class AgentGuardrails:
    """Étape "Garde-fou" du schéma : limiter le coût et le risque. Regroupées
    dans une dataclass séparée plutôt que comme paramètres épars de
    AgentLoop.run, pour qu'un appelant puisse construire un seul objet de
    politique et le réutiliser/logger tel quel, et pour que l'ajout d'un
    futur garde-fou (ex: budget en tokens) n'élargisse pas la signature de
    run().

    max_iterations et max_wall_clock_seconds sont volontairement TOUS LES
    DEUX présents et vérifiés indépendamment : un agent qui boucle sur des
    actions rapides peut épuiser max_iterations en quelques secondes (limite
    utile), tandis qu'un agent bloqué sur une action lente (ex: un outil
    réseau qui timeout lentement) peut n'avoir fait que 2 itérations en 10
    minutes (max_iterations ne protège pas contre ça, max_wall_clock_seconds
    si).
    """

    max_iterations: int = 20
    max_wall_clock_seconds: Optional[float] = None
    require_human_validation_every_n_steps: Optional[int] = None
    # Si non-None, run() s'arrête et retourne un statut demandant une
    # validation humaine toutes les N itérations plutôt que de continuer
    # indéfiniment sans supervision — pertinent surtout combiné à un
    # ToolExecutor qui expose des outils à effet de bord réel (écriture
    # disque, appels réseau), voir l'avertissement de tools.py sur
    # ToolExecutor qui ne fournit lui-même aucun sandbox.


@dataclass
class AgentStep:
    """Trace d'une seule itération de la boucle — une ligne par passage dans
    le cycle Observer->Choisir->Exécuter->Vérifier->Décider. Conservée dans
    AgentRunResult.steps pour permettre, après coup, de rejouer/logger/auditer
    exactement ce qui s'est passé, dans le même esprit que
    SelfCorrectionOutcome.attempts dans self_correction.py."""

    iteration: int
    observation: str
    chosen_action_raw: str  # texte brut généré par le modèle pour cette étape, avant parsing
    action: Optional[Union[ParsedToolCall, "CodeAction"]]
    action_parse_error: Optional[str]
    execution_result: Optional[Union[ToolExecutionResult, ExecutionResult]]
    verification_passed: Optional[bool]
    verification_message: str
    decision: Optional[NextStepDecision]
    issue_diagnosis: Optional["IssueDiagnosisOutcome"] = None
    # Rempli uniquement quand cette étape a détecté un bug et déclenché le
    # sous-processus du deuxième schéma (voir PARTIE 2) — None dans le cas
    # normal où l'exécution/vérification s'est déroulée sans détection de bug.


@dataclass
class AgentRunResult:
    stopped_reason: str  # "objectif_atteint" | "max_iterations" | "timeout" | "validation_humaine_requise" | "bloqué"
    succeeded: bool
    steps: List[AgentStep] = field(default_factory=list)


@dataclass
class CodeAction:
    """Une action peut être soit un appel d'outil au format tools.py
    (<|tool_call|>{...}<|/tool_call|>), soit un bloc de code Python direct
    (```python ... ```), qui est la convention déjà utilisée par
    SelfCorrectionLoop.extract_code. AgentLoop accepte les deux plutôt que de
    forcer toute action à passer par le format tool_call, parce que forcer un
    bloc de code arbitraire dans un JSON d'arguments de tool_call
    (ré-échapper les guillemets, les retours à la ligne...) est justement le
    genre de conversion qui introduit le JSON malformé que
    ToolCallParser._try_repair_json existe pour corriger — autant garder les
    deux formats distincts puisque le code source les distingue déjà
    nativement (self_correction.py a son propre extracteur regex pour
    exactement cette raison)."""

    code: str


def choose_action_type(
    generated_text: str,
    tool_parser: Optional[ToolCallParser],
    code_pattern_extractor: Optional[Callable[[str], Optional[str]]],
) -> tuple[Optional[Union[ParsedToolCall, CodeAction]], Optional[str]]:
    """Détermine, à partir du texte généré pour l'étape "Choisir l'action
    suivante", s'il s'agit d'un appel d'outil ou d'un bloc de code, et le
    parse en conséquence. Retourne (action, message_erreur) — message_erreur
    est None en cas de succès, jamais les deux en même temps.

    ORDRE DE PRIORITÉ délibéré : un appel d'outil valide est cherché
    D'ABORD. Un bloc ```python``` est cherché seulement si aucun tool_call
    n'a été trouvé. Justification : si le modèle a été entraîné avec
    use_tool_calling=True (voir config.py), le format tool_call est le canal
    "officiel" pour toute action qui a un ToolSpec enregistré ; un bloc de
    code brut hors de ce format ne devrait être interprété comme une action
    que pour les cas non couverts par un outil existant (typiquement:
    exécuter un calcul ad hoc). Chercher le code en premier risquerait de
    capturer un exemple de code mentionné DANS le raisonnement du modèle
    (```python``` utilisé pour illustrer, pas pour être exécuté) alors qu'un
    tool_call explicite est un signal d'intention nettement moins ambigu.
    """
    if tool_parser is not None:
        raw_calls = tool_parser.extract_calls(generated_text)
        if raw_calls:
            parsed = tool_parser.parse_call(raw_calls[0])
            if isinstance(parsed, ParseError):
                return None, f"{parsed.error_type}: {parsed.error_message}"
            return parsed, None

    if code_pattern_extractor is not None:
        code = code_pattern_extractor(generated_text)
        if code is not None:
            return CodeAction(code=code), None

    return None, (
        "Aucune action reconnaissable dans le texte généré : ni appel d'outil "
        "(<|tool_call|>...<|/tool_call|>) ni bloc de code (```python...```)."
    )


class AgentLoop:
    """
    Implémente le premier schéma dans son intégralité :

        1. Observer l'état actuel   -> observe_fn()
        2. Choisir l'action suivante -> generate_fn(prompt observation)
        3. Exécuter                  -> tool_executor / code_executor
        4. Vérifier le résultat      -> verify_fn(execution_result)
        5. Que faire ensuite ?       -> decide_next_step(...)
        6. Stop                      -> quand l'objectif vérifiable est atteint,
                                         ou qu'un garde-fou est atteint.

    Comme SelfCorrectionLoop, ce n'est PAS ce module qui possède le modèle de
    génération : generate_fn est fourni par l'appelant. C'est délibéré et pour
    la même raison (voir docstring de SelfCorrectionLoop) — permet de tester
    la boucle sans charger un modèle réel, et laisse l'appelant gérer la
    troncature/formatage de contexte, qui dépend du tokenizer et de
    max_seq_len (config.py), pas de ce module.
    """

    def __init__(
        self,
        guardrails: AgentGuardrails,
        tool_executor: Optional[ToolExecutor] = None,
        tool_parser: Optional[ToolCallParser] = None,
        code_executor=None,  # SubprocessCodeExecutor | DockerCodeExecutor (self_correction.py)
        code_pattern_extractor: Optional[Callable[[str], Optional[str]]] = None,
        issue_diagnosis_loop: Optional["IssueDiagnosisLoop"] = None,
    ):
        if tool_executor is not None and tool_parser is None:
            raise ValueError(
                "tool_executor fourni sans tool_parser : impossible de savoir "
                "comment extraire un appel d'outil du texte généré. Fournis "
                "les deux ensemble (voir tools.py: ToolCallParser/ToolExecutor "
                "sont conçus comme une paire, pas comme deux options indépendantes)."
            )
        if code_executor is not None and code_pattern_extractor is None:
            # Valeur par défaut cohérente avec SelfCorrectionLoop plutôt que de
            # forcer l'appelant à la refournir alors qu'elle existe déjà.
            import re

            _default_pattern = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)

            def code_pattern_extractor(text: str) -> Optional[str]:
                match = _default_pattern.search(text)
                return match.group(1).strip() if match else None

        self.guardrails = guardrails
        self.tool_executor = tool_executor
        self.tool_parser = tool_parser
        self.code_executor = code_executor
        self.code_pattern_extractor = code_pattern_extractor
        self.issue_diagnosis_loop = issue_diagnosis_loop

    def _execute_action(
        self, action: Union[ParsedToolCall, CodeAction]
    ) -> Union[ToolExecutionResult, ExecutionResult]:
        if isinstance(action, ParsedToolCall):
            if self.tool_executor is None:
                raise ValueError(
                    "Une action de type appel d'outil a été choisie mais aucun "
                    "tool_executor n'a été fourni à AgentLoop."
                )
            return self.tool_executor.execute(action)
        else:
            if self.code_executor is None:
                raise ValueError(
                    "Une action de type bloc de code a été choisie mais aucun "
                    "code_executor n'a été fourni à AgentLoop. Voir "
                    "self_correction.py pour SubprocessCodeExecutor/"
                    "DockerCodeExecutor et l'avertissement de sécurité associé "
                    "avant d'en choisir un."
                )
            return self.code_executor.execute(action.code)

    @staticmethod
    def _execution_looks_like_a_bug(
        execution_result: Union[ToolExecutionResult, ExecutionResult]
    ) -> bool:
        """Distingue "l'exécution a échoué" (déclenche potentiellement le
        sous-processus de diagnostic du deuxième schéma) de "l'exécution a
        réussi mais la vérification externe juge le résultat insuffisant"
        (relève de decide_next_step, pas d'un diagnostic de bug). Ce n'est
        PAS la même chose : un outil de recherche qui s'exécute sans
        exception mais retourne zéro résultat pertinent n'est pas un "bug détecté"
        au sens du schéma (pas d'ISSUE, pas de memory_temp.md, pas de
        verrouillage des autres corrections) — c'est un résultat correct
        d'un point de vue système, juste inadéquat pour l'objectif, à traiter
        par une nouvelle itération normale de la boucle."""
        if isinstance(execution_result, ToolExecutionResult):
            return not execution_result.success
        return not execution_result.success

    def run(
        self,
        goal_description: str,
        observe_fn: Callable[[List[AgentStep]], str],
        generate_fn: Callable[[str], str],
        verify_fn: Callable[[Union[ToolExecutionResult, ExecutionResult]], tuple[bool, str]],
        decide_next_step: Callable[[AgentStep, List[AgentStep]], NextStepDecision],
    ) -> AgentRunResult:
        """
        observe_fn(steps_so_far) -> texte d'observation (étape 1). Reçoit
            l'historique des étapes précédentes plutôt qu'un état opaque,
            pour que l'observation puisse dépendre de tout ce qui a déjà été
            tenté (fichiers déjà modifiés, erreurs déjà vues) sans que ce
            module impose une structure d'état particulière.
        generate_fn(prompt) -> texte généré (étape 2, réutilise le modèle via
            un wrapper fourni par l'appelant, même contrat que
            SelfCorrectionLoop.run).
        verify_fn(execution_result) -> (succès_objectif: bool, message: str)
            (étape 4). C'est CETTE fonction qui encode ce qu'est un "objectif
            vérifiable" pour la tâche réelle — voir note en tête de fichier
            sur pourquoi ce module ne le devine pas lui-même.
        decide_next_step(dernière_étape, toutes_les_étapes) -> NextStepDecision
            (étape 5). Reçoit l'étape complète (y compris
            verification_passed) pour pouvoir choisir CONTINUE si l'objectif
            n'est pas encore atteint mais qu'il y a des raisons de penser
            qu'une itération de plus aidera, ROLLBACK si l'action a eu un
            effet de bord à annuler, etc. — cette fonction encode la
            politique de décision, ce module ne la fixe pas.
        """
        steps: List[AgentStep] = []
        start_time = time.monotonic()

        for iteration in range(1, self.guardrails.max_iterations + 1):
            if (
                self.guardrails.max_wall_clock_seconds is not None
                and (time.monotonic() - start_time) > self.guardrails.max_wall_clock_seconds
            ):
                return AgentRunResult(stopped_reason="timeout", succeeded=False, steps=steps)

            if (
                self.guardrails.require_human_validation_every_n_steps is not None
                and iteration > 1
                and (iteration - 1) % self.guardrails.require_human_validation_every_n_steps == 0
            ):
                return AgentRunResult(
                    stopped_reason="validation_humaine_requise", succeeded=False, steps=steps
                )

            # --- 1. Observer l'état actuel ---
            observation = observe_fn(steps)

            # --- 2. Choisir l'action suivante ---
            prompt = (
                f"Objectif : {goal_description}\n"
                f"Observation actuelle : {observation}\n"
                f"Choisis la prochaine action (appel d'outil ou bloc de code)."
            )
            generated = generate_fn(prompt)
            action, parse_error = choose_action_type(
                generated, self.tool_parser, self.code_pattern_extractor
            )

            if action is None:
                step = AgentStep(
                    iteration=iteration, observation=observation, chosen_action_raw=generated,
                    action=None, action_parse_error=parse_error, execution_result=None,
                    verification_passed=False, verification_message=parse_error or "",
                    decision=None,
                )
                steps.append(step)
                decision = decide_next_step(step, steps)
                step.decision = decision
                if decision == NextStepDecision.STOP:
                    return AgentRunResult(stopped_reason="bloqué", succeeded=False, steps=steps)
                continue

            # --- 3. Exécuter ---
            execution_result = self._execute_action(action)
            issue_diagnosis_outcome = None

            # Branchement vers le deuxième schéma : un bug DÉTECTÉ PENDANT
            # L'EXÉCUTION (pas une simple insuffisance de résultat, voir
            # _execution_looks_like_a_bug) déclenche le sous-processus dédié
            # plutôt que de traiter ça comme une itération de plus de la
            # boucle générale — c'est exactement la branche "Bug détecté" du
            # schéma EXECUTION -> ISSUE.
            if self._execution_looks_like_a_bug(execution_result) and self.issue_diagnosis_loop is not None:
                issue_diagnosis_outcome = self.issue_diagnosis_loop.run(
                    issue_context=f"Action : {action}\nObjectif : {goal_description}",
                    initial_execution_result=execution_result,
                    generate_fn=generate_fn,
                    tool_parser=self.tool_parser,
                    # BUG CORRIGÉ (voir historique) : sans cette transmission
                    # explicite, IssueDiagnosisLoop._parse_fix retombe sur son
                    # défaut tool_parser=None et ne peut plus jamais
                    # reconnaître un Fix qui est un appel d'outil, même quand
                    # AgentLoop lui-même a un tool_parser configuré — il le
                    # traite alors à tort comme un bloc de code, ce qui casse
                    # _apply_fix dès que code_executor est un ToolExecutor
                    # pur (pas de CodeExecutor configuré).
                )
                if issue_diagnosis_outcome.final_status == IssueStatus.RESOLVED:
                    execution_result = issue_diagnosis_outcome.attempts[-1].execution_result

            # --- 4. Vérifier le résultat ---
            verification_passed, verification_message = verify_fn(execution_result)

            step = AgentStep(
                iteration=iteration, observation=observation, chosen_action_raw=generated,
                action=action, action_parse_error=None, execution_result=execution_result,
                verification_passed=verification_passed, verification_message=verification_message,
                decision=None, issue_diagnosis=issue_diagnosis_outcome,
            )
            steps.append(step)

            if verification_passed:
                # --- 6. Stop (objectif vérifiable atteint) ---
                step.decision = NextStepDecision.STOP
                return AgentRunResult(stopped_reason="objectif_atteint", succeeded=True, steps=steps)

            if (
                issue_diagnosis_outcome is not None
                and issue_diagnosis_outcome.final_status == IssueStatus.BLOCKED
            ):
                step.decision = NextStepDecision.STOP
                return AgentRunResult(stopped_reason="bloqué", succeeded=False, steps=steps)

            # --- 5. Que faire ensuite ? ---
            decision = decide_next_step(step, steps)
            step.decision = decision
            if decision == NextStepDecision.STOP:
                return AgentRunResult(stopped_reason="bloqué", succeeded=False, steps=steps)
            # CONTINUE / FIX / RETRY / ROLLBACK reboucle vers l'étape 1. Ce
            # module ne distingue pas leur traitement au niveau du contrôle de
            # boucle (les quatre reviennent à "encore une itération") — la
            # distinction sert à observe_fn/decide_next_step eux-mêmes (une
            # observation après ROLLBACK peut vouloir annuler un effet de
            # bord, ce que ce module ne peut pas faire à la place de
            # l'appelant puisque la nature de l'effet de bord dépend de
            # l'outil exécuté).

        return AgentRunResult(stopped_reason="max_iterations", succeeded=False, steps=steps)


# ==============================================================================
# PARTIE 2 — Processus de diagnostic de bug (deuxième schéma)
#   ISSUE -> DIAGNOSE -> FIX -> RETEST -> (RESOLVED | BLOCKED, avec
#   REGRESSION TEST avant RESOLVED, et jusqu'à 3 tentatives avant BLOCKED)
# ==============================================================================


class IssueStatus(str, Enum):
    ISSUE = "issue"
    DIAGNOSE = "diagnose"
    FIX = "fix"
    RETEST = "retest"
    REGRESSION_TEST = "regression_test"
    RESOLVED = "resolved"
    BLOCKED = "blocked"


@dataclass
class Diagnosis:
    """"Une seule cause proposée" (schéma) : root_cause est une chaîne
    unique, pas une liste. C'est une contrainte volontaire du format de
    données lui-même (pas seulement une instruction textuelle au modèle) —
    si generate_fn produit plusieurs causes candidates dans son texte,
    _parse_diagnosis (voir IssueDiagnosisLoop) doit en retenir UNE, parce
    qu'une correction atomique (voir Fix) suppose une cause unique à
    adresser ; corriger plusieurs causes à la fois dans une seule tentative
    rendrait impossible d'attribuer un échec de RETEST à laquelle des
    corrections était fautive."""

    root_cause: str
    raw_model_output: str


@dataclass
class Fix:
    """Une correction atomique — un seul changement, tracé par description
    plutôt que par diff automatique : ce module ne suppose PAS que l'action
    de correction est nécessairement une modification de fichier (ça peut
    être un changement d'argument d'appel d'outil, un ajustement de
    configuration...), donc pas de format de diff unique à imposer."""

    description: str
    code_or_action: Union[str, ParsedToolCall]


@dataclass
class DiagnosisAttempt:
    attempt_number: int
    diagnosis: Diagnosis
    fix: Fix
    execution_result: Union[ToolExecutionResult, ExecutionResult]
    retest_passed: bool


@dataclass
class IssueDiagnosisOutcome:
    final_status: IssueStatus  # RESOLVED ou BLOCKED uniquement en sortie de run()
    attempts: List[DiagnosisAttempt]
    memory_temp_content: Optional[str]
    # Non None si final_status == BLOCKED : correspond à "Conserver
    # memory_temp.md" dans le schéma — la trace des tentatives est préservée
    # pour reprise ultérieure (par un humain ou une session future) plutôt
    # que perdue silencieusement.


class IssueDiagnosisLoop:
    """
    Implémente le sous-processus du deuxième schéma une fois qu'un bug a été
    détecté (le schéma complet CONTEXT/CLARIFY/LOOP_DESIGN/READY qui précède
    EXECUTION dans l'image concerne la PLANIFICATION d'une tâche avant
    exécution, ce qui est du ressort de l'appelant qui construit le prompt
    initial / goal_description passé à AgentLoop.run, pas de ce sous-module
    dont le rôle commence au moment où EXECUTION détecte un bug) :

        ISSUE (verrouiller les autres corrections, checkpoint)
          -> DIAGNOSE (une seule cause)
          -> FIX (une seule correction atomique)
          -> RETEST
             -> échec, tentative < 3 -> retour à DIAGNOSE
             -> échec, tentative = 3 -> BLOCKED (garde memory_temp.md)
             -> succès -> REGRESSION_TEST
                -> échec -> retour à DIAGNOSE (compte comme une nouvelle
                   tentative : un regression test qui échoue signifie que le
                   fix a introduit un NOUVEAU problème, ce qui mérite un
                   diagnostic complet, pas un simple retry silencieux du même
                   fix)
                -> succès -> RESOLVED

    "Verrouiller les autres corrections" (étape ISSUE) : ce module l'implémente
    en s'assurant qu'un seul run() de cette classe est actif à la fois via
    _lock (threading.Lock), pas par convention documentaire seule — deux
    diagnostics concurrents sur le même état sous-jacent (ex: même base de
    code) invalideraient mutuellement leurs hypothèses de cause.
    """

    def __init__(
        self,
        tool_executor: Optional[ToolExecutor] = None,
        code_executor=None,  # SubprocessCodeExecutor | DockerCodeExecutor (self_correction.py)
        max_diagnosis_attempts: int = 3,
        regression_test_fn: Optional[Callable[[], tuple[bool, str]]] = None,
    ):
        """
        tool_executor / code_executor : les DEUX peuvent être fournis en même
            temps, exactement comme pour AgentLoop — un Fix proposé pendant le
            diagnostic peut tout aussi bien être un appel d'outil qu'un bloc
            de code, indépendamment de ce qu'était l'action originale qui a
            causé le bug (une action outil qui a échoué peut très bien être
            corrigée par un fix en code, et inversement). Forcer un seul
            executor au moment de la construction de IssueDiagnosisLoop
            obligerait à deviner par avance quel type de Fix le modèle va
            proposer, ce qui n'a pas de sens puisque c'est justement ce que
            DIAGNOSE/FIX décident dynamiquement. Au moins un des deux doit
            être fourni (voir l'assertion ci-dessous) — sans aucun executor,
            _apply_fix ne pourrait jamais rien exécuter.
        regression_test_fn : () -> (passed, message). Optionnel — si None,
            l'étape REGRESSION_TEST est sautée et un RETEST réussi mène
            directement à RESOLVED. C'est un choix explicite plutôt qu'un
            regression test factice toujours réussi : un appelant qui n'a
            pas de suite de tests de régression à disposition doit le savoir
            (documenté ici) plutôt que de croire silencieusement qu'une
            vérification a eu lieu alors qu'aucune régression n'a réellement
            été testée.
        """
        import threading

        if tool_executor is None and code_executor is None:
            raise ValueError(
                "IssueDiagnosisLoop a besoin d'au moins un executor "
                "(tool_executor et/ou code_executor) pour pouvoir appliquer "
                "un Fix — sans aucun des deux, _apply_fix ne peut jamais "
                "s'exécuter."
            )

        self.tool_executor = tool_executor
        self.code_executor = code_executor
        self.max_diagnosis_attempts = max_diagnosis_attempts
        self.regression_test_fn = regression_test_fn
        self._lock = threading.Lock()

    def _apply_fix(self, fix: Fix) -> Union[ToolExecutionResult, ExecutionResult]:
        if isinstance(fix.code_or_action, ParsedToolCall):
            if self.tool_executor is None:
                raise ValueError(
                    "Le Fix proposé est un appel d'outil mais aucun "
                    "tool_executor n'a été fourni à IssueDiagnosisLoop (voir "
                    "tools.py)."
                )
            return self.tool_executor.execute(fix.code_or_action)
        else:
            if self.code_executor is None:
                raise ValueError(
                    "Le Fix proposé est un bloc de code mais aucun "
                    "code_executor n'a été fourni à IssueDiagnosisLoop (voir "
                    "self_correction.py pour SubprocessCodeExecutor/"
                    "DockerCodeExecutor)."
                )
            return self.code_executor.execute(fix.code_or_action)

    @staticmethod
    def _parse_diagnosis(generated_text: str) -> Diagnosis:
        """Extrait 'une seule cause proposée' du texte généré. Convention
        adoptée : la ligne commençant par 'Cause :' (ou, à défaut, la
        première ligne non vide) — volontairement une convention texte
        simple plutôt qu'un format JSON, parce qu'un diagnostic est
        fondamentalement une explication en langue naturelle ; le forcer en
        JSON ({"root_cause": "..."}) n'apporterait pas de garantie
        supplémentaire (le modèle peut tout aussi bien halluciner un JSON
        avec plusieurs causes concatenées dans le même champ string) et
        ajouterait un point de parsing supplémentaire pour rien.
        """
        for line in generated_text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("cause"):
                _, _, cause = stripped.partition(":")
                cause = cause.strip()
                if cause:
                    return Diagnosis(root_cause=cause, raw_model_output=generated_text)
        first_nonempty = next(
            (line.strip() for line in generated_text.splitlines() if line.strip()), ""
        )
        return Diagnosis(root_cause=first_nonempty, raw_model_output=generated_text)

    @staticmethod
    def _parse_fix(generated_text: str, tool_parser: Optional[ToolCallParser]) -> Fix:
        """Réutilise choose_action_type via un import local pour éviter un
        import circulaire (choose_action_type est défini plus haut dans ce
        même fichier — pas de cycle réel, mais l'ordre de définition en
        Python impose de la référencer seulement au moment de l'appel, pas
        au chargement du module)."""
        import re

        default_pattern = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)

        action, error = choose_action_type(
            generated_text,
            tool_parser,
            lambda text: (m.group(1).strip() if (m := default_pattern.search(text)) else None),
        )
        if action is None:
            # Un Fix qui ne contient aucune action reconnaissable est traité
            # comme un fix textuel brut (par exemple, une modification de
            # config décrite en prose) — DiagnosisAttempt.fix.code_or_action
            # accepte une str précisément pour ce cas, mais _apply_fix ne
            # peut alors rien exécuter automatiquement ; c'est un signal que
            # generate_fn doit être guidé pour produire un fix actionnable.
            return Fix(description=generated_text.strip(), code_or_action=generated_text.strip())
        if isinstance(action, ParsedToolCall):
            return Fix(description=generated_text.strip(), code_or_action=action)
        return Fix(description=generated_text.strip(), code_or_action=action.code)

    def run(
        self,
        issue_context: str,
        initial_execution_result: Union[ToolExecutionResult, ExecutionResult],
        generate_fn: Callable[[str], str],
        tool_parser: Optional[ToolCallParser] = None,
    ) -> IssueDiagnosisOutcome:
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise RuntimeError(
                "IssueDiagnosisLoop.run() est déjà en cours d'exécution ailleurs "
                "(verrouillage 'autres corrections' du schéma ISSUE). Un second "
                "diagnostic concurrent sur le même état invaliderait les "
                "hypothèses de cause du premier."
            )

        try:
            attempts: List[DiagnosisAttempt] = []
            error_context = (
                initial_execution_result.stderr
                if isinstance(initial_execution_result, ExecutionResult)
                else str(initial_execution_result.error)
            )

            for attempt_number in range(1, self.max_diagnosis_attempts + 1):
                # --- DIAGNOSE ---
                diagnosis_prompt = (
                    f"{issue_context}\n"
                    f"Erreur observée :\n{error_context}\n"
                    f"Propose UNE SEULE cause probable, sous la forme "
                    f"'Cause : <explication>'."
                )
                diagnosis = self._parse_diagnosis(generate_fn(diagnosis_prompt))

                # --- FIX ---
                fix_prompt = (
                    f"Cause retenue : {diagnosis.root_cause}\n"
                    f"Propose une correction atomique (un seul changement) qui "
                    f"adresse cette cause, sous forme de bloc de code ```python``` "
                    f"ou d'appel d'outil."
                )
                fix = self._parse_fix(generate_fn(fix_prompt), tool_parser)

                # --- RETEST ---
                retest_result = self._apply_fix(fix)
                retest_passed = (
                    retest_result.success
                    if isinstance(retest_result, (ExecutionResult, ToolExecutionResult))
                    else False
                )

                if not retest_passed:
                    attempts.append(DiagnosisAttempt(
                        attempt_number=attempt_number, diagnosis=diagnosis, fix=fix,
                        execution_result=retest_result, retest_passed=False,
                    ))
                    error_context = (
                        retest_result.stderr
                        if isinstance(retest_result, ExecutionResult)
                        else str(retest_result.error)
                    )
                    if attempt_number == self.max_diagnosis_attempts:
                        # --- BLOCKED : conserver memory_temp.md, demander à l'utilisateur ---
                        memory_temp = self._render_memory_temp(issue_context, attempts)
                        return IssueDiagnosisOutcome(
                            final_status=IssueStatus.BLOCKED, attempts=attempts,
                            memory_temp_content=memory_temp,
                        )
                    continue  # échec, tentative < max -> retour à DIAGNOSE

                # --- REGRESSION_TEST (si fourni) ---
                if self.regression_test_fn is not None:
                    regression_passed, regression_message = self.regression_test_fn()
                    if not regression_passed:
                        attempts.append(DiagnosisAttempt(
                            attempt_number=attempt_number, diagnosis=diagnosis, fix=fix,
                            execution_result=retest_result, retest_passed=True,
                        ))
                        error_context = (
                            f"Le retest a réussi mais le test de régression a échoué : "
                            f"{regression_message}"
                        )
                        if attempt_number == self.max_diagnosis_attempts:
                            memory_temp = self._render_memory_temp(issue_context, attempts)
                            return IssueDiagnosisOutcome(
                                final_status=IssueStatus.BLOCKED, attempts=attempts,
                                memory_temp_content=memory_temp,
                            )
                        continue  # régression détectée -> nouveau cycle DIAGNOSE complet

                # --- RESOLVED : écrire la mémoire permanente, autoriser le bug suivant ---
                attempts.append(DiagnosisAttempt(
                    attempt_number=attempt_number, diagnosis=diagnosis, fix=fix,
                    execution_result=retest_result, retest_passed=True,
                ))
                return IssueDiagnosisOutcome(
                    final_status=IssueStatus.RESOLVED, attempts=attempts, memory_temp_content=None,
                )

            # Ne devrait être atteint que si max_diagnosis_attempts <= 0
            # (mauvaise config de l'appelant) — pas un état normal du schéma.
            return IssueDiagnosisOutcome(
                final_status=IssueStatus.BLOCKED, attempts=attempts,
                memory_temp_content=self._render_memory_temp(issue_context, attempts),
            )
        finally:
            self._lock.release()

    @staticmethod
    def _render_memory_temp(issue_context: str, attempts: List[DiagnosisAttempt]) -> str:
        """Rend la trace de diagnostic au format 'memory_temp.md' du schéma —
        conservée (pas supprimée, à la différence du cas RESOLVED où le
        schéma dit explicitement de supprimer memory_temp.md) pour permettre
        une reprise ultérieure avec le contexte complet des causes déjà
        écartées, plutôt que de refaire les mêmes hypothèses de diagnostic
        dans une session future."""
        lines = [f"# memory_temp.md — diagnostic bloqué\n", f"Contexte : {issue_context}\n"]
        for a in attempts:
            lines.append(f"## Tentative {a.attempt_number}")
            lines.append(f"- Cause proposée : {a.diagnosis.root_cause}")
            lines.append(f"- Correction tentée : {a.fix.description}")
            lines.append(f"- Résultat : {'succès du retest' if a.retest_passed else 'échec du retest'}")
        lines.append(
            "\nToutes les causes ci-dessus ont été écartées ou n'ont pas résolu le "
            "problème. Intervention humaine requise avant de poursuivre."
        )
        return "\n".join(lines)


"""
uvi_agent_extension.py — Intègre AgentLoop / IssueDiagnosisLoop
(agent_loop.py) comme capacité optionnelle de IAAgent (Uvi_08-1-2.py).

Ce module ne modifie PAS IAAgent en place dans le fichier principal : il
lui GREFFE une méthode `autonomous_fix` via monkey-patch de classe
(`attach_autonomous_capabilities`), appelé une seule fois au chargement du
script principal. Choix délibéré plutôt qu'un copier-coller de la méthode
dans Uvi_08-1-2.py, pour que ce module reste testable et remplaçable
indépendamment du fichier principal (2700+ lignes).

CE QUE `generate_fn` DOIT ÊTRE
================================================================================
agent_loop.py délègue volontairement la génération de texte à l'appelant
(voir sa docstring). Ce module NE fournit AUCUN generate_fn par défaut --
brancher un modèle réel (Shion via ShionServingClient, ou un LLM externe)
est un choix de l'appelant qui dépasse le périmètre de cette intégration.
`generate_fn` doit être une fonction `str -> str` : reçoit un prompt,
retourne du texte contenant soit un bloc ```python``` (en réalité ici du
CODE SOURCE UVI, pas du Python -- voir code_pattern_extractor plus bas),
soit un appel d'outil <|tool_call|>{...}<|/tool_call|>.

Exemple minimal avec le client Shion déjà présent dans Uvi_08-1-2.py :

    def generate_fn(prompt: str) -> str:
        resp = engine.shion_client.complete(prompt)
        return resp.text  # à adapter à la vraie signature de ShionServingClient

Exemple avec l'API Claude (nécessite le SDK anthropic et une clé API,
NON gérés par ce module) :

    import anthropic
    client = anthropic.Anthropic()
    def generate_fn(prompt: str) -> str:
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
================================================================================
"""


_UVI_CODE_PATTERN = re.compile(r"```uvi\n(.*?)```", re.DOTALL)


def _default_code_extractor(text: str) -> Optional[str]:
    """Reconnaît un bloc ```uvi ... ``` plutôt que ```python``` (le code
    généré est du UVI, pas du Python -- réutiliser le motif ```python```
    par défaut de agent_loop.py aurait été trompeur)."""
    m = _UVI_CODE_PATTERN.search(text)
    return m.group(1).strip() if m else None


def _default_verify_fn(execution_result):
    """Politique de vérification par défaut : l'objectif est considéré
    atteint si l'exécution n'a déclenché aucun trap. C'est une politique
    MINIMALE (voir agent_loop.py : la vérification d'un objectif réel doit
    être fournie par l'appelant) -- suffisante pour "corrige ce script
    jusqu'à ce qu'il s'exécute sans erreur", pas pour "corrige ce script
    jusqu'à ce qu'il produise le bon résultat", qui nécessite un verify_fn
    dédié comparant execution_result.stdout/variables à un résultat attendu."""
    if isinstance(execution_result, ExecutionResult):
        if execution_result.success:
            return True, "Exécution terminée sans erreur."
        return False, execution_result.stderr
    if isinstance(execution_result, ToolExecutionResult):
        if execution_result.success:
            return True, "Outil exécuté avec succès."
        return False, execution_result.error or "Échec de l'outil."
    return False, "Résultat d'exécution non reconnu."


def _default_decide_next_step(step, steps) -> NextStepDecision:
    """Politique de décision par défaut : corriger tant qu'il reste des
    itérations (le garde-fou max_iterations de AgentGuardrails arrête la
    boucle de toute façon), s'arrêter seulement si trois échecs
    consécutifs SANS qu'IssueDiagnosisLoop n'ait rien pu résoudre --
    évite de boucler indéfiniment sur une action qui échoue toujours de
    la même façon quand IssueDiagnosisLoop n'est pas configuré."""
    if step.verification_passed:
        return NextStepDecision.STOP
    if len(steps) >= 3:
        last_three = steps[-3:]
        if all(not s.verification_passed for s in last_three) and all(
            s.issue_diagnosis is None for s in last_three
        ):
            return NextStepDecision.STOP
    return NextStepDecision.FIX


def build_uvi_agent_loop(
    parser,
    vfs=None,
    shion_ask_fn: Optional[Callable[[str], str]] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    max_iterations: int = 8,
    max_wall_clock_seconds: Optional[float] = 120.0,
    max_diagnosis_attempts: int = 3,
) -> AgentLoop:
    """Construit un AgentLoop pré-câblé pour corriger/faire évoluer du code
    source UVI, avec IssueDiagnosisLoop activé (voir agent_loop.py PARTIE 2)
    pour qu'un trap runtime déclenche un diagnostic structuré plutôt qu'une
    simple nouvelle itération générique."""
    code_executor = UVICodeExecutor(
        parser=parser, vfs=vfs, shion_ask_fn=shion_ask_fn, input_fn=input_fn,
    )
    issue_loop = IssueDiagnosisLoop(
        code_executor=code_executor, max_diagnosis_attempts=max_diagnosis_attempts,
    )
    guardrails = AgentGuardrails(
        max_iterations=max_iterations, max_wall_clock_seconds=max_wall_clock_seconds,
    )
    return AgentLoop(
        guardrails=guardrails,
        code_executor=code_executor,
        code_pattern_extractor=_default_code_extractor,
        issue_diagnosis_loop=issue_loop,
    )


def attach_autonomous_capabilities(iaagent_cls):
    """Greffe IAAgent.autonomous_fix sur la classe passée (IAAgent). Appelé
    une fois depuis Uvi_08-1-2.py après la définition de la classe."""

    def autonomous_fix(
        self,
        goal_description: str,
        generate_fn: Callable[[str], str],
        initial_snippet: str = "",
        max_iterations: int = 8,
        verify_fn=None,
    ) -> AgentRunResult:
        """Boucle autonome observer/générer/exécuter/vérifier appliquée à
        un extrait de code UVI, en s'appuyant sur la VM (uvi_vm.py) comme
        exécuteur -- jamais le tree-walker (pas d'accès à self.engine
        pendant une correction non supervisée, pour ne pas faire fuiter un
        script en cours de génération vers l'état "production" de l'IDE
        avant qu'il n'ait été vérifié)."""
        loop = build_uvi_agent_loop(
            parser=self.engine.parser, vfs=self.vfs, max_iterations=max_iterations,
        )

        history: List[str] = [initial_snippet] if initial_snippet else []

        def observe_fn(steps):
            if not steps:
                return f"Code actuel :\n{history[-1] if history else '(vide)'}"
            last = steps[-1]
            detail = ""
            if isinstance(last.execution_result, ExecutionResult):
                detail = last.execution_result.stderr or last.execution_result.stdout
            return (
                f"Code actuel :\n{history[-1] if history else '(vide)'}\n"
                f"Dernier résultat : {'succès' if last.verification_passed else 'échec'} "
                f"({detail})"
            )

        result = loop.run(
            goal_description=goal_description,
            observe_fn=observe_fn,
            generate_fn=generate_fn,
            verify_fn=verify_fn or _default_verify_fn,
            decide_next_step=_default_decide_next_step,
        )
        self.speak_and_log(
            f"IA : boucle autonome terminée — {result.stopped_reason} "
            f"({'réussi' if result.succeeded else 'échoué'}, {len(result.steps)} étape(s)).",
            "sys" if result.succeeded else "err",
        )
        return result

    iaagent_cls.autonomous_fix = autonomous_fix
    return iaagent_cls

# ==============================================================================
# FIN DES MODULES FUSIONNÉS — reprise du fichier principal original.
HAS_VM = True  # tout est désormais dans ce même fichier, plus de dépendance externe.
# ==============================================================================

try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

try:
    # ShionServingClient (shion/serving.py) est un client HTTP stdlib-only
    # (urllib + json, comme DeepSeekClient ci-dessus -- AUCUNE dépendance
    # à installer). C'est un choix architectural délibéré du
    # paquet `shion`, pas un hasard : shion/__init__.py n'importe RIEN de
    # lourd au chargement (pas de numpy au niveau paquet), précisément pour
    # que ce try/except réussisse même sur un client minimal (Raspberry Pi)
    # qui n'a que besoin de PARLER à un serveur Shion distant, pas d'en
    # faire tourner un. numpy n'est requis que par les sous-modules
    # d'inférence (shion.model, shion.memory, shion.engine), importés
    # PARESSEUSEMENT par serving.py uniquement si le repli local (aucun
    # serveur HTTP joignable) est effectivement emprunté -- voir
    # shion/serving.py pour le détail. On isole quand même cet import ici,
    # comme pour pyttsx3 ci-dessus : si le paquet `shion` est absent du
    # dépôt (copie partielle, etc.), SHION.ASK devient indisponible
    # proprement plutôt que de faire planter le démarrage d'UVI entier.
    from shion.serving import ShionServingClient
    HAS_SHION_NATIVE = True
except ImportError:
    HAS_SHION_NATIVE = False

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

SAFE_COMPARATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

MAX_LOOP_ITERATIONS = 10000
MAX_CALL_DEPTH = 50
MAX_FUNCTION_CALLS = 5000
MAX_DISPLAY_LENGTH = 300
MAX_CONSOLE_LINES = 2000
MAX_STRING_LENGTH = 10000
MAX_MEMORY_BYTES = 500000
MAX_ARRAY_SIZE = 1000

# URL du serveur vLLM distant servant Shion, lue depuis la variable
# d'environnement SHION_SERVER_URL (repli sur localhost:8000 si absente —
# pratique en dev, mais à définir explicitement en déploiement réel, ex :
#   SHION_SERVER_URL="http://192.168.1.50:8000" python Uvi_08-1.py
# Lue une seule fois au chargement du module : changer la variable d'env en
# cours d'exécution n'aura pas d'effet tant qu'UVI n'est pas relancé (pas de
# reconfiguration en cours de script pour l'instant, pas d'instruction
# SHION.CONNECT) — voir serving.py pour ce qui doit tourner côté serveur
# (vLLM API OpenAI-compatible).
SHION_SERVER_URL = os.environ.get("SHION_SERVER_URL", "http://localhost:8000")
SHION_MODEL_NAME = "shion"
SHION_REQUEST_TIMEOUT_S = 60  # aligné sur le timeout par défaut de ShionServingClient
# Sentinelle stockée dans la variable de destination pendant qu'une requête
# SHION.ASK est encore en vol (mode non-bloquant, voir _exec_shion) — permet
# de distinguer "en cours" d'une absence de variable ou d'un résultat vide.
SHION_PENDING = "<SHION_PENDING>"

# =============================================================================
# BASCULE DE BACKEND SHION -- Shion natif par défaut, DeepSeek en secours
# =============================================================================
# HISTORIQUE (pour comprendre pourquoi cette bascule existe encore) :
# jusqu'à cette session, "Shion" (le modèle maison, censé être servi par
# shion/serving.py) n'existait pas dans ce dépôt -- son développement était
# bloqué, et SHION.ASK était donc mort dans les faits partout où
# shion.serving n'était pas installable. DeepSeek-R1-Distill-Qwen-7B via un
# serveur vLLM distant servait de remplacement temporaire.
#
# ÉTAT ACTUEL : le paquet `shion/` (voir shion/__init__.py et
# docs/SHION_ARCHITECTURE.md) est désormais une implémentation réelle et
# fonctionnelle -- moteur bare-metal numpy, poids quantifiés INT4/ternaires,
# chargement mémoire paresseux, attention par fenêtre glissante, entraînée
# et testée (shion/model.py, gradient-check inclus). Le défaut redevient
# donc "shion", pour la raison même du projet Shion : tourner en LOCAL, sans
# serveur GPU à administrer, sur un poste modeste -- exactement le profil
# de la majorité des postes qui lancent cet IDE. ShionServingClient
# (shion/serving.py) essaie d'abord un serveur HTTP dédié (SHION_SERVER_URL,
# pour un usage partagé), et bascule automatiquement sur le moteur
# embarqué EN PROCESSUS s'il est injoignable -- zéro serveur à démarrer
# pour un usage solo (voir shion/serving.py pour le détail).
#
# DeepSeek reste disponible et complet (serveur vLLM distant, bien plus
# capable que Shion-Nano) pour qui a un GPU et veut la meilleure qualité de
# réponse : SHION_BACKEND=deepseek python uvi_core.py -- zéro ligne de code
# à toucher, comme conçu dès l'origine de cette bascule.
#
# ATTENTION à une conséquence non évidente, VALABLE DANS LES DEUX SENS :
# HAS_SHION vaut True dès qu'UN backend utilisable existe, que ce soit
# `shion` (toujours présent : c'est un paquet de ce dépôt, pas une
# dépendance externe optionnelle) ou DeepSeek (toujours "utilisable" au
# sens où le client se construit, même s'il ne pourra pas joindre de
# serveur). HAS_SHION signifie "SHION.ASK est câblé", pas "une requête va
# forcément aboutir" -- toutes les gardes existantes
# (`if not HAS_SHION or self.shion_client is None`) restent correctes, parce
# qu'elles testent AUSSI la présence du client.
SHION_BACKEND = os.environ.get("SHION_BACKEND", "shion").strip().lower()

# Nom EXACT attendu par vLLM dans le champ "model" de la requête
# OpenAI-compatible. vLLM dérive ce nom du chemin passé à --model, SAUF si
# --served-model-name est fourni, auquel cas c'est ce dernier qui fait foi.
# Piège vérifié en documentation avant d'écrire ce code : une valeur qui ne
# correspond pas exactement à ce que sert l'instance renvoie un HTTP 404
# ("The model `X` does not exist."), pas un 400 -- c'est l'erreur n°1 au
# premier branchement, et elle est traduite explicitement dans
# DeepSeekClient._traduire_http ci-dessous plutôt que laissée cryptique.
# Vérifier la valeur réelle servie avec :  curl http://<serveur>:8000/v1/models
DEEPSEEK_MODEL_NAME = os.environ.get(
    "DEEPSEEK_MODEL_NAME", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"
)

# vLLM n'exige une clé QUE si le serveur a été lancé avec --api-key (ou
# VLLM_API_KEY). Sans cette option, l'en-tête Authorization est purement
# ignoré -- donc en envoyer un systématiquement est sans risque et évite
# deux chemins de code. Un piège réel existe cependant : envoyer
# littéralement la chaîne "None" (ce que produit un f-string sur une
# variable non définie) est REJETÉ par le vérificateur de jeton de vLLM.
# D'où un défaut non vide et jamais None.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "uvi-local") or "uvi-local"

# Recommandations OFFICIELLES DeepSeek pour les distillations R1 (model card
# "Usage Recommendations"), suivies telles quelles plutôt que devinées :
#   - température 0.5-0.7, 0.6 recommandé -- EN DESSOUS, le modèle part en
#     répétitions sans fin ou en sortie incohérente. Ne PAS mettre 0.0 en
#     croyant "rendre le modèle déterministe" : c'est le mode de défaillance
#     documenté n°1 de cette famille.
#   - top_p 0.95 (valeur des benchmarks officiels).
#   - PAS de message system : DeepSeek demande explicitement de tout mettre
#     dans le message user. C'est pour ça que _construire_messages ci-dessous
#     n'émet jamais de role "system", contrairement à ce qu'on écrirait par
#     réflexe avec l'API OpenAI.
def _lire_temperature_deepseek() -> float:
    """BUGFIX : la valeur lue depuis DEEPSEEK_TEMPERATURE n'était bornée
    NULLE PART au chargement du module -- seul le panneau PARAMÈTRES de
    l'IDE (appliquer_parametres) refusait <0.4, un chemin totalement
    différent qui ne protège ni le client construit au démarrage par
    _build_shion_client(), ni un DeepSeekClient() instancié directement
    (scripts, tests). Un DEEPSEEK_TEMPERATURE=0 dans l'environnement -- le
    réflexe naturel pour "rendre le modèle déterministe" -- déclenchait
    donc sans avertissement le mode de défaillance documenté n°1 de cette
    famille de modèles (répétitions sans fin), exactement ce que les
    commentaires ci-dessus et docs/SERVEUR_DEEPSEEK.md §6 mettent en garde de ne pas
    faire. Clampée ici, au point de lecture, pour que TOUTE construction
    du client en soit protégée, pas seulement celle qui passe par l'IDE."""
    valeur = float(os.environ.get("DEEPSEEK_TEMPERATURE", "0.6"))
    if valeur < 0.5:
        valeur = 0.5
    elif valeur > 0.7:
        valeur = 0.7
    return valeur


DEEPSEEK_TEMPERATURE = _lire_temperature_deepseek()
DEEPSEEK_TOP_P = float(os.environ.get("DEEPSEEK_TOP_P", "0.95"))

# Budget de génération. Généreux VOLONTAIREMENT : un modèle de raisonnement
# consomme d'abord son budget en chaîne de pensée (<think>...</think>) AVANT
# d'écrire la réponse finale. Un max_tokens serré ne produit pas une réponse
# courte, il produit une réponse TRONQUÉE (finish_reason="length") où la
# partie utile n'a jamais été atteinte -- détecté et signalé explicitement
# dans complete() plutôt que rendu comme une réponse vide inexpliquée.
DEEPSEEK_MAX_TOKENS = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "8192"))

# Timeout nettement plus large que SHION_REQUEST_TIMEOUT_S (60s) : un 7B de
# raisonnement qui génère plusieurs milliers de tokens de <think> sur un GPU
# modeste dépasse la minute sans être en panne pour autant. Un timeout trop
# court transformerait une lenteur normale en fausse erreur réseau.
DEEPSEEK_REQUEST_TIMEOUT_S = int(os.environ.get("DEEPSEEK_REQUEST_TIMEOUT_S", "180"))

# =============================================================================
# NIVEAUX D'ABONNEMENT -- Shion-flash (Pro) / Shion complet (Plus)
# =============================================================================
# Actif UNIQUEMENT quand uvi_auth.is_configured() (SUPABASE_URL/ANON_KEY
# renseignées -- voir docs/COMPTES_ABONNEMENTS.md). Tant que ce n'est pas le
# cas, _exec_shion garde son comportement historique non-gaté, piloté par
# SHION_BACKEND/DEEPSEEK_MODEL_NAME ci-dessus -- développement local
# inchangé. Une fois configuré : "pro" -> flash (DeepSeek 1,5B, rapide,
# local), "plus" -> complet (DeepSeek 7B, meilleure qualité, local) ; "free"
# ou pas de session -> SHION.ASK refusé (voir ERROR_HINTS SHION_*).
SHION_FLASH_SERVER_URL = os.environ.get("SHION_FLASH_SERVER_URL", "http://localhost:8000")
SHION_FLASH_MODEL_NAME = os.environ.get("SHION_FLASH_MODEL_NAME", "deepseek-r1-distill-qwen-1.5b")
SHION_FULL_SERVER_URL = os.environ.get("SHION_FULL_SERVER_URL", "http://localhost:8001")
SHION_FULL_MODEL_NAME = os.environ.get("SHION_FULL_MODEL_NAME", "deepseek-r1-distill-qwen-7b")

# HAS_SHION conserve son NOM et son SENS pour tout le reste du fichier
# ("SHION.ASK est utilisable ici"), seule sa provenance change.
HAS_SHION = HAS_SHION_NATIVE if SHION_BACKEND == "shion" else True


class LLMClientError(Exception):
    """Racine commune des erreurs de TOUS les clients de modèle de langage
    (SHION natif, DeepSeek/vLLM, et tout futur backend).

    POURQUOI cette racine : un seul `except LLMClientError`, dans
    _eval_expr_or_eris comme dans l'onglet de discussion, couvre tous les
    backends sans avoir à les connaître individuellement -- un futur
    backend n'a qu'à dériver de cette classe pour être couvert
    automatiquement, sans toucher aux points d'appel existants."""


class DeepSeekClientError(LLMClientError):
    """Erreur remontée par DeepSeekClient.complete() -- message déjà
    traduit, actionnable, jamais une trace urllib/json brute."""


class DeepSeekUnreachableError(DeepSeekClientError):
    """Sous-cas de DeepSeekClientError signalant spécifiquement que
    l'ENDPOINT lui-même semble absent (connexion refusée, DNS, timeout de
    connexion, ou réponse qui n'est même pas du JSON -- typiquement une
    page d'erreur ngrok pour un tunnel expiré), PAS que le serveur vLLM a
    répondu par une erreur applicative (400/401/404 JSON, budget de
    tokens épuisé...). _DeepSeekAvecReplisLocal ne bascule sur le moteur
    Shion local QUE sur ce sous-cas précis : une vraie erreur applicative
    (ex. mauvaise clé d'API) doit rester visible telle quelle -- un repli
    silencieux la masquerait derrière une réponse Shion sans rapport,
    rendant le vrai problème plus dur à diagnostiquer, pas plus facile."""


class ShionClientError(LLMClientError):
    """Erreur remontée par le backend Shion NATIF (paquet externe `shion`,
    voir _ShionNativeAdapter juste avant _build_shion_client). Même
    contrat que DeepSeekClientError."""


class LLMClient:
    """Contrat minimal commun aux clients de modèle de langage du projet.

    POURQUOI : les différents backends (Shion natif, DeepSeek/vLLM, et
    tout futur backend) font la même chose (envoyer un prompt, tenir un
    historique, rendre du texte) via des protocoles différents. Sans base
    commune, ils dérivent silencieusement -- l'un gagne un reset_history,
    l'autre pas ; l'un renomme complete() en ask(), et _exec_shion casse
    six mois plus tard sans que rien ne l'ait signalé.

    Volontairement MINIMALE : deux méthodes, aucun état imposé, aucune
    logique HTTP factorisée ici. Factoriser le transport aurait été
    prématuré et faux -- les deux protocoles diffèrent sur TOUT (en-tête
    d'auth, forme du payload, emplacement du texte dans la réponse, forme
    du corps d'erreur). Ce qui doit être commun, c'est le CONTRAT vu par
    l'appelant, pas l'implémentation.

    Cohérent avec le choix architectural déjà assumé ailleurs dans ce
    fichier (VM et tree-walker dupliquent volontairement
    _resolve_module_path pour rester sans dépendance croisée) : on partage
    l'interface, pas les tripes."""

    #: Nom affichable du backend, utilisé dans les messages d'erreur et les
    #: en-têtes d'onglet de l'IDE pour que l'utilisateur sache TOUJOURS qui
    #: lui répond.
    nom_affichage = "LLM"

    def complete(self, prompt: str, remember: bool = True) -> str:
        """Envoie `prompt`, retourne le texte de la réponse.

        Lève une sous-classe de LLMClientError (jamais une exception
        urllib/json brute) sur tout échec. Si remember est vrai, la paire
        (question, réponse) est ajoutée à l'historique interne pour que le
        tour suivant garde le contexte."""
        raise NotImplementedError

    def reset_history(self) -> None:
        """Repart d'une conversation vierge sans reconstruire le client
        (l'IDE en a besoin pour son bouton "Nouvelle conversation")."""
        self._history = []


class DeepSeekClient(LLMClient):
    """Client HTTP pour un serveur vLLM servant DeepSeek-R1-Distill-Qwen-7B,
    via l'API OpenAI-compatible /v1/chat/completions.

    REMPLACEMENT TEMPORAIRE DE SHION (bascule d'août 2026) -- voir le bloc
    de commentaires de SHION_BACKEND plus haut pour le contexte complet et
    la procédure de retour à Shion. Cette classe n'est PAS un client
    "DeepSeek" au sens de l'API cloud de DeepSeek : elle parle à VOTRE
    serveur vLLM, sur vos poids, sans quota ni clé personnelle par
    utilisateur -- c'est exactement la propriété qui rendait SHION adapté à
    un usage partagé, et elle est conservée.

    Respecte le même contrat que tout LLMClient (.complete(prompt,
    remember=True) -> str, même historique interne _history, mêmes erreurs
    traduites en LLMClientError) -- voir aussi _ShionNativeAdapter, qui
    fait respecter ce même contrat au client Shion natif malgré l'absence
    de dépendance croisée entre uvi_core.py et le paquet `shion`.

    Point de format d'erreur à souligner, il a été vérifié en
    documentation avant d'écrire ce code plutôt que supposé par analogie :
    le corps d'erreur de vLLM est PLAT ({"object":"error","message":...,
    "type":"BadRequestError","code":400}), il n'imite PAS l'enveloppe
    imbriquée {"error":{...}} d'OpenAI. Certaines versions récentes ajoutent
    en plus une clé "error" imbriquée par compatibilité, sans retirer les
    champs plats. _traduire_http lit donc les DEUX formes -- écrire
    corps["error"]["message"] par réflexe OpenAI aurait donné un KeyError
    non traduit exactement au moment où l'utilisateur a besoin d'un message
    clair."""

    # Les distillations R1 émettent leur chaîne de pensée entre <think> et
    # </think> AVANT la réponse finale. Deux cas à couvrir, et un seul des
    # deux est évident :
    #   1. le bloc complet <think>...</think> apparaît dans content quand le
    #      serveur tourne SANS --reasoning-parser ;
    #   2. le gabarit de conversation du modèle injecte lui-même le <think>
    #      OUVRANT, si bien que la réponse ne contient que le </think>
    #      FERMANT. Un motif exigeant les deux balises ne matcherait alors
    #      rien et laisserait tout le raisonnement dans la réponse affichée.
    # Les deux cas sont traités dans _nettoyer_reponse ci-dessous.
    _THINK_COMPLET_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

    def __init__(
        self,
        base_url: str = SHION_SERVER_URL,
        model_name: str = DEEPSEEK_MODEL_NAME,
        api_key: str = DEEPSEEK_API_KEY,
        timeout_s: int = DEEPSEEK_REQUEST_TIMEOUT_S,
        temperature: float = DEEPSEEK_TEMPERATURE,
        max_tokens: int = DEEPSEEK_MAX_TOKENS,
    ):
        # rstrip('/') : SHION_SERVER_URL peut légitimement être écrite avec
        # ou sans slash final selon qui configure la variable
        # d'environnement ; sans ce nettoyage on construirait une URL
        # ".../v1//chat/completions" que certains proxys refusent.
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        # Instance, pas classe : nom_affichage était fige a "...-7B" avant
        # l'ajout du serveur local llama.cpp (docs/SERVEUR_LOCAL_CPU.md),
        # qui peut aussi servir le 1,5B -- l'onglet SHION affichait donc
        # "7B" meme quand DEEPSEEK_MODEL_NAME pointait vers le 1,5B.
        self.nom_affichage = "SHION (%s)" % model_name
        self.api_key = api_key or "uvi-local"
        self.timeout_s = timeout_s
        # BUGFIX : température/budget étaient auparavant lus UNIQUEMENT
        # depuis les constantes de module DEEPSEEK_TEMPERATURE/MAX_TOKENS à
        # l'intérieur de complete(), jamais depuis l'instance -- le panneau
        # PARAMÈTRES de l'IDE (appliquer_parametres) calculait bien de
        # nouvelles valeurs depuis ses champs de saisie, les VALIDAIT, puis
        # les jetait : le DeepSeekClient reconstruit n'en recevait aucune
        # trace. Cliquer "Appliquer" changeait donc l'URL/le modèle/la clé
        # mais PAS la température ni le budget de tokens, sans le moindre
        # message d'erreur pour le signaler. Attributs d'instance
        # désormais, avec les constantes de module comme défaut pour ne
        # rien changer aux appels existants qui ne les précisent pas.
        self.temperature = min(0.7, max(0.5, temperature))
        self.max_tokens = max_tokens
        self._history: List[Dict[str, str]] = []

    # -- helpers ---------------------------------------------------------

    @classmethod
    def _nettoyer_reponse(cls, texte: str) -> str:
        """Retire la chaîne de pensée d'une réponse de modèle R1.

        Pourquoi c'est indispensable et pas cosmétique : SHION.ASK stocke le
        résultat dans une VARIABLE UVI, que le script affiche ou concatène.
        Sans ce nettoyage, `SHION.ASK "capitale de la France" -> r` mettrait
        dans r plusieurs milliers de caractères de monologue interne avant
        le mot "Paris" -- et _safe_store tronquerait à MAX_STRING_LENGTH,
        donc le script recevrait du raisonnement tronqué et JAMAIS la
        réponse. C'est un bug de bout en bout, pas un défaut d'affichage.

        La vraie parade est côté serveur (--reasoning-parser deepseek_r1,
        qui déplace le raisonnement dans un champ reasoning_content séparé
        et laisse content propre). Ce nettoyage client reste néanmoins en
        place comme filet : le client ne contrôle pas comment le serveur a
        été lancé, et un serveur mal configuré doit dégrader proprement."""
        if not texte:
            return ""
        nettoye = cls._THINK_COMPLET_RE.sub("", texte)
        # Cas 2 : balise fermante orpheline (ouvrante injectée par le
        # gabarit côté serveur) -- tout ce qui précède est du raisonnement.
        if "</think>" in nettoye:
            nettoye = nettoye.split("</think>")[-1]
        # Cas 3 : balise OUVRANTE orpheline. Le modèle a été coupé en plein
        # raisonnement (budget max_tokens épuisé, finish_reason="length")
        # et n'a jamais écrit sa réponse. Trouvé en écrivant le test
        # test_budget_epuise_en_raisonnement_est_explicite : sans ce cas, on
        # renvoyait le monologue interne tronqué EN GUISE DE RÉPONSE, ce qui
        # est pire qu'une erreur -- le script continuait avec une valeur qui
        # a l'air d'une réponse sans en être une. Rendre "" fait remonter le
        # cas à complete(), qui le traduit en message explicite.
        elif "<think>" in nettoye:
            nettoye = nettoye.split("<think>")[0]
        return nettoye.strip()

    def _construire_messages(self, prompt: str) -> List[Dict[str, str]]:
        """Historique + tour courant, au format OpenAI.

        AUCUN message de rôle "system", volontairement : la model card
        DeepSeek demande explicitement de ne pas en utiliser sur les
        distillations R1 et de tout mettre dans le message utilisateur. Le
        réflexe OpenAI (« on met les consignes en system ») dégrade
        réellement ce modèle-ci, ce n'est pas une préférence de style.

        L'historique ne contient QUE des réponses déjà nettoyées de leur
        <think> (voir complete()), ce qui suit la consigne officielle
        DeepSeek : le raisonnement d'un tour précédent ne doit pas être
        renvoyé dans le contexte des tours suivants."""
        return self._history + [{"role": "user", "content": prompt}]

    @staticmethod
    def _traduire_http(code: int, corps_texte: str, model_name: str,
                        max_tokens: int = DEEPSEEK_MAX_TOKENS) -> str:
        """Transforme une réponse d'erreur HTTP vLLM en phrase actionnable
        en français.

        Prend `code` et `corps_texte` (déjà LUS par l'appelant) plutôt que
        l'objet HTTPError lui-même : ce dernier est un flux à usage
        unique, et complete() doit déjà avoir lu son corps une première
        fois pour décider si la réponse est du JSON exploitable ou une
        page d'erreur d'un proxy (voir DeepSeekUnreachableError) --
        relire ici lèverait silencieusement une chaîne vide.

        Lit les DEUX formes de corps d'erreur (plate historique, imbriquée
        récente) -- voir la docstring de la classe. `max_tokens` : celui de
        l'INSTANCE appelante (voir __init__), pas la constante de module --
        sinon la piste affichée sur une erreur 400 mentirait dès que le
        panneau PARAMÈTRES a changé le budget de tokens du client actif."""
        message_serveur = corps_texte
        try:
            corps = json.loads(corps_texte)
            if isinstance(corps, dict):
                # Forme PLATE d'abord (celle que vLLM produit depuis
                # toujours), puis forme imbriquée façon OpenAI (ajoutée par
                # compatibilité dans des versions récentes, sans retirer les
                # champs plats). Lire les deux dans cet ordre couvre toutes
                # les versions sans supposer laquelle tourne en face.
                imbrique = corps.get("error")
                if not isinstance(imbrique, dict):
                    imbrique = {}
                message_serveur = (
                    corps.get("message")
                    or imbrique.get("message")
                    or message_serveur
                )
        except (json.JSONDecodeError, ValueError):
            pass

        piste = ""
        if code == 404:
            # L'erreur n°1 au premier branchement, et la plus déroutante :
            # le serveur RÉPOND (donc le réseau va bien), mais refuse le nom
            # de modèle. On donne directement la commande de diagnostic.
            piste = (
                " -- le nom de modèle envoyé ('%s') ne correspond pas à ce que "
                "sert cette instance vLLM. Vérifiez la valeur exacte avec : "
                "curl %s/v1/models  puis définissez DEEPSEEK_MODEL_NAME en "
                "conséquence." % (model_name, SHION_SERVER_URL.rstrip("/"))
            )
        elif code == 401:
            piste = (
                " -- le serveur vLLM a été lancé avec --api-key et attend un "
                "jeton. Définissez DEEPSEEK_API_KEY avec la même valeur."
            )
        elif code == 400:
            piste = (
                " -- requête refusée (souvent : contexte dépassé). Réduisez la "
                "longueur du prompt, ou baissez le budget de tokens (actuel : "
                "%d)." % max_tokens
            )
        elif code >= 500:
            piste = " -- erreur interne du serveur vLLM, consultez ses logs."
        return "SHION/DeepSeek a échoué (HTTP %d) : %s%s" % (code, message_serveur, piste)

    # -- contrat LLMClient ------------------------------------------------

    def complete(self, prompt: str, remember: bool = True) -> str:
        """Même signature et même contrat que ShionServingClient.complete --
        c'est ce qui permet à _exec_shion, _shion_worker et run_script_vm de
        ne subir AUCUNE modification lors de la bascule de backend (vérifié
        explicitement, voir les tests de contrat)."""
        if not prompt.strip():
            raise DeepSeekClientError("Question vide envoyée à SHION.")

        payload = json.dumps({
            "model": self.model_name,
            "messages": self._construire_messages(prompt),
            "temperature": self.temperature,
            "top_p": DEEPSEEK_TOP_P,
            # max_tokens plutôt que max_completion_tokens : le second est le
            # nom moderne côté OpenAI, mais vLLM accepte encore les deux et
            # max_tokens reste le plus largement compris par les versions
            # déployées. À rebasculer si un jour vLLM retire l'alias.
            "max_tokens": self.max_tokens,
            # Pas de streaming : le contrat complete() -> str est synchrone
            # et rend une chaîne complète. Le non-blocage de l'UI est déjà
            # assuré en amont par le thread daemon de _shion_worker, pas par
            # un affichage progressif -- ne pas ajouter une seconde
            # mécanique concurrente pour le même problème.
            "stream": False,
        }).encode("utf-8")

        url = "%s/v1/chat/completions" % self.base_url
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        # Envoyé systématiquement : ignoré si le serveur tourne sans
        # --api-key, obligatoire sinon. Voir DEEPSEEK_API_KEY pour le piège
        # de la chaîne littérale "None".
        req.add_header("Authorization", "Bearer %s" % self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                corps = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            corps_texte = e.read().decode("utf-8", "replace")
            try:
                json.loads(corps_texte)
                reponse_est_json = True
            except (json.JSONDecodeError, ValueError):
                reponse_est_json = False
            if not reponse_est_json:
                # vLLM répond TOUJOURS en JSON, même en erreur (voir
                # _traduire_http) -- une réponse qui n'en est pas du tout
                # signale qu'on ne parle probablement pas à notre serveur
                # mais à un intermédiaire (page d'erreur ngrok pour un
                # tunnel expiré, proxy, etc.). DeepSeekUnreachableError,
                # pas une DeepSeekClientError ordinaire : voir sa
                # docstring pour pourquoi cette distinction compte
                # (bascule automatique sur Shion local dans
                # _DeepSeekAvecReplisLocal).
                raise DeepSeekUnreachableError(
                    "SHION/DeepSeek à %s a répondu par autre chose que du "
                    "JSON (HTTP %d) -- probablement pas notre serveur vLLM "
                    "(tunnel expiré, mauvaise URL ?)." % (self.base_url, e.code)
                )
            raise DeepSeekClientError(
                self._traduire_http(e.code, corps_texte, self.model_name, self.max_tokens)
            )
        except urllib.error.URLError as e:
            # BUGFIX : urlopen(..., timeout=...) enveloppe TOUJOURS un
            # dépassement de délai dans une URLError, jamais en TimeoutError
            # nue -- le `except TimeoutError` qui suivait n'était donc
            # jamais atteint, et l'utilisateur ne voyait jamais la piste
            # "augmentez DEEPSEEK_REQUEST_TIMEOUT_S", pourtant la plus utile
            # ici vu la lenteur normale d'un modèle de raisonnement.
            #
            # PAS DeepSeekUnreachableError pour le cas timeout : un serveur
            # qui a établi la connexion mais répond lentement est bien LÀ
            # (un modèle de raisonnement est juste lent) -- y voir une
            # absence de serveur et basculer sur Shion local produirait une
            # réponse dégradée alors qu'attendre plus longtemps aurait
            # suffi. Seule une connexion qui n'aboutit MÊME PAS (refusée,
            # DNS, etc.) signale une vraie absence d'endpoint.
            if isinstance(e.reason, TimeoutError):
                raise DeepSeekClientError(
                    "SHION/DeepSeek n'a pas répondu en %ds. Un modèle de "
                    "raisonnement est lent : augmentez DEEPSEEK_REQUEST_TIMEOUT_S "
                    "avant de conclure à une panne." % self.timeout_s
                )
            raise DeepSeekUnreachableError(
                "SHION/DeepSeek injoignable à %s (%s)."
                % (self.base_url, e.reason)
            )
        except json.JSONDecodeError:
            raise DeepSeekUnreachableError(
                "SHION/DeepSeek a renvoyé une réponse illisible (JSON invalide) "
                "-- %s sert-elle bien une API OpenAI-compatible ?" % self.base_url
            )

        choix = corps.get("choices") or []
        if not choix:
            raise DeepSeekClientError("SHION/DeepSeek n'a renvoyé aucune réponse.")
        message = choix[0].get("message") or {}

        # reasoning_content n'existe QUE si le serveur tourne avec
        # --reasoning-parser deepseek_r1. Quand il est là, content est déjà
        # propre et _nettoyer_reponse est un no-op ; quand il n'est pas là,
        # c'est _nettoyer_reponse qui fait le travail. Les deux
        # configurations serveur donnent donc le même résultat côté script,
        # ce qui évite qu'UVI ne marche que sur un serveur réglé « comme il
        # faut » sans le dire.
        texte = self._nettoyer_reponse(message.get("content") or "")

        if not texte:
            raison = choix[0].get("finish_reason", "")
            if raison == "length":
                raise DeepSeekClientError(
                    "SHION/DeepSeek a épuisé son budget de %d tokens en "
                    "raisonnement sans produire de réponse finale. Augmentez "
                    "le budget de tokens, ou posez une question plus simple."
                    % self.max_tokens
                )
            raise DeepSeekClientError(
                "SHION/DeepSeek n'a renvoyé aucun texte exploitable "
                "(finish_reason=%s)." % (raison or "inconnu")
            )

        if remember:
            self._history.append({"role": "user", "content": prompt})
            # On mémorise la réponse NETTOYÉE : consigne officielle DeepSeek,
            # le raisonnement d'un tour ne doit pas repartir dans le contexte
            # du tour suivant (il serait de toute façon ignoré, tout en
            # consommant du budget de contexte pour rien).
            self._history.append({"role": "assistant", "content": texte})
        return texte


class _ShionNativeAdapter(LLMClient):
    """Enveloppe ShionServingClient (paquet externe `shion`, voir
    shion/serving.py) pour le faire respecter le contrat LLMClientError.

    POURQUOI CET ADAPTATEUR EST NÉCESSAIRE (bug latent corrigé en même
    temps que le branchement réel de Shion) : `shion` est un paquet
    autonome qui ne peut PAS importer uvi_core.py (dépendance circulaire :
    uvi_core importe shion.serving, pas l'inverse) -- il ne connaît donc
    pas LLMClientError et lève ses propres exceptions. Or deux points
    d'appel du fichier interceptent SPÉCIFIQUEMENT `except LLMClientError`
    (voir sa docstring, écrite exactement pour ce cas) : _eval_expr_or_eris
    (!ERIS(...) en expression) et le worker de l'onglet de discussion.
    Sans cet adaptateur, une erreur réseau du backend Shion natif aurait
    traversé ces `except LLMClientError` sans être rattrapée -- traceback
    Python brut pour !ERIS(...), exactement ce que ce fichier s'interdit
    partout ailleurs. Tant que SHION_BACKEND valait "deepseek" par défaut
    et que `shion` n'existait pas dans ce dépôt, ce chemin n'était jamais
    exercé ; il l'est désormais (voir SHION_BACKEND ci-dessus)."""

    def __init__(self, client):
        self._client = client
        self.nom_affichage = getattr(client, "nom_affichage", "SHION (natif)")

    def complete(self, prompt: str, remember: bool = True) -> str:
        try:
            return self._client.complete(prompt, remember=remember)
        except LLMClientError:
            raise
        except Exception as e:
            raise ShionClientError(str(e)) from e

    def reset_history(self) -> None:
        reset = getattr(self._client, "reset_history", None)
        if callable(reset):
            reset()
        else:
            self._client._history = []


class _DeepSeekAvecReplisLocal(LLMClient):
    """Enveloppe DeepSeekClient : bascule automatiquement sur le moteur
    Shion NATIF (embarqué, voir _ShionNativeAdapter) si l'endpoint
    DeepSeek configuré est injoignable -- PAS s'il répond par une erreur
    applicative (mauvaise clé, contexte dépassé...), voir
    DeepSeekUnreachableError pour la distinction exacte.

    POURQUOI : SHION_SERVER_URL pointe typiquement vers un tunnel
    temporaire (ex. ngrok sur une session Colab, voir
    docs/SERVEUR_DEEPSEEK.md) qui n'existe que tant que son propriétaire
    la garde ouverte. Deux situations où ce tunnel n'est plus joignable
    sont NORMALES, pas des pannes à corriger : la session a fini par se
    déconnecter (inactivité, limite de durée), ou ce dossier a été
    partagé avec quelqu'un qui n'a jamais eu accès à cette session en
    premier lieu. Dans les deux cas, SHION.ASK doit rester UTILISABLE --
    dégradé, mais fonctionnel -- plutôt que de se contenter d'une erreur
    de connexion. Shion natif (paquet shion/, poids fournis dans le
    dépôt) ne dépend d'aucune infrastructure externe : c'est le repli qui
    marche pour absolument quiconque ouvre ce projet.

    Le repli est signalé EXPLICITEMENT dans le texte retourné (préfixe
    ⚠️) plutôt que masqué : substituer silencieusement une réponse d'un
    modèle 100x plus petit sans le dire serait trompeur, pas pratique."""

    def __init__(self, deepseek_client: "DeepSeekClient"):
        self._deepseek = deepseek_client
        self._repli: Optional[_ShionNativeAdapter] = None  # construit paresseusement
        self.nom_affichage = deepseek_client.nom_affichage

    def _obtenir_repli_local(self) -> Optional[_ShionNativeAdapter]:
        if self._repli is None:
            if not HAS_SHION_NATIVE:
                return None
            natif = ShionServingClient(model_name=SHION_MODEL_NAME)
            # Force le chemin LOCAL directement, sans que ShionServingClient
            # ne perde plusieurs secondes à tenter SA PROPRE URL de serveur
            # distant (SHION_SERVER_URL vaut ici l'endpoint DeepSeek qu'on
            # vient déjà de confirmer injoignable, pas un serveur Shion) --
            # même mécanisme de cooldown que celui utilisé en usage normal
            # dans ShionServingClient.complete(), détourné ici pour un
            # effet immédiat et permanent sur cette instance.
            natif._serveur_indisponible_jusqua = float("inf")
            self._repli = _ShionNativeAdapter(natif)
        return self._repli

    def complete(self, prompt: str, remember: bool = True) -> str:
        try:
            return self._deepseek.complete(prompt, remember=remember)
        except DeepSeekUnreachableError:
            pass  # repli ci-dessous, hors du bloc except (ne pas chaîner si LUI échoue aussi)

        repli = self._obtenir_repli_local()
        if repli is None:
            raise DeepSeekClientError(
                "SHION/DeepSeek injoignable à %s, ET le paquet shion/ (repli "
                "local) est absent de ce dossier." % self._deepseek.base_url
            )
        try:
            reponse = repli.complete(prompt, remember=remember)
        except LLMClientError as e:
            raise DeepSeekClientError(
                "SHION/DeepSeek injoignable à %s, ET le repli local Shion a "
                "aussi échoué : %s" % (self._deepseek.base_url, e)
            )
        return (
            "⚠️ DeepSeek injoignable (tunnel expiré ou URL obsolète) -- "
            "réponse de Shion local, un modèle très réduit, à titre de "
            "repli :\n\n" + reponse
        )

    def reset_history(self) -> None:
        self._deepseek.reset_history()
        if self._repli is not None:
            self._repli.reset_history()


def _build_shion_client():
    """Fabrique le client branché derrière SHION.ASK, selon SHION_BACKEND.

    Point de bascule UNIQUE du projet : c'est la seule fonction à regarder
    pour savoir qui répond réellement à SHION.ASK. UVIEvaluator.__init__ ne
    fait plus que l'appeler, donc rebrancher Shion ne demande de toucher ni
    à l'évaluateur, ni à _exec_shion, ni à l'IDE.

    Retourne None si aucun backend n'est utilisable -- toutes les gardes
    existantes testent déjà `self.shion_client is None`, donc ce cas dégrade
    proprement vers l'erreur SHION_UNAVAILABLE habituelle."""
    if SHION_BACKEND == "shion":
        if not HAS_SHION_NATIVE:
            return None
        natif = ShionServingClient(base_url=SHION_SERVER_URL, model_name=SHION_MODEL_NAME)
        return _ShionNativeAdapter(natif)
    deepseek = DeepSeekClient(base_url=SHION_SERVER_URL, model_name=DEEPSEEK_MODEL_NAME)
    return _DeepSeekAvecReplisLocal(deepseek)


# =============================================================================
# ÉTAPE 7.2 -- suggestions d'action attachées aux erreurs d'ENVIRONNEMENT
# =============================================================================
# Le principe existait déjà, mais à UN SEUL endroit : le message
# SHION_UNAVAILABLE dit quoi faire selon le backend actif. Toutes les
# autres erreurs de configuration laissaient l'utilisateur devant
# un constat sans issue ("DRAW.* indisponible") sans jamais dire ce qui
# manque ni comment le réparer. Cette table généralise le principe.
#
# CRITÈRE D'ENTRÉE, volontairement étroit : seules les erreurs
# d'ENVIRONNEMENT/CONFIGURATION (quelque chose n'est pas installé, pas
# branché, pas lancé) ont une suggestion. Les erreurs de LOGIQUE du script
# (UNKNOWN_FUNCTION, INDEX_OUT_OF_BOUNDS, WRONG_ARG_COUNT...) n'en ont
# délibérément AUCUNE : elles se corrigent en lisant son propre code, et y
# coller un conseil générique ("vérifiez votre code") ajouterait du bruit à
# chaque ligne d'erreur sans rien apprendre à personne. Une suggestion doit
# faire gagner du temps, sinon elle n'a rien à faire ici.
ERROR_HINTS: Dict[str, str] = {
    "IA_INLINE_UNAVAILABLE": (
        "→ Aucun backend SHION disponible ici (paquet shion/ absent ?). Avec "
        "SHION_BACKEND=deepseek : montez un serveur vLLM et exportez "
        "SHION_SERVER_URL dessus (voir docs/SERVEUR_DEEPSEEK.md)."
    ),
    "IA_INLINE_REQUEST_FAILED": (
        "→ Le service a répondu par une erreur. Si c'est SHION/DeepSeek : "
        "vérifiez que le serveur vLLM tourne (curl $SHION_SERVER_URL/v1/models)."
    ),
    "SHION_UNAVAILABLE": (
        "→ Backend local (défaut) : vérifiez que le paquet shion/ est présent "
        "à côté de uvi_core.py. Backend distant (SHION_BACKEND=deepseek) : "
        "lancez un serveur vLLM et exportez SHION_SERVER_URL "
        "(ex. http://192.168.1.50:8000) -- voir docs/SERVEUR_DEEPSEEK.md."
    ),
    "SHION_REQUEST_FAILED": (
        "→ Serveur injoignable ou en erreur. Testez : "
        "curl $SHION_SERVER_URL/v1/models — un HTTP 404 signifie que "
        "DEEPSEEK_MODEL_NAME ne correspond pas au modèle réellement servi."
    ),
    "SHION_BUSY": (
        "→ Une seule requête SHION.ASK à la fois. Attendez la réponse "
        "précédente avant d'en relancer une."
    ),
    "SHION_LOGIN_REQUIRED": (
        "→ Connecte-toi avec ton compte UVI (bouton Compte de la barre "
        "d'outils) pour utiliser SHION.ASK."
    ),
    "SHION_TIER_LOCKED": (
        "→ Ton abonnement actuel (Gratuit) n'inclut pas Shion. Passe à "
        "UVI Pro (Shion-flash) ou UVI Plus (Shion complet) sur le site UVI, "
        "section Tarifs."
    ),
    "DRAW_UNAVAILABLE": (
        "→ DRAW.* a besoin du canevas de l'IDE. Indisponible en exécution "
        "headless (CLI/tests) : c'est attendu, pas une panne."
    ),
    "KEY_UNAVAILABLE": (
        "→ KEY.* a besoin du clavier de l'IDE. Indisponible en exécution "
        "headless (CLI/tests) : c'est attendu, pas une panne."
    ),
    "PACKAGE_UNAVAILABLE": (
        "→ Le registre de paquets n'est pas attaché au VFS. Il est monté par "
        "UVIIDE ; hors IDE, attachez vfs.package_registry = "
        "UVIPackageRegistry(vfs.storage_dir)."
    ),
    "PATH_TRAVERSAL": (
        "→ Les noms de fichiers UVI sont relatifs au stockage local et ne "
        "peuvent pas en sortir. Retirez les '..' et les chemins absolus."
    ),
    "FILE_NOT_FOUND": (
        "→ Listez ce que contient réellement le stockage avec &list."
    ),
}


@dataclass
class UVIError:
    line: int
    column: int
    kind: str
    message: str

    def __str__(self):
        return f"[{self.kind}] ligne {self.line}, col {self.column} : {self.message}"


class Token:
    def __init__(self, type_, value, line=0, column=0):
        self.type = type_
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)})"


@dataclass
class ASTNode:
    line: int = 0

@dataclass
class SignatureNode(ASTNode):
    pass

@dataclass
class AssignNode(ASTNode):
    var: str = ""
    expr: str = ""
    is_call: bool = False
    call_node: Any = None

@dataclass
class PrintNode(ASTNode):
    expr: str = ""

@dataclass
class IfNode(ASTNode):
    condition: str = ""
    then_block: List[Any] = field(default_factory=list)
    else_block: List[Any] = field(default_factory=list)

@dataclass
class WhileNode(ASTNode):
    condition: str = ""
    body: List[Any] = field(default_factory=list)

@dataclass
class ForNode(ASTNode):
    count_expr: str = ""
    body: List[Any] = field(default_factory=list)

@dataclass
class StopNode(ASTNode):
    """@STOP : sortie anticipée -- arrête tout le programme immédiatement,
    depuis n'importe quel niveau d'imbrication (corps de IF/FOR/WHILE,
    fonction, routine, pipe |), pas un simple `break` de boucle. Aucun
    champ propre (rien à droite de @STOP) : line hérité de ASTNode suffit
    à identifier la ligne pour les messages de journal."""
    pass

@dataclass
class SleepNode(ASTNode):
    duration: float = 0.0

@dataclass
class InputNode(ASTNode):
    var: str = ""
    type_hint: str = ""  # "" (aucune conversion, comportement historique),
                          # "E" (entier) ou "R" (reel) -- voir ?E.nom/?R.nom

@dataclass
class DefNode(ASTNode):
    name: str = ""
    params: List[str] = field(default_factory=list)
    body: List[Any] = field(default_factory=list)

@dataclass
class CallNode(ASTNode):
    name: str = ""
    args: List[str] = field(default_factory=list)
    # dest : variable de destination de « CALL f(x) -> dest », None si
    # l'appel n'en a pas. Ajoute avec la correction de _parse_call ; aucun
    # appelant existant ne le lit, le champ existe pour que l'information
    # ne soit plus PERDUE -- c'est sa perte qui produisait le bug.
    dest: Optional[str] = None

@dataclass
class RoutineDefNode(ASTNode):
    name: str = ""
    body: List[Any] = field(default_factory=list)

@dataclass
class RoutineCallNode(ASTNode):
    name: str = ""

@dataclass
class TabNode(ASTNode):
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class StrNode(ASTNode):
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class DiskNode(ASTNode):
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class ShionNode(ASTNode):
    # operands[0] = prompt (str, déjà évalué/reconstruit comme les autres *_CMD)
    # operands[1] = nom de la variable de stockage du résultat (str brut, non évalué)
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class DrawNode(ASTNode):
    # Même forme que TabNode/StrNode/DiskNode : cmd = sous-commande
    # DRAW.* lexée telle quelle (ex. "DRAW.RECT"), operands = liste de
    # chaînes séparées par '->', non évaluées à ce stade -- l'évaluation
    # (via evaluate()/compile_expr) a lieu au niveau _exec_draw /
    # _compile_draw_node, jamais dans le parser.
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class PackageNode(ASTNode):
    # PACKAGE.PUBLISH nom -> fichier -> version : même forme que
    # DrawNode (cmd = sous-commande lexée telle quelle, operands =
    # liste de chaînes séparées par '->', non évaluées à ce stade --
    # l'évaluation a lieu au niveau _exec_package / _compile_package_node,
    # jamais dans le parser). Système de paquets (ajout de cette
    # session, distinct du système de modules -- voir UVIPackageRegistry).
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class KeyNode(ASTNode):
    # KEY.DOWN/KEY.PRESSED "touche" -> dest : même schéma que STR.LEN
    # (operands[1] est le NOM de la variable destination, pas une cible
    # d'assignation classique) -- PAS comme DrawNode où tous les
    # opérandes sont des valeurs. cmd = "KEY.DOWN" ou "KEY.PRESSED".
    cmd: str = ""
    operands: List[str] = field(default_factory=list)

@dataclass
class ImportNode(ASTNode):
    filename: str = ""

@dataclass
class ModuleNode(ASTNode):
    """MODULE fichier -> alias : charge fichier dans un espace de noms
    ISOLÉ nommé alias (par opposition à ImportNode, inclusion textuelle
    dans le scope courant). Sémantique différente d'IMPORT -> nœud AST
    et mécanisme d'exécution dédiés, jamais réutilisation d'ImportNode/
    _exec_import (cf. principe déjà appliqué à routine vs CALL_FUNC :
    nouvel opcode/nœud dédié dès que la sémantique diffère, même
    légèrement)."""
    filename: str = ""
    alias: str = ""  # si vide, dérivé du nom de fichier (voir _exec_module)

@dataclass
class ExportNode(ASTNode):
    """EXPORT nom : à l'intérieur d'un fichier MODULE, marque nom
    (variable ou routine déjà défini(e) au moment de l'EXPORT) comme
    visible depuis l'extérieur via alias::nom. Sans EXPORT, tout reste
    privé au module -- ce n'est PAS une simple visibilité, ça encode
    l'API publique volontaire du module."""
    name: str = ""

@dataclass
class LearnNode(ASTNode):
    error: str = ""
    correction: str = ""

@dataclass
class SnippetNode(ASTNode):
    pattern: str = ""

@dataclass
class PipeNode(ASTNode):
    nodes: List[Any] = field(default_factory=list)

@dataclass
class UnknownNode(ASTNode):
    raw: str = ""


# Référence rapide du langage UVI, à préfixer aux prompts SHION qui
# portent sur du code UVI (voir UVIIDE._construire_contexte_langage_uvi).
#
# POURQUOI CE TEXTE EXISTE : DeepSeek/llama.cpp n'a jamais vu UVI à
# l'entraînement -- c'est un langage maison. Sans référence, le modèle
# devine la syntaxe par analogie avec des langages connus (Python, JS...),
# ce qui produit des explications plausibles mais fausses sur les points
# où UVI diverge (ex. `~ ... -> ... || ...` pour IF/ELSE n'existe dans
# aucun langage grand public). Ce texte est dérivé DIRECTEMENT de
# Lexer.TOKEN_SPEC et de _parse_command/_parse_def/etc. ci-dessus -- à
# tenir synchronisé si la grammaire change, plutôt que réécrit de mémoire.
UVI_REFERENCE_LANGAGE = """RÉFÉRENCE RAPIDE DU LANGAGE UVI (langage maison -- pas dans tes données
d'entraînement. Base-toi UNIQUEMENT sur cette référence et sur l'AST fourni
plus bas, jamais sur une analogie avec Python/JS/autre.)

Chaque ligne = une instruction. Un "corps" (après une flèche `->`) peut
contenir plusieurs instructions séparées par `|`.

  # texte                       commentaire (ignoré)
  var = expr                    affectation
  ! expr                        affiche expr
  ?nom                          lit une entrée utilisateur dans `nom`
  ?E.nom / ?R.nom                idem, convertie en entier / réel
  _N   (ex: _0.5)                 pause de N secondes -- instruction à part
                                   entière, pas un appel de fonction
  @STOP                            sortie anticipée : arrête TOUT le
                                    programme immédiatement, depuis
                                    n'importe où (même au fond d'un IF, d'une
                                    boucle ou d'une fonction) -- pas un
                                    `break` de boucle, un arrêt total
  ~ cond -> alors || sinon        IF/ELSE : si `cond` vraie -> bloc `alors`,
                                   sinon -> bloc après `||`
  % N -> corps                    FOR : répète `corps` N fois
  %% cond -> corps                 WHILE : répète `corps` tant que `cond`
  DEF nom(a, b) -> corps           définit une fonction ; le résultat se
                                    lit dans la variable `_retour`
  CALL nom(args)                    appelle une fonction
  CALL nom(args) -> dest             idem, stocke le résultat dans `dest`
  *nom                                appelle une routine (sans arguments)
  TAB nom                             crée un tableau
  TAB.PUSH/POP/GET/SET/DEL/LEN/PRINT/CREATE nom -> ...   opérations tableau
  STR.LEN/UPPER/LOWER/TRIM/SPLIT/JOIN/REPLACE/CONTAINS/
      STARTS/ENDS/SLICE/INDEX/NUM "texte" -> dest        opérations chaîne
  &save/&load/&list/&delete "fichier" -> ...             disque (VFS)
  SHION.ASK "prompt" -> var                              interroge SHION
  DRAW.CLEAR/RECT/CIRCLE/LINE/TEXT/SPRITE/COLOR/POLY ...  dessin (canevas)
  KEY.DOWN/KEY.PRESSED "touche" -> dest                   clavier (jeux)
  IMPORT "fichier"                 inclusion textuelle (même portée)
  MODULE "fichier" -> alias        chargement isolé, accès via alias::nom
  EXPORT nom                       rend `nom` public dans un fichier MODULE
  !LEARN "erreur" "correction"     enregistre une correction apprise
  !SNIPPET motif                   insère un modèle de code prédéfini

  Comparaisons à deux caractères : == != >= <=
  Chaînes entre guillemets doubles. Parenthèses/virgules réservées aux
  arguments de CALL/DEF.
"""


class Opcode:
    PUSH       = "PUSH"
    LOAD       = "LOAD"
    STORE      = "STORE"
    PRINT      = "PRINT"
    ADD        = "ADD"
    SUB        = "SUB"
    MUL        = "MUL"
    DIV        = "DIV"
    MOD        = "MOD"
    POW        = "POW"
    EQ         = "EQ"
    NEQ        = "NEQ"
    LT         = "LT"
    LTE        = "LTE"
    GT         = "GT"
    GTE        = "GTE"
    AND        = "AND"
    OR         = "OR"
    NOT        = "NOT"
    JUMP       = "JUMP"
    JUMP_IF    = "JUMP_IF"
    JUMP_ELSE  = "JUMP_ELSE"
    CALL       = "CALL"
    RET        = "RET"
    DEF        = "DEF"
    TAB_CREATE = "TAB_CREATE"
    TAB_PUSH   = "TAB_PUSH"
    TAB_POP    = "TAB_POP"
    TAB_GET    = "TAB_GET"
    TAB_SET    = "TAB_SET"
    TAB_DEL    = "TAB_DEL"
    TAB_LEN    = "TAB_LEN"
    TAB_PRINT  = "TAB_PRINT"
    STR_OP     = "STR_OP"
    SHION_ASK  = "SHION_ASK"
    DISK_SAVE  = "DISK_SAVE"
    DISK_LOAD  = "DISK_LOAD"
    DISK_LIST  = "DISK_LIST"
    DISK_DEL   = "DISK_DEL"
    INPUT      = "INPUT"
    SLEEP      = "SLEEP"
    IMPORT     = "IMPORT"
    # Ajout de cette session, pour que le désassembleur (flatten_bytecode_to_json)
    # ne fasse plus disparaître silencieusement MODULE/EXPORT -- IMPORT
    # ci-dessus a déjà ce défaut (jamais branché dans _compile_node
    # legacy avant cette session, cf. commentaire "BUG SILENCIEUX
    # CORRIGÉ" plus bas pour UnknownNode qui documente le même risque
    # général) ; ne pas reproduire ce trou pour un nœud nouvellement
    # introduit alors que le risque est maintenant connu. Ce
    # compilateur legacy ne sert QUE le désassembleur (jamais exécuté
    # en production, voir UVIIDE._export_bytecode) -- ces deux opcodes
    # servent uniquement à produire une représentation JSON lisible,
    # jamais interprétés par une VM réelle.
    MODULE_LOAD = "MODULE_LOAD"
    EXPORT_NAME = "EXPORT_NAME"
    HALT       = "HALT"
    LABEL      = "LABEL"
    JUMP_IF_FALSE = "JUMP_IF_FALSE"
    JUMP_ABS   = "JUMP_ABS"

@dataclass
class Instruction:
    opcode: str
    operands: List[Any] = field(default_factory=list)

    def to_dict(self):
        return {"opcode": self.opcode, "operands": self.operands}


class UVILexer:
    TOKEN_SPEC = [
        ('COMMENT',   r'#.*'),
        ('SIGNATURE', r'@UVI\b'),
        ('STOP_CMD',  r'@STOP\b'),
        ('IMPORT',    r'\bIMPORT\b'),
        ('MODULE',    r'\bMODULE\b'),
        ('EXPORT',    r'\bEXPORT\b'),
        ('PACKAGE_CMD', r'\bPACKAGE\.(PUBLISH)\b'),
        ('DEF',       r'\bDEF\b'),
        ('CALL',      r'\bCALL\b'),
        ('TAB_CMD',   r'\bTAB\.(PUSH|POP|GET|SET|DEL|LEN|PRINT|CREATE)\b|\bTAB\b'),
        ('STR_CMD',   r'\bSTR\.(LEN|UPPER|LOWER|TRIM|SPLIT|JOIN|REPLACE|CONTAINS|STARTS|ENDS|SLICE|INDEX|NUM)\b'),
        ('DISK_CMD',  r'&(save|load|list|delete)\b'),
        ('SHION_CMD', r'\bSHION\.(ASK)\b'),
        ('DRAW_CMD',  r'\bDRAW\.(CLEAR|RECT|CIRCLE|LINE|TEXT|SPRITE|COLOR|POLY)\b'),
        ('KEY_CMD',   r'\bKEY\.(DOWN|PRESSED)\b'),
        # CMP2 doit precede COMMAND (sinon '!=' perd son '!' au token
        # COMMAND avant meme d'atteindre ce point) et ASSIGN (sinon '==',
        # '>=', '<=' se lexent comme deux tokens ASSIGN/EXPR distincts,
        # rejoints avec un espace par _reconstruct_expr -- '= =' n'est pas
        # une expression Python valide, donc compile_expr() levait
        # systematiquement BytecodeError des qu'une condition ~/%%
        # utilisait un operateur de comparaison a deux caracteres. Type
        # dedie (pas 'ASSIGN') pour ne jamais etre capture par les split
        # sur ASSIGN qui detectent les affectations (ex. _parse_tokens
        # ligne ~3485) : 'res = a == b' doit rester une seule affectation,
        # pas se scinder sur le '==' interne.
        ('CMP2',      r'==|!=|>=|<='),
        ('COMMAND',   r'!LEARN\b|!SNIPPET\b|!'),
        ('ROUTINE',   r'\*[A-Za-z_][A-Za-z0-9_]*'),
        ('WHILE',     r'%%'),
        ('FOR',       r'%'),
        ('IF',        r'~'),
        ('INPUT',     r'\?'),
        ('SLEEP',     r'_[0-9.]+'),
        ('ARROW',     r'->'),
        ('ELSE_SEP',  r'\|\|'),
        ('PIPE',      r'\|'),
        ('ASSIGN',    r'='),
        ('LPAREN',    r'\('),
        ('RPAREN',    r'\)'),
        ('COMMA',     r','),
        ('STRING',    r'"[^"\\]*(?:\\.[^"\\]*)*"'),
        ('EXPR',      r'[^()|,\s"=]+'),
        ('SKIP',      r'[ \t]+'),
        ('NEWLINE',   r'\n'),
    ]

    def __init__(self):
        self.regex = re.compile('|'.join(f'(?P<{name}>{pattern})' for name, pattern in self.TOKEN_SPEC))

    def tokenize(self, code, line_hint=0):
        tokens = []
        for match in self.regex.finditer(code):
            kind = match.lastgroup
            value = match.group(kind)
            start = match.start()
            if kind in ('SKIP', 'COMMENT'):
                continue
            if kind == 'STRING':
                value = value[1:-1]
            tokens.append(Token(kind, value, line=line_hint, column=start))
        return tokens


class SafeEvalError(Exception):
    """Levée par SafeEval.eval_expr en cas d'échec d'évaluation (syntaxe
    invalide, opérande manquant, division par zéro, etc.).

    BUG SILENCIEUX CORRIGÉ : eval_expr retournait auparavant l'expression
    SOURCE telle quelle (une chaîne) en cas d'échec, au lieu de signaler
    l'erreur. Exemple concret : le script UVI
        x = 5 +
        y = SI x > 3 ALORS ...
    ne levait jamais d'erreur sur la ligne malformée -- x recevait
    littéralement la chaîne "5 + " au lieu d'un nombre, et la comparaison
    suivante (str > int) produisait soit un résultat incohérent soit une
    autre erreur bien plus loin dans le programme, sans lien évident avec
    la vraie cause. Le seul point d'appel qui exposait ce comportement au
    tree-walker sans le capturer était UVIEvaluator.evaluate() (cf. son
    dernier `return self.evaluator.eval_expr(...)`) -- corrigé pour
    attraper SafeEvalError et émettre UVIError(EXPR_EVAL_FAILED) au lieu
    de laisser filtrer une chaîne masquée en valeur."""
    pass


class SafeEval:
    def _replace_bools(self, s: str) -> str:
        parts = str(s).split('"')
        for i in range(0, len(parts), 2):
            parts[i] = parts[i].replace(" AND ", " and ").replace(" OR ", " or ").replace(" NOT ", " not ")
            if parts[i].startswith("NOT "):
                parts[i] = "not " + parts[i][4:]
        return '"'.join(parts)

    # E/R devant un littéral numérique collé (pas d'espace) : E2 -> 2,
    # R-2 -> -2, R1.5 -> 1.5. \b des deux côtés évite de mordre sur un
    # nom de variable plus long (Emma, Robert2 restent intacts) --
    # vérifié empiriquement avant intégration. Limite assumée et
    # documentée : une variable UVI nommée littéralement E5 ou R3 serait
    # à tort lue comme le littéral 5/3 -- inhérent à la syntaxe demandée
    # (préfixe collé à des chiffres), pas contournable sans un marqueur
    # supplémentaire.
    _NUM_PREFIX_RE = re.compile(r'\b([ER])(-?\d+(?:\.\d+)?)\b')

    def eval_expr(self, expr: str, variables: dict):
        expr_str = self._replace_bools(str(expr))
        expr_str = self._NUM_PREFIX_RE.sub(r'\2', expr_str)
        try:
            tree = ast.parse(expr_str, mode='eval')
            return self._eval(tree.body, variables)
        except Exception as e:
            # Avant : `return expr` -- avalait toute erreur d'évaluation en
            # renvoyant l'expression SOURCE comme si c'était sa valeur. Voir
            # SafeEvalError ci-dessus pour l'explication complète du bug et
            # de la correction. Le type d'exception d'origine est préservé
            # dans __cause__ (via `from e`) pour ne rien perdre côté
            # diagnostic si quelqu'un inspecte la trace complète.
            raise SafeEvalError(f"Échec d'évaluation de '{expr}' : {e}") from e

    def _eval(self, node, variables):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return variables.get(node.id, "")
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, variables)
            right = self._eval(node.right, variables)
            op = SAFE_OPERATORS.get(type(node.op))
            if op:
                if isinstance(node.op, ast.Pow):
                    # Même risque, même correctif que côté VM (voir
                    # UVIVirtualMachine.run, bloc BINOP/POW) -- vérifié
                    # empiriquement identique ici : 10**10000000 via ce
                    # chemin (SafeEval, utilisé par le tree-walker réel,
                    # celui de ▶ EXÉCUTER) bloque le processus sans lever
                    # la moindre exception, tout comme côté VM. Et c'est
                    # même le chemin le PLUS exposé des deux : c'est le
                    # moteur que les scripts réels (RPG, Pac-Man) utilisent
                    # en pratique, pas la VM bytecode. Seuil MAX_STRING_LENGTH
                    # (constante déjà utilisée pour la troncature de
                    # chaînes dans CE moteur spécifiquement, ligne ~4521
                    # et suivantes) plutôt que self.max_string_length
                    # (VM) : SafeEval n'a aucun état d'instance, pas
                    # d'attribut self à référencer.
                    if left != 0:
                        import math
                        chiffres_estimes = abs(right) * math.log10(abs(left))
                        if chiffres_estimes > MAX_STRING_LENGTH:
                            raise SafeEvalError(
                                f"Résultat de puissance trop grand (~{int(chiffres_estimes)} chiffres estimés)."
                            )
                return op(left, right)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, variables)
            for cmp_op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, variables)
                op = SAFE_COMPARATORS.get(type(cmp_op))
                if op and not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for val in node.values:
                    if not self._eval(val, variables):
                        return False
                return True
            if isinstance(node.op, ast.Or):
                for val in node.values:
                    if self._eval(val, variables):
                        return True
                return False
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return not self._eval(node.operand, variables)
            if isinstance(node.op, ast.USub):
                return -self._eval(node.operand, variables)
        raise ValueError(f"Expression non autorisée : {ast.dump(node)}")


def _safe_resolve(base_dir: str, filename: str) -> str:
    """Résout `filename` SOUS `base_dir` et lève UVIRuntimeTrap
    ("PATH_TRAVERSAL") si le chemin final sort de ce répertoire.

    POURQUOI (bug réel, pas théorique) : VFSManager construisait ses
    chemins par un simple os.path.join(self.storage_dir, filename) et
    n'a jamais vérifié le résultat. &SAVE("../../../.ssh/authorized_keys",
    "...") écrivait donc HORS du bac à sable, avec les droits du process
    UVI, depuis un simple script utilisateur -- le VFS n'était un "bac à
    sable" que par convention de nommage, jamais par contrôle. Même
    faiblesse sur UVIPackageRegistry.resolve_std/resolve_pkg, où `nom`
    vient d'un MODULE "std:..." écrit par l'utilisateur.

    Une SEULE fonction pour les 6 sites concernés : dupliquer la
    vérification aurait garanti qu'un des sites finisse par diverger
    (c'est exactement ce qui s'est passé pour get_path, oublié alors que
    read_file/write_file existaient déjà).

    Trois refus, dans cet ordre :
      1. chemin absolu ou lettre de lecteur Windows ("C:..."), qui
         ignorerait purement et simplement base_dir dans os.path.join
         (piège classique : os.path.join("box", "/etc/passwd") renvoie
         "/etc/passwd", pas "box/etc/passwd") ;
      2. base et cible sur des volumes différents (commonpath lève
         ValueError) ;
      3. cible dont l'ancêtre commun avec base n'est pas base -> le
         "../" a bien fait sortir du répertoire.

    realpath (et non abspath) des DEUX côtés : abspath normalise "..",
    ce qui suffit contre "../../etc", mais ne suit pas les liens
    symboliques -- un lien "evasion" créé dans UVI_Storage et pointant
    vers /etc laisserait passer "evasion/passwd". Les deux côtés sont
    résolus pour rester cohérent si storage_dir est lui-même sous un
    lien (cas courant : /tmp -> /private/tmp sur macOS), sinon la
    comparaison échouerait sur des chemins pourtant légitimes.

    LIMITES ASSUMÉES (dites explicitement plutôt que sous-entendues,
    comme le fait déjà UVIDebugPause pour ses propres limites) :
      - TOCTOU : la vérification et l'open() qui suit ne sont pas
        atomiques. Un lien symbolique créé entre les deux par un autre
        process passerait au travers. Fermer ce trou demanderait
        O_NOFOLLOW / openat(), non portable et hors périmètre ici.
      - Liens PHYSIQUES (hard links) : indétectables par cette méthode,
        un hard link créé dans storage_dir vers un fichier extérieur
        reste lisible. Aucune parade côté chemin.
      - Ceci protège la HIÉRARCHIE, pas les droits : tout ce qui est
        réellement sous storage_dir reste entièrement accessible à un
        script UVI, y compris .stdlib/ et .packages/.
    """
    if filename is None or not str(filename).strip():
        raise UVIRuntimeTrap("PATH_TRAVERSAL", "Nom de fichier vide.")
    nom = str(filename)

    if os.path.isabs(nom) or os.path.splitdrive(nom)[0]:
        raise UVIRuntimeTrap(
            "PATH_TRAVERSAL",
            "Chemin absolu refusé : '%s'. Les noms de fichiers UVI sont "
            "toujours relatifs au stockage local." % nom,
        )

    base_abs = os.path.realpath(os.path.abspath(base_dir))
    cible = os.path.realpath(os.path.abspath(os.path.join(base_abs, nom)))

    try:
        commun = os.path.commonpath([base_abs, cible])
    except ValueError:
        # Volumes différents sous Windows : par construction, jamais
        # sous base_dir. commonpath lève plutôt que de renvoyer "".
        raise UVIRuntimeTrap(
            "PATH_TRAVERSAL",
            "Chemin hors du stockage UVI (volume différent) : '%s'." % nom,
        )

    if commun != base_abs:
        raise UVIRuntimeTrap(
            "PATH_TRAVERSAL",
            "Chemin hors du stockage UVI : '%s' sortirait de '%s'."
            % (nom, base_abs),
        )
    return cible


class VFSManager:
    def __init__(self, storage_dir="UVI_Storage"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)

    def list_files(self):
        # Non modifié : listdir(storage_dir) ne prend aucun nom venant de
        # l'utilisateur, il n'y a donc rien à confiner ici.
        if os.path.exists(self.storage_dir):
            return sorted(os.listdir(self.storage_dir))
        return []

    def read_file(self, filename):
        if not filename:
            return None
        path = _safe_resolve(self.storage_dir, filename)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return None

    def write_file(self, filename, content):
        if not filename:
            return
        path = _safe_resolve(self.storage_dir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def delete_file(self, filename):
        if not filename:
            return False
        path = _safe_resolve(self.storage_dir, filename)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def get_path(self, filename):
        # get_path est le point de fuite le PLUS large des quatre : il ne
        # lit ni n'écrit lui-même, mais rend un chemin que l'appelant
        # ouvrira (cf. _resolve_module_path des DEUX moteurs, qui fait
        # ensuite open() dessus). Le confiner ici protège donc aussi
        # get_abs_path() et exists(), qui délèguent tous deux à get_path
        # sans reconstruire de chemin -- une seule garde pour trois
        # méthodes, plutôt qu'une garde par méthode qu'on oublierait.
        # Changement de contrat mineur et volontaire : le chemin renvoyé
        # est désormais ABSOLU et normalisé, là où il était relatif à
        # storage_dir. Vérifié : les 8 appelants existants n'en font que
        # os.path.exists() / open(), jamais une comparaison de chaîne ni
        # un affichage, donc aucun n'est cassé par ce changement.
        if not filename:
            return None
        return _safe_resolve(self.storage_dir, filename)

    def get_abs_path(self, filename):
        p = self.get_path(filename)
        return os.path.abspath(p) if p else None

    def exists(self, filename):
        p = self.get_path(filename)
        return os.path.exists(p) if p else False


class UVIPackageRegistry:
    """Système de paquets UVI (ajout de cette session, distinct du
    système de modules déjà en place -- MODULE/EXPORT/alias::nom restent
    inchangés, ce sont les résolveurs de chemin _resolve_module_path
    (VM et tree-walker) qui reconnaissent les préfixes std:/pkg: dans
    le nom de fichier passé à MODULE et délèguent la résolution ici,
    plutôt qu'une nouvelle syntaxe UVI).

    Deux répertoires séparés du storage_dir utilisateur (VFSManager),
    pour ne jamais risquer qu'un fichier utilisateur nommé comme un
    paquet standard ou publié n'entre en collision avec lui :
    - <storage_dir>/.stdlib/<nom>.uvi : bibliothèque standard, écrite
      UNE FOIS par l'infrastructure (voir ensure_stdlib_installed),
      jamais par un script utilisateur. Résolu par 'std:<nom>'.
    - <storage_dir>/.packages/<nom>/<version>.uvi : contenu FIGÉ d'un
      paquet publié via PACKAGE.PUBLISH -- une copie indépendante du
      fichier source au moment de la publication, pas juste un
      pointeur (publier une version doit être immuable : si le fichier
      source change après coup, la version déjà publiée ne doit pas
      changer avec lui). Résolu par 'pkg:<nom>' (dernière version) ou
      'pkg:<nom>@<version>' (version explicite).
    - <storage_dir>/.packages/registry.json : index {nom: {version:
      chemin_relatif}} des versions disponibles par paquet.

    Toute erreur de lecture/écriture du JSON (fichier absent au premier
    lancement, JSON corrompu) est absorbée silencieusement vers un
    registre vide plutôt que de faire planter tout chargement de
    module -- un registre cassé ne doit dégrader QUE la résolution
    pkg:, jamais empêcher un MODULE "fichier.uvi" -> alias ordinaire de
    fonctionner."""

    def __init__(self, storage_dir: str):
        self.stdlib_dir = os.path.join(storage_dir, ".stdlib")
        self.packages_dir = os.path.join(storage_dir, ".packages")
        self.registry_path = os.path.join(self.packages_dir, "registry.json")

    def _load_registry(self) -> dict:
        try:
            with open(self.registry_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_registry(self, registry: dict):
        os.makedirs(self.packages_dir, exist_ok=True)
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)

    def resolve_std(self, nom: str) -> Optional[str]:
        """'std:nom' -> chemin de <stdlib_dir>/<nom>.uvi, ou None si le
        fichier de bibliothèque standard n'existe pas sous ce nom."""
        if not nom:
            return None
        # `nom` vient directement du script utilisateur (MODULE "std:<nom>"
        # -> _resolve_module_path -> ici), donc un MODULE
        # "std:../../../../etc/passwd" faisait lire n'importe quel fichier
        # du disque en le présentant comme un module de bibliothèque
        # standard. Même garde que VFSManager, même fonction.
        chemin = _safe_resolve(self.stdlib_dir, f"{nom}.uvi")
        return chemin if os.path.exists(chemin) else None

    def resolve_pkg(self, nom: str, version: Optional[str] = None) -> Optional[str]:
        """'pkg:nom' (version=None -> la plus récente publiée, tri
        lexicographique des clés de version -- suffisant pour ce
        système minimal, pas de comparaison sémantique type SemVer) ou
        'pkg:nom@version' (version explicite). Retourne None si le
        paquet ou la version demandée n'existe pas dans le registre."""
        if not nom:
            return None
        registry = self._load_registry()
        versions = registry.get(nom)
        if not versions or not isinstance(versions, dict):
            return None
        if version is None:
            version = sorted(versions.keys())[-1]
        chemin_relatif = versions.get(version)
        if not chemin_relatif:
            return None
        # On confine chemin_relatif (valeur lue dans registry.json) et non
        # `nom` : c'est le chemin réellement ouvert. registry.json est un
        # fichier ORDINAIRE du stockage, éditable par un script UVI via
        # &SAVE -- il est donc une entrée non fiable au même titre que le
        # nom du module, et le vrai point de contrôle est ici, pas sur la
        # clé du dictionnaire.
        chemin_abs = _safe_resolve(self.packages_dir, chemin_relatif)
        return chemin_abs if os.path.exists(chemin_abs) else None

    def publish(self, nom: str, source_path: str, version: str) -> None:
        """Copie le CONTENU de source_path (déjà résolu et vérifié
        existant par l'appelant, voir _exec_package_publish côté
        tree-walker / PACKAGE_PUBLISH côté VM) vers
        .packages/<nom>/<version>.uvi, et met à jour registry.json.
        Republier la MÊME (nom, version) écrase silencieusement le
        contenu précédent -- pas une erreur : un script qui republie
        pour corriger une erreur de contenu doit pouvoir le faire sans
        détour, cohérent avec le rechargement idempotent déjà en place
        pour MODULE (même alias, même fichier physique -> no-op)."""
        # BUG CORRIGÉ : publish() était le DERNIER trou de traversée de
        # chemin du projet. resolve_std/resolve_pkg avaient été confinés,
        # publish non -- alors que `nom` et `version` viennent tous deux
        # directement du script utilisateur via PACKAGE.PUBLISH. Un
        # PACKAGE.PUBLISH "../../../.ssh/authorized_keys" écrivait donc
        # hors du bac, en ÉCRITURE, avec les droits du process.
        #
        # Exactement le même oubli que get_path à l'étape 1 : deux méthodes
        # sur trois protégées, la troisième passée à travers parce qu'elle
        # « n'avait pas l'air » d'un accès disque. D'où le confinement
        # systématique de tout chemin construit, sans exception.
        with open(source_path, 'r', encoding='utf-8') as f:
            content = f.read()
        chemin_relatif = f"{nom}/{version}.uvi"
        chemin_abs = _safe_resolve(self.packages_dir, chemin_relatif)
        # makedirs APRÈS la vérification, jamais avant : créer le
        # répertoire d'abord laisserait des dossiers vides en dehors du bac
        # même quand l'écriture est refusée.
        os.makedirs(os.path.dirname(chemin_abs), exist_ok=True)
        with open(chemin_abs, 'w', encoding='utf-8') as f:
            f.write(content)
        registry = self._load_registry()
        registry.setdefault(nom, {})[version] = chemin_relatif
        self._save_registry(registry)

    def ensure_stdlib_installed(self, sources: Dict[str, str]) -> None:
        """sources = {nom: contenu_uvi}. Écrit chaque fichier de
        bibliothèque standard SEULEMENT s'il n'existe pas déjà --
        n'écrase JAMAIS un fichier .stdlib existant, pour ne pas
        perdre une modification locale que quelqu'un aurait faite
        directement dans ce répertoire (cas limite mais peu coûteux à
        respecter). Appelée au besoin (voir UVIIDE.__init__ ou premier
        MODULE "std:..." rencontré), pas systématiquement à chaque
        session."""
        os.makedirs(self.stdlib_dir, exist_ok=True)
        for nom, contenu in sources.items():
            chemin = os.path.join(self.stdlib_dir, f"{nom}.uvi")
            if not os.path.exists(chemin):
                with open(chemin, 'w', encoding='utf-8') as f:
                    f.write(contenu)


class UVIParser:
    def __init__(self, lexer: UVILexer):
        self.lexer = lexer

    def _split_tokens_by_type(self, tokens, token_type):
        sublists, current = [], []
        for t in tokens:
            if t.type == token_type:
                sublists.append(current)
                current = []
            else:
                current.append(t)
        sublists.append(current)
        return sublists

    def _split_on_first_arrow(self, tokens):
        """Comme _split_tokens_by_type(tokens, 'ARROW') mais ne coupe que
        sur le PREMIER ARROW -- utilisé par _parse_if/_parse_while/
        _parse_for pour séparer condition/corps.

        BUG CORRIGÉ : ces trois méthodes appelaient auparavant
        _split_tokens_by_type(tokens[1:], 'ARROW') puis ne gardaient que
        parts[1] comme corps. Ça marche tant qu'aucune commande à
        opérandes multiples (TAB.GET, TAB.SET, STR.SLICE, STR.REPLACE...)
        n'apparaît dans le corps -- ces commandes utilisent ELLES-MÊMES
        '->' comme séparateur d'opérandes (ex. 'TAB.GET t -> i -> item'
        a DEUX ARROW internes). Sur une ligne comme
            %%i < n -> TAB.GET t -> i -> item | !item | i = i + 1
        le split sur TOUS les ARROW produit 4 parts, pas 2 : le corps
        réel ('TAB.GET t -> i -> item | !item | i = i + 1') se retrouve
        tronqué à 'TAB.GET t' seul -- '!item' et 'i = i + 1' disparaissent
        SILENCIEUSEMENT (aucune erreur, aucun UnknownNode). Dans une
        boucle %%, ça prive le corps de l'incrément de la variable de
        condition -> TRAP INFINITE_LOOP après 10000 tours au lieu de
        l'erreur de logique évidente qu'un vrai message aurait donnée.
        Vérifié avec le même symptôme sur IfNode (then_block tronqué).

        La condition elle-même ne contient jamais de '->' (seul point
        d'origine du token ARROW dans TOKEN_SPEC : le séparateur
        condition/corps ou un opérande interne de commande -- jamais une
        valeur de donnée), donc couper uniquement sur le premier ARROW
        pour isoler la condition est sûr, et laisser le corps intact
        permet à chaque sous-bloc (après split PIPE) de se re-parser lui-
        même via _parse_tokens -- qui route déjà TAB.GET/TAB.SET/etc. vers
        _parse_tab, lequel refait SON PROPRE split ARROW correctement sur
        ses seuls tokens à lui."""
        for i, t in enumerate(tokens):
            if t.type == 'ARROW':
                return [tokens[:i], tokens[i + 1:]]
        return [tokens]

    def _reconstruct_expr(self, tokens):
        parts = []
        for t in tokens:
            if t.type == 'STRING':
                parts.append(f'"{t.value}"')
            else:
                parts.append(t.value)
        return " ".join(parts).strip()

    def _parse_args_from_tokens(self, tokens):
        args, current, depth = [], [], 0
        for t in tokens:
            if t.type == 'LPAREN':
                depth += 1
                current.append(t)
            elif t.type == 'RPAREN':
                depth -= 1
                current.append(t)
            elif t.type == 'COMMA' and depth == 0:
                args.append(self._reconstruct_expr(current))
                current = []
            else:
                current.append(t)
        if current:
            args.append(self._reconstruct_expr(current))
        return args

    def parse_line(self, line: str, line_num: int) -> Optional[ASTNode]:
        clean = line.strip()
        if not clean or clean.startswith('#'):
            return None
        tokens = self.lexer.tokenize(clean, line_hint=line_num)
        if not tokens:
            return None
        return self._parse_tokens(tokens, line_num)

    def _parse_tokens(self, tokens, line_num=0) -> ASTNode:
        if not tokens:
            return None
        first = tokens[0]

        if first.type == 'SIGNATURE':
            return SignatureNode(line=line_num)

        if first.type == 'STOP_CMD':
            return StopNode(line=line_num)

        if first.type == 'IMPORT':
            expr = self._reconstruct_expr(tokens[1:])
            return ImportNode(line=line_num, filename=expr)

        if first.type == 'MODULE':
            # MODULE fichier -> alias : même découpe syntaxique que
            # DRAW.* (_parse_draw ci-dessous), séparé sur ARROW. alias
            # est optionnel -- si absent, ModuleNode.alias reste "" et
            # _exec_module/_compile_module_node dérivent l'alias du nom
            # de fichier (sans extension).
            parts = self._split_tokens_by_type(tokens[1:], 'ARROW')
            fichier_expr = self._reconstruct_expr(parts[0])
            alias = self._reconstruct_expr(parts[1]).strip() if len(parts) >= 2 else ""
            return ModuleNode(line=line_num, filename=fichier_expr, alias=alias)

        if first.type == 'EXPORT':
            # EXPORT nom : nom est un identifiant brut (nom de variable
            # ou de routine déjà défini(e) dans CE fichier au moment de
            # l'EXPORT), jamais évalué -- même convention que ops[0] de
            # DRAW.SPRITE/DRAW.POLY (nom de tableau non évalué).
            nom = self._reconstruct_expr(tokens[1:]).strip()
            return ExportNode(line=line_num, name=nom)

        if first.type == 'DEF':
            return self._parse_def(tokens, line_num)

        if first.type == 'CALL':
            return self._parse_call(tokens[1:], line_num)

        if first.type == 'ROUTINE':
            return self._parse_routine(tokens, line_num)

        if first.type == 'TAB_CMD':
            return self._parse_tab(tokens, line_num)

        if first.type == 'STR_CMD':
            return self._parse_str(tokens, line_num)

        if first.type == 'SHION_CMD':
            return self._parse_shion(tokens, line_num)

        if first.type == 'DRAW_CMD':
            return self._parse_draw(tokens, line_num)

        if first.type == 'PACKAGE_CMD':
            return self._parse_package(tokens, line_num)

        if first.type == 'KEY_CMD':
            return self._parse_key(tokens, line_num)

        if first.type == 'COMMAND':
            return self._parse_command(tokens, line_num)

        if first.type == 'IF':
            return self._parse_if(tokens, line_num)

        if first.type == 'WHILE':
            return self._parse_while(tokens, line_num)

        if first.type == 'FOR':
            return self._parse_for(tokens, line_num)

        if first.type == 'SLEEP':
            try:
                duration = float(first.value[1:])
            except Exception:
                duration = 0.0
            return SleepNode(line=line_num, duration=duration)

        if first.type == 'INPUT':
            raw_name = self._reconstruct_expr(tokens[1:]).strip()
            # ?E.nom / ?R.nom : le point est OBLIGATOIRE pour lever
            # l'ambiguite avec un nom qui commence naturellement par E/R
            # (Emma, Robert...) -- verifie empiriquement avant ce
            # correctif que sans le point, un simple prefixe de lettre
            # confondrait "Emma" avec "E" + "mma" typee entiere.
            m = re.match(r'^([ER])\.(.+)$', raw_name)
            if m:
                return InputNode(line=line_num, var=m.group(2), type_hint=m.group(1))
            return InputNode(line=line_num, var=raw_name)

        if first.type == 'DISK_CMD':
            return self._parse_disk(tokens, line_num)

        assign_split = self._split_tokens_by_type(tokens, 'ASSIGN')
        if len(assign_split) >= 2:
            var_name = self._reconstruct_expr(assign_split[0]).strip()
            expr_tokens = assign_split[1]
            if expr_tokens and expr_tokens[0].type == 'CALL':
                call_node = self._parse_call(expr_tokens[1:], line_num)
                return AssignNode(line=line_num, var=var_name, expr="", is_call=True, call_node=call_node)
            expr = self._reconstruct_expr(expr_tokens)
            return AssignNode(line=line_num, var=var_name, expr=expr)

        pipe_split = self._split_tokens_by_type(tokens, 'PIPE')
        if len(pipe_split) > 1:
            nodes = [self._parse_tokens(block, line_num) for block in pipe_split if block]
            return PipeNode(line=line_num, nodes=[n for n in nodes if n])

        expr_str = self._reconstruct_expr(tokens)
        return UnknownNode(line=line_num, raw=expr_str)

    def _parse_def(self, tokens, line_num):
        rest = tokens[1:]
        # _split_on_first_arrow, PAS _split_tokens_by_type : même bug que
        # celui déjà documenté et corrigé pour IF/WHILE/FOR (voir la
        # docstring de _split_on_first_arrow) mais jamais appliqué ICI --
        # un corps de DEF contenant lui-même une commande à ARROW interne
        # (CALL f(x) -> dest, TAB.GET/SET, STR.SLICE...) voyait son corps
        # tronqué juste après le premier ARROW rencontré, silencieusement.
        # Trouvé par tests/test_compiler.py (suite tree-walker/VM de ce
        # projet), reproduit avec « DEF F(n) -> CALL G(n) -> tmp » (perdait
        # sa destination) contre « *R -> CALL G(n) -> tmp » (routine,
        # correcte, déjà sur _split_on_first_arrow).
        arrow_split = self._split_on_first_arrow(rest)
        if len(arrow_split) < 2:
            return UnknownNode(line=line_num, raw="DEF invalid")
        body_tokens = arrow_split[1]
        head_str = self._reconstruct_expr(arrow_split[0])
        if '(' not in head_str or ')' not in head_str:
            return UnknownNode(line=line_num, raw="DEF invalid head")
        nom_func, reste = head_str.split('(', 1)
        params_str = reste.rstrip(')')
        params = [p.strip() for p in params_str.split(',') if p.strip()]
        pipe_split = self._split_tokens_by_type(body_tokens, 'PIPE')
        body_nodes = [self._parse_tokens(block, line_num) for block in pipe_split if block]
        body_nodes = [n for n in body_nodes if n]
        return DefNode(line=line_num, name=nom_func.strip(), params=params, body=body_nodes)

    def _parse_call(self, tokens, line_num):
        """CALL nom(args)  ou  CALL nom(args) -> destination

        BUG CORRIGÉ (trouvé en écrivant test_bug_connu_call_avec_destination).
        L'ancienne version faisait :

            nom_func, args_str = rest_str.split('(', 1)   # "5) -> r"
            args_str = args_str.rstrip(') ')              # ne retire RIEN

        rstrip(') ') ne retire que des caractères de FIN. Sur
        « CALL CARRE(5) -> r », la chaîne se termine par « r », donc rien
        n'était retiré et l'argument devenait littéralement « 5 ) -> r ».
        Résultat : toute fonction dont on voulait récupérer le résultat
        échouait, dans les DEUX moteurs (le parser est partagé), avec un
        message d'erreur incompréhensible parlant d'une parenthèse.
        « CALL CARRE(5) » sans destination fonctionnait, ce qui rendait le
        bug d'autant plus déroutant.

        Même famille que le bug _split_on_first_arrow déjà documenté : une
        flèche non séparée AVANT l'analyse de ce qui la précède.

        Correction : on découpe sur la DERNIÈRE parenthèse fermante, pas
        sur une supposition de fin de ligne. La dernière et non la
        première, pour que « CALL F(G(2)) » garde son argument imbriqué
        intact -- rsplit sur ')' aurait cassé les appels imbriqués.

        La destination est conservée dans CallNode.dest quand elle existe
        (None sinon), sans changer le sens des attributs name/args
        existants : aucun appelant actuel ne lit dest, donc rien ne
        casse."""
        rest_str = self._reconstruct_expr(tokens)
        if '(' not in rest_str or ')' not in rest_str:
            return UnknownNode(line=line_num, raw="CALL invalid")

        fin_args = rest_str.rfind(')')
        nom_func = rest_str[:rest_str.index('(')].strip()
        args_str = rest_str[rest_str.index('(') + 1:fin_args]
        suffixe = rest_str[fin_args + 1:].strip()

        destination = None
        if suffixe.startswith('->'):
            destination = suffixe[2:].strip() or None
        elif suffixe:
            # Quelque chose suit la parenthèse fermante sans être une
            # flèche : plutôt que de l'avaler en silence (le comportement
            # qui a produit ce bug), on le signale comme syntaxe inconnue.
            return UnknownNode(line=line_num, raw=rest_str)

        arg_tokens = self.lexer.tokenize(args_str, line_hint=line_num)
        raw_args = self._parse_args_from_tokens(arg_tokens)
        return CallNode(line=line_num, name=nom_func, args=raw_args, dest=destination)

    def _parse_routine(self, tokens, line_num):
        # *nom -> corps definit une routine ; *nom seul (sans ARROW) l'appelle.
        # Bug corrige (voir _split_on_first_arrow ci-dessus, meme famille) :
        # l'ancienne version testait la presence d'un ASSIGN dans le corps
        # pour distinguer definition/appel -- une routine sans aucune
        # assignation interne (ex. *saluer -> !"bonjour", ou tout corps
        # fait de DRAW.*/PRINT/IF seuls) n'a jamais d'ASSIGN et tombait
        # donc a tort dans le cas RoutineCallNode, avant meme d'avoir pu
        # etre definie -> UNKNOWN_ROUTINE au premier appel reel. Et quand
        # un ASSIGN existait bien dans le corps, _split_tokens_by_type sur
        # tout rest_tokens coupait au premier '=' de la ligne ENTIERE
        # (donc a l'interieur du corps lui-meme, ex. 'joueur_x = joueur_x
        # + 1' perdait son 'joueur_x =' initial) -> UNKNOWN_SYNTAX sur le
        # corps tronque. _split_on_first_arrow evite les deux : il ne
        # coupe que sur le ARROW qui separe le nom du corps, jamais sur
        # un '=' ou un ARROW interne au corps lui-meme (ex. TAB.GET a
        # l'interieur d'une routine, qui a ses propres '->').
        name = tokens[0].value[1:]
        rest_tokens = tokens[1:]
        parts = self._split_on_first_arrow(rest_tokens)
        if len(parts) < 2:
            return RoutineCallNode(line=line_num, name=name)
        body_tokens = parts[1]
        pipe_split = self._split_tokens_by_type(body_tokens, 'PIPE')
        body_nodes = [self._parse_tokens(block, line_num) for block in pipe_split if block]
        body_nodes = [n for n in body_nodes if n]
        return RoutineDefNode(line=line_num, name=name, body=body_nodes)

    def _parse_if(self, tokens, line_num):
        parts = self._split_on_first_arrow(tokens[1:])
        if len(parts) < 2:
            return UnknownNode(line=line_num, raw="IF invalid")
        condition = self._reconstruct_expr(parts[0])
        rest_block = parts[1]
        else_split = self._split_tokens_by_type(rest_block, 'ELSE_SEP')
        then_pipe = self._split_tokens_by_type(else_split[0], 'PIPE')
        then_nodes = [self._parse_tokens(b, line_num) for b in then_pipe if b]
        then_nodes = [n for n in then_nodes if n]
        else_nodes = []
        if len(else_split) > 1:
            else_pipe = self._split_tokens_by_type(else_split[1], 'PIPE')
            else_nodes = [self._parse_tokens(b, line_num) for b in else_pipe if b]
            else_nodes = [n for n in else_nodes if n]
        return IfNode(line=line_num, condition=condition, then_block=then_nodes, else_block=else_nodes)

    def _parse_while(self, tokens, line_num):
        parts = self._split_on_first_arrow(tokens[1:])
        if len(parts) < 2:
            return UnknownNode(line=line_num, raw="WHILE invalid")
        condition = self._reconstruct_expr(parts[0])
        body_pipe = self._split_tokens_by_type(parts[1], 'PIPE')
        body_nodes = [self._parse_tokens(b, line_num) for b in body_pipe if b]
        body_nodes = [n for n in body_nodes if n]
        return WhileNode(line=line_num, condition=condition, body=body_nodes)

    def _parse_for(self, tokens, line_num):
        parts = self._split_on_first_arrow(tokens[1:])
        if len(parts) < 2:
            return UnknownNode(line=line_num, raw="FOR invalid")
        count_expr = self._reconstruct_expr(parts[0])
        body_pipe = self._split_tokens_by_type(parts[1], 'PIPE')
        body_nodes = [self._parse_tokens(b, line_num) for b in body_pipe if b]
        body_nodes = [n for n in body_nodes if n]
        return ForNode(line=line_num, count_expr=count_expr, body=body_nodes)

    def _parse_tab(self, tokens, line_num):
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        if cmd == 'TAB':
            assign_split = self._split_tokens_by_type(tokens, 'ASSIGN')
            if len(assign_split) >= 2:
                nom = self._reconstruct_expr(assign_split[0]).replace('TAB', '').strip()
                vals_str = self._reconstruct_expr(assign_split[1])
                return TabNode(line=line_num, cmd='TAB_INIT', operands=[nom, vals_str])
            nom = self._reconstruct_expr(rest_tokens).strip()
            return TabNode(line=line_num, cmd='TAB_EMPTY', operands=[nom])
        return TabNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_str(self, tokens, line_num):
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        return StrNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_shion(self, tokens, line_num):
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        return ShionNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_draw(self, tokens, line_num):
        # Même schéma que _parse_str : opérandes séparés par '->',
        # reconstruits en chaînes non évaluées. L'évaluation numérique
        # (coordonnées, taille) est déléguée à _exec_draw /
        # _compile_draw_node, exactement comme STR.* délègue à ev()/
        # compile_val() -- le parser ne fait ici QUE de la découpe
        # syntaxique, jamais d'interprétation de valeur.
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        return DrawNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_package(self, tokens, line_num):
        # PACKAGE.PUBLISH nom -> fichier -> version : même découpe
        # syntaxique que _parse_draw (voir juste au-dessus) -- le
        # parser ne fait QUE de la découpe, l'évaluation des opérandes
        # est déléguée à _exec_package_publish / _compile_package_node.
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        return PackageNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_key(self, tokens, line_num):
        # KEY.DOWN "touche" -> dest : même découpe syntaxique que
        # _parse_draw/_parse_str -- operands[0] = expression de touche
        # (typiquement une STRING littérale, mais peut être une variable :
        # évaluée normalement par evaluate()/compile_expr, PAS traitée
        # comme un nom brut -- contrairement à operands[1], le nom de
        # variable destination, qui lui reste une chaîne non évaluée,
        # exactement comme le 2e opérande de STR.LEN.
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        operands = [self._reconstruct_expr(p) for p in parts]
        return KeyNode(line=line_num, cmd=cmd, operands=operands)

    def _parse_disk(self, tokens, line_num):
        cmd = tokens[0].value
        rest_tokens = tokens[1:]
        parts = self._split_tokens_by_type(rest_tokens, 'ARROW')
        clean_parts = [self._reconstruct_expr(p) for p in parts if p]
        return DiskNode(line=line_num, cmd=cmd, operands=clean_parts)

    def _parse_command(self, tokens, line_num):
        cmd_type = tokens[0].value
        rest_expr = self._reconstruct_expr(tokens[1:])
        if cmd_type == '!' and rest_expr.startswith('SNIPPET '):
            return SnippetNode(line=line_num, pattern=rest_expr[8:].strip())
        if cmd_type == '!' and rest_expr.startswith('LEARN '):
            try:
                parts = rest_expr[6:].split('" "')
                err = parts[0].strip('"')
                corr = parts[1].strip('"')
                return LearnNode(line=line_num, error=err, correction=corr)
            except Exception:
                return UnknownNode(line=line_num, raw="LEARN invalid")
        if 'LEARN' in cmd_type:
            try:
                parts = rest_expr.split('" "')
                err = parts[0].strip('"')
                corr = parts[1].strip('"')
                return LearnNode(line=line_num, error=err, correction=corr)
            except Exception:
                return UnknownNode(line=line_num, raw="LEARN invalid")
        if 'SNIPPET' in cmd_type:
            return SnippetNode(line=line_num, pattern=rest_expr.strip('" '))
        return PrintNode(line=line_num, expr=rest_expr)

    def parse_program(self, lines: List[str]) -> List[ASTNode]:
        nodes = []
        for i, line in enumerate(lines):
            node = self.parse_line(line, i + 1)
            if node is not None:
                nodes.append(node)
        return nodes

    def ast_to_dict(self, node: ASTNode) -> dict:
        if node is None:
            return {}
        d = {"type": type(node).__name__, "line": node.line}
        if isinstance(node, AssignNode):
            d.update({"var": node.var, "expr": node.expr, "is_call": node.is_call})
            if node.call_node:
                d["call_node"] = self.ast_to_dict(node.call_node)
        elif isinstance(node, PrintNode):
            d["expr"] = node.expr
        elif isinstance(node, IfNode):
            d.update({"condition": node.condition,
                      "then_block": [self.ast_to_dict(n) for n in node.then_block],
                      "else_block": [self.ast_to_dict(n) for n in node.else_block]})
        elif isinstance(node, WhileNode):
            d.update({"condition": node.condition,
                      "body": [self.ast_to_dict(n) for n in node.body]})
        elif isinstance(node, ForNode):
            d.update({"count_expr": node.count_expr,
                      "body": [self.ast_to_dict(n) for n in node.body]})
        elif isinstance(node, DefNode):
            d.update({"name": node.name, "params": node.params,
                      "body": [self.ast_to_dict(n) for n in node.body]})
        elif isinstance(node, CallNode):
            d.update({"name": node.name, "args": node.args})
        elif isinstance(node, RoutineDefNode):
            d.update({"name": node.name,
                      "body": [self.ast_to_dict(n) for n in node.body]})
        elif isinstance(node, RoutineCallNode):
            d["name"] = node.name
        elif isinstance(node, TabNode):
            d.update({"cmd": node.cmd, "operands": node.operands})
        elif isinstance(node, StrNode):
            d.update({"cmd": node.cmd, "operands": node.operands})
        elif isinstance(node, DiskNode):
            d.update({"cmd": node.cmd, "operands": node.operands})
        elif isinstance(node, ShionNode):
            d.update({"cmd": node.cmd, "operands": node.operands})
        elif isinstance(node, ImportNode):
            d["filename"] = node.filename
        elif isinstance(node, ModuleNode):
            d.update({"filename": node.filename, "alias": node.alias})
        elif isinstance(node, ExportNode):
            d["name"] = node.name
        elif isinstance(node, SleepNode):
            d["duration"] = node.duration
        elif isinstance(node, InputNode):
            d["var"] = node.var
        elif isinstance(node, LearnNode):
            d.update({"error": node.error, "correction": node.correction})
        elif isinstance(node, SnippetNode):
            d["pattern"] = node.pattern
        elif isinstance(node, PipeNode):
            d["nodes"] = [self.ast_to_dict(n) for n in node.nodes]
        elif isinstance(node, UnknownNode):
            d["raw"] = node.raw
        return d

    def program_to_json(self, nodes: List[ASTNode]) -> str:
        return json.dumps([self.ast_to_dict(n) for n in nodes], indent=2, ensure_ascii=False)


class UVIBytecodeCompiler:
    def __init__(self):
        self._label_counter = 0

    def _new_label(self, prefix="L"):
        self._label_counter += 1
        return f"{prefix}_{self._label_counter}"

    def compile(self, nodes: List[ASTNode]) -> List[Instruction]:
        instructions = []
        for node in nodes:
            instructions.extend(self._compile_node(node))
        instructions.append(Instruction(Opcode.HALT))
        return instructions

    def _compile_node(self, node: ASTNode) -> List[Instruction]:
        if node is None:
            return []
        if isinstance(node, SignatureNode):
            return []
        if isinstance(node, StopNode):
            # Désassemblage uniquement (ce compilateur legacy n'est jamais
            # exécuté, voir en-tête de classe) -- ne PAS retourner []
            # comme SignatureNode ci-dessus : @STOP est une vraie
            # instruction avec un effet (arrêt), la faire disparaître du
            # désassemblage serait le même "BUG SILENCIEUX" déjà corrigé
            # pour MODULE_LOAD/EXPORT_NAME (voir Opcode.MODULE_LOAD) --
            # Opcode.HALT existe déjà et porte exactement ce sens.
            return [Instruction(Opcode.HALT)]
        if isinstance(node, AssignNode):
            if node.is_call and node.call_node:
                instrs = self._compile_node(node.call_node)
                instrs.append(Instruction(Opcode.STORE, [node.var]))
                return instrs
            return [
                Instruction(Opcode.PUSH, [node.expr]),
                Instruction(Opcode.STORE, [node.var])
            ]
        if isinstance(node, PrintNode):
            return [
                Instruction(Opcode.PUSH, [node.expr]),
                Instruction(Opcode.PRINT)
            ]
        if isinstance(node, IfNode):
            return [Instruction(Opcode.JUMP_IF, [node.condition,
                                                  [self._compile_node(n) for n in node.then_block],
                                                  [self._compile_node(n) for n in node.else_block]])]
        if isinstance(node, WhileNode):
            return [Instruction(Opcode.JUMP, [node.condition,
                                               [self._compile_node(n) for n in node.body]])]
        if isinstance(node, ForNode):
            return [Instruction(Opcode.PUSH, [node.count_expr]),
                    Instruction(Opcode.JUMP, ["__for__",
                                               [self._compile_node(n) for n in node.body]])]
        if isinstance(node, SleepNode):
            return [Instruction(Opcode.SLEEP, [node.duration])]
        if isinstance(node, ModuleNode):
            # Désassemblage uniquement (voir Opcode.MODULE_LOAD) --
            # alias vide affiché tel quel plutôt que résolu depuis le
            # nom de fichier (cette résolution n'a de sens qu'à
            # l'exécution réelle, jamais à la désassemblage statique).
            return [Instruction(Opcode.MODULE_LOAD, [node.filename, node.alias])]
        if isinstance(node, ExportNode):
            return [Instruction(Opcode.EXPORT_NAME, [node.name])]
        if isinstance(node, InputNode):
            return [Instruction(Opcode.INPUT, [node.var])]
        if isinstance(node, DefNode):
            body_instrs = []
            for n in node.body:
                body_instrs.extend(self._compile_node(n))
            return [Instruction(Opcode.DEF, [node.name, node.params, body_instrs])]
        if isinstance(node, CallNode):
            return [Instruction(Opcode.CALL, [node.name, node.args])]
        if isinstance(node, RoutineDefNode):
            body_instrs = []
            for n in node.body:
                body_instrs.extend(self._compile_node(n))
            return [Instruction(Opcode.DEF, [f"*{node.name}", [], body_instrs])]
        if isinstance(node, RoutineCallNode):
            return [Instruction(Opcode.CALL, [f"*{node.name}", []])]
        if isinstance(node, TabNode):
            opcode_map = {
                'TAB.CREATE': Opcode.TAB_CREATE, 'TAB_EMPTY': Opcode.TAB_CREATE,
                'TAB_INIT':   Opcode.TAB_CREATE,
                'TAB.PUSH':   Opcode.TAB_PUSH,   'TAB.POP':   Opcode.TAB_POP,
                'TAB.GET':    Opcode.TAB_GET,     'TAB.SET':   Opcode.TAB_SET,
                'TAB.DEL':    Opcode.TAB_DEL,     'TAB.LEN':   Opcode.TAB_LEN,
                'TAB.PRINT':  Opcode.TAB_PRINT,
            }
            # NOTE DE SÉCURITÉ (vérifiée, pas un bug actif aujourd'hui) :
            # ce défaut n'est atteignable QUE si node.cmd contient une
            # valeur absente de opcode_map. Vérifié contre le lexer
            # (UVILexer, pattern TAB_CMD = r'\bTAB\.(PUSH|POP|GET|SET|DEL|
            # LEN|PRINT|CREATE)\b|\bTAB\b') : la regex n'accepte que ces 8
            # commandes précises, donc une faute de frappe (ex: TAB.PUSK)
            # ne matche jamais TAB_CMD et ne produit jamais de TabNode --
            # elle tombe dans un autre chemin du lexer en amont. Ce défaut
            # est donc mort avec le lexer TEL QU'IL EST ÉCRIT AUJOURD'HUI.
            # DANGER SI CE PATTERN LEXER CHANGE UN JOUR (regex élargie,
            # nouvelle commande TAB ajoutée) SANS mettre à jour opcode_map
            # en même temps : le défaut redeviendrait alors silencieusement
            # actif -- une commande TAB.XXX valide au lexage mais absente
            # d'opcode_map compilerait vers TAB_CREATE (réinitialisation du
            # tableau) au lieu de lever une erreur. Remplacer le défaut par
            # un raise serait plus sûr dans l'absolu, mais changerait le
            # comportement pour un cas aujourd'hui inatteignable -- laissé
            # tel quel pour ne pas modifier un comportement non testé sans
            # nécessité, ce commentaire sert d'alerte pour la prochaine
            # personne qui touche au pattern TAB_CMD du lexer.
            op = opcode_map.get(node.cmd, Opcode.TAB_CREATE)
            return [Instruction(op, node.operands)]
        if isinstance(node, StrNode):
            return [Instruction(Opcode.STR_OP, [node.cmd] + node.operands)]
        if isinstance(node, ShionNode):
            return [Instruction(Opcode.SHION_ASK, [node.cmd] + node.operands)]
        if isinstance(node, DiskNode):
            opcode_map = {
                '&save': Opcode.DISK_SAVE, '&load': Opcode.DISK_LOAD,
                '&list': Opcode.DISK_LIST, '&delete': Opcode.DISK_DEL,
            }
            # Même situation que TAB_CMD ci-dessus (vérifiée, pas un bug
            # actif aujourd'hui) : le lexer (pattern DISK_CMD =
            # r'&(save|load|list|delete)\b') ne matche que ces 4 commandes,
            # donc ce défaut est mort tant que ce pattern reste inchangé.
            # Même avertissement : si la regex DISK_CMD est élargie sans
            # mettre à jour ce opcode_map, une commande &xxx valide au
            # lexage mais non mappée ici s'exécuterait silencieusement
            # comme &list au lieu de lever une erreur.
            op = opcode_map.get(node.cmd, Opcode.DISK_LIST)
            return [Instruction(op, node.operands)]
        if isinstance(node, ImportNode):
            return [Instruction(Opcode.IMPORT, [node.filename])]
        if isinstance(node, PipeNode):
            instrs = []
            for n in node.nodes:
                instrs.extend(self._compile_node(n))
            return instrs
        if isinstance(node, UnknownNode):
            # BUG SILENCIEUX CORRIGÉ : ce noeud est ce que le parser produit
            # pour TOUTE erreur de syntaxe (DEF/CALL/IF/WHILE/FOR/LEARN
            # invalides, cf. UVIParser). Avant ce correctif, il tombait dans
            # le `return []` final ci-dessous : la ligne fautive disparaissait
            # silencieusement du bytecode compilé, sans aucun avertissement,
            # et le reste du script s'exécutait comme si de rien n'était.
            # Le tree-walker (UVIEvaluator._exec_node, cf. plus bas dans ce
            # fichier) gère lui correctement ce cas en émettant une erreur
            # UNKNOWN_SYNTAX -- sauf pour le cas où node.raw correspond à une
            # routine déjà définie (RoutineDefNode l'enregistre sous son nom
            # brut). Cette branche compilateur ne réplique PAS ce
            # rattrapage de routine : au moment de la compilation (une seule
            # passe linéaire), on ne peut pas garantir que la routine
            # visée a déjà été vue plus haut dans le flux si elle est
            # définie plus bas dans le script. Lever une erreur de
            # compilation explicite ici est donc le choix le plus sûr :
            # un faux positif (routine valide refusée) est visible et
            # corrigible immédiatement, alors qu'un faux négatif (erreur de
            # syntaxe avalée) ne l'est pas.
            raise BytecodeError(
                f"Erreur de syntaxe (ligne {getattr(node, 'line', '?')}) : {node.raw}"
            )
        if isinstance(node, (LearnNode, SnippetNode)):
            # Ces noeuds déclenchent un comportement d'assistant interactif
            # (self.agent.learn_from_terminal / generate_snippet côté
            # tree-walker) qui n'a pas d'équivalent en bytecode statique --
            # ce ne sont pas des instructions exécutables, ce sont des
            # effets de bord sur l'IDE/l'agent au moment de la frappe.
            # Compilés vers aucune instruction délibérément (comportement
            # inchangé), mais DOCUMENTÉ ici pour que ça ne soit plus un
            # silence non expliqué : contrairement à UnknownNode ci-dessus,
            # ce n'est pas un bug, c'est une limite de portée assumée du
            # compilateur bytecode par rapport au tree-walker.
            return []
        return []

    def bytecode_to_json(self, instructions: List[Instruction]) -> str:
        return json.dumps([i.to_dict() for i in instructions], indent=2, ensure_ascii=False)

    def flatten_bytecode(self, nodes: List[ASTNode]) -> List[Instruction]:
        self._label_counter = 0
        raw = self.compile(nodes)
        flat = []
        self._flatten_list(raw, flat)
        return flat

    def _flatten_list(self, instructions: List[Instruction], out: List[Instruction]):
        for instr in instructions:
            self._flatten_instr(instr, out)

    def _flatten_instr(self, instr: Instruction, out: List[Instruction]):
        if instr.opcode == Opcode.JUMP_IF:
            condition = instr.operands[0]
            then_nested = instr.operands[1]
            else_nested = instr.operands[2] if len(instr.operands) > 2 else []
            label_else = self._new_label("else")
            label_end = self._new_label("endif")
            out.append(Instruction(Opcode.PUSH, [condition]))
            out.append(Instruction(Opcode.JUMP_IF_FALSE, [label_else]))
            for block in then_nested:
                if isinstance(block, list):
                    self._flatten_list(block, out)
                else:
                    self._flatten_instr(block, out)
            out.append(Instruction(Opcode.JUMP_ABS, [label_end]))
            out.append(Instruction(Opcode.LABEL, [label_else]))
            for block in else_nested:
                if isinstance(block, list):
                    self._flatten_list(block, out)
                else:
                    self._flatten_instr(block, out)
            out.append(Instruction(Opcode.LABEL, [label_end]))

        elif instr.opcode == Opcode.JUMP and instr.operands[0] == "__for__":
            body_nested = instr.operands[1]
            label_start = self._new_label("for")
            label_end = self._new_label("endfor")
            out.append(Instruction("FOR_INIT", [label_start, label_end]))
            out.append(Instruction(Opcode.LABEL, [label_start]))
            out.append(Instruction("FOR_CHECK", [label_end]))
            for block in body_nested:
                if isinstance(block, list):
                    self._flatten_list(block, out)
                else:
                    self._flatten_instr(block, out)
            out.append(Instruction("FOR_STEP", [label_start]))
            out.append(Instruction(Opcode.LABEL, [label_end]))

        elif instr.opcode == Opcode.JUMP:
            condition = instr.operands[0]
            body_nested = instr.operands[1]
            label_start = self._new_label("while")
            label_end = self._new_label("endwhile")
            out.append(Instruction(Opcode.LABEL, [label_start]))
            out.append(Instruction(Opcode.PUSH, [condition]))
            out.append(Instruction(Opcode.JUMP_IF_FALSE, [label_end]))
            for block in body_nested:
                if isinstance(block, list):
                    self._flatten_list(block, out)
                else:
                    self._flatten_instr(block, out)
            out.append(Instruction(Opcode.JUMP_ABS, [label_start]))
            out.append(Instruction(Opcode.LABEL, [label_end]))

        elif instr.opcode == Opcode.DEF:
            name = instr.operands[0]
            params = instr.operands[1]
            body_nested = instr.operands[2]
            label_skip = self._new_label("skip_def")
            label_start = self._new_label(f"def_{name}")
            label_end = self._new_label(f"end_{name}")
            out.append(Instruction(Opcode.JUMP_ABS, [label_skip]))
            out.append(Instruction(Opcode.LABEL, [label_start]))
            flat_body = []
            if isinstance(body_nested, list):
                for item in body_nested:
                    if isinstance(item, list):
                        self._flatten_list(item, flat_body)
                    elif isinstance(item, Instruction):
                        self._flatten_instr(item, flat_body)
            out.extend(flat_body)
            out.append(Instruction(Opcode.RET, []))
            out.append(Instruction(Opcode.LABEL, [label_end]))
            out.append(Instruction(Opcode.LABEL, [label_skip]))
            out.append(Instruction("DEF_REG", [name, params, label_start]))

        else:
            out.append(instr)

    def flat_bytecode_to_json(self, nodes: List[ASTNode]) -> str:
        flat = self.flatten_bytecode(nodes)
        return json.dumps([i.to_dict() for i in flat], indent=2, ensure_ascii=False)


class UVIModule:
    """Espace de noms isolé produit par MODULE fichier -> alias, côté
    TREE-WALKER (UVIEvaluator/UVIEngine). Voir VMModule ci-dessous pour
    l'équivalent côté VM (UVIVirtualMachine) -- deux classes séparées,
    PAS une seule partagée : la représentation interne d'un module
    diffère structurellement entre les deux moteurs (memoire dict brut
    ici, vs Frame + bytecode compilé côté VM), donc forcer un type
    unique aurait mélangé des formats incompatibles selon le moteur qui
    l'utilise.

    Contrairement à IMPORT (inclusion textuelle : le fichier importé
    s'exécute directement dans self.memoire de l'appelant, voir
    _exec_import), un module chargé via MODULE a sa PROPRE mémoire,
    ses PROPRES fonctions/routines -- rien de tout ça ne touche jamais
    le scope de l'appelant. Seuls les noms passés à EXPORT (stockés
    dans `exports`) sont lisibles depuis l'extérieur, via alias::nom."""

    def __init__(self, alias: str, source_path: str):
        self.alias = alias
        self.source_path = source_path
        self.memoire: Dict[str, Any] = {}
        self.fonctions: Dict[str, Any] = {}
        self.routines: Dict[str, Any] = {}
        self.exports: set = set()

    def get_exported(self, name: str):
        """Retourne (trouvé, valeur) pour une lecture alias::nom. Ne
        distingue PAS "nom inexistant" de "nom existant mais non
        exporté" au niveau du type de retour -- c'est à l'appelant
        (_lookup_module_member) de choisir le bon code d'erreur selon
        lequel des deux cas s'est produit, pour ne pas fuiter
        l'information "ce nom existe en privé" à un script qui ne
        devrait même pas le savoir."""
        if name in self.exports and name in self.memoire:
            return True, self.memoire[name]
        return False, ""

    def has_exported_callable(self, name: str) -> bool:
        return name in self.exports and (name in self.fonctions or name in self.routines)


class VMModule:
    """Équivalent de UVIModule, côté VM (UVIVirtualMachine). Un fichier
    chargé via MODULE est compilé et exécuté UNE PREMIÈRE FOIS par une
    VM ENFANT complètement séparée (voir UVIVirtualMachine._load_module)
    -- pas un swap d'attributs sur self comme côté tree-walker, parce
    que la VM n'a pas d'équivalent direct à "self.memoire" swappable :
    pc, stack, frame_stack appartiennent à une exécution en cours et ne
    peuvent pas être remplacés à la volée sans casser l'exécution du
    script appelant lui-même. Une VM enfant garantit une isolation
    totale par construction (deux objets Python distincts), sans
    risque de fuite accidentelle vers la VM appelante.

    IMPORTANT (corrigé après une première tentative erronée) : la VM
    enfant elle-même (`vm`) et son bytecode compilé (`bytecode`) sont
    conservés ici, pas seulement un instantané de son état final --
    parce qu'un futur CALL alias::nom(args) doit pouvoir RE-SAUTER dans
    le bytecode de la fonction (à son entry_idx), ce qui n'est possible
    que si ce bytecode existe encore quelque part. Une extraction
    ponctuelle de global_frame/functions/routines au moment du
    chargement (première version de cette classe) suffisait pour lire
    des variables exportées, mais pas pour APPELER une fonction/routine
    exportée après coup -- entry_idx est un index dans CE bytecode
    précis, sans le bytecode l'index seul ne veut rien dire."""

    def __init__(self, alias: str, source_path: str, vm: "UVIVirtualMachine", bytecode: dict):
        self.alias = alias
        self.source_path = source_path
        self.vm = vm  # VM enfant, déjà exécutée une première fois (chargement)
        self.bytecode = bytecode  # bytecode compilé du fichier module
        self.exports: set = set()

    @property
    def global_frame(self):
        return self.vm.global_frame

    @property
    def functions(self):
        return self.vm.functions

    @property
    def routines(self):
        return self.vm.routines

    def get_exported(self, name: str):
        """Même contrat que UVIModule.get_exported (voir docstring
        là-bas) : ne distingue pas "inexistant" de "privé"."""
        if name in self.exports and name in self.global_frame.locals:
            return True, self.global_frame.locals[name]
        return False, ""

    def has_exported_callable(self, name: str) -> bool:
        return name in self.exports and (name in self.functions or name in self.routines)


class UVIEvaluator:
    def __init__(self, ide, vfs):
        self.ide = ide
        self.vfs = vfs
        self.agent = None
        self.lexer = UVILexer()
        self.parser = UVIParser(self.lexer)
        self.memoire = {}
        self.routines = {}
        self.fonctions = {}
        self.imported_files = set()
        # Registre des modules chargés via MODULE (alias -> UVIModule),
        # et fichiers modules actuellement en cours de chargement (pour
        # détecter un cycle A charge B qui charge A). Volontairement
        # SÉPARÉ de imported_files : IMPORT et MODULE sont deux
        # mécanismes distincts avec leurs propres règles de
        # rechargement (voir _exec_module), ne pas les faire
        # interférer l'un avec l'autre.
        self.modules: Dict[str, "UVIModule"] = {}
        self.loading_modules: set = set()
        # Pointeur vers le UVIModule dont le fichier est en cours
        # d'exécution (non-None seulement pendant le bloc try de
        # _exec_module) -- nécessaire car self.memoire EST
        # nouveau_module.memoire pendant ce temps (swap, pas copie),
        # donc _exec_export ne peut pas retrouver nouveau_module à
        # partir de self.memoire seul ; il lui faut cette référence
        # explicite pour savoir où ajouter le nom exporté.
        self._module_en_construction: Optional["UVIModule"] = None
        # Débogueur (ajout de cette session) : None par défaut, aucun
        # coût ni changement de comportement tant qu'aucun UVIDebugger
        # n'est explicitement attaché (voir run_sequence, qui ne fait
        # rien de plus qu'un `if self.debugger:` autour de l'appel à
        # check_line). _debug_call_stack maintenue manuellement en
        # PARALLÈLE du swap de self.memoire dans _exec_call/
        # _exec_module_call (poussée avant d'entrer dans le corps de la
        # fonction, dépilée dans le finally, jamais l'inverse) --
        # contrairement à la VM (frame_stack déjà une vraie pile
        # consultable), le tree-walker n'a rien d'équivalent nativement.
        self.debugger: Optional["UVIDebugger"] = None
        self._debug_call_stack: List[UVIDebugFrame] = []
        self.evaluator = SafeEval()
        self.is_running = False
        # ARRÊT COOPÉRATIF. Il n'existait AUCUN moyen d'interrompre un
        # script : une boucle de jeu ou un `%% 1 -> ...` obligeait à tuer
        # la fenêtre, donc à perdre le contenu non sauvegardé de l'éditeur.
        # C'était le manque le plus grave de l'IDE, avant l'esthétique.
        #
        # Coopératif et non forcé, VOLONTAIREMENT : Python ne sait pas tuer
        # proprement un thread, et un thread interrompu au milieu d'un
        # _safe_store laisserait la mémoire du moteur incohérente -- l'IDE
        # survivrait mais MENTIRAIT ensuite sur l'état des variables. Le
        # drapeau est relu aux points de reprise naturels (chaque
        # instruction, chaque tour de boucle).
        #
        # LIMITE ASSUMÉE : une instruction unique très longue (SLEEP de
        # 30 s, SHION.ASK qui attend le réseau) ne sera pas interrompue
        # avant d'avoir rendu la main. L'arrêt est garanti « à la prochaine
        # instruction », pas « immédiat » -- au sens où une instruction
        # DÉJÀ EN COURS ne peut pas être coupée en plein vol ; en revanche
        # AUCUNE instruction suivante, même dans un bloc/appel imbriqué,
        # ne s'exécute plus une fois ce drapeau levé (garde unique dans
        # exec_node, voir plus bas).
        self.stop_requested = False
        # Ligne de la commande @STOP qui a déclenché l'arrêt (StopNode
        # ci-dessous) ; 0 = pas d'arrêt en cours ou arrêt déclenché par le
        # bouton ARRÊTER (arreter_script) plutôt que par le script --
        # permet à run_sequence de distinguer les deux dans son message
        # de fin, sans dupliquer stop_requested lui-même.
        self._stop_line = 0
        self.call_depth = 0
        self.function_calls = 0
        self.named_call_counts: Dict[str, int] = {}  # "DEF:nom"/"ROUTINE:nom" -> appels
        self.current_memory_usage = 0
        self.current_run_line = 0
        self.failed_line = None
        self.error_flag = False
        self.last_errors: List[UVIError] = []
        # Bascule d'août 2026 : le client derrière SHION.ASK est choisi par
        # _build_shion_client() (DeepSeek par défaut, Shion natif si
        # SHION_BACKEND=shion). Le contrat .complete(prompt, remember=True)
        # -> str est identique dans les deux cas, donc _exec_shion,
        # _shion_worker et run_script_vm n'ont eu AUCUNE modification à
        # subir -- c'était la condition de la bascule, pas un espoir.
        self.shion_client = _build_shion_client() if HAS_SHION else None
        self.shion_is_busy = False  # garde anti-spam, voir _exec_shion
        self._shion_clients_par_niveau: Dict[str, Any] = {}  # cache "pro"/"plus" -> client, voir _shion_client_pour_niveau

    def set_agent(self, agent):
        self.agent = agent

    def _shion_client_pour_niveau(self, tier: str):
        """Client SHION.ASK pour un niveau d'abonnement donné ("pro" ->
        Shion-flash/DeepSeek 1,5B, "plus" -> Shion complet/DeepSeek 7B),
        construit une seule fois par niveau puis mis en cache -- même
        raison que le cache _moteurs_partages de ShionServingClient : ne
        pas reconstruire un client (donc perdre son historique de
        conversation) à chaque appel. Appelé uniquement quand
        uvi_auth.is_configured() est vrai -- voir _exec_shion."""
        if tier not in self._shion_clients_par_niveau:
            if tier == "plus":
                client = DeepSeekClient(base_url=SHION_FULL_SERVER_URL, model_name=SHION_FULL_MODEL_NAME)
            else:  # "pro"
                client = DeepSeekClient(base_url=SHION_FLASH_SERVER_URL, model_name=SHION_FLASH_MODEL_NAME)
            self._shion_clients_par_niveau[tier] = _DeepSeekAvecReplisLocal(client)
        return self._shion_clients_par_niveau[tier]

    def _resoudre_client_shion(self):
        """Point d'entrée UNIQUE pour savoir QUEL client répond à SHION,
        compte tenu de l'abonnement -- partagé par _exec_shion (SHION.ASK),
        llm_client_prioritaire (!ERIS(...), onglet de discussion), pour
        qu'un compte gratuit ne puisse pas contourner le verrouillage en
        passant par l'un plutôt que l'autre.

        Retourne (client, motif). `motif` vaut "connexion" (personne
        connecté), "abonnement" (connecté mais niveau "free") ou None
        (aucun verrouillage applicable -- soit un client valide est
        retourné, soit c'est une indisponibilité technique ordinaire,
        cf. SHION_UNAVAILABLE).

        Tant que uvi_auth.is_configured() est faux (SUPABASE_URL/ANON_KEY
        absentes -- développement local, voir docs/COMPTES_ABONNEMENTS.md),
        comportement HISTORIQUE inchangé : self.shion_client, choisi une
        fois pour toutes par _build_shion_client() selon SHION_BACKEND."""
        if uvi_auth.is_configured():
            tier = uvi_auth.current_tier()
            if tier is None:
                return None, "connexion"
            if tier == "free":
                return None, "abonnement"
            return self._shion_client_pour_niveau(tier), None
        return self.shion_client, None

    def llm_client_prioritaire(self):
        """Rend le client de langage à utiliser pour !ERIS(...) (syntaxe
        d'appel synchrone conservée dans le langage, voir _eval_expr_or_eris)
        et pour l'onglet de discussion SHION.

        HISTORIQUE : ERIS (assistant Gemini séparé, clé API personnelle)
        a été retiré -- SHION couvre déjà son rôle sans les inconvénients
        qui justifiaient son existence (ERIS_API_KEY était une variable
        d'environnement globale au process, donc une clé, un quota et un
        historique partagés entre tous les utilisateurs d'une même
        instance UVI ; SHION n'a structurellement pas ce problème). Cette
        méthode garde son nom et sa signature : c'est le point d'entrée
        UNIQUE déjà utilisé par _eval_expr_or_eris ET l'onglet SHION,
        inutile de le dupliquer à deux endroits pour un simple
        raccourcissement. Le motif de verrouillage (connexion/abonnement)
        n'est pas distingué ici : IA_INLINE_UNAVAILABLE reste le message
        générique pour ce chemin synchrone, SHION.ASK (_exec_shion) est
        l'endroit qui distingue précisément le motif."""
        client, _motif = self._resoudre_client_shion()
        return client

    def _pump_ui_events(self):
        """Laisse l'hôte (l'IDE) traiter ses évènements en attente pendant
        qu'un script tourne. Ne fait RIEN si l'hôte n'expose pas cette
        capacité.

        POURQUOI : _exec_node appelait self.ide.root.update() en dur pour
        WhileNode/ForNode. C'était la SEULE dépendance de l'évaluateur à
        Tkinter qui n'avait pas de garde -- _exec_draw teste déjà
        hasattr(self.ide, "canvas_draw") et _exec_key teste déjà
        hasattr(self.ide, "key_is_down") justement pour rester utilisables
        hors IDE, mais root.update() traversait quand même et faisait
        planter tout hôte sans attribut .root (AttributeError brute, non
        traduite en erreur UVI) dès la première boucle de 20 tours.

        Même contrat défensif que _exec_draw/_exec_key (getattr + test
        d'appelabilité), même esprit que les callbacks optionnels de
        UVIVirtualMachine (print_fn/draw_fn/input_fn/key_down_fn/
        key_pressed_fn) : absent = dégradation SILENCIEUSE, jamais un
        plantage.

        Aucun changement quand ide=UVIIDE réel : UVIIDE.pump_events()
        appelle exactement self.root.update(), donc la boucle de jeu
        continue de pomper aux mêmes instants qu'avant."""
        pump = getattr(self.ide, "pump_events", None)
        if callable(pump):
            pump()

    def _emit_error(self, kind: str, message: str, line: int = 0, column: int = 0):
        err = UVIError(line=line, column=column, kind=kind, message=message)
        self.last_errors.append(err)
        self.ide.log(str(err), "err")
        self.error_flag = True

    # Interpolation de chaîne : {expr} à l'intérieur d'une chaîne UVI est
    # remplacé par la valeur de expr, évaluée dans le contexte courant.
    # [^{}]* exclut délibérément les accolades imbriquées (UVI n'a pas de
    # syntaxe qui en aurait besoin) -- une accolade non fermée ou une
    # paire vide ({}) ne matche pas ce pattern et traverse donc telle
    # quelle, comme du texte littéral (pas d'interpolation forcée sur du
    # texte qui contient juste des accolades décoratives).
    _INTERP_RE = re.compile(r'\{([^{}]+)\}')

    # !ERIS("question") -- capture tout ce qui est entre les parenthèses,
    # ancré en tête ET en fin de l'expression entière (^...$) : ERIS doit
    # être la TOTALITÉ de l'expression, jamais combiné avec d'autres
    # opérateurs (ERIS("q") + 5 ne matche volontairement pas -- rester
    # simple plutôt que de permettre une intégration dans un calcul
    # arbitraire). \s* entre ERIS et '(' : le parser reconstruit
    # l'expression avec des espaces entre tokens (le code source original
    # 'ERIS("q")' devient 'ERIS ( "q" )' une fois reconstruit), donc
    # l'expression réelle à ce stade contient ces espaces -- vérifié
    # empiriquement avant d'écrire cette regex, une version sans cette
    # tolérance ne matcherait jamais rien en pratique.
    #
    # NOM GARDÉ APRÈS LE RETRAIT D'ERIS (l'assistant Gemini séparé) : cette
    # syntaxe reste la seule façon d'appeler l'IA de façon SYNCHRONE dans
    # une expression (SHION.ASK est une instruction dédiée, non-bloquante,
    # voir _exec_shion) -- la retirer aurait supprimé une capacité du
    # LANGAGE, pas juste un backend. Renommer le mot-clé casserait tout
    # script existant qui l'utilise déjà. Elle appelle désormais TOUJOURS
    # SHION (voir llm_client_prioritaire) : ERIS n'est plus qu'un nom
    # hérité pour cette syntaxe, plus un produit séparé.
    _ERIS_CALL_RE = re.compile(r'^\s*ERIS\s*\((.*)\)\s*$', re.DOTALL)

    def _eval_expr_or_eris(self, expr_str: str, line_num: int = 0):
        """Point d'entrée UNIQUE vers self.evaluator.eval_expr, appelé par
        evaluate() ET _interpolate_string() -- centralise l'interception
        de l'appel IA synchrone pour qu'elle fonctionne identiquement dans
        les deux contextes (!ERIS(...) direct comme {ERIS(...)} en
        interpolation), plutôt que de dupliquer la logique à deux endroits
        avec le risque d'oublier l'un des deux lors d'une future
        modification."""
        m = self._ERIS_CALL_RE.match(expr_str)
        if m:
            client = self.llm_client_prioritaire()
            if client is None:
                self._emit_error(
                    "IA_INLINE_UNAVAILABLE",
                    "Aucun backend SHION disponible ici pour répondre à ERIS(...).",
                    line_num,
                )
                return ""
            argument_brut = m.group(1).strip()
            try:
                question = self.evaluator.eval_expr(argument_brut, self.memoire)
            except Exception as e:
                self._emit_error(
                    "IA_INLINE_INVALID_ARGUMENT",
                    f"Argument invalide pour ERIS(...) : '{argument_brut}' ({e})",
                    line_num,
                )
                return ""
            try:
                return client.complete(str(question))
            except LLMClientError as e:
                self._emit_error("IA_INLINE_REQUEST_FAILED", str(e), line_num)
                return ""
        return self.evaluator.eval_expr(expr_str, self.memoire)

    def _interpolate_string(self, texte: str, line_num: int = 0) -> str:
        def _remplacer(m):
            expr = m.group(1)
            try:
                valeur = self._eval_expr_or_eris(expr, line_num)
            except Exception as e:
                # Cohérent avec le reste du langage (cf. eval_expr lui-
                # même) : une expression invalide devient une erreur
                # UVI explicite plutôt qu'un plantage Python brut ou un
                # texte "{expr}" laissé tel quel dans la sortie, qui
                # masquerait silencieusement la faute de frappe/erreur
                # de l'auteur du script.
                self._emit_error(
                    "INTERPOLATION_FAILED",
                    f"Expression d'interpolation invalide : '{expr}' ({e})",
                    line_num,
                )
                return ""
            return str(valeur)
        return self._INTERP_RE.sub(_remplacer, texte)

    def evaluate(self, value: str):
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            # BUG CORRIGÉ : startswith('"') and endswith('"') est vrai
            # pour BIEN PLUS qu'un littéral unique -- '"a" + "b"' et
            # '"a" == "a"' commencent et finissent aussi par un guillemet.
            # L'ancien code prenait alors TOUT le texte entre le PREMIER
            # et le DERNIER guillemet comme un seul littéral (y compris le
            # ' + ' ou ' == ' du milieu), donnant 'a" + "b' au lieu de
            # 'ab', silencieusement -- jamais d'erreur, juste un mauvais
            # résultat. Vérifie maintenant, via le VRAI lexer (celui qui
            # fait déjà foi partout ailleurs dans ce fichier), que `value`
            # tokenize en EXACTEMENT un seul token STRING avant de prendre
            # ce raccourci ; sinon on tombe dans _eval_expr_or_eris
            # ci-dessous, qui gère correctement la concaténation/comparaison
            # (SafeEval._eval, ast.BinOp/ast.Compare) -- seule
            # l'interpolation {var} au sein d'une expression composée
            # (ex. '"Bonjour {nom}" + "!"') reste hors de portée de ce
            # correctif, limite déjà partagée avec le reste de ce chemin
            # d'évaluation, pas introduite ici.
            tokens_test = self.lexer.tokenize(value)
            if len(tokens_test) == 1 and tokens_test[0].type == 'STRING':
                return self._interpolate_string(value[1:-1])
        if '::' in value and value.count('::') == 1:
            # alias::nom : lecture qualifiée d'un export de module. Un
            # seul '::' attendu -- deux ou plus (ex. 'a::b::c') n'est
            # pas une syntaxe supportée (pas de modules imbriqués dans
            # l'espace de noms), traité comme UNKNOWN_MODULE plutôt que
            # silencieusement mal interprété. Le cas d'une chaîne
            # littérale contenant '::' (ex. "IP::PORT") est déjà écarté
            # par le bloc guillemets ci-dessus (return anticipé), donc
            # aucune vérification supplémentaire n'est nécessaire ici.
            alias, _, nom = value.partition('::')
            alias, nom = alias.strip(), nom.strip()
            if alias not in self.modules:
                self._emit_error("UNKNOWN_MODULE", f"Module '{alias}' non chargé (utiliser MODULE d'abord).")
                return ""
            trouve, val = self.modules[alias].get_exported(nom)
            if not trouve:
                # Volontairement le MÊME code que "nom exporté
                # inexistant" ET "nom existant mais privé" (cf.
                # UVIModule.get_exported) -- ne pas distinguer les deux
                # cas au niveau du message évite de révéler à
                # l'extérieur qu'un nom privé existe dans le module.
                self._emit_error("NOT_EXPORTED", f"'{nom}' n'est pas exporté par le module '{alias}'.")
                return ""
            return val
        if value.startswith('TAB.GET '):
            rest = value[8:].strip()
            tokens = self.lexer.tokenize(rest)
            parts = self._split_tokens_by_type(tokens, 'ARROW')
            if len(parts) >= 2:
                nom = self._reconstruct_expr(parts[0]).strip()
                idx_str = self._reconstruct_expr(parts[1])
                if nom in self.memoire and isinstance(self.memoire[nom], list):
                    try:
                        idx = int(float(self.evaluator.eval_expr(idx_str, self.memoire)))
                        arr = self.memoire[nom]
                        if 0 <= idx < len(arr):
                            return arr[idx]
                        self._emit_error("INDEX_OUT_OF_BOUNDS", f"Index {idx} hors limites.")
                    except Exception:
                        self._emit_error("INVALID_INDEX", f"Index invalide : '{idx_str}'.")
                else:
                    self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.")
            return ""
        if value.startswith('TAB.LEN '):
            nom = value[8:].strip()
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                return len(self.memoire[nom])
            return 0
        if value in self.memoire:
            return self.memoire[value]
        try:
            return self._eval_expr_or_eris(value)
        except SafeEvalError as e:
            # Seul point d'appel de eval_expr qui n'était protégé par aucun
            # try/except (contrairement à idx = ...eval_expr(idx_str,...)
            # plus haut dans cette même méthode, déjà protégé). C'est ici
            # que l'ancien `return expr` de SafeEval.eval_expr s'échappait
            # silencieusement vers le reste du tree-walker.
            self._emit_error("EXPR_EVAL_FAILED", str(e))
            return ""

    def _split_tokens_by_type(self, tokens, token_type):
        sublists, current = [], []
        for t in tokens:
            if t.type == token_type:
                sublists.append(current)
                current = []
            else:
                current.append(t)
        sublists.append(current)
        return sublists

    def _reconstruct_expr(self, tokens):
        parts = []
        for t in tokens:
            if t.type == 'STRING':
                parts.append(f'"{t.value}"')
            else:
                parts.append(t.value)
        return " ".join(parts).strip()

    def _safe_store(self, var: str, val):
        var = var.strip()
        if isinstance(val, str) and len(val) > MAX_STRING_LENGTH:
            val = val[:MAX_STRING_LENGTH]
            self.ide.log(f"[SÉCURITÉ] '{var}' tronquée.", "err")
        old_val = self.memoire.get(var, "")
        delta = sys.getsizeof(val) - sys.getsizeof(old_val)
        if var not in self.memoire:
            delta += sys.getsizeof(var)
        if self.current_memory_usage + delta > MAX_MEMORY_BYTES:
            self._emit_error("MEMORY_OVERFLOW", f"Mémoire saturée — '{var}' bloquée.")
            return False
        self.current_memory_usage = max(0, self.current_memory_usage + delta)
        self.memoire[var] = val
        return True

    def _evaluate_debug_condition(self, expr: str):
        """Évalue une condition de point d'arrêt (voir UVIDebugger.
        check_line) SANS AUCUN effet de bord sur error_flag/last_errors
        du script observé -- self.evaluate() n'échoue pas en levant une
        exception Python pour une expression malformée, elle appelle
        self._emit_error(...) en interne (positionnant error_flag),
        donc un simple try/except autour de l'appel (comme dans
        UVIDebugger.check_line, qui reste volontairement indépendant du
        moteur) ne suffit PAS à absorber cet échec. Sauvegarde/restaure
        explicitement ici : une condition de point d'arrêt boiteuse ne
        doit JAMAIS faire échouer un script par ailleurs valide -- le
        point d'arrêt ne se déclenche simplement pas ce passage-ci
        (traité comme condition fausse par UVIDebugger._legacy_truthy
        sur le résultat, généralement "" dans ce cas)."""
        saved_error_flag = self.error_flag
        saved_last_errors = list(self.last_errors)
        try:
            return self.evaluate(expr)
        finally:
            self.error_flag = saved_error_flag
            self.last_errors = saved_last_errors

    def exec_node(self, node: ASTNode):
        # self.stop_requested inclus dans ce garde (pas seulement
        # error_flag) : exec_node est le point d'entrée UNIVERSEL de
        # toute instruction (voir commentaire juste plus bas sur
        # self.debugger) -- TOUS les corps de bloc (IF/WHILE/FOR, DEF,
        # routine, MODULE, pipe |) appellent exec_node(n) dans une boucle
        # `for n in bloc` qui ne revérifie stop_requested qu'à SA propre
        # boucle englobante (WHILE/FOR) ou au niveau run_sequence -- sans
        # ce garde ici, une instruction @STOP au milieu d'un bloc IF/DEF/
        # routine laisserait les instructions SUIVANTES du même bloc
        # s'exécuter quand même jusqu'à ce que le flux remonte à un point
        # qui revérifie le drapeau. Le garder ici, au seul endroit par
        # lequel passe CHAQUE instruction sans exception, rend l'arrêt
        # immédiat partout d'un coup, sans avoir à toucher individuellement
        # les neuf boucles de bloc existantes. Bénéfice secondaire : le
        # bouton ARRÊTER (même drapeau, cf. arreter_script) devient lui
        # aussi réactif immédiatement, y compris au milieu d'un bloc
        # imbriqué -- il ne l'était pas avant, silencieusement (l'arrêt
        # n'était garanti qu'« à la prochaine instruction DU MÊME NIVEAU »,
        # pas « à la toute prochaine instruction »).
        if self.error_flag or self.stop_requested or node is None:
            return ""
        if self.agent:
            self.agent.track_instruction(type(node).__name__)

        if self.debugger is not None and node.line:
            # Centralisé ICI plutôt que dans run_sequence seul : exec_node
            # est le point d'entrée UNIVERSEL, appelé aussi bien pour le
            # top-level (run_sequence) que pour chaque ligne à l'intérieur
            # d'un corps de fonction/routine/IF/WHILE (les neuf sites
            # `self.exec_node(n)` répartis dans ce fichier) -- un seul
            # branchement ici couvre tous les cas, sans avoir à modifier
            # individuellement chacun de ces sites. node.line (pas
            # self.current_run_line, qui n'est mis à jour QUE par
            # run_sequence au niveau top-level et resterait figé à la
            # ligne de l'appel CALL pendant l'exécution du corps d'une
            # fonction) -- node.line est TOUJOURS la vraie ligne en cours,
            # peu importe la profondeur d'imbrication.
            self.debugger.check_line(
                node.line,
                self.memoire,
                self._evaluate_debug_condition,
                self._debug_call_stack,
            )

        if isinstance(node, SignatureNode):
            return ""

        if isinstance(node, StopNode):
            # Sortie anticipée : même drapeau que le bouton ARRÊTER
            # (self.stop_requested), lu par le garde en tête de cette
            # méthode -- c'est CE garde qui arrête tout le reste du
            # programme, pas une exception ni un retour spécial. Ligne
            # mémorisée séparément (self._stop_line) pour que run_sequence
            # puisse distinguer dans son message « arrêté par @STOP à la
            # ligne N » d'un « interrompu par l'utilisateur » (bouton) --
            # les deux partagent stop_requested mais pas la même origine.
            self._stop_line = node.line
            self.stop_requested = True
            return ""

        if isinstance(node, ImportNode):
            return self._exec_import(node)

        if isinstance(node, ModuleNode):
            return self._exec_module(node)

        if isinstance(node, ExportNode):
            return self._exec_export(node)

        if isinstance(node, PackageNode):
            return self._exec_package_publish(node)

        if isinstance(node, DefNode):
            self.fonctions[node.name] = (node.params, node.body)
            self.ide.log(f"Fonction '{node.name}' déclarée.", "sys")
            return ""

        if isinstance(node, CallNode):
            return self._exec_call(node)

        if isinstance(node, RoutineDefNode):
            self.routines[node.name] = node.body
            self.ide.log(f"Routine '{node.name}' enregistrée.", "sys")
            return ""

        if isinstance(node, RoutineCallNode):
            if node.name not in self.routines:
                self._emit_error("UNKNOWN_ROUTINE", f"Routine inconnue : '{node.name}'", node.line)
                return ""
            # self.call_depth est le MÊME compteur que _exec_call (DEF/CALL)
            # utilise -- une routine et une fonction peuvent s'imbriquer
            # l'une dans l'autre, la protection doit compter les deux
            # ensemble, pas séparément, sinon 25 appels de routine +
            # 25 appels de fonction imbriqués contournerait une limite
            # à 50 pensée pour chaque mécanisme isolément.
            self.call_depth += 1
            if self.call_depth > MAX_CALL_DEPTH:
                self.call_depth -= 1
                self._emit_error("MAX_RECURSION", "Récursion maximale atteinte.", node.line)
                return ""
            cle = f"ROUTINE:{node.name}"
            self.named_call_counts[cle] = self.named_call_counts.get(cle, 0) + 1
            try:
                for n in self.routines[node.name]:
                    if self.error_flag:
                        break
                    self.exec_node(n)
            finally:
                self.call_depth -= 1
            return ""

        if isinstance(node, AssignNode):
            if node.is_call and node.call_node:
                val = self.exec_node(node.call_node)
            else:
                val = self.evaluate(node.expr)
            self._safe_store(node.var, val)
            return ""

        if isinstance(node, PrintNode):
            self.ide.log(str(self.evaluate(node.expr)))
            return ""

        if isinstance(node, IfNode):
            cond_val = self.evaluate(node.condition)
            if cond_val in (True, "True", 1):
                for n in node.then_block:
                    if self.error_flag:
                        break
                    self.exec_node(n)
            else:
                for n in node.else_block:
                    if self.error_flag:
                        break
                    self.exec_node(n)
            return ""

        if isinstance(node, WhileNode):
            tours = 0
            while self.evaluate(node.condition) in (True, "True", 1):
                if self.error_flag or self.stop_requested:
                    break
                tours += 1
                if tours > MAX_LOOP_ITERATIONS:
                    self._emit_error("INFINITE_LOOP", "Boucle infinie détectée.", node.line)
                    break
                for n in node.body:
                    if self.error_flag:
                        break
                    self.exec_node(n)
                if tours % 20 == 0:
                    # Etait self.ide.root.update() : seule dépendance Tk
                    # non gardée de l'évaluateur, cf. _pump_ui_events.
                    self._pump_ui_events()
            return ""

        if isinstance(node, ForNode):
            try:
                count = int(float(self.evaluate(node.count_expr)))
            except Exception:
                self._emit_error("INVALID_COUNT", "Compteur invalide.", node.line)
                return ""
            count = min(count, MAX_LOOP_ITERATIONS)
            for i in range(count):
                if self.error_flag or self.stop_requested:
                    break
                for n in node.body:
                    if self.error_flag:
                        break
                    self.exec_node(n)
                if i % 20 == 0:
                    # Idem WhileNode ci-dessus : passage par le point
                    # d'entrée unique _pump_ui_events.
                    self._pump_ui_events()
            return ""

        if isinstance(node, SleepNode):
            time.sleep(min(node.duration, 10.0))
            return ""

        if isinstance(node, InputNode):
            user_input = self.ide.request_input(node.var)
            if node.type_hint == "E":
                try:
                    valeur = int(user_input)
                except (ValueError, TypeError):
                    self._emit_error(
                        "INVALID_INPUT_TYPE",
                        f"Entrée entière attendue pour '{node.var}', reçu : '{user_input}'",
                        node.line,
                    )
                    return ""
                self._safe_store(node.var, valeur)
            elif node.type_hint == "R":
                try:
                    valeur = float(user_input)
                except (ValueError, TypeError):
                    self._emit_error(
                        "INVALID_INPUT_TYPE",
                        f"Entrée réelle attendue pour '{node.var}', reçu : '{user_input}'",
                        node.line,
                    )
                    return ""
                self._safe_store(node.var, valeur)
            else:
                # Comportement historique, totalement préservé : ?nom
                # sans préfixe stocke toujours la saisie brute en chaîne.
                self._safe_store(node.var, user_input)
            return ""

        if isinstance(node, TabNode):
            return self._exec_tab(node)

        if isinstance(node, StrNode):
            return self._exec_str(node)

        if isinstance(node, ShionNode):
            return self._exec_shion(node)

        if isinstance(node, DiskNode):
            return self._exec_disk(node)

        if isinstance(node, DrawNode):
            return self._exec_draw(node)

        if isinstance(node, KeyNode):
            return self._exec_key(node)

        if isinstance(node, LearnNode):
            if self.agent:
                self.agent.learn_from_terminal(node.error, node.correction)
            return ""

        if isinstance(node, SnippetNode):
            if self.agent:
                self.agent.generate_snippet(node.pattern)
            return ""

        if isinstance(node, PipeNode):
            for n in node.nodes:
                if self.error_flag:
                    break
                self.exec_node(n)
            return ""

        if isinstance(node, UnknownNode):
            if node.raw in self.routines:
                for n in self.routines[node.raw]:
                    if self.error_flag:
                        break
                    self.exec_node(n)
                return ""
            self._emit_error("UNKNOWN_SYNTAX", f"Syntaxe inconnue : '{node.raw}'", node.line)
            return ""

        return ""

    def _exec_import(self, node: ImportNode):
        nom_fic = str(self.evaluate(node.filename)).strip()
        if not nom_fic:
            self._emit_error("INVALID_IMPORT", "Nom de fichier/module invalide.", node.line)
            return ""
        # hasattr : même garde que UVIVirtualMachine._resolve_module_path
        # (ligne ~2228) -- sans elle, IMPORT plante en AttributeError brut
        # dès que self.vfs est un InMemoryVFS (aucune méthode get_path),
        # ce que fait justement HeadlessHost/InMemoryVFS, le harnais de
        # test officiel de ce projet (voir tests/). La VM avait déjà cette
        # garde, le tree-walker non -- oubli, pas un choix.
        chemin_vfs = self.vfs.get_path(nom_fic) if hasattr(self.vfs, "get_path") else None
        chemin_final = chemin_vfs if (chemin_vfs and os.path.exists(chemin_vfs)) else (nom_fic if os.path.exists(nom_fic) else None)
        if chemin_final:
            abs_path = os.path.abspath(chemin_final)
            if abs_path in self.imported_files:
                self._emit_error("CIRCULAR_IMPORT", f"Import circulaire bloqué : '{nom_fic}'.", node.line)
                return ""
            self.imported_files.add(abs_path)
            with open(chemin_final, 'r', encoding='utf-8') as f:
                content = f.read()
            self.run_sequence(content.splitlines(), is_sub_sequence=True)
            self.ide.log(f"Module '{nom_fic}' chargé.", "sys")
        else:
            self._emit_error("FILE_NOT_FOUND", f"Module '{nom_fic}' introuvable.", node.line)
        return ""

    def _default_module_alias(self, nom_fic: str) -> str:
        """Même logique et même justification que
        UVIVirtualMachine._default_module_alias côté VM -- dupliquée
        ici pour la même raison (garder les deux moteurs sans
        dépendance croisée)."""
        nom = nom_fic
        if nom.startswith("std:"):
            nom = nom[len("std:"):]
        elif nom.startswith("pkg:"):
            nom = nom[len("pkg:"):].partition("@")[0]
        return os.path.splitext(os.path.basename(nom))[0]

    def _resolve_module_path(self, nom_fic: str):
        """Même résolution que _exec_import (VFS puis chemin direct) --
        factorisée séparément pour MODULE afin que les deux mécanismes
        restent indépendants (voir commentaire sur imported_files vs
        loading_modules dans __init__), sans dupliquer la logique de
        résolution elle-même qui, elle, n'a aucune raison de diverger.

        Préfixes std:/pkg: (système de paquets, ajout de cette session)
        -- même logique et mêmes justifications que
        UVIVirtualMachine._resolve_module_path côté VM, dupliquée ici
        pour la même raison (garder les deux moteurs sans dépendance
        croisée)."""
        if nom_fic.startswith("std:"):
            registry = getattr(self.vfs, "package_registry", None)
            return registry.resolve_std(nom_fic[len("std:"):]) if registry else None
        if nom_fic.startswith("pkg:"):
            registry = getattr(self.vfs, "package_registry", None)
            if not registry:
                return None
            reste = nom_fic[len("pkg:"):]
            nom, _, version = reste.partition("@")
            return registry.resolve_pkg(nom, version if version else None)
        # hasattr : même garde et même raison que _exec_import ci-dessus
        # et que UVIVirtualMachine._resolve_module_path (ligne ~2228).
        chemin_vfs = self.vfs.get_path(nom_fic) if hasattr(self.vfs, "get_path") else None
        if chemin_vfs and os.path.exists(chemin_vfs):
            return chemin_vfs
        if os.path.exists(nom_fic):
            return nom_fic
        return None

    def _exec_module(self, node: ModuleNode):
        nom_fic = str(self.evaluate(node.filename)).strip()
        if not nom_fic:
            self._emit_error("INVALID_MODULE", "Nom de fichier de module invalide.", node.line)
            return ""
        alias = node.alias.strip() if node.alias else self._default_module_alias(nom_fic)
        if not alias:
            self._emit_error("INVALID_MODULE", "Alias de module invalide.", node.line)
            return ""
        chemin_final = self._resolve_module_path(nom_fic)
        if not chemin_final:
            self._emit_error("FILE_NOT_FOUND", f"Module '{nom_fic}' introuvable.", node.line)
            return ""
        abs_path = os.path.abspath(chemin_final)
        if abs_path in self.loading_modules:
            self._emit_error("CIRCULAR_MODULE", f"Chargement de module circulaire bloqué : '{nom_fic}'.", node.line)
            return ""
        # Rechargement du MÊME fichier sous un alias DÉJÀ pris : no-op
        # silencieux plutôt qu'erreur -- un script qui fait deux fois
        # MODULE "x.uvi" -> m par accident (ex. dans deux branches d'un
        # IF) ne doit pas planter, le module est simplement déjà là.
        # Un alias différent pour le même fichier physique est en
        # revanche parfaitement légal (voir _exec_module, deux entrées
        # self.modules distinctes, chacune ré-exécute le fichier dans
        # sa propre mémoire -- pas de partage entre alias).
        if alias in self.modules and self.modules[alias].source_path == abs_path:
            return ""
        with open(chemin_final, 'r', encoding='utf-8') as f:
            content = f.read()
        nouveau_module = UVIModule(alias=alias, source_path=abs_path)
        self.loading_modules.add(abs_path)
        # Isolation réelle : self.memoire/fonctions/routines sont
        # temporairement remplacés par ceux du nouveau module pendant
        # l'exécution de son corps, puis restaurés -- même schéma que
        # _exec_call (saved_memoire = self.memoire.copy() / restore en
        # finally), mais ici c'est un SWAP complet (pas une copie),
        # parce qu'on veut que exec_node() du corps du module écrive
        # DANS la mémoire du module, jamais dans celle de l'appelant.
        # self.error_flag/last_errors/call_depth restent PARTAGÉS et non
        # swappés : une erreur dans un module doit remonter comme une
        # vraie erreur du script appelant, pas être avalée en silence.
        saved_memoire = self.memoire
        saved_fonctions = self.fonctions
        saved_routines = self.routines
        # Sauvegarde de l'ANCIENNE valeur (pas None) : si ce module en
        # charge lui-même un autre via MODULE (imbrication légale),
        # _module_en_construction doit repointer vers CE module-ci une
        # fois le sous-module terminé, pas vers None -- sinon un EXPORT
        # placé après un MODULE imbriqué échouerait à tort avec
        # EXPORT_OUTSIDE_MODULE.
        saved_module_en_construction = self._module_en_construction
        try:
            self.memoire = nouveau_module.memoire
            self.fonctions = nouveau_module.fonctions
            self.routines = nouveau_module.routines
            self._module_en_construction = nouveau_module
            self.run_sequence(content.splitlines(), is_sub_sequence=True)
        finally:
            # nouveau_module.memoire/fonctions/routines sont les MÊMES
            # objets que self.memoire/fonctions/routines pendant le
            # bloc try (pas des copies) -- donc tout ce que le corps du
            # module a écrit est déjà dedans, restaurer self.* ci-dessous
            # ne perd rien.
            self.memoire = saved_memoire
            self.fonctions = saved_fonctions
            self.routines = saved_routines
            self._module_en_construction = saved_module_en_construction
            self.loading_modules.discard(abs_path)
        if not self.error_flag:
            self.modules[alias] = nouveau_module
            self.ide.log(f"Module '{nom_fic}' chargé sous l'alias '{alias}'.", "sys")
        return ""

    def _exec_export(self, node: ExportNode):
        nom = node.name.strip()
        if not nom:
            self._emit_error("INVALID_EXPORT", "Nom d'export invalide.", node.line)
            return ""
        # EXPORT n'a de sens qu'À L'INTÉRIEUR d'un fichier en cours de
        # chargement via MODULE (self.memoire/fonctions/routines sont
        # alors ceux du nouveau_module en construction, voir
        # _exec_module ci-dessus) -- appeler EXPORT au niveau du script
        # principal ou dans un IMPORT (inclusion textuelle, jamais de
        # module en cours de construction) n'a pas de sens et un nom ne
        # peut viser qu'une variable OU une routine déjà présente à cet
        # instant précis dans le scope courant, jamais les deux
        # sens confondus.
        if nom in self.memoire or nom in self.fonctions or nom in self.routines:
            # exports est un set -- ajouter le même nom deux fois (ex.
            # EXPORT répété par erreur) est un no-op naturel, pas une
            # erreur à signaler.
            pass
        else:
            self._emit_error("UNKNOWN_EXPORT_TARGET",
                              f"'{nom}' n'est ni une variable ni une routine définie à ce point.",
                              node.line)
            return ""
        # self.loading_modules ne donne pas directement accès à
        # l'objet UVIModule en construction -- mais comme self.memoire
        # EST nouveau_module.memoire pendant le chargement (swap, pas
        # copie, voir _exec_module), on ne peut pas ajouter à
        # nouveau_module.exports sans une référence à nouveau_module
        # lui-même. Solution : un pointeur explicite vers le module en
        # cours de construction, positionné par _exec_module.
        if self._module_en_construction is not None:
            self._module_en_construction.exports.add(nom)
        else:
            self._emit_error("EXPORT_OUTSIDE_MODULE",
                              "EXPORT ne peut être utilisé qu'à l'intérieur d'un fichier chargé via MODULE.",
                              node.line)
        return ""

    def _exec_call(self, node: CallNode):
        if '::' in node.name:
            # CALL alias::nom(args) : fonction exportée d'un module.
            # Détecté AVANT tout incrément de call_depth/function_calls
            # ici -- _exec_module_call gère entièrement son propre
            # comptage de profondeur, pour éviter un double contrôle de
            # MAX_RECURSION sur un seul appel qualifié. Comportement
            # historique de CALL nom(args) totalement inchangé pour
            # tout nom sans '::' (le reste de cette méthode, inchangé).
            alias, _, nom_func = node.name.partition('::')
            return self._exec_module_call(node, alias.strip(), nom_func.strip())

        self.call_depth += 1
        if self.call_depth > MAX_CALL_DEPTH:
            self.call_depth -= 1
            self._emit_error("MAX_RECURSION", "Récursion maximale atteinte.", node.line)
            return ""
        self.function_calls += 1
        if self.function_calls > MAX_FUNCTION_CALLS:
            self.call_depth -= 1
            self._emit_error("TOO_MANY_CALLS", "Trop d'appels de fonctions.", node.line)
            return ""
        cle = f"DEF:{node.name}"
        self.named_call_counts[cle] = self.named_call_counts.get(cle, 0) + 1

        saved_memoire = self.memoire.copy()
        saved_memory_usage = self.current_memory_usage
        retour_val = ""
        pushed_debug_frame = False
        try:
            if node.name not in self.fonctions:
                self._emit_error("UNKNOWN_FUNCTION", f"Fonction inconnue : '{node.name}'", node.line)
                return ""
            params, body_nodes = self.fonctions[node.name]
            if len(node.args) != len(params):
                self._emit_error("WRONG_ARG_COUNT", f"'{node.name}' attend {len(params)} argument(s).", node.line)
                return ""
            arg_values = [self.evaluate(a) for a in node.args]
            local_scope = saved_memoire.copy()
            for param, val in zip(params, arg_values):
                local_scope[param] = val
            local_scope['_retour'] = ""
            self.memoire = local_scope
            if self.debugger is not None:
                # Poussé APRÈS avoir lié les paramètres (local_scope
                # contient déjà leurs valeurs) -- une pile d'appel de
                # débogage sans les valeurs de paramètres serait
                # nettement moins utile à inspecter.
                self._debug_call_stack.append(UVIDebugFrame(
                    nom=node.name,
                    ligne_appel=node.line,
                    parametres={p: local_scope[p] for p in params},
                ))
                pushed_debug_frame = True
            for n in body_nodes:
                if self.error_flag:
                    break
                self.exec_node(n)
            retour_val = self.memoire.get('_retour', "")
        except Exception as e:
            _signaler_crash(e, source="tree_walker_call", error_code="RUNTIME_ERROR",
                             ast_node_type="CallNode", uvi_line=node.line)
            self._emit_error("RUNTIME_ERROR", f"Erreur dans la fonction : {e}", node.line)
            retour_val = ""
        finally:
            if pushed_debug_frame:
                self._debug_call_stack.pop()
            self.memoire = saved_memoire
            self.current_memory_usage = saved_memory_usage
            self.call_depth -= 1
        # « CALL f(x) -> dest » : on range le retour dans dest.
        #
        # Ce stockage est NOUVEAU. Avant la correction de _parse_call, la
        # destination n'arrivait jamais jusqu'ici (elle était avalée dans
        # les arguments), donc personne n'avait pu constater qu'aucun
        # moteur ne la consommait -- le bug de parsing masquait une
        # fonctionnalité jamais implémentée. Corriger l'un sans l'autre
        # aurait donné un « CALL f(5) -> r » qui parse bien et laisse r
        # vide : plus déroutant encore que l'erreur d'origine.
        #
        # _safe_store et non memoire[dest] = : c'est le chemin qui
        # comptabilise la mémoire et tronque les chaînes trop longues,
        # comme toute autre écriture de variable du langage.
        if getattr(node, "dest", None):
            self._safe_store(node.dest, retour_val)
        return retour_val

    def _exec_module_call(self, node: CallNode, alias: str, nom_func: str):
        """CALL alias::nom(args) : exécute nom_func DANS la mémoire du
        module alias (jamais celle de l'appelant), symétrique de
        _exec_call pour DEF normal mais avec le scope hérité qui est
        celui du MODULE plutôt que celui de l'appelant -- cohérent avec
        l'isolation de _exec_module (le corps d'une fonction de module
        voit les autres variables privées de SON module, jamais celles
        du script qui l'appelle)."""
        self.call_depth += 1
        if self.call_depth > MAX_CALL_DEPTH:
            self.call_depth -= 1
            self._emit_error("MAX_RECURSION", "Récursion maximale atteinte.", node.line)
            return ""
        self.function_calls += 1
        if self.function_calls > MAX_FUNCTION_CALLS:
            self.call_depth -= 1
            self._emit_error("TOO_MANY_CALLS", "Trop d'appels de fonctions.", node.line)
            return ""
        cle = f"DEF:{alias}::{nom_func}"
        self.named_call_counts[cle] = self.named_call_counts.get(cle, 0) + 1
        if alias not in self.modules:
            self.call_depth -= 1
            self._emit_error("UNKNOWN_MODULE", f"Module '{alias}' non chargé (utiliser MODULE d'abord).", node.line)
            return ""
        module = self.modules[alias]
        if not module.has_exported_callable(nom_func):
            self.call_depth -= 1
            self._emit_error("NOT_EXPORTED", f"'{nom_func}' n'est pas exporté par le module '{alias}'.", node.line)
            return ""
        if nom_func not in module.fonctions:
            # Exporté mais routine, pas fonction -- CALL ne sait
            # appeler que des fonctions DEF, jamais des routines (les
            # routines de module s'appellent différemment, voir
            # _exec_routine_call_module ci-dessous si besoin futur).
            self.call_depth -= 1
            self._emit_error("NOT_A_FUNCTION", f"'{alias}::{nom_func}' est une routine, pas une fonction (CALL attend une fonction DEF).", node.line)
            return ""
        params, body_nodes = module.fonctions[nom_func]
        if len(node.args) != len(params):
            self.call_depth -= 1
            self._emit_error("WRONG_ARG_COUNT", f"'{alias}::{nom_func}' attend {len(params)} argument(s).", node.line)
            return ""
        # Les arguments sont évalués dans le scope de L'APPELANT (self.memoire
        # actuel, celui du script qui fait le CALL) -- c'est la donnée qui
        # traverse la frontière du module, avant que le scope ne bascule.
        arg_values = [self.evaluate(a) for a in node.args]
        saved_memoire = self.memoire
        saved_memory_usage = self.current_memory_usage
        retour_val = ""
        try:
            local_scope = module.memoire.copy()
            for param, val in zip(params, arg_values):
                local_scope[param] = val
            local_scope['_retour'] = ""
            self.memoire = local_scope
            for n in body_nodes:
                if self.error_flag:
                    break
                self.exec_node(n)
            retour_val = self.memoire.get('_retour', "")
        except Exception as e:
            _signaler_crash(e, source="tree_walker_module_call", error_code="RUNTIME_ERROR",
                             ast_node_type="CallNode", uvi_line=node.line)
            self._emit_error("RUNTIME_ERROR", f"Erreur dans la fonction '{alias}::{nom_func}' : {e}", node.line)
            retour_val = ""
        finally:
            self.memoire = saved_memoire
            self.current_memory_usage = saved_memory_usage
            self.call_depth -= 1
        # « CALL alias::nom(x) -> dest » : BUG CORRIGÉ -- ce stockage
        # existe déjà pour CALL nom(x) -> dest à la fin de _exec_call
        # (juste après son propre try/finally), mais _exec_call RETOURNE
        # TÔT vers _exec_module_call dès qu'il voit '::' dans node.name
        # (voir plus haut, juste avant l'incrément de call_depth), donc ce
        # code-là n'était JAMAIS atteint pour un appel qualifié -- «
        # CALL m::carre(5) -> r » exécutait bien la fonction du module
        # (valeur de retour correcte dans retour_val), mais `r` restait
        # vide, silencieusement, aucune erreur. La VM n'a pas ce bug (le
        # STORE après CALL_MODULE_FUNC est émis par _compile_node_inner,
        # au niveau du nœud CallNode, pas dans _compile_call_node
        # lui-même) -- trouvé en creusant pourquoi
        # test_module_crashes_on_in_memory_vfs_BUG (tests/) échouait
        # encore sur la VALEUR après que le crash AttributeError original
        # (get_path non gardé) a été corrigé.
        if getattr(node, "dest", None):
            self._safe_store(node.dest, retour_val)
        return retour_val

    def _exec_tab(self, node: TabNode):
        cmd = node.cmd
        ops = node.operands

        if cmd in ('TAB.CREATE', 'TAB_EMPTY'):
            nom = ops[0].strip() if ops else ""
            if nom:
                self._safe_store(nom, [])
            return ""

        if cmd == 'TAB_INIT':
            nom = ops[0].strip() if ops else ""
            vals_str = ops[1] if len(ops) > 1 else ""
            val_tokens = self.lexer.tokenize(vals_str)
            raw_vals = self._parse_args_from_tokens_eval(val_tokens)
            vals = [self.evaluate(v) for v in raw_vals]
            if len(vals) > MAX_ARRAY_SIZE:
                self.ide.log(f"Tableau trop grand ({len(vals)} > {MAX_ARRAY_SIZE}).", "err")
                vals = vals[:MAX_ARRAY_SIZE]
            self._safe_store(nom, vals)
            return ""

        if cmd == 'TAB.PUSH':
            nom = ops[0].strip() if ops else ""
            val_str = ops[1] if len(ops) > 1 else ""
            val = self.evaluate(val_str)
            if isinstance(val, str) and len(val) > MAX_STRING_LENGTH:
                val = val[:MAX_STRING_LENGTH]
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                if len(self.memoire[nom]) >= MAX_ARRAY_SIZE:
                    self._emit_error("ARRAY_FULL", "Tableau plein.", node.line)
                else:
                    delta = sys.getsizeof(val)
                    if self.current_memory_usage + delta > MAX_MEMORY_BYTES:
                        self._emit_error("MEMORY_OVERFLOW", "Limite de mémoire atteinte.", node.line)
                        return ""
                    self.current_memory_usage += delta
                    self.memoire[nom].append(val)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.POP':
            nom = ops[0].strip() if ops else ""
            var_dest = ops[1].strip() if len(ops) >= 2 else None
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                if self.memoire[nom]:
                    val = self.memoire[nom].pop()
                    self.current_memory_usage = max(0, self.current_memory_usage - sys.getsizeof(val))
                    if var_dest:
                        self._safe_store(var_dest, val)
                else:
                    self._emit_error("EMPTY_ARRAY", f"Tableau '{nom}' vide.", node.line)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.GET':
            nom = ops[0].strip() if ops else ""
            idx_str = ops[1] if len(ops) > 1 else "0"
            var_dest = ops[2].strip() if len(ops) > 2 else ""
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                try:
                    idx = int(float(self.evaluate(idx_str)))
                    arr = self.memoire[nom]
                    if 0 <= idx < len(arr):
                        self._safe_store(var_dest, arr[idx])
                    else:
                        self._emit_error("INDEX_OUT_OF_BOUNDS", f"Index {idx} hors limites (taille {len(arr)}).", node.line)
                except Exception:
                    self._emit_error("INVALID_INDEX", "Index invalide.", node.line)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.SET':
            nom = ops[0].strip() if ops else ""
            idx_str = ops[1] if len(ops) > 1 else "0"
            val_str = ops[2] if len(ops) > 2 else ""
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                try:
                    idx = int(float(self.evaluate(idx_str)))
                    arr = self.memoire[nom]
                    if 0 <= idx < len(arr):
                        val = self.evaluate(val_str)
                        if isinstance(val, str) and len(val) > MAX_STRING_LENGTH:
                            val = val[:MAX_STRING_LENGTH]
                        old_val = arr[idx]
                        delta = sys.getsizeof(val) - sys.getsizeof(old_val)
                        if self.current_memory_usage + delta > MAX_MEMORY_BYTES:
                            self._emit_error("MEMORY_OVERFLOW", "Limite de mémoire.", node.line)
                            return ""
                        self.current_memory_usage = max(0, self.current_memory_usage + delta)
                        arr[idx] = val
                    else:
                        self._emit_error("INDEX_OUT_OF_BOUNDS", f"Index {idx} hors limites (taille {len(arr)}).", node.line)
                except Exception:
                    self._emit_error("INVALID_INDEX", "Index invalide.", node.line)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.DEL':
            nom = ops[0].strip() if ops else ""
            idx_str = ops[1] if len(ops) > 1 else "0"
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                try:
                    idx = int(float(self.evaluate(idx_str)))
                    arr = self.memoire[nom]
                    if 0 <= idx < len(arr):
                        val = self.memoire[nom].pop(idx)
                        self.current_memory_usage = max(0, self.current_memory_usage - sys.getsizeof(val))
                    else:
                        self._emit_error("INDEX_OUT_OF_BOUNDS", f"Index {idx} hors limites.", node.line)
                except Exception:
                    self._emit_error("INVALID_INDEX", "Index invalide.", node.line)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.LEN':
            nom = ops[0].strip() if ops else ""
            var_dest = ops[1].strip() if len(ops) >= 2 else ""
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                self._safe_store(var_dest, len(self.memoire[nom]))
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        if cmd == 'TAB.PRINT':
            nom = ops[0].strip() if ops else ""
            if nom in self.memoire and isinstance(self.memoire[nom], list):
                arr = self.memoire[nom]
                if not arr:
                    self.ide.log(f"[{nom}] vide")
                else:
                    for idx, val in enumerate(arr):
                        self.ide.log(f"[{nom}][{idx}] = {val}")
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom}' n'est pas un tableau.", node.line)
            return ""

        return ""

    def _exec_str(self, node: StrNode):
        cmd = node.cmd
        ops = node.operands

        def ev(i):
            return self.evaluate(ops[i]) if i < len(ops) else ""

        if cmd == 'STR.LEN' and len(ops) >= 2:
            self._safe_store(ops[1].strip(), len(str(ev(0))))
        elif cmd == 'STR.NUM' and len(ops) >= 2:
            # BUG CORRIGÉ (divergence tree-walker/VM) : STR.NUM était géré
            # côté VM (CALL_BUILTIN "STR_NUM", voir plus haut dans ce
            # fichier) mais absent de ce elif -- aucune des branches
            # ci-dessous ne matche "STR.NUM", donc l'appel tombait
            # silencieusement dans le `return ""` final SANS jamais
            # appeler _safe_store : la variable de destination restait
            # non définie (donc "" une fois relue), sans la moindre
            # erreur. Même sémantique que STR_NUM côté VM : int() d'abord,
            # repli sur float(), sinon erreur explicite plutôt qu'un
            # résultat silencieusement absent.
            texte = str(ev(0)).strip()
            try:
                valeur = int(texte)
            except ValueError:
                try:
                    valeur = float(texte)
                except ValueError:
                    self._emit_error("INVALID_NUMBER", f"'{texte}' n'est pas un nombre valide.", node.line)
                    return ""
            self._safe_store(ops[1].strip(), valeur)
        elif cmd == 'STR.UPPER' and len(ops) >= 2:
            self._safe_store(ops[1].strip(), str(ev(0)).upper())
        elif cmd == 'STR.LOWER' and len(ops) >= 2:
            self._safe_store(ops[1].strip(), str(ev(0)).lower())
        elif cmd == 'STR.TRIM' and len(ops) >= 2:
            self._safe_store(ops[1].strip(), str(ev(0)).strip())
        elif cmd == 'STR.SPLIT' and len(ops) >= 3:
            nom = str(ev(0))
            sep = str(ev(1))
            tab_dest = ops[2].strip()
            self._safe_store(tab_dest, nom.split(sep) if sep else list(nom))
        elif cmd == 'STR.JOIN' and len(ops) >= 3:
            tab_nom = ops[0].strip()
            sep = str(ev(1))
            var_dest = ops[2].strip()
            if tab_nom in self.memoire and isinstance(self.memoire[tab_nom], list):
                arr = [str(x) for x in self.memoire[tab_nom]]
                self._safe_store(var_dest, sep.join(arr))
            else:
                self._emit_error("NOT_A_ARRAY", f"'{tab_nom}' n'est pas un tableau.", node.line)
        elif cmd == 'STR.REPLACE' and len(ops) >= 4:
            nom = str(ev(0))
            old = str(ev(1))
            new = str(ev(2))
            var_dest = ops[3].strip()
            self._safe_store(var_dest, nom.replace(old, new))
        elif cmd == 'STR.CONTAINS' and len(ops) >= 3:
            self._safe_store(ops[2].strip(), str(ev(1)) in str(ev(0)))
        elif cmd == 'STR.STARTS' and len(ops) >= 3:
            self._safe_store(ops[2].strip(), str(ev(0)).startswith(str(ev(1))))
        elif cmd == 'STR.ENDS' and len(ops) >= 3:
            self._safe_store(ops[2].strip(), str(ev(0)).endswith(str(ev(1))))
        elif cmd == 'STR.SLICE' and len(ops) >= 4:
            nom = str(ev(0))
            try:
                debut = int(float(self.evaluate(ops[1])))
                fin = int(float(self.evaluate(ops[2])))
                self._safe_store(ops[3].strip(), nom[debut:fin])
            except Exception:
                self._emit_error("INVALID_SLICE", "Indices de SLICE invalides.", node.line)
        elif cmd == 'STR.INDEX' and len(ops) >= 3:
            self._safe_store(ops[2].strip(), str(ev(0)).find(str(ev(1))))
        return ""

    def _exec_shion(self, node: ShionNode):
        """
        SHION.ASK "prompt" -> var_resultat

        Non-bloquant par choix : la requête HTTP part dans un thread daemon
        et exec_node() retourne immédiatement. var_resultat est stocké
        d'abord avec la sentinelle SHION_PENDING, puis écrasé par la vraie
        réponse quand elle arrive. La console reste utilisable pendant ce
        temps — self.shion_is_busy empêche seulement d'EMPILER une seconde
        requête avant que la première ait fini, il ne bloque rien côté UI.

        Conséquence à connaître : si une ligne suivante lit var_resultat
        avant que la réponse soit revenue, elle lira SHION_PENDING (ou une
        réponse d'un appel SHION.ASK précédent si la variable était déjà
        utilisée), pas une erreur silencieuse ni un blocage. C'est un choix
        assumé plutôt qu'un oubli — voir _shion_worker pour l'écriture
        différée du résultat.
        """
        cmd = node.cmd
        ops = node.operands

        if cmd != 'SHION.ASK' or len(ops) < 2:
            self._emit_error(
                "SHION_SYNTAX", 'Syntaxe attendue : SHION.ASK "prompt" -> var_resultat',
                node.line,
            )
            return ""

        client, motif = self._resoudre_client_shion()
        if motif == "connexion":
            self._emit_error(
                "SHION_LOGIN_REQUIRED",
                "SHION.ASK nécessite un compte UVI connecté.",
                node.line,
            )
            return ""
        if motif == "abonnement":
            self._emit_error(
                "SHION_TIER_LOCKED",
                "SHION.ASK n'est pas inclus dans l'abonnement Gratuit.",
                node.line,
            )
            return ""
        if not HAS_SHION or client is None:
            self._emit_error(
                "SHION_UNAVAILABLE",
                "Aucun backend SHION.ASK disponible ici (paquet shion/ absent, "
                "ou serveur configuré injoignable). "
                "SHION.ASK ne peut pas s'exécuter dans ce contexte.",
                node.line,
            )
            return ""

        # Garde anti-spam : une seule requête SHION.ASK en vol à la fois.
        # N'empêche PAS de continuer à utiliser la console/éditeur pendant
        # l'attente — juste d'empiler un second appel réseau avant que le
        # premier ait répondu. Le chemin script (run_sequence, séquentiel
        # ligne par ligne) et le chemin console (un seul callback Tkinter à
        # la fois) n'exécutent jamais _exec_shion en parallèle avec
        # lui-même, donc ce check n'a pas besoin d'un verrou (Lock) pour
        # rester correct ici — mais s'il devient un jour appelable depuis
        # plusieurs threads concurrents, il en faudra un.
        if self.shion_is_busy:
            self._emit_error(
                "SHION_BUSY",
                "Une requête SHION.ASK est déjà en cours — attends la réponse "
                "avant d'en relancer une.",
                node.line,
            )
            return ""

        prompt = str(self.evaluate(ops[0]))
        # Même plafond que _safe_store (MAX_STRING_LENGTH), appliqué ICI en
        # plus : ops[0] peut être une expression évaluée à la volée (ex.
        # concaténation de plusieurs variables déjà au plafond chacune) et
        # n'a donc pas forcément transité par _safe_store avant d'atteindre
        # ce point -- sans ce clamp, un script pourrait envoyer un prompt
        # plus long que ce que toute variable UVI ne pourrait jamais
        # contenir, gaspillant des tokens/coûts d'API pour rien d'utile.
        if len(prompt) > MAX_STRING_LENGTH:
            prompt = prompt[:MAX_STRING_LENGTH]
        var_dest = ops[1].strip()

        if not prompt.strip():
            self._emit_error("SHION_EMPTY_PROMPT", "Prompt SHION.ASK vide.", node.line)
            return ""

        # Sentinelle + statut "busy" posés en synchrone, avant le lancement
        # du thread — donc sur le thread appelant (thread worker de
        # run_script, ou thread Tkinter principal pour run_command).
        # _safe_store peut logguer (troncature, saturation mémoire) mais ce
        # log part d'un contexte où toucher Tkinter est déjà sûr ici.
        self.shion_is_busy = True
        self._safe_store(var_dest, SHION_PENDING)
        self.ide.log(f"SHION.ASK envoyé (en attente de réponse pour '{var_dest}')…", "sys")
        self.ide._set_status(f"⏳ Shion réfléchit… (résultat -> {var_dest})")

        thread = threading.Thread(
            target=self._shion_worker, args=(prompt, var_dest, node.line, client), daemon=True,
        )
        thread.start()
        return ""

    def _poster_ide(self, travail):
        """Fait exécuter `travail` sur le thread d'interface de l'hôte.

        getattr défensif, comme _pump_ui_events et _exec_draw : un hôte
        headless n'a ni file d'attente ni boucle Tk, et le travail est
        alors exécuté directement -- il n'y a pas de thread d'interface à
        protéger dans ce cas.

        Corrige la même faute que pour la saisie : _shion_worker tournait
        dans un thread daemon et appelait self.ide.root.after(0, ...), soit
        un appel Tcl depuis un thread secondaire. Le commentaire d'origine
        de cette méthode disait déjà passer par root.after « pour la
        sécurité » -- l'intention était juste, le moyen ne l'était pas."""
        poster = getattr(self.ide, "_poster_ui", None)
        if callable(poster):
            poster(travail)
        else:
            travail()

    def _shion_worker(self, prompt: str, var_dest: str, line: int, client=None):
        """
        Tourne sur un thread séparé. NE JAMAIS toucher un widget Tkinter
        directement ici (Tkinter n'est pas thread-safe) — tout ce qui touche
        self.ide (log, statusbar, etc.) ou déclenche un log via
        _safe_store/_emit_error doit passer par self.ide.root.after(0, ...),
        comme le fait déjà run_script() ailleurs dans ce fichier pour son
        propre callback de fin.

        `client` est le client déjà résolu par _exec_shion (via
        _resoudre_client_shion -- tient compte de l'abonnement). Paramètre
        optionnel avec repli sur self.shion_client pour tout appelant
        antérieur à l'ajout des niveaux d'abonnement.

        self.shion_is_busy est remis à False dans tous les cas (succès ou
        échec) avant de retourner, pour ne jamais laisser SHION.ASK bloqué
        indéfiniment après une erreur réseau.
        """
        if client is None:
            client = self.shion_client
        try:
            # Note : timeout=60s est câblé en dur dans ShionServingClient.complete()
            # (serving.py), pas paramétrable depuis ici sans modifier serving.py —
            # hors scope de cette intégration. SHION_REQUEST_TIMEOUT_S (config
            # ci-dessus) documente cette valeur mais ne la contrôle pas.
            result = client.complete(prompt)
        except Exception as e:
            # Python supprime automatiquement la variable `e` d'un bloc
            # except à la sortie de ce bloc (voir la doc du langage sur
            # `except...as`). Comme _on_error s'exécute plus tard, via
            # root.after (donc APRÈS la sortie de ce except), une closure
            # qui référencerait `e` directement lèverait un NameError au
            # moment de l'exécution différée. On capture donc le message en
            # str dès maintenant, dans une variable qui n'est pas nettoyée.
            error_message = str(e)

            def _on_error():
                self.shion_is_busy = False
                self._emit_error(
                    "SHION_REQUEST_FAILED", f"Échec requête Shion : {error_message}", line,
                )
                self.ide._set_status("Prêt.")
                # Notification flottante EN PLUS du log console : la réponse
                # arrive en différé (jusqu'à DEEPSEEK_REQUEST_TIMEOUT_S), le
                # temps que l'utilisateur ait pu changer d'onglet -- un log
                # ajouté à une console qu'on ne regarde plus passe inaperçu.
                if hasattr(self.ide, "_empiler_toast"):
                    self.ide._empiler_toast("SHION.ASK a échoué — voir la console.", "err")
            self._poster_ide(_on_error)
            return

        def _on_success():
            self.shion_is_busy = False
            self._safe_store(var_dest, result)
            self.ide.log(f"SHION.ASK terminé, résultat stocké dans '{var_dest}'.", "ok")
            self.ide._set_status("Prêt.")
            if hasattr(self.ide, "_empiler_toast"):
                self.ide._empiler_toast("SHION a répondu → %s" % var_dest, "ok")
        self._poster_ide(_on_success)

    def _exec_disk(self, node: DiskNode):
        cmd = node.cmd
        ops = node.operands
        if cmd == '&save' and len(ops) >= 2:
            nom_fic = str(self.evaluate(ops[0])).strip()
            contenu = str(self.evaluate(ops[1]))
            if nom_fic:
                self.vfs.write_file(nom_fic, contenu)
                self.ide.log(f"Fichier '{nom_fic}' écrit.", "ok")
                self.ide.update_vfs_list()
            else:
                self._emit_error("MISSING_FILENAME", "Nom de fichier manquant.", node.line)
        elif cmd == '&load' and len(ops) >= 2:
            nom_fic = str(self.evaluate(ops[0])).strip()
            var_dest = ops[1].strip()
            if nom_fic:
                file_content = self.vfs.read_file(nom_fic)
                if file_content is not None:
                    self._safe_store(var_dest, file_content.rstrip('\r\n'))
                    self.ide.log(f"Fichier '{nom_fic}' chargé.", "ok")
                else:
                    self._emit_error("FILE_NOT_FOUND", "Fichier introuvable.", node.line)
            else:
                self._emit_error("MISSING_FILENAME", "Nom de fichier manquant.", node.line)
        elif cmd == '&list':
            self.ide.log(f"VFS: {', '.join(self.vfs.list_files()) or 'vide'}", "sys")
        elif cmd == '&delete' and len(ops) >= 1:
            nom_fic = str(self.evaluate(ops[0])).strip()
            if nom_fic:
                if self.vfs.delete_file(nom_fic):
                    self.ide.log(f"Fichier '{nom_fic}' effacé.", "ok")
                    self.ide.update_vfs_list()
                else:
                    self._emit_error("FILE_NOT_FOUND", "Fichier introuvable.", node.line)
            else:
                self._emit_error("MISSING_FILENAME", "Nom de fichier manquant.", node.line)
        return ""

    def _exec_draw(self, node: DrawNode):
        """DRAW.* -> délègue l'effet de bord graphique à self.ide, exactement
        comme &list délègue à self.ide.log (ligne 4727) ou ?INPUT à
        self.ide.request_input : l'évaluateur ne connaît jamais Tkinter
        directement, il appelle une méthode de contrat sur self.ide
        (canvas_draw), à charge de l'IDE de savoir la rendre. Si l'IDE
        n'expose pas canvas_draw (cas non censé arriver dans UVIIDE, mais
        prudence si ce tree-walker est un jour réutilisé hors IDE Tkinter),
        DRAW_UNAVAILABLE est émis plutôt qu'un AttributeError brut."""
        cmd = node.cmd
        ops = node.operands

        def ev(i, default=0):
            return self.evaluate(ops[i]) if i < len(ops) and str(ops[i]).strip() != "" else default

        if not hasattr(self.ide, "canvas_draw"):
            self._emit_error("DRAW_UNAVAILABLE", "Le canevas graphique n'est pas disponible ici.", node.line)
            return ""

        if cmd == "DRAW.CLEAR":
            self.ide.canvas_draw("clear")
        elif cmd == "DRAW.COLOR" and len(ops) >= 1:
            # DRAW.COLOR "#RRGGBB" -> couleur de fond du canevas.
            self.ide.canvas_draw("bgcolor", str(ev(0, "")))
        elif cmd == "DRAW.RECT" and len(ops) >= 4:
            x, y, w, h = ev(0), ev(1), ev(2), ev(3)
            couleur = str(ev(4, "#00FF00")) if len(ops) >= 5 else "#00FF00"
            # 6e opérande optionnelle = remplissage ; défaut "" (contour
            # seul) identique au comportement historique -- ne casse
            # aucun script existant qui ne fournit que 4 ou 5 opérandes.
            remplissage = str(ev(5, "")) if len(ops) >= 6 else ""
            # 7e opérande optionnelle = épaisseur du contour ; défaut 1,
            # valeur par défaut RÉELLE de Tkinter pour create_rectangle
            # (vérifié dans la doc officielle, pas supposé) -- absente,
            # le rendu est donc identique au comportement historique.
            epaisseur = int(self._draw_num(ev(6, 1))) if len(ops) >= 7 else 1
            self.ide.canvas_draw("rect", self._draw_num(x), self._draw_num(y),
                                  self._draw_num(w), self._draw_num(h), couleur,
                                  remplissage, epaisseur)
        elif cmd == "DRAW.CIRCLE" and len(ops) >= 3:
            x, y, r = ev(0), ev(1), ev(2)
            couleur = str(ev(3, "#00FF00")) if len(ops) >= 4 else "#00FF00"
            remplissage = str(ev(4, "")) if len(ops) >= 5 else ""
            epaisseur = int(self._draw_num(ev(5, 1))) if len(ops) >= 6 else 1
            self.ide.canvas_draw("circle", self._draw_num(x), self._draw_num(y),
                                  self._draw_num(r), couleur, remplissage, epaisseur)
        elif cmd == "DRAW.LINE" and len(ops) >= 4:
            x1, y1, x2, y2 = ev(0), ev(1), ev(2), ev(3)
            couleur = str(ev(4, "#00FF00")) if len(ops) >= 5 else "#00FF00"
            # 6e opérande optionnelle = épaisseur du trait ; défaut 1,
            # comportement historique exact si absente (voir même
            # justification que DRAW.RECT ci-dessus).
            epaisseur = int(self._draw_num(ev(5, 1))) if len(ops) >= 6 else 1
            self.ide.canvas_draw("line", self._draw_num(x1), self._draw_num(y1),
                                  self._draw_num(x2), self._draw_num(y2), couleur, epaisseur)
        elif cmd == "DRAW.TEXT" and len(ops) >= 3:
            x, y = ev(0), ev(1)
            texte = str(ev(2, ""))
            couleur = str(ev(3, "#00FF00")) if len(ops) >= 4 else "#00FF00"
            # 5e opérande optionnelle = taille de police ; défaut 10,
            # comportement historique exact si absente.
            taille_police = int(self._draw_num(ev(4, 10))) if len(ops) >= 5 else 10
            self.ide.canvas_draw("text", self._draw_num(x), self._draw_num(y), texte, couleur,
                                  taille_police)
        elif cmd == "DRAW.SPRITE" and len(ops) >= 3:
            # DRAW.SPRITE nom_tab -> x -> y -> taille : nom_tab est un NOM
            # DE TABLEAU non évalué (même convention que STR.JOIN, ops[0]
            # jamais passé à evaluate()) -- un tableau UVI plat de couleurs
            # ("#RRGGBB" ou nom Tk comme "black"), affiché en grille carrée
            # (côté = racine(longueur), arrondi au carré parfait inférieur).
            nom_tab = ops[0].strip()
            x, y = ev(1), ev(2)
            taille = ev(3, 16) if len(ops) >= 4 else 16
            if nom_tab in self.memoire and isinstance(self.memoire[nom_tab], list):
                pixels = self.memoire[nom_tab]
                self.ide.canvas_draw("sprite", self._draw_num(x), self._draw_num(y),
                                      self._draw_num(taille), list(pixels))
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom_tab}' n'est pas un tableau.", node.line)
        elif cmd == "DRAW.POLY" and len(ops) >= 1:
            # DRAW.POLY nom_tab -> couleur -> remplissage(optionnel) ->
            # épaisseur(optionnel) : même convention que DRAW.SPRITE,
            # nom_tab est un NOM DE TABLEAU non évalué (ops[0] jamais
            # passé à evaluate()). Le tableau contient des coordonnées
            # PLATES x1,y1,x2,y2,... (pas des paires), donc longueur
            # PAIRE obligatoire et au moins 3 points (6 éléments) pour
            # former un polygone.
            nom_tab = ops[0].strip()
            couleur = str(ev(1, "#00FF00")) if len(ops) >= 2 else "#00FF00"
            remplissage = str(ev(2, "")) if len(ops) >= 3 else ""
            epaisseur = int(self._draw_num(ev(3, 1))) if len(ops) >= 4 else 1
            if nom_tab in self.memoire and isinstance(self.memoire[nom_tab], list):
                coords = self.memoire[nom_tab]
                if len(coords) % 2 != 0 or len(coords) < 6:
                    self._emit_error("INVALID_POLYGON",
                                      f"'{nom_tab}' doit contenir un nombre pair de coordonnées "
                                      f"et au moins 3 points (6 valeurs), trouvé {len(coords)}.",
                                      node.line)
                else:
                    points = [self._draw_num(v) for v in coords]
                    self.ide.canvas_draw("poly", points, couleur, remplissage, epaisseur)
            else:
                self._emit_error("NOT_A_ARRAY", f"'{nom_tab}' n'est pas un tableau.", node.line)
        else:
            self._emit_error("UNKNOWN_DRAW_COMMAND", f"Commande DRAW inconnue ou arité insuffisante : {cmd}", node.line)
        return ""

    def _exec_package_publish(self, node: PackageNode):
        """PACKAGE.PUBLISH nom -> fichier -> version : copie le contenu
        de fichier (résolu comme n'importe quel MODULE -- VFS puis
        chemin direct, PAS de préfixe std:/pkg: accepté ici, publier UN
        paquet publié ou la bibliothèque standard elle-même n'a pas de
        sens et serait une source de confusion inutile) vers le
        registre de paquets, sous (nom, version).

        self.vfs.package_registry peut être absent (même prudence que
        DRAW_UNAVAILABLE pour canvas_draw ci-dessus, cf. _exec_draw) --
        PACKAGE_UNAVAILABLE plutôt qu'un AttributeError brut."""
        ops = node.operands
        if len(ops) < 3:
            self._emit_error("WRONG_ARG_COUNT", "PACKAGE.PUBLISH attend nom -> fichier -> version.", node.line)
            return ""
        registry = getattr(self.vfs, "package_registry", None)
        if registry is None:
            self._emit_error("PACKAGE_UNAVAILABLE", "Le système de paquets n'est pas disponible ici.", node.line)
            return ""
        nom = str(self.evaluate(ops[0])).strip()
        fichier = str(self.evaluate(ops[1])).strip()
        version = str(self.evaluate(ops[2])).strip()
        if not nom or not version:
            self._emit_error("INVALID_PACKAGE", "Nom ou version de paquet invalide.", node.line)
            return ""
        if fichier.startswith("std:") or fichier.startswith("pkg:"):
            self._emit_error("INVALID_PACKAGE", "Impossible de publier un paquet ou la bibliothèque standard eux-mêmes.", node.line)
            return ""
        chemin_final = self._resolve_module_path(fichier)
        if not chemin_final:
            self._emit_error("FILE_NOT_FOUND", f"Fichier à publier introuvable : '{fichier}'.", node.line)
            return ""
        registry.publish(nom, chemin_final, version)
        self.ide.log(f"Paquet '{nom}' publié en version '{version}'.", "sys")
        return ""

    def _exec_key(self, node: KeyNode):
        """KEY.DOWN/KEY.PRESSED "touche" -> dest : délègue à
        self.ide.key_is_down / self.ide.key_was_pressed, même principe
        de contrat que _exec_draw délègue à self.ide.canvas_draw. Écrit
        1 ou 0 (int Python, pas True/False) dans dest -- cohérent avec
        cond_val in (True, "True", 1), la vérité "legacy" déjà utilisée
        par _exec_if/_exec_while pour évaluer une condition UVI, de sorte
        que `~ touche == 1 -> ...` et `~ touche -> ...` fonctionnent
        tous les deux une fois KEY.DOWN stocké dans `touche`."""
        cmd = node.cmd
        ops = node.operands
        if len(ops) < 2:
            self._emit_error("MISSING_ARGS", f"{cmd} nécessite une touche et une variable destination.", node.line)
            return ""

        touche = self.evaluate(ops[0])
        dest = ops[1].strip()

        if not hasattr(self.ide, "key_is_down") or not hasattr(self.ide, "key_was_pressed"):
            self._emit_error("KEY_UNAVAILABLE", "La lecture clavier n'est pas disponible ici.", node.line)
            return ""

        if cmd == "KEY.DOWN":
            valeur = 1 if self.ide.key_is_down(touche) else 0
        elif cmd == "KEY.PRESSED":
            valeur = 1 if self.ide.key_was_pressed(touche) else 0
        else:
            self._emit_error("UNKNOWN_KEY_COMMAND", f"Commande KEY inconnue : {cmd}", node.line)
            return ""

        self._safe_store(dest, valeur)
        return ""

    @staticmethod
    def _draw_num(v):
        """Coercition tolérante en float pour les paramètres géométriques
        DRAW.* : une expression UVI peut renvoyer un int, un float, ou une
        chaîne numérique ('12' issu d'un ?INPUT non converti via STR.NUM).
        Retombe sur 0.0 plutôt que de lever, pour rester cohérent avec la
        philosophie legacy_truthy/valeur-vide du reste du langage (ex.
        _lookup renvoie "" pour une variable non définie, jamais d'erreur)."""
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _parse_args_from_tokens_eval(self, tokens):
        args, current, depth = [], [], 0
        for t in tokens:
            if t.type == 'LPAREN':
                depth += 1
                current.append(t)
            elif t.type == 'RPAREN':
                depth -= 1
                current.append(t)
            elif t.type == 'COMMA' and depth == 0:
                args.append(self._reconstruct_expr(current))
                current = []
            else:
                current.append(t)
        if current:
            args.append(self._reconstruct_expr(current))
        return args

    def run_sequence(self, lines, is_sub_sequence=False):
        if not is_sub_sequence:
            self.is_running = True
            self.call_depth = 0
            self.function_calls = 0
            self.error_flag = False
            self.last_errors = []
            self.imported_files.clear()

        ast_nodes = self.parser.parse_program(lines)

        for node in ast_nodes:
            if self.error_flag:
                break
            if self.stop_requested:
                # Pas un _emit_error : une interruption VOULUE n'est pas une
                # erreur du script, et la signaler comme telle déclencherait
                # l'auto-réparation de l'agent sur un script sain.
                if self._stop_line:
                    self.ide.log(f"⏹ Sortie anticipée : @STOP (ligne {self._stop_line}).", "sys")
                else:
                    self.ide.log("⏹ Exécution interrompue par l'utilisateur.", "sys")
                break
            self.current_run_line = node.line if node.line > 0 else 0
            try:
                self.exec_node(node)
            except Exception as e:
                _signaler_crash(e, source="tree_walker", error_code="CRASH",
                                 ast_node_type=type(node).__name__, uvi_line=node.line)
                self._emit_error("CRASH", f"Crash ligne {node.line} : {e}", node.line)

        if not is_sub_sequence:
            self.is_running = False
            self.current_run_line = 0

    def run_single_command(self, cmd):
        self.error_flag = False
        node = None
        try:
            node = self.parser.parse_line(cmd, 0)
            if node:
                self.exec_node(node)
        except Exception as e:
            _signaler_crash(e, source="tree_walker_repl", error_code="RUNTIME_ERROR",
                             ast_node_type=type(node).__name__ if node else None)
            self._emit_error("RUNTIME_ERROR", str(e))


class UVIEngine(UVIEvaluator):
    pass


class HeadlessHost:
    """Hôte minimal pour faire tourner UVIEvaluator/UVIEngine SANS Tkinter :
    CLI, tests automatisés, intégration continue, exécution serveur.

    POURQUOI CETTE CLASSE EXISTE (constat mesuré, pas anticipation).
    L'étape 2 a remplacé les self.ide.root.update() par _pump_ui_events, ce
    qui a supprimé le plantage des boucles. Mais un inventaire des
    dépendances de UVIEvaluator à self.ide a montré que ça ne SUFFISAIT
    PAS :

        18×  self.ide.log              <- AUCUNE garde
         9×  self.ide.canvas_draw      <- gardé (hasattr, _exec_draw)
         4×  self.ide.root             <- 2 corrigés, 2 restants (_shion_worker)
         3×  self.ide._set_status      <- AUCUNE garde
         2×  self.ide.update_vfs_list  <- AUCUNE garde
         2×  self.ide.request_input    <- AUCUNE garde
         2×  self.ide.key_is_down / key_was_pressed  <- gardés (_exec_key)

    Autrement dit : le premier !"bonjour" d'un script headless plantait sur
    AttributeError: 'X' object has no attribute 'log'.

    Deux façons de fermer ça : semer dix-huit hasattr dans l'évaluateur, ou
    écrire l'hôte que ces dix-huit appels attendent. La seconde est plus
    courte, ne touche à AUCUN code existant, et donne en prime un endroit où
    la sortie est CAPTURÉE (self.sortie) -- ce dont les tests d'équivalence
    tree-walker/VM ont besoin de toute façon pour comparer deux exécutions.

    CE QUI EST VOLONTAIREMENT ABSENT, et c'est le cœur du dispositif :
    pump_events, canvas_draw, key_is_down, key_was_pressed ne sont PAS
    définis ici. Les gardes existantes (_exec_draw, _exec_key,
    _pump_ui_events) détectent leur absence et dégradent proprement -- un
    script qui dessine ne plante pas, il émet DRAW_UNAVAILABLE. Ajouter des
    versions factices de ces méthodes ferait CROIRE à l'évaluateur qu'un
    canevas existe, et transformerait une erreur claire en dessin silencieux
    dans le vide.

    LIMITE ASSUMÉE : request_input rend toujours la même valeur (par défaut
    la chaîne vide). Un script qui lit une saisie utilisateur n'est pas
    testable tel quel -- passez input_fn pour scénariser les réponses. Il
    n'y a pas de « bonne » valeur par défaut ici, seulement un choix qui ne
    bloque pas."""

    def __init__(self, input_fn=None, echo: bool = False):
        #: Toutes les lignes passées à log(), dans l'ordre, avec leur niveau.
        #: C'est le pendant headless de VMResult.output, ce qui rend les deux
        #: moteurs comparables ligne à ligne (voir les tests d'équivalence).
        self.journal: List[tuple] = []
        self.sortie: List[str] = []
        self._input_fn = input_fn or (lambda var_name: "")
        self._echo = echo

    def log(self, text, level: str = "info"):
        texte = str(text)
        self.journal.append((level, texte))
        # sortie ne garde que ce qu'un utilisateur verrait comme "le
        # résultat du script" : les lignes de niveau info. Les "[SYS]" et
        # les erreurs sont dans journal, mais les mélanger à sortie rendrait
        # toute comparaison avec VMResult.output fausse (la VM n'émet pas
        # ces lignes de service).
        if level == "info":
            self.sortie.append(texte)
        if self._echo:
            prefixe = {"err": "[ERREUR] ", "sys": "[SYS] ", "ok": "[OK] "}.get(level, "")
            print(prefixe + texte)

    def _set_status(self, message: str):
        pass

    def update_vfs_list(self):
        pass

    def request_input(self, var_name):
        return self._input_fn(var_name)

    @property
    def erreurs(self) -> List[str]:
        return [t for niveau, t in self.journal if niveau == "err"]


class IAAgent:
    def __init__(self, engine: UVIEngine, ide, vfs):
        self.engine = engine
        self.ide = ide
        self.vfs = vfs
        self.instruction_counts = {}
        self.session_start = datetime.now()
        self.voice_enabled = HAS_TTS
        self.tts_queue = queue.Queue()
        self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self.tts_thread.start()

    def _tts_worker(self):
        if not HAS_TTS:
            return
        try:
            engine = pyttsx3.init()
            engine.setProperty('rate', 150)
        except Exception:
            return
        while True:
            text = self.tts_queue.get()
            if text is None:
                break
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass
            self.tts_queue.task_done()

    def speak(self, text):
        if self.voice_enabled and HAS_TTS:
            clean_text = text.replace("IA :", "").replace("[WARN]", "").strip()
            self.tts_queue.put(clean_text)

    def speak_and_log(self, msg, level="info"):
        self.ide.log(msg, level)
        if level != "err":
            self.speak(msg)

    def track_instruction(self, inst_type):
        self.instruction_counts[inst_type] = self.instruction_counts.get(inst_type, 0) + 1

    def execute_task(self, instruction):
        node = self.engine.parser.parse_line(instruction, 0)
        if node:
            return self.engine.exec_node(node)
        return ""

    def get_memory(self):
        return self.engine.memoire

    def get_system_state(self):
        return {
            "memoire": self.engine.memoire.copy(),
            "routines": list(self.engine.routines.keys()),
            "fonctions": list(self.engine.fonctions.keys()),
            "vfs_files": self.vfs.list_files(),
            "error_state": self.engine.error_flag,
            "last_errors": [str(e) for e in self.engine.last_errors]
        }

    def think_and_act(self):
        state = self.get_system_state()
        self.speak_and_log("IA : Analyse système en cours...", "sys")
        mem_pct = (self.engine.current_memory_usage / MAX_MEMORY_BYTES) * 100
        self.speak_and_log(f"IA : Charge mémoire à {mem_pct:.2f}%", "info")
        if mem_pct > 80:
            self.speak_and_log("IA : Attention, mémoire critique.", "err")
        if not state["vfs_files"]:
            self.speak_and_log("IA : Aucun fichier VFS trouvé, création de 'init.uvi'...", "sys")
            self.execute_task('&save "init.uvi" -> "Système initialisé par IA"')
        else:
            self.speak_and_log(f"IA : Fichiers VFS présents : {len(state['vfs_files'])}", "info")

    def report_stats(self):
        self.ide.log("=== STATISTIQUES DE SESSION ===", "sys")
        uptime = datetime.now() - self.session_start
        self.ide.log(f"Temps de session : {str(uptime).split('.')[0]}", "info")
        self.ide.log(f"Mémoire allouée : {self.engine.current_memory_usage} octets", "info")
        self.ide.log(f"Fonctions définies : {len(self.engine.fonctions)}", "info")
        self.ide.log(f"Routines actives : {len(self.engine.routines)}", "info")
        self.ide.log("Répartition des instructions :", "sys")
        for k, v in sorted(self.instruction_counts.items(), key=lambda x: x[1], reverse=True):
            self.ide.log(f"  - {k} : {v} appels", "info")

        if self.engine.named_call_counts:
            self.ide.log("Répartition des appels nommés (DEF/routines) :", "sys")
            for k, v in sorted(self.engine.named_call_counts.items(), key=lambda x: x[1], reverse=True):
                self.ide.log(f"  - {k} : {v} appel(s)", "info")

        if self.engine.last_errors:
            error_counts = {}
            for err in self.engine.last_errors:
                error_counts[err.kind] = error_counts.get(err.kind, 0) + 1
            self.ide.log(f"Erreurs rencontrées ({len(self.engine.last_errors)} au total) :", "sys")
            for kind, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True):
                self.ide.log(f"  - {kind} : {count} occurrence(s)", "info")
        else:
            self.ide.log("Erreurs rencontrées : aucune.", "info")

        knowledge_file = self.vfs.get_path("knowledge_base.json")
        if os.path.exists(knowledge_file):
            with open(knowledge_file, "r") as f:
                try:
                    knowledge = json.load(f)
                except Exception:
                    knowledge = {}
            if knowledge:
                self.ide.log(f"Base de connaissances : {len(knowledge)} correction(s) apprise(s).", "sys")
                most_used = max(
                    knowledge.items(),
                    key=lambda item: item[1].get("frequency", 0) if isinstance(item[1], dict) else 0,
                    default=None,
                )
                if most_used and isinstance(most_used[1], dict) and most_used[1].get("frequency", 0) > 0:
                    self.ide.log(
                        f"  - Correction la plus utilisée : '{most_used[0]}' "
                        f"({most_used[1]['frequency']} fois)", "info"
                    )
            else:
                self.ide.log("Base de connaissances : vide.", "info")
        else:
            self.ide.log("Base de connaissances : pas encore créée.", "info")

        self.speak("Les statistiques de la session ont été générées dans la console.")

    def generate_snippet(self, pattern_type=""):
        pattern_type = pattern_type.lower()
        snippets = {
            "boucle": "% 10 -> ! \"Itération\" | _0.1",
            "condition": "~ variable == 1 -> ! \"Vrai\" || ! \"Faux\"",
            "fonction": "DEF ma_fonction(x) -> _retour = x * 2",
            "tableau": "TAB mon_tab\nTAB.PUSH mon_tab -> 1\nTAB.PRINT mon_tab",
            "chaine": "STR.UPPER \"test\" -> majuscule\n! majuscule"
        }
        snip = snippets.get(pattern_type, "# Modèles: boucle, condition, fonction, tableau, chaine\n!SNIPPET boucle")
        self.write_to_editor(snip)

    def analyze_editor_code(self):
        code = self.ide.editor.get("1.0", "end")
        lines = code.splitlines()
        ast_nodes = self.engine.parser.parse_program(lines)
        warnings = []
        for node in ast_nodes:
            if isinstance(node, UnknownNode):
                warnings.append(f"Ligne {node.line}: Syntaxe non reconnue — '{node.raw}'")
            if isinstance(node, IfNode) and not node.then_block:
                warnings.append(f"Ligne {node.line}: Bloc IF vide détecté.")
            if isinstance(node, WhileNode) and not node.body:
                warnings.append(f"Ligne {node.line}: Bloc WHILE sans corps.")
            if isinstance(node, ForNode) and not node.body:
                warnings.append(f"Ligne {node.line}: Bloc FOR sans corps.")
            if isinstance(node, DefNode) and not node.body:
                warnings.append(f"Ligne {node.line}: Fonction '{node.name}' sans corps.")
            if isinstance(node, CallNode):
                if node.name not in self.engine.fonctions:
                    warnings.append(f"Ligne {node.line}: CALL vers '{node.name}' — fonction non encore définie.")
        if warnings:
            self.speak_and_log("IA : Avertissements d'analyse statique détectés :", "info")
            for w in warnings:
                self.ide.log(f"  [WARN] {w}", "err")
        else:
            self.speak_and_log("IA : Analyse statique OK. Aucun avertissement.", "ok")
        return warnings

    def export_ast(self):
        code = self.ide.editor.get("1.0", "end")
        lines = code.splitlines()
        ast_nodes = self.engine.parser.parse_program(lines)
        ast_json = self.engine.parser.program_to_json(ast_nodes)
        self.vfs.write_file("ast_export.json", ast_json)
        self.ide.update_vfs_list()
        self.speak_and_log("IA : AST exporté dans 'ast_export.json'.", "ok")

    def export_bytecode(self):
        code = self.ide.editor.get("1.0", "end")
        lines = code.splitlines()
        ast_nodes = self.engine.parser.parse_program(lines)
        compiler = UVIBytecodeCompiler()
        flat = compiler.flatten_bytecode(ast_nodes)
        bc_json = compiler.bytecode_to_json(flat)
        self.vfs.write_file("bytecode_export.json", bc_json)
        self.ide.update_vfs_list()
        self.speak_and_log(f"IA : Bytecode linéaire exporté ({len(flat)} instructions) dans 'bytecode_export.json'.", "ok")

    def write_to_editor(self, code_snippet):
        self.ide.editor.insert("end", f"\n{code_snippet}")
        self.ide._update_line_numbers()
        self.speak_and_log("IA : Code généré et inséré dans l'éditeur.", "ok")

    def scan_logs_for_errors(self):
        if self.engine.last_errors:
            return str(self.engine.last_errors[-1])
        console_content = self.ide.console.get("1.0", "end")
        lines = console_content.splitlines()
        errors = [line for line in lines if "err" in line.lower() or "erreur" in line.lower()]
        if errors:
            return errors[-1]
        return None

    def auto_repair(self):
        error = self.scan_logs_for_errors()
        if not error:
            self.speak_and_log("IA : Aucune erreur récente à réparer.", "sys")
            return
        knowledge_file = self.vfs.get_path("knowledge_base.json")
        if os.path.exists(knowledge_file):
            with open(knowledge_file, "r") as f:
                try:
                    knowledge = json.load(f)
                except Exception:
                    knowledge = {}
            for err_type, solution in knowledge.items():
                if err_type in error:
                    self.speak_and_log(f"IA : Utilisation d'une correction apprise pour : {err_type}", "ok")
                    return
        self.speak_and_log("IA : Diagnostic en cours...", "sys")

        # Le kind typé (déjà produit par _emit_error partout dans le moteur)
        # est un canal fiable et non ambigu — contrairement au matching de
        # sous-chaîne sur le texte affiché, qui peut se déclencher sur un mot
        # présent par coïncidence dans un message sans rapport. On privilégie
        # donc toujours .kind quand un objet UVIError récent existe, et on ne
        # retombe sur le texte que s'il n'y en a aucun (par exemple si
        # l'erreur vient d'ailleurs que du moteur lui-même).
        typed_kind = ""
        if self.engine.last_errors:
            typed_kind = self.engine.last_errors[-1].kind

        # Diagnostics couvrant l'ensemble des 21 types d'erreur réellement
        # émis par le moteur (voir toutes les occurrences de _emit_error) —
        # la version précédente n'en couvrait que 8, les 13 autres tombaient
        # tous dans le message générique "Erreur non reconnue" malgré un kind
        # parfaitement identifié.
        diagnostics = {
            "MAX_RECURSION": "IA : Récursion infinie détectée. Ajoutez une condition de sortie.",
            "UNKNOWN_FUNCTION": "IA : Fonction appelée introuvable. Vérifiez l'orthographe ou l'import.",
            "INDEX_OUT_OF_BOUNDS": "IA : Index de tableau hors limites. Vérifiez l'indexation.",
            "NOT_A_ARRAY": "IA : La variable n'existe pas ou n'est pas un tableau. Utilisez TAB nom pour l'initialiser.",
            "FILE_NOT_FOUND": "IA : Fichier ou module introuvable. Vérifiez le nom et l'explorateur VFS.",
            "UNKNOWN_SYNTAX": "IA : Typographie probable. Vérifiez la syntaxe ou les espaces.",
            "INFINITE_LOOP": "IA : Boucle infinie détectée. Vérifiez la condition de sortie.",
            "INVALID_INDEX": "IA : Index invalide. L'index d'un tableau doit être un entier positif.",
            "MEMORY_OVERFLOW": "IA : Mémoire saturée. Libérez des variables ou augmentez la limite mémoire.",
            "UNKNOWN_ROUTINE": "IA : Routine inconnue. Vérifiez qu'elle a bien été définie avec '*'.",
            "INVALID_COUNT": "IA : Compteur de boucle invalide. Le compteur doit être un entier.",
            "INVALID_IMPORT": "IA : Nom de fichier ou de module invalide dans l'import.",
            "CIRCULAR_IMPORT": "IA : Import circulaire détecté. Un module ne peut pas s'importer lui-même indirectement.",
            "TOO_MANY_CALLS": "IA : Trop d'appels de fonctions imbriqués. Vérifiez l'absence de boucle d'appels.",
            "WRONG_ARG_COUNT": "IA : Nombre d'arguments incorrect pour cette fonction. Vérifiez sa définition avec DEF.",
            "ARRAY_FULL": "IA : Tableau plein. Augmentez sa capacité ou videz-le avant d'ajouter des éléments.",
            "EMPTY_ARRAY": "IA : Tableau vide. Ajoutez des éléments avant d'y accéder.",
            "INVALID_SLICE": "IA : Découpage (slice) invalide. Vérifiez les bornes de début et de fin.",
            "MISSING_FILENAME": "IA : Nom de fichier manquant pour cette opération.",
            "CRASH": "IA : Erreur interne critique. Ouvrez la console pour la trace complète.",
            "RUNTIME_ERROR": "IA : Erreur d'exécution imprévue. Vérifiez la logique du code autour de la ligne signalée.",
        }

        if typed_kind in diagnostics:
            self.speak_and_log(diagnostics[typed_kind], "info")
        elif "not enough values to unpack" in error:
            self.speak_and_log("IA : Détection d'un séparateur manquant. Je corrige la syntaxe.", "info")
            code = self.ide.editor.get("1.0", "end")
            new_lines = []
            for line in code.splitlines():
                clean = line.strip()
                if clean.startswith(('~', '%', '%%')) and '->' not in clean:
                    new_lines.append(line + ' -> ! "Bloc vide ajouté par IA"')
                elif clean.startswith('*') and '=' not in clean:
                    new_lines.append(line + ' = ! "Routine vide ajoutée par IA"')
                else:
                    new_lines.append(line)
            self.ide.editor.delete("1.0", "end")
            self.ide.editor.insert("end", "\n".join(new_lines))
            self.ide._update_line_numbers()
            self.speak_and_log("IA : Code corrigé automatiquement.", "ok")
        else:
            # Repli sur le matching de texte historique uniquement si aucun
            # kind typé ne correspond — conservé pour ne pas régresser sur
            # des messages d'erreur qui ne viendraient pas de _emit_error.
            text_fallbacks = [
                ("récursion", diagnostics["MAX_RECURSION"]),
                ("Fonction inconnue", diagnostics["UNKNOWN_FUNCTION"]),
                ("hors limites", diagnostics["INDEX_OUT_OF_BOUNDS"]),
                ("pas un tableau", diagnostics["NOT_A_ARRAY"]),
                ("introuvable", diagnostics["FILE_NOT_FOUND"]),
                ("No such file", diagnostics["FILE_NOT_FOUND"]),
                ("Syntaxe inconnue", diagnostics["UNKNOWN_SYNTAX"]),
                ("Boucle infinie", diagnostics["INFINITE_LOOP"]),
            ]
            matched = False
            for needle, message in text_fallbacks:
                if needle in error:
                    self.speak_and_log(message, "info")
                    matched = True
                    break
            if not matched:
                self.speak_and_log("IA : Erreur non reconnue.", "err")

    def _save_to_knowledge_base(self, error_type, original, corrected):
        knowledge_file = self.vfs.get_path("knowledge_base.json")
        data = {}
        if os.path.exists(knowledge_file):
            with open(knowledge_file, "r") as f:
                try:
                    data = json.load(f)
                except Exception:
                    pass
        freq = 1
        if error_type in data and isinstance(data[error_type], dict):
            freq = data[error_type].get("frequency", 0) + 1
        data[error_type] = {
            "original": original,
            "corrected": corrected,
            "timestamp": datetime.now().isoformat(),
            "frequency": freq
        }
        # Écriture atomique : on écrit d'abord dans un fichier temporaire situé
        # dans le MÊME répertoire (nécessaire pour que os.replace reste atomique
        # sur la plupart des systèmes de fichiers, un rename entre systèmes de
        # fichiers différents ne l'étant pas), puis on remplace l'ancien fichier
        # seulement si l'écriture complète a réussi. Si json.dump échoue en
        # cours de route (disque plein, permissions), le fichier temporaire
        # incomplet est simplement supprimé et knowledge_base.json d'origine
        # reste intact — avant ce correctif, une écriture interrompue pouvait
        # tronquer le fichier existant, et la prochaine lecture effaçait alors
        # silencieusement tout l'historique appris (except Exception: data = {}).
        knowledge_dir = os.path.dirname(knowledge_file) or "."
        tmp_fd, tmp_path = tempfile.mkstemp(dir=knowledge_dir, prefix=".knowledge_base_", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_path, knowledge_file)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            self.ide.log(f"IA : Échec de sauvegarde de la base de connaissances : {e}", "err")

    def learn_correction(self, error_type, original, corrected):
        self._save_to_knowledge_base(error_type, original, corrected)
        self.speak_and_log("IA : Apprentissage structuré enregistré.", "sys")

    def learn_from_terminal(self, error_type, corrected_code):
        self._save_to_knowledge_base(error_type, error_type, corrected_code)
        self.speak_and_log("IA : Motif appris.", "ok")

    def get_auto_suggestion(self, current_word):
        current_word = current_word.strip().upper()
        if not current_word:
            return None
        uvi_keywords = [
            "TAB.SET", "TAB.GET", "TAB.PUSH", "TAB.PRINT", "TAB.POP", "TAB.CREATE",
            "TAB.LEN", "TAB.DEL", "IMPORT", "CALL", "DEF", "STR.LEN",
            "STR.UPPER", "STR.LOWER", "STR.TRIM", "STR.SPLIT", "STR.JOIN",
            "STR.REPLACE", "STR.CONTAINS", "STR.STARTS", "STR.ENDS",
            "STR.SLICE", "STR.INDEX"
        ]
        for var_name in self.engine.memoire.keys():
            str_var = str(var_name).upper()
            if str_var not in uvi_keywords:
                uvi_keywords.append(str_var)
        for func_name in self.engine.fonctions.keys():
            if func_name.upper() not in uvi_keywords:
                uvi_keywords.append(func_name.upper())
        for rout_name in self.engine.routines.keys():
            if rout_name.upper() not in uvi_keywords:
                uvi_keywords.append(rout_name.upper())
        matches = [kw for kw in uvi_keywords if kw.startswith(current_word)]
        if matches and matches[0] != current_word:
            return matches[0]
        return None

    def optimize_code(self):
        knowledge_file = self.vfs.get_path("knowledge_base.json")
        if not os.path.exists(knowledge_file):
            self.speak_and_log("IA : Aucune base trouvée pour l'optimisation.", "sys")
            return
        with open(knowledge_file, "r") as f:
            try:
                knowledge = json.load(f)
            except Exception:
                knowledge = {}
        if not knowledge:
            self.speak_and_log("IA : Base vide.", "sys")
            return
        code = self.ide.editor.get("1.0", "end")
        new_code = code
        optimizations_applied = 0
        for key, value in knowledge.items():
            target = None
            replacement = None
            if isinstance(value, dict) and "original" in value and "corrected" in value:
                target = value["original"]
                replacement = value["corrected"]
            elif isinstance(value, str):
                target = key
                replacement = value
            else:
                continue
            if target and replacement and target in new_code:
                new_code = new_code.replace(target, replacement)
                optimizations_applied += 1
        if optimizations_applied > 0:
            self.ide.editor.delete("1.0", "end")
            self.ide.editor.insert("end", new_code.rstrip('\n'))
            self.ide._update_line_numbers()
            self.speak_and_log(f"IA : {optimizations_applied} optimisation(s) effectuée.", "ok")
        else:
            self.speak_and_log("IA : Le code est déjà optimisé.", "info")


if HAS_VM:
    # Ajoute IAAgent.autonomous_fix (boucle observer/générer/exécuter/
    # vérifier appuyée sur AgentLoop + IssueDiagnosisLoop, cf.
    # uvi_agent_extension.py). Greffe plutôt que copie en place, pour que
    # cette capacité reste modifiable sans toucher à ce fichier principal.
    attach_autonomous_capabilities(IAAgent)


# Contenu de la bibliothèque standard (système de paquets, ajout de
# cette session) -- chaque entrée est du VRAI code UVI, chaque ligne
# VÉRIFIÉE EMPIRIQUEMENT par exécution réelle avant d'être figée ici
# (parsée ET exécutée avec les résultats numériques attendus contrôlés
# un par un, dans les deux moteurs -- même méthode que pour le reste de
# ce fichier, jamais de code écrit "de mémoire"). Installée au disque
# via UVIPackageRegistry.ensure_stdlib_installed (voir UVIIDE.__init__),
# résolue par MODULE "std:<nom>" -> alias.
STDLIB_SOURCES: Dict[str, str] = {
    "math": (
        "PI = 3.14159265\n"
        "DEF CARRE(n) -> _retour = n * n\n"
        "DEF CUBE(n) -> _retour = n * n * n\n"
        "DEF ABS(n) -> _retour = n * (n >= 0) + (0 - n) * (n < 0)\n"
        "DEF MAX(a, b) -> _retour = a * (a >= b) + b * (b > a)\n"
        "DEF MIN(a, b) -> _retour = a * (a <= b) + b * (b < a)\n"
        "EXPORT PI\n"
        "EXPORT CARRE\n"
        "EXPORT CUBE\n"
        "EXPORT ABS\n"
        "EXPORT MAX\n"
        "EXPORT MIN\n"
    ),
}

