"""Command line: python -m sheets_bridge [--dry-run] [--limit N] [--row N] ...

Defaults come from the environment (see settings.py / .env.example):
  SHEETS_BRIDGE_SPREADSHEET_ID, SHEETS_BRIDGE_GID, SHEETS_BRIDGE_SIGNATURE,
  SHEETS_BRIDGE_MAX_COST_INR, SHEETS_BRIDGE_CLIENT_SECRET, SHEETS_BRIDGE_TOKEN
"""
from __future__ import annotations

import argparse
import sys

from notesbot import categories

from .errors import BridgeError, describe_exception
from .mapping import SUBJECTS


def _category(value: str) -> str:
    """--category: "neet-jee", "foundation"; the old "neet"/"jee" mean NEET-JEE."""
    try:
        return categories.normalize(value)
    except ValueError:
        return value   # argparse's choices check reports it


def build_parser(defaults) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sheets_bridge",
        description="Sheet row -> Drive PDF -> short notes -> same Drive folder -> sheet write-back.",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and print only; no generation, no upload, no sheet writes")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="process at most N rows")
    parser.add_argument("--row", type=int, default=None, metavar="N", help="process only the row numbered N in the Sheets UI")
    parser.add_argument("--redo", action="store_true", help="reprocess rows already marked done/failed/claimed")
    parser.add_argument(
        "--subject",
        default="sheet",
        metavar="auto|sheet|<fixed>",
        help="auto = let the model decide; sheet = map column D (default); or a fixed value: " + ", ".join(SUBJECTS[1:]),
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=defaults.max_cost_inr,
        metavar="X",
        help="stop once cumulative spend (INR) passes X (env SHEETS_BRIDGE_MAX_COST_INR)",
    )
    parser.add_argument("--spreadsheet-id", default=defaults.spreadsheet_id, help="env SHEETS_BRIDGE_SPREADSHEET_ID")
    parser.add_argument("--gid", type=int, default=defaults.gid, help="tab gid (env SHEETS_BRIDGE_GID)")
    parser.add_argument("--profile", action="append", default=None, metavar="NAME", help="run this named sheet from sheets.json (repeatable)")
    parser.add_argument("--all-profiles", action="store_true", help="run every enabled sheet in sheets.json, one after another")
    parser.add_argument("--list-profiles", action="store_true", help="print the configured sheets and exit")
    parser.add_argument("--list-tabs", metavar="LINK_OR_ID", default=None, help="classify every tab of a workbook (which can run, how many rows are ready) and exit")
    parser.add_argument("--all-tabs", action="store_true", help="with --spreadsheet-id (a link works too): run every tab that has the needed columns and rows ready")
    parser.add_argument("--signature", default=defaults.signature, help="override column E for every row (env SHEETS_BRIDGE_SIGNATURE)")
    parser.add_argument("--output-folder", default=None, metavar="LINK", help="catalog sheets (PDF links): the Drive folder the notes are filed in; overrides the profile's outputFolder")
    parser.add_argument("--category", default=categories.DEFAULT, type=_category, choices=categories.CATEGORIES,
                        help="exam category whose note format to follow: neet-jee (default) or foundation")
    parser.add_argument("--sanskrit-style", default="hindi", choices=("hindi", "strict"), help="Sanskrit lectures: hindi = shlokas verbatim with Hindi meanings (default); strict = Sanskrit only")
    parser.add_argument("--no-page-numbers", action="store_true", help="omit page numbers from generated notes (included by default)")
    parser.add_argument("--no-browser", action="store_true", help="fail instead of opening a browser when no cached token exists")
    parser.add_argument("--serve", action="store_true", help="run the Studio app locally with the 'Run automation' button (http://localhost:3000)")
    parser.add_argument("--serve-page", action="store_true", help="serve only the standalone automation page (no Studio login)")
    parser.add_argument("--port", type=int, default=None, help="port for --serve (default 3000) / --serve-page (default 8765)")
    parser.add_argument("--no-open", action="store_true", help="with --serve: do not open the browser automatically")
    parser.add_argument("--check-setup", action="store_true", help="verify client_secret.json, libraries and .env; change nothing")
    parser.add_argument("--test-model", action="store_true", help="make one tiny model call to prove the key/proxy works")
    parser.add_argument("--probe-pdf", metavar="FILE", default=None, help="run the real generation call on one page group of a local PDF and report what the model returned (no sheet, no upload)")
    parser.add_argument("--probe-group", type=int, default=1, metavar="N", help="with --probe-pdf: which page group (1-based, default 1)")
    parser.add_argument("--probe-language", default="auto", metavar="LANG", help="with --probe-pdf: hindi, english or auto (default: detect from the PDF)")
    parser.add_argument("--probe-show", action="store_true", help="with --probe-pdf: also print the generated headings and points (for synthetic test pages)")
    parser.add_argument("--probe-verify", action="store_true", help="with --probe-pdf: run the batch's full per-group path including the source-coverage check")
    return parser


