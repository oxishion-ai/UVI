"""shion/train.py — Pipeline d'entraînement "Zero-Budget" : micro-batching
+ délestage disque de l'état de l'optimiseur, sur CPU, sans framework de
deep learning.

CE QUE "MICRO-BATCHING" VEUT DIRE ICI, CONCRÈTEMENT (voir
docs/SHION_ARCHITECTURE.md §4.2) : au lieu de calculer le gradient d'un
grand lot de séquences EN UNE FOIS (ce qui exige de garder toutes leurs
activations intermédiaires en RAM simultanément), on traite les séquences
UNE PAR UNE (model.forward_loss / model.backward, qui n'opèrent QUE sur une
séquence à la fois -- voir leur docstring), et on ACCUMULE les gradients
dans un dict avant d'appliquer un seul pas d'optimiseur tous les
`grad_accum_steps` micro-lots. Le résultat mathématique est identique à un
vrai batch (la moyenne des gradients), le coût mémoire de pointe est celui
d'UNE séquence, pas de `grad_accum_steps` séquences.

CE QUE "DÉLESTAGE DISQUE" VEUT DIRE ICI : l'état de l'optimiseur Adam (deux
tableaux de la taille du modèle, m et v) est le plus gros consommateur de
RAM de l'entraînement après les paramètres eux-mêmes -- deux fois la
taille du modèle, en FP32, jamais quantifiable sans casser la convergence.
OffloadedAdam les garde en `numpy.memmap` (fichiers sur disque mappés en
mémoire virtuelle) plutôt qu'en tableaux RAM classiques : le système
d'exploitation ne garde en RAM que les pages RÉELLEMENT touchées à chaque
pas, et peut en évincer d'autres sous pression mémoire -- exactement le
"swap intelligent" demandé, mais délégué à un mécanisme du noyau déjà
correct et rapide plutôt que réimplémenté à la main.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Iterator, Optional, Tuple

from .backend import GPU, to_numpy, xp as np

from . import tokenizer
from .config import ShionConfig
from .memory import save_checkpoint
from .model import backward, forward_loss, init_params


class OffloadedAdam:
    """Adam standard (Kingma & Ba, 2014). Sur CPU (NumPy) : m/v délestés sur
    disque via mmap, comportement HISTORIQUE inchangé. Sur GPU (CuPy) : m/v
    restent de simples tableaux en VRAM, SANS délestage disque.

    Formules canoniques, sans variation -- ce module doit rester un exemple
    d'entraînement CORRECT, pas un terrain d'expérimentation d'optimiseur.

    POURQUOI PAS de délestage sur GPU aussi : le délestage existe pour
    contourner une RAM HÔTE limitée sur un poste CPU frugal -- la ressource
    contrainte sur GPU est la VRAM, pas la RAM, et memmap ne mappe QUE de la
    mémoire hôte (aucun sens à y délester de la VRAM). Pour Shion-Micro
    (~14M paramètres), m+v tiennent en ~110 Mo -- négligeable même sur un
    T4 (16 Go) : le délestage n'apporterait ici qu'une latence disque
    inutile, sans le moindre bénéfice mémoire."""

    def __init__(self, param_shapes: Dict[str, tuple], state_dir: str,
                 lr: float = 3e-3, beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-8):
        self.lr, self.beta1, self.beta2, self.eps = lr, beta1, beta2, eps
        self.t = 0
        self.m: Dict[str, np.ndarray] = {}
        self.v: Dict[str, np.ndarray] = {}
        self._memmapped = not GPU
        if self._memmapped:
            os.makedirs(state_dir, exist_ok=True)
            import numpy as _np  # np.memmap est spécifiquement NumPy, jamais CuPy
            for name, shape in param_shapes.items():
                self.m[name] = _np.memmap(os.path.join(state_dir, "m_%s.dat" % _safe_name(name)),
                                          dtype=_np.float32, mode="w+", shape=shape)
                self.v[name] = _np.memmap(os.path.join(state_dir, "v_%s.dat" % _safe_name(name)),
                                          dtype=_np.float32, mode="w+", shape=shape)
                self.m[name][:] = 0.0
                self.v[name][:] = 0.0
        else:
            for name, shape in param_shapes.items():
                self.m[name] = np.zeros(shape, dtype=np.float32)
                self.v[name] = np.zeros(shape, dtype=np.float32)

    def step(self, params: Dict[str, np.ndarray], grads: Dict[str, np.ndarray]) -> None:
        self.t += 1
        bias1 = 1.0 - self.beta1 ** self.t
        bias2 = 1.0 - self.beta2 ** self.t
        for name, g in grads.items():
            m, v = self.m[name], self.v[name]
            m[:] = self.beta1 * m + (1.0 - self.beta1) * g
            v[:] = self.beta2 * v + (1.0 - self.beta2) * (g * g)
            m_hat = m / bias1
            v_hat = v / bias2
            params[name] -= self.lr * m_hat / (np.sqrt(v_hat) + self.eps)

    def flush(self) -> None:
        if not self._memmapped:
            return  # rien à vidanger sur disque : m/v vivent en VRAM/RAM classique
        for arr in list(self.m.values()) + list(self.v.values()):
            arr.flush()


def _safe_name(name: str) -> str:
    return name.replace(".", "_")


def iter_microbatches(corpus_ids: np.ndarray, seq_len: int, steps: int,
                       rng: np.random.Generator) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Un micro-lot = UNE séquence de `seq_len` jetons découpée aléatoirement
    dans le corpus, plus sa cible (même séquence décalée d'un cran) -- voir
    la docstring du module pour pourquoi une séquence à la fois."""
    n = len(corpus_ids)
    assert n > seq_len + 1, "corpus trop court pour seq_len=%d" % seq_len
    for _ in range(steps):
        start = int(rng.integers(0, n - seq_len - 1))
        chunk = corpus_ids[start:start + seq_len + 1]
        yield chunk[:-1], chunk[1:]


