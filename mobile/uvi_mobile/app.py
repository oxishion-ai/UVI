"""UVIMobileApp — l'IDE UVI pour Android et iOS (Kivy).

Réutilise TEL QUEL le moteur de uvi_engine.py : même lexer, même parseur,
même évaluateur, même VM bytecode que l'IDE de bureau. Seule l'interface est
réécrite, Tkinter n'existant sur aucune des deux plateformes mobiles. Un
script produit donc exactement la même sortie ici et sur ordinateur -- c'est
tout l'intérêt d'avoir séparé le moteur plutôt que de le réimplémenter.

MODÈLE D'EXÉCUTION, identique à celui du bureau (UVIIDE.run_script) :
le script tourne dans un thread daemon, l'interface reste vivante, et le
bouton EXÉCUTER devient ARRÊTER pendant ce temps -- c'est précisément
pendant l'exécution qu'on en a besoin. Aucun widget n'est touché depuis ce
thread : tout passe par UVIMobileHost, qui reposte vers le thread Kivy.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.graphics import Color, Rectangle

import uvi_engine as moteur_uvi

from .host import UVIMobileHost
from .theme import T, rgba
from .widgets.canvas import UVICanvas
from .widgets.console import Console
from .widgets.editor import EditeurUVI
from .widgets.explorer import Explorateur
from .widgets.keypad import PaveTactile

SCRIPT_EXEMPLE = """@UVI
# Bienvenue dans UVI mobile !
# Appuie sur EXECUTER pour lancer ce script.

! "Bonjour depuis UVI !"

Total = 0
% 5 -> Total = Total + 10
! "Total apres la boucle : {Total}"

DEF Carre(n) -> _retour = n * n
CALL Carre(7) -> R
! "7 au carre = {R}"

# Un dessin : bascule sur l'onglet CANEVAS
# Noter l'absence de fleche apres le nom de la commande : le premier
# operande la suit directement, sinon il est lu comme vide et TOUS les
# suivants sont decales d'un cran.
DRAW.CLEAR
DRAW.RECT 80 -> 80 -> 240 -> 160 -> "#3B82F6" -> "#3B82F6"
DRAW.CIRCLE 500 -> 160 -> 90 -> "#22D3EE" -> "#22D3EE"
DRAW.LINE 80 -> 380 -> 720 -> 380 -> "#A855F7" -> 4
DRAW.TEXT 80 -> 430 -> "UVI dessine !" -> "#EAF2FF" -> 34
"""


def _fond(widget, couleur):
    """Peint un fond uni derrière un widget Kivy (qui est transparent par
    défaut). Rebranché sur pos/size pour survivre aux rotations d'écran."""
    with widget.canvas.before:
        Color(*rgba(couleur))
        rect = Rectangle(pos=widget.pos, size=widget.size)

    def _maj(*_a):
        rect.pos = widget.pos
        rect.size = widget.size

    widget.bind(pos=_maj, size=_maj)
    return rect


