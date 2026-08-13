"""Tests de @STOP (StopNode) -- sortie anticipée qui arrête TOUT le
programme immédiatement, depuis n'importe quel niveau d'imbrication
(IF, boucle, DEF, routine), dans les DEUX moteurs.

Basé sur le script de validation manuelle
tests_scratch/test_stop.py (scratchpad de cette session), transformé en
suite pytest avec des cas supplémentaires (WHILE, routines, imbrication
profonde, top-level après une boucle qui n'atteint jamais le STOP)."""
import pytest

from conftest import assert_engines_agree, run_tw, run_vm, vm_output_as_str


# ---------------------------------------------------------------------
# 1) @STOP direct, tout en tête.
# ---------------------------------------------------------------------

def test_stop_direct_tree_walker():
    hote, moteur = run_tw('! "avant"\n@STOP\n! "apres"')
    assert hote.sortie == ["avant"]
    assert moteur.stop_requested is True
    assert moteur._stop_line == 2


def test_stop_direct_vm():
    r = run_vm('! "avant"\n@STOP\n! "apres"')
    assert vm_output_as_str(r) == ["avant"]
    assert r.trapped is None  # @STOP n'est pas un TRAP, juste un HALT propre


def test_stop_direct_engines_agree():
    assert_engines_agree('! "avant"\n@STOP\n! "apres"')


# ---------------------------------------------------------------------
# 2) @STOP à l'intérieur d'un IF -- coupe le reste du THEN ET tout le
#    reste du programme top-level, même hors boucle.
# ---------------------------------------------------------------------

def test_stop_in_if_then_branch():
    code = ('x = 1\n'
            '~ x == 1 -> ! "dans if" | @STOP | ! "reste du if" || ! "sinon"\n'
            '! "top-level apres if"')
    hote, moteur = run_tw(code)
    assert hote.sortie == ["dans if"]

    r = run_vm(code)
    assert vm_output_as_str(r) == ["dans if"]


def test_stop_in_if_engines_agree():
    code = ('x = 1\n'
            '~ x == 1 -> ! "dans if" | @STOP | ! "reste du if" || ! "sinon"\n'
            '! "top-level apres if"')
    assert_engines_agree(code)


def test_stop_in_if_else_branch():
    code = ('x = 0\n'
            '~ x == 1 -> ! "then" || ! "avant" | @STOP | ! "apres"\n'
            '! "top-level"')
    hote, moteur = run_tw(code)
    assert hote.sortie == ["avant"]
    r = run_vm(code)
    assert vm_output_as_str(r) == ["avant"]


# ---------------------------------------------------------------------
# 3) @STOP dans une boucle FOR -- arrête la boucle ET le programme
#    entier (pas juste un 'break').
# ---------------------------------------------------------------------

def test_stop_in_for_loop():
    code = '% 5 -> ! "tour" | @STOP\n! "apres boucle"'
    hote, moteur = run_tw(code)
    assert hote.sortie.count("tour") == 1
    assert "apres boucle" not in hote.sortie

    r = run_vm(code)
    out = vm_output_as_str(r)
    assert out.count("tour") == 1
    assert "apres boucle" not in out


def test_stop_in_for_loop_engines_agree():
    assert_engines_agree('% 5 -> ! "tour" | @STOP\n! "apres boucle"')


def test_stop_in_for_loop_deeply_nested_engines_agree():
    # STOP au fond d'un FOR imbriqué dans un FOR.
    code = '% 3 -> % 3 -> ! "inner" | @STOP\n! "never"'
    assert_engines_agree(code)


# ---------------------------------------------------------------------
# 4) @STOP dans une boucle WHILE.
# ---------------------------------------------------------------------

def test_stop_in_while_loop():
    code = 'x = 0\n%% x < 10 -> x = x + 1 | ! x | @STOP\n! "apres"'
    hote, moteur = run_tw(code)
    assert hote.sortie == ["1"]

    r = run_vm(code)
    assert vm_output_as_str(r) == ["1"]


def test_stop_in_while_loop_engines_agree():
    code = 'x = 0\n%% x < 10 -> x = x + 1 | ! x | @STOP\n! "apres"'
    assert_engines_agree(code)


# ---------------------------------------------------------------------
# 5) @STOP à l'intérieur d'une fonction (DEF/CALL) -- arrête tout le
#    programme, pas seulement le retour de la fonction.
# ---------------------------------------------------------------------

def test_stop_in_def_call():
    code = ('DEF F() -> ! "dans f" | @STOP | ! "reste de f"\n'
            'CALL F()\n'
            '! "apres call"')
    hote, moteur = run_tw(code)
    assert hote.sortie == ["dans f"]

    r = run_vm(code)
    assert vm_output_as_str(r) == ["dans f"]


def test_stop_in_def_call_engines_agree():
    code = ('DEF F() -> ! "dans f" | @STOP | ! "reste de f"\n'
            'CALL F()\n'
            '! "apres call"')
    assert_engines_agree(code)


