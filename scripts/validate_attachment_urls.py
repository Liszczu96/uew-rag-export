#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


MANIFEST_PATH = Path("public/rag/attachment-manifest.jsonl")
UNREGISTERED_PATH = Path(
    "public/rag/unregistered-attachment-links.jsonl"
)

RESULTS_PATH = Path(
    "public/rag/attachment-url-validation.jsonl"
)
STATUS_PATH = Path("public/attachment-url-status.json")

MAX_WORKERS = 4
CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
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

FALLBACK_MIME_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
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


def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=0.8,
        status_forcelist={
            429,
            500,
            502,
            503,
            504,
        },
        allowed_methods={"GET", "HEAD"},
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "UEW-RAG-Attachment-Validator/1.0"
            ),
            "Accept": "*/*",
        }
    )

    return session


def file_extension(url: str) -> str:
    path = unquote(urlsplit(url).path).lower()

    for extension in sorted(
        SUPPORTED_EXTENSIONS,
        key=len,
        reverse=True,
    ):
        if path.endswith(extension):
            return extension

    return Path(path).suffix.lower()


def parse_total_size(
    headers: requests.structures.CaseInsensitiveDict,
    status_code: int,
) -> int | None:
    content_range = headers.get("Content-Range", "")

    match = re.search(r"/(\d+)$", content_range)

    if match:
        return int(match.group(1))

    content_length = headers.get("Content-Length")

    if (
        content_length
        and content_length.isdigit()
        and status_code == 200
    ):
        return int(content_length)

    return None


def classify_content(
    content_type: str,
    extension: str,
) -> tuple[bool, str]:
    normalized_mime = (
        content_type.split(";", 1)[0]
        .strip()
        .lower()
    )

    if normalized_mime in SUPPORTED_MIME_TYPES:
        return True, "supported_mime"

    if (
        normalized_mime in FALLBACK_MIME_TYPES
        and extension in SUPPORTED_EXTENSIONS
    ):
        return True, "supported_extension_fallback"

    if (
        not normalized_mime
        and extension in SUPPORTED_EXTENSIONS
    ):
        return True, "missing_mime_supported_extension"

    return False, "unsupported_content_type"


