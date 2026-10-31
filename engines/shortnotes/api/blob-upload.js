import { issueSignedToken } from "@vercel/blob";
import { handleUploadPresigned } from "@vercel/blob/client";
import { getVercelOidcToken } from "@vercel/oidc";

import { authenticateBrowserRequest } from "../web/session.js";

export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

const MAX_HANDSHAKE_BYTES = 8 * 1024;
const UPLOAD_TOKEN_TTL_MS = 15 * 60 * 1_000;
const JOB_ID = /^[0-9a-f]{32}$/;
const STORE_ID = /^store_[A-Za-z0-9]{8,64}$/;
const EVENT_TYPE = "blob.generate-presigned-url";

const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow",
};

class PublicError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
    this.publicMessage = message;
  }
}

function jsonResponse(value, status = 200) {
  return Response.json(value, {
    status,
    headers: { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8" },
  });
}

function exactKeys(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

async function readBoundedJson(request, limit) {
  const mediaType = (request.headers.get("content-type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (mediaType !== "application/json") throw new PublicError(415, "JSON content type required");
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^[0-9]+$/.test(declared) || Number(declared) > limit)) {
    throw new PublicError(413, "Request too large");
  }
  if (!request.body) throw new PublicError(400, "Invalid request");

  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!(value instanceof Uint8Array)) throw new PublicError(400, "Invalid request");
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel().catch(() => {});
      throw new PublicError(413, "Request too large");
    }
    chunks.push(value);
  }
  if (!total) throw new PublicError(400, "Invalid request");
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return JSON.parse(text);
  } catch {
    throw new PublicError(400, "Invalid request");
  }
}

function configured(env) {
  const secret = typeof env.APP_SESSION_SECRET === "string" ? env.APP_SESSION_SECRET : "";
  const storeId = typeof env.BLOB_STORE_ID === "string" ? env.BLOB_STORE_ID.trim() : "";
  const webhookPublicKey = typeof env.BLOB_WEBHOOK_PUBLIC_KEY === "string"
    ? env.BLOB_WEBHOOK_PUBLIC_KEY.trim()
    : "";
  if (!secret || !STORE_ID.test(storeId) || !webhookPublicKey) {
    throw new Error("Blob upload is unavailable");
  }
  return { secret, storeId, webhookPublicKey };
}

function normalizeUploadBody(value, owner) {
  if (!exactKeys(value, ["type", "payload"]) || value.type !== EVENT_TYPE) {
    throw new PublicError(400, "Invalid request");
  }
  const payload = value.payload;
  if (!exactKeys(payload, ["pathname", "multipart", "clientPayload"])) {
    throw new PublicError(400, "Invalid request");
  }
  const id = payload.clientPayload;
  const canonicalPath = `shortnotes/v1/inputs/${owner}/${id}.pdf`;
  if (
    !JOB_ID.test(id || "") ||
    payload.pathname !== canonicalPath ||
    typeof payload.multipart !== "boolean"
  ) {
    throw new PublicError(400, "Invalid request");
  }
  return {
    id,
    canonicalPath,
    body: {
      type: EVENT_TYPE,
      payload: {
        pathname: canonicalPath,
        multipart: payload.multipart,
        clientPayload: id,
      },
    },
  };
}

function safeSdkResult(result) {
  if (
    !exactKeys(result, ["type", "presignedUrlPayload"]) ||
    result.type !== EVENT_TYPE ||
    !result.presignedUrlPayload ||
    typeof result.presignedUrlPayload !== "object" ||
    Array.isArray(result.presignedUrlPayload)
  ) {
    throw new Error("Invalid Blob response");
  }
  const { delegationToken, signature, params } = result.presignedUrlPayload;
  if (
    typeof delegationToken !== "string" ||
    !delegationToken ||
    delegationToken.length > 8_192 ||
    typeof signature !== "string" ||
    !signature ||
    signature.length > 512 ||
    !params ||
    typeof params !== "object" ||
    Array.isArray(params)
  ) {
    throw new Error("Invalid Blob response");
  }
  const safeParams = {};
  for (const [key, value] of Object.entries(params)) {
    if (
      !/^[a-z0-9-]{1,80}$/.test(key) ||
      typeof value !== "string" ||
      value.length > 8_192 ||
      Object.keys(safeParams).length >= 16
    ) {
      throw new Error("Invalid Blob response");
    }
    safeParams[key] = value;
  }
  return {
    type: EVENT_TYPE,
    presignedUrlPayload: { delegationToken, signature, params: safeParams },
  };
}

