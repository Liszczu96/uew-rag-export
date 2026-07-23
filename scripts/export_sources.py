import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup


CONFIG_PATH = Path("config/sources.yml")
DEFAULT_OUTPUT_DIRECTORY = Path("public")

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

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".csv",
    ".txt",
    ".zip",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku {CONFIG_PATH}.")

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict) or not isinstance(config.get("sources"), list):
        raise ValueError("Nieprawidłowy format config/sources.yml.")

    return config


def rendered_value(value: Any) -> str:
    if isinstance(value, dict):
        rendered = value.get("rendered")
        return rendered if isinstance(rendered, str) else ""

    return value if isinstance(value, str) else ""


def normalize_whitespace(text: str) -> str:
    lines: list[str] = []
    previous_blank = False

    for raw_line in text.replace("\xa0", " ").splitlines():
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


def html_to_text(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for element in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "canvas",
            "form",
            "iframe",
        ]
    ):
        element.decompose()

    for element in soup.find_all(["br", "hr"]):
        element.replace_with("\n")

    for cell in soup.find_all(["th", "td"]):
        cell.append(" | ")

    return normalize_whitespace(soup.get_text(separator="\n"))


def extract_headings(html: str) -> list[str]:
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    headings: list[str] = []

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = normalize_whitespace(heading.get_text(" ", strip=True))
        if text and text not in headings:
            headings.append(text)

    return headings


def collect_strings(value: Any) -> list[str]:
    collected: list[str] = []

    if isinstance(value, str):
        if value.strip():
            collected.append(value)
    elif isinstance(value, dict):
        for nested_value in value.values():
            collected.extend(collect_strings(nested_value))
    elif isinstance(value, list):
        for nested_value in value:
            collected.extend(collect_strings(nested_value))

    return collected


def acf_to_text(acf: Any) -> str:
    if not isinstance(acf, (dict, list)):
        return ""

    lines: list[str] = []

    for value in collect_strings(acf):
        candidate = html_to_text(value)

        if not candidate:
            continue

        if candidate.lower().startswith(("http://", "https://")):
            continue

        if candidate not in lines:
            lines.append(candidate)

    return normalize_whitespace("\n".join(lines))


def acf_html_fragments(acf: Any) -> str:
    fragments: list[str] = []

    for value in collect_strings(acf):
        lowered = value.lower()

        if (
            "<a " in lowered
            or "<h1" in lowered
            or "<h2" in lowered
            or "<h3" in lowered
        ):
            fragments.append(value)

    return "\n".join(fragments)


def normalize_url(url: str) -> str:
    if not url:
        return ""

    parts = urlsplit(url)

    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS
    ]

    path = re.sub(r"/{2,}", "/", parts.path or "/")

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(filtered_query, doseq=True),
            "",
        )
    )


def is_document_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.endswith(extension) for extension in DOCUMENT_EXTENSIONS)


def extract_links(
    html: str,
    page_url: str,
    source_host: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "internal_links": [],
        "institutional_links": [],
        "external_links": [],
        "attachments": [],
    }

    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href", "")).strip()

        if not raw_href or raw_href.startswith(
            ("#", "mailto:", "tel:", "javascript:")
        ):
            continue

        absolute_url = normalize_url(urljoin(page_url, raw_href))

        if not absolute_url.startswith(("http://", "https://")):
            continue

        anchor_text = normalize_whitespace(anchor.get_text(" ", strip=True))
        host = urlsplit(absolute_url).netloc.lower()
        key = (absolute_url, anchor_text)

        if key in seen:
            continue

        seen.add(key)

        link_record = {
            "url": absolute_url,
            "anchor_text": anchor_text or None,
            "host": host,
        }

        if is_document_url(absolute_url):
            result["attachments"].append(link_record)
        elif host == source_host:
            result["internal_links"].append(link_record)
        elif host == "uew.pl" or host.endswith(".uew.pl"):
            result["institutional_links"].append(link_record)
        else:
            result["external_links"].append(link_record)

    return result


