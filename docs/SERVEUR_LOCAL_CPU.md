# Serveur SHION — llama.cpp local, sur CPU (sans GPU, sans Colab)

Alternative à `docs/SERVEUR_DEEPSEEK.md` (vLLM + GPU) pour une machine
**sans GPU NVIDIA** : sert le même modèle, quantifié, entièrement sur CPU,
sans dépendre d'une session Colab/ngrok éphémère. Même contrat client
qu'avec vLLM : `DeepSeekClient` (dans `uvi_core.py`) parle à un serveur
HTTP compatible OpenAI, il ne sait pas — et n'a pas besoin de savoir — si
c'est vLLM ou llama.cpp de l'autre côté.

> **Mesuré, pas estimé : impraticable sur CETTE machine, 7B *et* 1,5B.**
> CPU réel de ce poste : **Intel Pentium N3710** (Atom/Braswell, 2016,
> 1,6 GHz, 4 cœurs) — **sans AVX ni AVX2**. C'est la cause racine : sans
> ces jeux d'instructions, llama.cpp retombe sur ses noyaux de calcul
> génériques les plus lents (le log de démarrage le confirme -- la ligne
> `CPU : LLAMAFILE = 1 | OPENMP = 1 | REPACK = 1 |` ne liste **aucun**
> `AVX2 = 1`, contrairement à un CPU qui le supporterait). Résultat mesuré
> pour "quelle est la capitale de la France ?" bornée à 150 tokens : ni le
> 7B (deux essais, coupés à 500 s puis 900 s) ni le 1,5B (coupé à 850 s)
> n'ont terminé. Le processus travaille réellement tout du long (temps CPU
> vérifié en hausse continue à chaque relevé — ce n'est pas un blocage),
> simplement à un débit trop faible pour un usage interactif sur ce
> matériel précis, quelle que soit la taille de modèle raisonnable
> essayée. **Ce n'est pas un bug de configuration : c'est un plafond
> matériel.** Voir §6. Ce document reste utile tel quel pour une machine
> plus récente (quasi tout CPU x86 depuis ~2013 a AVX2) — juste pas celle-ci.

---

## 1. Le modèle

Poids quantifiés au format GGUF (format natif de llama.cpp), pas les poids
bf16 originaux — c'est ce qui rend l'inférence CPU praticable.

**Deux tailles testées sur ce dépôt, les deux mesurées trop lentes sur
cette machine précise (Pentium N3710, sans AVX2 -- voir l'encart en tête
de document) :**

| | 1,5B | 7B |
|---|---|---|
| Dépôt Hugging Face | [`bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF`](https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF) | [`bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF`](https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF) |
| Fichier (Q4_K_M) | `DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf` (~1,12 Go) | `DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf` (~4,68 Go) |
| Paramètres | 1,5 milliard | 7,62 milliards |
| Vitesse mesurée ici | Bornée à 150 tokens, **pas terminé en 850 s** | Bornée à 150 tokens, **pas terminé en 500 s ni en 900 s** (deux essais) |

**Sur un CPU x86 normal (AVX2 présent — la quasi-totalité des machines
depuis ~2013), le 1,5B devrait être largement praticable** malgré le
résultat ci-dessus : ces mesures viennent d'un cas défavorable précis
(Atom/Braswell sans AVX2), pas d'une limite du modèle ou du logiciel.
Commencez toujours par le 1,5B sur une machine sans GPU ; le 7B reste
documenté ici parce qu'il correspond au modèle utilisé côté vLLM/Colab
(`docs/SERVEUR_DEEPSEEK.md`), où le GPU change complètement la donne.

Téléchargement (met le fichier dans `models/`, ignoré par git — voir
`.gitignore`) :

```bash
mkdir -p models

# 1,5B -- à essayer en premier sur CPU sans GPU
curl -L -o models/DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf \
  https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf

# 7B -- seulement si le 1,5B s'avère trop limité en qualité ET que sa
# vitesse mesurée laisse penser que le 7B resterait praticable
curl -L -o models/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf \
  https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf
```

`server/serve_llamacpp.py` accepte un préréglage (`1.5b` par défaut, ou
`7b`) ou un chemin de fichier arbitraire — détails et bascule entre les
deux en §3.

D'autres quantifications existent dans les deux dépôts (Q5_K_M, Q6_K,
Q8_0...) : plus le nombre de bits est élevé, meilleure est la qualité et
plus lourds sont le fichier et le calcul. Q4_K_M est le point de départ
raisonnable des deux côtés.

---

## 2. Installation

```bash
pip install "llama-cpp-python[server]"
```

**Pas de roue précompilée si votre Python est très récent** (ex. 3.14) :
pip compile alors llama.cpp depuis les sources (CMake + votre compilateur
C++ système). Sur une machine à 4 cœurs, observé pour durer facilement
plus d'une heure — c'est normal, ce n'est pas figé tant que `cl`/`gcc` (ou
`cmake`/`ninja`) consomme du CPU. Nécessite un compilateur C++ installé
(Visual Studio Build Tools sur Windows, `build-essential` sur Linux).

