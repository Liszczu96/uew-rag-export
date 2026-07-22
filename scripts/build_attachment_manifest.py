import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


DOCUMENTS_PATH = Path("public/rag/documents.jsonl")
ATTACHMENTS_PATH = Path("public/rag/attachments.jsonl")

MANIFEST_PATH = Path("public/rag/attachment-manifest.jsonl")
UNREGISTERED_PATH = Path(
    "public/rag/unregistered-attachment-links.jsonl"
)
STATUS_PATH = Path("public/attachment-status.json")

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
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
                    f"Nieprawidłowy JSON w {path}, "
                    f"linia {line_number}: {error}"
                ) from error

            if isinstance(record, dict):
                records.append(record)

    return records


def write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
) -> None:
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


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""

    parts = urlsplit(url.strip())

    query = [
        (key, value)
        for key, value in parse_qsl(
            parts.query,
            keep_blank_values=True,
        )
        if (
            key.lower() not in TRACKING_PARAMETERS
            and not key.lower().startswith("utm_")
        )
    ]

    path = unquote(parts.path or "/")
    path = re.sub(r"/{2,}", "/", path)

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def institutional_host(host: str) -> bool:
    normalized = host.lower().strip()

    return (
        normalized == "uew.pl"
        or normalized.endswith(".uew.pl")
        or normalized == "ue.wroc.pl"
        or normalized.endswith(".ue.wroc.pl")
    )


def duplicate_key(
    attachment: dict[str, Any],
) -> tuple[str, Any, str] | None:
    filename = str(
        attachment.get("filename") or ""
    ).strip().lower()

    filesize = attachment.get("filesize")
    mime_type = str(
        attachment.get("mime_type") or ""
    ).strip().lower()

    if not filename or filesize is None:
        return None

    return filename, filesize, mime_type


def duplicate_group_id(
    key: tuple[str, Any, str],
) -> str:
    raw = "|".join(str(value) for value in key)
    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]

    return f"duplicate:{digest}"


