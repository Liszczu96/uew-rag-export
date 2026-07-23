#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


DOCUMENTS_PATH = Path("public/rag/documents.jsonl")
ATTACHMENT_INDEX_PATH = Path("public/rag/attachment-text-index.jsonl")

CORPUS_DOCUMENTS_PATH = Path("public/rag/corpus-documents.jsonl")
CHUNKS_PATH = Path("public/rag/chunks.jsonl")
STATE_PATH = Path("public/rag/corpus-state.jsonl")
EXCLUSIONS_PATH = Path("public/rag/corpus-exclusions.jsonl")
CHANGES_PATH = Path("public/changes/rag-corpus-changes.jsonl")
STATUS_PATH = Path("public/rag-corpus-status.json")

INDEXABLE_ATTACHMENT_STATUSES = {"ok", "ok_ocr"}
PRIORITY_RANK = {"A": 0, "B": 1, "C": 2}
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines: list[str] = []
    previous_blank = False

    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()

        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue

        lines.append(line)
        previous_blank = False

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_text(payload)


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
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Nieprawidłowy JSON w {path}, linia {line_number}: {error}"
                ) from error

            if not isinstance(record, dict):
                raise ValueError(
                    f"Rekord w {path}, linia {line_number} nie jest obiektem JSON."
                )

            records.append(record)

    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=False,
                    separators=(",", ":"),
                )
            )
            file.write("\n")


def read_blob(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono blobu: {path}")

    with gzip.open(path, "rt", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"Blob {path} nie zawiera obiektu JSON.")

    return payload


def compact_unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        candidate = normalize_text(value)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)

    return result


def dedupe_dicts(records: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for record in records:
        key = tuple(record.get(field) for field in keys)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)

    return result


def priority_key(value: Any) -> tuple[int, str]:
    priority = str(value or "").upper()
    return (PRIORITY_RANK.get(priority, 99), priority)


def choose_attachment_title(group: list[dict[str, Any]]) -> str:
    metadata_titles: list[str] = []
    anchor_titles: list[str] = []

    for record in group:
        metadata = record.get("attachment_metadata")
        if isinstance(metadata, dict):
            metadata_titles.extend(
                [
                    metadata.get("title"),
                    metadata.get("caption"),
                    metadata.get("description"),
                ]
            )

        linked_from = record.get("linked_from")
        if isinstance(linked_from, list):
            for link in linked_from:
                if not isinstance(link, dict):
                    continue
                anchor_titles.append(link.get("anchor_text"))

    candidates = compact_unique_strings(metadata_titles)
    if candidates:
        return max(candidates, key=lambda item: (len(item.split()), len(item)))

    candidates = compact_unique_strings(anchor_titles)
    if candidates:
        return max(candidates, key=lambda item: (len(item.split()), len(item)))

    filename = normalize_text(group[0].get("filename")) or "Załącznik"
    stem = Path(filename).stem
    stem = re.sub(r"[_-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or filename


def web_document_record(document: dict[str, Any]) -> dict[str, Any] | None:
    text = normalize_text(document.get("text"))
    if not document.get("indexable") or word_count(text) < 10:
        return None

    corpus_id = f"web:{document['id']}"
    title = normalize_text(document.get("title")) or document.get("url") or corpus_id
    url = str(document.get("url") or "").strip()

    metadata = {
        "content_type": document.get("content_type"),
        "wordpress_type": document.get("wordpress_type"),
        "wordpress_id": document.get("wordpress_id"),
        "status": document.get("status"),
        "slug": document.get("slug"),
        "parent_wordpress_id": document.get("parent_wordpress_id"),
        "menu_order": document.get("menu_order"),
        "author_id": document.get("author_id"),
        "featured_media_id": document.get("featured_media_id"),
        "taxonomies": document.get("taxonomies") or {},
        "headings": document.get("headings") or [],
        "original_document_id": document.get("id"),
    }

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": corpus_id,
        "kind": "web",
        "source_id": document.get("source_id"),
        "source_ids": [document.get("source_id")],
        "source_name": document.get("source_name"),
        "source_priority": document.get("source_priority"),
        "title": title,
        "url": url,
        "url_aliases": [url] if url else [],
        "language": document.get("language"),
        "text": text,
        "char_count": len(text),
        "word_count": word_count(text),
        "content_sha256": sha256_text(text),
        "published_at": document.get("published_at"),
        "modified_at": document.get("modified_at"),
        "fetched_at": document.get("fetched_at"),
        "parent_document_ids": [],
        "linked_from": [],
        "metadata": metadata,
    }

    record["metadata_sha256"] = stable_json_hash(
        {
            "source_id": record["source_id"],
            "source_priority": record["source_priority"],
            "title": record["title"],
            "url": record["url"],
            "language": record["language"],
            "published_at": record["published_at"],
            "modified_at": record["modified_at"],
            "metadata": record["metadata"],
        }
    )
    return record


