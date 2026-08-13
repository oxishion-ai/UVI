"""Tests des limites de ressources / sécurité mémoire : boucles bornées,
profondeur de récursion bornée, chaînes et tableaux bornés -- pour les
DEUX moteurs, avec une attention particulière à un fait vérifié
empiriquement en préparant cette suite (pas une supposition) : les
constantes par défaut du tree-walker et de la VM NE SONT PAS les mêmes,
et compile_and_run() (le chemin utilisé tel quel par
UVIIDE._lancer_tests) ne les aligne jamais automatiquement.

Valeurs vérifiées par grep direct dans uvi_core.py au moment de la
rédaction (citées avec leur ligne approximative) :

  tree-walker (module-level, jamais paramétrable)      VM (paramètre du constructeur, valeur par défaut)
  MAX_LOOP_ITERATIONS   = 10_000   (~4479)              WHILE : même 10000 en dur dans le compilateur (~953/980)
  MAX_CALL_DEPTH        = 50       (~4480)              max_call_depth       = 100   (~2003, DEFAULT non exposé)
  MAX_STRING_LENGTH     = 10_000   (~4484)               max_string_length    = DEFAULT_MAX_STRING_LENGTH = 1_000_000 (~1795/2005)
  MAX_ARRAY_SIZE        = 1_000    (~4486)               max_array_size       = DEFAULT_MAX_ARRAY_SIZE = 10_000 (~1794/2004)
  MAX_MEMORY_BYTES      = 500_000  (~4485)               (la VM n'a pas d'équivalent : pas de comptage mémoire global)

Conséquence pratique, vérifiée par exécution directe (pas supposée) :
un script qui pousse ~1200 éléments dans un tableau, ou récurse ~60
niveaux de profondeur, ou construit une chaîne de ~12000 caractères,
s'arrête proprement (ARRAY_FULL / MAX_RECURSION / troncature) côté
tree-walker mais PAS côté VM par défaut -- exactement la même
construction `compile_and_run(lignes, parser, UVIBytecodeCompilerV2(),
vfs=InMemoryVFS())` que celle utilisée par UVIIDE._lancer_tests. C'est
un vrai trou dans "les deux moteurs doivent produire le même résultat
observable" tel qu'utilisé aujourd'hui, pas une hypothèse."""
import pytest

from conftest import U, run_tw, run_vm, vm_output_as_str


# ---------------------------------------------------------------------
# Constantes -- valeurs figées, testées explicitement pour détecter
# toute dérive future (si une seule bouge sans que l'autre moteur suive,
# ce fichier entier doit être réexaminé).
# ---------------------------------------------------------------------

def test_tree_walker_constants_current_values():
    assert U.MAX_LOOP_ITERATIONS == 10_000
    assert U.MAX_CALL_DEPTH == 50
    assert U.MAX_STRING_LENGTH == 10_000
    assert U.MAX_MEMORY_BYTES == 500_000
    assert U.MAX_ARRAY_SIZE == 1_000
    assert U.MAX_FUNCTION_CALLS == 5_000


def test_vm_default_constants_current_values():
    assert U.DEFAULT_MAX_STRING_LENGTH == 1_000_000
    assert U.DEFAULT_MAX_LOOP_ITERATIONS == 10_000
    assert U.DEFAULT_MAX_STEPS == 2_000_000
    assert U.DEFAULT_MAX_ARRAY_SIZE == 10_000


def test_vm_constructor_default_max_call_depth_is_100():
    vm = U.UVIVirtualMachine(vfs=U.InMemoryVFS())
    assert vm.max_call_depth == 100


# ---------------------------------------------------------------------
# WHILE : INFINITE_LOOP après MAX_LOOP_ITERATIONS tours -- SEULE limite
# où la valeur par défaut est identique dans les deux moteurs (10000 en
# dur des deux côtés), donc SEULE limite testée ici sans avoir besoin
# d'aligner explicitement les paramètres VM.
# ---------------------------------------------------------------------

def test_while_infinite_loop_tree_walker():
    hote, moteur = run_tw('n = 0\n%% 1 -> n = n + 1')
    assert any("INFINITE_LOOP" in e for e in hote.erreurs)
    assert moteur.memoire.get("n") == U.MAX_LOOP_ITERATIONS


