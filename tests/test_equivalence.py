"""Tests d'équivalence tree-walker / VM -- la propriété documentée comme
"la plus utile du projet" (docstring de UVIIDE._lancer_tests,
uvi_core.py ~11280-11287) : pour un même script UVI, HeadlessHost.sortie
(tree-walker) et [str(x) for x in VMResult.output] (VM) doivent être
IDENTIQUES. C'est la version pytest, automatisée, de la comparaison que
_lancer_tests fait interactivement dans l'onglet TESTS de l'IDE.

Table de ~25 scripts représentatifs (arithmétique, chaînes, TAB, IF/FOR/
WHILE, DEF/CALL, routines, @STOP) suivie de quelques cas ciblés
supplémentaires (arité, indisponibilité de service, comparaisons) et,
enfin, des cas xfail qui documentent les divergences RÉELLES trouvées en
préparant cette suite (voir chaque raison pour la citation exacte du
code source)."""
import pytest

from conftest import U, assert_engines_agree, run_tw, run_vm, vm_output_as_str

# ---------------------------------------------------------------------
# Table principale : (id lisible, script) -- chacun vérifié manuellement
# avant intégration (voir le corps de la tâche de préparation) pour
# confirmer qu'aucun n'effleure les bugs de parsing déjà documentés
# ailleurs (DEF+arrow interne, IF imbriqué avec branches multi-pipe).
# ---------------------------------------------------------------------

EQUIVALENCE_SCRIPTS = [
    ("assign_and_print", 'x = 5\n! x'),
    ("arithmetic_precedence", '! 2 + 3 * 4'),
    ("arithmetic_parens", '! (2 + 3) * 4'),
    ("division", '! 10 / 3'),
    ("modulo", '! 10 % 3'),
    ("power", '! 2 ** 8'),
    ("subtraction_two_vars", 'x = 5\ny = 3\n! x - y'),
    ("negative_number", 'x = -5\n! x'),
    ("string_literal", 's = "hello"\n! s'),
    ("string_interpolation", 'n = 5\n! "n vaut {n}"'),
    ("if_then_true", 'x = 1\n~ x == 1 -> !"one" || !"other"'),
    ("if_then_false", 'x = 2\n~ x == 1 -> !"one" || !"other"'),
    ("if_multi_statement_branches_top_level",
     'x = 1\n~ x == 1 -> !"a" | !"b" || !"c" | !"d"'),
    ("for_loop_fixed_count", '% 4 -> !"tick"'),
    ("for_loop_dynamic_count", 'n = 3\n% n -> !"x"'),
    ("while_loop_counter", 'x = 0\n%% x < 4 -> x = x + 1 | !x'),
    ("def_call_with_return", 'DEF add(a, b) -> _retour = a + b\nCALL add(3, 4) -> r\n! r'),
    ("def_call_no_args", 'DEF hello() -> _retour = "hi"\nCALL hello() -> r\n! r'),
    ("def_call_straight_line_body",
     'DEF f(a) -> x = a + 1 | y = x * 2 | _retour = y\nCALL f(3) -> r\n! r'),
    ("routine_shared_scope", 'x = 5\n*inc -> x = x + 1\n*inc\n*inc\n! x'),
    ("routine_multi_statement_body",
     '*greet -> !"hello" | !"world"\n*greet'),
    ("tab_len", 'TAB t = 1, 2, 3\nTAB.LEN t -> n\n! n'),
    ("tab_push_print", 'TAB t\nTAB.PUSH t -> 5\nTAB.PUSH t -> 6\nTAB.PRINT t'),
    ("tab_get_set", 'TAB t = 1, 2, 3\nTAB.SET t -> 1 -> 99\nTAB.GET t -> 1 -> x\n! x'),
    ("str_upper", 'STR.UPPER "abc" -> u\n! u'),
    ("str_split_join", 'STR.SPLIT "a,b,c" -> "," -> t\nSTR.JOIN t -> "-" -> s\n! s'),
    ("boolean_ops", '! 1 AND 0\n! 1 OR 0\n! NOT 1'),
    ("equality_ops", '! 5 == 5\n! 5 != 5'),
    ("comparison_ops", '! 5 > 3\n! 5 < 3\n! 5 >= 5\n! 5 <= 4'),
    ("stop_mid_script", 'x = 1\n~ x == 1 -> @STOP\n!"after"'),
    ("stop_in_for_loop", '% 5 -> !"tour" | @STOP\n!"after"'),
    ("nested_arithmetic", 'x = 3\ny = 4\n! x * x + y * y'),
    ("disk_roundtrip", '&save "f.txt" -> "contenu"\n&load "f.txt" -> c\n! c'),
    ("empty_program", ''),
    ("comment_only_program", '# rien à exécuter ici'),
]


