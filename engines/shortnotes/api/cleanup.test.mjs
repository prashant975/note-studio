import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CLEANUP_DELETE_BATCH_SIZE,
  CLEANUP_PAGE_LIMIT,
  createCleanupHandler,
} from "./cleanup.js";

const STORE_ID = "store_4mlmgDlkyHHkSimU";
const STORE_HOST = "4mlmgdlkyhhksimu.private.blob.vercel-storage.com";
const OWNER = "c".repeat(32);
const NOW_MS = Date.UTC(2026, 7, 1, 12, 0, 0);
const ENV = {
  BLOB_STORE_ID: STORE_ID,
  CRON_SECRET: "cron-secret-for-tests",
};

function id(number) {
  return number.toString(16).padStart(32, "0");
}

function listed(pathname, uploadedAt) {
  return {
    url: `https://${STORE_HOST}/${pathname}`,
    downloadUrl: `https://${STORE_HOST}/${pathname}?download=1`,
    pathname,
    size: 123,
    uploadedAt: new Date(uploadedAt),
    etag: `etag-${pathname}`,
  };
}

function cronRequest(secret = ENV.CRON_SECRET, path = "/api/cleanup") {
  return new Request(`https://shortnotes.example${path}`, {
    method: "GET",
    headers: { authorization: `Bearer ${secret}` },
  });
}

test("cleanup uses exact namespace pagination and exact 2h/6h/24h TTL boundaries", async () => {
  const pages = new Map([
    ["shortnotes/v1/inputs/", [
      {
        blobs: [
          listed(`shortnotes/v1/inputs/${OWNER}/${id(1)}.pdf`, NOW_MS - 2 * 60 * 60 * 1_000),
        ],
        hasMore: true,
        cursor: "input-page-two",
      },
      {
        blobs: [
          listed(`shortnotes/v1/inputs/${OWNER}/${id(2)}.pdf`, NOW_MS - 2 * 60 * 60 * 1_000 + 1),
        ],
        hasMore: false,
      },
    ]],
    ["shortnotes/v1/outputs/", [{
      blobs: [
        listed(`shortnotes/v1/outputs/${OWNER}/${id(3)}.pdf`, NOW_MS - 6 * 60 * 60 * 1_000),
        listed(`shortnotes/v1/outputs/${OWNER}/${id(4)}.pdf`, NOW_MS - 6 * 60 * 60 * 1_000 + 1),
      ],
      hasMore: false,
    }]],
    ["shortnotes/v1/jobs/", [{
      blobs: [
        listed(`shortnotes/v1/jobs/${OWNER}/${id(5)}.json`, NOW_MS - 24 * 60 * 60 * 1_000),
        listed(`shortnotes/v1/jobs/${OWNER}/${id(6)}.json`, NOW_MS - 24 * 60 * 60 * 1_000 + 1),
      ],
      hasMore: false,
    }]],
    ["shortnotes/v1/costs/", [{
      blobs: [
        listed(`shortnotes/v1/costs/${OWNER}/${id(7)}.json`, NOW_MS - 24 * 60 * 60 * 1_000),
        listed(`shortnotes/v1/costs/${OWNER}/${id(8)}.json`, NOW_MS - 24 * 60 * 60 * 1_000 + 1),
      ],
      hasMore: false,
    }]],
  ]);
  const pageIndexes = new Map();
  const calls = { oidc: 0, list: [], del: [] };
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    async getOidcTokenImpl() {
      calls.oidc += 1;
      return "oidc-test-token";
    },
    async listImpl(options) {
      calls.list.push(options);
      const index = pageIndexes.get(options.prefix) || 0;
      pageIndexes.set(options.prefix, index + 1);
      return pages.get(options.prefix)?.[index];
    },
    async delImpl(paths, options) {
      calls.del.push([paths, options]);
    },
  });

  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    scanned: 8,
    deleted: 4,
    truncated: false,
  });
  assert.equal(calls.oidc, 1);
  assert.deepEqual(calls.list, [
    {
      prefix: "shortnotes/v1/inputs/",
      limit: CLEANUP_PAGE_LIMIT,
      mode: "expanded",
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
    {
      prefix: "shortnotes/v1/inputs/",
      limit: CLEANUP_PAGE_LIMIT,
      mode: "expanded",
      cursor: "input-page-two",
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
    {
      prefix: "shortnotes/v1/outputs/",
      limit: CLEANUP_PAGE_LIMIT,
      mode: "expanded",
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
    {
      prefix: "shortnotes/v1/jobs/",
      limit: CLEANUP_PAGE_LIMIT,
      mode: "expanded",
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
    {
      prefix: "shortnotes/v1/costs/",
      limit: CLEANUP_PAGE_LIMIT,
      mode: "expanded",
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
  ]);
  assert.deepEqual(calls.del, [[
    [
      `shortnotes/v1/inputs/${OWNER}/${id(1)}.pdf`,
      `shortnotes/v1/outputs/${OWNER}/${id(3)}.pdf`,
      `shortnotes/v1/jobs/${OWNER}/${id(5)}.json`,
      `shortnotes/v1/costs/${OWNER}/${id(7)}.json`,
    ],
    { storeId: STORE_ID, oidcToken: "oidc-test-token" },
  ]]);
});

test("cleanup requires the exact cron bearer value before any Blob call", async () => {
  let calls = 0;
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => {
      calls += 1;
      return "oidc-test-token";
    },
    listImpl: async () => {
      calls += 1;
      return { blobs: [], hasMore: false };
    },
    delImpl: async () => {
      calls += 1;
    },
  });

  for (const request of [
    new Request("https://shortnotes.example/api/cleanup"),
    cronRequest("wrong"),
    cronRequest(`${ENV.CRON_SECRET}x`),
    new Request("https://shortnotes.example/api/cleanup", {
      headers: { authorization: ENV.CRON_SECRET },
    }),
  ]) {
    assert.equal((await handler.fetch(request)).status, 401);
  }
  assert.equal(calls, 0);
});

test("cleanup stops a repeated cursor instead of looping or deleting twice", async () => {
  let listCalls = 0;
  const listPrefixes = [];
  const deletes = [];
  const stalePath = `shortnotes/v1/inputs/${OWNER}/${id(1)}.pdf`;
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      listCalls += 1;
      listPrefixes.push(options.prefix);
      if (options.prefix !== "shortnotes/v1/inputs/") {
        return { blobs: [], hasMore: false };
      }
      return {
        blobs: [listed(stalePath, NOW_MS - 3 * 60 * 60 * 1_000)],
        hasMore: true,
        cursor: "repeated",
      };
    },
    async delImpl(paths) {
      deletes.push(...paths);
    },
  });
  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.equal((await response.json()).truncated, true);
  assert.equal(listCalls, 5);
  assert.deepEqual(listPrefixes, [
    "shortnotes/v1/inputs/",
    "shortnotes/v1/inputs/",
    "shortnotes/v1/outputs/",
    "shortnotes/v1/jobs/",
    "shortnotes/v1/costs/",
  ]);
  assert.deepEqual(deletes, [stalePath]);
});