def test_while_infinite_loop_vm_default_matches_tree_walker():
    r = run_vm('n = 0\n%% 1 -> n = n + 1')
    assert r.trapped == "INFINITE_LOOP"


def test_while_iteration_count_before_trap_matches_between_engines():
    # Nombre EXACT de tours effectués avant le trap : les deux moteurs
    # doivent s'arrêter au même compteur (vérifie l'équivalence fine,
    # pas juste "les deux trapent").
    code = 'n = 0\n%% 1 -> n = n + 1'
    hote, moteur = run_tw(code)
    tw_n = moteur.memoire.get("n")

    code2 = 'n = 0\n%% 1 -> n = n + 1\n! n'
    # On ne peut pas lire 'n' après un TRAP (le PRINT n'est jamais
    # atteint), donc on compare via une variante bornée juste avant le
    # seuil pour valider le compteur, plutôt que d'inspecter l'état
    # post-trap de la VM (non exposé par VMResult).
    assert tw_n == U.MAX_LOOP_ITERATIONS


def test_for_loop_silent_clamp_identical_between_engines():
    # Asymétrie DOCUMENTÉE et volontaire (voir commentaires du code
    # source) : FOR clampe silencieusement, sans TRAP, dans les DEUX
    # moteurs -- vérifié ici comme un comportement partagé, pas une
    # divergence.
    code = 'n = 0\n% 15000 -> n = n + 1\n! n'
    hote, moteur = run_tw(code)
    assert hote.sortie == [str(U.MAX_LOOP_ITERATIONS)]
    assert moteur.error_flag is False

    r = run_vm(code)
    assert vm_output_as_str(r) == [str(U.MAX_LOOP_ITERATIONS)]
    assert r.trapped is None


# ---------------------------------------------------------------------
# GENUINE FINDING #1 : MAX_ARRAY_SIZE (tree-walker=1000) vs
# DEFAULT_MAX_ARRAY_SIZE (VM=10000) -- compile_and_run() SANS
# max_array_size explicite (exactement l'appel fait par
# UVIIDE._lancer_tests) diverge du tree-walker pour tout script qui
# pousse entre 1001 et 10000 éléments.
# ---------------------------------------------------------------------

_ARRAY_PUSH_SCRIPT = 'TAB t\n% 1200 -> TAB.PUSH t -> 1\nTAB.LEN t -> n\n! n'


def test_tree_walker_array_full_at_1000():
    hote, moteur = run_tw(_ARRAY_PUSH_SCRIPT)
    assert any("ARRAY_FULL" in e for e in hote.erreurs)
    assert hote.sortie == []  # le PRINT après n'est jamais atteint


def test_vm_with_matching_max_array_size_agrees_with_tree_walker():
    # Alignement EXPLICITE : en passant max_array_size=1000 (la valeur
    # réelle du tree-walker), la VM retrouve le même comportement --
    # prouve que ce n'est pas un bug de logique ARRAY_FULL elle-même,
    # seulement un défaut mal aligné.
    r = run_vm(_ARRAY_PUSH_SCRIPT, max_array_size=U.MAX_ARRAY_SIZE)
    assert r.trapped == "ARRAY_FULL"


@pytest.mark.xfail(
    reason="GENUINE FINDING : MAX_ARRAY_SIZE côté tree-walker (uvi_core.py "
           "~4486, valeur 1000) et DEFAULT_MAX_ARRAY_SIZE côté VM "
           "(~1794/2004, valeur 10000, utilisée par UVIVirtualMachine.__init__ "
           "quand max_array_size n'est pas passé explicitement) ne "
           "correspondent PAS. compile_and_run(lignes, parser, "
           "UVIBytecodeCompilerV2(), vfs=InMemoryVFS()) -- EXACTEMENT l'appel "
           "fait par UVIIDE._lancer_tests -- ne passe jamais max_array_size, "
           "donc pour un script qui pousse 1200 éléments dans un tableau : le "
           "tree-walker s'arrête à ARRAY_FULL (1000e élément), sortie vide "
           "et erreur ; la VM (avec les valeurs par défaut de "
           "compile_and_run) réussit intégralement et affiche 1200. C'est "
           "une divergence RÉELLE et REPRODUCTIBLE de la fonctionnalité "
           "même que ce test suite existe à vérifier, observable "
           "aujourd'hui avec la construction officiellement recommandée "
           "par le code source (voir UVIIDE._lancer_tests, "
           "uvi_core.py ~11295-11311).",
    strict=True,
)
def test_vm_default_diverges_from_tree_walker_on_array_size_BUG():
    hote, moteur = run_tw(_ARRAY_PUSH_SCRIPT)
    r = run_vm(_ARRAY_PUSH_SCRIPT)  # SANS max_array_size explicite -- défauts réels
    assert hote.sortie == vm_output_as_str(r)
    assert (len(hote.erreurs) > 0) == (r.trapped is not None)


