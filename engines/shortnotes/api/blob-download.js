import { get, issueSignedToken, presignUrl } from "@vercel/blob";
import { getVercelOidcToken } from "@vercel/oidc";

import { authenticateBrowserRequest } from "../web/session.js";

export const MAX_JOB_RECORD_BYTES = 32 * 1024;

const MAX_REQUEST_BYTES = 4 * 1024;
const DOWNLOAD_TOKEN_TTL_MS = 5 * 60 * 1_000;
const JOB_ID = /^[0-9a-f]{32}$/;
const STORE_ID = /^store_[A-Za-z0-9]{8,64}$/;
const SUBJECTS = new Set([
  "auto", "chemistry", "physics", "biology", "mathematics", "sanskrit",
  "science", "social_science", "english", "hindi", "other",
]);
const LANGUAGES = new Set(["auto", "english", "hindi", "hinglish", "sanskrit", "sanskrit_hindi"]);
// "neet" and "jee" are the pre-2026-09-30 ids of NEET-JEE, still found in stored jobs.
const CATEGORIES = new Set(["neet_jee", "foundation", "neet", "jee"]);

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

async function readBody(request, limit) {
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
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new PublicError(400, "Invalid request");
  }
}

async function streamBytes(stream, expectedSize) {
  if (!stream || typeof stream.getReader !== "function") throw new Error("Invalid Blob stream");
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!(value instanceof Uint8Array)) throw new Error("Invalid Blob stream");
    total += value.byteLength;
    if (total > MAX_JOB_RECORD_BYTES) {
      await reader.cancel().catch(() => {});
      throw new Error("Invalid Blob record");
    }
    chunks.push(value);
  }
  if (total !== expectedSize || total === 0) throw new Error("Invalid Blob record");
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function configured(env) {
  const secret = typeof env.APP_SESSION_SECRET === "string" ? env.APP_SESSION_SECRET : "";
  const storeId = typeof env.BLOB_STORE_ID === "string" ? env.BLOB_STORE_ID.trim() : "";
  if (!secret || !STORE_ID.test(storeId)) throw new Error("Blob download is unavailable");
  const key = storeId.slice("store_".length).toLowerCase();
  return {
    secret,
    storeId,
    storeHost: `${key}.private.blob.vercel-storage.com`,
  };
}

function canonicalTeacherName(value) {
  if (typeof value !== "string") return null;
  const normalized = value.trim().split(/\s+/u).filter(Boolean).join(" ");
  return normalized && normalized.length <= 120 && normalized === value ? normalized : null;
}

function safeFilename(value) {
  if (typeof value !== "string" || value.normalize("NFKC") !== value) return null;
  if (
    !value.endsWith(".pdf") ||
    Array.from(value).length > 94 ||
    /[\\/:*?"<>|\x00-\x1f\x7f]/u.test(value)
  ) {
    return null;
  }
  const base = value.slice(0, -4);
  if (
    !base ||
    Array.from(base).length > 90 ||
    /^[. -]|[. -]$/.test(base) ||
    base.trim().split(/\s+/u).filter(Boolean).join(" ") !== base
  ) {
    return null;
  }
  return value;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validateRecord(value, { owner, jobId, storeHost }) {
  if (!exactKeys(value, ["version", "manifest", "settings", "filename", "dispatch_claim_id"])) {
    throw new Error("Invalid job record");
  }
  if (value.version !== 1 || !JOB_ID.test(value.dispatch_claim_id || "")) {
    throw new Error("Invalid job record");
  }
  const manifest = value.manifest;
  if (!exactKeys(manifest, [
    "id", "owner", "source_url", "state", "created_at", "updated_at",
    "output_url", "error", "dispatch_count",
  ])) {
    throw new Error("Invalid job record");
  }
  if (manifest.id !== jobId || manifest.owner !== owner) throw new PublicError(404, "Job not found");
  const inputPath = `shortnotes/v1/inputs/${owner}/${jobId}.pdf`;
  const outputPath = `shortnotes/v1/outputs/${owner}/${jobId}.pdf`;
  if (
    manifest.source_url !== `https://${storeHost}/${inputPath}` ||
    !nonnegativeInteger(manifest.created_at) ||
    !nonnegativeInteger(manifest.updated_at) ||
    manifest.updated_at < manifest.created_at ||
    manifest.dispatch_count !== 1 ||
    typeof manifest.error !== "string" ||
    typeof manifest.output_url !== "string"
  ) {
    throw new Error("Invalid job record");
  }
  if (manifest.state !== "complete") throw new PublicError(409, "Job is not ready");
  if (manifest.output_url !== `https://${storeHost}/${outputPath}` || manifest.error !== "") {
    throw new Error("Invalid job record");
  }

  const settings = value.settings;
  const legacySettingsKeys = ["teacher_name", "instruction", "language", "subject", "watermark"];
  const currentSettingsKeys = [...legacySettingsKeys, "logo_watermark"];
  const categorySettingsKeys = [...currentSettingsKeys, "category"];
  const exactSettings = exactKeys(settings, legacySettingsKeys)
    || exactKeys(settings, currentSettingsKeys)
    || exactKeys(settings, categorySettingsKeys);
  if (
    !exactSettings ||
    canonicalTeacherName(settings.teacher_name) === null ||
    typeof settings.instruction !== "string" ||
    settings.instruction.length > 1_000 ||
    !LANGUAGES.has(settings.language) ||
    !SUBJECTS.has(settings.subject) ||
    typeof settings.watermark !== "boolean" ||
    (
      Object.prototype.hasOwnProperty.call(settings, "logo_watermark") &&
      typeof settings.logo_watermark !== "boolean"
    ) ||
    (
      Object.prototype.hasOwnProperty.call(settings, "category") &&
      !CATEGORIES.has(settings.category)
    )
  ) {
    throw new Error("Invalid job record");
  }
  const filename = safeFilename(value.filename);
  if (!filename) throw new Error("Invalid job record");
  return { outputPath, filename };
}

function isExactPrivateBlobUrl(value, expectedPath, storeHost) {
  if (typeof value !== "string" || value.length > 16_384) return false;
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      parsed.hostname === storeHost &&
      !parsed.port &&
      !parsed.username &&
      !parsed.password &&
      parsed.pathname === `/${expectedPath}` &&
      !parsed.search &&
      !parsed.hash
    );
  } catch {
    return false;
  }
}

