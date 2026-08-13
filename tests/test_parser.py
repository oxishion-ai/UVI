"""Tests unitaires du parser (UVIParser).

Couvre : chaque forme de statement produit le bon type de noeud AST avec
les bons champs, l'entrée malformée ne lève jamais (toujours un noeud,
au pire UnknownNode), IF/FOR/WHILE imbriqués, corps séparés par '|',
fonctions à 0/1/plusieurs paramètres, et deux bugs de parsing réels
trouvés en préparant cette suite (voir les tests marqués xfail, avec
citation exacte du code source concerné)."""
import pytest

from conftest import U, new_parser


@pytest.fixture
def parser():
    return new_parser()


def parse1(parser, line, n=1):
    return parser.parse_line(line, n)


# ---------------------------------------------------------------------
# Formes de base
# ---------------------------------------------------------------------

def test_empty_line_returns_none(parser):
    assert parse1(parser, "") is None


def test_comment_line_returns_none(parser):
    assert parse1(parser, "# juste un commentaire") is None


def test_whitespace_line_returns_none(parser):
    assert parse1(parser, "    ") is None


def test_signature_node(parser):
    n = parse1(parser, "@UVI")
    assert isinstance(n, U.SignatureNode)


def test_stop_node(parser):
    n = parse1(parser, "@STOP")
    assert isinstance(n, U.StopNode)


def test_assign_node(parser):
    n = parse1(parser, "x = 5 + 2")
    assert isinstance(n, U.AssignNode)
    assert n.var == "x"
    assert n.expr == "5 + 2"
    assert n.is_call is False


def test_assign_from_call(parser):
    n = parse1(parser, "x = CALL f(1, 2)")
    assert isinstance(n, U.AssignNode)
    assert n.var == "x"
    assert n.is_call is True
    assert isinstance(n.call_node, U.CallNode)
    assert n.call_node.name == "f"
    assert n.call_node.args == ["1", "2"]


def test_print_node(parser):
    n = parse1(parser, '! "bonjour"')
    assert isinstance(n, U.PrintNode)
    assert n.expr == '"bonjour"'


def test_print_node_bare_expr(parser):
    n = parse1(parser, "! x + 1")
    assert isinstance(n, U.PrintNode)
    assert n.expr == "x + 1"


def test_input_node_no_hint(parser):
    n = parse1(parser, "?nom")
    assert isinstance(n, U.InputNode)
    assert n.var == "nom"
    assert n.type_hint == ""


def test_input_node_int_hint(parser):
    n = parse1(parser, "?E.age")
    assert isinstance(n, U.InputNode)
    assert n.var == "age"
    assert n.type_hint == "E"


def test_input_node_real_hint(parser):
    n = parse1(parser, "?R.taille")
    assert n.var == "taille"
    assert n.type_hint == "R"


def test_input_node_name_starting_with_e_without_dot(parser):
    # Sans le point, "Emma" ne doit PAS être interprété comme E + "mma"
    # (voir commentaire du code source sur ce garde-fou).
    n = parse1(parser, "?Emma")
    assert n.var == "Emma"
    assert n.type_hint == ""


def test_sleep_node(parser):
    n = parse1(parser, "_1.5")
    assert isinstance(n, U.SleepNode)
    assert n.duration == 1.5


def test_sleep_node_malformed_duration_defaults_zero(parser):
    # first.value[1:] toujours un motif [0-9.]+ valide par construction du
    # lexer, donc pas de cas d'échec réel atteignable ici -- documenté
    # pour mémoire (float() a un except Exception -> 0.0 de secours).
    n = parse1(parser, "_0")
    assert n.duration == 0.0


# ---------------------------------------------------------------------
# IF / WHILE / FOR
# ---------------------------------------------------------------------

def test_if_node_then_only(parser):
    n = parse1(parser, '~ x > 0 -> ! "pos"')
    assert isinstance(n, U.IfNode)
    assert n.condition == "x > 0"
    assert len(n.then_block) == 1
    assert isinstance(n.then_block[0], U.PrintNode)
    assert n.else_block == []


