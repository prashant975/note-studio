import { del, list } from "@vercel/blob";
import { getVercelOidcToken } from "@vercel/oidc";
import { timingSafeEqual } from "node:crypto";

export const CLEANUP_PAGE_LIMIT = 100;
export const CLEANUP_MAX_PAGES = 10;
export const CLEANUP_MAX_OBJECTS = CLEANUP_PAGE_LIMIT * CLEANUP_MAX_PAGES;
export const CLEANUP_DELETE_BATCH_SIZE = 100;

const STORE_ID = /^store_[A-Za-z0-9]{8,64}$/;
const INPUT_PATH = /^shortnotes\/v1\/inputs\/[0-9a-f]{32}\/[0-9a-f]{32}\.pdf$/;
const OUTPUT_PATH = /^shortnotes\/v1\/outputs\/[0-9a-f]{32}\/[0-9a-f]{32}\.pdf$/;
const JOB_PATH = /^shortnotes\/v1\/jobs\/[0-9a-f]{32}\/[0-9a-f]{32}\.json$/;
const COST_PATH = /^shortnotes\/v1\/costs\/[0-9a-f]{32}\/[0-9a-f]{32}\.json$/;
const INPUT_TTL_MS = 2 * 60 * 60 * 1_000;
const OUTPUT_TTL_MS = 6 * 60 * 60 * 1_000;
const JOB_TTL_MS = 24 * 60 * 60 * 1_000;
const COST_TTL_MS = JOB_TTL_MS;
const CLEANUP_NAMESPACES = Object.freeze([
  Object.freeze({ prefix: "shortnotes/v1/inputs/", pathname: INPUT_PATH, ttlMs: INPUT_TTL_MS }),
  Object.freeze({ prefix: "shortnotes/v1/outputs/", pathname: OUTPUT_PATH, ttlMs: OUTPUT_TTL_MS }),
  Object.freeze({ prefix: "shortnotes/v1/jobs/", pathname: JOB_PATH, ttlMs: JOB_TTL_MS }),
  Object.freeze({ prefix: "shortnotes/v1/costs/", pathname: COST_PATH, ttlMs: COST_TTL_MS }),
]);
export const CLEANUP_TOTAL_MAX_PAGES = CLEANUP_MAX_PAGES * CLEANUP_NAMESPACES.length;
export const CLEANUP_TOTAL_MAX_OBJECTS = CLEANUP_MAX_OBJECTS * CLEANUP_NAMESPACES.length;

const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
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

function constantTextEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string") return false;
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

function configured(env) {
  const storeId = typeof env.BLOB_STORE_ID === "string" ? env.BLOB_STORE_ID.trim() : "";
  const cronSecret = typeof env.CRON_SECRET === "string" ? env.CRON_SECRET : "";
  if (!STORE_ID.test(storeId) || !cronSecret || cronSecret.length > 4_096) {
    throw new Error("Cleanup is unavailable");
  }
  const key = storeId.slice("store_".length).toLowerCase();
  return {
    storeId,
    cronSecret,
    storeHost: `${key}.private.blob.vercel-storage.com`,
  };
}

function validListResult(value, requestedLimit) {
  return Boolean(
    value &&
    typeof value === "object" &&
    Array.isArray(value.blobs) &&
    value.blobs.length <= requestedLimit &&
    typeof value.hasMore === "boolean"
  );
}

function validateListedBlob(blob, storeHost, namespace) {
  if (
    !blob ||
    typeof blob !== "object" ||
    typeof blob.pathname !== "string" ||
    !blob.pathname.startsWith(namespace.prefix) ||
    !namespace.pathname.test(blob.pathname) ||
    typeof blob.url !== "string" ||
    blob.url !== `https://${storeHost}/${blob.pathname}` ||
    !(blob.uploadedAt instanceof Date) ||
    !Number.isFinite(blob.uploadedAt.getTime()) ||
    !Number.isSafeInteger(blob.size) ||
    blob.size < 0
  ) {
    return null;
  }
  return { pathname: blob.pathname, uploadedAt: blob.uploadedAt.getTime() };
}

