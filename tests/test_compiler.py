"""Tests unitaires du compilateur bytecode (UVIBytecodeCompilerV2) et de
l'optimiseur (UVIBytecodeOptimizer).

Couvre :
  - chaque type de noeud AST géré par _compile_node_inner compile sans
    lever BytecodeError (et produit au moins une instruction pertinente) ;
  - les noeuds explicitement NON supportés (ImportNode, PipeNode,
    UnknownNode, LearnNode, SnippetNode -- voir la docstring de ce
    module dans uvi_core.py ~1773-1776) lèvent bien BytecodeError,
    documentant le comportement RÉEL plutôt qu'un idéal supposé ;
  - les expressions genuinement malformées lèvent BytecodeError ;
  - compile_program ne laisse aucun label symbolique non résolu (tous
    les opérandes de saut/registre sont des entiers après résolution) ;
  - UVIBytecodeOptimizer produit un résultat observable IDENTIQUE au
    bytecode non optimisé pour un ensemble représentatif de programmes
    (constant folding + dead code elimination ne doivent jamais changer
    le comportement, seulement la taille du bytecode)."""
import pytest

from conftest import U, new_compiler, new_parser, run_vm, vm_output_as_str


def compile_source(source):
    """Parse puis compile `source` (str multi-lignes) -- retourne le
    dict bytecode complet (labels résolus)."""
    p = new_parser()
    nodes = p.parse_program(source.splitlines())
    return new_compiler().compile_program(nodes)


def compile_nodes(nodes):
    return new_compiler().compile_program(nodes)


# ---------------------------------------------------------------------
# Couverture par type de noeud AST -- chaque branche de
# _compile_node_inner exercée au moins une fois, sans lever.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    'x = 5',
    'x = CALL f(1)',
    '! "hello"',
    'DEF f() -> _retour = 1',
    'CALL f()',
    'CALL f() -> dest',
    'MODULE "x.uvi" -> m',
    'EXPORT nom',
    '~ x > 0 -> ! "a" || ! "b"',
    '%% x > 0 -> x = x - 1',
    '% 5 -> ! "tour"',
    'STR.LEN "a" -> n',
    '&save "f" -> "c"',
    'SHION.ASK "q" -> r',
    'TAB t',
    'DRAW.CLEAR',
    'PACKAGE.PUBLISH n -> "f" -> "1.0"',
    'KEY.DOWN "Left" -> touche',
    '@UVI',
    '@STOP',
    '_0.5',
    '?nom',
    '?E.age',
    '*r -> ! "hi"',
    '*r',
])
def test_node_types_compile_without_raising(source):
    bytecode = compile_source(source)
    assert bytecode["bytecode_version"] == "1.0"
    assert len(bytecode["instructions"]) >= 1
    # Toujours terminé par HALT (compile_symbolic l'ajoute systématiquement).
    assert bytecode["instructions"][-1]["opcode"] == "HALT"


def test_def_node_needed_special_handling_regression():
    # Régression documentée dans le code source (~1005-1015) : DefNode
    # n'était pas routé du tout avant correction -- vérifie explicitement
    # la présence de DEF_REG dans le flux compilé.
    bytecode = compile_source('DEF F(a) -> _retour = a')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    assert "DEF_REG" in opcodes


def test_routine_uses_dedicated_opcodes_not_call_func():
    bytecode = compile_source('*r -> x = 1')
    opcodes = {i["opcode"] for i in bytecode["instructions"]}
    assert "ROUTINE_REG" in opcodes
    assert "RET_ROUTINE" in opcodes
    assert "DEF_REG" not in opcodes


def test_routine_call_uses_call_routine():
    bytecode = compile_source('*r -> x = 1\n*r')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    assert "CALL_ROUTINE" in opcodes


def test_stop_node_compiles_to_single_halt():
    bytecode = compile_source('@STOP')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    # Un HALT pour @STOP, plus le HALT terminal de compile_symbolic.
    assert opcodes.count("HALT") == 2


def test_call_statement_pops_return_value():
    # CallNode-statement (pas capturé) doit émettre un POP après
    # CALL_FUNC pour ne pas faire fuir la pile (voir commentaire du code
    # source sur ce point précis).
    bytecode = compile_source('DEF f() -> _retour = 1\nCALL f()')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    # Le dernier CALL_FUNC (celui du site d'appel, pas celui hypothétique
    # à l'intérieur du corps) doit être suivi d'un POP.
    idx = len(opcodes) - 1 - opcodes[::-1].index("CALL_FUNC")
    assert opcodes[idx + 1] == "POP"


