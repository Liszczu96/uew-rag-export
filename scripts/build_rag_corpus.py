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


POLISH_STOPWORDS = {
    "aby", "albo", "bardzo", "bez", "będzie", "być", "dla", "do", "jest",
    "które", "który", "na", "nie", "oraz", "po", "przez", "się", "są", "to",
    "w", "we", "z", "za", "że",
}
ENGLISH_STOPWORDS = {
    "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "that", "the", "this", "to", "with",
}


def detect_language(text: str) -> str | None:
    sample = normalize_text(text)[:12000]
    if not sample:
        return None

    tokens = re.findall(r"[A-Za-zÀ-ž]+", sample.lower(), flags=re.UNICODE)
    if not tokens:
        return None

    polish_score = sum(token in POLISH_STOPWORDS for token in tokens)
    english_score = sum(token in ENGLISH_STOPWORDS for token in tokens)
    polish_score += sum(character in "ąćęłńóśźż" for character in sample.lower()) * 0.35

    if polish_score == 0 and english_score == 0:
        return None
    if polish_score >= english_score * 1.25:
        return "pl"
    if english_score >= polish_score * 1.25:
        return "en"
    return "pl-en"


def clean_title(value: Any, fallback: str = "Dokument") -> str:
    title = normalize_text(value)
    if not title:
        return fallback

    title = title.replace("_", " ")
    title = re.sub(r"(?<=\\w)-(?=\\w)", "-", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—_")
    return title or fallback


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
    return clean_title(stem or filename, "Załącznik")


def web_document_record(document: dict[str, Any]) -> dict[str, Any] | None:
    text = normalize_text(document.get("text"))
    if not document.get("indexable") or word_count(text) < 10:
        return None

    corpus_id = f"web:{document['id']}"
    title = clean_title(document.get("title"), document.get("url") or corpus_id)
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
        "language": document.get("language") or detect_language(text),
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
        "language": detect_language(text),
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


def split_sentences(text: str) -> list[str]:
    value = normalize_text(text)
    if not value:
        return []

    protected = value
    abbreviations = {
        "np.": "np<prd>",
        "nr.": "nr<prd>",
        "art.": "art<prd>",
        "ust.": "ust<prd>",
        "pkt.": "pkt<prd>",
        "poz.": "poz<prd>",
        "r.": "r<prd>",
        "dr.": "dr<prd>",
        "prof.": "prof<prd>",
        "mgr.": "mgr<prd>",
        "inż.": "inż<prd>",
        "e.g.": "e<prd>g<prd>",
        "i.e.": "i<prd>e<prd>",
    }
    for abbreviation, replacement in abbreviations.items():
        protected = re.sub(
            re.escape(abbreviation),
            replacement,
            protected,
            flags=re.IGNORECASE,
        )

    parts = re.split(
        r"(?<=[.!?…])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ0-9„\"(§])",
        protected,
    )

    sentences: list[str] = []
    for part in parts:
        restored = part.replace("<prd>", ".").strip()
        if restored:
            sentences.append(restored)

    return sentences or [value]


LIST_ITEM_PATTERN = re.compile(
    r"^(?:[-*•▪◦–—]|(?:\d+|[A-Za-z]|[IVXLCDM]+)[.)]|§\s*\d+)",
    re.IGNORECASE,
)
NUMBERED_HEADING_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+){0,5}[.)]?|§\s*\d+)\s+\S+",
    re.IGNORECASE,
)
GENERIC_SECTION_LABEL_PATTERN = re.compile(
    r"^(?:strona|page|slajd|slide|arkusz|sheet)\s+\d+$",
    re.IGNORECASE,
)


