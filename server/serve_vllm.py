import os
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
# dès qu'on importe vllm, ET vérifie séparément sa présence via
# importlib.util.find_spec (transformers.utils.import_utils). Sur certains
# environnements (ex. Colab avec un torch plus récent que le torchaudio
# publié, comme torch cu13.0 vs torchaudio cu12.8), le vrai import de
# torchaudio échoue en cours de route (RuntimeError dans sa vérification
# CUDA) -- et CPython laisse alors une entrée CASSÉE dans sys.modules
# (héritée de l'import partiel du paquet et de ses sous-modules), avec
# __spec__ à None. Toute vérification ultérieure via find_spec (pas
# seulement un nouvel `import torchaudio`) retrouve cette entrée cassée et
# plante avec `ValueError: torchaudio.__spec__ is None` -- essayé une
# première fois puis rattrapé après coup, ça arrivait déjà trop tard.
#
# Solution : poser le stub AVANT toute tentative d'import réel, pour que
# le vrai __init__.py cassé de torchaudio ne s'exécute jamais et ne laisse
# jamais cette entrée invalide derrière lui. On n'a de toute façon aucun
# besoin de la vraie bibliothèque pour servir un LLM texte.
if 'torchaudio' not in sys.modules:
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
#
# --dtype half (float16) et non bfloat16 : bfloat16 exige une capacité de
# calcul CUDA >= 8.0 (Ampere ou plus récent -- A100, RTX 3090/4090...).
# Un T4 (capacité 7.5, ex. le GPU gratuit de Colab) le REFUSE au démarrage
# avec "ValueError: Bfloat16 is only supported on GPUs with compute
# capability of at least 8.0". float16 fonctionne sur les DEUX générations
# -- légèrement moins stable numériquement que bf16 sur du tout récent
# matériel, mais c'est le choix qui marche partout, donc le défaut le plus
# sûr ici.
sys.argv = [
    "serve",
    "--model", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "--served-model-name", "deepseek-r1-distill-qwen-7b",
    "--host", "0.0.0.0",
    "--port", "8000",
    "--dtype", "half",
    "--max-model-len", "8192",
    "--gpu-memory-utilization", "0.90",
    "--tensor-parallel-size", "1",
    "--reasoning-parser", "deepseek_r1",
    # BUGFIX (fausse impression de sécurité) : ce serveur écoute sur
    # 0.0.0.0 (voir --host ci-dessus, nécessaire pour un tunnel
    # Colab/ngrok) -- la clé --api-key est donc la SEULE barrière avant
    # qu'un tiers qui trouve l'URL du tunnel puisse consommer le GPU.
    # Avant ce correctif, cette clé était figée en dur à "ma_cle_api",
    # IGNORANT complètement la variable d'environnement DEEPSEEK_API_KEY
    # -- exactement celle que docs/SERVEUR_DEEPSEEK.md §5 et
    # start_server.sh (l'autre lanceur vLLM du même dossier) utilisent
    # tous les deux. Un·e utilisateur·rice qui suivait la doc et faisait
    # `export DEEPSEEK_API_KEY="un-vrai-secret"` avant de lancer CE
    # script précis obtenait donc un serveur exposé protégé par le
    # placeholder public connu de quiconque lit ce fichier, pas par son
    # secret -- silencieusement, sans erreur. os.environ.get(...) avec le
    # même repli qu'avant préserve le comportement par défaut inchangé.
    "--api-key", os.environ.get("DEEPSEEK_API_KEY", "ma_cle_api"),
]

if launch_func:
    # Si run_server ou main existe, on l'appelle directement
    launch_func()
else:
    # Sinon, on initialise manuellement et on lance uvicorn
    initialize_server()
    uvicorn.run(app, host="0.0.0.0", port=8000, loop="asyncio")