def attachment_document_record(
    content_hash: str,
    group: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    primary = sorted(
        group,
        key=lambda record: (
            priority_key(record.get("source_priority")),
            str(record.get("source_id") or ""),
            str(record.get("effective_url") or ""),
        ),
    )[0]

    blob_path = Path(str(primary.get("blob_path") or ""))
    blob = read_blob(blob_path)
    raw_text = str(blob.get("text") or "")

    expected_text_hash = str(primary.get("text_sha256") or "")
    raw_text_hash = sha256_text(raw_text)
    if expected_text_hash and expected_text_hash != raw_text_hash:
        raise ValueError(
            f"Niezgodny hash tekstu źródłowego dla {primary.get('filename')}: "
            f"{expected_text_hash} != {raw_text_hash}"
        )

    text = normalize_text(raw_text)
    if word_count(text) < 10:
        raise ValueError(
            f"Załącznik {primary.get('filename')} ma za mało tekstu po ekstrakcji."
        )

    actual_text_hash = sha256_text(text)

    urls: list[str] = []
    source_ids: list[str] = []
    source_names: list[str] = []
    attachment_ids: list[str] = []
    filenames: list[str] = []
    linked_from: list[dict[str, Any]] = []
    metadata_records: list[dict[str, Any]] = []

    for record in group:
        urls.append(record.get("effective_url"))
        urls.extend(record.get("url_aliases") or [])
        source_ids.append(record.get("source_id"))
        source_names.append(record.get("source_name"))
        attachment_ids.append(record.get("attachment_id"))
        filenames.append(record.get("filename"))

        links = record.get("linked_from")
        if isinstance(links, list):
            linked_from.extend(link for link in links if isinstance(link, dict))

        metadata = record.get("attachment_metadata")
        if isinstance(metadata, dict) and metadata:
            metadata_records.append(metadata)

    urls = compact_unique_strings(urls)
    source_ids = compact_unique_strings(source_ids)
    source_names = compact_unique_strings(source_names)
    attachment_ids = compact_unique_strings(attachment_ids)
    filenames = compact_unique_strings(filenames)
    linked_from = dedupe_dicts(
        linked_from,
        ("document_id", "document_url", "anchor_text"),
    )

    parent_document_ids = compact_unique_strings(
        f"web:{link.get('document_id')}"
        for link in linked_from
        if link.get("document_id")
    )

    title = choose_attachment_title(group)
    canonical_url = str(primary.get("effective_url") or "").strip()
    if canonical_url and canonical_url not in urls:
        urls.insert(0, canonical_url)

    published_values = compact_unique_strings(
        metadata.get("published_at") for metadata in metadata_records
    )
    modified_values = compact_unique_strings(
        metadata.get("modified_at") for metadata in metadata_records
    )

    metadata = {
        "attachment_ids": attachment_ids,
        "binary_sha256": content_hash,
        "blob_path": str(blob_path),
        "filenames": filenames,
        "extension": primary.get("extension"),
        "parser": blob.get("parser") or primary.get("parser"),
        "extraction_status": blob.get("extraction_status")
        or primary.get("extraction_status"),
        "section_count": len(blob.get("sections") or []),
        "metrics": blob.get("metrics") or primary.get("metrics") or {},
        "warnings": blob.get("warnings") or primary.get("warnings") or [],
        "attachment_metadata": metadata_records,
    }

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": f"attachment:sha256:{content_hash}",
        "kind": "attachment",
        "source_id": primary.get("source_id"),
        "source_ids": source_ids,
        "source_name": primary.get("source_name"),
        "source_names": source_names,
        "source_priority": primary.get("source_priority"),
        "title": title,
        "url": canonical_url,
        "url_aliases": urls,
        "language": None,
        "text": text,
        "char_count": len(text),
        "word_count": word_count(text),
        "content_sha256": actual_text_hash,
        "published_at": min(published_values) if published_values else None,
        "modified_at": max(modified_values) if modified_values else None,
        "fetched_at": primary.get("checked_at") or primary.get("extracted_at"),
        "parent_document_ids": parent_document_ids,
        "linked_from": linked_from,
        "metadata": metadata,
    }

    record["metadata_sha256"] = stable_json_hash(
        {
            "source_ids": record["source_ids"],
            "source_priority": record["source_priority"],
            "title": record["title"],
            "url": record["url"],
            "url_aliases": record["url_aliases"],
            "published_at": record["published_at"],
            "modified_at": record["modified_at"],
            "parent_document_ids": record["parent_document_ids"],
            "linked_from": record["linked_from"],
            "metadata": record["metadata"],
        }
    )

    return record, blob