@pytest.mark.parametrize("name,script", EQUIVALENCE_SCRIPTS, ids=[n for n, _ in EQUIVALENCE_SCRIPTS])
def test_tree_walker_and_vm_agree(name, script):
    assert_engines_agree(script)


def test_equivalence_table_has_at_least_twenty_five_scripts():
    # Garde-fou : la tâche demande explicitement ~20-30 scripts
    # représentatifs -- vérifie que la table n'a pas été vidée par
    # inadvertance lors d'une future modification.
    assert len(EQUIVALENCE_SCRIPTS) >= 25


# ---------------------------------------------------------------------
# Cas ciblés supplémentaires : arité, services indisponibles, valeurs de
# retour par défaut -- chacun testé au niveau des DEUX moteurs plutôt
# qu'un seul, pour la même raison que la table ci-dessus.
# ---------------------------------------------------------------------

def test_wrong_arity_agrees_on_empty_output_both_sides():
    script = 'DEF f(a, b) -> _retour = a\nCALL f(1) -> r\n! r'
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert moteur.error_flag is True
    assert r.trapped == "WRONG_ARG_COUNT"


def test_unknown_function_agrees_on_empty_output_both_sides():
    script = "CALL inconnue(1)"
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert r.trapped == "UNKNOWN_FUNCTION"


def test_unknown_routine_agrees_on_empty_output_both_sides():
    script = "*jamais_definie"
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert r.trapped == "UNKNOWN_ROUTINE"


def test_draw_unavailable_agrees_on_empty_output_both_sides():
    script = "DRAW.CLEAR"
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert r.trapped == "DRAW_UNAVAILABLE"
    assert any("DRAW_UNAVAILABLE" in e for e in hote.erreurs)  # même CODE, cette fois


def test_key_unavailable_agrees_on_empty_output_both_sides():
    script = 'KEY.DOWN "Left" -> t'
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert r.trapped == "KEY_UNAVAILABLE"
    assert any("KEY_UNAVAILABLE" in e for e in hote.erreurs)


def test_array_index_out_of_bounds_agrees_on_empty_output_both_sides():
    script = 'TAB t\nTAB.PUSH t -> 1\nTAB.GET t -> 9 -> x\n! x'
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert r.trapped == "INDEX_OUT_OF_BOUNDS"


def test_str_split_of_single_char_agrees():
    assert_engines_agree('STR.SPLIT "" -> "," -> t\nTAB.LEN t -> n\n! n')


def test_str_num_agrees():
    # Divergence reelle trouvee et corrigee : STR.NUM etait geree cote VM
    # (CALL_BUILTIN "STR_NUM") mais absente du elif de UVIEvaluator._exec_str,
    # qui tombait silencieusement dans le `return ""` final sans jamais
    # appeler _safe_store -- la variable de destination restait non definie
    # cote tree-walker (donc "" une fois relue) alors que la VM convertissait
    # correctement en int/float. Verifie ici les deux issues (entier et
    # reel) pour que la regression ne puisse pas revenir sur l'un des deux
    # chemins de STR_NUM sans faire echouer ce test.
    assert_engines_agree('STR.NUM "42" -> n\n! n\n! n / 2')
    assert_engines_agree('STR.NUM "3.5" -> n\n! n + 1')


