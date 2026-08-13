"""Tests unitaires du lexer (UVILexer.TOKEN_SPEC / tokenize).

Couvre : chaque famille de token de TOKEN_SPEC produit le bon type,
les cas limites (ligne vide, commentaire seul, chaîne avec guillemets
échappés, opérateurs à deux caractères non mutilés), et la non-collision
entre @UVI et @STOP (deux préfixes '@' distincts, un piège classique de
regex si l'ordre ou les frontières \\b sont mal posés)."""
import pytest

from conftest import U


@pytest.fixture
def lexer():
    return U.UVILexer()


def types_of(tokens):
    return [t.type for t in tokens]


def values_of(tokens):
    return [t.value for t in tokens]


# ---------------------------------------------------------------------
# Cas limites généraux
# ---------------------------------------------------------------------

def test_empty_line_yields_no_tokens(lexer):
    assert lexer.tokenize("") == []


def test_whitespace_only_yields_no_tokens(lexer):
    assert lexer.tokenize("   \t  ") == []


def test_comment_only_line_yields_no_tokens(lexer):
    assert lexer.tokenize("# ceci est un commentaire") == []


def test_comment_after_code_is_stripped(lexer):
    toks = lexer.tokenize('! "a" # commentaire de fin de ligne')
    assert types_of(toks) == ["COMMAND", "STRING"]


def test_skip_does_not_produce_tokens(lexer):
    toks = lexer.tokenize("x   =   5")
    assert "SKIP" not in types_of(toks)


# ---------------------------------------------------------------------
# Chaînes
# ---------------------------------------------------------------------

def test_simple_string_strips_quotes(lexer):
    toks = lexer.tokenize('"bonjour"')
    assert len(toks) == 1
    assert toks[0].type == "STRING"
    assert toks[0].value == "bonjour"


def test_string_with_escaped_quote(lexer):
    toks = lexer.tokenize(r'"il dit \"salut\""')
    assert len(toks) == 1
    assert toks[0].type == "STRING"
    # La valeur brute garde l'échappement tel quel (le dé-échappement,
    # s'il existe, est un problème d'évaluation de chaîne, pas du lexer).
    assert toks[0].value == r'il dit \"salut\"'


def test_empty_string_literal(lexer):
    toks = lexer.tokenize('""')
    assert len(toks) == 1
    assert toks[0].type == "STRING"
    assert toks[0].value == ""


def test_two_strings_side_by_side(lexer):
    toks = lexer.tokenize('"a" "b"')
    assert types_of(toks) == ["STRING", "STRING"]
    assert values_of(toks) == ["a", "b"]


# ---------------------------------------------------------------------
# Opérateurs à deux caractères -- CMP2 doit précéder COMMAND/ASSIGN dans
# TOKEN_SPEC (commentaire du code source, ~5562-5573) pour que '==', '!=',
# '>=', '<=' ne soient jamais scindés en deux tokens.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("op", ["==", "!=", ">=", "<="])
def test_two_char_comparison_is_single_token(lexer, op):
    toks = lexer.tokenize(f"a {op} b")
    cmp2 = [t for t in toks if t.type == "CMP2"]
    assert len(cmp2) == 1
    assert cmp2[0].value == op
    # Ne doit jamais apparaître comme deux ASSIGN ou un COMMAND + ASSIGN.
    assert "ASSIGN" not in types_of(toks) or op not in ("==", ">=", "<=")


def test_not_equal_does_not_lose_bang_to_command(lexer):
    # '!=' commence par '!', qui est aussi le préfixe de COMMAND -- CMP2
    # doit gagner, sinon on obtiendrait COMMAND('!') + ASSIGN('=').
    toks = lexer.tokenize("a != b")
    assert types_of(toks) == ["EXPR", "CMP2", "EXPR"]


def test_single_equals_is_assign(lexer):
    toks = lexer.tokenize("x = 5")
    assert types_of(toks) == ["EXPR", "ASSIGN", "EXPR"]


# ---------------------------------------------------------------------
# @UVI vs @STOP -- deux préfixes '@' distincts, ne doivent jamais se
# confondre l'un avec l'autre ni avec un EXPR générique.
# ---------------------------------------------------------------------