def main() -> None:
    documents = load_jsonl(DOCUMENTS_PATH)
    attachments = load_jsonl(ATTACHMENTS_PATH)

    documents_by_wordpress: dict[
        tuple[str, Any],
        list[dict[str, Any]],
    ] = defaultdict(list)

    for document in documents:
        source_id = str(document.get("source_id") or "")
        wordpress_id = document.get("wordpress_id")

        if source_id and wordpress_id is not None:
            documents_by_wordpress[
                (source_id, wordpress_id)
            ].append(document)

    linked_from_by_url: dict[
        str,
        list[dict[str, Any]],
    ] = defaultdict(list)

    attachment_link_occurrences = 0

    for document in documents:
        links = document.get("links")

        if not isinstance(links, dict):
            continue

        attachment_links = links.get("attachments")

        if not isinstance(attachment_links, list):
            continue

        seen_in_document: set[str] = set()

        for link in attachment_links:
            if not isinstance(link, dict):
                continue

            raw_url = str(link.get("url") or "")
            normalized_url = normalize_url(raw_url)

            if not normalized_url:
                continue

            attachment_link_occurrences += 1

            if normalized_url in seen_in_document:
                continue

            seen_in_document.add(normalized_url)

            linked_from_by_url[normalized_url].append(
                {
                    "document_id": document.get("id"),
                    "document_source_id": document.get(
                        "source_id"
                    ),
                    "document_wordpress_id": document.get(
                        "wordpress_id"
                    ),
                    "document_title": document.get("title"),
                    "document_url": document.get("url"),
                    "anchor_text": link.get(
                        "anchor_text"
                    ),
                }
            )

    attachments_by_url: dict[
        str,
        list[dict[str, Any]],
    ] = defaultdict(list)

    for attachment in attachments:
        normalized_url = normalize_url(
            str(attachment.get("url") or "")
        )

        if normalized_url:
            attachments_by_url[
                normalized_url
            ].append(attachment)

    duplicate_groups: dict[
        tuple[str, Any, str],
        list[str],
    ] = defaultdict(list)

    for attachment in attachments:
        key = duplicate_key(attachment)

        if key is not None:
            duplicate_groups[key].append(
                str(attachment.get("id"))
            )

    actual_duplicate_groups = {
        key: ids
        for key, ids in duplicate_groups.items()
        if len(ids) > 1
    }

    manifest: list[dict[str, Any]] = []

    classification_counts: dict[str, int] = defaultdict(int)

    for attachment in attachments:
        attachment_id = str(attachment.get("id") or "")
        source_id = str(attachment.get("source_id") or "")
        raw_url = str(attachment.get("url") or "")
        normalized_url = normalize_url(raw_url)

        linked_from = (
            linked_from_by_url.get(
                normalized_url,
                [],
            )
            if normalized_url
            else []
        )

        parent_wordpress_id = attachment.get(
            "parent_wordpress_id"
        )

        parent_documents = (
            documents_by_wordpress.get(
                (
                    source_id,
                    parent_wordpress_id,
                ),
                [],
            )
            if parent_wordpress_id
            else []
        )

        if not normalized_url:
            classification = "missing_url"
            extraction_candidate = False
            reason = (
                "Rekord biblioteki mediów nie zawiera "
                "adresu pliku."
            )
        elif linked_from:
            classification = "linked"
            extraction_candidate = True
            reason = (
                "Adres pliku występuje bezpośrednio "
                "w aktualnej treści."
            )
        elif parent_documents:
            classification = "parent_only"
            extraction_candidate = False
            reason = (
                "Plik ma rodzica w WordPressie, ale jego "
                "adres nie występuje w aktualnej treści."
            )
        else:
            classification = "orphan"
            extraction_candidate = False
            reason = (
                "Brak bezpośredniego linku i brak rodzica "
                "w eksportowanych dokumentach."
            )

        classification_counts[classification] += 1

        key = duplicate_key(attachment)
        group_ids = (
            actual_duplicate_groups.get(key, [])
            if key is not None
            else []
        )

        manifest.append(
            {
                **attachment,
                "normalized_url": normalized_url,
                "classification": classification,
                "extraction_candidate": (
                    extraction_candidate
                ),
                "classification_reason": reason,
                "linked_from_count": len(linked_from),
                "linked_from": linked_from,
                "parent_in_export": bool(
                    parent_documents
                ),
                "parent_documents": [
                    {
                        "document_id": item.get("id"),
                        "document_title": item.get(
                            "title"
                        ),
                        "document_url": item.get("url"),
                    }
                    for item in parent_documents
                ],
                "duplicate_group_id": (
                    duplicate_group_id(key)
                    if key in actual_duplicate_groups
                    else None
                ),
                "duplicate_group_size": (
                    len(group_ids) if group_ids else 1
                ),
                "duplicate_attachment_ids": (
                    group_ids if group_ids else []
                ),
                "duplicate_canonical": (
                    bool(group_ids)
                    and attachment_id
                    == sorted(group_ids)[0]
                ),
            }
        )

    unregistered: list[dict[str, Any]] = []

    for normalized_url, linked_from in sorted(
        linked_from_by_url.items()
    ):
        if normalized_url in attachments_by_url:
            continue

        host = urlsplit(normalized_url).netloc.lower()
        is_institutional = institutional_host(host)

        unregistered.append(
            {
                "url": normalized_url,
                "host": host,
                "institutional": is_institutional,
                "extraction_candidate": is_institutional,
                "classification": (
                    "institutional_unregistered"
                    if is_institutional
                    else "external_reference"
                ),
                "linked_from_count": len(linked_from),
                "linked_from": linked_from,
            }
        )

    manifest.sort(
        key=lambda item: str(item.get("id") or "")
    )

    unregistered.sort(
        key=lambda item: str(item.get("url") or "")
    )

    write_jsonl(MANIFEST_PATH, manifest)
    write_jsonl(UNREGISTERED_PATH, unregistered)

    institutional_unregistered_count = sum(
        1
        for item in unregistered
        if item["institutional"]
    )

    external_unregistered_count = (
        len(unregistered)
        - institutional_unregistered_count
    )

    duplicate_extra_copy_count = sum(
        len(ids) - 1
        for ids in actual_duplicate_groups.values()
    )

    status = {
        "document_count": len(documents),
        "attachment_registry_count": len(attachments),
        "attachment_link_occurrence_count": (
            attachment_link_occurrences
        ),
        "unique_attachment_link_url_count": len(
            linked_from_by_url
        ),
        "registry_classifications": dict(
            sorted(classification_counts.items())
        ),
        "unregistered_link_count": len(unregistered),
        "institutional_unregistered_link_count": (
            institutional_unregistered_count
        ),
        "external_reference_count": (
            external_unregistered_count
        ),
        "high_confidence_extraction_candidate_count": (
            classification_counts["linked"]
            + institutional_unregistered_count
        ),
        "duplicate_group_count": len(
            actual_duplicate_groups
        ),
        "duplicate_extra_copy_count": (
            duplicate_extra_copy_count
        ),
        "files": {
            "manifest": str(MANIFEST_PATH),
            "unregistered_links": str(
                UNREGISTERED_PATH
            ),
        },
    }

    STATUS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with STATUS_PATH.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            status,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("Analiza załączników zakończona.")
    print(
        f"Rejestr załączników: {len(attachments)}"
    )
    print(
        "Bezpośrednio podlinkowane w rejestrze: "
        f"{classification_counts['linked']}"
    )
    print(
        "Tylko relacja rodzica: "
        f"{classification_counts['parent_only']}"
    )
    print(
        f"Osierocone: {classification_counts['orphan']}"
    )
    print(
        "Brak adresu URL: "
        f"{classification_counts['missing_url']}"
    )
    print(
        "Podlinkowane poza rejestrem: "
        f"{len(unregistered)}"
    )
    print(
        "Kandydaci wysokiej pewności: "
        f"{status['high_confidence_extraction_candidate_count']}"
    )


if __name__ == "__main__":
    main()