# ---------------------------------------------------------------------
# 6) @STOP à l'intérieur d'une routine.
# ---------------------------------------------------------------------

def test_stop_in_routine():
    code = ('*r -> ! "dans routine" | @STOP | ! "reste de routine"\n'
            '*r\n'
            '! "apres appel routine"')
    hote, moteur = run_tw(code)
    assert hote.sortie == ["dans routine"]

    r = run_vm(code)
    assert vm_output_as_str(r) == ["dans routine"]


def test_stop_in_routine_engines_agree():
    code = ('*r -> ! "dans routine" | @STOP | ! "reste de routine"\n'
            '*r\n'
            '! "apres appel routine"')
    assert_engines_agree(code)


# ---------------------------------------------------------------------
# 7) Script SANS @STOP -- non-régression : rien ne change quand le
#    drapeau reste jamais levé.
# ---------------------------------------------------------------------

def test_no_stop_no_regression():
    code = '% 3 -> ! "boucle"\n! "fin normale"'
    hote, moteur = run_tw(code)
    assert hote.sortie.count("boucle") == 3
    assert "fin normale" in hote.sortie
    assert moteur.stop_requested is False

    r = run_vm(code)
    out = vm_output_as_str(r)
    assert out.count("boucle") == 3
    assert "fin normale" in out


def test_no_stop_engines_agree():
    assert_engines_agree('% 3 -> ! "boucle"\n! "fin normale"')


# ---------------------------------------------------------------------
# 8) @STOP jamais atteint (dans une branche IF non prise) -- ne doit
#    rien arrêter du tout.
# ---------------------------------------------------------------------

def test_stop_in_unreached_branch_does_not_stop():
    code = 'x = 0\n~ x == 1 -> @STOP || ! "sinon"\n! "fin"'
    hote, moteur = run_tw(code)
    assert hote.sortie == ["sinon", "fin"]
    assert moteur.stop_requested is False

    r = run_vm(code)
    assert vm_output_as_str(r) == ["sinon", "fin"]


# ---------------------------------------------------------------------
# 9) @STOP au fond d'une récursion (routines mutuellement récursives).
#
# NOTE : ceci utilise des ROUTINES, pas DEF -- un DEF dont le corps
# contient un IF (comme il en faudrait un pour un cas de base récursif)
# est cassé par le bug documenté dans test_parser.py::
# test_def_body_if_is_corrupted_BUG (_parse_def ne préserve pas les
# flèches internes). Chaque branche de l'IF ci-dessous est réduite à UN
# SEUL statement (@STOP / *dec_and_recurse), pour rester également sous
# le radar du bug de pipe-splitting plus general documenté dans
# test_parser.py (un IF combiné à des pipes internes à SES PROPRES
# branches, imbriqué dans un corps de ROUTINE/WHILE/FOR/DEF, perd aussi
# sa structure -- vérifié empiriquement en préparant cette suite).
# ---------------------------------------------------------------------

def test_stop_deep_in_recursive_calls():
    # Chaque appel imbriqué s'arrête à STOP dès n=0, sans jamais revenir
    # exécuter quoi que ce soit après dans les frames parentes.
    code = ('n = 3\n'
            '*rec -> ~ n == 0 -> @STOP || *dec_and_recurse\n'
            '*dec_and_recurse -> n = n - 1 | *rec\n'
            '*rec\n'
            '! "jamais"')
    hote, moteur = run_tw(code)
    assert hote.sortie == []
    assert moteur.stop_requested is True

    r = run_vm(code)
    assert vm_output_as_str(r) == []
    assert r.trapped is None


def test_stop_deep_in_recursive_calls_engines_agree():
    code = ('n = 3\n'
            '*rec -> ~ n == 0 -> @STOP || *dec_and_recurse\n'
            '*dec_and_recurse -> n = n - 1 | *rec\n'
            '*rec\n'
            '! "jamais"')
    assert_engines_agree(code)


# ---------------------------------------------------------------------
# 10) @STOP comme toute première ligne du programme (rien avant).
# ---------------------------------------------------------------------

def test_stop_as_very_first_line():
    code = '@STOP\n! "jamais"'
    hote, moteur = run_tw(code)
    assert hote.sortie == []
    r = run_vm(code)
    assert vm_output_as_str(r) == []


def test_stop_as_very_first_line_engines_agree():
    assert_engines_agree('@STOP\n! "jamais"')


# ---------------------------------------------------------------------
# 11) Plusieurs @STOP sur le même script -- seul le premier atteint
#     compte (le deuxième n'est jamais exécuté).
# ---------------------------------------------------------------------

def test_multiple_stops_only_first_matters():
    code = '! "a"\n@STOP\n! "b"\n@STOP\n! "c"'
    hote, moteur = run_tw(code)
    assert hote.sortie == ["a"]
    assert moteur._stop_line == 2

    r = run_vm(code)
    assert vm_output_as_str(r) == ["a"]
