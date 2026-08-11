import sys
import asyncio
import types
import importlib.machinery
import uvicorn

# --- PATCH : contourner l'absence de uvloop sous Windows ---
import importlib
class MockUvloop:
    def install(self):
        pass
# On remplace uvloop par un mock avant que vLLM ne l'importe
sys.modules['uvloop'] = MockUvloop()

# --- PATCH : contourner le conflit de version CUDA torch/torchaudio ---
# transformers importe torchaudio de façon INCONDITIONNELLE (via
# loss/loss_rnnt.py, pour un modèle audio Parakeet qu'on n'utilise jamais)
# dès qu'on importe vllm. Sur certains environnements (ex. Colab avec un
# torch plus récent que le torchaudio publié, comme torch cu13.0 vs
# torchaudio cu12.8), torchaudio lève une RuntimeError À L'IMPORT à cause
# de cette incompatibilité -- avant même qu'on ait pu s'en servir, alors
# qu'on ne sert ici qu'un LLM texte et n'a besoin d'aucune fonctionnalité
# audio. Un module factice vide dans sys.modules court-circuite cet
# import : Python le trouve déjà "importé" et n'exécute jamais le vrai
# __init__.py de torchaudio (donc jamais la vérification CUDA foireuse).
# Même principe que le mock uvloop juste au-dessus.
#
# __spec__ DOIT être un vrai ModuleSpec, pas None : transformers vérifie
# aussi la présence de torchaudio via importlib.util.find_spec (pas
# seulement `import torchaudio`), qui lève explicitement ValueError si le
# module déjà présent dans sys.modules a un __spec__ à None -- un module
# créé à la main avec types.ModuleType() n'en a pas par défaut.
if 'torchaudio' not in sys.modules:
    try:
        import torchaudio  # noqa: F401  -- si ça marche déjà, tant mieux, rien à patcher
    except Exception:
        stub = types.ModuleType('torchaudio')
        stub.__spec__ = importlib.machinery.ModuleSpec('torchaudio', loader=None)
        sys.modules['torchaudio'] = stub

# --- Importer l'API server de vLLM ---
from vllm.entrypoints.openai import api_server

# Vérifier quelle fonction est disponible pour démarrer le serveur
if hasattr(api_server, 'run_server'):
    launch_func = api_server.run_server
elif hasattr(api_server, 'main'):
    launch_func = api_server.main
else:
    # Fallback : si aucune, on crée l'engine et on lance uvicorn nous-mêmes
    from vllm.entrypoints.openai.api_server import app, initialize_server
    launch_func = None

# Définir les arguments comme si on les passait en ligne de commande
sys.argv = [
    "serve",
    "--model", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "--served-model-name", "deepseek-r1-distill-qwen-7b",
    "--host", "0.0.0.0",
    "--port", "8000",
    "--dtype", "bfloat16",
    "--max-model-len", "8192",
    "--gpu-memory-utilization", "0.90",
    "--tensor-parallel-size", "1",
    "--reasoning-parser", "deepseek_r1",
    "--api-key", "ma_cle_api"
]

if launch_func:
    # Si run_server ou main existe, on l'appelle directement
    launch_func()
else:
    # Sinon, on initialise manuellement et on lance uvicorn
    initialize_server()
    uvicorn.run(app, host="0.0.0.0", port=8000, loop="asyncio")