def _utf8_console() -> None:
    """Best effort: keep Devanagari file names printable on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def explicit_target(args, defaults):
    """The ad-hoc target named on the command line, or None to use profiles.

    ``--spreadsheet-id`` accepts a full link; ``--all-tabs`` runs every tab of
    that workbook with rows ready; ``--gid`` alone is a configuration error
    rather than a run against nothing.
    """
    from . import profiles

    given_id = (args.spreadsheet_id or "").strip()
    if given_id == (defaults.spreadsheet_id or "").strip() and args.gid == defaults.gid and not args.all_tabs:
        return None
    if not given_id:
        raise BridgeError(
            "no_sheet",
            "--gid / --all-tabs need --spreadsheet-id (or SHEETS_BRIDGE_SPREADSHEET_ID in .env); "
            "to run a configured sheet use --profile NAME",
        )
    spreadsheet_id, link_gid = profiles.parse_sheet_link(given_id)
    if args.all_tabs:
        return profiles.SheetTarget(name="cli", spreadsheet_id=spreadsheet_id, gid=0, all_tabs=True)
    gid = args.gid if args.gid != defaults.gid or link_gid is None else link_gid
    return profiles.SheetTarget(name="cli", spreadsheet_id=spreadsheet_id, gid=gid)


def _print_tabs(defaults, link: str, *, interactive: bool) -> int:
    from . import profiles, tabs
    from .errors import safe_print
    from .google_auth import build_services, load_credentials

    try:
        spreadsheet_id, gid = profiles.parse_sheet_link(link)
        creds = load_credentials(defaults.client_secret_path, defaults.token_path, interactive=interactive)
        sheets_service, _drive = build_services(creds)
        workbook, summaries = tabs.list_tabs(sheets_service, spreadsheet_id)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {describe_exception(exc)}", file=sys.stderr)
        return 2
    safe_print(f"{workbook!r}  ({spreadsheet_id})")
    for summary in summaries:
        mark = "*" if summary.gid == gid else " "
        ready = f"{summary.eligible} ready" if summary.eligible is not None else "-"
        detail = f"  [{summary.note}]" if summary.note else ""
        columns = ""
        if summary.supported:
            columns = "  Drive Link=" + summary.mapping.get("drive_link", "?") + " Lecture=" + summary.mapping.get("lecture_number", "-")
        safe_print(f"{mark} gid={summary.gid:<12} {summary.kind:<9} rows={summary.rows:<5} {ready:<9} {summary.title!r}{columns}{detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    from .settings import Settings

    try:
        defaults = Settings.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    args = build_parser(defaults).parse_args(argv)

    if args.list_profiles:
        from . import profiles as _profiles

        try:
            targets = _profiles.resolve_targets(defaults)
        except BridgeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for target in targets:
            flag = "" if target.enabled else "  (disabled)"
            where = "all tabs" if target.all_tabs else f"gid {target.gid}"
            print(f"{target.name}	{target.spreadsheet_id}	{where}{flag}")
        return 0

    if args.list_tabs:
        return _print_tabs(defaults, args.list_tabs, interactive=not args.no_browser)

    if args.check_setup:
        from .setup_check import run_check

        return run_check(defaults)

    if args.probe_pdf:
        from .model_check import probe_pdf_file

        return probe_pdf_file(defaults, args.probe_pdf, group_index=args.probe_group,
                              language=args.probe_language, interactive=not args.no_browser,
                              show_text=args.probe_show, verify=args.probe_verify)
    if args.test_model:
        from .model_check import test_model

        return test_model(defaults, interactive=not args.no_browser)

    if args.serve:
        from .studio_server import serve_studio

        return serve_studio(defaults, port=args.port or 3000, open_browser=not args.no_open)

    if args.serve_page:
        from .webui import serve

        return serve(defaults, port=args.port or 8765, open_browser=not args.no_open)

    subject_mode = (args.subject or "sheet").strip().lower()
    if subject_mode != "sheet" and subject_mode not in SUBJECTS:
        print(f"--subject must be auto, sheet or one of {', '.join(SUBJECTS[1:])}", file=sys.stderr)
        return 2
    if args.row is not None and args.row < 2:
        print("--row must be >= 2 (row 1 is the header)", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 0:
        print("--limit must be >= 0", file=sys.stderr)
        return 2
    max_cost = args.max_cost if args.max_cost is not None and args.max_cost > 0 else None

    from .runner import RunOptions

    options = RunOptions(
        dry_run=bool(args.dry_run),
        limit=args.limit,
        row=args.row,
        redo=bool(args.redo),
        show_page_numbers=not args.no_page_numbers,
        subject_mode=subject_mode,
        category=args.category,
        sanskrit_style=args.sanskrit_style,
        max_cost_inr=max_cost,
        signature_override=(args.signature or "").strip(),
        max_input_bytes=defaults.max_input_bytes,
    )

    from . import batch, profiles
    from .errors import safe_print

    # Which sheet(s): an explicit --spreadsheet-id/--gid still wins, so the
    # single-sheet invocation is unchanged; otherwise pick from sheets.json.
    # Resolved BEFORE authenticating: a mistyped target is a configuration
    # error and should say so without first opening a browser tab.
    try:
        explicit = explicit_target(args, defaults)
        if explicit is not None:
            chosen = (explicit,)
        else:
            chosen = profiles.select(
                profiles.resolve_targets(defaults),
                args.profile,
                all_profiles=bool(args.all_profiles),
            )
        if args.output_folder:
            from dataclasses import replace as _replace

            folder_id = profiles.parse_folder_link(args.output_folder)
            chosen = tuple(_replace(t, output_folder=folder_id) for t in chosen)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        from .google_auth import build_services, load_credentials

        creds = load_credentials(defaults.client_secret_path, defaults.token_path, interactive=not args.no_browser)
        sheets_service, drive_service = build_services(creds)
    except BridgeError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"auth error: {describe_exception(exc)}", file=sys.stderr)
        return 2
    if (args.signature or "").strip() != (defaults.signature or "").strip():
        # The flag was given: it overrides column E *and* any profile signature.
        from dataclasses import replace as _replace

        chosen = tuple(_replace(t, signature="") for t in chosen)

    from . import output_cache

    output_cache.sweep()

    access = None
    if not options.dry_run:
        from .google_auth import token_provider, user_email
        from .pw_gate import ProxyAccess

        access = ProxyAccess(token_provider(creds), email=user_email(creds))
        if access.email and not access.domain_ok():
            print(f"error: {access.email} is not an @pw.live account", file=sys.stderr)
            return 2
        model = access.install_transport()
        print(f"PW proxy: {access.email or 'signed-in user'} - model {model}")
        try:
            access.ensure_allowed()
        except BridgeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        from .model_check import preflight

        problem = preflight(model)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2

    try:
        result = batch.run_targets(
            chosen,
            sheets_service,
            drive_service,
            options,
            access=access,
            out=safe_print,
            operator=getattr(access, "email", "") if access else "",
        )
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {describe_exception(exc)}", file=sys.stderr)
        return 1

    if result.interrupted:
        return 130
    # A profile that never started (bad gid, no access, layout mismatch) is a
    # failure even when no row ever ran.
    if any(r.error for r in result.results):
        return 1
    planned = sum(
        r.summary.counts().get("planned", 0) for r in result.results if r.summary is not None
    )
    return 1 if result.total_failed and not result.total_done and not planned else 0
