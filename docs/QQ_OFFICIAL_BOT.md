# Tencent official QQ Bot transport

Misty can receive official QQ group events through the pinned Tencent Node SDK,
then use the same Runtime, Agent, memory, image tools, attention checks and durable
segmented reply ledger as the OneBot transport. This adapter does not run OpenClaw
or a second conversation model. `tools/qqbot/bridge.mjs` handles authentication,
gateway events and one authenticated reply POST per reserved segment.

## Scope and credentials

Use an administrator-owned QQ Open Platform application. Platform certification,
group membership and event permissions must be completed in Tencent's console and
QQ. The adapter handles `GROUP_AT_MESSAGE_CREATE` and `GROUP_MESSAGE_CREATE` when
Tencent grants and delivers them. Gateway READY alone does not prove either event
permission, group membership, image semantics or successful replies.

Install Node.js 20 or newer. In the immutable candidate release run:

```sh
cd tools/qqbot
npm ci --ignore-scripts --omit=dev --no-audit --no-fund
npm test
```

`@tencent-connect/qqbot-nodejs` is pinned to 1.0.5 in the lockfile. TLS verification
is mandatory. Keep credentials outside the release, mode 0600, readable only by
the service account (and root). The JSON keys are exactly `appId`, `appSecret` and
`groups`. `groups` is an array of **raw official group OpenIDs** permitted to send.
Never put credentials in Git, command arguments, logs or the model prompt.

Use `tools/qqbot/check_connection.py` for a bounded READY check with an empty
`groups` list. `discover_group.py` also requires an empty send list and records
only group-event metadata. Neither sends QQ messages nor calls a model. An
observed group is not automatically authorized: bind its OpenID only after the
operator confirms the intended group. A numeric group label cannot derive an
OpenID.

## Application configuration

The existing direct and session configuration files are private. Set:

* `transport`: `qqbot`
* `qqbot_app_id`: the numeric application ID as a string
* `qqbot_credentials_file`: the absolute private JSON path
* `self_member_id`: `qqbot:<app-id>:bot:<app-id>` in both configurations
* `group_roster`: only authorized `qqbot:<app-id>:group:<group-openid>` entries
* session `allowed_group_ids`: the same authorized group scope
* `private_contacts` and `allowed_private_peer_ids`: empty

The credential `groups` set must exactly match entries with `send=true`. Keep
`observe=true` on each sending entry. Use separate inbox/outbox, evidence, image
cache and attention paths for the new transport. Session evidence storage may be
retained or separately configured; existing historical rows must not be rewritten.
The obsolete OneBot token path remains a required configuration field for schema
compatibility but this transport does not read it or open its WebSocket.

Official group, member and bot IDs are application scoped. They are never aliases
for numeric QQ accounts, including the bot administrator. Old numeric-group memory
does not become visible to an official group merely because its human label is the
same. Migrating identities or audiences requires a separate evidence-backed plan.

Runtime and Cognitive read the same session/config bindings. Before maintenance
inspect systemd `Requires`, `PartOf` and `After`: stopping Cognitive also stops
Runtime and its dependents in the standard deployment. Back up configurations and
durable state, stop through the existing lifecycle, switch only to a verified
candidate, and allow the full strict journal recovery. Never restore an old
database over new messages. Verify all services, identity hash, model route and
transport-specific health before marking recovery complete.

## Delivery and revocation

Only replies to authenticated inbound group messages are enabled. C2C/private
delivery, history replay and unsolicited pushes are not implemented. No legacy
pending message is resent during migration. Each segment reserves a durable send
slot and uses the trigger's original opaque message ID plus `msg_seq`. A successful
official response is retained as an **API acknowledgement**, not an independent
history read or proof that a user read the message. Unknown/timeout results retain
the existing no-blind-retry rule.

Platform removal or message-rejection events persist a deny record in
`qqbot_revocations` and cancel the current transport epoch. Restarts, re-addition
and message-receive events do not clear that record. Reauthorization requires an
explicit operator decision and an audited maintenance change; there is no automatic
re-enable endpoint. Configuration revocation is checked again before collection,
model admission and dispatch. The gateway child is bounded and fails closed on
invalid packets, queue overflow or disconnection.

An actual accepted event, model terminal and per-segment API acknowledgement are
needed to claim a natural reply succeeded. Unit tests and gateway checks are
separate evidence. Transport rollout does not enable affect v2 or autonomous
contact, and does not satisfy their independent semantic/latency/shadow gates.

Official SDK reference: <https://github.com/tencent-connect/qqbot-nodejs>.