def validate_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    url = str(candidate.get("url") or "").strip()

    result: dict[str, Any] = {
        **candidate,
        "checked_at": (
            time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            )
        ),
        "requested_url": url,
        "final_url": None,
        "http_status": None,
        "reachable": False,
        "valid_document": False,
        "result": None,
        "content_type": None,
        "content_length": None,
        "extension": file_extension(url) if url else "",
        "redirect_count": 0,
        "error": None,
    }

    if not url:
        result["result"] = "missing_url"
        result["error"] = "Brak adresu URL."
        return result

    session = create_session()

    try:
        with session.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            allow_redirects=True,
            timeout=(
                CONNECT_TIMEOUT_SECONDS,
                READ_TIMEOUT_SECONDS,
            ),
        ) as response:
            status_code = response.status_code
            final_url = response.url
            content_type = response.headers.get(
                "Content-Type",
                "",
            )
            extension = (
                file_extension(final_url)
                or result["extension"]
            )
            content_length = parse_total_size(
                response.headers,
                status_code,
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

            if status_code not in {200, 206}:
                result["result"] = "http_error"
                result["error"] = (
                    f"Serwer zwrócił HTTP {status_code}."
                )
                return result

            result["reachable"] = True

            valid_document, reason = classify_content(
                content_type,
                extension,
            )

            result["valid_document"] = valid_document
            result["result"] = (
                "ok" if valid_document
                else "invalid_content_type"
            )

            if not valid_document:
                result["error"] = (
                    "Odpowiedź nie wygląda jak obsługiwany "
                    "dokument."
                )

            result["validation_reason"] = reason
            return result

    except requests.Timeout as error:
        result["result"] = "timeout"
        result["error"] = str(error)
        return result

    except requests.RequestException as error:
        result["result"] = "network_error"
        result["error"] = str(error)
        return result

    finally:
        session.close()


def build_candidates() -> list[dict[str, Any]]:
    manifest = load_jsonl(MANIFEST_PATH)
    unregistered = load_jsonl(UNREGISTERED_PATH)

    candidates_by_url: dict[str, dict[str, Any]] = {}

    for item in manifest:
        if not item.get("extraction_candidate"):
            continue

        url = str(
            item.get("normalized_url")
            or item.get("url")
            or ""
        ).strip()

        if not url:
            continue

        candidates_by_url[url] = {
            "candidate_id": item.get("id"),
            "candidate_type": "registered_linked",
            "source_id": item.get("source_id"),
            "source_name": item.get("source_name"),
            "source_priority": item.get(
                "source_priority"
            ),
            "url": url,
            "filename": item.get("filename"),
            "declared_mime_type": item.get(
                "mime_type"
            ),
            "declared_filesize": item.get(
                "filesize"
            ),
            "linked_from_count": item.get(
                "linked_from_count",
                0,
            ),
            "duplicate_group_id": item.get(
                "duplicate_group_id"
            ),
            "duplicate_group_size": item.get(
                "duplicate_group_size",
                1,
            ),
        }

    for item in unregistered:
        if not item.get("extraction_candidate"):
            continue

        url = str(item.get("url") or "").strip()

        if not url:
            continue

        host = str(item.get("host") or "")

        candidates_by_url.setdefault(
            url,
            {
                "candidate_id": f"unregistered:{url}",
                "candidate_type": (
                    "institutional_unregistered"
                ),
                "source_id": host,
                "source_name": host,
                "source_priority": "A",
                "url": url,
                "filename": (
                    Path(
                        unquote(
                            urlsplit(url).path
                        )
                    ).name
                    or None
                ),
                "declared_mime_type": None,
                "declared_filesize": None,
                "linked_from_count": item.get(
                    "linked_from_count",
                    0,
                ),
                "duplicate_group_id": None,
                "duplicate_group_size": 1,
            },
        )

    return sorted(
        candidates_by_url.values(),
        key=lambda item: str(item.get("url") or ""),
    )


def main() -> None:
    candidates = build_candidates()

    print(
        "Kandydaci do walidacji: "
        f"{len(candidates)}"
    )

    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                validate_candidate,
                candidate,
            ): candidate
            for candidate in candidates
        }

        for number, future in enumerate(
            as_completed(futures),
            start=1,
        ):
            candidate = futures[future]

            try:
                result = future.result()
            except Exception as error:
                result = {
                    **candidate,
                    "result": "unexpected_error",
                    "reachable": False,
                    "valid_document": False,
                    "error": str(error),
                }

            results.append(result)

            if (
                number % 50 == 0
                or number == len(candidates)
            ):
                print(
                    f"Sprawdzono {number}/"
                    f"{len(candidates)}"
                )

    results.sort(
        key=lambda item: str(item.get("url") or "")
    )

    write_jsonl(RESULTS_PATH, results)

    result_counts = Counter(
        str(item.get("result") or "unknown")
        for item in results
    )

    http_status_counts = Counter(
        str(item["http_status"])
        for item in results
        if item.get("http_status") is not None
    )

    source_counts: dict[str, Counter[str]] = (
        defaultdict(Counter)
    )

    for item in results:
        source_id = str(
            item.get("source_id") or "unknown"
        )
        source_counts[source_id][
            str(item.get("result") or "unknown")
        ] += 1

    status = {
        "candidate_count": len(candidates),
        "reachable_count": sum(
            1 for item in results
            if item.get("reachable")
        ),
        "valid_document_count": sum(
            1 for item in results
            if item.get("valid_document")
        ),
        "invalid_or_unreachable_count": sum(
            1 for item in results
            if not item.get("valid_document")
        ),
        "result_counts": dict(
            sorted(result_counts.items())
        ),
        "http_status_counts": dict(
            sorted(http_status_counts.items())
        ),
        "candidate_types": dict(
            sorted(
                Counter(
                    str(
                        item.get("candidate_type")
                        or "unknown"
                    )
                    for item in results
                ).items()
            )
        ),
        "sources": {
            source_id: dict(
                sorted(counter.items())
            )
            for source_id, counter
            in sorted(source_counts.items())
        },
        "output_file": str(RESULTS_PATH),
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

    print("Walidacja adresów zakończona.")
    print(
        "Poprawne dokumenty: "
        f"{status['valid_document_count']}"
    )
    print(
        "Niepoprawne lub niedostępne: "
        f"{status['invalid_or_unreachable_count']}"
    )


if __name__ == "__main__":
    main()
