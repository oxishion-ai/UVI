"""Tests adversariaux / fuzz-lite : le parser (et par extension le
tree-walker et la VM) ne doit JAMAIS laisser filtrer une exception
Python non gérée, quelle que soit l'entrée -- au pire un ASTNode
dégradé (UnknownNode), une erreur UVI propre (_emit_error /
UVIRuntimeTrap), ou -- documenté comme un comportement ATTENDU et pas
un crash -- une BytecodeError de compilation pour les constructions
explicitement non supportées par UVIBytecodeCompilerV2 (ImportNode/
PipeNode/UnknownNode/LearnNode/SnippetNode, voir test_compiler.py).

Pas de dépendance à `hypothesis` (absent de cet environnement, vérifié
par `python -c "import hypothesis"` avant d'écrire ce fichier) -- fuzzing
fait à la main avec `random` (stdlib) sur un vocabulaire de fragments
UVI/non-UVI, graine fixée pour la reproductibilité entre exécutions."""
import random

import pytest

from conftest import U, new_parser, run_tw, run_vm


# ---------------------------------------------------------------------
# Batterie de chaînes adversariales écrites à la main : soupe de tokens,
# chaînes non terminées, parenthèses déséquilibrées, pipes profondément
# imbriqués, unicode, lignes énormes, caractères de contrôle.
# ---------------------------------------------------------------------

HANDCRAFTED_GARBAGE = [
    # Soupe de tokens / symboles bruts.
    "~~~~~~~~~~~~~~~~",
    "%%%%%%%%%%%%%%%%",
    "||||||||||||||||",
    "->->->->->->->->",
    "======000000====",
    "@@@@@@@@@@@@@@@@",
    "****************",
    "!!!!!!!!!!!!!!!!",
    "????????????????",
    "____________________",
    "&&&&&&&&&&&&&&&&",
    ".................",
    ",,,,,,,,,,,,,,,,,",
    "((((((((((((((((",
    "))))))))))))))))",
    "()()()()()()()()",
    ")(  )(  )(  )(  ",
    # Chaînes non terminées / échappement bancal.
    '"unterminated',
    '"escaped quote \\" still open',
    '"""""""""""""""""',
    'x = "a\\',
    '! "\\\\\\\\\\\\',
    # Mots-clés seuls, sans corps.
    "~",
    "~~",
    "%",
    "%%",
    "DEF",
    "CALL",
    "CALL()",
    "CALL(",
    "CALL )(",
    "DEF ()()()",
    "DEF F(",
    "DEF F)(",
    "MODULE",
    "EXPORT",
    "IMPORT",
    "TAB",
    "TAB.",
    "TAB.PUSH",
    "TAB.NONEXISTENT",
    "STR.",
    "STR.NONEXISTENT",
    "DRAW.",
    "DRAW.NONEXISTENT",
    "KEY.",
    "PACKAGE.",
    "PACKAGE.PUBLISH",
    "&",
    "&nonexistent",
    "*",
    "**",
    "***",
    "*123startswithdigit",
    "_",
    "_abc",
    "_-5",
    "?",
    "?.",
    "?E.",
    "?R.",
    # Pipes profondément imbriqués / répétés.
    "a" + " | b" * 200,
    "~ x -> " + "!\"y\" | " * 100 + "!\"z\"",
    "% 1 -> " + "|" * 50,
    # Flèches répétées / mal placées.
    "~ -> -> -> -> -> a",
    "DEF f() -> -> -> -> a",
    "CALL f() -> -> -> b",
    # Arité/parenthésage pathologique pour CALL.
    "CALL f(((((1)))))",
    "CALL f(,,,,,)",
    "CALL f(1,,2)",
    "CALL f(1,)",
    # Signature/STOP répétés ou combinés bizarrement.
    "@UVI@STOP",
    "@STOP@UVI",
    "@UVI @STOP @UVI @STOP",
    "@STOPSTOP",
    "@UVIX",
    # Unicode / emoji / caractères de contrôle.
    "! \"héllo wörld 😀🚀\"",
    "x_é = 1",
    "\x00\x01\x02\x03",
    "! \"\x00null byte inside\"",
    "\t\t\t\t~\t\t\t->\t\t\t",
    "​​​",  # zero-width spaces
    # Ligne énorme (stress du regex du lexer, pas une vraie boucle).
    # Profondeur limitée à 300 (pas plus) : au-delà, l'AST récursif
    # produit par ast.parse pour une chaîne binaire gauche-associative
    # ('1 + 1 + 1 + ...') fait grimper la récursion Python de
    # SafeEval._eval / _compile_expr_node proportionnellement -- une
    # profondeur bien plus grande risquerait un RecursionError avant même
    # d'atteindre la limite de récursion UVI elle-même (MAX_CALL_DEPTH),
    # ce qui est un axe de test différent (voir test_resource_limits.py),
    # pas l'objet de ce fichier.
    "x = " + "1 + " * 300 + "1",
    "! \"" + "a" * 5000 + "\"",
    "a" * 10000,
    # Assignations dégénérées.
    "= = = =",
    "x = = = 1",
    "= x",
    "x =",
    "x == y",  # ressemble à une comparaison mais en position d'assign
    # Combinaisons mots-clé + garbage adjacent.
    "~garbage->more_garbage||even_more",
    "%%%%x%%%%->%%%%y",
    "DEF~%%*CALL&STR.TAB.DRAW.KEY.",
    # Lignes vides/whitespace exotiques.
    "",
    " ",
    "\t",
    "\n",
    "\r\n",
    "   \t\t  \r  ",
    "# " + "#" * 500,
]