def test_if_node_then_else(parser):
    n = parse1(parser, '~ x > 0 -> ! "pos" || ! "neg"')
    assert isinstance(n, U.IfNode)
    assert len(n.then_block) == 1
    assert len(n.else_block) == 1


def test_if_node_multi_statement_then_and_else(parser):
    n = parse1(parser, '~ x > 0 -> ! "a" | ! "b" || ! "c" | ! "d"')
    assert len(n.then_block) == 2
    assert len(n.else_block) == 2


def test_if_node_invalid_no_arrow(parser):
    n = parse1(parser, "~ x > 0")
    assert isinstance(n, U.UnknownNode)


def test_nested_if_in_then_block(parser):
    # Un IF imbriqué DANS le then-block d'un IF top-level, exprimé comme
    # UNE seule instruction du then (pas de pipe) -- le parser ne
    # supporte pas nativement un second '~' au sein d'un then_block via
    # le pipe-split (chaque item de then_block est reparsé par
    # _parse_tokens, donc un item qui EST lui-même tout un IF fonctionne
    # tant qu'il n'y a qu'un seul statement de ce type à cette position).
    n = parse1(parser, '~ x > 0 -> ~ y > 0 -> ! "both" || ! "x only"')
    assert isinstance(n, U.IfNode)
    assert len(n.then_block) == 1
    inner = n.then_block[0]
    assert isinstance(inner, U.IfNode)
    assert inner.condition == "y > 0"


def test_while_node(parser):
    n = parse1(parser, "%% x > 0 -> x = x - 1")
    assert isinstance(n, U.WhileNode)
    assert n.condition == "x > 0"
    assert len(n.body) == 1


def test_while_node_invalid_no_arrow(parser):
    n = parse1(parser, "%% x > 0")
    assert isinstance(n, U.UnknownNode)


def test_for_node(parser):
    n = parse1(parser, '% 5 -> ! "tour"')
    assert isinstance(n, U.ForNode)
    assert n.count_expr == "5"
    assert len(n.body) == 1


def test_for_node_dynamic_count(parser):
    n = parse1(parser, "% n -> x = x + 1")
    assert n.count_expr == "n"


def test_for_node_invalid_no_arrow(parser):
    n = parse1(parser, "% 5")
    assert isinstance(n, U.UnknownNode)


def test_deeply_nested_if_for_while(parser):
    # FOR contenant un WHILE contenant un IF -- chacun comme SEUL
    # statement de son bloc parent (contourne le bug de pipe-splitting
    # documenté plus bas), pour vérifier que la récursion d'imbrication
    # elle-même n'a pas de limite artificielle.
    line = '% 3 -> %% x > 0 -> ~ x > 5 -> ! "big"'
    n = parse1(parser, line)
    assert isinstance(n, U.ForNode)
    assert isinstance(n.body[0], U.WhileNode)
    assert isinstance(n.body[0].body[0], U.IfNode)


def test_for_body_multi_statement_pipe(parser):
    n = parse1(parser, '% 3 -> ! "a" | ! "b" | ! "c"')
    assert isinstance(n, U.ForNode)
    assert len(n.body) == 3


def test_while_body_multi_statement_pipe(parser):
    n = parse1(parser, '%% x -> x = x - 1 | ! x')
    assert len(n.body) == 2


# ---------------------------------------------------------------------
# DEF / CALL
# ---------------------------------------------------------------------

def test_def_zero_params(parser):
    n = parse1(parser, 'DEF F() -> ! "hi"')
    assert isinstance(n, U.DefNode)
    assert n.name == "F"
    assert n.params == []
    assert len(n.body) == 1


def test_def_one_param(parser):
    n = parse1(parser, "DEF CARRE(x) -> _retour = x * x")
    assert n.params == ["x"]


def test_def_many_params(parser):
    n = parse1(parser, "DEF SOMME(a, b, c) -> _retour = a + b + c")
    assert n.params == ["a", "b", "c"]


def test_def_multi_statement_body_straight_line(parser):
    # Corps sans aucune construction à flèche interne -- ne déclenche
    # PAS le bug documenté plus bas (DEF perd tout ce qui suit une 2e
    # flèche dans son corps).
    n = parse1(parser, 'DEF F(a) -> x = a + 1 | y = x * 2 | _retour = y')
    assert len(n.body) == 3