function validateJobBlob(result, expectedPath, storeHost) {
  if (result === null) throw new PublicError(404, "Job not found");
  if (!result || result.statusCode !== 200 || !result.blob) throw new Error("Invalid Blob record");
  const blob = result.blob;
  if (
    blob.pathname !== expectedPath ||
    !isExactPrivateBlobUrl(blob.url, expectedPath, storeHost) ||
    !Number.isSafeInteger(blob.size) ||
    blob.size < 1 ||
    blob.size > MAX_JOB_RECORD_BYTES ||
    typeof blob.contentType !== "string" ||
    blob.contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    throw new Error("Invalid Blob record");
  }
  return blob.size;
}

function validateSignedUrl(value, outputPath, storeHost) {
  if (typeof value !== "string" || value.length > 16_384) throw new Error("Invalid signed URL");
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== storeHost ||
      parsed.port ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== `/${outputPath}` ||
      !parsed.search ||
      parsed.hash
    ) {
      throw new Error();
    }
    return parsed.href;
  } catch {
    throw new Error("Invalid signed URL");
  }
}

export function createBlobDownloadHandler({
  env = process.env,
  nowMs = Date.now,
  getOidcTokenImpl = getVercelOidcToken,
  getImpl = get,
  issueSignedTokenImpl = issueSignedToken,
  presignUrlImpl = presignUrl,
} = {}) {
  return {
    async fetch(request) {
      try {
        const url = new URL(request.url);
        if (url.pathname !== "/api/blob-download" || url.search || url.hash) {
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
        const body = await readBody(request, MAX_REQUEST_BYTES);
        if (!exactKeys(body, ["jobId"]) || !JOB_ID.test(body.jobId || "")) {
          throw new PublicError(400, "Invalid request");
        }

        const oidcToken = await getOidcTokenImpl();
        if (typeof oidcToken !== "string" || !oidcToken.trim()) {
          throw new Error("Blob authentication unavailable");
        }
        const exactOidcToken = oidcToken.trim();
        const jobPath = `shortnotes/v1/jobs/${authority.owner}/${body.jobId}.json`;
        const result = await getImpl(jobPath, {
          access: "private",
          useCache: false,
          storeId: config.storeId,
          oidcToken: exactOidcToken,
        });
        const expectedSize = validateJobBlob(result, jobPath, config.storeHost);
        const bytes = await streamBytes(result.stream, expectedSize);
        let value;
        try {
          value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
        } catch {
          throw new Error("Invalid Blob record");
        }
        const { outputPath, filename } = validateRecord(value, {
          owner: authority.owner,
          jobId: body.jobId,
          storeHost: config.storeHost,
        });

        const validUntil = timestamp + DOWNLOAD_TOKEN_TTL_MS;
        const token = await issueSignedTokenImpl({
          pathname: outputPath,
          operations: ["get"],
          validUntil,
          storeId: config.storeId,
          oidcToken: exactOidcToken,
        });
        const signed = await presignUrlImpl(token, {
          operation: "get",
          pathname: outputPath,
          validUntil,
          access: "private",
          useCache: false,
        });
        const signedUrl = validateSignedUrl(signed?.presignedUrl, outputPath, config.storeHost);
        return jsonResponse({ url: signedUrl, filename });
      } catch (error) {
        if (error instanceof PublicError) {
          return jsonResponse({ error: error.publicMessage }, error.status);
        }
        return jsonResponse({ error: "Service temporarily unavailable" }, 503);
      }
    },
  };
}

export default createBlobDownloadHandler();
