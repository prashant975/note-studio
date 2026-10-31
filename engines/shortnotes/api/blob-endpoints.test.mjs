import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import test from "node:test";

import { get as vercelBlobGet } from "@vercel/blob";
import { uploadPresigned } from "@vercel/blob/client";
import { MockAgent, getGlobalDispatcher, setGlobalDispatcher } from "undici";

import {
  MAX_UPLOAD_BYTES,
  createBlobUploadHandler,
} from "./blob-upload.js";
import {
  MAX_JOB_RECORD_BYTES,
  createBlobDownloadHandler,
} from "./blob-download.js";

const fixture = JSON.parse(
  await readFile(new URL("../web/session-fixtures.json", import.meta.url), "utf8"),
);

const JOB_ID = "a".repeat(32);
const OTHER_ID = "b".repeat(32);
const OWNER = "42ed62c69f8e694f03f9fb29a4ef2a1a";
const STORE_ID = "store_4mlmgDlkyHHkSimU";
const STORE_HOST = "4mlmgdlkyhhksimu.private.blob.vercel-storage.com";
const NOW_MS = (fixture.issuedAt + 10) * 1_000;
const ENV = {
  APP_SESSION_SECRET: fixture.secret,
  APP_ORIGIN: "https://shortnotes.example",
  BLOB_STORE_ID: STORE_ID,
  BLOB_WEBHOOK_PUBLIC_KEY: "test-webhook-public-key",
};

function browserHeaders(extra = {}) {
  return {
    "content-type": "application/json",
    cookie: `shortnotes_session=${fixture.token}`,
    host: "shortnotes.example",
    origin: "https://shortnotes.example",
    "x-csrf-token": fixture.csrf,
    ...extra,
  };
}

function browserPost(path, body, extraHeaders = {}) {
  return new Request(`https://shortnotes.example${path}`, {
    method: "POST",
    headers: browserHeaders(extraHeaders),
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function aliasPost(alias, path, body) {
  return new Request(`https://${alias}${path}`, {
    method: "POST",
    headers: browserHeaders({ host: alias, origin: `https://${alias}` }),
    body: JSON.stringify(body),
  });
}

function currentSessionToken(nowSeconds) {
  const expiry = nowSeconds + 12 * 60 * 60;
  const body = `v1.${expiry}.${fixture.csrf}.${fixture.nonce}`;
  const signature = createHmac("sha256", fixture.secret).update(body, "ascii").digest("base64url");
  return `${body}.${signature}`;
}

function presignBody(id = JOB_ID, overrides = {}) {
  return {
    type: "blob.generate-presigned-url",
    payload: {
      pathname: `shortnotes/v1/inputs/${OWNER}/${id}.pdf`,
      multipart: false,
      clientPayload: id,
      ...overrides,
    },
  };
}

test("prepare returns only the server-derived opaque canonical pathname", async () => {
  let oidcCalls = 0;
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => {
      oidcCalls += 1;
      return "unused";
    },
  });
  const response = await handler.fetch(
    browserPost("/api/blob-upload?action=prepare", { requestId: JOB_ID }),
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    pathname: `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`,
  });
  assert.equal(oidcCalls, 0);
});

test("upload preparation accepts an exact HTTPS alternate Vercel alias", async () => {
  const alias = "shortnotes-pw01-projects.vercel.app";
  const handler = createBlobUploadHandler({ env: ENV, nowMs: () => NOW_MS });
  const response = await handler.fetch(
    aliasPost(alias, "/api/blob-upload?action=prepare", { requestId: JOB_ID }),
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    pathname: `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`,
  });
});

function completeRecord(overrides = {}) {
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  const inputPath = `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`;
  const record = {
    version: 1,
    dispatch_claim_id: "d".repeat(32),
    manifest: {
      id: JOB_ID,
      owner: OWNER,
      source_url: `https://${STORE_HOST}/${inputPath}`,
      state: "complete",
      created_at: fixture.issuedAt,
      updated_at: fixture.issuedAt + 20,
      output_url: `https://${STORE_HOST}/${outputPath}`,
      error: "",
      dispatch_count: 1,
    },
    settings: {
      teacher_name: "PW Faculty",
      instruction: "Keep formula boxes compact.",
      language: "hinglish",
      subject: "chemistry",
      watermark: true,
      logo_watermark: true,
    },
    filename: "Electrochemistry revision.pdf",
  };
  return {
    ...record,
    ...overrides,
    manifest: { ...record.manifest, ...(overrides.manifest || {}) },
    settings: { ...record.settings, ...(overrides.settings || {}) },
  };
}