def test_def_invalid_no_arrow(parser):
    n = parse1(parser, "DEF F(a)")
    assert isinstance(n, U.UnknownNode)


def test_def_invalid_no_parens(parser):
    n = parse1(parser, "DEF F -> _retour = 1")
    assert isinstance(n, U.UnknownNode)


def test_call_no_args(parser):
    n = parse1(parser, "CALL F()")
    assert isinstance(n, U.CallNode)
    assert n.name == "F"
    assert n.args == []
    assert n.dest is None


def test_call_with_args(parser):
    n = parse1(parser, "CALL SOMME(1, 2, 3)")
    assert n.args == ["1", "2", "3"]


def test_call_with_destination(parser):
    n = parse1(parser, "CALL CARRE(5) -> r")
    assert n.name == "CARRE"
    assert n.args == ["5"]
    assert n.dest == "r"


def test_call_nested_call_argument(parser):
    # rfind(')') (dernière parenthèse fermante) doit garder l'argument
    # imbriqué intact -- régression testée explicitement (voir
    # commentaire _parse_call sur ce point précis).
    n = parse1(parser, "CALL F(G(2))")
    assert n.name == "F"
    assert n.args == ["G ( 2 )"]


def test_call_trailing_garbage_after_paren_is_unknown(parser):
    n = parse1(parser, "CALL F(1) garbage")
    assert isinstance(n, U.UnknownNode)


def test_call_invalid_no_parens(parser):
    n = parse1(parser, "CALL F")
    assert isinstance(n, U.UnknownNode)


def test_call_wrong_arity_still_parses(parser):
    # Le parser ne connaît pas l'arité déclarée d'une fonction -- il doit
    # accepter n'importe quel nombre d'arguments syntaxiquement valides ;
    # l'arité est vérifiée à l'exécution (WRONG_ARG_COUNT), pas ici.
    n = parse1(parser, "CALL F(1, 2, 3, 4, 5)")
    assert isinstance(n, U.CallNode)
    assert len(n.args) == 5


# ---------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------

def test_routine_call_no_arrow(parser):
    n = parse1(parser, "*saluer")
    assert isinstance(n, U.RoutineCallNode)
    assert n.name == "saluer"


def test_routine_def_with_body(parser):
    n = parse1(parser, '*saluer -> ! "bonjour"')
    assert isinstance(n, U.RoutineDefNode)
    assert n.name == "saluer"
    assert len(n.body) == 1


def test_routine_def_no_assign_in_body_still_a_def(parser):
    # Bug historique corrigé (voir commentaire _parse_routine) : une
    # routine SANS aucun '=' dans son corps doit quand même être reconnue
    # comme une DÉFINITION (pas un appel) dès qu'il y a un ARROW.
    n = parse1(parser, '*saluer -> ! "bonjour" | ! "monde"')
    assert isinstance(n, U.RoutineDefNode)
    assert len(n.body) == 2


def test_routine_def_with_if_inside(parser):
    # Contrairement à DEF (voir bug documenté plus bas), une ROUTINE
    # supporte un IF interne car _parse_routine utilise
    # _split_on_first_arrow (pas un split sur TOUTES les flèches).
    n = parse1(parser, '*r -> ~ x > 0 -> ! "pos" || ! "neg"')
    assert isinstance(n, U.RoutineDefNode)
    assert len(n.body) == 1
    assert isinstance(n.body[0], U.IfNode)
    assert len(n.body[0].then_block) == 1
    assert len(n.body[0].else_block) == 1


def test_routine_call_with_destination_arrow_preserved(parser):
    # Cas de référence pour le bug DEF ci-dessous : une routine garde
    # correctement 'CALL G(n) -> tmp' (avec sa destination) dans son
    # corps.
    n = parse1(parser, "*r -> CALL G(n) -> tmp")
    assert isinstance(n, U.RoutineDefNode)
    assert len(n.body) == 1
    call = n.body[0]
    assert isinstance(call, U.CallNode)
    assert call.dest == "tmp"


