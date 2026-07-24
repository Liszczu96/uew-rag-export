#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CHUNKS_PATH = Path("public/rag/chunks.jsonl")
STATUS_PATH = Path("public/rag-quality-status.json")
ISSUES_PATH = Path("public/rag/rag-quality-issues.jsonl")

REQUIRED_FIELDS = {
    "schema_version",
    "id",
    "document_id",
    "document_kind",
    "source_id",
    "source_ids",
    "source_priority",
    "title",
    "url",
    "url_aliases",
    "language",
    "chunk_index",
    "chunk_number",
    "chunk_count",
    "text",
    "embedding_text",
    "char_count",
    "word_count",
    "text_sha256",
    "document_content_sha256",
}

GENERIC_TITLE_PATTERN = re.compile(
    r"^(?:document|dokument|plik|attachment|załącznik|page|strona|"
    r"untitled|bez tytułu|\d+)$",
    re.IGNORECASE,
)

CONTROL_CHARACTER_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)

HTML_TAG_PATTERN = re.compile(r"<[A-Za-z!/][^>]*>")
HTML_ENTITY_PATTERN = re.compile(
    r"&(?:nbsp|amp|lt|gt|quot|apos|#[0-9]+|#x[0-9A-Fa-f]+);"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            file.write("\n")


def load_chunks(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku: {path}")

    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                issues.append(
                    {
                        "severity": "error",
                        "issue_type": "invalid_json",
                        "line_number": line_number,
                        "message": str(error),
                    }
                )
                continue

            if not isinstance(record, dict):
                issues.append(
                    {
                        "severity": "error",
                        "issue_type": "record_not_object",
                        "line_number": line_number,
                    }
                )
                continue

            record["_audit_line_number"] = line_number
            chunks.append(record)

    return chunks, issues


def ngram_unique_ratio(
    text: str,
    n: int = 5,
) -> float:
    tokens = re.findall(r"\w+", text.lower(), flags=re.UNICODE)

    if len(tokens) < max(20, n):
        return 1.0

    ngrams = [
        tuple(tokens[index:index + n])
        for index in range(len(tokens) - n + 1)
    ]

    if not ngrams:
        return 1.0

    return len(set(ngrams)) / len(ngrams)


def issue(
    severity: str,
    issue_type: str,
    record: dict[str, Any] | None = None,
    **details: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "severity": severity,
        "issue_type": issue_type,
    }

    if record is not None:
        payload.update(
            {
                "line_number": record.get("_audit_line_number"),
                "chunk_id": record.get("id"),
                "document_id": record.get("document_id"),
                "title": record.get("title"),
                "url": record.get("url"),
            }
        )

    payload.update(details)
    return payload


def main() -> None:
    chunks, issues = load_chunks(CHUNKS_PATH)

    ids: Counter[str] = Counter()
    text_hashes: Counter[str] = Counter()
    document_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    kind_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()

    for record in chunks:
        missing_fields = sorted(
            REQUIRED_FIELDS.difference(record.keys())
        )

        if missing_fields:
            issues.append(
                issue(
                    "error",
                    "missing_required_fields",
                    record,
                    missing_fields=missing_fields,
                )
            )

        chunk_id = str(record.get("id") or "")
        document_id = str(record.get("document_id") or "")
        text = str(record.get("text") or "")
        title = str(record.get("title") or "")
        url = str(record.get("url") or "")
        embedding_text = str(record.get("embedding_text") or "")

        ids[chunk_id] += 1
        text_hashes[str(record.get("text_sha256") or "")] += 1
        document_groups[document_id].append(record)

        kind_counts[str(record.get("document_kind") or "unknown")] += 1
        source_counts[str(record.get("source_id") or "unknown")] += 1
        priority_counts[str(record.get("source_priority") or "unknown")] += 1

        if not chunk_id:
            issues.append(issue("error", "missing_chunk_id", record))

        if not document_id:
            issues.append(issue("error", "missing_document_id", record))

        if not text:
            issues.append(issue("error", "empty_text", record))
        else:
            expected_hash = sha256_text(text)

            if record.get("text_sha256") != expected_hash:
                issues.append(
                    issue(
                        "error",
                        "text_hash_mismatch",
                        record,
                        expected=expected_hash,
                        actual=record.get("text_sha256"),
                    )
                )

            if record.get("char_count") != len(text):
                issues.append(
                    issue(
                        "error",
                        "char_count_mismatch",
                        record,
                        expected=len(text),
                        actual=record.get("char_count"),
                    )
                )

            expected_word_count = word_count(text)
            if record.get("word_count") != expected_word_count:
                issues.append(
                    issue(
                        "error",
                        "word_count_mismatch",
                        record,
                        expected=expected_word_count,
                        actual=record.get("word_count"),
                    )
                )

            if CONTROL_CHARACTER_PATTERN.search(text):
                issues.append(
                    issue(
                        "error",
                        "control_characters_in_text",
                        record,
                    )
                )

            if HTML_TAG_PATTERN.search(text):
                issues.append(
                    issue(
                        "warning",
                        "html_tag_in_text",
                        record,
                    )
                )

            if HTML_ENTITY_PATTERN.search(text):
                issues.append(
                    issue(
                        "warning",
                        "html_entity_in_text",
                        record,
                    )
                )

            if expected_word_count < 20:
                issues.append(
                    issue(
                        "warning",
                        "very_short_chunk",
                        record,
                        word_count=expected_word_count,
                    )
                )

            repetition_ratio = ngram_unique_ratio(text)

            if expected_word_count >= 50 and repetition_ratio < 0.50:
                issues.append(
                    issue(
                        "warning",
                        "high_internal_repetition",
                        record,
                        five_gram_unique_ratio=round(
                            repetition_ratio,
                            4,
                        ),
                        word_count=expected_word_count,
                    )
                )

        context_path = record.get("context_path") or []

        if not isinstance(context_path, list):
            issues.append(
                issue(
                    "error",
                    "invalid_context_path",
                    record,
                    actual_type=type(context_path).__name__,
                )
            )
            context_path = []

        embedding_parts = [title]

        normalized_context_path = [
            str(value).strip()
            for value in context_path
            if str(value).strip()
        ]

        if normalized_context_path:
            embedding_parts.append(" > ".join(normalized_context_path))

        embedding_parts.append(text)
        expected_embedding_text = "\n\n".join(embedding_parts)

        if embedding_text != expected_embedding_text:
            issues.append(
                issue(
                    "error",
                    "embedding_text_mismatch",
                    record,
                    expected=expected_embedding_text,
                    actual=embedding_text,
                    context_path=normalized_context_path,
                )
            )

        if not title.strip():
            issues.append(issue("error", "empty_title", record))
        else:
            if "\n" in title:
                issues.append(
                    issue(
                        "warning",
                        "title_contains_newline",
                        record,
                    )
                )

            if "_" in title:
                issues.append(
                    issue(
                        "warning",
                        "title_contains_underscore",
                        record,
                    )
                )

            if GENERIC_TITLE_PATTERN.fullmatch(title.strip()):
                issues.append(
                    issue(
                        "warning",
                        "generic_title",
                        record,
                    )
                )

        if not re.match(r"^https?://", url):
            issues.append(
                issue(
                    "error",
                    "invalid_or_missing_url",
                    record,
                )
            )

        if record.get("language") in (None, ""):
            issues.append(
                issue(
                    "warning",
                    "missing_language",
                    record,
                )
            )

        chunk_index = record.get("chunk_index")
        chunk_number = record.get("chunk_number")

        if (
            isinstance(chunk_index, int)
            and isinstance(chunk_number, int)
            and chunk_number != chunk_index + 1
        ):
            issues.append(
                issue(
                    "error",
                    "chunk_number_mismatch",
                    record,
                )
            )

    for chunk_id, count in ids.items():
        if chunk_id and count > 1:
            issues.append(
                {
                    "severity": "error",
                    "issue_type": "duplicate_chunk_id",
                    "chunk_id": chunk_id,
                    "occurrence_count": count,
                }
            )

    for document_id, group in document_groups.items():
        ordered = sorted(
            group,
            key=lambda record: (
                int(record.get("chunk_index") or 0)
            ),
        )

        indexes = [
            record.get("chunk_index")
            for record in ordered
        ]
        expected_indexes = list(range(len(ordered)))

        if indexes != expected_indexes:
            issues.append(
                {
                    "severity": "error",
                    "issue_type": "document_chunk_sequence_error",
                    "document_id": document_id,
                    "actual_indexes": indexes,
                    "expected_indexes": expected_indexes,
                }
            )

        declared_counts = {
            record.get("chunk_count")
            for record in group
        }

        if declared_counts != {len(group)}:
            issues.append(
                {
                    "severity": "error",
                    "issue_type": "document_chunk_count_error",
                    "document_id": document_id,
                    "actual_declared_counts": sorted(
                        str(value)
                        for value in declared_counts
                    ),
                    "expected_count": len(group),
                }
            )

    duplicate_text_groups = sum(
        1
        for text_hash, count in text_hashes.items()
        if text_hash and count > 1
    )
    duplicate_text_extra_copies = sum(
        count - 1
        for text_hash, count in text_hashes.items()
        if text_hash and count > 1
    )

    severity_counts = Counter(
        str(record.get("severity") or "unknown")
        for record in issues
    )
    issue_type_counts = Counter(
        str(record.get("issue_type") or "unknown")
        for record in issues
    )

    structural_error_count = severity_counts.get("error", 0)

    status = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "complete": structural_error_count == 0,
        "chunk_count": len(chunks),
        "document_count": len(document_groups),
        "unique_chunk_id_count": len(ids),
        "duplicate_text_group_count": duplicate_text_groups,
        "duplicate_text_extra_copy_count": (
            duplicate_text_extra_copies
        ),
        "kind_counts": dict(sorted(kind_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
        "severity_counts": dict(
            sorted(severity_counts.items())
        ),
        "issue_type_counts": dict(
            sorted(issue_type_counts.items())
        ),
        "files": {
            "chunks": str(CHUNKS_PATH),
            "issues": str(ISSUES_PATH),
        },
    }

    write_jsonl(
        ISSUES_PATH,
        sorted(
            issues,
            key=lambda record: (
                str(record.get("severity") or ""),
                str(record.get("issue_type") or ""),
                int(record.get("line_number") or 0),
            ),
        ),
    )

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Audyt jakości chunków zakończony.")
    print(f"Kompletność strukturalna: {status['complete']}")
    print(f"Chunki: {status['chunk_count']}")
    print(f"Dokumenty: {status['document_count']}")
    print(f"Błędy strukturalne: {structural_error_count}")
    print(
        "Ostrzeżenia jakościowe: "
        f"{severity_counts.get('warning', 0)}"
    )
    print(
        "Grupy identycznego tekstu: "
        f"{duplicate_text_groups}"
    )

    if structural_error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