function blobGetResult(value, overrides = {}) {
  const pathname = `shortnotes/v1/jobs/${OWNER}/${JOB_ID}.json`;
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  return {
    statusCode: 200,
    stream: new ReadableStream({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    }),
    headers: new Headers({ "content-type": "application/json" }),
    blob: {
      url: `https://${STORE_HOST}/${pathname}`,
      downloadUrl: `https://${STORE_HOST}/${pathname}?download=1`,
      pathname,
      contentDisposition: 'attachment; filename="job.json"',
      cacheControl: "private, no-store",
      uploadedAt: new Date(NOW_MS - 1_000),
      etag: "etag-job",
      contentType: "application/json",
      size: bytes.byteLength,
      ...overrides,
    },
  };
}

test("upload copies and verifies the prepared exact path before signing one private PDF PUT", async () => {
  const calls = { oidc: 0, handle: [], issue: [] };
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    async getOidcTokenImpl() {
      calls.oidc += 1;
      return "oidc-test-token";
    },
    async issueSignedTokenImpl(options) {
      calls.issue.push(options);
      return {
        delegationToken: "delegation-token",
        clientSigningToken: "client-signing-token",
        validUntil: options.validUntil,
      };
    },
    async handleUploadPresignedImpl(options) {
      calls.handle.push(options);
      const signed = await options.getSignedToken(
        options.body.payload.pathname,
        options.body.payload.clientPayload,
        options.body.payload.multipart,
      );
      assert.equal(options.webhookPublicKey, ENV.BLOB_WEBHOOK_PUBLIC_KEY);
      assert.equal(signed.urlOptions.allowOverwrite, false);
      assert.equal(signed.urlOptions.addRandomSuffix, false);
      assert.deepEqual(signed.urlOptions.allowedContentTypes, ["application/pdf"]);
      assert.equal(signed.urlOptions.maximumSizeInBytes, MAX_UPLOAD_BYTES);
      assert.equal(signed.urlOptions.validUntil, NOW_MS + 15 * 60 * 1_000);
      return {
        type: "blob.generate-presigned-url",
        presignedUrlPayload: {
          delegationToken: signed.token.delegationToken,
          signature: "safe-signature",
          params: {},
        },
      };
    },
  });

  const response = await handler.fetch(browserPost("/api/blob-upload", presignBody()));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const payload = await response.json();
  assert.equal(payload.type, "blob.generate-presigned-url");
  assert.equal(calls.oidc, 1);
  assert.equal(calls.handle.length, 1);
  assert.deepEqual(calls.handle[0].body, {
    type: "blob.generate-presigned-url",
    payload: {
      pathname: `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`,
      multipart: false,
      clientPayload: JOB_ID,
    },
  });
  assert.deepEqual(calls.issue, [
    {
      pathname: `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`,
      operations: ["put"],
      validUntil: NOW_MS + 15 * 60 * 1_000,
      allowedContentTypes: ["application/pdf"],
      maximumSizeInBytes: MAX_UPLOAD_BYTES,
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
  ]);
});

test("upload preserves the boolean multipart request without widening its scope", async () => {
  let observed;
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    issueSignedTokenImpl: async (options) => ({
      delegationToken: "delegation-token",
      clientSigningToken: "client-signing-token",
      validUntil: options.validUntil,
    }),
    async handleUploadPresignedImpl(options) {
      observed = options.body;
      await options.getSignedToken(
        options.body.payload.pathname,
        options.body.payload.clientPayload,
        options.body.payload.multipart,
      );
      return {
        type: options.body.type,
        presignedUrlPayload: {
          delegationToken: "delegation-token",
          signature: "safe-signature",
          params: {},
        },
      };
    },
  });
  const response = await handler.fetch(
    browserPost("/api/blob-upload", presignBody(JOB_ID, { multipart: true })),
  );
  assert.equal(response.status, 200);
  assert.equal(observed.payload.multipart, true);
  assert.equal(observed.payload.pathname, `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`);
});

