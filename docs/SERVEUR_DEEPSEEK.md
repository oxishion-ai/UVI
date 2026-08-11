# Serveur SHION — vLLM + DeepSeek-R1-Distill-Qwen-7B

Ce dossier monte le serveur derrière `SHION.ASK`, l'onglet SHION de l'IDE,
`!ERIS(...)` et le bouton AUTO-CORRIGER.

> **Pourquoi DeepSeek et pas Shion ?** Shion (le modèle maison) est bloqué en
> développement. DeepSeek-R1-Distill-Qwen-7B le remplace **temporairement**,
> servi par le même vLLM, avec la même architecture client/serveur. Revenir à
> Shion coûte zéro ligne de code : `SHION_BACKEND=shion`.

---

## 1. Le modèle

| | |
|---|---|
| Dépôt Hugging Face | `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` |
| Licence | MIT |
| Base | Qwen2.5-Math-7B (≈7,62 milliards de paramètres) |
| Contexte max configuré | 131 072 tokens (**à plafonner**, voir §3) |
| Particularité | Modèle de **raisonnement** : émet sa chaîne de pensée entre `<think>` et `</think>` avant la réponse |

### VRAM nécessaire

Les poids en bf16 pèsent ~15 Go, auxquels s'ajoutent le cache KV et la
surcharge CUDA.

| GPU | Verdict |
|---|---|
| **16 Go** | Serré en bf16. Plafonnez `--max-model-len` à 4096–8192, ou passez sur une quantification 4 bits (~5 Go) |
| **24 Go** (RTX 3090 / 4090) | **Cible recommandée.** bf16 confortable avec un contexte substantiel |
| **40–80 Go** (A100 / H100) | Trivial, contexte quasi complet possible |

Un seul GPU suffit : `--tensor-parallel-size 1`. Ce modèle n'a pas besoin de
multi-GPU.

---

## 2. Installation

```bash
# Python 3.10 à 3.14, CUDA 12.x
uv pip install vllm --torch-backend=auto     # méthode recommandée
# ou : pip install vllm
```

---

## 3. Lancement

```bash
./server/start_server.sh
```

Ou à la main :

```bash
vllm serve deepseek-ai/DeepSeek-R1-Distill-Qwen-7B \
  --served-model-name deepseek-r1-distill-qwen-7b \
  --host 0.0.0.0 --port 8000 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --tensor-parallel-size 1 \
  --reasoning-parser deepseek_r1 \
  --api-key "$DEEPSEEK_API_KEY"
```

### Les deux drapeaux qui comptent vraiment

**`--max-model-len 32768`** — Sans lui, vLLM tente de réserver le cache KV
pour 131 072 tokens et **refuse de démarrer** sur une carte modeste :

```
ValueError: To serve at least one request with the model's max seq len (131072),
X GiB KV cache is needed, but only Y GiB is available.
```

C'est l'échec de démarrage n°1. Baissez `--max-model-len` avant de toucher à
quoi que ce soit d'autre.

**`--reasoning-parser deepseek_r1`** — Déplace le `<think>…</think>` dans un
champ `reasoning_content` séparé et laisse `content` propre.

Sans lui, le raisonnement arrive **dans** `choices[0].message.content`, donc
dans la variable UVI de `SHION.ASK "question" -> r`. `_safe_store` tronquerait
alors à `MAX_STRING_LENGTH`, et le script recevrait du monologue interne
tronqué **au lieu de la réponse**. `DeepSeekClient._nettoyer_reponse` sait
retirer ces balises côté client, mais c'est un filet — activez le drapeau.

> Note : `--enable-reasoning` est déprécié depuis vLLM 0.9.0 et **supprimé**
> en 0.10.0. `--reasoning-parser` seul suffit désormais.

### Clé d'API

Avec `--api-key`, vLLM exige `Authorization: Bearer <clé>` sur `/v1/*`.
**Sans** ce drapeau, l'en-tête est simplement **ignoré** — `DeepSeekClient`
en envoie toujours un, ce qui fonctionne dans les deux cas.

Un piège vérifié : envoyer littéralement la chaîne `"None"` est **rejeté**
par le vérificateur de jeton. D'où le défaut non vide `"uvi-local"` dans le
code.

`--api-key` ne protège que les préfixes `/v1`, `/v2`, `/inference` — pas les
métriques ni `/health`. Ce n'est **pas** une sécurité de production :
mettez un reverse proxy devant si le serveur est exposé.

---

## 4. Vérification avant de brancher UVI

```bash
python3 server/smoke_test.py http://VOTRE_SERVEUR:8000
```

Le point critique est la **première** vérification : le nom exact du modèle.

```bash
curl http://VOTRE_SERVEUR:8000/v1/models
```

La valeur du champ `id` est **exactement** ce qu'il faut mettre dans
`DEEPSEEK_MODEL_NAME`. Une valeur qui ne correspond pas renvoie un **HTTP
404** — pas un 400 — avec `The model X does not exist.` C'est l'erreur n°1
au premier branchement, et elle est déroutante parce que le serveur
*répond* : le réseau va bien, seul le nom est faux.

