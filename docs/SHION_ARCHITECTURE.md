# Architecture de Shion — IA frugale, bare-metal, budget zéro

> **Rôle** : ce document est la spécification d'architecture de Shion, le
> modèle d'IA maison d'UVI, rédigée avec la casquette d'architecte
> IA senior / ingénieur en optimisation bare-metal. **Mission** : décrire
> un modèle le plus léger, le moins cher et le plus performant possible,
> conçu pour un budget de 0 $, un CPU lent et une RAM sévèrement limitée —
> l'élégance algorithmique et l'optimisation bas niveau plutôt que la force
> brute matérielle.
>
> Ce document n'est **pas que du papier** : chaque mécanisme décrit
> ci-dessous est implémenté, testé et mesuré dans `shion/` à la racine de
> ce dépôt, et branché dans `uvi_core.py` derrière `SHION.ASK`. Les
> chiffres cités (paramètres, taux de compression, erreur de gradient,
> courbe de perte, statistiques de pagination mémoire) sont des **mesures
> réelles**, obtenues en exécutant le code de ce dépôt le jour de
> rédaction, pas des estimations. Voir §0 pour la table fichier ↔ rôle, et
> chaque section pour la commande exacte utilisée.

---

## §0. État d'implémentation dans ce dépôt

| Fichier | Rôle | Statut |
|---|---|---|
| `shion/config.py` | Topologie (Nano/Micro/Base), §1 | Implémenté, testé |
| `shion/quantization.py` | INT4 + ternaire, §3 | Implémenté, testé (round-trip vérifié) |
| `shion/prune.py` | Élagage magnitude, §4 | Implémenté, testé (impact mesuré) |
| `shion/model.py` | Transformer + fenêtre glissante + backprop manuel, §5, §7 | Implémenté, **gradient-check validé** |
| `shion/memory.py` | Chargement paresseux, cache LRU, §6 | Implémenté, testé (éviction vérifiée) |
| `shion/tokenizer.py` | Tokenizer octet | Implémenté, testé |
| `shion/engine.py` | Inférence, cache KV circulaire O(fenêtre) | Implémenté, testé |
| `shion/train.py` | Micro-batching + Adam délesté sur disque, §7 | Implémenté, **checkpoint réellement entraîné** |
| `shion/distill.py` | Génération de dataset par distillation, §7.1 | Implémenté (nécessite un professeur joignable pour tourner) |
| `shion/serving.py` | Client réseau + repli moteur local, §8 | Implémenté, testé |
| `shion/server.py` | Serveur HTTP OpenAI-compatible, stdlib seule, §8 | Implémenté |
| `uvi_core.py` (`_build_shion_client`, `_ShionNativeAdapter`) | Branchement SHION.ASK | Implémenté, testé (réponse bout en bout obtenue) |
| Rust core (§2.3) | Noyau bas niveau pour Shion-Micro/Base | **Non codé cette session** — spécifié, voir §2.3 pour pourquoi et pour le plan |

Reproduire les mesures de ce document :

```bash
python -m shion.quantization    # §3
python -m shion.memory          # §6
python -m shion.model           # §7.3 (gradient-check)
python -m shion.train           # §7.3 (entraînement réel de Shion-Nano)
python -m shion.prune           # §4
python -m shion.server          # §8 (serveur local)
```

---

## §1. Topologie du modèle

**Type d'architecture retenu : Transformer causal pur (decoder-only),
attention par fenêtre glissante, MLP SwiGLU, RMSNorm, embedding/tête
liés.** Pas de MoE, pas de Mamba/RNN. Justification, parce que le choix
inverse était sur la table :

- **MoE (Mixture of Experts) restreint** — écarté pour Shion. Un MoE
  réduit le calcul *actif* par token, mais pas le nombre de paramètres à
  **stocker** (tous les experts vivent sur le disque même si un seul est
  activé) : sur un budget contraint en **disque et RAM**, pas seulement en
  FLOPs, ça n'aide pas la contrainte qui compte le plus ici. Le routage
  ajoute aussi de la complexité d'implémentation (donc de risque de bug)
  pour un gain qui ne se matérialise qu'à partir de plusieurs centaines de
  millions de paramètres — hors de la plage visée par Nano/Micro.
