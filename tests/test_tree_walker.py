"""Tests unitaires du tree-walker (UVIEvaluator / UVIEngine), via
HeadlessHost -- le harnais officiel du projet pour faire tourner ce
moteur sans Tkinter (voir sa docstring, uvi_core.py ~8467-8507).

Couvre : évaluation d'expressions (interpolation, alias::nom, TAB.GET/
TAB.LEN comme préfixes spéciaux), portée des fonctions DEF (isolation)
vs routines (scope partagé), erreurs runtime avec le bon code UVIError,
et un bug réel trouvé en préparant cette suite : IMPORT/MODULE plantent
sur InMemoryVFS côté tree-walker (mais pas côté VM)."""
import os

import pytest

from conftest import U, run_tw


# ---------------------------------------------------------------------
# evaluate() -- interpolation, littéraux, variables inconnues.
# ---------------------------------------------------------------------

def test_print_string_literal():
    hote, moteur = run_tw('! "bonjour"')
    assert hote.sortie == ["bonjour"]


def test_print_interpolated_string():
    hote, moteur = run_tw('x = 5\n! "valeur: {x}"')
    assert hote.sortie == ["valeur: 5"]


def test_print_interpolated_expression():
    hote, moteur = run_tw('x = 2\n! "double: {x * 2}"')
    assert hote.sortie == ["double: 4"]


def test_interpolation_failure_emits_error_not_crash():
    hote, moteur = run_tw('! "bad: {1 +}"')
    assert moteur.error_flag is True
    assert any("INTERPOLATION_FAILED" in e for e in hote.erreurs)


def test_undefined_variable_is_empty_string_legacy():
    hote, moteur = run_tw("! jamais_definie")
    assert hote.sortie == [""]


def test_arithmetic_expression():
    hote, moteur = run_tw("! 2 + 3 * 4")
    assert hote.sortie == ["14"]


def test_string_concatenation_with_a_variable_operand():
    # Fonctionne : dès que l'expression entière ne se termine plus par un
    # guillemet fermant (ici 'n' est un nom nu), evaluate() ne prend pas
    # le raccourci "chaîne littérale" -- voir le bug documenté juste en
    # dessous pour le cas où les DEUX opérandes sont des littéraux.
    hote, moteur = run_tw('n = "b"\n! "a" + n')
    assert hote.sortie == ["ab"]


def test_boolean_and_or_not():
    hote, moteur = run_tw('! 1 AND 1\n! 1 AND 0\n! NOT 0')
    assert hote.sortie == ["True", "False", "True"]


# ---------------------------------------------------------------------
# Portée : DEF isole (paramètres + copie de mémoire), routine partage.
# ---------------------------------------------------------------------

def test_def_call_isolates_scope():
    hote, moteur = run_tw('x = 100\nDEF f(x) -> x = x + 1\nCALL f(1)\n! x')
    assert hote.sortie == ["100"]  # x du top-level inchangé


def test_def_return_via_retour_variable():
    hote, moteur = run_tw('DEF carre(n) -> _retour = n * n\nCALL carre(6) -> r\n! r')
    assert hote.sortie == ["36"]


def test_def_default_return_is_empty_string():
    hote, moteur = run_tw('DEF f() -> x = 1\nCALL f() -> r\n! r')
    assert hote.sortie == [""]


def test_def_wrong_arity_emits_wrong_arg_count():
    hote, moteur = run_tw('DEF f(a, b) -> _retour = a\nCALL f(1)')
    assert any("WRONG_ARG_COUNT" in e for e in hote.erreurs)


def test_call_unknown_function_emits_unknown_function():
    hote, moteur = run_tw("CALL inconnue(1)")
    assert any("UNKNOWN_FUNCTION" in e for e in hote.erreurs)


def test_routine_shares_callers_scope():
    hote, moteur = run_tw('joueur_x = 5\n*avancer -> joueur_x = joueur_x + 1\n*avancer\n! joueur_x')
    assert hote.sortie == ["6"]


def test_routine_unknown_emits_unknown_routine():
    hote, moteur = run_tw("*jamais_definie")
    assert any("UNKNOWN_ROUTINE" in e for e in hote.erreurs)


# ---------------------------------------------------------------------
# Contrôle de flux.
# ---------------------------------------------------------------------