def zero_like_params(params: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    return {name: np.zeros_like(arr) for name, arr in params.items()}


def train(
    config: ShionConfig,
    corpus_text: str,
    out_dir: str,
    steps: int = 400,
    seq_len: int = 96,
    grad_accum_steps: int = 4,
    lr: float = 3e-3,
    seed: int = 0,
    log_every: int = 20,
    save_every: Optional[int] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, float]:
    """Boucle d'entraînement complète. Retourne un petit résumé (perte
    initiale/finale) -- suffisant pour un smoke test ou un script appelant,
    pas un système de suivi d'expérience (hors budget de ce projet : un
    simple print() + fichier de perte suffit à "zero-budget").

    `save_every` : écrit un checkpoint INTERMÉDIAIRE tous les N pas, pas
    seulement à la toute fin. Sur un entraînement de plusieurs milliers de
    pas lancé en tâche de fond, ne sauvegarder qu'à la fin signifierait
    perdre TOUT le travail si le job est interrompu (fermeture de session,
    besoin de libérer la machine...) avant d'avoir atteint `steps` -- un
    risque réel sur un poste modeste, pas hypothétique. None désactive
    (comportement historique : un seul écrit final)."""
    rng = np.random.default_rng(seed)
    corpus_ids = np.array(tokenizer.encode(corpus_text, add_bos=False), dtype=np.int64)

    params = init_params(config, rng)
    state_dir = state_dir or os.path.join(out_dir, "_optimizer_state")
    opt = OffloadedAdam({k: v.shape for k, v in params.items()}, state_dir, lr=lr)

    losses = []
    accum_grads = zero_like_params(params)
    accum_count = 0
    t0 = time.time()

    for step, (ids, targets) in enumerate(iter_microbatches(corpus_ids, seq_len, steps, rng), start=1):
        loss, dlogits, cache = forward_loss(params, config, ids, targets)
        grads = backward(dlogits, cache, config)
        for name, g in grads.items():
            accum_grads[name] += g
        accum_count += 1
        # float(...) immédiat : synchronise ce SEUL scalaire device->hôte
        # (négligeable) et garde `losses` comme une liste Python pure, aussi
        # bien sur backend CPU que GPU -- le print périodique et le résumé
        # final plus bas font ensuite de l'arithmétique Python simple, sans
        # jamais repasser par np.mean() sur une liste (non garanti côté
        # CuPy, qui attend un tableau, pas une liste Python hétérogène).
        losses.append(float(loss))

        if accum_count >= grad_accum_steps:
            for name in accum_grads:
                accum_grads[name] /= accum_count
            opt.step(params, accum_grads)
            accum_grads = zero_like_params(params)
            accum_count = 0

        if step % log_every == 0 or step == steps:
            recent = losses[-log_every:]
            print("pas %4d/%d  perte(moy. %d derniers)=%.4f  %.1fs écoulées"
                  % (step, steps, len(recent), sum(recent) / len(recent), time.time() - t0))

        if save_every and (step % save_every == 0) and step != steps:
            # to_numpy : save_checkpoint (memory.py) -> quantization.py sont
            # volontairement restés purs NumPy (voir backend.py) ; params
            # peut être CuPy en entraînement GPU, donc conversion hôte
            # explicite à CETTE frontière précise, pas ailleurs.
            save_checkpoint(out_dir, {k: to_numpy(v) for k, v in params.items()}, config)
            print("  -- checkpoint intermédiaire écrit (pas %d)" % step)

    if accum_count > 0:  # dernier lot partiel : ne pas perdre son gradient
        for name in accum_grads:
            accum_grads[name] /= accum_count
        opt.step(params, accum_grads)
    opt.flush()

    # init_params (model.py) nomme déjà ses tenseurs exactement comme
    # save_checkpoint (memory.py) les attend ("embed", "final_norm",
    # "layer.{i}.{nom}") -- aucune traduction nécessaire entre les deux.
    save_checkpoint(out_dir, {k: to_numpy(v) for k, v in params.items()}, config)

    derniers = losses[-log_every:]
    resume = {"perte_initiale": losses[0], "perte_finale": sum(derniers) / len(derniers)}
    print("Checkpoint écrit dans %s — perte %.4f -> %.4f" % (out_dir, resume["perte_initiale"], resume["perte_finale"]))
    return resume


def _corpus_from_dir(corpus_dir: str) -> str:
    textes = []
    if os.path.isdir(corpus_dir):
        for nom in sorted(os.listdir(corpus_dir)):
            chemin = os.path.join(corpus_dir, nom)
            if nom.endswith(".txt") and os.path.isfile(chemin):
                with open(chemin, "r", encoding="utf-8") as f:
                    textes.append(f.read())
    if not textes:
        raise FileNotFoundError(
            "Aucun corpus .txt trouvé dans %r -- voir shion/corpus/seed_corpus.txt "
            "(fourni avec ce dépôt) ou shion/distill.py pour générer un corpus "
            "de distillation à partir d'un professeur." % corpus_dir
        )
    return "\n".join(textes)


if __name__ == "__main__":
    import argparse

    from .config import PRESETS, get_config

    parser = argparse.ArgumentParser(description="Entraîne un modèle Shion sur un corpus local (auto-supervisé).")
    parser.add_argument("--preset", default="shion-nano", choices=sorted(PRESETS),
                         help="Palier à entraîner (voir config.py).")
    parser.add_argument("--corpus-dir", default=os.path.join(os.path.dirname(__file__), "corpus"))
    parser.add_argument("--out", default=None,
                         help="Dossier de sortie. Défaut : shion/weights/<preset>.")
    parser.add_argument("--steps", type=int, default=4000)
    parser.add_argument("--seq-len", type=int, default=80)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--save-every", type=int, default=250)
    args = parser.parse_args()

    cfg = get_config(args.preset)
    out_dir = args.out or os.path.join(os.path.dirname(__file__), "weights", cfg.name)
    texte = _corpus_from_dir(args.corpus_dir)
    print("Corpus : %d caractères, config=%s (%d paramètres), backend=%s"
          % (len(texte), cfg.name, cfg.param_count(), "GPU (CuPy)" if GPU else "CPU (NumPy)"))
    train(cfg, texte, out_dir, steps=args.steps, seq_len=args.seq_len,
          grad_accum_steps=args.grad_accum, lr=args.lr, save_every=args.save_every)
