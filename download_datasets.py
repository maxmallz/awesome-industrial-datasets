"""Download dataset files linked by the JSON catalog where possible."""

import argparse
import csv
import json
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
EXTENSIONS = (
    ".tar.gz", ".csv", ".zip", ".xlsx", ".xls", ".tgz", ".gz",
    ".7z", ".rar", ".mat", ".parquet", ".h5", ".hdf5", ".data",
    ".txt",
)


def is_data_file(url):
    return urlparse(url).path.lower().endswith(EXTENSIONS)


def is_uci_page(url):
    parsed = urlparse(url)
    return parsed.hostname == "archive.ics.uci.edu" and (
        parsed.path.startswith("/dataset/") or parsed.path.startswith("/ml/datasets/")
    )


class UciLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if "/static/public/" in href and is_data_file(href):
                self.links.append(href)


class Client:
    def __init__(self):
        self.requested = False

    def open(self, url):
        if self.requested:
            time.sleep(1)
        self.requested = True
        return urlopen(Request(url, headers={"User-Agent": "industrial-datasets-downloader/1.0"}), timeout=30)


def uci_download_link(client, url):
    with client.open(url) as response:
        page = UciLinks()
        page.feed(response.read().decode("utf-8", errors="replace"))
        base_url = response.url
    for href in page.links:
        candidate = urljoin(base_url, href)
        if urlparse(candidate).hostname == "archive.ics.uci.edu":
            return candidate
    return None


def download(client, url, directory):
    name = Path(unquote(urlparse(url).path)).name
    destination = directory / name
    if destination.exists():
        return "exists", destination
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (name + ".part")
    try:
        with client.open(url) as response:
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise ValueError("server returned a web page instead of a dataset file")
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return "downloaded", destination


def process(path, output, client, dry_run):
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (path.stem, "error", "", str(exc))

    references = [ref.get("Link", "") for ref in metadata.get("References", [])]
    candidates = [url for url in references if url.startswith("https://") and is_data_file(url)]
    candidates += [url for url in references if url.startswith("https://") and is_uci_page(url)]
    if not candidates:
        source = next((url for url in references if url.startswith("https://")), "")
        return (path.stem, "manual", source, "No direct data file or supported UCI page")

    if dry_run:
        return (path.stem, "candidate", candidates[0], "No network request made")

    errors = []
    for url in candidates:
        try:
            file_url = uci_download_link(client, url) if is_uci_page(url) else url
            if file_url is None:
                errors.append(f"{url}: no download link found")
                continue
            state, destination = download(client, file_url, output / path.stem)
            return (path.stem, state, file_url, str(destination))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    return (path.stem, "error", candidates[0], "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "downloads")
    parser.add_argument("--dataset", help="Download only this JSON filename stem")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without network requests")
    args = parser.parse_args()

    paths = sorted((ROOT / "json").glob("*.json"))
    if args.dataset:
        paths = [path for path in paths if path.stem == args.dataset]
        if not paths:
            parser.error(f"No catalog entry found for {args.dataset!r}")

    client = Client()
    rows = []
    for path in paths:
        row = process(path, args.output, client, args.dry_run)
        rows.append(row)
        print(f"{row[0]}: {row[1]} {row[2]} ({row[3]})")

    if not args.dry_run:
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / "download_report.csv").open("w", newline="", encoding="utf-8") as report:
            writer = csv.writer(report)
            writer.writerow(["dataset", "status", "url", "detail"])
            writer.writerows(rows)


if __name__ == "__main__":
    main()