---

## 3. Lancement, et bascule entre 1,5B et 7B

Un seul modèle chargé à la fois, sur le même port 8000. `server/serve_llamacpp.py`
prend un préréglage en argument :

```bash
python server/serve_llamacpp.py         # 1,5B (défaut)
python server/serve_llamacpp.py 1.5b    # 1,5B, explicite
python server/serve_llamacpp.py 7b      # 7B
```

**Basculer = arrêter (Ctrl+C) puis relancer avec l'autre préréglage.**
L'alias exposé sur `/v1/models` change avec la taille (le script l'affiche
au démarrage) — mettez `DEEPSEEK_MODEL_NAME` à jour en conséquence côté
UVI (§5bis) à chaque bascule, sans quoi `DeepSeekClient` reçoit un HTTP
404 (même piège que `docs/SERVEUR_DEEPSEEK.md` §4).

| Préréglage | Fichier | Alias `/v1/models` = `DEEPSEEK_MODEL_NAME` |
|---|---|---|
| `1.5b` (défaut) | `models/DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf` | `deepseek-r1-distill-qwen-1.5b` |
| `7b` | `models/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf` | `deepseek-r1-distill-qwen-7b` |

Un chemin de fichier arbitraire est aussi accepté (alias générique
`deepseek-local-custom`) pour tester un autre GGUF sans l'ajouter aux
préréglages.

Lancement direct sans le script, si besoin (exemple avec le 7B) :

```bash
python -m llama_cpp.server \
  --model models/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf \
  --model_alias deepseek-r1-distill-qwen-7b \
  --host 127.0.0.1 --port 8000 \
  --n_ctx 4096 --n_threads -1 --n_gpu_layers 0 \
  --api_key "$DEEPSEEK_API_KEY"
```

### Les drapeaux qui comptent

**`--n_threads -1`** — Le défaut de llama-cpp-python est **2 threads**,
quel que soit le nombre de cœurs réels. Sur une machine à 4 cœurs, ça
laisserait la moitié inutilisée. `-1` utilise tous les cœurs logiques.

**`--n_ctx 4096`** — Contexte total (prompt + raisonnement + réponse).
Défaut de llama-cpp-python : 2048, court pour un modèle qui pense avant de
répondre. Monter plus haut coûte de la RAM et ralentit le traitement du
prompt ; 4096 est un compromis pour du CPU pur.

**`--n_gpu_layers 0`** — Explicite : aucune couche déportée sur GPU (il
n'y en a pas ici). Sur une machine qui *aurait* un GPU (même modeste),
augmenter cette valeur déchargerait une partie du calcul dessus.

**`--chat_format` non précisé** — Les GGUF de bartowski embarquent le
template de chat officiel du modèle dans leurs métadonnées ; laisser
llama-cpp-python le lire évite de dupliquer ici un format qui pourrait
diverger de ce que le fichier contient réellement.

### `<think>...</think>`

Contrairement à vLLM (`--reasoning-parser deepseek_r1`, voir
`docs/SERVEUR_DEEPSEEK.md`), llama-cpp-python n'a pas d'équivalent : les
balises `<think>` reviennent **dans** `content`. Ce n'est pas un problème
côté UVI — `DeepSeekClient._nettoyer_reponse` les retire déjà côté client
dans tous les cas (c'était documenté comme un filet pour vLLM ; ici c'est
le mécanisme principal).

---

## 4. Vérification

```bash
curl http://127.0.0.1:8000/v1/models
```

Le champ `id` doit valoir exactement `deepseek-r1-distill-qwen-7b` (fixé
par `--model_alias` ci-dessus) — c'est ce qui doit se retrouver dans
`DEEPSEEK_MODEL_NAME`, sans quoi `DeepSeekClient` reçoit un HTTP 404 (même
piège que documenté dans `docs/SERVEUR_DEEPSEEK.md` §4).

```bash
python3 server/smoke_test.py http://127.0.0.1:8000
```

**La première requête sera lente** (chargement du modèle en mémoire,
quelques dizaines de secondes) — normal, pas une panne.

---

## 5. Branchement côté UVI

Identique à `docs/SERVEUR_DEEPSEEK.md` §5, seule l'URL change (exemple
avec le préréglage `1.5b`, celui recommandé en §1 et §6) :

```bash
export SHION_BACKEND=deepseek
export SHION_SERVER_URL=http://127.0.0.1:8000
export DEEPSEEK_MODEL_NAME=deepseek-r1-distill-qwen-1.5b
export DEEPSEEK_API_KEY=ma_cle_api
python uvi_core.py
```

Ou, pour un démarrage automatique sans rien exporter à la main, dans
`.shion.local.env` (non versionné, voir `.gitignore` et
`_charger_config_shion_locale` dans `uvi_core.py`) :

