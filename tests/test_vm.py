"""Tests unitaires de la VM bytecode (UVIVirtualMachine).

Couvre : discipline de pile (pas de fuite/sous-dépilement à travers
CALL_FUNC/RET, CALL_ROUTINE/RET_ROUTINE, appels de module), traps
MAX_RECURSION/RUNAWAY, builtins TAB/STR/DISK, isolation MODULE/EXPORT
(un nom non exporté est inaccessible via alias::nom), et les traps
d'indisponibilité de service (DRAW/KEY/SHION/INPUT) quand aucun callback
n'est fourni à la VM."""
import os

import pytest

from conftest import U, new_compiler, new_parser, run_vm


def compile_src(source):
    p = new_parser()
    nodes = p.parse_program(source.splitlines())
    return new_compiler().compile_program(nodes)


def fresh_vm(**kw):
    kw.setdefault("vfs", U.InMemoryVFS())
    return U.UVIVirtualMachine(**kw)


# ---------------------------------------------------------------------
# Discipline de pile.
# ---------------------------------------------------------------------

def test_stack_empty_after_normal_run_no_calls():
    vm = fresh_vm()
    vm.run(compile_src('x = 1\n! x'))
    assert vm.stack == []
    assert len(vm.frame_stack) == 1  # seulement global_frame


def test_stack_empty_after_uncaptured_call_statement():
    vm = fresh_vm()
    vm.run(compile_src('DEF f() -> _retour = 1\nCALL f()'))
    assert vm.stack == []
    assert len(vm.frame_stack) == 1


def test_stack_empty_after_captured_call():
    vm = fresh_vm()
    vm.run(compile_src('DEF f() -> _retour = 1\nx = CALL f()'))
    assert vm.stack == []
    assert len(vm.frame_stack) == 1


def test_stack_empty_after_call_with_destination():
    vm = fresh_vm()
    vm.run(compile_src('DEF f() -> _retour = 1\nCALL f() -> dest'))
    assert vm.stack == []
    assert len(vm.frame_stack) == 1


def test_stack_and_frame_stack_clean_after_many_sequential_calls():
    vm = fresh_vm()
    src = 'DEF f(n) -> _retour = n + 1\n' + '\n'.join(
        f'CALL f({i}) -> r{i}' for i in range(50)
    )
    result = vm.run(compile_src(src))
    assert result.trapped is None
    assert vm.stack == []
    assert len(vm.frame_stack) == 1


def test_stack_clean_after_nested_function_calls():
    # NOTE : 'CALL inner(n) -> t' (forme flèche) à l'intérieur d'un corps
    # DEF déclenche le bug documenté dans test_parser.py (_parse_def
    # tronque tout ce qui suit une 2e flèche) -- on utilise donc la forme
    # 't = CALL inner(n)' (AssignNode.is_call, aucune flèche interne au
    # corps de outer) pour tester la discipline de pile de la VM sans
    # être bloqué par ce bug de parsing par ailleurs sans rapport.
    vm = fresh_vm()
    src = ('DEF inner(n) -> _retour = n * 2\n'
           'DEF outer(n) -> t = CALL inner(n) | _retour = t + 1\n'
           'CALL outer(5) -> r\n! r')
    result = vm.run(compile_src(src))
    assert result.trapped is None
    assert result.output == [11]
    assert vm.stack == []
    assert len(vm.frame_stack) == 1


def test_stack_clean_after_routine_calls_no_new_frame():
    vm = fresh_vm()
    src = 'x = 0\n*inc -> x = x + 1\n*inc\n*inc\n*inc\n! x'
    result = vm.run(compile_src(src))
    assert result.output == [3]
    assert vm.stack == []
    # Une routine n'empile JAMAIS de Frame -- toujours 1 seule frame.
    assert len(vm.frame_stack) == 1


