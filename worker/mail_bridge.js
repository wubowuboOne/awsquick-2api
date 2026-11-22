/**
 * Quick 注册用邮件桥
 *
 * 职责：
 *   1. 接收 *@your-domain.com 的全部邮件（Email Routing catch-all 指过来）
 *   2. 解析出主题、发件人、正文，并抽出 6 位验证码
 *   3. 存入 KV
 *   4. 暴露只读 HTTP 接口，供注册机取码
 *
 * 绑定：
 *   KV  MAILBOX
 * 环境变量：
 *   API_TOKEN  取信接口的口令（必填）
 *   KEEP       保留最近多少封（默认 200）
 *
 * 接口：
 *   GET /latest?to=<address>&token=<API_TOKEN>   最新一封（可带 to 过滤）
 *   GET /list?token=<API_TOKEN>&limit=20         最近若干封
 *   GET /                                    健康检查
 */

const MAX_BODY = 64 * 1024;

function b64ToBytes(b64) {
  const bin = atob(b64.replace(/\s+/g, ""));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function decodeQP(s) {
  return s
    .replace(/=\r?\n/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
}

/** 从原始 MIME 里取正文：优先 text/plain，其次剥掉 HTML 标签 */
function extractBody(raw) {
  const sep = raw.indexOf("\r\n\r\n") >= 0 ? raw.indexOf("\r\n\r\n") : raw.indexOf("\n\n");
  const head = sep >= 0 ? raw.slice(0, sep) : "";
  let body = sep >= 0 ? raw.slice(sep + (raw[sep] === "\r" ? 4 : 2)) : raw;

  const isQP = /content-transfer-encoding:\s*quoted-printable/i.test(head);
  const isB64 = /content-transfer-encoding:\s*base64/i.test(head);

  if (isB64) {
    try {
      const cleaned = body.replace(/[^A-Za-z0-9+/=]/g, "");
      body = new TextDecoder("utf-8").decode(b64ToBytes(cleaned));
    } catch (e) {
      /* 解不出就保留原文 */
    }
  } else if (isQP) {
    body = decodeQP(body);
  }

  // multipart：尽量取第一部分
  if (/content-type:\s*multipart/i.test(head)) {
    const m = body.match(/content-type:\s*text\/plain[\s\S]*?\r?\n\r?\n([\s\S]*?)(?:\r?\n--|\s*$)/i);
    if (m) body = m[1];
  }

  if (/<html|<!doctype html/i.test(body)) {
    body = body
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&");
  }

  return body.replace(/\r/g, "").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim().slice(0, MAX_BODY);
}

/** AWS 的验证码是 6 位数字，正文里通常单独成行 */
function extractCode(text) {
  const near = text.match(/(?:code|verification|验证码)[^0-9]{0,40}(\d{6})/i);
  if (near) return near[1];
  const any = text.match(/(?:^|\D)(\d{6})(?:\D|$)/);
  return any ? any[1] : null;
}

async function save(env, rec) {
  const key = `msg:${String(rec.received_at).padStart(15, "0")}:${Math.random().toString(36).slice(2, 8)}`;
  await env.MAILBOX.put(key, JSON.stringify(rec), { expirationTtl: 60 * 60 * 24 * 7 });
  const idxRaw = await env.MAILBOX.get("index");
  const idx = idxRaw ? JSON.parse(idxRaw) : [];
  idx.unshift(key);
  const keep = parseInt(env.KEEP || "200", 10);
  const dropped = idx.splice(keep);
  for (const k of dropped) await env.MAILBOX.delete(k);
  await env.MAILBOX.put("index", JSON.stringify(idx));
  return key;
}

async function listMessages(env, limit) {
  const idxRaw = await env.MAILBOX.get("index");
  const idx = idxRaw ? JSON.parse(idxRaw) : [];
  const out = [];
  for (const k of idx.slice(0, limit)) {
    const v = await env.MAILBOX.get(k);
    if (v) out.push(JSON.parse(v));
  }
  return out;
}

export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).text();
    const body = extractBody(raw);
    const rec = {
      received_at: Date.now(),
      to: message.to,
      from: message.from,
      subject: message.headers.get("subject") || "",
      code: extractCode(body),
      body: body.slice(0, 4000),
    };
    await save(env, rec);
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      return new Response(JSON.stringify({ ok: true, service: "quick-mail-bridge" }), {
        headers: { "content-type": "application/json" },
      });
    }
    const token = url.searchParams.get("token") || "";
    if (!env.API_TOKEN || token !== env.API_TOKEN) {
      return new Response(JSON.stringify({ ok: false, error: "unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }

    if (url.pathname === "/latest") {
      const to = (url.searchParams.get("to") || "").toLowerCase();
      const since = parseInt(url.searchParams.get("since") || "0", 10);
      const msgs = await listMessages(env, 50);
      const hit = msgs.find(
        (m) => (!to || m.to.toLowerCase() === to) && m.received_at >= since && m.code
      );
      return new Response(JSON.stringify({ ok: true, message: hit || null }), {
        headers: { "content-type": "application/json" },
      });
    }

    if (url.pathname === "/list") {
      const limit = Math.min(parseInt(url.searchParams.get("limit") || "20", 10), 100);
      return new Response(JSON.stringify({ ok: true, messages: await listMessages(env, limit) }), {
        headers: { "content-type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ ok: false, error: "not found" }), {
      status: 404,
      headers: { "content-type": "application/json" },
    });
  },
};
