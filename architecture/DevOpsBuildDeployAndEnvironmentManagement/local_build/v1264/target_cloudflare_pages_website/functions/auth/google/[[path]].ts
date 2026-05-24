// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Cloudflare Pages Function — /auth/google/* proxy.
//
// Per EPIC-002-F-003-S-004 directive: the SharedServices HF Space URL
// must NOT appear in client-side HTML or be exposed publicly (no listing
// it as the redirect URI at Google). All client-side code references
// `https://{env}.chathealthy.ai/auth/google/*` only. This server-side
// Function is the bridge: it forwards each request to the correct
// per-env SharedServices HF Space.
//
// No OAuth logic lives here. Pure pass-through.

export interface Env {
  // Per-env SharedServices HF Space base URL (no trailing slash). Set
  // these as Cloudflare Pages env vars per environment (dev/qa/prod).
  // Falls back to dev if unset so the Function never serves a 500 from
  // a config gap during local CF Pages preview.
  SHARED_SERVICES_BASE_URL?: string;
}

const DEFAULT_SHARED_SERVICES = "https://skipsnow-dev-sharedservicesspace.hf.space";

export const onRequest: PagesFunction<Env> = async (context) => {
  const { request, env, params } = context;
  const path = Array.isArray(params.path) ? params.path.join("/") : (params.path ?? "");

  const base = (env.SHARED_SERVICES_BASE_URL ?? DEFAULT_SHARED_SERVICES).replace(/\/+$/, "");
  const url = new URL(request.url);
  const upstream = `${base}/auth/google/${path}${url.search}`;

  // Forward method, headers, and body. Strip the Host header (Cloudflare
  // will set it appropriately for the upstream). Set redirect: manual so
  // SS's 302 back to chathealthy.ai propagates to the user's browser.
  const headers = new Headers(request.headers);
  headers.delete("host");

  const upstreamResp = await fetch(upstream, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    redirect: "manual",
  });

  return new Response(upstreamResp.body, {
    status: upstreamResp.status,
    statusText: upstreamResp.statusText,
    headers: upstreamResp.headers,
  });
};