---

## 5. Branchement côté UVI

```bash
export SHION_BACKEND="deepseek"
export SHION_SERVER_URL="http://192.168.1.50:8000"
export DEEPSEEK_MODEL_NAME="deepseek-r1-distill-qwen-7b"   # cf. /v1/models
export DEEPSEEK_API_KEY="votre-jeton"
python3 uvi_core.py
```

> **Depuis que Shion natif est implémenté (voir docs/SHION_ARCHITECTURE.md),
> `SHION_BACKEND=deepseek` n'est plus le défaut** — il faut l'exporter
> explicitement comme ci-dessus pour utiliser ce serveur DeepSeek plutôt
> que le moteur Shion embarqué. DeepSeek reste la meilleure qualité de
> réponse disponible (7 milliards de paramètres, contre les quelques
> centaines de milliers de Shion-Nano) pour qui dispose d'un GPU.

| Variable | Défaut | Rôle |
|---|---|---|
| `SHION_BACKEND` | `shion` | `deepseek` pour ce serveur externe (ce document) |
| `SHION_SERVER_URL` | `http://localhost:8000` | Racine du serveur vLLM |
| `DEEPSEEK_MODEL_NAME` | `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` | Doit correspondre à `/v1/models` |
| `DEEPSEEK_API_KEY` | `uvi-local` | Ignoré si le serveur tourne sans `--api-key` |
| `DEEPSEEK_TEMPERATURE` | `0.6` | **Ne descendez pas sous 0.5** (voir §6) |
| `DEEPSEEK_TOP_P` | `0.95` | Valeur des benchmarks officiels |
| `DEEPSEEK_MAX_TOKENS` | `8192` | Le raisonnement consomme ce budget en premier |
| `DEEPSEEK_REQUEST_TIMEOUT_S` | `180` | Un modèle de raisonnement est lent, ce n'est pas une panne |

---

## 6. Trois règles d'usage propres à R1

Elles viennent de la model card officielle DeepSeek, pas d'une préférence de
style. Elles sont déjà appliquées par `DeepSeekClient` — ceci documente
*pourquoi*, pour qu'on ne les « simplifie » pas plus tard.

1. **Température 0.5–0.7 (0.6 recommandé).** Mettre 0.0 en croyant rendre le
   modèle déterministe est le **mode de défaillance documenté n°1** : le
   modèle part en répétitions sans fin ou en sortie incohérente.

2. **Pas de message `system`.** DeepSeek demande de tout mettre dans le
   message `user`. Le réflexe OpenAI (consignes en `system`) **dégrade
   réellement** ce modèle-ci.

3. **Ne pas renvoyer le raisonnement des tours précédents.** Le
   `reasoning_content` d'un tour antérieur ne doit pas repartir dans le
   contexte : il serait ignoré tout en consommant du budget.
   `DeepSeekClient` ne mémorise que la réponse nettoyée.

---

## 7. Successeur à considérer

`deepseek-ai/DeepSeek-R1-0528-Qwen3-8B` (mai 2025) est un distill nettement
plus performant pour ~1 Go de VRAM en plus.

**Attention, le contrat change** — ce n'est pas un remplacement transparent :
il **accepte** un prompt system, ne requiert **pas** le `<think>` forcé, et
étant architecturalement un Qwen3, c'est plutôt `--reasoning-parser qwen3`
qui s'applique. Basculer demanderait d'adapter `_construire_messages`.

---

## 8. Diagnostic

| Symptôme | Cause | Correctif |
|---|---|---|
| Le serveur refuse de démarrer, message sur le cache KV | `--max-model-len` trop grand pour la VRAM | Baissez à 8192, puis `--gpu-memory-utilization` |
| **HTTP 404** `The model X does not exist` | `DEEPSEEK_MODEL_NAME` ≠ nom servi | `curl .../v1/models` |
| HTTP 401 | Serveur lancé avec `--api-key` | Alignez `DEEPSEEK_API_KEY` |
| HTTP 400 « maximum context length » | Prompt + budget > contexte | Baissez `DEEPSEEK_MAX_TOKENS` |
| Réponse vide, `finish_reason: "length"` | Le raisonnement a mangé tout le budget | Augmentez `DEEPSEEK_MAX_TOKENS` |
| Réponses en boucle | Température trop basse | Remontez vers 0.6 |
| `<think>` visible dans la variable UVI | `--reasoning-parser` absent | Ajoutez le drapeau (le client nettoie déjà, mais mieux vaut la source) |

---

## Sources

Model card [DeepSeek-R1-Distill-Qwen-7B](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-7B) ·
[documentation vLLM](https://docs.vllm.ai/) ·
[DeepSeek-R1-0528-Qwen3-8B](https://huggingface.co/deepseek-ai/DeepSeek-R1-0528-Qwen3-8B)

Les chiffres de VRAM sont des **estimations de dimensionnement** issues de
sources communautaires, pas des garanties : ils varient avec la longueur de
contexte et les réglages de cache KV.