def test_call_with_destination_stores_instead_of_pop():
    bytecode = compile_source('DEF f() -> _retour = 1\nCALL f() -> dest')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    idx = len(opcodes) - 1 - opcodes[::-1].index("CALL_FUNC")
    assert opcodes[idx + 1] == "STORE"


def test_assign_is_call_compiles_call_directly_not_via_pop_path():
    # Régression documentée (~833-850) : AssignNode(is_call=True) doit
    # utiliser _compile_call_node directement (pas de POP intercalé) --
    # sinon le STORE suivant s'exécute sur une pile vide.
    bytecode = compile_source('DEF f() -> _retour = 1\nx = CALL f()')
    opcodes = [i["opcode"] for i in bytecode["instructions"]]
    idx = len(opcodes) - 1 - opcodes[::-1].index("CALL_FUNC")
    assert opcodes[idx + 1] == "STORE"  # jamais POP puis STORE


# ---------------------------------------------------------------------
# Noeuds explicitement non supportés -- comportement RÉEL documenté
# (voir docstring uvi_vm.py ~1773-1776 dans le fichier source).
# ---------------------------------------------------------------------

def test_unknown_node_raises_bytecode_error():
    p = new_parser()
    nodes = p.parse_program(["ceci n est pas valide du tout @@@"])
    assert isinstance(nodes[0], U.UnknownNode)
    with pytest.raises(U.BytecodeError):
        compile_nodes(nodes)


def test_import_node_raises_bytecode_error_documented_limitation():
    p = new_parser()
    nodes = p.parse_program(['IMPORT "f.uvi"'])
    with pytest.raises(U.BytecodeError):
        compile_nodes(nodes)


def test_pipe_node_raises_bytecode_error_documented_limitation():
    # PipeNode n'apparaît que pour une ligne top-level dont le premier
    # token ne matche aucun mot-clé ET qui contient un PIPE (cf.
    # _parse_tokens, branche de repli) -- ex. une variable seule suivie
    # d'un pipe sans '='.
    p = new_parser()
    nodes = p.parse_program(["bareword | autre"])
    assert isinstance(nodes[0], U.PipeNode)
    with pytest.raises(U.BytecodeError):
        compile_nodes(nodes)


def test_learn_node_raises_bytecode_error():
    p = new_parser()
    nodes = p.parse_program(['!LEARN "a" "b"'])
    with pytest.raises(U.BytecodeError):
        compile_nodes(nodes)


def test_snippet_node_raises_bytecode_error():
    p = new_parser()
    nodes = p.parse_program(["!SNIPPET pattern"])
    with pytest.raises(U.BytecodeError):
        compile_nodes(nodes)


def test_compile_and_run_propagates_bytecode_error_for_unsupported_node():
    # compile_and_run() (utilisé par _lancer_tests) ne catch PAS
    # BytecodeError -- un script avec une ligne UnknownNode/Import/Pipe
    # fait remonter l'exception jusqu'à l'appelant. Documenté ici pour
    # que personne ne suppose à tort une dégradation silencieuse.
    with pytest.raises(U.BytecodeError):
        run_vm("IMPORT \"f.uvi\"")


# ---------------------------------------------------------------------
# Expressions malformées.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("bad_expr", [
    "5 +",
    "1 = 2",
    "(((",
    ")))",
    "5 ** ** 2",
    "a b c d",
    "1 +* 2",
])
def test_malformed_expression_raises_bytecode_error(bad_expr):
    c = new_compiler()
    with pytest.raises(U.BytecodeError):
        c.compile_expr(bad_expr)


def test_well_formed_expressions_compile():
    c = new_compiler()
    for expr in ["1 + 2", "a * (b - c)", '"x" + "y"', "a AND b", "NOT a",
                 "a == b", "-a", "a OR b AND NOT c"]:
        instrs = c.compile_expr(expr)
        assert len(instrs) >= 1


def test_unsupported_function_call_in_expression_raises():
    c = new_compiler()
    # Un appel de fonction générique (pas __tab_get__/__tab_len__/
    # __module_get__) n'est pas une forme d'expression supportée.
    with pytest.raises(U.BytecodeError):
        c.compile_expr("foo(1, 2)")


# ---------------------------------------------------------------------
# BUG CORRIGÉ (était RÉEL, SÉVÈRE) : comparaison chaînée ('a < b < c')
# faisait planter le compilateur avec une AttributeError PYTHON BRUTE
# (pas même une BytecodeError propre), pas un simple souci d'équivalence
# entre moteurs. _compile_expr_node appelait
#   self._chained_compare_to_boolop(node)
# pour désucrer 'a < b < c' en '(a<b) AND (b<c)', mais aucune méthode de
# ce nom n'était définie -- une méthode prévue mais jamais écrite. Le
# tree-walker (SafeEval._eval, qui gère ast.Compare nativement via une
# boucle sur node.comparators) n'avait pas ce problème : '1 < x < 10'
# s'évaluait normalement là-bas, révélant la divergence. Corrigé en
# ajoutant _chained_compare_to_boolop (uvi_core.py, juste avant
# _compile_boolop) : construit le pyast.BoolOp(And, ...) équivalent à
# partir de node.left/ops/comparators, puis délègue à _compile_boolop
# déjà existant (même court-circuit que AND/OR).
# ---------------------------------------------------------------------