# ---------------------------------------------------------------------
# BUG CORRIGÉ, TROUVÉ EN PRÉPARANT CETTE SUITE : _parse_def (uvi_core.py)
# séparait tête/corps avec
#   self._split_tokens_by_type(rest, 'ARROW')
# qui coupe sur TOUTES les flèches de la ligne, alors que
# _parse_if/_parse_while/_parse_for/_parse_routine utilisent tous
# _split_on_first_arrow (introduit précisément pour corriger cette même
# famille de bug, voir la docstring de _split_on_first_arrow). _parse_def
# n'avait jamais reçu ce correctif : tout corps de DEF contenant une
# flèche interne (IF, WHILE, FOR, CALL f(x) -> dest, TAB.GET, TAB.SET,
# STR.SLICE, KEY.*, DISK, MODULE...) perdait silencieusement tout ce qui
# suit la 2e flèche de la ligne, sans lever la moindre erreur. Corrigé en
# faisant utiliser _split_on_first_arrow à _parse_def, comme les autres.
#
# Comparer test_call_with_destination et
# test_routine_call_with_destination_arrow_preserved ci-dessus (qui
# passaient déjà) avec les deux tests ci-dessous (exactement les mêmes
# constructions, mais à l'intérieur d'un DEF plutôt qu'une ROUTINE) :
# tous passent désormais de façon identique.
# ---------------------------------------------------------------------

def test_def_body_call_with_destination_arrow_preserved(parser):
    n = parse1(parser, "DEF F(n) -> CALL G(n) -> tmp")
    assert isinstance(n, U.DefNode)
    call = n.body[0]
    assert isinstance(call, U.CallNode)
    assert call.dest == "tmp"


def test_def_body_if_parses_correctly(parser):
    n = parse1(parser, 'DEF F(n) -> ~ n > 0 -> !"pos" || !"neg"')
    assert isinstance(n, U.DefNode)
    assert len(n.body) == 1
    assert isinstance(n.body[0], U.IfNode)
    assert len(n.body[0].then_block) == 1
    assert len(n.body[0].else_block) == 1


# ---------------------------------------------------------------------
# TAB / STR / DISK / SHION / DRAW / KEY / PACKAGE / MODULE / EXPORT --
# vérification de la découpe syntaxique (le parser ne fait que découper,
# jamais évaluer, cf. commentaires du code source).
# ---------------------------------------------------------------------

def test_tab_create(parser):
    n = parse1(parser, "TAB.CREATE t")
    assert isinstance(n, U.TabNode)
    assert n.cmd == "TAB.CREATE"


def test_tab_bare_declares_empty(parser):
    n = parse1(parser, "TAB monTableau")
    assert isinstance(n, U.TabNode)
    assert n.cmd == "TAB_EMPTY"
    assert n.operands == ["monTableau"]


def test_tab_init_with_values(parser):
    n = parse1(parser, "TAB t = 1, 2, 3")
    assert n.cmd == "TAB_INIT"
    assert n.operands[0] == "t"
    assert "1" in n.operands[1]


def test_tab_push(parser):
    n = parse1(parser, "TAB.PUSH t -> 5")
    assert n.cmd == "TAB.PUSH"
    assert n.operands == ["t", "5"]


def test_tab_get_three_operands(parser):
    n = parse1(parser, "TAB.GET t -> 0 -> x")
    assert n.operands == ["t", "0", "x"]


def test_str_len(parser):
    n = parse1(parser, 'STR.LEN "bonjour" -> n')
    assert isinstance(n, U.StrNode)
    assert n.cmd == "STR.LEN"
    assert n.operands == ['"bonjour"', "n"]


def test_str_slice(parser):
    n = parse1(parser, "STR.SLICE s -> 0 -> 3 -> dest")
    assert n.operands == ["s", "0", "3", "dest"]


def test_disk_save(parser):
    n = parse1(parser, '&save "f.txt" -> "contenu"')
    assert isinstance(n, U.DiskNode)
    assert n.cmd == "&save"


def test_disk_list(parser):
    n = parse1(parser, "&list")
    assert isinstance(n, U.DiskNode)
    assert n.cmd == "&list"


def test_shion_ask(parser):
    n = parse1(parser, 'SHION.ASK "question" -> reponse')
    assert isinstance(n, U.ShionNode)
    assert n.operands == ['"question"', "reponse"]