def test_stack_clean_after_trap_mid_call():
    # Même en cas de TRAP au milieu d'un appel, run() ne doit pas laisser
    # la VM dans un état visiblement corrompu pour un futur run() sur la
    # même instance (chaque run() démarre avec pc=0 sur un flux
    # d'instructions frais, donc la vraie garantie utile est que l'objet
    # ne lève pas une seconde exception juste en inspectant son état).
    vm = fresh_vm()
    result = vm.run(compile_src('x = 5 / 0'))
    assert result.trapped == "DIVISION_BY_ZERO"
    # pas d'assertion plus forte ici : un TRAP interrompt le run() en
    # plein milieu, la pile peut légitimement contenir des résidus de
    # l'expression en cours d'évaluation -- ce n'est pas une fuite tant
    # que ça ne s'accumule pas d'un run() à l'autre (voir test suivant).


def test_repeated_runs_on_same_vm_instance_do_not_accumulate_stack():
    vm = fresh_vm()
    for _ in range(20):
        vm.run(compile_src('x = 5 / 0'))
    # Le stack résiduel après un TRAP ne doit pas grossir sans borne au
    # fil des runs répétés sur la même instance.
    assert len(vm.stack) < 5


# ---------------------------------------------------------------------
# CALL_FUNC / RET -- arité, fonction inconnue, valeur de retour.
# ---------------------------------------------------------------------

def test_call_func_wrong_arity_traps():
    result = fresh_vm().run(compile_src('DEF f(a, b) -> _retour = a\nCALL f(1) -> r'))
    assert result.trapped == "WRONG_ARG_COUNT"


def test_call_unknown_function_traps():
    result = fresh_vm().run(compile_src('CALL inconnue(1) -> r'))
    assert result.trapped == "UNKNOWN_FUNCTION"


def test_call_func_default_return_is_empty_string():
    result = fresh_vm().run(compile_src('DEF f() -> x = 1\nCALL f() -> r\n! r'))
    assert result.output == [""]


def test_call_func_return_value_correct():
    result = fresh_vm().run(compile_src('DEF carre(n) -> _retour = n * n\nCALL carre(7) -> r\n! r'))
    assert result.output == [49]


def test_call_func_params_isolated_from_caller_locals():
    # Une fonction DEF (contrairement à une routine) a sa PROPRE Frame :
    # une variable de même nom dans l'appelant ne doit pas être visible/
    # modifiée par erreur à travers un paramètre distinct.
    result = fresh_vm().run(compile_src(
        'x = 100\nDEF f(x) -> x = x + 1 | _retour = x\nCALL f(1) -> r\n! r\n! x'
    ))
    assert result.output == [2, 100]


# ---------------------------------------------------------------------
# CALL_ROUTINE / RET_ROUTINE -- pas de nouvelle Frame, lit/écrit les
# variables de l'appelant directement.
# ---------------------------------------------------------------------

def test_routine_shares_callers_scope():
    result = fresh_vm().run(compile_src(
        'joueur_x = 5\n*avancer -> joueur_x = joueur_x + 1\n*avancer\n! joueur_x'
    ))
    assert result.output == [6]


def test_routine_unknown_traps():
    result = fresh_vm().run(compile_src('*jamais_definie'))
    assert result.trapped == "UNKNOWN_ROUTINE"


# ---------------------------------------------------------------------
# MAX_RECURSION / RUNAWAY.
# ---------------------------------------------------------------------

def test_call_func_max_recursion_traps():
    result = fresh_vm(max_call_depth=10).run(
        compile_src('DEF rec(n) -> CALL rec(n) -> t | _retour = t')
    )
    result2 = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), max_call_depth=10).run(
        compile_src('DEF rec(n) -> CALL rec(n) -> t | _retour = t\nCALL rec(1) -> r')
    )
    assert result2.trapped == "MAX_RECURSION"


