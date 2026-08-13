"""shion/config.py — Topologie du modèle Shion.

Un seul objet ShionConfig décrit entièrement la forme du réseau. Les poids
sur disque (shion/weights/*.npy) et le moteur (model.py, memory.py,
engine.py) lisent tous la même config : changer une dimension ici sans
ré-entraîner casserait le chargement au lieu de produire un résultat
silencieusement faux (voir memory.py:LazyLayerCache, qui vérifie les formes
attendues avant de dé-quantifier).

Trois paliers sont documentés (voir docs/SHION_ARCHITECTURE.md §1 pour le
raisonnement complet derrière chaque taille) :

  NANO  — ~0,47 M paramètres (468 000 exactement -- voir param_count(),
          dont la formule MLP a été corrigée lors de cette relecture, voir
          son propre commentaire : le terme W_out du SwiGLU était oublié,
          sous-comptant les trois paliers d'environ 30%). C'est la SEULE
          taille livrée avec un checkpoint entraîné DÈS le commit initial
          dans ce dépôt (shion/weights/shion-nano.*) -- shion-micro (voir
          juste dessous) en a depuis un aussi, mais entraîné dans un
          commit ultérieur, pas dans celui-ci. Tourne sur n'importe quoi,
          y compris un Raspberry Pi, en quelques millisecondes par token.
          Sert de PREUVE D'ARCHITECTURE (le pipeline bout en bout
          fonctionne réellement) plutôt que d'assistant compétent — voir
          l'avertissement dans README de ce paquet et dans le document
          d'architecture.
  MICRO — ~19,0 M paramètres (18 980 736 exactement). Cible réaliste pour
          des réponses courtes cohérentes sur CPU modeste. DÉSORMAIS
          ENTRAÎNÉE dans ce dépôt aussi (shion/weights/shion-micro/,
          voir l'historique git : 3 itérations, la dernière sur
          lr=1e-3/20 000 pas/corpus de 89 Ko) -- qualité non mesurée dans
          ce fichier ni dans docs/SHION_ARCHITECTURE.md (dont le §10 disait
          encore "non entraînée" avant cette relecture ; à re-vérifier par
          quiconque s'appuie sur cette affirmation plutôt que sur le
          checkpoint réel).
  BASE  — ~268,7 M paramètres (268 735 488 exactement). Plafond raisonnable
          du "bare-metal frugal" avant qu'un GPU ne devienne de facto
          nécessaire à l'entraînement (même quantifié, l'inférence CPU
          reste possible mais lente). DÉCRITE mais pas entraînée ici --
          demande un vrai corpus de distillation (voir distill.py) et
          plusieurs heures/jours de calcul, hors de portée d'une session.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ShionConfig:
    name: str
    vocab_size: int      # 256 octets + 4 jetons spéciaux, voir tokenizer.py
    d_model: int
    n_layer: int
    n_head: int
    d_ff: int             # dimension cachée du MLP (SwiGLU : 2 projections de d_ff)
    window: int           # portée de l'attention par fenêtre glissante causale
    max_seq_len: int
    rms_eps: float = 1e-5
    quant_bits: int = 4    # 4 = INT4, 2 = ternaire (voir quantization.py)
    quant_group: int = 32  # taille de groupe pour l'échelle de quantification

    @property
    def head_dim(self) -> int:
        assert self.d_model % self.n_head == 0, (
            "d_model doit être un multiple de n_head (têtes de taille égale)."
        )
        return self.d_model // self.n_head

    def param_count(self) -> int:
        """Compte réel des paramètres entraînables (utilisé par train.py
        pour afficher un résumé, et par les tests pour vérifier qu'un
        changement de config ne fait pas dériver silencieusement la taille
        annoncée dans la doc).

        BUG CORRIGÉ : le terme MLP comptait seulement `2 * d_model * d_ff`
        (la taille de W_in, dont la sortie fait 2*d_ff -- gate+up du
        SwiGLU) et OUBLIAIT la contribution de W_out (forme (d_ff,
        d_model) = d_model*d_ff éléments), pourtant bien présent et
        entraîné (voir model.py:init_params, "mlp_wout"). Trouvé en
        important les VRAIS tenseurs du checkpoint shion-nano livré
        (shion/weights/shion-nano/) pour l'export web mobile : leur somme
        réelle (468 000 éléments) ne correspondait PAS aux 357 408
        annoncés ici et dans docs/SHION_ARCHITECTURE.md -- l'écart exact
        (110 592 = 3 * 96 * 384 = n_layer * d_model * d_ff) confirme qu'il
        manquait précisément le terme W_out. Les trois paliers documentés
        (Nano/Micro/Base) avaient tous ce même écart -- voir
        docs/SHION_ARCHITECTURE.md, corrigé au même endroit."""
        embed = self.vocab_size * self.d_model  # tête de sortie LIÉE (tied) : pas comptée deux fois
        per_layer = (
            4 * self.d_model * self.d_model       # Wq, Wk, Wv, Wo
            + 3 * self.d_model * self.d_ff         # MLP SwiGLU : W_in (2*d_ff en sortie) + W_out (d_ff en entrée)
            + 2 * self.d_model                     # 2 gains RMSNorm (pré-attention, pré-MLP)
        )
        final_norm = self.d_model
        return embed + self.n_layer * per_layer + final_norm


# --- Paliers documentés -----------------------------------------------------

NANO = ShionConfig(
    # Recalibré après mesure empirique (voir docs/SHION_ARCHITECTURE.md
    # §7.3) : la première taille (d_model=64, n_layer=2, ~115k paramètres)
    # tenait dans un entraînement de quelques minutes mais restait TROP
    # petite pour apprendre grand-chose d'un corpus de code Python sur un
    # nombre de pas raisonnable. Chronométré avant d'augmenter (pas
    # deviné) : ce palier tourne à ~1s/pas boucle d'entraînement complète
    # incluse (OffloadedAdam compris) sur le poste de développement --
    # quelques milliers de pas restent un entraînement de l'ordre de
    # l'heure, pas du jour.
    name="shion-nano",
    vocab_size=260,
    d_model=96,
    n_layer=3,
    n_head=4,
    d_ff=384,
    window=80,
    max_seq_len=768,
    quant_bits=4,
    quant_group=32,
)

MICRO = ShionConfig(
    name="shion-micro",
    vocab_size=260,
    d_model=384,
    n_layer=8,
    n_head=6,
    d_ff=1536,
    window=256,
    max_seq_len=2048,
    quant_bits=4,
    quant_group=64,
)

BASE = ShionConfig(
    name="shion-base",
    vocab_size=260,
    d_model=1024,
    n_layer=16,
    n_head=16,
    d_ff=4096,
    window=1024,
    max_seq_len=4096,
    quant_bits=4,
    quant_group=64,
)

PRESETS = {c.name: c for c in (NANO, MICRO, BASE)}


def get_config(name: str = "shion-nano") -> ShionConfig:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(
            "Config Shion inconnue : %r. Disponibles : %s"
            % (name, ", ".join(sorted(PRESETS)))
        )


if __name__ == "__main__":
    for cfg in PRESETS.values():
        n = cfg.param_count()
        print("%-12s %10d paramètres  (~%.2f Mo en INT4)"
              % (cfg.name, n, n * 4 / 8 / 1e6))
