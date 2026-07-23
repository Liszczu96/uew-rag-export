#!/usr/bin/env python3
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

QUEUE_PATH = Path("public/rag/attachment-extraction-queue-final.jsonl")
INDEX_PATH = Path("public/rag/attachment-text-index.jsonl")
STATE_PATH = Path("public/rag/attachment-extraction-state.jsonl")
ERRORS_PATH = Path("public/rag/attachment-extraction-errors.jsonl")
REPORT_PATH = Path("public/attachment-extraction-audit.json")
ISSUES_PATH = Path("public/rag/attachment-extraction-audit-issues.jsonl")
OCR_QUEUE_PATH = Path("public/rag/attachment-ocr-queue.jsonl")
LOW_TEXT_WORD_THRESHOLD = 20


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Nieprawidłowy JSON w {path}, linia {line_number}: {error}"
                ) from error
            if isinstance(value, dict):
                records.append(value)
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")


def normalized_url(record: dict[str, Any]) -> str:
    return str(
        record.get("effective_url")
        or record.get("final_url")
        or record.get("url")
        or ""
    ).strip()


def duplicate_urls(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for position, record in enumerate(records, start=1):
        url = normalized_url(record)
        if url:
            positions.setdefault(url, []).append(position)
    return {url: values for url, values in positions.items() if len(values) > 1}


def make_issue(
    issue_type: str,
    severity: str,
    url: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "issue_type": issue_type,
        "severity": severity,
    }
    if url:
        record["effective_url"] = url
    if details:
        record["details"] = details
    return record


def read_blob(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception as error:
        return None, str(error)
    if not isinstance(payload, dict):
        return None, "Zawartość blobu nie jest obiektem JSON."
    return payload, None


def main() -> None:
    queue = load_jsonl(QUEUE_PATH)
    index = load_jsonl(INDEX_PATH)
    state = load_jsonl(STATE_PATH)
    errors = load_jsonl(ERRORS_PATH)

    issues: list[dict[str, Any]] = []
    ocr_queue: list[dict[str, Any]] = []

    queue_duplicates = duplicate_urls(queue)
    index_duplicates = duplicate_urls(index)
    state_duplicates = duplicate_urls(state)

    for name, duplicates in (
        ("queue", queue_duplicates),
        ("index", index_duplicates),
        ("state", state_duplicates),
    ):
        for url, positions in duplicates.items():
            issues.append(
                make_issue(
                    f"duplicate_{name}_url",
                    "error",
                    url,
                    {"positions": positions},
                )
            )

    queue_by_url = {normalized_url(x): x for x in queue if normalized_url(x)}
    index_by_url = {normalized_url(x): x for x in index if normalized_url(x)}
    state_by_url = {normalized_url(x): x for x in state if normalized_url(x)}

    queue_urls = set(queue_by_url)
    index_urls = set(index_by_url)
    state_urls = set(state_by_url)

    missing_in_index = sorted(queue_urls - index_urls)
    missing_in_state = sorted(queue_urls - state_urls)
    orphan_index = sorted(index_urls - queue_urls)
    orphan_state = sorted(state_urls - queue_urls)

    for url in missing_in_index:
        issues.append(make_issue("missing_index_record", "error", url))
    for url in missing_in_state:
        issues.append(make_issue("missing_state_record", "error", url))
    for url in orphan_index:
        issues.append(make_issue("orphan_index_record", "warning", url))
    for url in orphan_state:
        issues.append(make_issue("orphan_state_record", "warning", url))

    extraction_status_counts: Counter[str] = Counter()
    parser_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    extension_counts: Counter[str] = Counter()
    content_hash_counts: Counter[str] = Counter()
    text_hash_counts: Counter[str] = Counter()

    verified_blob_count = 0
    missing_blob_count = 0
    unreadable_blob_count = 0
    text_hash_mismatch_count = 0
    metadata_mismatch_count = 0
    empty_text_count = 0
    low_text_count = 0
    total_word_count = 0
    total_char_count = 0

    for url, record in sorted(index_by_url.items()):
        extraction_status = str(record.get("extraction_status") or "unknown")
        parser = str(record.get("parser") or "unknown")
        source_id = str(record.get("source_id") or "unknown")
        extension = str(record.get("extension") or "unknown")

        extraction_status_counts[extraction_status] += 1
        parser_counts[parser] += 1
        source_counts[source_id] += 1
        extension_counts[extension] += 1

        content_hash = str(record.get("content_sha256") or "")
        text_hash = str(record.get("text_sha256") or "")
        if content_hash:
            content_hash_counts[content_hash] += 1
        if text_hash:
            text_hash_counts[text_hash] += 1

        blob_value = str(record.get("blob_path") or "").strip()
        if not blob_value:
            missing_blob_count += 1
            issues.append(make_issue("missing_blob_path", "error", url))
            continue

        blob_path = Path(blob_value)
        if not blob_path.exists():
            missing_blob_count += 1
            issues.append(
                make_issue(
                    "missing_blob_file",
                    "error",
                    url,
                    {"blob_path": blob_value},
                )
            )
            continue

        payload, blob_error = read_blob(blob_path)
        if blob_error or payload is None:
            unreadable_blob_count += 1
            issues.append(
                make_issue(
                    "unreadable_blob",
                    "error",
                    url,
                    {"blob_path": blob_value, "error": blob_error},
                )
            )
            continue

        verified_blob_count += 1
        payload_url = normalized_url(payload)
        if payload_url and payload_url != url:
            metadata_mismatch_count += 1
            issues.append(
                make_issue(
                    "blob_url_mismatch",
                    "error",
                    url,
                    {"blob_effective_url": payload_url},
                )
            )

        text = str(payload.get("text") or "")
        calculated_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if text_hash and calculated_text_hash != text_hash:
            text_hash_mismatch_count += 1
            issues.append(
                make_issue(
                    "text_hash_mismatch",
                    "error",
                    url,
                    {
                        "index_hash": text_hash,
                        "calculated_hash": calculated_text_hash,
                    },
                )
            )

        words = int(payload.get("word_count") or 0)
        chars = int(payload.get("char_count") or 0)
        total_word_count += words
        total_char_count += chars

        if not text.strip():
            empty_text_count += 1
        if words < LOW_TEXT_WORD_THRESHOLD:
            low_text_count += 1

        if extraction_status == "needs_ocr":
            ocr_queue.append(
                {
                    "attachment_id": record.get("attachment_id"),
                    "queue_id": record.get("queue_id"),
                    "source_id": source_id,
                    "source_name": record.get("source_name"),
                    "filename": record.get("filename"),
                    "extension": extension,
                    "effective_url": url,
                    "url_aliases": record.get("url_aliases", []),
                    "content_sha256": content_hash,
                    "text_sha256": text_hash,
                    "blob_path": blob_value,
                    "parser": parser,
                    "char_count": chars,
                    "word_count": words,
                    "metrics": record.get("metrics", {}),
                    "warnings": record.get("warnings", []),
                    "ocr_status": "pending",
                }
            )

    duplicate_content_groups = {
        key: count for key, count in content_hash_counts.items() if count > 1
    }
    duplicate_text_groups = {
        key: count for key, count in text_hash_counts.items() if count > 1
    }

    severity_counts = Counter(str(x.get("severity") or "unknown") for x in issues)
    issue_type_counts = Counter(str(x.get("issue_type") or "unknown") for x in issues)

    complete = (
        len(queue) == len(index) == len(state)
        and not missing_in_index
        and not missing_in_state
        and not orphan_index
        and not orphan_state
        and not queue_duplicates
        and not index_duplicates
        and not state_duplicates
        and missing_blob_count == 0
        and unreadable_blob_count == 0
        and text_hash_mismatch_count == 0
        and metadata_mismatch_count == 0
        and len(errors) == 0
    )

    report = {
        "schema_version": 1,
        "complete": complete,
        "queue_count": len(queue),
        "index_count": len(index),
        "state_count": len(state),
        "error_record_count": len(errors),
        "verified_blob_count": verified_blob_count,
        "missing_in_index_count": len(missing_in_index),
        "missing_in_state_count": len(missing_in_state),
        "orphan_index_count": len(orphan_index),
        "orphan_state_count": len(orphan_state),
        "queue_duplicate_url_count": len(queue_duplicates),
        "index_duplicate_url_count": len(index_duplicates),
        "state_duplicate_url_count": len(state_duplicates),
        "missing_blob_count": missing_blob_count,
        "unreadable_blob_count": unreadable_blob_count,
        "text_hash_mismatch_count": text_hash_mismatch_count,
        "metadata_mismatch_count": metadata_mismatch_count,
        "empty_text_count": empty_text_count,
        "low_text_count": low_text_count,
        "ocr_queue_count": len(ocr_queue),
        "total_word_count": total_word_count,
        "total_char_count": total_char_count,
        "duplicate_content_hash_group_count": len(duplicate_content_groups),
        "duplicate_content_extra_copy_count": sum(
            count - 1 for count in duplicate_content_groups.values()
        ),
        "duplicate_text_hash_group_count": len(duplicate_text_groups),
        "duplicate_text_extra_copy_count": sum(
            count - 1 for count in duplicate_text_groups.values()
        ),
        "extraction_status_counts": dict(sorted(extraction_status_counts.items())),
        "parser_counts": dict(sorted(parser_counts.items())),
        "extension_counts": dict(sorted(extension_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "issue_type_counts": dict(sorted(issue_type_counts.items())),
        "files": {
            "issues": str(ISSUES_PATH),
            "ocr_queue": str(OCR_QUEUE_PATH),
        },
    }

    write_jsonl(
        ISSUES_PATH,
        sorted(
            issues,
            key=lambda x: (
                str(x.get("severity") or ""),
                str(x.get("issue_type") or ""),
                str(x.get("effective_url") or ""),
            ),
        ),
    )
    write_jsonl(
        OCR_QUEUE_PATH,
        sorted(
            ocr_queue,
            key=lambda x: (
                str(x.get("source_id") or ""),
                str(x.get("filename") or ""),
                str(x.get("effective_url") or ""),
            ),
        ),
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Audyt ekstrakcji zakończony.")
    print(f"Kompletność: {complete}")
    print(f"Kolejka: {len(queue)}")
    print(f"Indeks: {len(index)}")
    print(f"Stan: {len(state)}")
    print(f"Zweryfikowane bloby: {verified_blob_count}")
    print(f"Pozycje OCR: {len(ocr_queue)}")
    print(f"Problemy: {len(issues)}")

    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