- **Mamba / RNN à état linéaire** — sérieusement considéré : compute et
  mémoire strictement linéaires en longueur de séquence, sans même le
  correctif de fenêtre qu'un Transformer demande. Écarté pour LA
  PREMIÈRE implémentation pour une raison d'ingénierie concrète, pas de
  qualité : ses équations de rétropropagation à travers le temps (BPTT)
  sont moins standardisées et plus faciles à implémenter *silencieusement
  faux* qu'une attention softmax masquée, dont le gradient est
  extrêmement bien documenté et vérifiable par différences finies (voir
  §7.3). Sur un projet où « corriger les bugs » est un objectif explicite,
  partir du mécanisme le plus vérifiable était le bon calcul de risque.
  **Reste le candidat naturel pour une itération future** une fois
  l'attention par fenêtre éprouvée en production (voir §10).
- **Attention par fenêtre glissante (retenu)** — voir §5. Compromis
  délibéré : garde le mécanisme d'attention standard (gradient bien connu,
  qualité éprouvée à petite échelle) tout en bornant son coût, ce qui
  couvre la contrainte « empreinte mémoire fixe » sans changer de famille
  de modèle.

### Trois paliers (voir `shion/config.py`, `PRESETS`)

| Palier | Paramètres (mesuré) | Taille INT4 | d_model | Couches | Têtes | Fenêtre | Statut dans ce dépôt |
|---|---:|---:|---:|---:|---:|---:|---|
| **Shion-Nano** | **115 264** | **0,06 Mo** | 64 | 2 | 4 | 64 | **Livré, entraîné, testé** (`shion/weights/shion-nano/`) |
| Shion-Micro | 14 262 144 | 7,13 Mo | 384 | 8 | 6 | 256 | Décrit, config prête, **non entraîné** (voir §10) |
| Shion-Base | 201 626 624 | 100,81 Mo | 1024 | 16 | 16 | 1024 | Décrit, config prête, **non entraîné** (voir §10) |

Pourquoi ces trois-là précisément, et pas un seul « le plus gros
possible » :

- **Nano** est calibré pour être **entraînable en quelques minutes sur un
  CPU standard, sans aucune infrastructure** — c'est la preuve
  d'architecture livrée dans ce dépôt (voir §7.3 pour la courbe de perte
  réelle). Il n'est **pas** compétent comme assistant (voir §10) ; sa
  valeur est de prouver que le pipeline complet — quantification,
  pagination mémoire, fenêtre glissante, entraînement délesté, service
  HTTP — fonctionne réellement de bout en bout, pas seulement sur le
  papier.
- **Micro** vise la plage où un modèle byte-level commence à produire des
  réponses courtes globalement cohérentes (par analogie avec des modèles
  publiés de taille comparable) tout en restant inférable sur CPU en un
  temps raisonnable — la cible réaliste pour un usage quotidien local.
- **Base** est le plafond raisonnable de la philosophie « bare-metal
  frugal » : au-delà, même quantifié à 4 bits, l'entraînement redemande de
  facto un GPU pour converger dans un temps humain, ce qui sort du
  périmètre « budget 0 $ ».

Le nombre de paramètres n'est **jamais approximé** dans ce document ou
dans le code : `ShionConfig.param_count()` (`shion/config.py`) le calcule
exactement à partir de la topologie, et un test compare ce calcul au
nombre réel de tenseurs produits par `init_params` — un changement de
config qui ferait dériver la taille annoncée casserait ce test plutôt que
de laisser la documentation mentir silencieusement.

---

## §2. Le moteur d'inférence (Core Logic)

### 2.1 Aucun framework lourd — ce qui est vraiment livré

Le moteur (`shion/model.py`, `shion/engine.py`) **n'utilise ni PyTorch, ni
TensorFlow, ni JAX** — aucune dépendance d'autodiff. La seule dépendance
externe est **NumPy** (tableaux + BLAS), et le passage arrière
(rétropropagation) de `shion/model.py` est **écrit à la main**, fonction
par fonction, vérifié par différences finies (§7.3) plutôt que délégué à
un moteur d'autodiff générique. C'est délibéré et documenté dans
`shion/model.py` : un framework aurait cousu l'appariement forward/backward
dans de l'héritage, invisible et donc plus difficile à auditer bogue par
bogue ; des fonctions jumelles explicites (`attention_forward` /
`attention_backward`, etc.) rendent cet appariement lisible et vérifiable.
`shion/serving.py` (le client réseau) n'importe **même pas NumPy** au
niveau module — voir §2.4.

### 2.2 Le choix Python/NumPy pour CE dépôt, et la cible Rust

