# Batch 1: business AI authority

This control plane answers one question before Jarvis acts: **is this AI
identity allowed to perform this action for this business?**

## Safety model

- A CRM `admin` controls settings, identities, scopes, and policy versions.
- Jarvis authenticates with `X-AI-Principal-Token`, not a founder bearer token.
- Only a SHA-256 digest of the high-entropy Jarvis credential is stored.
- Jarvis cannot receive authority-management, approval, or audit-deletion scopes.
- Every query and mutation is scoped by `business_id` from authenticated identity.
- Mutating founder APIs require `Idempotency-Key`.
- Configuration and policy updates append immutable history rows and business events.
- New businesses begin in `recommend_only`, even though example limits are populated.

## Founder interface

Open `/crm/app`, sign in as an `admin`, and choose **Jarvis authority**. The page
allows the founder to configure the business limits, create a Jarvis identity,
and publish new policy versions. Save a new Jarvis credential immediately; it
is not available through any read endpoint.

## Important API routes

- `GET/PUT /crm/ai/settings`
- `GET /crm/ai/settings/history`
- `GET/POST /crm/ai/principals`
- `POST /crm/ai/principals/{id}/rotate`
- `POST /crm/ai/principals/{id}/revoke`
- `POST/DELETE /crm/ai/principals/{id}/scopes`
- `GET/PUT /crm/ai/policies`
- `GET /crm/ai/policies/{id}/history`
- `POST /crm/ai/evaluate` (founder preview)
- `POST /ai/authority/evaluate` (Jarvis credential)

The evaluation endpoint is deterministic and does not itself perform a sales
action. The next integration batch should place it in front of each mutating
business tool (quotation send, discount, reminder, order acceptance, and
handoff). This separation keeps Batch 1 from changing the current graph flow.
