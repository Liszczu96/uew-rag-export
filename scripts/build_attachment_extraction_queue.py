#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import yaml


VALIDATION_PATH = Path("public/rag/attachment-url-validation.jsonl")
MANIFEST_PATH = Path("public/rag/attachment-manifest.jsonl")
UNREGISTERED_PATH = Path("public/rag/unregistered-attachment-links.jsonl")
OVERRIDES_PATH = Path("config/attachment-url-overrides.yml")

QUEUE_PATH = Path("public/rag/attachment-extraction-queue.jsonl")
REVIEW_PATH = Path("public/rag/attachment-resolution-review.jsonl")
STATUS_PATH = Path("public/attachment-resolution-status.json")

DEFAULT_SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}

DEFAULT_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku: {path}")

    records: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Nieprawidłowy JSON w {path}, linia {line_number}: {error}"
                ) from error

            if isinstance(record, dict):
                records.append(record)

    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            file.write("\n")


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if not isinstance(data, dict):
        raise ValueError("Konfiguracja wyjątków musi być mapą YAML.")

    return data


def normalize_url(value: Any) -> str:
    return str(value or "").strip()


def file_extension(url: str) -> str:
    return Path(unquote(urlsplit(url).path)).suffix.lower()


def normalized_mime(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def metadata_from_manifest(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {}

    return {
        "candidate_id": item.get("id"),
        "candidate_type": "registered_attachment",
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "source_priority": item.get("source_priority"),
        "filename": item.get("filename"),
        "declared_mime_type": item.get("mime_type"),
        "declared_filesize": item.get("filesize"),
        "linked_from_count": item.get("linked_from_count", 0),
        "linked_from": item.get("linked_from", []),
        "duplicate_group_id": item.get("duplicate_group_id"),
        "duplicate_group_size": item.get("duplicate_group_size", 1),
    }


def merge_queue_item(
    queue_by_url: dict[str, dict[str, Any]],
    item: dict[str, Any],
    aliases: Iterable[str] = (),
) -> None:
    effective_url = normalize_url(item.get("effective_url") or item.get("url"))

    if not effective_url:
        return

    aliases_set = {
        normalize_url(alias)
        for alias in aliases
        if normalize_url(alias)
    }
    aliases_set.add(effective_url)

    existing = queue_by_url.get(effective_url)

    if existing is None:
        item["effective_url"] = effective_url
        item["url_aliases"] = sorted(aliases_set)
        queue_by_url[effective_url] = item
        return

    existing["url_aliases"] = sorted(
        set(existing.get("url_aliases") or []) | aliases_set
    )

    current_reasons = list(existing.get("resolution_reasons") or [])

    for reason in item.get("resolution_reasons") or []:
        if reason not in current_reasons:
            current_reasons.append(reason)

    existing["resolution_reasons"] = current_reasons


def main() -> None:
    validations = load_jsonl(VALIDATION_PATH)
    manifest = load_jsonl(MANIFEST_PATH)
    unregistered = load_jsonl(UNREGISTERED_PATH)
    overrides = load_overrides(OVERRIDES_PATH)

    validation_by_requested = {
        normalize_url(item.get("requested_url") or item.get("url")): item
        for item in validations
        if normalize_url(item.get("requested_url") or item.get("url"))
    }

    validation_by_effective = {
        normalize_url(item.get("final_url") or item.get("url")): item
        for item in validations
        if normalize_url(item.get("final_url") or item.get("url"))
    }

    manifest_by_url = {
        normalize_url(item.get("normalized_url") or item.get("url")): item
        for item in manifest
        if normalize_url(item.get("normalized_url") or item.get("url"))
    }

    unregistered_by_url = {
        normalize_url(item.get("url")): item
        for item in unregistered
        if normalize_url(item.get("url"))
    }

    supported_mimes = set(DEFAULT_SUPPORTED_MIME_TYPES)
    supported_extensions = set(DEFAULT_SUPPORTED_EXTENSIONS)
    supported_parsers: dict[str, str] = {}

    for rule in overrides.get("supported_document_types", []) or []:
        if not isinstance(rule, dict):
            continue

        extension = str(rule.get("extension") or "").lower()
        mime_type = normalized_mime(rule.get("mime_type"))
        parser = str(rule.get("parser") or "").strip()

        if extension:
            supported_extensions.add(extension)
            if parser:
                supported_parsers[extension] = parser

        if mime_type:
            supported_mimes.add(mime_type)

    queue_by_url: dict[str, dict[str, Any]] = {}
    review_items: list[dict[str, Any]] = []

    # Dokumenty zatwierdzone przez pierwotną walidację.
    for validation in validations:
        if not validation.get("valid_document"):
            continue

        requested_url = normalize_url(
            validation.get("requested_url") or validation.get("url")
        )
        effective_url = normalize_url(
            validation.get("final_url") or requested_url
        )
        extension = str(
            validation.get("extension") or file_extension(effective_url)
        ).lower()

        item = {
            **validation,
            "effective_url": effective_url,
            "resolution": "validated",
            "resolution_reasons": [
                "Podstawowa walidacja URL zakończyła się powodzeniem."
            ],
            "parser": supported_parsers.get(extension),
        }

        merge_queue_item(queue_by_url, item, aliases=[requested_url])

    # Dodatkowe, jawnie dopuszczone formaty, np. ODT.
    accepted_supported_override_count = 0

    for validation in validations:
        if validation.get("valid_document"):
            continue

        requested_url = normalize_url(
            validation.get("requested_url") or validation.get("url")
        )
        effective_url = normalize_url(
            validation.get("final_url") or requested_url
        )
        extension = str(
            validation.get("extension") or file_extension(effective_url)
        ).lower()
        mime_type = normalized_mime(validation.get("content_type"))

        if (
            validation.get("reachable")
            and extension in supported_extensions
            and mime_type in supported_mimes
        ):
            item = {
                **validation,
                "valid_document": True,
                "effective_url": effective_url,
                "resolution": "accepted_supported_type_override",
                "resolution_reasons": [
                    "Format dokumentu został jawnie dopuszczony w konfiguracji wyjątków."
                ],
                "parser": supported_parsers.get(extension),
            }

            merge_queue_item(queue_by_url, item, aliases=[requested_url])
            accepted_supported_override_count += 1

    replacement_resolved_count = 0
    replacement_pending_count = 0

    # Podmiany nieaktualnych URL-i na aktualne odpowiedniki.
    for rule in overrides.get("replacements", []) or []:
        if not isinstance(rule, dict):
            continue

        old_url = normalize_url(rule.get("from"))
        target_url = normalize_url(rule.get("to"))
        reason = str(
            rule.get("reason") or "Skonfigurowana podmiana adresu."
        )

        if not old_url or not target_url:
            continue

        target_validation = (
            validation_by_requested.get(target_url)
            or validation_by_effective.get(target_url)
        )
        target_manifest = manifest_by_url.get(target_url)

        if target_validation and target_validation.get("valid_document"):
            effective_url = normalize_url(
                target_validation.get("final_url") or target_url
            )
            extension = str(
                target_validation.get("extension")
                or file_extension(effective_url)
            ).lower()

            item = {
                **metadata_from_manifest(target_manifest),
                **target_validation,
                "effective_url": effective_url,
                "resolution": "replacement_existing_validated_target",
                "resolution_reasons": [reason],
                "parser": supported_parsers.get(extension),
            }

            merge_queue_item(
                queue_by_url,
                item,
                aliases=[old_url, target_url],
            )
            replacement_resolved_count += 1
            continue

        review_items.append(
            {
                **metadata_from_manifest(target_manifest),
                "url": old_url,
                "replacement_url": target_url,
                "decision": "replacement_target_needs_validation",
                "reason": reason,
                "source_validation": validation_by_requested.get(old_url),
            }
        )
        replacement_pending_count += 1

    # Jawne wykluczenia, np. ZIP-y z logotypami i fontami.
    excluded_count = 0

    for rule in overrides.get("exclude_from_rag", []) or []:
        if not isinstance(rule, dict):
            continue

        url = normalize_url(rule.get("url"))

        if not url:
            continue

        queue_by_url.pop(url, None)
        review_items.append(
            {
                "url": url,
                "decision": "excluded_from_rag",
                "reason": str(
                    rule.get("reason") or "Wykluczenie skonfigurowane."
                ),
                "validation": validation_by_requested.get(url),
            }
        )
        excluded_count += 1

    # Adresy wymagające osobnej ponownej próby pełnym GET.
    retry_required_count = 0

    for rule in overrides.get("retry_with_browser_get", []) or []:
        if not isinstance(rule, dict):
            continue

        url = normalize_url(rule.get("url"))

        if not url:
            continue

        source = unregistered_by_url.get(url) or {}

        review_items.append(
            {
                "url": url,
                "decision": "browser_get_retry_required",
                "reason": str(
                    rule.get("reason") or "Wymagana ponowna próba pełnym GET."
                ),
                "source_id": source.get("host") or urlsplit(url).netloc,
                "filename": Path(unquote(urlsplit(url).path)).name or None,
                "linked_from_count": source.get("linked_from_count", 0),
                "linked_from": source.get("linked_from", []),
                "validation": validation_by_requested.get(url),
            }
        )
        retry_required_count += 1

    # Przypadki pozostawione do ręcznej decyzji administratora treści.
    manual_review_count = 0

    for rule in overrides.get("manual_review", []) or []:
        if not isinstance(rule, dict):
            continue

        url = normalize_url(rule.get("url"))

        if not url:
            continue

        review_items.append(
            {
                "url": url,
                "decision": "manual_review",
                "reason": str(
                    rule.get("reason") or "Wymagana ręczna weryfikacja."
                ),
                "recommended_action": rule.get("recommended_action"),
                "validation": validation_by_requested.get(url),
            }
        )
        manual_review_count += 1

    queue = sorted(
        queue_by_url.values(),
        key=lambda item: str(item.get("effective_url") or ""),
    )
    review_items.sort(
        key=lambda item: (
            str(item.get("decision") or ""),
            str(item.get("url") or ""),
        )
    )

    for index, item in enumerate(queue, start=1):
        item["queue_id"] = f"attachment-{index:05d}"

    write_jsonl(QUEUE_PATH, queue)
    write_jsonl(REVIEW_PATH, review_items)

    resolution_counts = Counter(
        str(item.get("resolution") or "unknown")
        for item in queue
    )
    review_decision_counts = Counter(
        str(item.get("decision") or "unknown")
        for item in review_items
    )
    extension_counts = Counter(
        str(
            item.get("extension")
            or file_extension(str(item.get("effective_url") or ""))
        )
        for item in queue
    )
    source_counts: dict[str, int] = defaultdict(int)

    for item in queue:
        source_counts[str(item.get("source_id") or "unknown")] += 1

    status = {
        "input_validation_count": len(validations),
        "base_valid_document_count": sum(
            1 for item in validations if item.get("valid_document")
        ),
        "accepted_supported_type_override_count": (
            accepted_supported_override_count
        ),
        "final_extraction_queue_count": len(queue),
        "review_queue_count": len(review_items),
        "replacement_rule_count": len(
            overrides.get("replacements", []) or []
        ),
        "replacement_resolved_count": replacement_resolved_count,
        "replacement_pending_validation_count": replacement_pending_count,
        "excluded_rule_count": excluded_count,
        "browser_get_retry_required_count": retry_required_count,
        "manual_review_rule_count": manual_review_count,
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "review_decision_counts": dict(
            sorted(review_decision_counts.items())
        ),
        "extension_counts": dict(sorted(extension_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "files": {
            "extraction_queue": str(QUEUE_PATH),
            "resolution_review": str(REVIEW_PATH),
        },
    }

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Budowa kolejki ekstrakcji zakończona.")
    print(f"Dokumenty gotowe do ekstrakcji: {len(queue)}")
    print(f"Pozycje wymagające decyzji: {len(review_items)}")
    print(f"Rozwiązane podmiany URL: {replacement_resolved_count}")
    print(f"Podmiany oczekujące na walidację: {replacement_pending_count}")


if __name__ == "__main__":
    main()