@pytest.mark.parametrize("garbage", HANDCRAFTED_GARBAGE, ids=range(len(HANDCRAFTED_GARBAGE)))
def test_handcrafted_garbage_never_crashes_parser(garbage):
    p = new_parser()
    node = p.parse_line(garbage, 1)
    assert node is None or isinstance(node, U.ASTNode)


@pytest.mark.parametrize("garbage", HANDCRAFTED_GARBAGE, ids=range(len(HANDCRAFTED_GARBAGE)))
def test_handcrafted_garbage_never_crashes_tree_walker(garbage):
    # run_sequence catche déjà toute exception par noeud (voir
    # uvi_core.py ~8448-8453, CRASH UVIError) -- ce test vérifie que
    # cette garantie tient VRAIMENT pour chacune de ces entrées, pas
    # seulement en théorie.
    hote, moteur = run_tw(garbage)
    assert isinstance(hote.sortie, list)


@pytest.mark.parametrize("garbage", HANDCRAFTED_GARBAGE, ids=range(len(HANDCRAFTED_GARBAGE)))
def test_handcrafted_garbage_vm_only_raises_documented_exceptions(garbage):
    # compile_and_run() (donc run_vm ici) N'ATTRAPE PAS BytecodeError --
    # c'est un comportement documenté (voir test_compiler.py), pas un
    # crash : une entrée qui produit un noeud non compilable (Unknown/
    # Import/Pipe/Learn/Snippet) doit lever BytecodeError PROPREMENT,
    # jamais un autre type d'exception Python. La seule exception connue
    # à cette règle, déjà documentée séparément (test_compiler.py::
    # test_chained_comparison_crashes_compiler_BUG), est l'AttributeError
    # sur une comparaison chaînée -- explicitement autorisée ici SI ET
    # SEULEMENT SI son message correspond exactement à ce bug déjà
    # signalé, pour ne jamais masquer une AttributeError d'une origine
    # différente et donc potentiellement nouvelle.
    try:
        run_vm(garbage)
    except U.BytecodeError:
        pass  # documenté, acceptable (voir test_compiler.py)
    except AttributeError as e:
        assert "_chained_compare_to_boolop" in str(e), (
            f"AttributeError NON documentée (différente du bug connu) "
            f"pour l'entrée {garbage!r} : {e}"
        )
    # Toute autre exception Python remonte et fait échouer le test --
    # c'est le but de ce test.


# ---------------------------------------------------------------------
# Fuzzing léger fait à la main (pas de hypothesis dans cet environnement)
# : combinaisons pseudo-aléatoires de fragments UVI et de bruit pur,
# graine fixée pour la reproductibilité.
# ---------------------------------------------------------------------

_UVI_FRAGMENTS = [
    "~", "%%", "%", "->", "||", "|", "=", "==", "!=", ">=", "<=", "<", ">",
    "(", ")", ",", '"', "DEF", "CALL", "MODULE", "EXPORT", "IMPORT",
    "TAB", "TAB.PUSH", "TAB.GET", "STR.LEN", "STR.SLICE", "DRAW.RECT",
    "KEY.DOWN", "PACKAGE.PUBLISH", "&save", "&load", "*routine",
    # NOTE : pas de fragment SLEEP ('_1.5' etc) dans ce vocabulaire --
    # une ligne aléatoire qui se réduit à un SleepNode exécute un VRAI
    # time.sleep() (jusqu'à 10s, clampé) dans le tree-walker ET la VM ;
    # avec ~400 lignes générées et plusieurs fonctions de test qui les
    # exécutent chacune, quelques lignes "_N" tirées au hasard suffisent
    # à faire dépasser plusieurs dizaines de secondes de temps réel --
    # constaté empiriquement en préparant cette suite (le premier essai
    # a dépassé 120s). Le SLEEP est déjà couvert ailleurs
    # (test_parser.py::test_sleep_node, test_compiler.py) sans avoir
    # besoin de le refaire tourner ici des dizaines de fois pour rien.
    "?", "?E.", "?R.", "@UVI", "@STOP", "!", "!LEARN", "!SNIPPET",
    "x", "y", "n", "1", "2.5", "-3", "0", "AND", "OR", "NOT",
    "a_b_c", "_retour", "true", "false", "None", "", " ", "\t",
]

_NOISE_CHARS = list("{}[]<>;:'`~!@#$%^&*()-_=+\\|/?.,\"") + ["😀", "é", "\x00", "​"]


