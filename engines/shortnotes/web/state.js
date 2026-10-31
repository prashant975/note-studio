export const MAX_INPUT_BYTES = 100 * 1024 * 1024;
export const MAX_OUTPUT_BYTES = 100 * 1024 * 1024;
export const POLL_INTERVAL_MS = 2_500;
export const POLL_DEADLINE_MS = 720_000;

export const SUBJECTS = Object.freeze([
  "auto",
  "chemistry",
  "physics",
  "biology",
  "mathematics",
  "sanskrit",
  "science",
  "social_science",
  "english",
  "hindi",
  "other",
]);

export const LANGUAGES = Object.freeze([
  "auto",
  "english",
  "hindi",
  "sanskrit",
  "sanskrit_hindi",
  "hinglish",
]);

// Exam categories: each follows its own note-format commands on the server.
export const CATEGORIES = Object.freeze(["neet_jee", "foundation"]);

const TERMINAL_STATES = new Set(["complete", "failed", "expired"]);
const ACTIVE_STATES = new Set(["importing", "queued", "running"]);
const HEX_32 = /^[0-9a-f]{32}$/;
const WEB_ACTIONS = new Set(["auth", "session", "start", "import", "status", "downloaded", "logout"]);

export class PollDeadlineError extends Error {
  constructor(message = "Generation status check reached the 12-minute limit.") {
    super(message);
    this.name = "PollDeadlineError";
  }
}

export function defaultForm() {
  return {
    watermark: false,
    logoWatermark: true,
    subject: "auto",
    language: "auto",
    category: "neet_jee",
  };
}

export function normalizeTeacherName(value) {
  return typeof value === "string" ? value.trim().replace(/\s+/g, " ") : "";
}

export function validateFile(file) {
  const errors = [];
  if (!file || typeof file !== "object") {
    return ["Lecture PDF choose karo."];
  }

  const name = typeof file.name === "string" ? file.name : "";
  const type = typeof file.type === "string" ? file.type.toLowerCase() : "";
  const size = Number(file.size);

  if (!/\.pdf$/i.test(name) || (type && type !== "application/pdf")) {
    errors.push("Sirf PDF file upload karo.");
  }
  if (!Number.isFinite(size) || size <= 0) {
    errors.push("PDF empty hai; doosri file choose karo.");
  } else if (size > MAX_INPUT_BYTES) {
    errors.push("PDF 100 MB ya usse chhoti honi chahiye.");
  }
  return errors;
}

export function validateGeneration(source, form = {}) {
  const structured = source && typeof source === "object" && (Object.hasOwn(source, "file") || Object.hasOwn(source, "pdfUrl"));
  const file = structured ? source.file : source;
  const pdfUrl = source?.pdfUrl;
  const hasFile = Boolean(file);
  const hasLink = typeof pdfUrl === "string" && pdfUrl.trim() !== "";
  const errors = hasFile === hasLink ? ["Ek PDF file ya ek public PDF link choose karo."] : (hasFile ? validateFile(file) : []);
  let normalizedUrl = "";
  if (hasLink) {
    normalizedUrl = pdfUrl.trim();
    try { if (new URL(normalizedUrl).protocol !== "https:" || normalizedUrl.length > 2048) throw new Error(); }
    catch { errors.push("Valid HTTPS PDF link paste karo."); }
  }
  const teacherName = normalizeTeacherName(form.teacherName);
  const instructions = typeof form.instructions === "string" ? form.instructions : "";

  if (!teacherName || teacherName.length > 120) {
    errors.push("Teacher name 1–120 characters mein likho.");
  }
  if (!SUBJECTS.includes(form.subject)) {
    errors.push("Valid subject choose karo.");
  }
  if (!LANGUAGES.includes(form.language)) {
    errors.push("Valid output language choose karo.");
  }
  const category = form.category === undefined ? "neet_jee" : form.category;
  if (!CATEGORIES.includes(category)) {
    errors.push("Valid category choose karo (NEET-JEE ya Foundation).");
  }
  if (instructions.length > 1_000) {
    errors.push("Special instructions 1,000 characters tak rakho.");
  }
  if (typeof form.watermark !== "boolean") {
    errors.push("Teacher watermark option valid nahi hai.");
  }
  if (typeof form.logoWatermark !== "boolean") {
    errors.push("Logo watermark option valid nahi hai.");
  }

  return {
    ok: errors.length === 0,
    errors,
    value: {
      teacherName,
      subject: SUBJECTS.includes(form.subject) ? form.subject : "auto",
      language: LANGUAGES.includes(form.language) ? form.language : "auto",
      category: CATEGORIES.includes(category) ? category : "neet_jee",
      instructions,
      watermark: form.watermark === true,
      logoWatermark: form.logoWatermark === true,
      ...(hasLink ? { pdfUrl: normalizedUrl } : {}),
    },
  };
}

