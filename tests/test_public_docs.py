"""Local publication checks; no external links or Azure services are contacted."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import nbformat

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
            ROOT / "infra" / "pool" / "README.md"]


def headings(text):
    result = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        result.add(slug)
    return result


def check_links(source, text):
    for href in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", text):
        if re.match(r"^[a-z]+:", href):
            continue
        name, _, anchor = unquote(href).partition("#")
        target = (source.parent / name).resolve() if name else source
        assert target.is_file(), f"{source.name}: broken local link {href}"
        if anchor and target.suffix == ".md":
            assert anchor in headings(target.read_text(encoding="utf-8")), href


def test_markdown_and_notebook_links():
    assert len(MARKDOWN) == 8
    for path in MARKDOWN:
        check_links(path, path.read_text(encoding="utf-8"))
    for path in (ROOT / "examples").glob("*.ipynb"):
        notebook = nbformat.read(path, as_version=4)
        for cell in notebook.cells:
            if cell.cell_type == "markdown":
                check_links(path, cell.source)


def test_public_content_has_no_pilot_identifiers():
    paths = MARKDOWN + list((ROOT / "examples").glob("*.py")) + [
        ROOT / "examples" / ".env.example",
        *list((ROOT / "examples").glob("*.ipynb")),
        *list((ROOT / "docs" / "images").glob("*")),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"acrconfmcp\w*|(?:vnet|rg)-fabric-foundry|ambitiouscoast|"
            r"(?:hsm|kv)-cmcp|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|"
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            text, re.IGNORECASE,
        ), f"Environment-specific value in {path.name}"
        assert not re.search(r"C:[\\/]+Users[\\/]", text, re.IGNORECASE)


def test_architecture_assets_are_valid_and_editable():
    svg = ET.parse(ROOT / "docs" / "images" / "architecture.svg").getroot()
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert "http" not in "".join(
        node.attrib.get("href", "") for node in svg.iter()
    )
    diagram = json.loads((ROOT / "docs" / "images" / "architecture.excalidraw").read_text())
    assert diagram["type"] == "excalidraw" and diagram["version"] == 2
    elements = diagram["elements"]
    assert len({item["id"] for item in elements}) == len(elements)
    for item in elements:
        if item["type"] == "text":
            assert item["width"] > 0 and item["height"] > 0
            assert item["strokeColor"] == "#000000"
        if item["id"] in ("vnet", "control", "workers"):
            assert item["backgroundColor"] == "transparent"


def test_cost_table_has_one_consistent_baseline():
    text = (ROOT / "docs" / "session-pool-costs.md").read_text(encoding="utf-8")
    fixed = 20.28 + 153.30 + 32.85 + 7.30 + 180 + 21.90
    for count in (0, 2, 10, 20):
        worker = 0.158125 * 730 * count
        low, high = fixed + 35.48 + worker, fixed + 118.29 + worker
        assert f"${low:,.2f}-${high:,.2f}" in text
