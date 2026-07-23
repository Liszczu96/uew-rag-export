#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_QUEUE_PATH = Path(
    "public/rag/attachment-extraction-queue.jsonl"
)
REVIEW_PATH = Path(
    "public/rag/attachment-resolution-review.jsonl"
)

FINAL_QUEUE_PATH = Path(
    "public/rag/attachment-extraction-queue-final.jsonl"
)
FINAL_REVIEW_PATH = Path(
    "public/rag/attachment-resolution-final-review.jsonl"
)
AUDIT_PATH = Path(
    "public/rag/attachment-resolution-second-pass.jsonl"
)
STATUS_PATH = Path(
    "public/attachment-final-status.json"
)

CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 45
SAMPLE_BYTES = 8192

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".odt",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    "application/vnd.oasis.opendocument.text",
    "application/vnd.ms-powerpoint",
    (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation"
    ),
    "application/vnd.ms-excel",
    (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
}

GENERIC_MIME_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
}

ZIP_CONTAINER_EXTENSIONS = {
    ".docx",
    ".odt",
    ".pptx",
    ".xlsx",
}

OLE_CONTAINER_EXTENSIONS = {
    ".doc",
    ".ppt",
    ".xls",
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


def normalize_url(value: Any) -> str:
    return str(value or "").strip()


def file_extension(url: str) -> str:
    path = unquote(urlsplit(url).path)
    return Path(path).suffix.lower()


def normalized_mime(value: Any) -> str:
    return (
        str(value or "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )


def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=1.0,
        status_forcelist={
            429,
            500,
            502,
            503,
            504,
        },
        allowed_methods={"GET"},
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "application/pdf,"
                "application/octet-stream;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://uew.pl/",
        }
    )

    return session


def parse_content_length(
    headers: requests.structures.CaseInsensitiveDict,
) -> int | None:
    value = headers.get("Content-Length")

    if value and value.isdigit():
        return int(value)

    return None


def magic_matches(
    extension: str,
    sample: bytes,
) -> tuple[bool, str]:
    if extension == ".pdf":
        return (
            sample.startswith(b"%PDF-"),
            "pdf_signature",
        )

    if extension in ZIP_CONTAINER_EXTENSIONS:
        return (
            sample.startswith(b"PK\x03\x04")
            or sample.startswith(b"PK\x05\x06")
            or sample.startswith(b"PK\x07\x08"),
            "zip_container_signature",
        )

    if extension in OLE_CONTAINER_EXTENSIONS:
        return (
            sample.startswith(
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
            ),
            "ole_container_signature",
        )

    return False, "unsupported_extension"


def validate_full_get(
    url: str,
) -> dict[str, Any]:
    checked_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(),
    )

    result: dict[str, Any] = {
        "requested_url": url,
        "checked_at": checked_at,
        "final_url": None,
        "http_status": None,
        "reachable": False,
        "valid_document": False,
        "content_type": None,
        "content_length": None,
        "extension": file_extension(url),
        "redirect_count": 0,
        "sample_size": 0,
        "validation_reason": None,
        "error": None,
    }

    session = create_session()

    try:
        with session.get(
            url,
            stream=True,
            allow_redirects=True,
            timeout=(
                CONNECT_TIMEOUT_SECONDS,
                READ_TIMEOUT_SECONDS,
            ),
        ) as response:
            final_url = response.url
            status_code = response.status_code
            content_type = response.headers.get(
                "Content-Type",
                "",
            )
            content_length = parse_content_length(
                response.headers
            )
            extension = (
                file_extension(final_url)
                or file_extension(url)
            )

            result.update(
                {
                    "final_url": final_url,
                    "http_status": status_code,
                    "content_type": content_type,
                    "content_length": content_length,
                    "extension": extension,
                    "redirect_count": len(
                        response.history
                    ),
                }
            )

            if status_code != 200:
                result["validation_reason"] = (
                    "http_error"
                )
                result["error"] = (
                    f"Serwer zwrócił HTTP {status_code}."
                )
                return result

            result["reachable"] = True

            sample = b""

            for chunk in response.iter_content(
                chunk_size=SAMPLE_BYTES
            ):
                if not chunk:
                    continue

                sample = chunk[:SAMPLE_BYTES]
                break

            result["sample_size"] = len(sample)

            mime_type = normalized_mime(content_type)
            magic_ok, magic_reason = magic_matches(
                extension,
                sample,
            )

            if extension not in SUPPORTED_EXTENSIONS:
                result["validation_reason"] = (
                    "unsupported_extension"
                )
                result["error"] = (
                    "Nieobsługiwane rozszerzenie pliku."
                )
                return result

            if mime_type.startswith("text/html"):
                result["validation_reason"] = (
                    "html_instead_of_document"
                )
                result["error"] = (
                    "Serwer zwrócił stronę HTML "
                    "zamiast dokumentu."
                )
                return result

            if (
                mime_type in SUPPORTED_MIME_TYPES
                and magic_ok
            ):
                result["valid_document"] = True
                result["validation_reason"] = (
                    f"supported_mime_and_{magic_reason}"
                )
                return result

            if (
                mime_type in GENERIC_MIME_TYPES
                and magic_ok
            ):
                result["valid_document"] = True
                result["validation_reason"] = (
                    f"generic_mime_and_{magic_reason}"
                )
                return result

            result["validation_reason"] = (
                "mime_or_signature_mismatch"
            )
            result["error"] = (
                "Typ MIME albo sygnatura pliku "
                "nie odpowiada obsługiwanemu dokumentowi."
            )
            return result

    except requests.Timeout as error:
        result["validation_reason"] = "timeout"
        result["error"] = str(error)
        return result

    except requests.RequestException as error:
        result["validation_reason"] = (
            "network_error"
        )
        result["error"] = str(error)
        return result

    finally:
        session.close()