def test_if_then_else():
    hote, moteur = run_tw('~ 1 == 1 -> ! "yes" || ! "no"')
    assert hote.sortie == ["yes"]
    hote2, moteur2 = run_tw('~ 1 == 2 -> ! "yes" || ! "no"')
    assert hote2.sortie == ["no"]


def test_for_loop_iterates_n_times():
    hote, moteur = run_tw('% 5 -> ! "tour"')
    assert hote.sortie == ["tour"] * 5


def test_while_loop_runs_until_condition_false():
    hote, moteur = run_tw('x = 0\n%% x < 3 -> x = x + 1 | ! x')
    assert hote.sortie == ["1", "2", "3"]


def test_for_loop_count_from_expression():
    hote, moteur = run_tw('n = 3\n% n -> ! "x"')
    assert hote.sortie == ["x", "x", "x"]


def test_for_loop_invalid_count_emits_invalid_count():
    hote, moteur = run_tw('% "abc" -> ! "never"')
    assert any("INVALID_COUNT" in e for e in hote.erreurs)
    assert hote.sortie == []


# ---------------------------------------------------------------------
# TAB / STR / DISK.
# ---------------------------------------------------------------------

def test_tab_full_roundtrip():
    hote, moteur = run_tw(
        'TAB t\nTAB.PUSH t -> 10\nTAB.PUSH t -> 20\nTAB.GET t -> 1 -> x\nTAB.LEN t -> n\n! x\n! n'
    )
    assert hote.sortie == ["20", "2"]


def test_tab_get_via_evaluate_prefix():
    # evaluate() traite 'TAB.GET nom -> idx' comme un préfixe de chaîne
    # spécial quand il apparaît DANS une expression (pas seulement comme
    # statement TabNode dédié) -- voir evaluate() ~7137-7155. Vérifié
    # directement via evaluate(), qui prend TOUT ce qui suit la flèche
    # comme expression d'index (donc un index simple ici, pas une
    # comparaison composée -- voir _exec_tab pour la forme statement à 3
    # opérandes, déjà couverte par test_tab_full_roundtrip).
    hote, moteur = run_tw('TAB t = 10, 20, 30')
    assert moteur.evaluate("TAB.GET t -> 1") == 20


def test_tab_len_via_evaluate_prefix():
    hote, moteur = run_tw('TAB t = 10, 20, 30')
    assert moteur.evaluate("TAB.LEN t") == 3


def test_tab_index_out_of_bounds_emits_error():
    hote, moteur = run_tw('TAB t\nTAB.PUSH t -> 1\nTAB.GET t -> 5 -> x')
    assert any("INDEX_OUT_OF_BOUNDS" in e for e in hote.erreurs)


def test_str_ops():
    hote, moteur = run_tw(
        'STR.UPPER "abc" -> a\n! a\n'
        'STR.SPLIT "a,b,c" -> "," -> t\nTAB.LEN t -> n\n! n'
    )
    assert hote.sortie == ["ABC", "3"]


def test_disk_roundtrip_in_memory_vfs():
    hote, moteur = run_tw(
        '&save "f.txt" -> "contenu"\n&load "f.txt" -> c\n! c'
    )
    assert hote.sortie == ["contenu"]


def test_disk_load_missing_emits_error():
    hote, moteur = run_tw('&load "jamais.txt" -> x')
    assert any("FILE_NOT_FOUND" in e for e in hote.erreurs) or moteur.error_flag


# ---------------------------------------------------------------------
# Divisions par zéro et erreurs d'expression -- code EXPR_EVAL_FAILED
# (voir la note dans test_equivalence.py sur la divergence de code avec
# la VM, qui utilise DIVISION_BY_ZERO).
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# BUG CORRIGÉ (était RÉEL, SÉVÈRE) : concaténation de deux littéraux
# chaîne donnait un résultat FAUX côté tree-walker (silencieusement,
# aucune erreur), alors que la VM donnait le bon résultat pour le MÊME
# script.
#
# UVIEvaluator.evaluate() commençait par :
#   if value.startswith('"') and value.endswith('"'):
#       return self._interpolate_string(value[1:-1])
# C'était un raccourci pensé pour UN SEUL littéral chaîne ('"bonjour"'),
# mais rien ne distinguait ce cas de '"a" + "b"' : cette expression
# commence ET finit elle aussi par un guillemet. Le raccourci prenait
# donc TOUT le texte entre le PREMIER et le DERNIER guillemet comme
# littéral unique -- '"a" + "b"'[1:-1] = 'a" + "b' -- au lieu de passer
# par SafeEval pour évaluer la concaténation. Corrigé en ne prenant ce
# raccourci que si `value` tokenize (via le vrai lexer) en EXACTEMENT un
# seul token STRING -- sinon fallback sur _eval_expr_or_eris, qui gère
# BinOp/Compare correctement (SafeEval._eval, comme la VM via
# pyast.parse).
# ---------------------------------------------------------------------

