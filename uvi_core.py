import ast
import ast as pyast
import operator
import time
import json
import os
import sys
import re
import threading
import queue
import tempfile
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Dict, Callable, Union


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
            prompt, var_dest = str(args[0]), str(args[1])
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

    nom_affichage = "SHION (DeepSeek-R1-Distill-Qwen-7B)"

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
    def _traduire_http(e: "urllib.error.HTTPError", model_name: str,
                        max_tokens: int = DEEPSEEK_MAX_TOKENS) -> str:
        """Transforme une HTTPError vLLM en phrase actionnable en français.

        Lit les DEUX formes de corps d'erreur (plate historique, imbriquée
        récente) -- voir la docstring de la classe. `max_tokens` : celui de
        l'INSTANCE appelante (voir __init__), pas la constante de module --
        sinon la piste affichée sur une erreur 400 mentirait dès que le
        panneau PARAMÈTRES a changé le budget de tokens du client actif."""
        message_serveur = str(e)
        try:
            corps = json.loads(e.read().decode("utf-8"))
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
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, ValueError):
            pass

        piste = ""
        if e.code == 404:
            # L'erreur n°1 au premier branchement, et la plus déroutante :
            # le serveur RÉPOND (donc le réseau va bien), mais refuse le nom
            # de modèle. On donne directement la commande de diagnostic.
            piste = (
                " -- le nom de modèle envoyé ('%s') ne correspond pas à ce que "
                "sert cette instance vLLM. Vérifiez la valeur exacte avec : "
                "curl %s/v1/models  puis définissez DEEPSEEK_MODEL_NAME en "
                "conséquence." % (model_name, SHION_SERVER_URL.rstrip("/"))
            )
        elif e.code == 401:
            piste = (
                " -- le serveur vLLM a été lancé avec --api-key et attend un "
                "jeton. Définissez DEEPSEEK_API_KEY avec la même valeur."
            )
        elif e.code == 400:
            piste = (
                " -- requête refusée (souvent : contexte dépassé). Réduisez la "
                "longueur du prompt, ou baissez le budget de tokens (actuel : "
                "%d)." % max_tokens
            )
        elif e.code >= 500:
            piste = " -- erreur interne du serveur vLLM, consultez ses logs."
        return "SHION/DeepSeek a échoué (HTTP %d) : %s%s" % (e.code, message_serveur, piste)

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
            raise DeepSeekClientError(self._traduire_http(e, self.model_name, self.max_tokens))
        except urllib.error.URLError as e:
            # BUGFIX : urlopen(..., timeout=...) enveloppe TOUJOURS un
            # dépassement de délai dans une URLError, jamais en TimeoutError
            # nue -- le `except TimeoutError` qui suivait n'était donc
            # jamais atteint, et l'utilisateur ne voyait jamais la piste
            # "augmentez DEEPSEEK_REQUEST_TIMEOUT_S", pourtant la plus utile
            # ici vu la lenteur normale d'un modèle de raisonnement.
            if isinstance(e.reason, TimeoutError):
                raise DeepSeekClientError(
                    "SHION/DeepSeek n'a pas répondu en %ds. Un modèle de "
                    "raisonnement est lent : augmentez DEEPSEEK_REQUEST_TIMEOUT_S "
                    "avant de conclure à une panne." % self.timeout_s
                )
            raise DeepSeekClientError(
                "SHION/DeepSeek injoignable à %s (%s). Le serveur vLLM est-il "
                "démarré, et SHION_SERVER_URL pointe-t-elle dessus ?"
                % (self.base_url, e.reason)
            )
        except json.JSONDecodeError:
            raise DeepSeekClientError(
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
    return DeepSeekClient(base_url=SHION_SERVER_URL, model_name=DEEPSEEK_MODEL_NAME)


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
        arrow_split = self._split_tokens_by_type(rest, 'ARROW')
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
        # instruction », pas « immédiat ».
        self.stop_requested = False
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

    def set_agent(self, agent):
        self.agent = agent

    def llm_client_prioritaire(self):
        """Rend le client de langage à utiliser pour !ERIS(...) (syntaxe
        d'appel synchrone conservée dans le langage, voir _eval_expr_or_eris)
        et pour l'onglet de discussion SHION : toujours self.shion_client
        désormais.

        HISTORIQUE : ERIS (assistant Gemini séparé, clé API personnelle)
        a été retiré -- SHION couvre déjà son rôle sans les inconvénients
        qui justifiaient son existence (ERIS_API_KEY était une variable
        d'environnement globale au process, donc une clé, un quota et un
        historique partagés entre tous les utilisateurs d'une même
        instance UVI ; SHION n'a structurellement pas ce problème). Cette
        méthode garde son nom et sa signature : c'est le point d'entrée
        UNIQUE déjà utilisé par _eval_expr_or_eris ET l'onglet SHION,
        inutile de le dupliquer à deux endroits pour un simple
        raccourcissement."""
        return self.shion_client

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
        if self.error_flag or node is None:
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
        chemin_vfs = self.vfs.get_path(nom_fic)
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
        chemin_vfs = self.vfs.get_path(nom_fic)
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
            self._emit_error("RUNTIME_ERROR", f"Erreur dans la fonction '{alias}::{nom_func}' : {e}", node.line)
            retour_val = ""
        finally:
            self.memoire = saved_memoire
            self.current_memory_usage = saved_memory_usage
            self.call_depth -= 1
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

        if not HAS_SHION or self.shion_client is None:
            self._emit_error(
                "SHION_UNAVAILABLE",
                "Aucun backend SHION.ASK disponible ici (paquet shion/ absent, "
                "ou SHION_BACKEND=deepseek sans serveur vLLM joignable). "
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
            target=self._shion_worker, args=(prompt, var_dest, node.line), daemon=True,
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

    def _shion_worker(self, prompt: str, var_dest: str, line: int):
        """
        Tourne sur un thread séparé. NE JAMAIS toucher un widget Tkinter
        directement ici (Tkinter n'est pas thread-safe) — tout ce qui touche
        self.ide (log, statusbar, etc.) ou déclenche un log via
        _safe_store/_emit_error doit passer par self.ide.root.after(0, ...),
        comme le fait déjà run_script() ailleurs dans ce fichier pour son
        propre callback de fin.

        self.shion_is_busy est remis à False dans tous les cas (succès ou
        échec) avant de retourner, pour ne jamais laisser SHION.ASK bloqué
        indéfiniment après une erreur réseau.
        """
        try:
            # Note : timeout=60s est câblé en dur dans ShionServingClient.complete()
            # (serving.py), pas paramétrable depuis ici sans modifier serving.py —
            # hors scope de cette intégration. SHION_REQUEST_TIMEOUT_S (config
            # ci-dessus) documente cette valeur mais ne la contrôle pas.
            result = self.shion_client.complete(prompt)
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
                self.ide.log("⏹ Exécution interrompue par l'utilisateur.", "sys")
                break
            self.current_run_line = node.line if node.line > 0 else 0
            try:
                self.exec_node(node)
            except Exception as e:
                self._emit_error("CRASH", f"Crash ligne {node.line} : {e}", node.line)

        if not is_sub_sequence:
            self.is_running = False
            self.current_run_line = 0

    def run_single_command(self, cmd):
        self.error_flag = False
        try:
            node = self.parser.parse_line(cmd, 0)
            if node:
                self.exec_node(node)
        except Exception as e:
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
        code = self.ide.editor.get("1.0", tk.END)
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
        code = self.ide.editor.get("1.0", tk.END)
        lines = code.splitlines()
        ast_nodes = self.engine.parser.parse_program(lines)
        ast_json = self.engine.parser.program_to_json(ast_nodes)
        self.vfs.write_file("ast_export.json", ast_json)
        self.ide.update_vfs_list()
        self.speak_and_log("IA : AST exporté dans 'ast_export.json'.", "ok")

    def export_bytecode(self):
        code = self.ide.editor.get("1.0", tk.END)
        lines = code.splitlines()
        ast_nodes = self.engine.parser.parse_program(lines)
        compiler = UVIBytecodeCompiler()
        flat = compiler.flatten_bytecode(ast_nodes)
        bc_json = compiler.bytecode_to_json(flat)
        self.vfs.write_file("bytecode_export.json", bc_json)
        self.ide.update_vfs_list()
        self.speak_and_log(f"IA : Bytecode linéaire exporté ({len(flat)} instructions) dans 'bytecode_export.json'.", "ok")

    def write_to_editor(self, code_snippet):
        self.ide.editor.insert(tk.END, f"\n{code_snippet}")
        self.ide._update_line_numbers()
        self.speak_and_log("IA : Code généré et inséré dans l'éditeur.", "ok")

    def scan_logs_for_errors(self):
        if self.engine.last_errors:
            return str(self.engine.last_errors[-1])
        console_content = self.ide.console.get("1.0", tk.END)
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
            code = self.ide.editor.get("1.0", tk.END)
            new_lines = []
            for line in code.splitlines():
                clean = line.strip()
                if clean.startswith(('~', '%', '%%')) and '->' not in clean:
                    new_lines.append(line + ' -> ! "Bloc vide ajouté par IA"')
                elif clean.startswith('*') and '=' not in clean:
                    new_lines.append(line + ' = ! "Routine vide ajoutée par IA"')
                else:
                    new_lines.append(line)
            self.ide.editor.delete("1.0", tk.END)
            self.ide.editor.insert(tk.END, "\n".join(new_lines))
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
        code = self.ide.editor.get("1.0", tk.END)
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
            self.ide.editor.delete("1.0", tk.END)
            self.ide.editor.insert(tk.END, new_code.rstrip('\n'))
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


def _melanger_couleurs(hex1: str, hex2: str, t: float) -> str:
    """Interpole linéairement entre deux couleurs #RRGGBB. `t=0` -> hex1,
    `t=1` -> hex2. Utilisé par l'animation de pouls du voyant d'état
    (_animer_voyant) pour une transition continue plutôt qu'un clignotement
    binaire -- pas de dépendance graphique supplémentaire, juste de
    l'arithmétique sur trois octets."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(hex1[1:3], 16), int(hex1[3:5], 16), int(hex1[5:7], 16)
    r2, g2, b2 = int(hex2[1:3], 16), int(hex2[3:5], 16), int(hex2[5:7], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return "#%02X%02X%02X" % (r, g, b)


class T:
    """Palette de l'interface UVI v9.

    POURQUOI UNE CLASSE ET PAS DES LITTÉRAUX DISPERSÉS : la version
    précédente écrivait "#0D0D0D" et "#00FF00" en dur à plus de deux cents
    endroits. Changer de thème demandait un rechercher-remplacer sur des
    codes hexadécimaux, avec le risque évident d'en manquer un ou d'en
    remplacer un qui n'était pas un fond de panneau. Une table nommée rend
    le prochain changement de thème mécanique.

    CHANGEMENT D'IDENTITÉ ASSUMÉ. Le projet était en vert-sur-noir
    (esthétique terminal). La v9 passe en bleu profond / cyan. C'est un
    choix délibéré demandé par la maquette, pas une dérive : le vert
    terminal disait « console », le bleu dit « atelier ». Les deux restent
    des thèmes sombres de développeur ; ce qui change, c'est le registre.
    Le vert est CONSERVÉ mais requalifié : il ne signifie plus « UVI », il
    signifie « succès » (sortie OK, statut prêt), ce qui lui rend une
    valeur informative qu'il n'avait plus quand tout était vert."""

    # Fonds, du plus profond au plus clair
    FOND = "#060B18"          # fond de fenêtre
    PANNEAU = "#0B1424"       # cartes et panneaux
    PANNEAU_HAUT = "#0E1A2E"  # en-têtes de panneau
    CHAMP = "#0A1220"         # zones de saisie et de texte
    SURVOL = "#152640"

    # Traits
    BORDURE = "#1B2C48"
    BORDURE_VIVE = "#264066"

    # Textes
    TEXTE = "#C3D6F0"
    TEXTE_FORT = "#EAF2FF"
    TEXTE_FAIBLE = "#5D7599"

    # Accents
    CYAN = "#22D3EE"
    BLEU = "#2E7CF6"
    BLEU_VIF = "#3B82F6"
    VIOLET = "#A855F7"
    ROSE = "#EC4899"
    VERT = "#22C55E"          # succès uniquement
    AMBRE = "#F59E0B"
    ROUGE = "#EF4444"

    # Coloration syntaxique de l'éditeur
    SYN_MOTCLE = "#C084FC"
    SYN_CHAINE = "#FBBF24"
    SYN_NOMBRE = "#22D3EE"
    SYN_COMMENT = "#4B6280"
    SYN_FONCTION = "#38BDF8"
    SYN_OPERATEUR = "#F472B6"

    # Polices
    MONO = ("Consolas", 11)
    MONO_EDIT = ("Consolas", 12)
    MONO_PETIT = ("Consolas", 9)
    UI = ("Segoe UI", 9)
    UI_GRAS = ("Segoe UI", 9, "bold")
    UI_TITRE = ("Segoe UI", 10, "bold")


class _Tooltip:
    """Infobulle Tkinter minimale, écrite ici plutôt qu'importée.

    POURQUOI PAS UNE DÉPENDANCE : Tkinter n'a pas d'infobulle native, et les
    bibliothèques qui en fournissent une (tkinter-tooltip, Pmw...) sont des
    paquets supplémentaires à installer. Le projet tourne sur un Raspberry
    Pi où l'on a déjà écarté torch pour cette raison exacte -- ajouter une
    dépendance pip pour AFFICHER UNE PHRASE serait incohérent. Une Toplevel
    sans décoration, c'est 25 lignes et zéro installation.

    Double affichage volontaire (bulle + barre de statut) : la bulle est
    plus lisible, la barre de statut reste visible même si le survol est
    bref ou si le gestionnaire de fenêtres avale les Toplevel sans
    décoration (comportement variable selon les environnements de bureau
    Linux -- constaté, d'où la ceinture ET les bretelles)."""

    def __init__(self, widget, texte, statusbar_fn=None, delai_ms=450):
        self.widget = widget
        self.texte = texte
        self.statusbar_fn = statusbar_fn
        self.delai_ms = delai_ms
        self._fenetre = None
        self._apres_id = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        # <ButtonPress> aussi : sans ça, la bulle reste affichée par-dessus
        # la vue qui vient de changer quand on clique le bouton.
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None):
        if self.statusbar_fn:
            self.statusbar_fn(self.texte)
        self._apres_id = self.widget.after(self.delai_ms, self._afficher)

    def _on_leave(self, _event=None):
        if self._apres_id is not None:
            self.widget.after_cancel(self._apres_id)
            self._apres_id = None
        if self._fenetre is not None:
            self._fenetre.destroy()
            self._fenetre = None
        if self.statusbar_fn:
            self.statusbar_fn(" Prêt.")

    def _afficher(self):
        if self._fenetre is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._fenetre = tk.Toplevel(self.widget)
        self._fenetre.wm_overrideredirect(True)  # pas de barre de titre
        self._fenetre.wm_geometry("+%d+%d" % (x, y))
        tk.Label(
            self._fenetre, text=self.texte, justify=tk.LEFT,
            bg="#1A1A1A", fg="#00FF00", relief=tk.SOLID, borderwidth=1,
            font=("Consolas", 9), wraplength=380, padx=8, pady=5,
        ).pack()


# Textes des infobulles. Séparés de _build_topbar volontairement : la
# construction des boutons est déjà dense, et une table nommée se relit et
# se corrige sans naviguer dans du code Tkinter.
#
# Chaque texte répond à DEUX questions, pas une seule : ce que le bouton
# fait, ET pour qui il est utile. La distinction demandée est réelle --
# certains de ces boutons servent à COMPRENDRE le langage (intérêt
# pédagogique, on regarde la machinerie), d'autres à ÉCRIRE un script
# (intérêt quotidien). Un nouvel arrivant qui lit "AST" n'a aucun moyen de
# deviner dans quelle catégorie il tombe.
TOOLBAR_TOOLTIPS: Dict[str, str] = {
    "run": (
        "▶ EXÉCUTER — moteur complet (tree-walker).\n"
        "Gère TOUT le langage : IMPORT, MODULE, pipes |, DRAW, KEY, SHION.\n"
        "USAGE QUOTIDIEN : c'est celui-ci par défaut, en cas de doute."
    ),
    "run_vm": (
        "⚡ EXÉCUTER (VM) — machine virtuelle bytecode, plus rapide.\n"
        "Ne couvre PAS encore IMPORT ni les pipes | (voir run_script_vm).\n"
        "UTILE pour : les boucles de calcul longues, et vérifier que les deux\n"
        "moteurs donnent bien le même résultat."
    ),
    "ast": (
        "🌳 AST — exporte l'arbre syntaxique du script (ast_export.json).\n"
        "Montre comment le parser a COMPRIS votre code, avant exécution.\n"
        "UTILE pour : apprendre le fonctionnement interne du langage, ou\n"
        "comprendre pourquoi une ligne n'est pas interprétée comme prévu.\n"
        "Pas nécessaire pour écrire un script au quotidien."
    ),
    "bytecode": (
        "⚙ BYTECODE — exporte les instructions compilées (bytecode_export.json).\n"
        "L'étage d'après l'AST : ce que la VM exécute réellement.\n"
        "UTILE pour : apprendre comment un langage se compile.\n"
        "Pas nécessaire pour écrire un script au quotidien."
    ),
    "optimiser": (
        "✨ OPTIMISER — réécrit le bytecode (constantes pré-calculées,\n"
        "code mort retiré) sans changer le comportement du script.\n"
        "UTILE pour : voir ce qu'un optimiseur fait concrètement. Le gain\n"
        "réel est marginal sur un script court."
    ),
    "analyser": (
        "🔍 ANALYSER — relit votre script et signale les défauts probables\n"
        "(variables jamais utilisées, boucles suspectes) SANS l'exécuter.\n"
        "USAGE QUOTIDIEN : à lancer avant d'exécuter un script un peu long."
    ),
    "auto_repair": (
        "🛠 AUTO-RÉPARER — corrections déterministes de la dernière erreur,\n"
        "à partir de règles connues. Rapide, hors ligne, sans IA."
    ),
    "auto_fix": (
        "🤖 AUTO-CORRIGER (agent) — boucle IA autonome : elle modifie le\n"
        "script, l'exécute dans la VM, lit l'erreur, recommence.\n"
        "Demande SHION. Lent (appel réseau ou inférence locale).\n"
        "EXPÉRIMENTAL : pas encore validé sur des cas réels variés."
    ),
    "stats": (
        "📊 STATS — compteurs de la session (mémoire, appels par fonction,\n"
        "erreurs rencontrées)."
    ),
}


class UVIIDE:
    def __init__(self):
        self.vfs = VFSManager()
        # Système de paquets (ajout de cette session) : attaché à self.vfs
        # APRÈS construction plutôt qu'ajouté au constructeur de
        # VFSManager -- UVIPackageRegistry a besoin de storage_dir, que
        # VFSManager connaît déjà, donc un attribut posé ici évite de
        # changer la signature de VFSManager (une seule instanciation
        # dans tout le fichier, mais autant ne pas y toucher) et
        # n'importe quel _resolve_module_path (VM/tree-walker) peut
        # accéder à self.vfs.package_registry sans dépendance
        # supplémentaire à propager.
        self.vfs.package_registry = UVIPackageRegistry(self.vfs.storage_dir)
        # Installe la bibliothèque standard (idempotent -- n'écrase
        # jamais un fichier .stdlib déjà présent, voir docstring de
        # ensure_stdlib_installed) : pas de coût significatif à le
        # faire à chaque session, un simple os.path.exists par fichier.
        self.vfs.package_registry.ensure_stdlib_installed(STDLIB_SOURCES)
        self.engine = UVIEngine(self, self.vfs)
        self.agent = IAAgent(self.engine, self, self.vfs)
        self.engine.set_agent(self.agent)
        self.cmd_history = []
        self.history_index = 0
        self.is_waiting_input = False
        self.current_suggestion = None
        self._lock_highlight = False
        # Conseils déjà montrés dans cette session (voir _conseil_unique) --
        # sert aux messages "première fois" de l'étape 7.5.
        self._conseils_vus: set = set()
        # Fichier ouvert et état de modification. Sans ce suivi, rien
        # n'indiquait qu'un travail n'était pas enregistré -- on fermait la
        # fenêtre en croyant avoir sauvegardé.
        self.fichier_courant = None
        self.modifie = False
        # File des travaux d'interface demandés par les threads secondaires.
        # Voir _poster_ui / _pomper_file_ui : c'est le SEUL canal autorisé
        # entre un thread d'exécution et Tkinter.
        self._file_ui = queue.Queue()
        # ÉTAPE 7.1 : le premier lancement doit être détecté AVANT
        # think_and_act(), qui crée init.uvi et rend donc le VFS non vide.
        # Le tester après aurait été un faux négatif permanent -- personne
        # n'aurait jamais vu le message d'accueil.
        premier_lancement = not self.vfs.list_files()
        self._build_window()
        self.input_var = tk.StringVar()
        self._build_ui()
        self.show_editor()
        self.update_vfs_list()
        self.agent.think_and_act()
        if premier_lancement:
            self._afficher_accueil()

    def _afficher_accueil(self):
        """Message d'accueil du tout premier lancement (étape 7.1).

        Délibérément un MESSAGE DANS LE TERMINAL et pas un assistant modal :
        l'IDE affiche déjà tout le reste comme ça (log de l'agent, erreurs,
        sortie des scripts), et une fenêtre modale au premier démarrage
        serait le seul élément d'interface du projet à ne pas ressembler au
        projet. L'objectif est de rendre le démarrage informatif, pas de
        changer la personnalité de l'outil.

        Trois choses seulement, parce que c'est ce qu'un nouvel arrivant ne
        peut PAS deviner en regardant l'écran : ce qu'est UVI, pourquoi il y
        a deux boutons EXÉCUTER, et où trouver un exemple qui marche."""
        self.show_terminal()
        self.log("", "info")
        self.log("╔══════════════════════════════════════════════════════════════╗", "sys")
        self.log("║  BIENVENUE DANS UVI                                          ║", "sys")
        self.log("╚══════════════════════════════════════════════════════════════╝", "sys")
        self.log(
            "UVI est un langage de script complet — lexer, parser, deux moteurs "
            "d'exécution, modules, débogueur — et cet IDE est son atelier. Tout "
            "script commence par la ligne @UVI.", "info",
        )
        self.log(
            "DEUX BOUTONS D'EXÉCUTION, et ce n'est pas un doublon : "
            "▶ EXÉCUTER utilise le moteur complet (tout le langage : IMPORT, "
            "MODULE, pipes, DRAW, KEY). ⚡ EXÉCUTER (VM) compile en bytecode et "
            "va plus vite, mais ne couvre pas encore IMPORT ni les pipes. "
            "En cas de doute : ▶ EXÉCUTER.", "info",
        )
        self.log(
            "POUR COMMENCER : double-cliquez sur 'init.uvi' dans l'explorateur "
            "VFS à gauche, ou tapez  demo  ici même pour charger un exemple "
            "commenté dans l'éditeur.", "info",
        )
        self.log(
            "Survolez n'importe quel bouton de la barre du haut pour savoir ce "
            "qu'il fait et s'il vous sera utile.", "info",
        )
        self.log("", "info")

    def _conseil_unique(self, cle: str, message: str) -> None:
        """Affiche `message` une seule fois par session (étapes 7.5).

        Un conseil affiché à chaque clic devient du bruit qu'on apprend à
        ignorer — et il serait alors ignoré aussi la fois où il compte. Une
        fois par session : assez pour qu'un nouvel arrivant le lise, assez
        peu pour qu'un habitué ne le subisse pas.

        Pas de persistance sur disque volontairement : l'état vit dans
        self._conseils_vus, donc le conseil réapparaît à la session
        suivante. C'est un compromis assumé — écrire un fichier de
        préférences pour ça ajouterait un état à gérer, à migrer et à
        déboguer pour un gain minime."""
        if cle in self._conseils_vus:
            return
        self._conseils_vus.add(cle)
        self.log(message, "sys")

    def _build_window(self):
        self.root = tk.Tk()
        self.root.title("UVI OS v9.0.0 — UNIVERSE · VISION · INTELLIGENCE")
        # 1500x1000 et non 1100x700 : la v9 affiche l'éditeur, la console,
        # la mémoire et SHION SIMULTANÉMENT là où la v8 basculait entre des
        # vues plein écran. En dessous de ~1400 de large, le panneau SHION
        # écrase l'éditeur. minsize l'empêche plutôt que de laisser
        # l'utilisateur découvrir une fenêtre inutilisable.
        #
        # BUGFIX (dock console/saisie ?var invisible) : geometry() fixe
        # demandait 1500x1000 SANS jamais regarder la taille réelle de
        # l'écran. Sur un écran de 1366x768 (résolution de portable très
        # courante -- zone de travail réellement disponible ~1366x720 une
        # fois la barre des tâches déduite), la fenêtre demandée dépasse la
        # zone dispo ; Windows la contraint alors à la zone visible SANS
        # que Tkinter ne relance sa mise en page interne en conséquence.
        # Résultat mesuré : le dock du bas (console + barre de saisie
        # interactive répondant à ?var, voir _build_container et
        # _ouvrir_saisie) se retrouve écrasé hors de la fenêtre -- invisible
        # ET hors d'atteinte du clavier, donc un script qui attend une
        # réponse à ?var semble figé sans qu'on sache pourquoi (les
        # frappes tombent alors dans l'éditeur, resté seul visible). Se
        # caler sur winfo_screenwidth/height, avec une marge, corrige ça
        # pour toute taille d'écran -- pas seulement celle observée ici.
        largeur_ecran = self.root.winfo_screenwidth()
        hauteur_ecran = self.root.winfo_screenheight()
        largeur = min(1500, largeur_ecran - 40)
        hauteur = min(1000, hauteur_ecran - 80)
        self.root.geometry("%dx%d+%d+%d" % (
            largeur, hauteur,
            max(0, (largeur_ecran - largeur) // 2),
            max(0, (hauteur_ecran - hauteur) // 2 - 20),
        ))
        # Bornée par la géométrie réellement obtenue ci-dessus : un minsize
        # plus grand que la fenêtre elle-même ne "empêche" rien, il
        # garantit juste la même casse hors écran à chaque redémarrage.
        self.root.minsize(min(1180, largeur), min(760, hauteur))
        self.root.configure(bg=T.FOND)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- État clavier pour KEY.* (voir _exec_key / _compile_key_node
        # / builtins KEY_* de la VM) -------------------------------------
        # self._keys_down : touches ACTUELLEMENT enfoncées (mises à jour
        #   en continu par les bindings ci-dessous, lues par KEY.DOWN).
        # self._keys_pressed_edge : touches passées de relâchées à
        #   enfoncées DEPUIS LA DERNIÈRE LECTURE (consommé par KEY.PRESSED
        #   -- front montant, pour une action qui ne doit se déclencher
        #   qu'une fois par appui, pas répétée à chaque frame tant que la
        #   touche reste enfoncée).
        # threading.Lock : ces sets sont écrits par le thread Tkinter
        # (callbacks <KeyPress>/<KeyRelease> ci-dessous) et lus par le
        # thread du script UVI en cours d'exécution (cf. run_script,
        # threading.Thread(target=_run) déjà existant) -- accès concurrent
        # classique, protégé comme _lock dans IssueDiagnosisLoop
        # (self_correction.py) protège son propre état partagé.
        self._keys_lock = threading.Lock()
        self._keys_down = set()
        self._keys_pressed_edge = set()

        # bind_all (pas bind) : capte les touches quel que soit le widget
        # qui a le focus Tkinter au moment de l'appui -- un jeu doit
        # réagir aux flèches sans que l'utilisateur ait à cliquer sur le
        # canvas au préalable pour lui donner explicitement le focus.
        # Contrepartie assumée : taper du texte dans l'éditeur pendant
        # qu'un script KEY.*-dépendant tourne en fond alimente aussi son
        # état clavier -- acceptable, on ne code généralement pas pendant
        # qu'on joue.
        self.root.bind_all("<KeyPress>", self._on_game_key_press)
        self.root.bind_all("<KeyRelease>", self._on_game_key_release)

    def _build_ui(self):
        self._build_topbar()
        self._build_statusbar()
        self._build_container()

    def _on_game_key_press(self, event):
        """Callback Tkinter <KeyPress>, appelé sur le thread PRINCIPAL
        (celui de la boucle d'événements) -- jamais depuis le thread du
        script UVI. event.keysym donne un nom déjà lisible ("Up", "Down",
        "space", "Return", "a"...), normalisé en minuscules pour que
        KEY.DOWN "up" et KEY.DOWN "Up" soient équivalents côté script
        (le langage n'a par ailleurs aucune sensibilité à la casse
        documentée sur ses propres symboles ~ / %% / etc.). N'ajoute au
        front montant (_keys_pressed_edge) QUE si la touche n'était pas
        déjà enfoncée -- Tkinter répète nativement <KeyPress> tant qu'une
        touche reste appuyée (auto-repeat clavier), ce qui produirait
        sinon un flot continu de "fronts montants" plutôt qu'un seul au
        moment réel de l'appui initial."""
        key = event.keysym.lower()
        with self._keys_lock:
            if key not in self._keys_down:
                self._keys_pressed_edge.add(key)
            self._keys_down.add(key)

    def _on_game_key_release(self, event):
        key = event.keysym.lower()
        with self._keys_lock:
            self._keys_down.discard(key)

    def _bouton_plat(self, parent, texte, commande, couleur=None, icone="",
                     gras=True, largeur=None, police=None):
        """Bouton « carte » du thème v9 : fond de panneau, bordure discrète,
        éclaircissement au survol.

        Tkinter n'a pas de bouton plat avec état de survol : activebackground
        ne s'applique qu'au CLIC maintenu, pas au passage de la souris. D'où
        les deux liaisons <Enter>/<Leave> ci-dessous. Sans elles, toute la
        barre d'outils paraît inerte -- rien ne réagit avant le clic, ce qui
        donne l'impression que l'interface a gelé."""
        couleur = couleur or T.TEXTE
        bouton = tk.Button(
            parent, text=("%s  %s" % (icone, texte)).strip(),
            bg=T.PANNEAU, fg=couleur,
            activebackground=T.SURVOL, activeforeground=T.TEXTE_FORT,
            disabledforeground=T.TEXTE_FAIBLE,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.BORDURE,
            font=police or (T.UI_GRAS if gras else T.UI),
            cursor="hand2", command=commande, padx=10, pady=7,
        )
        if largeur:
            bouton.config(width=largeur)

        def entrer(_e, b=bouton):
            if b["state"] != tk.DISABLED:
                b.config(bg=T.SURVOL)

        def sortir(_e, b=bouton, c=T.PANNEAU):
            if b["state"] != tk.DISABLED:
                b.config(bg=c)

        bouton.bind("<Enter>", entrer, add="+")
        bouton.bind("<Leave>", sortir, add="+")
        return bouton

    def _groupe_boutons(self, parent):
        """Conteneur encadré regroupant des boutons liés (cf. maquette :
        NOUVEAU/OUVRIR/SAUVEGARDER forment un bloc, DÉBOGUEUR/AUTO-CORRIGER
        un autre). Le regroupement visuel remplace la séparation par
        padding de la v8, qui ne disait rien de la parenté des boutons."""
        cadre = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(side=tk.LEFT, padx=6, pady=8)
        return cadre

    def _build_topbar(self):
        topbar = tk.Frame(self.root, bg=T.FOND, height=76)
        topbar.pack(fill=tk.X, side=tk.TOP)
        topbar.pack_propagate(False)

        # --- identité ----------------------------------------------------
        bloc_logo = tk.Frame(topbar, bg=T.FOND)
        bloc_logo.pack(side=tk.LEFT, padx=(14, 18))
        pastille = tk.Canvas(bloc_logo, width=52, height=52, bg=T.FOND,
                             highlightthickness=0)
        pastille.pack(side=tk.LEFT, padx=(0, 12))
        pastille.create_rectangle(2, 2, 50, 50, outline=T.CYAN, width=2)
        pastille.create_text(26, 26, text="U", fill=T.CYAN,
                             font=("Segoe UI", 24, "bold"))
        textes = tk.Frame(bloc_logo, bg=T.FOND)
        textes.pack(side=tk.LEFT)
        ligne = tk.Frame(textes, bg=T.FOND)
        ligne.pack(anchor=tk.W)
        tk.Label(ligne, text="UVI", bg=T.FOND, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 19, "bold")).pack(side=tk.LEFT)
        tk.Label(ligne, text=" IDE", bg=T.FOND, fg=T.TEXTE,
                 font=("Segoe UI", 15)).pack(side=tk.LEFT, pady=(4, 0))
        tk.Label(textes, text="UNIVERSE · VISION · INTELLIGENCE",
                 bg=T.FOND, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 7)).pack(anchor=tk.W)

        # --- fichier ------------------------------------------------------
        groupe_fichier = self._groupe_boutons(topbar)
        for icone, libelle, commande in (
            ("＋", "NOUVEAU", self.nouveau_fichier),
            ("📂", "OUVRIR", self.load_from_disk),
            ("💾", "SAUVEGARDER", self.save_to_disk),
        ):
            self._bouton_plat(groupe_fichier, libelle, commande,
                              icone=icone, gras=False).pack(side=tk.LEFT)

        # --- exécution ----------------------------------------------------
        # Le bouton d'exécution est le SEUL en aplat bleu de toute la barre.
        # La v8 avait sept boutons de couleurs vives concurrentes, si bien
        # qu'aucun ne ressortait -- un nouvel arrivant ne voyait pas par où
        # commencer. Un seul accent plein = une seule action évidente.
        groupe_run = tk.Frame(topbar, bg=T.FOND)
        groupe_run.pack(side=tk.LEFT, padx=6)
        self.btn_run = tk.Button(
            groupe_run, text="▶  EXÉCUTER",
            bg=T.BLEU, fg="#FFFFFF",
            activebackground=T.BLEU_VIF, activeforeground="#FFFFFF",
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
            command=self.basculer_execution, padx=20, pady=9,
        )
        self.btn_run.pack(side=tk.LEFT, pady=8)
        self.btn_run.bind("<Enter>", lambda e: self.btn_run.config(
            bg=T.ROUGE if self.engine.is_running else T.BLEU_VIF), add="+")
        self.btn_run.bind("<Leave>", lambda e: self.btn_run.config(
            bg="#B91C1C" if self.engine.is_running else T.BLEU), add="+")

        if HAS_VM:
            self.btn_run_vm = self._bouton_plat(
                groupe_run, "VM", self.run_script_vm, couleur=T.CYAN, icone="⚡")
            self.btn_run_vm.pack(side=tk.LEFT, padx=(6, 0), pady=8)

        # --- outils IA ----------------------------------------------------
        groupe_ia = self._groupe_boutons(topbar)
        self.btn_debug = self._bouton_plat(
            groupe_ia, "DÉBOGUEUR", self.ouvrir_debogueur,
            couleur=T.AMBRE, icone="🐞", gras=False)
        self.btn_debug.pack(side=tk.LEFT)
        self.btn_auto_fix = self._bouton_plat(
            groupe_ia, "AUTO-CORRIGER", self.autonomous_fix_from_editor,
            couleur=T.VIOLET, icone="🪄", gras=False)
        self.btn_auto_fix.pack(side=tk.LEFT)
        if not HAS_SHION:
            self.btn_auto_fix.config(state=tk.DISABLED, cursor="X_cursor")

        # --- badge de version ---------------------------------------------
        badge = tk.Frame(topbar, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE_VIVE)
        badge.pack(side=tk.LEFT, padx=14, pady=8)
        noyau = tk.Canvas(badge, width=34, height=34, bg=T.PANNEAU,
                          highlightthickness=0)
        noyau.pack(side=tk.LEFT, padx=(10, 8), pady=6)
        noyau.create_oval(4, 4, 30, 30, outline=T.BLEU_VIF, width=2)
        noyau.create_oval(11, 11, 23, 23, fill=T.CYAN, outline="")
        colonne = tk.Frame(badge, bg=T.PANNEAU)
        colonne.pack(side=tk.LEFT, padx=(0, 14))
        tk.Label(colonne, text="UVI OS", bg=T.PANNEAU, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        tk.Label(colonne, text="v9.0.0", bg=T.PANNEAU, fg=T.CYAN,
                 font=("Segoe UI", 8)).pack(anchor=tk.W)

        # Les boutons réduire / agrandir / fermer ont été RETIRÉS. Ils
        # doublonnaient les décorations de fenêtre du système, qui restent
        # présentes : l'utilisateur avait deux croix de fermeture à l'écran,
        # à deux endroits différents, dont une seule respectait le style de
        # son bureau. Une barre de titre personnalisée n'a de sens qu'avec
        # overrideredirect(True) -- ce qui obligerait alors à réimplémenter
        # à la main le déplacement, le redimensionnement, l'accrochage aux
        # bords et les raccourcis du gestionnaire de fenêtres. Coût
        # disproportionné pour un gain purement décoratif.

        # --- boutons conservés, non affichés dans la barre v9 ---------------
        # AST / BYTECODE / OPTIMISER / ANALYSER / STATS ont migré dans le
        # panneau OUTILS UVI à droite (maquette v9). Mais _attacher_infobulles
        # et plusieurs méthodes de IAAgent référencent encore self.btn_ast,
        # self.btn_bytecode, etc. On les crée donc dans un conteneur JAMAIS
        # empaqueté : les attributs existent, les widgets sont valides, rien
        # ne s'affiche. C'est plus honnête qu'un getattr défensif partout,
        # et ça évite de casser du code qui marche pour une raison purement
        # cosmétique.
        self._boutons_orphelins = tk.Frame(self.root, bg=T.FOND)
        for attribut, libelle, commande in (
            ("btn_ast", "AST", lambda: self.agent.export_ast() if self.agent else None),
            ("btn_bytecode", "BYTECODE", lambda: self.agent.export_bytecode() if self.agent else None),
            ("btn_optimiser", "OPTIMISER", lambda: self.agent.optimize_code() if self.agent else None),
            ("btn_analyser", "ANALYSER", lambda: self.agent.analyze_editor_code() if self.agent else None),
            ("btn_stats", "STATS", lambda: self.agent.report_stats() if self.agent else None),
            ("btn_auto_repair", "AUTO-RÉPARER", lambda: self.agent.auto_repair() if self.agent else None),
            ("btn_voice", "VOIX", self.toggle_voice),
            ("btn_open", "OUVRIR", self.load_from_disk),
            ("btn_save", "SAUVER", self.save_to_disk),
        ):
            setattr(self, attribut, tk.Button(self._boutons_orphelins,
                                              text=libelle, command=commande))
        # BUG CORRIGÉ : la refonte v9 a réécrit _build_topbar sans
        # reconduire l'appel à _attacher_infobulles. La méthode existait
        # toujours, aucun chemin ne l'appelait -- les infobulles étaient
        # mortes en silence, exactement comme autonomous_fix avant l'étape
        # 6. Rien ne le signalait : une infobulle absente ne lève pas.
        self._attacher_infobulles()
        self._attacher_raccourcis()

    def nouveau_fichier(self):
        """Vide l'éditeur après confirmation si du travail non vidé s'y
        trouve. La v8 n'avait pas de bouton NOUVEAU du tout : on effaçait à
        la main avec Ctrl+A puis Suppr, sans aucun garde-fou."""
        if self.editor.get("1.0", tk.END).strip():
            if not tk.messagebox.askyesno(
                "Nouveau fichier",
                "Effacer le contenu de l'éditeur ? Le travail non sauvegardé "
                "sera perdu."):
                return
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", "@UVI\n")
        self._update_line_numbers()
        self.fichier_courant = None
        self._chemin_disque_courant = None
        self._marquer_enregistre()
        self.show_editor()
        self._set_status("Nouveau fichier.")

    def _basculer_plein_ecran(self):
        etat = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not etat)

    def ouvrir_debogueur(self):
        """Bouton DÉBOGUEUR de la barre v9.

        Le moteur de points d'arrêt existe déjà (UVIDebugPause, breakpoints,
        pile d'appel) mais n'avait aucun déclencheur dans l'interface v8 --
        même situation qu'autonomous_fix avant l'étape 6. Ce bouton bascule
        l'exécution en mode pas-à-pas et affiche l'état courant dans la
        console, sans fenêtre supplémentaire (cohérent avec le reste du
        projet, qui rend ses diagnostics en texte)."""
        self._conseil_unique(
            "debogueur",
            "🐞 DÉBOGUEUR — posez un point d'arrêt en cliquant sur un numéro de "
            "ligne, puis lancez ▶ EXÉCUTER : le script s'arrêtera là et la "
            "console affichera les variables et la pile d'appel.",
        )
        self._basculer_point_arret_ligne_courante()

    def _basculer_point_arret_ligne_courante(self):
        """Pose ou retire un point d'arrêt sur la ligne du curseur."""
        try:
            ligne = int(self.editor.index(tk.INSERT).split(".")[0])
        except (ValueError, tk.TclError):
            return
        points = getattr(self, "_points_arret", None)
        if points is None:
            points = self._points_arret = set()
        if ligne in points:
            points.discard(ligne)
            self.log("Point d'arrêt retiré ligne %d." % ligne, "sys")
        else:
            points.add(ligne)
            self.log("Point d'arrêt posé ligne %d." % ligne, "ok")
        self._update_line_numbers()

    def _attacher_raccourcis(self):
        """Raccourcis clavier standards.

        Leur absence était un vrai frein : sauvegarder demandait de viser
        un bouton à la souris, à chaque fois. bind_all et non bind sur
        l'éditeur, pour qu'ils fonctionnent aussi quand le focus est dans
        la console ou l'explorateur -- un Ctrl+S qui ne marche « que si le
        curseur est au bon endroit » est plus déroutant que pas de
        raccourci du tout.

        Les handlers rendent "break" pour empêcher Tk d'appliquer EN PLUS
        son comportement par défaut sur ces séquences."""
        raccourcis = (
            ("<F5>", lambda e: self.basculer_execution()),
            ("<Control-Return>", lambda e: self.basculer_execution()),
            ("<Escape>", lambda e: self.arreter_script()),
            ("<Control-s>", lambda e: self.save_to_disk()),
            ("<Control-o>", lambda e: self.load_from_disk()),
            ("<Control-n>", lambda e: self.nouveau_fichier()),
            ("<Control-f>", lambda e: self.afficher_panneau_gauche("recherche")),
            ("<Control-l>", lambda e: self._clear_console()),
            ("<Control-slash>", lambda e: self.basculer_commentaire()),
            ("<F1>", lambda e: self.afficher_aide()),
            # F11 remplace le bouton ▢ retiré de la barre : le plein écran
            # reste accessible, par la touche que tout le monde essaie déjà.
            ("<F11>", lambda e: self._basculer_plein_ecran()),
            # Ctrl+K et non Ctrl+Shift+P (VS Code) : geste à une main, et
            # surtout laissé LIBRE ici -- voir _ouvrir_portail pour pourquoi
            # ce composant n'a pas été calqué sur une palette de commandes
            # classique au-delà du principe "chercher une action au clavier".
            ("<Control-k>", lambda e: self._ouvrir_portail()),
        )
        for sequence, handler in raccourcis:
            self.root.bind_all(sequence, lambda e, h=handler: (h(e), "break")[1])

    # ------------------------------------------------------------------
    # LE PORTAIL — lanceur d'actions au clavier (Ctrl+K)
    # ------------------------------------------------------------------
    # Un IDE "à la VS Code" aurait une palette de commandes en haut de
    # fenêtre. Le Portail fait le même travail (chercher puis lancer une
    # action par son nom plutôt que d'aller la chercher à la souris dans un
    # menu, une barre d'outils ou le rail) mais avec un parti pris visuel
    # différent, en accord avec l'identité "atelier" de la palette T (voir
    # sa docstring) plutôt qu'une imitation : un panneau CENTRÉ, en fondu
    # sur fond assombri, pensé comme un geste ponctuel qu'on invoque plutôt
    # qu'une barre permanente de plus collée en haut de l'écran.
    #
    # Le contenu, lui, est délibérément SANS AMBITION : chaque entrée de
    # _actions_portail() appelle une méthode qui existe déjà ailleurs dans
    # cette classe (barre d'outils, menus, rail). Le Portail est une façade
    # de recherche par-dessus des commandes déjà câblées, jamais un second
    # chemin de code pour la même action.
    def _appeler_agent(self, nom_methode: str):
        """Appelle self.agent.<nom_methode>(), ou notifie proprement si
        l'agent n'est pas encore initialisé -- évite de dupliquer la garde
        `if self.agent else None` déjà utilisée par la barre d'outils
        (voir _build_topbar) à chaque entrée du Portail qui touche l'agent."""
        if not self.agent:
            self._empiler_toast("Agent IA non initialisé.", "err")
            return
        getattr(self.agent, nom_methode)()

    def _actions_portail(self):
        return [
            ("Exécuter le script (tree-walker)", "▶", self.run_script),
            ("Exécuter (VM bytecode)", "⚡", self.run_script_vm),
            ("Arrêter l'exécution", "⏹", self.arreter_script),
            ("Nouveau fichier", "📄", self.nouveau_fichier),
            ("Ouvrir…", "📂", self.load_from_disk),
            ("Sauvegarder", "💾", self.save_to_disk),
            ("Effacer la console", "🧹", self._clear_console),
            ("Basculer plein écran", "⛶", self._basculer_plein_ecran),
            ("Aide", "❓", self.afficher_aide),
            ("Afficher : Éditeur", "📝", self.show_editor),
            ("Afficher : Terminal", "⌨", self.show_terminal),
            ("Afficher : Canevas", "🎨", self.show_canvas),
            ("Panneau : Explorateur", "📁", lambda: self.afficher_panneau_gauche("explorateur")),
            ("Panneau : Recherche", "🔍", lambda: self.afficher_panneau_gauche("recherche")),
            ("Panneau : Variables", "{ }", lambda: self.afficher_panneau_gauche("variables")),
            ("Panneau : Outils", "🛠", lambda: self.afficher_panneau_gauche("outils")),
            ("Panneau : Modules", "📦", lambda: self.afficher_panneau_gauche("modules")),
            ("Panneau : Tests", "🧪", lambda: self.afficher_panneau_gauche("tests")),
            ("Panneau : Paramètres", "⚙", lambda: self.afficher_panneau_gauche("parametres")),
            ("Exporter l'AST", "🌳", lambda: self._appeler_agent("export_ast")),
            ("Exporter le bytecode", "⚙", lambda: self._appeler_agent("export_bytecode")),
            ("Analyser le script", "🔍", lambda: self._appeler_agent("analyze_editor_code")),
            ("Auto-réparer (déterministe)", "🛠", lambda: self._appeler_agent("auto_repair")),
            ("Auto-corriger (agent IA)", "🤖", self.autonomous_fix_from_editor),
            ("Statistiques de session", "📊", lambda: self._appeler_agent("report_stats")),
        ]

    def _ouvrir_portail(self, event=None):
        if getattr(self, "_portail_overlay", None) is not None:
            return "break"

        self.root.update_idletasks()
        largeur = max(420, min(600, self.root.winfo_width() - 100))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - largeur) // 2
        y = self.root.winfo_rooty() + 96

        overlay = tk.Toplevel(self.root)
        overlay.wm_overrideredirect(True)
        overlay.wm_transient(self.root)
        try:
            overlay.wm_attributes("-topmost", True)
            overlay.wm_attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        overlay.wm_geometry("%dx360+%d+%d" % (largeur, x, y))
        overlay.configure(bg=T.BORDURE_VIVE)

        cadre = tk.Frame(overlay, bg=T.PANNEAU_HAUT, highlightthickness=2,
                         highlightbackground=T.CYAN, highlightcolor=T.CYAN)
        cadre.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        entete = tk.Frame(cadre, bg=T.PANNEAU_HAUT)
        entete.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Label(entete, text="◈", bg=T.PANNEAU_HAUT, fg=T.CYAN,
                font=("Segoe UI", 14)).pack(side=tk.LEFT, padx=(0, 8))
        champ = tk.Entry(entete, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FORT,
                         insertbackground=T.CYAN, relief=tk.FLAT, bd=0,
                         font=("Segoe UI", 13))
        champ.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(cadre, bg=T.BORDURE, height=1).pack(fill=tk.X, padx=16)

        liste = tk.Listbox(
            cadre, bg=T.PANNEAU_HAUT, fg=T.TEXTE, selectbackground=T.SURVOL,
            selectforeground=T.TEXTE_FORT, relief=tk.FLAT, highlightthickness=0,
            font=("Segoe UI", 10), activestyle="none", bd=0,
        )
        liste.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 12))

        actions = self._actions_portail()
        etat = {"visibles": actions}

        def _rafraichir(_event=None):
            terme = champ.get().strip().lower()
            etat["visibles"] = [a for a in actions if terme in a[0].lower()] if terme else actions
            liste.delete(0, tk.END)
            for libelle, icone, _cb in etat["visibles"]:
                liste.insert(tk.END, "   %s   %s" % (icone, libelle))
            if etat["visibles"]:
                liste.selection_clear(0, tk.END)
                liste.selection_set(0)

        def _naviguer(delta):
            if not liste.size():
                return "break"
            actuel = (liste.curselection() or (0,))[0]
            nouveau = max(0, min(liste.size() - 1, actuel + delta))
            liste.selection_clear(0, tk.END)
            liste.selection_set(nouveau)
            liste.see(nouveau)
            return "break"

        def _executer(_event=None):
            cible = etat["visibles"]
            selection = liste.curselection()
            i = selection[0] if selection else 0
            _fermer()
            if 0 <= i < len(cible):
                cible[i][2]()
            return "break"

        def _fermer(_event=None):
            self._portail_overlay = None
            try:
                overlay.destroy()
            except tk.TclError:
                pass
            try:
                self.root.focus_set()
            except tk.TclError:
                pass
            return "break"

        champ.bind("<KeyRelease>", _rafraichir)
        champ.bind("<Down>", lambda e: _naviguer(1))
        champ.bind("<Up>", lambda e: _naviguer(-1))
        champ.bind("<Return>", _executer)
        liste.bind("<Return>", _executer)
        liste.bind("<Double-Button-1>", _executer)
        overlay.bind("<Escape>", _fermer)
        champ.bind("<Escape>", _fermer)
        # Un clic hors du Portail le referme -- FocusOut sur la fenêtre
        # elle-même (pas sur le champ, qui perd le focus à chaque flèche
        # vers la liste sans qu'on veuille fermer pour autant).
        overlay.bind("<FocusOut>", lambda e: overlay.after(120, _verifier_focus))

        def _verifier_focus():
            try:
                if self.root.focus_get() is None and getattr(self, "_portail_overlay", None) is overlay:
                    _fermer()
            except (tk.TclError, KeyError):
                pass

        self._portail_overlay = overlay
        _rafraichir()
        champ.focus_set()

        def _fondu(alpha=0.0):
            try:
                alpha = min(0.98, alpha + 0.20)
                overlay.wm_attributes("-alpha", alpha)
                if alpha < 0.98:
                    overlay.after(10, lambda: _fondu(alpha))
            except tk.TclError:
                pass
        overlay.after(5, _fondu)
        return "break"

    # ------------------------------------------------------------------
    # Notifications flottantes (toasts) — canal non bloquant, distinct de
    # messagebox (réservée aux confirmations qui EXIGENT une réponse, voir
    # _on_close). Utile quand un évènement arrive pendant que l'utilisateur
    # regarde un autre onglet (réponse SHION, fin de script en fond) : la
    # barre de statut seule, en bas de la fenêtre, passe facilement
    # inaperçue.
    # ------------------------------------------------------------------
    def _empiler_toast(self, message: str, niveau: str = "info", duree_ms: int = 3400):
        try:
            self.root.winfo_exists()
        except tk.TclError:
            return
        couleurs = {"info": T.CYAN, "ok": T.VERT, "err": T.ROUGE, "sys": T.TEXTE_FAIBLE}
        accent = couleurs.get(niveau, T.CYAN)
        pile = getattr(self, "_toasts_actifs", None)
        if pile is None:
            pile = self._toasts_actifs = []

        fenetre = tk.Toplevel(self.root)
        fenetre.wm_overrideredirect(True)
        try:
            fenetre.wm_attributes("-topmost", True)
            fenetre.wm_attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        cadre = tk.Frame(fenetre, bg=T.PANNEAU_HAUT, highlightthickness=1,
                         highlightbackground=T.BORDURE_VIVE)
        cadre.pack()
        interieur = tk.Frame(cadre, bg=T.PANNEAU_HAUT)
        interieur.pack()
        tk.Frame(interieur, bg=accent, width=4).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(interieur, text=str(message), bg=T.PANNEAU_HAUT, fg=T.TEXTE_FORT,
                font=T.UI, wraplength=300, justify=tk.LEFT,
                padx=12, pady=10).pack(side=tk.LEFT)

        pile.append(fenetre)
        self._repositionner_toasts()

        def _retirer(_event=None):
            if fenetre in pile:
                pile.remove(fenetre)
            try:
                fenetre.destroy()
            except tk.TclError:
                pass
            self._repositionner_toasts()

        def _fondu(alpha=0.0):
            try:
                alpha = min(1.0, alpha + 0.18)
                fenetre.wm_attributes("-alpha", alpha)
                if alpha < 1.0:
                    fenetre.after(14, lambda: _fondu(alpha))
            except tk.TclError:
                pass

        fenetre.after(8, _fondu)
        fenetre.after(duree_ms, _retirer)
        cadre.bind("<Button-1>", _retirer)
        interieur.bind("<Button-1>", _retirer)

    def _repositionner_toasts(self):
        pile = getattr(self, "_toasts_actifs", [])
        try:
            x_droite = self.root.winfo_rootx() + self.root.winfo_width() - 18
            y = self.root.winfo_rooty() + self.root.winfo_height() - 46
        except tk.TclError:
            return
        for fenetre in reversed(pile):
            try:
                fenetre.update_idletasks()
                largeur = fenetre.winfo_reqwidth()
                hauteur = fenetre.winfo_reqheight()
                fenetre.wm_geometry("+%d+%d" % (x_droite - largeur, y - hauteur))
                y -= hauteur + 8
            except tk.TclError:
                pass

    def basculer_commentaire(self):
        """Ctrl+/ : commente ou décommente les lignes sélectionnées.

        Le caractère de commentaire d'UVI est # -- vérifié contre la règle
        COMMENT du lexer, pas supposé par analogie avec un autre langage.

        Si TOUTES les lignes non vides sont déjà commentées, on
        décommente ; sinon on commente. Traiter chaque ligne
        indépendamment produirait un basculement en damier sur une
        sélection mixte, ce que personne n'attend."""
        # ValueError en plus de TclError : Tk lève TclError quand il n'y a
        # pas de sélection, mais index() peut aussi rendre une chaîne
        # inattendue selon la version. Un raccourci clavier qui fait
        # remonter une trace Python est pire que pas de raccourci.
        try:
            debut = int(self.editor.index("sel.first").split(".")[0])
            fin = int(self.editor.index("sel.last").split(".")[0])
        except (tk.TclError, ValueError):
            try:
                debut = fin = int(self.editor.index(tk.INSERT).split(".")[0])
            except (tk.TclError, ValueError):
                return
        lignes = [self.editor.get("%d.0" % n, "%d.end" % n)
                  for n in range(debut, fin + 1)]
        pleines = [l for l in lignes if l.strip()]
        decommenter = bool(pleines) and all(
            l.lstrip().startswith("#") for l in pleines)
        for decalage, ligne in enumerate(lignes):
            numero = debut + decalage
            if not ligne.strip():
                continue
            if decommenter:
                indentation = ligne[:len(ligne) - len(ligne.lstrip())]
                nouvelle = indentation + ligne.lstrip()[1:].lstrip()
            else:
                nouvelle = "# " + ligne
            self.editor.delete("%d.0" % numero, "%d.end" % numero)
            self.editor.insert("%d.0" % numero, nouvelle)
        self._update_line_numbers()

    def afficher_aide(self):
        """F1 : rappel des raccourcis, dans la console.

        Dans la console et pas dans une fenêtre d'aide : c'est là que
        l'IDE dit déjà tout le reste, et une aide qu'on doit refermer pour
        continuer à travailler n'est pas consultée pendant qu'on
        travaille."""
        self.show_terminal()
        self.log("=== AIDE UVI ===", "sys")
        for touche, description in (
            ("F5 / Ctrl+Entrée", "Exécuter ou arrêter le script"),
            ("Échap", "Arrêter le script en cours"),
            ("Ctrl+S", "Sauvegarder"),
            ("Ctrl+O", "Ouvrir un fichier"),
            ("Ctrl+N", "Nouveau fichier"),
            ("Ctrl+F", "Rechercher"),
            ("Ctrl+/", "Commenter ou décommenter la sélection"),
            ("Ctrl+L", "Vider la console"),
            ("F11", "Plein écran"),
            ("F1", "Cette aide"),
        ):
            self.log("  %-18s %s" % (touche, description), "info")
        self.log("Double-cliquez une erreur de la console pour sauter à la "
                 "ligne fautive.", "info")
        self.log("Cliquez un numéro de ligne pour poser un point d'arrêt.", "info")
        self.log("Syntaxe du langage : panneau RACCOURCIS UVI du rail — "
                 "cliquez une ligne pour insérer un modèle.", "sys")

    def _attacher_infobulles(self):
        """ÉTAPE 7.4 : une infobulle par bouton dont la fonction n'est pas
        lisible sur l'étiquette.

        La barre d'outils n'est PAS réorganisée (contrainte explicite) :
        aucun bouton n'est déplacé, renommé, regroupé ni supprimé. On ajoute
        seulement de l'explication au survol. Les boutons évidents
        (ÉDITEUR, TERMINAL, OUVRIR, SAUVER, CLEAR) n'en reçoivent pas --
        une infobulle qui répète l'étiquette apprend à ignorer les
        infobulles.

        getattr défensif : btn_run_vm n'existe que si HAS_VM, et
        btn_auto_fix pourrait disparaître d'une future variante de la
        barre. Une infobulle manquante ne doit jamais empêcher l'IDE de
        démarrer."""
        cibles = [
            ("btn_run", "run"), ("btn_run_vm", "run_vm"),
            ("btn_ast", "ast"), ("btn_bytecode", "bytecode"),
            ("btn_optimiser", "optimiser"), ("btn_analyser", "analyser"),
            ("btn_auto_repair", "auto_repair"), ("btn_auto_fix", "auto_fix"),
            ("btn_stats", "stats"),
        ]
        for nom_attr, cle in cibles:
            widget = getattr(self, nom_attr, None)
            texte = TOOLBAR_TOOLTIPS.get(cle)
            if widget is not None and texte:
                _Tooltip(widget, texte, statusbar_fn=self._set_status)

    def _build_statusbar(self):
        barre = tk.Frame(self.root, bg=T.PANNEAU, height=34)
        barre.pack(fill=tk.X, side=tk.BOTTOM)
        barre.pack_propagate(False)
        tk.Frame(self.root, bg=T.BORDURE, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        gauche = tk.Frame(barre, bg=T.PANNEAU)
        gauche.pack(side=tk.LEFT, padx=14)
        self.voyant_etat = tk.Canvas(gauche, width=10, height=10, bg=T.PANNEAU,
                                     highlightthickness=0)
        self.voyant_etat.pack(side=tk.LEFT, pady=12)
        self.voyant_etat.create_oval(1, 1, 9, 9, fill=T.VERT, outline="")
        # self.statusbar garde son NOM : 13 appels existants (_set_status,
        # run_script, l'agent...) écrivent dedans. Seule son apparence change.
        self.statusbar = tk.Label(
            gauche, text=" PRÊT", fg=T.VERT, bg=T.PANNEAU,
            font=("Segoe UI", 9, "bold"), anchor=tk.W,
        )
        self.statusbar.pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(barre, text="UVI SECURE IDE  ·  ÉCRIT. VISE. CRÉE. EXÉCUTE.  ·  ⌃K ouvre le Portail",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=22)

        self.horloge = tk.Label(barre, text="--:--:--", bg=T.PANNEAU,
                                fg=T.TEXTE_FAIBLE, font=("Consolas", 9))
        self.horloge.pack(side=tk.RIGHT, padx=(8, 16))

        # Indicateur de connexion SHION : la maquette le demande, et il
        # répond à une vraie question que l'utilisateur se pose avant de
        # taper SHION.ASK -- « est-ce que le serveur répond ? ». Le savoir
        # AVANT plutôt qu'après un timeout de 180 s change l'expérience.
        cadre_shion = tk.Frame(barre, bg=T.PANNEAU)
        cadre_shion.pack(side=tk.RIGHT, padx=10)
        connecte = HAS_SHION and self.engine.shion_client is not None
        self.etiquette_shion = tk.Label(
            cadre_shion,
            text="CONNECTÉ À SHION" if connecte else "SHION HORS LIGNE",
            bg=T.PANNEAU, fg=T.TEXTE if connecte else T.TEXTE_FAIBLE,
            font=("Segoe UI", 8),
        )
        self.etiquette_shion.pack(side=tk.LEFT)
        self.voyant_shion = tk.Canvas(cadre_shion, width=10, height=10,
                                      bg=T.PANNEAU, highlightthickness=0)
        self.voyant_shion.pack(side=tk.LEFT, padx=6, pady=12)
        self.voyant_shion.create_oval(1, 1, 9, 9,
                                      fill=T.CYAN if connecte else T.TEXTE_FAIBLE,
                                      outline="")
        self._rafraichir_horloge()
        self._pomper_file_ui()

    def _poster_ui(self, travail):
        """Demande au thread principal d'exécuter `travail`.

        APPELABLE DEPUIS N'IMPORTE QUEL THREAD -- c'est tout l'intérêt.
        queue.Queue.put est protégé par un verrou interne, contrairement à
        root.after() qui est un appel Tcl et n'offre aucune garantie depuis
        un thread secondaire.

        Remplace tous les `self.root.after(0, ...)` qui étaient émis depuis
        des threads d'exécution. Ceux émis depuis le thread principal
        (horloge, rafraîchissement des panneaux) restent en after() : ils
        sont légitimes et n'ont pas besoin de la file."""
        self._file_ui.put(travail)

    def _pomper_file_ui(self):
        """Vide la file et se replanifie. Thread principal uniquement.

        50 ms : assez court pour que l'apparition de la barre de saisie
        paraisse instantanée (le seuil perceptif est autour de 100 ms),
        assez long pour ne pas peser quand il n'y a rien à faire -- la
        boucle ne coûte qu'un get_nowait qui échoue.

        get_nowait en boucle plutôt que get(block=True) : cette méthode
        tourne sur le thread de l'interface, et s'y bloquer figerait toute
        la fenêtre. C'est précisément le piège qu'on est en train de
        corriger, il serait absurde de le réintroduire ici."""
        try:
            while True:
                try:
                    travail = self._file_ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    travail()
                except Exception as e:
                    # Un travail fautif ne doit pas arrêter la pompe :
                    # sinon la première exception gèle définitivement tout
                    # échange thread -> interface, y compris la saisie.
                    try:
                        self.log("Erreur d'interface différée : %s : %s"
                                 % (type(e).__name__, e), "err")
                    except Exception:
                        pass
        finally:
            try:
                self.root.after(50, self._pomper_file_ui)
            except tk.TclError:
                pass  # fenêtre fermée

    def _rafraichir_horloge(self):
        """after() et non un thread : tout ce qui touche un widget doit
        rester sur le thread Tkinter. Un thread avec un sleep(1) écrivant
        dans un Label est le bug de concurrence classique de ce genre
        d'horloge -- il ne se manifeste que sporadiquement, ce qui le rend
        très coûteux à diagnostiquer plus tard."""
        try:
            self.horloge.config(text=datetime.now().strftime("%H:%M:%S"))
            self.root.after(1000, self._rafraichir_horloge)
        except tk.TclError:
            pass  # fenêtre fermée pendant l'attente : rien à rafraîchir

    # =====================================================================
    # DISPOSITION v9
    # =====================================================================
    # rail | colonne gauche | centre (éditeur + console + mémoire) | SHION
    #
    # DIFFÉRENCE STRUCTURELLE MAJEURE AVEC LA v8 : la console n'est plus une
    # VUE concurrente de l'éditeur, elle est un DOCK permanent sous lui. En
    # v8, voir sa sortie signifiait perdre son code de vue et inversement --
    # on basculait sans arrêt entre deux plein-écrans pour comparer un
    # message d'erreur à la ligne qui l'a produit. C'est le changement qui
    # se sent le plus à l'usage, bien plus que la couleur.
    #
    # Conséquence sur _show_view : "terminal" sort de la pile de vues
    # (self._VUES), puisque frame_terminal n'est plus empaqueté dans
    # self.workspace. show_terminal() est conservée -- elle est appelée à
    # une quinzaine d'endroits -- mais donne désormais le focus à la console
    # au lieu de masquer l'éditeur.

    def _cadre_defilant(self, parent, bg=None):
        """Zone à défilement vertical. Rend le cadre INTÉRIEUR où empiler le
        contenu.

        POURQUOI CE BRICOLAGE : Tkinter ne sait pas faire défiler un Frame.
        Seuls Text, Listbox et Canvas ont un protocole de défilement. Le
        seul montage possible est donc : Canvas défilant + Frame posé
        dedans via create_window + Scrollbar liée. C'est la recette standard
        et il n'y en a pas de plus simple -- ce n'est pas un détour, c'est
        LE moyen.

        Trois liaisons indispensables, chacune corrigeant un défaut réel :
          1. <Configure> sur le cadre intérieur -> recalcule scrollregion.
             Sans elle, la molette ne fait rien : le Canvas croit que son
             contenu tient entièrement dans la fenêtre.
          2. <Configure> sur le Canvas -> impose au cadre intérieur la
             largeur du Canvas. Sans elle, les panneaux se réduisent à la
             largeur de leur texte le plus long et flottent à gauche.
          3. molette : liée à l'ENTRÉE/SORTIE du pointeur, pas globalement.
             Un bind_all("<MouseWheel>") volerait la molette à l'éditeur et
             à la console dès que cette zone existe."""
        bg = bg or T.FOND
        conteneur = tk.Frame(parent, bg=bg)
        conteneur.pack(fill=tk.BOTH, expand=True)

        toile = tk.Canvas(conteneur, bg=bg, highlightthickness=0, bd=0)
        barre = tk.Scrollbar(conteneur, orient="vertical", command=toile.yview,
                             bg=T.PANNEAU, troughcolor=T.FOND, relief=tk.FLAT,
                             bd=0, width=10, activebackground=T.BORDURE_VIVE)
        toile.configure(yscrollcommand=barre.set)
        barre.pack(side=tk.RIGHT, fill=tk.Y)
        toile.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        interieur = tk.Frame(toile, bg=bg)
        fenetre = toile.create_window((0, 0), window=interieur, anchor="nw")

        def sur_contenu(_e=None):
            try:
                toile.configure(scrollregion=toile.bbox("all"))
            except tk.TclError:
                pass

        def sur_toile(event):
            try:
                toile.itemconfig(fenetre, width=event.width)
            except tk.TclError:
                pass

        interieur.bind("<Configure>", sur_contenu)
        toile.bind("<Configure>", sur_toile)

        def molette(event):
            # Windows/macOS envoient <MouseWheel> avec event.delta ;
            # X11 envoie des <Button-4>/<Button-5> sans delta. Les deux
            # chemins sont nécessaires, le projet tournant aussi sur
            # Raspberry Pi (donc X11).
            if getattr(event, "num", None) == 4:
                pas = -1
            elif getattr(event, "num", None) == 5:
                pas = 1
            else:
                pas = -1 if event.delta > 0 else 1
            try:
                toile.yview_scroll(pas, "units")
            except tk.TclError:
                pass

        def activer(_e=None):
            toile.bind_all("<MouseWheel>", molette)
            toile.bind_all("<Button-4>", molette)
            toile.bind_all("<Button-5>", molette)

        def desactiver(_e=None):
            for evenement in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    toile.unbind_all(evenement)
                except tk.TclError:
                    pass

        toile.bind("<Enter>", activer)
        toile.bind("<Leave>", desactiver)
        interieur.bind("<Enter>", activer)
        interieur.bind("<Leave>", desactiver)
        return interieur

    def _carte(self, parent, titre, accent=None, hauteur=None, menu=True):
        """Panneau titré du thème v9 (cf. « EXPLORATEUR UVI », « VARIABLES »,
        « OUTILS UVI » de la maquette). Rend le conteneur de contenu."""
        exterieur = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                             highlightbackground=T.BORDURE)
        if hauteur:
            exterieur.configure(height=hauteur)
            exterieur.pack_propagate(False)
        entete = tk.Frame(exterieur, bg=T.PANNEAU_HAUT, height=34)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        tk.Label(entete, text=titre, bg=T.PANNEAU_HAUT,
                 fg=accent or T.CYAN, font=T.UI_TITRE).pack(side=tk.LEFT, padx=12)
        if menu:
            tk.Label(entete, text="···", bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 11)).pack(side=tk.RIGHT, padx=12)
        contenu = tk.Frame(exterieur, bg=T.PANNEAU)
        contenu.pack(fill=tk.BOTH, expand=True)
        return contenu

    def _build_container(self):
        self.container = tk.Frame(self.root, bg=T.FOND)
        self.container.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        self._build_rail()
        self._build_sidebar()

        centre = tk.Frame(self.container, bg=T.FOND)
        centre.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)

        # --- dock inférieur : console + mémoire ---------------------------
        # BUGFIX (dock invisible sur fenêtre étroite en hauteur) : ce cadre
        # porte la console ET la barre de saisie interactive qui répond à
        # ?var (voir _ouvrir_saisie) -- sans lui à l'écran, un script qui
        # attend une réponse semble figé sans qu'on sache pourquoi. Il est
        # empaqueté ICI, EN PREMIER (avant cadre_editeur), et avec
        # side=BOTTOM plutôt que le TOP implicite : cadre_editeur est
        # extensible (expand=True, plus bas) et absorbe tout l'espace
        # restant une fois les cadres à taille FIXE placés -- mais si
        # cadre_editeur était empaqueté en premier, c'est LUI qui
        # capturerait la cavité en premier, et ce dock (hauteur fixe,
        # pack_propagate(False)) se retrouverait poussé hors de la fenêtre
        # dès que celle-ci est plus basse que la somme des deux (ex. écran
        # 1366x768, cf. le clamp de geometry() dans _build_window). En le
        # plaçant en premier avec side=BOTTOM, il réserve ses 290 px EN BAS
        # de la cavité AVANT que cadre_editeur ne prenne le reste -- même
        # position visuelle qu'avant (l'éditeur au-dessus, le dock en
        # dessous), mais son espace n'est plus jamais sacrifié en premier.
        dock = tk.Frame(centre, bg=T.FOND, height=290)
        dock.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        dock.pack_propagate(False)

        # workspace = pile de vues (éditeur / canevas / SHION).
        # Nom conservé : _show_view et les _build_*_tab l'utilisent.
        cadre_editeur = tk.Frame(centre, bg=T.PANNEAU, highlightthickness=1,
                                 highlightbackground=T.BORDURE)
        cadre_editeur.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_barre_onglets(cadre_editeur)
        self.workspace = tk.Frame(cadre_editeur, bg=T.PANNEAU)
        self.workspace.pack(fill=tk.BOTH, expand=True)

        self._build_editor()
        self._build_canvas_tab()
        self._build_shion_tab()
        self._build_barre_etat_editeur(cadre_editeur)

        self._build_terminal(dock)
        self._build_panneau_memoire(dock)

        self._build_colonne_shion()

    # ------------------------------------------------------------------
    # Rail d'icônes
    # ------------------------------------------------------------------
    def _build_rail(self):
        """Colonne d'icônes de gauche. Chaque entrée bascule un panneau ou
        une vue déjà existante -- le rail n'ajoute AUCUNE fonctionnalité, il
        rend visible ce qui était enfoui dans une barre d'outils de treize
        boutons où rien ne ressortait."""
        rail = tk.Frame(self.container, bg=T.PANNEAU, width=84)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        rail.pack_propagate(False)

        self._boutons_rail = {}
        entrees = [
            ("explorateur", "📁", "EXPLORER", None),
            ("recherche", "🔍", "RECHERCHE", None),
            ("variables", "{ }", "VARIABLES", None),
            ("outils", "🛠", "OUTILS", None),
            ("modules", "📦", "MODULES", None),
            ("tests", "🧪", "TESTS", None),
            ("parametres", "⚙", "PARAMÈTRES", None),
        ]
        # commande=None dans la table : chaque entrée bascule vers le
        # panneau homonyme. Avant, elles appelaient _focus_panneau, qui ne
        # faisait que faire CLIGNOTER un titre -- l'utilisateur cliquait,
        # rien ne s'ouvrait, il en concluait que le bouton était mort. Le
        # rail pilote désormais réellement le contenu de la colonne gauche,
        # sur le modèle d'une barre d'activité d'éditeur moderne.
        for cle, icone, libelle, _ in entrees:
            commande = (lambda c=cle: self.afficher_panneau_gauche(c))
            bloc = tk.Frame(rail, bg=T.PANNEAU, cursor="hand2")
            bloc.pack(fill=tk.X, pady=3)
            icone_lbl = tk.Label(bloc, text=icone, bg=T.PANNEAU, fg=T.TEXTE,
                                 font=("Segoe UI", 17))
            icone_lbl.pack(pady=(9, 1))
            texte_lbl = tk.Label(bloc, text=libelle, bg=T.PANNEAU,
                                 fg=T.TEXTE_FAIBLE, font=("Segoe UI", 6, "bold"))
            texte_lbl.pack(pady=(0, 9))
            # Le clic doit fonctionner sur le bloc ET sur ses deux enfants :
            # un Label posé sur un Frame intercepte l'événement, donc lier
            # seulement le Frame rend la moitié haute du bouton inerte.
            for widget in (bloc, icone_lbl, texte_lbl):
                widget.bind("<Button-1>", lambda e, c=commande: c())
                widget.bind("<Enter>", lambda e, b=bloc, i=icone_lbl:
                            (b.config(bg=T.SURVOL), i.config(bg=T.SURVOL)))
                widget.bind("<Leave>", lambda e, b=bloc, i=icone_lbl:
                            (b.config(bg=T.PANNEAU), i.config(bg=T.PANNEAU)))
            self._boutons_rail[cle] = (bloc, icone_lbl, texte_lbl)

        pied = tk.Canvas(rail, width=52, height=52, bg=T.PANNEAU,
                         highlightthickness=0)
        pied.pack(side=tk.BOTTOM, pady=16)
        pied.create_rectangle(2, 2, 50, 50, outline=T.BORDURE_VIVE, width=1)
        pied.create_text(26, 24, text="UVI", fill=T.CYAN,
                         font=("Segoe UI", 11, "bold"))

    def afficher_panneau_gauche(self, nom):
        """Bascule la colonne gauche vers le panneau `nom` et met le rail en
        accord.

        Les sept panneaux sont construits UNE fois au démarrage puis
        masqués/affichés par pack_forget/pack -- pas reconstruits à chaque
        clic. Reconstruire perdrait l'état à chaque bascule : le texte tapé
        dans la recherche, les expressions surveillées, les champs de
        paramètres à moitié remplis."""
        panneaux = getattr(self, "_panneaux_gauche", {})
        if nom not in panneaux:
            return
        for cle, cadre in panneaux.items():
            cadre.pack_forget()
        panneaux[nom].pack(fill=tk.BOTH, expand=True)
        self._panneau_gauche_actif = nom
        self.titre_colonne_gauche.config(
            text=self._TITRES_PANNEAUX.get(nom, nom.upper()))

        for cle, (bloc, icone, texte) in getattr(self, "_boutons_rail", {}).items():
            actif = (cle == nom)
            fond = T.SURVOL if actif else T.PANNEAU
            try:
                bloc.config(bg=fond)
                icone.config(bg=fond, fg=T.CYAN if actif else T.TEXTE)
                texte.config(bg=fond, fg=T.TEXTE if actif else T.TEXTE_FAIBLE)
            except tk.TclError:
                pass

        # Les panneaux qui affichent un état vivant sont rafraîchis à
        # l'ouverture : un panneau MODULES montrant l'état d'il y a dix
        # minutes serait pire qu'un panneau vide.
        rafraichisseurs = {
            "explorateur": self.update_vfs_list,
            "variables": self.rafraichir_inspecteur,
            "modules": self.rafraichir_modules,
            "parametres": self.rafraichir_parametres,
        }
        action = rafraichisseurs.get(nom)
        if action:
            try:
                action()
            except Exception:
                pass

    def _focus_panneau(self, nom):
        """Conservée : _build_colonne_shion et d'anciens appels la
        référencent. Redirige vers la vraie bascule."""
        self.afficher_panneau_gauche(nom)

    def _ouvrir_recherche(self):
        """Recherche dans l'éditeur, via la barre de commande déjà présente
        sous la console -- plutôt qu'une boîte de dialogue supplémentaire."""
        self.show_terminal()
        self.cmd_input.delete(0, tk.END)
        self.cmd_input.insert(0, "chercher ")
        self.cmd_input.focus_set()
        self.cmd_input.icursor(tk.END)
        self.log("Tapez le texte à chercher après « chercher » puis Entrée.", "sys")

    def _afficher_modules(self):
        self.show_terminal()
        modules = sorted(getattr(self.engine, "modules", {}) or {})
        self.log("=== MODULES CHARGÉS ===", "sys")
        if not modules:
            self.log("Aucun module chargé. Utilisez MODULE \"fichier.uvi\" -> alias.", "info")
        for alias in modules:
            self.log("  %s" % alias, "info")
        fichiers = [f for f in self.vfs.list_files() if f.endswith(".uvi")]
        self.log("Fichiers .uvi disponibles : %s" % (", ".join(fichiers) or "aucun"), "info")

    def _lancer_tests(self):
        """Onglet TESTS : exécute le script dans les DEUX moteurs et compare.

        Ce n'est pas un lanceur de tests unitaires (ceux-là vivent dans
        tests/, sous pytest). C'est la version interactive de la propriété
        la plus utile du projet -- tree-walker et VM doivent rendre le même
        résultat observable. Un écart signalé ici est un vrai bug de moteur,
        pas une erreur de l'utilisateur."""
        self._selectionner_onglet_console("TESTS")
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.log("Éditeur vide : rien à comparer.", "err")
            return
        lignes = code.split("\n")
        self.log("=== COMPARAISON DES DEUX MOTEURS ===", "sys")
        hote = HeadlessHost()
        moteur = UVIEngine(hote, InMemoryVFS())
        try:
            moteur.run_sequence(list(lignes))
            sortie_tw, erreurs_tw = list(hote.sortie), list(hote.erreurs)
        except Exception as e:
            sortie_tw, erreurs_tw = [], ["exception : %s" % e]
        try:
            resultat = compile_and_run(list(lignes), self.engine.parser,
                                       UVIBytecodeCompilerV2(), vfs=InMemoryVFS())
            sortie_vm = [str(x) for x in resultat.output]
            trap = resultat.trapped
        except Exception as e:
            sortie_vm, trap = [], type(e).__name__
        self.log("  tree-walker : %d ligne(s), %d erreur(s)"
                 % (len(sortie_tw), len(erreurs_tw)), "info")
        self.log("  VM          : %d ligne(s), trap=%s" % (len(sortie_vm), trap), "info")
        if sortie_tw == sortie_vm and not erreurs_tw and trap is None:
            self.log("✓ Les deux moteurs sont d'accord.", "ok")
        else:
            self.log("✗ DIVERGENCE entre les moteurs — c'est un bug du moteur, "
                     "pas de votre script.", "err")
            for i, (a, b) in enumerate(zip(sortie_tw, sortie_vm)):
                if a != b:
                    self.log("  ligne %d : tree-walker=%r | VM=%r" % (i + 1, a, b), "err")
            for e in erreurs_tw[:3]:
                self.log("  tree-walker : %s" % e, "err")

    def _afficher_parametres(self):
        self.show_terminal()
        self.log("=== PARAMÈTRES UVI ===", "sys")
        for libelle, valeur in (
            ("Backend SHION", SHION_BACKEND),
            ("Serveur SHION", SHION_SERVER_URL),
            ("Modèle", DEEPSEEK_MODEL_NAME if SHION_BACKEND != "shion" else SHION_MODEL_NAME),
            ("SHION disponible", "oui" if HAS_SHION and self.engine.shion_client else "non"),
            ("VM disponible", "oui" if HAS_VM else "non"),
            ("Stockage", self.vfs.storage_dir if hasattr(self.vfs, "storage_dir") else "mémoire"),
            ("Mémoire max", "%d octets" % MAX_MEMORY_BYTES),
        ):
            self.log("  %-18s %s" % (libelle + " :", valeur), "info")
        self.log("Ces valeurs se règlent par variables d'environnement — "
                 "voir docs/SHION_ARCHITECTURE.md (backend shion) ou "
                 "docs/SERVEUR_DEEPSEEK.md (backend deepseek).", "sys")

    # ------------------------------------------------------------------
    # Colonne gauche
    # ------------------------------------------------------------------
    _TITRES_PANNEAUX = {
        "explorateur": "EXPLORATEUR UVI",
        "recherche": "RECHERCHE",
        "variables": "INSPECTEUR DE VARIABLES",
        "outils": "OUTILS UVI",
        "modules": "MODULES & PAQUETS",
        "tests": "TESTS",
        "parametres": "PARAMÈTRES",
    }

    def _build_sidebar(self):
        """Colonne gauche : sept panneaux pilotés par le rail.

        Garde son nom et self.frame_sidebar (référencés ailleurs), ainsi que
        self.vfs_listbox dont dépendent update_vfs_list() et
        _on_vfs_double_click()."""
        self.frame_sidebar = tk.Frame(self.container, bg=T.FOND, width=286)
        self.frame_sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0), pady=6)
        self.frame_sidebar.pack_propagate(False)

        entete = tk.Frame(self.frame_sidebar, bg=T.PANNEAU_HAUT, height=34)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        self.titre_colonne_gauche = tk.Label(
            entete, text="EXPLORATEUR UVI", bg=T.PANNEAU_HAUT, fg=T.CYAN,
            font=T.UI_TITRE)
        self.titre_colonne_gauche.pack(side=tk.LEFT, padx=12)

        self.zone_panneaux = tk.Frame(self.frame_sidebar, bg=T.FOND)
        self.zone_panneaux.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        self._panneaux_gauche = {}
        self._build_panneau_explorateur()
        self._build_panneau_recherche()
        self._build_panneau_variables()
        self._build_panneau_outils()
        self._build_panneau_modules()
        self._build_panneau_tests()
        self._build_panneau_parametres()
        self.afficher_panneau_gauche("explorateur")

    def _nouveau_panneau(self, nom):
        cadre = tk.Frame(self.zone_panneaux, bg=T.FOND)
        self._panneaux_gauche[nom] = cadre
        return cadre

    # ==================================================================
    # 1. EXPLORATEUR
    # ==================================================================
    def _build_panneau_explorateur(self):
        """Explorateur + variables + aide-mémoire, EMPILÉS ET DÉFILANTS.

        Les trois cartes ne tenaient pas dans la colonne : l'aide-mémoire
        était coupée en bas de fenêtre, invisible sur un écran de portable.
        Le défilement les rend toutes atteignables sans rien retirer."""
        panneau = self._nouveau_panneau("explorateur")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "FICHIERS", menu=False)
        contenu.master.pack(fill=tk.X)
        self._entete_explorateur = self.titre_colonne_gauche

        barre_actions = tk.Frame(contenu, bg=T.PANNEAU)
        barre_actions.pack(fill=tk.X, padx=10, pady=(8, 4))
        for icone, infobulle, commande in (
            ("＋", "Nouveau fichier dans le stockage", self.creer_fichier_vfs),
            ("✎", "Renommer le fichier sélectionné", self.renommer_fichier_vfs),
            ("⧉", "Dupliquer le fichier sélectionné", self.dupliquer_fichier_vfs),
            ("🗑", "Supprimer le fichier sélectionné", self.supprimer_fichier_vfs),
            ("🔄", "Actualiser la liste", self.update_vfs_list),
        ):
            bouton = tk.Button(
                barre_actions, text=icone, bg=T.CHAMP, fg=T.TEXTE,
                activebackground=T.SURVOL, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 9), cursor="hand2", command=commande,
                padx=8, pady=3)
            bouton.pack(side=tk.LEFT, padx=2)
            _Tooltip(bouton, infobulle, statusbar_fn=self._set_status)

        tk.Label(contenu, text="▾  PROJET_UVI", bg=T.PANNEAU, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, padx=12, pady=(4, 2))

        cadre_liste = tk.Frame(contenu, bg=T.PANNEAU, height=230)
        cadre_liste.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        cadre_liste.pack_propagate(False)
        defilement = tk.Scrollbar(cadre_liste, orient="vertical", bg=T.PANNEAU,
                                  troughcolor=T.CHAMP, relief=tk.FLAT, bd=0, width=9)
        defilement.pack(side=tk.RIGHT, fill=tk.Y)
        self.vfs_listbox = tk.Listbox(
            cadre_liste, bg=T.PANNEAU, fg=T.TEXTE,
            selectbackground=T.SURVOL, selectforeground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 10), highlightthickness=0,
            activestyle="none", borderwidth=0, yscrollcommand=defilement.set)
        self.vfs_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        defilement.config(command=self.vfs_listbox.yview)
        self.vfs_listbox.bind("<Double-Button-1>", self._on_vfs_double_click)

        contenu_vars = self._carte(interieur, "VARIABLES", hauteur=200, menu=False)
        contenu_vars.master.pack(fill=tk.X, pady=(8, 0))
        self._entete_variables = self.titre_colonne_gauche
        self.table_variables = tk.Text(
            contenu_vars, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6)
        self.table_variables.pack(fill=tk.BOTH, expand=True)
        for etiquette, couleur in (("nom", T.TEXTE), ("txt", T.SYN_CHAINE),
                                   ("num", T.SYN_NOMBRE), ("tab", T.VIOLET),
                                   ("bool", T.VERT), ("vide", T.TEXTE_FAIBLE)):
            self.table_variables.tag_configure(etiquette, foreground=couleur)

        contenu_aide = self._carte(interieur, "RACCOURCIS UVI", menu=False)
        contenu_aide.master.pack(fill=tk.X, pady=(8, 8))
        for symbole, nom, description, couleur in self._AIDE_SYNTAXE:
            ligne = tk.Frame(contenu_aide, bg=T.PANNEAU, cursor="hand2")
            ligne.pack(fill=tk.X, padx=10, pady=2)
            tk.Label(ligne, text=symbole, bg=T.CHAMP, fg=couleur,
                     font=("Consolas", 9, "bold"), width=4).pack(side=tk.LEFT)
            tk.Label(ligne, text=nom, bg=T.PANNEAU, fg=T.TEXTE,
                     font=("Segoe UI", 8, "bold"), width=10,
                     anchor=tk.W).pack(side=tk.LEFT, padx=(7, 0))
            tk.Label(ligne, text=description, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 7), anchor=tk.W).pack(side=tk.LEFT)
            # Cliquer insère un gabarit dans l'éditeur : l'aide-mémoire
            # devient un outil de saisie et plus seulement une légende.
            for widget in (ligne,) + tuple(ligne.winfo_children()):
                widget.bind("<Button-1>",
                            lambda e, s=symbole: self.inserer_gabarit(s))
        tk.Label(contenu_aide, text="Cliquez une ligne pour insérer un modèle.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 7, "italic")).pack(anchor=tk.W, padx=12, pady=(4, 10))

    _AIDE_SYNTAXE = (
        ("!", "AFFICHER", "Afficher un texte ou une variable", "#22C55E"),
        ("?", "DEMANDER", "Lire une saisie utilisateur", "#22D3EE"),
        ("%%", "BOUCLE", "Répéter tant que la condition tient", "#3B82F6"),
        ("%", "RÉPÉTER", "Répéter un nombre de fois", "#3B82F6"),
        ("~", "CONDITION", "Exécuter selon une condition", "#A855F7"),
        ("DEF", "FONCTION", "Définir une fonction", "#F59E0B"),
        ("CALL", "APPELER", "Appeler une fonction", "#38BDF8"),
        ("TAB", "TABLEAU", "Créer et manipuler un tableau", "#A855F7"),
        ("STR", "CHAÎNE", "Opérations sur les chaînes", "#FBBF24"),
        ("&", "DISQUE", "Lire et écrire dans le stockage", "#22D3EE"),
    )

    _GABARITS = {
        "!": '!"votre message"',
        "?": "? variable",
        "%%": "%% condition -> instruction",
        "%": "% 10 -> instruction",
        "~": '~ condition -> !"vrai"',
        "DEF": "DEF ma_fonction(a) -> _retour = a",
        "CALL": "CALL ma_fonction(1) -> resultat",
        "TAB": "TAB.CREATE mon_tableau",
        "STR": 'STR.LEN "texte" -> longueur',
        "&": '&save "fichier.uvi" -> "contenu"',
    }

    def inserer_gabarit(self, symbole):
        """Insère un modèle de la construction cliquée à la position du
        curseur. Les gabarits sont écrits dans la VRAIE syntaxe UVI, vérifiée
        par les tests du parser -- pas de pseudo-code approximatif qui
        obligerait l'utilisateur à deviner la forme correcte."""
        gabarit = self._GABARITS.get(symbole)
        if not gabarit:
            return
        self.show_editor()
        self.editor.insert(tk.INSERT, gabarit + "\n")
        self._update_line_numbers()
        self._set_status("Modèle inséré : %s" % symbole)

    # -- actions sur les fichiers du VFS --------------------------------
    def _fichier_selectionne(self):
        selection = self.vfs_listbox.curselection()
        if not selection:
            return None
        return getattr(self, "_index_fichiers", {}).get(selection[0])

    def creer_fichier_vfs(self):
        nom = self._demander_texte("Nouveau fichier", "Nom du fichier (.uvi) :")
        if not nom:
            return
        if not nom.endswith(".uvi"):
            nom += ".uvi"
        try:
            if self.vfs.exists(nom):
                self.log("[FILE_EXISTS] %s existe déjà." % nom, "err")
                return
            self.vfs.write_file(nom, '@UVI\n!"Bonjour depuis %s"\n' % nom)
        except UVIRuntimeTrap as trap:
            # Les noms de fichiers passent par _safe_resolve : « ../evasion »
            # est refusé ici comme il l'est depuis un script.
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s créé." % nom)

    def renommer_fichier_vfs(self):
        ancien = self._fichier_selectionne()
        if not ancien:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        nouveau = self._demander_texte("Renommer", "Nouveau nom :", ancien)
        if not nouveau or nouveau == ancien:
            return
        try:
            contenu = self.vfs.read_file(ancien) or ""
            self.vfs.write_file(nouveau, contenu)
            self.vfs.delete_file(ancien)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s → %s" % (ancien, nouveau))

    def dupliquer_fichier_vfs(self):
        nom = self._fichier_selectionne()
        if not nom:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        base = nom[:-4] if nom.endswith(".uvi") else nom
        copie = "%s_copie.uvi" % base
        try:
            self.vfs.write_file(copie, self.vfs.read_file(nom) or "")
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("Copie créée : %s" % copie)

    def supprimer_fichier_vfs(self):
        nom = self._fichier_selectionne()
        if not nom:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        if not tk.messagebox.askyesno("Supprimer",
                                      "Supprimer définitivement %s ?" % nom):
            return
        try:
            self.vfs.delete_file(nom)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s supprimé." % nom)

    def _demander_texte(self, titre, invite, valeur_initiale=""):
        """Petite boîte de saisie modale, écrite ici plutôt qu'importée de
        tkinter.simpledialog : simpledialog impose son propre style clair,
        qui jurerait au milieu d'une interface entièrement sombre. Vingt
        lignes évitent cette rupture visuelle."""
        fenetre = tk.Toplevel(self.root)
        fenetre.title(titre)
        fenetre.configure(bg=T.PANNEAU)
        fenetre.geometry("380x150")
        fenetre.transient(self.root)
        fenetre.grab_set()
        tk.Label(fenetre, text=invite, bg=T.PANNEAU, fg=T.TEXTE,
                 font=T.UI).pack(anchor=tk.W, padx=18, pady=(18, 6))
        champ = tk.Entry(fenetre, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
                         relief=tk.FLAT, font=("Consolas", 10),
                         highlightthickness=1, highlightbackground=T.BORDURE,
                         highlightcolor=T.CYAN)
        champ.pack(fill=tk.X, padx=18, ipady=6)
        champ.insert(0, valeur_initiale)
        champ.focus_set()
        resultat = {"valeur": None}

        def valider(_e=None):
            resultat["valeur"] = champ.get().strip()
            fenetre.destroy()

        champ.bind("<Return>", valider)
        champ.bind("<Escape>", lambda e: fenetre.destroy())
        barre = tk.Frame(fenetre, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=18, pady=16)
        tk.Button(barre, text="Valider", bg=T.BLEU, fg="#FFFFFF", relief=tk.FLAT,
                  bd=0, font=T.UI_GRAS, cursor="hand2", command=valider,
                  padx=16, pady=5).pack(side=tk.RIGHT)
        tk.Button(barre, text="Annuler", bg=T.CHAMP, fg=T.TEXTE, relief=tk.FLAT,
                  bd=0, font=T.UI, cursor="hand2", command=fenetre.destroy,
                  padx=16, pady=5).pack(side=tk.RIGHT, padx=8)
        self.root.wait_window(fenetre)
        return resultat["valeur"]

    # ==================================================================
    # 2. RECHERCHE
    # ==================================================================
    def _build_panneau_recherche(self):
        """Recherche dans l'éditeur ET dans tout le stockage.

        La v9 renvoyait auparavant vers la barre de commande avec un
        « tapez chercher... » -- une redirection, pas une fonctionnalité.
        Ici la recherche est réelle : résultats cliquables qui sautent à la
        ligne, ou ouvrent le fichier concerné."""
        panneau = self._nouveau_panneau("recherche")
        contenu = self._carte(panneau, "RECHERCHER", menu=False)
        contenu.master.pack(fill=tk.BOTH, expand=True)

        self.champ_recherche = tk.Entry(
            contenu, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 10), highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.CYAN)
        self.champ_recherche.pack(fill=tk.X, padx=10, pady=(10, 6), ipady=6)
        self.champ_recherche.bind("<Return>", lambda e: self.lancer_recherche())

        options = tk.Frame(contenu, bg=T.PANNEAU)
        options.pack(fill=tk.X, padx=10)
        self.portee_recherche = tk.StringVar(value="editeur")
        for valeur, libelle in (("editeur", "Éditeur"), ("vfs", "Tout le stockage")):
            tk.Radiobutton(
                options, text=libelle, value=valeur, variable=self.portee_recherche,
                bg=T.PANNEAU, fg=T.TEXTE, selectcolor=T.CHAMP,
                activebackground=T.PANNEAU, activeforeground=T.CYAN,
                font=("Segoe UI", 8), relief=tk.FLAT, highlightthickness=0,
                command=self.lancer_recherche).pack(side=tk.LEFT, padx=(0, 10))

        self._bouton_plat(contenu, "Rechercher", self.lancer_recherche,
                          couleur=T.CYAN, icone="🔍").pack(fill=tk.X, padx=10, pady=8)

        self.compteur_recherche = tk.Label(
            contenu, text="Aucune recherche.", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Segoe UI", 8), anchor=tk.W)
        self.compteur_recherche.pack(fill=tk.X, padx=12)

        cadre = tk.Frame(contenu, bg=T.PANNEAU)
        cadre.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))
        defilement = tk.Scrollbar(cadre, orient="vertical", bg=T.PANNEAU,
                                  troughcolor=T.CHAMP, relief=tk.FLAT, bd=0, width=9)
        defilement.pack(side=tk.RIGHT, fill=tk.Y)
        self.resultats_recherche = tk.Listbox(
            cadre, bg=T.PANNEAU, fg=T.TEXTE, selectbackground=T.SURVOL,
            selectforeground=T.CYAN, relief=tk.FLAT, font=("Consolas", 9),
            highlightthickness=0, activestyle="none", borderwidth=0,
            yscrollcommand=defilement.set)
        self.resultats_recherche.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        defilement.config(command=self.resultats_recherche.yview)
        self.resultats_recherche.bind("<Double-Button-1>", self._aller_au_resultat)
        self._cibles_recherche = {}

    def lancer_recherche(self):
        motif = self.champ_recherche.get().strip()
        self.resultats_recherche.delete(0, tk.END)
        self._cibles_recherche = {}
        if not motif:
            self.compteur_recherche.config(text="Saisissez un texte à chercher.")
            return
        minuscule = motif.lower()
        position = 0

        if self.portee_recherche.get() == "editeur":
            lignes = self.editor.get("1.0", tk.END).split("\n")
            for numero, ligne in enumerate(lignes, start=1):
                if minuscule in ligne.lower():
                    self.resultats_recherche.insert(
                        tk.END, "L%-4d %s" % (numero, ligne.strip()[:40]))
                    self._cibles_recherche[position] = ("editeur", None, numero)
                    position += 1
        else:
            for nom in self.vfs.list_files():
                if nom.startswith("."):
                    continue
                try:
                    contenu = self.vfs.read_file(nom) or ""
                except UVIRuntimeTrap:
                    continue
                for numero, ligne in enumerate(contenu.split("\n"), start=1):
                    if minuscule in ligne.lower():
                        self.resultats_recherche.insert(
                            tk.END, "%s:%d  %s" % (nom[:14], numero, ligne.strip()[:26]))
                        self._cibles_recherche[position] = ("vfs", nom, numero)
                        position += 1

        self.compteur_recherche.config(
            text="%d résultat(s) — double-cliquez pour y aller." % position
            if position else "Aucun résultat pour « %s »." % motif)

    def _aller_au_resultat(self, _event=None):
        selection = self.resultats_recherche.curselection()
        if not selection:
            return
        cible = self._cibles_recherche.get(selection[0])
        if not cible:
            return
        portee, nom, numero = cible
        if portee == "vfs":
            contenu = self.vfs.read_file(nom)
            if contenu is None:
                return
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", contenu)
            if hasattr(self, "etiquette_fichier"):
                self.etiquette_fichier.config(text=nom)
        self.show_editor()
        try:
            self.editor.mark_set(tk.INSERT, "%d.0" % numero)
            self.editor.see("%d.0" % numero)
            self.editor.tag_remove("ligne_courante", "1.0", tk.END)
            self.editor.tag_add("ligne_courante", "%d.0" % numero, "%d.end" % numero)
        except tk.TclError:
            pass
        self._update_line_numbers()
        self._rafraichir_position_curseur()

    # ==================================================================
    # 3. INSPECTEUR DE VARIABLES
    # ==================================================================
    def _build_panneau_variables(self):
        """Inspecteur complet : toutes les variables, leur type, et des
        expressions surveillées.

        Les expressions surveillées sont évaluées par
        UVIEvaluator.evaluate() -- le VRAI évaluateur du langage, pas un
        eval() Python. Écrire « x * 2 » ou « alias::CONSTANTE » dans une
        surveillance suit donc exactement les mêmes règles qu'une expression
        de script, y compris les gardes de mémoire."""
        panneau = self._nouveau_panneau("variables")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "ÉTAT DE LA MÉMOIRE", menu=False)
        contenu.master.pack(fill=tk.X)
        barre = tk.Frame(contenu, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.montrer_variables_internes = tk.BooleanVar(value=False)
        tk.Checkbutton(
            barre, text="Variables internes (_)",
            variable=self.montrer_variables_internes,
            bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, selectcolor=T.CHAMP,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            font=("Segoe UI", 8), relief=tk.FLAT, highlightthickness=0,
            command=self.rafraichir_inspecteur).pack(side=tk.LEFT)
        self._bouton_plat(barre, "Actualiser", self.rafraichir_inspecteur,
                          couleur=T.CYAN, icone="🔄", gras=False,
                          police=("Segoe UI", 8)).pack(side=tk.RIGHT)

        self.inspecteur = tk.Text(
            contenu, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6, height=16)
        self.inspecteur.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        for etiquette, couleur in (("nom", T.TEXTE_FORT), ("type", T.TEXTE_FAIBLE),
                                   ("txt", T.SYN_CHAINE), ("num", T.SYN_NOMBRE),
                                   ("tab", T.VIOLET), ("bool", T.VERT),
                                   ("vide", T.TEXTE_FAIBLE)):
            self.inspecteur.tag_configure(etiquette, foreground=couleur)

        contenu_surv = self._carte(interieur, "EXPRESSIONS SURVEILLÉES", menu=False)
        contenu_surv.master.pack(fill=tk.X, pady=(8, 8))
        saisie = tk.Frame(contenu_surv, bg=T.PANNEAU)
        saisie.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.champ_surveillance = tk.Entry(
            saisie, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 9), highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.CYAN)
        self.champ_surveillance.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.champ_surveillance.bind("<Return>", lambda e: self.ajouter_surveillance())
        tk.Button(saisie, text="＋", bg=T.BLEU, fg="#FFFFFF", relief=tk.FLAT,
                  bd=0, font=("Segoe UI", 9, "bold"), cursor="hand2",
                  command=self.ajouter_surveillance, padx=10).pack(side=tk.RIGHT, padx=(6, 0))

        self.surveillances = []
        self.liste_surveillances = tk.Text(
            contenu_surv, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6, height=7)
        self.liste_surveillances.pack(fill=tk.BOTH, expand=True)
        self.liste_surveillances.tag_configure("expr", foreground=T.CYAN)
        self.liste_surveillances.tag_configure("val", foreground=T.SYN_CHAINE)
        self.liste_surveillances.tag_configure("err", foreground=T.ROUGE)
        self.liste_surveillances.tag_configure("vide", foreground=T.TEXTE_FAIBLE)
        self._bouton_plat(contenu_surv, "Tout effacer",
                          self.effacer_surveillances, couleur=T.TEXTE_FAIBLE,
                          gras=False, police=("Segoe UI", 8)).pack(
            fill=tk.X, padx=10, pady=(4, 10))

    def rafraichir_inspecteur(self):
        if not hasattr(self, "inspecteur"):
            return
        try:
            self.inspecteur.config(state=tk.NORMAL)
            self.inspecteur.delete("1.0", tk.END)
            memoire = getattr(self.engine, "memoire", {}) or {}
            internes = self.montrer_variables_internes.get()
            entrees = [(n, v) for n, v in sorted(memoire.items())
                       if internes or not n.startswith("_")]
            if not entrees:
                self.inspecteur.insert(tk.END,
                    "Aucune variable en mémoire.\nExécutez un script.\n", "vide")
            for nom, valeur in entrees:
                if isinstance(valeur, list):
                    genre, etiquette = "tableau[%d]" % len(valeur), "tab"
                elif isinstance(valeur, bool) or str(valeur) in ("VRAI", "FAUX"):
                    genre, etiquette = "booléen", "bool"
                elif isinstance(valeur, (int, float)):
                    genre, etiquette = "nombre", "num"
                else:
                    genre, etiquette = "texte", "txt"
                texte = str(valeur)
                if len(texte) > 30:
                    texte = texte[:27] + "..."
                self.inspecteur.insert(tk.END, "%-16s " % nom[:16], "nom")
                self.inspecteur.insert(tk.END, "%-11s " % genre, "type")
                self.inspecteur.insert(tk.END, texte + "\n", etiquette)
            self.inspecteur.config(state=tk.DISABLED)
        except tk.TclError:
            pass
        self.rafraichir_surveillances()
        self.rafraichir_variables()

    def ajouter_surveillance(self):
        expression = self.champ_surveillance.get().strip()
        if not expression or expression in self.surveillances:
            return
        self.surveillances.append(expression)
        self.champ_surveillance.delete(0, tk.END)
        self.rafraichir_surveillances()

    def effacer_surveillances(self):
        self.surveillances = []
        self.rafraichir_surveillances()

    def rafraichir_surveillances(self):
        if not hasattr(self, "liste_surveillances"):
            return
        try:
            self.liste_surveillances.config(state=tk.NORMAL)
            self.liste_surveillances.delete("1.0", tk.END)
            if not self.surveillances:
                self.liste_surveillances.insert(tk.END,
                    "Ajoutez une expression UVI\n(ex. x * 2) pour la suivre.\n", "vide")
            for expression in self.surveillances:
                self.liste_surveillances.insert(tk.END, "%-18s " % expression[:18], "expr")
                try:
                    # evaluate() = l'évaluateur réel du langage. Une
                    # expression invalide lève ; on l'affiche comme erreur
                    # au lieu de laisser tomber la surveillance entière.
                    valeur = self.engine.evaluate(expression)
                    self.liste_surveillances.insert(tk.END, "%s\n" % str(valeur)[:22], "val")
                except Exception as e:
                    self.liste_surveillances.insert(
                        tk.END, "%s\n" % type(e).__name__, "err")
            self.liste_surveillances.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ==================================================================
    # 4. OUTILS
    # ==================================================================
    def _build_panneau_outils(self):
        """Donne un vrai domicile aux boutons orphelins de la v9.

        AST, BYTECODE, OPTIMISER, ANALYSER, STATS, AUTO-RÉPARER et VOIX
        existaient encore comme widgets jamais empaquetés -- attributs
        valides, aucun moyen de les déclencher. Ils sont ici, groupés par
        intention et avec une phrase disant à quoi ils servent."""
        panneau = self._nouveau_panneau("outils")
        interieur = self._cadre_defilant(panneau)

        groupes = (
            ("COMPRENDRE LE LANGAGE", T.VIOLET, (
                ("🌳", "Arbre syntaxique (AST)",
                 "Comment le parser a compris votre code",
                 lambda: self.agent.export_ast() if self.agent else None),
                ("⚙", "Bytecode compilé",
                 "Les instructions exécutées par la VM",
                 lambda: self.agent.export_bytecode() if self.agent else None),
                ("✨", "Optimiser le bytecode",
                 "Constantes pré-calculées, code mort retiré",
                 lambda: self.agent.optimize_code() if self.agent else None),
            )),
            ("ÉCRIRE AU QUOTIDIEN", T.CYAN, (
                ("🔍", "Analyser le script",
                 "Défauts probables sans exécuter",
                 lambda: self.agent.analyze_editor_code() if self.agent else None),
                ("🛠", "Auto-réparer",
                 "Corrections déterministes, hors ligne",
                 lambda: self.agent.auto_repair() if self.agent else None),
                ("🤖", "Auto-corriger (agent IA)",
                 "Boucle autonome — expérimental",
                 self.autonomous_fix_from_editor),
            )),
            ("SESSION", T.AMBRE, (
                ("📊", "Statistiques",
                 "Compteurs mémoire, appels, erreurs",
                 lambda: self.agent.report_stats() if self.agent else None),
                ("🔊", "Synthèse vocale",
                 "Activer ou couper la voix de l'agent",
                 self.toggle_voice),
                ("🧹", "Vider la console",
                 "Repartir d'une console propre",
                 self._clear_console),
            )),
        )
        for titre, couleur, outils in groupes:
            contenu = self._carte(interieur, titre, accent=couleur, menu=False)
            contenu.master.pack(fill=tk.X, pady=(0, 8))
            for icone, libelle, description, commande in outils:
                ligne = tk.Frame(contenu, bg=T.CHAMP, cursor="hand2",
                                 highlightthickness=1, highlightbackground=T.BORDURE)
                ligne.pack(fill=tk.X, padx=10, pady=3)
                haut = tk.Frame(ligne, bg=T.CHAMP)
                haut.pack(fill=tk.X, padx=9, pady=(7, 0))
                lbl_icone = tk.Label(haut, text=icone, bg=T.CHAMP, fg=couleur,
                                     font=("Segoe UI", 11))
                lbl_icone.pack(side=tk.LEFT)
                lbl_titre = tk.Label(haut, text=libelle, bg=T.CHAMP, fg=T.TEXTE,
                                     font=("Segoe UI", 8, "bold"))
                lbl_titre.pack(side=tk.LEFT, padx=(8, 0))
                lbl_desc = tk.Label(ligne, text=description, bg=T.CHAMP,
                                    fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                                    anchor=tk.W)
                lbl_desc.pack(fill=tk.X, padx=(31, 9), pady=(0, 7))
                for widget in (ligne, haut, lbl_icone, lbl_titre, lbl_desc):
                    widget.bind("<Button-1>", lambda e, c=commande: c())
                    widget.bind("<Enter>", lambda e, w=ligne: w.config(
                        highlightbackground=T.BORDURE_VIVE))
                    widget.bind("<Leave>", lambda e, w=ligne: w.config(
                        highlightbackground=T.BORDURE))

    # ==================================================================
    # 5. MODULES & PAQUETS
    # ==================================================================
    def _build_panneau_modules(self):
        """Modules chargés, bibliothèque standard, paquets publiés."""
        panneau = self._nouveau_panneau("modules")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "ÉTAT DES MODULES", menu=False)
        contenu.master.pack(fill=tk.X)
        self.arbre_modules = tk.Text(
            contenu, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=8, height=18)
        self.arbre_modules.pack(fill=tk.BOTH, expand=True)
        for etiquette, couleur in (("groupe", T.CYAN), ("membre", T.TEXTE),
                                   ("detail", T.TEXTE_FAIBLE), ("vide", T.TEXTE_FAIBLE)):
            self.arbre_modules.tag_configure(etiquette, foreground=couleur)

        actions = self._carte(interieur, "ACTIONS", menu=False)
        actions.master.pack(fill=tk.X, pady=(8, 8))
        for icone, libelle, commande in (
            ("🔄", "Actualiser", self.rafraichir_modules),
            ("📚", "Réinstaller la bibliothèque standard", self.reinstaller_stdlib),
            ("📤", "Publier le script comme paquet", self.publier_paquet),
        ):
            self._bouton_plat(actions, libelle, commande, couleur=T.TEXTE,
                              icone=icone, gras=False,
                              police=("Segoe UI", 8)).pack(fill=tk.X, padx=10, pady=3)
        tk.Label(actions, text="Un paquet publié devient importable par\n"
                               "IMPORT \"pkg:nom\" depuis n'importe quel script.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                 justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(4, 10))

    def rafraichir_modules(self):
        if not hasattr(self, "arbre_modules"):
            return
        try:
            self.arbre_modules.config(state=tk.NORMAL)
            self.arbre_modules.delete("1.0", tk.END)

            charges = sorted(getattr(self.engine, "modules", {}) or {})
            self.arbre_modules.insert(tk.END, "▾ CHARGÉS EN MÉMOIRE\n", "groupe")
            if not charges:
                self.arbre_modules.insert(
                    tk.END, "    aucun — utilisez MODULE \"f.uvi\" -> alias\n", "vide")
            for alias in charges:
                self.arbre_modules.insert(tk.END, "    %s\n" % alias, "membre")

            registre = getattr(self.vfs, "package_registry", None)
            self.arbre_modules.insert(tk.END, "\n▾ BIBLIOTHÈQUE STANDARD\n", "groupe")
            if registre is None:
                self.arbre_modules.insert(tk.END, "    registre non attaché\n", "vide")
            else:
                trouves = 0
                for nom in sorted(STDLIB_SOURCES):
                    try:
                        chemin = registre.resolve_std(nom)
                    except UVIRuntimeTrap:
                        chemin = None
                    if chemin:
                        trouves += 1
                        self.arbre_modules.insert(tk.END, "    %s\n" % nom, "membre")
                        self.arbre_modules.insert(
                            tk.END, '        MODULE "std:%s" -> %s\n' % (nom, nom), "detail")
                if not trouves:
                    self.arbre_modules.insert(
                        tk.END, "    non installée — voir Actions\n", "vide")

            self.arbre_modules.insert(tk.END, "\n▾ PAQUETS PUBLIÉS\n", "groupe")
            paquets = {}
            if registre is not None:
                try:
                    paquets = registre._load_registry() or {}
                except Exception:
                    paquets = {}
            if not paquets:
                self.arbre_modules.insert(tk.END, "    aucun paquet publié\n", "vide")
            for nom, versions in sorted(paquets.items()):
                self.arbre_modules.insert(tk.END, "    %s\n" % nom, "membre")
                for version in sorted(versions):
                    self.arbre_modules.insert(
                        tk.END, "        v%s\n" % version, "detail")

            self.arbre_modules.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def reinstaller_stdlib(self):
        registre = getattr(self.vfs, "package_registry", None)
        if registre is None:
            self.log("[PACKAGE_UNAVAILABLE] Registre de paquets non attaché.", "err")
            return
        try:
            registre.ensure_stdlib_installed(STDLIB_SOURCES)
        except Exception as e:
            self.log("Échec d'installation : %s" % e, "err")
            return
        self.rafraichir_modules()
        self._set_status("Bibliothèque standard réinstallée.")
        self.log("Bibliothèque standard réinstallée (%d module(s))."
                 % len(STDLIB_SOURCES), "ok")

    def publier_paquet(self):
        """Publie le contenu de l'éditeur comme paquet versionné.

        Passe par un fichier du VFS puis par registry.publish(), donc par
        _safe_resolve : un nom de paquet contenant « .. » est refusé ici
        exactement comme il l'est depuis PACKAGE.PUBLISH en script."""
        registre = getattr(self.vfs, "package_registry", None)
        if registre is None:
            self.log("[PACKAGE_UNAVAILABLE] Registre de paquets non attaché.", "err")
            return
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.log("Éditeur vide : rien à publier.", "err")
            return
        nom = self._demander_texte("Publier un paquet", "Nom du paquet :")
        if not nom:
            return
        version = self._demander_texte("Publier un paquet", "Version :", "1.0")
        if not version:
            return
        # Nom de fichier temporaire FIXE : y injecter `nom` faisait
        # remonter un FileNotFoundError illisible pour un nom hostile
        # (« ../evasion » donnait un répertoire « _publication_.. »
        # inexistant), au lieu du PATH_TRAVERSAL clair que publish() sait
        # produire. Le nom de paquet ne sert qu'à publish(), qui le confine.
        temporaire = "_publication_en_cours.uvi"
        try:
            self.vfs.write_file(temporaire, code)
            registre.publish(nom, self.vfs.get_path(temporaire), version)
            self.vfs.delete_file(temporaire)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        except Exception as e:
            self.log("Échec de publication : %s" % e, "err")
            return
        self.rafraichir_modules()
        self.update_vfs_list()
        self.log('Paquet publié : %s v%s — importable par IMPORT "pkg:%s"'
                 % (nom, version, nom), "ok")

    # ==================================================================
    # 6. TESTS
    # ==================================================================
    def _build_panneau_tests(self):
        """Vérifications exécutables depuis l'IDE.

        Ce ne sont pas les tests unitaires de tests/ (ceux-là tournent sous
        pytest, hors interface). Ce sont les propriétés qu'on veut pouvoir
        vérifier SUR SON PROPRE SCRIPT, en un clic, sans quitter l'IDE."""
        panneau = self._nouveau_panneau("tests")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "VÉRIFICATIONS", menu=False)
        contenu.master.pack(fill=tk.X)
        for icone, libelle, description, commande in (
            ("⚖", "Comparer les deux moteurs",
             "Le tree-walker et la VM doivent être d'accord",
             self._lancer_tests),
            ("🛡", "Tester les garde-fous",
             "Boucle infinie, récursion, mémoire",
             self.tester_garde_fous),
            ("📐", "Vérifier la syntaxe",
             "Analyse sans exécuter",
             self.verifier_syntaxe),
        ):
            ligne = tk.Frame(contenu, bg=T.CHAMP, cursor="hand2",
                             highlightthickness=1, highlightbackground=T.BORDURE)
            ligne.pack(fill=tk.X, padx=10, pady=3)
            haut = tk.Frame(ligne, bg=T.CHAMP)
            haut.pack(fill=tk.X, padx=9, pady=(7, 0))
            a = tk.Label(haut, text=icone, bg=T.CHAMP, fg=T.CYAN, font=("Segoe UI", 11))
            a.pack(side=tk.LEFT)
            b = tk.Label(haut, text=libelle, bg=T.CHAMP, fg=T.TEXTE,
                         font=("Segoe UI", 8, "bold"))
            b.pack(side=tk.LEFT, padx=(8, 0))
            c = tk.Label(ligne, text=description, bg=T.CHAMP, fg=T.TEXTE_FAIBLE,
                         font=("Segoe UI", 7), anchor=tk.W)
            c.pack(fill=tk.X, padx=(31, 9), pady=(0, 7))
            for widget in (ligne, haut, a, b, c):
                widget.bind("<Button-1>", lambda e, cmd=commande: cmd())

        contenu_res = self._carte(interieur, "RÉSULTATS", menu=False)
        contenu_res.master.pack(fill=tk.X, pady=(8, 8))
        self.resultats_tests = tk.Text(
            contenu_res, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.WORD, padx=10, pady=8, height=16)
        self.resultats_tests.pack(fill=tk.BOTH, expand=True)
        self.resultats_tests.tag_configure("ok", foreground=T.VERT)
        self.resultats_tests.tag_configure("ko", foreground=T.ROUGE)
        self.resultats_tests.tag_configure("titre", foreground=T.CYAN)
        self.resultats_tests.tag_configure("neutre", foreground=T.TEXTE_FAIBLE)

    def _ecrire_resultat_test(self, texte, etiquette="neutre", vider=False):
        if not hasattr(self, "resultats_tests"):
            return
        try:
            self.resultats_tests.config(state=tk.NORMAL)
            if vider:
                self.resultats_tests.delete("1.0", tk.END)
            self.resultats_tests.insert(tk.END, texte + "\n", etiquette)
            self.resultats_tests.config(state=tk.DISABLED)
            self.resultats_tests.see(tk.END)
        except tk.TclError:
            pass

    def tester_garde_fous(self):
        """Déclenche chaque garde-fou sur un script dégénéré et vérifie que
        l'erreur ATTENDUE sort -- et surtout que la VM reste utilisable
        après. Un garde-fou qui protège en laissant un état corrompu ne
        protège pas."""
        self.afficher_panneau_gauche("tests")
        self._ecrire_resultat_test("GARDE-FOUS", "titre", vider=True)
        cas = (
            ("Boucle infinie (RUNAWAY)",
             ["i = 0", "%% i < 100000 -> i = i + 1"], {"max_steps": 500}, "RUNAWAY"),
            ("Récursion infinie (MAX_RECURSION)",
             ["DEF F(n) -> CALL F(n)", "CALL F(1)"],
             {"max_call_depth": 10, "max_steps": 50000}, "MAX_RECURSION"),
            ("Tableau démesuré (ARRAY_FULL)",
             ["TAB.CREATE t", "% 5000 -> TAB.PUSH t -> 1"],
             {"max_array_size": 50}, "ARRAY_FULL"),
        )
        reussis = 0
        for libelle, script, options, attendu in cas:
            try:
                resultat = compile_and_run(list(script), self.engine.parser,
                                           UVIBytecodeCompilerV2(), **options)
                obtenu = resultat.trapped
            except Exception as e:
                obtenu = "exception %s" % type(e).__name__
            if obtenu == attendu:
                self._ecrire_resultat_test("  ✓ %s" % libelle, "ok")
                reussis += 1
            else:
                self._ecrire_resultat_test(
                    "  ✗ %s — attendu %s, obtenu %s" % (libelle, attendu, obtenu), "ko")

        try:
            sain = compile_and_run(['!"vivant"'], self.engine.parser, UVIBytecodeCompilerV2())
            survit = sain.trapped is None
        except Exception:
            survit = False
        if survit:
            self._ecrire_resultat_test("  ✓ La VM reste utilisable après", "ok")
            reussis += 1
        else:
            self._ecrire_resultat_test("  ✗ La VM est inutilisable après", "ko")
        self._ecrire_resultat_test("\n%d/%d vérifications passées." % (reussis, len(cas) + 1),
                                   "ok" if reussis == len(cas) + 1 else "ko")

    def verifier_syntaxe(self):
        """Analyse le script sans l'exécuter : signale les lignes que le
        parser ne comprend pas (UnknownNode)."""
        self.afficher_panneau_gauche("tests")
        self._ecrire_resultat_test("VÉRIFICATION DE SYNTAXE", "titre", vider=True)
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self._ecrire_resultat_test("  Éditeur vide.", "neutre")
            return
        problemes = 0
        for numero, ligne in enumerate(code.split("\n"), start=1):
            depouillee = ligne.strip()
            if not depouillee or depouillee.startswith("#") or depouillee == "@UVI":
                continue
            try:
                noeud = self.engine.parser.parse_line(depouillee, numero)
            except Exception as e:
                self._ecrire_resultat_test(
                    "  ✗ L%-4d %s" % (numero, type(e).__name__), "ko")
                problemes += 1
                continue
            if isinstance(noeud, UnknownNode):
                self._ecrire_resultat_test(
                    "  ✗ L%-4d non comprise : %s" % (numero, depouillee[:30]), "ko")
                problemes += 1
        if problemes:
            self._ecrire_resultat_test(
                "\n%d ligne(s) problématique(s)." % problemes, "ko")
        else:
            self._ecrire_resultat_test("  ✓ Toutes les lignes sont comprises.", "ok")

    # ==================================================================
    # 7. PARAMÈTRES
    # ==================================================================
    def _build_panneau_parametres(self):
        """Paramètres MODIFIABLES, pas seulement affichés.

        Le panneau v9 se contentait de déverser les valeurs dans la console.
        Ici, changer l'adresse du serveur ou le nom du modèle et cliquer
        « Appliquer » RECONSTRUIT réellement le client SHION -- utile au
        premier branchement, où l'on tâtonne sur le nom exact du modèle
        servi par vLLM (l'erreur 404 la plus courante). Sans ça, chaque
        essai demandait de fermer l'IDE et de relancer avec une autre
        variable d'environnement."""
        panneau = self._nouveau_panneau("parametres")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "SERVEUR SHION", menu=False)
        contenu.master.pack(fill=tk.X)
        self.champs_parametres = {}
        for cle, libelle, valeur, aide in (
            ("url", "Adresse du serveur", SHION_SERVER_URL, "ex. http://192.168.1.50:8000"),
            ("modele", "Nom du modèle", DEEPSEEK_MODEL_NAME, "doit correspondre à /v1/models"),
            ("cle", "Clé d'API", DEEPSEEK_API_KEY, "ignorée si le serveur n'en exige pas"),
            ("temperature", "Température", str(DEEPSEEK_TEMPERATURE), "0.5 à 0.7 — jamais 0"),
            ("max_tokens", "Budget de tokens", str(DEEPSEEK_MAX_TOKENS), "≥ 4096 conseillé"),
        ):
            tk.Label(contenu, text=libelle, bg=T.PANNEAU, fg=T.TEXTE,
                     font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=12, pady=(8, 2))
            champ = tk.Entry(contenu, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
                             relief=tk.FLAT, font=("Consolas", 9),
                             highlightthickness=1, highlightbackground=T.BORDURE,
                             highlightcolor=T.CYAN)
            champ.pack(fill=tk.X, padx=12, ipady=4)
            champ.insert(0, str(valeur))
            self.champs_parametres[cle] = champ
            tk.Label(contenu, text=aide, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 7)).pack(anchor=tk.W, padx=12, pady=(2, 0))

        barre = tk.Frame(contenu, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=12, pady=12)
        self._bouton_plat(barre, "Appliquer", self.appliquer_parametres,
                          couleur=T.VERT, icone="✓").pack(side=tk.LEFT)
        self._bouton_plat(barre, "Tester", self.tester_connexion_shion,
                          couleur=T.CYAN, icone="🔌").pack(side=tk.LEFT, padx=6)

        contenu_etat = self._carte(interieur, "ÉTAT DU SYSTÈME", menu=False)
        contenu_etat.master.pack(fill=tk.X, pady=(8, 8))
        self.table_parametres = tk.Text(
            contenu_etat, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=8, height=12)
        self.table_parametres.pack(fill=tk.BOTH, expand=True)
        self.table_parametres.tag_configure("cle", foreground=T.TEXTE_FAIBLE)
        self.table_parametres.tag_configure("oui", foreground=T.VERT)
        self.table_parametres.tag_configure("non", foreground=T.ROUGE)
        self.table_parametres.tag_configure("val", foreground=T.TEXTE)

    def rafraichir_parametres(self):
        if not hasattr(self, "table_parametres"):
            return
        try:
            self.table_parametres.config(state=tk.NORMAL)
            self.table_parametres.delete("1.0", tk.END)
            client = self.engine.shion_client
            lignes = (
                ("Backend SHION", SHION_BACKEND, "val"),
                ("Client actif", type(client).__name__ if client else "aucun",
                 "oui" if client else "non"),
                ("SHION", "disponible" if (HAS_SHION and client) else "indisponible",
                 "oui" if (HAS_SHION and client) else "non"),
                ("VM bytecode", "disponible" if HAS_VM else "indisponible",
                 "oui" if HAS_VM else "non"),
                ("Stockage", getattr(self.vfs, "storage_dir", "mémoire"), "val"),
                ("Mémoire max", "%d octets" % MAX_MEMORY_BYTES, "val"),
                ("Chaîne max", "%d caractères" % MAX_STRING_LENGTH, "val"),
            )
            for cle, valeur, etiquette in lignes:
                self.table_parametres.insert(tk.END, "%-16s " % cle, "cle")
                self.table_parametres.insert(tk.END, "%s\n" % valeur, etiquette)
            self.table_parametres.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def appliquer_parametres(self):
        """Reconstruit le client SHION avec les valeurs saisies.

        On remplace l'INSTANCE, pas les constantes du module : les
        constantes sont lues au chargement par d'autres chemins, et les
        réécrire donnerait un état incohérent entre ce que le client fait et
        ce que les messages d'erreur annoncent."""
        champs = self.champs_parametres
        try:
            temperature = float(champs["temperature"].get().strip())
            max_tokens = int(champs["max_tokens"].get().strip())
        except ValueError:
            self.log("Température et budget doivent être numériques.", "err")
            return
        if temperature < 0.4:
            # Refus assumé : c'est le mode de défaillance documenté n°1 des
            # distillations R1. Laisser passer 0.0 produirait des réponses
            # en boucle que l'utilisateur attribuerait au serveur.
            self.log("Température < 0.4 refusée : DeepSeek-R1 part en "
                     "répétitions sans fin. Utilisez 0.5 à 0.7.", "err")
            return
        try:
            client = DeepSeekClient(
                base_url=champs["url"].get().strip(),
                model_name=champs["modele"].get().strip(),
                api_key=champs["cle"].get().strip() or "uvi-local",
                # BUGFIX : température et budget de tokens étaient calculés
                # et VALIDÉS ci-dessus, puis jamais transmis au client
                # reconstruit (DeepSeekClient() ignorait ces deux champs à
                # la construction) -- cliquer "Appliquer" changeait donc
                # silencieusement l'URL/le modèle/la clé mais laissait
                # tourner l'ancienne température et l'ancien budget, sans
                # le moindre message pour le signaler. Voir aussi
                # DeepSeekClient.__init__, qui accepte désormais ces deux
                # valeurs par instance au lieu de lire uniquement les
                # constantes de module.
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            self.log("Client non construit : %s" % e, "err")
            return
        self.engine.shion_client = client
        self.rafraichir_parametres()
        self._set_status("Paramètres appliqués.")
        self.log("Client SHION reconstruit → %s (modèle %s, température %.2f, budget %d)"
                 % (client.base_url, client.model_name, client.temperature, client.max_tokens), "ok")

    def tester_connexion_shion(self):
        """Interroge /v1/models du serveur configuré, dans un thread daemon.

        Rend le NOM EXACT servi : c'est précisément l'information dont on a
        besoin pour corriger un HTTP 404, et la faire remonter ici évite
        d'aller la chercher au curl sur une autre machine."""
        url = self.champs_parametres["url"].get().strip().rstrip("/")
        cle = self.champs_parametres["cle"].get().strip() or "uvi-local"
        self.show_terminal()
        self.log("Test de connexion vers %s ..." % url, "sys")

        def _worker():
            try:
                requete = urllib.request.Request("%s/v1/models" % url)
                requete.add_header("Authorization", "Bearer %s" % cle)
                with urllib.request.urlopen(requete, timeout=15) as reponse:
                    corps = json.loads(reponse.read().decode("utf-8"))
                noms = [m.get("id") for m in corps.get("data", [])]
                message, niveau = ("Serveur joignable. Modèle(s) servi(s) : %s"
                                   % (", ".join(n for n in noms if n) or "aucun"), "ok")
            except urllib.error.HTTPError as e:
                message, niveau = ("Serveur joignable mais refuse (HTTP %d). "
                                   "Vérifiez la clé d'API." % e.code, "err")
            except Exception as e:
                message, niveau = ("Serveur injoignable : %s" % e, "err")
            self._poster_ui(lambda: self.log(message, niveau))

        threading.Thread(target=_worker, daemon=True).start()

    def rafraichir_variables(self):
        """Table des variables en direct. Appelée après chaque exécution.

        Type deviné pour la couleur uniquement : UVI ne typant pas ses
        variables, c'est un indice visuel, pas une vérité du langage --
        d'où le fait qu'aucune logique d'exécution ne s'appuie dessus."""
        if not hasattr(self, "table_variables"):
            return
        try:
            self.table_variables.config(state=tk.NORMAL)
            self.table_variables.delete("1.0", tk.END)
            memoire = getattr(self.engine, "memoire", {}) or {}
            visibles = [(n, v) for n, v in sorted(memoire.items())
                        if not n.startswith("_")]
            if not visibles:
                self.table_variables.insert(tk.END,
                    "Aucune variable.\nExécutez un script pour les voir ici.\n", "vide")
            for nom, valeur in visibles[:60]:
                texte = str(valeur)
                if len(texte) > 26:
                    texte = texte[:23] + "..."
                if isinstance(valeur, list):
                    etiquette = "tab"
                elif isinstance(valeur, bool) or texte in ("VRAI", "FAUX"):
                    etiquette = "bool"
                elif isinstance(valeur, (int, float)):
                    etiquette = "num"
                else:
                    etiquette = "txt"
                    texte = '"%s"' % texte
                self.table_variables.insert(tk.END, "%-14s " % nom[:14], "nom")
                self.table_variables.insert(tk.END, texte + "\n", etiquette)
            self.table_variables.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Barre d'onglets de l'éditeur et barre d'état
    # ------------------------------------------------------------------
    def _build_barre_onglets(self, parent):
        barre = tk.Frame(parent, bg=T.PANNEAU_HAUT, height=38)
        barre.pack(fill=tk.X)
        barre.pack_propagate(False)
        self.onglet_fichier = tk.Frame(barre, bg=T.PANNEAU,
                                       highlightthickness=1,
                                       highlightbackground=T.BORDURE)
        self.onglet_fichier.pack(side=tk.LEFT, padx=(8, 0), pady=4)
        tk.Label(self.onglet_fichier, text="U", bg=T.PANNEAU, fg=T.CYAN,
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(9, 5))
        self.etiquette_fichier = tk.Label(
            self.onglet_fichier, text="main.uvi", bg=T.PANNEAU,
            fg=T.TEXTE_FORT, font=("Segoe UI", 9))
        self.etiquette_fichier.pack(side=tk.LEFT, pady=6)
        tk.Label(self.onglet_fichier, text="✕", bg=T.PANNEAU,
                 fg=T.TEXTE_FAIBLE, font=("Segoe UI", 8),
                 cursor="hand2").pack(side=tk.LEFT, padx=(9, 9))

        # Les vues secondaires (canevas, SHION) deviennent des onglets à
        # côté du fichier : en v8 elles étaient des boutons perdus au
        # milieu de la barre d'outils supérieure, sans rien indiquer qu'il
        # s'agissait de VUES et non d'actions.
        for cle, libelle, commande in (
            ("editor", "◧ ÉDITEUR", self.show_editor),
            ("canvas", "▦ CANEVAS", self.show_canvas),
            ("shion", "🧠 SHION", self.show_shion),
        ):
            bouton = tk.Button(
                barre, text=libelle, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                activebackground=T.SURVOL, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 8), cursor="hand2",
                command=commande, padx=11, pady=5,
            )
            bouton.pack(side=tk.LEFT, padx=2, pady=4)
            setattr(self, "btn_%s" % {"editor": "edit", "canvas": "canvas",
                                      "shion": "shion"}[cle], bouton)
        # btn_term référencé ailleurs : conservé, redirigé vers la console
        # du dock (qui n'est plus une vue mais reste focalisable).
        self.btn_term = tk.Button(barre, text="TERMINAL", command=self.show_terminal)

    def _build_barre_etat_editeur(self, parent):
        barre = tk.Frame(parent, bg=T.PANNEAU_HAUT, height=26)
        barre.pack(fill=tk.X, side=tk.BOTTOM)
        barre.pack_propagate(False)
        self.etat_curseur = tk.Label(barre, text="L1  COL 1", bg=T.PANNEAU_HAUT,
                                     fg=T.TEXTE_FAIBLE, font=("Consolas", 8))
        self.etat_curseur.pack(side=tk.LEFT, padx=14)
        for texte in ("ESPACES: 4", "UTF-8", "LF", "UVI"):
            tk.Label(barre, text=texte, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                     font=("Consolas", 8)).pack(side=tk.LEFT, padx=11)
        tk.Label(barre, text="UVi", bg=T.PANNEAU_HAUT, fg=T.BORDURE_VIVE,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.RIGHT, padx=14)

    def _rafraichir_position_curseur(self, _event=None):
        try:
            ligne, colonne = self.editor.index(tk.INSERT).split(".")
            self.etat_curseur.config(text="L%s  COL %d" % (ligne, int(colonne) + 1))
        except (tk.TclError, AttributeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Panneau mémoire VM
    # ------------------------------------------------------------------
    def _build_panneau_memoire(self, parent):
        contenu = self._carte(parent, "MÉMOIRE VM", accent=T.CYAN)
        contenu.master.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        contenu.master.configure(width=352)
        contenu.master.pack_propagate(False)

        corps = tk.Frame(contenu, bg=T.PANNEAU)
        corps.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.anneau_memoire = tk.Canvas(corps, width=132, height=132,
                                        bg=T.PANNEAU, highlightthickness=0)
        self.anneau_memoire.pack(side=tk.LEFT, padx=(0, 12))

        self.legende_memoire = tk.Frame(corps, bg=T.PANNEAU)
        self.legende_memoire.pack(side=tk.LEFT, fill=tk.Y, pady=8)

        self.courbe_memoire = tk.Canvas(contenu, height=42, bg=T.PANNEAU,
                                        highlightthickness=0)
        self.courbe_memoire.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.etiquette_memoire = tk.Label(contenu, text="0% UTILISÉ", bg=T.PANNEAU,
                                          fg=T.CYAN, font=("Segoe UI", 8, "bold"))
        self.etiquette_memoire.pack(anchor=tk.W, padx=14, pady=(0, 8))
        self._historique_memoire = []
        self.rafraichir_memoire()

    def rafraichir_memoire(self):
        """Anneau + histogramme de la mémoire du moteur.

        create_arc en style ARC avec un trait épais : Tkinter n'a pas de
        graphique en anneau, mais un arc épais SANS remplissage en est
        exactement un. Inutile d'ajouter matplotlib (dépendance lourde) pour
        dessiner quatre arcs -- cohérent avec le refus de torch côté client
        et de tkinter-tooltip pour les infobulles.

        Les quatre catégories sont des ESTIMATIONS dérivées de
        current_memory_usage, pas une instrumentation de l'allocateur : le
        moteur ne comptabilise que le total. Elles donnent un ordre de
        grandeur utile, pas une mesure exacte -- dit ici pour que personne
        ne construise dessus en croyant à une métrique fine."""
        if not hasattr(self, "anneau_memoire"):
            return
        try:
            self.anneau_memoire.delete("all")
            self.legende_memoire.pack_forget()
            self.legende_memoire = tk.Frame(self.legende_memoire.master, bg=T.PANNEAU)
            self.legende_memoire.pack(side=tk.LEFT, fill=tk.Y, pady=8)

            utilise = float(getattr(self.engine, "current_memory_usage", 0) or 0)
            total = float(MAX_MEMORY_BYTES)
            fraction = max(0.0, min(1.0, utilise / total if total else 0.0))

            memoire = getattr(self.engine, "memoire", {}) or {}
            poids_tab = sum(len(str(v)) for v in memoire.values() if isinstance(v, list))
            poids_var = max(0.0, utilise - poids_tab)
            poids_code = len(self.editor.get("1.0", tk.END)) if hasattr(self, "editor") else 0

            segments = [
                ("Variables", poids_var, T.BLEU_VIF),
                ("Structures", float(poids_tab), T.CYAN),
                ("Code", float(poids_code), T.VIOLET),
                ("Libre", max(0.0, total - utilise - poids_code), T.BORDURE),
            ]
            somme = sum(v for _, v, _ in segments) or 1.0

            # Plancher de 3° pour tout segment NON NUL. Sans lui, un script
            # ordinaire (quelques dizaines d'octets sur 500 Ko de quota)
            # produit un anneau d'UNE SEULE couleur : « Libre » écrase tout
            # et les trois autres catégories tombent sous le dixième de
            # degré, donc invisibles. L'utilisateur voit alors un anneau
            # uni et en conclut que le panneau est cassé.
            #
            # DISTORSION ASSUMÉE, et c'est le genre de choix qu'il faut
            # écrire plutôt que subir : aux très faibles taux d'occupation,
            # l'anneau n'est PAS à lire comme une proportion. Il dit « ces
            # catégories existent et voici leur ordre », pas « voici leurs
            # parts exactes ». Les valeurs exactes sont dans la légende à
            # côté, en kilo-octets, sans aucune distorsion -- c'est là qu'il
            # faut regarder pour un chiffre.
            debut = 90.0
            visibles = [(v, c) for _, v, c in segments if v > 0]
            plancher = 3.0
            reste = 360.0 - plancher * len(visibles)
            for valeur, couleur in visibles:
                etendue = -(plancher + reste * (valeur / somme))
                self.anneau_memoire.create_arc(
                    12, 12, 120, 120, start=debut, extent=etendue,
                    style=tk.ARC, outline=couleur, width=15)
                debut += etendue
            self.anneau_memoire.create_text(
                66, 60, text="%.1f Ko" % (utilise / 1024.0),
                fill=T.TEXTE_FORT, font=("Segoe UI", 13, "bold"))
            self.anneau_memoire.create_text(
                66, 80, text="sur %d Ko" % int(total / 1024),
                fill=T.TEXTE_FAIBLE, font=("Segoe UI", 7))

            for libelle, valeur, couleur in segments:
                ligne = tk.Frame(self.legende_memoire, bg=T.PANNEAU)
                ligne.pack(fill=tk.X, pady=3)
                pastille = tk.Canvas(ligne, width=9, height=9, bg=T.PANNEAU,
                                     highlightthickness=0)
                pastille.pack(side=tk.LEFT)
                pastille.create_rectangle(0, 0, 9, 9, fill=couleur, outline="")
                tk.Label(ligne, text=libelle, bg=T.PANNEAU, fg=T.TEXTE,
                         font=("Segoe UI", 8), width=11,
                         anchor=tk.W).pack(side=tk.LEFT, padx=(7, 0))
                tk.Label(ligne, text="%.1f Ko" % (valeur / 1024.0), bg=T.PANNEAU,
                         fg=T.TEXTE_FAIBLE, font=("Consolas", 8)).pack(side=tk.RIGHT)

            self._historique_memoire.append(fraction)
            self._historique_memoire = self._historique_memoire[-70:]
            self.courbe_memoire.delete("all")
            largeur = max(self.courbe_memoire.winfo_width(), 260)
            points = self._historique_memoire
            if len(points) > 1:
                pas = largeur / float(len(points) - 1)
                coords = []
                for i, valeur in enumerate(points):
                    coords += [i * pas, 40 - valeur * 34]
                self.courbe_memoire.create_line(*coords, fill=T.CYAN,
                                                width=1, smooth=True)
            self.etiquette_memoire.config(text="%d%% UTILISÉ" % int(fraction * 100))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Colonne SHION
    # ------------------------------------------------------------------
    def _build_colonne_shion(self):
        colonne = tk.Frame(self.container, bg=T.FOND, width=372)
        colonne.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6), pady=6)
        colonne.pack_propagate(False)

        # --- panneau de discussion -----------------------------------------
        cadre = tk.Frame(colonne, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(fill=tk.BOTH, expand=True)
        entete = tk.Frame(cadre, bg=T.PANNEAU_HAUT, height=56)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        emblème = tk.Canvas(entete, width=30, height=30, bg=T.PANNEAU_HAUT,
                            highlightthickness=0)
        emblème.pack(side=tk.LEFT, padx=(12, 8), pady=13)
        emblème.create_oval(3, 3, 27, 27, outline=T.CYAN, width=2)
        emblème.create_oval(11, 11, 19, 19, fill=T.CYAN, outline="")
        colonne_titre = tk.Frame(entete, bg=T.PANNEAU_HAUT)
        colonne_titre.pack(side=tk.LEFT, pady=11)
        tk.Label(colonne_titre, text="SHION", bg=T.PANNEAU_HAUT, fg=T.CYAN,
                 font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        connecte = HAS_SHION and self.engine.shion_client is not None
        tk.Label(colonne_titre,
                 text="VOTRE ASSISTANT IA INTÉGRÉ" if connecte
                      else "NON CONFIGURÉ — VOIR PARAMÈTRES",
                 bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 6)).pack(anchor=tk.W)

        self.shion_historique = tk.Text(
            cadre, bg=T.PANNEAU, fg=T.TEXTE, font=("Segoe UI", 9),
            relief=tk.FLAT, wrap=tk.WORD, state=tk.DISABLED,
            highlightthickness=0, padx=12, pady=10,
        )
        self.shion_historique.pack(fill=tk.BOTH, expand=True)
        self.shion_historique.tag_configure("vous", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("assistant", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("eris", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("corps", foreground=T.TEXTE)
        self.shion_historique.tag_configure("erreur", foreground=T.ROUGE)

        saisie = tk.Frame(cadre, bg=T.PANNEAU)
        saisie.pack(fill=tk.X, padx=12, pady=10)
        self.shion_input = tk.Entry(
            saisie, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Segoe UI", 9),
            highlightthickness=1, highlightbackground=T.BORDURE,
            highlightcolor=T.CYAN,
        )
        self.shion_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7)
        self.shion_input.insert(0, "Pose ta question à SHION...")
        self.shion_input.bind("<Return>", lambda e: self._shion_send_message())
        self.shion_input.bind("<FocusIn>", self._vider_invite_shion)
        self.btn_shion_send = tk.Button(
            saisie, text="➤", bg=T.BLEU, fg="#FFFFFF",
            activebackground=T.BLEU_VIF, activeforeground="#FFFFFF",
            disabledforeground=T.TEXTE_FAIBLE,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 11, "bold"), cursor="hand2",
            command=self._shion_send_message, padx=13,
        )
        self.btn_shion_send.pack(side=tk.RIGHT, padx=(8, 0), ipady=4)
        self.frame_shion_panneau = cadre

        # --- quatre actions rapides ----------------------------------------
        # Elles ne font qu'assembler un prompt autour du code de l'éditeur.
        # C'est modeste, et c'est justement ce qui manquait : SHION savait
        # déjà répondre, mais il fallait rédiger la question à la main à
        # chaque fois pour lui parler de son propre code.
        grille = tk.Frame(colonne, bg=T.FOND)
        grille.pack(fill=tk.X, pady=(8, 0))
        actions = (
            ("🧭", "EXPLIQUER", "Du code", T.CYAN, "expliquer"),
            ("✨", "OPTIMISER", "Le code", T.AMBRE, "optimiser"),
            ("🐞", "DÉBOGGER", "Le code", T.ROSE, "deboguer"),
            ("🧬", "GÉNÉRER", "Du code", T.VERT, "generer"),
        )
        for i, (icone, titre, sous_titre, couleur, cle) in enumerate(actions):
            grille.columnconfigure(i, weight=1)
            carte = tk.Frame(grille, bg=T.PANNEAU, highlightthickness=1,
                             highlightbackground=T.BORDURE, cursor="hand2")
            carte.grid(row=0, column=i, sticky="nsew", padx=3)
            lbl_icone = tk.Label(carte, text=icone, bg=T.PANNEAU, fg=couleur,
                                 font=("Segoe UI", 15))
            lbl_icone.pack(pady=(10, 2))
            lbl_titre = tk.Label(carte, text=titre, bg=T.PANNEAU, fg=T.TEXTE,
                                 font=("Segoe UI", 7, "bold"))
            lbl_titre.pack()
            lbl_sous = tk.Label(carte, text=sous_titre, bg=T.PANNEAU,
                                fg=T.TEXTE_FAIBLE, font=("Segoe UI", 6))
            lbl_sous.pack(pady=(0, 10))
            for widget in (carte, lbl_icone, lbl_titre, lbl_sous):
                widget.bind("<Button-1>", lambda e, c=cle: self.action_shion(c))
                widget.bind("<Enter>", lambda e, w=carte: w.config(
                    highlightbackground=T.BORDURE_VIVE))
                widget.bind("<Leave>", lambda e, w=carte: w.config(
                    highlightbackground=T.BORDURE))

        # --- outils --------------------------------------------------------
        # Requalifiée : cette grille faisait doublon avec le panneau OUTILS
        # du rail depuis que celui-ci existe. Deux jeux d'actions parallèles
        # auraient divergé -- on en ajoute une ici, on oublie l'autre. Elle
        # devient donc un ACCÈS RAPIDE aux panneaux du rail : un seul endroit
        # définit ce que font les outils, deux endroits permettent d'y aller.
        contenu_outils = self._carte(colonne, "ACCÈS RAPIDE", hauteur=240)
        contenu_outils.master.pack(fill=tk.X, pady=(8, 0))
        self._entete_outils = contenu_outils.master.winfo_children()[0].winfo_children()[0]
        outils = (
            ("📁", "FICHIERS", lambda: self.afficher_panneau_gauche("explorateur")),
            ("🔍", "RECHERCHE", lambda: self.afficher_panneau_gauche("recherche")),
            ("🗄", "VARIABLES", lambda: self.afficher_panneau_gauche("variables")),
            ("🛠", "OUTILS", lambda: self.afficher_panneau_gauche("outils")),
            ("📦", "MODULES", lambda: self.afficher_panneau_gauche("modules")),
            ("🧪", "TESTS", lambda: self.afficher_panneau_gauche("tests")),
            ("🎯", "POINTS D'ARRÊT", lambda: self._selectionner_onglet_console("DÉBOGUEUR")),
            ("⚙", "PARAMÈTRES", lambda: self.afficher_panneau_gauche("parametres")),
        )
        grille_outils = tk.Frame(contenu_outils, bg=T.PANNEAU)
        grille_outils.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for i, (icone, libelle, commande) in enumerate(outils):
            ligne, colonne_i = divmod(i, 4)
            grille_outils.columnconfigure(colonne_i, weight=1)
            grille_outils.rowconfigure(ligne, weight=1)
            carte = tk.Frame(grille_outils, bg=T.CHAMP, highlightthickness=1,
                             highlightbackground=T.BORDURE, cursor="hand2")
            carte.grid(row=ligne, column=colonne_i, sticky="nsew", padx=3, pady=3)
            lbl_icone = tk.Label(carte, text=icone, bg=T.CHAMP, fg=T.CYAN,
                                 font=("Segoe UI", 14))
            lbl_icone.pack(pady=(11, 3))
            lbl_texte = tk.Label(carte, text=libelle, bg=T.CHAMP, fg=T.TEXTE_FAIBLE,
                                 font=("Segoe UI", 6, "bold"))
            lbl_texte.pack(pady=(0, 11))
            for widget in (carte, lbl_icone, lbl_texte):
                widget.bind("<Button-1>", lambda e, c=commande: c())
                widget.bind("<Enter>", lambda e, w=carte, t=lbl_texte:
                            (w.config(highlightbackground=T.CYAN),
                             t.config(fg=T.TEXTE)))
                widget.bind("<Leave>", lambda e, w=carte, t=lbl_texte:
                            (w.config(highlightbackground=T.BORDURE),
                             t.config(fg=T.TEXTE_FAIBLE)))

    def _vider_invite_shion(self, _event=None):
        if self.shion_input.get() == "Pose ta question à SHION...":
            self.shion_input.delete(0, tk.END)

    def _lister_points_arret(self):
        self.show_terminal()
        points = sorted(getattr(self, "_points_arret", set()))
        self.log("=== POINTS D'ARRÊT ===", "sys")
        if not points:
            self.log("Aucun. Placez le curseur sur une ligne et cliquez 🐞 DÉBOGUEUR.",
                     "info")
        for ligne in points:
            self.log("  ligne %d" % ligne, "info")

    def action_shion(self, action):
        """Quatre raccourcis qui envoient le code de l'éditeur à SHION.

        La consigne « réponds en français » n'est pas décorative : les
        distillations R1 de DeepSeek répondent volontiers en anglais ou en
        chinois quand la question ne fixe pas la langue, y compris quand le
        code commenté est en français."""
        code = self.editor.get("1.0", tk.END).strip()
        if action != "generer" and not code:
            self.show_terminal()
            self.log("Éditeur vide : écrivez d'abord un script.", "err")
            return
        gabarits = {
            "expliquer": "Explique ce code UVI en détail, en français, étape par "
                         "étape :\n\n%s" % code,
            "optimiser": "Propose une version optimisée de ce code UVI, en "
                         "français, en expliquant chaque changement :\n\n%s" % code,
            "deboguer": "Trouve les bugs de ce code UVI et propose une "
                        "correction, en français :\n\n%s" % code,
            "generer": "Génère un script UVI complet et commenté, en français. "
                       "Le langage UVI utilise : ! pour afficher, ? pour lire une "
                       "saisie, %% pour boucler, ~ pour une condition, DEF pour "
                       "définir une fonction, CALL pour l'appeler.",
        }
        self.show_shion()
        self.shion_input.delete(0, tk.END)
        self.shion_input.insert(0, gabarits[action])
        self._shion_send_message()

    def _build_editor(self):
        self.frame_editor = tk.Frame(self.workspace, bg=T.CHAMP)
        # self.line_numbers et self.editor gardent leurs noms : 8 et 53
        # références respectivement ailleurs dans le fichier
        # (_update_line_numbers, _highlight_error_line, la coloration
        # syntaxique, l'agent, run_script...). Seule leur peau change.
        self.line_numbers = tk.Text(
            self.frame_editor, width=5,
            bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=T.MONO_EDIT, relief=tk.FLAT,
            state=tk.DISABLED, cursor="arrow",
            highlightthickness=0, padx=6,
        )
        self.line_numbers.pack(side=tk.LEFT, fill=tk.Y)
        # Le clic sur un numéro de ligne pose un point d'arrêt. Le moteur de
        # points d'arrêt existait depuis longtemps mais n'avait AUCUN moyen
        # de déclenchement dans l'interface -- même situation
        # qu'autonomous_fix. La gouttière est l'endroit où tout le monde
        # cherche déjà à cliquer.
        self.line_numbers.bind("<Button-1>", self._clic_gouttiere)

        conteneur = tk.Frame(self.frame_editor, bg=T.CHAMP)
        conteneur.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.editor_vsb = tk.Scrollbar(conteneur, orient="vertical",
                                       bg=T.PANNEAU, troughcolor=T.CHAMP,
                                       relief=tk.FLAT, bd=0, width=10)
        self.editor_hsb = tk.Scrollbar(conteneur, orient="horizontal",
                                       bg=T.PANNEAU, troughcolor=T.CHAMP,
                                       relief=tk.FLAT, bd=0, width=10)
        self.editor = tk.Text(
            conteneur,
            bg=T.CHAMP, fg=T.TEXTE,
            insertbackground=T.CYAN, insertwidth=2,
            font=T.MONO_EDIT,
            relief=tk.FLAT, undo=True, tabs="4c",
            selectbackground="#1E3A5F", selectforeground=T.TEXTE_FORT,
            wrap=tk.NONE, highlightthickness=0,
            padx=12, pady=8, spacing1=2,
            xscrollcommand=self.editor_hsb.set,
        )

        def sync_y_scroll(*args):
            self.editor_vsb.set(*args)
            self.line_numbers.yview_moveto(args[0])

        def sync_y_view(*args):
            self.editor.yview(*args)
            self.line_numbers.yview(*args)

        self.editor.config(yscrollcommand=sync_y_scroll)
        self.editor_vsb.config(command=sync_y_view)
        self.editor_hsb.config(command=self.editor.xview)
        self.editor_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.editor_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.editor.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.editor.bind("<KeyRelease>", self._on_key_release_handler)
        self.editor.bind("<Control-space>", self._insert_suggestion)
        self.editor.bind("<Tab>", self._insert_suggestion)
        self.editor.bind("<KeyRelease>", self._rafraichir_position_curseur, add="+")
        self.editor.bind("<ButtonRelease-1>", self._rafraichir_position_curseur, add="+")
        self.editor.bind("<<Modified>>", self._sur_modification, add="+")
        self._define_tags()

    def _sur_modification(self, _event=None):
        """Marque le tampon comme modifié et met le point sur l'onglet.

        edit_modified(False) est INDISPENSABLE : sans cette remise à zéro,
        Tk n'émet <<Modified>> qu'UNE seule fois, à la première frappe, et
        l'indicateur ne bougerait plus jamais ensuite. Piège classique de
        ce virtual event, qui donne un indicateur « qui marche » en test
        rapide et faux en usage réel."""
        try:
            if not self.editor.edit_modified():
                return
            self.editor.edit_modified(False)
        except tk.TclError:
            return
        if not self.modifie:
            self.modifie = True
            self._rafraichir_titre_onglet()

    def _rafraichir_titre_onglet(self):
        if not hasattr(self, "etiquette_fichier"):
            return
        nom = self.fichier_courant or "sans_titre.uvi"
        try:
            self.etiquette_fichier.config(
                text=("● " + nom) if self.modifie else nom,
                fg=T.AMBRE if self.modifie else T.TEXTE_FORT)
        except tk.TclError:
            pass

    def _marquer_enregistre(self, nom=None):
        if nom:
            self.fichier_courant = nom
        self.modifie = False
        self._rafraichir_titre_onglet()

    def _clic_gouttiere(self, event):
        try:
            index = self.line_numbers.index("@%d,%d" % (event.x, event.y))
            ligne = int(index.split(".")[0])
        except (tk.TclError, ValueError):
            return "break"
        points = getattr(self, "_points_arret", None)
        if points is None:
            points = self._points_arret = set()
        points.discard(ligne) if ligne in points else points.add(ligne)
        self._update_line_numbers()
        return "break"

    # ------------------------------------------------------------------
    # Console : dock permanent à onglets
    # ------------------------------------------------------------------
    def _build_terminal(self, parent=None):
        """Console du dock inférieur.

        parent optionnel et non self.workspace : la console n'est plus une
        vue concurrente de l'éditeur mais un dock permanent (voir
        _build_container). Le paramètre garde toutefois une valeur par
        défaut pour que tout appel historique _build_terminal() continue de
        fonctionner.

        self.console, self.cmd_input et self.frame_terminal gardent leurs
        noms : 23, 18 et 3 références ailleurs."""
        parent = parent or self.workspace
        cadre = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.frame_terminal = cadre

        barre = tk.Frame(cadre, bg=T.PANNEAU_HAUT, height=34)
        barre.pack(fill=tk.X)
        barre.pack_propagate(False)
        self._onglets_console = {}
        # Les onglets FILTRENT une même console plutôt que d'être autant de
        # widgets distincts : la v8 mélangeait sortie de script, messages
        # système et erreurs dans un seul flux, si bien qu'une erreur pouvait
        # défiler hors de vue sous la sortie d'une boucle. Un filtre coûte
        # une relecture du tampon ; sept widgets auraient coûté sept chemins
        # d'écriture à tenir synchronisés dans log().

        # Boutons de fin, empaquetés AVANT la zone défilante : sous Tkinter,
        # un widget packé side=RIGHT après un side=LEFT expand=True se fait
        # écraser par ce dernier. L'ordre d'empaquetage est ici significatif
        # et non cosmétique.
        tk.Button(
            barre, text="🗑", bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
            activebackground=T.PANNEAU, activeforeground=T.ROUGE,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 9), cursor="hand2",
            command=self._clear_console, padx=10,
        ).pack(side=tk.RIGHT, padx=(4, 8))
        self.btn_onglet_suivant = tk.Button(
            barre, text="›", bg=T.PANNEAU_HAUT, fg=T.TEXTE,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 12, "bold"), cursor="hand2",
            command=lambda: self._defiler_onglets(1), padx=7)
        self.btn_onglet_suivant.pack(side=tk.RIGHT)
        self.btn_onglet_precedent = tk.Button(
            barre, text="‹", bg=T.PANNEAU_HAUT, fg=T.TEXTE,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 12, "bold"), cursor="hand2",
            command=lambda: self._defiler_onglets(-1), padx=7)
        self.btn_onglet_precedent.pack(side=tk.RIGHT)

        # Zone défilante horizontale. Même contrainte que pour la colonne
        # gauche : Tkinter ne fait pas défiler un Frame, il faut un Canvas
        # porteur. Sept onglets ne tiennent pas dans un dock de 900 px sur
        # un écran de portable -- sans défilement, DÉBOGUEUR et AGENT
        # seraient inatteignables.
        self.toile_onglets = tk.Canvas(barre, bg=T.PANNEAU_HAUT,
                                       highlightthickness=0, bd=0, height=30)
        self.toile_onglets.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                padx=(6, 0), pady=2)
        self.bande_onglets = tk.Frame(self.toile_onglets, bg=T.PANNEAU_HAUT)
        self._fenetre_onglets = self.toile_onglets.create_window(
            (0, 0), window=self.bande_onglets, anchor="nw")

        for nom in self._ONGLETS_CONSOLE:
            bouton = tk.Button(
                self.bande_onglets, text=nom, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                activebackground=T.PANNEAU, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 8, "bold"), cursor="hand2",
                padx=12, pady=6,
                command=lambda n=nom: self._selectionner_onglet_console(n),
            )
            bouton.pack(side=tk.LEFT, padx=1)
            self._onglets_console[nom] = bouton

        def _zone_onglets(_e=None):
            try:
                self.toile_onglets.configure(
                    scrollregion=self.toile_onglets.bbox("all"))
            except tk.TclError:
                pass

        self.bande_onglets.bind("<Configure>", _zone_onglets)

        def _molette_onglets(event):
            # La molette fait défiler HORIZONTALEMENT ici : c'est le seul
            # axe qui a un sens pour une bande d'onglets, et l'utilisateur
            # n'a pas à savoir qu'il devrait tenir Maj.
            pas = 1 if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0 else -1
            try:
                self.toile_onglets.xview_scroll(pas * 2, "units")
            except tk.TclError:
                pass

        for widget in (self.toile_onglets, self.bande_onglets):
            widget.bind("<MouseWheel>", _molette_onglets)
            widget.bind("<Button-4>", _molette_onglets)
            widget.bind("<Button-5>", _molette_onglets)

        self._onglet_console_actif = "CONSOLE UVI"

        # --- ligne de commande REPL --------------------------------------
        # BUGFIX (barre de saisie ?var invisible, second niveau) : ce cadre
        # (et, plus bas, barre_saisie qui répond à ?var) DOIT être empaqueté
        # AVANT `zone` (la console). `zone` est extensible (expand=True) et
        # capture tout le reste de la cavité dès qu'elle est empaquetée --
        # exactement le même mécanisme, à l'intérieur de ce dock, que celui
        # corrigé dans _build_container entre cadre_editeur et le dock
        # lui-même (voir son commentaire). Sans cet ordre, barre_cmd (et
        # barre_saisie) se retrouvent poussées hors des ~290 px du dock dès
        # que la fenêtre est un peu courte -- invisibles ET hors d'atteinte
        # du clavier, ce qui a exactement le même symptôme qu'un dock
        # entièrement absent : un script qui attend une réponse à ?var
        # semble figé sans qu'on sache pourquoi. side=BOTTOM préserve sa
        # position visuelle réelle (tout en bas) malgré cet ordre.
        barre_cmd = tk.Frame(cadre, bg=T.PANNEAU)
        barre_cmd.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        # Référence explicite plutôt que self.cmd_input.master : le jour où
        # la ligne de commande gagne un conteneur intermédiaire, .master ne
        # désigne plus le frère attendu et pack(before=...) lève.
        self.barre_commande = barre_cmd
        self.prompt_commande = tk.Label(
            barre_cmd, text="UVI ❯", fg=T.CYAN, bg=T.PANNEAU,
            font=("Consolas", 10, "bold"))
        self.prompt_commande.pack(side=tk.LEFT, padx=(0, 8))
        self.cmd_input = tk.Entry(
            barre_cmd, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            font=("Consolas", 10), relief=tk.FLAT,
            highlightthickness=1, highlightbackground=T.BORDURE,
            highlightcolor=T.CYAN,
        )
        self.cmd_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.cmd_input.bind("<Return>", self.run_command)
        self.cmd_input.bind("<Up>", self._history_up)
        self.cmd_input.bind("<Down>", self._history_down)

        self.message_onglet_vide = tk.Label(
            cadre, text="", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Segoe UI", 8, "italic"), anchor=tk.W, padx=12, pady=6)
        zone = tk.Frame(cadre, bg=T.CHAMP)
        zone.pack(fill=tk.BOTH, expand=True, padx=1)
        self.zone_console = zone
        self.console_vsb = tk.Scrollbar(zone, orient="vertical", bg=T.PANNEAU,
                                        troughcolor=T.CHAMP, relief=tk.FLAT,
                                        bd=0, width=10)
        self.console_hsb = tk.Scrollbar(zone, orient="horizontal", bg=T.PANNEAU,
                                        troughcolor=T.CHAMP, relief=tk.FLAT,
                                        bd=0, width=10)
        self.console = tk.Text(
            zone, bg=T.CHAMP, fg=T.TEXTE,
            font=("Consolas", 10), relief=tk.FLAT,
            state=tk.NORMAL, selectbackground="#1E3A5F",
            wrap=tk.NONE, highlightthickness=0, padx=12, pady=8,
            yscrollcommand=self.console_vsb.set,
            xscrollcommand=self.console_hsb.set,
        )
        self.console_vsb.config(command=self.console.yview)
        self.console_hsb.config(command=self.console.xview)
        self.console_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Double-clic sur une erreur -> saut à la ligne fautive. Les
        # messages contiennent déjà « ligne N » (format garanti par
        # UVIError.__str__) : l'information était là, mais il fallait la
        # lire puis faire défiler l'éditeur à la main pour s'en servir.
        self.console.bind("<Double-Button-1>", self._sauter_a_lerreur)

        # --- barre de SAISIE DE SCRIPT (commande ?) ----------------------
        # Cadre distinct de la barre de commande : ce sont deux choses
        # différentes qu'il ne faut surtout pas confondre. La barre de
        # commande est un REPL (on tape des instructions UVI hors
        # exécution) ; celle-ci répond à une question POSÉE PAR LE SCRIPT
        # EN COURS. Les mélanger, c'était demander à l'utilisateur de
        # deviner dans quel mode se trouve un même champ.
        #
        # Bandeau ambre plein volontairement : c'est le seul moment où
        # l'IDE ATTEND quelque chose de l'utilisateur, et un script bloqué
        # sans qu'on comprenne pourquoi est la pire expérience possible.
        # Il doit être impossible de le rater.
        self.barre_saisie = tk.Frame(cadre, bg=T.AMBRE)
        self.etiquette_saisie = tk.Label(
            self.barre_saisie, text="?", bg=T.AMBRE, fg="#1A1000",
            font=("Consolas", 10, "bold"))
        self.etiquette_saisie.pack(side=tk.LEFT, padx=(12, 8), pady=9)
        self.champ_saisie = tk.Entry(
            self.barre_saisie, bg="#1A1000", fg=T.AMBRE,
            insertbackground=T.AMBRE, relief=tk.FLAT,
            font=("Consolas", 11), highlightthickness=0)
        self.champ_saisie.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.champ_saisie.bind("<Return>", lambda e: self.valider_saisie())
        tk.Button(
            self.barre_saisie, text="VALIDER  ⏎", bg="#1A1000", fg=T.AMBRE,
            activebackground=T.AMBRE, activeforeground="#1A1000",
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 8, "bold"), cursor="hand2",
            command=self.valider_saisie, padx=12,
        ).pack(side=tk.RIGHT, padx=10, pady=6)
        # Non empaquetée au démarrage : elle n'apparaît que sur demande du
        # script, et disparaît dès la réponse donnée. (barre_cmd/cmd_input
        # sont construits PLUS HAUT, avant `zone` -- voir le commentaire à
        # cet endroit.)
        self._saisie_evenement = None
        self._saisie_resultat = {"valeur": ""}

        self._selectionner_onglet_console("CONSOLE UVI")

    #: Onglets du dock inférieur, dans l'ordre d'affichage.
    #: TERMINAL, DÉBOGUEUR et AGENT sont NOUVEAUX : la v9 mélangeait
    #: l'historique des commandes, l'état des points d'arrêt et les étapes
    #: de l'agent au milieu de la sortie du script. Trois choses qu'on
    #: consulte pour des raisons différentes, à des moments différents.
    _ONGLETS_CONSOLE = ("CONSOLE UVI", "SORTIE", "ERREURS", "TESTS",
                        "TERMINAL", "DÉBOGUEUR", "AGENT")

    def _defiler_onglets(self, sens):
        """Flèches ‹ › de la bande d'onglets. Existent en plus de la molette
        parce qu'un pavé tactile sans défilement horizontal est courant, et
        qu'un onglet inatteignable est un onglet qui n'existe pas."""
        try:
            self.toile_onglets.xview_scroll(sens * 3, "units")
        except (tk.TclError, AttributeError):
            pass

    def _sauter_a_lerreur(self, _event=None):
        """Amène le curseur de l'éditeur à la ligne citée par la ligne de
        console double-cliquée."""
        try:
            numero_console = int(self.console.index(tk.CURRENT).split(".")[0])
            texte = self.console.get("%d.0" % numero_console,
                                     "%d.end" % numero_console)
        except (tk.TclError, ValueError):
            return
        correspondance = re.search(r"ligne\s+(\d+)", texte)
        if not correspondance:
            return
        ligne = int(correspondance.group(1))
        self.show_editor()
        try:
            self.editor.mark_set(tk.INSERT, "%d.0" % ligne)
            self.editor.see("%d.0" % ligne)
            self.editor.tag_remove("ligne_courante", "1.0", tk.END)
            self.editor.tag_add("ligne_courante", "%d.0" % ligne, "%d.end" % ligne)
        except tk.TclError:
            return
        self._rafraichir_position_curseur()
        self._set_status("Ligne %d" % ligne)

    def _selectionner_onglet_console(self, nom):
        """Filtre l'affichage de la console.

        Relecture du tampon plutôt que quatre widgets : voir la
        justification dans _build_terminal. Conséquence assumée -- le filtre
        travaille sur le TEXTE des lignes, donc une ligne de sortie de
        script qui commencerait par « [ERREUR] » serait classée comme
        erreur. Cas rare et sans gravité, mais réel."""
        self._onglet_console_actif = nom
        for cle, bouton in getattr(self, "_onglets_console", {}).items():
            actif = (cle == nom)
            bouton.config(fg=T.CYAN if actif else T.TEXTE_FAIBLE,
                          bg=T.PANNEAU if actif else T.PANNEAU_HAUT)
        self._appliquer_filtre_console()

    def _appliquer_filtre_console(self):
        if not hasattr(self, "console"):
            return
        actif = getattr(self, "_onglet_console_actif", "CONSOLE UVI")
        if actif == "CONSOLE UVI":
            self.console.tag_configure("masque", elide=False)
            return

        # Chaque onglet est un prédicat « faut-il montrer cette ligne ? ».
        # Écrire les filtres comme des fonctions plutôt qu'en cascade de if
        # rend l'ajout d'un huitième onglet mécanique.
        def est_erreur(ligne):
            return ligne.lstrip().startswith("[ERREUR]")

        filtres = {
            # « demande une valeur » et « → » sont conservés : c'est
            # l'invite de saisie et la réponse. Les masquer ici est
            # précisément ce qui rendait la commande ? inutilisable --
            # l'utilisateur voyait un script figé sans invite visible.
            "SORTIE": lambda l: (
                "demande une valeur" in l or l.lstrip().startswith("→")
                or (not est_erreur(l) and not l.lstrip().startswith(
                    ("[SYS]", "[OK]", "[AGENT", "UVI>")))),
            "ERREURS": est_erreur,
            "TESTS": lambda l: ("moteur" in l.lower() or "vérification" in l.lower()
                                or l.lstrip().startswith(("✓", "✗", "[SYS] ==="))),
            "TERMINAL": lambda l: l.lstrip().startswith("UVI>"),
            "DÉBOGUEUR": lambda l: ("point d'arrêt" in l.lower()
                                    or "ligne" in l.lower() and "arrêt" in l.lower()),
            "AGENT": lambda l: l.lstrip().startswith("[AGENT"),
        }
        garder = filtres.get(actif)
        if garder is None:
            self.console.tag_configure("masque", elide=False)
            return
        try:
            self.console.tag_remove("masque", "1.0", tk.END)
            total = int(self.console.index("end-1c").split(".")[0])
            vides = True
            for i in range(1, total + 1):
                texte = self.console.get("%d.0" % i, "%d.end" % i)
                if not texte.strip():
                    continue
                if garder(texte):
                    vides = False
                else:
                    self.console.tag_add("masque", "%d.0" % i, "%d.end+1c" % i)
            self.console.tag_configure("masque", elide=True)
            self._afficher_onglet_vide(actif if vides else None)
        except tk.TclError:
            pass

    def _afficher_onglet_vide(self, nom):
        """Affiche (ou masque) le message d'onglet vide.

        BUG CORRIGÉ : ce message était INSÉRÉ DANS LA CONSOLE. Il devenait
        donc du contenu permanent, refiltré au passage suivant, et chaque
        changement d'onglet en empilait un de plus -- au bout de dix
        bascules, la console contenait dix lignes de service que
        l'utilisateur n'avait jamais produites, et que _clear_console seul
        pouvait retirer.

        Un Label distinct au lieu du tampon : le message n'appartient pas à
        la sortie du programme, il appartient à l'interface. Les mélanger
        était l'erreur de départ."""
        if not hasattr(self, "message_onglet_vide"):
            return
        messages = {
            "SORTIE": "Aucune sortie de script pour l'instant. Cliquez ▶ EXÉCUTER.",
            "ERREURS": "Aucune erreur. ",
            "TESTS": "Aucun test lancé. Panneau TESTS du rail, ou 🧪 dans OUTILS UVI.",
            "TERMINAL": "Aucune commande saisie. Tapez dans le champ UVI ❯ ci-dessous.",
            "DÉBOGUEUR": "Aucun point d'arrêt. Cliquez un numéro de ligne dans l'éditeur.",
            "AGENT": "L'agent n'a pas encore tourné. Bouton 🪄 AUTO-CORRIGER.",
        }
        message = messages.get(nom) if nom else None
        try:
            if message:
                self.message_onglet_vide.config(text="  " + message)
                self.message_onglet_vide.pack(fill=tk.X, before=self.zone_console)
            else:
                self.message_onglet_vide.pack_forget()
        except tk.TclError:
            pass

    def _build_canvas_tab(self):
        """Onglet CANVAS : troisième vue du workspace, même mécanisme de
        bascule que ÉDITEUR/TERMINAL (pack_forget / pack, cf. show_editor/
        show_terminal/show_canvas). Contient un tk.Canvas où sont rendues
        toutes les commandes DRAW.* -- unique point d'entrée graphique de
        l'IDE, appelé par canvas_draw() ci-dessous, elle-même appelée par
        _exec_draw (tree-walker, script réel) et par le draw_fn passé à
        UVIVirtualMachine dans run_script_vm (VM Phase 3)."""
        self.frame_canvas = tk.Frame(self.workspace, bg=T.CHAMP)

        toolbar = tk.Frame(self.frame_canvas, bg="#111111", height=32)
        toolbar.pack(fill=tk.X, side=tk.TOP)
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar, text="🎨 CANEVAS 2D — rendu des commandes DRAW.*",
            bg="#111111", fg="#00FFFF", font=("Consolas", 9, "bold"),
        ).pack(side=tk.LEFT, padx=10, pady=6)
        tk.Button(
            toolbar, text="🗑 Effacer",
            bg="#111111", fg="#555555", activebackground="#222222",
            activeforeground="#FF4444", relief=tk.FLAT,
            font=("Consolas", 9), cursor="hand2",
            command=lambda: self.canvas_draw("clear"),
        ).pack(side=tk.RIGHT, padx=10, pady=4)

        canvas_frame = tk.Frame(self.frame_canvas, bg="#050505")
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.dev_canvas = tk.Canvas(
            canvas_frame, bg="#000000", highlightthickness=1,
            highlightbackground="#333333",
        )
        self.dev_canvas.pack(fill=tk.BOTH, expand=True)
        self.dev_canvas_bg = "#000000"

    def canvas_draw(self, op: str, *args):
        """Contrat appelé pour chaque effet de bord DRAW.* -- voir
        _exec_draw (tree-walker) et le draw_fn transmis à
        UVIVirtualMachine dans run_script_vm (VM Phase 3).

        DOCSTRING CORRIGÉE (elle décrivait encore l'ancienne implémentation
        après que le corps de la méthode soit passé à _poster_ui -- voir le
        commentaire juste avant l'appel, en bas de cette méthode, qui
        explique pourquoi root.after() nu a été abandonné). Ce qui est
        VRAI aujourd'hui : _do() est posté sur la file thread-safe
        _poster_ui, vidée uniquement par _pomper_file_ui sur le thread
        principal -- sûr quel que soit le thread appelant. run_script
        (tree-walker) ET run_script_vm (VM Phase 3) tournent maintenant
        TOUS LES DEUX dans un thread dédié (voir leurs docstrings
        respectives), donc canvas_draw y est systématiquement appelée hors
        du thread principal ; _poster_ui est ce qui rend ça sûr, pas
        root.after."""
        def _do():
            c = self.dev_canvas
            if op == "clear":
                c.delete("all")
            elif op == "bgcolor":
                couleur = args[0] if args and str(args[0]).strip() else "#000000"
                try:
                    c.configure(bg=couleur)
                    self.dev_canvas_bg = couleur
                except tk.TclError:
                    pass  # couleur invalide -- effet de bord silencieusement ignoré
            elif op == "rect":
                # 6e argument optionnel = remplissage, 7e = épaisseur du
                # contour ; "" et 1 = comportement historique EXACT si
                # absents (1 est aussi le défaut natif de Tkinter).
                x, y, w, h, couleur = args[:5]
                remplissage = args[5] if len(args) >= 6 else ""
                epaisseur = args[6] if len(args) >= 7 else 1
                c.create_rectangle(x, y, x + w, y + h, outline=couleur, fill=remplissage,
                                    width=epaisseur)
            elif op == "circle":
                x, y, r, couleur = args[:4]
                remplissage = args[4] if len(args) >= 5 else ""
                epaisseur = args[5] if len(args) >= 6 else 1
                c.create_oval(x - r, y - r, x + r, y + r, outline=couleur, fill=remplissage,
                               width=epaisseur)
            elif op == "line":
                x1, y1, x2, y2, couleur = args[:5]
                epaisseur = args[5] if len(args) >= 6 else 1
                c.create_line(x1, y1, x2, y2, fill=couleur, width=epaisseur)
            elif op == "text":
                # 5e argument optionnel = taille de police ; 10 =
                # comportement historique EXACT si absent.
                x, y, texte, couleur = args[:4]
                taille_police = args[4] if len(args) >= 5 else 10
                c.create_text(x, y, text=texte, fill=couleur, anchor="nw",
                               font=("Consolas", taille_police))
            elif op == "sprite":
                x, y, taille, pixels = args
                self._draw_sprite_grid(x, y, taille, pixels)
            elif op == "poly":
                # points est une liste PLATE x1,y1,x2,y2,... (déjà
                # validée pair + ≥3 points côté _exec_draw / VM avant
                # d'arriver ici) ; create_polygon accepte cette forme
                # plate directement en *args. 4e argument optionnel =
                # épaisseur du contour, défaut 1 (natif Tkinter).
                points, couleur, remplissage = args[:3]
                epaisseur = args[3] if len(args) >= 4 else 1
                c.create_polygon(*points, outline=couleur, fill=remplissage, width=epaisseur)
        # _poster_ui et non root.after : canvas_draw est appelée depuis le
        # THREAD D'EXÉCUTION du script, à chaque instruction DRAW.*. C'était
        # donc un appel Tcl depuis un thread secondaire répété des dizaines
        # de fois par seconde dans une boucle de jeu -- la variante la plus
        # dangereuse de la faute corrigée pour la saisie, et une explication
        # plausible des blocages observés en jeu.
        self._poster_ui(_do)

    def _build_chat_panel(self, prefixe: str, titre: str, couleur_titre: str,
                          on_send, on_reset, couleur_reponse: str = "#00FF00"):
        """Construit un panneau de discussion (historique + saisie + bouton).

        FACTORISÉE plutôt qu'écrite en place dans _build_shion_tab : un
        panneau de discussion IA est un composant générique (historique +
        champ de saisie + bouton), et le projet a déjà eu un second onglet
        de ce type par le passé (ERIS, un assistant Gemini séparé, retiré
        depuis -- SHION couvre son rôle). Garder la construction factorisée
        évite qu'un futur second onglet de discussion ne reparte d'une
        copie plutôt que de cette fabrique.

        Les widgets sont rangés sous self.<prefixe>_historique,
        self.<prefixe>_input, self.btn_<prefixe>_send -- voir
        _build_shion_tab pour l'unique usage actuel (prefixe="shion")."""
        frame = tk.Frame(self.workspace, bg="#0D0D0D")

        toolbar = tk.Frame(frame, bg="#111111", height=32)
        toolbar.pack(fill=tk.X, side=tk.TOP)
        toolbar.pack_propagate(False)
        label_titre = tk.Label(
            toolbar, text=titre,
            bg="#111111", fg=couleur_titre, font=("Consolas", 9, "bold"),
        )
        label_titre.pack(side=tk.LEFT, padx=10, pady=6)
        setattr(self, prefixe + "_titre_label", label_titre)
        tk.Button(
            toolbar, text="🗑 Nouvelle conversation",
            bg="#111111", fg="#555555", activebackground="#222222",
            activeforeground="#FF4444", relief=tk.FLAT,
            font=("Consolas", 9), cursor="hand2",
            command=on_reset,
        ).pack(side=tk.RIGHT, padx=10, pady=4)

        historique_frame = tk.Frame(frame, bg="#050505")
        historique_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        historique = tk.Text(
            historique_frame, bg="#000000", fg="#D8FFE8",
            insertbackground="#00FF00", font=("Consolas", 10),
            relief=tk.FLAT, wrap=tk.WORD, state=tk.DISABLED,
            padx=10, pady=10,
        )
        historique.pack(fill=tk.BOTH, expand=True)
        historique.tag_configure("vous", foreground="#00FFFF", font=("Consolas", 10, "bold"))
        historique.tag_configure("assistant", foreground=couleur_reponse, font=("Consolas", 10, "bold"))
        historique.tag_configure("erreur", foreground="#FF4444")
        historique.tag_configure("corps", foreground="#D8FFE8")
        setattr(self, prefixe + "_historique", historique)

        saisie_frame = tk.Frame(frame, bg="#0D0D0D")
        saisie_frame.pack(fill=tk.X, padx=10, pady=10)
        saisie = tk.Entry(
            saisie_frame, bg="#111111", fg="#D8FFE8",
            insertbackground="#00FF00", relief=tk.FLAT,
            font=("Consolas", 11),
        )
        saisie.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 8))
        saisie.bind("<Return>", lambda e: on_send())
        setattr(self, prefixe + "_input", saisie)

        bouton = tk.Button(
            saisie_frame, text="Envoyer ➤",
            bg="#00FF00", fg="#000000", activebackground="#33FF33",
            disabledforeground="#444444", relief=tk.FLAT,
            font=("Consolas", 10, "bold"), cursor="hand2",
            command=on_send,
        )
        bouton.pack(side=tk.RIGHT, ipadx=10, ipady=4)
        setattr(self, "btn_" + prefixe + "_send", bouton)
        return frame

    def _chat_append(self, prefixe: str, texte: str, tag: str, entete: str = ""):
        """Ajoute une entrée dans l'historique du panneau `prefixe`.

        Widget en lecture seule (state=DISABLED en permanence sauf le
        temps de l'insertion) -- même principe que self.console pour le
        terminal (voir log()), pour empêcher l'utilisateur de modifier
        l'historique directement au clavier."""
        historique = getattr(self, prefixe + "_historique")
        historique.config(state=tk.NORMAL)
        if entete:
            historique.insert(tk.END, entete + "\n", tag)
        historique.insert(tk.END, str(texte) + "\n\n", "corps" if tag != "erreur" else tag)
        historique.config(state=tk.DISABLED)
        historique.see(tk.END)

    def _chat_send(self, prefixe: str, client, nom_repondeur: str):
        """Envoi non bloquant commun aux deux onglets de discussion.

        La requête réseau (LLMClient.complete, bloquante -- urllib avec
        timeout) tourne dans un thread daemon séparé pour ne JAMAIS geler
        le thread principal Tkinter pendant l'attente -- même principe que
        _exec_shion (SHION.ASK non-bloquant). Le champ de saisie et le
        bouton sont désactivés pendant l'attente pour empêcher d'empiler
        une seconde requête avant que la première ait répondu (pas de Lock
        nécessaire : un seul thread daemon en vol à la fois, garanti par
        cette désactivation d'UI plutôt que par une variable partagée --
        cf. le commentaire équivalent sur shion_is_busy).

        Le retour au thread principal passe par _poster_ui (file
        d'attente) : rien de ce qui touche un widget ne s'exécute depuis le
        thread worker, pas même un root.after -- qui est lui-même un appel
        Tcl et donc soumis à la même contrainte."""
        saisie = getattr(self, prefixe + "_input")
        bouton = getattr(self, "btn_" + prefixe + "_send")
        question = saisie.get().strip()
        if not question:
            return
        if client is None:
            self._chat_append(prefixe, "Service non configuré (voir le message ci-dessus).", "erreur")
            return

        saisie.delete(0, tk.END)
        self._chat_append(prefixe, question, "vous", entete="Vous :")
        saisie.config(state=tk.DISABLED)
        bouton.config(state=tk.DISABLED)

        def _worker():
            try:
                reponse = client.complete(question)
                erreur = None
            except LLMClientError as e:
                # LLMClientError couvre tous les backends (DeepSeekClientError,
                # ShionClientError...) sans avoir à les connaître un par un --
                # rattraper une exception plus spécifique aurait laissé filer
                # les autres, invisibles dans un thread daemon, et l'onglet
                # serait resté figé sur "saisie désactivée".
                reponse, erreur = None, str(e)
            except Exception as e:  # filet : un thread daemon qui meurt en silence gèlerait l'onglet
                reponse, erreur = None, "Erreur inattendue : %s" % e

            def _apply():
                if erreur is not None:
                    self._chat_append(prefixe, erreur, "erreur")
                else:
                    self._chat_append(prefixe, reponse, "assistant", entete=nom_repondeur + " :")
                saisie.config(state=tk.NORMAL)
                bouton.config(state=tk.NORMAL)
                saisie.focus_set()

            # Même raison que pour request_input : ce code tourne dans un
            # thread daemon, root.after() y est un appel Tk non fiable.
            self._poster_ui(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def _build_shion_tab(self):
        """Onglet de discussion SHION : troisième vue du workspace (voir
        _VUES), même mécanisme de bascule que ÉDITEUR/CANEVAS.

        Avant, SHION n'était accessible QUE par l'instruction SHION.ASK
        dans un script -- pour poser une question il fallait écrire un
        programme, l'enregistrer et l'exécuter. Cet onglet donne un accès
        direct, en discussion, à la MÊME instance et au MÊME historique que
        SHION.ASK en script (self.engine.shion_client) -- une question
        posée ici est dans le contexte du script exécuté ensuite, et
        réciproquement.

        (L'onglet ERIS, assistant Gemini séparé, a été retiré : SHION
        couvre déjà son rôle sans dépendre d'une clé API personnelle. La
        syntaxe !ERIS(...) reste utilisable dans un script -- voir
        _eval_expr_or_eris -- mais répond désormais toujours via SHION.)"""
        self.frame_shion = self._build_chat_panel(
            prefixe="shion",
            titre="🧠 SHION — %s" % (
                self.engine.shion_client.nom_affichage
                if self.engine.shion_client is not None else "non configuré"
            ),
            couleur_titre="#00FF00" if self.engine.shion_client is not None else "#FF4444",
            on_send=self._shion_send_message,
            on_reset=self._shion_reset_conversation,
        )
        if self.engine.shion_client is None:
            self._chat_append(
                "shion",
                "SHION n'est pas configuré ici (le paquet shion/ est-il bien "
                "présent à côté de uvi_core.py ?). Avec SHION_BACKEND=deepseek, "
                "lancez plutôt un serveur vLLM servant DeepSeek-R1-Distill-"
                "Qwen-7B et exportez SHION_SERVER_URL (ex. "
                "http://192.168.1.50:8000) avant de relancer UVI -- voir "
                "docs/SERVEUR_DEEPSEEK.md pour la commande exacte. Aucune clé "
                "personnelle n'est nécessaire dans un cas comme dans l'autre.",
                "erreur",
            )

    def _shion_send_message(self):
        self._chat_send("shion", self.engine.shion_client, "SHION")

    def _shion_reset_conversation(self):
        if self.engine.shion_client is not None:
            self.engine.shion_client.reset_history()
        self.shion_historique.config(state=tk.NORMAL)
        self.shion_historique.delete("1.0", tk.END)
        self.shion_historique.config(state=tk.DISABLED)

    def _draw_sprite_grid(self, x: float, y: float, taille: float, pixels: list):
        """DRAW.SPRITE : pixels est un tableau UVI PLAT de couleurs
        (réutilise le système TAB existant plutôt qu'un format image
        séparé). Affiché en grille carrée -- côté = racine entière de la
        longueur du tableau, arrondi au carré parfait inférieur, le reste
        des éléments (au-delà de côté²) est ignoré plutôt que de planter
        sur un tableau de longueur non carrée (ex. 5 éléments -> grille
        2x2, le 5e pixel n'est pas affiché)."""
        import math
        n = len(pixels)
        if n <= 0:
            return
        cote = int(math.isqrt(n))
        if cote <= 0:
            return
        for i in range(cote * cote):
            row, col = divmod(i, cote)
            couleur = str(pixels[i]) if pixels[i] not in (None, "") else self.dev_canvas_bg
            px = x + col * taille
            py = y + row * taille
            try:
                self.dev_canvas.create_rectangle(
                    px, py, px + taille, py + taille, outline="", fill=couleur,
                )
            except tk.TclError:
                pass  # couleur invalide dans le tableau -- pixel ignoré, pas de crash

    def load_from_disk(self):
        filepath = filedialog.askopenfilename(
            filetypes=[("UVI Scripts", "*.uvi"), ("All Files", "*.*")]
        )
        if filepath:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.engine.memoire.clear()
            self.engine.routines.clear()
            self.engine.fonctions.clear()
            self.engine.imported_files.clear()
            self.engine.current_memory_usage = 0
            self.engine.error_flag = False
            self.engine.failed_line = None
            self.engine.last_errors = []
            self.editor.delete("1.0", tk.END)
            self.editor.insert(tk.END, content)
            self.show_editor()
            self._update_line_numbers()
            self.log(f"Fichier chargé depuis le disque : {filepath}", "sys")

    def save_to_disk(self, forcer_dialogue=False):
        """Ctrl+S / bouton SAUVEGARDER.

        Écrase le fichier courant s'il y en a un, au lieu de redemander un
        nom À CHAQUE FOIS comme le faisait la version précédente. Redemander
        systématiquement rendait Ctrl+S inutilisable en frappe rapide :
        chaque sauvegarde ouvrait une boîte de dialogue et cassait le
        rythme de travail.

        Le fichier VFS est mis à jour EN PLUS du disque quand il porte le
        même nom : sans ça, l'explorateur affichait encore l'ancienne
        version d'un fichier qu'on venait de sauvegarder, et un IMPORT
        chargeait l'ancienne."""
        chemin = getattr(self, "_chemin_disque_courant", None)
        if forcer_dialogue or not chemin:
            chemin = filedialog.asksaveasfilename(
                defaultextension=".uvi",
                filetypes=[("UVI Scripts", "*.uvi"), ("All Files", "*.*")]
            )
            if not chemin:
                return
        contenu = self.editor.get("1.0", tk.END)
        try:
            with open(chemin, 'w', encoding='utf-8') as f:
                f.write(contenu)
        except OSError as e:
            self.log("Écriture impossible : %s" % e, "err")
            return
        self._chemin_disque_courant = chemin
        nom = os.path.basename(chemin)
        # Miroir dans le VFS : write_file passe par _safe_resolve, donc un
        # nom hostile est refusé ici comme partout ailleurs.
        try:
            self.vfs.write_file(nom, contenu)
            self.update_vfs_list()
        except UVIRuntimeTrap:
            pass
        self._marquer_enregistre(nom)
        self.log("Sauvegardé : %s" % chemin, "ok")
        self._set_status("Sauvegardé — %s" % nom)

    def update_vfs_list(self):
        """Explorateur en arborescence (maquette v9).

        Le VFS est PLAT -- c'est un répertoire unique, il n'y a pas de vrais
        sous-dossiers. L'arborescence affichée est donc un REGROUPEMENT PAR
        TYPE, pas une hiérarchie de disque. Dit explicitement ici pour que
        personne n'en déduise plus tard que le VFS gère des chemins
        imbriqués : « src » et « lib » sont des étiquettes de présentation,
        et double-cliquer dessus ne fait rien.

        Le mapping index -> nom de fichier est conservé dans
        self._index_fichiers parce que _on_vfs_double_click lit l'index de
        la Listbox : sans lui, cliquer sur « ▾ src » tenterait d'ouvrir un
        fichier nommé « ▾ src ».

        self.vfs_listbox garde son nom (8 références ailleurs)."""
        self.vfs_listbox.delete(0, tk.END)
        self._index_fichiers = {}
        # Les entrées commençant par un point (.stdlib, .packages) sont
        # l'intendance interne du registre de paquets, pas des documents de
        # l'utilisateur. list_files() les rend -- c'est correct, elle décrit
        # le disque -- mais les AFFICHER dans l'explorateur ferait croire à
        # des fichiers ouvrables, alors qu'un double-clic dessus ne peut que
        # échouer (ce sont des répertoires). Filtrage à l'affichage
        # uniquement : list_files() n'est pas modifiée, huit autres appelants
        # en dépendent.
        fichiers = [f for f in self.vfs.list_files() if not f.startswith(".")]
        groupes = [
            ("src", [f for f in fichiers if f.endswith(".uvi")
                     and not f.startswith(("lib_", "std_"))]),
            ("lib", [f for f in fichiers if f.endswith(".uvi")
                     and f.startswith(("lib_", "std_"))]),
            ("données", [f for f in fichiers if not f.endswith(".uvi")]),
        ]
        position = 0
        for nom_groupe, membres in groupes:
            if not membres:
                continue
            self.vfs_listbox.insert(tk.END, "  ▾ %s" % nom_groupe)
            self.vfs_listbox.itemconfig(position, foreground=T.TEXTE_FAIBLE)
            position += 1
            for fichier in sorted(membres):
                icone = "📄" if fichier.endswith(".uvi") else "📋"
                self.vfs_listbox.insert(tk.END, "      %s %s" % (icone, fichier))
                self._index_fichiers[position] = fichier
                position += 1
        if not fichiers:
            self.vfs_listbox.insert(tk.END, "  (stockage vide)")
            self.vfs_listbox.itemconfig(0, foreground=T.TEXTE_FAIBLE)

    def _on_vfs_double_click(self, event):
        """Ouvre le fichier sélectionné dans l'éditeur.

        Passe par self._index_fichiers et non par le texte de la ligne :
        l'arborescence v9 préfixe les entrées d'une indentation et d'une
        icône, et les lignes de groupe (« ▾ src ») ne correspondent à aucun
        fichier."""
        selection = self.vfs_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        nom = getattr(self, "_index_fichiers", {}).get(index)
        if nom is None:
            return  # ligne de groupe : rien à ouvrir
        contenu = self.vfs.read_file(nom)
        if contenu is None:
            self.log("[FILE_NOT_FOUND] Fichier introuvable : %s" % nom, "err")
            return
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", contenu)
        self._update_line_numbers()
        self.show_editor()
        self._marquer_enregistre(nom)
        self._set_status("%s chargé." % nom)

    def _define_tags(self):
        """Coloration syntaxique v9.

        Les NOMS de tags sont strictement conservés (signature, comment,
        print_cmd, assign, routine, condition, loop, string, sleep, disk,
        ask_input, func_def, func_call, array, error_line, import) : c'est
        _highlight() qui les applique par expression régulière, et renommer
        un seul tag rendrait sa règle silencieusement inopérante -- le texte
        s'afficherait simplement sans couleur, sans la moindre erreur pour
        le signaler.

        Regroupement en familles plutôt qu'une teinte par mot-clé : la v8
        avait seize couleurs sans parenté, si bien qu'un script un peu long
        ressemblait à un arc-en-ciel où plus rien ne ressortait. Ici, les
        mots-clés de structure partagent le violet, les données le jaune, la
        machinerie le cyan."""
        police_gras = ("Consolas", 12, "bold")
        self.editor.tag_configure("signature",  foreground=T.SYN_OPERATEUR, font=police_gras)
        self.editor.tag_configure("comment",    foreground=T.SYN_COMMENT, font=("Consolas", 12, "italic"))
        self.editor.tag_configure("print_cmd",  foreground=T.VERT, font=police_gras)
        self.editor.tag_configure("assign",     foreground=T.TEXTE)
        self.editor.tag_configure("routine",    foreground=T.ROSE, font=police_gras)
        self.editor.tag_configure("condition",  foreground=T.SYN_MOTCLE, font=police_gras)
        self.editor.tag_configure("loop",       foreground=T.SYN_MOTCLE, font=police_gras)
        self.editor.tag_configure("string",     foreground=T.SYN_CHAINE)
        self.editor.tag_configure("sleep",      foreground=T.AMBRE)
        self.editor.tag_configure("disk",       foreground=T.CYAN, font=police_gras)
        self.editor.tag_configure("ask_input",  foreground=T.CYAN, font=police_gras)
        self.editor.tag_configure("func_def",   foreground=T.AMBRE, font=police_gras)
        self.editor.tag_configure("func_call",  foreground=T.SYN_FONCTION, font=police_gras)
        self.editor.tag_configure("array",      foreground=T.VIOLET, font=police_gras)
        self.editor.tag_configure("import",     foreground=T.SYN_FONCTION, font=("Consolas", 12, "bold", "underline"))
        # Ligne en erreur : bandeau rouge sombre plutôt que le #8B0000 plein
        # de la v8, qui rendait le texte de la ligne fautive quasi illisible
        # -- exactement la ligne qu'on a le plus besoin de lire.
        self.editor.tag_configure("error_line", background="#3A1520",
                                  foreground="#FFB4B4")
        self.editor.tag_configure("ligne_courante", background="#0E1A2E")

    def _highlight(self):
        if self._lock_highlight:
            return
        self._lock_highlight = True
        try:
            for tag in (
                "signature", "comment", "print_cmd", "assign",
                "routine", "condition", "loop", "string", "sleep",
                "disk", "ask_input", "func_def", "func_call", "array", "error_line", "import"
            ):
                self.editor.tag_remove(tag, "1.0", tk.END)
            content = self.editor.get("1.0", tk.END)
            lines = content.split("\n")
            for i, line in enumerate(lines):
                row = i + 1
                stripped = line.strip()
                def apply(tag, col_start=0, col_end=None, _line=line):
                    end = col_end if col_end is not None else len(_line)
                    self.editor.tag_add(tag, f"{row}.{col_start}", f"{row}.{end}")
                if stripped == "@UVI":
                    apply("signature")
                elif stripped.startswith("#"):
                    apply("comment")
                elif stripped.startswith("IMPORT "):
                    apply("import")
                elif stripped.startswith("DEF "):
                    apply("func_def")
                elif stripped.startswith("CALL "):
                    apply("func_call")
                elif stripped.startswith("TAB") or stripped.startswith("STR"):
                    apply("array")
                elif stripped.startswith("!"):
                    apply("print_cmd")
                elif stripped.startswith("*"):
                    apply("routine")
                elif stripped.startswith("~"):
                    apply("condition")
                elif stripped.startswith("%"):
                    apply("loop")
                elif stripped.startswith("_") and "=" not in stripped:
                    apply("sleep")
                elif stripped.startswith("&"):
                    apply("disk")
                elif stripped.startswith("?"):
                    apply("ask_input")
                elif "=" in stripped:
                    apply("assign")
                in_str = False
                start = 0
                for j, ch in enumerate(line):
                    if ch == '"':
                        if not in_str:
                            in_str = True
                            start = j
                        else:
                            in_str = False
                            self.editor.tag_add("string", f"{row}.{start}", f"{row}.{j + 1}")
            if getattr(self.engine, 'failed_line', None):
                r = self.engine.failed_line
                self.editor.tag_add("error_line", f"{r}.0", f"{r}.end")
        finally:
            self._lock_highlight = False

    def _update_line_numbers(self, event=None):
        if event is not None:
            self.engine.failed_line = None
        self._highlight()
        self._sync_line_numbers()

    def _sync_line_numbers(self):
        """Numéros de ligne + pastilles de points d'arrêt.

        Le losange ◆ est rendu DANS le texte de la gouttière, pas comme un
        widget superposé : un widget par ligne serait à repositionner à
        chaque défilement, et Tkinter ne synchronise pas les widgets flottants
        avec le scroll d'un Text. Le caractère, lui, défile avec la ligne
        gratuitement."""
        total = int(self.editor.index(tk.END).split('.')[0])
        points = getattr(self, "_points_arret", set())
        self.line_numbers.config(state=tk.NORMAL)
        self.line_numbers.delete("1.0", tk.END)
        lignes = []
        for n in range(1, total):
            lignes.append("◆%3d" % n if n in points else " %3d" % n)
        self.line_numbers.insert(tk.END, "\n".join(lignes))
        self.line_numbers.tag_configure("point_arret", foreground=T.ROUGE)
        for n in points:
            if n < total:
                self.line_numbers.tag_add("point_arret", "%d.0" % n, "%d.1" % n)
        self.line_numbers.config(state=tk.DISABLED)

    def _highlight_error_line(self, row):
        self.engine.failed_line = row
        self.show_editor()
        self.root.update_idletasks()
        self.editor.see(f"{row}.0")

    # "terminal" ne fait PLUS partie de la pile de vues : frame_terminal
    # vit désormais dans le dock inférieur, visible en permanence à côté de
    # l'éditeur (voir _build_container). L'y laisser aurait fait
    # pack_forget() la console à chaque changement de vue -- elle aurait
    # disparu au premier clic sur CANEVAS pour ne jamais revenir.
    _VUES = ("editor", "canvas", "shion")

    def _show_view(self, nom: str):
        """Bascule vers une vue du workspace (pack_forget des autres, pack
        de celle-ci) et met la topbar en accord.

        FACTORISATION SIGNALÉE. Avant : plusieurs méthodes show_* qui
        répétaient chacune leurs pack_forget et config de bouton. La table
        _VUES rend l'ajout (ou le retrait -- voir le départ de l'onglet
        ERIS) d'une vue mécanique.

        Les méthodes show_editor/show_terminal/show_canvas sont CONSERVÉES
        et délèguent ici : elles sont référencées par les commandes de
        boutons et appelées depuis run_script, _highlight_error_line,
        _afficher_accueil, etc."""
        for autre in self._VUES:
            frame = getattr(self, "frame_" + autre, None)
            if frame is not None:
                frame.pack_forget()
        frame_active = getattr(self, "frame_" + nom, None)
        if frame_active is not None:
            frame_active.pack(fill=tk.BOTH, expand=True)
        boutons = {
            "editor": "btn_edit", "terminal": "btn_term", "canvas": "btn_canvas",
            "shion": "btn_shion",
        }
        for vue, nom_attr in boutons.items():
            bouton = getattr(self, nom_attr, None)
            if bouton is None:
                continue
            if vue == nom:
                bouton.config(bg="#252525", fg="#00FF00")
            else:
                bouton.config(bg="#111111", fg="#555555")

    def show_editor(self):
        self._show_view("editor")
        self.editor.focus_set()
        self._update_line_numbers()

    def show_terminal(self):
        """Conservée : appelée à une quinzaine d'endroits (run_script, les
        méthodes de l'agent, _afficher_accueil...).

        CORRIGÉ (retour utilisateur) : la console est certes un dock
        permanent (voir §DISPOSITION v9), mais « afficher le terminal » se
        limitait à y amener le REGARD (scroll + focus), sans jamais
        ramener la vue centrale sur l'éditeur. Concrètement : lancer un
        script pendant qu'on est sur l'onglet SHION laissait ce panneau
        de discussion occuper tout le centre de l'écran -- la
        console restait techniquement visible dans le dock du bas, mais
        hors du regard, ce qui donnait l'impression que l'exécution
        n'affichait plus rien et qu'un ?INPUT restait sans réponse
        possible. _show_view("editor") ramène l'éditeur (et donc le dock
        qui vit juste en dessous) au premier plan à chaque appel -- c'est
        ce qui manquait pour que « basculer sur le terminal » veuille
        vraiment dire quelque chose."""
        self._show_view("editor")
        self._selectionner_onglet_console("CONSOLE UVI")
        try:
            self.console.see(tk.END)
            if not self.engine.is_running:
                self.cmd_input.focus_set()
        except tk.TclError:
            pass

    def show_canvas(self):
        self._show_view("canvas")

    def show_shion(self):
        self._show_view("shion")
        self.shion_input.focus_set()

    # ---------------------------------------------------------------
    # ÉTAPE 6 -- déclenchement de IAAgent.autonomous_fix depuis l'IDE
    # ---------------------------------------------------------------
    #: Objectif par défaut passé à autonomous_fix. Formulé pour correspondre
    #: EXACTEMENT à ce que _default_verify_fn sait vérifier (absence de trap
    #: à l'exécution) -- ni plus, ni moins. Promettre « le script doit faire
    #: ce que je veux » alors que la vérification ne teste que « il ne
    #: plante pas » produirait un agent qui se déclare satisfait d'un script
    #: syntaxiquement valide et fonctionnellement faux.
    OBJECTIF_AUTO_FIX = (
        "Le script UVI doit s'exécuter du début à la fin sans déclencher "
        "d'erreur d'exécution. Corrige uniquement ce qui empêche l'exécution "
        "d'aboutir, sans changer l'intention du script."
    )

    def _generate_fn_disponible(self):
        """Rend (fonction_de_generation, nom_du_backend) ou (None, None)."""
        if HAS_SHION and self.engine.shion_client is not None:
            return (lambda prompt: self.engine.shion_client.complete(prompt),
                    self.engine.shion_client.nom_affichage)
        return (None, None)

    def _log_agent_steps(self, resultat, total_prevu: int):
        """ÉTAPE 7.3 : rend AgentRunResult.steps lisible dans la console
        texte existante, sans nouveau widget.

        Avant : autonomous_fix ne rendait qu'une ligne finale
        (stopped_reason brut), donc une boucle de huit itérations et deux
        minutes d'attente se résumait à « bloqué » -- impossible de savoir
        si l'agent avait progressé, tourné en rond, ou échoué dès le premier
        appel réseau.

        Format retenu : un préfixe [AGENT n/N] par étape, et à l'intérieur
        des lignes indentées et étiquetées. Le préfixe permet de retrouver
        les traces de l'agent au milieu de la sortie du script (les deux se
        mélangent dans la même console). Les textes longs (observation, code
        généré) sont tronqués -- l'objectif est de suivre la progression,
        pas de relire le prompt complet.

        Le résumé final distingue TROIS issues, là où stopped_reason en
        mélangeait plusieurs sous le même mot : réussi / épuisé (l'agent
        avait encore des idées mais plus de budget) / bloqué (l'agent
        n'avançait plus). La distinction est actionnable : « épuisé »
        invite à relancer avec plus d'itérations, « bloqué » indique que
        relancer à l'identique ne servirait à rien."""
        etapes = resultat.steps or []
        for i, etape in enumerate(etapes, start=1):
            self.log("[AGENT %d/%d] ─────────────────────────────" % (i, total_prevu), "sys")
            if etape.observation:
                self.log("    observé   : %s" % self._tronquer(etape.observation, 220), "info")
            if etape.action_parse_error:
                self.log("    action    : NON COMPRISE — %s" % etape.action_parse_error, "err")
            elif isinstance(etape.action, CodeAction):
                self.log("    action    : réécriture du script (%d caractères)"
                         % len(etape.action.code), "info")
            elif etape.action is not None:
                self.log("    action    : %s" % self._tronquer(str(etape.action), 160), "info")
            else:
                self.log("    action    : aucune action exploitable produite", "err")
            marque = "OK" if etape.verification_passed else "ÉCHEC"
            self.log("    vérifié   : %s — %s"
                     % (marque, self._tronquer(etape.verification_message, 220)),
                     "ok" if etape.verification_passed else "err")
            if etape.issue_diagnosis is not None:
                self.log("    diagnostic: sous-boucle de diagnostic déclenchée "
                         "(%d tentative(s))" % len(getattr(etape.issue_diagnosis, "attempts", []) or []),
                         "sys")
            if etape.decision is not None:
                self.log("    décision  : %s" % etape.decision.value, "sys")

        self.log("─" * 46, "sys")
        if resultat.succeeded:
            self.log("[AGENT] RÉUSSI — objectif atteint en %d étape(s). Le script "
                     "corrigé est dans l'éditeur." % len(etapes), "ok")
        elif resultat.stopped_reason in ("max_iterations", "timeout"):
            self.log("[AGENT] ÉPUISÉ — budget atteint (%s) après %d étape(s). "
                     "L'agent progressait encore : relancer peut aboutir."
                     % (resultat.stopped_reason, len(etapes)), "err")
        else:
            self.log("[AGENT] BLOQUÉ — %s après %d étape(s). Relancer à "
                     "l'identique ne changera rien ; corrigez la piste "
                     "indiquée ci-dessus à la main."
                     % (resultat.stopped_reason, len(etapes)), "err")

    @staticmethod
    def _tronquer(texte, limite: int) -> str:
        texte = str(texte or "").replace("\n", " ⏎ ").strip()
        return texte if len(texte) <= limite else texte[:limite] + " […]"

    def autonomous_fix_from_editor(self, max_iterations: int = 8):
        """Lance IAAgent.autonomous_fix sur le contenu de l'éditeur.

        THREAD DAEMON OBLIGATOIRE, ce n'est pas une précaution de confort :
        autonomous_fix enchaîne jusqu'à `max_iterations` appels réseau
        bloquants, chacun pouvant durer des dizaines de secondes sur un
        modèle de raisonnement. Exécuté sur le thread Tkinter, l'IDE
        paraîtrait planté pendant plusieurs minutes -- et le gestionnaire de
        fenêtres proposerait de tuer l'application.

        AVERTISSEMENT DE FIABILITÉ, affiché à l'utilisateur et pas seulement
        écrit ici : cette boucle n'a PAS été validée sur des cas réels
        variés. Elle est structurellement correcte (AgentLoop +
        IssueDiagnosisLoop sont testés unitairement) mais sa réussite dépend
        entièrement de la qualité du modèle branché derrière generate_fn.
        Un 7B distillé produit fréquemment du code hors format
        ```uvi ... ``` -- que _default_code_extractor ne reconnaît alors pas,
        ce qui donne une étape sans action exploitable. C'est pour ça que
        _log_agent_steps affiche « action : NON COMPRISE » explicitement au
        lieu de laisser croire à un échec du script."""
        generate_fn, nom_backend = self._generate_fn_disponible()
        if generate_fn is None:
            self.show_terminal()
            self.log(
                "[SHION_UNAVAILABLE] AUTO-CORRIGER a besoin d'un modèle de "
                "langage : SHION n'est pas configuré.", "err",
            )
            return
        if getattr(self, "_agent_fix_en_cours", False):
            self.log("Une correction autonome est déjà en cours.", "err")
            return
        if self.agent is None:
            return

        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.show_terminal()
            self.log("Éditeur vide : rien à corriger.", "err")
            return

        self.show_terminal()
        self._agent_fix_en_cours = True
        self.btn_auto_fix.config(state=tk.DISABLED, cursor="X_cursor")
        self.log("=== AGENT AUTONOME — DÉMARRAGE ===", "sys")
        self.log("Modèle : %s | budget : %d itérations" % (nom_backend, max_iterations), "info")
        self.log("Objectif : %s" % self.OBJECTIF_AUTO_FIX, "info")
        self.log("⚠ Capacité EXPÉRIMENTALE, non validée sur des cas réels variés — "
                 "relisez toujours le script proposé avant de l'exécuter.", "sys")
        self._set_status("🤖 Agent autonome en cours…")

        def _worker():
            try:
                resultat = self.agent.autonomous_fix(
                    goal_description=self.OBJECTIF_AUTO_FIX,
                    generate_fn=generate_fn,
                    initial_snippet=code,
                    max_iterations=max_iterations,
                )
                echec = None
            except Exception as e:
                # Filet volontairement large : autonomous_fix traverse
                # AgentLoop, IssueDiagnosisLoop, la VM et un client réseau.
                # Une exception non prévue dans un thread daemon
                # disparaîtrait sans trace, laissant le bouton grisé pour
                # toujours -- exactement le « échec silencieux » à ne pas
                # laisser passer.
                resultat, echec = None, e

            def _apply():
                self._agent_fix_en_cours = False
                self.btn_auto_fix.config(state=tk.NORMAL, cursor="hand2")
                self._set_status("Terminé.")
                if echec is not None:
                    self.log("[AGENT] INTERROMPU — %s : %s"
                             % (type(echec).__name__, echec), "err")
                    return
                self._log_agent_steps(resultat, max_iterations)
                self.log("=== AGENT AUTONOME — FIN ===", "sys")

            self._poster_ui(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def toggle_voice(self):
        if not HAS_TTS:
            self.log("Erreur : pyttsx3 n'est pas installé.", "err")
            return
        if self.agent:
            self.agent.voice_enabled = not self.agent.voice_enabled
            if self.agent.voice_enabled:
                self.btn_voice.config(text="🔊 IA VOIX", fg="#00FF00")
                self.log("IA : Synthèse vocale activée.", "sys")
            else:
                self.btn_voice.config(text="🔇 IA MUETTE", fg="#FF4444")
                self.log("IA : Synthèse vocale désactivée.", "sys")

    def request_input(self, var_name):
        """Appelée par ? depuis le THREAD D'EXÉCUTION du script.

        Bloque ce thread jusqu'à ce que l'utilisateur réponde, sans faire
        UN SEUL appel Tk depuis ce thread -- toute manipulation de widget
        est renvoyée au thread principal par _poster_ui (file d'attente
        vidée par la boucle Tk), et l'attente se fait sur un
        threading.Event.

        POURQUOI PAS root.wait_variable, qui était utilisé avant : c'est
        une primitive Tk, et l'appeler depuis un thread secondaire est
        exactement le genre de faute qui « marche souvent ». Un blocage
        aléatoire chez l'utilisateur est plus coûteux à diagnostiquer
        qu'une panne franche, parce qu'il n'est pas reproductible.

        Event et non Queue : on attend UNE valeur, une seule fois, et
        l'Event dit exactement ça. Une Queue suggérerait un flux."""
        self._saisie_evenement = threading.Event()
        self._saisie_resultat = {"valeur": ""}
        # _poster_ui et NON root.after : after() est un appel Tcl, donc
        # interdit depuis ce thread. C'est ce point précis qui faisait que
        # la barre de saisie n'apparaissait jamais et que le script restait
        # suspendu sans que rien ne s'affiche.
        self._poster_ui(lambda: self._ouvrir_saisie(var_name))

        # timeout de 10 minutes plutôt qu'une attente infinie : si la
        # fenêtre est fermée pendant l'attente, ce thread daemon resterait
        # sinon bloqué à jamais et empêcherait un arrêt propre.
        obtenu = self._saisie_evenement.wait(timeout=600)
        if not obtenu:
            self._poster_ui(self._fermer_saisie)
            return ""
        return self._saisie_resultat.get("valeur", "")

    def _ouvrir_saisie(self, var_name):
        """Affiche la barre de saisie. Thread principal uniquement."""
        self.is_waiting_input = True
        # Ramène aussi la vue CENTRALE sur l'éditeur (même raison que le
        # correctif de show_terminal juste au-dessus) : un script qui
        # dessine (CANEVAS affiché, cf. run_script/run_script_vm) puis
        # demande une saisie ne doit pas laisser l'invite invisible
        # derrière le canevas -- répondre à ?INPUT est plus urgent que
        # regarder un dessin à moitié terminé.
        self._show_view("editor")
        # Bascule sur un onglet où l'invite est LISIBLE : sur SORTIE ou
        # ERREURS, la ligne d'invite serait masquée par le filtre, et
        # l'utilisateur verrait un script figé sans savoir pourquoi. C'est
        # exactement le bug corrigé ici.
        if self._onglet_console_actif not in ("CONSOLE UVI", "TERMINAL"):
            self._selectionner_onglet_console("CONSOLE UVI")
        self.log("Le script demande une valeur pour « %s »." % var_name, "sys")
        self.etiquette_saisie.config(text="?  %s  =" % var_name)
        self.champ_saisie.delete(0, tk.END)
        # BUGFIX : l'ancienne version n'utilisait que before=barre_commande,
        # sans side=BOTTOM (raisonnement d'origine : "before= seul suffit").
        # Insuffisant en pratique -- barre_cmd et zone (la console,
        # expand=True) sont TOUTES DEUX empaquetées AVANT que ce script ne
        # tourne (voir _build_terminal) ; sans side=BOTTOM ici, ce widget
        # est carvé côté TOP et finit coincé entre la barre d'onglets et la
        # console plutôt qu'au-dessus de la ligne de commande -- et sur une
        # fenêtre courte, la zone déjà expand=True peut ne rien laisser du
        # tout. side=BOTTOM + after=barre_commande (et non before=) le place
        # dans la cavité BOTTOM juste au-dessus de barre_cmd (le dernier
        # empaqueté côté BOTTOM se retrouve juste AU-DESSUS du précédent,
        # pas en dessous) avec son espace garanti, quelle que soit la taille
        # de la fenêtre -- même mécanisme que le réordonnancement de
        # _build_terminal, voir son commentaire.
        self.barre_saisie.pack(side=tk.BOTTOM, fill=tk.X, after=self.barre_commande)
        self.champ_saisie.config(state=tk.NORMAL)
        self.champ_saisie.focus_set()
        self._set_status("En attente de votre saisie…")
        self.prompt_commande.config(text="⏸ ❯", fg=T.AMBRE)

    def valider_saisie(self, _event=None):
        """Handler du bouton VALIDER / touche Entrée. Thread principal."""
        if not self.is_waiting_input:
            return
        valeur = self.champ_saisie.get()
        self._saisie_resultat["valeur"] = valeur
        self.log("→ %s" % (valeur if valeur.strip() else "(vide)"), "info")
        self._fermer_saisie()
        evenement = self._saisie_evenement
        if evenement is not None:
            # Libéré EN DERNIER : le thread d'exécution repart aussitôt et
            # peut réécrire dans la console. Fermer la barre après aurait
            # produit un ordre d'affichage aléatoire.
            evenement.set()

    def _fermer_saisie(self):
        self.is_waiting_input = False
        try:
            self.barre_saisie.pack_forget()
            self.prompt_commande.config(text="UVI ❯", fg=T.CYAN)
            self._set_status("Exécution en cours…")
        except tk.TclError:
            pass

    def pump_events(self):
        """Contrat attendu par UVIEvaluator._pump_ui_events (et par lui
        seul). N'existe que pour donner UN nom stable à "laisse Tk
        respirer", au lieu de laisser l'évaluateur connaître self.root.

        Ce n'est pas une couche d'indirection gratuite : c'est ce qui
        rend l'évaluateur exécutable hors IDE (CLI, tests, CI) -- un hôte
        sans cette méthode voit simplement les boucles ne rien pomper,
        au lieu de lever AttributeError sur .root.

        Le risque thread-safety documenté ci-dessous (key_is_down) n'est
        NI aggravé NI corrigé par ce déplacement : c'est le même
        root.update(), appelé depuis le même thread de script, juste
        derrière un nom. Le corriger vraiment (queue + root.after)
        reste hors périmètre, et est désormais localisable en UN endroit
        au lieu de quatre."""
        self.root.update()

    def key_is_down(self, key_name: str) -> bool:
        """KEY.DOWN -- appelé depuis le thread du script UVI (celui lancé
        par run_script via threading.Thread(target=_run), même thread
        que celui qui exécute _exec_while/_exec_for). root.update() AVANT
        la lecture, pour que _keys_down reflète l'état réel du clavier au
        moment de CET appel plutôt que de dépendre du pompage "tous les
        20 tours" déjà présent dans _exec_while/_exec_for (pensé pour une
        boucle de calcul long, pas pour ~30 appels/seconde) -- un jeu
        appelant KEY.DOWN à chaque frame veut une fraîcheur garantie à
        CE moment précis, pas une coïncidence avec ce compteur de 20.

        Note honnête : appeler root.update() hors du thread principal
        Tkinter n'est pas rigoureusement garanti thread-safe par Tk. Ce
        n'est cependant pas une invention isolée pour KEY.* -- c'est
        EXACTEMENT le même pattern que self.ide.root.update() déjà
        présent dans _exec_while/_exec_for (ligne ~4366/4384), appelé
        depuis ce même thread de script. Introduire ici une convention
        différente (ex. passer par une queue + root.after) créerait une
        incohérence entre deux mécanismes très proches dans le même
        projet, pour un risque déjà accepté ailleurs -- non traité dans
        cette passe, cohérent avec l'existant plutôt qu'une réécriture
        risquée de _exec_while/_exec_for hors du périmètre demandé."""
        self.pump_events()
        key = str(key_name).strip().lower()
        with self._keys_lock:
            return key in self._keys_down

    def key_was_pressed(self, key_name: str) -> bool:
        """KEY.PRESSED -- front montant, CONSOMMÉ à la lecture (retiré de
        _keys_pressed_edge une fois lu) : un appui physique ne doit
        déclencher qu'UNE seule fois "vrai", pas à chaque frame tant que
        la touche reste enfoncée -- sans quoi KEY.PRESSED serait
        strictement équivalent à KEY.DOWN et n'aurait aucune raison
        d'exister séparément."""
        self.pump_events()
        key = str(key_name).strip().lower()
        with self._keys_lock:
            if key in self._keys_pressed_edge:
                self._keys_pressed_edge.discard(key)
                return True
            return False

    def log(self, text: str, level: str = "info"):
        prefixes = {"info": "", "ok": "[OK] ", "err": "[ERREUR] ", "sys": "[SYS] "}
        # Le vert ne signifie plus « UVI » (tout l'était en v8) mais
        # « succès » : il redevient porteur d'information. La sortie
        # ordinaire passe en gris-bleu neutre, ce qui laisse ressortir les
        # lignes qui comptent vraiment.
        colors = {"info": T.TEXTE, "ok": T.VERT, "err": T.ROUGE, "sys": T.TEXTE_FAIBLE}
        prefix = prefixes.get(level, "")
        color = colors.get(level, "#00FF00")
        text_str = str(text)
        if len(text_str) > MAX_DISPLAY_LENGTH:
            text_str = text_str[:MAX_DISPLAY_LENGTH] + f"... [{len(text_str)} chars au total]"
        full_msg = f"{prefix}{text_str}\n"
        self.console.tag_configure(level, foreground=color)
        self.console.insert(tk.END, full_msg, level)
        self.console.see(tk.END)
        lines = int(self.console.index('end-1c').split('.')[0])
        if lines > MAX_CONSOLE_LINES:
            self.console.delete("1.0", "201.0")
            self.console.insert("1.0", f"[SYS] Terminal purgé (limite {MAX_CONSOLE_LINES} lignes).\n", "sys")
        if level == "err":
            # ÉTAPE 7.2 : le `kind` typé de UVIError était construit
            # proprement par _emit_error puis... uniquement affiché entre
            # crochets. Toute l'information de typage servait à colorer une
            # ligne, jamais à aider. On la relit ici pour attacher une
            # action concrète quand il y en a une.
            #
            # Extraction par regex sur le message plutôt que par un
            # paramètre supplémentaire à log() : log() est appelée depuis
            # des dizaines d'endroits (moteur, agent, VM, IDE) et lui
            # ajouter un argument obligerait à toucher tous ces appels pour
            # un bénéfice nul sur 95% d'entre eux. Le format "[KIND] ..."
            # est déjà garanti par UVIError.__str__.
            #
            # Insertion DIRECTE dans la console, pas un self.log() récursif :
            # un appel récursif relancerait tout ce bloc (speak, error_flag,
            # _highlight_error_line) pour une simple ligne d'aide.
            self._selectionner_onglet_console(self._onglet_console_actif) \
                if getattr(self, "_onglets_console", None) else None
            correspondance = re.match(r"^\[([A-Z_]+)\]", text_str)
            if correspondance:
                suggestion = ERROR_HINTS.get(correspondance.group(1))
                if suggestion:
                    self.console.tag_configure("hint", foreground="#FFCC00")
                    self.console.insert(tk.END, "        " + suggestion + "\n", "hint")
                    self.console.see(tk.END)
            if getattr(self, 'agent', None):
                self.agent.speak(text_str)
            self.engine.error_flag = True
            if self.engine.is_running and self.engine.current_run_line > 0:
                self._highlight_error_line(self.engine.current_run_line)

    def _rafraichir_panneaux(self):
        """Un seul point d'entrée pour remettre à jour les panneaux vivants.

        Replanifié tant que le script tourne : une boucle de jeu fait bouger
        les variables en continu, et un panneau figé sur l'état de départ
        serait pire qu'un panneau vide -- il afficherait des valeurs fausses
        avec l'assurance de valeurs justes."""
        self.rafraichir_variables()
        self.rafraichir_memoire()
        if getattr(self.engine, "is_running", False):
            self.root.after(700, self._rafraichir_panneaux)

    def _clear_console(self):
        self.console.delete("1.0", tk.END)
        self.log("Console effacée.", "sys")

    def basculer_execution(self):
        """Le bouton principal est un INTERRUPTEUR : EXÉCUTER quand rien ne
        tourne, ARRÊTER pendant l'exécution.

        Un seul bouton plutôt que deux : les deux actions sont mutuellement
        exclusives, et un bouton ARRÊTER grisé occuperait de la place pour
        ne rien dire 99 % du temps. L'utilisateur regarde toujours au même
        endroit."""
        if self.engine.is_running:
            self.arreter_script()
        else:
            self.run_script()

    def arreter_script(self):
        """Demande l'arrêt coopératif du script en cours."""
        if not self.engine.is_running:
            return
        self.engine.stop_requested = True
        self._set_status("Arrêt demandé…")
        self.log("⏹ Arrêt demandé — le script s'arrêtera à la prochaine "
                 "instruction.", "sys")
        # Une saisie en attente doit être débloquée, sinon le thread reste
        # suspendu sur son Event et l'arrêt n'a jamais lieu.
        if getattr(self, "is_waiting_input", False):
            self._saisie_resultat["valeur"] = ""
            self._fermer_saisie()
            if self._saisie_evenement is not None:
                self._saisie_evenement.set()

    def _mode_execution(self, actif: bool):
        """Bascule l'apparence du bouton principal."""
        try:
            if actif:
                self.btn_run.config(text="⏹  ARRÊTER", bg="#B91C1C",
                                    activebackground=T.ROUGE,
                                    state=tk.NORMAL, cursor="hand2")
            else:
                self.btn_run.config(text="▶  EXÉCUTER", bg=T.BLEU,
                                    activebackground=T.BLEU_VIF,
                                    state=tk.NORMAL, cursor="hand2")
        except tk.TclError:
            pass

    def run_script(self):
        if self.engine.is_running:
            return
        self.engine.stop_requested = False
        code = self.editor.get("1.0", tk.END)
        lines = code.splitlines()
        if not lines or lines[0].strip() != "@UVI":
            self.show_terminal()
            self.log("Signature @UVI absente.", "err")
            return
        # Le bouton devient ARRÊTER au lieu d'être grisé : c'est
        # précisément pendant l'exécution qu'on en a besoin.
        self._mode_execution(True)
        self.cmd_input.config(state=tk.DISABLED)
        self._debut_execution = time.time()
        # Même bascule automatique que run_script_vm : si le script
        # contient au moins un DRAW.*, on affiche le canevas plutôt que le
        # terminal -- détecté ici sur l'AST (le tree-walker n'a pas de
        # bytecode à inspecter comme la VM) AVANT de lancer le thread
        # d'exécution, donc sans risque de course avec _exec_draw.
        try:
            ast_nodes_preview = self.engine.parser.parse_program(lines[1:])
            has_drawing = any(isinstance(n, DrawNode) for n in ast_nodes_preview)
        except Exception:
            has_drawing = False
        self.show_canvas() if has_drawing else self.show_terminal()
        ts = datetime.now().strftime("%H:%M:%S")
        # Panneaux VARIABLES et MÉMOIRE remis à jour APRÈS l'exécution :
        # planifié par root.after plutôt qu'appelé ici, parce que
        # run_script lance le script dans un thread et que ces panneaux
        # doivent refléter l'état FINAL, pas l'état d'avant le départ.
        self.root.after(120, self._rafraichir_panneaux)
        # ÉTAPE 7.5 : la coexistence de deux moteurs était expliquée
        # uniquement dans les commentaires de run_script_vm -- c'est-à-dire
        # nulle part, pour quelqu'un qui utilise l'IDE sans lire son code
        # source. Affiché une fois par session, au moment où la question se
        # pose réellement (le premier clic), plutôt que noyé dans une doc.
        self._conseil_unique(
            "moteur_treewalker",
            "▶ EXÉCUTER utilise le moteur COMPLET (tree-walker) : il gère tout "
            "le langage, IMPORT et pipes | compris. C'est le bon choix par "
            "défaut. ⚡ EXÉCUTER (VM) compile en bytecode et va plus vite, mais "
            "ne couvre pas encore IMPORT ni les pipes.",
        )
        self.log(f"=== SÉQUENCE D'EXÉCUTION — {ts} ===", "sys")
        self._set_status("Exécution en cours…")

        def _run():
            self.engine.run_sequence(lines[1:])
            self._poster_ui(self._on_script_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_script_finished(self):
        duree = time.time() - getattr(self, "_debut_execution", time.time())
        interrompu = getattr(self.engine, "stop_requested", False)
        self.log("=== FIN DE LA SÉQUENCE — %.2f s ===" % duree, "sys")
        # L'auto-réparation ne se déclenche PAS sur une interruption
        # volontaire : le script n'a rien de cassé, l'utilisateur a
        # simplement décidé d'arrêter. La lancer ici ferait travailler
        # l'agent (et consommer du réseau) pour rien.
        if self.engine.error_flag and self.agent and not interrompu:
            self.agent.auto_repair()
        self._set_status("Interrompu (%.2f s)." % duree if interrompu
                         else "Terminé en %.2f s." % duree)
        if interrompu:
            pass  # arrêt volontaire : aucune notification, l'utilisateur sait déjà
        elif self.engine.error_flag:
            self._empiler_toast("Le script s'est terminé en erreur.", "err")
        elif duree > 1.5:
            self._empiler_toast("Script terminé (%.1f s)." % duree, "ok")
        # CORRIGÉ AU PASSAGE : ce bloc remettait le bouton en #00FF00 sur
        # #000000 -- les couleurs de l'ancien thème vert v8. Après la
        # première exécution, le bouton principal virait donc au vert fluo
        # au milieu d'une interface bleue. Résidu visible du changement de
        # thème, que seule une exécution réelle faisait apparaître.
        self._mode_execution(False)
        self.engine.stop_requested = False
        self.cmd_input.config(state=tk.NORMAL)
        self._rafraichir_panneaux()

    def run_script_vm(self):
        """Exécute le script courant via la VM Phase 3 (compiler_v2 +
        uvi_vm) au lieu du tree-walker (UVIEvaluator). Séparé de
        run_script() plutôt que substitué en place : la VM ne couvre pas
        encore ImportNode/PipeNode (cf. limites documentées dans
        uvi_vm.py) -- les deux moteurs coexistent pendant la période de
        validation de la VM, l'utilisateur choisit explicitement lequel
        utiliser via le bouton correspondant."""
        if not HAS_VM:
            self.log("VM indisponible : compiler_v2.py / uvi_vm.py introuvables.", "err")
            return
        if self.engine.is_running:
            return
        code = self.editor.get("1.0", tk.END)
        lines = code.splitlines()
        if not lines or lines[0].strip() != "@UVI":
            self.show_terminal()
            self.log("Signature @UVI absente.", "err")
            return
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            ast_nodes = self.engine.parser.parse_program(lines[1:])
            compiler = UVIBytecodeCompilerV2()
            bytecode = compiler.compile_program(ast_nodes)
        except BytecodeError as e:
            self.show_terminal()
            self.log(f"Erreur de compilation VM : {e}", "err")
            return

        # Bascule vers l'onglet CANVAS si le script dessine effectivement
        # quelque chose (au moins un CALL_BUILTIN DRAW_*) -- sinon on garde
        # le comportement existant (terminal), un script purement textuel
        # ne doit pas surprendre l'utilisateur en changeant de vue.
        has_drawing = any(
            i["opcode"] == "CALL_BUILTIN" and i["operands"] and str(i["operands"][0]).startswith("DRAW_")
            for i in bytecode["instructions"]
        )
        if has_drawing:
            self.show_canvas()
        else:
            self.show_terminal()
        self.root.after(120, self._rafraichir_panneaux)
        self._conseil_unique(
            "moteur_vm",
            "⚡ EXÉCUTER (VM) compile le script en bytecode : plus rapide sur les "
            "boucles longues, mais IMPORT et les pipes | ne sont pas encore "
            "couverts et lèveront une erreur de compilation. Si un script "
            "refuse de compiler ici, réessayez avec ▶ EXÉCUTER. Faire tourner "
            "le même script dans les deux moteurs est aussi la meilleure façon "
            "de vérifier qu'ils restent d'accord.",
        )
        self.log(f"=== EXÉCUTION VM — {ts} ===", "sys")

        def shion_ask_fn(prompt: str) -> str:
            if not HAS_SHION or self.engine.shion_client is None:
                raise RuntimeError("SHION.ASK indisponible dans ce contexte VM.")
            return self.engine.shion_client.complete(prompt)

        vm = UVIVirtualMachine(
            vfs=self.vfs,
            shion_ask_fn=shion_ask_fn if HAS_SHION else None,
            input_fn=lambda var: self.request_input(var),
            draw_fn=self.canvas_draw,
            key_down_fn=self.key_is_down,
            key_pressed_fn=self.key_was_pressed,
        )

        # BUGFIX (gel de l'IDE, + deadlock sur ?INPUT) : vm.run() était
        # appelée ICI, directement sur le thread principal Tkinter. C'est
        # une boucle Python bloquante, qui en plus peut attendre un réseau
        # (SHION.ASK -> shion_ask_fn -> DEEPSEEK_REQUEST_TIMEOUT_S=180s) ou
        # une saisie utilisateur (?INPUT -> request_input). request_input
        # POSTE l'ouverture de sa barre de saisie via _poster_ui, qui n'est
        # vidée que par _pomper_file_ui -- qui elle-même ne tourne que si
        # la boucle d'événements Tk est libre. Appelée depuis LE thread Tk,
        # elle attendait donc (.wait(timeout=600)) une réponse qui ne
        # pouvait jamais arriver : deadlock de 10 minutes garanti sur tout
        # script VM utilisant ?, et gel simple (sans deadlock) sur tout
        # script sans ? mais un peu long ou utilisant SHION.ASK.
        #
        # Correctif : même schéma que run_script (tree-walker) -- vm.run()
        # part dans un thread daemon, is_running sert de verrou de
        # ré-entrance (comme pour run_sequence), et tout ce qui touche
        # Tkinter après coup (log, statut, panneaux, bouton) repasse par
        # _poster_ui via _on_vm_finished, jamais appelé directement depuis
        # ce thread. draw_fn (canvas_draw) et input_fn (request_input)
        # passent déjà par _poster_ui en interne -- ce sont eux qui
        # deviennent enfin SÛRS une fois appelés depuis un vrai thread
        # secondaire, comme ils étaient conçus pour l'être.
        #
        # LIMITE ASSUMÉE (non traitée ici) : UVIVirtualMachine.run() ne lit
        # stop_requested nulle part dans sa boucle -- le bouton ARRÊTER
        # reste donc sans effet sur un script VM en cours, contrairement au
        # tree-walker. C'était déjà vrai avant ce correctif (juste invisible,
        # l'IDE entière étant gelée) ; le rendre coopératif demande de
        # toucher la boucle interne de la VM, hors périmètre de ce correctif
        # de thread.
        self.engine.is_running = True
        self._mode_execution(True)
        self.cmd_input.config(state=tk.DISABLED)
        self._debut_execution_vm = time.time()

        def _run():
            try:
                resultat = vm.run(bytecode)
            except Exception as e:
                # `except ... as e` supprime automatiquement `e` à la sortie
                # de ce bloc (voir la même remarque, déjà faite, dans
                # _shion_worker) -- la lambda ci-dessous n'est appelée que
                # plus tard, via _poster_ui, APRÈS cette suppression. On
                # rebinde donc l'exception à un nom ordinaire AVANT de
                # construire la closure, pour ne pas répéter le même bug.
                exc = e
                self._poster_ui(lambda: self._on_vm_finished(None, exc))
                return
            self._poster_ui(lambda: self._on_vm_finished(resultat, None))

        threading.Thread(target=_run, daemon=True).start()

    def _on_vm_finished(self, result, exc):
        """Callback de fin d'exécution VM, TOUJOURS invoqué sur le thread
        principal via _poster_ui (voir run_script_vm) -- jamais appelé
        directement depuis le thread worker."""
        duree = time.time() - getattr(self, "_debut_execution_vm", time.time())
        self.engine.is_running = False
        self._mode_execution(False)
        self.cmd_input.config(state=tk.NORMAL)
        if exc is not None:
            self.log(f"Erreur VM inattendue : {type(exc).__name__} : {exc}", "err")
            self._empiler_toast("Exécution VM interrompue par une erreur.", "err")
        else:
            for line in result.output:
                self.log(str(line))
            if result.trapped:
                self.log(f"Erreur VM [{result.trapped}] : {result.trap_message}", "err")
                self._empiler_toast("Erreur VM : %s" % result.trapped, "err")
            elif duree > 1.5:
                # Notification seulement si l'exécution a duré assez pour
                # que l'utilisateur ait plausiblement fait autre chose
                # entre-temps -- pas de bruit sur un script instantané.
                self._empiler_toast("Script terminé (VM, %.1f s)." % duree, "ok")
        self.log("=== FIN D'EXÉCUTION VM — %.2f s ===" % duree, "sys")
        self._set_status("Terminé (VM, %.2f s)." % duree)
        self._rafraichir_panneaux()

    def run_command(self, event=None):
        # La saisie de script ne passe PLUS par ici : elle a sa propre
        # barre (voir request_input). Ce champ redevient ce qu'il prétend
        # être depuis toujours, un REPL -- un champ, un rôle.
        if self.is_waiting_input:
            # Filet : si l'utilisateur tape ici par réflexe pendant qu'une
            # saisie est demandée, on redirige plutôt que d'ignorer en
            # silence. Ignorer donnerait l'impression d'un clavier mort.
            texte = self.cmd_input.get()
            if texte.strip():
                self.champ_saisie.delete(0, tk.END)
                self.champ_saisie.insert(0, texte)
                self.cmd_input.delete(0, tk.END)
                self.valider_saisie()
            else:
                self.champ_saisie.focus_set()
            return
        if self.engine.is_running:
            return
        cmd = self.cmd_input.get().strip()
        if not cmd:
            return
        self.cmd_history.append(cmd)
        self.history_index = len(self.cmd_history)
        self.log(f"UVI> {cmd}", "sys")
        self.cmd_input.delete(0, tk.END)

        # BUGFIX (gel de l'IDE) : une commande REPL peut contenir !ERIS(...),
        # évalué en EXPRESSION par _eval_expr_or_eris -- lequel appelle
        # client.complete(...) de façon SYNCHRONE (requête réseau, jusqu'à
        # DEEPSEEK_REQUEST_TIMEOUT_S=180s si SHION répond à ERIS en
        # priorité, cf. llm_client_prioritaire). SHION.ASK en tant
        # qu'INSTRUCTION dédiée est déjà non-bloquant (_exec_shion, thread
        # daemon) ; ce chemin-ci, tapé directement dans la console, ne
        # l'était pas. Même correctif que run_script/run_script_vm :
        # is_running sert de verrou de ré-entrance partagé entre les trois
        # points d'entrée (ils touchent le même moteur, mutable).
        self.engine.is_running = True

        def _run():
            try:
                self.engine.run_single_command(cmd)
            finally:
                self.engine.is_running = False
                self._poster_ui(self._rafraichir_panneaux)

        threading.Thread(target=_run, daemon=True).start()

    def _history_up(self, event=None):
        if self.history_index > 0:
            self.history_index -= 1
            self._fill_input(self.cmd_history[self.history_index])

    def _history_down(self, event=None):
        if self.history_index < len(self.cmd_history) - 1:
            self.history_index += 1
            self._fill_input(self.cmd_history[self.history_index])
        else:
            self.history_index = len(self.cmd_history)
            self.cmd_input.delete(0, tk.END)

    def _fill_input(self, text: str):
        if self.cmd_input['state'] == tk.DISABLED:
            return
        self.cmd_input.delete(0, tk.END)
        self.cmd_input.insert(0, text)

    def _set_status(self, msg: str):
        """Garde son nom et sa signature (13 appels ailleurs). Le voyant
        change de couleur selon le contenu -- une information de plus sans
        rien demander aux appelants existants."""
        self.statusbar.config(text=" %s" % msg)
        voyant = getattr(self, "voyant_etat", None)
        if voyant is None:
            return
        minuscule = str(msg).lower()
        if any(mot in minuscule for mot in ("erreur", "échec", "echec", "interrompu")):
            couleur, teinte = T.ROUGE, T.ROUGE
        elif any(mot in minuscule for mot in ("cours", "exécution", "execution", "agent")):
            couleur, teinte = T.AMBRE, T.AMBRE
        else:
            couleur, teinte = T.VERT, T.VERT
        try:
            self.statusbar.config(fg=teinte)
        except tk.TclError:
            pass
        # Le voyant ne se contente plus d'une couleur fixe : il RESPIRE --
        # un battement lent et discret au repos, plus rapide pendant une
        # exécution -- plutôt que le point statique des barres d'état
        # habituelles. C'est délibérément subtil (l'amplitude reste faible,
        # voir _animer_voyant) : l'objectif est de donner une sensation
        # continue que l'atelier est "vivant", pas de distraire l'œil.
        self._animer_voyant(couleur)

    def _animer_voyant(self, couleur_base: str):
        """(Re)démarre le pouls du voyant d'état autour de `couleur_base`.

        Un seul cycle d'animation à la fois : l'identifiant `after()`
        précédent est annulé avant d'en programmer un nouveau, sinon
        chaque appel à _set_status empilerait une boucle .after()
        supplémentaire -- une fuite lente mais réelle sur une session
        longue avec beaucoup de changements de statut."""
        voyant = getattr(self, "voyant_etat", None)
        if voyant is None:
            return
        ancien = getattr(self, "_voyant_anim_id", None)
        if ancien is not None:
            try:
                self.root.after_cancel(ancien)
            except tk.TclError:
                pass
        self._voyant_anim_phase = 0.0
        # Battement rapide pendant une exécution (retour visuel immédiat
        # que "quelque chose tourne"), lent et discret au repos.
        rapide = getattr(self.engine, "is_running", False)
        pas = 0.09 if rapide else 0.025
        intervalle_ms = 45
        import math  # local à la méthode, pas au cycle -- cf. la même convention pour BINOP/POW dans la VM

        def _cycle():
            try:
                self._voyant_anim_phase = (self._voyant_anim_phase + pas) % (2 * 3.14159265)
                # (sin+1)/2 -> [0,1], mais borné à [0.15, 0.55] : le voyant
                # ne s'éteint jamais complètement et ne sature jamais à
                # fond, pour rester un battement plutôt qu'un clignotement.
                intensite = 0.15 + 0.40 * (0.5 * (1 + math.sin(self._voyant_anim_phase)))
                teinte = _melanger_couleurs(T.PANNEAU, couleur_base, intensite + 0.45)
                voyant.delete("all")
                voyant.create_oval(1, 1, 9, 9, fill=teinte, outline="")
                self._voyant_anim_id = self.root.after(intervalle_ms, _cycle)
            except tk.TclError:
                pass  # fenêtre fermée pendant l'animation

        _cycle()

    def _on_close(self):
        """Confirme avant de fermer si du travail n'est pas enregistré.

        La version précédente fermait sans rien demander : un clic sur la
        croix effaçait un script non sauvegardé, sans recours. C'est le
        genre de perte qu'on ne pardonne pas à un outil."""
        if getattr(self, "modifie", False):
            reponse = messagebox.askyesnocancel(
                "Quitter UVI",
                "Le fichier « %s » contient des modifications non "
                "enregistrées.\n\nSauvegarder avant de quitter ?"
                % (self.fichier_courant or "sans_titre.uvi"))
            if reponse is None:
                return  # Annuler : on ne ferme pas
            if reponse:
                self.save_to_disk()
                if self.modifie:
                    return  # la sauvegarde a échoué ou été annulée
        # Un script encore en cours doit être prié de s'arrêter : son
        # thread est daemon, donc il mourra de toute façon, mais lui
        # demander proprement évite qu'il écrive dans des widgets détruits
        # pendant la fermeture.
        if getattr(self.engine, "is_running", False):
            self.engine.stop_requested = True
        self.root.destroy()

    def start(self):
        self.root.mainloop()

    def _on_key_release_handler(self, event=None):
        self._update_line_numbers(event)
        if not getattr(self, 'agent', None):
            return
        try:
            if event and getattr(event, 'keysym', '') in ["space", "Return", "BackSpace", "Tab"]:
                self.statusbar.config(text=" Prêt.", fg="#444444")
                self.current_suggestion = None
                if event.keysym in ["space", "Return", "Tab"]:
                    return
            cursor_index = self.editor.index(tk.INSERT)
            line, col = cursor_index.split('.')
            line_text = self.editor.get(f"{line}.0", cursor_index)
            if not line_text or line_text[-1] in [" ", "\t", "\n"]:
                self.statusbar.config(text=" Prêt.", fg="#444444")
                self.current_suggestion = None
                return
            words = line_text.split()
            if words:
                last_word = words[-1]
                suggestion = self.agent.get_auto_suggestion(last_word)
                if suggestion:
                    self.statusbar.config(
                        text=f" 💡 IA : Appuyez sur [Tab] ou [Ctrl+Espace] pour insérer '{suggestion}'",
                        fg="#FFFF00"
                    )
                    self.current_suggestion = (suggestion, last_word)
                else:
                    self.statusbar.config(text=" Prêt.", fg="#444444")
                    self.current_suggestion = None
        except Exception:
            pass

    def _insert_suggestion(self, event=None):
        if getattr(self, 'current_suggestion', None):
            suggestion, last_word = self.current_suggestion
            cursor_index = self.editor.index(tk.INSERT)
            start_index = f"{cursor_index} - {len(last_word)} chars"
            self.editor.delete(start_index, cursor_index)
            self.editor.insert(start_index, suggestion)
            self.statusbar.config(text=" Prêt.", fg="#444444")
            self.current_suggestion = None
            return "break"
        return None


if __name__ == "__main__":
    app = UVIIDE()
    app.start()
