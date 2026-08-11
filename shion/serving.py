"""shion/serving.py — ShionServingClient : le client attendu par
uvi_core.py (`from shion.serving import ShionServingClient`).

STDLIB UNIQUEMENT AU NIVEAU MODULE (urllib, json) -- voir le commentaire
autour de `from shion.serving import ShionServingClient` dans uvi_core.py :
un client minimal (Raspberry Pi qui ne fait que PARLER à un serveur Shion
distant) ne doit jamais avoir besoin de numpy pour ce seul import. Le
moteur d'inférence (shion.engine, qui a besoin de numpy) n'est importé
QUE dans _moteur_local(), paresseusement, et seulement si le repli local
est effectivement emprunté -- voir plus bas.

DEUX MODES, choisis automatiquement (pas de configuration séparée) :

  1. SERVEUR DÉDIÉ (base_url joignable) -- même protocole OpenAI-compatible
     que DeepSeekClient dans uvi_core.py, pour un usage partagé (plusieurs
     utilisateurs, un seul serveur Shion sur le réseau local). C'est le
     chemin essayé EN PREMIER.
  2. MOTEUR EMBARQUÉ (base_url injoignable -- connexion refusée, pas de
     route) -- ShionEngine tourne EN PROCESSUS, dans ce même interpréteur
     Python, sur les poids livrés dans shion/weights/. Zéro serveur à
     démarrer : c'est ce qui rend SHION.ASK utilisable dès la première
     ouverture de l'IDE, sur un poste solo, sans administration -- voir
     docs/SHION_ARCHITECTURE.md §5 (Plan de Déploiement, palier 1).

Une erreur HTTP en provenance d'un serveur qui A répondu (mauvais nom de
modèle, 500...) N'ENTRAÎNE PAS de repli silencieux vers le moteur local :
le serveur existe et est mal configuré, le dire clairement vaut mieux que
masquer le problème derrière une réponse locale de qualité différente.
Seule une IMPOSSIBILITÉ DE JOINDRE le serveur déclenche le repli.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT_S = 60  # cf. commentaire SHION_REQUEST_TIMEOUT_S dans uvi_core.py
_WEIGHTS_ROOT = os.path.join(os.path.dirname(__file__), "weights")


class ShionServingError(Exception):
    """Erreur de ShionServingClient.complete() -- message toujours déjà en
    français et actionnable, jamais une trace urllib/json brute (même
    contrat que ErisClientError/DeepSeekClientError côté uvi_core.py ;
    voir _ShionNativeAdapter dans uvi_core.py pour comment les deux mondes
    se raccordent malgré l'absence de dépendance croisée)."""


class ShionServingClient:
    nom_affichage = "SHION (natif)"

    # Un seul moteur embarqué par PROCESSUS, partagé entre toutes les
    # instances de client qui retombent sur le même modèle -- charger les
    # poids (même quantifiés) à chaque appel serait le contraire de la
    # promesse "frugal" du projet. Clé = (weights_dir, model_name).
    _moteurs_partages: Dict[tuple, Any] = {}

    def __init__(self, base_url: str = "http://localhost:8000", model_name: str = "shion",
                 timeout_s: int = DEFAULT_TIMEOUT_S, temperature: float = 0.8, top_p: float = 0.9,
                 max_new_tokens: int = 200):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.top_p = top_p
        self.max_new_tokens = max_new_tokens
        self._history: List[tuple] = []  # [(question, réponse), ...] -- même rôle que _history ailleurs
        # BUGFIX (lenteur perçue) : sans ce délai, CHAQUE message retentait
        # une connexion au serveur distant (défaut injoignable en usage
        # solo : ~4 s de refus de connexion à chaque fois, avant même de
        # démarrer la génération locale). Un serveur qui vient de refuser la
        # connexion n'a aucune raison de répondre différemment 2 s plus
        # tard : on arrête de retenter pendant ce délai, PAS indéfiniment
        # (un serveur démarré en cours de session reste détecté sous 30 s).
        self._serveur_indisponible_jusqua = 0.0

    def reset_history(self) -> None:
        self._history = []

    # -- chemin réseau ----------------------------------------------------

    def _essayer_serveur(self, prompt: str) -> Optional[str]:
        """Retourne le texte de la réponse si un serveur a répondu, lève
        ShionServingError s'il a répondu par une ERREUR (pas de repli dans
        ce cas), ou retourne None si le serveur est tout simplement
        INJOIGNABLE (repli local possible)."""
        if time.time() < self._serveur_indisponible_jusqua:
            return None
        payload = json.dumps({
            "model": self.model_name,
            "messages": self._messages_openai(prompt),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_new_tokens,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "%s/v1/chat/completions" % self.base_url, data=payload, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer uvi-local")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                corps = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise ShionServingError(
                "Serveur Shion à %s a refusé la requête (HTTP %d) : %s"
                % (self.base_url, e.code, detail)
            )
        except urllib.error.URLError:
            self._serveur_indisponible_jusqua = time.time() + 30.0
            return None  # injoignable : le serveur n'existe probablement pas ici -> repli local
        except (TimeoutError, json.JSONDecodeError) as e:
            raise ShionServingError(
                "Serveur Shion à %s a répondu de façon inexploitable : %s" % (self.base_url, e)
            )

        choix = (corps.get("choices") or [{}])[0]
        texte = ((choix.get("message") or {}).get("content") or "").strip()
        if not texte:
            raise ShionServingError("Serveur Shion à %s n'a renvoyé aucun texte." % self.base_url)
        return texte

    def _messages_openai(self, prompt: str) -> List[Dict[str, str]]:
        msgs = []
        for q, r in self._history:
            msgs.append({"role": "user", "content": q})
            msgs.append({"role": "assistant", "content": r})
        msgs.append({"role": "user", "content": prompt})
        return msgs

    # -- chemin local (repli) ---------------------------------------------

    def _moteur_local(self):
        cle = (_WEIGHTS_ROOT, self.model_name)
        moteur = ShionServingClient._moteurs_partages.get(cle)
        if moteur is None:
            # Import PARESSEUX et volontaire : c'est ICI, seulement si le
            # repli local est vraiment emprunté, que numpy/le moteur
            # entrent en jeu -- voir la docstring du module.
            from .engine import ShionEngine
            # BUGFIX (toujours nano, même après avoir entraîné un autre
            # palier) : ce chemin ignorait purement et simplement
            # self.model_name et rechargeait "shion-nano" en dur -- un
            # checkpoint shion-micro fraîchement entraîné n'était donc
            # JAMAIS utilisé par l'IDE tant qu'on ne touchait pas ce
            # fichier. get_config() lève ValueError si model_name n'est pas
            # un palier connu (cas normal : model_name="shion", le label
            # générique envoyé par défaut à un serveur distant, ne
            # correspond à aucun palier) -- on retombe alors sur NANO,
            # comportement historique inchangé. Pour bascule explicite :
            # SHION_MODEL_NAME=shion-micro côté uvi_core.py.
            from .config import NANO, get_config
            try:
                preset = get_config(self.model_name)
            except ValueError:
                preset = NANO
            weights_dir = os.path.join(_WEIGHTS_ROOT, preset.name)
            if not os.path.isdir(weights_dir):
                raise ShionServingError(
                    "Aucun serveur Shion joignable à %s, ET aucun checkpoint local dans "
                    "%s. Entraînez-en un (python -m shion.train) ou démarrez un serveur "
                    "(python -m shion.server)." % (self.base_url, weights_dir)
                )
            # BUGFIX (lenteur ~15x en génération) : max_resident_layers=2
            # forçait un cache LRU trop petit pour contenir le modèle entier
            # dès que n_layer > 2 (vrai pour les trois paliers). En
            # génération autorégressive, CHAQUE token retraverse TOUTES les
            # couches dans le même ordre (0, 1, 2, ...) -- contrairement à
            # un passage unique (entraînement), rien ne redevient inutile
            # entre deux tokens. Avec une capacité < n_layer, la couche 0
            # est systématiquement évincée avant d'être redemandée au token
            # suivant : 100% de ratés de cache, donc une dé-quantification
            # complète du modèle À CHAQUE TOKEN (mesuré sur nano : ~60 s
            # pour 200 tokens, contre ~4 s en gardant les couches
            # résidentes). Nano et Micro tiennent largement en RAM une fois
            # dé-quantifiés : la capacité par défaut doit couvrir le modèle
            # ENTIER pour ce cas d'usage, pas une fenêtre glissante de 2
            # pensée pour un passage séquentiel unique (voir
            # memory.py:LazyLayerCache -- toujours pertinent pour
            # Shion-Base en RAM contrainte, via server.py --resident-layers).
            moteur = ShionEngine(weights_dir, preset, max_resident_layers=preset.n_layer)
            ShionServingClient._moteurs_partages[cle] = moteur
        return moteur

    # -- contrat ------------------------------------------------------------

    def complete(self, prompt: str, remember: bool = True) -> str:
        if not prompt.strip():
            raise ShionServingError("Question vide envoyée à SHION.")

        try:
            reponse = self._essayer_serveur(prompt)
        except ShionServingError:
            raise
        if reponse is None:
            moteur = self._moteur_local()
            try:
                reponse = moteur.chat(self._history, prompt, max_new_tokens=self.max_new_tokens,
                                       temperature=self.temperature, top_p=self.top_p)
            except Exception as e:
                raise ShionServingError("Moteur Shion local en échec : %s" % e) from e
            if not reponse:
                reponse = ("(Shion-Nano n'a rien produit d'exploitable pour cette question -- "
                            "c'est un modèle de démonstration de quelques centaines de milliers "
                            "de paramètres, pas encore un assistant compétent. Voir "
                            "docs/SHION_ARCHITECTURE.md.)")

        if remember:
            self._history.append((prompt, reponse))
        return reponse