def assess_attachment_text_quality(
    record: dict[str, Any],
) -> dict[str, Any]:
    text = str(record.get("text") or "")
    tokens = re.findall(r"\S+", text)
    token_count = len(tokens)
    single_character_tokens = sum(
        1
        for token in tokens
        if len(re.sub(r"[^\wÀ-ž]", "", token)) == 1
    )
    alphabetic_characters = sum(character.isalpha() for character in text)
    visible_characters = sum(not character.isspace() for character in text)

    single_character_ratio = (
        single_character_tokens / token_count
        if token_count
        else 1.0
    )
    alphabetic_character_ratio = (
        alphabetic_characters / visible_characters
        if visible_characters
        else 0.0
    )

    parser = str(
        (record.get("metadata") or {}).get("parser") or ""
    )
    accepted = True
    reason = None

    if parser == "tesseract-ocr" and (
        single_character_ratio > 0.25
        or alphabetic_character_ratio < 0.60
    ):
        accepted = False
        reason = "low_quality_ocr"

    return {
        "accepted": accepted,
        "reason": reason,
        "parser": parser,
        "token_count": token_count,
        "single_character_token_ratio": round(single_character_ratio, 4),
        "alphabetic_character_ratio": round(alphabetic_character_ratio, 4),
    }


def sliding_word_chunks(
    text: str,
    max_words: int,
    overlap_words: int,
) -> Iterator[str]:
    words = re.findall(r"\S+", normalize_text(text))
    if not words:
        return

    if len(words) <= max_words:
        yield " ".join(words)
        return

    step = max_words - overlap_words
    if step <= 0:
        raise ValueError("overlap_words musi być mniejsze niż max_words.")

    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunk = " ".join(words[start:end]).strip()
        if chunk:
            yield chunk
        if end >= len(words):
            break
        start += step