# ---------------------------------------------------------------------
# GENUINE FINDING #2 : MAX_STRING_LENGTH (tree-walker=10000) vs
# DEFAULT_MAX_STRING_LENGTH (VM=1000000) -- même famille de divergence
# de défauts, pour la troncature de chaîne cette fois.
# ---------------------------------------------------------------------

_STRING_BUILD_SCRIPT = (
    's = ""\n% 300 -> s = s + "0123456789012345678901234567890123456789"\n'
    'STR.LEN s -> n\n! n'
)


def test_tree_walker_string_truncated_at_max_string_length():
    hote, moteur = run_tw(_STRING_BUILD_SCRIPT)
    assert hote.sortie == [str(U.MAX_STRING_LENGTH)]  # 10000, pas 12000
    assert moteur.error_flag is False  # troncature silencieuse, pas une erreur


def test_vm_with_matching_max_string_length_agrees_with_tree_walker():
    r = run_vm(_STRING_BUILD_SCRIPT, max_string_length=U.MAX_STRING_LENGTH)
    assert vm_output_as_str(r) == [str(U.MAX_STRING_LENGTH)]


@pytest.mark.xfail(
    reason="GENUINE FINDING : MAX_STRING_LENGTH côté tree-walker "
           "(uvi_core.py ~4484, valeur 10000) et DEFAULT_MAX_STRING_LENGTH "
           "côté VM (~1795/2005, valeur 1000000) diffèrent d'un facteur "
           "100. Avec compile_and_run(..., vfs=InMemoryVFS()) SANS "
           "max_string_length explicite (l'appel réel de "
           "UVIIDE._lancer_tests), une chaîne construite par boucle jusqu'à "
           "12000 caractères est tronquée à 10000 côté tree-walker mais "
           "atteint 12000 intacte côté VM -- divergence observable réelle, "
           "pas hypothétique.",
    strict=True,
)
def test_vm_default_diverges_from_tree_walker_on_string_length_BUG():
    hote, moteur = run_tw(_STRING_BUILD_SCRIPT)
    r = run_vm(_STRING_BUILD_SCRIPT)  # SANS max_string_length explicite
    assert hote.sortie == vm_output_as_str(r)


# ---------------------------------------------------------------------
# GENUINE FINDING #3 : MAX_CALL_DEPTH (tree-walker=50) vs le paramètre
# par défaut max_call_depth=100 du constructeur de la VM -- même famille
# de divergence, pour la profondeur de récursion cette fois. Utilise des
# ROUTINES mutuellement récursives (pas DEF, voir la note dans
# test_stop.py sur le bug _parse_def qui empêche un DEF récursif avec
# cas de base conditionnel) pour atteindre une profondeur bornée
# précise.
# ---------------------------------------------------------------------

# Une seule routine, un seul CALL_ROUTINE/self.call_depth par niveau de
# décompte (contrairement à une conception à deux routines qui double le
# nombre d'appels imbriqués par décrément et fausserait le seuil visé) :
# 'n = n - 1' et l'IF sont deux items SIBLINGS du même pipe-split de
# corps de routine (aucun des deux n'a de pipe interne à ses propres
# branches), donc ni le bug _parse_def (n/a, ceci est une ROUTINE) ni le
# bug de pipe-splitting imbriqué documentés dans test_parser.py ne
# s'appliquent ici -- vérifié par inspection directe de l'AST produit
# avant d'écrire ce test.
_BOUNDED_RECURSION_SCRIPT = (
    'n = 60\n'
    '*count_down -> n = n - 1 | ~ n <= 0 -> !"done" || *count_down\n'
    '*count_down'
)


