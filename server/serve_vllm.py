import sys
import asyncio
import uvicorn

# --- PATCH : contourner l'absence de uvloop sous Windows ---
import importlib
class MockUvloop:
    def install(self):
        pass
# On remplace uvloop par un mock avant que vLLM ne l'importe
sys.modules['uvloop'] = MockUvloop()

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