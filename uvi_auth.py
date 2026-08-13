"""uvi_auth.py — connexion Google (via Supabase Auth) et niveau d'abonnement
pour l'app de bureau UVI.

STDLIB UNIQUEMENT (urllib, http.server, webbrowser) -- même philosophie que
shion/serving.py : un module minimal, importable sans dépendance lourde,
même si l'authentification n'est pas configurée.

Mise en place complète (projet Supabase, OAuth Google, Stripe) : voir
docs/COMPTES_ABONNEMENTS.md. Tant que SUPABASE_URL/SUPABASE_ANON_KEY ne
sont pas renseignées (voir is_configured()), ce module est un no-op : le
reste de uvi_core.py garde son comportement historique non-gaté (voir le
commentaire dans _build_shion_client / _exec_shion).

Session persistée dans .uvi_prefs.json, clé "auth", à côté de la clé
"langue" déjà utilisée par l'IDE -- même fichier, mêmes règles de fusion
(on ne réécrit jamais l'autre clé).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()

LOOPBACK_PORT = 53682
LOOPBACK_REDIRECT = "http://127.0.0.1:%d/callback" % LOOPBACK_PORT
LOGIN_TIMEOUT_S = 180

_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".uvi_prefs.json")

TIERS = ("free", "pro", "plus")


class UviAuthError(Exception):
    """Erreur de connexion/abonnement -- message toujours déjà actionable,
    même contrat que ShionServingError/DeepSeekClientError."""


def is_configured() -> bool:
    """Faux tant que SUPABASE_URL/SUPABASE_ANON_KEY n'ont pas été
    renseignées (voir docs/COMPTES_ABONNEMENTS.md) -- dans ce cas, l'IDE ne
    doit JAMAIS gater SHION.ASK : c'est le comportement de développement
    local historique, inchangé."""
    return bool(SUPABASE_URL) and bool(SUPABASE_ANON_KEY) and "VOTRE-PROJET" not in SUPABASE_URL


# ---------------------------------------------------------------------------
# Persistance locale (.uvi_prefs.json, clé "auth")
# ---------------------------------------------------------------------------

def _lire_prefs() -> Dict[str, Any]:
    try:
        with open(_PREFS_PATH, "r", encoding="utf-8") as f:
            donnees = json.load(f)
            return donnees if isinstance(donnees, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _ecrire_prefs(donnees: Dict[str, Any]) -> None:
    try:
        with open(_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(donnees, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_session() -> Optional[Dict[str, Any]]:
    return _lire_prefs().get("auth")


def save_session(session: Optional[Dict[str, Any]]) -> None:
    donnees = _lire_prefs()
    if session is None:
        donnees.pop("auth", None)
    else:
        donnees["auth"] = session
    _ecrire_prefs(donnees)


def logout() -> None:
    save_session(None)


# ---------------------------------------------------------------------------
# Appels réseau Supabase (stdlib urllib uniquement, même patron que
# DeepSeekClient dans uvi_core.py)
# ---------------------------------------------------------------------------

def _requete_supabase(url: str, *, headers: Dict[str, str], data: Optional[bytes] = None,
                       method: str = "GET", timeout_s: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=data, method=method)
    for cle, valeur in headers.items():
        req.add_header(cle, valeur)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            corps = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise UviAuthError("Supabase a refusé la requête (HTTP %d) : %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise UviAuthError("Impossible de joindre Supabase (%s) : %s" % (url, e))
    try:
        return json.loads(corps) if corps else {}
    except json.JSONDecodeError:
        raise UviAuthError("Réponse Supabase illisible depuis %s." % url)


def _refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    url = "%s/auth/v1/token?grant_type=refresh_token" % SUPABASE_URL
    payload = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    return _requete_supabase(url, method="POST", data=payload, headers={
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    })


def _fetch_user(access_token: str) -> Dict[str, Any]:
    url = "%s/auth/v1/user" % SUPABASE_URL
    return _requete_supabase(url, headers={
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": "Bearer %s" % access_token,
    })


def _fetch_tier(access_token: str, user_id: str) -> str:
    url = "%s/rest/v1/profiles?id=eq.%s&select=tier" % (SUPABASE_URL, urllib.parse.quote(user_id))
    lignes = _requete_supabase(url, headers={
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": "Bearer %s" % access_token,
    })
    if isinstance(lignes, list) and lignes and lignes[0].get("tier") in TIERS:
        return lignes[0]["tier"]
    return "free"


# ---------------------------------------------------------------------------
# Connexion Google -- ouvre le navigateur système, récupère la session via
# un petit serveur HTTP local temporaire (patron RFC 8252 pour app de
# bureau, adapté à Supabase Auth -- voir docs/COMPTES_ABONNEMENTS.md §9).
# ---------------------------------------------------------------------------

_PAGE_CALLBACK = """<!doctype html><html><head><meta charset="utf-8">
<title>Connexion UVI</title></head><body style="font-family:sans-serif;
padding:40px;text-align:center;">
<p id="msg">Connexion en cours...</p>
<script>
(function () {
  var hash = window.location.hash.replace(/^#/, "");
  var params = new URLSearchParams(hash);
  var msg = document.getElementById("msg");
  if (params.get("error")) {
    msg.textContent = "Connexion refusée : " + (params.get("error_description") || params.get("error"));
    return;
  }
  var accessToken = params.get("access_token");
  var refreshToken = params.get("refresh_token");
  if (!accessToken || !refreshToken) {
    msg.textContent = "Réponse de connexion incomplète -- réessaie depuis UVI.";
    return;
  }
  fetch("/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_token: accessToken, refresh_token: refreshToken }),
  }).then(function () {
    msg.textContent = "Connexion réussie -- tu peux fermer cette fenêtre et revenir à UVI.";
  }).catch(function () {
    msg.textContent = "Connexion reçue mais non transmise à UVI -- réessaie.";
  });
})();
</script></body></html>""".encode("utf-8")


def _construire_url_autorisation() -> str:
    query = urllib.parse.urlencode({"provider": "google", "redirect_to": LOOPBACK_REDIRECT})
    return "%s/auth/v1/authorize?%s" % (SUPABASE_URL, query)


def start_google_login(on_status=None) -> Dict[str, Any]:
    """Ouvre le navigateur système sur l'écran de connexion Google (via
    Supabase), attend la redirection locale, construit et sauvegarde la
    session. Bloquant -- à appeler depuis un thread d'arrière-plan, jamais
    depuis le thread Tkinter principal (même règle que _shion_worker dans
    uvi_core.py)."""
    if not is_configured():
        raise UviAuthError(
            "Connexion non configurée : SUPABASE_URL/SUPABASE_ANON_KEY absentes de "
            ".shion.local.env. Voir docs/COMPTES_ABONNEMENTS.md."
        )

    resultat: Dict[str, Any] = {}
    evenement = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence les logs stdout par défaut
            pass

        def do_GET(self):
            if self.path.startswith("/callback"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_PAGE_CALLBACK)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/token":
                longueur = int(self.headers.get("Content-Length", "0"))
                try:
                    corps = json.loads(self.rfile.read(longueur).decode("utf-8"))
                    resultat["access_token"] = corps["access_token"]
                    resultat["refresh_token"] = corps["refresh_token"]
                except (ValueError, KeyError):
                    resultat["erreur"] = "Jeton de connexion illisible."
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                evenement.set()
            else:
                self.send_response(404)
                self.end_headers()

    serveur = HTTPServer(("127.0.0.1", LOOPBACK_PORT), Handler)
    thread = threading.Thread(target=serveur.serve_forever, daemon=True)
    thread.start()

    if on_status:
        on_status("Ouverture du navigateur pour la connexion Google...")
    webbrowser.open(_construire_url_autorisation())

    recu = evenement.wait(timeout=LOGIN_TIMEOUT_S)
    serveur.shutdown()
    serveur.server_close()

    if not recu:
        raise UviAuthError("Connexion annulée ou expirée (délai de %ds dépassé)." % LOGIN_TIMEOUT_S)
    if "erreur" in resultat:
        raise UviAuthError(resultat["erreur"])

    access_token = resultat["access_token"]
    refresh_token = resultat["refresh_token"]
    utilisateur = _fetch_user(access_token)
    user_id = utilisateur.get("id")
    email = utilisateur.get("email", "")
    if not user_id:
        raise UviAuthError("Connexion Google réussie mais identifiant utilisateur introuvable.")

    tier = _fetch_tier(access_token, user_id)
    session = {
        "user_id": user_id,
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "tier": tier,
        "verifie_le": time.time(),
    }
    save_session(session)
    return session


# ---------------------------------------------------------------------------
# Lecture du niveau d'abonnement courant
# ---------------------------------------------------------------------------

def current_session() -> Optional[Dict[str, Any]]:
    """Session locale telle quelle (pas de vérification réseau) -- utile
    pour l'affichage (email, badge de niveau) sans latence."""
    return load_session()