test("official presigned helper accepts both single-PUT and multipart handshakes", async () => {
  const sdkNow = Date.now();
  const token = currentSessionToken(Math.floor(sdkNow / 1_000));
  for (const multipart of [false, true]) {
    const handler = createBlobUploadHandler({
      env: ENV,
      nowMs: () => sdkNow,
      getOidcTokenImpl: async () => "oidc-test-token",
      async issueSignedTokenImpl(options) {
        const delegation = {
          storeId: STORE_ID,
          ownerId: "owner-for-local-contract-test",
          pathname: options.pathname,
          operations: options.operations,
          validUntil: options.validUntil,
          iat: sdkNow,
          maximumSizeInBytes: options.maximumSizeInBytes,
          allowedContentTypes: options.allowedContentTypes,
        };
        return {
          delegationToken: `${Buffer.from(JSON.stringify(delegation)).toString("base64url")}.test-control-signature`,
          clientSigningToken: "local-client-signing-key",
          validUntil: options.validUntil,
        };
      },
    });
    const response = await handler.fetch(
      browserPost(
        "/api/blob-upload",
        presignBody(JOB_ID, { multipart }),
        { cookie: `shortnotes_session=${token}` },
      ),
    );
    assert.equal(response.status, 200);
    const payload = await response.json();
    assert.equal(payload.type, "blob.generate-presigned-url");
    assert.equal(typeof payload.presignedUrlPayload.delegationToken, "string");
    assert.equal(typeof payload.presignedUrlPayload.signature, "string");
    assert.equal(typeof payload.presignedUrlPayload.params, "object");
  }
});

