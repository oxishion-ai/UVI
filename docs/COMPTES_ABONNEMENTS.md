# Comptes UVI, connexion Google et abonnements — mise en place

> Comme pour `docs/DEPLOIEMENT_NETLIFY.md`, cette page couvre des étapes
> qui se font dans TES comptes (Supabase, Google Cloud, Stripe) — je ne
> peux pas les créer à ta place. Le code qui s'appuie dessus
> (`supabase/schema.sql`, `supabase/functions/stripe-webhook/`, la
> connexion dans `uvi_core.py`, la page web) est déjà écrit ou en cours ;
> cette page explique quoi configurer, dans quel ordre, pour que ce code
> ait quelque chose à quoi se connecter.

## Vue d'ensemble

```
Utilisateur
   |
   |  1. Clique "Se connecter avec Google" (site web OU app de bureau)
   v
Google  --(consentement)-->  Supabase Auth  --(callback)-->  session (JWT)
   |
   |  2. Le site web / l'app de bureau lit profiles.tier via l'API Supabase
   v
profiles.tier = 'free' | 'pro' | 'plus'   <-- table Postgres, RLS : lecture
                                               seule pour l'utilisateur
   ^
   |  3. Stripe notifie l'abonnement (achat / annulation) par webhook
   |
Stripe  -->  fonction Edge Supabase (stripe-webhook)  -->  met à jour tier
```

Trois services externes, chacun avec un rôle précis :

| Service | Rôle | Compte nécessaire |
|---|---|---|
| **Supabase** | Base de données Postgres + authentification (dont Google) | supabase.com, gratuit pour ce volume |
| **Google Cloud** | Fournisseur d'identité pour "Se connecter avec Google" | console.cloud.google.com |
| **Stripe** | Encaissement des abonnements UVI Pro / UVI Plus | stripe.com |

## 1. Créer le projet Supabase