Le cahier des charges demande un noyau « proche de la machine » (Rust) pour
contrôler l'allocation mémoire à la main et maximiser le CPU. C'est la
cible **long terme**, honnêtement pas ce qui est livré cette session, pour
une raison d'ingénierie assumée plutôt que cachée :

- Ce dépôt est un **IDE Python** (`uvi_core.py`, Tkinter) qui appelle
  `SHION.ASK` **dans le même process ou via HTTP local**. Une
  implémentation Python/NumPy s'intègre, se teste et se répare *dans la
  même session de travail* que le reste du projet, sans étape de
  compilation ni de toolchain supplémentaire à faire fonctionner sur la
  machine de l'utilisateur — cohérent avec la contrainte « budget 0 $,
  aucune installation superflue » déjà appliquée ailleurs dans ce dépôt
  (voir les commentaires sur l'isolation de `torch`/`shion.serving` dans
  `uvi_core.py`).
- NumPy **n'est pas un framework de deep learning** : c'est une couche
  fine sur BLAS (calcul matriciel compilé, lui-même en C/Fortran). Le
  contrôle mémoire manuel (§6), la quantification à la main (§3), le
  chargement par memory-mapping (§6) sont déjà « bare-metal » au sens où
  ce document l'entend : aucune magie cachée, chaque octet chargé est
  explicite et auditable.
- Ce que Python **coûte réellement** : la boucle d'interprétation autour
  des appels NumPy (overhead visible sur l'entraînement, §7.3 : ~1,7 s par
  pas sur Shion-Nano). C'est le coût que paierait précisément une
  réécriture Rust — et l'architecture ci-dessous est conçue pour que cette
  réécriture soit un **remplacement**, pas une refonte.

### 2.3 Esquisse du noyau Rust (spécification, pas du code livré)

Le contrat que le noyau Rust devra honorer est déjà figé par
l'implémentation Python — c'est ce qui rend la portabilité réelle plutôt
qu'un vœu pieux :

- **Le format binaire des poids** (§6) est un format de fichiers plats,
  sans dépendance à `pickle`/NumPy à la lecture : deux tableaux d'octets
  bruts (`*.packed.npy`, `*.scales.npy`, en-tête NumPy minimal) + un
  `meta.json`. Un lecteur Rust n'a besoin de comprendre que l'en-tête NumPy
  v1 (format documenté, trivial) — pas d'appeler dans le runtime NumPy.
- **La quantification** (§3) est arithmétique entière pure (nibbles,
  échelles `f16`) — traduisible en Rust ligne à ligne.
- **L'attention par fenêtre glissante en génération** (`engine.py`,
  `_RollingKV`) est déjà écrite comme un **tampon circulaire de taille
  fixe** — c'est *exactement* la structure de données qu'on écrirait en
  premier lieu en Rust (`[f32; WINDOW * D_MODEL]`, pas de `Vec`
  redimensionné).

Squelette (illustratif, non compilé — le contrat, pas le produit fini) :

```rust
// shion-core/src/tensor.rs
#[repr(C)]
pub struct QuantizedTensor<'a> {
    packed: &'a [u8],      // nibbles empaquetés, cf. quantization.py
    scales: &'a [f16],     // une échelle par groupe
    shape: (usize, usize),
    group_size: usize,
}

impl<'a> QuantizedTensor<'a> {
    // Dé-quantifie UNIQUEMENT les lignes demandées : portage direct de la
    // discussion memory.py §6.3 sur l'embedding par ligne, jamais fait en
    // Python faute de temps cette session — le premier candidat naturel
    // pour le noyau Rust, où le coût d'un accès aléatoire fin est plus
    // supportable qu'en Python.
    pub fn dequantize_rows(&self, rows: &[usize], out: &mut [f32]) { /* ... */ }
}

// shion-core/src/attention.rs — anneau de taille FIXE, allocation UNIQUE
// à l'initialisation, jamais de réallocation pendant la génération.
pub struct RollingKV {
    k: Box<[f32]>,   // window * d_model, alloué une fois
    v: Box<[f32]>,
    pos: usize,
    filled: usize,
}
```

