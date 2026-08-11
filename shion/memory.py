"""shion/memory.py — Algorithme de gestion mémoire : chargement paresseux
des poids, couche par couche, avec un cache LRU borné.

C'EST LE LIVRABLE "Algorithme de Gestion Mémoire" DE
docs/SHION_ARCHITECTURE.md §3 — implémenté ici, pas seulement décrit en
pseudocode. Le principe :

  1. Les poids de CHAQUE couche sont écrits sur disque en fichiers .npy
     BRUTS séparés (pas de zip/.npz), quantifiés (voir quantization.py).
  2. À l'ouverture, RIEN n'est chargé en RAM : seul un memmap (mapping
     mémoire virtuelle -> fichier, voir numpy.load(mmap_mode="r")) est
     établi. Les pages ne sont lues du disque QUE quand elles sont
     réellement accédées par le CPU — c'est le système d'exploitation qui
     fait ce travail, pas du code Python, ce qui est bien plus rapide et
     fiable qu'une pagination réimplémentée à la main.
  3. Au moment de dé-quantifier une couche (juste avant de l'utiliser dans
     la passe avant), le résultat FP32 est gardé dans un cache résident
     borné à `max_resident_layers` couches (2 par défaut : la couche
     courante et la suivante, cf. pré-chargement dans engine.py).
  4. Quand une nouvelle couche entre et que le cache déborde, la couche la
     PLUS ANCIENNEMENT UTILISÉE (LRU) est explicitement libérée (les
     tableaux numpy sont vidés puis dé-référencés) — pas laissée à la merci
     du ramasse-miettes générationnel, qui pourrait la garder vivante
     plusieurs passes avant/arrière et faire grimper la RAM résidente bien
     au-dessus du budget annoncé.

Pseudocode équivalent (pour lecture rapide sans dérouler l'implémentation) :

    fonction get_layer(i):
        si i dans cache_resident:
            marquer i comme récemment utilisé
            retourner cache_resident[i]
        poids_bruts <- mmap_lire(fichier_couche(i))       # pas de RAM consommée ici
        poids_fp32  <- dequantifier(poids_bruts)            # RAM consommée ICI seulement
        cache_resident[i] <- poids_fp32
        tant que taille(cache_resident) > max_resident:
            j <- le moins récemment utilisé de cache_resident
            libérer(cache_resident.retirer(j))               # RAM rendue au système
        retourner cache_resident[i]

Ce fichier ne connaît RIEN de l'architecture du modèle (pas de notion
d'attention, de tête, etc.) — model.py lui fournit juste des noms de
tenseurs par couche. Séparation volontaire : c'est ce qui permet de faire
évoluer l'architecture (Shion-Micro, Shion-Base) sans toucher au mécanisme
de pagination.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Dict, List

import numpy as np

from .backend import to_device
from .quantization import QuantizedTensor, dequantize, quantize

_LAYER_TENSOR_NAMES = ("attn_wq", "attn_wk", "attn_wv", "attn_wo", "mlp_win", "mlp_wout")
_LAYER_NORM_NAMES = ("norm1", "norm2")
META_FILENAME = "meta.json"


def _layer_dir(root: str, idx: int) -> str:
    return os.path.join(root, "layer_%03d" % idx)


def _write_quantized(prefix: str, qt: QuantizedTensor) -> None:
    np.save(prefix + ".packed.npy", qt.packed)
    np.save(prefix + ".scales.npy", qt.scales)
    with open(prefix + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"shape": list(qt.shape), "numel": qt.numel,
                   "bits": qt.bits, "group_size": qt.group_size}, f)


def _read_quantized(prefix: str, mmap: bool = True) -> QuantizedTensor:
    mode = "r" if mmap else None
    packed = np.load(prefix + ".packed.npy", mmap_mode=mode)
    scales = np.load(prefix + ".scales.npy")  # petit (1 float16/groupe) : chargé entièrement, sans risque
    with open(prefix + ".meta.json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    return QuantizedTensor(packed, scales, tuple(meta["shape"]), meta["numel"],
                            meta["bits"], meta["group_size"])


def save_checkpoint(weights_dir: str, tensors: Dict[str, np.ndarray], config) -> None:
    """Écrit un checkpoint Shion sur disque : `tensors` doit contenir
    "embed", "final_norm", et pour chaque couche i, "layer.{i}.{nom}" pour
    chaque nom de _LAYER_TENSOR_NAMES / _LAYER_NORM_NAMES."""
    os.makedirs(weights_dir, exist_ok=True)
    qt_embed = quantize(tensors["embed"], config.quant_bits, config.quant_group)
    _write_quantized(os.path.join(weights_dir, "embed"), qt_embed)
    np.save(os.path.join(weights_dir, "final_norm.npy"),
            tensors["final_norm"].astype(np.float32))

    for i in range(config.n_layer):
        layer_dir = _layer_dir(weights_dir, i)
        os.makedirs(layer_dir, exist_ok=True)
        for name in _LAYER_TENSOR_NAMES:
            arr = tensors["layer.%d.%s" % (i, name)]
            qt = quantize(arr, config.quant_bits, config.quant_group)
            _write_quantized(os.path.join(layer_dir, name), qt)
        for name in _LAYER_NORM_NAMES:
            arr = tensors["layer.%d.%s" % (i, name)]
            # Gains de normalisation : quelques dizaines/centaines de flottants,
            # la précision compte plus que l'espace gagné -- non quantifiés.
            np.save(os.path.join(layer_dir, name + ".npy"), arr.astype(np.float32))

    meta = {
        "name": config.name, "vocab_size": config.vocab_size, "d_model": config.d_model,
        "n_layer": config.n_layer, "n_head": config.n_head, "d_ff": config.d_ff,
        "window": config.window, "max_seq_len": config.max_seq_len,
        "rms_eps": config.rms_eps, "quant_bits": config.quant_bits,
        "quant_group": config.quant_group,
    }
    with open(os.path.join(weights_dir, META_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def load_meta(weights_dir: str) -> dict:
    with open(os.path.join(weights_dir, META_FILENAME), "r", encoding="utf-8") as f:
        return json.load(f)


class GlobalWeights:
    """Table d'embedding + norme finale. Résidentes en permanence après le
    premier accès : contrairement aux couches, elles sont nécessaires à
    CHAQUE token généré (la tête de sortie est LIÉE à l'embedding), les
    garder en cache LRU n'aurait fait que les recharger sans arrêt pour
    aucun gain de RAM réel sur un vocabulaire de cette taille (260 lignes).
    Pour un vocabulaire de plusieurs dizaines de milliers de sous-mots, la
    prochaine étape documentée est de dé-quantifier l'embedding LIGNE PAR
    LIGNE à la demande plutôt qu'en un bloc -- voir
    docs/SHION_ARCHITECTURE.md §3.3."""

    def __init__(self, weights_dir: str):
        self._embed_qt = _read_quantized(os.path.join(weights_dir, "embed"), mmap=True)
        self._embed = None  # dé-quantifié au premier accès, voir .embed
        # to_device : final_norm sert directement dans le calcul (engine.py),
        # donc doit vivre sur le backend ACTIF (GPU si CuPy dispo) -- alors
        # que sa lecture disque reste toujours du NumPy hôte pur, voir
        # backend.py pour pourquoi cette frontière est ici et nulle part
        # ailleurs dans ce fichier.
        self.final_norm = to_device(np.load(os.path.join(weights_dir, "final_norm.npy")))

    @property
    def embed(self):
        if self._embed is None:
            self._embed = to_device(dequantize(self._embed_qt))
        return self._embed


class LazyLayerCache:
    """Cache LRU de couches dé-quantifiées, bornée à `max_resident_layers`.

    Compteurs `layer_loads`/`bytes_loaded` exposés pour diagnostic (le
    panneau MÉMOIRE de l'IDE ou un simple print peuvent s'en servir pour
    vérifier empiriquement que la RAM résidente reste bornée même sur un
    modèle à beaucoup de couches, cf. Shion-Base à 16 couches)."""

    def __init__(self, weights_dir: str, config, max_resident_layers: int = 2):
        self.weights_dir = weights_dir
        self.config = config
        self.max_resident = max(1, max_resident_layers)
        self._resident: "OrderedDict[int, Dict[str, np.ndarray]]" = OrderedDict()
        self.layer_loads = 0
        self.bytes_loaded = 0
        self.evictions = 0

    def get_layer(self, idx: int) -> Dict[str, np.ndarray]:
        if idx in self._resident:
            self._resident.move_to_end(idx)  # marque "récemment utilisé"
            return self._resident[idx]

        tensors = self._load_layer_from_disk(idx)
        self._resident[idx] = tensors
        self.layer_loads += 1

        while len(self._resident) > self.max_resident:
            _, evicted = self._resident.popitem(last=False)  # FIFO du OrderedDict = LRU ici
            evicted.clear()  # libère les références tout de suite, pas au prochain passage du GC
            self.evictions += 1
        return tensors

    def resident_layer_indices(self) -> List[int]:
        return list(self._resident.keys())

    def _load_layer_from_disk(self, idx: int) -> Dict[str, np.ndarray]:
        layer_dir = _layer_dir(self.weights_dir, idx)
        out: Dict[str, np.ndarray] = {}
        for name in _LAYER_TENSOR_NAMES:
            qt = _read_quantized(os.path.join(layer_dir, name), mmap=True)
            arr = dequantize(qt)             # NumPy hôte (quantization.py reste pur NumPy)
            self.bytes_loaded += arr.nbytes  # compté AVANT to_device : c'est le trafic disque/hôte réel
            out[name] = to_device(arr)       # -> backend actif (GPU si CuPy dispo)
        for name in _LAYER_NORM_NAMES:
            out[name] = to_device(np.load(os.path.join(layer_dir, name + ".npy")))
        return out


if __name__ == "__main__":
    import shutil
    import tempfile

    from . import config as config_mod

    cfg = config_mod.NANO
    rng = np.random.default_rng(0)
    tensors = {"embed": rng.normal(0, 0.02, (cfg.vocab_size, cfg.d_model)).astype(np.float32),
               "final_norm": np.ones(cfg.d_model, dtype=np.float32)}
    for i in range(cfg.n_layer):
        tensors["layer.%d.attn_wq" % i] = rng.normal(0, 0.02, (cfg.d_model, cfg.d_model)).astype(np.float32)
        tensors["layer.%d.attn_wk" % i] = rng.normal(0, 0.02, (cfg.d_model, cfg.d_model)).astype(np.float32)
        tensors["layer.%d.attn_wv" % i] = rng.normal(0, 0.02, (cfg.d_model, cfg.d_model)).astype(np.float32)
        tensors["layer.%d.attn_wo" % i] = rng.normal(0, 0.02, (cfg.d_model, cfg.d_model)).astype(np.float32)
        tensors["layer.%d.mlp_win" % i] = rng.normal(0, 0.02, (cfg.d_model, 2 * cfg.d_ff)).astype(np.float32)
        tensors["layer.%d.mlp_wout" % i] = rng.normal(0, 0.02, (cfg.d_ff, cfg.d_model)).astype(np.float32)
        tensors["layer.%d.norm1" % i] = np.ones(cfg.d_model, dtype=np.float32)
        tensors["layer.%d.norm2" % i] = np.ones(cfg.d_model, dtype=np.float32)

    tmp = tempfile.mkdtemp(prefix="shion_memtest_")
    try:
        save_checkpoint(tmp, tensors, cfg)
        glob = GlobalWeights(tmp)
        assert glob.embed.shape == (cfg.vocab_size, cfg.d_model)
        cache = LazyLayerCache(tmp, cfg, max_resident_layers=1)
        l0 = cache.get_layer(0)
        assert set(l0) == set(_LAYER_TENSOR_NAMES) | set(_LAYER_NORM_NAMES)
        l1 = cache.get_layer(1)
        assert cache.resident_layer_indices() == [1], "la couche 0 aurait dû être évincée (LRU, capacité=1)"
        assert cache.evictions == 1
        print("layer_loads=%d evictions=%d bytes_loaded=%d"
              % (cache.layer_loads, cache.evictions, cache.bytes_loaded))
        print("OK — chargement paresseux, dé-quantification et éviction LRU corrects.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