def test_draw_rect(parser):
    n = parse1(parser, "DRAW.RECT 0 -> 0 -> 10 -> 10 -> \"#FF0000\"")
    assert isinstance(n, U.DrawNode)
    assert n.cmd == "DRAW.RECT"
    assert len(n.operands) == 5


def test_key_down(parser):
    n = parse1(parser, 'KEY.DOWN "Left" -> touche')
    assert isinstance(n, U.KeyNode)
    assert n.operands == ['"Left"', "touche"]


def test_package_publish(parser):
    n = parse1(parser, 'PACKAGE.PUBLISH outils -> "outils.uvi" -> "1.0"')
    assert isinstance(n, U.PackageNode)
    assert n.cmd == "PACKAGE.PUBLISH"
    assert len(n.operands) == 3


def test_module_with_alias(parser):
    n = parse1(parser, 'MODULE "math.uvi" -> m')
    assert isinstance(n, U.ModuleNode)
    assert n.alias == "m"


def test_module_without_alias(parser):
    n = parse1(parser, 'MODULE "math.uvi"')
    assert isinstance(n, U.ModuleNode)
    assert n.alias == ""


def test_export_node(parser):
    n = parse1(parser, "EXPORT maFonction")
    assert isinstance(n, U.ExportNode)
    assert n.name == "maFonction"


def test_import_node(parser):
    n = parse1(parser, 'IMPORT "fichier.uvi"')
    assert isinstance(n, U.ImportNode)
    assert n.filename == '"fichier.uvi"'


def test_learn_node(parser):
    n = parse1(parser, '!LEARN "erreur" "correction"')
    assert isinstance(n, U.LearnNode)
    assert n.error == "erreur"
    assert n.correction == "correction"


def test_snippet_node(parser):
    n = parse1(parser, "!SNIPPET boucle")
    assert isinstance(n, U.SnippetNode)
    assert n.pattern == "boucle"


# ---------------------------------------------------------------------
# Robustesse : rien ne doit jamais lever, tout retombe sur un noeud
# (au pire UnknownNode).
# ---------------------------------------------------------------------

@pytest.mark.parametrize("garbage", [
    "~",
    "%%",
    "%",
    "DEF",
    "CALL",
    "CALL (",
    "CALL )(",
    "TAB.PUSH",
    "STR.SLICE",
    "&save",
    "*",
    "**",
    '"unterminated string',
    "((((",
    "))))",
    "= = = =",
    "->->->->",
    "|||||",
    "@",
    "@@@@",
    "@UVI@STOP",
    "?",
    "_",
    "_abc",
    "MODULE",
    "EXPORT",
    "PACKAGE.PUBLISH",
    "KEY.DOWN",
    "DRAW.RECT",
    "SHION.ASK",
    "\t\t\t",
    "     ~   ->   ",
])
def test_garbage_never_raises_and_returns_a_node_or_none(parser, garbage):
    node = parse1(parser, garbage)
    assert node is None or isinstance(node, U.ASTNode)


def test_unknown_syntax_fallback(parser):
    # Une ligne qui ne matche aucun premier-token connu, sans ASSIGN ni
    # PIPE, retombe sur UnknownNode plutôt que de lever.
    n = parse1(parser, "ceci n est pas du uvi valide du tout")
    assert isinstance(n, U.UnknownNode)


def test_parse_program_never_raises_on_mixed_garbage():
    p = new_parser()
    lines = ["x = 1", "~~~", "DEF (((", "! valid", "", "# comment", "@@@@"]
    nodes = p.parse_program(lines)
    assert isinstance(nodes, list)
    # Chaque ligne non vide/non-commentaire produit un noeud.
    assert len(nodes) == 5


def test_parse_program_empty_input():
    p = new_parser()
    assert p.parse_program([]) == []


def test_parse_line_no_tokens_returns_none(parser):
    # Une ligne qui contient seulement des caractères SKIP/COMMENT une
    # fois tokenisée (mais qui n'est pas strip()-vide avant) -- ex. une
    # ligne de seuls espaces est déjà court-circuitée par clean.strip(),
    # documenté ici pour mémoire.
    assert parse1(parser, "   \t  ") is None