export function getSafeStorage(globalObject = globalThis) {
  try {
    return globalObject?.localStorage || null;
  } catch {
    return null;
  }
}

function resolvedStorage(storage) {
  return storage === undefined ? getSafeStorage() : storage;
}

function safeStorageGet(storage, key) {
  try {
    return storage?.getItem?.(key) ?? null;
  } catch {
    return null;
  }
}

function safeStorageSet(storage, key, value) {
  try {
    storage?.setItem?.(key, value);
  } catch {
    // Memory state remains authoritative when persistence is unavailable.
  }
}

function safeStorageRemove(storage, key) {
  try {
    storage?.removeItem?.(key);
  } catch {
    // Memory state remains authoritative when persistence is unavailable.
  }
}

export function compareAndSetSavedJob(storage, expectedJobId = "", nextJobId = "") {
  storage = resolvedStorage(storage);
  if (!storage) return true;
  if (expectedJobId !== "" && !HEX_32.test(expectedJobId)) {
    throw new TypeError("Invalid expected job ID.");
  }
  if (nextJobId !== "" && !HEX_32.test(nextJobId)) {
    throw new TypeError("Invalid next job ID.");
  }

  let current;
  try {
    current = storage.getItem?.("lastJobId") ?? null;
  } catch {
    return true;
  }
  const currentJobId = HEX_32.test(current || "") ? current : "";
  if (currentJobId !== expectedJobId && currentJobId !== nextJobId) return false;

  try {
    if (nextJobId) storage.setItem?.("lastJobId", nextJobId);
    else storage.removeItem?.("lastJobId");
  } catch {
    // Keep the in-memory job authoritative when persistence is unavailable.
  }
  return true;
}

export function loadPreferences(storage) {
  storage = resolvedStorage(storage);
  const defaults = defaultForm();
  if (!storage) {
    return {
      teacherName: "",
      subject: defaults.subject,
      language: defaults.language,
      category: defaults.category,
      watermark: defaults.watermark,
      logoWatermark: defaults.logoWatermark,
      lastJobId: "",
    };
  }

  const teacherName = normalizeTeacherName(safeStorageGet(storage, "teacherName"));
  const subject = safeStorageGet(storage, "subject");
  const language = safeStorageGet(storage, "language");
  const category = safeStorageGet(storage, "category");
  const lastJobId = safeStorageGet(storage, "lastJobId");
  const watermark = safeStorageGet(storage, "watermark");
  const logoWatermark = safeStorageGet(storage, "logoWatermark");
  return {
    teacherName,
    subject: SUBJECTS.includes(subject) ? subject : defaults.subject,
    language: LANGUAGES.includes(language) ? language : defaults.language,
    category: CATEGORIES.includes(category) ? category : defaults.category,
    watermark: watermark === "true"
      ? true
      : watermark === "false"
        ? false
        : defaults.watermark,
    logoWatermark: logoWatermark === "true"
      ? true
      : logoWatermark === "false"
        ? false
        : defaults.logoWatermark,
    lastJobId: HEX_32.test(lastJobId || "") ? lastJobId : "",
  };
}

