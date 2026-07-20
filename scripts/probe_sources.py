import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


CONFIG_PATH = Path("config/sources.yml")
OUTPUT_PATH = Path("public/source-probe.json")


def load_config() -> dict[str, Any]:
    """Wczytuje konfigurację źródeł z pliku YAML."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Nie znaleziono pliku konfiguracyjnego: {CONFIG_PATH}"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict) or "sources" not in config:
        raise ValueError("Plik sources.yml nie zawiera sekcji 'sources'.")

    return config


def request_url(
    session: requests.Session,
    url: str,
    timeout: int,
) -> dict[str, Any]:
    """Wykonuje zapytanie GET i zwraca podstawowe informacje."""
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
        )

        return {
            "requested_url": url,
            "final_url": response.url,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "ok": response.ok,
            "response": response,
            "error": None,
        }

    except requests.RequestException as error:
        return {
            "requested_url": url,
            "final_url": None,
            "status_code": None,
            "content_type": None,
            "ok": False,
            "response": None,
            "error": str(error),
        }


def check_json_endpoint(
    session: requests.Session,
    url: str,
    timeout: int,
) -> dict[str, Any]:
    """Sprawdza, czy endpoint zwraca poprawny JSON."""
    result = request_url(session, url, timeout)
    response = result.pop("response")

    result["valid_json"] = False
    result["item_count"] = None

    if response is not None and response.status_code == 200:
        try:
            data = response.json()
            result["valid_json"] = True

            if isinstance(data, list):
                result["item_count"] = len(data)
            elif isinstance(data, dict):
                result["item_count"] = len(data.keys())

        except ValueError:
            result["valid_json"] = False

    return result


def check_sitemap(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> dict[str, Any]:
    """Szuka standardowej mapy witryny."""
    candidates = [
        f"{base_url}/sitemap.xml",
        f"{base_url}/wp-sitemap.xml",
        f"{base_url}/sitemap_index.xml",
    ]

    checks = []

    for url in candidates:
        result = request_url(session, url, timeout)
        response = result.pop("response")

        valid_xml = False

        if response is not None and response.status_code == 200:
            text = response.text.lower()

            valid_xml = (
                "<urlset" in text
                or "<sitemapindex" in text
            )

        result["valid_sitemap"] = valid_xml
        checks.append(result)

        if valid_xml:
            return {
                "available": True,
                "url": result["final_url"],
                "checks": checks,
            }

    return {
        "available": False,
        "url": None,
        "checks": checks,
    }


def probe_source(
    session: requests.Session,
    source: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Przeprowadza audyt techniczny pojedynczej witryny."""
    base_url = source["base_url"].rstrip("/")

    print(f"\nSprawdzam: {source['name']}")
    print(f"Adres: {base_url}")

    homepage = request_url(session, base_url, timeout)
    homepage.pop("response")

    robots = request_url(
        session,
        f"{base_url}/robots.txt",
        timeout,
    )
    robots.pop("response")

    wp_root = check_json_endpoint(
        session,
        f"{base_url}/wp-json/",
        timeout,
    )

    wordpress_endpoints = {
        "pages": check_json_endpoint(
            session,
            f"{base_url}/wp-json/wp/v2/pages?per_page=1",
            timeout,
        ),
        "posts": check_json_endpoint(
            session,
            f"{base_url}/wp-json/wp/v2/posts?per_page=1",
            timeout,
        ),
        "media": check_json_endpoint(
            session,
            f"{base_url}/wp-json/wp/v2/media?per_page=1",
            timeout,
        ),
    }

    sitemap = check_sitemap(
        session,
        base_url,
        timeout,
    )

    available_wp_endpoints = [
        name
        for name, endpoint in wordpress_endpoints.items()
        if endpoint["status_code"] == 200
        and endpoint["valid_json"]
    ]

    wp_api_available = (
        wp_root["status_code"] == 200
        and wp_root["valid_json"]
        and len(available_wp_endpoints) > 0
    )

    if wp_api_available:
        recommended_adapter = "wordpress"
    elif sitemap["available"]:
        recommended_adapter = "sitemap_html"
    else:
        recommended_adapter = "seeded_html"

    print(f"Strona główna: {homepage['status_code']}")
    print(f"WordPress API: {wp_api_available}")
    print(f"Sitemapa: {sitemap['available']}")
    print(f"Rekomendowany adapter: {recommended_adapter}")

    return {
        "source_id": source["id"],
        "source_name": source["name"],
        "base_url": base_url,
        "priority": source.get("priority"),
        "enabled": source.get("enabled", True),
        "required": source.get("required", False),
        "configured_adapter": source.get("adapter", "auto"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "homepage": homepage,
        "robots": robots,
        "wordpress": {
            "available": wp_api_available,
            "root": wp_root,
            "available_endpoints": available_wp_endpoints,
            "endpoints": wordpress_endpoints,
        },
        "sitemap": sitemap,
        "recommended_adapter": recommended_adapter,
    }


def main() -> None:
    """Uruchamia audyt wszystkich źródeł."""
    config = load_config()

    project_config = config.get("project", {})
    timeout = int(project_config.get("timeout_seconds", 30))
    user_agent = project_config.get(
        "user_agent",
        "UEW-RAG-Exporter/1.0",
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": (
                "application/json, application/xml, "
                "text/xml, text/html;q=0.9, */*;q=0.8"
            ),
        }
    )

    results = []

    for source in config["sources"]:
        try:
            result = probe_source(
                session=session,
                source=source,
                timeout=timeout,
            )
            results.append(result)

        except Exception as error:
            results.append(
                {
                    "source_id": source.get("id"),
                    "source_name": source.get("name"),
                    "base_url": source.get("base_url"),
                    "checked_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "recommended_adapter": None,
                    "error": str(error),
                }
            )

            print(
                f"Błąd podczas sprawdzania "
                f"{source.get('name')}: {error}"
            )

    output = {
        "project": project_config.get(
            "name",
            "uew-rag-export",
        ),
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "source_count": len(results),
        "sources": results,
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

    print("\nAudyt zakończony.")
    print(f"Wynik zapisano w: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
