# Colloqui integration contract

How an external service (for example a self-hosted CRM) integrates with Colloqui.
This is the contract both sides honor. The CRM is built against it; Colloqui
already speaks it. Scope of this version: bidirectional events, separate logins
(no SSO), and link-out (no embedded chat UI).

## Topology

Both apps are self-hosted Docker services with their own Postgres database. They
talk over HTTP server to server. Nothing leaves your infrastructure, so the
no-third-party principle holds.

- Same host or LAN: put both on a shared Docker network and have the CRM call
  Colloqui at `http://colloqui-api:8000` (the api container listens on 8000).
- Across hosts: call the public origin, e.g. `https://colloqui.jjrrr.co`.

All API paths below are under `/api/v1`. Breaking changes would ship under a new
prefix (`/api/v2`); additive changes will not.

## Authentication: API keys

The CRM authenticates with a long-lived API key, sent as a bearer token:

```
Authorization: Bearer colq_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

An API key is created by a Colloqui server admin and is bound to a designated
**service user** (for example a user named `crm`). Requests made with the key act
as that user and inherit exactly that user's permissions and channel
memberships. This reuses Colloqui's normal authorization: to let the CRM read or
post in a channel, add the service user to that channel (or make it an admin).

Keys never expire but can be revoked. Treat a key like a password: store it in
the CRM's secret store, never in source.

### Managing keys (admin only)

```
POST   /api/v1/admin/api-keys      {"name": "crm", "username": "crm"}
       -> 201 {"id", "name", "user_id", "key": "colq_..."}   # key shown once
GET    /api/v1/admin/api-keys      -> [{"id","name","user_id","created_at","last_used_at"}]
DELETE /api/v1/admin/api-keys/{id} -> 204                     # revoke
```

Create the `crm` service user first (admin > users, or `POST /api/v1/admin/users`
with a password), then mint a key bound to it, then add it to the channels the
CRM will use.

## CRM to Colloqui (push into chat)

Two options, use either or both:

1. **API key + REST** (full control, acts as the service user):
   ```
   POST /api/v1/channels/{channel_id}/messages   (via the messages API)
   ```
   Supply a client-generated `id` (UUID) on the message body for idempotent
   send: replaying the same id returns the existing message instead of creating
   a duplicate. Safe to retry.

2. **Incoming webhook** (simplest, per channel, no key needed):
   ```
   POST /hooks/{webhook_token}   {"text": "Deal closed: Acme", "name": "CRM"}
   ```
   Create the token in Colloqui under the channel's webhook settings. Good for
   one-way "post this notification to this channel" cases.

## Colloqui to CRM (chat events out): outgoing webhooks

Colloqui POSTs events to a URL you register. Use this to react to chat in near
real time (for example, log a customer reply against the matching CRM record).

### Subscribing (admin only)

```
POST   /api/v1/admin/event-subscriptions   {"url": "https://crm.internal/colloqui/events", "events": ["message.created"]}
       -> 201 {"id","url","events","active","secret": "whsec_..."}   # secret shown once
GET    /api/v1/admin/event-subscriptions   -> [{"id","url","events","active","created_at"}]
DELETE /api/v1/admin/event-subscriptions/{id} -> 204
```

`events` is optional; omit it to receive all event types.

### Delivery format

Each event is a POST with a JSON body and these headers:

```
X-Colloqui-Event:     message.created
X-Colloqui-Delivery:  <uuid>                      # unique per delivery attempt
X-Colloqui-Signature: sha256=<hex>                # HMAC-SHA256 of the raw body
Content-Type:         application/json
```

Body:

```json
{
  "id": "<event uuid>",
  "type": "message.created",
  "sent_at": "2026-06-26T21:00:00Z",
  "data": { "...": "the MessageOut object (channel_id, sender, content, ...)" }
}
```

Verify the signature by computing `HMAC_SHA256(secret, raw_request_body)` and
comparing (constant time) to the hex after `sha256=`. Reject on mismatch.

Event types in this version: `message.created`, `message.updated`,
`message.deleted` (the deleted event carries the id and a tombstone, not full
content).

### Reliability rules for the CRM endpoint

- **Return 2xx fast.** Do the real work asynchronously. Delivery uses a short
  timeout; slow endpoints get treated as failures.
- **Be idempotent.** Dedupe on the event `id` (or `X-Colloqui-Delivery`).
  Delivery is best effort with limited retries, so an event can arrive more than
  once, and on rare occasions not at all.
- **Backfill with the sync feed** (below) after any downtime; do not rely on
  webhooks alone for completeness.

## Pull alternative: the sync feed

If you prefer polling, or to backfill missed events:

```
GET /api/v1/sync?since=<cursor>&limit=<n>
    -> {"messages": [MessageOut...], "cursor": <int>, "has_more": bool}
```

Deleted messages come through with `deleted_at` set (tombstones). Start from
`since=0`, persist the returned `cursor`, and call again while `has_more` is true.
This is the authoritative, gap-free record of message changes across the service
user's channels.

## Linking CRM records to chat

- Every Colloqui id is a stable UUID. Store the relevant channel UUID on the CRM
  record (contact, company, or deal).
- Recommended model: one channel per account/customer. Create it from the CRM via
  the channels API, add the service user (and the humans who should see it), then
  save the returned channel id on the CRM record.
- To jump a user from the CRM into a conversation, deep link to the channel with
  a `channel` query parameter (optionally `root` for a thread):
  `https://colloqui.jjrrr.co/?channel=<channel_id>` (link-out; opens Colloqui in
  its own tab). The user signs in to Colloqui separately (no SSO in this phase).

## Identifiers and idempotency, in brief

- Ids: UUIDs everywhere.
- Idempotent send: client-supplied message `id`.
- Idempotent receive: dedupe webhook events on their `id`.

## Out of scope this phase (revisit later)

- **SSO / shared identity.** Users have separate accounts in each app. A future
  phase could add OIDC.
- **Embedded chat UI.** Colloqui sends `X-Frame-Options: DENY` and
  `frame-ancestors 'none'`, so it cannot be iframed yet. Link out instead.
- **Browser-side calls from the CRM.** Colloqui sends no CORS headers, so call it
  from the CRM backend, not from the CRM's browser code.