export function createCleanupHandler({
  env = process.env,
  nowMs = Date.now,
  getOidcTokenImpl = getVercelOidcToken,
  listImpl = list,
  delImpl = del,
} = {}) {
  return {
    async fetch(request) {
      try {
        const url = new URL(request.url);
        if (url.pathname !== "/api/cleanup" || url.search || url.hash) {
          throw new PublicError(404, "Not found");
        }
        if (request.method !== "GET") throw new PublicError(405, "Method not allowed");
        const config = configured(env);
        if (!constantTextEqual(request.headers.get("authorization"), `Bearer ${config.cronSecret}`)) {
          throw new PublicError(401, "Unauthorized");
        }
        const timestamp = nowMs();
        if (!Number.isSafeInteger(timestamp) || timestamp < 0) throw new Error("Clock unavailable");
        const oidcToken = await getOidcTokenImpl();
        if (typeof oidcToken !== "string" || !oidcToken.trim()) {
          throw new Error("Blob authentication unavailable");
        }
        const exactOidcToken = oidcToken.trim();

        const eligible = new Set();
        let scanned = 0;
        let truncated = false;

        for (const namespace of CLEANUP_NAMESPACES) {
          const seenCursors = new Set();
          let cursor;
          let namespaceScanned = 0;

          for (let page = 0; page < CLEANUP_MAX_PAGES; page += 1) {
            const remaining = CLEANUP_MAX_OBJECTS - namespaceScanned;
            if (remaining <= 0) {
              truncated = true;
              break;
            }
            const limit = Math.min(CLEANUP_PAGE_LIMIT, remaining);
            const options = {
              prefix: namespace.prefix,
              limit,
              mode: "expanded",
              ...(cursor === undefined ? {} : { cursor }),
              storeId: config.storeId,
              oidcToken: exactOidcToken,
            };
            const result = await listImpl(options);
            if (!validListResult(result, limit)) {
              truncated = true;
              break;
            }

            let anomalous = false;
            for (const candidate of result.blobs) {
              if (namespaceScanned >= CLEANUP_MAX_OBJECTS) {
                truncated = true;
                anomalous = true;
                break;
              }
              const blob = validateListedBlob(candidate, config.storeHost, namespace);
              if (!blob) {
                truncated = true;
                anomalous = true;
                break;
              }
              namespaceScanned += 1;
              scanned += 1;
              if (blob.uploadedAt <= timestamp - namespace.ttlMs) {
                eligible.add(blob.pathname);
              }
            }
            if (anomalous || !result.hasMore) break;

            const nextCursor = result.cursor;
            if (
              typeof nextCursor !== "string" ||
              !nextCursor ||
              nextCursor.length > 2_048 ||
              seenCursors.has(nextCursor)
            ) {
              truncated = true;
              break;
            }
            seenCursors.add(nextCursor);
            cursor = nextCursor;
            if (page + 1 >= CLEANUP_MAX_PAGES) truncated = true;
          }
        }

        const paths = [...eligible];
        let deleted = 0;
        for (let offset = 0; offset < paths.length; offset += CLEANUP_DELETE_BATCH_SIZE) {
          const batch = paths.slice(offset, offset + CLEANUP_DELETE_BATCH_SIZE);
          await delImpl(batch, { storeId: config.storeId, oidcToken: exactOidcToken });
          deleted += batch.length;
        }
        return jsonResponse({ ok: true, scanned, deleted, truncated });
      } catch (error) {
        if (error instanceof PublicError) {
          return jsonResponse({ error: error.publicMessage }, error.status);
        }
        return jsonResponse({ error: "Service temporarily unavailable" }, 503);
      }
    },
  };
}

export default createCleanupHandler();