export function savePreferences(storage, values = {}) {
  storage = resolvedStorage(storage);
  if (!storage) return;
  safeStorageSet(storage, "teacherName", normalizeTeacherName(values.teacherName));
  safeStorageSet(
    storage,
    "subject",
    SUBJECTS.includes(values.subject) ? values.subject : "auto",
  );
  safeStorageSet(
    storage,
    "language",
    LANGUAGES.includes(values.language) ? values.language : "auto",
  );
  const defaults = defaultForm();
  safeStorageSet(
    storage,
    "category",
    CATEGORIES.includes(values.category) ? values.category : defaults.category,
  );
  safeStorageSet(
    storage,
    "watermark",
    String(typeof values.watermark === "boolean" ? values.watermark : defaults.watermark),
  );
  safeStorageSet(
    storage,
    "logoWatermark",
    String(
      typeof values.logoWatermark === "boolean"
        ? values.logoWatermark
        : defaults.logoWatermark,
    ),
  );
  if (HEX_32.test(values.lastJobId || "")) {
    compareAndSetSavedJob(storage, "", values.lastJobId);
  }
}

export function clearSavedJob(storage, expectedJobId) {
  if (expectedJobId === undefined) {
    safeStorageRemove(resolvedStorage(storage), "lastJobId");
    return true;
  }
  return compareAndSetSavedJob(storage, expectedJobId, "");
}

export function randomRequestId(cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl || typeof cryptoImpl.getRandomValues !== "function") {
    throw new Error("Secure random IDs are unavailable in this browser.");
  }
  const bytes = new Uint8Array(16);
  cryptoImpl.getRandomValues(bytes);
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export function webApiUrl(action, { jobId } = {}) {
  if (!WEB_ACTIONS.has(action)) throw new TypeError("Invalid web API action.");
  const query = new URLSearchParams({ action });
  if (jobId !== undefined) {
    if (action !== "status" || !HEX_32.test(jobId)) {
      throw new TypeError("Invalid status job ID.");
    }
    query.set("jobId", jobId);
  }
  return `/api/web?${query.toString()}`;
}

export function requestSignedDownload(apiJson, { jobId, csrf, signal } = {}) {
  if (typeof apiJson !== "function") throw new TypeError("Download API required.");
  if (!HEX_32.test(jobId || "")) throw new TypeError("Invalid download job ID.");
  if (!HEX_32.test(csrf || "")) throw new TypeError("Invalid download session.");
  return apiJson("/api/blob-download", {
    method: "POST",
    csrf,
    body: { jobId },
    signal,
  });
}

export function presignedUploadConfig(
  pathname,
  requestId,
  csrf,
  { onUploadProgress, abortSignal, multipart = false } = {},
) {
  if (!HEX_32.test(requestId || "")) throw new TypeError("Invalid upload request ID.");
  if (!HEX_32.test(csrf || "")) throw new TypeError("Invalid upload session.");
  const expectedPath = new RegExp(
    `^shortnotes/v1/inputs/[0-9a-f]{32}/${requestId}\\.pdf$`,
  );
  if (typeof pathname !== "string" || !expectedPath.test(pathname)) {
    throw new TypeError("Invalid prepared upload pathname.");
  }
  if (typeof multipart !== "boolean") throw new TypeError("Invalid multipart option.");
  const options = {
    access: "private",
    contentType: "application/pdf",
    handleUploadUrl: "/api/blob-upload",
    clientPayload: requestId,
    headers: { "X-CSRF-Token": csrf },
    multipart,
  };
  if (typeof onUploadProgress === "function") options.onUploadProgress = onUploadProgress;
  if (abortSignal) options.abortSignal = abortSignal;
  return { pathname, options };
}

