# Déployer le site UVI sur Netlify avec un dépôt GitHub privé

> Pourquoi cette page existe : le site vitrine (`web/site/index.html`) doit
> rester public, mais le code du projet (`uvi_core.py`, `shion/`, etc.) doit
> rester privé. C'est un cas d'usage standard de Netlify — il ne publie que
> le dossier que tu lui désignes (« publish directory »), jamais le dépôt
> entier. Je ne peux pas me connecter à ta place à GitHub ou à Netlify (ce
> sont tes comptes), donc voici la marche à suivre, étape par étape.

## 0. Vérifier que le dépôt GitHub est bien privé

Sur `github.com/oxishion-ai/UVI` → **Settings** → tout en bas, section
**Danger Zone** → **Change repository visibility**. S'il est encore
public, clique sur **Change visibility → Make private** et confirme.

(Vérification faite pendant cette session : le dépôt n'est actuellement
pas accessible via l'API publique de GitHub, donc soit il est déjà privé,
soit il n'a encore jamais été poussé sur GitHub — dans les deux cas,
confirme sa visibilité manuellement sur la page Settings avant de
continuer.)

## 1. Créer un compte Netlify

Va sur [app.netlify.com/signup](https://app.netlify.com/signup) et choisis
**« Sign up with GitHub »**. C'est le choix recommandé ici : Netlify
utilise l'OAuth GitHub (il ne voit jamais ton mot de passe GitHub) et ça
simplifie l'étape suivante.

## 2. Importer le dépôt privé

1. Dans le tableau de bord Netlify → **Add new site → Import an existing
   project**.
2. Choisis **Deploy with GitHub**.
3. Netlify va demander l'autorisation d'accéder à tes dépôts GitHub. GitHub
   affiche un écran avec deux options :
   - « All repositories »
   - **« Only select repositories »** ← choisis celle-ci, puis coche
     uniquement `oxishion-ai/UVI`.

   C'est le point de sécurité important : ça donne à Netlify l'accès à ce
   seul dépôt privé, pas à tout ton compte GitHub.
4. Valide, puis sélectionne `oxishion-ai/UVI` dans la liste des dépôts qui
   s'affiche côté Netlify.

## 3. Configurer le build

Sur l'écran « Site settings » qui suit :

| Champ | Valeur |
|---|---|
| Branch to deploy | `main` |
| Base directory | *(laisser vide)* |
| Build command | *(laisser vide — page HTML statique, rien à compiler)* |
| Publish directory | `web/site` |

Clique **Deploy site**. Netlify clone le dépôt en interne (via l'accès
OAuth restreint accordé à l'étape 2), mais ne met en ligne que le contenu
de `web/site` — `uvi_core.py`, `shion/`, les poids du modèle, etc. ne sont
jamais servis publiquement. C'est la garantie structurelle que « site
public, code privé » cherche : le code reste dans le dépôt privé, seul du
HTML/CSS/JS statique sort de la « publish directory ».

## 4. Domaine

- Par défaut Netlify attribue une URL du type `nom-aleatoire.netlify.app`.
- Pour la changer : **Site settings → Domain management → Options → Edit
  site name** (ex. `uvi-lang.netlify.app`).
- Pour un domaine personnalisé (ex. `uvi.dev`) : **Domain management → Add
  a domain**, puis suis les instructions DNS affichées (Netlify délivre
  ensuite un certificat HTTPS gratuit automatiquement, sans action
  supplémentaire une fois le DNS propagé).

## 5. Déploiement continu

C'est automatique dès l'étape 3 : chaque `git push` sur `main` redéclenche
un déploiement Netlify en quelques secondes, sans rien faire de plus.

## 6. (Plus tard) Héberger aussi Shion Mobile séparément

`web/shion-mobile` est une seconde page statique indépendante. Une fois
prête à être présentée, répète les étapes 2-3 pour créer un **second site
Netlify** pointant vers le même dépôt privé, avec cette fois **Publish
directory = `web/shion-mobile`**. Netlify permet de connecter plusieurs
sites au même dépôt, chacun avec son propre dossier de publication et sa
propre URL. Une fois en ligne, remplace le lien placeholder
« Essayer bientôt disponible » dans `web/site/index.html` par l'URL réelle
de ce second site.

## Alternative : Netlify CLI

Si tu préfères la ligne de commande plutôt que l'interface web :

```bash
npm install -g netlify-cli
netlify login        # ouvre un navigateur pour l'auth OAuth — interactif, je ne peux pas le faire à ta place
netlify init          # depuis la racine du dépôt, relie le dossier local à un site Netlify
netlify deploy --prod --dir=web/site
```

`netlify login` ouvre une fenêtre de navigateur et attend une connexion
interactive — c'est pour ça que cette étape doit être faite par toi
directement, je n'ai pas accès à ta session de navigateur.
