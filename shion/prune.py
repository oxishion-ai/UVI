"""shion/prune.py — Élagage (pruning) par magnitude, qui MESURE son propre
impact plutôt que de se contenter de l'affirmer.

Deux granularités, avec des propriétés différentes (voir
docs/SHION_ARCHITECTURE.md §2.4 pour le détail) :

  NON STRUCTURÉ (poids par poids) -- la plus fine, la meilleure rétention
  de qualité à taux de parcimonie égal. Mais sur un CPU générique SANS
  support matériel/bibliothèque dédiée au calcul creux, une multiplication
  par zéro coûte toujours un cycle : ce mode réduit la place occupée
  (utile combiné à un format clairsemé indices+valeurs), pas le temps de
  calcul.

  STRUCTURÉ (lignes entières retirées) -- moins fin, dégrade plus vite la
  qualité à taux égal, mais RACCOURCIT réellement les matrices utilisées :
  seul ce mode accélère effectivement l'inférence sur CPU générique.

Shion-Nano (livré dans ce dépôt) N'EST PAS élagué : à ~10^5 paramètres, il
est déjà au plancher de ce qui reste entraînable sur le corpus minimal
fourni -- l'élaguer détruirait sa capacité avant même de pouvoir mesurer un
gain. Le mécanisme est néanmoins implémenté et VÉRIFIÉ ici (pas seulement
décrit en pseudocode) : le self-test en bas de fichier mesure l'impact réel
sur la perte, sur le checkpoint entraîné s'il existe. Prêt pour
Shion-Micro/Base, où la marge de manœuvre est réelle.
"""

from __future__ import annotations

import numpy as np


def prune_unstructured(w: np.ndarray, sparsity: float) -> np.ndarray:
    """Met à zéro les `sparsity` (0..1) poids de PLUS FAIBLE magnitude."""
    assert 0.0 <= sparsity < 1.0, "sparsity doit être dans [0, 1["
    if sparsity == 0.0:
        return w.copy()
    seuil = np.quantile(np.abs(w), sparsity)
    out = w.copy()
    out[np.abs(out) < seuil] = 0.0
    return out


def prune_structured_rows(w: np.ndarray, sparsity: float) -> np.ndarray:
    """Met à zéro les LIGNES entières de plus faible norme L2 -- réduit le
    calcul réel (une ligne nulle peut être sautée par l'appelant), pas
    seulement le stockage. `w` : (fan_in, fan_out), lignes = fan_in."""
    assert 0.0 <= sparsity < 1.0, "sparsity doit être dans [0, 1["
    if sparsity == 0.0:
        return w.copy()
    normes = np.linalg.norm(w, axis=1)
    n_a_retirer = int(sparsity * w.shape[0])
    if n_a_retirer == 0:
        return w.copy()
    indices = np.argsort(normes)[:n_a_retirer]
    out = w.copy()
    out[indices, :] = 0.0
    return out


def sparsity_ratio(w: np.ndarray) -> float:
    return float((w == 0).sum()) / w.size


if __name__ == "__main__":
    import os

    from . import tokenizer
    from .config import NANO
    from .memory import GlobalWeights, LazyLayerCache
    from .model import forward_loss, init_params

    weights_dir = os.path.join(os.path.dirname(__file__), "weights", "shion-nano")
    if os.path.isdir(weights_dir):
        glob = GlobalWeights(weights_dir)
        cache = LazyLayerCache(weights_dir, NANO, max_resident_layers=NANO.n_layer)
        params = {"embed": glob.embed, "final_norm": glob.final_norm}
        for i in range(NANO.n_layer):
            for nom, arr in cache.get_layer(i).items():
                params["layer.%d.%s" % (i, nom)] = arr
        source = "checkpoint entraîné (shion/weights/shion-nano)"
    else:
        params = init_params(NANO, np.random.default_rng(0))
        source = "poids aléatoires (aucun checkpoint entraîné trouvé -- lancez shion/train.py d'abord)"

    print("Source des poids : %s" % source)
    texte = "Shion est un modele de langage frugal qui tourne sur un ordinateur lent."
    ids = np.array(tokenizer.encode(texte, add_bos=False))
    tokens, targets = ids[:-1], ids[1:]
    perte_base, _, _ = forward_loss(params, NANO, tokens, targets)
    print("perte de référence (aucun élagage) = %.4f" % perte_base)

    cible = "layer.0.mlp_win"  # la plus grosse matrice d'une couche -- la plus représentative
    for sparsite in (0.1, 0.3, 0.5, 0.7):
        elague = dict(params)
        elague[cible] = prune_unstructured(params[cible], sparsite)
        perte, _, _ = forward_loss(elague, NANO, tokens, targets)
        taux_reel = sparsity_ratio(elague[cible])
        print("sparsité visée=%3.0f%%  réelle=%5.1f%%  perte %.4f -> %.4f (delta %+.4f)"
              % (sparsite * 100, taux_reel * 100, perte_base, perte, perte - perte_base))

    print("OK -- élagage magnitude appliqué, impact mesuré (pas supposé).")
