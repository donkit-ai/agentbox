/**
 * AgentBox TypeScript SDK.
 *
 *   import { AgentBox } from "@agentbox/sdk";
 *   const mail = new AgentBox({ apiKey: "ab_live_...", baseUrl: "https://api.agentbox.ru" });
 *   const inbox = await mail.inboxes.create({ username: "procurement-agent" });
 *   await mail.messages.send(inbox.id, { to: ["sales@supplier.ru"], subject: "RFQ", text: "..." });
 *   const reply = await mail.messages.waitFor(inbox.id, { timeoutMs: 300_000 });
 */

export type Address = string | { email: string; name?: string };
export type Json = Record<string, any>;
export interface Page<T> { data: T[]; next_cursor: string | null }
export interface Inbox { id: string; email: string; username: string; status: string; provider_mode: string;
  display_name: string | null; metadata: Json; created_at: string; updated_at: string; expires_at: string | null }
export interface SendResult { id: string; thread_id: string; status: string; created_at: string }
export interface Message extends Json { id: string; inbox_id: string; thread_id: string; direction: string;
  status: string; subject: string; text: string | null; html: string | null; attachments: Json[]; created_at: string }
export interface ReplyWait { message_id: string; status: "pending" | "matched" | "expired";
  reply_message_id: string | null; expires_at: string }

export class AgentBoxError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public details: Json = {},
  ) {
    super(`${code}: ${message}`);
    this.name = "AgentBoxError";
  }
}

export interface AgentBoxOptions {
  apiKey: string;
  baseUrl?: string;
  fetch?: typeof fetch;
  maxRetries?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
}

const RETRY = new Set([429, 502, 503, 504]);

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(signal.reason); return; }
    const abort = () => { clearTimeout(timer); reject(signal?.reason); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

function addresses(items?: Address | Address[]): Json[] {
  if (!items) return [];
  const list = Array.isArray(items) ? items : [items];
  return list.map((a) => (typeof a === "string" ? { email: a } : a));
}

function clean(obj: Json): Json {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined && v !== null));
}

export class AgentBox {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;
  private readonly maxRetries: number;
  private readonly timeoutMs: number;
  private readonly signal?: AbortSignal;

  readonly inboxes = new Inboxes(this);
  readonly connections = new Connections(this);
  readonly messages = new Messages(this);
  readonly threads = new Threads(this);
  readonly attachments = new Attachments(this);
  readonly webhooks = new Webhooks(this);
  readonly events = new Events(this);
  readonly domains = new Domains(this);
  readonly policy = new PolicyResource(this);
  readonly suppressions = new Suppressions(this);
  readonly providerAccounts = new ProviderAccounts(this);
  readonly routingRules = new RoutingRules(this);
  readonly analytics = new Analytics(this);
  readonly usage = new Usage(this);
  readonly apiKeys = new ApiKeys(this);

  constructor(opts: AgentBoxOptions) {
    this.apiKey = opts.apiKey;
    this.baseUrl = (opts.baseUrl ?? "http://localhost:8000").replace(/\/+$/, "");
    this.fetchImpl = opts.fetch ?? fetch;
    this.maxRetries = opts.maxRetries ?? 3;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.signal = opts.signal;
  }

  me(): Promise<Json> {
    return this.request("GET", "/v1/me");
  }