def attachment_segments(
    blob: dict[str, Any],
    fallback_text: str,
    max_words: int,
    overlap_words: int,
) -> list[dict[str, Any]]:
    sections = blob.get("sections")
    if not isinstance(sections, list) or not sections:
        return [
            {
                "text": chunk,
                "section_ids": [],
                "section_labels": [],
                "section_kind": None,
                "page_start": None,
                "page_end": None,
                "slide_start": None,
                "slide_end": None,
                "sheet_names": [],
            }
            for chunk in sliding_word_chunks(fallback_text, max_words, overlap_words)
        ]

    segments: list[dict[str, Any]] = []

    for section in sections:
        if not isinstance(section, dict):
            continue

        section_text = normalize_text(section.get("text"))
        if not section_text:
            continue

        section_id = str(section.get("section_id") or "")
        section_label = str(section.get("label") or "")
        section_kind = section.get("kind")
        section_metadata = section.get("metadata")
        if not isinstance(section_metadata, dict):
            section_metadata = {}

        for chunk in sliding_word_chunks(section_text, max_words, overlap_words):
            page_number = section_metadata.get("page_number")
            slide_number = section_metadata.get("slide_number")
            sheet_name = section_metadata.get("sheet_name")

            segments.append(
                {
                    "text": chunk,
                    "section_ids": [section_id] if section_id else [],
                    "section_labels": [section_label] if section_label else [],
                    "section_kind": section_kind,
                    "page_start": page_number,
                    "page_end": page_number,
                    "slide_start": slide_number,
                    "slide_end": slide_number,
                    "sheet_names": [sheet_name] if sheet_name else [],
                }
            )

    if not segments:
        return [
            {
                "text": chunk,
                "section_ids": [],
                "section_labels": [],
                "section_kind": None,
                "page_start": None,
                "page_end": None,
                "slide_start": None,
                "slide_end": None,
                "sheet_names": [],
            }
            for chunk in sliding_word_chunks(fallback_text, max_words, overlap_words)
        ]

    return segments


def build_chunks(
    document: dict[str, Any],
    max_words: int,
    overlap_words: int,
    blob: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if document["kind"] == "attachment" and blob is not None:
        segments = attachment_segments(
            blob=blob,
            fallback_text=document["text"],
            max_words=max_words,
            overlap_words=overlap_words,
        )
    else:
        segments = [
            {
                "text": chunk,
                "section_ids": [],
                "section_labels": [],
                "section_kind": None,
                "page_start": None,
                "page_end": None,
                "slide_start": None,
                "slide_end": None,
                "sheet_names": [],
            }
            for chunk in sliding_word_chunks(
                document["text"],
                max_words,
                overlap_words,
            )
        ]

    chunks: list[dict[str, Any]] = []
    total = len(segments)

    for index, segment in enumerate(segments):
        chunk_text = normalize_text(segment["text"])
        if word_count(chunk_text) < 5:
            continue

        prefix = f"{document['title']}\n\n"
        embedding_text = prefix + chunk_text
        chunk_id = f"{document['id']}:chunk:{index + 1:04d}"

        chunks.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": chunk_id,
                "document_id": document["id"],
                "document_kind": document["kind"],
                "source_id": document["source_id"],
                "source_ids": document.get("source_ids") or [],
                "source_priority": document.get("source_priority"),
                "title": document["title"],
                "url": document["url"],
                "url_aliases": document.get("url_aliases") or [],
                "language": document.get("language"),
                "chunk_index": index,
                "chunk_number": index + 1,
                "chunk_count": total,
                "text": chunk_text,
                "embedding_text": embedding_text,
                "char_count": len(chunk_text),
                "word_count": word_count(chunk_text),
                "text_sha256": sha256_text(chunk_text),
                "document_content_sha256": document["content_sha256"],
                "published_at": document.get("published_at"),
                "modified_at": document.get("modified_at"),
                "parent_document_ids": document.get("parent_document_ids") or [],
                "section_ids": segment.get("section_ids") or [],
                "section_labels": segment.get("section_labels") or [],
                "section_kind": segment.get("section_kind"),
                "page_start": segment.get("page_start"),
                "page_end": segment.get("page_end"),
                "slide_start": segment.get("slide_start"),
                "slide_end": segment.get("slide_end"),
                "sheet_names": segment.get("sheet_names") or [],
            }
        )

    for index, chunk in enumerate(chunks):
        chunk["chunk_index"] = index
        chunk["chunk_number"] = index + 1
        chunk["chunk_count"] = len(chunks)
        chunk["id"] = f"{document['id']}:chunk:{index + 1:04d}"

    return chunks


