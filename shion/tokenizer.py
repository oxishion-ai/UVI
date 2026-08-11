"""shion/tokenizer.py — Tokenizer OCTET, zéro entraînement, zéro fichier de
vocabulaire à distribuer.

POURQUOI octet et pas BPE : un tokenizer BPE (façon GPT/Llama) doit être
ENTRAÎNÉ sur un corpus pour produire sa table de fusions, puis cette table
doit être embarquée et chargée (souvent plusieurs centaines de Ko à
plusieurs Mo). Sur le budget de ce projet (0$, disque contraint), un
tokenizer octet est GRATUIT : 256 valeurs possibles couvrent déjà tout
texte UTF-8 (une lettre accentuée devient 2-3 octets, pas un problème pour
un modèle qui apprend sur des séquences d'octets), et vocab_size=260 tient
dans un `int` sans table externe. Le coût réel : des séquences plus
longues qu'avec un tokenizer par sous-mots, compensé ici par la fenêtre
glissante (attention.py / model.py) dont le coût est linéaire, pas
quadratique, en longueur de séquence — voir docs/SHION_ARCHITECTURE.md §2.3.
"""

from __future__ import annotations

from typing import List

BOS, EOS, PAD, UNK = 256, 257, 258, 259
VOCAB_SIZE = 260

SPECIAL_NAMES = {BOS: "<bos>", EOS: "<eos>", PAD: "<pad>", UNK: "<unk>"}


def encode(text: str, add_bos: bool = True, add_eos: bool = False) -> List[int]:
    ids = list(text.encode("utf-8", errors="replace"))
    if add_bos:
        ids = [BOS] + ids
    if add_eos:
        ids = ids + [EOS]
    return ids


def decode(ids: List[int]) -> str:
    raw = bytes(i for i in ids if 0 <= i < 256)
    return raw.decode("utf-8", errors="replace")


if __name__ == "__main__":
    for phrase in ("Shion tourne partout.", "Café à 3€ — ça passe ?", ""):
        ids = encode(phrase)
        back = decode(ids)
        assert back == phrase, (phrase, back)
        print("%-30r -> %2d jetons -> %r" % (phrase, len(ids), back))
    print("OK — encodage/décodage octet corrects (round-trip UTF-8 exact).")
