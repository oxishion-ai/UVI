"""Configuration pytest partagée pour toute la suite tests/.

Ce fichier fait deux choses :

1. Met la racine du dépôt (parent de tests/) sur sys.path, pour que
   `import uvi_core` fonctionne même si pytest est lancé depuis un autre
   répertoire ou via un runner qui ne préconfigure pas sys.path lui-même.
   uvi_core.py n'est pas un package installé (pas de setup.py/pyproject
   avec un point d'entrée) -- c'est un unique fichier à la racine du
   dépôt, donc la manipulation manuelle ci-dessous est le moyen le plus
   direct de le rendre importable depuis tests/.

2. Fournit des petits utilitaires réutilisés par (presque) tous les
   fichiers de test : lancer un script UVI dans le tree-walker
   (HeadlessHost + UVIEngine) ou dans la VM bytecode (compile_and_run),
   avec une InMemoryVFS fraîche à chaque appel pour qu'aucun test
   n'écrive jamais sur le vrai dossier UVI_Storage/ du dépôt (contrainte
   explicite de la tâche : tout artefact sur disque doit être nettoyé,
   et le plus simple pour ne rien salir est de ne jamais toucher au
   disque réel du tout, sauf dans les quelques tests MODULE qui ont
   BESOIN d'un vrai fichier -- ceux-là utilisent la fixture tmp_path de
   pytest, jamais UVI_Storage/).

Ces utilitaires sont volontairement de simples fonctions (pas des
fixtures) exposées au niveau module : elles n'ont besoin d'aucun état
géré par pytest, et les importer directement (`from conftest import
run_tw, run_vm`) depuis un fichier de test voisin est plus lisible que de
les redemander en paramètre de chaque fonction de test. C'est un usage
courant et supporté de conftest.py (il est déjà sur sys.path pour tout
module du même répertoire, pytest l'y place lui-même)."""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import uvi_core as U  # noqa: E402


# -----------------------------------------------------------------------
# Constructeurs de base, toujours frais (aucun état partagé entre appels
# -- chaque test doit démarrer d'un moteur/VM vierge, jamais réutiliser
# une instance d'un test précédent).
# -----------------------------------------------------------------------

def new_parser() -> "U.UVIParser":
    return U.UVIParser(U.UVILexer())


def new_compiler() -> "U.UVIBytecodeCompilerV2":
    return U.UVIBytecodeCompilerV2()


def _as_lines(source):
    if isinstance(source, str):
        return source.splitlines()
    return list(source)


def run_tw(source, input_fn=None, echo=False):
    """Exécute `source` (str multi-lignes ou liste de lignes) dans le
    tree-walker (UVIEngine) via HeadlessHost -- exactement le motif
    utilisé par UVIIDE._lancer_tests (voir uvi_core.py ~11295-11298).

    Retourne (hote, moteur) : hote.sortie/hote.journal/hote.erreurs pour
    inspecter la sortie, moteur pour inspecter l'état interne
    (error_flag, stop_requested, call_depth, memoire, etc)."""
    hote = U.HeadlessHost(input_fn=input_fn, echo=echo)
    moteur = U.UVIEngine(hote, U.InMemoryVFS())
    moteur.run_sequence(_as_lines(source))
    return hote, moteur


def run_vm(source, **vm_kwargs) -> "U.VMResult":
    """Exécute `source` dans la VM bytecode via compile_and_run --
    exactement le motif utilisé par UVIIDE._lancer_tests (voir
    uvi_core.py ~11302-11304). Une InMemoryVFS fraîche est fournie par
    défaut (jamais le vrai VFSManager) sauf si l'appelant en passe une
    explicitement via vm_kwargs['vfs'].

    Peut lever BytecodeError (erreur de COMPILATION, ex. ImportNode/
    PipeNode/UnknownNode -- limite documentée du compilateur, voir
    uvi_core.py ~1773-1776) ou UVIRuntimeTrap dans de rares cas non
    couverts par le try/except interne de run() -- volontairement laissé
    remonter ici plutôt qu'avalé, pour que les tests qui s'attendent à
    une compilation propre échouent bruyamment si ce n'est pas le cas."""
    vm_kwargs.setdefault("vfs", U.InMemoryVFS())
    p = new_parser()
    return U.compile_and_run(_as_lines(source), p, new_compiler(), **vm_kwargs)


def vm_output_as_str(result: "U.VMResult"):
    """Normalise VMResult.output en liste de str, exactement comme le
    fait UVIIDE._lancer_tests avant de comparer à hote.sortie (voir
    uvi_core.py ~11305) -- un PRINT de valeur non-str (bool, int, float)
    pousse la valeur Python brute dans VMResult.output, jamais une str
    pré-formatée par la VM elle-même."""
    return [str(x) for x in result.output]


def assert_engines_agree(source, **vm_kwargs):
    """Lance `source` dans les deux moteurs et affirme l'égalité de
    sortie observable -- le coeur de la propriété documentée comme "la
    plus utile du projet" (docstring de _lancer_tests). Retourne
    (hote, moteur, vm_result) pour des assertions supplémentaires par
    l'appelant."""
    hote, moteur = run_tw(source)
    vm_result = run_vm(source, **vm_kwargs)
    assert hote.sortie == vm_output_as_str(vm_result), (
        f"Divergence tree-walker/VM pour :\n{source}\n"
        f"tree-walker sortie = {hote.sortie!r}\n"
        f"VM sortie          = {vm_output_as_str(vm_result)!r}\n"
        f"tree-walker erreurs = {hote.erreurs!r}\n"
        f"VM trapped = {vm_result.trapped!r} ({vm_result.trap_message!r})"
    )
    return hote, moteur, vm_result