test("cleanup enforces finite 40-page and 4000-object total caps", async () => {
  let listCalls = 0;
  const pagesByPrefix = new Map();
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      listCalls += 1;
      const page = pagesByPrefix.get(options.prefix) || 0;
      pagesByPrefix.set(options.prefix, page + 1);
      const first = page * CLEANUP_PAGE_LIMIT + 1;
      const extension = /(?:jobs|costs)\/$/.test(options.prefix) ? "json" : "pdf";
      return {
        blobs: Array.from({ length: CLEANUP_PAGE_LIMIT }, (_, index) =>
          listed(
            `${options.prefix}${OWNER}/${id(first + index)}.${extension}`,
            NOW_MS,
          )),
        hasMore: true,
        cursor: `${options.prefix}-page-${page + 1}`,
      };
    },
    delImpl: async () => {},
  });
  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    scanned: 4_000,
    deleted: 0,
    truncated: true,
  });
  assert.equal(listCalls, 40);
  assert.deepEqual([...pagesByPrefix.entries()], [
    ["shortnotes/v1/inputs/", 10],
    ["shortnotes/v1/outputs/", 10],
    ["shortnotes/v1/jobs/", 10],
    ["shortnotes/v1/costs/", 10],
  ]);
});

test("a saturated input scan cannot starve stale outputs or jobs", async () => {
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${id(2_001)}.pdf`;
  const jobPath = `shortnotes/v1/jobs/${OWNER}/${id(2_002)}.json`;
  const costPath = `shortnotes/v1/costs/${OWNER}/${id(2_003)}.json`;
  const listPrefixes = [];
  const pagesByPrefix = new Map();
  const deletes = [];
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      listPrefixes.push(options.prefix);
      const page = pagesByPrefix.get(options.prefix) || 0;
      pagesByPrefix.set(options.prefix, page + 1);
      if (options.prefix === "shortnotes/v1/inputs/") {
        const first = page * CLEANUP_PAGE_LIMIT + 1;
        return {
          blobs: Array.from({ length: CLEANUP_PAGE_LIMIT }, (_, index) =>
            listed(
              `shortnotes/v1/inputs/${OWNER}/${id(first + index)}.pdf`,
              NOW_MS - 3 * 60 * 60 * 1_000,
            )),
          hasMore: true,
          cursor: `input-page-${page + 1}`,
        };
      }
      if (options.prefix === "shortnotes/v1/outputs/") {
        return {
          blobs: [listed(outputPath, NOW_MS - 7 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      if (options.prefix === "shortnotes/v1/jobs/") {
        return {
          blobs: [listed(jobPath, NOW_MS - 25 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      if (options.prefix === "shortnotes/v1/costs/") {
        return {
          blobs: [listed(costPath, NOW_MS - 25 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      throw new Error(`Unexpected prefix: ${options.prefix}`);
    },
    async delImpl(paths) {
      deletes.push(...paths);
    },
  });

  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    scanned: 1_003,
    deleted: 1_003,
    truncated: true,
  });
  assert.deepEqual(listPrefixes, [
    ...Array(10).fill("shortnotes/v1/inputs/"),
    "shortnotes/v1/outputs/",
    "shortnotes/v1/jobs/",
    "shortnotes/v1/costs/",
  ]);
  assert.ok(deletes.includes(outputPath));
  assert.ok(deletes.includes(jobPath));
  assert.ok(deletes.includes(costPath));
});

test("cleanup rejects cross-namespace list entries and still scans later namespaces", async () => {
  const misplacedOutput = `shortnotes/v1/outputs/${OWNER}/${id(3_001)}.pdf`;
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${id(3_002)}.pdf`;
  const jobPath = `shortnotes/v1/jobs/${OWNER}/${id(3_003)}.json`;
  const costPath = `shortnotes/v1/costs/${OWNER}/${id(3_004)}.json`;
  const deletes = [];
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      if (options.prefix === "shortnotes/v1/inputs/") {
        return {
          blobs: [listed(misplacedOutput, NOW_MS - 7 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      if (options.prefix === "shortnotes/v1/outputs/") {
        return {
          blobs: [listed(outputPath, NOW_MS - 6 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      if (options.prefix === "shortnotes/v1/jobs/") {
        return {
          blobs: [listed(jobPath, NOW_MS - 24 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      if (options.prefix === "shortnotes/v1/costs/") {
        return {
          blobs: [listed(costPath, NOW_MS - 24 * 60 * 60 * 1_000)],
          hasMore: false,
        };
      }
      throw new Error(`Unexpected prefix: ${options.prefix}`);
    },
    async delImpl(paths) {
      deletes.push(...paths);
    },
  });

  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    scanned: 3,
    deleted: 3,
    truncated: true,
  });
  assert.deepEqual(deletes, [outputPath, jobPath, costPath]);
  assert.ok(!deletes.includes(misplacedOutput));
});

test("cleanup deletes stale objects in bounded batches", async () => {
  const objects = Array.from({ length: 205 }, (_, index) =>
    listed(
      `shortnotes/v1/jobs/${OWNER}/${id(index + 1)}.json`,
      NOW_MS - 25 * 60 * 60 * 1_000,
    ));
  let jobPage = 0;
  const batches = [];
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      if (options.prefix !== "shortnotes/v1/jobs/") {
        return { blobs: [], hasMore: false };
      }
      const start = jobPage * CLEANUP_PAGE_LIMIT;
      const blobs = objects.slice(start, start + CLEANUP_PAGE_LIMIT);
      jobPage += 1;
      return {
        blobs,
        hasMore: start + blobs.length < objects.length,
        cursor: start + blobs.length < objects.length ? `cursor-${jobPage}` : undefined,
      };
    },
    async delImpl(paths) {
      batches.push([...paths]);
    },
  });
  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.equal((await response.json()).deleted, 205);
  assert.deepEqual(batches.map((batch) => batch.length), [
    CLEANUP_DELETE_BATCH_SIZE,
    CLEANUP_DELETE_BATCH_SIZE,
    5,
  ]);
});

test("cleanup stops safely when hasMore omits a usable cursor", async () => {
  let listCalls = 0;
  const listPrefixes = [];
  const handler = createCleanupHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    async listImpl(options) {
      listCalls += 1;
      listPrefixes.push(options.prefix);
      return { blobs: [], hasMore: true };
    },
    delImpl: async () => {},
  });
  const response = await handler.fetch(cronRequest());
  assert.equal(response.status, 200);
  assert.equal((await response.json()).truncated, true);
  assert.equal(listCalls, 4);
  assert.deepEqual(listPrefixes, [
    "shortnotes/v1/inputs/",
    "shortnotes/v1/outputs/",
    "shortnotes/v1/jobs/",
    "shortnotes/v1/costs/",
  ]);
});

test("cleanup production module contains only Blob/OIDC dependencies", async () => {
  const source = await readFile(new URL("./cleanup.js", import.meta.url), "utf8");
  const imports = [...source.matchAll(/from\s+["']([^"']+)["']/g)].map((match) => match[1]);
  assert.deepEqual(imports.sort(), ["@vercel/blob", "@vercel/oidc", "node:crypto"].sort());
  assert.doesNotMatch(source, /notesbot|gemini|litellm|image_gen|generate_short_notes/);
  assert.doesNotMatch(source, /BLOB_READ_WRITE_TOKEN/);
});