**Plan de branchement, sans toucher `uvi_core.py`** : le noyau Rust
exposerait une bibliothèque partagée (`cdylib`) avec une frontière C ABI
(via `PyO3`/`maturin`, ou un simple `ctypes.CDLL` côté Python) qui
implémente exactement l'interface de `ShionEngine.generate`/`.chat`
(`shion/engine.py`). `shion/serving.py` et `shion/server.py` continueraient
d'appeler `ShionEngine` sans modification — seul le contenu de
`shion/engine.py` basculerait vers des appels FFI. `uvi_core.py`, lui, ne
verrait **jamais** la différence : il parle à `ShionServingClient` (§8),
qui ne change pas de contrat. C'est la même propriété que la bascule
DeepSeek → Shion documentée dans `uvi_core.py` (« coûte zéro ligne »),
appliquée un niveau plus bas.

### 2.4 « Scripts noyaux » / routage sans latence

Le cahier des charges demande un environnement d'exécution basé sur des
scripts natifs optimisés pour un routage sans latence. Traduit dans ce
dépôt : `shion/serving.py` **n'ajoute aucune couche RPC quand ce n'est pas
nécessaire**. En mode embarqué (§8, palier 1 — le cas normal pour un
utilisateur solo), `ShionServingClient.complete()` appelle
`ShionEngine.chat()` **directement, en process, sans sérialisation JSON ni
socket** : le seul chemin HTTP est celui, optionnel, vers un serveur
**dédié et distant** (§8, palier 2), utile pour du partage, jamais imposé
pour un usage local. C'est la version « scripts noyaux sans latence » de
ce cahier des charges : pas de machinerie générique à charger avant de
répondre, l'appelant et le moteur sont dans le même interpréteur par
défaut.

---

## §3. Quantification (FP32 → INT4 / ternaire)

Implémenté dans `shion/quantization.py` : quantification **symétrique par
groupe** (un facteur d'échelle `f16` toutes les `group_size` valeurs), pas
une quantification globale par tenseur — un groupe capture mieux la
variance locale des poids qu'une échelle unique sur toute une matrice, au
prix d'un tout petit surcoût de stockage (une échelle par groupe, pas par
poids).

**Mesures réelles** (`python -m shion.quantization`, matrice aléatoire
37×53, `group_size=16`) :

| Schéma | Erreur moyenne absolue (aller-retour) | Taux de compression vs FP32 |
|---|---:|---:|
| INT4 symétrique | 0,0210 | **×6,4** |
| Ternaire {-1,0,+1} | 0,1037 | **×10,6** |

Le ternaire n'est **pas** le défaut de `ShionConfig` (`quant_bits=4`) : sur
une matrice de cette taille, son erreur est ~5× plus élevée que l'INT4
pour ~1,7× de compression supplémentaire — mauvais rapport à cette échelle.
Il reste implémenté et testé comme option pour les tenseurs les plus
tolérants (voir la documentation de `quantize_ternary` : candidats naturels
= grandes matrices de MLP, où la redondance interne absorbe mieux
l'erreur qu'une petite matrice d'attention).

Format sur disque : deux fichiers `.npy` **bruts** par tenseur (`*.packed.
npy` uint8, `*.scales.npy` float16) — jamais de `.npz`/zip. C'est ce détail
précis qui rend `numpy.load(mmap_mode="r")` (§6) capable de ne lire que
les octets réellement nécessaires : un `.npz` est un zip, qui doit être
au moins partiellement décompressé pour qu'un memmap ait un sens.

---

## §4. Élagage (pruning) agressif

Implémenté dans `shion/prune.py` : deux granularités, **non structurée**
(poids par poids, la plus fine, ne réduit que le stockage sur CPU
générique) et **structurée** (lignes entières retirées par norme L2 la
plus faible, réduit aussi le calcul réel — voir la docstring du module
pour la distinction, qui conditionne où chaque mode a un intérêt réel).

**Mesure réelle**, sur `shion/weights/shion-nano` (checkpoint entraîné,
§7.3), élagage non structuré de `layer.0.mlp_win` :

| Sparsité visée | Sparsité réelle | Perte avant | Perte après | Δ |
|---:|---:|---:|---:|---:|
| 10 % | 10,9 % | 2,8949 | 2,8949 | +0,0000 |
| 30 % | 30,0 % | 2,8949 | 2,8949 | +0,0000 |
| 50 % | 50,0 % | 2,8949 | 2,8947 | −0,0001 |
| 70 % | 70,0 % | 2,8949 | 2,8945 | −0,0003 |

**Lecture honnête de ce résultat**, pas une victoire à prendre pour argent
comptant : Shion-Nano, entraîné 250 pas sur 4,4 Ko de corpus, est
**sous-entraîné** (voir §7.3 et §10) — beaucoup de poids de cette matrice
sont encore proches de leur initialisation aléatoire, donc de faible
magnitude, donc peu affectés par un seuil de magnitude. Ce test démontre
que **le mécanisme fonctionne et mesure correctement son impact** (c'est
son unique rôle ici) ; il ne démontre PAS que Shion-Micro/Base
supporteront 70 % de sparsité sans perte — un modèle réellement convergé a
une distribution de magnitudes bien plus étalée, et c'est sur un tel
modèle que ce module devra être ré-exécuté avant toute conclusion sur le
taux de sparsité viable en production. Shion-Nano lui-même n'est **pas**
élagué dans le checkpoint livré (voir §0) : à 115 k paramètres, il est déjà
au plancher de ce qui reste entraînable sur un corpus de cette taille.

---

## §5. Attention linéaire par fenêtre glissante

Remplace l'attention standard O(N²) par une attention **causale par
fenêtre glissante** de portée fixe `window` : la position *t* n'attend
plus jamais que les `window` positions qui la précèdent (au lieu de
toutes les positions depuis le début), ce qui borne le coût à **O(N·
window)** — linéaire en longueur de séquence, à fenêtre fixée.

Deux implémentations **volontairement différentes** du même calcul,
choisies pour l'usage réel de chacune (documenté en tête de
`shion/engine.py`) :