test("official uploadPresigned sends the prepared canonical pathname on the actual PUT and MPU request", async () => {
  const sdkNow = Date.now();
  const sessionToken = currentSessionToken(Math.floor(sdkNow / 1_000));
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => sdkNow,
    getOidcTokenImpl: async () => "oidc-test-token",
    async issueSignedTokenImpl(options) {
      const delegation = {
        storeId: STORE_ID,
        ownerId: "owner-for-local-put-contract-test",
        pathname: options.pathname,
        operations: options.operations,
        validUntil: options.validUntil,
        iat: sdkNow,
        maximumSizeInBytes: options.maximumSizeInBytes,
        allowedContentTypes: options.allowedContentTypes,
      };
      return {
        delegationToken: `${Buffer.from(JSON.stringify(delegation)).toString("base64url")}.test-control-signature`,
        clientSigningToken: "local-client-signing-key",
        validUntil: options.validUntil,
      };
    },
  });
  const preparedResponse = await handler.fetch(
    browserPost(
      "/api/blob-upload?action=prepare",
      { requestId: JOB_ID },
      { cookie: `shortnotes_session=${sessionToken}` },
    ),
  );
  assert.equal(preparedResponse.status, 200);
  const { pathname } = await preparedResponse.json();
  assert.equal(pathname, `shortnotes/v1/inputs/${OWNER}/${JOB_ID}.pdf`);

  for (const multipart of [false, true]) {
    const captured = [];
    const abortController = new AbortController();
    let baseUrl = "";
    const server = createServer((incoming, outgoing) => {
      void (async () => {
        const chunks = [];
        for await (const chunk of incoming) chunks.push(chunk);
        if (incoming.url === "/api/blob-upload") {
          const sdkRequest = new Request("https://shortnotes.example/api/blob-upload", {
            method: "POST",
            headers: browserHeaders({ cookie: `shortnotes_session=${sessionToken}` }),
            body: Buffer.concat(chunks),
          });
          const sdkResponse = await handler.fetch(sdkRequest);
          const responseBytes = Buffer.from(await sdkResponse.arrayBuffer());
          outgoing.statusCode = sdkResponse.status;
          outgoing.setHeader("content-type", sdkResponse.headers.get("content-type"));
          outgoing.end(responseBytes);
          return;
        }
        captured.push({ method: incoming.method, url: incoming.url });
        abortController.abort();
        outgoing.statusCode = 499;
        outgoing.end();
      })().catch(() => {
        if (!outgoing.headersSent) outgoing.statusCode = 500;
        outgoing.end();
      });
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const address = server.address();
    assert.equal(typeof address, "object");
    baseUrl = `http://127.0.0.1:${address.port}`;
    const previousApiUrl = process.env.VERCEL_BLOB_API_URL;
    process.env.VERCEL_BLOB_API_URL = baseUrl;
    try {
      await assert.rejects(
        uploadPresigned(
          pathname,
          new Blob(["%PDF-1.7\ncontract"], { type: "application/pdf" }),
          {
            access: "private",
            contentType: "application/pdf",
            handleUploadUrl: `${baseUrl}/api/blob-upload`,
            clientPayload: JOB_ID,
            headers: { "X-CSRF-Token": fixture.csrf },
            multipart,
            abortSignal: abortController.signal,
          },
        ),
      );
    } finally {
      if (previousApiUrl === undefined) delete process.env.VERCEL_BLOB_API_URL;
      else process.env.VERCEL_BLOB_API_URL = previousApiUrl;
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
    assert.equal(captured.length, 1);
    const actualRequest = new URL(captured[0].url, baseUrl);
    assert.equal(actualRequest.searchParams.get("pathname"), pathname);
    assert.equal(actualRequest.pathname, multipart ? "/mpu" : "/");
    assert.notEqual(actualRequest.searchParams.get("pathname"), `${JOB_ID}.pdf`);
  }
});

test("upload rejects path substitution, ID mismatch, extras, and non-boolean multipart before the SDK", async () => {
  let calls = 0;
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    issueSignedTokenImpl: async () => ({}),
    handleUploadPresignedImpl: async () => {
      calls += 1;
      return {};
    },
  });
  const bodies = [
    presignBody(JOB_ID, { clientPayload: OTHER_ID }),
    presignBody(JOB_ID, { pathname: `${JOB_ID}.pdf` }),
    presignBody(JOB_ID, { pathname: `shortnotes/v1/inputs/${OTHER_ID}/${JOB_ID}.pdf` }),
    presignBody(JOB_ID, { multipart: "true" }),
    { ...presignBody(), extra: true },
    { type: "blob.upload-completed", payload: {} },
  ];
  for (const body of bodies) {
    const response = await handler.fetch(browserPost("/api/blob-upload", body));
    assert.equal(response.status, 400);
  }
  assert.equal(calls, 0);
});

test("upload fails closed on session, origin, CSRF, MIME, and oversized handshake bodies", async () => {
  let calls = 0;
  const handler = createBlobUploadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    issueSignedTokenImpl: async () => ({}),
    handleUploadPresignedImpl: async () => {
      calls += 1;
      return {};
    },
  });
  const cases = [
    browserPost("/api/blob-upload", presignBody(), { cookie: "" }),
    browserPost("/api/blob-upload", presignBody(), { origin: "https://evil.example" }),
    browserPost("/api/blob-upload", presignBody(), { "x-csrf-token": OTHER_ID }),
    browserPost("/api/blob-upload", presignBody(), { "content-type": "text/plain" }),
    browserPost("/api/blob-upload", `{"padding":"${"x".repeat(9_000)}"}`),
  ];
  const statuses = [];
  for (const request of cases) statuses.push((await handler.fetch(request)).status);
  assert.deepEqual(statuses, [401, 403, 403, 415, 413]);
  assert.equal(calls, 0);
});

