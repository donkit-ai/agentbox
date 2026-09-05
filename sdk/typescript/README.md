# @agentbox/sdk

Typed TypeScript client for AgentBox. Node 20+ and modern browsers; Apache-2.0.
After publication: `npm install @agentbox/sdk`. Before publication, build from this checkout with `npm ci && npm run build`.

```ts
import { AgentBox } from "@agentbox/sdk";

const abort = new AbortController();
const mail = new AgentBox({ apiKey: process.env.AGENTBOX_API_KEY!, baseUrl: process.env.AGENTBOX_API_URL,
                           timeoutMs: 30_000, signal: abort.signal });
const sent = await mail.messages.send(process.env.AGENTBOX_INBOX_ID!,
  { to: "supplier@example.com", subject: "Quote", text: "Please quote 100 units." }, "persisted-task-id");
console.log(await mail.messages.startReplyWait(sent.id));
for await (const message of mail.messages.iter(process.env.AGENTBOX_INBOX_ID!)) console.log(message.id);
```

Retrying a send uses the same idempotency key. Supply your own stable key across restarts; do not blindly retry
older than server retention (default 24 hours). `queued`/`provider_accepted` are not proof of delivery.
`connections.createLink()` keeps mailbox passwords out of agent context. Use inbox-scoped permission presets.
See [setup and runnable examples](https://github.com/Slimper/agentbox/blob/main/docs/agents.md).