- **`shion/model.py` (entraînement)** : matrice de scores `(T, T)`
  **masquée** — hors-fenêtre mis à `-∞` avant softmax. Simple à dériver et
  à vérifier par différences finies (§7.3), au prix d'un peu de calcul
  gâché en zéros au-delà de la fenêtre. Standard dans les frameworks de
  production pour l'entraînement (le masquage est la méthode la plus sûre
  à auditer), même quand l'inférence, elle, ne matérialise jamais cette
  matrice.
- **`shion/engine.py` (génération)** : **`_RollingKV`**, un tampon
  circulaire qui ne garde que les `window` dernières clés/valeurs, PAR
  COUCHE. Une conversation de 10 000 tokens ne coûte pas plus cher en
  mémoire qu'une de 100 — c'est ce détail précis qui tient la promesse
  « empreinte mémoire fixe même avec un long contexte », vérifiable
  directement dans le code (`_RollingKV.push`, taille du tampon jamais
  redimensionnée après construction).

---

## §6. Algorithme de gestion mémoire

**C'est le livrable demandé explicitement — implémenté, pas seulement
pseudocodé, dans `shion/memory.py`.**

### 6.1 Principe

1. Les poids de **chaque couche** sont écrits sur disque en fichiers `.npy`
   **bruts séparés** (§3), jamais un zip/`.npz`.
2. À l'ouverture, **rien n'est chargé en RAM** : `numpy.load(mmap_mode=
   "r")` établit un mapping mémoire virtuelle → fichier. Les pages ne sont
   lues du disque **que lorsqu'elles sont réellement accédées** — c'est le
   système d'exploitation qui fait ce travail, pas du code Python
   réimplémentant une pagination à la main (plus rapide, plus fiable).
3. Au moment de dé-quantifier une couche pour l'utiliser, le résultat FP32
   est gardé dans un **cache résident borné** à `max_resident_layers`
   couches.
4. Quand le cache déborde, la couche **la plus anciennement utilisée**
   (LRU) est **explicitement libérée** (`dict.clear()` sur ses tableaux)
   — pas laissée à la merci du ramasse-miettes générationnel, qui pourrait
   la garder vivante plusieurs passes et faire dériver la RAM résidente
   au-dessus du budget annoncé.

### 6.2 Pseudocode

```
fonction get_layer(i):
    si i dans cache_resident:
        marquer i comme récemment utilisé          # LRU touch
        retourner cache_resident[i]

    poids_bruts <- mmap_lire(fichier_couche(i))     # zéro RAM consommée ici
    poids_fp32  <- dequantifier(poids_bruts)          # RAM consommée ICI seulement
    cache_resident[i] <- poids_fp32

    tant que taille(cache_resident) > max_resident:
        j <- le moins récemment utilisé de cache_resident
        libérer(cache_resident.retirer(j))            # RAM rendue au système

    retourner cache_resident[i]
```

Implémentation réelle : `LazyLayerCache.get_layer` (`shion/memory.py`),
avec `collections.OrderedDict` pour l'ordre LRU en O(1) amorti.