def looks_like_heading(line: str) -> bool:
    value = normalize_text(line)
    words = re.findall(r"\S+", value)

    if not value or len(words) > 18 or len(value) > 180:
        return False
    if LIST_ITEM_PATTERN.match(value):
        return False
    if NUMBERED_HEADING_PATTERN.match(value):
        return True
    if value.endswith(":") and len(words) <= 14:
        return True
    if value.endswith((".", "!", "?", ";", ",")):
        return False

    alphabetic = [character for character in value if character.isalpha()]
    if alphabetic:
        uppercase_ratio = sum(character.isupper() for character in alphabetic) / len(alphabetic)
        if uppercase_ratio >= 0.72 and len(words) >= 2:
            return True

    return len(words) <= 10 and value[:1].isupper()


def canonical_unit_key(text: str) -> str:
    value = normalize_text(text).lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def collapse_repeated_prefix(text: str) -> str:
    value = normalize_text(text)
    words = re.findall(r"\S+", value)

    if len(words) < 30:
        return value

    maximum_block = min(120, len(words) // 3)

    for block_length in range(8, maximum_block + 1):
        block = words[:block_length]
        repeat_count = 1

        while (repeat_count + 1) * block_length <= len(words):
            start = repeat_count * block_length
            end = start + block_length
            if words[start:end] != block:
                break
            repeat_count += 1

        if repeat_count >= 3:
            remainder = words[repeat_count * block_length:]
            return normalize_text(" ".join([*block, *remainder]))

    return value


def collapse_repeated_word_runs(text: str) -> str:
    value = normalize_text(text)
    words = re.findall(r"\S+", value)

    if len(words) < 16:
        return value

    result: list[str] = []
    index = 0

    while index < len(words):
        matched = False
        maximum_block = min(24, (len(words) - index) // 4)

        for block_length in range(maximum_block, 1, -1):
            block = words[index:index + block_length]
            repeat_count = 1

            while index + (repeat_count + 1) * block_length <= len(words):
                start = index + repeat_count * block_length
                end = start + block_length
                if words[start:end] != block:
                    break
                repeat_count += 1

            if repeat_count >= 4:
                result.extend(block)
                index += repeat_count * block_length
                matched = True
                break

        if not matched:
            result.append(words[index])
            index += 1

    return normalize_text(" ".join(result))


def split_paragraph_into_units(text: str) -> list[dict[str, Any]]:
    paragraph = collapse_repeated_word_runs(collapse_repeated_prefix(text))
    if not paragraph:
        return []

    lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
    if not lines:
        return []

    units: list[dict[str, Any]] = []
    buffer: list[str] = []
    list_buffer: list[str] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        value = normalize_text(" ".join(buffer))
        buffer.clear()
        if value:
            units.append({"text": value, "unit_kind": "paragraph"})

    def flush_list() -> None:
        if not list_buffer:
            return
        value = normalize_text("\n".join(list_buffer))
        list_buffer.clear()
        if value:
            units.append({"text": value, "unit_kind": "list"})

    for line in lines:
        if looks_like_heading(line):
            flush_buffer()
            flush_list()
            units.append({"text": line, "unit_kind": "heading"})
            continue

        if LIST_ITEM_PATTERN.match(line):
            flush_buffer()
            list_buffer.append(line)
            continue

        if list_buffer:
            if line[:1].islower() or not re.search(r"[.!?…:]$", list_buffer[-1]):
                list_buffer[-1] = normalize_text(list_buffer[-1] + " " + line)
                continue
            flush_list()

        buffer.append(line)

    flush_buffer()
    flush_list()

    if len(units) == 1 and units[0]["unit_kind"] == "paragraph":
        value = units[0]["text"]
        if word_count(value) > 180:
            sentences = split_sentences(value)
            if len(sentences) > 1:
                return [
                    {"text": sentence, "unit_kind": "sentence_group"}
                    for sentence in sentences
                    if normalize_text(sentence)
                ]

    return units


def natural_units_from_text(
    text: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    value = normalize_text(text)
    if not value:
        return []

    base_metadata = dict(metadata or {})
    paragraphs = re.split(r"\n\s*\n+", value)
    units: list[dict[str, Any]] = []

    for paragraph in paragraphs:
        for unit in split_paragraph_into_units(paragraph):
            payload = dict(base_metadata)
            payload.update(unit)
            payload["text"] = normalize_text(payload.get("text"))
            if payload["text"]:
                units.append(payload)

    return dedupe_natural_units(units)


def dedupe_natural_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    globally_seen_long_units: set[str] = set()
    previous_key: str | None = None

    for unit in units:
        text = normalize_text(unit.get("text"))
        key = canonical_unit_key(text)
        if not key:
            continue

        if key == previous_key:
            continue

        unit_words = word_count(text)
        if unit_words >= 18 and key in globally_seen_long_units:
            continue

        if unit_words >= 18:
            globally_seen_long_units.add(key)

        payload = dict(unit)
        payload["text"] = text
        result.append(payload)
        previous_key = key

    return result


def split_oversized_unit(
    unit: dict[str, Any],
    hard_max_words: int,
) -> list[dict[str, Any]]:
    text = normalize_text(unit.get("text"))
    if word_count(text) <= hard_max_words:
        return [unit]

    sentences = split_sentences(text)
    if len(sentences) <= 1:
        sentences = [
            normalize_text(part)
            for part in re.split(r"(?<=[;:])\s+", text)
            if normalize_text(part)
        ]

    if len(sentences) <= 1:
        words = re.findall(r"\S+", text)
        pieces: list[dict[str, Any]] = []
        for start in range(0, len(words), hard_max_words):
            payload = dict(unit)
            payload["text"] = " ".join(words[start:start + hard_max_words])
            payload["forced_split"] = True
            pieces.append(payload)
        return pieces

    pieces: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_words = 0

    for sentence in sentences:
        sentence_words = word_count(sentence)

        if sentence_words > hard_max_words:
            if buffer:
                payload = dict(unit)
                payload["text"] = normalize_text(" ".join(buffer))
                payload["forced_split"] = True
                pieces.append(payload)
                buffer = []
                buffer_words = 0

            sentence_tokens = re.findall(r"\S+", sentence)
            for start in range(0, len(sentence_tokens), hard_max_words):
                payload = dict(unit)
                payload["text"] = " ".join(
                    sentence_tokens[start:start + hard_max_words]
                )
                payload["forced_split"] = True
                pieces.append(payload)
            continue

        if buffer and buffer_words + sentence_words > hard_max_words:
            payload = dict(unit)
            payload["text"] = normalize_text(" ".join(buffer))
            payload["forced_split"] = True
            pieces.append(payload)
            buffer = []
            buffer_words = 0

        buffer.append(sentence)
        buffer_words += sentence_words

    if buffer:
        payload = dict(unit)
        payload["text"] = normalize_text(" ".join(buffer))
        payload["forced_split"] = True
        pieces.append(payload)

    return pieces


def attachment_natural_units(
    blob: dict[str, Any],
    fallback_text: str,
) -> list[dict[str, Any]]:
    sections = blob.get("sections")
    if not isinstance(sections, list) or not sections:
        return natural_units_from_text(fallback_text)

    units: list[dict[str, Any]] = []

    for section in sections:
        if not isinstance(section, dict):
            continue

        section_text = normalize_text(section.get("text"))
        if not section_text:
            continue

        section_metadata = section.get("metadata")
        if not isinstance(section_metadata, dict):
            section_metadata = {}

        metadata = {
            "section_id": str(section.get("section_id") or ""),
            "section_label": normalize_text(section.get("label")),
            "section_kind": section.get("kind"),
            "page_number": section_metadata.get("page_number"),
            "slide_number": section_metadata.get("slide_number"),
            "sheet_name": section_metadata.get("sheet_name"),
        }
        units.extend(natural_units_from_text(section_text, metadata))

    return dedupe_natural_units(units) or natural_units_from_text(fallback_text)


def aggregate_segment_metadata(units: list[dict[str, Any]]) -> dict[str, Any]:
    section_ids = compact_unique_strings(unit.get("section_id") for unit in units)
    section_labels = compact_unique_strings(unit.get("section_label") for unit in units)
    section_kinds = compact_unique_strings(unit.get("section_kind") for unit in units)
    pages = sorted(
        {
            int(unit["page_number"])
            for unit in units
            if isinstance(unit.get("page_number"), int)
        }
    )
    slides = sorted(
        {
            int(unit["slide_number"])
            for unit in units
            if isinstance(unit.get("slide_number"), int)
        }
    )
    sheets = compact_unique_strings(unit.get("sheet_name") for unit in units)
    headings = compact_unique_strings(
        unit.get("text")
        for unit in units
        if unit.get("unit_kind") == "heading"
    )

    meaningful_labels = [
        label
        for label in section_labels
        if not GENERIC_SECTION_LABEL_PATTERN.fullmatch(label)
    ]
    context_path = compact_unique_strings([*meaningful_labels, *headings])

    return {
        "section_ids": section_ids,
        "section_labels": section_labels,
        "section_kind": section_kinds[0] if len(section_kinds) == 1 else None,
        "page_start": min(pages) if pages else None,
        "page_end": max(pages) if pages else None,
        "slide_start": min(slides) if slides else None,
        "slide_end": max(slides) if slides else None,
        "sheet_names": sheets,
        "context_path": context_path,
        "semantic_unit_count": len(units),
        "forced_split": any(bool(unit.get("forced_split")) for unit in units),
        "unit_kinds": compact_unique_strings(unit.get("unit_kind") for unit in units),
    }


def structure_aware_segments(
    units: list[dict[str, Any]],
    target_words: int,
) -> list[dict[str, Any]]:
    hard_max_words = max(650, target_words * 2)
    minimum_preferred_words = max(45, target_words // 4)

    expanded: list[dict[str, Any]] = []
    for unit in units:
        expanded.extend(split_oversized_unit(unit, hard_max_words))

    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if not current:
            return

        text = normalize_text("\n\n".join(unit["text"] for unit in current))
        if text:
            metadata = aggregate_segment_metadata(current)
            metadata["text"] = text
            segments.append(metadata)

        current = []
        current_words = 0

    for unit in expanded:
        unit_words = word_count(unit.get("text"))
        unit_kind = str(unit.get("unit_kind") or "paragraph")
        section_kind = str(unit.get("section_kind") or "")

        strong_boundary = unit_kind == "heading" or section_kind in {"slide", "sheet"}

        if current and strong_boundary and current_words >= minimum_preferred_words:
            flush()

        if current and current_words + unit_words > hard_max_words:
            flush()

        if (
            current
            and current_words >= target_words
            and unit_kind not in {"list", "sentence_group"}
        ):
            flush()

        current.append(unit)
        current_words += unit_words

        if current_words >= hard_max_words:
            flush()

    flush()

    if len(segments) >= 2:
        last_words = word_count(segments[-1]["text"])
        previous_words = word_count(segments[-2]["text"])
        if last_words < 35 and previous_words + last_words <= hard_max_words:
            merged_text = normalize_text(
                segments[-2]["text"] + "\n\n" + segments[-1]["text"]
            )
            merged_units = []
            for key in (
                "section_ids",
                "section_labels",
                "sheet_names",
                "context_path",
                "unit_kinds",
            ):
                segments[-2][key] = compact_unique_strings(
                    [*(segments[-2].get(key) or []), *(segments[-1].get(key) or [])]
                )
            segments[-2]["text"] = merged_text
            segments[-2]["semantic_unit_count"] = int(
                segments[-2].get("semantic_unit_count") or 0
            ) + int(segments[-1].get("semantic_unit_count") or 0)
            segments[-2]["forced_split"] = bool(
                segments[-2].get("forced_split") or segments[-1].get("forced_split")
            )
            page_values = [
                value
                for value in (
                    segments[-2].get("page_start"),
                    segments[-2].get("page_end"),
                    segments[-1].get("page_start"),
                    segments[-1].get("page_end"),
                )
                if isinstance(value, int)
            ]
            slide_values = [
                value
                for value in (
                    segments[-2].get("slide_start"),
                    segments[-2].get("slide_end"),
                    segments[-1].get("slide_start"),
                    segments[-1].get("slide_end"),
                )
                if isinstance(value, int)
            ]
            segments[-2]["page_start"] = min(page_values) if page_values else None
            segments[-2]["page_end"] = max(page_values) if page_values else None
            segments[-2]["slide_start"] = min(slide_values) if slide_values else None
            segments[-2]["slide_end"] = max(slide_values) if slide_values else None
            segments.pop()

    return segments


def build_chunks(
    document: dict[str, Any],
    max_words: int,
    overlap_words: int,
    blob: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    del overlap_words  # Zachowane wyłącznie dla zgodności z istniejącymi workflowami.

    if document["kind"] == "attachment" and blob is not None:
        units = attachment_natural_units(blob, document["text"])
    else:
        units = natural_units_from_text(document["text"])

    segments = structure_aware_segments(
        units=units,
        target_words=max_words,
    )

    chunks: list[dict[str, Any]] = []

    for segment in segments:
        chunk_text = normalize_text(segment.get("text"))
        if word_count(chunk_text) < 5:
            continue

        context_path = segment.get("context_path") or []
        embedding_parts = [document["title"]]
        if context_path:
            embedding_parts.append(" > ".join(context_path))
        embedding_parts.append(chunk_text)
        embedding_text = "\n\n".join(embedding_parts)

        chunks.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": "",
                "document_id": document["id"],
                "document_kind": document["kind"],
                "source_id": document["source_id"],
                "source_ids": document.get("source_ids") or [],
                "source_priority": document.get("source_priority"),
                "title": document["title"],
                "url": document["url"],
                "url_aliases": document.get("url_aliases") or [],
                "language": document.get("language") or detect_language(chunk_text),
                "chunk_index": 0,
                "chunk_number": 0,
                "chunk_count": 0,
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
                "context_path": context_path,
                "semantic_unit_count": segment.get("semantic_unit_count") or 0,
                "unit_kinds": segment.get("unit_kinds") or [],
                "forced_split": bool(segment.get("forced_split")),
                "chunking_method": "structure_aware_v2",
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
    parser.add_argument(
        "--max-words",
        type=int,
        default=350,
        help=(
            "Docelowa wielkość chunku. Granice naturalnych jednostek mają "
            "pierwszeństwo; twardy limit wynosi co najmniej 650 słów."
        ),
    )
    parser.add_argument(
        "--overlap-words",
        type=int,
        default=0,
        help=(
            "Parametr zachowany dla zgodności. Chunkowanie strukturalne nie "
            "kopiuje części słów między chunkami."
        ),
    )
    arguments = parser.parse_args()

    if arguments.max_words < 50:
        raise ValueError("--max-words musi wynosić co najmniej 50.")
    if arguments.overlap_words < 0:
        raise ValueError("--overlap-words nie może być ujemne.")
    if arguments.overlap_words >= arguments.max_words and arguments.overlap_words != 0:
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
            "method": "structure_aware_v2",
            "target_words": arguments.max_words,
            "hard_max_words": max(650, arguments.max_words * 2),
            "requested_overlap_words": arguments.overlap_words,
            "applied_overlap_words": 0,
            "natural_boundaries": [
                "section",
                "heading",
                "paragraph",
                "list",
                "sentence",
                "page",
                "slide",
                "sheet",
            ],
            "embedding_prefix": "document_title_and_context_path",
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
            "chunk_words_total": sum(record["word_count"] for record in all_chunks),
            "chunk_characters_total": sum(record["char_count"] for record in all_chunks),
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
