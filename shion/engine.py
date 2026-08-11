"""shion/engine.py — Inférence : charge un checkpoint quantifié et génère du
texte, avec une empreinte mémoire BORNÉE quel que soit le nombre de tokens
déjà générés.

Différence assumée avec model.py (entraînement) : model.py calcule
l'attention par fenêtre glissante via une matrice (T,T) MASQUÉE -- simple à
dériver et à vérifier par gradient-check, mais O(window) de calcul RÉEL
gâché en zéros au-delà de la fenêtre, et O(T) de mémoire pour la matrice de
masque. ICI, en génération, la fenêtre est un vrai ANNEAU : on ne garde en
mémoire QUE les `window` dernières clés/valeurs par tête et par couche, pas
l'historique complet. C'est ce qui tient la promesse "empreinte mémoire
fixe même avec un long contexte" de docs/SHION_ARCHITECTURE.md §2.3 -- une
conversation de 10 000 tokens ne coûte pas plus cher qu'une de 100.

Couplé à memory.LazyLayerCache (voir ce module) pour la seconde moitié de
la promesse : les poids eux-mêmes ne sont dé-quantifiés en RAM que pour les
`max_resident_layers` couches actuellement utiles, pas tout le modèle.
"""

from __future__ import annotations

from typing import List, Optional

from .backend import xp as np

from . import tokenizer
from .memory import GlobalWeights, LazyLayerCache
from .model import rmsnorm_forward, sigmoid  # réutilisés tels quels (inférence = sous-ensemble de forward)


class _RollingKV:
    """Tampon circulaire des `window` dernières clés/valeurs, PAR COUCHE.

    C'est la structure qui rend l'attention réellement O(window) en mémoire
    pendant la génération (contrairement à model.py, qui matérialise (T,T)
    pour pouvoir en dériver le gradient). Pas de gradient ici : c'est de
    l'inférence pure."""

    def __init__(self, window: int, d_model: int):
        self.window = window
        self.K = np.zeros((window, d_model), dtype=np.float32)
        self.V = np.zeros((window, d_model), dtype=np.float32)
        self.filled = 0   # nombre d'entrées valides (<= window)
        self._pos = 0       # prochain index d'écriture (anneau)

    def push(self, k_vec: np.ndarray, v_vec: np.ndarray):
        self.K[self._pos] = k_vec
        self.V[self._pos] = v_vec
        self._pos = (self._pos + 1) % self.window
        self.filled = min(self.filled + 1, self.window)

    def ordered(self):
        """Renvoie (K, V) dans l'ordre chronologique (plus ancien -> plus
        récent), tronqués à `filled` entrées."""
        if self.filled < self.window:
            return self.K[:self.filled], self.V[:self.filled]
        # Anneau plein : la position d'écriture actuelle est aussi le plus
        # ancien élément (celui qui va être écrasé au prochain push).
        idx = np.concatenate([np.arange(self._pos, self.window), np.arange(0, self._pos)])
        return self.K[idx], self.V[idx]


