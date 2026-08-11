"""shion/server.py — Serveur Shion, API OpenAI-compatible, stdlib SEULE
(http.server) : pas de Flask/FastAPI/uvicorn, cohérent avec la directive
"aucun framework lourd ou superflu" (docs/SHION_ARCHITECTURE.md §1) et une
dépendance de moins sur un poste déjà contraint.

À quoi ça sert, puisque ShionServingClient sait déjà tourner sans serveur
(repli local, voir serving.py) : PARTAGER un modèle entre plusieurs
utilisateurs/processus sur un même réseau (palier 2 du plan de déploiement,
docs/SHION_ARCHITECTURE.md §5) -- c'est la même distinction que
DeepSeekClient/vLLM dans uvi_core.py, en beaucoup plus léger.

Usage :
    python -m shion.server                          # localhost:8000, shion-nano
    python -m shion.server --port 8010 --preset shion-nano --weights chemin/perso
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import get_config
from .engine import ShionEngine

_engine: "ShionEngine | None" = None
_model_name = "shion"
# ShionEngine.cache (LazyLayerCache) n'est pas pensé pour des accès
# concurrents (éviction LRU = état mutable partagé) -- un verrou global
# suffit pour l'usage visé ("une poignée d'utilisateurs sur un LAN"), pas
# pour de la mise à l'échelle massive (hors périmètre de ce projet).
_lock = threading.Lock()


class ShionRequestHandler(BaseHTTPRequestHandler):
    server_version = "ShionHTTP/1.0"

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — signature imposée par BaseHTTPRequestHandler
        pass  # silencieux : évite de polluer stdout d'un IDE qui lancerait ce serveur en sous-processus

    def do_GET(self):
        chemin = self.path.rstrip("/")
        if chemin == "/v1/models":
            self._send_json({"object": "list", "data": [{"id": _model_name, "object": "model"}]})
        elif chemin == "/health" or chemin == "":
            self._send_json({"status": "ok", "model": _model_name})
        else:
            self._send_json({"object": "error", "message": "introuvable : %s" % self.path}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json({"object": "error", "message": "introuvable : %s" % self.path}, 404)
            return
        try:
            longueur = int(self.headers.get("Content-Length", "0"))
            corps = json.loads(self.rfile.read(longueur).decode("utf-8"))
        except Exception as e:
            self._send_json({"object": "error", "message": "corps JSON invalide : %s" % e}, 400)
            return

        tours = [(m.get("role"), str(m.get("content", "")))
                 for m in (corps.get("messages") or []) if m.get("role") in ("user", "assistant")]
        if not tours or tours[-1][0] != "user":
            self._send_json(
                {"object": "error", "message": "'messages' doit se terminer par un tour 'user'"}, 400)
            return

        prompt = tours[-1][1]
        historique = []
        i = 0
        while i + 1 < len(tours) - 1:
            if tours[i][0] == "user" and tours[i + 1][0] == "assistant":
                historique.append((tours[i][1], tours[i + 1][1]))
                i += 2
            else:
                i += 1

        temperature = float(corps.get("temperature", 0.8))
        top_p = float(corps.get("top_p", 0.9))
        max_tokens = int(corps.get("max_tokens", 200))

        with _lock:
            try:
                texte = _engine.chat(historique, prompt, max_new_tokens=max_tokens,
                                      temperature=temperature, top_p=top_p)
            except Exception as e:
                self._send_json({"object": "error", "message": "erreur d'inférence Shion : %s" % e}, 500)
                return

        self._send_json({
            "id": "shion-cmpl-0",
            "object": "chat.completion",
            "model": _model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": texte},
                "finish_reason": "stop",
            }],
        })


def main():
    global _engine, _model_name

    parser = argparse.ArgumentParser(description="Serveur Shion (OpenAI-compatible, stdlib uniquement).")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--preset", default="shion-nano", choices=["shion-nano", "shion-micro", "shion-base"])
    parser.add_argument("--weights", default=None, help="Dossier de checkpoint (défaut : shion/weights/<preset>)")
    parser.add_argument("--resident-layers", type=int, default=None,
                         help="Couches dé-quantifiées gardées en RAM simultanément (voir "
                              "memory.py). Défaut : le modèle ENTIER (toutes ses couches) -- "
                              "voir le commentaire ci-dessous sur pourquoi une petite valeur "
                              "par défaut cassait la génération plutôt que d'économiser de la "
                              "RAM. Ne réduire qu'explicitement, sur Shion-Base/Micro et RAM "
                              "contrainte, en connaissance du coût en vitesse.")
    args = parser.parse_args()

    _model_name = args.preset
    config = get_config(args.preset)
    weights_dir = args.weights or os.path.join(os.path.dirname(__file__), "weights", args.preset)
    if not os.path.isdir(weights_dir):
        raise SystemExit(
            "Checkpoint introuvable : %s\nEntraînez-en un d'abord :\n"
            "  python -m shion.train --out %s" % (weights_dir, weights_dir)
        )

    # BUGFIX : l'ancien défaut (2) est plus petit que n_layer pour LES TROIS
    # paliers (3/8/16) -- en génération autorégressive, qui retraverse toutes
    # les couches À CHAQUE token, une capacité < n_layer fait rater le cache
    # sur CHAQUE couche à CHAQUE token (la couche 0 est évincée avant d'être
    # redemandée) : dé-quantification complète du modèle par token généré,
    # mesuré ~15x plus lent sur Shion-Nano. Le défaut couvre donc désormais
    # le modèle entier ; --resident-layers reste disponible pour borrer la
    # RAM explicitement sur Shion-Base en connaissance de ce compromis.
    resident_layers = args.resident_layers if args.resident_layers is not None else config.n_layer
    _engine = ShionEngine(weights_dir, config, max_resident_layers=resident_layers)
    httpd = ThreadingHTTPServer((args.host, args.port), ShionRequestHandler)
    print("=== Serveur Shion ===")
    print("  modèle       : %s" % args.preset)
    print("  écoute       : %s:%d" % (args.host, args.port))
    print("  checkpoint   : %s" % weights_dir)
    print("  couches résidentes max : %d / %d" % (resident_layers, config.n_layer))
    print("\n  Côté UVI :")
    print("    SHION_BACKEND=shion")
    print("    SHION_SERVER_URL=http://<cette machine>:%d" % args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur Shion.")


if __name__ == "__main__":
    main()
