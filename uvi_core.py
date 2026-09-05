"""uvi_core.py — IDE de bureau UVI (Tkinter).

Le MOTEUR du langage (lexer, parseur, évaluateur, VM, VFS, agent) vit
désormais dans uvi_engine.py, qui ne dépend d'aucune bibliothèque graphique :
c'est ce qui permet à l'application mobile (mobile/, interface Kivy) de
réutiliser exactement le même interpréteur, Tkinter n'existant ni sur Android
ni sur iOS. Ce fichier ne contient plus que l'interface de bureau.

`from uvi_engine import *` réexporte tout le moteur sous le nom `uvi_core`,
pour que les tests (`import uvi_core as U`) et tout code existant qui
référence uvi_core.UVIEngine, uvi_core.HeadlessHost, etc. continuent de
fonctionner sans modification.
"""

import ast
import ast as pyast
import operator
import time
import json
import math
import os
import random
import sys
import re
import threading
import queue
import subprocess
import tempfile
import urllib.request
import urllib.error
import uvi_auth
import uvi_updater
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Dict, Callable, Union

# Tout le moteur, réexporté sous le nom uvi_core (voir docstring ci-dessus).
from uvi_engine import *  # noqa: F401,F403
# Les noms privés ne passent pas par `import *` et sont pourtant utilisés
# plus bas par l'interface -- importés explicitement plutôt que renommés.
from uvi_engine import (  # noqa: F401
    _default_code_extractor,
    _default_verify_fn,
    _safe_resolve,
    _signaler_crash,
)


def _melanger_couleurs(hex1: str, hex2: str, t: float) -> str:
    """Interpole linéairement entre deux couleurs #RRGGBB. `t=0` -> hex1,
    `t=1` -> hex2. Utilisé par l'animation de pouls du voyant d'état
    (_animer_voyant) pour une transition continue plutôt qu'un clignotement
    binaire -- pas de dépendance graphique supplémentaire, juste de
    l'arithmétique sur trois octets."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(hex1[1:3], 16), int(hex1[3:5], 16), int(hex1[5:7], 16)
    r2, g2, b2 = int(hex2[1:3], 16), int(hex2[3:5], 16), int(hex2[5:7], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return "#%02X%02X%02X" % (r, g, b)


class T:
    """Palette de l'interface UVI v9.

    POURQUOI UNE CLASSE ET PAS DES LITTÉRAUX DISPERSÉS : la version
    précédente écrivait "#0D0D0D" et "#00FF00" en dur à plus de deux cents
    endroits. Changer de thème demandait un rechercher-remplacer sur des
    codes hexadécimaux, avec le risque évident d'en manquer un ou d'en
    remplacer un qui n'était pas un fond de panneau. Une table nommée rend
    le prochain changement de thème mécanique.

    CHANGEMENT D'IDENTITÉ ASSUMÉ. Le projet était en vert-sur-noir
    (esthétique terminal). La v9 passe en bleu profond / cyan. C'est un
    choix délibéré demandé par la maquette, pas une dérive : le vert
    terminal disait « console », le bleu dit « atelier ». Les deux restent
    des thèmes sombres de développeur ; ce qui change, c'est le registre.
    Le vert est CONSERVÉ mais requalifié : il ne signifie plus « UVI », il
    signifie « succès » (sortie OK, statut prêt), ce qui lui rend une
    valeur informative qu'il n'avait plus quand tout était vert."""

    # Fonds, du plus profond au plus clair
    FOND = "#060B18"          # fond de fenêtre
    PANNEAU = "#0B1424"       # cartes et panneaux
    PANNEAU_HAUT = "#0E1A2E"  # en-têtes de panneau
    CHAMP = "#0A1220"         # zones de saisie et de texte
    SURVOL = "#152640"

    # Traits
    BORDURE = "#1B2C48"
    BORDURE_VIVE = "#264066"

    # Textes
    TEXTE = "#C3D6F0"
    TEXTE_FORT = "#EAF2FF"
    TEXTE_FAIBLE = "#5D7599"

    # Accents
    CYAN = "#22D3EE"
    BLEU = "#2E7CF6"
    BLEU_VIF = "#3B82F6"
    VIOLET = "#A855F7"
    ROSE = "#EC4899"
    VERT = "#22C55E"          # succès uniquement
    AMBRE = "#F59E0B"
    ROUGE = "#EF4444"

    # Coloration syntaxique de l'éditeur
    SYN_MOTCLE = "#C084FC"
    SYN_CHAINE = "#FBBF24"
    SYN_NOMBRE = "#22D3EE"
    SYN_COMMENT = "#4B6280"
    SYN_FONCTION = "#38BDF8"
    SYN_OPERATEUR = "#F472B6"

    # Polices
    MONO = ("Consolas", 11)
    MONO_EDIT = ("Consolas", 12)
    MONO_PETIT = ("Consolas", 9)
    UI = ("Segoe UI", 9)
    UI_GRAS = ("Segoe UI", 9, "bold")
    UI_TITRE = ("Segoe UI", 10, "bold")


def dessiner_noyau_uvi(canvas: "tk.Canvas", cx: float, cy: float, rayon: float,
                        *, epaisseur: Optional[float] = None,
                        tags: str = "") -> None:
    """Dessine le « noyau » UVI : trois arcs orbitaux (Univers · Vision ·
    Intelligence) autour d'un point central -- l'identité visuelle du
    projet, dans le même vocabulaire graphique que l'avatar SHION (formes
    Canvas pures, pas d'image bitmap) et que l'ancien badge deux-cercles de
    la barre du haut, dont ce dessin est l'extension directe : le grand
    cercle plein devient trois arcs de couleur, le petit point central est
    conservé tel quel.

    Vectoriel et redessiné à la taille demandée (pas un bitmap figé) : le
    même appel sert au badge 34px de la barre du haut comme à une future
    fenêtre « À propos » en plus grand, sans perte de netteté -- cohérent
    avec le choix déjà fait pour l'avatar SHION (formes recalculées à
    chaque image plutôt qu'image pré-rendue).

    `epaisseur` par défaut à ~28% du rayon : assez épais pour rester lisible
    à 16-20px de rayon, sans manger le disque central aux tailles plus
    grandes."""
    if epaisseur is None:
        epaisseur = max(2.0, rayon * 0.28)
    r_arc = rayon - epaisseur / 2.0
    # 100° d'arc pour 20° de coupure entre les trois orbites : la coupure
    # est ce qui lit visuellement "trois éléments distincts" plutôt qu'un
    # simple anneau plein -- sans elle, le message "trois piliers" du nom
    # UVI (Univers / Vision / Intelligence) disparaît.
    for angle_depart, couleur in (
        (90, T.BLEU_VIF),   # Univers
        (210, T.CYAN),      # Vision
        (330, T.VIOLET),    # Intelligence
    ):
        canvas.create_arc(
            cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc,
            start=angle_depart, extent=100, style=tk.ARC,
            outline=couleur, width=max(1, round(epaisseur)),
            tags=tags,
        )
    r_coeur = rayon * 0.44
    canvas.create_oval(cx - r_coeur, cy - r_coeur, cx + r_coeur, cy + r_coeur,
                        fill=T.CYAN, outline="", tags=tags)
    r_reflet = rayon * 0.16
    dx = -r_coeur * 0.28
    dy = -r_coeur * 0.28
    canvas.create_oval(cx + dx - r_reflet, cy + dy - r_reflet,
                        cx + dx + r_reflet, cy + dy + r_reflet,
                        fill=T.TEXTE_FORT, outline="", tags=tags)


class _Tooltip:
    """Infobulle Tkinter minimale, écrite ici plutôt qu'importée.

    POURQUOI PAS UNE DÉPENDANCE : Tkinter n'a pas d'infobulle native, et les
    bibliothèques qui en fournissent une (tkinter-tooltip, Pmw...) sont des
    paquets supplémentaires à installer. Le projet tourne sur un Raspberry
    Pi où l'on a déjà écarté torch pour cette raison exacte -- ajouter une
    dépendance pip pour AFFICHER UNE PHRASE serait incohérent. Une Toplevel
    sans décoration, c'est 25 lignes et zéro installation.

    Double affichage volontaire (bulle + barre de statut) : la bulle est
    plus lisible, la barre de statut reste visible même si le survol est
    bref ou si le gestionnaire de fenêtres avale les Toplevel sans
    décoration (comportement variable selon les environnements de bureau
    Linux -- constaté, d'où la ceinture ET les bretelles)."""

    def __init__(self, widget, texte, statusbar_fn=None, delai_ms=450):
        self.widget = widget
        self.texte = texte
        self.statusbar_fn = statusbar_fn
        self.delai_ms = delai_ms
        self._fenetre = None
        self._apres_id = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        # <ButtonPress> aussi : sans ça, la bulle reste affichée par-dessus
        # la vue qui vient de changer quand on clique le bouton.
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None):
        if self.statusbar_fn:
            self.statusbar_fn(self.texte)
        self._apres_id = self.widget.after(self.delai_ms, self._afficher)

    def _on_leave(self, _event=None):
        if self._apres_id is not None:
            self.widget.after_cancel(self._apres_id)
            self._apres_id = None
        if self._fenetre is not None:
            self._fenetre.destroy()
            self._fenetre = None
        if self.statusbar_fn:
            self.statusbar_fn(" Prêt.")

    def _afficher(self):
        if self._fenetre is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._fenetre = tk.Toplevel(self.widget)
        self._fenetre.wm_overrideredirect(True)  # pas de barre de titre
        self._fenetre.wm_geometry("+%d+%d" % (x, y))
        tk.Label(
            self._fenetre, text=self.texte, justify=tk.LEFT,
            bg="#1A1A1A", fg="#00FF00", relief=tk.SOLID, borderwidth=1,
            font=("Consolas", 9), wraplength=380, padx=8, pady=5,
        ).pack()


# Textes des infobulles. Séparés de _build_topbar volontairement : la
# construction des boutons est déjà dense, et une table nommée se relit et
# se corrige sans naviguer dans du code Tkinter.
#
# Chaque texte répond à DEUX questions, pas une seule : ce que le bouton
# fait, ET pour qui il est utile. La distinction demandée est réelle --
# certains de ces boutons servent à COMPRENDRE le langage (intérêt
# pédagogique, on regarde la machinerie), d'autres à ÉCRIRE un script
# (intérêt quotidien). Un nouvel arrivant qui lit "AST" n'a aucun moyen de
# deviner dans quelle catégorie il tombe.
TOOLBAR_TOOLTIPS: Dict[str, str] = {
    "run": (
        "▶ EXÉCUTER — moteur complet (tree-walker).\n"
        "Gère TOUT le langage : IMPORT, MODULE, pipes |, DRAW, KEY, SHION.\n"
        "USAGE QUOTIDIEN : c'est celui-ci par défaut, en cas de doute."
    ),
    "run_vm": (
        "⚡ EXÉCUTER (VM) — machine virtuelle bytecode, plus rapide.\n"
        "Ne couvre PAS encore IMPORT ni les pipes | (voir run_script_vm).\n"
        "UTILE pour : les boucles de calcul longues, et vérifier que les deux\n"
        "moteurs donnent bien le même résultat."
    ),
    "ast": (
        "🌳 AST — exporte l'arbre syntaxique du script (ast_export.json).\n"
        "Montre comment le parser a COMPRIS votre code, avant exécution.\n"
        "UTILE pour : apprendre le fonctionnement interne du langage, ou\n"
        "comprendre pourquoi une ligne n'est pas interprétée comme prévu.\n"
        "Pas nécessaire pour écrire un script au quotidien."
    ),
    "bytecode": (
        "⚙ BYTECODE — exporte les instructions compilées (bytecode_export.json).\n"
        "L'étage d'après l'AST : ce que la VM exécute réellement.\n"
        "UTILE pour : apprendre comment un langage se compile.\n"
        "Pas nécessaire pour écrire un script au quotidien."
    ),
    "optimiser": (
        "✨ OPTIMISER — réécrit le bytecode (constantes pré-calculées,\n"
        "code mort retiré) sans changer le comportement du script.\n"
        "UTILE pour : voir ce qu'un optimiseur fait concrètement. Le gain\n"
        "réel est marginal sur un script court."
    ),
    "analyser": (
        "🔍 ANALYSER — relit votre script et signale les défauts probables\n"
        "(variables jamais utilisées, boucles suspectes) SANS l'exécuter.\n"
        "USAGE QUOTIDIEN : à lancer avant d'exécuter un script un peu long."
    ),
    "auto_repair": (
        "🛠 AUTO-RÉPARER — corrections déterministes de la dernière erreur,\n"
        "à partir de règles connues. Rapide, hors ligne, sans IA."
    ),
    "auto_fix": (
        "🤖 AUTO-CORRIGER (agent) — boucle IA autonome : elle modifie le\n"
        "script, l'exécute dans la VM, lit l'erreur, recommence.\n"
        "Demande SHION. Lent (appel réseau ou inférence locale).\n"
        "EXPÉRIMENTAL : pas encore validé sur des cas réels variés."
    ),
    "stats": (
        "📊 STATS — compteurs de la session (mémoire, appels par fonction,\n"
        "erreurs rencontrées)."
    ),
}


# =============================================================================
# SHION AVATAR — système comportemental (état, priorité, micro-animations)
# =============================================================================
#
# CONTRAINTE À CONNAÎTRE AVANT DE LIRE CE QUI SUIT : ce projet est du
# Tkinter pur (aucune dépendance graphique externe, cf. philosophie
# "zéro framework" du reste du dépôt). Le Canvas Tkinter sait dessiner des
# formes (ovales, arcs, lignes), les redessiner en boucle via root.after()
# (voir _rafraichir_horloge pour le précédent déjà dans ce fichier), et
# recevoir des événements souris -- c'est LARGEMENT assez pour un visage
# expressif animé par des formes, des couleurs et du mouvement. Ce qu'il
# NE sait PAS faire nativement : flou, halo (glow) semi-transparent,
# particules -- Tkinter n'a pas de canal alpha sur les couleurs, et
# composer de vraies images nécessiterait Pillow (dépendance absente du
# projet). Le halo est donc APPROCHÉ par des anneaux concentriques de
# teintes pleines plutôt que par un vrai flou gaussien -- assumé, pas un
# oubli. Tout le reste de la demande (machine à états, priorités,
# animations composables, micro-animations, réaction au curseur, event
# bus, API publique, mode debug) est implémenté réellement, pas en
# pseudo-code.
#
# ARCHITECTURE (EVENT -> CONTEXTE -> DÉCISION -> ÉTAT -> ANIMATION -> RENDU) :
#
#   EventBus            -- pub/sub découplé, l'appli émet, l'avatar écoute
#        |
#   ShionBehaviorController -- traduit un événement en demande d'état
#        |                      (la "décision" -- couche volontairement
#        |                      simple aujourd'hui, remplaçable plus tard
#        |                      par quelque chose de plus intelligent
#        |                      sans toucher au reste, voir sa docstring)
#        v
#   ShionEmotionController  -- machine à états : respecte priorité,
#        |                      durée minimale (hystérésis), durée par
#        |                      défaut avant retour à IDLE
#        v
#   ShionAnimationController -- interpole les TRAITS visuels courants
#        |                       vers ceux de l'état actif (easing), et
#        |                       superpose les micro-animations IDLE
#        v
#   ShionAvatarRenderer      -- dessine les traits sur le Canvas, à
#                                ~30 im/s (root.after, jamais un thread :
#                                même règle que le reste de l'UI Tkinter)
#
#   ShionInteractionDetector -- lié aux événements souris du Canvas,
#                                émet lui-même sur l'EventBus (survol,
#                                approche, éloignement)
#
#   ShionAvatar               -- façade publique regroupant tout ça,
#                                voir sa docstring pour l'API.

class AvatarState(str, Enum):
    """Hérite de str : comparable directement à une chaîne (utile dans
    les logs/debug) tout en restant un vrai Enum (fautes de frappe
    détectées à l'usage, pas seulement à l'exécution d'un cas précis)."""
    IDLE = "IDLE"
    CURIOUS = "CURIOUS"
    SURPRISED = "SURPRISED"
    HAPPY = "HAPPY"
    EXCITED = "EXCITED"
    FOCUSED = "FOCUSED"
    THINKING = "THINKING"
    CONFUSED = "CONFUSED"
    ANGRY = "ANGRY"
    SAD = "SAD"
    SLEEPY = "SLEEPY"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


@dataclass(frozen=True)
class _Traits:
    """Paramètres visuels COMPOSABLES d'une expression -- voir §6 de la
    demande d'origine. Un état ne code pas une "animation" au sens
    d'une séquence de dessins : il code un JEU DE VALEURS ici, que
    ShionAnimationController interpole depuis les valeurs courantes.
    C'est cette composition (yeux + bouche + inclinaison + échelle +
    couleur, indépendants les uns des autres) qui permet de décrire
    "curieux" ou "en colère" comme une COMBINAISON plutôt qu'un sprite
    figé par émotion."""
    decalage_x: float = 0.0        # px, tête qui se penche latéralement
    decalage_y: float = 0.0        # px, rebond/affaissement
    inclinaison: float = 0.0       # -1..1, penche la tête (yeux/sourcils asymétriques)
    echelle: float = 1.0           # 1 = taille normale
    ouverture_yeux: float = 1.0    # 0 = fermés, 1 = normal, 1.6 = écarquillés
    ecart_yeux: float = 1.0        # écartement des yeux (1 = normal)
    courbure_bouche: float = 0.0   # -1 triste, 0 neutre, +1 sourire
    ouverture_bouche: float = 0.15  # 0 fermée, 1 grande ouverte
    sourcils: float = 0.0          # -1 froncés/en colère, 0 neutre, +1 relevés/inquiets
    couleur: str = T.CYAN


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease_out_cubic(t: float) -> float:
    """Accélère puis ralentit en douceur vers la cible -- une
    transition "position = nouvellePosition" instantanée est exactement
    ce que §11 de la demande d'origine proscrit."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _lerp_traits(a: _Traits, b: _Traits, t: float) -> _Traits:
    t = _ease_out_cubic(t)
    return _Traits(
        decalage_x=_lerp(a.decalage_x, b.decalage_x, t),
        decalage_y=_lerp(a.decalage_y, b.decalage_y, t),
        inclinaison=_lerp(a.inclinaison, b.inclinaison, t),
        echelle=_lerp(a.echelle, b.echelle, t),
        ouverture_yeux=_lerp(a.ouverture_yeux, b.ouverture_yeux, t),
        ecart_yeux=_lerp(a.ecart_yeux, b.ecart_yeux, t),
        courbure_bouche=_lerp(a.courbure_bouche, b.courbure_bouche, t),
        ouverture_bouche=_lerp(a.ouverture_bouche, b.ouverture_bouche, t),
        sourcils=_lerp(a.sourcils, b.sourcils, t),
        couleur=b.couleur if t >= 0.5 else a.couleur,  # pas d'interpolation de teinte : évite un dégradé boueux
    )


@dataclass(frozen=True)
class _DefEtat:
    priorite: int
    traits: _Traits
    duree_min_s: float = 0.5          # hystérésis : protège contre le zapping (§13)
    duree_defaut_s: Optional[float] = 2.4  # None = reste jusqu'au prochain événement (IDLE, FOCUSED...)


# Table §5 (priorités) + §6 (traits par émotion), un seul endroit pour
# les deux -- une émotion à haute priorité DOIT pouvoir interrompre une
# animation en cours de priorité plus faible (§5, exemple CURIOUS/ERROR).
_DEF_ETATS: Dict[AvatarState, _DefEtat] = {
    AvatarState.IDLE: _DefEtat(
        priorite=0, duree_defaut_s=None,
        traits=_Traits(couleur=T.CYAN),
    ),
    AvatarState.SLEEPY: _DefEtat(
        priorite=5, duree_min_s=1.0, duree_defaut_s=None,
        traits=_Traits(ouverture_yeux=0.25, courbure_bouche=-0.1,
                       decalage_y=1.5, couleur=T.TEXTE_FAIBLE),
    ),
    AvatarState.THINKING: _DefEtat(
        priorite=40, duree_min_s=0.6, duree_defaut_s=None,
        traits=_Traits(inclinaison=0.35, ouverture_yeux=0.8, ecart_yeux=0.9,
                       courbure_bouche=-0.15, couleur=T.VIOLET),
    ),
    AvatarState.PROCESSING: _DefEtat(
        priorite=45, duree_min_s=0.6, duree_defaut_s=None,
        traits=_Traits(echelle=1.0, ouverture_yeux=0.7, ecart_yeux=0.9,
                       courbure_bouche=0.0, couleur=T.BLEU),
    ),
    AvatarState.CURIOUS: _DefEtat(
        priorite=50, duree_min_s=0.5, duree_defaut_s=2.2,
        traits=_Traits(inclinaison=0.5, ouverture_yeux=1.15, courbure_bouche=0.15,
                       sourcils=0.3, couleur=T.CYAN),
    ),
    AvatarState.FOCUSED: _DefEtat(
        priorite=55, duree_min_s=0.8, duree_defaut_s=None,
        traits=_Traits(ouverture_yeux=0.85, ecart_yeux=0.95, courbure_bouche=-0.05,
                       sourcils=-0.15, couleur=T.CYAN),
    ),
    AvatarState.HAPPY: _DefEtat(
        priorite=60, duree_min_s=0.6, duree_defaut_s=2.2,
        traits=_Traits(courbure_bouche=0.85, ouverture_bouche=0.35, ouverture_yeux=0.9,
                       sourcils=0.15, decalage_y=-1.0, couleur=T.VERT),
    ),
    AvatarState.SUCCESS: _DefEtat(
        priorite=65, duree_min_s=0.6, duree_defaut_s=1.8,
        traits=_Traits(courbure_bouche=0.9, ouverture_yeux=0.95, echelle=1.08,
                       decalage_y=-1.5, couleur=T.VERT),
    ),
    AvatarState.EXCITED: _DefEtat(
        priorite=70, duree_min_s=0.6, duree_defaut_s=1.8,
        traits=_Traits(courbure_bouche=0.7, ouverture_bouche=0.5, echelle=1.12,
                       ouverture_yeux=1.2, decalage_y=-2.0, couleur=T.AMBRE),
    ),
    AvatarState.SAD: _DefEtat(
        priorite=75, duree_min_s=1.0, duree_defaut_s=2.6,
        traits=_Traits(courbure_bouche=-0.7, ouverture_yeux=0.6, sourcils=0.4,
                       decalage_y=1.5, echelle=0.95, couleur=T.TEXTE),
    ),
    AvatarState.ANGRY: _DefEtat(
        priorite=80, duree_min_s=0.8, duree_defaut_s=2.2,
        traits=_Traits(sourcils=-0.9, courbure_bouche=-0.4, ouverture_yeux=0.75,
                       ecart_yeux=0.85, couleur=T.ROUGE),
    ),
    AvatarState.CONFUSED: _DefEtat(
        priorite=85, duree_min_s=0.8, duree_defaut_s=2.6,
        traits=_Traits(inclinaison=-0.6, sourcils=0.25, courbure_bouche=-0.25,
                       ecart_yeux=1.1, couleur=T.AMBRE),
    ),
    AvatarState.SURPRISED: _DefEtat(
        priorite=90, duree_min_s=0.4, duree_defaut_s=1.4,
        traits=_Traits(ouverture_yeux=1.6, ouverture_bouche=0.8, echelle=1.1,
                       decalage_y=-1.0, sourcils=0.5, couleur=T.ROSE),
    ),
    AvatarState.ERROR: _DefEtat(
        priorite=100, duree_min_s=0.8, duree_defaut_s=2.0,
        traits=_Traits(sourcils=-0.7, courbure_bouche=-0.5, ouverture_yeux=0.9,
                       echelle=1.05, couleur=T.ROUGE),
    ),
}


@dataclass
class ShionAvatarConfig:
    """§20 : configuration centralisée plutôt que des valeurs éparpillées
    dans le code. Toutes les émotions restent réglables via _DEF_ETATS
    (table dédiée juste au-dessus, séparée pour rester lisible) ; ceci ne
    regroupe que les réglages GLOBAUX au comportement, pas par-émotion."""
    animation_speed: float = 1.0
    idle_intensity: float = 0.35
    spontaneous_behavior: bool = True
    cursor_tracking: bool = True
    debug: bool = False


class EventBus:
    """Pub/sub minimal (§15). Volontairement sans dépendance au reste de
    l'application : ShionAvatar ne connaît QUE cet objet, jamais
    directement l'IDE, l'éditeur ou le moteur -- c'est ce qui permet à
    n'importe quel composant d'émettre des événements vers l'avatar sans
    créer un couplage fort dans les deux sens."""

    def __init__(self):
        self._abonnes: Dict[str, List[Callable[..., None]]] = {}

    def on(self, evenement: str, fn: Callable[..., None]) -> None:
        self._abonnes.setdefault(evenement, []).append(fn)

    def emit(self, evenement: str, **donnees) -> None:
        for fn in list(self._abonnes.get(evenement, ())):
            try:
                fn(**donnees)
            except Exception:
                pass  # un abonné fautif ne doit jamais empêcher les autres de recevoir l'événement


class ShionEmotionController:
    """Machine à états (§4) : priorité (§5) + hystérésis/cooldown (§13).

    Règle centrale, celle qui fait toute la différence avec un simple
    `if event: play(animation)` (§23) : une demande de transition n'est
    honorée QUE si le nouvel état a une priorité supérieure OU ÉGALE à
    l'état courant, OU que la durée minimale de l'état courant est déjà
    écoulée. Un ERROR (100) interrompt donc toujours un CURIOUS (50) en
    cours, mais deux CURIOUS consécutifs répétés en rafale ne se
    chevauchent pas frénétiquement."""

    def __init__(self, sur_changement: Optional[Callable[[AvatarState, AvatarState], None]] = None):
        self.etat = AvatarState.IDLE
        self.etat_precedent = AvatarState.IDLE
        self._depuis = time.time()
        self._sur_changement = sur_changement

    def definition(self, etat: Optional[AvatarState] = None) -> _DefEtat:
        return _DEF_ETATS[etat if etat is not None else self.etat]

    def demander_etat(self, nouvel_etat: AvatarState, forcer: bool = False) -> bool:
        if nouvel_etat == self.etat and not forcer:
            return False
        actuelle = self.definition()
        nouvelle = self.definition(nouvel_etat)
        ecoule = time.time() - self._depuis
        if not forcer and nouvelle.priorite < actuelle.priorite and ecoule < actuelle.duree_min_s:
            return False  # état courant encore protégé (hystérésis, §13)
        ancien = self.etat
        self.etat_precedent = ancien
        self.etat = nouvel_etat
        self._depuis = time.time()
        if self._sur_changement:
            self._sur_changement(ancien, nouvel_etat)
        return True

    def temps_dans_etat(self) -> float:
        return time.time() - self._depuis

    def tick(self) -> None:
        """Retour automatique à IDLE une fois la durée par défaut de
        l'état actif écoulée (§4 "comportement de retour à l'état
        précédent") -- appelé à chaque image par ShionAvatarRenderer."""
        d = self.definition()
        if self.etat != AvatarState.IDLE and d.duree_defaut_s is not None:
            if self.temps_dans_etat() >= d.duree_defaut_s:
                self.demander_etat(AvatarState.IDLE)


class ShionAnimationController:
    """Traduit l'état actif en TRAITS affichés (§6), en interpolant en
    douceur depuis les traits actuellement affichés (§11 -- jamais un
    saut brut), et superpose les micro-animations IDLE (§7 : clignement,
    respiration, petit regard) au-dessus des traits de base -- ADDITIF,
    pas un état de plus dans la machine à états, pour ne jamais entrer
    en conflit avec une émotion "sérieuse" en cours (une micro-animation
    ne s'applique QUE si l'état actif est IDLE, voir step())."""

    _DUREE_TRANSITION_S = 0.35

    def __init__(self, controleur: ShionEmotionController, config: ShionAvatarConfig):
        self._controleur = controleur
        self._config = config
        self._traits_depart = _DEF_ETATS[AvatarState.IDLE].traits
        self._traits_cible = self._traits_depart
        self._debut_transition = time.time()
        self._etat_anime_pour = controleur.etat
        # micro-animations (§7) : chacune a sa probabilité par tick et son cooldown
        self._prochain_clin_oeil = time.time() + random.uniform(2.0, 5.0)
        self._clin_oeil_jusqua = 0.0
        self._prochain_mouvement_tete = time.time() + random.uniform(4.0, 9.0)
        self._decalage_tete_micro = 0.0
        self._t0_respiration = time.time()

    def _traits_courants(self) -> _Traits:
        duree = max(1e-6, self._DUREE_TRANSITION_S / max(0.1, self._config.animation_speed))
        t = (time.time() - self._debut_transition) / duree
        return _lerp_traits(self._traits_depart, self._traits_cible, t)

    def step(self) -> _Traits:
        # Une nouvelle cible est apparue (changement d'état) : on repart
        # de ce qui est AFFICHÉ MAINTENANT (pas de l'ancienne cible), pour
        # qu'une interruption en cours de transition reste fluide plutôt
        # que de sauter en arrière (§19, "les animations doivent être
        # annulables").
        if self._controleur.etat != self._etat_anime_pour:
            self._traits_depart = self._traits_courants()
            self._traits_cible = self._controleur.definition().traits
            self._debut_transition = time.time()
            self._etat_anime_pour = self._controleur.etat

        base = self._traits_courants()

        if self._controleur.etat != AvatarState.IDLE or not self._config.spontaneous_behavior:
            return base

        # --- micro-animations, IDLE uniquement (§7, §14) -----------------
        maintenant = time.time()
        intensite = self._config.idle_intensity

        # respiration : oscillation lente et continue, jamais interrompue
        respiration = math.sin((maintenant - self._t0_respiration) * 1.1) * 0.6 * intensite

        # clignement : probabiliste, cooldown, durée courte
        if maintenant >= self._prochain_clin_oeil and self._clin_oeil_jusqua == 0.0:
            self._clin_oeil_jusqua = maintenant + 0.12
            self._prochain_clin_oeil = maintenant + random.uniform(2.5, 6.0)
        ouverture_yeux = base.ouverture_yeux
        if maintenant < self._clin_oeil_jusqua:
            ouverture_yeux = 0.05
        elif self._clin_oeil_jusqua and maintenant >= self._clin_oeil_jusqua:
            self._clin_oeil_jusqua = 0.0

        # petit mouvement de tête occasionnel : probabilité moyenne, rare
        if maintenant >= self._prochain_mouvement_tete:
            self._decalage_tete_micro = random.uniform(-0.4, 0.4) * intensite
            self._prochain_mouvement_tete = maintenant + random.uniform(5.0, 11.0)

        return _Traits(
            decalage_x=base.decalage_x + self._decalage_tete_micro,
            decalage_y=base.decalage_y + respiration,
            inclinaison=base.inclinaison + self._decalage_tete_micro * 0.5,
            echelle=base.echelle + respiration * 0.01,
            ouverture_yeux=ouverture_yeux,
            ecart_yeux=base.ecart_yeux,
            courbure_bouche=base.courbure_bouche,
            ouverture_bouche=base.ouverture_bouche,
            sourcils=base.sourcils,
            couleur=base.couleur,
        )


class ShionAvatarRenderer:
    """Dessine les TRAITS courants sur un Canvas Tkinter, à ~30 im/s via
    root.after (JAMAIS un thread -- même contrainte que tout le reste de
    cette interface, voir _rafraichir_horloge pour le précédent déjà
    présent dans ce fichier). Le halo est approché par des anneaux
    concentriques opaques plutôt qu'un vrai flou (voir le commentaire en
    tête de section) -- décision assumée, pas un raccourci caché."""

    _INTERVALLE_MS = 33

    def __init__(self, canvas: tk.Canvas, animation: ShionAnimationController,
                 config: ShionAvatarConfig, taille: int = 56):
        self.canvas = canvas
        self._animation = animation
        self._config = config
        self._taille = taille
        self._centre = taille / 2
        self._actif = True
        self._apres_id: Optional[str] = None

    def demarrer(self) -> None:
        if self._apres_id is None:
            self._tick()

    def arreter(self) -> None:
        if self._apres_id is not None:
            try:
                self.canvas.after_cancel(self._apres_id)
            except tk.TclError:
                pass
            self._apres_id = None

    def _tick(self) -> None:
        try:
            if self._config.debug is not None:  # simple lecture pour valider que le canvas existe encore
                pass
            traits = self._animation.step()
            if self._actif:
                self._dessiner(traits)
            self._apres_id = self.canvas.after(self._INTERVALLE_MS, self._tick)
        except tk.TclError:
            self._apres_id = None  # fenêtre/canvas détruits : on arrête proprement, pas d'exception qui remonte

    def _dessiner(self, tr: _Traits) -> None:
        c = self.canvas
        c.delete("all")
        cx = self._centre + tr.decalage_x
        cy = self._centre + tr.decalage_y
        r = self._centre - 3
        r *= max(0.5, tr.echelle)

        # halo (approximation sans flou réel, voir docstring de la classe)
        if r > 4:
            c.create_oval(cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4,
                         outline=tr.couleur, width=1)

        # tête / corps
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=tr.couleur, width=2)

        # yeux (écartement, ouverture, inclinaison composés ensemble)
        ecart = r * 0.42 * tr.ecart_yeux
        oy = max(0.05, tr.ouverture_yeux)
        rayon_oeil = max(0.6, r * 0.16 * min(1.3, oy))
        hauteur_oeil = max(0.6, rayon_oeil * min(1.0, oy))
        decalage_incl = tr.inclinaison * r * 0.12
        for signe in (-1, 1):
            ex = cx + signe * ecart
            ey = cy - r * 0.12 + (decalage_incl if signe < 0 else -decalage_incl) * 0.4
            c.create_oval(ex - rayon_oeil, ey - hauteur_oeil, ex + rayon_oeil, ey + hauteur_oeil,
                         fill=tr.couleur, outline="")
            # sourcil : simple segment, angle piloté par tr.sourcils
            sx0, sx1 = ex - rayon_oeil * 1.3, ex + rayon_oeil * 1.3
            sy = ey - hauteur_oeil - r * 0.18
            pente = -tr.sourcils * r * 0.12 * signe
            c.create_line(sx0, sy + pente * 0.3, sx1, sy - pente * 0.3,
                         fill=tr.couleur, width=2, capstyle=tk.ROUND)

        # bouche : arc dont la courbure et l'ouverture sont composées
        largeur_bouche = r * 0.55
        hauteur_bouche = max(1.0, r * 0.25 * (0.15 + tr.ouverture_bouche))
        by = cy + r * 0.38
        if abs(tr.courbure_bouche) < 0.05 and tr.ouverture_bouche < 0.2:
            c.create_line(cx - largeur_bouche * 0.5, by, cx + largeur_bouche * 0.5, by,
                         fill=tr.couleur, width=2, capstyle=tk.ROUND)
        else:
            depart = 200 if tr.courbure_bouche >= 0 else 20
            etendue = 140
            style = tk.ARC if tr.ouverture_bouche < 0.3 else tk.CHORD
            c.create_arc(cx - largeur_bouche * 0.5, by - hauteur_bouche * (1 if tr.courbure_bouche >= 0 else -1),
                        cx + largeur_bouche * 0.5, by + hauteur_bouche * (1 if tr.courbure_bouche >= 0 else -1),
                        start=depart, extent=etendue, style=style,
                        outline=tr.couleur, width=2)


class ShionInteractionDetector:
    """§8 : réaction au curseur. Lié aux événements natifs Tk du Canvas
    (Enter/Leave/Motion) -- pas de polling ni de calcul de distance à
    chaque frame, le Canvas sait déjà nous dire quand le curseur entre ou
    sort de sa zone."""

    _SURVOL_LONG_S = 1.2

    def __init__(self, canvas: tk.Canvas, bus: EventBus):
        self._canvas = canvas
        self._bus = bus
        self._survol_depuis: Optional[float] = None
        self._apres_id: Optional[str] = None
        canvas.bind("<Enter>", self._survole)
        canvas.bind("<Leave>", self._quitte)

    def _survole(self, _event=None) -> None:
        self._survol_depuis = time.time()
        self._bus.emit("CURSOR_NEAR")
        self._apres_id = self._canvas.after(int(self._SURVOL_LONG_S * 1000), self._verifier_survol_long)

    def _verifier_survol_long(self) -> None:
        if self._survol_depuis is not None:
            self._bus.emit("CURSOR_HOVER_LONG")

    def _quitte(self, _event=None) -> None:
        self._survol_depuis = None
        if self._apres_id is not None:
            try:
                self._canvas.after_cancel(self._apres_id)
            except tk.TclError:
                pass
            self._apres_id = None
        self._bus.emit("CURSOR_LEAVE")


class ShionBehaviorController:
    """§9/§10 : traduit un événement applicatif en demande d'état. Table
    de correspondance délibérément SIMPLE (§24) -- c'est la couche que la
    demande d'origine identifie explicitement comme devant pouvoir être
    remplacée plus tard par quelque chose de plus intelligent (contexte
    de conversation, historique...) SANS toucher au reste de l'avatar :
    tout ce qu'un remplaçant doit faire, c'est appeler
    controleur.demander_etat(...) depuis un abonnement à l'EventBus,
    exactement comme celle-ci."""

    _CORRESPONDANCE: Dict[str, AvatarState] = {
        "USER_MESSAGE_SENT": AvatarState.THINKING,
        "USER_MESSAGE_RECEIVED": AvatarState.HAPPY,
        "TASK_STARTED": AvatarState.FOCUSED,
        "TASK_COMPLETED": AvatarState.SUCCESS,
        "TASK_FAILED": AvatarState.CONFUSED,
        "CODE_EXECUTED": AvatarState.FOCUSED,
        "CODE_FAILED": AvatarState.CONFUSED,
        "ERROR_OCCURRED": AvatarState.SURPRISED,
        "FILE_OPENED": AvatarState.CURIOUS,
        "FILE_SAVED": AvatarState.HAPPY,
        "SEARCH_STARTED": AvatarState.FOCUSED,
        "SEARCH_COMPLETED": AvatarState.IDLE,
        "CURSOR_NEAR": AvatarState.CURIOUS,
        "CURSOR_LEAVE": AvatarState.IDLE,
    }

    # §14 : les animations spontanées / réactions au curseur ne doivent
    # JAMAIS interrompre une interaction sérieuse en cours.
    _PROTEGES = frozenset({
        AvatarState.THINKING, AvatarState.PROCESSING, AvatarState.ERROR,
    })
    _EVENEMENTS_CURSEUR = frozenset({"CURSOR_NEAR", "CURSOR_LEAVE", "CURSOR_HOVER_LONG"})

    def __init__(self, bus: EventBus, controleur: ShionEmotionController):
        self._bus = bus
        self._controleur = controleur
        for evenement in self._CORRESPONDANCE:
            bus.on(evenement, self._sur_evenement_fabrique(evenement))
        bus.on("ERROR_OCCURRED", self._sur_erreur)  # §10 : SURPRISED puis CONFUSED
        bus.on("SET_EMOTION", self._sur_emotion_explicite)

    def _sur_evenement_fabrique(self, evenement: str):
        def _gerer(**_donnees):
            if evenement in self._EVENEMENTS_CURSEUR and self._controleur.etat in self._PROTEGES:
                return  # §14 : ne perturbe pas une interaction sérieuse en cours
            self._controleur.demander_etat(self._CORRESPONDANCE[evenement])
        return _gerer

    def _sur_erreur(self, **_donnees) -> None:
        def _puis_confus():
            if self._controleur.etat == AvatarState.SURPRISED:
                self._controleur.demander_etat(AvatarState.CONFUSED)
        self._controleur.demander_etat(AvatarState.SURPRISED)
        try:
            self._bus_root_after(900, _puis_confus)
        except Exception:
            pass

    def _bus_root_after(self, delai_ms: int, fn) -> None:
        # Petit détour : ShionBehaviorController ne connaît pas root
        # directement (pas de couplage à l'IDE, voir docstring EventBus),
        # mais a besoin d'un after() pour enchaîner SURPRISED -> CONFUSED
        # sans bloquer. On passe par le canvas du renderer si possible ;
        # à défaut (mode debug/tests sans renderer), l'enchaînement est
        # simplement sauté -- SURPRISED retombera à IDLE via son propre
        # duree_defaut_s, ce qui reste un comportement correct.
        widget = getattr(self, "_widget_after", None)
        if widget is not None:
            widget.after(delai_ms, fn)

    def _sur_emotion_explicite(self, etat: Optional[AvatarState] = None,
                               forcer: bool = False, **_ignore) -> None:
        if etat is not None:
            self._controleur.demander_etat(etat, forcer=forcer)


class ShionAvatar:
    """Façade publique du système (§16). C'est le SEUL point d'entrée que
    le reste de l'application doit connaître :

        avatar.set_emotion("CURIOUS")
        avatar.react("TASK_COMPLETED")
        avatar.play("THINKING")             # alias de set_emotion
        avatar.reset()
        avatar.set_intensity(0.8)
        avatar.set_enabled(True)

    Conserve le logo existant (anneau + point) comme état IDLE par
    défaut plutôt que de le remplacer par un design différent (§21) --
    seule la taille du Canvas est agrandie (30px -> ~56px) pour que les
    yeux/bouche restent lisibles, voir _build_colonne_shion."""

    def __init__(self, canvas: tk.Canvas, config: Optional[ShionAvatarConfig] = None):
        self.config = config or ShionAvatarConfig()
        self.bus = EventBus()
        self._controleur = ShionEmotionController()
        self._animation = ShionAnimationController(self._controleur, self.config)
        self.renderer = ShionAvatarRenderer(canvas, self._animation, self.config,
                                            taille=int(canvas["width"]))
        self._interaction = ShionInteractionDetector(canvas, self.bus) if self.config.cursor_tracking else None
        self._comportement = ShionBehaviorController(self.bus, self._controleur)
        self._comportement._widget_after = canvas
        self._debug_fenetre: Optional[tk.Toplevel] = None
        self._debug_apres_id: Optional[str] = None
        if self.config.debug:
            self.ouvrir_debug(canvas.winfo_toplevel())

    # -- API publique (§16) -----------------------------------------------

    def set_emotion(self, nom_etat: str, forcer: bool = False) -> bool:
        try:
            etat = AvatarState(nom_etat)
        except ValueError:
            return False
        return self._controleur.demander_etat(etat, forcer=forcer)

    def play(self, nom_etat: str) -> bool:
        return self.set_emotion(nom_etat, forcer=True)

    def react(self, evenement: str, **donnees) -> None:
        self.bus.emit(evenement, **donnees)

    def reset(self) -> None:
        self._controleur.demander_etat(AvatarState.IDLE, forcer=True)

    def set_intensity(self, valeur: float) -> None:
        self.config.idle_intensity = max(0.0, min(1.0, valeur))

    def set_enabled(self, actif: bool) -> None:
        self.renderer._actif = actif
        if actif:
            self.renderer.demarrer()

    def demarrer(self) -> None:
        self.renderer.demarrer()

    def arreter(self) -> None:
        """Arrête proprement la boucle d'animation -- à appeler à la
        fermeture de l'IDE pour ne laisser aucun root.after() en
        attente sur un widget détruit (§18, "listeners non nettoyés")."""
        self.renderer.arreter()
        if self._debug_apres_id is not None and self._debug_fenetre is not None:
            try:
                self._debug_fenetre.after_cancel(self._debug_apres_id)
            except tk.TclError:
                pass

    # -- mode debug (§17) ---------------------------------------------------

    def ouvrir_debug(self, parent: tk.Misc) -> None:
        if self._debug_fenetre is not None and self._debug_fenetre.winfo_exists():
            self._debug_fenetre.lift()
            return
        fen = tk.Toplevel(parent)
        fen.title("SHION Avatar — Debug")
        fen.configure(bg=T.PANNEAU)
        fen.geometry("340x420")
        self._debug_fenetre = fen

        etiquette_etat = tk.Label(fen, justify=tk.LEFT, anchor="w", bg=T.PANNEAU,
                                  fg=T.TEXTE, font=("Consolas", 9))
        etiquette_etat.pack(fill=tk.X, padx=10, pady=10)

        def _rafraichir():
            if not fen.winfo_exists():
                return
            d = self._controleur.definition()
            etiquette_etat.config(text=(
                "État courant   : %s\n"
                "État précédent  : %s\n"
                "Priorité        : %d\n"
                "Depuis          : %.1fs\n"
                "Durée min       : %.1fs\n"
                "Durée par défaut: %s\n"
                "Intensité idle  : %.2f\n"
                "Suivi curseur   : %s\n"
                "Spontané        : %s"
            ) % (
                self._controleur.etat.value, self._controleur.etat_precedent.value,
                d.priorite, self._controleur.temps_dans_etat(), d.duree_min_s,
                "∞" if d.duree_defaut_s is None else "%.1fs" % d.duree_defaut_s,
                self.config.idle_intensity, self.config.cursor_tracking,
                self.config.spontaneous_behavior,
            ))
            self._debug_apres_id = fen.after(200, _rafraichir)

        grille = tk.Frame(fen, bg=T.PANNEAU)
        grille.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        etats_testables = (
            AvatarState.IDLE, AvatarState.CURIOUS, AvatarState.SURPRISED,
            AvatarState.HAPPY, AvatarState.EXCITED, AvatarState.FOCUSED,
            AvatarState.THINKING, AvatarState.CONFUSED, AvatarState.ANGRY,
            AvatarState.SAD, AvatarState.SLEEPY, AvatarState.PROCESSING,
            AvatarState.SUCCESS, AvatarState.ERROR,
        )
        for i, etat in enumerate(etats_testables):
            btn = tk.Button(grille, text=etat.value, bg=T.PANNEAU_HAUT, fg=T.CYAN,
                            activebackground=T.BORDURE, relief=tk.FLAT,
                            font=("Consolas", 8, "bold"), cursor="hand2",
                            command=lambda e=etat: self.play(e.value))
            btn.grid(row=i // 2, column=i % 2, sticky="nsew", padx=3, pady=3)
        grille.columnconfigure(0, weight=1)
        grille.columnconfigure(1, weight=1)

        _rafraichir()


# ==============================================================================
# INTERNATIONALISATION DE L'INTERFACE (pas du langage UVI ni des messages
# produits PENDANT l'exécution d'un script -- voir la portée exacte
# ci-dessous).
#
# Clé = texte SOURCE en français, tel qu'écrit ailleurs dans ce fichier --
# pas un identifiant abstrait séparé (pas de "PARAMS_TITLE" à maintenir
# en plus du code). UVIIDE._t(texte) traduit vers self.langue ; l'absence
# d'entrée pour une clé, ou langue == "fr", retombe TOUJOURS proprement
# sur le texte français d'origine (jamais de clé brute affichée).
#
# PORTÉE ASSUMÉE, DITE EXPLICITEMENT : couvre le CHROME de l'interface
# (rail, onglets, barre d'outils, titres de panneaux, PARAMÈTRES) --
# c'est ce qu'un utilisateur veut dire par « changer la langue de
# l'IDE ». NE COUVRE PAS les milliers de messages générés PENDANT
# l'exécution d'un script (logs, erreurs UVIError, sorties de !) : ces
# textes sont produits par le MOTEUR (UVIEvaluator/UVIVirtualMachine),
# pas par l'IDE, et les traduire tous aurait exigé de toucher des
# centaines de sites d'émission d'erreur à travers tout le fichier --
# un chantier séparé, bien plus large que « changer la langue de
# l'interface », hors de portée ici. Ce que l'utilisateur ÉCRIT (le
# langage UVI lui-même) n'est de toute façon pas concerné : ~/%/DEF/CALL
# etc. restent des mots-clés fixes, indépendants de la langue de l'IDE.
LANGUES = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "zh": "中文",
    "hi": "हिन्दी",
}

TRANSLATIONS = {
    # --- rail + titres de panneaux (colonne gauche) -----------------------
    "EXPLORER":  {"en": "EXPLORER",   "es": "EXPLORADOR",   "zh": "资源管理器", "hi": "एक्सप्लोरर"},
    "EXPLORATEUR UVI": {"en": "UVI EXPLORER", "es": "EXPLORADOR UVI", "zh": "UVI 资源管理器", "hi": "UVI एक्सप्लोरर"},
    "RECHERCHE": {"en": "SEARCH",     "es": "BUSCAR",        "zh": "搜索",       "hi": "खोजें"},
    "VARIABLES": {"en": "VARIABLES",  "es": "VARIABLES",     "zh": "变量",       "hi": "चर"},
    "OUTILS":    {"en": "TOOLS",      "es": "HERRAMIENTAS",  "zh": "工具",       "hi": "उपकरण"},
    "MODULES":   {"en": "MODULES",    "es": "MÓDULOS",       "zh": "模块",       "hi": "मॉड्यूल"},
    "MODULES & PAQUETS": {"en": "MODULES & PACKAGES", "es": "MÓDULOS Y PAQUETES", "zh": "模块与包", "hi": "मॉड्यूल और पैकेज"},
    "INSPECTEUR DE VARIABLES": {"en": "VARIABLE INSPECTOR", "es": "INSPECTOR DE VARIABLES", "zh": "变量检查器", "hi": "चर निरीक्षक"},
    "OUTILS UVI": {"en": "UVI TOOLS", "es": "HERRAMIENTAS UVI", "zh": "UVI 工具", "hi": "UVI उपकरण"},
    "TESTS":     {"en": "TESTS",      "es": "PRUEBAS",       "zh": "测试",       "hi": "परीक्षण"},
    "PARAMÈTRES": {"en": "SETTINGS",  "es": "AJUSTES",       "zh": "设置",       "hi": "सेटिंग्स"},

    # --- onglets de la console (dock inférieur) ----------------------------
    "CONSOLE UVI": {"en": "UVI CONSOLE", "es": "CONSOLA UVI", "zh": "UVI 控制台", "hi": "UVI कंसोल"},
    "SORTIE":      {"en": "OUTPUT",      "es": "SALIDA",      "zh": "输出",       "hi": "आउटपुट"},
    "ERREURS":     {"en": "ERRORS",      "es": "ERRORES",     "zh": "错误",       "hi": "त्रुटियाँ"},
    "TERMINAL":    {"en": "TERMINAL",    "es": "TERMINAL",    "zh": "终端",       "hi": "टर्मिनल"},
    "DÉBOGUEUR":   {"en": "DEBUGGER",    "es": "DEPURADOR",   "zh": "调试器",     "hi": "डिबगर"},
    "AGENT":       {"en": "AGENT",       "es": "AGENTE",      "zh": "代理",       "hi": "एजेंट"},
    "CONSOLE":     {"en": "CONSOLE",     "es": "CONSOLA",     "zh": "控制台",     "hi": "कंसोल"},

    # --- onglets de l'espace de travail central -----------------------------
    "◧ ÉDITEUR": {"en": "◧ EDITOR", "es": "◧ EDITOR", "zh": "◧ 编辑器", "hi": "◧ संपादक"},
    "▦ CANEVAS": {"en": "▦ CANVAS", "es": "▦ LIENZO", "zh": "▦ 画布",   "hi": "▦ कैनवस"},

    # --- barre d'outils supérieure ------------------------------------------
    "NOUVEAU":      {"en": "NEW",        "es": "NUEVO",        "zh": "新建",     "hi": "नया"},
    "OUVRIR":       {"en": "OPEN",       "es": "ABRIR",        "zh": "打开",     "hi": "खोलें"},
    "SAUVEGARDER":  {"en": "SAVE",       "es": "GUARDAR",      "zh": "保存",     "hi": "सहेजें"},
    "▶  EXÉCUTER":  {"en": "▶  RUN",     "es": "▶  EJECUTAR",  "zh": "▶  运行",  "hi": "▶  चलाएँ"},
    "⏹  ARRÊTER":   {"en": "⏹  STOP",    "es": "⏹  DETENER",   "zh": "⏹  停止",  "hi": "⏹  रोकें"},
    "DÉBOGUEUR_BTN": {"en": "DEBUGGER", "es": "DEPURADOR", "zh": "调试器", "hi": "डिबगर"},
    "AUTO-CORRIGER": {"en": "AUTO-FIX", "es": "AUTOCORREGIR", "zh": "自动修复", "hi": "स्वतः सुधार"},

    # --- PARAMÈTRES : titres de cartes --------------------------------------
    "SERVEUR SHION": {"en": "SHION SERVER", "es": "SERVIDOR SHION", "zh": "SHION 服务器", "hi": "SHION सर्वर"},
    "MODÈLE LOCAL (llama.cpp, CPU)": {
        "en": "LOCAL MODEL (llama.cpp, CPU)", "es": "MODELO LOCAL (llama.cpp, CPU)",
        "zh": "本地模型（llama.cpp，CPU）", "hi": "स्थानीय मॉडल (llama.cpp, CPU)"},
    "RAPPORTS DE BUGS": {"en": "BUG REPORTS", "es": "INFORMES DE ERRORES", "zh": "错误报告", "hi": "बग रिपोर्ट"},
    "ÉTAT DU SYSTÈME": {"en": "SYSTEM STATUS", "es": "ESTADO DEL SISTEMA", "zh": "系统状态", "hi": "सिस्टम स्थिति"},
    "LANGUE": {"en": "LANGUAGE", "es": "IDIOMA", "zh": "语言", "hi": "भाषा"},

    # --- PARAMÈTRES : champs et boutons -------------------------------------
    "Adresse du serveur": {"en": "Server address", "es": "Dirección del servidor",
                            "zh": "服务器地址", "hi": "सर्वर पता"},
    "Nom du modèle": {"en": "Model name", "es": "Nombre del modelo",
                       "zh": "模型名称", "hi": "मॉडल नाम"},
    "Clé d'API": {"en": "API key", "es": "Clave de API", "zh": "API 密钥", "hi": "एपीआई कुंजी"},
    "Température": {"en": "Temperature", "es": "Temperatura", "zh": "温度", "hi": "तापमान"},
    "Budget de tokens": {"en": "Token budget", "es": "Presupuesto de tokens",
                          "zh": "令牌预算", "hi": "टोकन बजट"},
    "Appliquer": {"en": "Apply", "es": "Aplicar", "zh": "应用", "hi": "लागू करें"},
    "Tester": {"en": "Test", "es": "Probar", "zh": "测试", "hi": "जाँचें"},
    "Arrêter": {"en": "Stop", "es": "Detener", "zh": "停止", "hi": "रोकें"},
    "Ouvrir le dossier": {"en": "Open folder", "es": "Abrir carpeta",
                           "zh": "打开文件夹", "hi": "फ़ोल्डर खोलें"},

    # --- titre de fenêtre -----------------------------------------------
    # Clé formée avec UVI_VERSION (définie en tête de fichier) plutôt qu'un
    # littéral "v9.0.0" figé -- sinon cette entrée cesserait de matcher dès
    # le prochain bump de version et _t() retomberait sur le texte français
    # tel quel dans toutes les langues (repli de _t, voir sa docstring).
    "UVI OS v%s — UNIVERSE · VISION · INTELLIGENCE" % UVI_VERSION: {
        "en": "UVI OS v%s — UNIVERSE · VISION · INTELLIGENCE" % UVI_VERSION,
        "es": "UVI OS v%s — UNIVERSO · VISIÓN · INTELIGENCIA" % UVI_VERSION,
        "zh": "UVI OS v%s — 宇宙 · 视野 · 智能" % UVI_VERSION,
        "hi": "UVI OS v%s — ब्रह्मांड · दृष्टि · बुद्धिमत्ता" % UVI_VERSION,
    },

    # --- compte / abonnement (voir docs/COMPTES_ABONNEMENTS.md) ------------
    "Se connecter avec Google": {
        "en": "Sign in with Google", "es": "Iniciar sesión con Google",
        "zh": "使用 Google 登录", "hi": "Google से साइन इन करें"},
    "Se connecter": {"en": "Sign in", "es": "Iniciar sesión", "zh": "登录", "hi": "साइन इन करें"},
    "Se déconnecter": {"en": "Sign out", "es": "Cerrar sesión", "zh": "退出登录", "hi": "साइन आउट करें"},
    "Non connecté": {"en": "Not signed in", "es": "No conectado", "zh": "未登录", "hi": "साइन इन नहीं है"},
    "Connexion UVI": {"en": "UVI sign-in", "es": "Conexión UVI", "zh": "UVI 登录", "hi": "UVI साइन-इन"},
    "Un compte UVI est nécessaire pour utiliser UVI.": {
        "en": "A UVI account is required to use UVI.",
        "es": "Se requiere una cuenta UVI para usar UVI.",
        "zh": "使用 UVI 需要一个 UVI 账户。",
        "hi": "UVI का उपयोग करने के लिए UVI खाता आवश्यक है।"},
    "Ouverture du navigateur pour la connexion Google...": {
        "en": "Opening browser for Google sign-in...",
        "es": "Abriendo el navegador para iniciar sesión con Google...",
        "zh": "正在打开浏览器以进行 Google 登录…",
        "hi": "Google साइन-इन के लिए ब्राउज़र खोला जा रहा है..."},
    "Déconnecté.": {"en": "Signed out.", "es": "Sesión cerrada.", "zh": "已退出登录。", "hi": "साइन आउट हो गया।"},

    # --- auto-mise-à-jour (voir uvi_updater.py et docs/MISE_A_JOUR.md) -----
    "Vérifier les mises à jour": {
        "en": "Check for updates", "es": "Buscar actualizaciones",
        "zh": "检查更新", "hi": "अपडेट जाँचें"},
    "UVI est à jour (v%s).": {
        "en": "UVI is up to date (v%s).", "es": "UVI está actualizado (v%s).",
        "zh": "UVI 已是最新版本（v%s）。", "hi": "UVI अद्यतित है (v%s)।"},
    "Mise à jour disponible": {
        "en": "Update available", "es": "Actualización disponible",
        "zh": "有可用更新", "hi": "अपडेट उपलब्ध है"},
    "UVI v%s est disponible (tu as v%s).": {
        "en": "UVI v%s is available (you have v%s).",
        "es": "UVI v%s está disponible (tienes v%s).",
        "zh": "UVI v%s 可用（当前版本 v%s）。",
        "hi": "UVI v%s उपलब्ध है (आपके पास v%s है)।"},
    "Plus tard": {"en": "Later", "es": "Más tarde", "zh": "稍后", "hi": "बाद में"},
    "Mettre à jour": {"en": "Update", "es": "Actualizar", "zh": "更新", "hi": "अपडेट करें"},
    "Téléchargement...": {
        "en": "Downloading...", "es": "Descargando...", "zh": "正在下载…", "hi": "डाउनलोड हो रहा है..."},
    "Téléchargement... %d%%": {
        "en": "Downloading... %d%%", "es": "Descargando... %d%%",
        "zh": "正在下载… %d%%", "hi": "डाउनलोड हो रहा है... %d%%"},
    "Installation en cours -- UVI va se fermer et redémarrer.": {
        "en": "Installing -- UVI will close and restart.",
        "es": "Instalando -- UVI se cerrará y reiniciará.",
        "zh": "正在安装 -- UVI 即将关闭并重启。",
        "hi": "इंस्टॉल हो रहा है -- UVI बंद होकर पुनः आरंभ होगा।"},
}
# ==============================================================================


class UVIIDE:
    def __init__(self):
        # Langue de l'interface (voir TRANSLATIONS/_t/_tr plus haut) --
        # chargée AVANT toute construction de widget, puisque _tr() lit
        # self.langue au moment même où chaque widget traduisible est créé.
        # "en" par défaut si aucune préférence sauvegardée -- demande
        # explicite (l'anglais est la langue par défaut, pas le français
        # dans lequel ce fichier est écrit).
        self._widgets_traduits = []
        self.langue = self._charger_langue_sauvegardee()
        self.vfs = VFSManager()
        # Système de paquets (ajout de cette session) : attaché à self.vfs
        # APRÈS construction plutôt qu'ajouté au constructeur de
        # VFSManager -- UVIPackageRegistry a besoin de storage_dir, que
        # VFSManager connaît déjà, donc un attribut posé ici évite de
        # changer la signature de VFSManager (une seule instanciation
        # dans tout le fichier, mais autant ne pas y toucher) et
        # n'importe quel _resolve_module_path (VM/tree-walker) peut
        # accéder à self.vfs.package_registry sans dépendance
        # supplémentaire à propager.
        self.vfs.package_registry = UVIPackageRegistry(self.vfs.storage_dir)
        # Installe la bibliothèque standard (idempotent -- n'écrase
        # jamais un fichier .stdlib déjà présent, voir docstring de
        # ensure_stdlib_installed) : pas de coût significatif à le
        # faire à chaque session, un simple os.path.exists par fichier.
        self.vfs.package_registry.ensure_stdlib_installed(STDLIB_SOURCES)
        self.engine = UVIEngine(self, self.vfs)
        self.agent = IAAgent(self.engine, self, self.vfs)
        self.engine.set_agent(self.agent)
        self.cmd_history = []
        self.history_index = 0
        self.is_waiting_input = False
        self.current_suggestion = None
        self._lock_highlight = False
        # Conseils déjà montrés dans cette session (voir _conseil_unique) --
        # sert aux messages "première fois" de l'étape 7.5.
        self._conseils_vus: set = set()
        # Fichier ouvert et état de modification. Sans ce suivi, rien
        # n'indiquait qu'un travail n'était pas enregistré -- on fermait la
        # fenêtre en croyant avoir sauvegardé.
        self.fichier_courant = None
        self.modifie = False
        # File des travaux d'interface demandés par les threads secondaires.
        # Voir _poster_ui / _pomper_file_ui : c'est le SEUL canal autorisé
        # entre un thread d'exécution et Tkinter.
        self._file_ui = queue.Queue()
        # ÉTAPE 7.1 : le premier lancement doit être détecté AVANT
        # think_and_act(), qui crée init.uvi et rend donc le VFS non vide.
        # Le tester après aurait été un faux négatif permanent -- personne
        # n'aurait jamais vu le message d'accueil.
        premier_lancement = not self.vfs.list_files()
        # Serveur llama.cpp local (server/serve_llamacpp.py) lancé DEPUIS
        # l'IDE -- voir _definir_modele_local. None tant qu'aucun préréglage
        # n'a été démarré depuis ce panneau (un serveur lancé à la main
        # dans un terminal externe n'apparaît pas ici, par construction).
        self._processus_llm_local: Optional[subprocess.Popen] = None
        self._preset_llm_local_actif: Optional[str] = None
        self._build_window()
        self.input_var = tk.StringVar()
        self._build_ui()
        self.show_editor()
        self.update_vfs_list()
        self.agent.think_and_act()
        if premier_lancement:
            self._afficher_accueil()
        # Porte de connexion (voir docs/COMPTES_ABONNEMENTS.md) -- APRÈS
        # _build_ui() : _poster_ui/_pomper_file_ui (démarré en fin de
        # _build_ui) doivent déjà tourner pour que le thread de connexion
        # puisse faire remonter son statut vers ce dialogue. No-op complet
        # si uvi_auth.is_configured() est faux.
        self._gate_connexion_obligatoire()
        # No-op si uvi_updater.UPDATE_MANIFEST_URL est injoignable ou si UVI
        # est déjà à jour (voir _verifier_maj_au_demarrage) -- jamais
        # bloquant, contrairement à la porte de connexion ci-dessus.
        self._verifier_maj_au_demarrage()

    def _afficher_accueil(self):
        """Message d'accueil du tout premier lancement (étape 7.1).

        Délibérément un MESSAGE DANS LE TERMINAL et pas un assistant modal :
        l'IDE affiche déjà tout le reste comme ça (log de l'agent, erreurs,
        sortie des scripts), et une fenêtre modale au premier démarrage
        serait le seul élément d'interface du projet à ne pas ressembler au
        projet. L'objectif est de rendre le démarrage informatif, pas de
        changer la personnalité de l'outil.

        Trois choses seulement, parce que c'est ce qu'un nouvel arrivant ne
        peut PAS deviner en regardant l'écran : ce qu'est UVI, pourquoi il y
        a deux boutons EXÉCUTER, et où trouver un exemple qui marche."""
        self.show_terminal()
        self.log("", "info")
        self.log("╔══════════════════════════════════════════════════════════════╗", "sys")
        self.log("║  BIENVENUE DANS UVI                                          ║", "sys")
        self.log("╚══════════════════════════════════════════════════════════════╝", "sys")
        self.log(
            "UVI est un langage de script complet — lexer, parser, deux moteurs "
            "d'exécution, modules, débogueur — et cet IDE est son atelier. Tout "
            "script commence par la ligne @UVI.", "info",
        )
        self.log(
            "DEUX BOUTONS D'EXÉCUTION, et ce n'est pas un doublon : "
            "▶ EXÉCUTER utilise le moteur complet (tout le langage : IMPORT, "
            "MODULE, pipes, DRAW, KEY). ⚡ EXÉCUTER (VM) compile en bytecode et "
            "va plus vite, mais ne couvre pas encore IMPORT ni les pipes. "
            "En cas de doute : ▶ EXÉCUTER.", "info",
        )
        self.log(
            "POUR COMMENCER : double-cliquez sur 'init.uvi' dans l'explorateur "
            "VFS à gauche, ou tapez  demo  ici même pour charger un exemple "
            "commenté dans l'éditeur.", "info",
        )
        self.log(
            "Survolez n'importe quel bouton de la barre du haut pour savoir ce "
            "qu'il fait et s'il vous sera utile.", "info",
        )
        self.log("", "info")

    def _conseil_unique(self, cle: str, message: str) -> None:
        """Affiche `message` une seule fois par session (étapes 7.5).

        Un conseil affiché à chaque clic devient du bruit qu'on apprend à
        ignorer — et il serait alors ignoré aussi la fois où il compte. Une
        fois par session : assez pour qu'un nouvel arrivant le lise, assez
        peu pour qu'un habitué ne le subisse pas.

        Pas de persistance sur disque volontairement : l'état vit dans
        self._conseils_vus, donc le conseil réapparaît à la session
        suivante. C'est un compromis assumé — écrire un fichier de
        préférences pour ça ajouterait un état à gérer, à migrer et à
        déboguer pour un gain minime."""
        if cle in self._conseils_vus:
            return
        self._conseils_vus.add(cle)
        self.log(message, "sys")

    # ------------------------------------------------------------------
    # Internationalisation de l'interface (voir TRANSLATIONS plus haut
    # pour la portée exacte -- le chrome de l'IDE, pas les messages émis
    # PENDANT l'exécution d'un script).
    # ------------------------------------------------------------------
    _FICHIER_PREFS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".uvi_prefs.json")

    def _charger_langue_sauvegardee(self) -> str:
        try:
            with open(self._FICHIER_PREFS, "r", encoding="utf-8") as f:
                code = json.load(f).get("langue")
            if code in LANGUES:
                return code
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return "en"

    def _sauvegarder_prefs_langue(self) -> None:
        # BUGFIX : json.dump({"langue": ...}) écrasait TOUT le fichier --
        # correct tant que "langue" était l'unique clé de .uvi_prefs.json,
        # mais uvi_auth.save_session() y écrit désormais aussi une clé
        # "auth" (session de connexion). Changer de langue effaçait donc
        # silencieusement la session de quiconque était connecté. On lit
        # le contenu existant et on ne touche que la clé "langue" --
        # même correctif que celui déjà appliqué à _safe_store ailleurs
        # dans ce fichier pour ne jamais écraser un état qu'on ne
        # possède pas.
        donnees = {}
        try:
            with open(self._FICHIER_PREFS, "r", encoding="utf-8") as f:
                lu = json.load(f)
                if isinstance(lu, dict):
                    donnees = lu
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        donnees["langue"] = self.langue
        try:
            with open(self._FICHIER_PREFS, "w", encoding="utf-8") as f:
                json.dump(donnees, f, ensure_ascii=False)
        except OSError:
            pass  # préférence non persistée -- pas fatal, juste pas mémorisée

    def _t(self, texte: str) -> str:
        """Traduit `texte` (le texte SOURCE en français, pas une clé
        abstraite séparée) vers self.langue. Retombe TOUJOURS sur `texte`
        inchangé si aucune traduction n'existe pour cette clé+langue --
        couvre nativement le français (langue source, aucune entrée à
        maintenir pour elle) et dégrade proprement une clé oubliée en
        français plutôt qu'en identifiant brut illisible."""
        return TRANSLATIONS.get(texte, {}).get(self.langue, texte)

    def _tr(self, widget, texte_source: str, option: str = "text"):
        """Enregistre `widget` comme traduisible sous la clé `texte_source`
        (voir _t) et applique IMMÉDIATEMENT la traduction pour self.langue.
        Appelée à la CONSTRUCTION de chaque widget de chrome qu'on veut
        voir changer de langue sans redémarrer l'IDE -- _appliquer_langue
        rejoue ensuite le registre à chaque changement. Retourne `widget`
        (permet de chaîner : self._tr(tk.Label(...), "X").pack())."""
        def _rafraichir():
            try:
                widget.config(**{option: self._t(texte_source)})
            except tk.TclError:
                pass  # widget détruit entre-temps (panneau reconstruit)
        self._widgets_traduits.append(_rafraichir)
        _rafraichir()
        return widget

    def _tr_personnalise(self, rafraichir_fn):
        """Variante de _tr pour un widget dont le texte affiché n'est PAS
        directement _t(texte_source) tel quel -- ex. _bouton_plat, qui
        compose icône + texte en une seule chaîne. `rafraichir_fn` est un
        callable SANS argument qui réapplique lui-même le texte correct
        pour self.langue à chaque appel ; enregistré dans le même registre
        que _tr, rejoué de la même façon par _appliquer_langue."""
        self._widgets_traduits.append(rafraichir_fn)
        rafraichir_fn()

    # ------------------------------------------------------------------
    # Connexion Google et niveau d'abonnement (voir uvi_auth.py et
    # docs/COMPTES_ABONNEMENTS.md). No-op complet tant que
    # uvi_auth.is_configured() est faux -- comportement de développement
    # local historique, inchangé (self.badge_compte reste None).
    # ------------------------------------------------------------------
    def _rafraichir_badge_compte(self) -> None:
        if self.badge_compte is None:
            return
        session = uvi_auth.current_session()
        if session is None:
            self.lbl_compte.config(text=self._t("Non connecté"))
            self.btn_compte.config(text=self._t("Se connecter"), command=self._sur_clic_connexion)
        else:
            niveau = str(session.get("tier", "free")).upper()
            self.lbl_compte.config(text="%s · %s" % (session.get("email", "?"), niveau))
            self.btn_compte.config(text=self._t("Se déconnecter"), command=self._sur_clic_deconnexion)

    def _sur_clic_connexion(self) -> None:
        self._afficher_dialogue_connexion(bloquant=False)

    def _sur_clic_deconnexion(self) -> None:
        uvi_auth.logout()
        self.engine._shion_clients_par_niveau.clear()
        self._rafraichir_badge_compte()
        self._set_status(self._t("Déconnecté."))

    def _apres_connexion(self) -> None:
        self.engine._shion_clients_par_niveau.clear()
        self._rafraichir_badge_compte()
        self._set_status("Prêt.")

    def _afficher_dialogue_connexion(self, bloquant: bool) -> None:
        """Fenêtre de connexion Google.

        `bloquant=True` : porte d'entrée obligatoire au démarrage (voir
        _gate_connexion_obligatoire) -- fermer la fenêtre sans se connecter
        ferme l'IDE (os._exit, pas self.root.destroy : la fenêtre principale
        n'a pas fini de se construire à ce stade, la détruire proprement
        n'apporterait rien puisqu'aucun travail utilisateur n'existe encore).

        `bloquant=False` : ouverte depuis le badge de compte, fermable
        normalement -- l'IDE reste utilisable sans connexion, seul
        SHION.ASK restera verrouillé (voir _resoudre_client_shion)."""
        fenetre = tk.Toplevel(self.root)
        fenetre.title(self._t("Connexion UVI"))
        fenetre.resizable(False, False)
        fenetre.configure(bg=T.PANNEAU)
        fenetre.transient(self.root)
        fenetre.grab_set()
        if bloquant:
            fenetre.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))

        tk.Label(fenetre, bg=T.PANNEAU, fg=T.TEXTE_FORT, padx=28, pady=(24, 6),
                 font=("Segoe UI", 10), wraplength=320,
                 text=self._t("Un compte UVI est nécessaire pour utiliser UVI.")).pack()
        statut_var = tk.StringVar(value="")
        tk.Label(fenetre, textvariable=statut_var, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                 padx=28, wraplength=320).pack()

        def lancer_connexion():
            bouton.config(state=tk.DISABLED)
            statut_var.set(self._t("Ouverture du navigateur pour la connexion Google..."))

            def travail():
                try:
                    uvi_auth.start_google_login(
                        on_status=lambda m: self._poster_ui(lambda: statut_var.set(m)))
                except uvi_auth.UviAuthError as e:
                    message = str(e)
                    self._poster_ui(lambda: (statut_var.set(message), bouton.config(state=tk.NORMAL)))
                    return
                self._poster_ui(lambda: (fenetre.destroy(), self._apres_connexion()))

            threading.Thread(target=travail, daemon=True).start()

        bouton = tk.Button(
            fenetre, text=self._t("Se connecter avec Google"), command=lancer_connexion,
            relief=tk.FLAT, bd=0, bg=T.BLEU, fg="white", font=("Segoe UI", 10, "bold"),
            cursor="hand2", padx=18, pady=10,
        )
        bouton.pack(pady=18)

        if bloquant:
            self.root.wait_window(fenetre)

    def _gate_connexion_obligatoire(self) -> None:
        """Si uvi_auth est configuré (docs/COMPTES_ABONNEMENTS.md) et
        qu'aucune session locale n'existe, bloque l'IDE derrière l'écran de
        connexion avant de rendre la main -- « avant d'utiliser UVI, chaque
        utilisateur doit se connecter », pas seulement pour SHION.ASK.
        No-op complet si uvi_auth n'est pas configuré (développement
        local, comportement historique)."""
        if not uvi_auth.is_configured():
            return
        if uvi_auth.current_session() is not None:
            return
        self._afficher_dialogue_connexion(bloquant=True)

    # ------------------------------------------------------------------
    # AUTO-MISE-À-JOUR (voir uvi_updater.py et docs/MISE_A_JOUR.md)
    # ------------------------------------------------------------------

    def _verifier_maj_au_demarrage(self):
        """Planifiée depuis __init__, APRÈS _gate_connexion_obligatoire --
        la vérification part 2,5s après l'affichage de la fenêtre pour ne
        jamais concurrencer le premier rendu ni un éventuel écran de
        connexion bloquant. Silencieuse : rien ne s'affiche si UVI est déjà
        à jour ou si le manifeste est injoignable (voir
        uvi_updater.check_for_update, qui renvoie None dans les deux cas
        sans distinction -- volontaire, un échec réseau au démarrage n'a
        rien à voir avec l'utilisateur)."""
        self.root.after(2500, lambda: self._lancer_verification_maj(silencieux=True))

    def _lancer_verification_maj(self, silencieux: bool):
        """silencieux=False (déclenché depuis le Portail, voir
        _actions_portail) confirme aussi l'absence de mise à jour par un
        toast -- sinon un clic sur "Vérifier les mises à jour" qui ne fait
        RIEN à l'écran donnerait l'impression que l'action a échoué."""
        def travail():
            manifest = uvi_updater.check_for_update(UVI_VERSION)
            if manifest is not None:
                self._poster_ui(lambda: self._proposer_mise_a_jour(manifest))
            elif not silencieux:
                self._poster_ui(lambda: self._empiler_toast(
                    self._t("UVI est à jour (v%s).") % UVI_VERSION, "ok"))
        threading.Thread(target=travail, daemon=True).start()

    def _proposer_mise_a_jour(self, manifest: dict):
        """Petite fenêtre nommant la version disponible, jamais modale de
        force (pas de grab_set/wait_window : contrairement à la porte de
        connexion, une mise à jour ne doit jamais empêcher de continuer à
        travailler) -- "Plus tard" ou la croix suffisent à la fermer sans
        rien installer."""
        version = str(manifest.get("version", "?"))
        notes = str(manifest.get("notes") or "").strip()

        fenetre = tk.Toplevel(self.root)
        fenetre.title(self._t("Mise à jour disponible"))
        fenetre.resizable(False, False)
        fenetre.configure(bg=T.PANNEAU)
        fenetre.transient(self.root)

        tk.Label(fenetre, bg=T.PANNEAU, fg=T.TEXTE_FORT, padx=28, pady=(24, 6),
                 font=("Segoe UI", 10, "bold"), wraplength=340,
                 text=self._t("UVI v%s est disponible (tu as v%s).") % (version, UVI_VERSION),
                 justify=tk.LEFT).pack(anchor=tk.W)
        if notes:
            tk.Label(fenetre, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, padx=28,
                     wraplength=340, justify=tk.LEFT, text=notes).pack(anchor=tk.W, pady=(0, 6))

        statut_var = tk.StringVar(value="")
        tk.Label(fenetre, textvariable=statut_var, bg=T.PANNEAU, fg=T.CYAN,
                 padx=28, wraplength=340, justify=tk.LEFT).pack(anchor=tk.W)

        barre_boutons = tk.Frame(fenetre, bg=T.PANNEAU)
        barre_boutons.pack(fill=tk.X, padx=18, pady=18)

        def plus_tard():
            fenetre.destroy()

        def mettre_a_jour():
            bouton_maj.config(state=tk.DISABLED)
            bouton_plus_tard.config(state=tk.DISABLED)
            statut_var.set(self._t("Téléchargement..."))

            def progress_cb(recu, total):
                if total:
                    self._poster_ui(lambda: statut_var.set(
                        self._t("Téléchargement... %d%%") % int(recu * 100 / total)))

            def travail():
                try:
                    chemin = uvi_updater.download_installer(manifest, progress_cb=progress_cb)
                    uvi_updater.launch_installer(chemin)
                except uvi_updater.UviUpdaterError as e:
                    message = str(e)
                    self._poster_ui(lambda: (
                        statut_var.set(message),
                        bouton_maj.config(state=tk.NORMAL),
                        bouton_plus_tard.config(state=tk.NORMAL),
                    ))
                    return
                self._poster_ui(lambda: statut_var.set(self._t(
                    "Installation en cours -- UVI va se fermer et redémarrer.")))

            threading.Thread(target=travail, daemon=True).start()

        bouton_plus_tard = tk.Button(
            barre_boutons, text=self._t("Plus tard"), command=plus_tard,
            relief=tk.FLAT, bd=0, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Segoe UI", 9), cursor="hand2", padx=14, pady=8,
        )
        bouton_plus_tard.pack(side=tk.LEFT)
        bouton_maj = tk.Button(
            barre_boutons, text=self._t("Mettre à jour"), command=mettre_a_jour,
            relief=tk.FLAT, bd=0, bg=T.BLEU, fg="white", font=("Segoe UI", 9, "bold"),
            cursor="hand2", padx=14, pady=8,
        )
        bouton_maj.pack(side=tk.RIGHT)

    def _appliquer_langue(self, code: str):
        """Change la langue de l'interface EN DIRECT (sans redémarrage) --
        rejoue le registre rempli par _tr()/_tr_personnalise(), puis les
        cas particuliers qui n'y tiennent pas du tout (voir
        _retraduire_cas_speciaux)."""
        if code not in LANGUES or code == self.langue:
            return
        self.langue = code
        for rafraichir in self._widgets_traduits:
            try:
                rafraichir()
            except tk.TclError:
                pass  # widget détruit entre-temps (panneau reconstruit) -- ignoré
        self._retraduire_cas_speciaux()
        self._sauvegarder_prefs_langue()

    def _retraduire_cas_speciaux(self):
        """Widgets dont le texte dépend de PLUS que la langue (l'état
        d'exécution, l'état replié/déplié du dock...) -- pas de simple
        entrée de registre possible pour eux, _appliquer_langue les
        rafraîchit explicitement après avoir traité le registre."""
        if hasattr(self, "btn_run"):
            self._mode_execution(self.engine.is_running)
        if hasattr(self, "btn_basculer_dock"):
            prefixe = "▾ " if getattr(self, "_dock_ouvert", True) else "▴ "
            self.btn_basculer_dock.config(text=prefixe + self._t("CONSOLE"))
        if hasattr(self, "titre_colonne_gauche") and getattr(self, "_panneau_gauche_actif", None):
            self.titre_colonne_gauche.config(text=self._t(
                self._TITRES_PANNEAUX.get(self._panneau_gauche_actif,
                                          self._panneau_gauche_actif.upper())))
        self._rafraichir_boutons_langue()
        try:
            self.root.title(self._t(
                "UVI OS v%s — UNIVERSE · VISION · INTELLIGENCE" % UVI_VERSION))
        except tk.TclError:
            pass

    def _changer_langue_parametres(self, code: str):
        """Clic sur un bouton de langue dans PARAMÈTRES (voir
        _build_panneau_parametres) -- change la langue de toute
        l'interface EN DIRECT, sans redémarrage."""
        self._appliquer_langue(code)

    def _rafraichir_boutons_langue(self):
        """Met en évidence le bouton de la langue ACTIVE dans la carte
        LANGUE de PARAMÈTRES (même principe que la mise en évidence de
        l'onglet actif du rail, voir afficher_panneau_gauche) -- séparée
        de _changer_langue_parametres pour être aussi appelable depuis
        _retraduire_cas_speciaux (ex. langue changée par un autre moyen
        que ces boutons eux-mêmes, pour rester cohérent dans tous les cas)."""
        for code, bouton in getattr(self, "_boutons_langue", {}).items():
            try:
                bouton.config(bg=T.SURVOL if code == self.langue else T.PANNEAU)
            except tk.TclError:
                pass

    def _on_uncaught_exception(self, exc_type, exc_value, exc_tb):
        """Point d'accroche Tkinter (self.root.report_callback_exception,
        voir son branchement juste après la création de self.root
        ci-dessous) -- appelé pour TOUTE exception levée dans un callback
        lié au mainloop (clic de bouton, raccourci clavier, etc.) qui n'a
        pas déjà son propre try/except. Sans ce point d'accroche, Tkinter
        se contente par défaut d'imprimer la trace sur stderr et de
        continuer -- invisible dans une app fenêtrée lancée sans console,
        l'utilisateur ne voit alors RIEN se passer d'autre qu'un clic sans
        effet. On journalise dans la console UVI (visible) ET on signale
        au système de rapports de bugs (bug_reports.py -- jamais de code
        utilisateur dedans, voir sa docstring), sans jamais laisser
        l'exception remonter plus loin : report_callback_exception ne
        doit elle-même jamais lever, sous peine de faire planter le
        mainloop Tk tout entier."""
        try:
            _signaler_crash(exc_value, source="tkinter_callback")
            self.log(f"Erreur interne inattendue ({exc_type.__name__}) — "
                     "un rapport anonymisé (sans code) a été enregistré "
                     "localement, voir PARAMÈTRES.", "err")
        except Exception:
            pass

    def _on_uncaught_thread_exception(self, args):
        """threading.excepthook (branché ci-dessous) -- filet pour les
        threads daemon qui n'ont pas déjà leur propre try/except (ceux
        qui en ont un, ex. run_script_vm._run, appellent déjà
        _signaler_crash directement et n'atteignent jamais ce point).
        Sans ce filet, un thread qui lève silencieusement ne fait
        qu'imprimer sur stderr et mourir -- invisible, et le script
        semble juste s'être arrêté sans raison."""
        try:
            _signaler_crash(args.exc_value, source="thread")
        except Exception:
            pass
        # Rappelle TOUJOURS le comportement par défaut : ce point
        # d'accroche AJOUTE la capture, il ne la remplace pas -- la trace
        # reste imprimée sur stderr comme avant, utile en développement.
        threading.__excepthook__(args)

    def _on_uncaught_main_exception(self, exc_type, exc_value, exc_tb):
        """sys.excepthook (branché ci-dessous) -- filet pour le thread
        principal EN DEHORS du mainloop Tk (celui-ci passe par
        report_callback_exception, pas par ici) : essentiellement la
        fenêtre de code entre la fin de __init__ et root.mainloop(), et
        tout code qui tournerait après root.destroy()."""
        try:
            _signaler_crash(exc_value, source="main")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _build_window(self):
        self.root = tk.Tk()
        # Trois filets d'exception COMPLÉMENTAIRES (jamais redondants) :
        # report_callback_exception couvre tout callback Tk (la quasi-
        # totalité de l'interaction utilisateur), threading.excepthook
        # couvre les threads daemon sans try/except propre, sys.excepthook
        # couvre le thread principal hors mainloop. Ensemble, c'est la
        # première fois que ce fichier capture des plantages qui, avant,
        # disparaissaient silencieusement dans stderr d'une app fenêtrée
        # sans console -- voir bug_reports.py pour ce qu'un rapport
        # contient (jamais de code utilisateur).
        self.root.report_callback_exception = self._on_uncaught_exception
        threading.excepthook = self._on_uncaught_thread_exception
        sys.excepthook = self._on_uncaught_main_exception
        self.root.title("UVI OS v%s — UNIVERSE · VISION · INTELLIGENCE" % UVI_VERSION)
        # 1500x1000 et non 1100x700 : la v9 affiche l'éditeur, la console,
        # la mémoire et SHION SIMULTANÉMENT là où la v8 basculait entre des
        # vues plein écran. En dessous de ~1400 de large, le panneau SHION
        # écrase l'éditeur. minsize l'empêche plutôt que de laisser
        # l'utilisateur découvrir une fenêtre inutilisable.
        #
        # BUGFIX (dock console/saisie ?var invisible) : geometry() fixe
        # demandait 1500x1000 SANS jamais regarder la taille réelle de
        # l'écran. Sur un écran de 1366x768 (résolution de portable très
        # courante -- zone de travail réellement disponible ~1366x720 une
        # fois la barre des tâches déduite), la fenêtre demandée dépasse la
        # zone dispo ; Windows la contraint alors à la zone visible SANS
        # que Tkinter ne relance sa mise en page interne en conséquence.
        # Résultat mesuré : le dock du bas (console + barre de saisie
        # interactive répondant à ?var, voir _build_container et
        # _ouvrir_saisie) se retrouve écrasé hors de la fenêtre -- invisible
        # ET hors d'atteinte du clavier, donc un script qui attend une
        # réponse à ?var semble figé sans qu'on sache pourquoi (les
        # frappes tombent alors dans l'éditeur, resté seul visible). Se
        # caler sur winfo_screenwidth/height, avec une marge, corrige ça
        # pour toute taille d'écran -- pas seulement celle observée ici.
        largeur_ecran = self.root.winfo_screenwidth()
        hauteur_ecran = self.root.winfo_screenheight()
        largeur = min(1500, largeur_ecran - 40)
        hauteur = min(1000, hauteur_ecran - 80)
        self.root.geometry("%dx%d+%d+%d" % (
            largeur, hauteur,
            max(0, (largeur_ecran - largeur) // 2),
            max(0, (hauteur_ecran - hauteur) // 2 - 20),
        ))
        # Bornée par la géométrie réellement obtenue ci-dessus : un minsize
        # plus grand que la fenêtre elle-même ne "empêche" rien, il
        # garantit juste la même casse hors écran à chaque redémarrage.
        self.root.minsize(min(1180, largeur), min(760, hauteur))
        self.root.configure(bg=T.FOND)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Icône de fenêtre (barre des tâches / alt-tab) : le même « noyau »
        # que le badge de la barre du haut (dessiner_noyau_uvi), pré-généré
        # en PNG par assets/generer_logo.py -- iconphoto() sait choisir la
        # résolution adaptée parmi celles fournies. Best-effort : un poste
        # où le dossier assets/ a été supprimé ou déplacé doit quand même
        # lancer l'IDE, juste sans icône personnalisée (icône Tk par
        # défaut), donc jamais fatal.
        try:
            dossier_assets = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "assets")
            self._images_icone = [
                tk.PhotoImage(file=os.path.join(dossier_assets, f"uvi_icon_{t}.png"))
                for t in (16, 24, 32, 48, 64, 128, 256)
            ]
            self.root.iconphoto(True, *self._images_icone)
        except (tk.TclError, OSError) as exc:
            print(f"[UVI] icône de fenêtre non chargée ({exc}) -- ignoré.")

        # --- État clavier pour KEY.* (voir _exec_key / _compile_key_node
        # / builtins KEY_* de la VM) -------------------------------------
        # self._keys_down : touches ACTUELLEMENT enfoncées (mises à jour
        #   en continu par les bindings ci-dessous, lues par KEY.DOWN).
        # self._keys_pressed_edge : touches passées de relâchées à
        #   enfoncées DEPUIS LA DERNIÈRE LECTURE (consommé par KEY.PRESSED
        #   -- front montant, pour une action qui ne doit se déclencher
        #   qu'une fois par appui, pas répétée à chaque frame tant que la
        #   touche reste enfoncée).
        # threading.Lock : ces sets sont écrits par le thread Tkinter
        # (callbacks <KeyPress>/<KeyRelease> ci-dessous) et lus par le
        # thread du script UVI en cours d'exécution (cf. run_script,
        # threading.Thread(target=_run) déjà existant) -- accès concurrent
        # classique, protégé comme _lock dans IssueDiagnosisLoop
        # (self_correction.py) protège son propre état partagé.
        self._keys_lock = threading.Lock()
        self._keys_down = set()
        self._keys_pressed_edge = set()

        # bind_all (pas bind) : capte les touches quel que soit le widget
        # qui a le focus Tkinter au moment de l'appui -- un jeu doit
        # réagir aux flèches sans que l'utilisateur ait à cliquer sur le
        # canvas au préalable pour lui donner explicitement le focus.
        # Contrepartie assumée : taper du texte dans l'éditeur pendant
        # qu'un script KEY.*-dépendant tourne en fond alimente aussi son
        # état clavier -- acceptable, on ne code généralement pas pendant
        # qu'on joue.
        self.root.bind_all("<KeyPress>", self._on_game_key_press)
        self.root.bind_all("<KeyRelease>", self._on_game_key_release)

    def _build_ui(self):
        self._build_topbar()
        self._build_statusbar()
        self._build_container()

    def _on_game_key_press(self, event):
        """Callback Tkinter <KeyPress>, appelé sur le thread PRINCIPAL
        (celui de la boucle d'événements) -- jamais depuis le thread du
        script UVI. event.keysym donne un nom déjà lisible ("Up", "Down",
        "space", "Return", "a"...), normalisé en minuscules pour que
        KEY.DOWN "up" et KEY.DOWN "Up" soient équivalents côté script
        (le langage n'a par ailleurs aucune sensibilité à la casse
        documentée sur ses propres symboles ~ / %% / etc.). N'ajoute au
        front montant (_keys_pressed_edge) QUE si la touche n'était pas
        déjà enfoncée -- Tkinter répète nativement <KeyPress> tant qu'une
        touche reste appuyée (auto-repeat clavier), ce qui produirait
        sinon un flot continu de "fronts montants" plutôt qu'un seul au
        moment réel de l'appui initial."""
        key = event.keysym.lower()
        with self._keys_lock:
            if key not in self._keys_down:
                self._keys_pressed_edge.add(key)
            self._keys_down.add(key)

    def _on_game_key_release(self, event):
        key = event.keysym.lower()
        with self._keys_lock:
            self._keys_down.discard(key)

    def _bouton_plat(self, parent, texte, commande, couleur=None, icone="",
                     gras=True, largeur=None, police=None):
        """Bouton « carte » du thème v9 : fond de panneau, bordure discrète,
        éclaircissement au survol.

        Tkinter n'a pas de bouton plat avec état de survol : activebackground
        ne s'applique qu'au CLIC maintenu, pas au passage de la souris. D'où
        les deux liaisons <Enter>/<Leave> ci-dessous. Sans elles, toute la
        barre d'outils paraît inerte -- rien ne réagit avant le clic, ce qui
        donne l'impression que l'interface a gelé."""
        couleur = couleur or T.TEXTE
        bouton = tk.Button(
            parent, text=("%s  %s" % (icone, texte)).strip(),
            bg=T.PANNEAU, fg=couleur,
            activebackground=T.SURVOL, activeforeground=T.TEXTE_FORT,
            disabledforeground=T.TEXTE_FAIBLE,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.BORDURE,
            font=police or (T.UI_GRAS if gras else T.UI),
            cursor="hand2", command=commande, padx=10, pady=7,
        )
        if largeur:
            bouton.config(width=largeur)

        # _tr_personnalise (pas _tr) : le texte affiché combine icône +
        # texte en une seule chaîne (icone n'est jamais traduit, c'est un
        # symbole) -- il faut recomposer les deux à chaque changement de
        # langue, pas juste réinjecter _t(texte) tel quel. Rend TOUS les
        # boutons construits via _bouton_plat traduisibles en direct dès
        # lors que `texte` a une entrée dans TRANSLATIONS -- sinon reste
        # affiché en français, jamais cassé.
        self._tr_personnalise(
            lambda b=bouton, i=icone, t=texte:
                b.config(text=("%s  %s" % (i, self._t(t))).strip()))

        def entrer(_e, b=bouton):
            if b["state"] != tk.DISABLED:
                b.config(bg=T.SURVOL)

        def sortir(_e, b=bouton, c=T.PANNEAU):
            if b["state"] != tk.DISABLED:
                b.config(bg=c)

        bouton.bind("<Enter>", entrer, add="+")
        bouton.bind("<Leave>", sortir, add="+")
        return bouton

    def _groupe_boutons(self, parent):
        """Conteneur encadré regroupant des boutons liés (cf. maquette :
        NOUVEAU/OUVRIR/SAUVEGARDER forment un bloc, DÉBOGUEUR/AUTO-CORRIGER
        un autre). Le regroupement visuel remplace la séparation par
        padding de la v8, qui ne disait rien de la parenté des boutons."""
        cadre = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(side=tk.LEFT, padx=6, pady=8)
        return cadre

    def _build_topbar(self):
        topbar = tk.Frame(self.root, bg=T.FOND, height=76)
        topbar.pack(fill=tk.X, side=tk.TOP)
        topbar.pack_propagate(False)

        # --- identité ----------------------------------------------------
        bloc_logo = tk.Frame(topbar, bg=T.FOND)
        bloc_logo.pack(side=tk.LEFT, padx=(14, 18))
        pastille = tk.Canvas(bloc_logo, width=52, height=52, bg=T.FOND,
                             highlightthickness=0)
        pastille.pack(side=tk.LEFT, padx=(0, 12))
        pastille.create_rectangle(2, 2, 50, 50, outline=T.CYAN, width=2)
        pastille.create_text(26, 26, text="U", fill=T.CYAN,
                             font=("Segoe UI", 24, "bold"))
        textes = tk.Frame(bloc_logo, bg=T.FOND)
        textes.pack(side=tk.LEFT)
        ligne = tk.Frame(textes, bg=T.FOND)
        ligne.pack(anchor=tk.W)
        tk.Label(ligne, text="UVI", bg=T.FOND, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 19, "bold")).pack(side=tk.LEFT)
        tk.Label(ligne, text=" IDE", bg=T.FOND, fg=T.TEXTE,
                 font=("Segoe UI", 15)).pack(side=tk.LEFT, pady=(4, 0))
        tk.Label(textes, text="UNIVERSE · VISION · INTELLIGENCE",
                 bg=T.FOND, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 7)).pack(anchor=tk.W)

        # --- fichier ------------------------------------------------------
        groupe_fichier = self._groupe_boutons(topbar)
        for icone, libelle, commande in (
            ("＋", "NOUVEAU", self.nouveau_fichier),
            ("📂", "OUVRIR", self.load_from_disk),
            ("💾", "SAUVEGARDER", self.save_to_disk),
        ):
            self._bouton_plat(groupe_fichier, libelle, commande,
                              icone=icone, gras=False).pack(side=tk.LEFT)

        # --- exécution ----------------------------------------------------
        # Le bouton d'exécution est le SEUL en aplat bleu de toute la barre.
        # La v8 avait sept boutons de couleurs vives concurrentes, si bien
        # qu'aucun ne ressortait -- un nouvel arrivant ne voyait pas par où
        # commencer. Un seul accent plein = une seule action évidente.
        groupe_run = tk.Frame(topbar, bg=T.FOND)
        groupe_run.pack(side=tk.LEFT, padx=6)
        self.btn_run = tk.Button(
            groupe_run, text=self._t("▶  EXÉCUTER"),
            bg=T.BLEU, fg="#FFFFFF",
            activebackground=T.BLEU_VIF, activeforeground="#FFFFFF",
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
            command=self.basculer_execution, padx=20, pady=9,
        )
        self.btn_run.pack(side=tk.LEFT, pady=8)
        self.btn_run.bind("<Enter>", lambda e: self.btn_run.config(
            bg=T.ROUGE if self.engine.is_running else T.BLEU_VIF), add="+")
        self.btn_run.bind("<Leave>", lambda e: self.btn_run.config(
            bg="#B91C1C" if self.engine.is_running else T.BLEU), add="+")

        if HAS_VM:
            self.btn_run_vm = self._bouton_plat(
                groupe_run, "VM", self.run_script_vm, couleur=T.CYAN, icone="⚡")
            self.btn_run_vm.pack(side=tk.LEFT, padx=(6, 0), pady=8)

        # --- outils IA ----------------------------------------------------
        groupe_ia = self._groupe_boutons(topbar)
        self.btn_debug = self._bouton_plat(
            groupe_ia, "DÉBOGUEUR", self.ouvrir_debogueur,
            couleur=T.AMBRE, icone="🐞", gras=False)
        self.btn_debug.pack(side=tk.LEFT)
        self.btn_auto_fix = self._bouton_plat(
            groupe_ia, "AUTO-CORRIGER", self.autonomous_fix_from_editor,
            couleur=T.VIOLET, icone="🪄", gras=False)
        self.btn_auto_fix.pack(side=tk.LEFT)
        if not HAS_SHION:
            self.btn_auto_fix.config(state=tk.DISABLED, cursor="X_cursor")

        # --- badge de version ---------------------------------------------
        badge = tk.Frame(topbar, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE_VIVE)
        badge.pack(side=tk.LEFT, padx=14, pady=8)
        noyau = tk.Canvas(badge, width=34, height=34, bg=T.PANNEAU,
                          highlightthickness=0)
        noyau.pack(side=tk.LEFT, padx=(10, 8), pady=6)
        dessiner_noyau_uvi(noyau, 17, 17, 15)
        colonne = tk.Frame(badge, bg=T.PANNEAU)
        colonne.pack(side=tk.LEFT, padx=(0, 14))
        tk.Label(colonne, text="UVI OS", bg=T.PANNEAU, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        tk.Label(colonne, text="v%s" % UVI_VERSION, bg=T.PANNEAU, fg=T.CYAN,
                 font=("Segoe UI", 8)).pack(anchor=tk.W)

        # --- badge de compte / abonnement (voir docs/COMPTES_ABONNEMENTS.md) --
        # Construit UNIQUEMENT quand uvi_auth.is_configured() est vrai --
        # sinon SUPABASE_URL/ANON_KEY ne sont pas renseignées (développement
        # local, voir uvi_auth.py) et un bouton "Se connecter" qui ne mènerait
        # nulle part serait plus trompeur qu'utile.
        self.badge_compte = None
        if uvi_auth.is_configured():
            self.badge_compte = tk.Frame(topbar, bg=T.PANNEAU, highlightthickness=1,
                                          highlightbackground=T.BORDURE_VIVE)
            self.badge_compte.pack(side=tk.LEFT, padx=(0, 14), pady=8)
            self.lbl_compte = tk.Label(self.badge_compte, bg=T.PANNEAU, fg=T.TEXTE_FORT,
                                        font=("Segoe UI", 9))
            self.lbl_compte.pack(side=tk.LEFT, padx=(12, 8), pady=6)
            self.btn_compte = tk.Button(
                self.badge_compte, relief=tk.FLAT, bd=0, bg=T.PANNEAU, fg=T.CYAN,
                font=("Segoe UI", 9, "bold"), cursor="hand2", padx=10, pady=6,
            )
            self.btn_compte.pack(side=tk.LEFT, padx=(0, 10))
            self._rafraichir_badge_compte()

        # Les boutons réduire / agrandir / fermer ont été RETIRÉS. Ils
        # doublonnaient les décorations de fenêtre du système, qui restent
        # présentes : l'utilisateur avait deux croix de fermeture à l'écran,
        # à deux endroits différents, dont une seule respectait le style de
        # son bureau. Une barre de titre personnalisée n'a de sens qu'avec
        # overrideredirect(True) -- ce qui obligerait alors à réimplémenter
        # à la main le déplacement, le redimensionnement, l'accrochage aux
        # bords et les raccourcis du gestionnaire de fenêtres. Coût
        # disproportionné pour un gain purement décoratif.

        # --- boutons conservés, non affichés dans la barre v9 ---------------
        # AST / BYTECODE / OPTIMISER / ANALYSER / STATS ont migré dans le
        # panneau OUTILS UVI à droite (maquette v9). Mais _attacher_infobulles
        # et plusieurs méthodes de IAAgent référencent encore self.btn_ast,
        # self.btn_bytecode, etc. On les crée donc dans un conteneur JAMAIS
        # empaqueté : les attributs existent, les widgets sont valides, rien
        # ne s'affiche. C'est plus honnête qu'un getattr défensif partout,
        # et ça évite de casser du code qui marche pour une raison purement
        # cosmétique.
        self._boutons_orphelins = tk.Frame(self.root, bg=T.FOND)
        for attribut, libelle, commande in (
            ("btn_ast", "AST", lambda: self.agent.export_ast() if self.agent else None),
            ("btn_bytecode", "BYTECODE", lambda: self.agent.export_bytecode() if self.agent else None),
            ("btn_optimiser", "OPTIMISER", lambda: self.agent.optimize_code() if self.agent else None),
            ("btn_analyser", "ANALYSER", lambda: self.agent.analyze_editor_code() if self.agent else None),
            ("btn_stats", "STATS", lambda: self.agent.report_stats() if self.agent else None),
            ("btn_auto_repair", "AUTO-RÉPARER", lambda: self.agent.auto_repair() if self.agent else None),
            ("btn_voice", "VOIX", self.toggle_voice),
            ("btn_open", "OUVRIR", self.load_from_disk),
            ("btn_save", "SAUVER", self.save_to_disk),
        ):
            setattr(self, attribut, tk.Button(self._boutons_orphelins,
                                              text=libelle, command=commande))
        # BUG CORRIGÉ : la refonte v9 a réécrit _build_topbar sans
        # reconduire l'appel à _attacher_infobulles. La méthode existait
        # toujours, aucun chemin ne l'appelait -- les infobulles étaient
        # mortes en silence, exactement comme autonomous_fix avant l'étape
        # 6. Rien ne le signalait : une infobulle absente ne lève pas.
        self._attacher_infobulles()
        self._attacher_raccourcis()

    def nouveau_fichier(self):
        """Vide l'éditeur après confirmation si du travail non vidé s'y
        trouve. La v8 n'avait pas de bouton NOUVEAU du tout : on effaçait à
        la main avec Ctrl+A puis Suppr, sans aucun garde-fou."""
        if self.editor.get("1.0", tk.END).strip():
            if not tk.messagebox.askyesno(
                "Nouveau fichier",
                "Effacer le contenu de l'éditeur ? Le travail non sauvegardé "
                "sera perdu."):
                return
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", "@UVI\n")
        self._update_line_numbers()
        self.fichier_courant = None
        self._chemin_disque_courant = None
        self._marquer_enregistre()
        self.show_editor()
        self._set_status("Nouveau fichier.")

    def _basculer_plein_ecran(self):
        etat = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not etat)

    def ouvrir_debogueur(self):
        """Bouton DÉBOGUEUR de la barre v9.

        Le moteur de points d'arrêt existe déjà (UVIDebugPause, breakpoints,
        pile d'appel) mais n'avait aucun déclencheur dans l'interface v8 --
        même situation qu'autonomous_fix avant l'étape 6. Ce bouton bascule
        l'exécution en mode pas-à-pas et affiche l'état courant dans la
        console, sans fenêtre supplémentaire (cohérent avec le reste du
        projet, qui rend ses diagnostics en texte)."""
        self._conseil_unique(
            "debogueur",
            "🐞 DÉBOGUEUR — posez un point d'arrêt en cliquant sur un numéro de "
            "ligne, puis lancez ▶ EXÉCUTER : le script s'arrêtera là et la "
            "console affichera les variables et la pile d'appel.",
        )
        self._basculer_point_arret_ligne_courante()

    def _basculer_point_arret_ligne_courante(self):
        """Pose ou retire un point d'arrêt sur la ligne du curseur."""
        try:
            ligne = int(self.editor.index(tk.INSERT).split(".")[0])
        except (ValueError, tk.TclError):
            return
        points = getattr(self, "_points_arret", None)
        if points is None:
            points = self._points_arret = set()
        if ligne in points:
            points.discard(ligne)
            self.log("Point d'arrêt retiré ligne %d." % ligne, "sys")
        else:
            points.add(ligne)
            self.log("Point d'arrêt posé ligne %d." % ligne, "ok")
        self._update_line_numbers()

    def _attacher_raccourcis(self):
        """Raccourcis clavier standards.

        Leur absence était un vrai frein : sauvegarder demandait de viser
        un bouton à la souris, à chaque fois. bind_all et non bind sur
        l'éditeur, pour qu'ils fonctionnent aussi quand le focus est dans
        la console ou l'explorateur -- un Ctrl+S qui ne marche « que si le
        curseur est au bon endroit » est plus déroutant que pas de
        raccourci du tout.

        Les handlers rendent "break" pour empêcher Tk d'appliquer EN PLUS
        son comportement par défaut sur ces séquences."""
        raccourcis = (
            ("<F5>", lambda e: self.basculer_execution()),
            ("<Control-Return>", lambda e: self.basculer_execution()),
            ("<Escape>", lambda e: self.arreter_script()),
            ("<Control-s>", lambda e: self.save_to_disk()),
            ("<Control-o>", lambda e: self.load_from_disk()),
            ("<Control-n>", lambda e: self.nouveau_fichier()),
            ("<Control-f>", lambda e: self.afficher_panneau_gauche("recherche")),
            ("<Control-l>", lambda e: self._clear_console()),
            ("<Control-slash>", lambda e: self.basculer_commentaire()),
            ("<F1>", lambda e: self.afficher_aide()),
            # F11 remplace le bouton ▢ retiré de la barre : le plein écran
            # reste accessible, par la touche que tout le monde essaie déjà.
            ("<F11>", lambda e: self._basculer_plein_ecran()),
            # Ctrl+K et non Ctrl+Shift+P (VS Code) : geste à une main, et
            # surtout laissé LIBRE ici -- voir _ouvrir_portail pour pourquoi
            # ce composant n'a pas été calqué sur une palette de commandes
            # classique au-delà du principe "chercher une action au clavier".
            ("<Control-k>", lambda e: self._ouvrir_portail()),
        )
        for sequence, handler in raccourcis:
            self.root.bind_all(sequence, lambda e, h=handler: (h(e), "break")[1])

    # ------------------------------------------------------------------
    # LE PORTAIL — lanceur d'actions au clavier (Ctrl+K)
    # ------------------------------------------------------------------
    # Un IDE "à la VS Code" aurait une palette de commandes en haut de
    # fenêtre. Le Portail fait le même travail (chercher puis lancer une
    # action par son nom plutôt que d'aller la chercher à la souris dans un
    # menu, une barre d'outils ou le rail) mais avec un parti pris visuel
    # différent, en accord avec l'identité "atelier" de la palette T (voir
    # sa docstring) plutôt qu'une imitation : un panneau CENTRÉ, en fondu
    # sur fond assombri, pensé comme un geste ponctuel qu'on invoque plutôt
    # qu'une barre permanente de plus collée en haut de l'écran.
    #
    # Le contenu, lui, est délibérément SANS AMBITION : chaque entrée de
    # _actions_portail() appelle une méthode qui existe déjà ailleurs dans
    # cette classe (barre d'outils, menus, rail). Le Portail est une façade
    # de recherche par-dessus des commandes déjà câblées, jamais un second
    # chemin de code pour la même action.
    def _appeler_agent(self, nom_methode: str):
        """Appelle self.agent.<nom_methode>(), ou notifie proprement si
        l'agent n'est pas encore initialisé -- évite de dupliquer la garde
        `if self.agent else None` déjà utilisée par la barre d'outils
        (voir _build_topbar) à chaque entrée du Portail qui touche l'agent."""
        if not self.agent:
            self._empiler_toast("Agent IA non initialisé.", "err")
            return
        getattr(self.agent, nom_methode)()

    def _actions_portail(self):
        return [
            ("Exécuter le script (tree-walker)", "▶", self.run_script),
            ("Exécuter (VM bytecode)", "⚡", self.run_script_vm),
            ("Arrêter l'exécution", "⏹", self.arreter_script),
            ("Nouveau fichier", "📄", self.nouveau_fichier),
            ("Ouvrir…", "📂", self.load_from_disk),
            ("Sauvegarder", "💾", self.save_to_disk),
            ("Effacer la console", "🧹", self._clear_console),
            ("Basculer plein écran", "⛶", self._basculer_plein_ecran),
            ("Aide", "❓", self.afficher_aide),
            ("Vérifier les mises à jour", "🔄", lambda: self._lancer_verification_maj(silencieux=False)),
            ("Afficher : Éditeur", "📝", self.show_editor),
            ("Afficher : Terminal", "⌨", self.show_terminal),
            ("Afficher : Canevas", "🎨", self.show_canvas),
            ("Panneau : Explorateur", "📁", lambda: self.afficher_panneau_gauche("explorateur")),
            ("Panneau : Recherche", "🔍", lambda: self.afficher_panneau_gauche("recherche")),
            ("Panneau : Variables", "{ }", lambda: self.afficher_panneau_gauche("variables")),
            ("Panneau : Outils", "🛠", lambda: self.afficher_panneau_gauche("outils")),
            ("Panneau : Modules", "📦", lambda: self.afficher_panneau_gauche("modules")),
            ("Panneau : Tests", "🧪", lambda: self.afficher_panneau_gauche("tests")),
            ("Panneau : Paramètres", "⚙", lambda: self.afficher_panneau_gauche("parametres")),
            ("Exporter l'AST", "🌳", lambda: self._appeler_agent("export_ast")),
            ("Exporter le bytecode", "⚙", lambda: self._appeler_agent("export_bytecode")),
            ("Analyser le script", "🔍", lambda: self._appeler_agent("analyze_editor_code")),
            ("Auto-réparer (déterministe)", "🛠", lambda: self._appeler_agent("auto_repair")),
            ("Auto-corriger (agent IA)", "🤖", self.autonomous_fix_from_editor),
            ("Statistiques de session", "📊", lambda: self._appeler_agent("report_stats")),
        ]

    def _ouvrir_portail(self, event=None):
        if getattr(self, "_portail_overlay", None) is not None:
            return "break"

        self.root.update_idletasks()
        largeur = max(420, min(600, self.root.winfo_width() - 100))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - largeur) // 2
        y = self.root.winfo_rooty() + 96

        overlay = tk.Toplevel(self.root)
        overlay.wm_overrideredirect(True)
        overlay.wm_transient(self.root)
        try:
            overlay.wm_attributes("-topmost", True)
            overlay.wm_attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        overlay.wm_geometry("%dx360+%d+%d" % (largeur, x, y))
        overlay.configure(bg=T.BORDURE_VIVE)

        cadre = tk.Frame(overlay, bg=T.PANNEAU_HAUT, highlightthickness=2,
                         highlightbackground=T.CYAN, highlightcolor=T.CYAN)
        cadre.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        entete = tk.Frame(cadre, bg=T.PANNEAU_HAUT)
        entete.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Label(entete, text="◈", bg=T.PANNEAU_HAUT, fg=T.CYAN,
                font=("Segoe UI", 14)).pack(side=tk.LEFT, padx=(0, 8))
        champ = tk.Entry(entete, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FORT,
                         insertbackground=T.CYAN, relief=tk.FLAT, bd=0,
                         font=("Segoe UI", 13))
        champ.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(cadre, bg=T.BORDURE, height=1).pack(fill=tk.X, padx=16)

        liste = tk.Listbox(
            cadre, bg=T.PANNEAU_HAUT, fg=T.TEXTE, selectbackground=T.SURVOL,
            selectforeground=T.TEXTE_FORT, relief=tk.FLAT, highlightthickness=0,
            font=("Segoe UI", 10), activestyle="none", bd=0,
        )
        liste.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 12))

        actions = self._actions_portail()
        etat = {"visibles": actions}

        def _rafraichir(_event=None):
            terme = champ.get().strip().lower()
            etat["visibles"] = [a for a in actions if terme in a[0].lower()] if terme else actions
            liste.delete(0, tk.END)
            for libelle, icone, _cb in etat["visibles"]:
                liste.insert(tk.END, "   %s   %s" % (icone, libelle))
            if etat["visibles"]:
                liste.selection_clear(0, tk.END)
                liste.selection_set(0)

        def _naviguer(delta):
            if not liste.size():
                return "break"
            actuel = (liste.curselection() or (0,))[0]
            nouveau = max(0, min(liste.size() - 1, actuel + delta))
            liste.selection_clear(0, tk.END)
            liste.selection_set(nouveau)
            liste.see(nouveau)
            return "break"

        def _executer(_event=None):
            cible = etat["visibles"]
            selection = liste.curselection()
            i = selection[0] if selection else 0
            _fermer()
            if 0 <= i < len(cible):
                cible[i][2]()
            return "break"

        def _fermer(_event=None):
            self._portail_overlay = None
            try:
                overlay.destroy()
            except tk.TclError:
                pass
            try:
                self.root.focus_set()
            except tk.TclError:
                pass
            return "break"

        champ.bind("<KeyRelease>", _rafraichir)
        champ.bind("<Down>", lambda e: _naviguer(1))
        champ.bind("<Up>", lambda e: _naviguer(-1))
        champ.bind("<Return>", _executer)
        liste.bind("<Return>", _executer)
        liste.bind("<Double-Button-1>", _executer)
        overlay.bind("<Escape>", _fermer)
        champ.bind("<Escape>", _fermer)
        # Un clic hors du Portail le referme -- FocusOut sur la fenêtre
        # elle-même (pas sur le champ, qui perd le focus à chaque flèche
        # vers la liste sans qu'on veuille fermer pour autant).
        overlay.bind("<FocusOut>", lambda e: overlay.after(120, _verifier_focus))

        def _verifier_focus():
            try:
                if self.root.focus_get() is None and getattr(self, "_portail_overlay", None) is overlay:
                    _fermer()
            except (tk.TclError, KeyError):
                pass

        self._portail_overlay = overlay
        _rafraichir()
        champ.focus_set()

        def _fondu(alpha=0.0):
            try:
                alpha = min(0.98, alpha + 0.20)
                overlay.wm_attributes("-alpha", alpha)
                if alpha < 0.98:
                    overlay.after(10, lambda: _fondu(alpha))
            except tk.TclError:
                pass
        overlay.after(5, _fondu)
        return "break"

    # ------------------------------------------------------------------
    # Notifications flottantes (toasts) — canal non bloquant, distinct de
    # messagebox (réservée aux confirmations qui EXIGENT une réponse, voir
    # _on_close). Utile quand un évènement arrive pendant que l'utilisateur
    # regarde un autre onglet (réponse SHION, fin de script en fond) : la
    # barre de statut seule, en bas de la fenêtre, passe facilement
    # inaperçue.
    # ------------------------------------------------------------------
    def _empiler_toast(self, message: str, niveau: str = "info", duree_ms: int = 3400):
        try:
            self.root.winfo_exists()
        except tk.TclError:
            return
        couleurs = {"info": T.CYAN, "ok": T.VERT, "err": T.ROUGE, "sys": T.TEXTE_FAIBLE}
        accent = couleurs.get(niveau, T.CYAN)
        pile = getattr(self, "_toasts_actifs", None)
        if pile is None:
            pile = self._toasts_actifs = []

        fenetre = tk.Toplevel(self.root)
        fenetre.wm_overrideredirect(True)
        try:
            fenetre.wm_attributes("-topmost", True)
            fenetre.wm_attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        cadre = tk.Frame(fenetre, bg=T.PANNEAU_HAUT, highlightthickness=1,
                         highlightbackground=T.BORDURE_VIVE)
        cadre.pack()
        interieur = tk.Frame(cadre, bg=T.PANNEAU_HAUT)
        interieur.pack()
        tk.Frame(interieur, bg=accent, width=4).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(interieur, text=str(message), bg=T.PANNEAU_HAUT, fg=T.TEXTE_FORT,
                font=T.UI, wraplength=300, justify=tk.LEFT,
                padx=12, pady=10).pack(side=tk.LEFT)

        pile.append(fenetre)
        self._repositionner_toasts()

        def _retirer(_event=None):
            if fenetre in pile:
                pile.remove(fenetre)
            try:
                fenetre.destroy()
            except tk.TclError:
                pass
            self._repositionner_toasts()

        def _fondu(alpha=0.0):
            try:
                alpha = min(1.0, alpha + 0.18)
                fenetre.wm_attributes("-alpha", alpha)
                if alpha < 1.0:
                    fenetre.after(14, lambda: _fondu(alpha))
            except tk.TclError:
                pass

        fenetre.after(8, _fondu)
        fenetre.after(duree_ms, _retirer)
        cadre.bind("<Button-1>", _retirer)
        interieur.bind("<Button-1>", _retirer)

    def _repositionner_toasts(self):
        pile = getattr(self, "_toasts_actifs", [])
        try:
            x_droite = self.root.winfo_rootx() + self.root.winfo_width() - 18
            y = self.root.winfo_rooty() + self.root.winfo_height() - 46
        except tk.TclError:
            return
        for fenetre in reversed(pile):
            try:
                fenetre.update_idletasks()
                largeur = fenetre.winfo_reqwidth()
                hauteur = fenetre.winfo_reqheight()
                fenetre.wm_geometry("+%d+%d" % (x_droite - largeur, y - hauteur))
                y -= hauteur + 8
            except tk.TclError:
                pass

    def basculer_commentaire(self):
        """Ctrl+/ : commente ou décommente les lignes sélectionnées.

        Le caractère de commentaire d'UVI est # -- vérifié contre la règle
        COMMENT du lexer, pas supposé par analogie avec un autre langage.

        Si TOUTES les lignes non vides sont déjà commentées, on
        décommente ; sinon on commente. Traiter chaque ligne
        indépendamment produirait un basculement en damier sur une
        sélection mixte, ce que personne n'attend."""
        # ValueError en plus de TclError : Tk lève TclError quand il n'y a
        # pas de sélection, mais index() peut aussi rendre une chaîne
        # inattendue selon la version. Un raccourci clavier qui fait
        # remonter une trace Python est pire que pas de raccourci.
        try:
            debut = int(self.editor.index("sel.first").split(".")[0])
            fin = int(self.editor.index("sel.last").split(".")[0])
        except (tk.TclError, ValueError):
            try:
                debut = fin = int(self.editor.index(tk.INSERT).split(".")[0])
            except (tk.TclError, ValueError):
                return
        lignes = [self.editor.get("%d.0" % n, "%d.end" % n)
                  for n in range(debut, fin + 1)]
        pleines = [l for l in lignes if l.strip()]
        decommenter = bool(pleines) and all(
            l.lstrip().startswith("#") for l in pleines)
        for decalage, ligne in enumerate(lignes):
            numero = debut + decalage
            if not ligne.strip():
                continue
            if decommenter:
                indentation = ligne[:len(ligne) - len(ligne.lstrip())]
                nouvelle = indentation + ligne.lstrip()[1:].lstrip()
            else:
                nouvelle = "# " + ligne
            self.editor.delete("%d.0" % numero, "%d.end" % numero)
            self.editor.insert("%d.0" % numero, nouvelle)
        self._update_line_numbers()

    def afficher_aide(self):
        """F1 : rappel des raccourcis, dans la console.

        Dans la console et pas dans une fenêtre d'aide : c'est là que
        l'IDE dit déjà tout le reste, et une aide qu'on doit refermer pour
        continuer à travailler n'est pas consultée pendant qu'on
        travaille."""
        self.show_terminal()
        self.log("=== AIDE UVI ===", "sys")
        for touche, description in (
            ("F5 / Ctrl+Entrée", "Exécuter ou arrêter le script"),
            ("Échap", "Arrêter le script en cours"),
            ("Ctrl+S", "Sauvegarder"),
            ("Ctrl+O", "Ouvrir un fichier"),
            ("Ctrl+N", "Nouveau fichier"),
            ("Ctrl+F", "Rechercher"),
            ("Ctrl+/", "Commenter ou décommenter la sélection"),
            ("Ctrl+L", "Vider la console"),
            ("F11", "Plein écran"),
            ("F1", "Cette aide"),
        ):
            self.log("  %-18s %s" % (touche, description), "info")
        self.log("Double-cliquez une erreur de la console pour sauter à la "
                 "ligne fautive.", "info")
        self.log("Cliquez un numéro de ligne pour poser un point d'arrêt.", "info")
        self.log("Syntaxe du langage : panneau RACCOURCIS UVI du rail — "
                 "cliquez une ligne pour insérer un modèle.", "sys")

    def _attacher_infobulles(self):
        """ÉTAPE 7.4 : une infobulle par bouton dont la fonction n'est pas
        lisible sur l'étiquette.

        La barre d'outils n'est PAS réorganisée (contrainte explicite) :
        aucun bouton n'est déplacé, renommé, regroupé ni supprimé. On ajoute
        seulement de l'explication au survol. Les boutons évidents
        (ÉDITEUR, TERMINAL, OUVRIR, SAUVER, CLEAR) n'en reçoivent pas --
        une infobulle qui répète l'étiquette apprend à ignorer les
        infobulles.

        getattr défensif : btn_run_vm n'existe que si HAS_VM, et
        btn_auto_fix pourrait disparaître d'une future variante de la
        barre. Une infobulle manquante ne doit jamais empêcher l'IDE de
        démarrer."""
        cibles = [
            ("btn_run", "run"), ("btn_run_vm", "run_vm"),
            ("btn_ast", "ast"), ("btn_bytecode", "bytecode"),
            ("btn_optimiser", "optimiser"), ("btn_analyser", "analyser"),
            ("btn_auto_repair", "auto_repair"), ("btn_auto_fix", "auto_fix"),
            ("btn_stats", "stats"),
        ]
        for nom_attr, cle in cibles:
            widget = getattr(self, nom_attr, None)
            texte = TOOLBAR_TOOLTIPS.get(cle)
            if widget is not None and texte:
                _Tooltip(widget, texte, statusbar_fn=self._set_status)

    def _build_statusbar(self):
        barre = tk.Frame(self.root, bg=T.PANNEAU, height=34)
        barre.pack(fill=tk.X, side=tk.BOTTOM)
        barre.pack_propagate(False)
        tk.Frame(self.root, bg=T.BORDURE, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        gauche = tk.Frame(barre, bg=T.PANNEAU)
        gauche.pack(side=tk.LEFT, padx=14)
        self.voyant_etat = tk.Canvas(gauche, width=10, height=10, bg=T.PANNEAU,
                                     highlightthickness=0)
        self.voyant_etat.pack(side=tk.LEFT, pady=12)
        self.voyant_etat.create_oval(1, 1, 9, 9, fill=T.VERT, outline="")
        # self.statusbar garde son NOM : 13 appels existants (_set_status,
        # run_script, l'agent...) écrivent dedans. Seule son apparence change.
        self.statusbar = tk.Label(
            gauche, text=" PRÊT", fg=T.VERT, bg=T.PANNEAU,
            font=("Segoe UI", 9, "bold"), anchor=tk.W,
        )
        self.statusbar.pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(barre, text="UVI SECURE IDE  ·  ÉCRIT. VISE. CRÉE. EXÉCUTE.  ·  ⌃K ouvre le Portail",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=22)

        self.horloge = tk.Label(barre, text="--:--:--", bg=T.PANNEAU,
                                fg=T.TEXTE_FAIBLE, font=("Consolas", 9))
        self.horloge.pack(side=tk.RIGHT, padx=(8, 16))

        # Indicateur de connexion SHION : la maquette le demande, et il
        # répond à une vraie question que l'utilisateur se pose avant de
        # taper SHION.ASK -- « est-ce que le serveur répond ? ». Le savoir
        # AVANT plutôt qu'après un timeout de 180 s change l'expérience.
        cadre_shion = tk.Frame(barre, bg=T.PANNEAU)
        cadre_shion.pack(side=tk.RIGHT, padx=10)
        connecte = HAS_SHION and self.engine.shion_client is not None
        self.etiquette_shion = tk.Label(
            cadre_shion,
            text="CONNECTÉ À SHION" if connecte else "SHION HORS LIGNE",
            bg=T.PANNEAU, fg=T.TEXTE if connecte else T.TEXTE_FAIBLE,
            font=("Segoe UI", 8),
        )
        self.etiquette_shion.pack(side=tk.LEFT)
        self.voyant_shion = tk.Canvas(cadre_shion, width=10, height=10,
                                      bg=T.PANNEAU, highlightthickness=0)
        self.voyant_shion.pack(side=tk.LEFT, padx=6, pady=12)
        self.voyant_shion.create_oval(1, 1, 9, 9,
                                      fill=T.CYAN if connecte else T.TEXTE_FAIBLE,
                                      outline="")
        self._rafraichir_horloge()
        self._pomper_file_ui()

    def _poster_ui(self, travail):
        """Demande au thread principal d'exécuter `travail`.

        APPELABLE DEPUIS N'IMPORTE QUEL THREAD -- c'est tout l'intérêt.
        queue.Queue.put est protégé par un verrou interne, contrairement à
        root.after() qui est un appel Tcl et n'offre aucune garantie depuis
        un thread secondaire.

        Remplace tous les `self.root.after(0, ...)` qui étaient émis depuis
        des threads d'exécution. Ceux émis depuis le thread principal
        (horloge, rafraîchissement des panneaux) restent en after() : ils
        sont légitimes et n'ont pas besoin de la file."""
        self._file_ui.put(travail)

    def _pomper_file_ui(self):
        """Vide la file et se replanifie. Thread principal uniquement.

        50 ms : assez court pour que l'apparition de la barre de saisie
        paraisse instantanée (le seuil perceptif est autour de 100 ms),
        assez long pour ne pas peser quand il n'y a rien à faire -- la
        boucle ne coûte qu'un get_nowait qui échoue.

        get_nowait en boucle plutôt que get(block=True) : cette méthode
        tourne sur le thread de l'interface, et s'y bloquer figerait toute
        la fenêtre. C'est précisément le piège qu'on est en train de
        corriger, il serait absurde de le réintroduire ici."""
        try:
            while True:
                try:
                    travail = self._file_ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    travail()
                except Exception as e:
                    # Un travail fautif ne doit pas arrêter la pompe :
                    # sinon la première exception gèle définitivement tout
                    # échange thread -> interface, y compris la saisie.
                    try:
                        self.log("Erreur d'interface différée : %s : %s"
                                 % (type(e).__name__, e), "err")
                    except Exception:
                        pass
        finally:
            try:
                self.root.after(50, self._pomper_file_ui)
            except tk.TclError:
                pass  # fenêtre fermée

    def _rafraichir_horloge(self):
        """after() et non un thread : tout ce qui touche un widget doit
        rester sur le thread Tkinter. Un thread avec un sleep(1) écrivant
        dans un Label est le bug de concurrence classique de ce genre
        d'horloge -- il ne se manifeste que sporadiquement, ce qui le rend
        très coûteux à diagnostiquer plus tard."""
        try:
            self.horloge.config(text=datetime.now().strftime("%H:%M:%S"))
            self.root.after(1000, self._rafraichir_horloge)
        except tk.TclError:
            pass  # fenêtre fermée pendant l'attente : rien à rafraîchir

    # =====================================================================
    # DISPOSITION v9
    # =====================================================================
    # rail | colonne gauche | centre (éditeur + console + mémoire) | SHION
    #
    # DIFFÉRENCE STRUCTURELLE MAJEURE AVEC LA v8 : la console n'est plus une
    # VUE concurrente de l'éditeur, elle est un DOCK permanent sous lui. En
    # v8, voir sa sortie signifiait perdre son code de vue et inversement --
    # on basculait sans arrêt entre deux plein-écrans pour comparer un
    # message d'erreur à la ligne qui l'a produit. C'est le changement qui
    # se sent le plus à l'usage, bien plus que la couleur.
    #
    # Conséquence sur _show_view : "terminal" sort de la pile de vues
    # (self._VUES), puisque frame_terminal n'est plus empaqueté dans
    # self.workspace. show_terminal() est conservée -- elle est appelée à
    # une quinzaine d'endroits -- mais donne désormais le focus à la console
    # au lieu de masquer l'éditeur.

    def _cadre_defilant(self, parent, bg=None):
        """Zone à défilement vertical. Rend le cadre INTÉRIEUR où empiler le
        contenu.

        POURQUOI CE BRICOLAGE : Tkinter ne sait pas faire défiler un Frame.
        Seuls Text, Listbox et Canvas ont un protocole de défilement. Le
        seul montage possible est donc : Canvas défilant + Frame posé
        dedans via create_window + Scrollbar liée. C'est la recette standard
        et il n'y en a pas de plus simple -- ce n'est pas un détour, c'est
        LE moyen.

        Trois liaisons indispensables, chacune corrigeant un défaut réel :
          1. <Configure> sur le cadre intérieur -> recalcule scrollregion.
             Sans elle, la molette ne fait rien : le Canvas croit que son
             contenu tient entièrement dans la fenêtre.
          2. <Configure> sur le Canvas -> impose au cadre intérieur la
             largeur du Canvas. Sans elle, les panneaux se réduisent à la
             largeur de leur texte le plus long et flottent à gauche.
          3. molette : liée à l'ENTRÉE/SORTIE du pointeur, pas globalement.
             Un bind_all("<MouseWheel>") volerait la molette à l'éditeur et
             à la console dès que cette zone existe."""
        bg = bg or T.FOND
        conteneur = tk.Frame(parent, bg=bg)
        conteneur.pack(fill=tk.BOTH, expand=True)

        toile = tk.Canvas(conteneur, bg=bg, highlightthickness=0, bd=0)
        barre = tk.Scrollbar(conteneur, orient="vertical", command=toile.yview,
                             bg=T.PANNEAU, troughcolor=T.FOND, relief=tk.FLAT,
                             bd=0, width=10, activebackground=T.BORDURE_VIVE)
        toile.configure(yscrollcommand=barre.set)
        barre.pack(side=tk.RIGHT, fill=tk.Y)
        toile.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        interieur = tk.Frame(toile, bg=bg)
        fenetre = toile.create_window((0, 0), window=interieur, anchor="nw")

        def sur_contenu(_e=None):
            try:
                toile.configure(scrollregion=toile.bbox("all"))
            except tk.TclError:
                pass

        def sur_toile(event):
            try:
                toile.itemconfig(fenetre, width=event.width)
            except tk.TclError:
                pass

        interieur.bind("<Configure>", sur_contenu)
        toile.bind("<Configure>", sur_toile)

        def molette(event):
            # Windows/macOS envoient <MouseWheel> avec event.delta ;
            # X11 envoie des <Button-4>/<Button-5> sans delta. Les deux
            # chemins sont nécessaires, le projet tournant aussi sur
            # Raspberry Pi (donc X11).
            if getattr(event, "num", None) == 4:
                pas = -1
            elif getattr(event, "num", None) == 5:
                pas = 1
            else:
                pas = -1 if event.delta > 0 else 1
            try:
                toile.yview_scroll(pas, "units")
            except tk.TclError:
                pass

        def activer(_e=None):
            toile.bind_all("<MouseWheel>", molette)
            toile.bind_all("<Button-4>", molette)
            toile.bind_all("<Button-5>", molette)

        def desactiver(_e=None):
            for evenement in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    toile.unbind_all(evenement)
                except tk.TclError:
                    pass

        toile.bind("<Enter>", activer)
        toile.bind("<Leave>", desactiver)
        interieur.bind("<Enter>", activer)
        interieur.bind("<Leave>", desactiver)
        return interieur

    def _carte(self, parent, titre, accent=None, hauteur=None, menu=True):
        """Panneau titré du thème v9 (cf. « EXPLORATEUR UVI », « VARIABLES »,
        « OUTILS UVI » de la maquette). Rend le conteneur de contenu."""
        exterieur = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                             highlightbackground=T.BORDURE)
        if hauteur:
            exterieur.configure(height=hauteur)
            exterieur.pack_propagate(False)
        entete = tk.Frame(exterieur, bg=T.PANNEAU_HAUT, height=34)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        # self._tr (pas juste text=titre) : _carte() est le point de
        # construction PARTAGÉ de la quasi-totalité des panneaux titrés de
        # l'IDE (PARAMÈTRES, MODULES, RAPPORTS DE BUGS...) -- un seul
        # changement ici rend TOUS ces titres traduisibles en direct (voir
        # TRANSLATIONS/_t/_tr), sans toucher chacun de leurs appelants.
        # Un titre sans entrée dans TRANSLATIONS reste simplement affiché
        # en français, jamais cassé.
        self._tr(tk.Label(entete, bg=T.PANNEAU_HAUT,
                          fg=accent or T.CYAN, font=T.UI_TITRE), titre).pack(
                              side=tk.LEFT, padx=12)
        if menu:
            tk.Label(entete, text="···", bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 11)).pack(side=tk.RIGHT, padx=12)
        contenu = tk.Frame(exterieur, bg=T.PANNEAU)
        contenu.pack(fill=tk.BOTH, expand=True)
        return contenu

    def _build_container(self):
        self.container = tk.Frame(self.root, bg=T.FOND)
        self.container.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        self._build_rail()
        self._build_sidebar()

        centre = tk.Frame(self.container, bg=T.FOND)
        centre.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)

        # --- dock inférieur : console + mémoire ---------------------------
        # BUGFIX (dock invisible sur fenêtre étroite en hauteur) : ce cadre
        # porte la console ET la barre de saisie interactive qui répond à
        # ?var (voir _ouvrir_saisie) -- sans lui à l'écran, un script qui
        # attend une réponse semble figé sans qu'on sache pourquoi. Il est
        # empaqueté ICI, EN PREMIER (avant cadre_editeur), et avec
        # side=BOTTOM plutôt que le TOP implicite : cadre_editeur est
        # extensible (expand=True, plus bas) et absorbe tout l'espace
        # restant une fois les cadres à taille FIXE placés -- mais si
        # cadre_editeur était empaqueté en premier, c'est LUI qui
        # capturerait la cavité en premier, et ce dock (hauteur fixe,
        # pack_propagate(False)) se retrouverait poussé hors de la fenêtre
        # dès que celle-ci est plus basse que la somme des deux (ex. écran
        # 1366x768, cf. le clamp de geometry() dans _build_window). En le
        # plaçant en premier avec side=BOTTOM, il réserve ses 290 px EN BAS
        # de la cavité AVANT que cadre_editeur ne prenne le reste -- même
        # position visuelle qu'avant (l'éditeur au-dessus, le dock en
        # dessous), mais son espace n'est plus jamais sacrifié en premier.
        dock = tk.Frame(centre, bg=T.FOND, height=290)
        dock.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        dock.pack_propagate(False)
        # Gardés sur self (dock était purement local avant) : nécessaires
        # à _basculer_dock_console pour replier/déplier tout le dock d'un
        # bloc -- voir sa docstring pour pourquoi cadre_editeur doit lui
        # aussi être ré-empaqueté à chaque bascule, pas seulement dock.
        self.dock = dock
        self._dock_ouvert = True

        # workspace = pile de vues (éditeur / canevas).
        # Nom conservé : _show_view et les _build_*_tab l'utilisent.
        cadre_editeur = tk.Frame(centre, bg=T.PANNEAU, highlightthickness=1,
                                 highlightbackground=T.BORDURE)
        cadre_editeur.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.cadre_editeur = cadre_editeur
        self._build_barre_onglets(cadre_editeur)
        self.workspace = tk.Frame(cadre_editeur, bg=T.PANNEAU)
        self.workspace.pack(fill=tk.BOTH, expand=True)

        self._build_editor()
        self._build_canvas_tab()
        self._build_barre_etat_editeur(cadre_editeur)

        self._build_terminal(dock)
        self._build_panneau_memoire(dock)

        self._build_colonne_shion()

    # ------------------------------------------------------------------
    # Rail d'icônes
    # ------------------------------------------------------------------
    def _build_rail(self):
        """Colonne d'icônes de gauche. Chaque entrée bascule un panneau ou
        une vue déjà existante -- le rail n'ajoute AUCUNE fonctionnalité, il
        rend visible ce qui était enfoui dans une barre d'outils de treize
        boutons où rien ne ressortait."""
        rail = tk.Frame(self.container, bg=T.PANNEAU, width=84)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        rail.pack_propagate(False)

        self._boutons_rail = {}
        entrees = [
            ("explorateur", "📁", "EXPLORER", None),
            ("recherche", "🔍", "RECHERCHE", None),
            ("variables", "{ }", "VARIABLES", None),
            ("outils", "🛠", "OUTILS", None),
            ("modules", "📦", "MODULES", None),
            ("tests", "🧪", "TESTS", None),
            ("parametres", "⚙", "PARAMÈTRES", None),
        ]
        # commande=None dans la table : chaque entrée bascule vers le
        # panneau homonyme. Avant, elles appelaient _focus_panneau, qui ne
        # faisait que faire CLIGNOTER un titre -- l'utilisateur cliquait,
        # rien ne s'ouvrait, il en concluait que le bouton était mort. Le
        # rail pilote désormais réellement le contenu de la colonne gauche,
        # sur le modèle d'une barre d'activité d'éditeur moderne.
        for cle, icone, libelle, _ in entrees:
            commande = (lambda c=cle: self._basculer_panneau_gauche(c))
            bloc = tk.Frame(rail, bg=T.PANNEAU, cursor="hand2")
            bloc.pack(fill=tk.X, pady=3)
            icone_lbl = tk.Label(bloc, text=icone, bg=T.PANNEAU, fg=T.TEXTE,
                                 font=("Segoe UI", 17))
            icone_lbl.pack(pady=(9, 1))
            texte_lbl = tk.Label(bloc, bg=T.PANNEAU,
                                 fg=T.TEXTE_FAIBLE, font=("Segoe UI", 6, "bold"))
            self._tr(texte_lbl, libelle)
            texte_lbl.pack(pady=(0, 9))
            # Le clic doit fonctionner sur le bloc ET sur ses deux enfants :
            # un Label posé sur un Frame intercepte l'événement, donc lier
            # seulement le Frame rend la moitié haute du bouton inerte.
            for widget in (bloc, icone_lbl, texte_lbl):
                widget.bind("<Button-1>", lambda e, c=commande: c())
                widget.bind("<Enter>", lambda e, b=bloc, i=icone_lbl:
                            (b.config(bg=T.SURVOL), i.config(bg=T.SURVOL)))
                widget.bind("<Leave>", lambda e, b=bloc, i=icone_lbl:
                            (b.config(bg=T.PANNEAU), i.config(bg=T.PANNEAU)))
            self._boutons_rail[cle] = (bloc, icone_lbl, texte_lbl)

        pied = tk.Canvas(rail, width=52, height=52, bg=T.PANNEAU,
                         highlightthickness=0)
        pied.pack(side=tk.BOTTOM, pady=16)
        pied.create_rectangle(2, 2, 50, 50, outline=T.BORDURE_VIVE, width=1)
        pied.create_text(26, 24, text="UVI", fill=T.CYAN,
                         font=("Segoe UI", 11, "bold"))

    def _basculer_panneau_gauche(self, nom):
        """Clic sur une icône du RAIL (pas les autres appelants de
        afficher_panneau_gauche, ex. menu « Panneau : Modules ») : referme
        la colonne si `nom` est déjà le panneau actif ET visible, sinon
        l'ouvre/y bascule -- même comportement qu'une barre d'activité
        d'éditeur moderne (VS Code). Rend à l'éditeur/canevas les 286px de
        la colonne à la demande, sans jamais les perdre pour de bon (un
        second clic les rouvre)."""
        if self.frame_sidebar.winfo_ismapped() and getattr(self, "_panneau_gauche_actif", None) == nom:
            self.frame_sidebar.pack_forget()
            return
        self.afficher_panneau_gauche(nom)

    def afficher_panneau_gauche(self, nom):
        """Bascule la colonne gauche vers le panneau `nom` et met le rail en
        accord. RÉOUVRE la colonne si elle avait été repliée (voir
        _basculer_panneau_gauche) -- tout appelant DIRECT de cette méthode
        (menus, quick-access) veut voir `nom`, jamais juste changer son
        contenu en coulisses pendant qu'elle reste invisible.

        Les sept panneaux sont construits UNE fois au démarrage puis
        masqués/affichés par pack_forget/pack -- pas reconstruits à chaque
        clic. Reconstruire perdrait l'état à chaque bascule : le texte tapé
        dans la recherche, les expressions surveillées, les champs de
        paramètres à moitié remplis."""
        panneaux = getattr(self, "_panneaux_gauche", {})
        if nom not in panneaux:
            return
        if not self.frame_sidebar.winfo_ismapped():
            self.frame_sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0), pady=6)
        for cle, cadre in panneaux.items():
            cadre.pack_forget()
        panneaux[nom].pack(fill=tk.BOTH, expand=True)
        self._panneau_gauche_actif = nom
        self.titre_colonne_gauche.config(
            text=self._t(self._TITRES_PANNEAUX.get(nom, nom.upper())))

        for cle, (bloc, icone, texte) in getattr(self, "_boutons_rail", {}).items():
            actif = (cle == nom)
            fond = T.SURVOL if actif else T.PANNEAU
            try:
                bloc.config(bg=fond)
                icone.config(bg=fond, fg=T.CYAN if actif else T.TEXTE)
                texte.config(bg=fond, fg=T.TEXTE if actif else T.TEXTE_FAIBLE)
            except tk.TclError:
                pass

        # Les panneaux qui affichent un état vivant sont rafraîchis à
        # l'ouverture : un panneau MODULES montrant l'état d'il y a dix
        # minutes serait pire qu'un panneau vide.
        rafraichisseurs = {
            "explorateur": self.update_vfs_list,
            "variables": self.rafraichir_inspecteur,
            "modules": self.rafraichir_modules,
            "parametres": self.rafraichir_parametres,
        }
        action = rafraichisseurs.get(nom)
        if action:
            try:
                action()
            except Exception:
                pass

    def _focus_panneau(self, nom):
        """Conservée : _build_colonne_shion et d'anciens appels la
        référencent. Redirige vers la vraie bascule."""
        self.afficher_panneau_gauche(nom)

    def _ouvrir_recherche(self):
        """Recherche dans l'éditeur, via la barre de commande déjà présente
        sous la console -- plutôt qu'une boîte de dialogue supplémentaire."""
        self.show_terminal()
        self.cmd_input.delete(0, tk.END)
        self.cmd_input.insert(0, "chercher ")
        self.cmd_input.focus_set()
        self.cmd_input.icursor(tk.END)
        self.log("Tapez le texte à chercher après « chercher » puis Entrée.", "sys")

    def _afficher_modules(self):
        self.show_terminal()
        modules = sorted(getattr(self.engine, "modules", {}) or {})
        self.log("=== MODULES CHARGÉS ===", "sys")
        if not modules:
            self.log("Aucun module chargé. Utilisez MODULE \"fichier.uvi\" -> alias.", "info")
        for alias in modules:
            self.log("  %s" % alias, "info")
        fichiers = [f for f in self.vfs.list_files() if f.endswith(".uvi")]
        self.log("Fichiers .uvi disponibles : %s" % (", ".join(fichiers) or "aucun"), "info")

    def _lancer_tests(self):
        """Onglet TESTS : exécute le script dans les DEUX moteurs et compare.

        Ce n'est pas un lanceur de tests unitaires (ceux-là vivent dans
        tests/, sous pytest). C'est la version interactive de la propriété
        la plus utile du projet -- tree-walker et VM doivent rendre le même
        résultat observable. Un écart signalé ici est un vrai bug de moteur,
        pas une erreur de l'utilisateur."""
        self._selectionner_onglet_console("TESTS")
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.log("Éditeur vide : rien à comparer.", "err")
            return
        lignes = code.split("\n")
        self.log("=== COMPARAISON DES DEUX MOTEURS ===", "sys")
        hote = HeadlessHost()
        moteur = UVIEngine(hote, InMemoryVFS())
        try:
            moteur.run_sequence(list(lignes))
            sortie_tw, erreurs_tw = list(hote.sortie), list(hote.erreurs)
        except Exception as e:
            sortie_tw, erreurs_tw = [], ["exception : %s" % e]
        try:
            # max_string_length/max_call_depth/max_array_size alignés sur
            # les plafonds du tree-walker (MAX_STRING_LENGTH/MAX_CALL_DEPTH/
            # MAX_ARRAY_SIZE) -- sans ça, cette comparaison même ne peut pas
            # détecter une divergence causée par ces plafonds, puisqu'elle
            # donnerait alors à chaque moteur une limite différente (voir
            # le même correctif dans run_script_vm).
            resultat = compile_and_run(list(lignes), self.engine.parser,
                                       UVIBytecodeCompilerV2(), vfs=InMemoryVFS(),
                                       max_string_length=MAX_STRING_LENGTH,
                                       max_call_depth=MAX_CALL_DEPTH,
                                       max_array_size=MAX_ARRAY_SIZE)
            sortie_vm = [str(x) for x in resultat.output]
            trap = resultat.trapped
        except Exception as e:
            sortie_vm, trap = [], type(e).__name__
        self.log("  tree-walker : %d ligne(s), %d erreur(s)"
                 % (len(sortie_tw), len(erreurs_tw)), "info")
        self.log("  VM          : %d ligne(s), trap=%s" % (len(sortie_vm), trap), "info")
        if sortie_tw == sortie_vm and not erreurs_tw and trap is None:
            self.log("✓ Les deux moteurs sont d'accord.", "ok")
        else:
            self.log("✗ DIVERGENCE entre les moteurs — c'est un bug du moteur, "
                     "pas de votre script.", "err")
            for i, (a, b) in enumerate(zip(sortie_tw, sortie_vm)):
                if a != b:
                    self.log("  ligne %d : tree-walker=%r | VM=%r" % (i + 1, a, b), "err")
            for e in erreurs_tw[:3]:
                self.log("  tree-walker : %s" % e, "err")

    def _afficher_parametres(self):
        self.show_terminal()
        self.log("=== PARAMÈTRES UVI ===", "sys")
        for libelle, valeur in (
            ("Backend SHION", SHION_BACKEND),
            ("Serveur SHION", SHION_SERVER_URL),
            ("Modèle", DEEPSEEK_MODEL_NAME if SHION_BACKEND != "shion" else SHION_MODEL_NAME),
            ("SHION disponible", "oui" if HAS_SHION and self.engine.shion_client else "non"),
            ("VM disponible", "oui" if HAS_VM else "non"),
            ("Stockage", self.vfs.storage_dir if hasattr(self.vfs, "storage_dir") else "mémoire"),
            ("Mémoire max", "%d octets" % MAX_MEMORY_BYTES),
        ):
            self.log("  %-18s %s" % (libelle + " :", valeur), "info")
        self.log("Ces valeurs se règlent par variables d'environnement — "
                 "voir docs/SHION_ARCHITECTURE.md (backend shion) ou "
                 "docs/SERVEUR_DEEPSEEK.md (backend deepseek).", "sys")

    # ------------------------------------------------------------------
    # Colonne gauche
    # ------------------------------------------------------------------
    _TITRES_PANNEAUX = {
        "explorateur": "EXPLORATEUR UVI",
        "recherche": "RECHERCHE",
        "variables": "INSPECTEUR DE VARIABLES",
        "outils": "OUTILS UVI",
        "modules": "MODULES & PAQUETS",
        "tests": "TESTS",
        "parametres": "PARAMÈTRES",
    }

    def _build_sidebar(self):
        """Colonne gauche : sept panneaux pilotés par le rail.

        Garde son nom et self.frame_sidebar (référencés ailleurs), ainsi que
        self.vfs_listbox dont dépendent update_vfs_list() et
        _on_vfs_double_click()."""
        self.frame_sidebar = tk.Frame(self.container, bg=T.FOND, width=286)
        self.frame_sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0), pady=6)
        self.frame_sidebar.pack_propagate(False)

        entete = tk.Frame(self.frame_sidebar, bg=T.PANNEAU_HAUT, height=34)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        self.titre_colonne_gauche = tk.Label(
            entete, text="EXPLORATEUR UVI", bg=T.PANNEAU_HAUT, fg=T.CYAN,
            font=T.UI_TITRE)
        self.titre_colonne_gauche.pack(side=tk.LEFT, padx=12)

        self.zone_panneaux = tk.Frame(self.frame_sidebar, bg=T.FOND)
        self.zone_panneaux.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        self._panneaux_gauche = {}
        self._build_panneau_explorateur()
        self._build_panneau_recherche()
        self._build_panneau_variables()
        self._build_panneau_outils()
        self._build_panneau_modules()
        self._build_panneau_tests()
        self._build_panneau_parametres()
        self.afficher_panneau_gauche("explorateur")

    def _nouveau_panneau(self, nom):
        cadre = tk.Frame(self.zone_panneaux, bg=T.FOND)
        self._panneaux_gauche[nom] = cadre
        return cadre

    # ==================================================================
    # 1. EXPLORATEUR
    # ==================================================================
    def _build_panneau_explorateur(self):
        """Explorateur + variables + aide-mémoire, EMPILÉS ET DÉFILANTS.

        Les trois cartes ne tenaient pas dans la colonne : l'aide-mémoire
        était coupée en bas de fenêtre, invisible sur un écran de portable.
        Le défilement les rend toutes atteignables sans rien retirer."""
        panneau = self._nouveau_panneau("explorateur")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "FICHIERS", menu=False)
        contenu.master.pack(fill=tk.X)
        self._entete_explorateur = self.titre_colonne_gauche

        barre_actions = tk.Frame(contenu, bg=T.PANNEAU)
        barre_actions.pack(fill=tk.X, padx=10, pady=(8, 4))
        for icone, infobulle, commande in (
            ("＋", "Nouveau fichier dans le stockage", self.creer_fichier_vfs),
            ("✎", "Renommer le fichier sélectionné", self.renommer_fichier_vfs),
            ("⧉", "Dupliquer le fichier sélectionné", self.dupliquer_fichier_vfs),
            ("🗑", "Supprimer le fichier sélectionné", self.supprimer_fichier_vfs),
            ("🔄", "Actualiser la liste", self.update_vfs_list),
        ):
            bouton = tk.Button(
                barre_actions, text=icone, bg=T.CHAMP, fg=T.TEXTE,
                activebackground=T.SURVOL, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 9), cursor="hand2", command=commande,
                padx=8, pady=3)
            bouton.pack(side=tk.LEFT, padx=2)
            _Tooltip(bouton, infobulle, statusbar_fn=self._set_status)

        tk.Label(contenu, text="▾  PROJET_UVI", bg=T.PANNEAU, fg=T.TEXTE_FORT,
                 font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, padx=12, pady=(4, 2))

        cadre_liste = tk.Frame(contenu, bg=T.PANNEAU, height=230)
        cadre_liste.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        cadre_liste.pack_propagate(False)
        defilement = tk.Scrollbar(cadre_liste, orient="vertical", bg=T.PANNEAU,
                                  troughcolor=T.CHAMP, relief=tk.FLAT, bd=0, width=9)
        defilement.pack(side=tk.RIGHT, fill=tk.Y)
        self.vfs_listbox = tk.Listbox(
            cadre_liste, bg=T.PANNEAU, fg=T.TEXTE,
            selectbackground=T.SURVOL, selectforeground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 10), highlightthickness=0,
            activestyle="none", borderwidth=0, yscrollcommand=defilement.set)
        self.vfs_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        defilement.config(command=self.vfs_listbox.yview)
        self.vfs_listbox.bind("<Double-Button-1>", self._on_vfs_double_click)

        contenu_vars = self._carte(interieur, "VARIABLES", hauteur=200, menu=False)
        contenu_vars.master.pack(fill=tk.X, pady=(8, 0))
        self._entete_variables = self.titre_colonne_gauche
        self.table_variables = tk.Text(
            contenu_vars, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6)
        self.table_variables.pack(fill=tk.BOTH, expand=True)
        for etiquette, couleur in (("nom", T.TEXTE), ("txt", T.SYN_CHAINE),
                                   ("num", T.SYN_NOMBRE), ("tab", T.VIOLET),
                                   ("bool", T.VERT), ("vide", T.TEXTE_FAIBLE)):
            self.table_variables.tag_configure(etiquette, foreground=couleur)

        contenu_aide = self._carte(interieur, "RACCOURCIS UVI", menu=False)
        contenu_aide.master.pack(fill=tk.X, pady=(8, 8))
        for symbole, nom, description, couleur in self._AIDE_SYNTAXE:
            ligne = tk.Frame(contenu_aide, bg=T.PANNEAU, cursor="hand2")
            ligne.pack(fill=tk.X, padx=10, pady=2)
            tk.Label(ligne, text=symbole, bg=T.CHAMP, fg=couleur,
                     font=("Consolas", 9, "bold"), width=4).pack(side=tk.LEFT)
            tk.Label(ligne, text=nom, bg=T.PANNEAU, fg=T.TEXTE,
                     font=("Segoe UI", 8, "bold"), width=10,
                     anchor=tk.W).pack(side=tk.LEFT, padx=(7, 0))
            tk.Label(ligne, text=description, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 7), anchor=tk.W).pack(side=tk.LEFT)
            # Cliquer insère un gabarit dans l'éditeur : l'aide-mémoire
            # devient un outil de saisie et plus seulement une légende.
            for widget in (ligne,) + tuple(ligne.winfo_children()):
                widget.bind("<Button-1>",
                            lambda e, s=symbole: self.inserer_gabarit(s))
        tk.Label(contenu_aide, text="Cliquez une ligne pour insérer un modèle.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 7, "italic")).pack(anchor=tk.W, padx=12, pady=(4, 10))

    _AIDE_SYNTAXE = (
        ("!", "AFFICHER", "Afficher un texte ou une variable", "#22C55E"),
        ("?", "DEMANDER", "Lire une saisie utilisateur", "#22D3EE"),
        ("%%", "BOUCLE", "Répéter tant que la condition tient", "#3B82F6"),
        ("%", "RÉPÉTER", "Répéter un nombre de fois", "#3B82F6"),
        ("~", "CONDITION", "Exécuter selon une condition", "#A855F7"),
        ("DEF", "FONCTION", "Définir une fonction", "#F59E0B"),
        ("CALL", "APPELER", "Appeler une fonction", "#38BDF8"),
        ("TAB", "TABLEAU", "Créer et manipuler un tableau", "#A855F7"),
        ("STR", "CHAÎNE", "Opérations sur les chaînes", "#FBBF24"),
        ("&", "DISQUE", "Lire et écrire dans le stockage", "#22D3EE"),
    )

    _GABARITS = {
        "!": '!"votre message"',
        "?": "? variable",
        "%%": "%% condition -> instruction",
        "%": "% 10 -> instruction",
        "~": '~ condition -> !"vrai"',
        "DEF": "DEF ma_fonction(a) -> _retour = a",
        "CALL": "CALL ma_fonction(1) -> resultat",
        "TAB": "TAB.CREATE mon_tableau",
        "STR": 'STR.LEN "texte" -> longueur',
        "&": '&save "fichier.uvi" -> "contenu"',
    }

    def inserer_gabarit(self, symbole):
        """Insère un modèle de la construction cliquée à la position du
        curseur. Les gabarits sont écrits dans la VRAIE syntaxe UVI, vérifiée
        par les tests du parser -- pas de pseudo-code approximatif qui
        obligerait l'utilisateur à deviner la forme correcte."""
        gabarit = self._GABARITS.get(symbole)
        if not gabarit:
            return
        self.show_editor()
        self.editor.insert(tk.INSERT, gabarit + "\n")
        self._update_line_numbers()
        self._set_status("Modèle inséré : %s" % symbole)

    # -- actions sur les fichiers du VFS --------------------------------
    def _fichier_selectionne(self):
        selection = self.vfs_listbox.curselection()
        if not selection:
            return None
        return getattr(self, "_index_fichiers", {}).get(selection[0])

    def creer_fichier_vfs(self):
        nom = self._demander_texte("Nouveau fichier", "Nom du fichier (.uvi) :")
        if not nom:
            return
        if not nom.endswith(".uvi"):
            nom += ".uvi"
        try:
            if self.vfs.exists(nom):
                self.log("[FILE_EXISTS] %s existe déjà." % nom, "err")
                return
            self.vfs.write_file(nom, '@UVI\n!"Bonjour depuis %s"\n' % nom)
        except UVIRuntimeTrap as trap:
            # Les noms de fichiers passent par _safe_resolve : « ../evasion »
            # est refusé ici comme il l'est depuis un script.
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s créé." % nom)

    def renommer_fichier_vfs(self):
        ancien = self._fichier_selectionne()
        if not ancien:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        nouveau = self._demander_texte("Renommer", "Nouveau nom :", ancien)
        if not nouveau or nouveau == ancien:
            return
        try:
            contenu = self.vfs.read_file(ancien) or ""
            self.vfs.write_file(nouveau, contenu)
            self.vfs.delete_file(ancien)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s → %s" % (ancien, nouveau))

    def dupliquer_fichier_vfs(self):
        nom = self._fichier_selectionne()
        if not nom:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        base = nom[:-4] if nom.endswith(".uvi") else nom
        copie = "%s_copie.uvi" % base
        try:
            self.vfs.write_file(copie, self.vfs.read_file(nom) or "")
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("Copie créée : %s" % copie)

    def supprimer_fichier_vfs(self):
        nom = self._fichier_selectionne()
        if not nom:
            self.log("Sélectionnez d'abord un fichier.", "err")
            return
        if not tk.messagebox.askyesno("Supprimer",
                                      "Supprimer définitivement %s ?" % nom):
            return
        try:
            self.vfs.delete_file(nom)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        self.update_vfs_list()
        self._set_status("%s supprimé." % nom)

    def _demander_texte(self, titre, invite, valeur_initiale=""):
        """Petite boîte de saisie modale, écrite ici plutôt qu'importée de
        tkinter.simpledialog : simpledialog impose son propre style clair,
        qui jurerait au milieu d'une interface entièrement sombre. Vingt
        lignes évitent cette rupture visuelle."""
        fenetre = tk.Toplevel(self.root)
        fenetre.title(titre)
        fenetre.configure(bg=T.PANNEAU)
        fenetre.geometry("380x150")
        fenetre.transient(self.root)
        fenetre.grab_set()
        tk.Label(fenetre, text=invite, bg=T.PANNEAU, fg=T.TEXTE,
                 font=T.UI).pack(anchor=tk.W, padx=18, pady=(18, 6))
        champ = tk.Entry(fenetre, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
                         relief=tk.FLAT, font=("Consolas", 10),
                         highlightthickness=1, highlightbackground=T.BORDURE,
                         highlightcolor=T.CYAN)
        champ.pack(fill=tk.X, padx=18, ipady=6)
        champ.insert(0, valeur_initiale)
        champ.focus_set()
        resultat = {"valeur": None}

        def valider(_e=None):
            resultat["valeur"] = champ.get().strip()
            fenetre.destroy()

        champ.bind("<Return>", valider)
        champ.bind("<Escape>", lambda e: fenetre.destroy())
        barre = tk.Frame(fenetre, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=18, pady=16)
        tk.Button(barre, text="Valider", bg=T.BLEU, fg="#FFFFFF", relief=tk.FLAT,
                  bd=0, font=T.UI_GRAS, cursor="hand2", command=valider,
                  padx=16, pady=5).pack(side=tk.RIGHT)
        tk.Button(barre, text="Annuler", bg=T.CHAMP, fg=T.TEXTE, relief=tk.FLAT,
                  bd=0, font=T.UI, cursor="hand2", command=fenetre.destroy,
                  padx=16, pady=5).pack(side=tk.RIGHT, padx=8)
        self.root.wait_window(fenetre)
        return resultat["valeur"]

    # ==================================================================
    # 2. RECHERCHE
    # ==================================================================
    def _build_panneau_recherche(self):
        """Recherche dans l'éditeur ET dans tout le stockage.

        La v9 renvoyait auparavant vers la barre de commande avec un
        « tapez chercher... » -- une redirection, pas une fonctionnalité.
        Ici la recherche est réelle : résultats cliquables qui sautent à la
        ligne, ou ouvrent le fichier concerné."""
        panneau = self._nouveau_panneau("recherche")
        contenu = self._carte(panneau, "RECHERCHER", menu=False)
        contenu.master.pack(fill=tk.BOTH, expand=True)

        self.champ_recherche = tk.Entry(
            contenu, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 10), highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.CYAN)
        self.champ_recherche.pack(fill=tk.X, padx=10, pady=(10, 6), ipady=6)
        self.champ_recherche.bind("<Return>", lambda e: self.lancer_recherche())

        options = tk.Frame(contenu, bg=T.PANNEAU)
        options.pack(fill=tk.X, padx=10)
        self.portee_recherche = tk.StringVar(value="editeur")
        for valeur, libelle in (("editeur", "Éditeur"), ("vfs", "Tout le stockage")):
            tk.Radiobutton(
                options, text=libelle, value=valeur, variable=self.portee_recherche,
                bg=T.PANNEAU, fg=T.TEXTE, selectcolor=T.CHAMP,
                activebackground=T.PANNEAU, activeforeground=T.CYAN,
                font=("Segoe UI", 8), relief=tk.FLAT, highlightthickness=0,
                command=self.lancer_recherche).pack(side=tk.LEFT, padx=(0, 10))

        self._bouton_plat(contenu, "Rechercher", self.lancer_recherche,
                          couleur=T.CYAN, icone="🔍").pack(fill=tk.X, padx=10, pady=8)

        self.compteur_recherche = tk.Label(
            contenu, text="Aucune recherche.", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Segoe UI", 8), anchor=tk.W)
        self.compteur_recherche.pack(fill=tk.X, padx=12)

        cadre = tk.Frame(contenu, bg=T.PANNEAU)
        cadre.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))
        defilement = tk.Scrollbar(cadre, orient="vertical", bg=T.PANNEAU,
                                  troughcolor=T.CHAMP, relief=tk.FLAT, bd=0, width=9)
        defilement.pack(side=tk.RIGHT, fill=tk.Y)
        self.resultats_recherche = tk.Listbox(
            cadre, bg=T.PANNEAU, fg=T.TEXTE, selectbackground=T.SURVOL,
            selectforeground=T.CYAN, relief=tk.FLAT, font=("Consolas", 9),
            highlightthickness=0, activestyle="none", borderwidth=0,
            yscrollcommand=defilement.set)
        self.resultats_recherche.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        defilement.config(command=self.resultats_recherche.yview)
        self.resultats_recherche.bind("<Double-Button-1>", self._aller_au_resultat)
        self._cibles_recherche = {}

    def lancer_recherche(self):
        motif = self.champ_recherche.get().strip()
        self.resultats_recherche.delete(0, tk.END)
        self._cibles_recherche = {}
        if not motif:
            self.compteur_recherche.config(text="Saisissez un texte à chercher.")
            return
        minuscule = motif.lower()
        position = 0

        if self.portee_recherche.get() == "editeur":
            lignes = self.editor.get("1.0", tk.END).split("\n")
            for numero, ligne in enumerate(lignes, start=1):
                if minuscule in ligne.lower():
                    self.resultats_recherche.insert(
                        tk.END, "L%-4d %s" % (numero, ligne.strip()[:40]))
                    self._cibles_recherche[position] = ("editeur", None, numero)
                    position += 1
        else:
            for nom in self.vfs.list_files():
                if nom.startswith("."):
                    continue
                try:
                    contenu = self.vfs.read_file(nom) or ""
                except UVIRuntimeTrap:
                    continue
                for numero, ligne in enumerate(contenu.split("\n"), start=1):
                    if minuscule in ligne.lower():
                        self.resultats_recherche.insert(
                            tk.END, "%s:%d  %s" % (nom[:14], numero, ligne.strip()[:26]))
                        self._cibles_recherche[position] = ("vfs", nom, numero)
                        position += 1

        self.compteur_recherche.config(
            text="%d résultat(s) — double-cliquez pour y aller." % position
            if position else "Aucun résultat pour « %s »." % motif)

    def _aller_au_resultat(self, _event=None):
        selection = self.resultats_recherche.curselection()
        if not selection:
            return
        cible = self._cibles_recherche.get(selection[0])
        if not cible:
            return
        portee, nom, numero = cible
        if portee == "vfs":
            contenu = self.vfs.read_file(nom)
            if contenu is None:
                return
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", contenu)
            if hasattr(self, "etiquette_fichier"):
                self.etiquette_fichier.config(text=nom)
        self.show_editor()
        try:
            self.editor.mark_set(tk.INSERT, "%d.0" % numero)
            self.editor.see("%d.0" % numero)
            self.editor.tag_remove("ligne_courante", "1.0", tk.END)
            self.editor.tag_add("ligne_courante", "%d.0" % numero, "%d.end" % numero)
        except tk.TclError:
            pass
        self._update_line_numbers()
        self._rafraichir_position_curseur()

    # ==================================================================
    # 3. INSPECTEUR DE VARIABLES
    # ==================================================================
    def _build_panneau_variables(self):
        """Inspecteur complet : toutes les variables, leur type, et des
        expressions surveillées.

        Les expressions surveillées sont évaluées par
        UVIEvaluator.evaluate() -- le VRAI évaluateur du langage, pas un
        eval() Python. Écrire « x * 2 » ou « alias::CONSTANTE » dans une
        surveillance suit donc exactement les mêmes règles qu'une expression
        de script, y compris les gardes de mémoire."""
        panneau = self._nouveau_panneau("variables")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "ÉTAT DE LA MÉMOIRE", menu=False)
        contenu.master.pack(fill=tk.X)
        barre = tk.Frame(contenu, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.montrer_variables_internes = tk.BooleanVar(value=False)
        tk.Checkbutton(
            barre, text="Variables internes (_)",
            variable=self.montrer_variables_internes,
            bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, selectcolor=T.CHAMP,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            font=("Segoe UI", 8), relief=tk.FLAT, highlightthickness=0,
            command=self.rafraichir_inspecteur).pack(side=tk.LEFT)
        self._bouton_plat(barre, "Actualiser", self.rafraichir_inspecteur,
                          couleur=T.CYAN, icone="🔄", gras=False,
                          police=("Segoe UI", 8)).pack(side=tk.RIGHT)

        self.inspecteur = tk.Text(
            contenu, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6, height=16)
        self.inspecteur.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        for etiquette, couleur in (("nom", T.TEXTE_FORT), ("type", T.TEXTE_FAIBLE),
                                   ("txt", T.SYN_CHAINE), ("num", T.SYN_NOMBRE),
                                   ("tab", T.VIOLET), ("bool", T.VERT),
                                   ("vide", T.TEXTE_FAIBLE)):
            self.inspecteur.tag_configure(etiquette, foreground=couleur)

        contenu_surv = self._carte(interieur, "EXPRESSIONS SURVEILLÉES", menu=False)
        contenu_surv.master.pack(fill=tk.X, pady=(8, 8))
        saisie = tk.Frame(contenu_surv, bg=T.PANNEAU)
        saisie.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.champ_surveillance = tk.Entry(
            saisie, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Consolas", 9), highlightthickness=1,
            highlightbackground=T.BORDURE, highlightcolor=T.CYAN)
        self.champ_surveillance.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.champ_surveillance.bind("<Return>", lambda e: self.ajouter_surveillance())
        tk.Button(saisie, text="＋", bg=T.BLEU, fg="#FFFFFF", relief=tk.FLAT,
                  bd=0, font=("Segoe UI", 9, "bold"), cursor="hand2",
                  command=self.ajouter_surveillance, padx=10).pack(side=tk.RIGHT, padx=(6, 0))

        self.surveillances = []
        self.liste_surveillances = tk.Text(
            contenu_surv, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=6, height=7)
        self.liste_surveillances.pack(fill=tk.BOTH, expand=True)
        self.liste_surveillances.tag_configure("expr", foreground=T.CYAN)
        self.liste_surveillances.tag_configure("val", foreground=T.SYN_CHAINE)
        self.liste_surveillances.tag_configure("err", foreground=T.ROUGE)
        self.liste_surveillances.tag_configure("vide", foreground=T.TEXTE_FAIBLE)
        self._bouton_plat(contenu_surv, "Tout effacer",
                          self.effacer_surveillances, couleur=T.TEXTE_FAIBLE,
                          gras=False, police=("Segoe UI", 8)).pack(
            fill=tk.X, padx=10, pady=(4, 10))

    def rafraichir_inspecteur(self):
        if not hasattr(self, "inspecteur"):
            return
        try:
            self.inspecteur.config(state=tk.NORMAL)
            self.inspecteur.delete("1.0", tk.END)
            memoire = getattr(self.engine, "memoire", {}) or {}
            internes = self.montrer_variables_internes.get()
            entrees = [(n, v) for n, v in sorted(memoire.items())
                       if internes or not n.startswith("_")]
            if not entrees:
                self.inspecteur.insert(tk.END,
                    "Aucune variable en mémoire.\nExécutez un script.\n", "vide")
            for nom, valeur in entrees:
                if isinstance(valeur, list):
                    genre, etiquette = "tableau[%d]" % len(valeur), "tab"
                elif isinstance(valeur, bool) or str(valeur) in ("VRAI", "FAUX"):
                    genre, etiquette = "booléen", "bool"
                elif isinstance(valeur, (int, float)):
                    genre, etiquette = "nombre", "num"
                else:
                    genre, etiquette = "texte", "txt"
                texte = str(valeur)
                if len(texte) > 30:
                    texte = texte[:27] + "..."
                self.inspecteur.insert(tk.END, "%-16s " % nom[:16], "nom")
                self.inspecteur.insert(tk.END, "%-11s " % genre, "type")
                self.inspecteur.insert(tk.END, texte + "\n", etiquette)
            self.inspecteur.config(state=tk.DISABLED)
        except tk.TclError:
            pass
        self.rafraichir_surveillances()
        self.rafraichir_variables()

    def ajouter_surveillance(self):
        expression = self.champ_surveillance.get().strip()
        if not expression or expression in self.surveillances:
            return
        self.surveillances.append(expression)
        self.champ_surveillance.delete(0, tk.END)
        self.rafraichir_surveillances()

    def effacer_surveillances(self):
        self.surveillances = []
        self.rafraichir_surveillances()

    def rafraichir_surveillances(self):
        if not hasattr(self, "liste_surveillances"):
            return
        try:
            self.liste_surveillances.config(state=tk.NORMAL)
            self.liste_surveillances.delete("1.0", tk.END)
            if not self.surveillances:
                self.liste_surveillances.insert(tk.END,
                    "Ajoutez une expression UVI\n(ex. x * 2) pour la suivre.\n", "vide")
            for expression in self.surveillances:
                self.liste_surveillances.insert(tk.END, "%-18s " % expression[:18], "expr")
                try:
                    # evaluate() = l'évaluateur réel du langage. Une
                    # expression invalide lève ; on l'affiche comme erreur
                    # au lieu de laisser tomber la surveillance entière.
                    valeur = self.engine.evaluate(expression)
                    self.liste_surveillances.insert(tk.END, "%s\n" % str(valeur)[:22], "val")
                except Exception as e:
                    self.liste_surveillances.insert(
                        tk.END, "%s\n" % type(e).__name__, "err")
            self.liste_surveillances.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ==================================================================
    # 4. OUTILS
    # ==================================================================
    def _build_panneau_outils(self):
        """Donne un vrai domicile aux boutons orphelins de la v9.

        AST, BYTECODE, OPTIMISER, ANALYSER, STATS, AUTO-RÉPARER et VOIX
        existaient encore comme widgets jamais empaquetés -- attributs
        valides, aucun moyen de les déclencher. Ils sont ici, groupés par
        intention et avec une phrase disant à quoi ils servent."""
        panneau = self._nouveau_panneau("outils")
        interieur = self._cadre_defilant(panneau)

        groupes = (
            ("COMPRENDRE LE LANGAGE", T.VIOLET, (
                ("🌳", "Arbre syntaxique (AST)",
                 "Comment le parser a compris votre code",
                 lambda: self.agent.export_ast() if self.agent else None),
                ("⚙", "Bytecode compilé",
                 "Les instructions exécutées par la VM",
                 lambda: self.agent.export_bytecode() if self.agent else None),
                ("✨", "Optimiser le bytecode",
                 "Constantes pré-calculées, code mort retiré",
                 lambda: self.agent.optimize_code() if self.agent else None),
            )),
            ("ÉCRIRE AU QUOTIDIEN", T.CYAN, (
                ("🔍", "Analyser le script",
                 "Défauts probables sans exécuter",
                 lambda: self.agent.analyze_editor_code() if self.agent else None),
                ("🛠", "Auto-réparer",
                 "Corrections déterministes, hors ligne",
                 lambda: self.agent.auto_repair() if self.agent else None),
                ("🤖", "Auto-corriger (agent IA)",
                 "Boucle autonome — expérimental",
                 self.autonomous_fix_from_editor),
            )),
            ("SESSION", T.AMBRE, (
                ("📊", "Statistiques",
                 "Compteurs mémoire, appels, erreurs",
                 lambda: self.agent.report_stats() if self.agent else None),
                ("🔊", "Synthèse vocale",
                 "Activer ou couper la voix de l'agent",
                 self.toggle_voice),
                ("🧹", "Vider la console",
                 "Repartir d'une console propre",
                 self._clear_console),
            )),
        )
        for titre, couleur, outils in groupes:
            contenu = self._carte(interieur, titre, accent=couleur, menu=False)
            contenu.master.pack(fill=tk.X, pady=(0, 8))
            for icone, libelle, description, commande in outils:
                ligne = tk.Frame(contenu, bg=T.CHAMP, cursor="hand2",
                                 highlightthickness=1, highlightbackground=T.BORDURE)
                ligne.pack(fill=tk.X, padx=10, pady=3)
                haut = tk.Frame(ligne, bg=T.CHAMP)
                haut.pack(fill=tk.X, padx=9, pady=(7, 0))
                lbl_icone = tk.Label(haut, text=icone, bg=T.CHAMP, fg=couleur,
                                     font=("Segoe UI", 11))
                lbl_icone.pack(side=tk.LEFT)
                lbl_titre = tk.Label(haut, text=libelle, bg=T.CHAMP, fg=T.TEXTE,
                                     font=("Segoe UI", 8, "bold"))
                lbl_titre.pack(side=tk.LEFT, padx=(8, 0))
                lbl_desc = tk.Label(ligne, text=description, bg=T.CHAMP,
                                    fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                                    anchor=tk.W)
                lbl_desc.pack(fill=tk.X, padx=(31, 9), pady=(0, 7))
                for widget in (ligne, haut, lbl_icone, lbl_titre, lbl_desc):
                    widget.bind("<Button-1>", lambda e, c=commande: c())
                    widget.bind("<Enter>", lambda e, w=ligne: w.config(
                        highlightbackground=T.BORDURE_VIVE))
                    widget.bind("<Leave>", lambda e, w=ligne: w.config(
                        highlightbackground=T.BORDURE))

    # ==================================================================
    # 5. MODULES & PAQUETS
    # ==================================================================
    def _build_panneau_modules(self):
        """Modules chargés, bibliothèque standard, paquets publiés."""
        panneau = self._nouveau_panneau("modules")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "ÉTAT DES MODULES", menu=False)
        contenu.master.pack(fill=tk.X)
        self.arbre_modules = tk.Text(
            contenu, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=8, height=18)
        self.arbre_modules.pack(fill=tk.BOTH, expand=True)
        for etiquette, couleur in (("groupe", T.CYAN), ("membre", T.TEXTE),
                                   ("detail", T.TEXTE_FAIBLE), ("vide", T.TEXTE_FAIBLE)):
            self.arbre_modules.tag_configure(etiquette, foreground=couleur)

        actions = self._carte(interieur, "ACTIONS", menu=False)
        actions.master.pack(fill=tk.X, pady=(8, 8))
        for icone, libelle, commande in (
            ("🔄", "Actualiser", self.rafraichir_modules),
            ("📚", "Réinstaller la bibliothèque standard", self.reinstaller_stdlib),
            ("📤", "Publier le script comme paquet", self.publier_paquet),
        ):
            self._bouton_plat(actions, libelle, commande, couleur=T.TEXTE,
                              icone=icone, gras=False,
                              police=("Segoe UI", 8)).pack(fill=tk.X, padx=10, pady=3)
        tk.Label(actions, text="Un paquet publié devient importable par\n"
                               "IMPORT \"pkg:nom\" depuis n'importe quel script.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                 justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(4, 10))

    def rafraichir_modules(self):
        if not hasattr(self, "arbre_modules"):
            return
        try:
            self.arbre_modules.config(state=tk.NORMAL)
            self.arbre_modules.delete("1.0", tk.END)

            charges = sorted(getattr(self.engine, "modules", {}) or {})
            self.arbre_modules.insert(tk.END, "▾ CHARGÉS EN MÉMOIRE\n", "groupe")
            if not charges:
                self.arbre_modules.insert(
                    tk.END, "    aucun — utilisez MODULE \"f.uvi\" -> alias\n", "vide")
            for alias in charges:
                self.arbre_modules.insert(tk.END, "    %s\n" % alias, "membre")

            registre = getattr(self.vfs, "package_registry", None)
            self.arbre_modules.insert(tk.END, "\n▾ BIBLIOTHÈQUE STANDARD\n", "groupe")
            if registre is None:
                self.arbre_modules.insert(tk.END, "    registre non attaché\n", "vide")
            else:
                trouves = 0
                for nom in sorted(STDLIB_SOURCES):
                    try:
                        chemin = registre.resolve_std(nom)
                    except UVIRuntimeTrap:
                        chemin = None
                    if chemin:
                        trouves += 1
                        self.arbre_modules.insert(tk.END, "    %s\n" % nom, "membre")
                        self.arbre_modules.insert(
                            tk.END, '        MODULE "std:%s" -> %s\n' % (nom, nom), "detail")
                if not trouves:
                    self.arbre_modules.insert(
                        tk.END, "    non installée — voir Actions\n", "vide")

            self.arbre_modules.insert(tk.END, "\n▾ PAQUETS PUBLIÉS\n", "groupe")
            paquets = {}
            if registre is not None:
                try:
                    paquets = registre._load_registry() or {}
                except Exception:
                    paquets = {}
            if not paquets:
                self.arbre_modules.insert(tk.END, "    aucun paquet publié\n", "vide")
            for nom, versions in sorted(paquets.items()):
                self.arbre_modules.insert(tk.END, "    %s\n" % nom, "membre")
                for version in sorted(versions):
                    self.arbre_modules.insert(
                        tk.END, "        v%s\n" % version, "detail")

            self.arbre_modules.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def reinstaller_stdlib(self):
        registre = getattr(self.vfs, "package_registry", None)
        if registre is None:
            self.log("[PACKAGE_UNAVAILABLE] Registre de paquets non attaché.", "err")
            return
        try:
            registre.ensure_stdlib_installed(STDLIB_SOURCES)
        except Exception as e:
            self.log("Échec d'installation : %s" % e, "err")
            return
        self.rafraichir_modules()
        self._set_status("Bibliothèque standard réinstallée.")
        self.log("Bibliothèque standard réinstallée (%d module(s))."
                 % len(STDLIB_SOURCES), "ok")

    def publier_paquet(self):
        """Publie le contenu de l'éditeur comme paquet versionné.

        Passe par un fichier du VFS puis par registry.publish(), donc par
        _safe_resolve : un nom de paquet contenant « .. » est refusé ici
        exactement comme il l'est depuis PACKAGE.PUBLISH en script."""
        registre = getattr(self.vfs, "package_registry", None)
        if registre is None:
            self.log("[PACKAGE_UNAVAILABLE] Registre de paquets non attaché.", "err")
            return
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.log("Éditeur vide : rien à publier.", "err")
            return
        nom = self._demander_texte("Publier un paquet", "Nom du paquet :")
        if not nom:
            return
        version = self._demander_texte("Publier un paquet", "Version :", "1.0")
        if not version:
            return
        # Nom de fichier temporaire FIXE : y injecter `nom` faisait
        # remonter un FileNotFoundError illisible pour un nom hostile
        # (« ../evasion » donnait un répertoire « _publication_.. »
        # inexistant), au lieu du PATH_TRAVERSAL clair que publish() sait
        # produire. Le nom de paquet ne sert qu'à publish(), qui le confine.
        temporaire = "_publication_en_cours.uvi"
        try:
            self.vfs.write_file(temporaire, code)
            registre.publish(nom, self.vfs.get_path(temporaire), version)
            self.vfs.delete_file(temporaire)
        except UVIRuntimeTrap as trap:
            self.log("[%s] %s" % (trap.code, trap.message), "err")
            return
        except Exception as e:
            self.log("Échec de publication : %s" % e, "err")
            return
        self.rafraichir_modules()
        self.update_vfs_list()
        self.log('Paquet publié : %s v%s — importable par IMPORT "pkg:%s"'
                 % (nom, version, nom), "ok")

    # ==================================================================
    # 6. TESTS
    # ==================================================================
    def _build_panneau_tests(self):
        """Vérifications exécutables depuis l'IDE.

        Ce ne sont pas les tests unitaires de tests/ (ceux-là tournent sous
        pytest, hors interface). Ce sont les propriétés qu'on veut pouvoir
        vérifier SUR SON PROPRE SCRIPT, en un clic, sans quitter l'IDE."""
        panneau = self._nouveau_panneau("tests")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "VÉRIFICATIONS", menu=False)
        contenu.master.pack(fill=tk.X)
        for icone, libelle, description, commande in (
            ("⚖", "Comparer les deux moteurs",
             "Le tree-walker et la VM doivent être d'accord",
             self._lancer_tests),
            ("🛡", "Tester les garde-fous",
             "Boucle infinie, récursion, mémoire",
             self.tester_garde_fous),
            ("📐", "Vérifier la syntaxe",
             "Analyse sans exécuter",
             self.verifier_syntaxe),
        ):
            ligne = tk.Frame(contenu, bg=T.CHAMP, cursor="hand2",
                             highlightthickness=1, highlightbackground=T.BORDURE)
            ligne.pack(fill=tk.X, padx=10, pady=3)
            haut = tk.Frame(ligne, bg=T.CHAMP)
            haut.pack(fill=tk.X, padx=9, pady=(7, 0))
            a = tk.Label(haut, text=icone, bg=T.CHAMP, fg=T.CYAN, font=("Segoe UI", 11))
            a.pack(side=tk.LEFT)
            b = tk.Label(haut, text=libelle, bg=T.CHAMP, fg=T.TEXTE,
                         font=("Segoe UI", 8, "bold"))
            b.pack(side=tk.LEFT, padx=(8, 0))
            c = tk.Label(ligne, text=description, bg=T.CHAMP, fg=T.TEXTE_FAIBLE,
                         font=("Segoe UI", 7), anchor=tk.W)
            c.pack(fill=tk.X, padx=(31, 9), pady=(0, 7))
            for widget in (ligne, haut, a, b, c):
                widget.bind("<Button-1>", lambda e, cmd=commande: cmd())

        contenu_res = self._carte(interieur, "RÉSULTATS", menu=False)
        contenu_res.master.pack(fill=tk.X, pady=(8, 8))
        self.resultats_tests = tk.Text(
            contenu_res, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.WORD, padx=10, pady=8, height=16)
        self.resultats_tests.pack(fill=tk.BOTH, expand=True)
        self.resultats_tests.tag_configure("ok", foreground=T.VERT)
        self.resultats_tests.tag_configure("ko", foreground=T.ROUGE)
        self.resultats_tests.tag_configure("titre", foreground=T.CYAN)
        self.resultats_tests.tag_configure("neutre", foreground=T.TEXTE_FAIBLE)

    def _ecrire_resultat_test(self, texte, etiquette="neutre", vider=False):
        if not hasattr(self, "resultats_tests"):
            return
        try:
            self.resultats_tests.config(state=tk.NORMAL)
            if vider:
                self.resultats_tests.delete("1.0", tk.END)
            self.resultats_tests.insert(tk.END, texte + "\n", etiquette)
            self.resultats_tests.config(state=tk.DISABLED)
            self.resultats_tests.see(tk.END)
        except tk.TclError:
            pass

    def tester_garde_fous(self):
        """Déclenche chaque garde-fou sur un script dégénéré et vérifie que
        l'erreur ATTENDUE sort -- et surtout que la VM reste utilisable
        après. Un garde-fou qui protège en laissant un état corrompu ne
        protège pas."""
        self.afficher_panneau_gauche("tests")
        self._ecrire_resultat_test("GARDE-FOUS", "titre", vider=True)
        cas = (
            ("Boucle infinie (RUNAWAY)",
             ["i = 0", "%% i < 100000 -> i = i + 1"], {"max_steps": 500}, "RUNAWAY"),
            ("Récursion infinie (MAX_RECURSION)",
             ["DEF F(n) -> CALL F(n)", "CALL F(1)"],
             {"max_call_depth": 10, "max_steps": 50000}, "MAX_RECURSION"),
            ("Tableau démesuré (ARRAY_FULL)",
             ["TAB.CREATE t", "% 5000 -> TAB.PUSH t -> 1"],
             {"max_array_size": 50}, "ARRAY_FULL"),
        )
        reussis = 0
        for libelle, script, options, attendu in cas:
            try:
                resultat = compile_and_run(list(script), self.engine.parser,
                                           UVIBytecodeCompilerV2(), **options)
                obtenu = resultat.trapped
            except Exception as e:
                obtenu = "exception %s" % type(e).__name__
            if obtenu == attendu:
                self._ecrire_resultat_test("  ✓ %s" % libelle, "ok")
                reussis += 1
            else:
                self._ecrire_resultat_test(
                    "  ✗ %s — attendu %s, obtenu %s" % (libelle, attendu, obtenu), "ko")

        try:
            sain = compile_and_run(['!"vivant"'], self.engine.parser, UVIBytecodeCompilerV2())
            survit = sain.trapped is None
        except Exception:
            survit = False
        if survit:
            self._ecrire_resultat_test("  ✓ La VM reste utilisable après", "ok")
            reussis += 1
        else:
            self._ecrire_resultat_test("  ✗ La VM est inutilisable après", "ko")
        self._ecrire_resultat_test("\n%d/%d vérifications passées." % (reussis, len(cas) + 1),
                                   "ok" if reussis == len(cas) + 1 else "ko")

    def verifier_syntaxe(self):
        """Analyse le script sans l'exécuter : signale les lignes que le
        parser ne comprend pas (UnknownNode)."""
        self.afficher_panneau_gauche("tests")
        self._ecrire_resultat_test("VÉRIFICATION DE SYNTAXE", "titre", vider=True)
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self._ecrire_resultat_test("  Éditeur vide.", "neutre")
            return
        problemes = 0
        for numero, ligne in enumerate(code.split("\n"), start=1):
            depouillee = ligne.strip()
            if not depouillee or depouillee.startswith("#") or depouillee == "@UVI":
                continue
            try:
                noeud = self.engine.parser.parse_line(depouillee, numero)
            except Exception as e:
                self._ecrire_resultat_test(
                    "  ✗ L%-4d %s" % (numero, type(e).__name__), "ko")
                problemes += 1
                continue
            if isinstance(noeud, UnknownNode):
                self._ecrire_resultat_test(
                    "  ✗ L%-4d non comprise : %s" % (numero, depouillee[:30]), "ko")
                problemes += 1
        if problemes:
            self._ecrire_resultat_test(
                "\n%d ligne(s) problématique(s)." % problemes, "ko")
        else:
            self._ecrire_resultat_test("  ✓ Toutes les lignes sont comprises.", "ok")

    # ==================================================================
    # 7. PARAMÈTRES
    # ==================================================================
    def _build_panneau_parametres(self):
        """Paramètres MODIFIABLES, pas seulement affichés.

        Le panneau v9 se contentait de déverser les valeurs dans la console.
        Ici, changer l'adresse du serveur ou le nom du modèle et cliquer
        « Appliquer » RECONSTRUIT réellement le client SHION -- utile au
        premier branchement, où l'on tâtonne sur le nom exact du modèle
        servi par vLLM (l'erreur 404 la plus courante). Sans ça, chaque
        essai demandait de fermer l'IDE et de relancer avec une autre
        variable d'environnement."""
        panneau = self._nouveau_panneau("parametres")
        interieur = self._cadre_defilant(panneau)

        contenu = self._carte(interieur, "SERVEUR SHION", menu=False)
        contenu.master.pack(fill=tk.X)
        self.champs_parametres = {}
        for cle, libelle, valeur, aide in (
            ("url", "Adresse du serveur", SHION_SERVER_URL, "ex. http://192.168.1.50:8000"),
            ("modele", "Nom du modèle", DEEPSEEK_MODEL_NAME, "doit correspondre à /v1/models"),
            ("cle", "Clé d'API", DEEPSEEK_API_KEY, "ignorée si le serveur n'en exige pas"),
            ("temperature", "Température", str(DEEPSEEK_TEMPERATURE), "0.5 à 0.7 — jamais 0"),
            ("max_tokens", "Budget de tokens", str(DEEPSEEK_MAX_TOKENS), "≥ 4096 conseillé"),
        ):
            self._tr(tk.Label(contenu, bg=T.PANNEAU, fg=T.TEXTE,
                              font=("Segoe UI", 8, "bold")), libelle).pack(
                                  anchor=tk.W, padx=12, pady=(8, 2))
            champ = tk.Entry(contenu, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
                             relief=tk.FLAT, font=("Consolas", 9),
                             highlightthickness=1, highlightbackground=T.BORDURE,
                             highlightcolor=T.CYAN,
                             # Masquée comme un champ de mot de passe : la
                             # clé d'API reste lisible par quiconque a accès
                             # au fichier de config, mais n'a aucune raison
                             # de rester affichée EN CLAIR à l'écran (partage
                             # d'écran, capture, épaule au-dessus) une fois
                             # déjà saisie une première fois.
                             show=("•" if cle == "cle" else None))
            champ.pack(fill=tk.X, padx=12, ipady=4)
            champ.insert(0, str(valeur))
            self.champs_parametres[cle] = champ
            tk.Label(contenu, text=aide, bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
                     font=("Segoe UI", 7)).pack(anchor=tk.W, padx=12, pady=(2, 0))

        barre = tk.Frame(contenu, bg=T.PANNEAU)
        barre.pack(fill=tk.X, padx=12, pady=12)
        self._bouton_plat(barre, "Appliquer", self.appliquer_parametres,
                          couleur=T.VERT, icone="✓").pack(side=tk.LEFT)
        self._bouton_plat(barre, "Tester", self.tester_connexion_shion,
                          couleur=T.CYAN, icone="🔌").pack(side=tk.LEFT, padx=6)

        # --- Langue de l'interface --------------------------------------
        # Un bouton par langue (même schéma que les préréglages 1,5B/7B du
        # modèle local plus bas -- choix mutuellement exclusif, la langue
        # active mise en évidence) plutôt qu'un menu déroulant Tk
        # (OptionMenu/Combobox) : ni l'un ni l'autre ne s'habille bien du
        # thème plat sombre déjà utilisé partout ailleurs dans ce panneau,
        # alors qu'une rangée de _bouton_plat s'y fond nativement.
        contenu_langue = self._carte(interieur, "LANGUE", menu=False)
        contenu_langue.master.pack(fill=tk.X, pady=(8, 0))
        barre_langue = tk.Frame(contenu_langue, bg=T.PANNEAU)
        barre_langue.pack(fill=tk.X, padx=12, pady=12)
        self._boutons_langue = {}
        for code, nom in LANGUES.items():
            bouton = self._bouton_plat(
                barre_langue, nom, lambda c=code: self._changer_langue_parametres(c),
                couleur=T.CYAN, gras=False)
            bouton.pack(side=tk.LEFT, padx=(0, 6), pady=2)
            self._boutons_langue[code] = bouton
        self._rafraichir_boutons_langue()

        # --- Modèle local llama.cpp, géré DEPUIS l'IDE (voir
        # _definir_modele_local) : démarre server/serve_llamacpp.py comme
        # sous-processus, attend qu'il soit prêt, puis remplit les champs
        # ci-dessus et clique "Appliquer" à la place de l'utilisateur --
        # voir docs/SERVEUR_LOCAL_CPU.md pour l'équivalent en ligne de
        # commande et les mesures de vitesse par préréglage.
        contenu_local = self._carte(interieur, "MODÈLE LOCAL (llama.cpp, CPU)", menu=False)
        contenu_local.master.pack(fill=tk.X, pady=(8, 0))
        tk.Label(contenu_local, text=
                 "Lance server/serve_llamacpp.py depuis l'IDE et reconnecte "
                 "SHION dessus automatiquement. Un seul modèle chargé à la "
                 "fois : changer de taille arrête l'autre proprement.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                 wraplength=280, justify=tk.LEFT).pack(
                     anchor=tk.W, padx=12, pady=(8, 6))
        barre_local = tk.Frame(contenu_local, bg=T.PANNEAU)
        barre_local.pack(fill=tk.X, padx=12)
        # Étiquettes des boutons seulement -- "Shion-Flash"/"Shion" plutôt
        # que "1,5B"/"7B" (les tailles DeepSeek d'origine), à la demande.
        # Le préréglage interne (clé "1.5b"/"7b" passée à
        # _definir_modele_local, alias exposé sur /v1/models, champ "Nom du
        # modèle" et table ÉTAT DU SYSTÈME plus bas) reste identique --
        # seul ce que l'utilisateur LIT sur ces deux boutons change, pas la
        # mécanique ni les identifiants techniques réels.
        self._bouton_plat(barre_local, "Shion-Flash", lambda: self._definir_modele_local("1.5b"),
                          couleur=T.CYAN, icone="⚡", gras=False).pack(side=tk.LEFT)
        self._bouton_plat(barre_local, "Shion", lambda: self._definir_modele_local("7b"),
                          couleur=T.VIOLET, icone="🐢", gras=False).pack(side=tk.LEFT, padx=6)
        self._bouton_plat(barre_local, "Arrêter", self._arreter_modele_local,
                          couleur=T.ROUGE, icone="■", gras=False).pack(side=tk.LEFT)
        self.label_statut_modele_local = tk.Label(
            contenu_local, text="Arrêté.", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Consolas", 8))
        self.label_statut_modele_local.pack(anchor=tk.W, padx=12, pady=(8, 10))

        # Rapports de bugs (voir bug_reports.py) : carte volontairement
        # visible depuis l'IDE, pas juste un mécanisme silencieux en
        # arrière-plan -- la garantie "jamais votre code" (voir la
        # docstring du module) est plus crédible si l'utilisateur peut
        # lui-même ouvrir le dossier et vérifier qu'il n'y a que des
        # faits structurels (type d'erreur, ligne, traceback interne),
        # jamais son script.
        contenu_bugs = self._carte(interieur, "RAPPORTS DE BUGS", menu=False)
        contenu_bugs.master.pack(fill=tk.X, pady=(8, 0))
        tk.Label(contenu_bugs, text=
                 "Un plantage inattendu enregistre localement un rapport "
                 "SANS AUCUN CODE (type d'erreur, ligne, traceback interne "
                 "uniquement) -- jamais transmis nulle part, voir "
                 "bug_reports.py. Désactivable via UVI_BUG_REPORTS=0.",
                 bg=T.PANNEAU, fg=T.TEXTE_FAIBLE, font=("Segoe UI", 7),
                 wraplength=280, justify=tk.LEFT).pack(
                     anchor=tk.W, padx=12, pady=(8, 6))
        self._bouton_plat(contenu_bugs, "Ouvrir le dossier", self._ouvrir_dossier_rapports_bugs,
                          couleur=T.CYAN, icone="📂", gras=False).pack(
                              anchor=tk.W, padx=12, pady=(0, 6))
        self.label_statut_rapports_bugs = tk.Label(
            contenu_bugs, text="", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Consolas", 8))
        self.label_statut_rapports_bugs.pack(anchor=tk.W, padx=12, pady=(0, 10))

        contenu_etat = self._carte(interieur, "ÉTAT DU SYSTÈME", menu=False)
        contenu_etat.master.pack(fill=tk.X, pady=(8, 8))
        self.table_parametres = tk.Text(
            contenu_etat, bg=T.PANNEAU, fg=T.TEXTE, font=("Consolas", 9),
            relief=tk.FLAT, highlightthickness=0, state=tk.DISABLED,
            wrap=tk.NONE, padx=10, pady=8, height=12)
        self.table_parametres.pack(fill=tk.BOTH, expand=True)
        self.table_parametres.tag_configure("cle", foreground=T.TEXTE_FAIBLE)
        self.table_parametres.tag_configure("oui", foreground=T.VERT)
        self.table_parametres.tag_configure("non", foreground=T.ROUGE)
        self.table_parametres.tag_configure("val", foreground=T.TEXTE)

    def _ouvrir_dossier_rapports_bugs(self):
        """Ouvre bug_reports/ dans l'explorateur -- crée le dossier s'il
        n'existe pas encore (aucun plantage capturé jusqu'ici) plutôt que
        d'échouer silencieusement sur un chemin absent, pour que le bouton
        fasse toujours quelque chose de visible."""
        if not HAS_BUG_REPORTS:
            self._empiler_toast("Rapports de bugs indisponibles (bug_reports.py manquant).", "err")
            return
        try:
            os.makedirs(bug_reports.reports_dir(), exist_ok=True)
            os.startfile(bug_reports.reports_dir())
        except Exception as e:
            self._empiler_toast("Impossible d'ouvrir le dossier : %s" % type(e).__name__, "err")

    def rafraichir_parametres(self):
        if hasattr(self, "label_statut_rapports_bugs"):
            try:
                n = bug_reports.report_count() if HAS_BUG_REPORTS else 0
                self.label_statut_rapports_bugs.config(
                    text="%d rapport(s) enregistré(s) localement." % n)
            except tk.TclError:
                pass
        if not hasattr(self, "table_parametres"):
            return
        try:
            self.table_parametres.config(state=tk.NORMAL)
            self.table_parametres.delete("1.0", tk.END)
            client = self.engine.shion_client
            lignes = (
                ("Backend SHION", SHION_BACKEND, "val"),
                ("Client actif", type(client).__name__ if client else "aucun",
                 "oui" if client else "non"),
                ("SHION", "disponible" if (HAS_SHION and client) else "indisponible",
                 "oui" if (HAS_SHION and client) else "non"),
                ("VM bytecode", "disponible" if HAS_VM else "indisponible",
                 "oui" if HAS_VM else "non"),
                ("Stockage", getattr(self.vfs, "storage_dir", "mémoire"), "val"),
                ("Mémoire max", "%d octets" % MAX_MEMORY_BYTES, "val"),
                ("Chaîne max", "%d caractères" % MAX_STRING_LENGTH, "val"),
                ("Rapports de bugs", "disponible" if HAS_BUG_REPORTS else "indisponible",
                 "oui" if HAS_BUG_REPORTS else "non"),
            )
            for cle, valeur, etiquette in lignes:
                self.table_parametres.insert(tk.END, "%-16s " % cle, "cle")
                self.table_parametres.insert(tk.END, "%s\n" % valeur, etiquette)
            self.table_parametres.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def appliquer_parametres(self):
        """Reconstruit le client SHION avec les valeurs saisies.

        On remplace l'INSTANCE, pas les constantes du module : les
        constantes sont lues au chargement par d'autres chemins, et les
        réécrire donnerait un état incohérent entre ce que le client fait et
        ce que les messages d'erreur annoncent."""
        champs = self.champs_parametres
        try:
            temperature = float(champs["temperature"].get().strip())
            max_tokens = int(champs["max_tokens"].get().strip())
        except ValueError:
            self.log("Température et budget doivent être numériques.", "err")
            return
        if temperature < 0.4:
            # Refus assumé : c'est le mode de défaillance documenté n°1 des
            # distillations R1. Laisser passer 0.0 produirait des réponses
            # en boucle que l'utilisateur attribuerait au serveur.
            self.log("Température < 0.4 refusée : DeepSeek-R1 part en "
                     "répétitions sans fin. Utilisez 0.5 à 0.7.", "err")
            return
        try:
            client = DeepSeekClient(
                base_url=champs["url"].get().strip(),
                model_name=champs["modele"].get().strip(),
                api_key=champs["cle"].get().strip() or "uvi-local",
                # BUGFIX : température et budget de tokens étaient calculés
                # et VALIDÉS ci-dessus, puis jamais transmis au client
                # reconstruit (DeepSeekClient() ignorait ces deux champs à
                # la construction) -- cliquer "Appliquer" changeait donc
                # silencieusement l'URL/le modèle/la clé mais laissait
                # tourner l'ancienne température et l'ancien budget, sans
                # le moindre message pour le signaler. Voir aussi
                # DeepSeekClient.__init__, qui accepte désormais ces deux
                # valeurs par instance au lieu de lire uniquement les
                # constantes de module.
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            self.log("Client non construit : %s" % e, "err")
            return
        self.engine.shion_client = client
        self.rafraichir_parametres()
        self._set_status("Paramètres appliqués.")
        self.log("Client SHION reconstruit → %s (modèle %s, température %.2f, budget %d)"
                 % (client.base_url, client.model_name, client.temperature, client.max_tokens), "ok")

    def tester_connexion_shion(self):
        """Interroge /v1/models du serveur configuré, dans un thread daemon.

        Rend le NOM EXACT servi : c'est précisément l'information dont on a
        besoin pour corriger un HTTP 404, et la faire remonter ici évite
        d'aller la chercher au curl sur une autre machine."""
        url = self.champs_parametres["url"].get().strip().rstrip("/")
        cle = self.champs_parametres["cle"].get().strip() or "uvi-local"
        self.show_terminal()
        self.log("Test de connexion vers %s ..." % url, "sys")

        def _worker():
            try:
                requete = urllib.request.Request("%s/v1/models" % url)
                requete.add_header("Authorization", "Bearer %s" % cle)
                with urllib.request.urlopen(requete, timeout=15) as reponse:
                    corps = json.loads(reponse.read().decode("utf-8"))
                noms = [m.get("id") for m in corps.get("data", [])]
                message, niveau = ("Serveur joignable. Modèle(s) servi(s) : %s"
                                   % (", ".join(n for n in noms if n) or "aucun"), "ok")
            except urllib.error.HTTPError as e:
                message, niveau = ("Serveur joignable mais refuse (HTTP %d). "
                                   "Vérifiez la clé d'API." % e.code, "err")
            except Exception as e:
                message, niveau = ("Serveur injoignable : %s" % e, "err")
            self._poster_ui(lambda: self.log(message, niveau))

        threading.Thread(target=_worker, daemon=True).start()

    def _set_statut_modele_local(self, texte: str) -> None:
        if hasattr(self, "label_statut_modele_local"):
            self.label_statut_modele_local.config(text=texte)

    #: Noms affichés pour les préréglages du modèle local -- voir la note
    #: sur les étiquettes de bouton dans _build_panneau_parametres. Ne
    #: change QUE ce qui est écrit dans les messages utilisateur ci-dessous
    #: (log/statut) ; la clé "1.5b"/"7b" elle-même, l'alias technique
    #: (deepseek-r1-distill-qwen-*) et le champ "Nom du modèle" restent
    #: inchangés.
    _NOMS_AFFICHES_PRESET = {"1.5b": "Shion-Flash", "7b": "Shion"}

    def _definir_modele_local(self, preset: str) -> None:
        """Démarre server/serve_llamacpp.py (préréglage \"1.5b\" ou \"7b\")
        comme sous-processus de l'IDE, attend qu'il soit prêt, puis
        reconnecte SHION dessus -- voir docs/SERVEUR_LOCAL_CPU.md pour les
        mesures de vitesse par préréglage sur cette machine (les deux sont
        lents ici faute d'AVX2, mais le principe vaut pour toute machine).

        Aucun redémarrage si le préréglage demandé tourne déjà (évite de
        perdre ~10-20s de rechargement pour rien en cliquant deux fois sur
        le même bouton)."""
        nom_affiche = self._NOMS_AFFICHES_PRESET.get(preset, preset)
        if (self._preset_llm_local_actif == preset
                and self._processus_llm_local is not None
                and self._processus_llm_local.poll() is None):
            self.log("%s local tourne déjà." % nom_affiche, "sys")
            return

        racine = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(racine, "server", "serve_llamacpp.py")
        alias_attendu = {
            "1.5b": "deepseek-r1-distill-qwen-1.5b",
            "7b": "deepseek-r1-distill-qwen-7b",
        }.get(preset)
        if alias_attendu is None:
            return
        if not os.path.isfile(script):
            self.log("server/serve_llamacpp.py introuvable.", "err")
            return

        self._arreter_modele_local(silencieux=True)
        self._set_statut_modele_local("Démarrage de %s..." % nom_affiche)
        self.log("Lancement du serveur local llama.cpp (%s)..." % nom_affiche, "sys")
        try:
            self._processus_llm_local = subprocess.Popen(
                [sys.executable, script, preset], cwd=racine,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.log("Impossible de lancer le serveur local : %s" % e, "err")
            self._set_statut_modele_local("Erreur au lancement.")
            return
        self._preset_llm_local_actif = preset

        # Même clé que celle affichée dans le champ "Clé d'API" : lue ICI,
        # sur le thread Tk, pas dans le thread de sondage ci-dessous (accès
        # widget hors thread principal à éviter -- même règle que
        # tester_connexion_shion juste au-dessus).
        cle = self.champs_parametres["cle"].get().strip() or "uvi-local"

        def _attendre_pret():
            url = "http://127.0.0.1:8000/v1/models"
            debut = time.time()
            while time.time() - debut < 120:
                if (self._processus_llm_local is None
                        or self._processus_llm_local.poll() is not None):
                    def _sur_arret_premature():
                        self._set_statut_modele_local("Le serveur s'est arrêté prématurément.")
                        self.log(
                            "Le serveur local (%s) s'est arrêté avant d'être "
                            "prêt -- modèle GGUF manquant dans models/ ? Voir "
                            "docs/SERVEUR_LOCAL_CPU.md §1." % nom_affiche, "err")
                    self._poster_ui(_sur_arret_premature)
                    return
                try:
                    requete = urllib.request.Request(url)
                    requete.add_header("Authorization", "Bearer %s" % cle)
                    with urllib.request.urlopen(requete, timeout=3) as reponse:
                        corps = json.loads(reponse.read().decode("utf-8"))
                    noms = [m.get("id") for m in corps.get("data", [])]
                    if alias_attendu in noms:
                        self._poster_ui(
                            lambda: self._finaliser_bascule_modele_local(alias_attendu))
                        return
                except Exception:
                    pass
                time.sleep(2)

            def _sur_timeout():
                self._set_statut_modele_local("Chargement > 2 min, pas encore prêt.")
                self.log(
                    "Le serveur local (%s) met plus de 2 min à charger -- pas "
                    "forcément une panne (chargement du GGUF en RAM), "
                    "réessayez « Tester » dans un instant." % nom_affiche, "err")
            self._poster_ui(_sur_timeout)

        threading.Thread(target=_attendre_pret, daemon=True).start()

    def _finaliser_bascule_modele_local(self, alias: str) -> None:
        self.champs_parametres["url"].delete(0, tk.END)
        self.champs_parametres["url"].insert(0, "http://127.0.0.1:8000")
        self.champs_parametres["modele"].delete(0, tk.END)
        self.champs_parametres["modele"].insert(0, alias)
        self.appliquer_parametres()
        self._set_statut_modele_local("Prêt : %s" % alias)
        self.log("Serveur local prêt et connecté (%s)." % alias, "ok")

    def _arreter_modele_local(self, silencieux: bool = False) -> None:
        """Arrête proprement le sous-processus lancé par _definir_modele_local
        (le cas échéant). N'affecte PAS un serveur lancé à la main dans un
        terminal externe -- l'IDE ne le connaît pas, donc ne le touche pas."""
        proc = self._processus_llm_local
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if not silencieux:
                self.log("Serveur local arrêté.", "sys")
        self._processus_llm_local = None
        if not silencieux:
            self._preset_llm_local_actif = None
            self._set_statut_modele_local("Arrêté.")

    def rafraichir_variables(self):
        """Table des variables en direct. Appelée après chaque exécution.

        Type deviné pour la couleur uniquement : UVI ne typant pas ses
        variables, c'est un indice visuel, pas une vérité du langage --
        d'où le fait qu'aucune logique d'exécution ne s'appuie dessus."""
        if not hasattr(self, "table_variables"):
            return
        try:
            self.table_variables.config(state=tk.NORMAL)
            self.table_variables.delete("1.0", tk.END)
            memoire = getattr(self.engine, "memoire", {}) or {}
            visibles = [(n, v) for n, v in sorted(memoire.items())
                        if not n.startswith("_")]
            if not visibles:
                self.table_variables.insert(tk.END,
                    "Aucune variable.\nExécutez un script pour les voir ici.\n", "vide")
            for nom, valeur in visibles[:60]:
                texte = str(valeur)
                if len(texte) > 26:
                    texte = texte[:23] + "..."
                if isinstance(valeur, list):
                    etiquette = "tab"
                elif isinstance(valeur, bool) or texte in ("VRAI", "FAUX"):
                    etiquette = "bool"
                elif isinstance(valeur, (int, float)):
                    etiquette = "num"
                else:
                    etiquette = "txt"
                    texte = '"%s"' % texte
                self.table_variables.insert(tk.END, "%-14s " % nom[:14], "nom")
                self.table_variables.insert(tk.END, texte + "\n", etiquette)
            self.table_variables.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Barre d'onglets de l'éditeur et barre d'état
    # ------------------------------------------------------------------
    def _build_barre_onglets(self, parent):
        barre = tk.Frame(parent, bg=T.PANNEAU_HAUT, height=38)
        barre.pack(fill=tk.X)
        barre.pack_propagate(False)
        self.onglet_fichier = tk.Frame(barre, bg=T.PANNEAU,
                                       highlightthickness=1,
                                       highlightbackground=T.BORDURE)
        self.onglet_fichier.pack(side=tk.LEFT, padx=(8, 0), pady=4)
        tk.Label(self.onglet_fichier, text="U", bg=T.PANNEAU, fg=T.CYAN,
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(9, 5))
        self.etiquette_fichier = tk.Label(
            self.onglet_fichier, text="main.uvi", bg=T.PANNEAU,
            fg=T.TEXTE_FORT, font=("Segoe UI", 9))
        self.etiquette_fichier.pack(side=tk.LEFT, pady=6)
        tk.Label(self.onglet_fichier, text="✕", bg=T.PANNEAU,
                 fg=T.TEXTE_FAIBLE, font=("Segoe UI", 8),
                 cursor="hand2").pack(side=tk.LEFT, padx=(9, 9))

        # Les vues secondaires (canevas) deviennent des onglets à côté du
        # fichier : en v8 elles étaient des boutons perdus au milieu de la
        # barre d'outils supérieure, sans rien indiquer qu'il s'agissait de
        # VUES et non d'actions.
        #
        # L'onglet SHION (troisième vue, à côté de CANEVAS) a été RETIRÉ
        # ici : il partageait les noms self.shion_input/self.shion_historique/
        # btn_shion_send avec _build_colonne_shion (la colonne de droite,
        # construite APRÈS lui dans _build_container), qui les écrasait
        # systématiquement -- cet onglet était donc mort depuis sa
        # création, ses propres champs de saisie et historique n'ayant
        # jamais reçu la moindre interaction réelle. Les conversations
        # SHION passent uniquement par _build_colonne_shion, la colonne de
        # droite -- c'est elle qui reste, avec les mêmes boutons
        # EXPLIQUER/OPTIMISER/DÉBOGUER/GÉNÉRER. _build_shion_tab et
        # show_shion ont été supprimées avec ce commentaire, pas juste
        # débranchées, pour ne pas laisser du code mort qu'une prochaine
        # session croirait à tort fonctionnel.
        for cle, libelle, commande in (
            ("editor", "◧ ÉDITEUR", self.show_editor),
            ("canvas", "▦ CANEVAS", self.show_canvas),
        ):
            bouton = tk.Button(
                barre, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                activebackground=T.SURVOL, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 8), cursor="hand2",
                command=commande, padx=11, pady=5,
            )
            self._tr(bouton, libelle)
            bouton.pack(side=tk.LEFT, padx=2, pady=4)
            setattr(self, "btn_%s" % {"editor": "edit", "canvas": "canvas"}[cle], bouton)
        # btn_term référencé ailleurs : conservé, redirigé vers la console
        # du dock (qui n'est plus une vue mais reste focalisable).
        self.btn_term = tk.Button(barre, text="TERMINAL", command=self.show_terminal)

    def _build_barre_etat_editeur(self, parent):
        barre = tk.Frame(parent, bg=T.PANNEAU_HAUT, height=26)
        barre.pack(fill=tk.X, side=tk.BOTTOM)
        barre.pack_propagate(False)
        self.etat_curseur = tk.Label(barre, text="L1  COL 1", bg=T.PANNEAU_HAUT,
                                     fg=T.TEXTE_FAIBLE, font=("Consolas", 8))
        self.etat_curseur.pack(side=tk.LEFT, padx=14)
        for texte in ("ESPACES: 4", "UTF-8", "LF", "UVI"):
            tk.Label(barre, text=texte, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                     font=("Consolas", 8)).pack(side=tk.LEFT, padx=11)
        tk.Label(barre, text="UVi", bg=T.PANNEAU_HAUT, fg=T.BORDURE_VIVE,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.RIGHT, padx=14)
        # Bascule du dock console (voir _basculer_dock_console) : posée ICI,
        # dans la barre d'état de l'ÉDITEUR (toujours visible, en dehors du
        # dock lui-même) et non dans le dock -- un bouton qui vivrait DANS
        # le dock disparaîtrait avec lui une fois replié, sans aucun moyen
        # de le rouvrir.
        self.btn_basculer_dock = tk.Button(
            barre, text="▾ CONSOLE", bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
            activebackground=T.SURVOL, activeforeground=T.CYAN,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 8, "bold"), cursor="hand2",
            command=self._basculer_dock_console, padx=10)
        self.btn_basculer_dock.pack(side=tk.RIGHT, padx=(0, 4))

    def _basculer_dock_console(self):
        """Replie/déplie le dock console (console + mémoire VM) EN BLOC,
        pour rendre à l'éditeur/canevas ses ~290px de hauteur à la
        demande.

        pack_forget() PUIS pack() sur dock seul ne suffirait pas : Tkinter
        réinsère un widget ré-empaqueté à la FIN de l'ordre de packing
        courant, pas à sa position d'origine -- si cadre_editeur (déjà
        pack(expand=True)) restait premier dans cet ordre, il capturerait
        toute la cavité et écraserait le dock tout juste rouvert (même
        piège que celui déjà documenté dans _build_container pour l'ordre
        initial dock-avant-cadre_editeur). Retirer puis ré-empaqueter LES
        DEUX widgets, dans le même ordre qu'au démarrage, reproduit
        exactement l'agencement d'origine à chaque bascule."""
        if self._dock_ouvert:
            self.dock.pack_forget()
            self._dock_ouvert = False
        else:
            self.cadre_editeur.pack_forget()
            self.dock.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
            self.cadre_editeur.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self._dock_ouvert = True
        self.btn_basculer_dock.config(
            text="▾ CONSOLE" if self._dock_ouvert else "▴ CONSOLE")

    def _assurer_dock_ouvert(self):
        """Rouvre le dock s'il a été replié -- appelée partout où quelque
        chose qui VIT dans le dock doit redevenir visible (nouvelle sortie
        de script via show_terminal, invite ?INPUT via _ouvrir_saisie).
        Sans ça, replier le dock pour gagner de la place reproduirait
        exactement le bug déjà corrigé une fois ailleurs dans ce fichier :
        un ?INPUT qui semble figé parce que la barre qui y répond est
        invisible."""
        if not self._dock_ouvert:
            self._basculer_dock_console()

    def _rafraichir_position_curseur(self, _event=None):
        try:
            ligne, colonne = self.editor.index(tk.INSERT).split(".")
            self.etat_curseur.config(text="L%s  COL %d" % (ligne, int(colonne) + 1))
        except (tk.TclError, AttributeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Panneau mémoire VM
    # ------------------------------------------------------------------
    def _build_panneau_memoire(self, parent):
        contenu = self._carte(parent, "MÉMOIRE VM", accent=T.CYAN)
        contenu.master.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        contenu.master.configure(width=352)
        contenu.master.pack_propagate(False)

        corps = tk.Frame(contenu, bg=T.PANNEAU)
        corps.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.anneau_memoire = tk.Canvas(corps, width=132, height=132,
                                        bg=T.PANNEAU, highlightthickness=0)
        self.anneau_memoire.pack(side=tk.LEFT, padx=(0, 12))

        self.legende_memoire = tk.Frame(corps, bg=T.PANNEAU)
        self.legende_memoire.pack(side=tk.LEFT, fill=tk.Y, pady=8)

        self.courbe_memoire = tk.Canvas(contenu, height=42, bg=T.PANNEAU,
                                        highlightthickness=0)
        self.courbe_memoire.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.etiquette_memoire = tk.Label(contenu, text="0% UTILISÉ", bg=T.PANNEAU,
                                          fg=T.CYAN, font=("Segoe UI", 8, "bold"))
        self.etiquette_memoire.pack(anchor=tk.W, padx=14, pady=(0, 8))
        self._historique_memoire = []
        self.rafraichir_memoire()

    def rafraichir_memoire(self):
        """Anneau + histogramme de la mémoire du moteur.

        create_arc en style ARC avec un trait épais : Tkinter n'a pas de
        graphique en anneau, mais un arc épais SANS remplissage en est
        exactement un. Inutile d'ajouter matplotlib (dépendance lourde) pour
        dessiner quatre arcs -- cohérent avec le refus de torch côté client
        et de tkinter-tooltip pour les infobulles.

        Les quatre catégories sont des ESTIMATIONS dérivées de
        current_memory_usage, pas une instrumentation de l'allocateur : le
        moteur ne comptabilise que le total. Elles donnent un ordre de
        grandeur utile, pas une mesure exacte -- dit ici pour que personne
        ne construise dessus en croyant à une métrique fine."""
        if not hasattr(self, "anneau_memoire"):
            return
        try:
            self.anneau_memoire.delete("all")
            self.legende_memoire.pack_forget()
            self.legende_memoire = tk.Frame(self.legende_memoire.master, bg=T.PANNEAU)
            self.legende_memoire.pack(side=tk.LEFT, fill=tk.Y, pady=8)

            utilise = float(getattr(self.engine, "current_memory_usage", 0) or 0)
            total = float(MAX_MEMORY_BYTES)
            fraction = max(0.0, min(1.0, utilise / total if total else 0.0))

            memoire = getattr(self.engine, "memoire", {}) or {}
            poids_tab = sum(len(str(v)) for v in memoire.values() if isinstance(v, list))
            poids_var = max(0.0, utilise - poids_tab)
            poids_code = len(self.editor.get("1.0", tk.END)) if hasattr(self, "editor") else 0

            segments = [
                ("Variables", poids_var, T.BLEU_VIF),
                ("Structures", float(poids_tab), T.CYAN),
                ("Code", float(poids_code), T.VIOLET),
                ("Libre", max(0.0, total - utilise - poids_code), T.BORDURE),
            ]
            somme = sum(v for _, v, _ in segments) or 1.0

            # Plancher de 3° pour tout segment NON NUL. Sans lui, un script
            # ordinaire (quelques dizaines d'octets sur 500 Ko de quota)
            # produit un anneau d'UNE SEULE couleur : « Libre » écrase tout
            # et les trois autres catégories tombent sous le dixième de
            # degré, donc invisibles. L'utilisateur voit alors un anneau
            # uni et en conclut que le panneau est cassé.
            #
            # DISTORSION ASSUMÉE, et c'est le genre de choix qu'il faut
            # écrire plutôt que subir : aux très faibles taux d'occupation,
            # l'anneau n'est PAS à lire comme une proportion. Il dit « ces
            # catégories existent et voici leur ordre », pas « voici leurs
            # parts exactes ». Les valeurs exactes sont dans la légende à
            # côté, en kilo-octets, sans aucune distorsion -- c'est là qu'il
            # faut regarder pour un chiffre.
            debut = 90.0
            visibles = [(v, c) for _, v, c in segments if v > 0]
            plancher = 3.0
            reste = 360.0 - plancher * len(visibles)
            for valeur, couleur in visibles:
                etendue = -(plancher + reste * (valeur / somme))
                self.anneau_memoire.create_arc(
                    12, 12, 120, 120, start=debut, extent=etendue,
                    style=tk.ARC, outline=couleur, width=15)
                debut += etendue
            self.anneau_memoire.create_text(
                66, 60, text="%.1f Ko" % (utilise / 1024.0),
                fill=T.TEXTE_FORT, font=("Segoe UI", 13, "bold"))
            self.anneau_memoire.create_text(
                66, 80, text="sur %d Ko" % int(total / 1024),
                fill=T.TEXTE_FAIBLE, font=("Segoe UI", 7))

            for libelle, valeur, couleur in segments:
                ligne = tk.Frame(self.legende_memoire, bg=T.PANNEAU)
                ligne.pack(fill=tk.X, pady=3)
                pastille = tk.Canvas(ligne, width=9, height=9, bg=T.PANNEAU,
                                     highlightthickness=0)
                pastille.pack(side=tk.LEFT)
                pastille.create_rectangle(0, 0, 9, 9, fill=couleur, outline="")
                tk.Label(ligne, text=libelle, bg=T.PANNEAU, fg=T.TEXTE,
                         font=("Segoe UI", 8), width=11,
                         anchor=tk.W).pack(side=tk.LEFT, padx=(7, 0))
                tk.Label(ligne, text="%.1f Ko" % (valeur / 1024.0), bg=T.PANNEAU,
                         fg=T.TEXTE_FAIBLE, font=("Consolas", 8)).pack(side=tk.RIGHT)

            self._historique_memoire.append(fraction)
            self._historique_memoire = self._historique_memoire[-70:]
            self.courbe_memoire.delete("all")
            largeur = max(self.courbe_memoire.winfo_width(), 260)
            points = self._historique_memoire
            if len(points) > 1:
                pas = largeur / float(len(points) - 1)
                coords = []
                for i, valeur in enumerate(points):
                    coords += [i * pas, 40 - valeur * 34]
                self.courbe_memoire.create_line(*coords, fill=T.CYAN,
                                                width=1, smooth=True)
            self.etiquette_memoire.config(text="%d%% UTILISÉ" % int(fraction * 100))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Colonne SHION
    # ------------------------------------------------------------------
    def _build_colonne_shion(self):
        colonne = tk.Frame(self.container, bg=T.FOND, width=372)
        colonne.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6), pady=6)
        colonne.pack_propagate(False)

        # --- panneau de discussion -----------------------------------------
        cadre = tk.Frame(colonne, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(fill=tk.BOTH, expand=True)
        # En-tête agrandie (56 -> 72px) : c'est ici que vit l'avatar SHION
        # (voir ShionAvatar plus haut dans ce fichier). L'ancien emblème
        # (anneau + point fixes, 30x30) était trop petit pour que des
        # yeux/une bouche restent lisibles -- seule la TAILLE change,
        # l'emplacement et le rôle ("logo SHION en tête du panneau de
        # discussion") restent ceux d'origine (§21 de la demande : ne pas
        # détruire l'identité visuelle existante).
        entete = tk.Frame(cadre, bg=T.PANNEAU_HAUT, height=72)
        entete.pack(fill=tk.X)
        entete.pack_propagate(False)
        emblème = tk.Canvas(entete, width=52, height=52, bg=T.PANNEAU_HAUT,
                            highlightthickness=0)
        emblème.pack(side=tk.LEFT, padx=(12, 8), pady=10)
        # ShionAvatar dessine et anime CE canvas lui-même à chaque tick
        # (voir ShionAvatarRenderer) -- plus besoin des deux create_oval
        # statiques d'origine, l'état IDLE de l'avatar produit un rendu
        # équivalent (anneau + "yeux" au repos) mais vivant.
        self.shion_avatar = ShionAvatar(emblème, config=ShionAvatarConfig(debug=False))
        self.shion_avatar.demarrer()
        # Clic droit sur l'avatar : mode debug (§17 -- panneau d'état +
        # boutons de test par émotion), sans ajouter de chrome UI visible
        # en permanence pour une fonctionnalité de développement.
        emblème.bind("<Button-3>", lambda e: self.shion_avatar.ouvrir_debug(self.root))
        colonne_titre = tk.Frame(entete, bg=T.PANNEAU_HAUT)
        colonne_titre.pack(side=tk.LEFT, pady=11)
        tk.Label(colonne_titre, text="SHION", bg=T.PANNEAU_HAUT, fg=T.CYAN,
                 font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        connecte = HAS_SHION and self.engine.shion_client is not None
        tk.Label(colonne_titre,
                 text="VOTRE ASSISTANT IA INTÉGRÉ" if connecte
                      else "NON CONFIGURÉ — VOIR PARAMÈTRES",
                 bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                 font=("Segoe UI", 6)).pack(anchor=tk.W)

        self.shion_historique = tk.Text(
            cadre, bg=T.PANNEAU, fg=T.TEXTE, font=("Segoe UI", 9),
            relief=tk.FLAT, wrap=tk.WORD, state=tk.DISABLED,
            highlightthickness=0, padx=12, pady=10,
        )
        self.shion_historique.pack(fill=tk.BOTH, expand=True)
        self.shion_historique.tag_configure("vous", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("assistant", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("eris", foreground=T.CYAN,
                                            font=("Segoe UI", 9, "bold"))
        self.shion_historique.tag_configure("corps", foreground=T.TEXTE)
        self.shion_historique.tag_configure("erreur", foreground=T.ROUGE)

        saisie = tk.Frame(cadre, bg=T.PANNEAU)
        saisie.pack(fill=tk.X, padx=12, pady=10)
        self.shion_input = tk.Entry(
            saisie, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            relief=tk.FLAT, font=("Segoe UI", 9),
            highlightthickness=1, highlightbackground=T.BORDURE,
            highlightcolor=T.CYAN,
        )
        self.shion_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7)
        self.shion_input.insert(0, "Pose ta question à SHION...")
        self.shion_input.bind("<Return>", lambda e: self._shion_send_message())
        self.shion_input.bind("<FocusIn>", self._vider_invite_shion)
        self.btn_shion_send = tk.Button(
            saisie, text="➤", bg=T.BLEU, fg="#FFFFFF",
            activebackground=T.BLEU_VIF, activeforeground="#FFFFFF",
            disabledforeground=T.TEXTE_FAIBLE,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 11, "bold"), cursor="hand2",
            command=self._shion_send_message, padx=13,
        )
        self.btn_shion_send.pack(side=tk.RIGHT, padx=(8, 0), ipady=4)
        self.frame_shion_panneau = cadre

        # --- quatre actions rapides ----------------------------------------
        # Elles ne font qu'assembler un prompt autour du code de l'éditeur.
        # C'est modeste, et c'est justement ce qui manquait : SHION savait
        # déjà répondre, mais il fallait rédiger la question à la main à
        # chaque fois pour lui parler de son propre code.
        grille = tk.Frame(colonne, bg=T.FOND)
        grille.pack(fill=tk.X, pady=(8, 0))
        actions = (
            ("🧭", "EXPLIQUER", "Du code", T.CYAN, "expliquer"),
            ("✨", "OPTIMISER", "Le code", T.AMBRE, "optimiser"),
            ("🐞", "DÉBOGGER", "Le code", T.ROSE, "deboguer"),
            ("🧬", "GÉNÉRER", "Du code", T.VERT, "generer"),
        )
        for i, (icone, titre, sous_titre, couleur, cle) in enumerate(actions):
            grille.columnconfigure(i, weight=1)
            carte = tk.Frame(grille, bg=T.PANNEAU, highlightthickness=1,
                             highlightbackground=T.BORDURE, cursor="hand2")
            carte.grid(row=0, column=i, sticky="nsew", padx=3)
            lbl_icone = tk.Label(carte, text=icone, bg=T.PANNEAU, fg=couleur,
                                 font=("Segoe UI", 15))
            lbl_icone.pack(pady=(10, 2))
            lbl_titre = tk.Label(carte, text=titre, bg=T.PANNEAU, fg=T.TEXTE,
                                 font=("Segoe UI", 7, "bold"))
            lbl_titre.pack()
            lbl_sous = tk.Label(carte, text=sous_titre, bg=T.PANNEAU,
                                fg=T.TEXTE_FAIBLE, font=("Segoe UI", 6))
            lbl_sous.pack(pady=(0, 10))
            for widget in (carte, lbl_icone, lbl_titre, lbl_sous):
                widget.bind("<Button-1>", lambda e, c=cle: self.action_shion(c))
                widget.bind("<Enter>", lambda e, w=carte: w.config(
                    highlightbackground=T.BORDURE_VIVE))
                widget.bind("<Leave>", lambda e, w=carte: w.config(
                    highlightbackground=T.BORDURE))

        # --- outils --------------------------------------------------------
        # Requalifiée : cette grille faisait doublon avec le panneau OUTILS
        # du rail depuis que celui-ci existe. Deux jeux d'actions parallèles
        # auraient divergé -- on en ajoute une ici, on oublie l'autre. Elle
        # devient donc un ACCÈS RAPIDE aux panneaux du rail : un seul endroit
        # définit ce que font les outils, deux endroits permettent d'y aller.
        contenu_outils = self._carte(colonne, "ACCÈS RAPIDE", hauteur=240)
        contenu_outils.master.pack(fill=tk.X, pady=(8, 0))
        self._entete_outils = contenu_outils.master.winfo_children()[0].winfo_children()[0]
        outils = (
            ("📁", "FICHIERS", lambda: self.afficher_panneau_gauche("explorateur")),
            ("🔍", "RECHERCHE", lambda: self.afficher_panneau_gauche("recherche")),
            ("🗄", "VARIABLES", lambda: self.afficher_panneau_gauche("variables")),
            ("🛠", "OUTILS", lambda: self.afficher_panneau_gauche("outils")),
            ("📦", "MODULES", lambda: self.afficher_panneau_gauche("modules")),
            ("🧪", "TESTS", lambda: self.afficher_panneau_gauche("tests")),
            ("🎯", "POINTS D'ARRÊT", lambda: self._selectionner_onglet_console("DÉBOGUEUR")),
            ("⚙", "PARAMÈTRES", lambda: self.afficher_panneau_gauche("parametres")),
        )
        grille_outils = tk.Frame(contenu_outils, bg=T.PANNEAU)
        grille_outils.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for i, (icone, libelle, commande) in enumerate(outils):
            ligne, colonne_i = divmod(i, 4)
            grille_outils.columnconfigure(colonne_i, weight=1)
            grille_outils.rowconfigure(ligne, weight=1)
            carte = tk.Frame(grille_outils, bg=T.CHAMP, highlightthickness=1,
                             highlightbackground=T.BORDURE, cursor="hand2")
            carte.grid(row=ligne, column=colonne_i, sticky="nsew", padx=3, pady=3)
            lbl_icone = tk.Label(carte, text=icone, bg=T.CHAMP, fg=T.CYAN,
                                 font=("Segoe UI", 14))
            lbl_icone.pack(pady=(11, 3))
            lbl_texte = tk.Label(carte, text=libelle, bg=T.CHAMP, fg=T.TEXTE_FAIBLE,
                                 font=("Segoe UI", 6, "bold"))
            lbl_texte.pack(pady=(0, 11))
            for widget in (carte, lbl_icone, lbl_texte):
                widget.bind("<Button-1>", lambda e, c=commande: c())
                widget.bind("<Enter>", lambda e, w=carte, t=lbl_texte:
                            (w.config(highlightbackground=T.CYAN),
                             t.config(fg=T.TEXTE)))
                widget.bind("<Leave>", lambda e, w=carte, t=lbl_texte:
                            (w.config(highlightbackground=T.BORDURE),
                             t.config(fg=T.TEXTE_FAIBLE)))

    def _vider_invite_shion(self, _event=None):
        if self.shion_input.get() == "Pose ta question à SHION...":
            self.shion_input.delete(0, tk.END)

    def _lister_points_arret(self):
        self.show_terminal()
        points = sorted(getattr(self, "_points_arret", set()))
        self.log("=== POINTS D'ARRÊT ===", "sys")
        if not points:
            self.log("Aucun. Placez le curseur sur une ligne et cliquez 🐞 DÉBOGUEUR.",
                     "info")
        for ligne in points:
            self.log("  ligne %d" % ligne, "info")

    def action_shion(self, action):
        """Quatre raccourcis qui envoient le code de l'éditeur à SHION.

        La consigne « réponds en français » n'est pas décorative : les
        distillations R1 de DeepSeek répondent volontiers en anglais ou en
        chinois quand la question ne fixe pas la langue, y compris quand le
        code commenté est en français.

        expliquer/optimiser/deboguer envoient désormais bien plus que le
        code brut : _construire_contexte_langage_uvi y ajoute la référence
        du langage UVI ET l'AST réel du code (produit par le VRAI parseur,
        pas deviné par SHION) -- voir sa docstring pour le pourquoi. Avant
        ce changement, SHION ne recevait que le texte source et devait
        deviner la syntaxe par analogie avec un langage connu, ce qui
        produisait des explications plausibles mais parfois fausses sur
        les points où UVI diverge. L'historique affiché reste COURT (la
        consigne seule) grâce aux paramètres question/prompt_envoye de
        _chat_send -- l'utilisateur ne voit pas défiler l'AST complet à
        chaque clic, seulement la question posée."""
        code = self.editor.get("1.0", tk.END).strip()
        if action != "generer" and not code:
            self.show_terminal()
            self.log("Éditeur vide : écrivez d'abord un script.", "err")
            return
        consignes = {
            "expliquer": "Explique ce code UVI en détail, en français, étape par étape.",
            "optimiser": "Propose une version optimisée de ce code UVI, en "
                         "français, en expliquant chaque changement.",
            "deboguer": "Trouve les bugs de ce code UVI et propose une correction, en français.",
        }
        # show_shion() basculait vers l'onglet SHION du workspace central --
        # supprimé (voir la note dans _build_barre_onglets) car mort : la
        # VRAIE conversation se déroule dans la colonne de droite
        # (_build_colonne_shion), déjà en permanence à l'écran, donc rien à
        # « montrer » explicitement ici.
        if action == "generer":
            texte_affiche = "Génère un script UVI complet et commenté, en français."
            prompt = (
                UVI_REFERENCE_LANGAGE + "\n\n" + texte_affiche
                + " Respecte STRICTEMENT la syntaxe ci-dessus : n'invente "
                  "aucun mot-clé ou symbole qui n'y figure pas."
            )
        else:
            texte_affiche = consignes[action]
            prompt = self._construire_contexte_langage_uvi() + "\n\nQuestion : " + texte_affiche
        self._chat_send("shion", self.engine.shion_client, "SHION",
                        question=texte_affiche, prompt_envoye=prompt)

    def _build_editor(self):
        self.frame_editor = tk.Frame(self.workspace, bg=T.CHAMP)
        # self.line_numbers et self.editor gardent leurs noms : 8 et 53
        # références respectivement ailleurs dans le fichier
        # (_update_line_numbers, _highlight_error_line, la coloration
        # syntaxique, l'agent, run_script...). Seule leur peau change.
        self.line_numbers = tk.Text(
            self.frame_editor, width=5,
            bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=T.MONO_EDIT, relief=tk.FLAT,
            state=tk.DISABLED, cursor="arrow",
            highlightthickness=0, padx=6,
        )
        self.line_numbers.pack(side=tk.LEFT, fill=tk.Y)
        # Le clic sur un numéro de ligne pose un point d'arrêt. Le moteur de
        # points d'arrêt existait depuis longtemps mais n'avait AUCUN moyen
        # de déclenchement dans l'interface -- même situation
        # qu'autonomous_fix. La gouttière est l'endroit où tout le monde
        # cherche déjà à cliquer.
        self.line_numbers.bind("<Button-1>", self._clic_gouttiere)

        conteneur = tk.Frame(self.frame_editor, bg=T.CHAMP)
        conteneur.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.editor_vsb = tk.Scrollbar(conteneur, orient="vertical",
                                       bg=T.PANNEAU, troughcolor=T.CHAMP,
                                       relief=tk.FLAT, bd=0, width=10)
        self.editor_hsb = tk.Scrollbar(conteneur, orient="horizontal",
                                       bg=T.PANNEAU, troughcolor=T.CHAMP,
                                       relief=tk.FLAT, bd=0, width=10)
        self.editor = tk.Text(
            conteneur,
            bg=T.CHAMP, fg=T.TEXTE,
            insertbackground=T.CYAN, insertwidth=2,
            font=T.MONO_EDIT,
            relief=tk.FLAT, undo=True, tabs="4c",
            selectbackground="#1E3A5F", selectforeground=T.TEXTE_FORT,
            wrap=tk.NONE, highlightthickness=0,
            padx=12, pady=8, spacing1=2,
            xscrollcommand=self.editor_hsb.set,
        )

        def sync_y_scroll(*args):
            self.editor_vsb.set(*args)
            self.line_numbers.yview_moveto(args[0])

        def sync_y_view(*args):
            self.editor.yview(*args)
            self.line_numbers.yview(*args)

        self.editor.config(yscrollcommand=sync_y_scroll)
        self.editor_vsb.config(command=sync_y_view)
        self.editor_hsb.config(command=self.editor.xview)
        self.editor_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.editor_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.editor.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.editor.bind("<KeyRelease>", self._on_key_release_handler)
        self.editor.bind("<Control-space>", self._insert_suggestion)
        self.editor.bind("<Tab>", self._insert_suggestion)
        self.editor.bind("<KeyRelease>", self._rafraichir_position_curseur, add="+")
        self.editor.bind("<ButtonRelease-1>", self._rafraichir_position_curseur, add="+")
        self.editor.bind("<<Modified>>", self._sur_modification, add="+")
        self._define_tags()

    def _sur_modification(self, _event=None):
        """Marque le tampon comme modifié et met le point sur l'onglet.

        edit_modified(False) est INDISPENSABLE : sans cette remise à zéro,
        Tk n'émet <<Modified>> qu'UNE seule fois, à la première frappe, et
        l'indicateur ne bougerait plus jamais ensuite. Piège classique de
        ce virtual event, qui donne un indicateur « qui marche » en test
        rapide et faux en usage réel."""
        try:
            if not self.editor.edit_modified():
                return
            self.editor.edit_modified(False)
        except tk.TclError:
            return
        if not self.modifie:
            self.modifie = True
            self._rafraichir_titre_onglet()

    def _rafraichir_titre_onglet(self):
        if not hasattr(self, "etiquette_fichier"):
            return
        nom = self.fichier_courant or "sans_titre.uvi"
        try:
            self.etiquette_fichier.config(
                text=("● " + nom) if self.modifie else nom,
                fg=T.AMBRE if self.modifie else T.TEXTE_FORT)
        except tk.TclError:
            pass

    def _marquer_enregistre(self, nom=None):
        if nom:
            self.fichier_courant = nom
        self.modifie = False
        self._rafraichir_titre_onglet()

    def _clic_gouttiere(self, event):
        try:
            index = self.line_numbers.index("@%d,%d" % (event.x, event.y))
            ligne = int(index.split(".")[0])
        except (tk.TclError, ValueError):
            return "break"
        points = getattr(self, "_points_arret", None)
        if points is None:
            points = self._points_arret = set()
        points.discard(ligne) if ligne in points else points.add(ligne)
        self._update_line_numbers()
        return "break"

    # ------------------------------------------------------------------
    # Console : dock permanent à onglets
    # ------------------------------------------------------------------
    def _build_terminal(self, parent=None):
        """Console du dock inférieur.

        parent optionnel et non self.workspace : la console n'est plus une
        vue concurrente de l'éditeur mais un dock permanent (voir
        _build_container). Le paramètre garde toutefois une valeur par
        défaut pour que tout appel historique _build_terminal() continue de
        fonctionner.

        self.console, self.cmd_input et self.frame_terminal gardent leurs
        noms : 23, 18 et 3 références ailleurs."""
        parent = parent or self.workspace
        cadre = tk.Frame(parent, bg=T.PANNEAU, highlightthickness=1,
                         highlightbackground=T.BORDURE)
        cadre.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.frame_terminal = cadre

        barre = tk.Frame(cadre, bg=T.PANNEAU_HAUT, height=34)
        barre.pack(fill=tk.X)
        barre.pack_propagate(False)
        self._onglets_console = {}
        # Les onglets FILTRENT une même console plutôt que d'être autant de
        # widgets distincts : la v8 mélangeait sortie de script, messages
        # système et erreurs dans un seul flux, si bien qu'une erreur pouvait
        # défiler hors de vue sous la sortie d'une boucle. Un filtre coûte
        # une relecture du tampon ; sept widgets auraient coûté sept chemins
        # d'écriture à tenir synchronisés dans log().

        # Boutons de fin, empaquetés AVANT la zone défilante : sous Tkinter,
        # un widget packé side=RIGHT après un side=LEFT expand=True se fait
        # écraser par ce dernier. L'ordre d'empaquetage est ici significatif
        # et non cosmétique.
        tk.Button(
            barre, text="🗑", bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
            activebackground=T.PANNEAU, activeforeground=T.ROUGE,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 9), cursor="hand2",
            command=self._clear_console, padx=10,
        ).pack(side=tk.RIGHT, padx=(4, 8))
        self.btn_onglet_suivant = tk.Button(
            barre, text="›", bg=T.PANNEAU_HAUT, fg=T.TEXTE,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 12, "bold"), cursor="hand2",
            command=lambda: self._defiler_onglets(1), padx=7)
        self.btn_onglet_suivant.pack(side=tk.RIGHT)
        self.btn_onglet_precedent = tk.Button(
            barre, text="‹", bg=T.PANNEAU_HAUT, fg=T.TEXTE,
            activebackground=T.PANNEAU, activeforeground=T.CYAN,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 12, "bold"), cursor="hand2",
            command=lambda: self._defiler_onglets(-1), padx=7)
        self.btn_onglet_precedent.pack(side=tk.RIGHT)

        # Zone défilante horizontale. Même contrainte que pour la colonne
        # gauche : Tkinter ne fait pas défiler un Frame, il faut un Canvas
        # porteur. Sept onglets ne tiennent pas dans un dock de 900 px sur
        # un écran de portable -- sans défilement, DÉBOGUEUR et AGENT
        # seraient inatteignables.
        self.toile_onglets = tk.Canvas(barre, bg=T.PANNEAU_HAUT,
                                       highlightthickness=0, bd=0, height=30)
        self.toile_onglets.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                padx=(6, 0), pady=2)
        self.bande_onglets = tk.Frame(self.toile_onglets, bg=T.PANNEAU_HAUT)
        self._fenetre_onglets = self.toile_onglets.create_window(
            (0, 0), window=self.bande_onglets, anchor="nw")

        for nom in self._ONGLETS_CONSOLE:
            # nom reste l'identifiant INTERNE partout ailleurs (filtres de
            # _selectionner_onglet_console, self._onglet_console_actif,
            # etc.) -- seul le TEXTE AFFICHÉ sur le bouton doit changer de
            # langue, via _tr (option="text"), jamais nom lui-même.
            bouton = tk.Button(
                self.bande_onglets, bg=T.PANNEAU_HAUT, fg=T.TEXTE_FAIBLE,
                activebackground=T.PANNEAU, activeforeground=T.CYAN,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                font=("Segoe UI", 8, "bold"), cursor="hand2",
                padx=12, pady=6,
                command=lambda n=nom: self._selectionner_onglet_console(n),
            )
            self._tr(bouton, nom)
            bouton.pack(side=tk.LEFT, padx=1)
            self._onglets_console[nom] = bouton

        def _zone_onglets(_e=None):
            try:
                self.toile_onglets.configure(
                    scrollregion=self.toile_onglets.bbox("all"))
            except tk.TclError:
                pass

        self.bande_onglets.bind("<Configure>", _zone_onglets)

        def _molette_onglets(event):
            # La molette fait défiler HORIZONTALEMENT ici : c'est le seul
            # axe qui a un sens pour une bande d'onglets, et l'utilisateur
            # n'a pas à savoir qu'il devrait tenir Maj.
            pas = 1 if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0 else -1
            try:
                self.toile_onglets.xview_scroll(pas * 2, "units")
            except tk.TclError:
                pass

        for widget in (self.toile_onglets, self.bande_onglets):
            widget.bind("<MouseWheel>", _molette_onglets)
            widget.bind("<Button-4>", _molette_onglets)
            widget.bind("<Button-5>", _molette_onglets)

        self._onglet_console_actif = "CONSOLE UVI"

        # --- ligne de commande REPL --------------------------------------
        # BUGFIX (barre de saisie ?var invisible, second niveau) : ce cadre
        # (et, plus bas, barre_saisie qui répond à ?var) DOIT être empaqueté
        # AVANT `zone` (la console). `zone` est extensible (expand=True) et
        # capture tout le reste de la cavité dès qu'elle est empaquetée --
        # exactement le même mécanisme, à l'intérieur de ce dock, que celui
        # corrigé dans _build_container entre cadre_editeur et le dock
        # lui-même (voir son commentaire). Sans cet ordre, barre_cmd (et
        # barre_saisie) se retrouvent poussées hors des ~290 px du dock dès
        # que la fenêtre est un peu courte -- invisibles ET hors d'atteinte
        # du clavier, ce qui a exactement le même symptôme qu'un dock
        # entièrement absent : un script qui attend une réponse à ?var
        # semble figé sans qu'on sache pourquoi. side=BOTTOM préserve sa
        # position visuelle réelle (tout en bas) malgré cet ordre.
        barre_cmd = tk.Frame(cadre, bg=T.PANNEAU)
        barre_cmd.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        # Référence explicite plutôt que self.cmd_input.master : le jour où
        # la ligne de commande gagne un conteneur intermédiaire, .master ne
        # désigne plus le frère attendu et pack(before=...) lève.
        self.barre_commande = barre_cmd
        self.prompt_commande = tk.Label(
            barre_cmd, text="UVI ❯", fg=T.CYAN, bg=T.PANNEAU,
            font=("Consolas", 10, "bold"))
        self.prompt_commande.pack(side=tk.LEFT, padx=(0, 8))
        self.cmd_input = tk.Entry(
            barre_cmd, bg=T.CHAMP, fg=T.TEXTE, insertbackground=T.CYAN,
            font=("Consolas", 10), relief=tk.FLAT,
            highlightthickness=1, highlightbackground=T.BORDURE,
            highlightcolor=T.CYAN,
        )
        self.cmd_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.cmd_input.bind("<Return>", self.run_command)
        self.cmd_input.bind("<Up>", self._history_up)
        self.cmd_input.bind("<Down>", self._history_down)

        self.message_onglet_vide = tk.Label(
            cadre, text="", bg=T.PANNEAU, fg=T.TEXTE_FAIBLE,
            font=("Segoe UI", 8, "italic"), anchor=tk.W, padx=12, pady=6)
        zone = tk.Frame(cadre, bg=T.CHAMP)
        zone.pack(fill=tk.BOTH, expand=True, padx=1)
        self.zone_console = zone
        self.console_vsb = tk.Scrollbar(zone, orient="vertical", bg=T.PANNEAU,
                                        troughcolor=T.CHAMP, relief=tk.FLAT,
                                        bd=0, width=10)
        self.console_hsb = tk.Scrollbar(zone, orient="horizontal", bg=T.PANNEAU,
                                        troughcolor=T.CHAMP, relief=tk.FLAT,
                                        bd=0, width=10)
        self.console = tk.Text(
            zone, bg=T.CHAMP, fg=T.TEXTE,
            font=("Consolas", 10), relief=tk.FLAT,
            state=tk.NORMAL, selectbackground="#1E3A5F",
            wrap=tk.NONE, highlightthickness=0, padx=12, pady=8,
            yscrollcommand=self.console_vsb.set,
            xscrollcommand=self.console_hsb.set,
        )
        self.console_vsb.config(command=self.console.yview)
        self.console_hsb.config(command=self.console.xview)
        self.console_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Double-clic sur une erreur -> saut à la ligne fautive. Les
        # messages contiennent déjà « ligne N » (format garanti par
        # UVIError.__str__) : l'information était là, mais il fallait la
        # lire puis faire défiler l'éditeur à la main pour s'en servir.
        self.console.bind("<Double-Button-1>", self._sauter_a_lerreur)

        # --- barre de SAISIE DE SCRIPT (commande ?) ----------------------
        # Cadre distinct de la barre de commande : ce sont deux choses
        # différentes qu'il ne faut surtout pas confondre. La barre de
        # commande est un REPL (on tape des instructions UVI hors
        # exécution) ; celle-ci répond à une question POSÉE PAR LE SCRIPT
        # EN COURS. Les mélanger, c'était demander à l'utilisateur de
        # deviner dans quel mode se trouve un même champ.
        #
        # Bandeau ambre plein volontairement : c'est le seul moment où
        # l'IDE ATTEND quelque chose de l'utilisateur, et un script bloqué
        # sans qu'on comprenne pourquoi est la pire expérience possible.
        # Il doit être impossible de le rater.
        self.barre_saisie = tk.Frame(cadre, bg=T.AMBRE)
        self.etiquette_saisie = tk.Label(
            self.barre_saisie, text="?", bg=T.AMBRE, fg="#1A1000",
            font=("Consolas", 10, "bold"))
        self.etiquette_saisie.pack(side=tk.LEFT, padx=(12, 8), pady=9)
        self.champ_saisie = tk.Entry(
            self.barre_saisie, bg="#1A1000", fg=T.AMBRE,
            insertbackground=T.AMBRE, relief=tk.FLAT,
            font=("Consolas", 11), highlightthickness=0)
        self.champ_saisie.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.champ_saisie.bind("<Return>", lambda e: self.valider_saisie())
        tk.Button(
            self.barre_saisie, text="VALIDER  ⏎", bg="#1A1000", fg=T.AMBRE,
            activebackground=T.AMBRE, activeforeground="#1A1000",
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=("Segoe UI", 8, "bold"), cursor="hand2",
            command=self.valider_saisie, padx=12,
        ).pack(side=tk.RIGHT, padx=10, pady=6)
        # Non empaquetée au démarrage : elle n'apparaît que sur demande du
        # script, et disparaît dès la réponse donnée. (barre_cmd/cmd_input
        # sont construits PLUS HAUT, avant `zone` -- voir le commentaire à
        # cet endroit.)
        self._saisie_evenement = None
        self._saisie_resultat = {"valeur": ""}

        self._selectionner_onglet_console("CONSOLE UVI")

    #: Onglets du dock inférieur, dans l'ordre d'affichage.
    #: TERMINAL, DÉBOGUEUR et AGENT sont NOUVEAUX : la v9 mélangeait
    #: l'historique des commandes, l'état des points d'arrêt et les étapes
    #: de l'agent au milieu de la sortie du script. Trois choses qu'on
    #: consulte pour des raisons différentes, à des moments différents.
    _ONGLETS_CONSOLE = ("CONSOLE UVI", "SORTIE", "ERREURS", "TESTS",
                        "TERMINAL", "DÉBOGUEUR", "AGENT")

    def _defiler_onglets(self, sens):
        """Flèches ‹ › de la bande d'onglets. Existent en plus de la molette
        parce qu'un pavé tactile sans défilement horizontal est courant, et
        qu'un onglet inatteignable est un onglet qui n'existe pas."""
        try:
            self.toile_onglets.xview_scroll(sens * 3, "units")
        except (tk.TclError, AttributeError):
            pass

    def _sauter_a_lerreur(self, _event=None):
        """Amène le curseur de l'éditeur à la ligne citée par la ligne de
        console double-cliquée."""
        try:
            numero_console = int(self.console.index(tk.CURRENT).split(".")[0])
            texte = self.console.get("%d.0" % numero_console,
                                     "%d.end" % numero_console)
        except (tk.TclError, ValueError):
            return
        correspondance = re.search(r"ligne\s+(\d+)", texte)
        if not correspondance:
            return
        ligne = int(correspondance.group(1))
        self.show_editor()
        try:
            self.editor.mark_set(tk.INSERT, "%d.0" % ligne)
            self.editor.see("%d.0" % ligne)
            self.editor.tag_remove("ligne_courante", "1.0", tk.END)
            self.editor.tag_add("ligne_courante", "%d.0" % ligne, "%d.end" % ligne)
        except tk.TclError:
            return
        self._rafraichir_position_curseur()
        self._set_status("Ligne %d" % ligne)

    def _selectionner_onglet_console(self, nom):
        """Filtre l'affichage de la console.

        Relecture du tampon plutôt que quatre widgets : voir la
        justification dans _build_terminal. Conséquence assumée -- le filtre
        travaille sur le TEXTE des lignes, donc une ligne de sortie de
        script qui commencerait par « [ERREUR] » serait classée comme
        erreur. Cas rare et sans gravité, mais réel."""
        self._onglet_console_actif = nom
        for cle, bouton in getattr(self, "_onglets_console", {}).items():
            actif = (cle == nom)
            bouton.config(fg=T.CYAN if actif else T.TEXTE_FAIBLE,
                          bg=T.PANNEAU if actif else T.PANNEAU_HAUT)
        self._appliquer_filtre_console()

    def _appliquer_filtre_console(self):
        if not hasattr(self, "console"):
            return
        actif = getattr(self, "_onglet_console_actif", "CONSOLE UVI")
        if actif == "CONSOLE UVI":
            self.console.tag_configure("masque", elide=False)
            return

        # Chaque onglet est un prédicat « faut-il montrer cette ligne ? ».
        # Écrire les filtres comme des fonctions plutôt qu'en cascade de if
        # rend l'ajout d'un huitième onglet mécanique.
        def est_erreur(ligne):
            return ligne.lstrip().startswith("[ERREUR]")

        filtres = {
            # « demande une valeur » et « → » sont conservés : c'est
            # l'invite de saisie et la réponse. Les masquer ici est
            # précisément ce qui rendait la commande ? inutilisable --
            # l'utilisateur voyait un script figé sans invite visible.
            "SORTIE": lambda l: (
                "demande une valeur" in l or l.lstrip().startswith("→")
                or (not est_erreur(l) and not l.lstrip().startswith(
                    ("[SYS]", "[OK]", "[AGENT", "UVI>")))),
            "ERREURS": est_erreur,
            "TESTS": lambda l: ("moteur" in l.lower() or "vérification" in l.lower()
                                or l.lstrip().startswith(("✓", "✗", "[SYS] ==="))),
            "TERMINAL": lambda l: l.lstrip().startswith("UVI>"),
            "DÉBOGUEUR": lambda l: ("point d'arrêt" in l.lower()
                                    or "ligne" in l.lower() and "arrêt" in l.lower()),
            "AGENT": lambda l: l.lstrip().startswith("[AGENT"),
        }
        garder = filtres.get(actif)
        if garder is None:
            self.console.tag_configure("masque", elide=False)
            return
        try:
            self.console.tag_remove("masque", "1.0", tk.END)
            total = int(self.console.index("end-1c").split(".")[0])
            vides = True
            for i in range(1, total + 1):
                texte = self.console.get("%d.0" % i, "%d.end" % i)
                if not texte.strip():
                    continue
                if garder(texte):
                    vides = False
                else:
                    self.console.tag_add("masque", "%d.0" % i, "%d.end+1c" % i)
            self.console.tag_configure("masque", elide=True)
            self._afficher_onglet_vide(actif if vides else None)
        except tk.TclError:
            pass

    def _afficher_onglet_vide(self, nom):
        """Affiche (ou masque) le message d'onglet vide.

        BUG CORRIGÉ : ce message était INSÉRÉ DANS LA CONSOLE. Il devenait
        donc du contenu permanent, refiltré au passage suivant, et chaque
        changement d'onglet en empilait un de plus -- au bout de dix
        bascules, la console contenait dix lignes de service que
        l'utilisateur n'avait jamais produites, et que _clear_console seul
        pouvait retirer.

        Un Label distinct au lieu du tampon : le message n'appartient pas à
        la sortie du programme, il appartient à l'interface. Les mélanger
        était l'erreur de départ."""
        if not hasattr(self, "message_onglet_vide"):
            return
        messages = {
            "SORTIE": "Aucune sortie de script pour l'instant. Cliquez ▶ EXÉCUTER.",
            "ERREURS": "Aucune erreur. ",
            "TESTS": "Aucun test lancé. Panneau TESTS du rail, ou 🧪 dans OUTILS UVI.",
            "TERMINAL": "Aucune commande saisie. Tapez dans le champ UVI ❯ ci-dessous.",
            "DÉBOGUEUR": "Aucun point d'arrêt. Cliquez un numéro de ligne dans l'éditeur.",
            "AGENT": "L'agent n'a pas encore tourné. Bouton 🪄 AUTO-CORRIGER.",
        }
        message = messages.get(nom) if nom else None
        try:
            if message:
                self.message_onglet_vide.config(text="  " + message)
                self.message_onglet_vide.pack(fill=tk.X, before=self.zone_console)
            else:
                self.message_onglet_vide.pack_forget()
        except tk.TclError:
            pass

    def _build_canvas_tab(self):
        """Onglet CANVAS : troisième vue du workspace, même mécanisme de
        bascule que ÉDITEUR/TERMINAL (pack_forget / pack, cf. show_editor/
        show_terminal/show_canvas). Contient un tk.Canvas où sont rendues
        toutes les commandes DRAW.* -- unique point d'entrée graphique de
        l'IDE, appelé par canvas_draw() ci-dessous, elle-même appelée par
        _exec_draw (tree-walker, script réel) et par le draw_fn passé à
        UVIVirtualMachine dans run_script_vm (VM Phase 3)."""
        self.frame_canvas = tk.Frame(self.workspace, bg=T.CHAMP)

        toolbar = tk.Frame(self.frame_canvas, bg="#111111", height=32)
        toolbar.pack(fill=tk.X, side=tk.TOP)
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar, text="🎨 CANEVAS 2D — rendu des commandes DRAW.*",
            bg="#111111", fg="#00FFFF", font=("Consolas", 9, "bold"),
        ).pack(side=tk.LEFT, padx=10, pady=6)
        tk.Button(
            toolbar, text="🗑 Effacer",
            bg="#111111", fg="#555555", activebackground="#222222",
            activeforeground="#FF4444", relief=tk.FLAT,
            font=("Consolas", 9), cursor="hand2",
            command=lambda: self.canvas_draw("clear"),
        ).pack(side=tk.RIGHT, padx=10, pady=4)

        canvas_frame = tk.Frame(self.frame_canvas, bg="#050505")
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.dev_canvas = tk.Canvas(
            canvas_frame, bg="#000000", highlightthickness=1,
            highlightbackground="#333333",
        )
        self.dev_canvas.pack(fill=tk.BOTH, expand=True)
        self.dev_canvas_bg = "#000000"

    def canvas_draw(self, op: str, *args):
        """Contrat appelé pour chaque effet de bord DRAW.* -- voir
        _exec_draw (tree-walker) et le draw_fn transmis à
        UVIVirtualMachine dans run_script_vm (VM Phase 3).

        DOCSTRING CORRIGÉE (elle décrivait encore l'ancienne implémentation
        après que le corps de la méthode soit passé à _poster_ui -- voir le
        commentaire juste avant l'appel, en bas de cette méthode, qui
        explique pourquoi root.after() nu a été abandonné). Ce qui est
        VRAI aujourd'hui : _do() est posté sur la file thread-safe
        _poster_ui, vidée uniquement par _pomper_file_ui sur le thread
        principal -- sûr quel que soit le thread appelant. run_script
        (tree-walker) ET run_script_vm (VM Phase 3) tournent maintenant
        TOUS LES DEUX dans un thread dédié (voir leurs docstrings
        respectives), donc canvas_draw y est systématiquement appelée hors
        du thread principal ; _poster_ui est ce qui rend ça sûr, pas
        root.after."""
        def _do():
            c = self.dev_canvas
            if op == "clear":
                c.delete("all")
            elif op == "bgcolor":
                couleur = args[0] if args and str(args[0]).strip() else "#000000"
                try:
                    c.configure(bg=couleur)
                    self.dev_canvas_bg = couleur
                except tk.TclError:
                    pass  # couleur invalide -- effet de bord silencieusement ignoré
            elif op == "rect":
                # 6e argument optionnel = remplissage, 7e = épaisseur du
                # contour ; "" et 1 = comportement historique EXACT si
                # absents (1 est aussi le défaut natif de Tkinter).
                x, y, w, h, couleur = args[:5]
                remplissage = args[5] if len(args) >= 6 else ""
                epaisseur = args[6] if len(args) >= 7 else 1
                c.create_rectangle(x, y, x + w, y + h, outline=couleur, fill=remplissage,
                                    width=epaisseur)
            elif op == "circle":
                x, y, r, couleur = args[:4]
                remplissage = args[4] if len(args) >= 5 else ""
                epaisseur = args[5] if len(args) >= 6 else 1
                c.create_oval(x - r, y - r, x + r, y + r, outline=couleur, fill=remplissage,
                               width=epaisseur)
            elif op == "line":
                x1, y1, x2, y2, couleur = args[:5]
                epaisseur = args[5] if len(args) >= 6 else 1
                c.create_line(x1, y1, x2, y2, fill=couleur, width=epaisseur)
            elif op == "text":
                # 5e argument optionnel = taille de police ; 10 =
                # comportement historique EXACT si absent.
                x, y, texte, couleur = args[:4]
                taille_police = args[4] if len(args) >= 5 else 10
                c.create_text(x, y, text=texte, fill=couleur, anchor="nw",
                               font=("Consolas", taille_police))
            elif op == "sprite":
                x, y, taille, pixels = args
                self._draw_sprite_grid(x, y, taille, pixels)
            elif op == "poly":
                # points est une liste PLATE x1,y1,x2,y2,... (déjà
                # validée pair + ≥3 points côté _exec_draw / VM avant
                # d'arriver ici) ; create_polygon accepte cette forme
                # plate directement en *args. 4e argument optionnel =
                # épaisseur du contour, défaut 1 (natif Tkinter).
                points, couleur, remplissage = args[:3]
                epaisseur = args[3] if len(args) >= 4 else 1
                c.create_polygon(*points, outline=couleur, fill=remplissage, width=epaisseur)
        # _poster_ui et non root.after : canvas_draw est appelée depuis le
        # THREAD D'EXÉCUTION du script, à chaque instruction DRAW.*. C'était
        # donc un appel Tcl depuis un thread secondaire répété des dizaines
        # de fois par seconde dans une boucle de jeu -- la variante la plus
        # dangereuse de la faute corrigée pour la saisie, et une explication
        # plausible des blocages observés en jeu.
        self._poster_ui(_do)

    # _build_chat_panel (fabrique de panneau de discussion générique) a
    # été retirée avec _build_shion_tab, son unique appelant -- elle
    # n'était plus jamais invoquée nulle part ailleurs (_build_colonne_shion,
    # la colonne de droite toujours en usage, construit ses propres
    # widgets directement, sans passer par cette fabrique).
    def _chat_append(self, prefixe: str, texte: str, tag: str, entete: str = ""):
        """Ajoute une entrée dans l'historique du panneau `prefixe`.

        Widget en lecture seule (state=DISABLED en permanence sauf le
        temps de l'insertion) -- même principe que self.console pour le
        terminal (voir log()), pour empêcher l'utilisateur de modifier
        l'historique directement au clavier."""
        historique = getattr(self, prefixe + "_historique")
        historique.config(state=tk.NORMAL)
        if entete:
            historique.insert(tk.END, entete + "\n", tag)
        historique.insert(tk.END, str(texte) + "\n\n", "corps" if tag != "erreur" else tag)
        historique.config(state=tk.DISABLED)
        historique.see(tk.END)

    def _construire_contexte_langage_uvi(self) -> str:
        """Bloc de contexte optionnel pour SHION : référence du langage
        (UVI_REFERENCE_LANGAGE) + code actuel de l'éditeur + son AST RÉEL,
        produit par le vrai parseur UVI (self.engine.parser), pas par SHION
        lui-même -- voir la case « Inclure le code + AST de l'éditeur » de
        _build_shion_tab, qui déclenche cet enrichissement dans _chat_send.

        POURQUOI L'AST ET PAS SEULEMENT LE CODE SOURCE : lui montrer le
        texte seul laisse SHION deviner la syntaxe par analogie avec un
        langage connu -- justement la source d'erreur qu'UVI_REFERENCE_LANGAGE
        cherche à éviter. L'AST est la sortie du VRAI parseur : aucune
        réinterprétation possible, juste la structure telle qu'UVI l'a
        effectivement comprise (via Parser.program_to_json, déjà utilisé
        par IAAgent.export_ast pour le même besoin côté humain).

        Troncature volontaire : le serveur local tourne avec un contexte de
        4096 tokens (voir docs/SERVEUR_LOCAL_CPU.md) -- un script long,
        une fois transformé en JSON d'AST (souvent 3 à 5x plus verbeux que
        le source), l'épuiserait à lui seul, sans laisser de place à la
        réponse."""
        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            return UVI_REFERENCE_LANGAGE + "\n(Éditeur vide -- aucun code à analyser.)"
        try:
            noeuds = self.engine.parser.parse_program(code.splitlines())
            ast_json = self.engine.parser.program_to_json(noeuds)
        except Exception as e:
            ast_json = "(échec de l'analyse syntaxique : %s)" % e
        code_tronque = code if len(code) <= 3000 else code[:3000] + "\n... (tronqué)"
        ast_tronque = ast_json if len(ast_json) <= 6000 else ast_json[:6000] + "\n... (tronqué)"
        return (
            UVI_REFERENCE_LANGAGE
            + "\n[Code source UVI actuel de l'éditeur]\n```\n%s\n```\n" % code_tronque
            + "\n[AST réel de ce code, produit par le parseur UVI -- source "
              "de vérité structurelle, pas une reformulation]\n```json\n%s\n```"
            % ast_tronque
        )

    def _chat_send(self, prefixe: str, client, nom_repondeur: str,
                   question: Optional[str] = None,
                   prompt_envoye: Optional[str] = None):
        """Envoi non bloquant commun aux deux onglets de discussion.

        La requête réseau (LLMClient.complete, bloquante -- urllib avec
        timeout) tourne dans un thread daemon séparé pour ne JAMAIS geler
        le thread principal Tkinter pendant l'attente -- même principe que
        _exec_shion (SHION.ASK non-bloquant). Le champ de saisie et le
        bouton sont désactivés pendant l'attente pour empêcher d'empiler
        une seconde requête avant que la première ait répondu (pas de Lock
        nécessaire : un seul thread daemon en vol à la fois, garanti par
        cette désactivation d'UI plutôt que par une variable partagée --
        cf. le commentaire équivalent sur shion_is_busy).

        Le retour au thread principal passe par _poster_ui (file
        d'attente) : rien de ce qui touche un widget ne s'exécute depuis le
        thread worker, pas même un root.after -- qui est lui-même un appel
        Tcl et donc soumis à la même contrainte.

        `question`/`prompt_envoye` : envoi PROGRAMMATIQUE (voir action_shion),
        sans passer par le champ de saisie. `question` est ce qui s'affiche
        dans l'historique (COURT, lisible) ; `prompt_envoye` (par défaut
        identique à `question` si omis) est ce qui part réellement au
        modèle -- peut être bien plus long (référence du langage UVI + AST,
        voir _construire_contexte_langage_uvi) sans polluer l'historique
        affiché. Sans ces paramètres (appel interactif normal), les deux
        sont identiques et lus depuis le champ de saisie."""
        saisie = getattr(self, prefixe + "_input")
        bouton = getattr(self, "btn_" + prefixe + "_send")
        interactif = question is None
        if interactif:
            question = saisie.get().strip()
        if not question:
            return
        if client is None:
            self._chat_append(prefixe, "Service non configuré (voir le message ci-dessus).", "erreur")
            return
        if prompt_envoye is None:
            prompt_envoye = question

        if interactif:
            saisie.delete(0, tk.END)
        self._chat_append(prefixe, question, "vous", entete="Vous :")
        saisie.config(state=tk.DISABLED)
        bouton.config(state=tk.DISABLED)
        if getattr(self, "shion_avatar", None) is not None:
            self.shion_avatar.react("USER_MESSAGE_SENT")

        def _worker():
            try:
                reponse = client.complete(prompt_envoye)
                erreur = None
            except LLMClientError as e:
                # LLMClientError couvre tous les backends (DeepSeekClientError,
                # ShionClientError...) sans avoir à les connaître un par un --
                # rattraper une exception plus spécifique aurait laissé filer
                # les autres, invisibles dans un thread daemon, et l'onglet
                # serait resté figé sur "saisie désactivée".
                reponse, erreur = None, str(e)
            except Exception as e:  # filet : un thread daemon qui meurt en silence gèlerait l'onglet
                reponse, erreur = None, "Erreur inattendue : %s" % e

            def _apply():
                if erreur is not None:
                    self._chat_append(prefixe, erreur, "erreur")
                    if getattr(self, "shion_avatar", None) is not None:
                        self.shion_avatar.react("ERROR_OCCURRED")
                else:
                    self._chat_append(prefixe, reponse, "assistant", entete=nom_repondeur + " :")
                    if getattr(self, "shion_avatar", None) is not None:
                        self.shion_avatar.react("USER_MESSAGE_RECEIVED")
                saisie.config(state=tk.NORMAL)
                bouton.config(state=tk.NORMAL)
                saisie.focus_set()

            # Même raison que pour request_input : ce code tourne dans un
            # thread daemon, root.after() y est un appel Tk non fiable.
            self._poster_ui(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    # _build_shion_tab (troisième vue du workspace, à côté de CANEVAS) a
    # été RETIRÉE : elle partageait les noms self.shion_input/
    # self.shion_historique/btn_shion_send avec _build_colonne_shion (la
    # colonne de droite, construite APRÈS elle dans _build_container), qui
    # les écrasait systématiquement -- cet onglet était donc mort depuis
    # sa création, son propre champ de saisie et son historique n'ayant
    # jamais reçu la moindre interaction réelle (voir l'ancienne "NOTE
    # D'ARCHITECTURE" à cet endroit, qui documentait déjà ce constat sans
    # le corriger). _shion_send_message/_shion_reset_conversation restent
    # : la colonne de droite (seule survivante, seule jamais réellement
    # utilisée) les appelle toujours -- voir _build_colonne_shion.
    def _shion_send_message(self):
        self._chat_send("shion", self.engine.shion_client, "SHION")

    def _shion_reset_conversation(self):
        if self.engine.shion_client is not None:
            self.engine.shion_client.reset_history()
        self.shion_historique.config(state=tk.NORMAL)
        self.shion_historique.delete("1.0", tk.END)
        self.shion_historique.config(state=tk.DISABLED)

    def _draw_sprite_grid(self, x: float, y: float, taille: float, pixels: list):
        """DRAW.SPRITE : pixels est un tableau UVI PLAT de couleurs
        (réutilise le système TAB existant plutôt qu'un format image
        séparé). Affiché en grille carrée -- côté = racine entière de la
        longueur du tableau, arrondi au carré parfait inférieur, le reste
        des éléments (au-delà de côté²) est ignoré plutôt que de planter
        sur un tableau de longueur non carrée (ex. 5 éléments -> grille
        2x2, le 5e pixel n'est pas affiché)."""
        import math
        n = len(pixels)
        if n <= 0:
            return
        cote = int(math.isqrt(n))
        if cote <= 0:
            return
        for i in range(cote * cote):
            row, col = divmod(i, cote)
            couleur = str(pixels[i]) if pixels[i] not in (None, "") else self.dev_canvas_bg
            px = x + col * taille
            py = y + row * taille
            try:
                self.dev_canvas.create_rectangle(
                    px, py, px + taille, py + taille, outline="", fill=couleur,
                )
            except tk.TclError:
                pass  # couleur invalide dans le tableau -- pixel ignoré, pas de crash

    def load_from_disk(self):
        filepath = filedialog.askopenfilename(
            filetypes=[("UVI Scripts", "*.uvi"), ("All Files", "*.*")]
        )
        if filepath:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.engine.memoire.clear()
            self.engine.routines.clear()
            self.engine.fonctions.clear()
            self.engine.imported_files.clear()
            self.engine.current_memory_usage = 0
            self.engine.error_flag = False
            self.engine.failed_line = None
            self.engine.last_errors = []
            self.editor.delete("1.0", tk.END)
            self.editor.insert(tk.END, content)
            self.show_editor()
            self._update_line_numbers()
            self.log(f"Fichier chargé depuis le disque : {filepath}", "sys")

    def save_to_disk(self, forcer_dialogue=False):
        """Ctrl+S / bouton SAUVEGARDER.

        Écrase le fichier courant s'il y en a un, au lieu de redemander un
        nom À CHAQUE FOIS comme le faisait la version précédente. Redemander
        systématiquement rendait Ctrl+S inutilisable en frappe rapide :
        chaque sauvegarde ouvrait une boîte de dialogue et cassait le
        rythme de travail.

        Le fichier VFS est mis à jour EN PLUS du disque quand il porte le
        même nom : sans ça, l'explorateur affichait encore l'ancienne
        version d'un fichier qu'on venait de sauvegarder, et un IMPORT
        chargeait l'ancienne."""
        chemin = getattr(self, "_chemin_disque_courant", None)
        if forcer_dialogue or not chemin:
            chemin = filedialog.asksaveasfilename(
                defaultextension=".uvi",
                filetypes=[("UVI Scripts", "*.uvi"), ("All Files", "*.*")]
            )
            if not chemin:
                return
        contenu = self.editor.get("1.0", tk.END)
        try:
            with open(chemin, 'w', encoding='utf-8') as f:
                f.write(contenu)
        except OSError as e:
            self.log("Écriture impossible : %s" % e, "err")
            return
        self._chemin_disque_courant = chemin
        nom = os.path.basename(chemin)
        # Miroir dans le VFS : write_file passe par _safe_resolve, donc un
        # nom hostile est refusé ici comme partout ailleurs.
        try:
            self.vfs.write_file(nom, contenu)
            self.update_vfs_list()
        except UVIRuntimeTrap:
            pass
        self._marquer_enregistre(nom)
        self.log("Sauvegardé : %s" % chemin, "ok")
        self._set_status("Sauvegardé — %s" % nom)
        if getattr(self, "shion_avatar", None) is not None:
            self.shion_avatar.react("FILE_SAVED")

    def update_vfs_list(self):
        """Explorateur en arborescence (maquette v9).

        Le VFS est PLAT -- c'est un répertoire unique, il n'y a pas de vrais
        sous-dossiers. L'arborescence affichée est donc un REGROUPEMENT PAR
        TYPE, pas une hiérarchie de disque. Dit explicitement ici pour que
        personne n'en déduise plus tard que le VFS gère des chemins
        imbriqués : « src » et « lib » sont des étiquettes de présentation,
        et double-cliquer dessus ne fait rien.

        Le mapping index -> nom de fichier est conservé dans
        self._index_fichiers parce que _on_vfs_double_click lit l'index de
        la Listbox : sans lui, cliquer sur « ▾ src » tenterait d'ouvrir un
        fichier nommé « ▾ src ».

        self.vfs_listbox garde son nom (8 références ailleurs)."""
        self.vfs_listbox.delete(0, tk.END)
        self._index_fichiers = {}
        # Les entrées commençant par un point (.stdlib, .packages) sont
        # l'intendance interne du registre de paquets, pas des documents de
        # l'utilisateur. list_files() les rend -- c'est correct, elle décrit
        # le disque -- mais les AFFICHER dans l'explorateur ferait croire à
        # des fichiers ouvrables, alors qu'un double-clic dessus ne peut que
        # échouer (ce sont des répertoires). Filtrage à l'affichage
        # uniquement : list_files() n'est pas modifiée, huit autres appelants
        # en dépendent.
        fichiers = [f for f in self.vfs.list_files() if not f.startswith(".")]
        groupes = [
            ("src", [f for f in fichiers if f.endswith(".uvi")
                     and not f.startswith(("lib_", "std_"))]),
            ("lib", [f for f in fichiers if f.endswith(".uvi")
                     and f.startswith(("lib_", "std_"))]),
            ("données", [f for f in fichiers if not f.endswith(".uvi")]),
        ]
        position = 0
        for nom_groupe, membres in groupes:
            if not membres:
                continue
            self.vfs_listbox.insert(tk.END, "  ▾ %s" % nom_groupe)
            self.vfs_listbox.itemconfig(position, foreground=T.TEXTE_FAIBLE)
            position += 1
            for fichier in sorted(membres):
                icone = "📄" if fichier.endswith(".uvi") else "📋"
                self.vfs_listbox.insert(tk.END, "      %s %s" % (icone, fichier))
                self._index_fichiers[position] = fichier
                position += 1
        if not fichiers:
            self.vfs_listbox.insert(tk.END, "  (stockage vide)")
            self.vfs_listbox.itemconfig(0, foreground=T.TEXTE_FAIBLE)

    def _on_vfs_double_click(self, event):
        """Ouvre le fichier sélectionné dans l'éditeur.

        Passe par self._index_fichiers et non par le texte de la ligne :
        l'arborescence v9 préfixe les entrées d'une indentation et d'une
        icône, et les lignes de groupe (« ▾ src ») ne correspondent à aucun
        fichier."""
        selection = self.vfs_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        nom = getattr(self, "_index_fichiers", {}).get(index)
        if nom is None:
            return  # ligne de groupe : rien à ouvrir
        contenu = self.vfs.read_file(nom)
        if contenu is None:
            self.log("[FILE_NOT_FOUND] Fichier introuvable : %s" % nom, "err")
            return
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", contenu)
        self._update_line_numbers()
        self.show_editor()
        self._marquer_enregistre(nom)
        self._set_status("%s chargé." % nom)
        if getattr(self, "shion_avatar", None) is not None:
            self.shion_avatar.react("FILE_OPENED")

    def _define_tags(self):
        """Coloration syntaxique v9.

        Les NOMS de tags sont strictement conservés (signature, comment,
        print_cmd, assign, routine, condition, loop, string, sleep, disk,
        ask_input, func_def, func_call, array, error_line, import) : c'est
        _highlight() qui les applique par expression régulière, et renommer
        un seul tag rendrait sa règle silencieusement inopérante -- le texte
        s'afficherait simplement sans couleur, sans la moindre erreur pour
        le signaler.

        Regroupement en familles plutôt qu'une teinte par mot-clé : la v8
        avait seize couleurs sans parenté, si bien qu'un script un peu long
        ressemblait à un arc-en-ciel où plus rien ne ressortait. Ici, les
        mots-clés de structure partagent le violet, les données le jaune, la
        machinerie le cyan."""
        police_gras = ("Consolas", 12, "bold")
        self.editor.tag_configure("signature",  foreground=T.SYN_OPERATEUR, font=police_gras)
        self.editor.tag_configure("comment",    foreground=T.SYN_COMMENT, font=("Consolas", 12, "italic"))
        self.editor.tag_configure("print_cmd",  foreground=T.VERT, font=police_gras)
        self.editor.tag_configure("assign",     foreground=T.TEXTE)
        self.editor.tag_configure("routine",    foreground=T.ROSE, font=police_gras)
        self.editor.tag_configure("condition",  foreground=T.SYN_MOTCLE, font=police_gras)
        self.editor.tag_configure("loop",       foreground=T.SYN_MOTCLE, font=police_gras)
        self.editor.tag_configure("string",     foreground=T.SYN_CHAINE)
        self.editor.tag_configure("sleep",      foreground=T.AMBRE)
        self.editor.tag_configure("disk",       foreground=T.CYAN, font=police_gras)
        self.editor.tag_configure("ask_input",  foreground=T.CYAN, font=police_gras)
        self.editor.tag_configure("func_def",   foreground=T.AMBRE, font=police_gras)
        self.editor.tag_configure("func_call",  foreground=T.SYN_FONCTION, font=police_gras)
        self.editor.tag_configure("array",      foreground=T.VIOLET, font=police_gras)
        self.editor.tag_configure("import",     foreground=T.SYN_FONCTION, font=("Consolas", 12, "bold", "underline"))
        # Ligne en erreur : bandeau rouge sombre plutôt que le #8B0000 plein
        # de la v8, qui rendait le texte de la ligne fautive quasi illisible
        # -- exactement la ligne qu'on a le plus besoin de lire.
        self.editor.tag_configure("error_line", background="#3A1520",
                                  foreground="#FFB4B4")
        self.editor.tag_configure("ligne_courante", background="#0E1A2E")

    def _highlight(self):
        if self._lock_highlight:
            return
        self._lock_highlight = True
        try:
            for tag in (
                "signature", "comment", "print_cmd", "assign",
                "routine", "condition", "loop", "string", "sleep",
                "disk", "ask_input", "func_def", "func_call", "array", "error_line", "import"
            ):
                self.editor.tag_remove(tag, "1.0", tk.END)
            content = self.editor.get("1.0", tk.END)
            lines = content.split("\n")
            for i, line in enumerate(lines):
                row = i + 1
                stripped = line.strip()
                def apply(tag, col_start=0, col_end=None, _line=line):
                    end = col_end if col_end is not None else len(_line)
                    self.editor.tag_add(tag, f"{row}.{col_start}", f"{row}.{end}")
                if stripped == "@UVI":
                    apply("signature")
                elif stripped.startswith("#"):
                    apply("comment")
                elif stripped.startswith("IMPORT "):
                    apply("import")
                elif stripped.startswith("DEF "):
                    apply("func_def")
                elif stripped.startswith("CALL "):
                    apply("func_call")
                elif stripped.startswith("TAB") or stripped.startswith("STR"):
                    apply("array")
                elif stripped.startswith("!"):
                    apply("print_cmd")
                elif stripped.startswith("*"):
                    apply("routine")
                elif stripped.startswith("~"):
                    apply("condition")
                elif stripped.startswith("%"):
                    apply("loop")
                elif stripped.startswith("_") and "=" not in stripped:
                    apply("sleep")
                elif stripped.startswith("&"):
                    apply("disk")
                elif stripped.startswith("?"):
                    apply("ask_input")
                elif "=" in stripped:
                    apply("assign")
                in_str = False
                start = 0
                for j, ch in enumerate(line):
                    if ch == '"':
                        if not in_str:
                            in_str = True
                            start = j
                        else:
                            in_str = False
                            self.editor.tag_add("string", f"{row}.{start}", f"{row}.{j + 1}")
            if getattr(self.engine, 'failed_line', None):
                r = self.engine.failed_line
                self.editor.tag_add("error_line", f"{r}.0", f"{r}.end")
        finally:
            self._lock_highlight = False

    def _update_line_numbers(self, event=None):
        if event is not None:
            self.engine.failed_line = None
        self._highlight()
        self._sync_line_numbers()

    def _sync_line_numbers(self):
        """Numéros de ligne + pastilles de points d'arrêt.

        Le losange ◆ est rendu DANS le texte de la gouttière, pas comme un
        widget superposé : un widget par ligne serait à repositionner à
        chaque défilement, et Tkinter ne synchronise pas les widgets flottants
        avec le scroll d'un Text. Le caractère, lui, défile avec la ligne
        gratuitement."""
        total = int(self.editor.index(tk.END).split('.')[0])
        points = getattr(self, "_points_arret", set())
        self.line_numbers.config(state=tk.NORMAL)
        self.line_numbers.delete("1.0", tk.END)
        lignes = []
        for n in range(1, total):
            lignes.append("◆%3d" % n if n in points else " %3d" % n)
        self.line_numbers.insert(tk.END, "\n".join(lignes))
        self.line_numbers.tag_configure("point_arret", foreground=T.ROUGE)
        for n in points:
            if n < total:
                self.line_numbers.tag_add("point_arret", "%d.0" % n, "%d.1" % n)
        self.line_numbers.config(state=tk.DISABLED)

    def _highlight_error_line(self, row):
        self.engine.failed_line = row
        self.show_editor()
        self.root.update_idletasks()
        self.editor.see(f"{row}.0")

    # "terminal" ne fait PLUS partie de la pile de vues : frame_terminal
    # vit désormais dans le dock inférieur, visible en permanence à côté de
    # l'éditeur (voir _build_container). L'y laisser aurait fait
    # pack_forget() la console à chaque changement de vue -- elle aurait
    # disparu au premier clic sur CANEVAS pour ne jamais revenir.
    _VUES = ("editor", "canvas")

    def _show_view(self, nom: str):
        """Bascule vers une vue du workspace (pack_forget des autres, pack
        de celle-ci) et met la topbar en accord.

        FACTORISATION SIGNALÉE. Avant : plusieurs méthodes show_* qui
        répétaient chacune leurs pack_forget et config de bouton. La table
        _VUES rend l'ajout (ou le retrait -- voir le départ de l'onglet
        ERIS) d'une vue mécanique.

        Les méthodes show_editor/show_terminal/show_canvas sont CONSERVÉES
        et délèguent ici : elles sont référencées par les commandes de
        boutons et appelées depuis run_script, _highlight_error_line,
        _afficher_accueil, etc."""
        for autre in self._VUES:
            frame = getattr(self, "frame_" + autre, None)
            if frame is not None:
                frame.pack_forget()
        frame_active = getattr(self, "frame_" + nom, None)
        if frame_active is not None:
            frame_active.pack(fill=tk.BOTH, expand=True)
        boutons = {
            "editor": "btn_edit", "terminal": "btn_term", "canvas": "btn_canvas",
        }
        for vue, nom_attr in boutons.items():
            bouton = getattr(self, nom_attr, None)
            if bouton is None:
                continue
            if vue == nom:
                bouton.config(bg="#252525", fg="#00FF00")
            else:
                bouton.config(bg="#111111", fg="#555555")

    def show_editor(self):
        self._show_view("editor")
        self.editor.focus_set()
        self._update_line_numbers()

    def show_terminal(self):
        """Conservée : appelée à une quinzaine d'endroits (run_script, les
        méthodes de l'agent, _afficher_accueil...).

        CORRIGÉ (retour utilisateur) : la console est certes un dock
        permanent (voir §DISPOSITION v9), mais « afficher le terminal » se
        limitait à y amener le REGARD (scroll + focus), sans jamais
        ramener la vue centrale sur l'éditeur. Concrètement : lancer un
        script pendant qu'on est sur l'onglet SHION laissait ce panneau
        de discussion occuper tout le centre de l'écran -- la
        console restait techniquement visible dans le dock du bas, mais
        hors du regard, ce qui donnait l'impression que l'exécution
        n'affichait plus rien et qu'un ?INPUT restait sans réponse
        possible. _show_view("editor") ramène l'éditeur (et donc le dock
        qui vit juste en dessous) au premier plan à chaque appel -- c'est
        ce qui manquait pour que « basculer sur le terminal » veuille
        vraiment dire quelque chose."""
        self._assurer_dock_ouvert()
        self._show_view("editor")
        self._selectionner_onglet_console("CONSOLE UVI")
        try:
            self.console.see(tk.END)
            if not self.engine.is_running:
                self.cmd_input.focus_set()
        except tk.TclError:
            pass

    def show_canvas(self):
        self._show_view("canvas")

    # ---------------------------------------------------------------
    # ÉTAPE 6 -- déclenchement de IAAgent.autonomous_fix depuis l'IDE
    # ---------------------------------------------------------------
    #: Objectif par défaut passé à autonomous_fix. Formulé pour correspondre
    #: EXACTEMENT à ce que _default_verify_fn sait vérifier (absence de trap
    #: à l'exécution) -- ni plus, ni moins. Promettre « le script doit faire
    #: ce que je veux » alors que la vérification ne teste que « il ne
    #: plante pas » produirait un agent qui se déclare satisfait d'un script
    #: syntaxiquement valide et fonctionnellement faux.
    OBJECTIF_AUTO_FIX = (
        "Le script UVI doit s'exécuter du début à la fin sans déclencher "
        "d'erreur d'exécution. Corrige uniquement ce qui empêche l'exécution "
        "d'aboutir, sans changer l'intention du script."
    )

    def _generate_fn_disponible(self):
        """Rend (fonction_de_generation, nom_du_backend) ou (None, None)."""
        if HAS_SHION and self.engine.shion_client is not None:
            return (lambda prompt: self.engine.shion_client.complete(prompt),
                    self.engine.shion_client.nom_affichage)
        return (None, None)

    def _log_agent_steps(self, resultat, total_prevu: int):
        """ÉTAPE 7.3 : rend AgentRunResult.steps lisible dans la console
        texte existante, sans nouveau widget.

        Avant : autonomous_fix ne rendait qu'une ligne finale
        (stopped_reason brut), donc une boucle de huit itérations et deux
        minutes d'attente se résumait à « bloqué » -- impossible de savoir
        si l'agent avait progressé, tourné en rond, ou échoué dès le premier
        appel réseau.

        Format retenu : un préfixe [AGENT n/N] par étape, et à l'intérieur
        des lignes indentées et étiquetées. Le préfixe permet de retrouver
        les traces de l'agent au milieu de la sortie du script (les deux se
        mélangent dans la même console). Les textes longs (observation, code
        généré) sont tronqués -- l'objectif est de suivre la progression,
        pas de relire le prompt complet.

        Le résumé final distingue TROIS issues, là où stopped_reason en
        mélangeait plusieurs sous le même mot : réussi / épuisé (l'agent
        avait encore des idées mais plus de budget) / bloqué (l'agent
        n'avançait plus). La distinction est actionnable : « épuisé »
        invite à relancer avec plus d'itérations, « bloqué » indique que
        relancer à l'identique ne servirait à rien."""
        etapes = resultat.steps or []
        for i, etape in enumerate(etapes, start=1):
            self.log("[AGENT %d/%d] ─────────────────────────────" % (i, total_prevu), "sys")
            if etape.observation:
                self.log("    observé   : %s" % self._tronquer(etape.observation, 220), "info")
            if etape.action_parse_error:
                self.log("    action    : NON COMPRISE — %s" % etape.action_parse_error, "err")
            elif isinstance(etape.action, CodeAction):
                self.log("    action    : réécriture du script (%d caractères)"
                         % len(etape.action.code), "info")
            elif etape.action is not None:
                self.log("    action    : %s" % self._tronquer(str(etape.action), 160), "info")
            else:
                self.log("    action    : aucune action exploitable produite", "err")
            marque = "OK" if etape.verification_passed else "ÉCHEC"
            self.log("    vérifié   : %s — %s"
                     % (marque, self._tronquer(etape.verification_message, 220)),
                     "ok" if etape.verification_passed else "err")
            if etape.issue_diagnosis is not None:
                self.log("    diagnostic: sous-boucle de diagnostic déclenchée "
                         "(%d tentative(s))" % len(getattr(etape.issue_diagnosis, "attempts", []) or []),
                         "sys")
            if etape.decision is not None:
                self.log("    décision  : %s" % etape.decision.value, "sys")

        self.log("─" * 46, "sys")
        if resultat.succeeded:
            self.log("[AGENT] RÉUSSI — objectif atteint en %d étape(s). Le script "
                     "corrigé est dans l'éditeur." % len(etapes), "ok")
        elif resultat.stopped_reason in ("max_iterations", "timeout"):
            self.log("[AGENT] ÉPUISÉ — budget atteint (%s) après %d étape(s). "
                     "L'agent progressait encore : relancer peut aboutir."
                     % (resultat.stopped_reason, len(etapes)), "err")
        else:
            self.log("[AGENT] BLOQUÉ — %s après %d étape(s). Relancer à "
                     "l'identique ne changera rien ; corrigez la piste "
                     "indiquée ci-dessus à la main."
                     % (resultat.stopped_reason, len(etapes)), "err")

    @staticmethod
    def _tronquer(texte, limite: int) -> str:
        texte = str(texte or "").replace("\n", " ⏎ ").strip()
        return texte if len(texte) <= limite else texte[:limite] + " […]"

    def autonomous_fix_from_editor(self, max_iterations: int = 8):
        """Lance IAAgent.autonomous_fix sur le contenu de l'éditeur.

        THREAD DAEMON OBLIGATOIRE, ce n'est pas une précaution de confort :
        autonomous_fix enchaîne jusqu'à `max_iterations` appels réseau
        bloquants, chacun pouvant durer des dizaines de secondes sur un
        modèle de raisonnement. Exécuté sur le thread Tkinter, l'IDE
        paraîtrait planté pendant plusieurs minutes -- et le gestionnaire de
        fenêtres proposerait de tuer l'application.

        AVERTISSEMENT DE FIABILITÉ, affiché à l'utilisateur et pas seulement
        écrit ici : cette boucle n'a PAS été validée sur des cas réels
        variés. Elle est structurellement correcte (AgentLoop +
        IssueDiagnosisLoop sont testés unitairement) mais sa réussite dépend
        entièrement de la qualité du modèle branché derrière generate_fn.
        Un 7B distillé produit fréquemment du code hors format
        ```uvi ... ``` -- que _default_code_extractor ne reconnaît alors pas,
        ce qui donne une étape sans action exploitable. C'est pour ça que
        _log_agent_steps affiche « action : NON COMPRISE » explicitement au
        lieu de laisser croire à un échec du script."""
        generate_fn, nom_backend = self._generate_fn_disponible()
        if generate_fn is None:
            self.show_terminal()
            self.log(
                "[SHION_UNAVAILABLE] AUTO-CORRIGER a besoin d'un modèle de "
                "langage : SHION n'est pas configuré.", "err",
            )
            return
        if getattr(self, "_agent_fix_en_cours", False):
            self.log("Une correction autonome est déjà en cours.", "err")
            return
        if self.agent is None:
            return

        code = self.editor.get("1.0", tk.END).rstrip()
        if not code.strip():
            self.show_terminal()
            self.log("Éditeur vide : rien à corriger.", "err")
            return

        self.show_terminal()
        self._agent_fix_en_cours = True
        self.btn_auto_fix.config(state=tk.DISABLED, cursor="X_cursor")
        self.log("=== AGENT AUTONOME — DÉMARRAGE ===", "sys")
        self.log("Modèle : %s | budget : %d itérations" % (nom_backend, max_iterations), "info")
        self.log("Objectif : %s" % self.OBJECTIF_AUTO_FIX, "info")
        self.log("⚠ Capacité EXPÉRIMENTALE, non validée sur des cas réels variés — "
                 "relisez toujours le script proposé avant de l'exécuter.", "sys")
        self._set_status("🤖 Agent autonome en cours…")

        def _worker():
            try:
                resultat = self.agent.autonomous_fix(
                    goal_description=self.OBJECTIF_AUTO_FIX,
                    generate_fn=generate_fn,
                    initial_snippet=code,
                    max_iterations=max_iterations,
                )
                echec = None
            except Exception as e:
                # Filet volontairement large : autonomous_fix traverse
                # AgentLoop, IssueDiagnosisLoop, la VM et un client réseau.
                # Une exception non prévue dans un thread daemon
                # disparaîtrait sans trace, laissant le bouton grisé pour
                # toujours -- exactement le « échec silencieux » à ne pas
                # laisser passer.
                resultat, echec = None, e

            def _apply():
                self._agent_fix_en_cours = False
                self.btn_auto_fix.config(state=tk.NORMAL, cursor="hand2")
                self._set_status("Terminé.")
                if echec is not None:
                    self.log("[AGENT] INTERROMPU — %s : %s"
                             % (type(echec).__name__, echec), "err")
                    return
                self._log_agent_steps(resultat, max_iterations)
                self.log("=== AGENT AUTONOME — FIN ===", "sys")

            self._poster_ui(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def toggle_voice(self):
        if not HAS_TTS:
            self.log("Erreur : pyttsx3 n'est pas installé.", "err")
            return
        if self.agent:
            self.agent.voice_enabled = not self.agent.voice_enabled
            if self.agent.voice_enabled:
                self.btn_voice.config(text="🔊 IA VOIX", fg="#00FF00")
                self.log("IA : Synthèse vocale activée.", "sys")
            else:
                self.btn_voice.config(text="🔇 IA MUETTE", fg="#FF4444")
                self.log("IA : Synthèse vocale désactivée.", "sys")

    def request_input(self, var_name):
        """Appelée par ? depuis le THREAD D'EXÉCUTION du script.

        Bloque ce thread jusqu'à ce que l'utilisateur réponde, sans faire
        UN SEUL appel Tk depuis ce thread -- toute manipulation de widget
        est renvoyée au thread principal par _poster_ui (file d'attente
        vidée par la boucle Tk), et l'attente se fait sur un
        threading.Event.

        POURQUOI PAS root.wait_variable, qui était utilisé avant : c'est
        une primitive Tk, et l'appeler depuis un thread secondaire est
        exactement le genre de faute qui « marche souvent ». Un blocage
        aléatoire chez l'utilisateur est plus coûteux à diagnostiquer
        qu'une panne franche, parce qu'il n'est pas reproductible.

        Event et non Queue : on attend UNE valeur, une seule fois, et
        l'Event dit exactement ça. Une Queue suggérerait un flux."""
        self._saisie_evenement = threading.Event()
        self._saisie_resultat = {"valeur": ""}
        # _poster_ui et NON root.after : after() est un appel Tcl, donc
        # interdit depuis ce thread. C'est ce point précis qui faisait que
        # la barre de saisie n'apparaissait jamais et que le script restait
        # suspendu sans que rien ne s'affiche.
        self._poster_ui(lambda: self._ouvrir_saisie(var_name))

        # timeout de 10 minutes plutôt qu'une attente infinie : si la
        # fenêtre est fermée pendant l'attente, ce thread daemon resterait
        # sinon bloqué à jamais et empêcherait un arrêt propre.
        obtenu = self._saisie_evenement.wait(timeout=600)
        if not obtenu:
            self._poster_ui(self._fermer_saisie)
            return ""
        return self._saisie_resultat.get("valeur", "")

    def _ouvrir_saisie(self, var_name):
        """Affiche la barre de saisie. Thread principal uniquement."""
        self.is_waiting_input = True
        # Rouvre le dock s'il a été replié (voir _assurer_dock_ouvert) --
        # AVANT tout le reste : barre_saisie (plus bas) VIT dans le dock,
        # la rouvrir alors que le dock est replié la laisserait empaquetée
        # mais invisible, soit exactement le bug déjà corrigé une fois
        # pour cette même barre (voir le commentaire BUGFIX plus bas).
        self._assurer_dock_ouvert()
        # Ramène aussi la vue CENTRALE sur l'éditeur (même raison que le
        # correctif de show_terminal juste au-dessus) : un script qui
        # dessine (CANEVAS affiché, cf. run_script/run_script_vm) puis
        # demande une saisie ne doit pas laisser l'invite invisible
        # derrière le canevas -- répondre à ?INPUT est plus urgent que
        # regarder un dessin à moitié terminé.
        self._show_view("editor")
        # Bascule sur un onglet où l'invite est LISIBLE : sur SORTIE ou
        # ERREURS, la ligne d'invite serait masquée par le filtre, et
        # l'utilisateur verrait un script figé sans savoir pourquoi. C'est
        # exactement le bug corrigé ici.
        if self._onglet_console_actif not in ("CONSOLE UVI", "TERMINAL"):
            self._selectionner_onglet_console("CONSOLE UVI")
        self.log("Le script demande une valeur pour « %s »." % var_name, "sys")
        self.etiquette_saisie.config(text="?  %s  =" % var_name)
        self.champ_saisie.delete(0, tk.END)
        # BUGFIX : l'ancienne version n'utilisait que before=barre_commande,
        # sans side=BOTTOM (raisonnement d'origine : "before= seul suffit").
        # Insuffisant en pratique -- barre_cmd et zone (la console,
        # expand=True) sont TOUTES DEUX empaquetées AVANT que ce script ne
        # tourne (voir _build_terminal) ; sans side=BOTTOM ici, ce widget
        # est carvé côté TOP et finit coincé entre la barre d'onglets et la
        # console plutôt qu'au-dessus de la ligne de commande -- et sur une
        # fenêtre courte, la zone déjà expand=True peut ne rien laisser du
        # tout. side=BOTTOM + after=barre_commande (et non before=) le place
        # dans la cavité BOTTOM juste au-dessus de barre_cmd (le dernier
        # empaqueté côté BOTTOM se retrouve juste AU-DESSUS du précédent,
        # pas en dessous) avec son espace garanti, quelle que soit la taille
        # de la fenêtre -- même mécanisme que le réordonnancement de
        # _build_terminal, voir son commentaire.
        self.barre_saisie.pack(side=tk.BOTTOM, fill=tk.X, after=self.barre_commande)
        self.champ_saisie.config(state=tk.NORMAL)
        self.champ_saisie.focus_set()
        self._set_status("En attente de votre saisie…")
        self.prompt_commande.config(text="⏸ ❯", fg=T.AMBRE)

    def valider_saisie(self, _event=None):
        """Handler du bouton VALIDER / touche Entrée. Thread principal."""
        if not self.is_waiting_input:
            return
        valeur = self.champ_saisie.get()
        self._saisie_resultat["valeur"] = valeur
        self.log("→ %s" % (valeur if valeur.strip() else "(vide)"), "info")
        self._fermer_saisie()
        evenement = self._saisie_evenement
        if evenement is not None:
            # Libéré EN DERNIER : le thread d'exécution repart aussitôt et
            # peut réécrire dans la console. Fermer la barre après aurait
            # produit un ordre d'affichage aléatoire.
            evenement.set()

    def _fermer_saisie(self):
        self.is_waiting_input = False
        try:
            self.barre_saisie.pack_forget()
            self.prompt_commande.config(text="UVI ❯", fg=T.CYAN)
            self._set_status("Exécution en cours…")
        except tk.TclError:
            pass

    def pump_events(self):
        """Contrat attendu par UVIEvaluator._pump_ui_events (et par lui
        seul). N'existe que pour donner UN nom stable à "laisse Tk
        respirer", au lieu de laisser l'évaluateur connaître self.root.

        Ce n'est pas une couche d'indirection gratuite : c'est ce qui
        rend l'évaluateur exécutable hors IDE (CLI, tests, CI) -- un hôte
        sans cette méthode voit simplement les boucles ne rien pomper,
        au lieu de lever AttributeError sur .root.

        Le risque thread-safety documenté ci-dessous (key_is_down) n'est
        NI aggravé NI corrigé par ce déplacement : c'est le même
        root.update(), appelé depuis le même thread de script, juste
        derrière un nom. Le corriger vraiment (queue + root.after)
        reste hors périmètre, et est désormais localisable en UN endroit
        au lieu de quatre."""
        self.root.update()

    def key_is_down(self, key_name: str) -> bool:
        """KEY.DOWN -- appelé depuis le thread du script UVI (celui lancé
        par run_script via threading.Thread(target=_run), même thread
        que celui qui exécute _exec_while/_exec_for). root.update() AVANT
        la lecture, pour que _keys_down reflète l'état réel du clavier au
        moment de CET appel plutôt que de dépendre du pompage "tous les
        20 tours" déjà présent dans _exec_while/_exec_for (pensé pour une
        boucle de calcul long, pas pour ~30 appels/seconde) -- un jeu
        appelant KEY.DOWN à chaque frame veut une fraîcheur garantie à
        CE moment précis, pas une coïncidence avec ce compteur de 20.

        Note honnête : appeler root.update() hors du thread principal
        Tkinter n'est pas rigoureusement garanti thread-safe par Tk. Ce
        n'est cependant pas une invention isolée pour KEY.* -- c'est
        EXACTEMENT le même pattern que self.ide.root.update() déjà
        présent dans _exec_while/_exec_for (ligne ~4366/4384), appelé
        depuis ce même thread de script. Introduire ici une convention
        différente (ex. passer par une queue + root.after) créerait une
        incohérence entre deux mécanismes très proches dans le même
        projet, pour un risque déjà accepté ailleurs -- non traité dans
        cette passe, cohérent avec l'existant plutôt qu'une réécriture
        risquée de _exec_while/_exec_for hors du périmètre demandé."""
        self.pump_events()
        key = str(key_name).strip().lower()
        with self._keys_lock:
            return key in self._keys_down

    def key_was_pressed(self, key_name: str) -> bool:
        """KEY.PRESSED -- front montant, CONSOMMÉ à la lecture (retiré de
        _keys_pressed_edge une fois lu) : un appui physique ne doit
        déclencher qu'UNE seule fois "vrai", pas à chaque frame tant que
        la touche reste enfoncée -- sans quoi KEY.PRESSED serait
        strictement équivalent à KEY.DOWN et n'aurait aucune raison
        d'exister séparément."""
        self.pump_events()
        key = str(key_name).strip().lower()
        with self._keys_lock:
            if key in self._keys_pressed_edge:
                self._keys_pressed_edge.discard(key)
                return True
            return False

    def log(self, text: str, level: str = "info"):
        prefixes = {"info": "", "ok": "[OK] ", "err": "[ERREUR] ", "sys": "[SYS] "}
        # Le vert ne signifie plus « UVI » (tout l'était en v8) mais
        # « succès » : il redevient porteur d'information. La sortie
        # ordinaire passe en gris-bleu neutre, ce qui laisse ressortir les
        # lignes qui comptent vraiment.
        colors = {"info": T.TEXTE, "ok": T.VERT, "err": T.ROUGE, "sys": T.TEXTE_FAIBLE}
        prefix = prefixes.get(level, "")
        color = colors.get(level, "#00FF00")
        text_str = str(text)
        if len(text_str) > MAX_DISPLAY_LENGTH:
            text_str = text_str[:MAX_DISPLAY_LENGTH] + f"... [{len(text_str)} chars au total]"
        full_msg = f"{prefix}{text_str}\n"
        self.console.tag_configure(level, foreground=color)
        self.console.insert(tk.END, full_msg, level)
        self.console.see(tk.END)
        lines = int(self.console.index('end-1c').split('.')[0])
        if lines > MAX_CONSOLE_LINES:
            self.console.delete("1.0", "201.0")
            self.console.insert("1.0", f"[SYS] Terminal purgé (limite {MAX_CONSOLE_LINES} lignes).\n", "sys")
        if level == "err":
            # ÉTAPE 7.2 : le `kind` typé de UVIError était construit
            # proprement par _emit_error puis... uniquement affiché entre
            # crochets. Toute l'information de typage servait à colorer une
            # ligne, jamais à aider. On la relit ici pour attacher une
            # action concrète quand il y en a une.
            #
            # Extraction par regex sur le message plutôt que par un
            # paramètre supplémentaire à log() : log() est appelée depuis
            # des dizaines d'endroits (moteur, agent, VM, IDE) et lui
            # ajouter un argument obligerait à toucher tous ces appels pour
            # un bénéfice nul sur 95% d'entre eux. Le format "[KIND] ..."
            # est déjà garanti par UVIError.__str__.
            #
            # Insertion DIRECTE dans la console, pas un self.log() récursif :
            # un appel récursif relancerait tout ce bloc (speak, error_flag,
            # _highlight_error_line) pour une simple ligne d'aide.
            self._selectionner_onglet_console(self._onglet_console_actif) \
                if getattr(self, "_onglets_console", None) else None
            correspondance = re.match(r"^\[([A-Z_]+)\]", text_str)
            if correspondance:
                suggestion = ERROR_HINTS.get(correspondance.group(1))
                if suggestion:
                    self.console.tag_configure("hint", foreground="#FFCC00")
                    self.console.insert(tk.END, "        " + suggestion + "\n", "hint")
                    self.console.see(tk.END)
            if getattr(self, 'agent', None):
                self.agent.speak(text_str)
            self.engine.error_flag = True
            if self.engine.is_running and self.engine.current_run_line > 0:
                self._highlight_error_line(self.engine.current_run_line)

    def _rafraichir_panneaux(self):
        """Un seul point d'entrée pour remettre à jour les panneaux vivants.

        Replanifié tant que le script tourne : une boucle de jeu fait bouger
        les variables en continu, et un panneau figé sur l'état de départ
        serait pire qu'un panneau vide -- il afficherait des valeurs fausses
        avec l'assurance de valeurs justes."""
        self.rafraichir_variables()
        self.rafraichir_memoire()
        if getattr(self.engine, "is_running", False):
            self.root.after(700, self._rafraichir_panneaux)

    def _clear_console(self):
        self.console.delete("1.0", tk.END)
        self.log("Console effacée.", "sys")

    def basculer_execution(self):
        """Le bouton principal est un INTERRUPTEUR : EXÉCUTER quand rien ne
        tourne, ARRÊTER pendant l'exécution.

        Un seul bouton plutôt que deux : les deux actions sont mutuellement
        exclusives, et un bouton ARRÊTER grisé occuperait de la place pour
        ne rien dire 99 % du temps. L'utilisateur regarde toujours au même
        endroit."""
        if self.engine.is_running:
            self.arreter_script()
        else:
            self.run_script()

    def arreter_script(self):
        """Demande l'arrêt coopératif du script en cours."""
        if not self.engine.is_running:
            return
        self.engine.stop_requested = True
        self._set_status("Arrêt demandé…")
        self.log("⏹ Arrêt demandé — le script s'arrêtera à la prochaine "
                 "instruction.", "sys")
        # Une saisie en attente doit être débloquée, sinon le thread reste
        # suspendu sur son Event et l'arrêt n'a jamais lieu.
        if getattr(self, "is_waiting_input", False):
            self._saisie_resultat["valeur"] = ""
            self._fermer_saisie()
            if self._saisie_evenement is not None:
                self._saisie_evenement.set()

    def _mode_execution(self, actif: bool):
        """Bascule l'apparence du bouton principal."""
        try:
            if actif:
                self.btn_run.config(text=self._t("⏹  ARRÊTER"), bg="#B91C1C",
                                    activebackground=T.ROUGE,
                                    state=tk.NORMAL, cursor="hand2")
            else:
                self.btn_run.config(text=self._t("▶  EXÉCUTER"), bg=T.BLEU,
                                    activebackground=T.BLEU_VIF,
                                    state=tk.NORMAL, cursor="hand2")
        except tk.TclError:
            pass

    def run_script(self):
        if self.engine.is_running:
            return
        self.engine.stop_requested = False
        self.engine._stop_line = 0
        code = self.editor.get("1.0", tk.END)
        lines = code.splitlines()
        if not lines or lines[0].strip() != "@UVI":
            self.show_terminal()
            self.log("Signature @UVI absente.", "err")
            return
        # Le bouton devient ARRÊTER au lieu d'être grisé : c'est
        # précisément pendant l'exécution qu'on en a besoin.
        self._mode_execution(True)
        self.cmd_input.config(state=tk.DISABLED)
        self._debut_execution = time.time()
        # Même bascule automatique que run_script_vm : si le script
        # contient au moins un DRAW.*, on affiche le canevas plutôt que le
        # terminal -- détecté ici sur l'AST (le tree-walker n'a pas de
        # bytecode à inspecter comme la VM) AVANT de lancer le thread
        # d'exécution, donc sans risque de course avec _exec_draw.
        try:
            ast_nodes_preview = self.engine.parser.parse_program(lines[1:])
            has_drawing = any(isinstance(n, DrawNode) for n in ast_nodes_preview)
        except Exception:
            has_drawing = False
        self.show_canvas() if has_drawing else self.show_terminal()
        ts = datetime.now().strftime("%H:%M:%S")
        # Panneaux VARIABLES et MÉMOIRE remis à jour APRÈS l'exécution :
        # planifié par root.after plutôt qu'appelé ici, parce que
        # run_script lance le script dans un thread et que ces panneaux
        # doivent refléter l'état FINAL, pas l'état d'avant le départ.
        self.root.after(120, self._rafraichir_panneaux)
        # ÉTAPE 7.5 : la coexistence de deux moteurs était expliquée
        # uniquement dans les commentaires de run_script_vm -- c'est-à-dire
        # nulle part, pour quelqu'un qui utilise l'IDE sans lire son code
        # source. Affiché une fois par session, au moment où la question se
        # pose réellement (le premier clic), plutôt que noyé dans une doc.
        self._conseil_unique(
            "moteur_treewalker",
            "▶ EXÉCUTER utilise le moteur COMPLET (tree-walker) : il gère tout "
            "le langage, IMPORT et pipes | compris. C'est le bon choix par "
            "défaut. ⚡ EXÉCUTER (VM) compile en bytecode et va plus vite, mais "
            "ne couvre pas encore IMPORT ni les pipes.",
        )
        self.log(f"=== SÉQUENCE D'EXÉCUTION — {ts} ===", "sys")
        self._set_status("Exécution en cours…")
        if getattr(self, "shion_avatar", None) is not None:
            self.shion_avatar.react("TASK_STARTED")

        def _run():
            self.engine.run_sequence(lines[1:])
            self._poster_ui(self._on_script_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_script_finished(self):
        duree = time.time() - getattr(self, "_debut_execution", time.time())
        interrompu = getattr(self.engine, "stop_requested", False)
        self.log("=== FIN DE LA SÉQUENCE — %.2f s ===" % duree, "sys")
        # L'auto-réparation ne se déclenche PAS sur une interruption
        # volontaire : le script n'a rien de cassé, l'utilisateur a
        # simplement décidé d'arrêter. La lancer ici ferait travailler
        # l'agent (et consommer du réseau) pour rien.
        if self.engine.error_flag and self.agent and not interrompu:
            self.agent.auto_repair()
        self._set_status("Interrompu (%.2f s)." % duree if interrompu
                         else "Terminé en %.2f s." % duree)
        if interrompu:
            pass  # arrêt volontaire : aucune notification, l'utilisateur sait déjà
        elif self.engine.error_flag:
            self._empiler_toast("Le script s'est terminé en erreur.", "err")
            if getattr(self, "shion_avatar", None) is not None:
                self.shion_avatar.react("TASK_FAILED")
        else:
            if duree > 1.5:
                self._empiler_toast("Script terminé (%.1f s)." % duree, "ok")
            if getattr(self, "shion_avatar", None) is not None:
                self.shion_avatar.react("TASK_COMPLETED")
        # CORRIGÉ AU PASSAGE : ce bloc remettait le bouton en #00FF00 sur
        # #000000 -- les couleurs de l'ancien thème vert v8. Après la
        # première exécution, le bouton principal virait donc au vert fluo
        # au milieu d'une interface bleue. Résidu visible du changement de
        # thème, que seule une exécution réelle faisait apparaître.
        self._mode_execution(False)
        self.engine.stop_requested = False
        self.engine._stop_line = 0
        self.cmd_input.config(state=tk.NORMAL)
        self._rafraichir_panneaux()

    def run_script_vm(self):
        """Exécute le script courant via la VM Phase 3 (compiler_v2 +
        uvi_vm) au lieu du tree-walker (UVIEvaluator). Séparé de
        run_script() plutôt que substitué en place : la VM ne couvre pas
        encore ImportNode/PipeNode (cf. limites documentées dans
        uvi_vm.py) -- les deux moteurs coexistent pendant la période de
        validation de la VM, l'utilisateur choisit explicitement lequel
        utiliser via le bouton correspondant."""
        if not HAS_VM:
            self.log("VM indisponible : compiler_v2.py / uvi_vm.py introuvables.", "err")
            return
        if self.engine.is_running:
            return
        code = self.editor.get("1.0", tk.END)
        lines = code.splitlines()
        if not lines or lines[0].strip() != "@UVI":
            self.show_terminal()
            self.log("Signature @UVI absente.", "err")
            return
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            ast_nodes = self.engine.parser.parse_program(lines[1:])
            compiler = UVIBytecodeCompilerV2()
            bytecode = compiler.compile_program(ast_nodes)
        except BytecodeError as e:
            self.show_terminal()
            self.log(f"Erreur de compilation VM : {e}", "err")
            return

        # Bascule vers l'onglet CANVAS si le script dessine effectivement
        # quelque chose (au moins un CALL_BUILTIN DRAW_*) -- sinon on garde
        # le comportement existant (terminal), un script purement textuel
        # ne doit pas surprendre l'utilisateur en changeant de vue.
        has_drawing = any(
            i["opcode"] == "CALL_BUILTIN" and i["operands"] and str(i["operands"][0]).startswith("DRAW_")
            for i in bytecode["instructions"]
        )
        if has_drawing:
            self.show_canvas()
        else:
            self.show_terminal()
        self.root.after(120, self._rafraichir_panneaux)
        self._conseil_unique(
            "moteur_vm",
            "⚡ EXÉCUTER (VM) compile le script en bytecode : plus rapide sur les "
            "boucles longues, mais IMPORT et les pipes | ne sont pas encore "
            "couverts et lèveront une erreur de compilation. Si un script "
            "refuse de compiler ici, réessayez avec ▶ EXÉCUTER. Faire tourner "
            "le même script dans les deux moteurs est aussi la meilleure façon "
            "de vérifier qu'ils restent d'accord.",
        )
        self.log(f"=== EXÉCUTION VM — {ts} ===", "sys")

        def shion_ask_fn(prompt: str) -> str:
            if not HAS_SHION or self.engine.shion_client is None:
                raise RuntimeError("SHION.ASK indisponible dans ce contexte VM.")
            return self.engine.shion_client.complete(prompt)

        vm = UVIVirtualMachine(
            vfs=self.vfs,
            shion_ask_fn=shion_ask_fn if HAS_SHION else None,
            input_fn=lambda var: self.request_input(var),
            draw_fn=self.canvas_draw,
            key_down_fn=self.key_is_down,
            key_pressed_fn=self.key_was_pressed,
            # Sans ces trois kwargs, la VM retombe sur ses défauts de classe
            # (max_string_length=1 000 000, max_call_depth=100,
            # max_array_size=10 000) -- très au-dessus des plafonds du
            # tree-walker (MAX_STRING_LENGTH=10 000, MAX_CALL_DEPTH=50,
            # MAX_ARRAY_SIZE=1 000, voir plus haut dans ce fichier). Un même
            # script pouvait donc dépasser silencieusement la limite dans
            # UN moteur mais pas l'autre -- exactement le genre d'écart que
            # _lancer_tests (onglet TESTS) est censé détecter, sauf que
            # cette divergence-là ne se manifeste que sur un script qui
            # pousse délibérément une chaîne/récursion/tableau au-delà du
            # plafond, donc rarement rencontrée par hasard. Faire pointer
            # les deux moteurs vers LES MÊMES constantes élimine la dérive,
            # plutôt que de la corriger une fois constatée.
            max_string_length=MAX_STRING_LENGTH,
            max_call_depth=MAX_CALL_DEPTH,
            max_array_size=MAX_ARRAY_SIZE,
        )

        # BUGFIX (gel de l'IDE, + deadlock sur ?INPUT) : vm.run() était
        # appelée ICI, directement sur le thread principal Tkinter. C'est
        # une boucle Python bloquante, qui en plus peut attendre un réseau
        # (SHION.ASK -> shion_ask_fn -> DEEPSEEK_REQUEST_TIMEOUT_S=180s) ou
        # une saisie utilisateur (?INPUT -> request_input). request_input
        # POSTE l'ouverture de sa barre de saisie via _poster_ui, qui n'est
        # vidée que par _pomper_file_ui -- qui elle-même ne tourne que si
        # la boucle d'événements Tk est libre. Appelée depuis LE thread Tk,
        # elle attendait donc (.wait(timeout=600)) une réponse qui ne
        # pouvait jamais arriver : deadlock de 10 minutes garanti sur tout
        # script VM utilisant ?, et gel simple (sans deadlock) sur tout
        # script sans ? mais un peu long ou utilisant SHION.ASK.
        #
        # Correctif : même schéma que run_script (tree-walker) -- vm.run()
        # part dans un thread daemon, is_running sert de verrou de
        # ré-entrance (comme pour run_sequence), et tout ce qui touche
        # Tkinter après coup (log, statut, panneaux, bouton) repasse par
        # _poster_ui via _on_vm_finished, jamais appelé directement depuis
        # ce thread. draw_fn (canvas_draw) et input_fn (request_input)
        # passent déjà par _poster_ui en interne -- ce sont eux qui
        # deviennent enfin SÛRS une fois appelés depuis un vrai thread
        # secondaire, comme ils étaient conçus pour l'être.
        #
        # LIMITE ASSUMÉE (non traitée ici) : UVIVirtualMachine.run() ne lit
        # stop_requested nulle part dans sa boucle -- le bouton ARRÊTER
        # reste donc sans effet sur un script VM en cours, contrairement au
        # tree-walker. C'était déjà vrai avant ce correctif (juste invisible,
        # l'IDE entière étant gelée) ; le rendre coopératif demande de
        # toucher la boucle interne de la VM, hors périmètre de ce correctif
        # de thread.
        self.engine.is_running = True
        self._mode_execution(True)
        self.cmd_input.config(state=tk.DISABLED)
        self._debut_execution_vm = time.time()

        def _run():
            try:
                resultat = vm.run(bytecode)
            except Exception as e:
                # `except ... as e` supprime automatiquement `e` à la sortie
                # de ce bloc (voir la même remarque, déjà faite, dans
                # _shion_worker) -- la lambda ci-dessous n'est appelée que
                # plus tard, via _poster_ui, APRÈS cette suppression. On
                # rebinde donc l'exception à un nom ordinaire AVANT de
                # construire la closure, pour ne pas répéter le même bug.
                exc = e
                _signaler_crash(exc, source="vm", error_code="VM_CRASH")
                self._poster_ui(lambda: self._on_vm_finished(None, exc))
                return
            self._poster_ui(lambda: self._on_vm_finished(resultat, None))

        threading.Thread(target=_run, daemon=True).start()

    def _on_vm_finished(self, result, exc):
        """Callback de fin d'exécution VM, TOUJOURS invoqué sur le thread
        principal via _poster_ui (voir run_script_vm) -- jamais appelé
        directement depuis le thread worker."""
        duree = time.time() - getattr(self, "_debut_execution_vm", time.time())
        self.engine.is_running = False
        self._mode_execution(False)
        self.cmd_input.config(state=tk.NORMAL)
        if exc is not None:
            self.log(f"Erreur VM inattendue : {type(exc).__name__} : {exc}", "err")
            self._empiler_toast("Exécution VM interrompue par une erreur.", "err")
        else:
            for line in result.output:
                self.log(str(line))
            if result.trapped:
                self.log(f"Erreur VM [{result.trapped}] : {result.trap_message}", "err")
                self._empiler_toast("Erreur VM : %s" % result.trapped, "err")
            elif duree > 1.5:
                # Notification seulement si l'exécution a duré assez pour
                # que l'utilisateur ait plausiblement fait autre chose
                # entre-temps -- pas de bruit sur un script instantané.
                self._empiler_toast("Script terminé (VM, %.1f s)." % duree, "ok")
        self.log("=== FIN D'EXÉCUTION VM — %.2f s ===" % duree, "sys")
        self._set_status("Terminé (VM, %.2f s)." % duree)
        self._rafraichir_panneaux()

    def run_command(self, event=None):
        # La saisie de script ne passe PLUS par ici : elle a sa propre
        # barre (voir request_input). Ce champ redevient ce qu'il prétend
        # être depuis toujours, un REPL -- un champ, un rôle.
        if self.is_waiting_input:
            # Filet : si l'utilisateur tape ici par réflexe pendant qu'une
            # saisie est demandée, on redirige plutôt que d'ignorer en
            # silence. Ignorer donnerait l'impression d'un clavier mort.
            texte = self.cmd_input.get()
            if texte.strip():
                self.champ_saisie.delete(0, tk.END)
                self.champ_saisie.insert(0, texte)
                self.cmd_input.delete(0, tk.END)
                self.valider_saisie()
            else:
                self.champ_saisie.focus_set()
            return
        if self.engine.is_running:
            return
        cmd = self.cmd_input.get().strip()
        if not cmd:
            return
        self.cmd_history.append(cmd)
        self.history_index = len(self.cmd_history)
        self.log(f"UVI> {cmd}", "sys")
        self.cmd_input.delete(0, tk.END)

        # BUGFIX (gel de l'IDE) : une commande REPL peut contenir !ERIS(...),
        # évalué en EXPRESSION par _eval_expr_or_eris -- lequel appelle
        # client.complete(...) de façon SYNCHRONE (requête réseau, jusqu'à
        # DEEPSEEK_REQUEST_TIMEOUT_S=180s si SHION répond à ERIS en
        # priorité, cf. llm_client_prioritaire). SHION.ASK en tant
        # qu'INSTRUCTION dédiée est déjà non-bloquant (_exec_shion, thread
        # daemon) ; ce chemin-ci, tapé directement dans la console, ne
        # l'était pas. Même correctif que run_script/run_script_vm :
        # is_running sert de verrou de ré-entrance partagé entre les trois
        # points d'entrée (ils touchent le même moteur, mutable).
        self.engine.is_running = True

        def _run():
            try:
                self.engine.run_single_command(cmd)
            finally:
                self.engine.is_running = False
                self._poster_ui(self._rafraichir_panneaux)

        threading.Thread(target=_run, daemon=True).start()

    def _history_up(self, event=None):
        if self.history_index > 0:
            self.history_index -= 1
            self._fill_input(self.cmd_history[self.history_index])

    def _history_down(self, event=None):
        if self.history_index < len(self.cmd_history) - 1:
            self.history_index += 1
            self._fill_input(self.cmd_history[self.history_index])
        else:
            self.history_index = len(self.cmd_history)
            self.cmd_input.delete(0, tk.END)

    def _fill_input(self, text: str):
        if self.cmd_input['state'] == tk.DISABLED:
            return
        self.cmd_input.delete(0, tk.END)
        self.cmd_input.insert(0, text)

    def _set_status(self, msg: str):
        """Garde son nom et sa signature (13 appels ailleurs). Le voyant
        change de couleur selon le contenu -- une information de plus sans
        rien demander aux appelants existants."""
        self.statusbar.config(text=" %s" % msg)
        voyant = getattr(self, "voyant_etat", None)
        if voyant is None:
            return
        minuscule = str(msg).lower()
        if any(mot in minuscule for mot in ("erreur", "échec", "echec", "interrompu")):
            couleur, teinte = T.ROUGE, T.ROUGE
        elif any(mot in minuscule for mot in ("cours", "exécution", "execution", "agent")):
            couleur, teinte = T.AMBRE, T.AMBRE
        else:
            couleur, teinte = T.VERT, T.VERT
        try:
            self.statusbar.config(fg=teinte)
        except tk.TclError:
            pass
        # Le voyant ne se contente plus d'une couleur fixe : il RESPIRE --
        # un battement lent et discret au repos, plus rapide pendant une
        # exécution -- plutôt que le point statique des barres d'état
        # habituelles. C'est délibérément subtil (l'amplitude reste faible,
        # voir _animer_voyant) : l'objectif est de donner une sensation
        # continue que l'atelier est "vivant", pas de distraire l'œil.
        self._animer_voyant(couleur)

    def _animer_voyant(self, couleur_base: str):
        """(Re)démarre le pouls du voyant d'état autour de `couleur_base`.

        Un seul cycle d'animation à la fois : l'identifiant `after()`
        précédent est annulé avant d'en programmer un nouveau, sinon
        chaque appel à _set_status empilerait une boucle .after()
        supplémentaire -- une fuite lente mais réelle sur une session
        longue avec beaucoup de changements de statut."""
        voyant = getattr(self, "voyant_etat", None)
        if voyant is None:
            return
        ancien = getattr(self, "_voyant_anim_id", None)
        if ancien is not None:
            try:
                self.root.after_cancel(ancien)
            except tk.TclError:
                pass
        self._voyant_anim_phase = 0.0
        # Battement rapide pendant une exécution (retour visuel immédiat
        # que "quelque chose tourne"), lent et discret au repos.
        rapide = getattr(self.engine, "is_running", False)
        pas = 0.09 if rapide else 0.025
        intervalle_ms = 45
        import math  # local à la méthode, pas au cycle -- cf. la même convention pour BINOP/POW dans la VM

        def _cycle():
            try:
                self._voyant_anim_phase = (self._voyant_anim_phase + pas) % (2 * 3.14159265)
                # (sin+1)/2 -> [0,1], mais borné à [0.15, 0.55] : le voyant
                # ne s'éteint jamais complètement et ne sature jamais à
                # fond, pour rester un battement plutôt qu'un clignotement.
                intensite = 0.15 + 0.40 * (0.5 * (1 + math.sin(self._voyant_anim_phase)))
                teinte = _melanger_couleurs(T.PANNEAU, couleur_base, intensite + 0.45)
                voyant.delete("all")
                voyant.create_oval(1, 1, 9, 9, fill=teinte, outline="")
                self._voyant_anim_id = self.root.after(intervalle_ms, _cycle)
            except tk.TclError:
                pass  # fenêtre fermée pendant l'animation

        _cycle()

    def _on_close(self):
        """Confirme avant de fermer si du travail n'est pas enregistré.

        La version précédente fermait sans rien demander : un clic sur la
        croix effaçait un script non sauvegardé, sans recours. C'est le
        genre de perte qu'on ne pardonne pas à un outil."""
        if getattr(self, "modifie", False):
            reponse = messagebox.askyesnocancel(
                "Quitter UVI",
                "Le fichier « %s » contient des modifications non "
                "enregistrées.\n\nSauvegarder avant de quitter ?"
                % (self.fichier_courant or "sans_titre.uvi"))
            if reponse is None:
                return  # Annuler : on ne ferme pas
            if reponse:
                self.save_to_disk()
                if self.modifie:
                    return  # la sauvegarde a échoué ou été annulée
        # Un script encore en cours doit être prié de s'arrêter : son
        # thread est daemon, donc il mourra de toute façon, mais lui
        # demander proprement évite qu'il écrive dans des widgets détruits
        # pendant la fermeture.
        if getattr(self.engine, "is_running", False):
            self.engine.stop_requested = True
        if getattr(self, "shion_avatar", None) is not None:
            self.shion_avatar.arreter()
        # Sous-processus llama.cpp lancé par _definir_modele_local (le cas
        # échéant) : sans cet arrêt explicite, il continuerait à tourner
        # après la fermeture de l'IDE -- un serveur invisible qui consomme
        # RAM/CPU sans qu'on sache pourquoi, jusqu'au prochain redémarrage.
        if getattr(self, "_processus_llm_local", None) is not None:
            self._arreter_modele_local(silencieux=True)
        self.root.destroy()

    def start(self):
        self.root.mainloop()

    def _on_key_release_handler(self, event=None):
        self._update_line_numbers(event)
        if not getattr(self, 'agent', None):
            return
        try:
            if event and getattr(event, 'keysym', '') in ["space", "Return", "BackSpace", "Tab"]:
                self.statusbar.config(text=" Prêt.", fg="#444444")
                self.current_suggestion = None
                if event.keysym in ["space", "Return", "Tab"]:
                    return
            cursor_index = self.editor.index(tk.INSERT)
            line, col = cursor_index.split('.')
            line_text = self.editor.get(f"{line}.0", cursor_index)
            if not line_text or line_text[-1] in [" ", "\t", "\n"]:
                self.statusbar.config(text=" Prêt.", fg="#444444")
                self.current_suggestion = None
                return
            words = line_text.split()
            if words:
                last_word = words[-1]
                suggestion = self.agent.get_auto_suggestion(last_word)
                if suggestion:
                    self.statusbar.config(
                        text=f" 💡 IA : Appuyez sur [Tab] ou [Ctrl+Espace] pour insérer '{suggestion}'",
                        fg="#FFFF00"
                    )
                    self.current_suggestion = (suggestion, last_word)
                else:
                    self.statusbar.config(text=" Prêt.", fg="#444444")
                    self.current_suggestion = None
        except Exception:
            pass

    def _insert_suggestion(self, event=None):
        if getattr(self, 'current_suggestion', None):
            suggestion, last_word = self.current_suggestion
            cursor_index = self.editor.index(tk.INSERT)
            start_index = f"{cursor_index} - {len(last_word)} chars"
            self.editor.delete(start_index, cursor_index)
            self.editor.insert(start_index, suggestion)
            self.statusbar.config(text=" Prêt.", fg="#444444")
            self.current_suggestion = None
            return "break"
        return None


if __name__ == "__main__":
    # AVANT UVIIDE() : c'est le mutex que uvi_installer.iss (AppMutex=...)
    # surveille pour fermer proprement une instance en cours lors d'une
    # auto-mise-à-jour silencieuse (voir uvi_updater.hold_app_mutex).
    uvi_updater.hold_app_mutex()
    app = UVIIDE()
    app.start()