class ShionEngine:
    """Moteur d'inférence Shion, EN PROCESSUS (pas de serveur HTTP). Utilisé
    directement par shion/server.py, et par ShionServingClient en repli
    local (voir serving.py) quand aucun serveur dédié n'est joignable."""

    def __init__(self, weights_dir: str, config, max_resident_layers: int = 2):
        self.config = config
        self.weights_dir = weights_dir
        self.globals = GlobalWeights(weights_dir)
        self.cache = LazyLayerCache(weights_dir, config, max_resident_layers)

    @classmethod
    def from_preset(cls, weights_dir: str, preset_name: str = "shion-nano", **kw) -> "ShionEngine":
        from .config import get_config
        return cls(weights_dir, get_config(preset_name), **kw)

    def _forward_step(self, x: np.ndarray, kv_state: List[_RollingKV]) -> np.ndarray:
        """Une seule position (T=1) à travers toutes les couches, en
        s'appuyant sur le contexte déjà accumulé dans `kv_state` (une
        _RollingKV par couche) plutôt que de refaire la passe complète sur
        toute la séquence à chaque nouveau token -- c'est le pendant
        "génération" du calcul par blocs de model.py (qui, lui, traite un
        bloc complet d'un coup parce qu'il n'a pas besoin d'être
        incrémental pour l'entraînement)."""
        cfg = self.config
        n_head, Dh = cfg.n_head, cfg.head_dim
        scale = 1.0 / np.sqrt(Dh)

        for i in range(cfg.n_layer):
            w = self.cache.get_layer(i)
            n1, _ = rmsnorm_forward(x, w["norm1"], cfg.rms_eps)
            q = n1 @ w["attn_wq"]
            k = n1 @ w["attn_wk"]
            v = n1 @ w["attn_wv"]
            kv_state[i].push(k[0], v[0])
            K_hist, V_hist = kv_state[i].ordered()  # (Wc, d_model), Wc <= window

            out = np.zeros((1, cfg.d_model), dtype=np.float32)
            for h in range(n_head):
                sl = slice(h * Dh, (h + 1) * Dh)
                qh = q[:, sl]                      # (1, Dh)
                kh, vh = K_hist[:, sl], V_hist[:, sl]  # (Wc, Dh)
                scores = (qh @ kh.T) * scale         # (1, Wc) -- pas de masque : kv_state ne
                scores -= scores.max()                # contient QUE le passé, la fenêtre EST le masque
                p = np.exp(scores)
                p /= p.sum()
                out[:, sl] = p @ vh
            attn_out = out @ w["attn_wo"]
            h1 = x + attn_out

            n2, _ = rmsnorm_forward(h1, w["norm2"], cfg.rms_eps)
            d_ff = w["mlp_win"].shape[1] // 2
            u = n2 @ w["mlp_win"]
            gate, up = u[:, :d_ff], u[:, d_ff:]
            act = (gate * sigmoid(gate)) * up
            x = h1 + act @ w["mlp_wout"]

        y_final, _ = rmsnorm_forward(x, self.globals.final_norm, cfg.rms_eps)
        logits = y_final @ self.globals.embed.T
        return logits[0]  # (vocab_size,)

    def chat(self, history: List[tuple], prompt: str, max_new_tokens: int = 200,
              temperature: float = 0.8, top_p: float = 0.9,
              rng: Optional[np.random.Generator] = None) -> str:
        """Formate `history` (liste de (question, réponse)) + `prompt` selon
        le gabarit "Q: ...\\nR: ...\\n\\n" -- EXACTEMENT celui produit par
        distill.pairs_to_training_text pour l'entraînement (voir train.py).
        Un modèle causal générique comme Shion-Nano n'a pas de gabarit de
        conversation appris séparément : le format d'inférence DOIT
        reproduire le format d'entraînement, sans quoi le modèle continue
        n'importe comment (il n'a jamais vu cette structure).

        Point d'entrée COMMUN à shion/server.py et au repli local de
        serving.py -- centralisé ici pour que les deux ne puissent PAS
        diverger sur le gabarit utilisé."""
        transcript = "".join(
            "Q: %s\nR: %s\n\n" % (q.strip(), r.strip()) for q, r in history
        )
        transcript += "Q: %s\nR:" % prompt.strip()
        brut = self.generate(transcript, max_new_tokens=max_new_tokens,
                              temperature=temperature, top_p=top_p, rng=rng)
        # Le modèle n'a aucune notion explicite de "fin de réponse" hors
        # <eos> (rare, tant qu'il est peu entraîné) -- on coupe donc aussi
        # dès qu'il commence à hallucider un nouveau tour "Q:", pour ne
        # jamais renvoyer un faux dialogue complet en guise de réponse.
        return brut.split("\nQ:")[0].strip()

    def generate(self, prompt: str, max_new_tokens: int = 200,
                 temperature: float = 0.8, top_p: float = 0.9,
                 rng: Optional[np.random.Generator] = None) -> str:
        rng = rng or np.random.default_rng()
        cfg = self.config
        ids = tokenizer.encode(prompt, add_bos=True)
        ids = ids[-cfg.max_seq_len:]

        kv_state = [_RollingKV(cfg.window, cfg.d_model) for _ in range(cfg.n_layer)]
        # "Amorçage" : rejoue tout le prompt token par token pour remplir
        # kv_state, comme le ferait la génération elle-même -- pas de
        # chemin de code séparé "prompt" vs "génération" à maintenir.
        logits = None
        for tok in ids:
            x = self.globals.embed[[tok]]
            logits = self._forward_step(x, kv_state)

        out_ids: List[int] = []
        for _ in range(max_new_tokens):
            next_id = _sample(logits, temperature, top_p, rng)
            if next_id == tokenizer.EOS:
                break
            out_ids.append(next_id)
            x = self.globals.embed[[next_id]]
            logits = self._forward_step(x, kv_state)
        return tokenizer.decode(out_ids)


def _sample(logits: np.ndarray, temperature: float, top_p: float, rng: np.random.Generator) -> int:
    logits = logits / max(temperature, 1e-4)
    order = np.argsort(-logits)
    sorted_logits = logits[order]
    probs = np.exp(sorted_logits - sorted_logits.max())
    probs /= probs.sum()
    cumulative = np.cumsum(probs)
    # int(...) : sur backend GPU, cutoff est un scalaire device (tableau
    # 0-d) -- max(1, cutoff) et le slicing qui suit veulent un entier Python
    # nu, pas une comparaison élément-par-élément ambiguë.
    cutoff = int(np.searchsorted(cumulative, top_p)) + 1
    keep = order[:max(1, cutoff)]
    p_keep = probs[:max(1, cutoff)]
    p_keep = p_keep / p_keep.sum()
    return int(rng.choice(keep, p=p_keep))
