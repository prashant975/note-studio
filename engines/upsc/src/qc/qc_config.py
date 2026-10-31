"""Load and validate the QC rule set and source registry.

The YAML files are the production policy (config/upsc_qc_rules.yaml,
config/upsc_qc_sources.yaml); the DOCX framework is documentation only and is
never parsed at run time (spec §4).

Validation is strict and loud: a malformed config raises QCConfigError at the
point the QC stage starts, rather than silently degrading (spec §7). Loading
is cached per process — the files are read once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .paths import resource_path


class QCConfigError(RuntimeError):
    """Raised when a QC configuration file is missing or malformed."""


@dataclass
class QCRule:
    id: str
    name: str
    category: str
    severity: str
    description: str = ""
    external_verification: bool = False
    auto_correct_allowed: bool = False
    date_sensitive: bool = False


@dataclass
class QCLimits:
    # Defaults mirror config/upsc_qc_rules.yaml; the YAML is the policy and
    # these apply only if it is missing a key.
    max_claims_verified: int = 200
    sources_per_claim: int = 3
    passages_per_source: int = 3
    retry_attempts: int = 1
    max_download_bytes: int = 15 * 1024 * 1024
    request_timeout_seconds: int = 20
    max_concurrent_requests: int = 4
    max_visual_check_pages: int = 16


@dataclass
class QCRules:
    version: str
    prompt_version: str
    limits: QCLimits
    rules: dict[str, QCRule]                      # rule_id -> rule
    categories: dict[str, list[str]]              # category -> [rule_id]

    def rules_for(self, category: str) -> list[QCRule]:
        return [self.rules[r] for r in self.categories.get(category, [])]


@dataclass
class SourceRegistry:
    version: str
    source_priority: list[str]
    blocked_source_types: list[str]
    subject_aliases: dict[str, str]               # normalised alias -> subject key
    subjects: dict[str, dict[str, Any]]           # subject key -> raw config


_SEVERITIES = {"high", "medium", "low"}
_cache: dict[str, Any] = {}


def _load_yaml(relative: str) -> dict:
    try:
        import yaml
    except ImportError as e:
        # PyYAML is a newer requirement than the rest of the app. On a machine
        # where the code was updated but `pip install -r requirements.txt` was
        # not re-run, a bare ImportError here would escape run_qc (which only
        # handles QCConfigError) and kill the whole run *after* the paid
        # generation call. As a config error it degrades the QC stage instead.
        raise QCConfigError(
            "PyYAML is not installed, so the QC rule set cannot be read. "
            "Run: pip install -r requirements.txt"
        ) from e

    path = resource_path(relative)
    if not path.exists():
        raise QCConfigError(f"QC config file missing: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise QCConfigError(f"QC config not valid YAML ({relative}): {e}") from e
    if not isinstance(data, dict):
        raise QCConfigError(f"QC config must be a mapping ({relative})")
    return data


def load_rules(force_reload: bool = False) -> QCRules:
    if "rules" in _cache and not force_reload:
        return _cache["rules"]
    data = _load_yaml("config/upsc_qc_rules.yaml")

    for key in ("version", "prompt_version", "categories"):
        if key not in data:
            raise QCConfigError(f"upsc_qc_rules.yaml missing required key: {key}")

    raw_limits = data.get("limits") or {}
    if not isinstance(raw_limits, dict):
        raise QCConfigError("upsc_qc_rules.yaml: 'limits' must be a mapping")
    known = {f for f in QCLimits.__dataclass_fields__}
    limits = QCLimits(**{k: int(v) for k, v in raw_limits.items() if k in known})

    rules: dict[str, QCRule] = {}
    categories: dict[str, list[str]] = {}
    if not isinstance(data["categories"], dict) or not data["categories"]:
        raise QCConfigError("upsc_qc_rules.yaml: 'categories' must be a non-empty mapping")
    for cat, cat_data in data["categories"].items():
        rule_list = (cat_data or {}).get("rules")
        if not isinstance(rule_list, list) or not rule_list:
            raise QCConfigError(f"upsc_qc_rules.yaml: category '{cat}' has no rules")
        categories[cat] = []
        for raw in rule_list:
            for req in ("id", "name", "severity"):
                if not raw.get(req):
                    raise QCConfigError(f"upsc_qc_rules.yaml: rule in '{cat}' missing '{req}': {raw}")
            if raw["severity"] not in _SEVERITIES:
                raise QCConfigError(f"upsc_qc_rules.yaml: rule {raw['id']} has invalid severity {raw['severity']!r}")
            if raw["id"] in rules:
                raise QCConfigError(f"upsc_qc_rules.yaml: duplicate rule id {raw['id']}")
            rules[raw["id"]] = QCRule(
                id=raw["id"], name=raw["name"], category=cat,
                severity=raw["severity"], description=str(raw.get("description", "")).strip(),
                external_verification=bool(raw.get("external_verification", False)),
                auto_correct_allowed=bool(raw.get("auto_correct_allowed", False)),
                date_sensitive=bool(raw.get("date_sensitive", False)),
            )
            categories[cat].append(raw["id"])

    result = QCRules(
        version=str(data["version"]), prompt_version=str(data["prompt_version"]),
        limits=limits, rules=rules, categories=categories,
    )
    _cache["rules"] = result
    return result


def load_sources(force_reload: bool = False) -> SourceRegistry:
    if "sources" in _cache and not force_reload:
        return _cache["sources"]
    data = _load_yaml("config/upsc_qc_sources.yaml")

    for key in ("version", "source_priority", "blocked_source_types", "subjects"):
        if key not in data:
            raise QCConfigError(f"upsc_qc_sources.yaml missing required key: {key}")
    subjects = data["subjects"]
    if not isinstance(subjects, dict) or not subjects:
        raise QCConfigError("upsc_qc_sources.yaml: 'subjects' must be a non-empty mapping")

    for skey, scfg in subjects.items():
        if not isinstance(scfg, dict) or not scfg.get("display_name"):
            raise QCConfigError(f"upsc_qc_sources.yaml: subject '{skey}' missing display_name")
        for book in scfg.get("books") or []:
            if not book.get("id") or not book.get("name"):
                raise QCConfigError(f"upsc_qc_sources.yaml: subject '{skey}' has a book without id/name")
        for domain in scfg.get("official_domains") or []:
            if not isinstance(domain, str) or "/" in domain or " " in domain:
                raise QCConfigError(f"upsc_qc_sources.yaml: subject '{skey}' invalid domain {domain!r}")

    aliases = {}
    for alias, target in (data.get("subject_aliases") or {}).items():
        aliases[str(alias).strip().lower()] = str(target)
        if target != "_history_umbrella" and target not in subjects:
            raise QCConfigError(f"upsc_qc_sources.yaml: alias '{alias}' targets unknown subject '{target}'")

    result = SourceRegistry(
        version=str(data["version"]),
        source_priority=list(data["source_priority"]),
        blocked_source_types=list(data["blocked_source_types"]),
        subject_aliases=aliases,
        subjects=subjects,
    )
    _cache["sources"] = result
    return result


def validate_all() -> list[str]:
    """Load both configs; return their versions. Raises QCConfigError on any
    problem — called at QC-stage start so a bad config fails the stage loudly,
    not the whole app at import time."""
    r = load_rules()
    s = load_sources()
    return [r.version, s.version]