def test_at_uvi_signature_token(lexer):
    toks = lexer.tokenize("@UVI")
    assert types_of(toks) == ["SIGNATURE"]


def test_at_stop_token(lexer):
    toks = lexer.tokenize("@STOP")
    assert types_of(toks) == ["STOP_CMD"]


def test_at_uvi_and_at_stop_never_collide(lexer):
    assert types_of(lexer.tokenize("@UVI")) != types_of(lexer.tokenize("@STOP"))
    assert lexer.tokenize("@UVI")[0].type == "SIGNATURE"
    assert lexer.tokenize("@STOP")[0].type == "STOP_CMD"


def test_at_uvi_word_boundary(lexer):
    # \b après @UVI : un identifiant qui commence par "UVI" mais continue
    # (ex. "@UVIX") ne doit pas matcher SIGNATURE.
    toks = lexer.tokenize("@UVIX")
    assert toks[0].type != "SIGNATURE"


# ---------------------------------------------------------------------
# Mots-clés / commandes structurées
# ---------------------------------------------------------------------

def test_if_token(lexer):
    assert types_of(lexer.tokenize("~ x -> y"))[0] == "IF"


def test_while_token_before_for(lexer):
    # '%%' doit se lexer comme WHILE, pas comme deux FOR consécutifs --
    # ordre de TOKEN_SPEC (WHILE avant FOR) est significatif ici.
    toks = lexer.tokenize("%% x -> y")
    assert toks[0].type == "WHILE"
    assert toks[0].value == "%%"


def test_for_token(lexer):
    toks = lexer.tokenize("% 5 -> y")
    assert toks[0].type == "FOR"
    assert toks[0].value == "%"


def test_def_call_tokens(lexer):
    assert lexer.tokenize("DEF")[0].type == "DEF"
    assert lexer.tokenize("CALL")[0].type == "CALL"


def test_routine_token(lexer):
    toks = lexer.tokenize("*maroutine")
    assert toks[0].type == "ROUTINE"
    assert toks[0].value == "*maroutine"


def test_tab_cmd_vs_bare_tab(lexer):
    assert lexer.tokenize("TAB.PUSH")[0].type == "TAB_CMD"
    assert lexer.tokenize("TAB")[0].type == "TAB_CMD"
    assert lexer.tokenize("TAB mon_tab")[0].value == "TAB"


def test_str_cmd_tokens(lexer):
    for sub in ("LEN", "UPPER", "LOWER", "TRIM", "SPLIT", "JOIN",
                "REPLACE", "CONTAINS", "STARTS", "ENDS", "SLICE",
                "INDEX", "NUM"):
        toks = lexer.tokenize(f"STR.{sub}")
        assert toks[0].type == "STR_CMD", f"STR.{sub} devrait être STR_CMD"


def test_disk_cmd_tokens(lexer):
    for cmd in ("&save", "&load", "&list", "&delete"):
        toks = lexer.tokenize(f'{cmd} "f"')
        assert toks[0].type == "DISK_CMD"


def test_shion_ask_token(lexer):
    toks = lexer.tokenize('SHION.ASK "q" -> v')
    assert toks[0].type == "SHION_CMD"


def test_draw_cmd_tokens(lexer):
    for sub in ("CLEAR", "RECT", "CIRCLE", "LINE", "TEXT", "SPRITE",
                "COLOR", "POLY"):
        toks = lexer.tokenize(f"DRAW.{sub}")
        assert toks[0].type == "DRAW_CMD"


def test_key_cmd_tokens(lexer):
    assert lexer.tokenize("KEY.DOWN")[0].type == "KEY_CMD"
    assert lexer.tokenize("KEY.PRESSED")[0].type == "KEY_CMD"


def test_package_publish_token(lexer):
    toks = lexer.tokenize("PACKAGE.PUBLISH")
    assert toks[0].type == "PACKAGE_CMD"


def test_module_export_tokens(lexer):
    assert lexer.tokenize('MODULE "f" -> a')[0].type == "MODULE"
    assert lexer.tokenize("EXPORT nom")[0].type == "EXPORT"