  async request(
    method: string,
    path: string,
    opts: { params?: Json; json?: unknown; idempotencyKey?: string; timeoutMs?: number; signal?: AbortSignal } = {},
  ): Promise<any> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(clean(opts.params ?? {}))) url.searchParams.set(k, String(v));
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      "User-Agent": "agentbox-sdk-ts/0.2",
    };
    if (opts.json !== undefined) headers["Content-Type"] = "application/json";
    method = method.toUpperCase();
    const safePost = method === "POST" && (path === "/v1/inboxes" || path === "/v1/connections" || /\/(messages|reply|forward)$/.test(path));
    const operationId = opts.idempotencyKey ?? (safePost ? globalThis.crypto.randomUUID() : undefined);
    if (operationId) headers["Idempotency-Key"] = operationId;
    const retryable = method === "GET" || !!operationId;
    const signal = opts.signal ?? this.signal;
    for (let attempt = 1; ; attempt++) {
      if (signal?.aborted) throw signal.reason;
      const controller = new AbortController();
      const onAbort = () => controller.abort(signal?.reason);
      signal?.addEventListener("abort", onAbort, { once: true });
      const timer = setTimeout(() => controller.abort(new Error("AgentBox request timed out")), opts.timeoutMs ?? this.timeoutMs);
      let resp: Response;
      let text: string;
      try {
        resp = await this.fetchImpl(url, { method, headers, signal: controller.signal,
          body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined });
        text = await resp.text();
      } catch (error) {
        if (signal?.aborted || !retryable || attempt > this.maxRetries) throw error;
        await delay(500 * attempt, signal);
        continue;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
      }
      if (RETRY.has(resp.status) && retryable && attempt <= this.maxRetries) {
        const wait = Number(resp.headers.get("Retry-After") ?? 0) || 0.5 * attempt;
        await delay(Math.min(wait, 10) * 1000, signal);
        continue;
      }
      if (resp.status >= 400) {
        let err: Json | undefined;
        try {
          err = JSON.parse(text).error;
        } catch {
          throw new AgentBoxError(resp.status, "http_error", text.slice(0, 500));
        }
        throw new AgentBoxError(resp.status, err?.code ?? "error", err?.message ?? "", err?.request_id, err?.details);
      }
      if (resp.status === 204) return null;
      return text ? JSON.parse(text) : null;
    }
  }
}

class Resource {
  constructor(protected readonly c: AgentBox) {}

  async *paginate<T>(load: (cursor?: string) => Promise<Page<T>>): AsyncGenerator<T> {
    let cursor: string | undefined;
    do {
      const page = await load(cursor);
      yield* page.data;
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
  }
}

export class Inboxes extends Resource {
  create(body: { username?: string; domain?: string; display_name?: string; metadata?: Json; ttl?: string } = {},
         idempotencyKey?: string): Promise<Inbox> {
    return this.c.request("POST", "/v1/inboxes", { json: clean(body), idempotencyKey });
  }
  list(params: { status?: string; domain?: string; limit?: number; cursor?: string; metadata?: Json } = {}): Promise<Page<Inbox>> {
    const { metadata, ...rest } = params;
    const p: Json = { ...rest };
    for (const [k, v] of Object.entries(metadata ?? {})) p[`metadata.${k}`] = v;
    return this.c.request("GET", "/v1/inboxes", { params: p });
  }
  get(id: string): Promise<Inbox> { return this.c.request("GET", `/v1/inboxes/${id}`); }
  iter(params: { status?: string; domain?: string; limit?: number } = {}): AsyncGenerator<Inbox> {
    return this.paginate(cursor => this.list({ ...params, cursor }));
  }
  disable(id: string): Promise<Json> { return this.c.request("POST", `/v1/inboxes/${id}/disable`); }
  enable(id: string): Promise<Json> { return this.c.request("POST", `/v1/inboxes/${id}/enable`); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/inboxes/${id}`); }
  getPolicy(id: string): Promise<Json> { return this.c.request("GET", `/v1/inboxes/${id}/policy`); }
  setPolicy(id: string, config: Json): Promise<Json> { return this.c.request("PUT", `/v1/inboxes/${id}/policy`, { json: config }); }
}

export interface SendOptions {
  to: Address | Address[]; cc?: Address | Address[]; bcc?: Address | Address[]; reply_to?: Address | Address[];
  subject?: string; text?: string; html?: string; attachment_ids?: string[]; headers?: Record<string, string>; metadata?: Json;
}

/** Existing mailboxes (Gmail, Yandex 360, Microsoft 365, any IMAP) exposed as inboxes. */
export class Connections extends Resource {
  createLink(provider = "gmail"): Promise<Json> { return this.c.request("POST", "/v1/connection-links", { json: { provider } }); }
  linkStatus(id: string): Promise<Json> { return this.c.request("GET", `/v1/connection-links/${id}`); }
  async presets(): Promise<Json[]> { return (await this.c.request("GET", "/v1/connections/presets") as any).data; }
  create(body: { provider: string; address: string; password: string; username?: string; imap_host?: string; imap_port?: number;
                 smtp_host?: string; smtp_port?: number; smtp_ssl?: boolean; display_name?: string; metadata?: Json },
         idempotencyKey?: string): Promise<Json> {
    return this.c.request("POST", "/v1/connections", { json: clean(body), idempotencyKey });
  }
  async list(): Promise<Json[]> { return (await this.c.request("GET", "/v1/connections") as any).data; }
  get(id: string): Promise<Json> { return this.c.request("GET", `/v1/connections/${id}`); }
  sync(id: string): Promise<Json> { return this.c.request("POST", `/v1/connections/${id}/sync`); }
  pause(id: string): Promise<Json> { return this.c.request("POST", `/v1/connections/${id}/pause`); }
  resume(id: string): Promise<Json> { return this.c.request("POST", `/v1/connections/${id}/resume`); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/connections/${id}`); }
}