def test_chained_comparison_compiles_without_crash():
    c = new_compiler()
    instrs = c.compile_expr("1 < x < 10")
    assert instrs


def test_chained_comparison_tw_vs_vm_agree():
    from conftest import run_tw
    source = "x = 5\ny = 1 < x < 10\n! y"
    hote, moteur = run_tw(source)
    assert hote.sortie == ["True"]
    resultat = run_vm(source)
    assert vm_output_as_str(resultat) == ["True"]
    assert resultat.trapped is None


# ---------------------------------------------------------------------
# Résolution des labels -- aucun label symbolique ne doit survivre dans
# le flux résolu par compile_program.
# ---------------------------------------------------------------------

def _jump_opcodes():
    return {"JUMP_ABS", "JUMP_IF_FALSE", "JUMP_IF_TRUE"}


@pytest.mark.parametrize("source", [
    '~ x > 0 -> ! "a" || ! "b"',
    '%% x > 0 -> x = x - 1',
    '% 5 -> ! "tour"',
    'DEF f(a) -> _retour = a * a',
    # Note : un DEF dont le corps contient un IF ('DEF f(a) -> ~ a > 0 ->
    # ...') n'est PAS utilisable ici -- voir le bug documenté dans
    # test_parser.py::test_def_body_if_is_corrupted_BUG, qui corrompt le
    # corps en UnknownNode avant même d'atteindre la résolution de labels.
    '*r -> ~ x > 0 -> ! "a" || ! "b"',
    'x = 1 AND 0 OR 1',
])
def test_no_dangling_symbolic_labels(source):
    bytecode = compile_source(source)
    for instr in bytecode["instructions"]:
        if instr["opcode"] in _jump_opcodes():
            assert isinstance(instr["operands"][0], int), (
                f"Opérande de saut non résolue : {instr}")
        if instr["opcode"] == "DEF_REG":
            entry_idx = instr["operands"][2]
            assert isinstance(entry_idx, int)
        if instr["opcode"] == "ROUTINE_REG":
            entry_idx = instr["operands"][1]
            assert isinstance(entry_idx, int)


def test_labels_resolve_to_valid_indices_within_bounds():
    bytecode = compile_source('~ x > 0 -> ! "a" || ! "b"\n! "fin"')
    n = len(bytecode["instructions"])
    for instr in bytecode["instructions"]:
        if instr["opcode"] in _jump_opcodes():
            target = instr["operands"][0]
            assert 0 <= target <= n


def test_unresolved_label_raises_if_referenced_but_missing():
    # _resolve_labels lève BytecodeError si un JUMP_* référence un label
    # jamais émis -- construit un flux symbolique artificiel pour le
    # vérifier directement (situation qui ne peut pas survenir via le
    # compilateur normal, donc testée au niveau de _resolve_labels lui-même).
    c = new_compiler()
    Instr = U.Instr
    Opcode = U.OpcodeV1
    bogus = [Instr(Opcode.JUMP_ABS, ["__never_defined__"])]
    with pytest.raises(U.BytecodeError):
        c._resolve_labels(bogus)


# ---------------------------------------------------------------------
# UVIBytecodeOptimizer -- doit préserver le comportement observable.
# ---------------------------------------------------------------------

_OPTIMIZER_SOURCES = [
    'x = 1 + 2 * 3\n! x',
    '~ 1 == 1 -> ! "a" || ! "b"',
    '~ 5 > 10 -> ! "never" || ! "always"',
    '% 5 -> ! "tour"',
    '%% x > 0 -> x = x - 1',
    'x = 2 + 3\ny = x * (4 - 1)\n! y',
    'DEF carre(n) -> _retour = n * n\nCALL carre(6) -> r\n! r',
    's = "a" + "b" + "c"\n! s',
    'x = 1\n~ x == 1 -> @STOP\n! "unreachable"',
    'TAB t\nTAB.PUSH t -> 1 + 1\nTAB.PUSH t -> 2 * 2\nTAB.PRINT t',
    'x = (1 + 2) * (3 - 1)\n! x',
    '~ 0 -> ! "never" || ! "else branch"',
]