```
SHION_BACKEND=deepseek
SHION_SERVER_URL=http://127.0.0.1:8000
DEEPSEEK_MODEL_NAME=deepseek-r1-distill-qwen-1.5b
DEEPSEEK_API_KEY=ma_cle_api
DEEPSEEK_MAX_TOKENS=4096
```

### 5bis. En basculant vers le 7B

`DEEPSEEK_MODEL_NAME` doit suivre le préréglage lancé côté serveur (§3) --
sinon HTTP 404 immédiat. Pour le 7B :

```
DEEPSEEK_MODEL_NAME=deepseek-r1-distill-qwen-7b
```

Et augmentez très probablement `DEEPSEEK_REQUEST_TIMEOUT_S` (défaut 180s
côté client) -- voir §6, le 7B n'a pas terminé une requête de test même
bien au-delà de cette valeur sur cette machine.

---

## 6. Limitations

- **Vitesse sur cette machine : mesurée, pas praticable, cause identifiée.**
  Quatre requêtes de test, sur les 4 cœurs de ce poste (aucun GPU) :
  - 7B, `max_tokens=2048` (complétion générique) : toujours en cours
    après **22 minutes**, processus actif (temps CPU en hausse continue,
    confirmé par deux relevés à 5 s d'écart) — abandonné, pas un blocage,
    juste trop lent pour attendre la fin.
  - 7B, `max_tokens=150` (« quelle est la capitale de la France ? ») : pas
    terminé en **506 s** (coupé par le client à 500 s).
  - 7B, même requête, second essai : pas terminé en **902 s** (coupé à 900 s).
  - **1,5B**, même requête bornée à 150 tokens : pas terminé non plus en
    **851 s** — alors qu'un 1,5B est ~5x moins coûteux en calcul que le
    7B, ce qui élimine "le modèle est juste gros" comme explication
    suffisante.

  **Cause racine identifiée : CPU sans AVX2.** `(Get-CimInstance
  Win32_Processor).Name` renvoie `Intel(R) Pentium(R) CPU N3710 @
  1.60GHz` — un cœur Atom/Braswell (2016), 1,6 GHz, **sans jeu
  d'instructions AVX ni AVX2**. Le log de démarrage du serveur le confirme
  indirectement : la ligne `CPU : LLAMAFILE = 1 | OPENMP = 1 | REPACK = 1 |`
  ne mentionne aucun `AVX2 = 1`, contrairement à ce qu'affiche llama.cpp
  sur un CPU qui le supporte. Sans AVX2, les noyaux de calcul quantifiés
  de llama.cpp retombent sur un chemin générique nettement plus lent — un
  ordre de grandeur, pas un détail. Un modèle de raisonnement aggrave
  encore les choses (bloc `<think>` potentiellement long avant la réponse
  visible, donc plus de tokens réellement générés que ce que suggère la
  question posée), mais le facteur dominant ici est l'absence d'AVX2.

  **Sur une machine avec AVX2 (la quasi-totalité des CPU x86 depuis
  ~2013), attendez-vous à un tout autre résultat** — ce n'est donc pas une
  limite de ce document ou de cette approche en général, seulement de
  *cette* machine précise. **Utilisez le 1,5B en premier** dans tous les
  cas ; le 7B reste documenté pour un poste avec AVX2 (voire un GPU, où
  `docs/SERVEUR_DEEPSEEK.md`/vLLM est de toute façon le meilleur choix).
- **`DEEPSEEK_REQUEST_TIMEOUT_S=180` (défaut du client) est très
  insuffisant ici**, 7B comme 1,5B — les mesures ci-dessus le dépassent
  déjà largement sans même aboutir. À relever nettement (voire à ne pas
  compter dessus) tant que la vitesse réelle du modèle choisi n'a pas été
  vérifiée sur la machine cible.
- **Qualité.** Q4_K_M perd un peu de précision par rapport aux poids
  originaux bf16 servis par vLLM — généralement peu perceptible en usage
  courant, plus sensible sur du raisonnement mathématique très fin. Plus
  vrai encore en comparant le 1,5B au 7B : le 1,5B répondra plus vite mais
  avec un raisonnement nettement moins solide.
- **Un seul modèle chargé à la fois**, et il reste en RAM tant que le
  serveur tourne (~4,5 Go pour le 7B, ~1 Go pour le 1,5B, plus le cache KV
  du contexte).
- Ce document ne couvre pas l'accélération GPU de llama.cpp
  (`--n_gpu_layers` > 0, CUDA/Metal/Vulkan) : hors sujet ici puisque la
  machine ciblée n'a pas de GPU, mais l'option existe pour qui en a un.

---

## Sources

[bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF](https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF) ·
[documentation llama-cpp-python](https://llama-cpp-python.readthedocs.io/) ·
`docs/SERVEUR_DEEPSEEK.md` (variante GPU/vLLM de ce même document).