def build_state_record(
    document: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": document["id"],
        "kind": document["kind"],
        "source_id": document["source_id"],
        "url": document["url"],
        "content_sha256": document["content_sha256"],
        "metadata_sha256": document["metadata_sha256"],
        "chunk_ids": [chunk["id"] for chunk in chunks],
        "chunk_text_sha256": [chunk["text_sha256"] for chunk in chunks],
        "chunk_count": len(chunks),
    }


def build_changes(
    previous_state: list[dict[str, Any]],
    current_state: list[dict[str, Any]],
    generated_at: str,
) -> list[dict[str, Any]]:
    previous = {
        str(record.get("document_id")): record
        for record in previous_state
        if record.get("document_id")
    }
    current = {
        str(record.get("document_id")): record
        for record in current_state
        if record.get("document_id")
    }

    changes: list[dict[str, Any]] = []

    for document_id in sorted(current):
        current_record = current[document_id]
        previous_record = previous.get(document_id)

        if previous_record is None:
            action = "new"
            reason = "document_added"
        elif (
            previous_record.get("content_sha256")
            != current_record.get("content_sha256")
        ):
            action = "changed"
            reason = "content_changed"
        elif (
            previous_record.get("metadata_sha256")
            != current_record.get("metadata_sha256")
        ):
            action = "changed"
            reason = "metadata_changed"
        elif (
            previous_record.get("chunk_text_sha256")
            != current_record.get("chunk_text_sha256")
        ):
            action = "changed"
            reason = "chunking_changed"
        else:
            action = "unchanged"
            reason = "no_change"

        changes.append(
            {
                "schema_version": SCHEMA_VERSION,
                "document_id": document_id,
                "action": action,
                "reason": reason,
                "kind": current_record.get("kind"),
                "source_id": current_record.get("source_id"),
                "url": current_record.get("url"),
                "previous_content_sha256": (
                    previous_record.get("content_sha256")
                    if previous_record
                    else None
                ),
                "current_content_sha256": current_record.get("content_sha256"),
                "delete_chunk_ids": (
                    previous_record.get("chunk_ids", [])
                    if previous_record and action == "changed"
                    else []
                ),
                "upsert_chunk_ids": (
                    current_record.get("chunk_ids", [])
                    if action in {"new", "changed"}
                    else []
                ),
                "generated_at": generated_at,
            }
        )

    for document_id in sorted(set(previous) - set(current)):
        previous_record = previous[document_id]
        changes.append(
            {
                "schema_version": SCHEMA_VERSION,
                "document_id": document_id,
                "action": "deleted",
                "reason": "document_removed",
                "kind": previous_record.get("kind"),
                "source_id": previous_record.get("source_id"),
                "url": previous_record.get("url"),
                "previous_content_sha256": previous_record.get("content_sha256"),
                "current_content_sha256": None,
                "delete_chunk_ids": previous_record.get("chunk_ids", []),
                "upsert_chunk_ids": [],
                "generated_at": generated_at,
            }
        )

    return changes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Buduje zunifikowany korpus i chunki RAG dla treści UEW."
    )
    parser.add_argument("--max-words", type=int, default=350)
    parser.add_argument("--overlap-words", type=int, default=50)
    arguments = parser.parse_args()

    if arguments.max_words < 50:
        raise ValueError("--max-words musi wynosić co najmniej 50.")
    if arguments.overlap_words < 0:
        raise ValueError("--overlap-words nie może być ujemne.")
    if arguments.overlap_words >= arguments.max_words:
        raise ValueError("--overlap-words musi być mniejsze niż --max-words.")

    generated_at = utc_now()
    source_documents = load_jsonl(DOCUMENTS_PATH)
    attachment_index = load_jsonl(ATTACHMENT_INDEX_PATH)
    previous_state = load_jsonl(STATE_PATH)

    corpus_documents: list[dict[str, Any]] = []
    all_chunks: list[dict[str, Any]] = []
    current_state: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []

    web_count = 0
    attachment_count = 0
    attachment_duplicate_record_count = 0

    for source_document in source_documents:
        record = web_document_record(source_document)
        if record is None:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "web",
                    "id": f"web:{source_document.get('id')}",
                    "source_id": source_document.get("source_id"),
                    "title": source_document.get("title"),
                    "url": source_document.get("url"),
                    "reason": "not_indexable",
                    "word_count": source_document.get("word_count"),
                }
            )
            continue

        chunks = build_chunks(
            record,
            max_words=arguments.max_words,
            overlap_words=arguments.overlap_words,
        )
        if not chunks:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "web",
                    "id": record["id"],
                    "source_id": record.get("source_id"),
                    "title": record.get("title"),
                    "url": record.get("url"),
                    "reason": "no_chunks_created",
                    "word_count": record.get("word_count"),
                }
            )
            continue

        corpus_documents.append(record)
        all_chunks.extend(chunks)
        current_state.append(build_state_record(record, chunks))
        web_count += 1

    attachment_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for attachment in attachment_index:
        status = str(attachment.get("extraction_status") or "")
        content_hash = str(attachment.get("content_sha256") or "")

        if status not in INDEXABLE_ATTACHMENT_STATUSES:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "attachment",
                    "id": attachment.get("attachment_id"),
                    "source_id": attachment.get("source_id"),
                    "title": attachment.get("filename"),
                    "url": attachment.get("effective_url"),
                    "reason": status or "unknown_extraction_status",
                    "word_count": attachment.get("word_count"),
                    "binary_sha256": content_hash or None,
                }
            )
            continue

        if not content_hash:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "attachment",
                    "id": attachment.get("attachment_id"),
                    "source_id": attachment.get("source_id"),
                    "title": attachment.get("filename"),
                    "url": attachment.get("effective_url"),
                    "reason": "missing_content_sha256",
                    "word_count": attachment.get("word_count"),
                }
            )
            continue

        if int(attachment.get("word_count") or 0) < 10:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "attachment",
                    "id": attachment.get("attachment_id"),
                    "source_id": attachment.get("source_id"),
                    "title": attachment.get("filename"),
                    "url": attachment.get("effective_url"),
                    "reason": "low_text",
                    "word_count": attachment.get("word_count"),
                    "binary_sha256": content_hash,
                }
            )
            continue

        attachment_groups[content_hash].append(attachment)

    attachment_duplicate_record_count = sum(
        len(group) - 1 for group in attachment_groups.values()
    )

    for content_hash in sorted(attachment_groups):
        group = attachment_groups[content_hash]
        record, blob = attachment_document_record(content_hash, group)
        quality = assess_attachment_text_quality(record)
        record["metadata"]["quality"] = quality

        if not quality["accepted"]:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "attachment",
                    "id": record["id"],
                    "source_id": record.get("source_id"),
                    "title": record.get("title"),
                    "url": record.get("url"),
                    "reason": quality["reason"],
                    "word_count": record.get("word_count"),
                    "binary_sha256": content_hash,
                    "quality": quality,
                }
            )
            continue

        chunks = build_chunks(
            record,
            max_words=arguments.max_words,
            overlap_words=arguments.overlap_words,
            blob=blob,
        )

        if not chunks:
            exclusions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "attachment",
                    "id": record["id"],
                    "source_id": record.get("source_id"),
                    "title": record.get("title"),
                    "url": record.get("url"),
                    "reason": "no_chunks_created",
                    "word_count": record.get("word_count"),
                    "binary_sha256": content_hash,
                }
            )
            continue

        corpus_documents.append(record)
        all_chunks.extend(chunks)
        current_state.append(build_state_record(record, chunks))
        attachment_count += 1

    corpus_documents.sort(key=lambda record: record["id"])
    all_chunks.sort(key=lambda record: record["id"])
    current_state.sort(key=lambda record: record["document_id"])
    exclusions.sort(
        key=lambda record: (
            str(record.get("kind") or ""),
            str(record.get("source_id") or ""),
            str(record.get("id") or ""),
        )
    )

    document_ids = [record["id"] for record in corpus_documents]
    chunk_ids = [record["id"] for record in all_chunks]

    if len(document_ids) != len(set(document_ids)):
        raise RuntimeError("W korpusie występują zduplikowane identyfikatory dokumentów.")
    if len(chunk_ids) != len(set(chunk_ids)):
        raise RuntimeError("W korpusie występują zduplikowane identyfikatory chunków.")

    changes = build_changes(previous_state, current_state, generated_at)

    write_jsonl(CORPUS_DOCUMENTS_PATH, corpus_documents)
    write_jsonl(CHUNKS_PATH, all_chunks)
    write_jsonl(STATE_PATH, current_state)
    write_jsonl(EXCLUSIONS_PATH, exclusions)
    write_jsonl(CHANGES_PATH, changes)

    document_kind_counts = Counter(record["kind"] for record in corpus_documents)
    document_source_counts = Counter(
        str(record.get("source_id") or "unknown") for record in corpus_documents
    )
    chunk_kind_counts = Counter(record["document_kind"] for record in all_chunks)
    chunk_source_counts = Counter(
        str(record.get("source_id") or "unknown") for record in all_chunks
    )
    change_counts = Counter(record["action"] for record in changes)
    exclusion_counts = Counter(record["reason"] for record in exclusions)

    status = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "chunking": {
            "max_words": arguments.max_words,
            "overlap_words": arguments.overlap_words,
            "embedding_prefix": "document_title",
        },
        "input_counts": {
            "web_records": len(source_documents),
            "attachment_index_records": len(attachment_index),
            "previous_state_records": len(previous_state),
        },
        "corpus_counts": {
            "documents": len(corpus_documents),
            "web_documents": web_count,
            "attachment_documents": attachment_count,
            "attachment_duplicate_records_merged": attachment_duplicate_record_count,
            "chunks": len(all_chunks),
            "exclusions": len(exclusions),
        },
        "text_counts": {
            "document_words": sum(record["word_count"] for record in corpus_documents),
            "document_characters": sum(record["char_count"] for record in corpus_documents),
            "chunk_words_with_overlap": sum(record["word_count"] for record in all_chunks),
            "chunk_characters_with_overlap": sum(record["char_count"] for record in all_chunks),
        },
        "document_kind_counts": dict(sorted(document_kind_counts.items())),
        "document_source_counts": dict(sorted(document_source_counts.items())),
        "chunk_kind_counts": dict(sorted(chunk_kind_counts.items())),
        "chunk_source_counts": dict(sorted(chunk_source_counts.items())),
        "change_counts": dict(sorted(change_counts.items())),
        "exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
        "integrity": {
            "duplicate_document_id_count": len(document_ids) - len(set(document_ids)),
            "duplicate_chunk_id_count": len(chunk_ids) - len(set(chunk_ids)),
            "documents_without_chunks": len(corpus_documents)
            - len({chunk["document_id"] for chunk in all_chunks}),
        },
        "files": {
            "documents": str(CORPUS_DOCUMENTS_PATH),
            "chunks": str(CHUNKS_PATH),
            "state": str(STATE_PATH),
            "exclusions": str(EXCLUSIONS_PATH),
            "changes": str(CHANGES_PATH),
        },
    }

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Zbudowano zunifikowany korpus RAG.")
    print(f"Dokumenty: {len(corpus_documents)}")
    print(f"- strony WWW: {web_count}")
    print(f"- załączniki: {attachment_count}")
    print(f"Chunki: {len(all_chunks)}")
    print(f"Wykluczenia: {len(exclusions)}")
    print(f"Zmiany: {dict(sorted(change_counts.items()))}")


if __name__ == "__main__":
    main()