test("upload production source cannot enter the classic static-token client-upload flow", async () => {
  const source = await readFile(new URL("./blob-upload.js", import.meta.url), "utf8");
  assert.match(source, /\bhandleUploadPresigned\b/);
  assert.doesNotMatch(source, /\bhandleUpload\s*\(/);
  assert.doesNotMatch(source, /\bgenerateClientTokenFromReadWriteToken\b/);
  assert.doesNotMatch(source, /BLOB_READ_WRITE_TOKEN/);
});

test("download accepts legacy settings, reads uncached, and signs only its exact output GET", async () => {
  const calls = { get: [], issue: [], presign: [], oidc: 0 };
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  const legacyRecord = completeRecord();
  delete legacyRecord.settings.logo_watermark;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    async getOidcTokenImpl() {
      calls.oidc += 1;
      return "oidc-test-token";
    },
    async getImpl(pathname, options) {
      calls.get.push([pathname, options]);
      return blobGetResult(legacyRecord);
    },
    async issueSignedTokenImpl(options) {
      calls.issue.push(options);
      return {
        delegationToken: "delegation-token",
        clientSigningToken: "client-signing-token",
        validUntil: options.validUntil,
      };
    },
    async presignUrlImpl(token, options) {
      calls.presign.push([token, options]);
      return {
        presignedUrl: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
      };
    },
  });

  const response = await handler.fetch(
    browserPost("/api/blob-download", { jobId: JOB_ID }),
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), {
    url: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
    filename: "Electrochemistry revision.pdf",
  });
  assert.equal(calls.oidc, 1);
  assert.deepEqual(calls.get, [[
    `shortnotes/v1/jobs/${OWNER}/${JOB_ID}.json`,
    {
      access: "private",
      useCache: false,
      storeId: STORE_ID,
      oidcToken: "oidc-test-token",
    },
  ]]);
  assert.deepEqual(calls.issue, [{
    pathname: outputPath,
    operations: ["get"],
    validUntil: NOW_MS + 5 * 60 * 1_000,
    storeId: STORE_ID,
    oidcToken: "oidc-test-token",
  }]);
  assert.deepEqual(calls.presign[0][1], {
    operation: "get",
    pathname: outputPath,
    validUntil: NOW_MS + 5 * 60 * 1_000,
    access: "private",
    useCache: false,
  });
});

test("download accepts an exact HTTPS alternate Vercel alias", async () => {
  const alias = "shortnotes-pw01-projects.vercel.app";
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => blobGetResult(completeRecord()),
    issueSignedTokenImpl: async (options) => ({
      delegationToken: "delegation-token",
      clientSigningToken: "client-signing-token",
      validUntil: options.validUntil,
    }),
    presignUrlImpl: async () => ({
      presignedUrl: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
    }),
  });
  const response = await handler.fetch(
    aliasPost(alias, "/api/blob-download", { jobId: JOB_ID }),
  );
  assert.equal(response.status, 200);
  assert.equal((await response.json()).filename, "Electrochemistry revision.pdf");
});

test("official get accepts the configured mixed-case store ID without weakening the job URL boundary", async () => {
  const jobPath = `shortnotes/v1/jobs/${OWNER}/${JOB_ID}.json`;
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  const body = JSON.stringify(completeRecord());
  const previousDispatcher = getGlobalDispatcher();
  const mockAgent = new MockAgent();
  mockAgent.disableNetConnect();
  setGlobalDispatcher(mockAgent);
  mockAgent
    .get(`https://${STORE_HOST}`)
    .intercept({
      path: `/${jobPath}?cache=0`,
      method: "GET",
      headers: { authorization: "Bearer oidc-test-token" },
    })
    .reply(200, body, {
      headers: {
        "content-length": String(Buffer.byteLength(body)),
        "content-type": "application/json",
        "last-modified": new Date(NOW_MS - 1_000).toUTCString(),
        etag: '"etag-job"',
      },
    });

  let signedPaths = 0;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: vercelBlobGet,
    async issueSignedTokenImpl(options) {
      signedPaths += 1;
      assert.equal(options.pathname, outputPath);
      return {
        delegationToken: "delegation-token",
        clientSigningToken: "client-signing-token",
        validUntil: options.validUntil,
      };
    },
    presignUrlImpl: async () => ({
      presignedUrl: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
    }),
  });

  try {
    const response = await handler.fetch(
      browserPost("/api/blob-download", { jobId: JOB_ID }),
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
      url: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
      filename: "Electrochemistry revision.pdf",
    });
    assert.equal(signedPaths, 1);
    mockAgent.assertNoPendingInterceptors();
  } finally {
    setGlobalDispatcher(previousDispatcher);
    await mockAgent.close();
  }
});