export async function startGeneration({
  file,
  source,
  form,
  csrf,
  upload,
  start,
  startUploaded = start,
  importAndStart,
  onUploaded,
  onReserved,
  requestId = randomRequestId(),
  onUploadProgress,
}) {
  const normalizedSource = source || { file };
  const selectedFile = normalizedSource.file;
  const validation = validateGeneration(normalizedSource, form);
  if (!validation.ok) {
    throw new TypeError(validation.errors[0]);
  }
  if (!HEX_32.test(requestId)) {
    throw new TypeError("Request ID invalid hai. Page refresh karke try karo.");
  }
  if (!HEX_32.test(csrf || "")) {
    throw new TypeError("Session expire ho gayi. Dobara login karo.");
  }
  if ((selectedFile && (typeof upload !== "function" || typeof startUploaded !== "function")) || (!selectedFile && typeof importAndStart !== "function")) {
    throw new TypeError("Generation service available nahi hai.");
  }

  onReserved?.(requestId);
  if (!selectedFile) {
    const started = await importAndStart({ requestId, pdfUrl: validation.value.pdfUrl, ...validation.value, csrf });
    if (
      !started ||
      typeof started !== "object" ||
      !HEX_32.test(started.jobId || "") ||
      !ACTIVE_STATES.has(started.state) && !TERMINAL_STATES.has(started.state)
    ) {
      throw new Error("Generation start nahi hui.");
    }
    return { ...started, requestId };
  }
  const uploaded = await upload({
    file: selectedFile,
    csrf,
    requestId,
    onUploadProgress,
  });
  if (!uploaded || typeof uploaded.url !== "string" || !uploaded.url.startsWith("https://")) {
    throw new Error("PDF upload complete nahi hua.");
  }
  onUploaded?.(requestId, uploaded);

  const value = validation.value;
  const started = await startUploaded({
    requestId,
    inputUrl: uploaded.url,
    inputSize: Number(selectedFile.size),
    filename: selectedFile.name,
    teacherName: value.teacherName,
    subject: value.subject,
    language: value.language,
    instructions: value.instructions,
    watermark: value.watermark,
    logoWatermark: value.logoWatermark,
    category: value.category,
  });
  if (!started || typeof started !== "object") {
    throw new Error("Generation start nahi hui.");
  }
  return { ...started, requestId };
}

function normalizeStatus(status) {
  if (!status || typeof status !== "object") {
    throw new Error("Job status invalid mila.");
  }
  const state = status.state || status.status;
  if (![...TERMINAL_STATES, ...ACTIVE_STATES].includes(state)) {
    throw new Error("Job status invalid mila.");
  }
  return status.state === state ? status : { ...status, state };
}

function abortReason(signal) {
  return signal.reason instanceof Error
    ? signal.reason
    : Object.assign(new Error("Status check stopped."), { name: "AbortError" });
}

function raceWithAbort(promise, signal) {
  if (signal.aborted) return Promise.reject(abortReason(signal));
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortReason(signal));
    signal.addEventListener("abort", onAbort, { once: true });
    Promise.resolve(promise).then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

