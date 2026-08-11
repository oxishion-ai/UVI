"""shion/distill.py — Distillation de connaissances : Shion (l'élève)
apprend à partir des réponses d'un modèle professeur déjà entraîné.

CE MODULE NE DÉPEND D'AUCUN CLIENT LLM PARTICULIER. `teacher_complete_fn`
est n'importe quel callable `str -> str` -- en pratique, l'un des clients
DÉJÀ PRÉSENTS dans uvi_core.py (DeepSeekClient.complete, ErisClient.complete
ou tout objet respectant le même contrat LLMClient). Shion n'a donc besoin
d'AUCUNE dépendance réseau propre pour se distiller : il réutilise
l'infrastructure existante du projet, exactement l'esprit "zero-budget" —
pas de clé API supplémentaire à créer, pas de quota séparé à gérer.

Format produit : un fichier .jsonl de paires {"prompt", "completion"}, que
train.py peut ensuite consommer directement (voir
pairs_to_training_text) -- Shion-Nano n'a pas de gabarit de conversation
dédié comme DeepSeek-R1 : le format d'ENTRAÎNEMENT "Q: ...\\nR: ...\\n\\n"
EST le format que ShionServingClient doit reproduire à l'inférence (voir
serving.py), pour que le modèle reconnaisse la structure qu'il a appris.

USAGE (non exécuté automatiquement par ce dépôt : nécessite un teacher
réellement joignable, réseau ou serveur local -- voir
docs/SHION_ARCHITECTURE.md §4.1) :

    from shion.distill import build_dataset
    from uvi_core import DeepSeekClient   # le professeur "open source" déjà présent
    teacher = DeepSeekClient()
    build_dataset(teacher.complete, prompts, "shion/corpus/distillation.jsonl")
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, List, Tuple


def build_dataset(teacher_complete_fn: Callable[[str], str], prompts: Iterable[str],
                   out_path: str, on_progress: Callable[[int, int], None] = None) -> int:
    """Interroge le professeur pour chaque prompt, écrit un .jsonl au fur
    et à mesure (pas tout en mémoire puis un seul gros write -- un dataset
    de distillation utile fait facilement plusieurs milliers de prompts,
    et un plantage à mi-parcours ne doit pas perdre le travail déjà fait).
    Retourne le nombre de paires effectivement écrites (les échecs
    individuels -- timeout, question refusée -- sont comptés et ignorés,
    pas fatals pour le reste du lot)."""
    prompts = list(prompts)
    ecrites = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for i, prompt in enumerate(prompts):
            try:
                completion = teacher_complete_fn(prompt)
            except Exception:
                completion = None
            if completion and completion.strip():
                f.write(json.dumps({"prompt": prompt, "completion": completion},
                                    ensure_ascii=False) + "\n")
                ecrites += 1
            if on_progress:
                on_progress(i + 1, len(prompts))
    return ecrites


def load_dataset(path: str) -> List[Tuple[str, str]]:
    pairs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            pairs.append((obj["prompt"], obj["completion"]))
    return pairs


def pairs_to_training_text(pairs: Iterable[Tuple[str, str]]) -> str:
    parts = []
    for prompt, completion in pairs:
        parts.append("Q: %s\nR: %s\n\n" % (prompt.strip(), completion.strip()))
    return "".join(parts)
