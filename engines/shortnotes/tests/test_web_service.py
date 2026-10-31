"""Behavioral contract for the authenticated web generation service."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from notesbot.blob_store import (
    BlobConflictError,
    BlobNotFoundError,
    BlobRef,
    JsonBlob,
)
from notesbot.errors import VisualFidelityError
from notesbot.web_jobs import JobManifest, JobSettings, transition
from notesbot.web_service import (
    MAX_INPUT_BYTES,
    RECORD_VERSION,
    BlobJobRepository,
    DispatchUnavailable,
    JobNotFound,
    JobNotReady,
    JobRecord,
    InternalRunDispatcher,
    PublicJob,
    StartRequest,
    ImportStartRequest,
    StoredJob,
    WebService,
    derive_owner,
    issue_run_token,
    job_record_from_dict,
    job_record_to_dict,
    normalize_signature,
    normalize_teacher_name,
    owned_paths,
    parse_start_request,
    run_job,
    sanitize_pdf_filename,
    verify_run_token,
)


STORE_HOST = "4mlmgdlkyhhksimu.private.blob.vercel-storage.com"
OWNER = "5f21f508e1d1160fd2237d41042fc013"
JOB_ID = "a" * 32
SOURCE_PATH = f"shortnotes/v1/inputs/{OWNER}/{JOB_ID}.pdf"
JOB_PATH = f"shortnotes/v1/jobs/{OWNER}/{JOB_ID}.json"
OUTPUT_PATH = f"shortnotes/v1/outputs/{OWNER}/{JOB_ID}.pdf"
SOURCE_URL = f"https://{STORE_HOST}/{SOURCE_PATH}"
JOB_URL = f"https://{STORE_HOST}/{JOB_PATH}"
OUTPUT_URL = f"https://{STORE_HOST}/{OUTPUT_PATH}"
CLAIM_ID = "c" * 32


def queued_record(*, dispatch_claim_id="", **settings_overrides) -> JobRecord:
    settings = {
        "teacher_name": "PW Faculty",
        "instruction": "Keep formula boxes compact.",
        "language": "hinglish",
        "subject": "chemistry",
        "watermark": True,
        "logo_watermark": True,
        **settings_overrides,
    }
    return JobRecord(
        version=RECORD_VERSION,
        manifest=JobManifest(
            id=JOB_ID,
            owner=OWNER,
            source_url=SOURCE_URL,
            state="queued",
            created_at=1_000,
            updated_at=1_000,
            output_url="",
            error="",
            dispatch_count=0,
        ),
        settings=JobSettings(**settings),
        filename="Electrochemistry lecture.pdf",
        store_host=STORE_HOST,
        dispatch_claim_id=dispatch_claim_id,
    )


class MemoryBlobStore:
    """Strict-enough in-memory implementation of the accepted Blob seam."""

    def __init__(self):
        self.store_host = STORE_HOST
        self.documents: dict[str, tuple[dict, str]] = {}
        self.pdf = b"%PDF-1.7\ninput"
        self.get_pdf_calls: list[tuple[str, int]] = []
        self.put_bytes_calls: list[tuple] = []
        self.deleted: list[list[str]] = []
        self.events: list[str] = []
        self.fail_get_pdf = False
        self.fail_put_bytes = False
        self.fail_delete = False
        self.fail_delete_urls: set[str] = set()
        self._etag = 0

    def _next_etag(self):
        self._etag += 1
        return f'"etag-{self._etag}"'

    def get_json(self, url, max_bytes):
        if url not in self.documents:
            raise BlobNotFoundError("missing")
        value, etag = self.documents[url]
        return JsonBlob(copy.deepcopy(value), etag)

    def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
        url = f"https://{self.store_host}/{pathname}"
        current = self.documents.get(url)
        if not allow_overwrite and current is not None:
            raise BlobConflictError("collision")
        if allow_overwrite and (current is None or current[1] != if_match):
            raise BlobConflictError("stale")
        etag = self._next_etag()
        self.documents[url] = (copy.deepcopy(value), etag)
        return BlobRef(url, pathname, etag)

    def get_pdf(self, url, max_bytes):
        self.events.append("fetch")
        self.get_pdf_calls.append((url, max_bytes))
        if self.fail_get_pdf:
            raise RuntimeError("provider secret from fetch")
        return self.pdf

    def put_bytes(self, pathname, data, content_type, allow_overwrite=False):
        self.events.append("upload")
        self.put_bytes_calls.append((pathname, data, content_type, allow_overwrite))
        if self.fail_put_bytes:
            raise RuntimeError("provider secret from upload")
        return BlobRef(OUTPUT_URL, pathname, self._next_etag())

    def delete(self, urls):
        self.events.append("delete:" + urls[0])
        self.deleted.append(list(urls))
        if self.fail_delete or any(url in self.fail_delete_urls for url in urls):
            raise RuntimeError("cleanup secret")
        for url in urls:
            self.documents.pop(url, None)


def start_request() -> StartRequest:
    return StartRequest(
        id=JOB_ID,
        source_url=SOURCE_URL,
        source_size=4_096,
        filename="Electrochemistry lecture.pdf",
        settings=queued_record().settings,
    )


def import_request(job_id=JOB_ID) -> ImportStartRequest:
    return ImportStartRequest(job_id, "https://public.example/lecture.pdf", queued_record().settings)


def importing_record(*, created_at=1_000) -> JobRecord:
    return JobRecord(
        RECORD_VERSION,
        JobManifest(JOB_ID, OWNER, SOURCE_URL, "importing", created_at, created_at, "", "", 0),
        queued_record().settings,
        "linked-lecture.pdf",
        STORE_HOST,
    )


def import_service(store, *, fetcher, dispatches=None, clock=lambda: 1_000):
    return WebService(
        BlobJobRepository(store), store,
        lambda *_args: (dispatches if dispatches is not None else []).append(_args),
        lambda *_args, **_kwargs: object(), lambda *_args, **_kwargs: b"%PDF-1.7\n%%EOF",
        run_secret="test-secret", clock=clock, acquire_remote_pdf=fetcher,
    )


def test_import_writes_create_only_to_exact_owned_private_input_path():
    store = MemoryBlobStore(); calls = []
    def fetcher(url, blob, path):
        calls.append((url, blob, path))
        return __import__("notesbot.remote_pdf", fromlist=["ImportedBlob"]).ImportedBlob(SOURCE_URL, 12, "remote.pdf")
    result = import_service(store, fetcher=fetcher).import_and_start(OWNER, import_request())
    assert calls == [("https://public.example/lecture.pdf", store, SOURCE_PATH)]
    assert result.job.filename == "remote.pdf" and result.job.state == "queued"


def test_importing_reservation_is_durable_before_dns_and_never_dispatches_early():
    store = MemoryBlobStore(); observed = []
    def fetcher(_url, _blob, _path):
        record = BlobJobRepository(store).load(OWNER, JOB_ID).record
        observed.append((record.manifest.state, record.dispatch_claim_id, record.manifest.dispatch_count))
        raise RuntimeError("acquisition unavailable")
    with pytest.raises(RuntimeError):
        import_service(store, fetcher=fetcher).import_and_start(OWNER, import_request())
    assert observed == [("importing", "", 0)]


def test_import_and_start_rejects_existing_terminal_job_id_before_remote_or_blob_work():
    store = MemoryBlobStore(); repository = BlobJobRepository(store)
    failed = replace(queued_record(), manifest=transition(queued_record().manifest, "failed", now=1_001, error=True))
    repository.create(failed); calls = []
    with pytest.raises(BlobConflictError):
        import_service(store, fetcher=lambda *_args: calls.append("fetch")).import_and_start(OWNER, import_request())
    assert calls == [] and store.deleted == []


def test_local_start_rejects_existing_importing_reservation_without_dispatch():
    store = MemoryBlobStore(); BlobJobRepository(store).create(importing_record()); dispatched = []
    with pytest.raises(BlobConflictError):
        import_service(store, fetcher=lambda *_args: None, dispatches=dispatched).start(OWNER, start_request())
    assert dispatched == []


def test_lost_import_start_response_recovers_existing_manifest_by_status_without_reimport():
    store = MemoryBlobStore(); calls = []
    def fetcher(*_args):
        calls.append("fetch")
        return __import__("notesbot.remote_pdf", fromlist=["ImportedBlob"]).ImportedBlob(SOURCE_URL, 9, "lecture.pdf")
    service = import_service(store, fetcher=fetcher)
    service.import_and_start(OWNER, import_request())
    assert service.status(OWNER, JOB_ID).state == "queued"
    assert calls == ["fetch"]


def test_stale_importing_status_expires_and_cleans_canonical_input_once():
    store = MemoryBlobStore(); BlobJobRepository(store).create(importing_record(created_at=1))
    service = import_service(store, fetcher=lambda *_args: None, clock=lambda: 1 + 121)
    assert service.status(OWNER, JOB_ID).state == "expired"
    assert store.deleted == [[SOURCE_URL]]


def test_link_value_is_never_saved_in_preferences_job_record_or_public_status():
    store = MemoryBlobStore(); secret_link = "https://public.example/lecture.pdf?token=secret"
    def fetcher(_url, _blob, _path):
        return __import__("notesbot.remote_pdf", fromlist=["ImportedBlob"]).ImportedBlob(SOURCE_URL, 9, "lecture.pdf")
    service = import_service(store, fetcher=fetcher)
    public = service.import_and_start(OWNER, ImportStartRequest(JOB_ID, secret_link, queued_record().settings)).job.to_dict()
    raw = job_record_to_dict(BlobJobRepository(store).load(OWNER, JOB_ID).record)
    assert secret_link not in repr(raw) and secret_link not in repr(public)


def test_imported_source_enters_unchanged_start_run_and_source_cleanup_lifecycle():
    store = MemoryBlobStore(); dispatched = []
    def fetcher(*_args):
        return __import__("notesbot.remote_pdf", fromlist=["ImportedBlob"]).ImportedBlob(SOURCE_URL, 9, "lecture.pdf")
    service = import_service(store, fetcher=fetcher, dispatches=dispatched)
    started = service.import_and_start(OWNER, import_request())
    terminal = service.run(OWNER, started.job.id)
    assert terminal.state == "complete" and dispatched and store.deleted == [[SOURCE_URL]]


def test_import_route_and_service_use_injected_fetcher_without_network_in_unit_tests():
    store = MemoryBlobStore(); calls = []
    def fetcher(url, blob, path):
        calls.append((url, blob is store, path))
        return __import__("notesbot.remote_pdf", fromlist=["ImportedBlob"]).ImportedBlob(SOURCE_URL, 9, "lecture.pdf")
    import_service(store, fetcher=fetcher).import_and_start(OWNER, import_request())
    assert calls == [("https://public.example/lecture.pdf", True, SOURCE_PATH)]


def test_concurrent_import_or_local_start_for_one_id_cannot_create_unowned_input():
    store = MemoryBlobStore(); entered = threading.Event(); release = threading.Event()
    def fetcher(*_args):
        entered.set()
        assert release.wait(1)
        raise RuntimeError("stop")
    service = import_service(store, fetcher=fetcher)
    with ThreadPoolExecutor(max_workers=2) as workers:
        importing = workers.submit(service.import_and_start, OWNER, import_request())
        assert entered.wait(1)  # reservation is durable while the other request races it
        local = workers.submit(service.start, OWNER, start_request())
        with pytest.raises(BlobConflictError): local.result(timeout=1)
        release.set()
        with pytest.raises(RuntimeError): importing.result(timeout=1)
    assert store.deleted == [[SOURCE_URL]]


def test_ambiguous_blob_write_settles_reservation_and_cleans_without_retry_or_overwrite():
    store = MemoryBlobStore(); calls = []
    def ambiguous(*_args): calls.append("put"); raise RuntimeError("ambiguous put")
    service = import_service(store, fetcher=ambiguous)
    with pytest.raises(RuntimeError): service.import_and_start(OWNER, import_request())
    stored = BlobJobRepository(store).load(OWNER, JOB_ID)
    assert calls == ["put"] and stored.record.manifest.state == "failed" and store.deleted == [[SOURCE_URL]]


def test_owner_paths_and_internal_token_have_fixed_cross_runtime_vectors():
    nonce = "0123456789abcdef0123456789abcdef"
    assert derive_owner("test-secret", nonce) == OWNER
    paths = owned_paths(OWNER, JOB_ID)
    assert (paths.input, paths.job, paths.output) == (
        SOURCE_PATH,
        JOB_PATH,
        OUTPUT_PATH,
    )

    token = issue_run_token("test-secret", OWNER, JOB_ID, now=1_000)
    assert token == (
        "v1.1300.5f21f508e1d1160fd2237d41042fc013."
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa."
        "cRWHOr7tXCZP5qfDSgxX8Ru1RrcFWYWj8JDwc0IaeIE"
    )
    assert verify_run_token(token, "test-secret", OWNER, JOB_ID, now=1_299)
    assert not verify_run_token(token, "test-secret", OWNER, JOB_ID, now=1_300)
    assert not verify_run_token(token + "x", "test-secret", OWNER, JOB_ID, now=1_001)
    assert not verify_run_token(token, "test-secret", "b" * 32, JOB_ID, now=1_001)


def test_normalization_and_start_parser_match_the_browser_contract():
    assert normalize_teacher_name("  PW\n  Faculty  ") == "PW Faculty"
    assert normalize_signature("PW   Faculty") == "by PW Faculty"
    assert normalize_signature("BY  PW Faculty") == "BY PW Faculty"
    assert sanitize_pdf_filename(" ../../Electro:\x00 notes.PDF ") == "Electro notes.pdf"

    parsed = parse_start_request(
        {
            "requestId": JOB_ID,
            "inputUrl": SOURCE_URL,
            "inputSize": MAX_INPUT_BYTES,
            "filename": " ../../Electro:\x00 notes.PDF ",
            "teacherName": "  PW\n Faculty ",
            "subject": "chemistry",
            "language": "hinglish",
            "instructions": "Keep formula boxes compact.",
            "watermark": True,
            "logoWatermark": False,
        },
        owner=OWNER,
        store_host=STORE_HOST,
    )
    assert parsed.id == JOB_ID
    assert parsed.source_size == MAX_INPUT_BYTES
    assert parsed.filename == "Electro notes.pdf"
    assert parsed.settings.teacher_name == "PW Faculty"
    assert (parsed.settings.watermark, parsed.settings.logo_watermark) == (True, False)


def test_start_parser_accepts_exact_legacy_payload_and_defaults_logo_off():
    payload = {
        "requestId": JOB_ID,
        "inputUrl": SOURCE_URL,
        "inputSize": 1,
        "filename": "lecture.pdf",
        "teacherName": "Teacher",
        "subject": "auto",
        "language": "auto",
        "instructions": "",
        "watermark": True,
    }

    parsed = parse_start_request(payload, owner=OWNER, store_host=STORE_HOST)

    assert parsed.settings.watermark is True
    assert parsed.settings.logo_watermark is False


def test_start_parser_accepts_a_category_and_defaults_to_neet_jee():
    payload = {
        "requestId": JOB_ID,
        "inputUrl": SOURCE_URL,
        "inputSize": 1,
        "filename": "lecture.pdf",
        "teacherName": "Teacher",
        "subject": "auto",
        "language": "auto",
        "instructions": "",
        "watermark": True,
        "logoWatermark": False,
    }
    assert parse_start_request(payload, owner=OWNER, store_host=STORE_HOST).settings.category == "neet_jee"
    parsed = parse_start_request({**payload, "category": "foundation"}, owner=OWNER, store_host=STORE_HOST)
    assert parsed.settings.category == "foundation"
    parsed = parse_start_request({**payload, "category": "jee"}, owner=OWNER, store_host=STORE_HOST)
    assert parsed.settings.category == "neet_jee"   # a pre-merge id
    with pytest.raises(ValueError):
        parse_start_request({**payload, "category": "ias"}, owner=OWNER, store_host=STORE_HOST)


def test_record_keeps_category_and_older_records_default_to_neet_jee():
    value = job_record_to_dict(queued_record(category="foundation"))
    assert value["settings"]["category"] == "foundation"
    restored = job_record_from_dict(value, store_host=STORE_HOST, expected_owner=OWNER, expected_id=JOB_ID)
    assert restored.settings.category == "foundation"
    value["settings"].pop("category")
    restored = job_record_from_dict(value, store_host=STORE_HOST, expected_owner=OWNER, expected_id=JOB_ID)
    assert restored.settings.category == "neet_jee"
    value["settings"]["category"] = "jee"   # stored before NEET and JEE merged
    restored = job_record_from_dict(value, store_host=STORE_HOST, expected_owner=OWNER, expected_id=JOB_ID)
    assert restored.settings.category == "neet_jee"
    value["settings"]["category"] = "ias"
    with pytest.raises(ValueError):
        job_record_from_dict(value, store_host=STORE_HOST, expected_owner=OWNER, expected_id=JOB_ID)


@pytest.mark.parametrize(
    "change",
    [
        {"requestId": "A" * 32},
        {"inputUrl": f"https://evil.example/{SOURCE_PATH}"},
        {"inputUrl": f"https://{STORE_HOST}/shortnotes/v1/inputs/{'b' * 32}/{JOB_ID}.pdf"},
        {"inputSize": True},
        {"inputSize": MAX_INPUT_BYTES + 1},
        {"teacherName": " "},
        {"teacherName": "x" * 121},
        {"subject": "history"},
        {"language": "french"},
        {"instructions": "x" * 1_001},
        {"watermark": "yes"},
        {"logoWatermark": "yes"},
        {"extra": "not accepted"},
    ],
)
def test_start_parser_rejects_wrong_types_limits_enums_and_paths(change):
    payload = {
        "requestId": JOB_ID,
        "inputUrl": SOURCE_URL,
        "inputSize": 1,
        "filename": "lecture.pdf",
        "teacherName": "Teacher",
        "subject": "auto",
        "language": "auto",
        "instructions": "",
        "watermark": True,
        "logoWatermark": False,
    }
    payload.update(change)
    with pytest.raises(ValueError):
        parse_start_request(payload, owner=OWNER, store_host=STORE_HOST)


def test_versioned_record_round_trip_has_exact_keys_and_no_secret_fields():
    record = queued_record()
    value = job_record_to_dict(record)
    assert set(value) == {
        "version", "dispatch_claim_id", "manifest", "settings", "filename",
    }
    assert value["dispatch_claim_id"] == ""
    assert set(value["manifest"]) == {
        "id", "owner", "source_url", "state", "created_at", "updated_at",
        "output_url", "error", "dispatch_count",
    }
    assert set(value["settings"]) == {
        "teacher_name", "instruction", "language", "subject", "watermark",
        "logo_watermark", "category",
    }
    assert "secret" not in repr(value).lower()
    assert job_record_from_dict(
        value,
        store_host=STORE_HOST,
        expected_owner=OWNER,
        expected_id=JOB_ID,
    ) == record


def test_record_decoder_accepts_exact_legacy_settings_and_defaults_logo_off():
    value = job_record_to_dict(queued_record(logo_watermark=False))
    value["settings"].pop("logo_watermark")
    value["settings"].pop("category")  # legacy records predate both keys

    restored = job_record_from_dict(
        value,
        store_host=STORE_HOST,
        expected_owner=OWNER,
        expected_id=JOB_ID,
    )

    assert restored.settings.logo_watermark is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(version=2),
        lambda value: value.update(extra=True),
        lambda value: value.pop("dispatch_claim_id"),
        lambda value: value.update(dispatch_claim_id="C" * 32),
        lambda value: value.update(dispatch_claim_id="c" * 31),
        lambda value: value["manifest"].update(owner="b" * 32),
        lambda value: value["manifest"].update(id="b" * 32),
        lambda value: value["manifest"].update(created_at=True),
        lambda value: value["settings"].update(subject="History"),
        lambda value: value["settings"].update(language="ENGLISH"),
        lambda value: value["settings"].update(teacher_name="  Teacher  "),
        lambda value: value.update(filename="../lecture.pdf"),
        lambda value: value["settings"].update(extra="x"),
    ],
)
def test_record_decoder_rejects_version_extra_types_enums_and_identity(mutation):
    value = job_record_to_dict(queued_record())
    mutation(value)
    with pytest.raises(ValueError):
        job_record_from_dict(
            value,
            store_host=STORE_HOST,
            expected_owner=OWNER,
            expected_id=JOB_ID,
        )


def test_record_decoder_requires_a_claim_for_running_and_terminal_states():
    claimed = queued_record(dispatch_claim_id=CLAIM_ID)
    running = replace(
        claimed,
        manifest=transition(claimed.manifest, "running", now=1_001),
    )
    complete = replace(
        running,
        manifest=transition(
            running.manifest, "complete", now=1_002, output_url=OUTPUT_URL
        ),
    )

    for record in (running, complete):
        value = job_record_to_dict(record)
        value["dispatch_claim_id"] = ""
        with pytest.raises(ValueError):
            job_record_from_dict(
                value,
                store_host=STORE_HOST,
                expected_owner=OWNER,
                expected_id=JOB_ID,
            )


def test_blob_repository_creation_is_atomic_and_cas_rejects_stale_claims():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    record = queued_record()

    first, created = repository.create(record)
    second, duplicate_created = repository.create(record)
    assert created is True
    assert duplicate_created is False
    assert second.record == first.record

    claimed_record = replace(record, dispatch_claim_id=CLAIM_ID)
    claimed = repository.replace(first, claimed_record)
    running_record = replace(
        claimed.record,
        manifest=transition(claimed.record.manifest, "running", now=1_001),
    )
    running = repository.replace(claimed, running_record)
    with pytest.raises(BlobConflictError):
        repository.replace(claimed, running_record)
    assert repository.load(OWNER, JOB_ID) == running


def test_create_treats_sanitized_bad_request_as_duplicate_only_after_exact_read():
    class BadRequestCollisionStore(MemoryBlobStore):
        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            url = f"https://{self.store_host}/{pathname}"
            if not allow_overwrite and url in self.documents:
                raise RuntimeError("Blob storage operation failed")
            return super().put_json(
                pathname, value, allow_overwrite=allow_overwrite, if_match=if_match
            )

    store = BadRequestCollisionStore()
    repository = BlobJobRepository(store)
    first, created = repository.create(queued_record())
    duplicate, duplicate_created = repository.create(queued_record())

    assert created is True
    assert duplicate_created is False
    assert duplicate == first


def test_start_claims_and_dispatches_after_create_commits_but_response_fails():
    """A committed create must not be mistaken for an inert duplicate."""

    class CommitThenFailStore(MemoryBlobStore):
        def __init__(self):
            super().__init__()
            self.failed_create_response = False

        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            ref = super().put_json(
                pathname, value, allow_overwrite=allow_overwrite, if_match=if_match
            )
            if not allow_overwrite and not self.failed_create_response:
                self.failed_create_response = True
                raise RuntimeError("Blob storage returned an invalid response")
            return ref

    store = CommitThenFailStore()
    dispatched = []
    service = WebService(
        BlobJobRepository(store),
        store,
        lambda owner, job_id, token: dispatched.append((owner, job_id, token)),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_000,
    )

    result = service.start(OWNER, start_request())

    assert result.created is False
    assert len(dispatched) == 1
    stored = BlobJobRepository(store).load(OWNER, JOB_ID).record
    assert len(stored.dispatch_claim_id) == 32
    assert stored.dispatch_claim_id.isascii()
    assert all(character in "0123456789abcdef" for character in stored.dispatch_claim_id)


def test_ambiguous_dispatch_claim_write_dispatches_only_when_reloaded_id_matches():
    class CommitClaimThenFailStore(MemoryBlobStore):
        def __init__(self):
            super().__init__()
            self.claim_response_failed = False
            self.get_json_calls = 0

        def get_json(self, url, max_bytes):
            self.get_json_calls += 1
            return super().get_json(url, max_bytes)

        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            ref = super().put_json(
                pathname, value, allow_overwrite=allow_overwrite, if_match=if_match
            )
            if (
                allow_overwrite
                and value.get("dispatch_claim_id") == CLAIM_ID
                and not self.claim_response_failed
            ):
                self.claim_response_failed = True
                raise RuntimeError("Blob storage is unavailable")
            return ref

    store = CommitClaimThenFailStore()
    dispatched = []
    service = WebService(
        BlobJobRepository(store),
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_000,
        claim_factory=lambda: CLAIM_ID,
    )

    result = service.start(OWNER, start_request())

    assert result.created is True
    assert len(dispatched) == 1
    assert store.get_json_calls == 1
    assert BlobJobRepository(store).load(OWNER, JOB_ID).record.dispatch_claim_id == CLAIM_ID


def test_ambiguous_dispatch_claim_write_does_not_dispatch_a_different_winner():
    rival_claim = "d" * 32

    class LosingClaimStore(MemoryBlobStore):
        def __init__(self):
            super().__init__()
            self.get_json_calls = 0

        def get_json(self, url, max_bytes):
            self.get_json_calls += 1
            return super().get_json(url, max_bytes)

        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            if allow_overwrite and value.get("dispatch_claim_id") == CLAIM_ID:
                url = f"https://{self.store_host}/{pathname}"
                rival = copy.deepcopy(value)
                rival["dispatch_claim_id"] = rival_claim
                self.documents[url] = (rival, self._next_etag())
                raise RuntimeError("ambiguous claim response")
            return super().put_json(
                pathname, value, allow_overwrite=allow_overwrite, if_match=if_match
            )

    store = LosingClaimStore()
    dispatched = []
    service = WebService(
        BlobJobRepository(store),
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_000,
        claim_factory=lambda: CLAIM_ID,
    )

    result = service.start(OWNER, start_request())

    assert result.job.state == "queued"
    assert dispatched == []
    assert store.get_json_calls == 1
    assert BlobJobRepository(store).load(OWNER, JOB_ID).record.dispatch_claim_id == rival_claim


@pytest.mark.parametrize("remove_record", [False, True], ids=["empty", "missing"])
def test_ambiguous_dispatch_claim_without_a_winner_rethrows_without_dispatch(
    remove_record,
):
    class FailedClaimStore(MemoryBlobStore):
        def __init__(self):
            super().__init__()
            self.get_json_calls = 0

        def get_json(self, url, max_bytes):
            self.get_json_calls += 1
            return super().get_json(url, max_bytes)

        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            if allow_overwrite and value.get("dispatch_claim_id") == CLAIM_ID:
                if remove_record:
                    self.documents.pop(f"https://{self.store_host}/{pathname}", None)
                raise RuntimeError("Blob storage is unavailable")
            return super().put_json(
                pathname, value, allow_overwrite=allow_overwrite, if_match=if_match
            )

    store = FailedClaimStore()
    dispatched = []
    service = WebService(
        BlobJobRepository(store),
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_000,
        claim_factory=lambda: CLAIM_ID,
    )

    with pytest.raises(RuntimeError, match="Blob storage is unavailable"):
        service.start(OWNER, start_request())

    assert dispatched == []
    assert store.get_json_calls == 1


def test_simultaneous_starts_cas_distinct_claims_and_dispatch_exactly_once():
    class LockedMemoryBlobStore(MemoryBlobStore):
        def __init__(self):
            super().__init__()
            self.write_lock = threading.Lock()

        def put_json(self, pathname, value, allow_overwrite=False, *, if_match=None):
            with self.write_lock:
                return super().put_json(
                    pathname,
                    value,
                    allow_overwrite=allow_overwrite,
                    if_match=if_match,
                )

    store = LockedMemoryBlobStore()
    repository = BlobJobRepository(store)
    claim_barrier = threading.Barrier(2)

    class BarrierRepository:
        def load(self, owner, job_id):
            return repository.load(owner, job_id)

        def create(self, record):
            return repository.create(record)

        def replace(self, current, record):
            if record.manifest.state == "queued" and record.dispatch_claim_id:
                claim_barrier.wait(timeout=2)
            return repository.replace(current, record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    dispatched = []

    def make_service(claim_id):
        return WebService(
            BarrierRepository(),
            store,
            lambda *args: dispatched.append(args),
            lambda *_args, **_kwargs: object(),
            lambda *_args, **_kwargs: b"%PDF-1.7\nout",
            run_secret="test-secret",
            clock=lambda: 1_000,
            claim_factory=lambda: claim_id,
        )

    services = (make_service("c" * 32), make_service("d" * 32))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda service: service.start(OWNER, start_request()), services)
        )

    assert sorted(result.created for result in results) == [False, True]
    assert len(dispatched) == 1
    stored = repository.load(OWNER, JOB_ID).record
    assert stored.dispatch_claim_id in {"c" * 32, "d" * 32}


def test_start_dispatches_only_atomic_winner_and_duplicate_returns_existing():
    store = MemoryBlobStore()
    dispatched = []
    service = WebService(
        BlobJobRepository(store),
        store,
        lambda owner, job_id, token: dispatched.append((owner, job_id, token)),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_000,
    )

    first = service.start(OWNER, start_request())
    second = service.start(OWNER, start_request())

    assert first.created is True
    assert second.created is False
    assert first.job.state == second.job.state == "queued"
    assert len(dispatched) == 1
    assert dispatched[0][:2] == (OWNER, JOB_ID)
    assert verify_run_token(dispatched[0][2], "test-secret", OWNER, JOB_ID, now=1_000)


def test_stale_claim_is_cas_rotated_and_redispatched_by_duplicate_start():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    dispatched = []
    service = WebService(
        repository,
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_031,
        claim_factory=lambda: "d" * 32,
    )

    result = service.start(OWNER, start_request())

    stored = repository.load(OWNER, JOB_ID).record
    assert result.created is False
    assert result.job.state == "queued"
    assert len(dispatched) == 1
    assert dispatched[0][:2] == (OWNER, JOB_ID)
    assert stored.dispatch_claim_id != CLAIM_ID
    assert stored.manifest.updated_at == 1_031


def test_status_recovers_a_stale_claim_but_not_a_fresh_claim():
    now = [1_010]
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    dispatched = []
    service = WebService(
        repository,
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: now[0],
        claim_factory=lambda: "d" * 32,
    )

    assert service.status(OWNER, JOB_ID).state == "queued"
    assert dispatched == []

    now[0] = 1_031
    assert service.status(OWNER, JOB_ID).state == "queued"
    assert len(dispatched) == 1


def test_stale_delivery_recovery_has_a_finite_attempt_and_age_cap():
    now = [1_000]
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    dispatched = []
    service = WebService(
        repository,
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: now[0],
        claim_factory=lambda: f"{len(dispatched) + 1:032x}",
    )

    assert service.start(OWNER, start_request()).job.state == "queued"
    assert len(dispatched) == 1
    for timestamp in (1_031, 1_062):
        now[0] = timestamp
        assert service.status(OWNER, JOB_ID).state == "queued"
    assert len(dispatched) == 3

    now[0] = 1_093
    assert service.status(OWNER, JOB_ID).state == "expired"
    assert len(dispatched) == 3
    assert store.deleted == [[SOURCE_URL]]

    now[0] = 2_000
    assert service.status(OWNER, JOB_ID).state == "expired"
    assert len(dispatched) == 3


def test_claim_older_than_recovery_deadline_expires_without_redispatch():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    dispatched = []
    service = WebService(
        repository,
        store,
        lambda *args: dispatched.append(args),
        lambda *_args, **_kwargs: object(),
        lambda *_args, **_kwargs: b"%PDF-1.7\nout",
        run_secret="test-secret",
        clock=lambda: 1_301,
        claim_factory=lambda: "d" * 32,
    )

    assert service.status(OWNER, JOB_ID).state == "expired"
    assert dispatched == []
    assert store.deleted == [[SOURCE_URL]]


def test_known_dispatch_failure_is_not_retried_or_run_inline():
    store = MemoryBlobStore()
    attempts = 0

    def unavailable(*_args):
        nonlocal attempts
        attempts += 1
        raise DispatchUnavailable("network detail")

    service = WebService(
        BlobJobRepository(store), store, unavailable,
        lambda *_args, **_kwargs: pytest.fail("must not run inline"),
        lambda *_args, **_kwargs: pytest.fail("must not render inline"),
        run_secret="test-secret", clock=lambda: 1_000,
    )
    with pytest.raises(DispatchUnavailable):
        service.start(OWNER, start_request())
    assert attempts == 1
    expired = BlobJobRepository(store).load(OWNER, JOB_ID).record
    assert expired.manifest.state == "expired"
    assert expired.dispatch_claim_id
    assert store.deleted == [[SOURCE_URL]]

    duplicate = service.start(OWNER, start_request())
    assert duplicate.created is False
    assert duplicate.job.state == "expired"
    assert attempts == 1


def test_dispatch_error_does_not_expire_or_delete_input_after_run_claimed_it():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    attempts = 0

    def delivered_then_failed(owner, job_id, _token):
        nonlocal attempts
        attempts += 1
        current = repository.load(owner, job_id)
        assert current.record.dispatch_claim_id
        running_record = replace(
            current.record,
            manifest=transition(current.record.manifest, "running", now=1_001),
        )
        repository.replace(current, running_record)
        raise DispatchUnavailable("response failed after delivery")

    service = WebService(
        repository,
        store,
        delivered_then_failed,
        lambda *_args, **_kwargs: pytest.fail("dispatcher owns only delivery"),
        lambda *_args, **_kwargs: pytest.fail("dispatcher owns only delivery"),
        run_secret="test-secret",
        clock=lambda: 1_001,
    )

    result = service.start(OWNER, start_request())

    assert result.created is True
    assert result.job.state == "running"
    assert attempts == 1
    assert store.deleted == []
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "running"


def test_completed_non_2xx_run_response_expires_a_stale_running_record():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)

    def completed_with_error(owner, job_id, _token):
        current = repository.load(owner, job_id)
        running_record = replace(
            current.record,
            manifest=transition(current.record.manifest, "running", now=1_002),
        )
        repository.replace(current, running_record)
        raise DispatchUnavailable("run returned 503", request_finished=True)

    service = WebService(
        repository,
        store,
        completed_with_error,
        lambda *_args, **_kwargs: pytest.fail("dispatcher owns only delivery"),
        lambda *_args, **_kwargs: pytest.fail("dispatcher owns only delivery"),
        run_secret="test-secret",
        clock=lambda: 1_002,
    )

    with pytest.raises(DispatchUnavailable):
        service.start(OWNER, start_request())

    stored = repository.load(OWNER, JOB_ID).record
    assert stored.manifest.state == "expired"
    assert stored.manifest.dispatch_count == 1
    assert store.deleted == [[SOURCE_URL]]


def test_run_claims_running_before_fetch_and_calls_each_stage_once():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    calls = []

    def generate(pdf, instruction, *, language, subject_hint):
        stored = repository.load(OWNER, JOB_ID).record.manifest
        calls.append(("generate", pdf, instruction, language, subject_hint, stored.state))
        return {"notes": True}

    def render(notes, *, signature, watermark, logo_watermark):
        calls.append(("render", notes, signature, watermark, logo_watermark))
        return b"%PDF-1.7\noutput"

    service = WebService(
        repository, store, lambda *_args: None, generate, render,
        run_secret="test-secret", clock=lambda: 1_010,
    )
    result = service.run(OWNER, JOB_ID)

    assert result.state == "complete"
    assert calls == [
        ("generate", store.pdf, "Keep formula boxes compact.", "hinglish", "chemistry", "running"),
        ("render", {"notes": True}, "by PW Faculty", True, True),
    ]
    assert store.get_pdf_calls == [(SOURCE_URL, MAX_INPUT_BYTES)]
    assert store.put_bytes_calls == [
        (OUTPUT_PATH, b"%PDF-1.7\noutput", "application/pdf", False),
    ]
    assert store.deleted == [[SOURCE_URL]]
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "complete"
    assert repository.load(OWNER, JOB_ID).record.filename == "Electrochemistry lecture Concise Notes.pdf"


def test_run_rejects_an_unclaimed_queued_record_without_model_or_cleanup_work():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record())
    service = WebService(
        repository,
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: pytest.fail("unclaimed job generated"),
        lambda *_args, **_kwargs: pytest.fail("unclaimed job rendered"),
        run_secret="test-secret",
        clock=lambda: 1_010,
    )

    with pytest.raises(JobNotReady):
        service.run(OWNER, JOB_ID)

    assert store.get_pdf_calls == []
    assert store.put_bytes_calls == []
    assert store.deleted == []


@pytest.mark.parametrize("failure", ["fetch", "generate", "render", "upload"])
def test_run_job_deletes_input_and_returns_only_generic_failure_for_every_stage(failure):
    store = MemoryBlobStore()
    running = replace(
        queued_record(dispatch_claim_id=CLAIM_ID),
        manifest=transition(
            queued_record(dispatch_claim_id=CLAIM_ID).manifest,
            "running",
            now=1_001,
        ),
    )
    if failure == "fetch":
        store.fail_get_pdf = True
    if failure == "upload":
        store.fail_put_bytes = True

    def generate(*_args, **_kwargs):
        if failure == "generate":
            raise RuntimeError("provider-api-key=super-secret")
        return {"notes": True}

    def render(*_args, **_kwargs):
        if failure == "render":
            raise RuntimeError("prompt and PDF text super-secret")
        return b"%PDF-1.7\noutput"

    result = run_job(running, store, generate, render, now=1_002)
    assert result.state == "failed"
    assert result.error == "Generation failed"
    assert "secret" not in result.error.lower()
    assert store.deleted[-1] == [SOURCE_URL]


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (VisualFidelityError("inventory_incomplete"), "inventory_incomplete"),
        (RuntimeError("provider-api-key=super-secret"), "generate_failed"),
    ],
)
def test_run_job_logs_only_a_fixed_failure_category(failure, category, capsys):
    store = MemoryBlobStore()
    running = replace(
        queued_record(dispatch_claim_id=CLAIM_ID),
        manifest=transition(
            queued_record(dispatch_claim_id=CLAIM_ID).manifest,
            "running",
            now=1_001,
        ),
    )

    def generate(*_args, **_kwargs):
        raise failure

    result = run_job(running, store, generate, lambda *_args: b"", now=1_002)

    assert result.state == "failed"
    output = capsys.readouterr().out
    # Category plus exception CLASS names only; never message content.
    assert output.startswith(f"[notesbot.visual-error] category={category} exc=")
    assert f"{type(failure).__module__}.{type(failure).__qualname__}" in output
    assert "secret" not in output.lower()


def test_cleanup_failure_does_not_mask_a_successful_generation():
    store = MemoryBlobStore()
    store.fail_delete = True
    running = replace(
        queued_record(dispatch_claim_id=CLAIM_ID, subject="auto"),
        manifest=transition(
            queued_record(dispatch_claim_id=CLAIM_ID, subject="auto").manifest,
            "running",
            now=1_001,
        ),
    )
    result = run_job(
        running,
        store,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        now=1_002,
    )
    assert result.state == "complete"
    assert store.deleted == [[SOURCE_URL]]


@pytest.mark.parametrize("state", ["running", "complete", "failed", "expired"])
def test_duplicate_run_never_generates_and_retries_only_terminal_source_cleanup(state):
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    record = queued_record(dispatch_claim_id=CLAIM_ID)
    repository.create(record)
    stored = repository.load(OWNER, JOB_ID)
    running = replace(record, manifest=transition(record.manifest, "running", now=1_001))
    stored = repository.replace(stored, running)
    if state == "complete":
        terminal = replace(running, manifest=transition(running.manifest, state, now=1_002, output_url=OUTPUT_URL))
        repository.replace(stored, terminal)
    elif state == "failed":
        terminal = replace(running, manifest=transition(running.manifest, state, now=1_002, error=RuntimeError("x")))
        repository.replace(stored, terminal)
    elif state == "expired":
        terminal = replace(running, manifest=transition(running.manifest, state, now=1_002))
        repository.replace(stored, terminal)

    service = WebService(
        repository, store, lambda *_args: None,
        lambda *_args, **_kwargs: pytest.fail("duplicate generated"),
        lambda *_args, **_kwargs: pytest.fail("duplicate rendered"),
        run_secret="test-secret", clock=lambda: 1_010,
    )
    assert service.run(OWNER, JOB_ID).state == state
    assert store.get_pdf_calls == []
    assert store.deleted == ([] if state == "running" else [[SOURCE_URL]])


def test_losing_parallel_cas_claim_returns_winner_without_generation():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    initial, _ = repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class RacingRepository:
        def load(self, owner, job_id):
            return repository.load(owner, job_id)

        def replace(self, current, record):
            if record.manifest.state == "running":
                winner = replace(
                    initial.record,
                    manifest=transition(initial.record.manifest, "running", now=1_001),
                )
                repository.replace(initial, winner)
                raise BlobConflictError("lost race")
            return repository.replace(current, record)

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    service = WebService(
        RacingRepository(), store, lambda *_args: None,
        lambda *_args, **_kwargs: pytest.fail("loser generated"),
        lambda *_args, **_kwargs: pytest.fail("loser rendered"),
        run_secret="test-secret", clock=lambda: 1_010,
    )
    assert service.run(OWNER, JOB_ID).state == "running"
    assert store.get_pdf_calls == []
    assert store.deleted == []


def test_generic_terminal_persist_failure_keeps_possibly_committed_output():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class TerminalFailureRepository:
        def load(self, owner, job_id):
            return repository.load(owner, job_id)

        def replace(self, current, record):
            if record.manifest.state == "complete":
                raise RuntimeError("storage credential=super-secret")
            return repository.replace(current, record)

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    service = WebService(
        TerminalFailureRepository(),
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        run_secret="test-secret",
        clock=lambda: 1_010,
    )
    with pytest.raises(RuntimeError, match="storage is unavailable") as caught:
        service.run(OWNER, JOB_ID)
    assert "super-secret" not in str(caught.value)
    assert store.deleted == [[SOURCE_URL]]
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "running"


def test_definitive_terminal_cas_conflict_deletes_the_true_orphan_output():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class TerminalConflictRepository:
        def load(self, owner, job_id):
            return repository.load(owner, job_id)

        def replace(self, current, record):
            if record.manifest.state == "complete":
                raise BlobConflictError("terminal CAS lost")
            return repository.replace(current, record)

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    service = WebService(
        TerminalConflictRepository(),
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        run_secret="test-secret",
        clock=lambda: 1_010,
    )

    result = service.run(OWNER, JOB_ID)

    assert result.state == "running"
    assert store.deleted == [[SOURCE_URL], [OUTPUT_URL]]


def test_terminal_commit_with_lost_response_is_accepted_without_deleting_output():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class CommitTerminalThenFailRepository:
        def load(self, owner, job_id):
            return repository.load(owner, job_id)

        def replace(self, current, record):
            persisted = repository.replace(current, record)
            if record.manifest.state == "complete":
                raise RuntimeError("terminal response was lost")
            return persisted

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    service = WebService(
        CommitTerminalThenFailRepository(),
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        run_secret="test-secret",
        clock=lambda: 1_010,
    )

    result = service.run(OWNER, JOB_ID)

    assert result.state == "complete"
    assert repository.load(OWNER, JOB_ID).record.manifest == result
    assert store.deleted == [[SOURCE_URL]]


def test_indeterminate_terminal_reload_is_bounded_and_keeps_possible_output():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class IndeterminateTerminalRepository:
        def __init__(self):
            self.load_calls = 0
            self.terminal_failed = False

        def load(self, owner, job_id):
            self.load_calls += 1
            if self.terminal_failed:
                raise RuntimeError("read response unavailable")
            return repository.load(owner, job_id)

        def replace(self, current, record):
            if record.manifest.state == "complete":
                self.terminal_failed = True
                raise RuntimeError("write response unavailable")
            return repository.replace(current, record)

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    uncertain = IndeterminateTerminalRepository()
    service = WebService(
        uncertain,
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        run_secret="test-secret",
        clock=lambda: 1_010,
    )

    with pytest.raises(RuntimeError, match="storage is unavailable"):
        service.run(OWNER, JOB_ID)

    assert uncertain.load_calls == 3
    assert store.deleted == [[SOURCE_URL]]


def test_stale_terminal_read_then_error_is_bounded_and_keeps_possible_output():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record(dispatch_claim_id=CLAIM_ID))

    class StaleThenUnavailableRepository:
        def __init__(self):
            self.load_calls = 0
            self.terminal_failed = False

        def load(self, owner, job_id):
            self.load_calls += 1
            if self.terminal_failed and self.load_calls == 3:
                raise RuntimeError("recovery read unavailable")
            return repository.load(owner, job_id)

        def replace(self, current, record):
            if record.manifest.state == "complete":
                self.terminal_failed = True
                raise RuntimeError("terminal response ambiguous")
            return repository.replace(current, record)

        def create(self, record):
            return repository.create(record)

        def delete(self, owner, job_id):
            return repository.delete(owner, job_id)

    uncertain = StaleThenUnavailableRepository()
    service = WebService(
        uncertain,
        store,
        lambda *_args: None,
        lambda *_args, **_kwargs: {"notes": True},
        lambda *_args, **_kwargs: b"%PDF-1.7\noutput",
        run_secret="test-secret",
        clock=lambda: 1_010,
    )

    with pytest.raises(RuntimeError, match="storage is unavailable"):
        service.run(OWNER, JOB_ID)

    assert uncertain.load_calls == 3
    assert store.deleted == [[SOURCE_URL]]


def test_download_ack_requires_literal_true_and_deletes_output_before_manifest():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    initial, _ = repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    running_record = replace(
        initial.record,
        manifest=transition(initial.record.manifest, "running", now=1_001),
    )
    running = repository.replace(initial, running_record)
    complete_record = replace(
        running.record,
        manifest=transition(running.record.manifest, "complete", now=1_002, output_url=OUTPUT_URL),
    )
    repository.replace(running, complete_record)
    service = WebService(
        repository, store, lambda *_args: None, lambda *_args: None,
        lambda *_args: None, run_secret="test-secret", clock=lambda: 1_010,
    )

    with pytest.raises(ValueError):
        service.downloaded(OWNER, JOB_ID, fetched=False)
    assert store.deleted == []

    receipt = service.downloaded(OWNER, JOB_ID, fetched=True)
    assert receipt.deleted is True
    assert store.events[-3:] == [
        f"delete:{SOURCE_URL}", f"delete:{OUTPUT_URL}", f"delete:{JOB_URL}",
    ]
    assert repository.load(OWNER, JOB_ID) is None

    repeated = service.downloaded(OWNER, JOB_ID, fetched=True)
    assert repeated.deleted is False
    assert store.deleted == [[SOURCE_URL], [OUTPUT_URL], [JOB_URL]]


def test_output_delete_failure_keeps_complete_manifest_for_safe_receipt_retry():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    initial, _ = repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    running_record = replace(
        initial.record,
        manifest=transition(initial.record.manifest, "running", now=1_001),
    )
    running = repository.replace(initial, running_record)
    complete_record = replace(
        running.record,
        manifest=transition(
            running.record.manifest, "complete", now=1_002, output_url=OUTPUT_URL
        ),
    )
    repository.replace(running, complete_record)
    store.fail_delete_urls.add(OUTPUT_URL)
    service = WebService(
        repository, store, lambda *_args: None, lambda *_args: None,
        lambda *_args: None, run_secret="test-secret", clock=lambda: 1_010,
    )

    with pytest.raises(RuntimeError):
        service.downloaded(OWNER, JOB_ID, fetched=True)
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "complete"
    assert store.deleted == [[SOURCE_URL], [OUTPUT_URL]]


def test_terminal_status_retries_source_cleanup_without_masking_the_result():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    initial, _ = repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    running_record = replace(
        initial.record,
        manifest=transition(initial.record.manifest, "running", now=1_001),
    )
    running = repository.replace(initial, running_record)
    complete_record = replace(
        running.record,
        manifest=transition(
            running.record.manifest, "complete", now=1_002, output_url=OUTPUT_URL
        ),
    )
    repository.replace(running, complete_record)
    store.fail_delete_urls.add(SOURCE_URL)
    service = WebService(
        repository, store, lambda *_args: None, lambda *_args: None,
        lambda *_args: None, run_secret="test-secret", clock=lambda: 1_010,
    )

    assert service.status(OWNER, JOB_ID).state == "complete"
    assert store.deleted == [[SOURCE_URL]]
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "complete"

    store.fail_delete_urls.clear()
    assert service.status(OWNER, JOB_ID).state == "complete"
    assert store.deleted == [[SOURCE_URL], [SOURCE_URL]]


def test_download_ack_keeps_manifest_and_output_until_source_cleanup_succeeds():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    initial, _ = repository.create(queued_record(dispatch_claim_id=CLAIM_ID))
    running_record = replace(
        initial.record,
        manifest=transition(initial.record.manifest, "running", now=1_001),
    )
    running = repository.replace(initial, running_record)
    complete_record = replace(
        running.record,
        manifest=transition(
            running.record.manifest, "complete", now=1_002, output_url=OUTPUT_URL
        ),
    )
    repository.replace(running, complete_record)
    store.fail_delete_urls.add(SOURCE_URL)
    service = WebService(
        repository, store, lambda *_args: None, lambda *_args: None,
        lambda *_args: None, run_secret="test-secret", clock=lambda: 1_010,
    )

    with pytest.raises(RuntimeError, match="cleanup is unavailable") as caught:
        service.downloaded(OWNER, JOB_ID, fetched=True)

    assert "cleanup secret" not in str(caught.value)
    assert store.deleted == [[SOURCE_URL]]
    assert repository.load(OWNER, JOB_ID).record.manifest.state == "complete"

    store.fail_delete_urls.clear()
    assert service.downloaded(OWNER, JOB_ID, fetched=True).deleted is True
    assert store.deleted == [[SOURCE_URL], [SOURCE_URL], [OUTPUT_URL], [JOB_URL]]
    assert repository.load(OWNER, JOB_ID) is None


def test_status_is_public_and_missing_job_is_not_guessed_from_an_output_path():
    store = MemoryBlobStore()
    repository = BlobJobRepository(store)
    repository.create(queued_record())
    service = WebService(
        repository, store, lambda *_args: None, lambda *_args: None,
        lambda *_args: None, run_secret="test-secret", clock=lambda: 1_000,
    )
    public = service.status(OWNER, JOB_ID).to_dict()
    assert public == {
        "jobId": JOB_ID,
        "state": "queued",
        "filename": "Electrochemistry lecture.pdf",
        "createdAt": 1_000,
        "updatedAt": 1_000,
        "error": "",
    }
    serialized = repr(public)
    assert SOURCE_URL not in serialized
    assert OUTPUT_URL not in serialized
    assert OWNER not in serialized
    assert "instruction" not in serialized

    with pytest.raises(JobNotFound):
        service.status(OWNER, "b" * 32)


def test_public_job_cannot_carry_provider_detail_across_the_http_boundary():
    with pytest.raises(ValueError):
        PublicJob(
            JOB_ID,
            "failed",
            "lecture.pdf",
            1_000,
            1_001,
            "provider token=super-secret",
        )


class DispatchSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return type("Response", (), {"status_code": self.outcome})()


def test_internal_dispatch_read_timeout_is_ambiguous_delivered_and_never_retried():
    import requests

    session = DispatchSession(requests.exceptions.ReadTimeout("accepted but still running"))
    dispatcher = InternalRunDispatcher("https://shortnotes-azure.vercel.app", session=session)
    dispatcher(OWNER, JOB_ID, "signed-token")

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "https://shortnotes-azure.vercel.app/api/web?action=run"
    assert kwargs["json"] == {"owner": OWNER, "jobId": JOB_ID}
    assert kwargs["headers"] == {"X-Shortnotes-Run-Token": "signed-token"}
    assert kwargs["allow_redirects"] is False


def test_internal_dispatch_connect_timeout_is_definitive_failure_without_retry():
    import requests

    session = DispatchSession(requests.exceptions.ConnectTimeout("never connected"))
    dispatcher = InternalRunDispatcher("https://shortnotes-azure.vercel.app", session=session)
    with pytest.raises(DispatchUnavailable) as caught:
        dispatcher(OWNER, JOB_ID, "signed-token")
    assert caught.value.request_finished is False
    assert len(session.calls) == 1


def test_internal_dispatch_rejects_redirect_without_following_or_retrying():
    session = DispatchSession(302)
    dispatcher = InternalRunDispatcher("https://shortnotes-azure.vercel.app", session=session)
    with pytest.raises(DispatchUnavailable) as caught:
        dispatcher(OWNER, JOB_ID, "signed-token")
    assert caught.value.request_finished is True
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