class BarreEtat(Label):
    """Ligne de statut en bas de l'écran (équivalent de _set_status)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("text", "Prêt.")
        kwargs.setdefault("color", rgba(T.TEXTE_FAIBLE))
        kwargs.setdefault("font_size", "12sp")
        kwargs.setdefault("halign", "left")
        kwargs.setdefault("valign", "middle")
        kwargs.setdefault("size_hint_y", None)
        kwargs.setdefault("height", dp(26))
        super().__init__(**kwargs)
        self.bind(size=lambda i, v: setattr(i, "text_size", (v[0] - dp(16), v[1])))

    def definir(self, message: str):
        self.text = str(message)


class UVIMobileApp(App):
    """Application Kivy."""

    title = "UVI"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hote = UVIMobileHost()
        self.vfs = None
        self.moteur = None
        self.fichier_courant = None
        self._debut_execution = 0.0
        self._vues = {}
        self._onglet_actif = "editeur"

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def build(self):
        Window.clearcolor = rgba(T.FOND)

        # --- moteur --------------------------------------------------------
        # user_data_dir est le SEUL emplacement inscriptible garanti sur
        # Android comme sur iOS (le dossier de l'application, lui, est en
        # lecture seule) : le VFS y est ancré explicitement, là où le bureau
        # se contente du "UVI_Storage" relatif au dossier courant.
        dossier = os.path.join(self.user_data_dir, "UVI_Storage")
        self.vfs = moteur_uvi.VFSManager(dossier)
        self.vfs.package_registry = moteur_uvi.UVIPackageRegistry(self.vfs.storage_dir)
        self.vfs.package_registry.ensure_stdlib_installed(moteur_uvi.STDLIB_SOURCES)
        self.moteur = moteur_uvi.UVIEngine(self.hote, self.vfs)

        racine = BoxLayout(orientation="vertical")
        racine.add_widget(self._construire_barre_haut())

        self.zone = BoxLayout()
        racine.add_widget(self.zone)

        self.barre_saisie = self._construire_barre_saisie()
        racine.add_widget(self.barre_saisie)

        self.barre_etat = BarreEtat()
        racine.add_widget(self.barre_etat)
        racine.add_widget(self._construire_onglets())

        # --- vues -----------------------------------------------------------
        self.editeur = EditeurUVI(text=SCRIPT_EXEMPLE)
        self.console = Console()
        self.canevas_vue, self.canevas, self.pave = self._construire_vue_canevas()
        self.explorateur = Explorateur(
            self.vfs, on_ouvrir=self.ouvrir_fichier,
            on_supprimer=self.supprimer_fichier)
        self._vues = {
            "editeur": self.editeur,
            "console": self.console,
            "canevas": self.canevas_vue,
            "fichiers": self.explorateur,
        }

        self.hote.attacher(
            console=self.console, canevas=self.canevas,
            explorateur=self.explorateur, barre_etat=self.barre_etat,
            clavier=self.pave, demande_saisie=self._ouvrir_saisie,
        )

        self._afficher("editeur")
        self._masquer_saisie()
        return racine

    def _construire_barre_haut(self):
        barre = BoxLayout(orientation="horizontal", size_hint_y=None,
                          height=dp(52), padding=(dp(8), dp(6)), spacing=dp(6))
        _fond(barre, T.PANNEAU_HAUT)

        titre = Label(text="[b]UVI[/b]  [size=11sp]v%s[/size]" % moteur_uvi.UVI_VERSION,
                      markup=True, color=rgba(T.CYAN), font_size="18sp",
                      size_hint_x=None, width=dp(96), halign="left", valign="middle")
        titre.bind(size=lambda i, v: setattr(i, "text_size", v))
        barre.add_widget(titre)

        # Libellés en TEXTE et non en pictogrammes (▶, ⚡, 💾) : la police par
        # défaut de Kivy (Roboto) ne contient ni les formes géométriques ni
        # les émojis, qui s'affichent alors en carrés vides -- vérifié à
        # l'écran. Embarquer une police qui les couvre alourdirait l'APK pour
        # un gain purement décoratif.
        self.btn_executer = Button(
            text="EXÉCUTER", font_size="15sp", bold=True,
            background_normal="", background_down="",
            background_color=rgba(T.BLEU), color=(1, 1, 1, 1))
        self.btn_executer.bind(on_release=lambda _b: self.basculer_execution())
        barre.add_widget(self.btn_executer)

        self.btn_vm = Button(
            text="VM", font_size="14sp", bold=True, size_hint_x=None, width=dp(60),
            background_normal="", background_down="",
            background_color=rgba(T.PANNEAU), color=rgba(T.CYAN))
        self.btn_vm.bind(on_release=lambda _b: self.executer(vm=True))
        barre.add_widget(self.btn_vm)

        self.btn_sauver = Button(
            text="ENREG.", font_size="12sp", size_hint_x=None, width=dp(72),
            background_normal="", background_down="",
            background_color=rgba(T.PANNEAU), color=rgba(T.TEXTE))
        self.btn_sauver.bind(on_release=lambda _b: self.sauvegarder())
        barre.add_widget(self.btn_sauver)
        return barre

    def _construire_vue_canevas(self):
        boite = BoxLayout(orientation="vertical")
        canevas = UVICanvas()
        boite.add_widget(canevas)
        pave = PaveTactile(size_hint_y=None, height=dp(180))
        _fond(pave, T.PANNEAU)
        boite.add_widget(pave)
        return boite, canevas, pave

    def _construire_barre_saisie(self):
        barre = BoxLayout(orientation="horizontal", size_hint_y=None,
                          height=dp(48), padding=(dp(8), dp(4)), spacing=dp(6))
        _fond(barre, T.PANNEAU)
        self.etiquette_saisie = Label(
            text="?", color=rgba(T.CYAN), font_size="14sp", bold=True,
            size_hint_x=None, width=dp(110), halign="left", valign="middle")
        self.etiquette_saisie.bind(
            size=lambda i, v: setattr(i, "text_size", v))
        barre.add_widget(self.etiquette_saisie)

        self.champ_saisie = TextInput(
            multiline=False, font_size="15sp",
            background_color=rgba(T.CHAMP), foreground_color=rgba(T.TEXTE_FORT),
            cursor_color=rgba(T.CYAN))
        self.champ_saisie.bind(on_text_validate=lambda _i: self._valider_saisie())
        barre.add_widget(self.champ_saisie)

        valider = Button(text="OK", size_hint_x=None, width=dp(64), bold=True,
                         background_normal="", background_down="",
                         background_color=rgba(T.VERT), color=(1, 1, 1, 1))
        valider.bind(on_release=lambda _b: self._valider_saisie())
        barre.add_widget(valider)
        return barre

    def _construire_onglets(self):
        barre = BoxLayout(orientation="horizontal", size_hint_y=None,
                          height=dp(54), spacing=dp(2))
        _fond(barre, T.PANNEAU_HAUT)
        self._boutons_onglet = {}
        for cle, libelle in (("editeur", "Éditeur"), ("console", "Console"),
                             ("canevas", "Canevas"), ("fichiers", "Fichiers")):
            bouton = Button(
                text=libelle, font_size="13sp",
                background_normal="", background_down="",
                background_color=rgba(T.PANNEAU_HAUT), color=rgba(T.TEXTE_FAIBLE))
            bouton.bind(on_release=lambda _b, c=cle: self._afficher(c))
            self._boutons_onglet[cle] = bouton
            barre.add_widget(bouton)
        return barre

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _afficher(self, cle: str):
        if cle not in self._vues:
            return
        self.zone.clear_widgets()
        self.zone.add_widget(self._vues[cle])
        self._onglet_actif = cle
        for c, bouton in self._boutons_onglet.items():
            actif = (c == cle)
            bouton.background_color = rgba(T.SURVOL if actif else T.PANNEAU_HAUT)
            bouton.color = rgba(T.CYAN if actif else T.TEXTE_FAIBLE)

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def basculer_execution(self):
        """Le bouton EXÉCUTER devient ARRÊTER pendant l'exécution (même
        parti pris que UVIIDE._mode_execution : un bouton grisé pendant
        qu'un script tourne serait précisément l'inverse du besoin)."""
        if self.moteur.is_running:
            self.arreter()
        else:
            self.executer()

    def executer(self, vm: bool = False):
        if self.moteur.is_running:
            return
        lignes = self.editeur.lignes
        if not lignes or lignes[0].strip() != "@UVI":
            self._afficher("console")
            self.hote.log("Signature @UVI absente.", "err")
            return

        self.moteur.stop_requested = False
        self.moteur._stop_line = 0
        self.pave.reinitialiser()
        self.canevas.effacer()

        # Bascule automatique vers le canevas si le script dessine -- même
        # détection que UVIIDE.run_script, faite sur l'AST AVANT de lancer le
        # thread, donc sans course avec _exec_draw.
        try:
            noeuds = self.moteur.parser.parse_program(lignes[1:])
            dessine = any(isinstance(n, moteur_uvi.DrawNode) for n in noeuds)
        except Exception:
            dessine = False
        self._afficher("canevas" if dessine else "console")

        self._mode_execution(True)
        self._debut_execution = time.time()
        horodatage = datetime.now().strftime("%H:%M:%S")
        self.hote.log("=== SÉQUENCE D'EXÉCUTION — %s ===" % horodatage, "sys")
        self.barre_etat.definir("Exécution en cours…")

        corps = lignes[1:]
        if vm:
            threading.Thread(target=self._executer_vm, args=(corps,), daemon=True).start()
        else:
            threading.Thread(target=self._executer_treewalker, args=(corps,), daemon=True).start()

    def _executer_treewalker(self, corps):
        try:
            self.moteur.run_sequence(corps)
        finally:
            Clock.schedule_once(lambda _dt: self._fin_execution(), 0)

    def _executer_vm(self, corps):
        """Chemin VM bytecode -- même appel que UVIIDE._lancer_tests /
        run_script_vm : compile_and_run avec le parseur et le compilateur du
        moteur, la sortie étant renvoyée par callbacks vers l'hôte."""
        try:
            resultat = moteur_uvi.compile_and_run(
                corps,
                moteur_uvi.UVIParser(moteur_uvi.UVILexer()),
                moteur_uvi.UVIBytecodeCompilerV2(),
                vfs=self.vfs,
                print_fn=lambda v: self.hote.log(str(v), "info"),
                draw_fn=self.hote.canvas_draw,
                input_fn=self.hote.request_input,
                key_down_fn=self.hote.key_is_down,
                key_pressed_fn=self.hote.key_was_pressed,
            )
            if getattr(resultat, "trapped", None):
                self.hote.log("[%s] %s" % (resultat.trapped,
                                           getattr(resultat, "trap_message", "")), "err")
        except Exception as e:
            self.hote.log("Erreur VM : %s" % e, "err")
        finally:
            Clock.schedule_once(lambda _dt: self._fin_execution(), 0)

    def _fin_execution(self):
        duree = time.time() - self._debut_execution
        interrompu = bool(getattr(self.moteur, "stop_requested", False))
        self.hote.log("=== FIN DE LA SÉQUENCE — %.2f s ===" % duree, "sys")
        self.barre_etat.definir("Interrompu (%.2f s)." % duree if interrompu
                                else "Terminé en %.2f s." % duree)
        self._mode_execution(False)
        self._masquer_saisie()

    def arreter(self):
        self.moteur.stop_requested = True
        # Débloque un éventuel ?nom en attente : sans ça, un script arrêté
        # pendant une saisie laisserait son thread suspendu sur l'Event
        # jusqu'au timeout de dix minutes.
        self.hote.annuler_saisie()
        self.barre_etat.definir("Arrêt demandé…")

    def _mode_execution(self, en_cours: bool):
        self.btn_executer.text = "ARRÊTER" if en_cours else "EXÉCUTER"
        self.btn_executer.background_color = rgba(T.ROUGE if en_cours else T.BLEU)
        self.btn_vm.disabled = en_cours

    # ------------------------------------------------------------------
    # Saisie utilisateur (?nom)
    # ------------------------------------------------------------------

    def _ouvrir_saisie(self, nom_variable: str):
        self.etiquette_saisie.text = "?  %s  =" % nom_variable
        self.champ_saisie.text = ""
        self.barre_saisie.height = dp(48)
        self.barre_saisie.opacity = 1
        self.barre_saisie.disabled = False
        if self._onglet_actif not in ("console", "canevas"):
            self._afficher("console")
        self.champ_saisie.focus = True

    def _valider_saisie(self):
        valeur = self.champ_saisie.text
        self._masquer_saisie()
        self.hote.fournir_saisie(valeur)

    def _masquer_saisie(self):
        self.barre_saisie.height = 0
        self.barre_saisie.opacity = 0
        self.barre_saisie.disabled = True

    # ------------------------------------------------------------------
    # Fichiers
    # ------------------------------------------------------------------

    def ouvrir_fichier(self, nom: str):
        contenu = self.vfs.read_file(nom)
        if contenu is None:
            self.hote.log("Fichier introuvable : %s" % nom, "err")
            return
        self.editeur.text = contenu
        self.fichier_courant = nom
        self.barre_etat.definir("Ouvert : %s" % nom)
        self._afficher("editeur")

    def supprimer_fichier(self, nom: str):
        if self.vfs.delete_file(nom):
            self.barre_etat.definir("Supprimé : %s" % nom)
            if self.fichier_courant == nom:
                self.fichier_courant = None
        self.explorateur.rafraichir()

    def sauvegarder(self):
        nom = self.fichier_courant or "script_%s.uvi" % datetime.now().strftime("%H%M%S")
        self.vfs.write_file(nom, self.editeur.text)
        self.fichier_courant = nom
        self.barre_etat.definir("Enregistré : %s" % nom)
        self.explorateur.rafraichir()

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def on_pause(self):
        """Android/iOS suspendent l'application quand elle passe en arrière-
        plan. Renvoyer True demande à la conserver en mémoire plutôt que de
        la tuer -- sans quoi un script en cours et le contenu non enregistré
        de l'éditeur seraient perdus à chaque appel entrant."""
        return True

    def on_resume(self):
        pass