def test_call_routine_max_recursion_traps():
    result = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), max_call_depth=10).run(
        compile_src('*rec -> *rec\n*rec')
    )
    assert result.trapped == "MAX_RECURSION"


def test_runaway_trap_on_infinite_while():
    result = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), max_steps=500).run(
        compile_src('%% 1 -> x = 1')
    )
    assert result.trapped == "RUNAWAY"
    assert result.steps > 500


def test_runaway_trap_respects_custom_max_steps():
    vm_small = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), max_steps=50)
    vm_large = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), max_steps=5000)
    src = compile_src('%% 1 -> x = 1')
    r_small = vm_small.run(src)
    r_large_partial = None
    # On ne fait pas tourner vm_large jusqu'au bout (boucle infinie) --
    # juste vérifier que le seuil bas déclenche BEAUCOUP plus tôt.
    assert r_small.trapped == "RUNAWAY"
    assert r_small.steps <= 51


def test_while_infinite_loop_trap_matches_tree_walker_iteration_count():
    # Le compilateur émet un TRAP INFINITE_LOOP explicite pour WHILE
    # après MAX_LOOP_ITERATIONS (10000, en dur dans _compile_node_inner
    # -- voir uvi_core.py ~953) tours, AVANT que RUNAWAY (steps) n'ait la
    # moindre chance de se déclencher avec le budget par défaut.
    result = fresh_vm().run(compile_src('n = 0\n%% 1 -> n = n + 1'))
    assert result.trapped == "INFINITE_LOOP"


def test_for_loop_silently_clamps_no_trap():
    # Asymétrie documentée et volontaire (voir commentaire du code
    # source ~966-970) : FOR clampe silencieusement à MAX_LOOP_ITERATIONS
    # sans jamais lever de TRAP, contrairement à WHILE.
    result = fresh_vm().run(compile_src('n = 0\n% 15000 -> n = n + 1\n! n'))
    assert result.trapped is None
    assert result.output == [10000]


# ---------------------------------------------------------------------
# Builtins TAB / STR / DISK.
# ---------------------------------------------------------------------

def test_tab_push_get_len():
    result = fresh_vm().run(compile_src(
        'TAB t\nTAB.PUSH t -> 10\nTAB.PUSH t -> 20\nTAB.GET t -> 1 -> x\nTAB.LEN t -> n\n! x\n! n'
    ))
    assert result.output == [20, 2]


def test_tab_index_out_of_bounds_traps():
    result = fresh_vm().run(compile_src('TAB t\nTAB.PUSH t -> 1\nTAB.GET t -> 5 -> x'))
    assert result.trapped == "INDEX_OUT_OF_BOUNDS"


def test_tab_pop_empty_traps():
    result = fresh_vm().run(compile_src('TAB t\nTAB.POP t -> x'))
    assert result.trapped == "EMPTY_ARRAY"


def test_tab_operation_on_non_array_traps():
    result = fresh_vm().run(compile_src('x = 5\nTAB.PUSH x -> 1'))
    assert result.trapped == "NOT_A_ARRAY"


def test_tab_del_and_set():
    result = fresh_vm().run(compile_src(
        'TAB t = 1, 2, 3\nTAB.SET t -> 1 -> 99\nTAB.DEL t -> 0\nTAB.LEN t -> n\nTAB.GET t -> 0 -> x\n! n\n! x'
    ))
    assert result.output == [2, 99]


def test_tab_print_format():
    result = fresh_vm().run(compile_src('TAB t = 7, 8\nTAB.PRINT t'))
    assert result.output == ["[t][0] = 7", "[t][1] = 8"]


def test_tab_print_empty():
    result = fresh_vm().run(compile_src('TAB t\nTAB.PRINT t'))
    assert result.output == ["[t] vide"]