function abortableDelay(delay, signal, setTimeoutFn, clearTimeoutFn) {
  if (signal.aborted) return Promise.reject(abortReason(signal));
  return new Promise((resolve, reject) => {
    let handle;
    const onAbort = () => {
      clearTimeoutFn(handle);
      signal.removeEventListener("abort", onAbort);
      reject(abortReason(signal));
    };
    handle = setTimeoutFn(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delay);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function defaultRetryableStatusError(error) {
  const status = Number(error?.status);
  return ![400, 401, 403, 404, 410].includes(status);
}

export async function pollJob(
  fetchStatus,
  {
    intervalMs = POLL_INTERVAL_MS,
    deadlineMs = POLL_DEADLINE_MS,
    onStatus,
    onTransientError,
    shouldRetryError = defaultRetryableStatusError,
    signal: externalSignal,
    setTimeoutFn = globalThis.setTimeout.bind(globalThis),
    clearTimeoutFn = globalThis.clearTimeout.bind(globalThis),
    AbortControllerImpl = globalThis.AbortController,
  } = {},
) {
  if (typeof fetchStatus !== "function") throw new TypeError("fetchStatus required");
  if (!Number.isFinite(intervalMs) || intervalMs < 0) throw new TypeError("Invalid poll interval");
  if (!Number.isFinite(deadlineMs) || deadlineMs <= 0) throw new TypeError("Invalid poll deadline");

  const controller = new AbortControllerImpl();
  const deadlineError = new PollDeadlineError();
  const deadlineHandle = setTimeoutFn(() => controller.abort(deadlineError), deadlineMs);
  const onExternalAbort = () => controller.abort(abortReason(externalSignal));
  if (externalSignal) {
    if (externalSignal.aborted) onExternalAbort();
    else externalSignal.addEventListener("abort", onExternalAbort, { once: true });
  }

  let attempt = 0;
  try {
    while (true) {
      let status;
      try {
        const raw = await raceWithAbort(
          fetchStatus({ signal: controller.signal, attempt }),
          controller.signal,
        );
        status = normalizeStatus(raw);
      } catch (error) {
        if (controller.signal.aborted) throw abortReason(controller.signal);
        if (!shouldRetryError(error)) throw error;
        onTransientError?.(error);
        attempt += 1;
        await abortableDelay(
          intervalMs,
          controller.signal,
          setTimeoutFn,
          clearTimeoutFn,
        );
        continue;
      }
      onStatus?.(status);
      if (TERMINAL_STATES.has(status.state)) return status;
      attempt += 1;
      await abortableDelay(
        intervalMs,
        controller.signal,
        setTimeoutFn,
        clearTimeoutFn,
      );
    }
  } finally {
    controller.abort();
    clearTimeoutFn(deadlineHandle);
    externalSignal?.removeEventListener?.("abort", onExternalAbort);
  }
}

export async function resumeSavedJob({
  jobId,
  storage,
  getStatus,
  onStatus,
  pollOptions = {},
}) {
  const lastJobId = HEX_32.test(jobId || "")
    ? jobId
    : loadPreferences(storage).lastJobId;
  if (!lastJobId) return null;
  return pollJob(
    ({ signal }) => getStatus(lastJobId, { signal }),
    { ...pollOptions, onStatus },
  );
}

export async function recoverSavedJob({
  jobId,
  storage,
  getStatus,
  onStatus,
  onComplete,
  pollOptions = {},
}) {
  const status = await resumeSavedJob({
    jobId,
    storage,
    getStatus,
    onStatus,
    pollOptions,
  });
  if (!status) return { status: null, delivery: null };
  const delivery = status.state === "complete" && typeof onComplete === "function"
    ? await onComplete(status)
    : null;
  return { status, delivery };
}

export function shouldClearJob({
  state = "",
  statusCode = 0,
  acknowledged = false,
  explicit = false,
} = {}) {
  return (
    explicit === true ||
    acknowledged === true ||
    state === "failed" ||
    state === "expired" ||
    [400, 408, 413, 422].includes(Number(statusCode)) ||
    Number(statusCode) === 404 ||
    Number(statusCode) === 410
  );
}

export function createObjectUrlRegistry(revoke) {
  if (typeof revoke !== "function") throw new TypeError("Object URL revoke function required.");
  let current = null;
  return {
    get current() {
      return current;
    },
    replace(next) {
      const normalized = typeof next === "string" && next ? next : null;
      if (current && current !== normalized) revoke(current);
      current = normalized;
      return current;
    },
    clear() {
      if (!current) return;
      const previous = current;
      current = null;
      revoke(previous);
    },
  };
}

export function sanitizeFilename(value) {
  let filename = typeof value === "string" ? value.normalize("NFKC") : "";
  filename = filename
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "")
    .replace(/\s+/g, " ")
    .replace(/^[.\s-]+|[.\s-]+$/g, "")
    .replace(/\.pdf$/i, "")
    .replace(/[.\s-]+$/g, "")
    .slice(0, 90)
    .trim();
  return `${filename || "studygram-notes"}.pdf`;
}

export function detectIOS(navigatorImpl = globalThis.navigator) {
  if (!navigatorImpl) return false;
  const platform = navigatorImpl.platform || "";
  const userAgent = navigatorImpl.userAgent || "";
  const touchMac = platform === "MacIntel" && Number(navigatorImpl.maxTouchPoints) > 1;
  return /iPad|iPhone|iPod/i.test(userAgent) || touchMac;
}

export function reserveDownloadWindow(openImpl = globalThis.window?.open?.bind(globalThis.window)) {
  if (typeof openImpl !== "function") return null;
  try {
    const reserved = openImpl("/download-wait.html", "shortnotes_download");
    if (reserved) {
      try {
        reserved.opener = null;
      } catch {
        // Some WebKit WindowProxy objects make opener read-only.
      }
    }
    return reserved;
  } catch {
    return null;
  }
}

function navigateWindow(targetWindow, url) {
  if (!targetWindow || targetWindow.closed) return false;
  try {
    if (typeof targetWindow.location?.replace === "function") {
      targetWindow.location.replace(url);
    } else {
      targetWindow.location = url;
    }
    return true;
  } catch {
    return false;
  }
}

function defaultTriggerDownload(url, filename) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function isLikelyCorsFailure(error) {
  return error instanceof TypeError;
}

function beginBoundedAcknowledgement(
  acknowledge,
  {
    timeoutMs,
    setTimeoutFn,
    clearTimeoutFn,
    AbortControllerImpl,
  },
) {
  if (typeof acknowledge !== "function") return Promise.resolve(false);
  const controller = new AbortControllerImpl();
  let timeoutHandle;
  const timeout = new Promise((resolve) => {
    timeoutHandle = setTimeoutFn(() => {
      controller.abort();
      resolve(false);
    }, timeoutMs);
  });
  const attempt = Promise.resolve()
    .then(() => acknowledge({ signal: controller.signal }))
    .then(() => true, () => false);
  return Promise.race([attempt, timeout]).finally(() => {
    clearTimeoutFn(timeoutHandle);
    controller.abort();
  });
}

async function readBoundedResponseBody(response, maxBytes, signal) {
  const getReader = response?.body?.getReader;
  if (typeof getReader !== "function") return null;

  const reader = getReader.call(response.body);
  const chunks = [];
  let total = 0;
  let cancelled = false;
  const cancel = async (reason) => {
    if (cancelled) return;
    cancelled = true;
    try {
      await reader.cancel?.(reason);
    } catch {
      // Cancellation is best-effort after the local safety decision is made.
    }
  };

  try {
    for (;;) {
      if (signal?.aborted) {
        const reason = abortReason(signal);
        await cancel(reason);
        throw reason;
      }
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) {
        await cancel();
        throw new Error("Downloaded PDF stream valid nahi hai.");
      }
      total += value.byteLength;
      if (!Number.isSafeInteger(total) || total > maxBytes) {
        await cancel();
        throw new Error("Generated PDF allowed size se badi hai.");
      }
      if (value.byteLength) chunks.push(value);
    }
  } catch (error) {
    await cancel(error);
    if (signal?.aborted) throw abortReason(signal);
    throw error;
  } finally {
    try {
      reader.releaseLock?.();
    } catch {
      // The response is already safely consumed or cancelled.
    }
  }
  return { chunks, total };
}

export async function fetchThenDownload(
  url,
  filename,
  reservedWindow,
  {
    fetchImpl = globalThis.fetch?.bind(globalThis),
    BlobCtor = globalThis.Blob,
    createObjectURL = globalThis.URL?.createObjectURL?.bind(globalThis.URL),
    triggerDownload = defaultTriggerDownload,
    isIOS = detectIOS(),
    acknowledge,
    acknowledgementTimeoutMs = 5_000,
    setTimeoutFn = globalThis.setTimeout.bind(globalThis),
    clearTimeoutFn = globalThis.clearTimeout.bind(globalThis),
    AbortControllerImpl = globalThis.AbortController,
    maxBytes = MAX_OUTPUT_BYTES,
    corsFailure = isLikelyCorsFailure,
    signal,
  } = {},
) {
  const safeFilename = sanitizeFilename(filename);
  let response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw abortReason(signal);
    if (!corsFailure(error)) throw error;
    navigateWindow(reservedWindow, url);
    return {
      mode: "signed-url-fallback",
      objectUrl: null,
      acknowledged: false,
      fallbackUrl: url,
      filename: safeFilename,
    };
  }

  if (!response?.ok) {
    throw new Error("PDF download link accept nahi hua.");
  }
  const mime = (response.headers?.get?.("content-type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (mime !== "application/pdf") {
    throw new Error("Downloaded file valid PDF nahi hai.");
  }
  const rawLength = response.headers?.get?.("content-length");
  if (rawLength !== null && rawLength !== undefined) {
    const normalizedLength = String(rawLength).trim();
    if (!/^[0-9]+$/.test(normalizedLength)) {
      throw new Error("Downloaded PDF size valid nahi hai.");
    }
    const declaredLength = Number(normalizedLength);
    if (!Number.isSafeInteger(declaredLength) || declaredLength > maxBytes) {
      throw new Error("Generated PDF allowed size se badi hai.");
    }
  }

  const bounded = await readBoundedResponseBody(response, maxBytes, signal);
  if (bounded === null) {
    // Older browsers without streaming bodies must not allocate an unbounded
    // arrayBuffer. Keep the signed URL alive and hand it to the reserved tab;
    // the server output is deliberately not acknowledged/deleted here.
    navigateWindow(reservedWindow, url);
    return {
      mode: "signed-url-fallback",
      objectUrl: null,
      acknowledged: false,
      fallbackUrl: url,
      filename: safeFilename,
    };
  }
  const { chunks, total } = bounded;
  if (total < 5) {
    throw new Error("Downloaded PDF size valid nahi hai.");
  }
  const signature = new Uint8Array(5);
  let signatureOffset = 0;
  for (const chunk of chunks) {
    if (signatureOffset >= signature.byteLength) break;
    const take = Math.min(chunk.byteLength, signature.byteLength - signatureOffset);
    signature.set(chunk.subarray(0, take), signatureOffset);
    signatureOffset += take;
  }
  if (
    signature[0] !== 0x25 ||
    signature[1] !== 0x50 ||
    signature[2] !== 0x44 ||
    signature[3] !== 0x46 ||
    signature[4] !== 0x2d
  ) {
    throw new Error("Downloaded file PDF signature match nahi karti.");
  }
  if (typeof BlobCtor !== "function" || typeof createObjectURL !== "function") {
    throw new Error("Is browser mein local PDF banana possible nahi hai.");
  }

  const blob = new BlobCtor(chunks, { type: "application/pdf" });
  const objectUrl = createObjectURL(blob);
  triggerDownload(objectUrl, safeFilename);
  if (isIOS) navigateWindow(reservedWindow, objectUrl);
  else {
    try {
      reservedWindow?.close?.();
    } catch {
      // The visible local download remains available when a browser blocks close().
    }
  }

  const acknowledgement = beginBoundedAcknowledgement(acknowledge, {
    timeoutMs: acknowledgementTimeoutMs,
    setTimeoutFn,
    clearTimeoutFn,
    AbortControllerImpl,
  });
  return {
    mode: "local",
    objectUrl,
    acknowledgement,
    fallbackUrl: null,
    filename: safeFilename,
    size: total,
  };
}
