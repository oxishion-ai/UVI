"""UVIMobileHost — implémentation Kivy du contrat « hôte » du moteur UVI.

Le moteur (uvi_engine.py) ne connaît son environnement qu'à travers un objet
`self.ide` dont il n'utilise que neuf membres. HeadlessHost en est la version
minimale (tests), UVIIDE la version Tkinter (bureau), cette classe la version
Kivy (Android/iOS). Aucune ligne d'interpréteur n'est dupliquée : c'est le
MÊME uvi_engine qui tourne sur les trois.

DEUX RÈGLES DE THREAD, non négociables (mêmes que côté bureau, pour les mêmes
raisons) :

1. Le script tourne dans un thread de fond, JAMAIS dans le thread Kivy --
   sinon un `%% 1 -> ...` infini figerait toute l'interface, y compris le
   bouton STOP censé l'arrêter.
2. Ce thread ne touche AUCUN widget directement. Tout passe par
   `Clock.schedule_once`, seul point d'entrée thread-safe de Kivy. C'est
   l'exact équivalent de _poster_ui côté Tkinter, et pour la même raison :
   manipuler un widget depuis un thread secondaire est le genre de faute qui
   « marche souvent », donc qui casse de façon non reproductible chez
   l'utilisateur.

request_input bloque le thread d'exécution sur un threading.Event pendant que
l'interface affiche le champ de saisie -- calqué sur UVIIDE.request_input,
timeout compris (une fenêtre fermée pendant l'attente ne doit pas laisser un
thread suspendu à jamais).
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

from kivy.clock import Clock


class UVIMobileHost:
    """Hôte Kivy. Les widgets sont branchés après coup par UVIMobileApp
    (`attacher(...)`) : l'hôte est créé avant eux pour pouvoir être passé au
    constructeur de UVIEngine, et doit rester utilisable — en dégradant
    silencieusement — tant qu'ils n'existent pas."""

    #: Attente maximale d'une saisie utilisateur, en secondes. Même valeur
    #: que UVIIDE.request_input : au-delà, on rend "" plutôt que de laisser
    #: le thread bloqué indéfiniment.
    TIMEOUT_SAISIE_S = 600

    def __init__(self):
        self.console = None
        self.canevas = None
        self.explorateur = None
        self.barre_etat = None
        self.clavier = None

        # --- saisie utilisateur (?nom) -------------------------------------
        self._saisie_evenement: Optional[threading.Event] = None
        self._saisie_resultat = {"valeur": ""}
        self._demande_saisie = None  # callback fourni par l'app

        # --- tampon console -------------------------------------------------
        # Les lignes sont accumulées ici puis poussées vers le widget par
        # paquets (voir _vider_tampon) : un script qui journalise en boucle
        # serrée produirait sinon un schedule_once par ligne, et la file
        # d'évènements Kivy deviendrait le goulot d'étranglement.
        self._tampon: List[tuple] = []
        self._tampon_verrou = threading.Lock()
        self._vidange_planifiee = False

    # ------------------------------------------------------------------
    # Branchement des widgets (appelé par l'app une fois l'UI construite)
    # ------------------------------------------------------------------

    def attacher(self, *, console=None, canevas=None, explorateur=None,
                 barre_etat=None, clavier=None, demande_saisie=None):
        if console is not None:
            self.console = console
        if canevas is not None:
            self.canevas = canevas
        if explorateur is not None:
            self.explorateur = explorateur
        if barre_etat is not None:
            self.barre_etat = barre_etat
        if clavier is not None:
            self.clavier = clavier
        if demande_saisie is not None:
            self._demande_saisie = demande_saisie

    # ------------------------------------------------------------------
    # Contrat hôte : sortie console
    # ------------------------------------------------------------------

    def log(self, text, level: str = "info"):
        """Appelé depuis le thread d'exécution -- accumule et planifie une
        vidange groupée vers le widget console (thread principal)."""
        with self._tampon_verrou:
            self._tampon.append((str(text), level))
            deja = self._vidange_planifiee
            self._vidange_planifiee = True
        if not deja:
            Clock.schedule_once(self._vider_tampon, 0)

    def _vider_tampon(self, _dt):
        """Thread Kivy uniquement."""
        with self._tampon_verrou:
            paquet = self._tampon
            self._tampon = []
            self._vidange_planifiee = False
        if self.console is not None and paquet:
            self.console.ajouter_lignes(paquet)

    # ------------------------------------------------------------------
    # Contrat hôte : barre d'état et notifications
    # ------------------------------------------------------------------

    def _set_status(self, message: str, *args, **kwargs):
        """Nom préfixé d'un underscore parce que le moteur l'appelle sous ce
        nom-là (self.ide._set_status) -- renommer côté hôte casserait le
        contrat. *args/**kwargs absorbent les variantes d'appel de l'IDE
        bureau (couleur, icône) qui n'ont pas d'équivalent ici."""
        texte = str(message)
        if self.barre_etat is not None:
            Clock.schedule_once(lambda _dt: self.barre_etat.definir(texte), 0)

    def _empiler_toast(self, message: str, niveau: str = "info", *args, **kwargs):
        """Pas de fenêtre flottante sur mobile : l'écran est trop petit pour
        qu'un toast ne recouvre pas ce qu'on regarde. Le message part dans la
        console, où il reste consultable, plutôt que de disparaître au bout
        de trois secondes."""
        self.log(str(message), niveau if niveau in ("err", "ok", "sys") else "sys")

    # ------------------------------------------------------------------
    # Contrat hôte : explorateur de fichiers (VFS)
    # ------------------------------------------------------------------

    def update_vfs_list(self):
        if self.explorateur is not None:
            Clock.schedule_once(lambda _dt: self.explorateur.rafraichir(), 0)

    # ------------------------------------------------------------------
    # Contrat hôte : saisie utilisateur (?nom / ?E.nom / ?R.nom)
    # ------------------------------------------------------------------

    def request_input(self, var_name):
        """BLOQUE le thread d'exécution jusqu'à la réponse de l'utilisateur.

        Calqué sur UVIIDE.request_input : Event (on attend UNE valeur, une
        seule fois) et non Queue (qui suggérerait un flux), plus un timeout
        pour qu'une application quittée pendant l'attente ne laisse pas ce
        thread suspendu."""
        if self._demande_saisie is None:
            return ""
        self._saisie_evenement = threading.Event()
        self._saisie_resultat = {"valeur": ""}
        nom = str(var_name)
        Clock.schedule_once(lambda _dt: self._demande_saisie(nom), 0)
        if not self._saisie_evenement.wait(timeout=self.TIMEOUT_SAISIE_S):
            return ""
        return self._saisie_resultat.get("valeur", "")

    def fournir_saisie(self, valeur: str):
        """Appelé par l'interface quand l'utilisateur valide. Thread Kivy."""
        self._saisie_resultat["valeur"] = str(valeur)
        if self._saisie_evenement is not None:
            self._saisie_evenement.set()

    def annuler_saisie(self):
        """Débloque le script avec une valeur vide (arrêt, retour arrière)."""
        self._saisie_resultat["valeur"] = ""
        if self._saisie_evenement is not None:
            self._saisie_evenement.set()

    # ------------------------------------------------------------------
    # Contrat hôte : canevas graphique (DRAW.*)
    # ------------------------------------------------------------------

    def canvas_draw(self, op: str, *args):
        """Reçoit les opérations de dessin du moteur et les transmet au
        widget canevas. Le moteur envoie des coordonnées en repère Tkinter
        (origine en HAUT à gauche) ; la conversion vers le repère Kivy
        (origine en BAS à gauche) est faite dans le widget, pas ici, pour
        qu'elle dépende de la hauteur réelle du canevas au moment du rendu."""
        if self.canevas is None:
            return
        self.canevas.empiler(op, args)

    # ------------------------------------------------------------------
    # Contrat hôte : clavier (KEY.DOWN / KEY.PRESSED)
    # ------------------------------------------------------------------

    def key_is_down(self, key_name: str) -> bool:
        if self.clavier is None:
            return False
        return self.clavier.est_enfoncee(key_name)

    def key_was_pressed(self, key_name: str) -> bool:
        if self.clavier is None:
            return False
        return self.clavier.a_ete_pressee(key_name)

    # ------------------------------------------------------------------
    # Contrat hôte : respiration de l'interface pendant les boucles
    # ------------------------------------------------------------------

    def pump_events(self):
        """Appelé par le moteur tous les ~20 tours de boucle. Sur bureau,
        UVIIDE y fait root.update() parce que script et interface partagent
        le même thread. Ici ils sont sur deux threads : il n'y a rien à
        « pomper », mais un sleep(0) rend explicitement la main au thread
        Kivy. Sans lui, une boucle UVI serrée garde le GIL et l'interface
        devient saccadée alors qu'elle n'est pas bloquée."""
        time.sleep(0)