1. [supabase.com](https://supabase.com) → **Start your project** → connecte-toi
   (GitHub recommandé, cohérent avec Netlify).
2. **New project** : nom `uvi`, mot de passe de base de données (généré,
   garde-le de côté), région proche de tes utilisateurs.
3. Une fois le projet créé, note deux valeurs dans **Project Settings →
   API** :
   - **Project URL** (`https://xxxxxxxx.supabase.co`)
   - **anon public key** — celle-ci est faite pour être publique côté
     client (site web, app de bureau) ; la sécurité vient des règles RLS,
     pas du secret de cette clé.
   - Note aussi la **service_role key** à part, dans un endroit privé —
     elle ne doit **jamais** apparaître dans le site web ni dans
     `uvi_core.py`. Elle ne sert que pour la fonction webhook (§7).

## 2. Charger le schéma

**SQL Editor** (menu de gauche) → **New query** → colle le contenu de
[`supabase/schema.sql`](../supabase/schema.sql) → **Run**.

Ça crée :
- `public.profiles` (une ligne par utilisateur, colonne `tier`)
- Une règle RLS qui ne laisse chaque utilisateur lire QUE sa propre ligne
- Un déclencheur qui crée automatiquement la ligne `profiles` (niveau
  `free`) à la première connexion
- `public.stripe_events` pour la dé-duplication des webhooks (§7)

## 3. Activer Google comme fournisseur de connexion

**Authentication → Providers → Google** → active le bouton. Supabase
affiche une **Callback URL (for OAuth)** du type :

```
https://xxxxxxxx.supabase.co/auth/v1/callback
```

Garde cet onglet ouvert, elle sert à l'étape suivante.

## 4. Créer les identifiants OAuth côté Google Cloud

1. [console.cloud.google.com](https://console.cloud.google.com) → crée un
   projet (ou réutilise un projet existant) → nomme-le `UVI`.
2. **APIs & Services → OAuth consent screen** :
   - Type : **External**
   - Nom de l'app : `UVI`
   - Logo : `assets/uvi_icon_128.png`
   - Domaine autorisé : ton domaine Netlify (voir
     `docs/DEPLOIEMENT_NETLIFY.md`) une fois connu
   - Scopes : garde les scopes par défaut (`email`, `profile`, `openid`) —
     UVI n'a besoin de rien de plus qu'identifier l'utilisateur.
3. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** :
   - Type d'application : **Web application**
   - Nom : `UVI - Supabase`
   - **Authorized redirect URIs** : colle exactement la Callback URL
     notée à l'étape 3 (`https://xxxxxxxx.supabase.co/auth/v1/callback`).
4. Google affiche un **Client ID** et un **Client Secret** — colle les
   deux dans Supabase (**Authentication → Providers → Google**), puis
   **Save**.

Un seul client OAuth Google suffit pour le site web ET l'app de bureau :
c'est Supabase qui parle à Google dans les deux cas (voir §8 pour comment
l'app de bureau, elle, reçoit la session en retour).

## 5. Autoriser les URLs de redirection

**Authentication → URL Configuration → Redirect URLs**, ajoute :

- L'URL de ton site Netlify une fois déployé (`docs/DEPLOIEMENT_NETLIFY.md`),
  ex. `https://uvi-lang.netlify.app/**`
- `http://127.0.0.1:53682/callback` — port fixe local utilisé par
  l'app de bureau pour récupérer la session après le login Google (§8) ;
  ne pas changer ce port sans le changer aussi dans `uvi_core.py`.

## 6. Créer les produits Stripe

1. [dashboard.stripe.com](https://dashboard.stripe.com) → active le mode
   **Test** pour développer sans encaisser de vrai argent, tu repasseras
   en **Live** une fois prêt.
2. **Product catalog → Add product**, deux fois :
   - **UVI Pro** — prix récurrent mensuel, ex. `9,99 €/mois`
   - **UVI Plus** — prix récurrent mensuel, ex. `19,99 €/mois`
3. Pour chaque prix créé, note son **Price ID** (`price_xxxxxxxxxxxx`,
   visible sur la page du produit) — nécessaire pour la fonction webhook
   (§7) ET pour construire un lien de paiement (§9).
4. **Payment Links** (menu de gauche) → **New** → un lien par produit.
   Dans les options du lien :
   - **After payment**, redirige vers une page de ton site
     (ex. `https://uvi-lang.netlify.app/?abonnement=succes`)
   - Laisse **client reference ID** activable par URL (par défaut chez
     Stripe) : c'est ce qui permet au site d'ajouter
     `?client_reference_id=<id-utilisateur-supabase>` quand il génère le
     bouton "Passer à Pro/Plus", pour que le webhook (§7) sache à quel
     compte Supabase rattacher l'abonnement.

## 7. Déployer la fonction webhook

Le code est déjà dans
[`supabase/functions/stripe-webhook/index.ts`](../supabase/functions/stripe-webhook/index.ts).
Depuis une machine avec la Supabase CLI (`npm install -g supabase`) :

```bash
supabase login
supabase link --project-ref xxxxxxxx        # le ref est dans Project Settings -> General

supabase secrets set \
  STRIPE_SECRET_KEY=sk_test_xxx \
  STRIPE_WEBHOOK_SECRET=whsec_xxx \
  STRIPE_PRICE_PRO=price_xxx \
  STRIPE_PRICE_PLUS=price_xxx

supabase functions deploy stripe-webhook
```

(`SUPABASE_URL` et `SUPABASE_SERVICE_ROLE_KEY` sont injectées
automatiquement par Supabase à toutes les fonctions Edge — inutile de les
définir toi-même.)

La fonction est alors joignable à :

```
https://xxxxxxxx.supabase.co/functions/v1/stripe-webhook
```

Dans Stripe : **Developers → Webhooks → Add endpoint**, colle cette URL,
sélectionne les événements `checkout.session.completed`,
`customer.subscription.updated`, `customer.subscription.deleted`. Stripe
affiche alors un **Signing secret** (`whsec_...`) — c'est la valeur à
mettre dans `STRIPE_WEBHOOK_SECRET` ci-dessus (à refaire si tu régénères
l'endpoint).

## 8. Variables d'environnement à renseigner

Une fois tout ce qui précède fait, deux jeux de variables (jamais la
`service_role key`, qui ne doit vivre que dans les secrets Supabase du
§7) :

**Site web** (`web/site/index.html`, valeurs à coller dans le script —
voir la section suivante de mon plan de travail pour le câblage exact) :

```
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
STRIPE_PAYMENT_LINK_PRO=https://buy.stripe.com/xxxxx
STRIPE_PAYMENT_LINK_PLUS=https://buy.stripe.com/xxxxx
```

**App de bureau**, dans `.shion.local.env` (mécanisme déjà existant, non
versionné, voir `.gitignore` et `_charger_config_shion_locale` dans
`uvi_core.py`) :

```
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
```

## 9. Ce que ça donne côté app de bureau (aperçu)

L'app de bureau ne peut pas recevoir un callback OAuth classique (pas de
domaine web à elle) : elle ouvre le navigateur système sur l'URL
d'autorisation Supabase, avec un `redirect_to` qui pointe vers
`http://127.0.0.1:53682/callback` — un tout petit serveur HTTP local que
l'app démarre juste avant d'ouvrir le navigateur, le temps de récupérer le
jeton, puis s'arrête. C'est le patron standard pour les apps de bureau
(RFC 8252) adapté à Supabase Auth. Détails d'implémentation dans
`uvi_core.py` une fois cette partie câblée (§4 du fil de travail en
cours).