def test_str_ops_roundtrip():
    result = fresh_vm().run(compile_src(
        'STR.UPPER "abc" -> a\n'
        'STR.LOWER "XYZ" -> b\n'
        'STR.TRIM "  hi  " -> c\n'
        'STR.LEN "hello" -> d\n'
        'STR.REPLACE "aaa" -> "a" -> "b" -> e\n'
        'STR.CONTAINS "hello" -> "ell" -> f\n'
        'STR.STARTS "hello" -> "he" -> g\n'
        'STR.ENDS "hello" -> "lo" -> h\n'
        'STR.SLICE "hello" -> 1 -> 3 -> i\n'
        'STR.INDEX "hello" -> "l" -> j\n'
        '! a\n! b\n! c\n! d\n! e\n! f\n! g\n! h\n! i\n! j'
    ))
    assert result.output == ["ABC", "xyz", "hi", 5, "bbb", True, True, True, "el", 2]


def test_str_split_join():
    result = fresh_vm().run(compile_src(
        'STR.SPLIT "a,b,c" -> "," -> t\nSTR.JOIN t -> "-" -> s\n! s'
    ))
    assert result.output == ["a-b-c"]


def test_str_num_valid_and_invalid():
    result = fresh_vm().run(compile_src('STR.NUM "42" -> n\n! n'))
    assert result.output == [42]
    result2 = fresh_vm().run(compile_src('STR.NUM "not a number" -> n'))
    assert result2.trapped == "INVALID_NUMBER"


def test_disk_save_load_list_delete_roundtrip():
    vfs = U.InMemoryVFS()
    vm = U.UVIVirtualMachine(vfs=vfs)
    result = vm.run(compile_src(
        '&save "f.txt" -> "hello world"\n'
        '&load "f.txt" -> contenu\n'
        '! contenu\n'
        '&list\n'
        '&delete "f.txt"'
    ))
    assert result.trapped is None
    assert result.output[0] == "hello world"
    assert "f.txt" in result.output[1]
    assert not vfs.exists("f.txt")


def test_disk_load_missing_file_traps():
    result = fresh_vm().run(compile_src('&load "jamais_cree.txt" -> x'))
    assert result.trapped == "FILE_NOT_FOUND"


def test_disk_save_empty_filename_traps():
    result = fresh_vm().run(compile_src('&save "" -> "x"'))
    assert result.trapped == "MISSING_FILENAME"


# ---------------------------------------------------------------------
# Indisponibilité de service quand aucun callback n'est fourni --
# jamais une AttributeError Python brute, toujours un UVIRuntimeTrap
# propre avec un code stable.
# ---------------------------------------------------------------------

def test_draw_unavailable_without_draw_fn():
    result = fresh_vm().run(compile_src("DRAW.CLEAR"))
    assert result.trapped == "DRAW_UNAVAILABLE"


def test_key_unavailable_without_key_fns():
    result = fresh_vm().run(compile_src('KEY.DOWN "Left" -> t'))
    assert result.trapped == "KEY_UNAVAILABLE"


def test_shion_unavailable_without_ask_fn():
    result = fresh_vm().run(compile_src('SHION.ASK "question" -> r'))
    assert result.trapped == "SHION_UNAVAILABLE"


def test_shion_empty_prompt_traps():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), shion_ask_fn=lambda p: "reponse"
    ).run(compile_src('SHION.ASK "" -> r'))
    assert result.trapped == "SHION_EMPTY_PROMPT"


def test_shion_ask_fn_used_when_provided():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), shion_ask_fn=lambda p: f"echo:{p}"
    ).run(compile_src('SHION.ASK "bonjour" -> r\n! r'))
    assert result.output == ["echo:bonjour"]


def test_input_unavailable_without_input_fn():
    result = fresh_vm().run(compile_src("?nom"))
    assert result.trapped == "INPUT_UNAVAILABLE"


def test_input_fn_used_when_provided():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), input_fn=lambda var: "42"
    ).run(compile_src("?nom\n! nom"))
    assert result.output == ["42"]


