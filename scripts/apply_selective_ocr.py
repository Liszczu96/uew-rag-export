#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SELECTION_PATH = Path("config/ocr-selection.yml")
OCR_QUEUE_PATH = Path("public/rag/attachment-ocr-queue.jsonl")
INDEX_PATH = Path("public/rag/attachment-text-index.jsonl")
STATE_PATH = Path("public/rag/attachment-extraction-state.jsonl")

RESULTS_PATH = Path("public/rag/attachment-ocr-results.jsonl")
ERRORS_PATH = Path("public/rag/attachment-ocr-errors.jsonl")
CHANGES_PATH = Path("public/changes/attachment-ocr-changes.jsonl")
STATUS_PATH = Path("public/attachment-ocr-status.json")

DEFAULT_DPI = 250
DEFAULT_TIMEOUT_SECONDS = 180
OCR_LANGUAGES = "pol+eng"

DECISION_PRIORITY = {
    "exclude": 1,
    "manual_review": 2,
    "ocr": 3,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def load_selection(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku: {path}")

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = payload.get("rules", [])

    if not isinstance(rules, list):
        raise ValueError("Pole rules w konfiguracji OCR nie jest listą.")

    normalized: list[dict[str, Any]] = []

    for rule in rules:
        if not isinstance(rule, dict):
            continue

        decision = str(rule.get("decision") or "").strip()

        if decision not in DECISION_PRIORITY:
            raise ValueError(
                "Nieobsługiwana decyzja OCR: "
                f"{decision!r}"
            )

        normalized.append(rule)

    return normalized


def normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\x00", "")
    text = re.sub(
        r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]",
        " ",
        text,
    )
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    cleaned_lines: list[str] = []
    previous_blank = False

    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()

        if not line:
            if not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue

        cleaned_lines.append(line)
        previous_blank = False

    return "\n".join(cleaned_lines).strip()


def word_count(text: str) -> int:
    return len(
        re.findall(
            r"\b[\wÀ-ž'-]+\b",
            text,
            flags=re.UNICODE,
        )
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def natural_sort_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", path.name)
    return [
        int(part) if part.isdigit() else part.lower()
        for part in parts
    ]


def read_blob(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(
            f"Blob {path} nie zawiera obiektu JSON."
        )

    return payload


def write_blob(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    compressed = gzip.compress(
        serialized,
        compresslevel=9,
        mtime=0,
    )

    path.write_bytes(compressed)


def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=3,
        status=4,
        backoff_factor=1.0,
        status_forcelist={429, 500, 502, 503, 504},
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
            "User-Agent": "UEW-RAG-Selective-OCR/1.0",
            "Accept": "application/pdf,*/*;q=0.8",
        }
    )

    return session


def download_pdf(
    session: requests.Session,
    url: str,
    destination: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    with session.get(
        url,
        stream=True,
        allow_redirects=True,
        timeout=(20, timeout_seconds),
    ) as response:
        response.raise_for_status()

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        downloaded_bytes = 0

        with destination.open("wb") as file:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if not chunk:
                    continue

                file.write(chunk)
                downloaded_bytes += len(chunk)

        return {
            "http_status": response.status_code,
            "final_url": response.url or url,
            "content_type": response.headers.get(
                "Content-Type"
            ),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get(
                "Last-Modified"
            ),
            "downloaded_bytes": downloaded_bytes,
            "redirect_count": len(response.history),
        }


def require_command(command: str) -> None:
    result = subprocess.run(
        ["bash", "-lc", f"command -v {command}"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Brak wymaganego programu: {command}"
        )


def render_pdf(
    pdf_path: Path,
    output_directory: Path,
    dpi: int,
    timeout_seconds: int,
) -> list[Path]:
    output_prefix = output_directory / "page"

    process = subprocess.run(
        [
            "pdftoppm",
            "-png",
            "-r",
            str(dpi),
            str(pdf_path),
            str(output_prefix),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )

    if process.returncode != 0:
        raise RuntimeError(
            "pdftoppm zakończył się błędem: "
            f"{process.stderr.strip()}"
        )

    images = sorted(
        output_directory.glob("page-*.png"),
        key=natural_sort_key,
    )

    if not images:
        raise RuntimeError(
            "Nie wygenerowano obrazów stron PDF."
        )

    return images


def ocr_image(
    image_path: Path,
    timeout_seconds: int,
) -> str:
    process = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "-l",
            OCR_LANGUAGES,
            "--psm",
            "3",
            "--dpi",
            str(DEFAULT_DPI),
        ],
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )

    text = process.stdout.decode(
        "utf-8",
        errors="replace",
    )

    if process.returncode != 0 and not text.strip():
        stderr = process.stderr.decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            "Tesseract zakończył się błędem: "
            f"{stderr.strip()}"
        )

    return normalize_text(text)


def perform_ocr(
    pdf_path: Path,
    dpi: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        page_directory = Path(directory)
        images = render_pdf(
            pdf_path=pdf_path,
            output_directory=page_directory,
            dpi=dpi,
            timeout_seconds=timeout_seconds,
        )

        sections: list[dict[str, Any]] = []
        warnings: list[str] = []

        for page_number, image_path in enumerate(
            images,
            start=1,
        ):
            page_text = ocr_image(
                image_path=image_path,
                timeout_seconds=timeout_seconds,
            )

            if not page_text:
                warnings.append(
                    f"OCR nie zwrócił tekstu dla strony "
                    f"{page_number}."
                )
                continue

            sections.append(
                {
                    "section_id": f"page-{page_number}",
                    "kind": "page",
                    "label": f"Strona {page_number}",
                    "text": page_text,
                    "char_count": len(page_text),
                    "word_count": word_count(page_text),
                    "metadata": {
                        "page_number": page_number,
                        "extraction_method": "ocr",
                    },
                }
            )

        text = normalize_text(
            "\n\n".join(
                section["text"]
                for section in sections
            )
        )

        status = (
            "ok_ocr"
            if len(text) >= 20
            else "ocr_failed"
        )

        if status == "ocr_failed":
            warnings.append(
                "OCR nie zwrócił wystarczającej ilości tekstu."
            )

        return {
            "text": text,
            "sections": sections,
            "parser": "tesseract-ocr",
            "extraction_status": status,
            "warnings": warnings,
            "metrics": {
                "page_count": len(images),
                "ocr_text_page_count": len(sections),
                "ocr_empty_page_count": (
                    len(images) - len(sections)
                ),
                "ocr_languages": OCR_LANGUAGES,
                "ocr_dpi": dpi,
                "ocr_psm": 3,
            },
        }


def group_rules_by_content_hash(
    rules: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for rule in rules:
        content_hash = str(
            rule.get("content_sha256") or ""
        ).strip()

        if not content_hash:
            raise ValueError(
                "Reguła OCR nie zawiera content_sha256: "
                f"{rule.get('filename')}"
            )

        grouped[content_hash].append(rule)

    resolved: dict[str, dict[str, Any]] = {}

    for content_hash, group in grouped.items():
        chosen = max(
            group,
            key=lambda rule: DECISION_PRIORITY[
                str(rule.get("decision"))
            ],
        )

        resolved[content_hash] = {
            "content_sha256": content_hash,
            "decision": chosen["decision"],
            "priority": chosen.get("priority"),
            "reasons": sorted(
                {
                    str(rule.get("reason") or "")
                    for rule in group
                    if str(rule.get("reason") or "")
                }
            ),
            "rules": group,
            "effective_url": chosen.get(
                "effective_url"
            ),
            "filename": chosen.get("filename"),
            "source_id": chosen.get("source_id"),
        }

    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stosuje selektywne decyzje OCR do "
            "dokumentów UEW."
        )
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    arguments = parser.parse_args()

    require_command("pdftoppm")
    require_command("tesseract")

    rules = load_selection(SELECTION_PATH)
    ocr_queue = load_jsonl(OCR_QUEUE_PATH)
    index = load_jsonl(INDEX_PATH)
    state = load_jsonl(STATE_PATH)

    if len(rules) != len(ocr_queue):
        raise ValueError(
            "Liczba reguł OCR nie odpowiada kolejce: "
            f"{len(rules)} != {len(ocr_queue)}"
        )

    decisions_by_hash = group_rules_by_content_hash(rules)

    index_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    state_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in index:
        content_hash = str(
            record.get("content_sha256") or ""
        ).strip()

        if content_hash:
            index_by_hash[content_hash].append(record)

    for record in state:
        content_hash = str(
            record.get("content_sha256") or ""
        ).strip()

        if content_hash:
            state_by_hash[content_hash].append(record)

    session = create_session()

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    decision_counts: Counter[str] = Counter()
    result_counts: Counter[str] = Counter()
    processed_page_count = 0
    downloaded_bytes = 0

    with tempfile.TemporaryDirectory() as directory:
        temp_directory = Path(directory)

        for position, (
            content_hash,
            decision_group,
        ) in enumerate(
            sorted(decisions_by_hash.items()),
            start=1,
        ):
            decision = str(decision_group["decision"])
            decision_counts[decision] += 1

            matching_index = index_by_hash.get(
                content_hash,
                [],
            )
            matching_state = state_by_hash.get(
                content_hash,
                [],
            )

            if not matching_index:
                errors.append(
                    {
                        "content_sha256": content_hash,
                        "decision": decision,
                        "error": (
                            "Brak pasującego rekordu indeksu."
                        ),
                        "failed_at": utc_now(),
                    }
                )
                result_counts["failed"] += 1
                continue

            blob_path = Path(
                str(matching_index[0].get("blob_path") or "")
            )

            if not blob_path.exists():
                errors.append(
                    {
                        "content_sha256": content_hash,
                        "decision": decision,
                        "error": (
                            f"Brak blobu: {blob_path}"
                        ),
                        "failed_at": utc_now(),
                    }
                )
                result_counts["failed"] += 1
                continue

            reason_text = " | ".join(
                decision_group.get("reasons", [])
            )

            print(
                f"[{position}/{len(decisions_by_hash)}] "
                f"{decision}: "
                f"{decision_group.get('filename')}"
            )

            if decision in {
                "exclude",
                "manual_review",
            }:
                new_status = (
                    "excluded_from_rag"
                    if decision == "exclude"
                    else "manual_review"
                )

                payload = read_blob(blob_path)
                warnings = list(
                    payload.get("warnings") or []
                )

                if reason_text and reason_text not in warnings:
                    warnings.append(reason_text)

                payload.update(
                    {
                        "extraction_status": new_status,
                        "warnings": warnings,
                        "selection_decision": decision,
                        "selection_reason": reason_text,
                        "selection_applied_at": utc_now(),
                    }
                )
                write_blob(blob_path, payload)

                for record in matching_index:
                    record["extraction_status"] = new_status
                    record["warnings"] = warnings
                    record["selection_decision"] = decision
                    record["selection_reason"] = reason_text
                    record["checked_at"] = utc_now()

                for record in matching_state:
                    record["extraction_status"] = new_status
                    record["selection_decision"] = decision
                    record["selection_reason"] = reason_text
                    record["checked_at"] = utc_now()

                results.append(
                    {
                        "content_sha256": content_hash,
                        "decision": decision,
                        "status": new_status,
                        "record_count": len(matching_index),
                        "blob_path": str(blob_path),
                        "reason": reason_text,
                    }
                )
                changes.append(
                    {
                        "content_sha256": content_hash,
                        "change_type": new_status,
                        "record_count": len(matching_index),
                        "changed_at": utc_now(),
                    }
                )
                result_counts[new_status] += 1
                continue

            url = str(
                decision_group.get("effective_url") or ""
            ).strip()

            if not url:
                errors.append(
                    {
                        "content_sha256": content_hash,
                        "decision": decision,
                        "error": "Brak URL dokumentu OCR.",
                        "failed_at": utc_now(),
                    }
                )
                result_counts["failed"] += 1
                continue

            pdf_path = temp_directory / (
                content_hash + ".pdf"
            )

            try:
                download = download_pdf(
                    session=session,
                    url=url,
                    destination=pdf_path,
                    timeout_seconds=(
                        arguments.timeout_seconds
                    ),
                )
                downloaded_bytes += int(
                    download.get("downloaded_bytes") or 0
                )

                ocr_result = perform_ocr(
                    pdf_path=pdf_path,
                    dpi=arguments.dpi,
                    timeout_seconds=(
                        arguments.timeout_seconds
                    ),
                )

                processed_page_count += int(
                    ocr_result["metrics"]["page_count"]
                )

                payload = read_blob(blob_path)
                original_parser = payload.get("parser")
                original_status = payload.get(
                    "extraction_status"
                )

                ocr_text = str(ocr_result["text"])
                text_hash = sha256_text(ocr_text)
                extracted_at = utc_now()

                payload.update(
                    {
                        "text": ocr_text,
                        "sections": ocr_result["sections"],
                        "text_sha256": text_hash,
                        "parser": ocr_result["parser"],
                        "extraction_status": (
                            ocr_result[
                                "extraction_status"
                            ]
                        ),
                        "warnings": (
                            ocr_result["warnings"]
                        ),
                        "metrics": (
                            ocr_result["metrics"]
                        ),
                        "char_count": len(ocr_text),
                        "word_count": word_count(ocr_text),
                        "section_count": len(
                            ocr_result["sections"]
                        ),
                        "extracted_at": extracted_at,
                        "selection_decision": "ocr",
                        "selection_reason": reason_text,
                        "ocr": {
                            "performed": True,
                            "performed_at": extracted_at,
                            "languages": OCR_LANGUAGES,
                            "dpi": arguments.dpi,
                            "original_parser": original_parser,
                            "original_status": original_status,
                        },
                    }
                )
                write_blob(blob_path, payload)

                for record in matching_index:
                    record.update(
                        {
                            "text_sha256": text_hash,
                            "parser": (
                                ocr_result["parser"]
                            ),
                            "extraction_status": (
                                ocr_result[
                                    "extraction_status"
                                ]
                            ),
                            "char_count": len(ocr_text),
                            "word_count": (
                                word_count(ocr_text)
                            ),
                            "section_count": len(
                                ocr_result["sections"]
                            ),
                            "metrics": (
                                ocr_result["metrics"]
                            ),
                            "warnings": (
                                ocr_result["warnings"]
                            ),
                            "selection_decision": "ocr",
                            "selection_reason": (
                                reason_text
                            ),
                            "extracted_at": (
                                extracted_at
                            ),
                            "checked_at": utc_now(),
                        }
                    )

                for record in matching_state:
                    record.update(
                        {
                            "text_sha256": text_hash,
                            "parser": (
                                ocr_result["parser"]
                            ),
                            "extraction_status": (
                                ocr_result[
                                    "extraction_status"
                                ]
                            ),
                            "selection_decision": "ocr",
                            "selection_reason": (
                                reason_text
                            ),
                            "extracted_at": (
                                extracted_at
                            ),
                            "checked_at": utc_now(),
                            "http_status": (
                                download.get(
                                    "http_status"
                                )
                            ),
                            "etag": download.get("etag"),
                            "last_modified": (
                                download.get(
                                    "last_modified"
                                )
                            ),
                        }
                    )

                result_status = str(
                    ocr_result["extraction_status"]
                )

                results.append(
                    {
                        "content_sha256": content_hash,
                        "decision": "ocr",
                        "status": result_status,
                        "record_count": len(matching_index),
                        "effective_url": url,
                        "filename": decision_group.get(
                            "filename"
                        ),
                        "blob_path": str(blob_path),
                        "text_sha256": text_hash,
                        "char_count": len(ocr_text),
                        "word_count": word_count(ocr_text),
                        "page_count": (
                            ocr_result["metrics"][
                                "page_count"
                            ]
                        ),
                        "reason": reason_text,
                    }
                )
                changes.append(
                    {
                        "content_sha256": content_hash,
                        "change_type": result_status,
                        "record_count": len(matching_index),
                        "text_sha256": text_hash,
                        "changed_at": utc_now(),
                    }
                )
                result_counts[result_status] += 1

            except Exception as error:
                errors.append(
                    {
                        "content_sha256": content_hash,
                        "decision": "ocr",
                        "effective_url": url,
                        "filename": decision_group.get(
                            "filename"
                        ),
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_at": utc_now(),
                    }
                )
                result_counts["failed"] += 1

            finally:
                pdf_path.unlink(missing_ok=True)

    session.close()

    index.sort(
        key=lambda record: str(
            record.get("effective_url") or ""
        )
    )
    state.sort(
        key=lambda record: str(
            record.get("effective_url") or ""
        )
    )
    results.sort(
        key=lambda record: (
            str(record.get("decision") or ""),
            str(record.get("filename") or ""),
            str(record.get("content_sha256") or ""),
        )
    )
    errors.sort(
        key=lambda record: str(
            record.get("content_sha256") or ""
        )
    )

    write_jsonl(INDEX_PATH, index)
    write_jsonl(STATE_PATH, state)
    write_jsonl(RESULTS_PATH, results)
    write_jsonl(ERRORS_PATH, errors)
    write_jsonl(CHANGES_PATH, changes)

    status_counts = Counter(
        str(record.get("extraction_status") or "unknown")
        for record in index
    )

    status = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "input_rule_count": len(rules),
        "unique_content_hash_count": len(
            decisions_by_hash
        ),
        "decision_counts_by_unique_content": dict(
            sorted(decision_counts.items())
        ),
        "result_counts": dict(
            sorted(result_counts.items())
        ),
        "error_count": len(errors),
        "processed_ocr_page_count": processed_page_count,
        "downloaded_bytes": downloaded_bytes,
        "downloaded_megabytes": round(
            downloaded_bytes / (1024 * 1024),
            2,
        ),
        "final_index_status_counts": dict(
            sorted(status_counts.items())
        ),
        "files": {
            "results": str(RESULTS_PATH),
            "errors": str(ERRORS_PATH),
            "changes": str(CHANGES_PATH),
            "index": str(INDEX_PATH),
            "state": str(STATE_PATH),
        },
    }

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Selektywny OCR zakończony.")
    print(f"Reguły: {len(rules)}")
    print(
        "Unikalne pliki: "
        f"{len(decisions_by_hash)}"
    )
    print(
        "Strony OCR: "
        f"{processed_page_count}"
    )
    print(f"Błędy: {len(errors)}")
    print(
        "Statusy indeksu: "
        f"{dict(sorted(status_counts.items()))}"
    )

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