def current_tier(force_refresh: bool = False, max_age_s: int = 300) -> Optional[str]:
    """None si personne n'est connecté. Sinon le niveau d'abonnement,
    re-vérifié auprès de Supabase toutes les max_age_s secondes (pour
    qu'une mise à niveau Stripe se reflète sans redémarrer l'IDE), avec
    repli sur la dernière valeur connue si le réseau est indisponible --
    une coupure réseau momentanée ne doit pas faire perdre son niveau à un
    utilisateur payant déjà vérifié."""
    session = load_session()
    if session is None:
        return None

    age = time.time() - session.get("verifie_le", 0)
    if not force_refresh and age < max_age_s:
        return session.get("tier", "free")

    try:
        tier = _fetch_tier(session["access_token"], session["user_id"])
    except UviAuthError:
        try:
            rafraichi = _refresh_access_token(session["refresh_token"])
            session["access_token"] = rafraichi["access_token"]
            session["refresh_token"] = rafraichi.get("refresh_token", session["refresh_token"])
            tier = _fetch_tier(session["access_token"], session["user_id"])
        except UviAuthError:
            return session.get("tier", "free")  # hors-ligne : dernière valeur connue

    session["tier"] = tier
    session["verifie_le"] = time.time()
    save_session(session)
    return tier
