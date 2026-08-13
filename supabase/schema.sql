-- Schéma Supabase pour les comptes UVI + niveaux d'abonnement.
-- À exécuter dans Supabase : SQL Editor -> New query -> coller -> Run.
-- Voir docs/COMPTES_ABONNEMENTS.md pour le contexte complet (création du
-- projet, OAuth Google, Stripe).

create type public.uvi_tier as enum ('free', 'pro', 'plus');

-- Une ligne par utilisateur authentifié (auth.users est géré par Supabase
-- Auth ; cette table stocke ce qui est spécifique à UVI : le niveau
-- d'abonnement et les identifiants Stripe associés).
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null,
  tier public.uvi_tier not null default 'free',
  stripe_customer_id text,
  stripe_subscription_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- Chaque utilisateur ne peut lire que sa propre ligne -- c'est ce que le
-- site web ET l'app de bureau utilisent pour savoir quel niveau afficher.
create policy "profiles_select_own"
  on public.profiles for select
  using (auth.uid() = id);

-- Volontairement AUCUNE policy d'UPDATE/INSERT/DELETE ouverte aux
-- utilisateurs : le niveau d'abonnement ne doit changer que via le webhook
-- Stripe (supabase/functions/stripe-webhook), qui utilise la clé
-- service_role et contourne donc RLS. Un utilisateur ne peut pas
-- s'auto-attribuer le niveau "plus" en modifiant sa propre ligne.

-- Crée automatiquement la ligne profils au moment de l'inscription
-- (premier login Google), avec le niveau "free" par défaut.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (id, email, tier)
  values (new.id, new.email, 'free');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- Déduplication des événements Stripe (les webhooks peuvent être livrés
-- plus d'une fois pour le même événement -- Stripe le documente
-- explicitement). insert échoue sur la clé déjà vue -> la fonction
-- Edge interprète ça comme "déjà traité", pas comme une erreur.
create table public.stripe_events (
  id text primary key,
  received_at timestamptz not null default now()
);

alter table public.stripe_events enable row level security;
-- Aucune policy : cette table n'est accédée que par la fonction Edge via
-- la clé service_role, jamais par un client (site ou app de bureau).
