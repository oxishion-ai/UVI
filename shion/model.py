"""shion/model.py — Shion : Transformer causal minimal, attention par
fenêtre glissante, MLP SwiGLU, RMSNorm, embedding/tête liés.

Style délibérément FONCTIONNEL (dict de paramètres nommés + fonctions
forward/backward pures) plutôt qu'orienté-objet façon PyTorch nn.Module :
il n'y a PAS de framework d'autodiff ici (c'est tout le sujet — voir
docs/SHION_ARCHITECTURE.md §1, "aucun framework lourd ou superflu"), donc
chaque forward est apparié À LA MAIN à son backward. Une classe aurait
caché cet appariement derrière de l'héritage ; des fonctions jumelles
forward_xxx/backward_xxx le rendent visible et vérifiable (voir le
gradient-check en bas de fichier, qui compare CHAQUE gradient analytique à
une dérivée numérique avant que quiconque ne fasse confiance à
l'entraînement de train.py).

Toutes les fonctions sont indépendantes du dtype (float32 en usage normal,
float64 dans le gradient-check pour ne pas confondre une vraie erreur
analytique avec du simple bruit d'arrondi float32).

Forme des séquences : (T, d_model) -- UNE séquence à la fois, sans axe de
batch. Le "micro-batching" de train.py empile les gradients de plusieurs
séquences par accumulation plutôt que par un axe supplémentaire ; voir sa
docstring pour pourquoi (c'est le mécanisme qui permet de tenir dans une
RAM minuscule, cf. spécification "Zero-Budget").
"""

from __future__ import annotations

from typing import Dict, Tuple

from .backend import xp as np

NEG_INF = -1.0e9


def _scatter_add(dest: np.ndarray, indices, src: np.ndarray) -> None:
    """dest[indices] += src, avec ACCUMULATION correcte sur les indices
    dupliqués (un même token peut apparaître plusieurs fois dans une
    séquence -- dest[indices] = src écraserait au lieu d'accumuler leurs
    gradients). np.add.at fait ça nativement ; CuPy expose la même
    opération sous cupyx.scatter_add plutôt que ufunc.at (couverture plus
    fiable entre versions CuPy que de compter sur add.at).

    Décidé sur le type RÉEL de `dest`, pas sur backend.GPU : le
    gradient-check en bas de fichier force volontairement du NumPy pur
    (précision float64) même quand CuPy est installé et actif ailleurs --
    se fier au flag global romprait précisément CE cas."""
    if type(dest).__module__.startswith("cupy"):
        import cupyx
        cupyx.scatter_add(dest, indices, src)
    else:
        import numpy as _np
        _np.add.at(dest, indices, src)


# =============================================================================
# RMSNorm
# =============================================================================

def rmsnorm_forward(x: np.ndarray, gain: np.ndarray, eps: float) -> Tuple[np.ndarray, tuple]:
    rms = np.sqrt((x * x).mean(axis=-1, keepdims=True) + eps)
    n = x / rms
    out = n * gain
    return out, (n, rms, gain)


def rmsnorm_backward(dout: np.ndarray, cache: tuple):
    n, rms, gain = cache
    d = n.shape[-1]
    dgain = (dout * n).sum(axis=0)
    dn = dout * gain
    dx = (dn - n * (dn * n).mean(axis=-1, keepdims=True)) / rms
    return dx, dgain


# =============================================================================
# Attention causale par fenêtre glissante (multi-têtes)
# =============================================================================

def _causal_window_mask(T: int, window: int) -> np.ndarray:
    idx = np.arange(T)
    rows, cols = idx[:, None], idx[None, :]
    return (cols <= rows) & (cols > rows - window)


def attention_forward(x_norm, Wq, Wk, Wv, Wo, n_head: int, window: int):
    T, d = x_norm.shape
    Dh = d // n_head
    Q, K, V = x_norm @ Wq, x_norm @ Wk, x_norm @ Wv
    mask = _causal_window_mask(T, window)
    scale = 1.0 / np.sqrt(Dh)

    O = np.zeros_like(x_norm)
    P_heads = []
    for h in range(n_head):
        sl = slice(h * Dh, (h + 1) * Dh)
        Qh, Kh, Vh = Q[:, sl], K[:, sl], V[:, sl]
        scores = np.where(mask, (Qh @ Kh.T) * scale, NEG_INF)
        scores = scores - scores.max(axis=1, keepdims=True)
        expo = np.exp(scores) * mask  # positions masquées EXACTEMENT à zéro, pas juste ~0
        P = expo / expo.sum(axis=1, keepdims=True)
        O[:, sl] = P @ Vh
        P_heads.append(P)

    attn_out = O @ Wo
    cache = (x_norm, Q, K, V, O, P_heads, mask, Dh, n_head, Wq, Wk, Wv, Wo)
    return attn_out, cache


