"""server/sitecustomize.py — Correctifs appliqués AVANT tout code utilisateur,
via le mécanisme standard `sitecustomize` de Python (auto-importé au
démarrage de l'interpréteur si son dossier est dans PYTHONPATH).

POURQUOI ICI ET PAS DANS serve_vllm.py : ce fichier patchait déjà uvloop
et torchaudio, mais en enveloppant l'appel aux fonctions internes de vLLM
(api_server.run_server / .main) -- une surface fragile qui change de
signature d'une version de vLLM à l'autre (vu en pratique : "run_server()
missing 1 required positional argument: 'args'"). En patchant sys.modules
ICI, en amont, on peut se contenter d'invoquer la vraie commande `vllm
serve ...` (l'interface publique STABLE, documentée dans
docs/SERVEUR_DEEPSEEK.md) sans jamais avoir à deviner ses fonctions
internes -- voir la cellule Colab correspondante pour l'invocation.
"""

import sys
import types
import importlib.machinery

# --- uvloop : mock seulement s'il est vraiment absent (ex. Windows) ---
# Sur Linux/Colab, le vrai uvloop est généralement installable et apporte
# un vrai gain de perf pour vLLM -- pas de raison de le masquer s'il
# fonctionne. Contrairement à torchaudio plus bas, un import de uvloop qui
# échoue reste un ImportError propre (pas d'état cassé dans sys.modules à
# nettoyer), donc essayer-puis-secourir est sûr ici.
if 'uvloop' not in sys.modules:
    try:
        import uvloop  # noqa: F401
    except ImportError:
        class _MockUvloop:
            def install(self):
                pass
        sys.modules['uvloop'] = _MockUvloop()

# --- torchaudio : voir server/serve_vllm.py pour l'explication complète ---
# (conflit de version CUDA torch/torchaudio observé sur Colab -- transformers
# l'importe sans condition alors qu'on ne sert qu'un LLM texte). Le stub
# DOIT être posé avant toute tentative d'import réel : un import qui
# échoue en cours de route laisse une entrée cassée (__spec__=None) dans
# sys.modules, que essayer-puis-secourir ne peut plus réparer après coup.
if 'torchaudio' not in sys.modules:
    stub = types.ModuleType('torchaudio')
    stub.__spec__ = importlib.machinery.ModuleSpec('torchaudio', loader=None)
    sys.modules['torchaudio'] = stub
