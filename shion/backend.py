"""shion/backend.py — Sélection du module de calcul tableau : CuPy (GPU,
CUDA) si disponible, NumPy (CPU) sinon. AUCUN autre fichier ne doit importer
numpy/cupy directement pour du calcul de modèle (forward/backward,
génération, optimiseur) -- ils importent `xp` d'ici. Objectif : le MÊME
code (model.py, engine.py, train.py) tourne sans modification sur un poste
CPU (comme aujourd'hui) ou sur un GPU CUDA (ex. Colab, T4) -- seul le
module réellement chargé change, pas une ligne de logique.

CE QUI RESTE VOLONTAIREMENT EN NUMPY PUR, PARTOUT, MÊME QUAND xp=cupy :
  - quantization.py (empaquetage bits, lu/écrit sur disque) -- coût
    négligeable face aux matmuls de l'entraînement, aucun intérêt à le
    faire tourner sur GPU, et ça évite de complexifier le format de
    checkpoint (toujours des .npy hôte, quel que soit le backend de calcul
    utilisé pour l'ENTRAÎNER).
  - memory.py côté disque (np.load/np.save) -- même raison.

Ces deux modules utilisent donc `to_device`/`to_numpy` ci-dessous comme
SEULE frontière avec le reste : un tableau NumPy hôte entre ou sort, ce que
xp fait au milieu ne les regarde pas.
"""

from __future__ import annotations

try:
    import cupy as xp  # type: ignore[import-not-found]
    GPU = True
except ImportError:
    import numpy as xp  # type: ignore[no-redef]
    GPU = False

import numpy as _np


def to_numpy(arr):
    """Ramène un tableau du backend ACTIF vers un tableau NumPy hôte.

    No-op (identité) en backend NumPy -- appelable sans condition partout,
    y compris sur du code qui tournera aussi bien avec xp=numpy."""
    if GPU:
        return arr.get() if hasattr(arr, "get") else _np.asarray(arr)
    return arr


def to_device(arr):
    """Envoie un tableau NumPy hôte vers le backend actif.

    No-op en backend NumPy (xp.asarray sur un tableau déjà NumPy ne copie
    pas inutilement -- même tableau si le dtype correspond déjà)."""
    return xp.asarray(arr)


if __name__ == "__main__":
    print("backend actif : %s" % ("CuPy (GPU)" if GPU else "NumPy (CPU)"))
    a = xp.arange(6, dtype=xp.float32).reshape(2, 3)
    b = to_numpy(a)
    assert isinstance(b, _np.ndarray)
    c = to_device(b)
    assert _np.allclose(to_numpy(c), b)
    print("OK — aller-retour to_numpy/to_device correct.")
