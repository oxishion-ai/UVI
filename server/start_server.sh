#!/usr/bin/env bash
# Lance le serveur vLLM servant DeepSeek-R1-Distill-Qwen-7B pour SHION.
#
# set -euo pipefail : un serveur d'inference qui demarre a moitie (modele
# absent, GPU pris) doit echouer BRUYAMMENT ici, pas rendre des HTTP 500 que
# le client UVI traduira en "SHION injoignable" -- diagnostic beaucoup plus
# long a remonter depuis l'IDE que depuis ce script.
set -euo pipefail

MODELE="${MODELE:-deepseek-ai/DeepSeek-R1-Distill-Qwen-7B}"
NOM_SERVI="${NOM_SERVI:-deepseek-r1-distill-qwen-7b}"
PORT="${PORT:-8000}"
# 32768 et non le maximum du modele (131072) : vLLM reserve le cache KV pour
# la longueur declaree et REFUSE DE DEMARRER si la VRAM n'y suffit pas.
# C'est l'echec de demarrage n1. Baissez a 8192 sur une carte 16 Go.
MAX_LEN="${MAX_LEN:-32768}"
UTILISATION_GPU="${UTILISATION_GPU:-0.90}"
CLE_API="${DEEPSEEK_API_KEY:-uvi-local}"
NB_GPU="${NB_GPU:-1}"

if ! command -v vllm >/dev/null 2>&1; then
  echo "ERREUR : vllm introuvable. Installez-le :" >&2
  echo "  uv pip install vllm --torch-backend=auto" >&2
  exit 1
fi

echo "=== Serveur SHION (vLLM) ==="
echo "  modele        : ${MODELE}"
echo "  nom servi     : ${NOM_SERVI}   <- valeur a mettre dans DEEPSEEK_MODEL_NAME"
echo "  ecoute        : 0.0.0.0:${PORT}"
echo "  contexte max  : ${MAX_LEN} tokens"
echo
echo "  Cote UVI :"
echo "    export SHION_SERVER_URL=http://\$(hostname -I | awk '{print \$1}'):${PORT}"
echo "    export DEEPSEEK_MODEL_NAME=${NOM_SERVI}"
echo "    export DEEPSEEK_API_KEY=${CLE_API}"
echo

exec vllm serve "${MODELE}" \
  --served-model-name "${NOM_SERVI}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --dtype bfloat16 \
  --max-model-len "${MAX_LEN}" \
  --gpu-memory-utilization "${UTILISATION_GPU}" \
  --tensor-parallel-size "${NB_GPU}" \
  --reasoning-parser deepseek_r1 \
  --api-key "${CLE_API}"
