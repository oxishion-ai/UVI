"""shion/quantization.py — Compression des poids : FP32 -> INT4 / ternaire.

Aucune dépendance hors numpy. Deux schémas, choisis pour leur simplicité
d'implémentation CORRECTE plutôt que pour l'optimalité théorique (un schéma
de quantification bugué qui produit un modèle qui "raisonne de travers"
silencieusement est bien pire qu'un schéma simple mais vérifié — voir le
gradcheck de model.py pour la même philosophie côté entraînement) :

  INT4 symétrique par groupe (group_size éléments partagent une échelle
  float16). C'est le schéma PAR DÉFAUT (voir ShionConfig.quant_bits).
  Rapport de taille : 8x plus petit qu'en FP32, 4x plus petit qu'en FP16.

  Ternaire {-1, 0, +1} par groupe, 2 bits/poids empaquetés (4 valeurs par
  octet). 16x plus petit qu'en FP32. Dégrade davantage la capacité du
  réseau — réservé aux tenseurs les plus tolérants (voir
  docs/SHION_ARCHITECTURE.md §2.2 pour quelles matrices s'y prêtent).

Format sur disque (voir memory.py) : chaque tenseur quantifié est un
triplet (packed: uint8[N/2 ou N/4], scales: float16[n_groupes], meta),
enregistré en .npy BRUTS (pas de .npz/zip) pour que np.load(mmap_mode="r")
puisse réellement différer la lecture disque — c'est ce détail qui rend le
chargement paresseux de memory.py efficace, pas seulement l'API qui
l'entoure.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np


class QuantizedTensor(NamedTuple):
    packed: np.ndarray      # uint8, empaqueté (2 ou 4 valeurs/octet)
    scales: np.ndarray      # float16, une échelle par groupe
    shape: tuple            # forme ORIGINALE du tenseur (avant aplatissement)
    numel: int               # nombre d'éléments ORIGINAL (avant remplissage)
    bits: int                 # 4 (INT4) ou 2 (ternaire)
    group_size: int


# -- empaquetage bas niveau ---------------------------------------------------

def _pack_nibbles(vals: np.ndarray) -> np.ndarray:
    """4 bits/valeur, 2 valeurs/octet. `vals` : uint8 dans [0, 15], longueur PAIRE."""
    vals = vals.astype(np.uint8)
    lo, hi = vals[0::2], vals[1::2]
    return (lo & 0x0F) | ((hi & 0x0F) << 4)


def _unpack_nibbles(packed: np.ndarray, n: int) -> np.ndarray:
    lo = packed & 0x0F
    hi = (packed >> 4) & 0x0F
    out = np.empty(len(packed) * 2, dtype=np.uint8)
    out[0::2], out[1::2] = lo, hi
    return out[:n]


def _pack_2bit(vals: np.ndarray) -> np.ndarray:
    """2 bits/valeur, 4 valeurs/octet. `vals` : uint8 dans [0, 3], longueur multiple de 4."""
    vals = vals.astype(np.uint8)
    v0, v1, v2, v3 = vals[0::4], vals[1::4], vals[2::4], vals[3::4]
    return (v0 & 3) | ((v1 & 3) << 2) | ((v2 & 3) << 4) | ((v3 & 3) << 6)


def _unpack_2bit(packed: np.ndarray, n: int) -> np.ndarray:
    v0 = packed & 3
    v1 = (packed >> 2) & 3
    v2 = (packed >> 4) & 3
    v3 = (packed >> 6) & 3
    out = np.empty(len(packed) * 4, dtype=np.uint8)
    out[0::4], out[1::4], out[2::4], out[3::4] = v0, v1, v2, v3
    return out[:n]


# -- INT4 ----------------------------------------------------------------

def quantize_int4(arr: np.ndarray, group_size: int = 32) -> QuantizedTensor:
    """Quantification symétrique par groupe sur 4 bits signés ([-7, 7],
    -8 volontairement inutilisé pour que la plage reste SYMÉTRIQUE autour
    de zéro — un poids nul doit rester exactement nul après un aller-retour
    quantifier/dé-quantifier, ce qui échouerait avec une plage [-8, 7])."""
    flat = arr.astype(np.float32).ravel()
    numel = flat.size
    pad = (-numel) % group_size
    if pad:
        flat = np.concatenate([flat, np.zeros(pad, dtype=np.float32)])
    groups = flat.reshape(-1, group_size)

    max_abs = np.abs(groups).max(axis=1)
    scales = np.where(max_abs > 0, max_abs / 7.0, 1.0).astype(np.float32)
    q = np.clip(np.round(groups / scales[:, None]), -7, 7).astype(np.int8)

    nibbles = (q.ravel() + 8).astype(np.uint8)  # [-7,7] -> [1,15], jamais 0 ni 8..15 hors plage
    packed = _pack_nibbles(nibbles)
    return QuantizedTensor(packed, scales.astype(np.float16), arr.shape, numel, 4, group_size)


def dequantize_int4(qt: QuantizedTensor) -> np.ndarray:
    n_groups = len(qt.scales)
    nibbles = _unpack_nibbles(qt.packed, n_groups * qt.group_size)
    q = nibbles.astype(np.int16) - 8
    vals = (q.reshape(n_groups, qt.group_size).astype(np.float32)
            * qt.scales.astype(np.float32)[:, None])
    return vals.ravel()[:qt.numel].reshape(qt.shape)


# -- Ternaire --------------------------------------------------------------

def quantize_ternary(arr: np.ndarray, group_size: int = 64) -> QuantizedTensor:
    """{-1, 0, +1} par groupe. Seuil à 0.5×échelle (heuristique BitNet-like) :
    en dessous, le poids est jugé négligeable et mis à zéro plutôt que
    forcé à ±1 — préserve la parcimonie plutôt que de la détruire."""
    flat = arr.astype(np.float32).ravel()
    numel = flat.size
    pad = (-numel) % group_size
    if pad:
        flat = np.concatenate([flat, np.zeros(pad, dtype=np.float32)])
    groups = flat.reshape(-1, group_size)

    scale = np.abs(groups).mean(axis=1)
    scale = np.where(scale > 0, scale, 1.0).astype(np.float32)
    threshold = 0.5 * scale[:, None]
    trit = np.zeros_like(groups, dtype=np.int8)
    trit[groups > threshold] = 1
    trit[groups < -threshold] = -1

    codes = (trit.ravel() + 1).astype(np.uint8)  # {-1,0,1} -> {0,1,2}, tient sur 2 bits
    packed = _pack_2bit(codes)
    return QuantizedTensor(packed, scale.astype(np.float16), arr.shape, numel, 2, group_size)


def dequantize_ternary(qt: QuantizedTensor) -> np.ndarray:
    n_groups = len(qt.scales)
    codes = _unpack_2bit(qt.packed, n_groups * qt.group_size)
    trit = codes.astype(np.int16) - 1
    vals = (trit.reshape(n_groups, qt.group_size).astype(np.float32)
            * qt.scales.astype(np.float32)[:, None])
    return vals.ravel()[:qt.numel].reshape(qt.shape)


def quantize(arr: np.ndarray, bits: int, group_size: int) -> QuantizedTensor:
    if bits == 4:
        return quantize_int4(arr, group_size)
    if bits == 2:
        return quantize_ternary(arr, group_size)
    raise ValueError("bits doit être 4 (INT4) ou 2 (ternaire), reçu %r" % bits)


def dequantize(qt: QuantizedTensor) -> np.ndarray:
    if qt.bits == 4:
        return dequantize_int4(qt)
    if qt.bits == 2:
        return dequantize_ternary(qt)
    raise ValueError("QuantizedTensor.bits inattendu : %r" % qt.bits)


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    for bits, qfn, dqfn in ((4, quantize_int4, dequantize_int4),
                             (2, quantize_ternary, dequantize_ternary)):
        w = rng.normal(0, 0.3, size=(37, 53)).astype(np.float32)  # dims impaires exprès
        qt = qfn(w, group_size=16)
        back = dqfn(qt)
        err = np.abs(w - back).mean()
        octets_fp32 = w.size * 4
        octets_quant = qt.packed.nbytes + qt.scales.nbytes
        print("bits=%d  erreur_moy_abs=%.4f  taille %d -> %d octets (x%.1f)"
              % (bits, err, octets_fp32, octets_quant, octets_fp32 / octets_quant))
        assert back.shape == w.shape
        assert err < 0.15, "erreur de quantification anormalement haute"
    print("OK — quantification/déquantification correctes.")
