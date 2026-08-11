#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie qu'un VRAI serveur vLLM répond comme DeepSeekClient l'attend.

    python3 server/smoke_test.py http://192.168.1.50:8000

POURQUOI CE FICHIER EXISTE ALORS QUE tests/test_deepseek_client.py EXISTE
DÉJÀ. Les tests automatiques tournent contre un serveur FACTICE qui
reproduit les formats documentés de vLLM. Ils prouvent que le client est
cohérent avec la documentation ; ils ne prouvent pas que votre instance se
comporte comme la documentation. C'est la seule vérification qui demande le
vrai serveur, et elle ne se fait qu'UNE fois, au premier branchement.

Aucune dépendance : urllib seulement, comme le client lui-même. Ce script
doit pouvoir tourner sur le Raspberry Pi client, là où l'on a précisément
refusé d'installer torch.
"""
import json
import sys
import urllib.error
import urllib.request

VERT, ROUGE, JAUNE, GRIS, RAZ = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"
resultats = []


def etape(titre):
    print("\n%s──%s %s" % (GRIS, RAZ, titre))


def ok(message):
    print("  %s✓%s %s" % (VERT, RAZ, message))
    resultats.append(True)


def echec(message, correctif=""):
    print("  %s✗%s %s" % (ROUGE, RAZ, message))
    if correctif:
        print("    %s→ %s%s" % (JAUNE, correctif, RAZ))
    resultats.append(False)


def appeler(url, charge=None, cle=None, timeout=180):
    requete = urllib.request.Request(url, data=charge, method="POST" if charge else "GET")
    if charge:
        requete.add_header("Content-Type", "application/json")
    requete.add_header("Authorization", "Bearer %s" % (cle or "uvi-local"))
    with urllib.request.urlopen(requete, timeout=timeout) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


def principal(base_url, cle):
    base_url = base_url.rstrip("/")
    print("Serveur cible : %s" % base_url)

    # --- 1. Le nom du modèle : LA vérification qui compte -----------------
    etape("1/5  Nom exact du modèle servi")
    nom_modele = None
    try:
        modeles = appeler("%s/v1/models" % base_url, cle=cle, timeout=15)
        noms = [m.get("id") for m in modeles.get("data", [])]
        if not noms:
            echec("le serveur ne déclare aucun modèle")
        else:
            nom_modele = noms[0]
            ok("modèle servi : %s" % nom_modele)
            print("    %sexport DEEPSEEK_MODEL_NAME=\"%s\"%s" % (JAUNE, nom_modele, RAZ))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            echec("HTTP 401 sur /v1/models",
                  "le serveur exige une clé : relancez avec --cle VOTRE_JETON")
        else:
            echec("HTTP %d sur /v1/models" % e.code)
    except urllib.error.URLError as e:
        echec("serveur injoignable (%s)" % e.reason,
              "vérifiez que vLLM tourne et que le port est ouvert")
        return 1

    if nom_modele is None:
        return 1

    # --- 2. Une vraie complétion ------------------------------------------
    etape("2/5  Complétion sur /v1/chat/completions")
    charge = json.dumps({
        "model": nom_modele,
        # Pas de message system : consigne officielle DeepSeek pour R1.
        "messages": [{"role": "user", "content":
                      "Réponds en un seul mot : quelle est la capitale de la France ?"}],
        "temperature": 0.6, "top_p": 0.95, "max_tokens": 2048, "stream": False,
    }).encode("utf-8")
    try:
        reponse = appeler("%s/v1/chat/completions" % base_url, charge, cle)
    except urllib.error.HTTPError as e:
        corps = e.read().decode("utf-8", "replace")[:300]
        if e.code == 404:
            echec("HTTP 404 — nom de modèle refusé",
                  "utilisez exactement : %s" % nom_modele)
        else:
            echec("HTTP %d : %s" % (e.code, corps))
        return 1
    except Exception as e:
        echec("échec de la requête : %s" % e)
        return 1

    choix = (reponse.get("choices") or [{}])[0]
    message = choix.get("message") or {}
    contenu = message.get("content") or ""
    if contenu:
        ok("réponse reçue (%d caractères, finish_reason=%s)"
           % (len(contenu), choix.get("finish_reason")))
    else:
        echec("réponse vide (finish_reason=%s)" % choix.get("finish_reason"),
              "si finish_reason='length', augmentez max_tokens")

    # --- 3. Le parseur de raisonnement est-il actif ? ----------------------
    etape("3/5  Séparation du raisonnement (--reasoning-parser deepseek_r1)")
    if message.get("reasoning_content"):
        ok("reasoning_content séparé : le drapeau est actif, content est propre")
    elif "<think>" in contenu or "</think>" in contenu:
        echec("le raisonnement arrive DANS content",
              "relancez vLLM avec --reasoning-parser deepseek_r1 ; sans lui, "
              "SHION.ASK stockera du monologue interne dans vos variables UVI "
              "(le client sait le nettoyer, mais mieux vaut la source)")
    else:
        ok("aucune balise <think> dans content")

    # --- 4. Forme du corps d'erreur ---------------------------------------
    etape("4/5  Format des erreurs (nom de modèle volontairement faux)")
    mauvaise_charge = json.dumps({
        "model": "modele-qui-nexiste-pas",
        "messages": [{"role": "user", "content": "test"}], "max_tokens": 8,
    }).encode("utf-8")
    try:
        appeler("%s/v1/chat/completions" % base_url, mauvaise_charge, cle, timeout=20)
        echec("un modèle inexistant a été accepté (inattendu)")
    except urllib.error.HTTPError as e:
        try:
            corps = json.loads(e.read().decode("utf-8"))
        except Exception:
            corps = {}
        plat = "message" in corps
        imbrique = isinstance(corps.get("error"), dict)
        if e.code == 404 and (plat or imbrique):
            ok("HTTP 404 + corps %s — conforme à ce que traduit le client"
               % ("plat" if plat else "imbriqué"))
        else:
            echec("forme inattendue : HTTP %d, clés=%s" % (e.code, list(corps)),
                  "signalez-le : DeepSeekClient._traduire_http devra être ajusté")
    except Exception as e:
        echec("erreur inattendue : %s" % e)

    # --- 5. Multi-tours ----------------------------------------------------
    etape("5/5  Conversation à plusieurs tours")
    suite = json.dumps({
        "model": nom_modele,
        "messages": [
            {"role": "user", "content": "Retiens le nombre 42."},
            {"role": "assistant", "content": "D'accord, je retiens 42."},
            {"role": "user", "content": "Quel nombre t'ai-je demandé de retenir ?"},
        ],
        "temperature": 0.6, "top_p": 0.95, "max_tokens": 2048,
    }).encode("utf-8")
    try:
        r2 = appeler("%s/v1/chat/completions" % base_url, suite, cle)
        texte = ((r2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if "42" in texte:
            ok("le contexte multi-tours est bien pris en compte")
        else:
            echec("le modèle n'a pas restitué le contexte (réponse : %s)" % texte[:80],
                  "non bloquant, mais surveillez la qualité des réponses")
    except Exception as e:
        echec("échec du test multi-tours : %s" % e)

    # --- Bilan -------------------------------------------------------------
    reussis, total = sum(resultats), len(resultats)
    print("\n" + "─" * 58)
    if reussis == total:
        print("%s%d/%d vérifications passées.%s UVI peut être branché :"
              % (VERT, reussis, total, RAZ))
        print('  export SHION_SERVER_URL="%s"' % base_url)
        print('  export DEEPSEEK_MODEL_NAME="%s"' % nom_modele)
        return 0
    print("%s%d/%d vérifications passées.%s Corrigez ce qui précède avant de "
          "brancher UVI." % (ROUGE, reussis, total, RAZ))
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cle_api = sys.argv[sys.argv.index("--cle") + 1] if "--cle" in sys.argv else "uvi-local"
    sys.exit(principal(sys.argv[1], cle_api))