### 6.3 Mesures réelles

Test dédié (`python -m shion.memory`, `max_resident_layers=1`) :
`layer_loads=2, evictions=1` pour deux couches consécutivement demandées
— comportement exact attendu.

Sous charge réelle (génération de 40 tokens via `ShionEngine.chat`,
`max_resident_layers=1` volontairement agressif pour forcer la pagination
à chaque token) : **236 chargements depuis le disque, 235 évictions**. Le
modèle a 2 couches ; avec une seule couche résidente autorisée, chaque
étage du forward évince nécessairement l'autre — la stratégie de pagination
est donc vérifiée sous le pire cas réaliste, pas seulement sur un scénario
favorable.

### 6.4 Prochaine étape documentée, non implémentée cette session

L'embedding (table complète) est actuellement chargé et dé-quantifié
**en un bloc**, résident en permanence (`GlobalWeights.embed` — voir sa
docstring). Négligeable pour Shion-Nano (260 lignes) ; pour un vocabulaire
de plusieurs dizaines de milliers de sous-mots (hors périmètre de Shion,
qui reste byte-level, §0), la même logique de pagination devrait
s'appliquer **ligne par ligne** à l'embedding — direction déjà repérée
comme premier candidat pour le noyau Rust (§2.3, `dequantize_rows`).

---

## §7. Pipeline d'entraînement « Zero-Budget »

### 7.1 Distillation de connaissances