def test_str_num_invalid_number_traps_both_sides():
    script = 'STR.NUM "pas un nombre" -> n\n! n'
    hote, moteur = run_tw(script)
    r = run_vm(script)
    assert hote.sortie == vm_output_as_str(r) == []
    assert any("INVALID_NUMBER" in e for e in hote.erreurs)
    assert r.trapped == "INVALID_NUMBER"


def test_deeply_nested_for_agrees():
    assert_engines_agree('% 3 -> % 3 -> !"x"')


def test_legacy_truthy_zero_is_false_agrees():
    assert_engines_agree('~ 0 -> !"yes" || !"no"')


def test_legacy_truthy_empty_string_is_false_agrees():
    assert_engines_agree('~ "" -> !"yes" || !"no"')


# ---------------------------------------------------------------------
# DIVERGENCES trouvées en préparant cette suite, DEPUIS CORRIGÉES dans
# uvi_core.py -- gardées ici comme tests de non-régression (citation
# exacte du bug d'origine conservée dans le commentaire, plus xfail).
# ---------------------------------------------------------------------

def test_string_literal_concatenation_now_agrees():
    """BUG CORRIGÉ : UVIEvaluator.evaluate() traitait '"a" + "b"' comme un
    littéral chaîne UNIQUE (l'expression entière commence ET finit par un
    guillemet) plutôt qu'une concaténation -- résultat 'a" + "b' côté
    tree-walker contre 'ab' (correct) côté VM, silencieusement, sans
    erreur des deux côtés. Corrigé en ne prenant ce raccourci que si
    `value` tokenize en EXACTEMENT un seul token STRING (sinon fallback
    sur _eval_expr_or_eris, qui gère BinOp/Compare correctement)."""
    assert_engines_agree('! "a" + "b"')


def test_chained_comparison_now_agrees():
    """BUG CORRIGÉ (voir aussi test_compiler.py) : toute comparaison
    chaînée ('1 < x < 10') faisait planter compile_expr côté VM avec
    AttributeError ('_chained_compare_to_boolop' jamais définie) --
    run_vm() levait cette exception au lieu de renvoyer un VMResult.
    Corrigé en ajoutant _chained_compare_to_boolop à
    UVIBytecodeCompilerV2."""
    assert_engines_agree('x = 5\n! 1 < x < 10')


@pytest.mark.xfail(
    reason="GENUINE FINDING (voir test_tree_walker.py sur evaluate() et "
           "EXPR_EVAL_FAILED) : une division par zéro produit le MÊME "
           "résultat observable vide dans les deux moteurs (donc "
           "n'apparaît PAS dans la table principale ci-dessus, qui ne "
           "compare QUE la sortie), mais le CODE d'erreur interne diffère "
           ": EXPR_EVAL_FAILED côté tree-walker (SafeEval.eval_expr avale "
           "TOUTE exception Python, y compris ZeroDivisionError, en "
           "SafeEvalError générique -- uvi_core.py ~7163-7172) contre "
           "DIVISION_BY_ZERO côté VM (BINOP dédié -- ~2768-2769). Un "
           "humain qui débogue par code d'erreur verrait deux diagnostics "
           "différents pour la même faute. Ce test compare explicitement "
           "les CODES (pas seulement la sortie) pour documenter l'écart.",
    strict=True,
)
def test_division_by_zero_error_code_matches_BUG():
    hote, moteur = run_tw("x = 5 / 0")
    r = run_vm("x = 5 / 0")
    tw_code = next((e.split("]")[0].lstrip("[") for e in hote.erreurs), None)
    assert tw_code == r.trapped