def attention_backward(dattn_out, cache):
    x_norm, Q, K, V, O, P_heads, mask, Dh, n_head, Wq, Wk, Wv, Wo = cache
    dO = dattn_out @ Wo.T
    dWo = O.T @ dattn_out
    dQ, dK, dV = np.zeros_like(Q), np.zeros_like(K), np.zeros_like(V)
    scale = 1.0 / np.sqrt(Dh)

    for h in range(n_head):
        sl = slice(h * Dh, (h + 1) * Dh)
        P, Vh, dOh = P_heads[h], V[:, sl], dO[:, sl]
        dP = dOh @ Vh.T
        dV[:, sl] = P.T @ dOh
        row_dot = (P * dP).sum(axis=1, keepdims=True)
        dscores = P * (dP - row_dot) * mask  # cf. docstring module : sécurité, déjà nul par construction
        dQ[:, sl] = (dscores @ K[:, sl]) * scale
        dK[:, sl] = (dscores.T @ Q[:, sl]) * scale

    dWq = x_norm.T @ dQ
    dWk = x_norm.T @ dK
    dWv = x_norm.T @ dV
    dx_norm = dQ @ Wq.T + dK @ Wk.T + dV @ Wv.T
    return dx_norm, dWq, dWk, dWv, dWo


# =============================================================================
# MLP SwiGLU
# =============================================================================

def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


def mlp_forward(x_norm, Win, Wout):
    d_ff = Win.shape[1] // 2
    u = x_norm @ Win
    gate, up = u[:, :d_ff], u[:, d_ff:]
    s = sigmoid(gate)
    silu_gate = gate * s
    act = silu_gate * up
    out = act @ Wout
    cache = (x_norm, gate, up, s, silu_gate, act, Win, Wout, d_ff)
    return out, cache


def mlp_backward(dout, cache):
    x_norm, gate, up, s, silu_gate, act, Win, Wout, d_ff = cache
    dact = dout @ Wout.T
    dWout = act.T @ dout
    dup = dact * silu_gate
    dsilu_gate = dact * up
    dgate = dsilu_gate * (s + gate * s * (1.0 - s))  # d(silu)/dx = s + x*s*(1-s)
    du = np.concatenate([dgate, dup], axis=-1)
    dWin = x_norm.T @ du
    dx_norm = du @ Win.T
    return dx_norm, dWin, dWout


# =============================================================================
# Bloc = RMSNorm -> Attention -> résiduel -> RMSNorm -> MLP -> résiduel
# =============================================================================

def block_forward(x, p, prefix, n_head, window, eps):
    n1, c_n1 = rmsnorm_forward(x, p[prefix + "norm1"], eps)
    attn_out, c_attn = attention_forward(
        n1, p[prefix + "attn_wq"], p[prefix + "attn_wk"],
        p[prefix + "attn_wv"], p[prefix + "attn_wo"], n_head, window,
    )
    h = x + attn_out
    n2, c_n2 = rmsnorm_forward(h, p[prefix + "norm2"], eps)
    mlp_out, c_mlp = mlp_forward(n2, p[prefix + "mlp_win"], p[prefix + "mlp_wout"])
    y = h + mlp_out
    return y, (c_n1, c_attn, c_n2, c_mlp)


def block_backward(dy, cache, prefix):
    c_n1, c_attn, c_n2, c_mlp = cache
    grads = {}

    dh = dy
    dmlp_out = dy
    dn2, grads[prefix + "mlp_win"], grads[prefix + "mlp_wout"] = mlp_backward(dmlp_out, c_mlp)
    dx_from_n2, grads[prefix + "norm2"] = rmsnorm_backward(dn2, c_n2)
    dh = dh + dx_from_n2

    dx = dh
    dattn_out = dh
    (dn1, grads[prefix + "attn_wq"], grads[prefix + "attn_wk"],
     grads[prefix + "attn_wv"], grads[prefix + "attn_wo"]) = attention_backward(dattn_out, c_attn)
    dx_from_n1, grads[prefix + "norm1"] = rmsnorm_backward(dn1, c_n1)
    dx = dx + dx_from_n1

    return dx, grads


# =============================================================================
# Modèle complet : embedding -> blocs -> RMSNorm finale -> tête liée
# =============================================================================

def init_params(config, rng: np.random.Generator) -> Dict[str, np.ndarray]:
    def lin(fan_in, fan_out):
        return (rng.standard_normal((fan_in, fan_out)) * (0.02)).astype(np.float32)

    p: Dict[str, np.ndarray] = {
        "embed": (rng.standard_normal((config.vocab_size, config.d_model)) * 0.02).astype(np.float32),
        "final_norm": np.ones(config.d_model, dtype=np.float32),
    }
    for i in range(config.n_layer):
        pre = "layer.%d." % i
        p[pre + "attn_wq"] = lin(config.d_model, config.d_model)
        p[pre + "attn_wk"] = lin(config.d_model, config.d_model)
        p[pre + "attn_wv"] = lin(config.d_model, config.d_model)
        p[pre + "attn_wo"] = lin(config.d_model, config.d_model)
        p[pre + "mlp_win"] = lin(config.d_model, 2 * config.d_ff)
        p[pre + "mlp_wout"] = lin(config.d_ff, config.d_model)
        p[pre + "norm1"] = np.ones(config.d_model, dtype=np.float32)
        p[pre + "norm2"] = np.ones(config.d_model, dtype=np.float32)
    return p


