---
adr_id: ADR-0005
title: chat-url.txt Committed Per Branch for Iframe URL Injection
status: DECIDED
risk: Low
framework: framework_02
created_by: Claude
created_at: 2026-03-25
---

## Decision
The iframe src URL in the static website is controlled by a committed file (Website/chat-url.txt) and injected at Cloudflare Pages build time via sed substitution.

## Rationale
Engineers control the build. No manual Cloudflare dashboard steps required to change the iframe URL. One-line git commit changes the target per environment.

## Constraints
- Build command: CHAT_URL=$(cat chat-url.txt) && sed -i "s|%%CHAT_URL%%|$CHAT_URL|g" index.html
- chat-url.txt must exist on every branch that deploys to Cloudflare Pages
- Placeholder %%CHAT_URL%% must exist in Website/index.html

## Components
Website