`shion/distill.py` : Shion (l'élève) apprend à partir des réponses d'un
**modèle professeur open source déjà présent dans ce projet** —
`DeepSeekClient` (`uvi_core.py`, DeepSeek-R1-Distill-Qwen-7B, licence MIT,
backend `SHION_BACKEND=deepseek`). Aucune dépendance réseau
supplémentaire : le professeur est n'importe quel callable `str -> str`
respectant le contrat déjà en place (`LLMClient.complete`), donc **zéro
clé API ni quota distincts** à créer pour distiller Shion. (L'assistant
ERIS/Gemini, qui pouvait auparavant jouer ce rôle de repli, a depuis été
retiré du projet — voir uvi_core.py, `llm_client_prioritaire`.)

```python
from shion.distill import build_dataset
from uvi_core import DeepSeekClient
teacher = DeepSeekClient()
build_dataset(teacher.complete, prompts, "shion/corpus/distillation.jsonl")
```

Le dataset produit (`.jsonl` de paires question/réponse) est ensuite
transformé en texte d'entraînement par `pairs_to_training_text` (gabarit
`"Q: ...\nR: ...\n\n"`) et consommé par `shion/train.py` exactement comme
un corpus brut — **une seule boucle d'entraînement** pour les deux modes
(auto-supervisé et distillation), pas deux chemins de code à maintenir en
parallèle. `ShionEngine.chat` (`shion/engine.py`) reproduit le même
gabarit à l'inférence — condition nécessaire pour qu'un modèle causal
générique sans gabarit de conversation appris séparément (Shion n'a pas la
distinction system/user/assistant d'un DeepSeek-R1) réponde de façon
structurée.

**Non exécuté dans cette session** : générer un vrai dataset de
distillation demande un professeur réellement joignable (serveur vLLM ou
clé Gemini), indisponible dans cet environnement d'exécution. Le
mécanisme est implémenté et prêt ; le checkpoint livré (§7.3) est
entraîné en auto-supervisé sur `shion/corpus/seed_corpus.txt` faute de
professeur disponible ici.

### 7.2 Micro-batching et délestage disque

`shion/train.py` :

- **Micro-batching** : chaque micro-lot est **une seule séquence**
  (`model.forward_loss`/`model.backward` n'opèrent que sur une séquence à
  la fois, §5) ; les gradients de `grad_accum_steps` micro-lots sont
  **accumulés** dans un dictionnaire avant un seul pas d'optimiseur. Le
  résultat mathématique est identique à un vrai batch (moyenne des
  gradients) ; le pic mémoire est celui d'**une** séquence, pas de
  `grad_accum_steps` séquences simultanées.
- **Délestage disque de l'optimiseur** (`OffloadedAdam`) : les deux
  tableaux d'état d'Adam (moments `m` et `v`, deux fois la taille du
  modèle en FP32 — le plus gros poste mémoire de l'entraînement après les
  paramètres eux-mêmes) sont des **`numpy.memmap`**, pas des tableaux RAM
  classiques. Le système d'exploitation ne garde en RAM que les pages
  **réellement touchées** à chaque pas, et peut en évincer d'autres sous
  pression mémoire — le « transfert intelligent disque ↔ RAM » demandé,
  délégué à un mécanisme noyau déjà correct plutôt que réimplémenté à la
  main (même philosophie qu'en §6 pour les poids).

### 7.3 Résultats mesurés

**Gradient-check** (`python -m shion.model`) — condition sine qua non
avant de faire confiance à `train.py` : chaque gradient analytique de
`backward()` est comparé à une dérivée numérique par différences finies,
sur un modèle jouet en float64 (pour ne pas confondre bug réel et bruit
d'arrondi FP32) :

```
écart relatif max (gradient numérique vs analytique) = 1.64e-05
```

Largement sous le seuil de tolérance (5e-3) — la rétropropagation
manuelle de `shion/model.py` (RMSNorm, attention par fenêtre, MLP SwiGLU,
embedding lié) est **vérifiée correcte**, pas supposée.

**Entraînement réel** de Shion-Nano (`python -m shion.train`), 250 pas,
`seq_len=64`, `grad_accum_steps=4`, corpus de 4 416 caractères
(`shion/corpus/seed_corpus.txt`) :

```
pas   50/250   perte(moy. 50 derniers)=3.8191
pas  100/250   perte(moy. 50 derniers)=3.1045
pas  150/250   perte(moy. 50 derniers)=3.1069
pas  200/250   perte(moy. 50 derniers)=3.0544
pas  250/250   perte(moy. 50 derniers)=3.0857
perte 5.5116 -> 3.0857
```

Temps mesuré : ~430 s sur le poste de développement (~1,7 s/pas) — cohérent
avec le coût Python/NumPy discuté en §2.2, sur un modèle qui reste
entraînable **en moins de dix minutes sans aucune infrastructure**.

---

## §8. Plan de déploiement

Trois paliers, du plus frugal au plus partagé — **implémentés**, pas
seulement décrits (`shion/serving.py`, `shion/server.py`) :

### Palier 1 — Embarqué en process (défaut, zéro administration)

`ShionServingClient.complete()` (`shion/serving.py`) essaie d'abord un
serveur HTTP dédié (`SHION_SERVER_URL`) ; s'il est **injoignable**
(connexion refusée — pas une erreur d'un serveur qui répond mal, voir la
distinction dans le code), il **bascule automatiquement** sur
`ShionEngine` **dans le même process Python**, sans configuration
supplémentaire. C'est le mode par défaut d'UVI (`SHION_BACKEND=shion`,
§9) : ouvrir l'IDE et taper `SHION.ASK` fonctionne sans qu'aucun serveur
n'ait été démarré. Empreinte : un seul interpréteur Python, la RAM bornée
par `max_resident_layers` (§6), le disque par la taille quantifiée du
checkpoint (0,06 Mo pour Nano, §1).

### Palier 2 — Serveur dédié local ou LAN (usage partagé)

`shion/server.py` : serveur HTTP **API OpenAI-compatible**
(`/v1/models`, `/v1/chat/completions`), écrit avec `http.server` de la
stdlib **exclusivement** — aucune dépendance supplémentaire
(pas de Flask/FastAPI/uvicorn), cohérent avec §2.1. Un `ShionEngine`
unique est partagé entre toutes les requêtes (verrou global, §
`shion/server.py`) — suffisant pour « une poignée d'utilisateurs sur un
LAN », le même profil d'usage que le serveur vLLM DeepSeek déjà documenté
dans `uvi_core.py`/`docs/SERVEUR_DEEPSEEK.md`, en très largement plus léger
(zéro GPU, zéro dépendance lourde à installer).

```bash
python -m shion.server --port 8000 --preset shion-nano
# côté UVI :
export SHION_BACKEND=shion
export SHION_SERVER_URL=http://<cette-machine>:8000
```

### Palier 3 — Matériel obsolète / smartphone

Ce qui rend ce palier réaliste, listé dans l'ordre où il compte :

1. **Aucune dépendance native lourde.** NumPy est disponible via `pip`
   sur toute plateforme avec un interpréteur Python — y compris Termux
   (Android) et un Raspberry Pi, déjà les cibles explicites documentées
   ailleurs dans ce dépôt (`ShionServingClient`/`serving.py` historique).
   Aucune compilation CUDA, aucun runtime GPU.
2. **Poids quantifiés minuscules.** Shion-Nano : 0,06 Mo. Shion-Micro
   (cible réaliste pour un usage quotidien, §1) : 7,13 Mo — tient sur
   n'importe quel stockage, y compris embarqué.
3. **RAM bornée par construction, pas par accident.** `max_resident_
   layers` (§6) est un paramètre explicite : sur un appareil à 512 Mo de
   RAM disponible, le fixer à 1 ou 2 borne la RAM résidente du modèle à
   quelques mégaoctets, indépendamment du nombre total de couches du
   modèle choisi.
4. **Empaquetage.** `shion/` est un paquet Python autonome (une seule
   dépendance : NumPy) : `pip install numpy && python -m shion.server`
   suffit. Pour un smartphone via Termux : identique, `pip` fonctionne
   nativement dans Termux. Pas de conteneur, pas de runtime spécifique à
   installer — la seule "installation" est celle de Python lui-même,
   déjà présente sur la plupart des distributions Linux embarquées.

---

## §9. Branchement dans `uvi_core.py`

Changements apportés à `uvi_core.py` pour rendre ce qui précède
**réellement fonctionnel**, pas seulement présent sur disque à côté :

1. **`SHION_BACKEND` passe à `"shion"` par défaut** (précédemment
   `"deepseek"`, le temps que Shion soit bloqué en développement — voir le
   commentaire historique conservé dans le code). `SHION_BACKEND=deepseek`
   reste disponible et complet pour qui a un serveur vLLM et veut la
   meilleure qualité de réponse disponible aujourd'hui — Shion-Nano ne
   rivalise PAS avec un modèle de 7 milliards de paramètres (§10).
2. **`_ShionNativeAdapter`** (nouvelle classe) : traduit les exceptions du
   paquet `shion` (qui ne peut pas dépendre de `uvi_core.py`, dépendance
   circulaire) vers `LLMClientError`, pour que les points de code qui
   interceptent spécifiquement `except LLMClientError`
   (`_eval_expr_or_eris`, l'onglet de discussion) continuent de fonctionner
   sans traceback brut — un bug latent qui n'était simplement jamais
   exercé tant que `SHION_BACKEND=shion` n'avait aucun paquet `shion` à
   charger.
3. **`_build_shion_client()`** construit désormais un `ShionServingClient`
   réel (enveloppé par l'adaptateur ci-dessus) quand `HAS_SHION_NATIVE` est
   vrai — ce qui est désormais le cas, puisque `shion/` existe dans ce
   dépôt.

Vérifié bout en bout (voir le test d'intégration exécuté pour ce
document) : `uvi_core._build_shion_client()` construit un
`_ShionNativeAdapter`, et `.complete("...")` retourne une vraie réponse du
moteur Shion embarqué — sans serveur externe, sans configuration.

---

## §10. Limites assumées et prochaines étapes

À dire clairement plutôt qu'à laisser découvrir :

- **Shion-Nano n'est pas un assistant compétent.** ~115 k paramètres,
  tokenizer octet, 250 pas d'entraînement sur 4,4 Ko de corpus : les
  réponses générées (voir les tests de ce document) sont des suites de
  caractères plausibles côté distribution de lettres, **pas du texte
  cohérent**. C'est attendu et volontaire à cette échelle — la valeur
  livrée cette session est l'**architecture qui fonctionne bout en bout**
  (quantification, pagination mémoire, fenêtre glissante, entraînement
  vérifié par gradient-check, service HTTP), pas la qualité du modèle
  Nano lui-même.
- **Shion-Micro/Base ne sont pas entraînés.** Leur config existe et leur
  nombre de paramètres est calculé exactement (§1), mais les entraîner
  jusqu'à une qualité utile demande un vrai corpus (distillation à
  grande échelle, §7.1) et des heures/jours de calcul CPU — hors du
  périmètre d'une session.
- **Le noyau Rust n'est pas codé** (§2.3) — spécifié précisément pour
  qu'un portage soit un remplacement mécanique de `shion/engine.py`, pas
  une refonte d'architecture.
- **L'embedding n'est pas paginé ligne par ligne** (§6.4) — sans
  conséquence pour un vocabulaire de 260 jetons, à corriger avant
  d'envisager un tokenizer par sous-mots.
- **Un Mamba/RNN reste l'itération naturelle** une fois l'attention par
  fenêtre éprouvée (§1) — coût linéaire par construction, sans même la
  fenêtre comme paramètre à régler.
