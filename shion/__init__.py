"""shion — Moteur d'IA frugal "bare-metal" pour UVI.

Voir docs/SHION_ARCHITECTURE.md (racine du dépôt) pour l'architecture
complète : topologie, quantification, gestion mémoire, entraînement,
déploiement.

IMPORTS DÉLIBÉRÉMENT MINIMAUX ICI -- voir le commentaire entourant
`from shion.serving import ShionServingClient` dans uvi_core.py. Ce fichier
ne doit PAS déclencher le chargement de numpy pour un simple `import
shion` : ShionServingClient (client réseau, stdlib pure) doit rester
utilisable sur une machine qui n'a besoin QUE de parler à un serveur Shion
distant. numpy n'entre en jeu que si l'appelant importe explicitement un
module qui en a réellement besoin (shion.engine, shion.model, shion.memory,
shion.train) -- typiquement le serveur (shion/server.py) ou le repli local
de ShionServingClient lui-même, jamais ce fichier.
"""

from .serving import ShionServingClient, ShionServingError

__version__ = "0.1.0-nano"
__all__ = ["ShionServingClient", "ShionServingError", "__version__"]