def forward_logits(params, config, token_ids: np.ndarray):
    """token_ids: (T,) int. Retourne logits (T, vocab_size) et un cache
    pour backward_loss. Utilisé tel quel par train.py (avec cible) et,
    sans la partie perte, par engine.py (génération -- voir sa propre
    boucle qui n'a besoin que du dernier pas de temps)."""
    embed = params["embed"]
    x = embed[token_ids]
    block_caches = []
    for i in range(config.n_layer):
        x, c = block_forward(x, params, "layer.%d." % i, config.n_head, config.window, config.rms_eps)
        block_caches.append(c)
    y_final, c_final_norm = rmsnorm_forward(x, params["final_norm"], config.rms_eps)
    logits = y_final @ embed.T
    cache = (token_ids, block_caches, c_final_norm, y_final, embed)
    return logits, cache


def cross_entropy_loss(logits: np.ndarray, targets: np.ndarray):
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    probs = exp / exp.sum(axis=-1, keepdims=True)
    T = logits.shape[0]
    nll = -np.log(np.clip(probs[np.arange(T), targets], 1e-12, None))
    loss = nll.mean()
    dlogits = probs.copy()
    dlogits[np.arange(T), targets] -= 1.0
    dlogits /= T
    return loss, dlogits


def backward_loss(dlogits, cache, config):
    token_ids, block_caches, c_final_norm, y_final, embed = cache
    grads: Dict[str, np.ndarray] = {}

    dy_final = dlogits @ embed
    d_embed_from_head = dlogits.T @ y_final

    dx, grads["final_norm"] = rmsnorm_backward(dy_final, c_final_norm)

    for i in reversed(range(config.n_layer)):
        dx, layer_grads = block_backward(dx, block_caches[i], "layer.%d." % i)
        grads.update(layer_grads)

    d_embed = np.zeros_like(embed)
    _scatter_add(d_embed, token_ids, dx)  # gradient de l'embedding d'ENTRÉE (lookup)
    d_embed += d_embed_from_head          # + gradient de la tête de sortie LIÉE
    grads["embed"] = d_embed
    return grads


def forward_loss(params, config, token_ids: np.ndarray, targets: np.ndarray):
    logits, cache = forward_logits(params, config, token_ids)
    loss, dlogits = cross_entropy_loss(logits, targets)
    return loss, dlogits, cache


def backward(dlogits, cache, config) -> Dict[str, np.ndarray]:
    return backward_loss(dlogits, cache, config)


if __name__ == "__main__":
    from dataclasses import replace

    import numpy as np  # NumPy pur, PAS le backend actif : gradient-check en
    # float64 (précision) + np.nditer (sans équivalent CuPy) -- une
    # vérification de CORRECTION, indépendante de quel backend sert
    # l'entraînement réel. Rebinding local du nom `np` pour ce bloc
    # __main__ seulement (n'affecte jamais le reste du module importé
    # ailleurs, qui garde le backend choisi par shion/backend.py).

    from .config import NANO

    rng = np.random.default_rng(42)
    tiny = replace(NANO, vocab_size=12, d_model=8, n_layer=2, n_head=2, d_ff=10, window=3)
    params = init_params(tiny, rng)
    params = {k: v.astype(np.float64) for k, v in params.items()}  # float64 : le bruit d'arrondi
    T = 6                                                            # ne doit pas masquer un vrai bug
    token_ids = rng.integers(0, tiny.vocab_size, size=T)
    targets = rng.integers(0, tiny.vocab_size, size=T)

    loss, dlogits, cache = forward_loss(params, tiny, token_ids, targets)
    grads = backward(dlogits, cache, tiny)

    def loss_fn():
        logits, _ = forward_logits(params, tiny, token_ids)
        l, _ = cross_entropy_loss(logits, targets)
        return l

    eps = 1e-5
    worst = 0.0
    for name, arr in params.items():
        g_analytic = grads[name]
        it = np.nditer(arr, flags=["multi_index"])
        checked = 0
        for _ in it:
            idx = it.multi_index
            orig = arr[idx]
            arr[idx] = orig + eps
            l_plus = loss_fn()
            arr[idx] = orig - eps
            l_minus = loss_fn()
            arr[idx] = orig
            g_num = (l_plus - l_minus) / (2 * eps)
            diff = abs(g_num - g_analytic[idx])
            denom = max(1e-6, abs(g_num), abs(g_analytic[idx]))
            worst = max(worst, diff / denom)
            checked += 1
            if checked >= 6:  # quelques coordonnées par tenseur suffisent, pas un balayage complet
                break
    print("perte=%.4f  écart relatif max (gradient numérique vs analytique) = %.2e" % (loss, worst))
    assert worst < 5e-3, "gradient-check ÉCHOUÉ — bug dans une passe backward"
    print("OK — forward/backward vérifiés par différences finies sur tous les tenseurs de paramètres.")
