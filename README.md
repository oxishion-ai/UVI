# UVI — atelier de programmation avec assistant IA intégré

UVI est un langage de script (lexer, parser, compilateur bytecode, machine
virtuelle et interpréteur tree-walker) livré avec son propre IDE (éditeur,
console, débogueur, canevas de dessin) et un assistant IA intégré,
**Shion**, exécuté localement et sans dépendance lourde.

## Démarrage rapide

```bash
pip install numpy          # seule dépendance non-stdlib, pour Shion
python uvi_core.py
```

`SHION.ASK "question" -> reponse` fonctionne dès le premier lancement,
sans serveur à configurer : Shion tourne en process, dans l'IDE lui-même
(voir `docs/SHION_ARCHITECTURE.md` §8 pour les autres paliers de
déploiement — serveur partagé, matériel obsolète).

## Structure du dépôt

| Dossier / fichier | Contenu |
|---|---|
| `uvi_core.py` | Le langage UVI (lexer/parser/VM/interpréteur), l'IDE Tkinter, les clients IA (Shion, DeepSeek) |
| `shion/` | Moteur d'IA Shion : quantification, chargement mémoire paresseux, attention par fenêtre glissante, entraînement, serveur HTTP — voir `docs/SHION_ARCHITECTURE.md` |
| `docs/SHION_ARCHITECTURE.md` | Architecture complète de Shion (topologie, algorithme mémoire, plan de déploiement) |
| `docs/SERVEUR_DEEPSEEK.md` | Comment servir DeepSeek-R1 via vLLM en remplacement/complément de Shion |
| `server/` | Scripts du serveur DeepSeek/vLLM (`start_server.sh`, `smoke_test.py`, `serve_vllm.py`) |
| `UVI_Storage/` | Système de fichiers virtuel persistant de l'IDE (scripts, bibliothèque standard `.uvi`) |

## Deux backends pour SHION.ASK

| | `SHION_BACKEND=shion` (défaut) | `SHION_BACKEND=deepseek` |
|---|---|---|
| Où ça tourne | En process ou serveur local léger (`shion/server.py`) | Serveur vLLM distant, GPU requis |
| Dépendances | NumPy uniquement | vLLM + GPU (voir `docs/SERVEUR_DEEPSEEK.md`) |
| Qualité de réponse | Modèle de démonstration (~10^5 à 10^8 paramètres selon le palier entraîné) | DeepSeek-R1-Distill-Qwen-7B, état de l'art à cette taille |
| Coût | 0 $, tourne sur machine modeste | Nécessite un GPU |

Basculer d'un backend à l'autre ne coûte qu'une variable d'environnement :

```bash
export SHION_BACKEND=deepseek   # ou "shion" pour revenir au moteur local
```

## Documentation

- `docs/SHION_ARCHITECTURE.md` — architecture de Shion, de bout en bout.
- `docs/SERVEUR_DEEPSEEK.md` — déploiement du backend DeepSeek/vLLM.