export function createBlobUploadHandler({
  env = process.env,
  nowMs = Date.now,
  getOidcTokenImpl = getVercelOidcToken,
  issueSignedTokenImpl = issueSignedToken,
  handleUploadPresignedImpl = handleUploadPresigned,
} = {}) {
  return {
    async fetch(request) {
      try {
        const url = new URL(request.url);
        if (
          url.pathname !== "/api/blob-upload" ||
          (url.search !== "" && url.search !== "?action=prepare") ||
          url.hash
        ) {
          throw new PublicError(404, "Not found");
        }
        if (request.method !== "POST") throw new PublicError(405, "Method not allowed");
        const config = configured(env);
        const timestamp = nowMs();
        if (!Number.isSafeInteger(timestamp) || timestamp < 0) throw new Error("Clock unavailable");
        const authority = await authenticateBrowserRequest(request, {
          secret: config.secret,
          now: Math.floor(timestamp / 1_000),
          productionHost: "",
          requireCsrf: true,
        });
        if (!authority.ok) {
          throw new PublicError(
            authority.status,
            authority.status === 401 ? "Unauthorized" : "Request rejected",
          );
        }

        const untrusted = await readBoundedJson(request, MAX_HANDSHAKE_BYTES);
        if (url.search === "?action=prepare") {
          if (!exactKeys(untrusted, ["requestId"]) || !JOB_ID.test(untrusted.requestId || "")) {
            throw new PublicError(400, "Invalid request");
          }
          return jsonResponse({
            pathname: `shortnotes/v1/inputs/${authority.owner}/${untrusted.requestId}.pdf`,
          });
        }
        const normalized = normalizeUploadBody(untrusted, authority.owner);
        const oidcToken = await getOidcTokenImpl();
        if (typeof oidcToken !== "string" || !oidcToken.trim()) {
          throw new Error("Blob authentication unavailable");
        }
        const exactOidcToken = oidcToken.trim();
        const validUntil = timestamp + UPLOAD_TOKEN_TTL_MS;
        let issued = false;
        const result = await handleUploadPresignedImpl({
          body: normalized.body,
          request,
          webhookPublicKey: config.webhookPublicKey,
          async getSignedToken(pathname, clientPayload, multipart) {
            if (
              issued ||
              pathname !== normalized.canonicalPath ||
              clientPayload !== normalized.id ||
              multipart !== normalized.body.payload.multipart
            ) {
              throw new Error("Invalid Blob signing scope");
            }
            issued = true;
            const token = await issueSignedTokenImpl({
              pathname: normalized.canonicalPath,
              operations: ["put"],
              validUntil,
              allowedContentTypes: ["application/pdf"],
              maximumSizeInBytes: MAX_UPLOAD_BYTES,
              storeId: config.storeId,
              oidcToken: exactOidcToken,
            });
            return {
              token,
              urlOptions: {
                validUntil,
                allowedContentTypes: ["application/pdf"],
                maximumSizeInBytes: MAX_UPLOAD_BYTES,
                allowOverwrite: false,
                addRandomSuffix: false,
              },
            };
          },
        });
        if (!issued) throw new Error("Blob token was not issued");
        return jsonResponse(safeSdkResult(result));
      } catch (error) {
        if (error instanceof PublicError) {
          return jsonResponse({ error: error.publicMessage }, error.status);
        }
        return jsonResponse({ error: "Service temporarily unavailable" }, 503);
      }
    },
  };
}

export default createBlobUploadHandler();