export class Messages extends Resource {
  startReplyWait(id: string, expiresIn = 86400): Promise<ReplyWait> {
    return this.c.request("POST", `/v1/messages/${id}/reply-wait`, { json: { expires_in: expiresIn } });
  }
  replyWait(id: string): Promise<ReplyWait> { return this.c.request("GET", `/v1/messages/${id}/reply-wait`); }
  send(inboxId: string, o: SendOptions, idempotencyKey?: string): Promise<SendResult> {
    const body = clean({ ...o, to: addresses(o.to), cc: addresses(o.cc), bcc: addresses(o.bcc), reply_to: addresses(o.reply_to) });
    return this.c.request("POST", `/v1/inboxes/${inboxId}/messages`, { json: body, idempotencyKey });
  }
  list(inboxId: string, params: { direction?: string; status?: string; thread_id?: string; from?: string; to?: string;
       since?: string; until?: string; wait?: number; limit?: number; cursor?: string } = {}): Promise<Page<Message>> {
    return this.c.request("GET", `/v1/inboxes/${inboxId}/messages`, { params, timeoutMs: Math.max(30_000, ((params.wait ?? 0) + 10) * 1000) });
  }
  iter(inboxId: string, params: { direction?: string; thread_id?: string; since?: string; limit?: number } = {}): AsyncGenerator<Message> {
    return this.paginate(cursor => this.list(inboxId, { ...params, cursor }));
  }
  /** Long-poll until an inbound message exists; resolves to the newest message or null on timeout. */
  async waitFor(inboxId: string, o: { direction?: string; since?: string; thread_id?: string; timeoutMs?: number; signal?: AbortSignal } = {}): Promise<Message | null> {
    const deadline = Date.now() + (o.timeoutMs ?? 300_000);
    const since = o.since ?? new Date().toISOString();
    while (Date.now() < deadline) {
      const wait = Math.max(1, Math.min(60, Math.floor((deadline - Date.now()) / 1000)));
      const page: Page<Message> = await this.c.request("GET", `/v1/inboxes/${inboxId}/messages`, {
        params: { direction: o.direction ?? "inbound", since, thread_id: o.thread_id, wait, limit: 1 },
        timeoutMs: (wait + 10) * 1000, signal: o.signal });
      if (page.data.length) return page.data[0];
    }
    return null;
  }
  get(id: string, includeHeaders = false): Promise<Json> {
    return this.c.request("GET", `/v1/messages/${id}`, { params: includeHeaders ? { include: "headers" } : {} });
  }
  reply(id: string, o: { text?: string; html?: string; reply_all?: boolean; to?: Address[]; cc?: Address[]; bcc?: Address[]; attachment_ids?: string[] }, idempotencyKey?: string): Promise<Json> {
    return this.c.request("POST", `/v1/messages/${id}/reply`, { json: clean({ ...o, to: addresses(o.to), cc: addresses(o.cc), bcc: addresses(o.bcc) }), idempotencyKey });
  }
  forward(id: string, o: { to: Address[]; text?: string; html?: string; cc?: Address[]; bcc?: Address[]; include_attachments?: boolean }, idempotencyKey?: string): Promise<Json> {
    return this.c.request("POST", `/v1/messages/${id}/forward`, { json: clean({ ...o, to: addresses(o.to), cc: addresses(o.cc), bcc: addresses(o.bcc) }), idempotencyKey });
  }
  pendingApprovals(params: { limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/approvals", { params }); }
  approve(id: string): Promise<Json> { return this.c.request("POST", `/v1/messages/${id}/approve`); }
  reject(id: string, reason?: string): Promise<Json> { return this.c.request("POST", `/v1/messages/${id}/reject`, { json: clean({ reason }) }); }
}

export class Threads extends Resource {
  list(inboxId: string, params: { limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", `/v1/inboxes/${inboxId}/threads`, { params }); }
  get(id: string): Promise<Json> { return this.c.request("GET", `/v1/threads/${id}`); }
}

export class Attachments extends Resource {
  /** Create a pre-signed upload, PUT the bytes, return the attachment id. */
  async upload(filename: string, content: Uint8Array | ArrayBuffer | Blob, contentType = "application/octet-stream"): Promise<string> {
    const size = content instanceof Blob ? content.size : (content as ArrayBuffer).byteLength ?? (content as Uint8Array).length;
    const up = await this.c.request("POST", "/v1/attachments/uploads", { json: { filename, content_type: contentType, size_bytes: size } });
    const r = await fetch(up.upload_url, { method: "PUT", headers: up.headers ?? { "Content-Type": contentType }, body: content as any });
    if (!r.ok) throw new AgentBoxError(r.status, "upload_failed", await r.text());
    return up.attachment_id;
  }
  get(id: string): Promise<Json> { return this.c.request("GET", `/v1/attachments/${id}`); }
  list(messageId: string): Promise<Json> { return this.c.request("GET", `/v1/messages/${messageId}/attachments`); }
  downloadUrl(id: string): Promise<Json> { return this.c.request("GET", `/v1/attachments/${id}/download`); }
  async download(id: string): Promise<Uint8Array> {
    const { url } = await this.downloadUrl(id);
    const r = await fetch(url);
    if (!r.ok) throw new AgentBoxError(r.status, "download_failed", await r.text());
    return new Uint8Array(await r.arrayBuffer());
  }
}

export class Webhooks extends Resource {
  create(body: { url: string; event_types?: string[]; inbox_id?: string; description?: string }): Promise<Json> { return this.c.request("POST", "/v1/webhooks", { json: clean(body) }); }
  list(params: { limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/webhooks", { params }); }
  get(id: string): Promise<Json> { return this.c.request("GET", `/v1/webhooks/${id}`); }
  update(id: string, fields: Json): Promise<Json> { return this.c.request("PATCH", `/v1/webhooks/${id}`, { json: clean(fields) }); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/webhooks/${id}`); }
  deliveries(id: string, params: { limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", `/v1/webhooks/${id}/deliveries`, { params }); }
  retry(id: string, deliveryId: string): Promise<Json> { return this.c.request("POST", `/v1/webhooks/${id}/deliveries/${deliveryId}/retry`); }
}

export class Events extends Resource {
  claim(consumer: string, options: { types?: string[]; limit?: number; lease_seconds?: number; since?: string } = {}): Promise<Json> {
    return this.c.request("POST", `/v1/consumers/${encodeURIComponent(consumer)}/claim`, { json: options });
  }
  ack(consumer: string, eventId: string, leaseToken: string): Promise<Json> {
    return this.c.request("POST", `/v1/consumers/${encodeURIComponent(consumer)}/events/${eventId}/ack`, { json: { lease_token: leaseToken } });
  }
  list(params: { type?: string; resource_id?: string; since?: string; until?: string; limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/events", { params }); }
}

export class Domains extends Resource {
  create(domain: string): Promise<Json> { return this.c.request("POST", "/v1/domains", { json: { domain } }); }
  list(params: { limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/domains", { params }); }
  get(id: string): Promise<Json> { return this.c.request("GET", `/v1/domains/${id}`); }
  verify(id: string): Promise<Json> { return this.c.request("POST", `/v1/domains/${id}/verify`); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/domains/${id}`); }
}

export class PolicyResource extends Resource {
  get(): Promise<Json> { return this.c.request("GET", "/v1/policy"); }
  set(config: Json): Promise<Json> { return this.c.request("PUT", "/v1/policy", { json: config }); }
  delete(): Promise<null> { return this.c.request("DELETE", "/v1/policy"); }
}

export class Suppressions extends Resource {
  list(params: { email?: string; reason?: string; limit?: number; cursor?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/suppressions", { params }); }
  create(body: { email: string; reason?: string; note?: string; expires_at?: string }): Promise<Json> { return this.c.request("POST", "/v1/suppressions", { json: clean(body) }); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/suppressions/${id}`); }
}

export class ProviderAccounts extends Resource {
  list(): Promise<Json> { return this.c.request("GET", "/v1/provider-accounts"); }
  create(body: { provider: string; name: string; config: Json }): Promise<Json> { return this.c.request("POST", "/v1/provider-accounts", { json: body }); }
  test(id: string): Promise<Json> { return this.c.request("POST", `/v1/provider-accounts/${id}/test`); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/provider-accounts/${id}`); }
}

export class RoutingRules extends Resource {
  list(): Promise<Json> { return this.c.request("GET", "/v1/routing-rules"); }
  create(body: { provider_account_id: string; priority?: number; match?: Json; description?: string }): Promise<Json> { return this.c.request("POST", "/v1/routing-rules", { json: clean(body) }); }
  delete(id: string): Promise<null> { return this.c.request("DELETE", `/v1/routing-rules/${id}`); }
}

export class Analytics extends Resource {
  delivery(params: { since?: string; until?: string; group_by?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/analytics/delivery", { params }); }
}

export class Usage extends Resource {
  get(params: { since?: string; until?: string } = {}): Promise<Json> { return this.c.request("GET", "/v1/usage", { params }); }
}

export class ApiKeys extends Resource {
  list(): Promise<Json> { return this.c.request("GET", "/v1/api-keys"); }
  create(body: { name: string; scopes?: string[]; environment?: string; permission_profile?: "readonly" | "review" | "autonomous"; inbox_ids?: string[]; allowed_recipients?: string[] }): Promise<Json> { return this.c.request("POST", "/v1/api-keys", { json: clean(body) }); }
  revoke(id: string): Promise<null> { return this.c.request("DELETE", `/v1/api-keys/${id}`); }
}

/** Verify `AgentBox-Signature: t=<unix>,v1=<hex>` against the raw body (Node >= 18). */
export async function verifyWebhookSignature(secret: string, header: string, body: string | Uint8Array,
                                             toleranceSeconds = 300, now?: number): Promise<boolean> {
  const parts = Object.fromEntries(header.split(",").filter((p) => p.includes("=")).map((p) => p.split(/=(.*)/s).slice(0, 2)));
  const ts = Number(parts.t);
  if (!Number.isFinite(ts)) return false;
  const nowSec = now ?? Math.floor(Date.now() / 1000);
  if (Math.abs(nowSec - ts) > toleranceSeconds) return false;
  const enc = new TextEncoder();
  const bodyBytes = typeof body === "string" ? enc.encode(body) : body;
  const payload = new Uint8Array(enc.encode(`${ts}.`).length + bodyBytes.length);
  payload.set(enc.encode(`${ts}.`), 0);
  payload.set(bodyBytes, enc.encode(`${ts}.`).length);
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, payload));
  const expected = Array.from(sig).map((b) => b.toString(16).padStart(2, "0")).join("");
  return expected === parts.v1;
}