def test_string_literal_concatenation_gives_correct_result():
    hote, moteur = run_tw('! "a" + "b"')
    assert hote.sortie == ["ab"]


def test_division_by_zero_emits_expr_eval_failed():
    hote, moteur = run_tw("x = 5 / 0")
    assert moteur.error_flag is True
    assert any("EXPR_EVAL_FAILED" in e for e in hote.erreurs)
    assert hote.sortie == []


def test_error_halts_rest_of_script():
    hote, moteur = run_tw('x = 5 / 0\n! "never printed"')
    assert hote.sortie == []


# ---------------------------------------------------------------------
# Limites de ressources spécifiques au tree-walker (constantes module,
# voir test_resource_limits.py pour la comparaison avec la VM).
# ---------------------------------------------------------------------

def test_max_call_depth_constant_value():
    assert U.MAX_CALL_DEPTH == 50


def test_max_array_size_constant_value():
    assert U.MAX_ARRAY_SIZE == 1000


def test_max_string_length_constant_value():
    assert U.MAX_STRING_LENGTH == 10000


def test_array_full_after_max_array_size_pushes():
    hote, moteur = run_tw('TAB t\n% 1200 -> TAB.PUSH t -> 1')
    assert any("ARRAY_FULL" in e for e in hote.erreurs)


def test_string_truncated_at_max_string_length():
    src = 's = ""\n% 300 -> s = s + "0123456789012345678901234567890123456789"\nSTR.LEN s -> n\n! n'
    hote, moteur = run_tw(src)
    assert hote.sortie == [str(U.MAX_STRING_LENGTH)]


def test_max_recursion_on_call_func():
    hote, moteur = run_tw('DEF rec(n) -> CALL rec(n) -> t | _retour = t\nCALL rec(1) -> r')
    assert any("MAX_RECURSION" in e for e in hote.erreurs)


def test_max_recursion_on_routine():
    hote, moteur = run_tw('*rec -> *rec\n*rec')
    assert any("MAX_RECURSION" in e for e in hote.erreurs)


def test_memory_overflow_emits_error_not_crash():
    # Une seule très grosse chaîne : _safe_store (uvi_core.py ~7194-7208)
    # tronque d'abord à MAX_STRING_LENGTH AVANT de calculer le delta
    # mémoire -- donc une seule affectation, même énorme, ne peut pas
    # déclencher MEMORY_OVERFLOW à elle seule (elle est bornée par la
    # troncature de chaîne en premier). Ce test vérifie donc surtout
    # l'absence de crash et le respect effectif de la borne de longueur,
    # MEMORY_OVERFLOW lui-même étant couvert par
    # test_memory_overflow_after_many_large_variables ci-dessous.
    huge = "x" * 600_000
    hote, moteur = run_tw(f'x = "{huge}"')
    assert moteur.error_flag is False
    assert len(str(moteur.memoire.get("x", ""))) <= U.MAX_STRING_LENGTH


def test_memory_overflow_after_many_large_variables():
    # Beaucoup de variables DISTINCTES, chacune sous MAX_STRING_LENGTH
    # mais dont le total dépasse MAX_MEMORY_BYTES (500000 octets) --
    # cette fois current_memory_usage doit bien finir par saturer.
    lines = [f'v{i} = "{"a" * 9000}"' for i in range(80)]
    hote, moteur = run_tw("\n".join(lines))
    assert any("MEMORY_OVERFLOW" in e for e in hote.erreurs)


# ---------------------------------------------------------------------
# Draw/Key indisponibles hors IDE Tkinter (HeadlessHost n'expose ni
# canvas_draw ni key_is_down/key_was_pressed -- voir sa docstring).
# ---------------------------------------------------------------------

def test_draw_unavailable_headless():
    hote, moteur = run_tw("DRAW.CLEAR")
    assert any("DRAW_UNAVAILABLE" in e for e in hote.erreurs)


