// Fonction Edge Supabase (Deno) : reçoit les webhooks Stripe et met à jour
// profiles.tier en conséquence. C'est le SEUL endroit autorisé à changer le
// niveau d'abonnement d'un utilisateur (voir supabase/schema.sql : aucune
// policy RLS ne permet à un client de le faire lui-même).
//
// Déploiement : voir docs/COMPTES_ABONNEMENTS.md §6-7.
//   supabase functions deploy stripe-webhook
//
// Secrets requis (supabase secrets set ...) :
//   STRIPE_SECRET_KEY            clé secrète Stripe (sk_live_... / sk_test_...)
//   STRIPE_WEBHOOK_SECRET         signing secret de l'endpoint webhook (whsec_...)
//   STRIPE_PRICE_PRO               Price ID Stripe de l'abonnement UVI Pro
//   STRIPE_PRICE_PLUS              Price ID Stripe de l'abonnement UVI Plus
//   SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY  fournis automatiquement par
//     Supabase à toute fonction Edge -- rien à configurer pour ceux-là.

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import Stripe from "https://esm.sh/stripe@14?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});
const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET")!;

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const PRICE_TO_TIER: Record<string, "pro" | "plus"> = {
  [Deno.env.get("STRIPE_PRICE_PRO")!]: "pro",
  [Deno.env.get("STRIPE_PRICE_PLUS")!]: "plus",
};

function tierForPrice(priceId: string | undefined): "free" | "pro" | "plus" {
  if (!priceId) return "free";
  return PRICE_TO_TIER[priceId] ?? "free";
}

serve(async (req) => {
  const signature = req.headers.get("stripe-signature");
  const body = await req.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, signature!, webhookSecret);
  } catch (err) {
    return new Response(`Signature webhook invalide : ${(err as Error).message}`, { status: 400 });
  }

  // Idempotence : Stripe peut livrer le même événement plusieurs fois.
  const { error: dedupError } = await supabase.from("stripe_events").insert({ id: event.id });
  if (dedupError) {
    return new Response("ok (déjà traité)", { status: 200 });
  }

  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object as Stripe.Checkout.Session;
      const userId = session.client_reference_id;
      const subscriptionId = session.subscription as string | null;
      if (userId && subscriptionId) {
        const subscription = await stripe.subscriptions.retrieve(subscriptionId);
        const priceId = subscription.items.data[0]?.price.id;
        await supabase
          .from("profiles")
          .update({
            tier: tierForPrice(priceId),
            stripe_customer_id: session.customer as string,
            stripe_subscription_id: subscriptionId,
            updated_at: new Date().toISOString(),
          })
          .eq("id", userId);
      }
      break;
    }

    case "customer.subscription.updated": {
      const subscription = event.data.object as Stripe.Subscription;
      const priceId = subscription.items.data[0]?.price.id;
      const active = subscription.status === "active" || subscription.status === "trialing";
      await supabase
        .from("profiles")
        .update({
          tier: active ? tierForPrice(priceId) : "free",
          updated_at: new Date().toISOString(),
        })
        .eq("stripe_subscription_id", subscription.id);
      break;
    }

    case "customer.subscription.deleted": {
      const subscription = event.data.object as Stripe.Subscription;
      await supabase
        .from("profiles")
        .update({ tier: "free", updated_at: new Date().toISOString() })
        .eq("stripe_subscription_id", subscription.id);
      break;
    }
  }

  return new Response("ok", { status: 200 });
});