test("download filename validation counts Unicode code points like the Python producer", async () => {
  const validFilename = `${"😀".repeat(90)}.pdf`;
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  let presigns = 0;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => blobGetResult(completeRecord({ filename: validFilename })),
    issueSignedTokenImpl: async (options) => ({
      delegationToken: "delegation-token",
      clientSigningToken: "client-signing-token",
      validUntil: options.validUntil,
    }),
    presignUrlImpl: async () => {
      presigns += 1;
      return {
        presignedUrl: `https://${STORE_HOST}/${outputPath}?vercel-blob-signature=test`,
      };
    },
  });
  const response = await handler.fetch(
    browserPost("/api/blob-download", { jobId: JOB_ID }),
  );
  assert.equal(response.status, 200);
  assert.equal((await response.json()).filename, validFilename);
  assert.equal(presigns, 1);

  const invalidHandler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => blobGetResult(
      completeRecord({ filename: `${"😀".repeat(91)}.pdf` }),
    ),
    issueSignedTokenImpl: async () => {
      presigns += 1;
      return {};
    },
    presignUrlImpl: async () => {
      presigns += 1;
      return {};
    },
  });
  const invalidResponse = await invalidHandler.fetch(
    browserPost("/api/blob-download", { jobId: JOB_ID }),
  );
  assert.equal(invalidResponse.status, 503);
  assert.equal(presigns, 1);
});

test("download rejects job Blob URLs that change the exact HTTPS authority or path", async () => {
  const jobPath = `shortnotes/v1/jobs/${OWNER}/${JOB_ID}.json`;
  const unsafeUrls = [
    `http://${STORE_HOST}/${jobPath}`,
    `https://evil.example/${jobPath}`,
    `https://user@${STORE_HOST}/${jobPath}`,
    `https://${STORE_HOST}:8443/${jobPath}`,
    `https://${STORE_HOST}/${jobPath}.bak`,
    `https://${STORE_HOST}/${jobPath}?cache=0`,
    `https://${STORE_HOST}/${jobPath}#fragment`,
  ];
  let presigns = 0;
  for (const url of unsafeUrls) {
    const handler = createBlobDownloadHandler({
      env: ENV,
      nowMs: () => NOW_MS,
      getOidcTokenImpl: async () => "oidc-test-token",
      getImpl: async () => blobGetResult(completeRecord(), { url }),
      issueSignedTokenImpl: async () => {
        presigns += 1;
        return {};
      },
      presignUrlImpl: async () => {
        presigns += 1;
        return {};
      },
    });
    const response = await handler.fetch(
      browserPost("/api/blob-download", { jobId: JOB_ID }),
    );
    assert.equal(response.status, 503);
  }
  assert.equal(presigns, 0);
});

test("official presignUrl produces the exact private output URL with cache bypass", async () => {
  const sdkNow = Date.now();
  const sessionToken = currentSessionToken(Math.floor(sdkNow / 1_000));
  const outputPath = `shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf`;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => sdkNow,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => blobGetResult(completeRecord()),
    async issueSignedTokenImpl(options) {
      const delegation = {
        storeId: STORE_ID,
        ownerId: "owner-for-local-get-contract-test",
        pathname: options.pathname,
        operations: options.operations,
        validUntil: options.validUntil,
        iat: sdkNow,
      };
      return {
        delegationToken: `${Buffer.from(JSON.stringify(delegation)).toString("base64url")}.test-control-signature`,
        clientSigningToken: "local-client-signing-key",
        validUntil: options.validUntil,
      };
    },
  });
  const response = await handler.fetch(
    browserPost(
      "/api/blob-download",
      { jobId: JOB_ID },
      { cookie: `shortnotes_session=${sessionToken}` },
    ),
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  const signed = new URL(payload.url);
  assert.equal(signed.hostname, STORE_HOST);
  assert.equal(signed.pathname, `/${outputPath}`);
  assert.equal(signed.searchParams.get("cache"), "0");
  assert.ok(signed.searchParams.has("vercel-blob-delegation"));
  assert.ok(signed.searchParams.has("vercel-blob-signature"));
});