def _random_line(rng: random.Random) -> str:
    n_parts = rng.randint(1, 12)
    parts = []
    for _ in range(n_parts):
        if rng.random() < 0.7:
            parts.append(rng.choice(_UVI_FRAGMENTS))
        else:
            parts.append("".join(rng.choice(_NOISE_CHARS) for _ in range(rng.randint(1, 6))))
    sep = rng.choice([" ", "", "  "])
    return sep.join(parts)


@pytest.fixture(scope="module")
def fuzz_lines():
    rng = random.Random(20260812)  # graine fixe -- reproductible
    return [_random_line(rng) for _ in range(400)]


def test_fuzz_parser_never_crashes(fuzz_lines):
    p = new_parser()
    for line in fuzz_lines:
        node = p.parse_line(line, 1)
        assert node is None or isinstance(node, U.ASTNode)


def test_fuzz_parse_program_never_crashes(fuzz_lines):
    p = new_parser()
    nodes = p.parse_program(fuzz_lines)
    assert isinstance(nodes, list)


def test_fuzz_tree_walker_never_crashes(fuzz_lines):
    # Un seul run_sequence sur TOUT le paquet de lignes -- reproduit
    # l'usage réel (un script multi-lignes), et exerce en prime les
    # interactions entre lignes adjacentes (ex. un ROUTINE bizarrement
    # formé suivi d'un appel).
    hote, moteur = run_tw(fuzz_lines)
    assert isinstance(hote.sortie, list)
    assert isinstance(hote.erreurs, list)


def test_fuzz_vm_only_raises_documented_exceptions(fuzz_lines):
    try:
        run_vm(fuzz_lines)
    except U.BytecodeError:
        pass
    except AttributeError as e:
        assert "_chained_compare_to_boolop" in str(e)


def test_fuzz_vm_line_by_line_never_raises_undocumented_exception(fuzz_lines):
    # Complément du test précédent (tout le paquet d'un coup, qui
    # s'arrête à la PREMIÈRE ligne non compilable) : exécute chaque ligne
    # INDIVIDUELLEMENT dans sa propre VM, pour couvrir plus de lignes
    # malgré l'arrêt à la première erreur de compilation.
    bytecode_errors = 0
    attribute_errors = 0
    for line in fuzz_lines:
        try:
            run_vm([line])
        except U.BytecodeError:
            bytecode_errors += 1
        except AttributeError as e:
            attribute_errors += 1
            assert "_chained_compare_to_boolop" in str(e)
    # Pas d'assertion sur les comptes eux-mêmes (dépend du contenu
    # aléatoire) -- la vraie assertion est l'absence de toute exception
    # non gérée, déjà garantie par le fait que la boucle est allée à son
    # terme sans lever.
    assert bytecode_errors >= 0  # simple marqueur que la boucle a tourné


def test_fuzz_seed_is_deterministic():
    # Vérifie que _random_line est bien déterministe pour une graine
    # donnée -- si ce test échoue un jour, tous les tests fuzz ci-dessus
    # deviennent non-reproductibles d'une exécution à l'autre, ce qui
    # serait un vrai problème pour le triage d'un futur échec.
    rng1 = random.Random(12345)
    rng2 = random.Random(12345)
    lines1 = [_random_line(rng1) for _ in range(20)]
    lines2 = [_random_line(rng2) for _ in range(20)]
    assert lines1 == lines2


# ---------------------------------------------------------------------
# Cas spécifiques à haut risque de crash Python (plutôt qu'une simple
# UnknownNode) -- construits à la main pour cibler des points connus du
# code comme fragiles (index/slice, conversions numériques, récursion
# structurelle du parser).
# ---------------------------------------------------------------------

@pytest.mark.parametrize("script", [
    'STR.SLICE "abc" -> "not a number" -> 2 -> dest',
    'STR.SLICE "abc" -> 0 -> "not a number" -> dest',
    'TAB.GET t -> "not a number" -> dest',
    'TAB.SET t -> "not a number" -> 1 -> dest',
    "% \"abc\" -> !\"x\"",
    '?E.x\n! x',  # sans input_fn : ne doit pas planter
    "CALL f(" + "1," * 150 + "1)",  # arité énorme
    "DEF f(" + "a," * 150 + "z) -> _retour = 1",  # énormément de params
    "x" * 3000 + " = 1",  # nom de variable énorme
])
def test_high_risk_scripts_never_crash_tree_walker(script):
    hote, moteur = run_tw(script)
    assert isinstance(hote.sortie, list)


@pytest.mark.parametrize("script", [
    'STR.SLICE "abc" -> "not a number" -> 2 -> dest',
    'STR.SLICE "abc" -> 0 -> "not a number" -> dest',
    "% \"abc\" -> !\"x\"",
    "CALL f(" + "1," * 150 + "1)",
    "DEF f(" + "a," * 150 + "z) -> _retour = 1",
    "x" * 3000 + " = 1",
])
def test_high_risk_scripts_never_crash_vm_beyond_documented_exceptions(script):
    try:
        run_vm(script)
    except U.BytecodeError:
        pass