def test_key_unavailable_headless():
    hote, moteur = run_tw('KEY.DOWN "Left" -> t')
    assert any("KEY_UNAVAILABLE" in e for e in hote.erreurs)


def test_input_uses_provided_input_fn():
    hote, moteur = run_tw("?nom\n! nom", input_fn=lambda var: "Alice")
    assert hote.sortie == ["Alice"]


def test_input_strict_int_conversion():
    hote, moteur = run_tw("?E.age\n! age", input_fn=lambda var: "42")
    assert hote.sortie == ["42"]


def test_input_strict_int_conversion_fails_cleanly():
    hote, moteur = run_tw("?E.age", input_fn=lambda var: "abc")
    assert any("INVALID_INPUT_TYPE" in e for e in hote.erreurs)


# ---------------------------------------------------------------------
# EXPORT hors MODULE -- doit échouer proprement (pas de crash).
# ---------------------------------------------------------------------

def test_export_outside_module_emits_error():
    hote, moteur = run_tw("DEF f() -> _retour = 1\nEXPORT f")
    assert any("EXPORT_OUTSIDE_MODULE" in e for e in hote.erreurs)


def test_module_get_unknown_module_emits_error():
    hote, moteur = run_tw("! jamaischarge::nom")
    assert any("UNKNOWN_MODULE" in e for e in hote.erreurs)


# ---------------------------------------------------------------------
# DEUX BUGS CORRIGÉS, trouvés ensemble en préparant cette suite :
#
# 1. UVIEvaluator._resolve_module_path et _exec_import appelaient tous
#    deux self.vfs.get_path(nom_fic) SANS jamais vérifier
#    hasattr(self.vfs, "get_path") d'abord -- contrairement à
#    UVIVirtualMachine._resolve_module_path, qui a toujours eu cette
#    garde. InMemoryVFS (la VFS officiellement fournie pour faire tourner
#    ce moteur hors IDE, utilisée par HeadlessHost et par
#    UVIIDE._lancer_tests lui-même) NE DÉFINIT PAS get_path -> tout
#    script IMPORT/MODULE exécuté via HeadlessHost+InMemoryVFS plantait
#    avec AttributeError (rattrapée en [CRASH] par run_sequence). Corrigé
#    en ajoutant la même garde hasattr aux deux méthodes, symétrique de
#    la VM.
#
# 2. Une fois ce premier bug corrigé, MODULE ne plantait plus mais
#    renvoyait encore un résultat FAUX : UVIEvaluator._exec_call route
#    "CALL alias::nom(args)" vers _exec_module_call et RETOURNE
#    IMMÉDIATEMENT (return self._exec_module_call(...)), donc le code de
#    stockage "if node.dest: self._safe_store(node.dest, retour_val)"
#    présent à la fin de _exec_call n'était JAMAIS atteint pour un appel
#    qualifié -- "CALL m::carre(5) -> r" exécutait bien la fonction
#    (retour_val correct en interne) mais `r` restait vide, sans la
#    moindre erreur. La VM n'a jamais eu ce bug (le STORE après
#    CALL_MODULE_FUNC est émis au niveau du nœud CallNode dans
#    _compile_node_inner, pas dans _compile_call_node). Corrigé en
#    ajoutant le même stockage à la fin de _exec_module_call.
# ---------------------------------------------------------------------

def test_module_call_result_stored_via_in_memory_vfs(tmp_path):
    modpath = tmp_path / "mathmod.uvi"
    modpath.write_text('DEF carre(n) -> _retour = n * n\nEXPORT carre\n', encoding="utf-8")
    posix_path = str(modpath).replace(os.sep, "/")
    hote, moteur = run_tw(f'MODULE "{posix_path}" -> m\nCALL m::carre(5) -> r\n! r')
    assert hote.sortie == ["25"]
    assert not any("CRASH" in e for e in hote.erreurs)


def test_import_works_via_in_memory_vfs(tmp_path):
    incpath = tmp_path / "inc.uvi"
    incpath.write_text('! "imported"\n', encoding="utf-8")
    posix_path = str(incpath).replace(os.sep, "/")
    hote, moteur = run_tw(f'IMPORT "{posix_path}"\n! "after"')
    assert hote.sortie == ["imported", "after"]
    assert not any("CRASH" in e for e in hote.erreurs)