test("download cannot sign a different owner, host, pathname, or non-complete state", async () => {
  const records = [
    completeRecord({ manifest: { owner: OTHER_ID } }),
    completeRecord({ manifest: { output_url: `https://evil.example/shortnotes/v1/outputs/${OWNER}/${JOB_ID}.pdf` } }),
    completeRecord({ manifest: { output_url: `https://${STORE_HOST}/shortnotes/v1/outputs/${OWNER}/${OTHER_ID}.pdf` } }),
    completeRecord({ manifest: { state: "running", output_url: "", dispatch_count: 1 } }),
  ];
  let presigns = 0;
  for (const record of records) {
    const handler = createBlobDownloadHandler({
      env: ENV,
      nowMs: () => NOW_MS,
      getOidcTokenImpl: async () => "oidc-test-token",
      getImpl: async () => blobGetResult(record),
      issueSignedTokenImpl: async () => {
        presigns += 1;
        return {};
      },
      presignUrlImpl: async () => {
        presigns += 1;
        return {};
      },
    });
    const response = await handler.fetch(
      browserPost("/api/blob-download", { jobId: JOB_ID }),
    );
    assert.notEqual(response.status, 200);
  }
  assert.equal(presigns, 0);
});

test("download rejects missing, oversized, malformed, and unsafe records before presigning", async () => {
  const variants = [
    null,
    blobGetResult(completeRecord(), { size: MAX_JOB_RECORD_BYTES + 1 }),
    blobGetResult(completeRecord({ extra: true })),
    blobGetResult(completeRecord({ dispatch_claim_id: "" })),
    blobGetResult(completeRecord({ dispatch_claim_id: "D".repeat(32) })),
    blobGetResult(completeRecord({ filename: "../escape.pdf" })),
    blobGetResult(completeRecord({ settings: { logo_watermark: "yes" } })),
    blobGetResult(completeRecord({ settings: { extra: true } })),
  ];
  let presigns = 0;
  for (const result of variants) {
    const handler = createBlobDownloadHandler({
      env: ENV,
      nowMs: () => NOW_MS,
      getOidcTokenImpl: async () => "oidc-test-token",
      getImpl: async () => result,
      issueSignedTokenImpl: async () => {
        presigns += 1;
        return {};
      },
      presignUrlImpl: async () => ({}),
    });
    const response = await handler.fetch(
      browserPost("/api/blob-download", { jobId: JOB_ID }),
    );
    assert.notEqual(response.status, 200);
  }
  assert.equal(presigns, 0);
});

test("download rejects invalid browser authority and caller-chosen body fields", async () => {
  let gets = 0;
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => {
      gets += 1;
      return blobGetResult(completeRecord());
    },
    issueSignedTokenImpl: async () => ({}),
    presignUrlImpl: async () => ({}),
  });
  const requests = [
    browserPost("/api/blob-download", { jobId: JOB_ID }, { cookie: "" }),
    browserPost("/api/blob-download", { jobId: JOB_ID }, { origin: "https://evil.example" }),
    browserPost("/api/blob-download", { jobId: JOB_ID }, { "x-csrf-token": OTHER_ID }),
    browserPost("/api/blob-download", { jobId: JOB_ID, outputPath: "other.pdf" }),
    browserPost("/api/blob-download", { jobId: "../escape" }),
  ];
  for (const request of requests) assert.notEqual((await handler.fetch(request)).status, 200);
  assert.equal(gets, 0);
});

test("download rejects a presigner result that escapes the exact private output URL", async () => {
  const handler = createBlobDownloadHandler({
    env: ENV,
    nowMs: () => NOW_MS,
    getOidcTokenImpl: async () => "oidc-test-token",
    getImpl: async () => blobGetResult(completeRecord()),
    issueSignedTokenImpl: async (options) => ({
      delegationToken: "delegation-token",
      clientSigningToken: "client-signing-token",
      validUntil: options.validUntil,
    }),
    presignUrlImpl: async () => ({ presignedUrl: "https://evil.example/output.pdf?signed=1" }),
  });
  const response = await handler.fetch(
    browserPost("/api/blob-download", { jobId: JOB_ID }),
  );
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "Service temporarily unavailable" });
});
