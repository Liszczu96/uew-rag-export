import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


CONFIG_PATH = Path("config/sources.yml")
OUTPUT_PATH = Path("public/source-inventory.json")


def load_config() -> dict[str, Any]:
    """Wczytuje konfigurację projektu."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Nie znaleziono pliku: {CONFIG_PATH}"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Nieprawidłowa konfiguracja YAML.")

    if "sources" not in config:
        raise ValueError(
            "Brak sekcji 'sources' w konfiguracji."
        )

    return config


def get_json(
    session: requests.Session,
    url: str,
    timeout: int,
) -> tuple[Any | None, dict[str, Any]]:
    """Pobiera JSON i podstawowe metadane odpowiedzi."""
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
        )

        metadata = {
            "requested_url": url,
            "final_url": response.url,
            "status_code": response.status_code,
            "content_type": response.headers.get(
                "Content-Type"
            ),
            "x_wp_total": response.headers.get(
                "X-WP-Total"
            ),
            "x_wp_total_pages": response.headers.get(
                "X-WP-TotalPages"
            ),
            "error": None,
        }

        if response.status_code != 200:
            return None, metadata

        try:
            return response.json(), metadata
        except ValueError:
            metadata["error"] = (
                "Odpowiedź nie jest poprawnym JSON-em."
            )
            return None, metadata

    except requests.RequestException as error:
        return None, {
            "requested_url": url,
            "final_url": None,
            "status_code": None,
            "content_type": None,
            "x_wp_total": None,
            "x_wp_total_pages": None,
            "error": str(error),
        }


def summarize_collection(
    session: requests.Session,
    base_url: str,
    rest_base: str,
    timeout: int,
) -> dict[str, Any]:
    """Sprawdza kolekcję konkretnego typu treści."""
    endpoint = (
        f"{base_url}/wp-json/wp/v2/"
        f"{rest_base}?per_page=1"
    )

    data, metadata = get_json(
        session=session,
        url=endpoint,
        timeout=timeout,
    )

    sample_keys: list[str] = []
    sample_record: dict[str, Any] | None = None

    if isinstance(data, list) and data:
        first_item = data[0]

        if isinstance(first_item, dict):
            sample_keys = sorted(first_item.keys())

            sample_record = {
                "id": first_item.get("id"),
                "type": first_item.get("type"),
                "slug": first_item.get("slug"),
                "status": first_item.get("status"),
                "link": first_item.get("link"),
                "date": first_item.get("date"),
                "modified": first_item.get("modified"),
                "parent": first_item.get("parent"),
                "top_level_fields": sample_keys,
            }

    total = metadata.get("x_wp_total")
    total_pages = metadata.get("x_wp_total_pages")

    return {
        "rest_base": rest_base,
        "endpoint": endpoint,
        "status_code": metadata.get("status_code"),
        "total_records": (
            int(total) if total is not None else None
        ),
        "total_api_pages": (
            int(total_pages)
            if total_pages is not None
            else None
        ),
        "sample_available": sample_record is not None,
        "sample": sample_record,
        "error": metadata.get("error"),
    }


def inventory_source(
    session: requests.Session,
    source: dict[str, Any],
    timeout: int,
    request_delay: float,
) -> dict[str, Any]:
    """Tworzy inwentaryzację pojedynczej witryny."""
    base_url = source["base_url"].rstrip("/")

    print(f"\nInwentaryzacja: {source['name']}")
    print(f"Adres: {base_url}")

    root_data, root_metadata = get_json(
        session,
        f"{base_url}/wp-json/",
        timeout,
    )

    types_data, types_metadata = get_json(
        session,
        f"{base_url}/wp-json/wp/v2/types",
        timeout,
    )

    taxonomies_data, taxonomies_metadata = get_json(
        session,
        f"{base_url}/wp-json/wp/v2/taxonomies",
        timeout,
    )

    discovered_types = []

    if isinstance(types_data, dict):
        for type_slug, type_details in types_data.items():
            if not isinstance(type_details, dict):
                continue

            rest_base = type_details.get("rest_base")
            rest_namespace = type_details.get(
                "rest_namespace",
                "wp/v2",
            )

            type_result = {
                "slug": type_slug,
                "name": type_details.get("name"),
                "description": type_details.get(
                    "description"
                ),
                "hierarchical": type_details.get(
                    "hierarchical"
                ),
                "viewable": type_details.get("viewable"),
                "rest_base": rest_base,
                "rest_namespace": rest_namespace,
                "collection": None,
            }

            if (
                rest_base
                and rest_namespace == "wp/v2"
            ):
                type_result["collection"] = (
                    summarize_collection(
                        session=session,
                        base_url=base_url,
                        rest_base=rest_base,
                        timeout=timeout,
                    )
                )

                time.sleep(request_delay)

            discovered_types.append(type_result)

    discovered_taxonomies = []

    if isinstance(taxonomies_data, dict):
        for taxonomy_slug, details in (
            taxonomies_data.items()
        ):
            if not isinstance(details, dict):
                continue

            discovered_taxonomies.append(
                {
                    "slug": taxonomy_slug,
                    "name": details.get("name"),
                    "description": details.get(
                        "description"
                    ),
                    "hierarchical": details.get(
                        "hierarchical"
                    ),
                    "rest_base": details.get(
                        "rest_base"
                    ),
                    "rest_namespace": details.get(
                        "rest_namespace"
                    ),
                    "types": details.get("types", []),
                }
            )

    namespaces = []

    if isinstance(root_data, dict):
        namespaces = root_data.get(
            "namespaces",
            [],
        )

    standard_types = {
        "post",
        "page",
        "attachment",
    }

    custom_types = [
        item
        for item in discovered_types
        if item["slug"] not in standard_types
    ]

    total_public_records = 0

    for item in discovered_types:
        collection = item.get("collection")

        if (
            isinstance(collection, dict)
            and isinstance(
                collection.get("total_records"),
                int,
            )
        ):
            total_public_records += collection[
                "total_records"
            ]

    print(
        f"Typy treści: {len(discovered_types)}"
    )
    print(
        f"Typy niestandardowe: {len(custom_types)}"
    )
    print(
        f"Taksonomie: {len(discovered_taxonomies)}"
    )
    print(
        f"Łączna liczba rekordów: "
        f"{total_public_records}"
    )

    return {
        "source_id": source["id"],
        "source_name": source["name"],
        "base_url": base_url,
        "priority": source.get("priority"),
        "enabled": source.get("enabled", True),
        "required": source.get("required", False),
        "adapter": source.get("adapter"),
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "wordpress_root": {
            "status_code": root_metadata.get(
                "status_code"
            ),
            "namespaces": namespaces,
            "namespace_count": len(namespaces),
            "error": root_metadata.get("error"),
        },
        "types_endpoint": {
            "status_code": types_metadata.get(
                "status_code"
            ),
            "error": types_metadata.get("error"),
        },
        "taxonomies_endpoint": {
            "status_code": taxonomies_metadata.get(
                "status_code"
            ),
            "error": taxonomies_metadata.get(
                "error"
            ),
        },
        "content_type_count": len(discovered_types),
        "custom_content_type_count": len(custom_types),
        "taxonomy_count": len(
            discovered_taxonomies
        ),
        "total_public_records": total_public_records,
        "content_types": discovered_types,
        "custom_content_types": custom_types,
        "taxonomies": discovered_taxonomies,
    }


def main() -> None:
    """Uruchamia inwentaryzację źródeł."""
    config = load_config()

    project = config.get("project", {})
    timeout = int(
        project.get("timeout_seconds", 30)
    )
    request_delay = float(
        project.get(
            "request_delay_seconds",
            1,
        )
    )
    user_agent = project.get(
        "user_agent",
        "UEW-RAG-Exporter/1.0",
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": (
                "application/json, "
                "text/plain;q=0.9, */*;q=0.8"
            ),
        }
    )

    results = []
    skipped_sources = []

    for source in config["sources"]:
        if not source.get("enabled", True):
            skipped_sources.append(
                {
                    "source_id": source.get("id"),
                    "source_name": source.get("name"),
                    "reason": "Źródło jest wyłączone.",
                }
            )
            continue

        try:
            result = inventory_source(
                session=session,
                source=source,
                timeout=timeout,
                request_delay=request_delay,
            )
            results.append(result)

        except Exception as error:
            print(
                f"Błąd dla źródła "
                f"{source.get('name')}: {error}"
            )

            results.append(
                {
                    "source_id": source.get("id"),
                    "source_name": source.get("name"),
                    "base_url": source.get("base_url"),
                    "checked_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "error": str(error),
                }
            )

        time.sleep(request_delay)

    output = {
        "project": project.get(
            "name",
            "uew-rag-export",
        ),
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "inventoried_source_count": len(results),
        "skipped_source_count": len(
            skipped_sources
        ),
        "sources": results,
        "skipped_sources": skipped_sources,
    }

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT_PATH.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            output,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\nInwentaryzacja zakończona.")
    print(f"Wynik: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
