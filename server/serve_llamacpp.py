"""Sert DeepSeek-R1-Distill-Qwen (1,5B ou 7B) en local, sur CPU, via
llama-cpp-python.

POURQUOI CE FICHIER À CÔTÉ DE serve_vllm.py : même rôle (exposer un
serveur HTTP compatible OpenAI que DeepSeekClient, dans uvi_core.py, sait
déjà consommer sans aucune modification), mais pour la machine qui n'a PAS
le GPU que vLLM exige -- voir docs/SERVEUR_LOCAL_CPU.md pour le
comparatif complet et les étapes de mise en place (téléchargement du
modèle GGUF notamment).

Contrairement à serve_vllm.py, aucun patch n'est nécessaire ici : vLLM
tire torch + torchaudio (d'où les contournements uvloop/torchaudio de
l'autre fichier) ; llama-cpp-python n'a aucune de ces dépendances, le
lancement est direct.

DEUX TAILLES, UN SEUL SERVEUR À LA FOIS (même port 8000) : basculer de
l'une à l'autre = arrêter ce script (Ctrl+C) et le relancer avec l'autre
préréglage. Le nom de modèle exposé sur /v1/models change avec la taille
(voir PRESETS ci-dessous) -- `DEEPSEEK_MODEL_NAME` doit être mis à jour en
conséquence côté UVI (voir docs/SERVEUR_LOCAL_CPU.md §5bis).

    python server/serve_llamacpp.py            # 1,5B (défaut -- voir §6
                                                 # du doc : le 7B ne s'est
                                                 # pas montré praticable
                                                 # sur un CPU 4 cœurs lors
                                                 # des mesures)
    python server/serve_llamacpp.py 1.5b        # équivalent, explicite
    python server/serve_llamacpp.py 7b          # variante 7B
    python server/serve_llamacpp.py chemin.gguf # fichier GGUF arbitraire
                                                 # (alias générique utilisé)
"""
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (chemin du GGUF, alias exposé sur /v1/models = valeur attendue pour
# DEEPSEEK_MODEL_NAME) -- l'alias diffère entre les deux tailles pour
# qu'on sache TOUJOURS, en lisant /v1/models, laquelle tourne réellement.
PRESETS = {
    "1.5b": (
        os.path.join(RACINE, "models", "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf"),
        "deepseek-r1-distill-qwen-1.5b",
    ),
    "7b": (
        os.path.join(RACINE, "models", "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf"),
        "deepseek-r1-distill-qwen-7b",
    ),
}

if __name__ == "__main__":
    argument = sys.argv[1] if len(sys.argv) > 1 else "1.5b"
    if argument in PRESETS:
        chemin_modele, alias = PRESETS[argument]
    else:
        chemin_modele, alias = argument, "deepseek-local-custom"

    if not os.path.isfile(chemin_modele):
        print(f"[UVI] Modèle introuvable : {chemin_modele}")
        print("      Téléchargez-le d'abord -- voir docs/SERVEUR_LOCAL_CPU.md §1.")
        sys.exit(1)

    print(f"[UVI] Chargement : {chemin_modele}")
    print(f"[UVI] Alias exposé (= DEEPSEEK_MODEL_NAME côté UVI) : {alias}")

    # --model_alias : DOIT correspondre à DEEPSEEK_MODEL_NAME dans
    # .shion.local.env / les variables d'environnement -- DeepSeekClient
    # interroge /v1/models et rejette (HTTP 404) tout nom qui ne
    # correspond pas exactement (voir docs/SERVEUR_DEEPSEEK.md §4).
    #
    # --n_threads -1 : utilise tous les cœurs logiques disponibles. Le
    # défaut de llama-cpp-python est 2, ce qui laisserait la moitié des
    # cœurs d'une machine à 4 cœurs inutilisés.
    #
    # --n_ctx 4096 : compromis pour du CPU pur (voir §2 du même doc) --
    # plus haut ralentit le traitement du prompt et la RAM utilisée, plus
    # bas risque de tronquer un raisonnement long avant la réponse.
    #
    # --chat_format non précisé : les GGUF bartowski embarquent le
    # template de chat officiel dans leurs métadonnées ; laisser
    # llama-cpp-python le lire évite de dupliquer ici un format qui
    # pourrait diverger du modèle réellement chargé.
    sys.argv = [
        "serve",
        "--model", chemin_modele,
        "--model_alias", alias,
        "--host", "127.0.0.1",
        "--port", "8000",
        "--n_ctx", "4096",
        "--n_threads", "-1",
        "--n_gpu_layers", "0",
        "--api_key", os.environ.get("DEEPSEEK_API_KEY", "ma_cle_api"),
    ]
    from llama_cpp.server.__main__ import main
    main()