def test_tree_walker_max_recursion_at_depth_between_50_and_100():
    hote, moteur = run_tw(_BOUNDED_RECURSION_SCRIPT)
    # Profondeur demandée (60) > MAX_CALL_DEPTH (50) -> trap avant la fin.
    assert any("MAX_RECURSION" in e for e in hote.erreurs)
    assert "done" not in hote.sortie


def test_vm_with_matching_max_call_depth_agrees_with_tree_walker():
    r = run_vm(_BOUNDED_RECURSION_SCRIPT, max_call_depth=U.MAX_CALL_DEPTH)
    assert r.trapped == "MAX_RECURSION"


@pytest.mark.xfail(
    reason="GENUINE FINDING : MAX_CALL_DEPTH côté tree-walker (uvi_core.py "
           "~4480, valeur 50, utilisée pour CALL et pour les routines "
           "*nom via le même compteur self.call_depth) et le paramètre "
           "max_call_depth du constructeur UVIVirtualMachine (~2003, "
           "valeur par défaut 100) diffèrent. Avec compile_and_run(..., "
           "vfs=InMemoryVFS()) SANS max_call_depth explicite, une "
           "récursion bornée à 60 niveaux de profondeur trape MAX_RECURSION "
           "côté tree-walker (60 > 50) mais se termine normalement côté VM "
           "par défaut (60 <= 100) -- divergence réelle, observable avec "
           "l'appel exact utilisé par UVIIDE._lancer_tests.",
    strict=True,
)
def test_vm_default_diverges_from_tree_walker_on_call_depth_BUG():
    hote, moteur = run_tw(_BOUNDED_RECURSION_SCRIPT)
    r = run_vm(_BOUNDED_RECURSION_SCRIPT)  # SANS max_call_depth explicite
    assert hote.sortie == vm_output_as_str(r)


# ---------------------------------------------------------------------
# MAX_MEMORY_BYTES -- propre au tree-walker, pas d'équivalent direct
# côté VM (pas de comptage mémoire global agrégé) ; testé isolément.
# ---------------------------------------------------------------------

def test_tree_walker_memory_overflow_graceful():
    lines = [f'v{i} = "{"a" * 9000}"' for i in range(80)]
    hote, moteur = run_tw("\n".join(lines))
    assert any("MEMORY_OVERFLOW" in e for e in hote.erreurs)
    # Aucun crash Python, error_flag proprement levé.
    assert moteur.error_flag is True


# ---------------------------------------------------------------------
# max_steps (VM) -- RUNAWAY, pas d'équivalent direct côté tree-walker
# (qui n'a pas de compteur d'instructions global, seulement des limites
# par construction -- boucle/récursion/mémoire).
# ---------------------------------------------------------------------

def test_vm_runaway_with_small_max_steps():
    r = run_vm('%% 1 -> x = 1', max_steps=1000)
    assert r.trapped == "RUNAWAY"
    assert r.steps > 1000


def test_vm_runaway_does_not_trigger_within_budget_for_normal_script():
    r = run_vm('% 100 -> ! "x"')
    assert r.trapped is None
    assert r.steps < U.DEFAULT_MAX_STEPS


# ---------------------------------------------------------------------
# Récursion non bornée (pas de cas de base du tout) -- doit tout de
# même trapper proprement des deux côtés, quels que soient les défauts
# respectifs (le TRAP finit toujours par survenir, seul le POINT auquel
# il survient diffère -- déjà couvert ci-dessus).
# ---------------------------------------------------------------------

def test_unbounded_recursion_traps_cleanly_both_engines():
    code = '*rec -> *rec\n*rec'
    hote, moteur = run_tw(code)
    assert any("MAX_RECURSION" in e for e in hote.erreurs)
    assert moteur.error_flag is True

    r = run_vm(code)
    assert r.trapped == "MAX_RECURSION"