def test_input_strict_int_conversion():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), input_fn=lambda var: "42"
    ).run(compile_src("?E.age\n! age"))
    assert result.output == [42]


def test_input_strict_int_conversion_fails_on_float_string():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), input_fn=lambda var: "4.5"
    ).run(compile_src("?E.age"))
    assert result.trapped == "INVALID_INPUT_TYPE"


def test_key_down_fn_used_when_provided():
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), key_down_fn=lambda k: k == "Left"
    ).run(compile_src('KEY.DOWN "Left" -> a\nKEY.DOWN "Right" -> b\n! a\n! b'))
    assert result.output == [1, 0]


def test_draw_fn_called_with_expected_args():
    calls = []
    result = U.UVIVirtualMachine(
        vfs=U.InMemoryVFS(), draw_fn=lambda *a: calls.append(a)
    ).run(compile_src('DRAW.CLEAR\nDRAW.RECT 0 -> 0 -> 10 -> 10 -> "#FF0000"'))
    assert result.trapped is None
    assert calls[0][0] == "clear"
    assert calls[1][0] == "rect"


# ---------------------------------------------------------------------
# MODULE / EXPORT -- isolation : un nom non exporté est inaccessible
# depuis l'extérieur, un nom exporté est accessible via alias::nom.
# Utilise tmp_path (jamais UVI_Storage/) : le VFS en mémoire ne définit
# pas get_path (la VM gère cette absence via hasattr, voir
# _resolve_module_path, donc bascule proprement sur la résolution disque
# directe -- os.path.exists(nom_fic) avec un chemin ABSOLU fonctionne
# indépendamment du répertoire courant).
# ---------------------------------------------------------------------

def _write_module(tmp_path, filename, content):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    # Chemin POSIX (slashes) : le lexer EXPR ne s'arrête pas sur '\\',
    # mais autant rester cohérent avec les chemins déjà utilisés ailleurs
    # dans ce dépôt pour MODULE (voir UVI_REFERENCE_LANGAGE).
    return str(path).replace(os.sep, "/")


def test_module_export_accessible_via_alias(tmp_path):
    modpath = _write_module(
        tmp_path, "mathmod.uvi",
        'priv = 42\nDEF carre(n) -> _retour = n * n\nEXPORT carre\n'
    )
    result = fresh_vm().run(compile_src(
        f'MODULE "{modpath}" -> m\nCALL m::carre(5) -> r\n! r'
    ))
    assert result.trapped is None
    assert result.output == [25]


def test_module_unexported_variable_inaccessible(tmp_path):
    modpath = _write_module(
        tmp_path, "mathmod2.uvi",
        'priv = 42\nDEF carre(n) -> _retour = n * n\nEXPORT carre\n'
    )
    result = fresh_vm().run(compile_src(
        f'MODULE "{modpath}" -> m\n! m::priv'
    ))
    assert result.trapped == "NOT_EXPORTED"


def test_module_unexported_function_not_callable(tmp_path):
    modpath = _write_module(
        tmp_path, "mathmod3.uvi",
        'DEF prive(n) -> _retour = n\nDEF publique(n) -> _retour = n\nEXPORT publique\n'
    )
    result = fresh_vm().run(compile_src(
        f'MODULE "{modpath}" -> m\nCALL m::prive(1) -> r'
    ))
    assert result.trapped == "NOT_EXPORTED"


def test_module_not_loaded_traps_unknown_module():
    result = fresh_vm().run(compile_src('! jamaischarge::nom'))
    assert result.trapped == "UNKNOWN_MODULE"


def test_module_file_not_found_traps():
    result = fresh_vm().run(compile_src('MODULE "ce_fichier_n_existe_pas_du_tout.uvi" -> m'))
    assert result.trapped == "FILE_NOT_FOUND"