def merge_into_queue(
    queue_by_url: dict[str, dict[str, Any]],
    item: dict[str, Any],
) -> None:
    effective_url = normalize_url(
        item.get("effective_url")
        or item.get("final_url")
        or item.get("url")
    )

    if not effective_url:
        return

    aliases = {
        normalize_url(alias)
        for alias in item.get("url_aliases", [])
        if normalize_url(alias)
    }

    for field in (
        "url",
        "requested_url",
        "replacement_url",
        "final_url",
        "effective_url",
    ):
        value = normalize_url(item.get(field))

        if value:
            aliases.add(value)

    existing = queue_by_url.get(effective_url)

    if existing is None:
        item["effective_url"] = effective_url
        item["url_aliases"] = sorted(aliases)
        queue_by_url[effective_url] = item
        return

    existing["url_aliases"] = sorted(
        set(existing.get("url_aliases") or [])
        | aliases
    )

    reasons = list(
        existing.get("resolution_reasons") or []
    )

    for reason in item.get(
        "resolution_reasons",
        [],
    ):
        if reason not in reasons:
            reasons.append(reason)

    existing["resolution_reasons"] = reasons


def main() -> None:
    base_queue = load_jsonl(BASE_QUEUE_PATH)
    review_items = load_jsonl(REVIEW_PATH)

    queue_by_url: dict[str, dict[str, Any]] = {}

    for item in base_queue:
        merge_into_queue(queue_by_url, dict(item))

    audit_items: list[dict[str, Any]] = []
    final_review: list[dict[str, Any]] = []

    attempted_count = 0
    second_pass_resolved_count = 0
    excluded_terminal_count = 0
    manual_review_count = 0

    for item in review_items:
        decision = str(
            item.get("decision") or ""
        )

        if decision == "excluded_from_rag":
            excluded_terminal_count += 1
            audit_items.append(
                {
                    **item,
                    "second_pass_status": (
                        "resolved_excluded"
                    ),
                }
            )
            continue

        if decision == "manual_review":
            manual_review_count += 1
            final_review.append(item)
            audit_items.append(
                {
                    **item,
                    "second_pass_status": (
                        "manual_review_retained"
                    ),
                }
            )
            continue

        if decision not in {
            "browser_get_retry_required",
            "replacement_target_needs_validation",
        }:
            final_review.append(item)
            audit_items.append(
                {
                    **item,
                    "second_pass_status": (
                        "unsupported_review_decision"
                    ),
                }
            )
            continue

        attempted_count += 1

        target_url = normalize_url(
            item.get("replacement_url")
            if decision
            == "replacement_target_needs_validation"
            else item.get("url")
        )

        validation = validate_full_get(target_url)

        audit_record = {
            **item,
            "second_pass_target_url": target_url,
            "second_pass_validation": validation,
        }

        if validation.get("valid_document"):
            effective_url = normalize_url(
                validation.get("final_url")
                or target_url
            )
            extension = str(
                validation.get("extension")
                or file_extension(effective_url)
            ).lower()

            resolution = (
                "replacement_target_validated_second_pass"
                if decision
                == "replacement_target_needs_validation"
                else "browser_get_retry_validated"
            )

            queue_item = {
                **item,
                **validation,
                "effective_url": effective_url,
                "resolution": resolution,
                "resolution_reasons": [
                    str(
                        item.get("reason")
                        or "Rozwiązano w drugiej walidacji."
                    )
                ],
                "parser": (
                    "odt"
                    if extension == ".odt"
                    else None
                ),
                "url_aliases": [
                    value
                    for value in {
                        normalize_url(item.get("url")),
                        normalize_url(
                            item.get("replacement_url")
                        ),
                        target_url,
                        effective_url,
                    }
                    if value
                ],
            }

            merge_into_queue(
                queue_by_url,
                queue_item,
            )

            second_pass_resolved_count += 1
            audit_record["second_pass_status"] = (
                "resolved_added_to_queue"
            )

        else:
            unresolved = {
                **item,
                "decision": (
                    "second_pass_validation_failed"
                ),
                "previous_decision": decision,
                "second_pass_target_url": target_url,
                "second_pass_validation": validation,
                "recommended_action": (
                    item.get("recommended_action")
                    or (
                        "Sprawdzić lub poprawić link "
                        "w źródłowej witrynie."
                    )
                ),
            }

            final_review.append(unresolved)
            audit_record["second_pass_status"] = (
                "unresolved_after_second_pass"
            )

        audit_items.append(audit_record)

    final_queue = sorted(
        queue_by_url.values(),
        key=lambda item: str(
            item.get("effective_url") or ""
        ),
    )

    for index, item in enumerate(
        final_queue,
        start=1,
    ):
        item["queue_id"] = (
            f"attachment-{index:05d}"
        )

    final_review.sort(
        key=lambda item: (
            str(item.get("decision") or ""),
            str(item.get("url") or ""),
        )
    )

    audit_items.sort(
        key=lambda item: (
            str(
                item.get(
                    "second_pass_status"
                )
                or ""
            ),
            str(item.get("url") or ""),
        )
    )

    write_jsonl(FINAL_QUEUE_PATH, final_queue)
    write_jsonl(FINAL_REVIEW_PATH, final_review)
    write_jsonl(AUDIT_PATH, audit_items)

    final_extension_counts = Counter(
        str(
            item.get("extension")
            or file_extension(
                str(
                    item.get(
                        "effective_url"
                    )
                    or ""
                )
            )
        )
        for item in final_queue
    )

    remaining_decision_counts = Counter(
        str(item.get("decision") or "unknown")
        for item in final_review
    )

    audit_status_counts = Counter(
        str(
            item.get("second_pass_status")
            or "unknown"
        )
        for item in audit_items
    )

    status = {
        "base_queue_count": len(base_queue),
        "input_review_count": len(review_items),
        "second_pass_attempted_count": (
            attempted_count
        ),
        "second_pass_resolved_count": (
            second_pass_resolved_count
        ),
        "excluded_terminal_count": (
            excluded_terminal_count
        ),
        "manual_review_retained_count": (
            manual_review_count
        ),
        "final_extraction_queue_count": len(
            final_queue
        ),
        "final_review_count": len(
            final_review
        ),
        "queue_growth_count": (
            len(final_queue) - len(base_queue)
        ),
        "final_extension_counts": dict(
            sorted(final_extension_counts.items())
        ),
        "remaining_review_decision_counts": dict(
            sorted(
                remaining_decision_counts.items()
            )
        ),
        "audit_status_counts": dict(
            sorted(audit_status_counts.items())
        ),
        "files": {
            "final_extraction_queue": str(
                FINAL_QUEUE_PATH
            ),
            "final_review": str(
                FINAL_REVIEW_PATH
            ),
            "second_pass_audit": str(
                AUDIT_PATH
            ),
        },
    }

    STATUS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    STATUS_PATH.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Druga walidacja zakończona.")
    print(
        "Próby walidacji: "
        f"{attempted_count}"
    )
    print(
        "Rozwiązane w drugiej próbie: "
        f"{second_pass_resolved_count}"
    )
    print(
        "Końcowa kolejka ekstrakcji: "
        f"{len(final_queue)}"
    )
    print(
        "Pozostałe pozycje do ręcznej decyzji: "
        f"{len(final_review)}"
    )


if __name__ == "__main__":
    main()
