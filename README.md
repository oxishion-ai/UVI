<img src="assets/uvi_logo.svg" width="72" height="72" alt="Logo UVI : trois arcs orbitaux bleu, cyan et violet autour d'un noyau central">

# UVI — atelier de programmation avec assistant IA intégré

UVI est un langage de script (lexer, parser, compilateur bytecode, machine
virtuelle et interpréteur tree-walker) livré avec son propre IDE (éditeur,
console, débogueur, canevas de dessin) et un assistant IA intégré,
**Shion**, exécuté localement et sans dépendance lourde.

## Identité visuelle

Le « noyau » UVI : trois arcs orbitaux — bleu, cyan, violet — autour d'un
point central, pour les trois mots du nom (**U**nivers · **V**ision ·
**I**ntelligence). Généré par [`assets/generer_logo.py`](assets/generer_logo.py)
sans aucune dépendance d'image (ni Pillow, ni bibliothèque externe — juste
`tkinter.PhotoImage` pour les PNG et du XML écrit à la main pour le SVG),
dans l'esprit « zéro framework » du reste du projet. Le même dessin sert
trois fois : le badge de la barre du haut de l'IDE (`dessiner_noyau_uvi`
dans `uvi_core.py`), l'icône de fenêtre (barre des tâches / alt-tab), et
ce SVG pour la documentation.

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
| `assets/` | Identité visuelle UVI (logo/icônes) et le script qui les génère |
| `docs/SHION_ARCHITECTURE.md` | Architecture complète de Shion (topologie, algorithme mémoire, plan de déploiement) |
| `docs/SERVEUR_DEEPSEEK.md` | Comment servir DeepSeek-R1 via vLLM en remplacement/complément de Shion |
| `server/` | Scripts du serveur DeepSeek/vLLM (`start_server.sh`, `smoke_test.py`, `serve_vllm.py`) |
| `UVI_Storage/` | Système de fichiers virtuel persistant de l'IDE (scripts, bibliothèque standard `.uvi`) |
| `uvi_auth.py` | Connexion Google (Supabase Auth) et niveau d'abonnement de l'app de bureau — voir `docs/COMPTES_ABONNEMENTS.md` |
| `supabase/` | Schéma de la base de données (comptes, abonnements) et fonction webhook Stripe |
| `web/site/` | Site vitrine UVI (statique, à déployer sur Netlify — voir `docs/DEPLOIEMENT_NETLIFY.md`) |
| `packaging/` | Empaquetage Windows (PyInstaller + Inno Setup) — voir `docs/EMPAQUETAGE_WINDOWS.md` |

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
- `docs/SERVEUR_DEEPSEEK.md` — déploiement du backend DeepSeek/vLLM (GPU requis).
- `docs/SERVEUR_LOCAL_CPU.md` — même backend DeepSeek, en local sur CPU via llama.cpp (sans GPU, sans Colab).
- `docs/DEPLOIEMENT_NETLIFY.md` — déployer le site vitrine sur Netlify depuis le dépôt privé.
- `docs/COMPTES_ABONNEMENTS.md` — comptes utilisateurs, connexion Google, abonnements UVI Pro/Plus (Supabase + Stripe).
- `docs/EMPAQUETAGE_WINDOWS.md` — empaqueter UVI en logiciel Windows téléchargeable (PyInstaller + Inno Setup).