@pytest.mark.parametrize("source", _OPTIMIZER_SOURCES)
def test_optimizer_preserves_observable_vm_output(source):
    p = new_parser()
    nodes = p.parse_program(source.splitlines())
    compiler = new_compiler()
    unoptimized = compiler.compile_program(nodes)

    compiler2 = new_compiler()
    optimized = U.UVIBytecodeOptimizer().optimize_program(compiler2, nodes)

    vm1 = U.UVIVirtualMachine(vfs=U.InMemoryVFS())
    result1 = vm1.run(unoptimized)
    vm2 = U.UVIVirtualMachine(vfs=U.InMemoryVFS())
    result2 = vm2.run(optimized)

    assert vm_output_as_str(result1) == vm_output_as_str(result2)
    assert result1.trapped == result2.trapped


def test_optimizer_constant_folding_actually_folds():
    # Vérifie que le repliement a bien lieu (pas juste "ne casse rien").
    # NOTE COMPORTEMENT RÉEL (pas une supposition) : _fold_constants ne
    # fait qu'UNE SEULE passe linéaire de gauche à droite (voir
    # uvi_core.py ~3218-3243) -- elle ne revient jamais en arrière sur un
    # PUSH_CONST déjà émis dans `out` pour le recombiner avec un
    # repliement voisin qui vient d'être produit. Pour 'x = 1 + 2 * 3'
    # (PUSH 1, PUSH 2, PUSH 3, BINOP MUL, BINOP ADD), seul le motif
    # (PUSH 2, PUSH 3, MUL) est repérable en un passage -> replié en
    # PUSH 6, laissant (PUSH 1, PUSH 6, BINOP ADD) : UN SEUL BINOP replié
    # sur deux, pas les deux. Un optimiseur itéré jusqu'à point fixe
    # replierait aussi le ADD restant, mais optimize()/optimize_program()
    # n'appelle _fold_constants qu'une fois -- documenté ici comme le
    # comportement actuel plutôt que corrigé ou masqué (le résultat
    # observable à l'exécution reste correct, voir
    # test_optimizer_preserves_observable_vm_output ci-dessus, seule la
    # taille du bytecode optimisé est plus grande qu'un optimiseur à
    # point fixe ne le produirait).
    p = new_parser()
    nodes = p.parse_program(["x = 1 + 2 * 3"])
    compiler = new_compiler()
    symbolic = compiler.compile_symbolic(nodes)
    unopt_binops = sum(1 for i in symbolic if i.opcode == "BINOP")
    assert unopt_binops == 2

    optimized = U.UVIBytecodeOptimizer().optimize(symbolic)
    opt_binops = sum(1 for i in optimized if i.opcode == "BINOP")
    assert opt_binops == 1  # le MUL interne est replié, le ADD externe ne l'est pas
    assert any(i.opcode == "PUSH_CONST" and i.operands == [6] for i in optimized)


def test_optimizer_does_not_fold_division_by_zero():
    # Erreurs runtime légitimes volontairement NON repliées (voir
    # commentaire du code source ~3146-3151) -- le TRAP doit toujours
    # survenir à l'exécution, identique dans les deux versions.
    p = new_parser()
    nodes = p.parse_program(["x = 5 / 0"])
    compiler = new_compiler()
    unoptimized = compiler.compile_program(nodes)
    compiler2 = new_compiler()
    optimized = U.UVIBytecodeOptimizer().optimize_program(compiler2, nodes)

    r1 = U.UVIVirtualMachine(vfs=U.InMemoryVFS()).run(unoptimized)
    r2 = U.UVIVirtualMachine(vfs=U.InMemoryVFS()).run(optimized)
    assert r1.trapped == "DIVISION_BY_ZERO"
    assert r2.trapped == "DIVISION_BY_ZERO"


def test_optimizer_dead_code_after_halt_is_removed():
    p = new_parser()
    nodes = p.parse_program(['@STOP', '! "dead"'])
    compiler = new_compiler()
    symbolic = compiler.compile_symbolic(nodes)
    optimized = U.UVIBytecodeOptimizer().optimize(symbolic)
    # Le PRINT mort après le premier HALT doit disparaître.
    assert sum(1 for i in symbolic if i.opcode == "PRINT") == 1
    assert sum(1 for i in optimized if i.opcode == "PRINT") == 0


def test_optimizer_on_empty_program():
    compiler = new_compiler()
    optimized = U.UVIBytecodeOptimizer().optimize_program(compiler, [])
    assert optimized["instructions"][-1]["opcode"] == "HALT"
    result = U.UVIVirtualMachine(vfs=U.InMemoryVFS()).run(optimized)
    assert result.output == []
    assert result.trapped is None