def test_import_token(lexer):
    assert lexer.tokenize('IMPORT "f"')[0].type == "IMPORT"


def test_command_bang_print(lexer):
    toks = lexer.tokenize('! "hello"')
    assert toks[0].type == "COMMAND"
    assert toks[0].value == "!"


def test_command_learn_snippet(lexer):
    assert lexer.tokenize('!LEARN "a" "b"')[0].type == "COMMAND"
    assert lexer.tokenize("!SNIPPET pattern")[0].type == "COMMAND"


def test_input_token(lexer):
    toks = lexer.tokenize("?nom")
    assert toks[0].type == "INPUT"


def test_sleep_token(lexer):
    toks = lexer.tokenize("_0.5")
    assert toks[0].type == "SLEEP"
    assert toks[0].value == "_0.5"


def test_sleep_token_integer(lexer):
    toks = lexer.tokenize("_3")
    assert toks[0].type == "SLEEP"


def test_arrow_else_pipe_tokens(lexer):
    toks = lexer.tokenize('~ x -> a || b')
    types = types_of(toks)
    assert "ARROW" in types
    assert "ELSE_SEP" in types


def test_pipe_vs_else_sep_distinct(lexer):
    # PIPE ('|') et ELSE_SEP ('||') doivent être reconnus distinctement --
    # '||' ne doit jamais se lexer comme deux PIPE consécutifs (l'ordre
    # ELSE_SEP avant PIPE dans TOKEN_SPEC garantit ceci).
    toks = lexer.tokenize("a | b || c")
    types = types_of(toks)
    assert types.count("PIPE") == 1
    assert types.count("ELSE_SEP") == 1


def test_parens_and_comma(lexer):
    toks = lexer.tokenize("CALL f(1, 2)")
    types = types_of(toks)
    assert "LPAREN" in types
    assert "RPAREN" in types
    assert "COMMA" in types


# ---------------------------------------------------------------------
# EXPR générique (fallback)
# ---------------------------------------------------------------------

def test_expr_token_for_bare_identifier(lexer):
    toks = lexer.tokenize("mavariable")
    assert types_of(toks) == ["EXPR"]


def test_expr_token_for_number(lexer):
    toks = lexer.tokenize("42")
    assert types_of(toks) == ["EXPR"]
    assert values_of(toks) == ["42"]


def test_expr_token_for_float(lexer):
    toks = lexer.tokenize("3.14")
    assert values_of(toks) == ["3.14"]


def test_expr_stops_at_reserved_chars(lexer):
    # EXPR = r'[^()|,\s"=]+' -- doit s'arrêter à un espace, une parenthèse,
    # une virgule, un guillemet ou un '='.
    toks = lexer.tokenize("a+b")
    assert values_of(toks) == ["a+b"]  # pas de séparateur ici, un seul EXPR
    toks2 = lexer.tokenize("a,b")
    assert types_of(toks2) == ["EXPR", "COMMA", "EXPR"]


# ---------------------------------------------------------------------
# Ordre des priorités TOKEN_SPEC -- cas de collision documentés dans le
# code source lui-même (commentaires ~5562-5573), vérifiés ici comme des
# garanties de non-régression.
# ---------------------------------------------------------------------

def test_tab_dot_push_not_split_into_tab_and_dot(lexer):
    toks = lexer.tokenize("TAB.PUSH t -> 1")
    assert toks[0].type == "TAB_CMD"
    assert toks[0].value == "TAB.PUSH"


def test_line_hint_propagates_to_tokens(lexer):
    toks = lexer.tokenize('! "x"', line_hint=42)
    assert all(t.line == 42 for t in toks)


def test_newline_token_type_exists_in_spec(lexer):
    # NEWLINE n'apparaît normalement jamais (tokenize reçoit une ligne
    # sans '\n'), mais si un '\n' littéral est présent il doit être
    # reconnu comme NEWLINE, pas planter le regex.
    toks = lexer.tokenize("a\nb")
    assert "NEWLINE" in types_of(toks)