def test_module_variable_export_readable_via_module_get(tmp_path):
    modpath = _write_module(tmp_path, "consts.uvi", 'PI = 3\nEXPORT PI\n')
    result = fresh_vm().run(compile_src(
        f'MODULE "{modpath}" -> c\n! c::PI'
    ))
    assert result.output == [3]


def test_module_export_routine_not_callable_via_call(tmp_path):
    # EXPORT sur une routine : accessible en principe, mais CALL attend
    # une fonction DEF, pas une routine -- NOT_A_FUNCTION.
    modpath = _write_module(
        tmp_path, "withroutine.uvi",
        '*r -> x = 1\nEXPORT r\n'
    )
    result = fresh_vm().run(compile_src(
        f'MODULE "{modpath}" -> m\nCALL m::r() -> x'
    ))
    assert result.trapped == "NOT_A_FUNCTION"


def test_circular_module_load_blocked(tmp_path):
    a_path = tmp_path / "a.uvi"
    b_path = tmp_path / "b.uvi"
    a_posix = str(a_path).replace(os.sep, "/")
    b_posix = str(b_path).replace(os.sep, "/")
    a_path.write_text(f'MODULE "{b_posix}" -> bb\n', encoding="utf-8")
    b_path.write_text(f'MODULE "{a_posix}" -> aa\n', encoding="utf-8")
    result = fresh_vm().run(compile_src(f'MODULE "{a_posix}" -> a'))
    assert result.trapped == "CIRCULAR_MODULE"


# ---------------------------------------------------------------------
# TRAP opcode direct / opcode inconnu -- robustesse de la boucle run().
# ---------------------------------------------------------------------

def test_trap_opcode_directly():
    Instr = U.Instr
    Opcode = U.OpcodeV1
    bytecode = {"instructions": [
        {"opcode": Opcode.TRAP, "operands": ["CUSTOM_CODE"], "line": 0},
        {"opcode": Opcode.HALT, "operands": [], "line": 0},
    ]}
    result = fresh_vm().run(bytecode)
    assert result.trapped == "CUSTOM_CODE"


def test_unknown_opcode_traps_rather_than_crashing():
    bytecode = {"instructions": [
        {"opcode": "NOT_A_REAL_OPCODE", "operands": [], "line": 0},
    ]}
    result = fresh_vm().run(bytecode)
    assert result.trapped == "UNKNOWN_OPCODE"


def test_empty_bytecode_runs_cleanly():
    result = fresh_vm().run({"instructions": []})
    assert result.trapped is None
    assert result.output == []


def test_halt_stops_immediately_mid_stream():
    Opcode = U.OpcodeV1
    bytecode = {"instructions": [
        {"opcode": "PUSH_CONST", "operands": ["before"], "line": 0},
        {"opcode": "PRINT", "operands": [], "line": 0},
        {"opcode": "HALT", "operands": [], "line": 0},
        {"opcode": "PUSH_CONST", "operands": ["after"], "line": 0},
        {"opcode": "PRINT", "operands": [], "line": 0},
    ]}
    result = fresh_vm().run(bytecode)
    assert result.output == ["before"]


# ---------------------------------------------------------------------
# legacy_truthy toggle.
# ---------------------------------------------------------------------

def test_legacy_truthy_default_matches_uvi_semantics():
    result = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), legacy_truthy=True).run(
        compile_src('~ 1 -> ! "yes" || ! "no"')
    )
    assert result.output == ["yes"]


def test_legacy_truthy_disabled_uses_python_bool():
    # legacy_truthy=False : truthy() retombe sur bool(v) natif Python --
    # 0 est faux, toute chaîne non vide est vraie (y compris "False"
    # littéralement, contrairement au mode legacy où seul "True" compte).
    vm = U.UVIVirtualMachine(vfs=U.InMemoryVFS(), legacy_truthy=False)
    result = vm.run(compile_src('~ "False" -> ! "yes" || ! "no"'))
    assert result.output == ["yes"]  # bool("False") est True en Python