def taxonomy_fields(item: dict[str, Any]) -> dict[str, Any]:
    taxonomies: dict[str, Any] = {}

    for key, value in item.items():
        if key in {"categories", "tags"} or key.endswith(
            ("_category", "_categories")
        ):
            if isinstance(value, (list, int, str)):
                taxonomies[key] = value

    return taxonomies


def merge_text_parts(parts: Iterable[str]) -> str:
    merged: list[str] = []

    for part in parts:
        normalized = normalize_whitespace(part)

        if not normalized:
            continue

        for block in normalized.split("\n\n"):
            block = block.strip()

            if block and block not in merged:
                merged.append(block)

    return "\n\n".join(merged)


def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout: int,
    attempts: int = 3,
) -> tuple[Any, requests.Response]:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json(), response
        except (requests.RequestException, ValueError) as error:
            last_error = error

            if attempt < attempts:
                time.sleep(attempt * 2)

    raise RuntimeError(f"Nie udało się pobrać {url}: {last_error}")


def fetch_collection(
    session: requests.Session,
    endpoint: str,
    timeout: int,
    request_delay: float,
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        params: dict[str, Any] = {
            "per_page": 100,
            "page": page,
            "orderby": "id",
            "order": "asc",
        }

        if extra_params:
            params.update(extra_params)

        data, response = request_json(
            session=session,
            url=endpoint,
            params=params,
            timeout=timeout,
        )

        if not isinstance(data, list):
            raise RuntimeError(
                f"Endpoint {endpoint} nie zwrócił listy rekordów."
            )

        records.extend(item for item in data if isinstance(item, dict))

        if total_pages is None:
            header = response.headers.get("X-WP-TotalPages")
            total_pages = int(header) if header and header.isdigit() else None

        progress = f"    strona API {page}"

        if total_pages is not None:
            progress += f"/{total_pages}"

        print(f"{progress}, pobrano {len(data)}")

        if not data or (total_pages is None and len(data) < 100):
            break

        page += 1
        time.sleep(request_delay)

    return records


def build_document(
    item: dict[str, Any],
    source: dict[str, Any],
    content_type: str,
    fetched_at: str,
    include_raw_html: bool,
    include_acf: bool,
) -> dict[str, Any]:
    title_html = rendered_value(item.get("title"))
    content_html = rendered_value(item.get("content"))
    excerpt_html = rendered_value(item.get("excerpt"))

    acf = (
        item.get("acf")
        if isinstance(item.get("acf"), (dict, list))
        else {}
    )

    acf_text = acf_to_text(acf)
    acf_html = acf_html_fragments(acf)

    title = html_to_text(title_html) or str(
        item.get("slug") or "Bez tytułu"
    )

    body_text = html_to_text(content_html)
    excerpt_text = html_to_text(excerpt_html)

    text = merge_text_parts(
        [
            body_text,
            excerpt_text,
            acf_text,
        ]
    )

    guid = item.get("guid")
    guid_url = rendered_value(guid)

    raw_url = str(item.get("link") or guid_url or "")
    canonical_url = normalize_url(raw_url)

    source_host = urlsplit(str(source["base_url"])).netloc.lower()

    link_html = "\n".join(
        [
            content_html,
            excerpt_html,
            acf_html,
        ]
    )

    links = extract_links(
        html=link_html,
        page_url=canonical_url or str(source["base_url"]),
        source_host=source_host,
    )

    headings = extract_headings(
        content_html + "\n" + acf_html
    )

    wordpress_id = item.get("id")
    document_id = f"{source['id']}:{content_type}:{wordpress_id}"

    hash_source = f"{title}\n{canonical_url}\n{text}".encode("utf-8")
    content_hash = hashlib.sha256(hash_source).hexdigest()

    document: dict[str, Any] = {
        "id": document_id,
        "source_id": source["id"],
        "source_name": source["name"],
        "source_priority": source.get("priority"),
        "content_type": content_type,
        "wordpress_type": item.get("type"),
        "wordpress_id": wordpress_id,
        "status": item.get("status"),
        "slug": item.get("slug"),
        "url": canonical_url,
        "title": title,
        "text": text,
        "excerpt": excerpt_text or None,
        "headings": headings,
        "word_count": len(text.split()),
        "indexable": len(text.split()) >= 10,
        "published_at": item.get("date_gmt") or item.get("date"),
        "modified_at": item.get("modified_gmt") or item.get("modified"),
        "fetched_at": fetched_at,
        "parent_wordpress_id": item.get("parent"),
        "menu_order": item.get("menu_order"),
        "author_id": item.get("author"),
        "featured_media_id": item.get("featured_media"),
        "taxonomies": taxonomy_fields(item),
        "links": links,
        "content_hash": f"sha256:{content_hash}",
        "language": None,
        "meta": (
            item.get("meta")
            if isinstance(item.get("meta"), dict)
            else {}
        ),
    }

    if include_raw_html:
        document["raw_html"] = content_html

    if include_acf:
        document["acf"] = acf
        document["acf_text"] = acf_text or None

    return document


def build_attachment(
    item: dict[str, Any],
    source: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    title = html_to_text(rendered_value(item.get("title")))
    caption = html_to_text(rendered_value(item.get("caption")))
    description = html_to_text(rendered_value(item.get("description")))

    source_url = normalize_url(str(item.get("source_url") or ""))

    return {
        "id": f"{source['id']}:media:{item.get('id')}",
        "source_id": source["id"],
        "source_name": source["name"],
        "source_priority": source.get("priority"),
        "wordpress_id": item.get("id"),
        "url": source_url,
        "attachment_page_url": normalize_url(
            str(item.get("link") or "")
        ),
        "filename": (
            item.get("filename")
            or Path(urlsplit(source_url).path).name
        ),
        "mime_type": item.get("mime_type"),
        "media_type": item.get("media_type"),
        "filesize": item.get("filesize"),
        "title": title or None,
        "caption": caption or None,
        "description": description or None,
        "parent_wordpress_id": item.get("post"),
        "published_at": item.get("date_gmt") or item.get("date"),
        "modified_at": item.get("modified_gmt") or item.get("modified"),
        "fetched_at": fetched_at,
    }


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


def write_markdown(
    path: Path,
    documents: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        file.write("# UEW RAG — eksport treści\n\n")

        for document in documents:
            if not document.get("text"):
                continue

            file.write(f"## {document['title']}\n\n")
            file.write(f"- Źródło: {document['source_name']}\n")
            file.write(f"- Typ: {document['content_type']}\n")
            file.write(f"- URL: {document['url']}\n")
            file.write(
                "- Aktualizacja: "
                f"{document.get('modified_at') or 'brak danych'}\n\n"
            )
            file.write(document["text"])
            file.write("\n\n---\n\n")


def main() -> None:
    config = load_config()

    project = config.get("project", {})
    export_config = config.get("export", {})

    output_directory = Path(
        project.get(
            "output_directory",
            DEFAULT_OUTPUT_DIRECTORY,
        )
    )

    timeout = int(project.get("timeout_seconds", 30))
    request_delay = float(
        project.get("request_delay_seconds", 1)
    )

    include_raw_html = bool(
        export_config.get("include_raw_html", True)
    )

    include_acf = bool(
        export_config.get("include_acf", True)
    )

    collect_media_metadata = bool(
        export_config.get(
            "collect_media_metadata",
            True,
        )
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": project.get(
                "user_agent",
                "UEW-RAG-Exporter/1.0",
            ),
            "Accept": "application/json",
        }
    )

    fetched_at = utc_now()

    all_documents: list[dict[str, Any]] = []
    all_attachments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []

    enabled_sources = [
        source
        for source in config["sources"]
        if source.get("enabled", True)
    ]

    for source in enabled_sources:
        source_id = source["id"]
        base_url = str(source["base_url"]).rstrip("/")

        source_documents: list[dict[str, Any]] = []
        source_attachments: list[dict[str, Any]] = []

        print(f"\nEksport: {source['name']}")

        for content_type in source.get(
            "content_types",
            ["posts", "pages"],
        ):
            endpoint = (
                f"{base_url}/wp-json/wp/v2/{content_type}"
            )

            print(f"  Typ treści: {content_type}")

            try:
                items = fetch_collection(
                    session=session,
                    endpoint=endpoint,
                    timeout=timeout,
                    request_delay=request_delay,
                )

                for item in items:
                    source_documents.append(
                        build_document(
                            item=item,
                            source=source,
                            content_type=content_type,
                            fetched_at=fetched_at,
                            include_raw_html=include_raw_html,
                            include_acf=include_acf,
                        )
                    )

            except Exception as error:
                print(f"    BŁĄD: {error}")

                errors.append(
                    {
                        "source_id": source_id,
                        "content_type": content_type,
                        "endpoint": endpoint,
                        "error": str(error),
                    }
                )

            time.sleep(request_delay)

        if collect_media_metadata:
            endpoint = f"{base_url}/wp-json/wp/v2/media"
            print("  Załączniki z biblioteki mediów")

            try:
                media_items = fetch_collection(
                    session=session,
                    endpoint=endpoint,
                    timeout=timeout,
                    request_delay=request_delay,
                    extra_params={
                        "media_type": "application",
                    },
                )

                source_attachments = [
                    build_attachment(
                        item,
                        source,
                        fetched_at,
                    )
                    for item in media_items
                    if (
                        item.get("media_type") == "file"
                        or not str(
                            item.get("mime_type", "")
                        ).startswith("image/")
                    )
                ]

            except Exception as error:
                print(f"    BŁĄD: {error}")

                errors.append(
                    {
                        "source_id": source_id,
                        "content_type": "media_files",
                        "endpoint": endpoint,
                        "error": str(error),
                    }
                )

        source_documents.sort(
            key=lambda item: item["id"]
        )

        source_attachments.sort(
            key=lambda item: item["id"]
        )

        site_directory = (
            output_directory
            / "sites"
            / source_id
        )

        write_jsonl(
            site_directory / "documents.jsonl",
            source_documents,
        )

        write_jsonl(
            site_directory / "attachments.jsonl",
            source_attachments,
        )

        all_documents.extend(source_documents)
        all_attachments.extend(source_attachments)

        source_result = {
            "source_id": source_id,
            "source_name": source["name"],
            "document_count": len(source_documents),
            "indexable_document_count": sum(
                1
                for item in source_documents
                if item["indexable"]
            ),
            "attachment_count": len(
                source_attachments
            ),
            "content_types": source.get(
                "content_types",
                [],
            ),
        }

        source_results.append(source_result)

        print(
            f"  Zapisano: {len(source_documents)} dokumentów, "
            f"{len(source_attachments)} załączników."
        )

    all_documents.sort(
        key=lambda item: item["id"]
    )

    all_attachments.sort(
        key=lambda item: item["id"]
    )

    write_jsonl(
        output_directory
        / "rag"
        / "documents.jsonl",
        all_documents,
    )

    write_jsonl(
        output_directory
        / "rag"
        / "attachments.jsonl",
        all_attachments,
    )

    write_markdown(
        output_directory
        / "rag"
        / "all-content.md",
        all_documents,
    )

    status = {
        "project": project.get(
            "name",
            "uew-rag-export",
        ),
        "generated_at": fetched_at,
        "source_count": len(enabled_sources),
        "document_count": len(all_documents),
        "indexable_document_count": sum(
            1
            for item in all_documents
            if item["indexable"]
        ),
        "attachment_count": len(all_attachments),
        "error_count": len(errors),
        "sources": source_results,
    }

    manifest = {
        "project": status["project"],
        "generated_at": fetched_at,
        "format_version": "1.0",
        "files": {
            "documents": "public/rag/documents.jsonl",
            "attachments": "public/rag/attachments.jsonl",
            "markdown": "public/rag/all-content.md",
            "status": "public/status.json",
            "errors": "public/errors.json",
        },
    }

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    with (
        output_directory / "status.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            status,
            file,
            ensure_ascii=False,
            indent=2,
        )

    with (
        output_directory / "errors.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            errors,
            file,
            ensure_ascii=False,
            indent=2,
        )

    with (
        output_directory / "manifest.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            manifest,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\nEksport zakończony.")
    print(f"Dokumenty: {len(all_documents)}")
    print(f"Załączniki: {len(all_attachments)}")
    print(f"Błędy: {len(errors)}")


if __name__ == "__main__":
    main()
