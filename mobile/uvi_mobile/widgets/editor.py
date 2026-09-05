"""Éditeur de code mobile, avec coloration syntaxique du langage UVI.

Kivy fournit `CodeInput`, qui colore via Pygments. Il n'existe évidemment pas
de lexer Pygments pour UVI : `LexerUVI` ci-dessous en est un, écrit à partir
de la grammaire réelle (voir UVILexer/UVIParser dans uvi_engine.py) et non
d'une approximation -- les catégories et les couleurs reprennent celles de
l'éditeur de bureau (classe T : SYN_MOTCLE, SYN_CHAINE, SYN_NOMBRE...).

REPLI SANS PYGMENTS : si Pygments est absent (build Android minimal), on
retombe sur un TextInput ordinaire. On perd la couleur, jamais la capacité
d'écrire du code -- un éditeur qui refuse de s'ouvrir serait un bien pire
défaut qu'un éditeur monochrome.
"""

from __future__ import annotations

from kivy.uix.textinput import TextInput

from ..theme import T, rgba

try:
    from pygments.lexer import RegexLexer, bygroups
    from pygments.token import (Comment, Keyword, Name, Number, Operator,
                                String, Text)
    from pygments.style import Style
    from kivy.uix.codeinput import CodeInput
    PYGMENTS_DISPONIBLE = True
except ImportError:  # pragma: no cover - dépend de l'environnement de build
    PYGMENTS_DISPONIBLE = False


if PYGMENTS_DISPONIBLE:

    class LexerUVI(RegexLexer):
        """Lexer Pygments du langage UVI.

        Suit l'ordre de reconnaissance du vrai parseur : les commandes
        préfixées (TAB./STR./DRAW./KEY./SHION.) avant les identifiants, et
        les marqueurs d'une seule lettre (! ? ~ % * & @ _) avant les
        opérateurs, sinon `!` serait lu comme un opérateur de négation et
        `%%` comme deux modulos."""

        name = "UVI"
        aliases = ["uvi"]
        filenames = ["*.uvi"]

        tokens = {
            "root": [
                (r"#.*$", Comment.Single),
                (r"@UVI\b|@STOP\b", Keyword.Declaration),
                (r'"[^"\n]*"', String.Double),
                # Familles de commandes à préfixe (TAB.PUSH, STR.UPPER, ...)
                (r"\b(TAB|STR|DRAW|KEY|SHION)\.[A-Z]+\b", Name.Builtin),
                (r"\b(TAB|MODULE|IMPORT|EXPORT|DEF|CALL)\b", Keyword),
                # Marqueurs de début d'instruction, à traiter avant les
                # opérateurs arithmétiques (voir docstring).
                (r"!LEARN\b", Keyword.Reserved),
                (r"^\s*!", Keyword.Reserved),          # affichage
                (r"^\s*\?[A-Za-z_.]*", Name.Attribute),  # saisie ?nom / ?E.nom
                (r"^\s*~", Keyword),                   # condition
                (r"^\s*%%", Keyword),                  # tant que
                (r"^\s*%", Keyword),                   # répéter N fois
                (r"^\s*\*[A-Za-z_][\w]*", Name.Function),  # routine
                (r"^\s*&(save|load|list|delete)\b", Name.Builtin),
                (r"^\s*_\d+(\.\d+)?", Number),         # pause _0.5
                (r"->|\|\||\|", Operator),
                (r"\b\d+(\.\d+)?\b", Number),
                (r"\b(AND|OR|NOT)\b", Operator.Word),
                (r"[+\-*/%<>=!]+", Operator),
                (r"\b_retour\b", Name.Variable.Magic),
                (r"\b[A-Za-z_][\w]*(?=\s*\()", Name.Function),
                (r"[A-Za-z_][\w]*", Name.Variable),
                (r"::", Operator),
                (r"\s+", Text),
                (r".", Text),
            ],
        }

    class StyleUVI(Style):
        """Mêmes couleurs que l'éditeur de bureau (classe T)."""

        background_color = T.CHAMP
        styles = {
            Comment.Single: T.SYN_COMMENT,
            Keyword: "bold " + T.SYN_MOTCLE,
            Keyword.Declaration: "bold " + T.CYAN,
            Keyword.Reserved: "bold " + T.SYN_MOTCLE,
            String.Double: T.SYN_CHAINE,
            Number: T.SYN_NOMBRE,
            Name.Builtin: "bold " + T.VIOLET,
            Name.Function: "bold " + T.SYN_FONCTION,
            Name.Attribute: "bold " + T.CYAN,
            Name.Variable: T.TEXTE,
            Name.Variable.Magic: "bold " + T.AMBRE,
            Operator: T.SYN_OPERATEUR,
            Operator.Word: T.SYN_OPERATEUR,
            Text: T.TEXTE,
        }


def _classe_editeur():
    return CodeInput if PYGMENTS_DISPONIBLE else TextInput


class EditeurUVI(_classe_editeur()):
    """Éditeur de scripts .uvi."""

    def __init__(self, **kwargs):
        kwargs.setdefault("font_name", "RobotoMono-Regular")
        kwargs.setdefault("font_size", "14sp")
        kwargs.setdefault("background_color", rgba(T.CHAMP))
        kwargs.setdefault("foreground_color", rgba(T.TEXTE))
        kwargs.setdefault("cursor_color", rgba(T.CYAN))
        kwargs.setdefault("selection_color", rgba(T.BLEU, 0.35))
        # auto_indent : un langage à blocs ouverts par `->` gagne beaucoup à
        # conserver l'indentation courante à chaque retour à la ligne.
        kwargs.setdefault("auto_indent", True)
        if PYGMENTS_DISPONIBLE:
            kwargs.setdefault("lexer", LexerUVI())
            kwargs.setdefault("style", StyleUVI)
        super().__init__(**kwargs)

    @property
    def lignes(self):
        """Le script sous la forme attendue par UVIEngine.run_sequence :
        une liste de lignes sans caractère de fin de ligne."""
        return self.text.splitlines()
