"""
WUT OMEGA-PSIR MCP Server  v5
==============================
Model Context Protocol server for the Warsaw University of Technology research
repository (OMEGA-PSIR / repo.pw.edu.pl).

REST API base:  https://repo.pw.edu.pl/seam/resource/rest
URL pattern:    GET /accesspoint/search/{record_type}/@{field}='{value}'/{offset}/{limit}
Response:       XML  <collection>  containing zero or more record elements.

Supported record types:
  author   — WUT researcher profiles
  article  — journal articles and conference papers
  book     — monographs and other book-type publications
  phd      — doctoral dissertations

Transport:
  stdio            — default; used by Claude Desktop and the MCP CLI.
  SSE / HTTP       — activated when the PORT or WEBSITES_PORT env var is set
                     (Azure App Service convention).

"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Section 1 — Constants
# ---------------------------------------------------------------------------

WUT_API_BASE_URL       = "https://repo.pw.edu.pl/seam/resource/rest"
WUT_REPOSITORY_BASE_URL = "https://repo.pw.edu.pl"
MAX_RECORDS_PER_PAGE   = 25   # WUT API returns at most 25 records per page

mcp_server = Server("omega-psir-mcp")

# ---------------------------------------------------------------------------
# Section 2 — Diacritics helpers
# ---------------------------------------------------------------------------

_DIACRITICS_TRANSLATION_TABLE = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)


def strip_diacritics(text_with_polish_characters: str) -> str:
    """Remove Polish diacritical marks, returning ASCII equivalent."""
    return text_with_polish_characters.translate(_DIACRITICS_TRANSLATION_TABLE)


def _generate_diacritics_variants(surname_or_name: str) -> list[str]:
    """
    Return a list of spelling variants for a Polish name/surname.

    The first element is always the original. Subsequent elements substitute
    common Latin letters with their accented Polish equivalents:
        o → ó, l → ł, z → ż, z → ź, a → ą, e → ę, s → ś, c → ć, n → ń

    Only variants that differ from already-generated ones are included.

    Needed because users often type "Rybinski" without diacritics, but the WUT
    REST API performs exact matching and requires the accented form "Rybiński".
    search_wut_api() calls this when an unaccented query returns empty results.
    """
    ascii_to_polish_substitutions = [
        ("o", "ó"), ("l", "ł"), ("z", "ż"), ("z", "ź"),
        ("a", "ą"), ("e", "ę"), ("s", "ś"), ("c", "ć"), ("n", "ń"),
    ]
    seen_variants: set[str] = {surname_or_name}
    generated_variants: list[str] = [surname_or_name]
    for ascii_character, polish_character in ascii_to_polish_substitutions:
        candidate_variant = surname_or_name.replace(ascii_character, polish_character)
        if candidate_variant not in seen_variants:
            seen_variants.add(candidate_variant)
            generated_variants.append(candidate_variant)
    return generated_variants


# ---------------------------------------------------------------------------
# Section 3 — XML utilities
# ---------------------------------------------------------------------------

_XML_NAMESPACE_PATTERN = re.compile(r"\{[^}]+\}")


def _strip_xml_namespace_prefixes(xml_root_element: ET.Element) -> None:
    """Strip Clark-notation namespace prefixes from all tags and attrib keys in-place."""
    for node in xml_root_element.iter():
        node.tag = _XML_NAMESPACE_PATTERN.sub("", node.tag)
        node.attrib = {_XML_NAMESPACE_PATTERN.sub("", k): v for k, v in node.attrib.items()}


def _parse_xml_bytes_to_element(response_bytes: bytes) -> ET.Element:
    """Parse XML bytes, strip namespaces, return root element."""
    xml_root_element = ET.fromstring(response_bytes)
    _strip_xml_namespace_prefixes(xml_root_element)
    return xml_root_element


def _get_child_text(
    parent_element: ET.Element,
    *child_tags: str,
    default: str = "",
) -> str:
    """
    Walk a chain of child-tag names and return the text of the last found element.
    Returns *default* if any step is missing or the element has no text.
    """
    current_node = parent_element
    for child_tag in child_tags:
        child_element = current_node.find(child_tag)
        if child_element is None:
            return default
        current_node = child_element
    return (current_node.text or "").strip() or default


def _extract_record_id(record_element: ET.Element) -> str:
    """
    Extract the WUT numeric record ID.
    Tries the 'id' attribute first, then a direct <id> child element.
    """
    attribute_id_value = record_element.get("id", "").strip()
    if attribute_id_value:
        return attribute_id_value
    id_child_element = record_element.find("id")
    if id_child_element is not None and id_child_element.text:
        return id_child_element.text.strip()
    return ""


def _get_all_direct_children(
    parent_element: ET.Element,
    child_tag: str,
) -> list[ET.Element]:
    """Return all direct children with the given tag."""
    return parent_element.findall(child_tag)


# ---------------------------------------------------------------------------
# Section 4 — Entity parsers
# ---------------------------------------------------------------------------

_ARTICLE_TYPE_MAP: dict[str, str] = {
    "article": "Journal article",
    "journal": "Journal article",
    "journalpaper": "Journal article",
    "journalarticle": "Journal article",
    "art": "Journal article",
    "conference": "Conference paper",
    "conferencepaper": "Conference paper",
    "conferenceproc": "Conference paper",
    "conferenceproceedings": "Conference paper",
    "proceedings": "Conference paper",
    "proceeding": "Conference paper",
    "konfpaper": "Conference paper",
    "konf": "Conference paper",
    "bookchapter": "Book chapter",
    "chapter": "Book chapter",
    "roz": "Book chapter",
    "inbook": "Book chapter",
    "inproceedings": "Conference paper",
    "incollection": "Book chapter",
}

_BOOK_TYPE_MAP: dict[str, str] = {
    "book": "Book",
    "monograph": "Book",
    "textbook": "Book",
    "edited": "Edited book",
    "editedbook": "Edited book",
    "editedvolume": "Edited book",
    "proceedings": "Conference proceedings",
    "conferenceproceedings": "Conference proceedings",
    "bookchapter": "Book chapter",
    "chapter": "Book chapter",
}

_BOOK_DISPLAY_TYPES = frozenset({"Book", "Edited book", "Conference proceedings"})

_PUB_TYPE_ORDER = [
    "Journal article",
    "Conference paper",
    "Book chapter",
    "Book",
    "Edited book",
    "Conference proceedings",
]


def _map_article_type(raw_type: str, xml_element: "ET.Element") -> str:
    """
    Determine the human-readable publication sub-type for an article element.

    Priority:
      1. Explicit publicationType / articleType field in the XML.
      2. Structural inference: <book> child means conference/proceedings paper;
         <journalissue> child means journal article.
      3. Default: "Journal article".
    """
    if raw_type:
        key = raw_type.lower().replace(" ", "").replace("_", "").replace("-", "")
        if key in _ARTICLE_TYPE_MAP:
            return _ARTICLE_TYPE_MAP[key]

    # Structural inference
    if xml_element.find("book") is not None:
        return "Conference paper"
    if xml_element.find("journalissue") is not None:
        return "Journal article"
    return "Journal article"


def _map_book_type(raw_type: str) -> str:
    """Map a WUT bookType string to a human-readable category label."""
    if not raw_type:
        return "Book"
    key = raw_type.lower().replace(" ", "").replace("_", "").replace("-", "")
    return _BOOK_TYPE_MAP.get(key, "Book")

def _parse_author_list(publication_element: ET.Element) -> list[dict]:
    """
    Parse all <author> child elements into a list of dicts.
    Each dict has keys: name, id, profileUrl.
    """
    author_list: list[dict] = []
    for author_element in _get_all_direct_children(publication_element, "author"):
        record_id = _extract_record_id(author_element)
        author_list.append({
            "name": (
                _get_child_text(author_element, "presentedFullName")
                or f"{_get_child_text(author_element, 'name')} {_get_child_text(author_element, 'surname')}".strip()
            ),
            "id": record_id,
            "profileUrl": f"{WUT_REPOSITORY_BASE_URL}/info/card/WUT{record_id}" if record_id else "",
        })
    return author_list


def _get_author_display_names(publication_element: ET.Element) -> list[str]:
    """Return a flat list of author display names from all <author> children."""
    return [author_entry["name"] for author_entry in _parse_author_list(publication_element) if author_entry["name"]]


def parse_researcher_element(author_xml_element: ET.Element) -> dict:
    """
    Parse a WUT <author> XML element into a structured profile dict.

    Note: the API uses 'possitionEN' and 'possitionPL' (double 's') — this is
    an intentional misspelling in the upstream API schema.
    """
    record_id = _extract_record_id(author_xml_element)
    raw_hindex_text = _get_child_text(author_xml_element, "authorprofile", "hindex")
    try:
        hindex = int(raw_hindex_text) if raw_hindex_text else None
    except ValueError:
        hindex = None

    # Affiliation — prefer structured <unit> hierarchy; fall back to filtered itertext.
    # The hierarchy runs department → faculty → university (PW/WUT); skip the top level.
    _UUID_LIKE = re.compile(r"^[A-Z]+-[0-9a-f-]{20,}$", re.IGNORECASE)
    _SKIP_AFF_VALUES = frozenset({
        "completed", "false", "true", "active", "inactive", "closed",
        "politechnika warszawska", "warsaw university of technology",
    })
    affiliation_text = _get_child_text(author_xml_element, "affiliation")
    if not affiliation_text:
        affiliation_element = author_xml_element.find("affiliation")
        if affiliation_element is not None:
            # Strategy 1: structured <unit> children with <nameEN>
            unit_names: list[str] = []
            for unit_elem in affiliation_element.findall(".//unit"):
                name_en = (
                    _get_child_text(unit_elem, "nameEN")
                    or _get_child_text(unit_elem, "name")
                )
                abbrev    = (_get_child_text(unit_elem, "abbreviation")   or "").strip().upper()
                abbrev_en = (_get_child_text(unit_elem, "abbreviationEN") or "").strip().upper()
                if name_en and abbrev not in ("PW", "WUT") and abbrev_en not in ("WUT",):
                    unit_names.append(name_en)
            if unit_names:
                affiliation_text = ", ".join(unit_names)
            else:
                # Strategy 2: filtered itertext — keep multi-word, non-UUID, non-boolean strings
                candidates: list[str] = []
                for chunk in affiliation_element.itertext():
                    chunk = chunk.strip()
                    if not chunk or len(chunk) < 8:
                        continue
                    if _UUID_LIKE.match(chunk):
                        continue
                    if chunk.lower() in _SKIP_AFF_VALUES:
                        continue
                    if " " not in chunk:   # skip single-word abbreviations
                        continue
                    candidates.append(chunk)
                affiliation_text = ", ".join(candidates[:2])

    return {
        "id": record_id,
        "urn": _build_record_urn(record_id),
        "fullName": (
            _get_child_text(author_xml_element, "presentedFullName")
            or f"{_get_child_text(author_xml_element, 'name')} {_get_child_text(author_xml_element, 'surname')}".strip()
        ),
        "firstName": _get_child_text(author_xml_element, "name"),
        "lastName": _get_child_text(author_xml_element, "surname"),
        "academicDegree": _get_child_text(author_xml_element, "academicDegree"),
        "officialDegree": _get_child_text(author_xml_element, "officialAcademicDegree", "value"),
        "positionEN": _get_child_text(author_xml_element, "possitionEN"),   # API misspelling: double 's'
        "positionPL": _get_child_text(author_xml_element, "possitionPL"),   # API misspelling: double 's'
        "affiliation": affiliation_text,
        "status": _get_child_text(author_xml_element, "status"),
        "researchArea": _get_child_text(author_xml_element, "researchArea"),
        "hindex": hindex,
        "profileUrl": f"{WUT_REPOSITORY_BASE_URL}/info/card/WUT{record_id}" if record_id else "",
    }


_FOUR_DIGIT_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")

# Tags that carry administrative/system timestamps, never publication years.
# Skipped in the last-resort descendant scan to prevent picking up dates like
# lastModified=2026-05-... or scoreDate=2026-... as the publication year.
_ADMINISTRATIVE_TAGS_TO_SKIP = frozenset({
    "metaData", "lastModified", "lastModifiedBy", "created", "meta_datestamp",
    "scoreDate", "evaluationDate", "verificationDate", "disciplinesApprovalDate",
    "responseDate", "lastTransferDate",
    "legalBasis",   # e.g. "Rozporządzenie MNiSW z dnia 22 lutego 2019 r." — regulation year
    "rulesetName",  # e.g. "reguly_2017_v1f" — scoring rule version year
    "id",           # UUID-like IDs contain year-looking fragments e.g. "WEITI-8ad8b193-1981-4d19-..."
})

_DIRECT_YEAR_FIELD_NAMES = (
    "year", "issueDate", "publishDate", "publicationDate",
    "publicationYear", "datePublished", "beginDate",
    "defenseDate",  # PhD theses
)
# Step-wise paths tried in order.  journalissue covers journal articles;
# book covers conference-proceedings articles (stored as article + book child).
_NESTED_YEAR_ELEMENT_PATHS = (
    ("journalissue", "issueDate"),
    ("book", "issueDate"),   # conference proceedings: <article><book><issueDate>
    ("book", "year"),
    ("book", "date"),
    ("issue", "year"),
)
# Fields searched recursively inside <journalissue> for conference/proceedings papers
# where issueDate / date is nested inside a <conference> or <journalseries> child.
_JOURNAL_ISSUE_RECURSIVE_YEAR_FIELDS = ("issueDate", "date", "startDate")


def _extract_publication_year(record_element: ET.Element) -> str:
    """
    Best-effort extraction of a 4-digit publication year from a record element.

    Strategy (in order):
    1. Direct children of the record element (covers books and simple records).
    2. Named paths: journalissue/issueDate, book/issueDate (journal & conference articles).
    3. Recursive search within <journalissue> (conference/proceedings papers where
       issueDate is nested inside a <conference> or <journalseries> sub-element).
    4. Last-resort scan of all descendants, skipping known administrative tags
       (lastModified, scoreDate, evaluationDate, etc.) that carry system timestamps.
    """
    # Step 1 — direct children
    for year_field_name in _DIRECT_YEAR_FIELD_NAMES:
        year_candidate_element = record_element.find(year_field_name)
        if year_candidate_element is not None and year_candidate_element.text:
            year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(year_candidate_element.text)
            if year_regex_match:
                return year_regex_match.group(0)

    # Step 2 — specific nested paths
    for element_path in _NESTED_YEAR_ELEMENT_PATHS:
        current_node = record_element
        for path_tag in element_path:
            current_node = current_node.find(path_tag) if current_node is not None else None
        if current_node is not None and current_node.text:
            year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(current_node.text)
            if year_regex_match:
                return year_regex_match.group(0)

    # Step 3 — recursive search within journalissue (conference papers)
    journal_issue_element = record_element.find("journalissue")
    if journal_issue_element is not None:
        for year_field_name in _JOURNAL_ISSUE_RECURSIVE_YEAR_FIELDS:
            year_candidate_element = journal_issue_element.find(".//" + year_field_name)
            if year_candidate_element is not None and year_candidate_element.text:
                year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(year_candidate_element.text)
                if year_regex_match:
                    return year_regex_match.group(0)

    # Step 4 — last resort: scan all descendants, skip admin timestamps
    for descendant_node in record_element.iter():
        if descendant_node.tag in _ADMINISTRATIVE_TAGS_TO_SKIP:
            continue
        if descendant_node.text:
            year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(descendant_node.text)
            if year_regex_match:
                return year_regex_match.group(0)
    return ""


def parse_article_element(article_xml_element: ET.Element) -> dict:
    """Parse a WUT <article> (or <publication>) element into a structured dict."""
    record_id = _extract_record_id(article_xml_element)
    raw_pub_type = (
        _get_child_text(article_xml_element, "publicationType")
        or _get_child_text(article_xml_element, "articleType")
        or _get_child_text(article_xml_element, "type")
        or ""
    )
    pub_type = _map_article_type(raw_pub_type, article_xml_element)

    journal_name = (
        _get_child_text(article_xml_element, "journalissue", "journalseries", "title")
        or _get_child_text(article_xml_element, "journalissue", "journalseries", "name")
        or _get_child_text(article_xml_element, "journalissue", "title")
        or _get_child_text(article_xml_element, "journal")
        or _get_child_text(article_xml_element, "book", "title")
        or _get_child_text(article_xml_element, "book", "publisher")
    )
    return {
        "type": pub_type,
        "id": record_id,
        "urn": _build_record_urn(record_id),
        "title": _get_child_text(article_xml_element, "title"),
        "authors": _get_author_display_names(article_xml_element),
        "year": _extract_publication_year(article_xml_element),
        "doi": _get_child_text(article_xml_element, "doi"),
        "journal": journal_name,
        "collation": _get_child_text(article_xml_element, "collation"),
        "score": _get_child_text(article_xml_element, "score"),
        "abstractEN": _get_child_text(article_xml_element, "abstractEN"),
        "keywordsEN": _get_child_text(article_xml_element, "keywordsEN"),
        "keywordsPL": _get_child_text(article_xml_element, "keywordsPL"),
        "url": f"{WUT_REPOSITORY_BASE_URL}/info/article/{record_id}" if record_id else "",
    }


def parse_book_element(book_xml_element: ET.Element) -> dict:
    """Parse a WUT <book> or <bookchapter> element into a structured dict."""
    record_id = _extract_record_id(book_xml_element)
    publisher_name = (
        _get_child_text(book_xml_element, "publisher")
        or _get_child_text(book_xml_element, "publisherInstitution", "name")
    )
    return {
        "type": _map_book_type(_get_child_text(book_xml_element, "bookType") or ""),
        "id": record_id,
        "urn": _build_record_urn(record_id),
        "title": _get_child_text(book_xml_element, "title"),
        "authors": _get_author_display_names(book_xml_element),
        "year": _extract_publication_year(book_xml_element),
        "doi": _get_child_text(book_xml_element, "doi"),
        "isbn": _get_child_text(book_xml_element, "isbn"),
        "publisher": publisher_name,
        "collation": _get_child_text(book_xml_element, "collation"),
        "abstractEN": _get_child_text(book_xml_element, "abstractEN"),
        "keywordsEN": _get_child_text(book_xml_element, "keywordsEN"),
        "keywordsPL": _get_child_text(book_xml_element, "keywordsPL"),
        "url": f"{WUT_REPOSITORY_BASE_URL}/info/book/{record_id}" if record_id else "",
    }


def parse_phd_thesis_element(phd_xml_element: ET.Element) -> dict:
    """
    Parse a WUT <phd> (doctoral dissertation) element.

    Title preference: titleEN → titlePL → title.
    Supervisor lookup: tries <supervisor> first, then <promoter> as fallback.
    """
    record_id = _extract_record_id(phd_xml_element)

    thesis_title = (
        _get_child_text(phd_xml_element, "titleEN")
        or _get_child_text(phd_xml_element, "titlePL")
        or _get_child_text(phd_xml_element, "title")
    )

    # Author name — <author type="author"> is a nested element, not plain text
    author_element = phd_xml_element.find("author")
    if author_element is not None:
        thesis_author_name = (
            _get_child_text(author_element, "presentedFullName")
            or f"{_get_child_text(author_element, 'name')} {_get_child_text(author_element, 'surname')}".strip()
        )
    else:
        thesis_author_name = ""

    # Supervisor name
    supervisor_element = phd_xml_element.find("supervisor")
    if supervisor_element is None:
        supervisor_element = phd_xml_element.find("promoter")
    if supervisor_element is not None:
        supervisor_full_name = f"{_get_child_text(supervisor_element, 'name')} {_get_child_text(supervisor_element, 'surname')}".strip()
    else:
        supervisor_full_name = ""

    return {
        "type": "phd_thesis",
        "id": record_id,
        "urn": _build_record_urn(record_id),
        "titleEN": _get_child_text(phd_xml_element, "titleEN"),
        "titlePL": _get_child_text(phd_xml_element, "titlePL"),
        "title": thesis_title,
        "author": thesis_author_name,
        "supervisor": supervisor_full_name,
        "year": _extract_publication_year(phd_xml_element),
        "defenseDate": _get_child_text(phd_xml_element, "defenseDate"),
        "abstractEN": _get_child_text(phd_xml_element, "abstractEN"),
        "abstractPL": _get_child_text(phd_xml_element, "abstractPL"),
        "keywordsEN": _get_child_text(phd_xml_element, "keywordsEN"),
        "keywordsPL": _get_child_text(phd_xml_element, "keywordsPL"),
        "url": f"{WUT_REPOSITORY_BASE_URL}/info/phd/{record_id}" if record_id else "",
    }


# ---------------------------------------------------------------------------
# Section 5 — HTTP client and cache
# ---------------------------------------------------------------------------

_HTTP_REQUEST_HEADERS = {
    "Accept": "application/xml",
    "User-Agent": "OmegaPSIR-MCP/3.0",
}
_HTTP_CLIENT = httpx.AsyncClient(
    headers=_HTTP_REQUEST_HEADERS,
    timeout=30.0,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)

# In-memory response cache: url → (expiry_monotonic_ts, raw_bytes | None)
# None is cached for empty/error responses so we don't hammer dead endpoints.
# TTL matches Claude's prompt-cache window so repeated tool calls in one session
# are free.
_RESPONSE_CACHE: dict[str, tuple[float, bytes | None]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


async def _fetch_api_page(
    record_type: str,
    search_field: str,
    search_value: str,
    pagination_offset: int,
    page_size: int,
) -> ET.Element | None:
    """
    Single paged request to the WUT REST API.

    URL pattern: /accesspoint/search/{record_type}/@{search_field}='{search_value}'/{pagination_offset}/{page_size}
    Returns the parsed root element, or None on 404/empty/error.
    Results are cached for _CACHE_TTL_SECONDS seconds (including None for empty responses).
    """
    # Polish diacritics and spaces in field values must be percent-encoded;
    # safe="" ensures even forward-slash characters in values are encoded.
    url_encoded_field_value = quote(search_value, safe="")
    request_url = (
        f"{WUT_API_BASE_URL}/accesspoint/search/{record_type}"
        f"/@{search_field}='{url_encoded_field_value}'/{pagination_offset}/{page_size}"
    )

    current_monotonic_time = time.monotonic()
    if request_url in _RESPONSE_CACHE:
        cache_expiry_time, cached_response_bytes = _RESPONSE_CACHE[request_url]
        if current_monotonic_time < cache_expiry_time:
            if cached_response_bytes is None:
                return None
            return _parse_xml_bytes_to_element(cached_response_bytes)

    try:
        http_response = await _HTTP_CLIENT.get(request_url)
        if http_response.status_code == 404:
            _RESPONSE_CACHE[request_url] = (current_monotonic_time + _CACHE_TTL_SECONDS, None)
            return None
        http_response.raise_for_status()
        response_bytes = http_response.content
        _RESPONSE_CACHE[request_url] = (current_monotonic_time + _CACHE_TTL_SECONDS, response_bytes)
        return _parse_xml_bytes_to_element(response_bytes)
    except (httpx.HTTPError, ET.ParseError):
        # ET.ParseError fires when the API returns an HTML error page instead of XML
        # (observed for unsupported record types and high pagination offsets).
        _RESPONSE_CACHE[request_url] = (current_monotonic_time + _CACHE_TTL_SECONDS, None)
        return None


async def _fetch_all_api_pages(
    record_type: str,
    search_field: str,
    search_value: str,
    maximum_results: int,
) -> list[ET.Element]:
    """
    Paginate _fetch_api_page to collect up to *maximum_results* record elements.

    Stops early if a page returns fewer records than requested (last page)
    or returns None (error/empty).
    """
    accumulated_record_elements: list[ET.Element] = []
    pagination_offset = 0
    while len(accumulated_record_elements) < maximum_results:
        page_size = min(MAX_RECORDS_PER_PAGE, maximum_results - len(accumulated_record_elements))
        page_root_element = await _fetch_api_page(
            record_type, search_field, search_value, pagination_offset, page_size
        )
        if page_root_element is None:
            break
        # Records are direct children; filter out wrapper/meta elements
        page_record_elements = [
            record_element for record_element in page_root_element
            if record_element.tag not in ("status", "count", "total", "offset", "limit")
        ]
        if not page_record_elements:
            break
        accumulated_record_elements.extend(page_record_elements)
        if len(page_record_elements) < page_size:
            break
        pagination_offset += page_size
    return accumulated_record_elements


async def search_wut_api(
    record_type: str,
    search_field: str,
    search_value: str,
    maximum_results: int = 25,
) -> list[ET.Element]:
    """
    High-level search with cold-start retry and Polish diacritics fallback.

    1. Calls _fetch_all_api_pages.
    2. If empty: waits 0.5 s and retries once (cold-start API quirk).
    3. If still empty AND this is a name/surname field: tries accented variants
       generated by _generate_diacritics_variants, with 0.3 s between attempts.
    """
    record_elements = await _fetch_all_api_pages(record_type, search_field, search_value, maximum_results)
    if not record_elements:
        # The WUT API can return empty on the first request after a cold start;
        # a single 500 ms pause and retry is sufficient to recover.
        await asyncio.sleep(0.5)
        record_elements = await _fetch_all_api_pages(record_type, search_field, search_value, maximum_results)

    if not record_elements and search_field in (
        "surname", "name", "fullName", "presentedFullName",
        "author.surname", "supervisor.surname",
    ):
        # API requires exact diacritics match ("Rybinski" ≠ "Rybiński").
        # Try substituted variants so users can type without Polish characters.
        for diacritics_variant in _generate_diacritics_variants(search_value)[1:]:
            await asyncio.sleep(0.3)
            record_elements = await _fetch_all_api_pages(
                record_type, search_field, diacritics_variant, maximum_results
            )
            if record_elements:
                break

    return record_elements


# ---------------------------------------------------------------------------
# Section 6 — Name helpers
# ---------------------------------------------------------------------------

def _split_full_name(full_name: str) -> tuple[str, str]:
    """
    Split a full name into (first_names, surname).

    The last whitespace-delimited token is treated as the surname.
    A single-token name is returned as ("", token).
    """
    name_parts = full_name.strip().split()
    if not name_parts:
        return ("", "")
    if len(name_parts) == 1:
        return ("", name_parts[0])
    return (" ".join(name_parts[:-1]), name_parts[-1])


def _filter_elements_by_first_name(
    researcher_elements: list[ET.Element],
    first_name_prefix: str,
) -> list[ET.Element]:
    """
    Filter *researcher_elements* to those whose <name> child starts with
    *first_name_prefix* (case- and diacritics-insensitive).

    If the filter would leave an empty list, the original list is returned unchanged
    (better to return ambiguous results than nothing).
    """
    if not first_name_prefix:
        return researcher_elements
    normalized_first_name = strip_diacritics(first_name_prefix.lower())
    filtered_researcher_elements = [
        researcher_element for researcher_element in researcher_elements
        if strip_diacritics(
            (_get_child_text(researcher_element, "name") or "").lower()
        ).startswith(normalized_first_name)
    ]
    return filtered_researcher_elements if filtered_researcher_elements else researcher_elements


# ---------------------------------------------------------------------------
# Section 7 — Researcher resolution helpers
# ---------------------------------------------------------------------------

def _build_record_urn(record_id: str) -> str:
    """
    Build a WUT URN from a numeric record ID.

    Returns "" for empty input, the value unchanged if it already starts with
    "urn:", otherwise "urn:pw-repo:WUT{record_id}".
    """
    if not record_id:
        return ""
    if record_id.startswith("urn:"):
        return record_id
    return f"urn:pw-repo:WUT{record_id}"


def _compute_publication_statistics(publications: list[dict]) -> dict:
    """
    Compute aggregate publication statistics in a single pass.

    Returns a dict with:
      totalPublications, topKeywords (up to 8), topVenues (up to 5),
      ministryScoreAvg (float | None), activeYears (sorted list of strings).
    """
    year_frequency_counter: Counter = Counter()
    keyword_frequency_counter: Counter = Counter()
    venue_frequency_counter: Counter = Counter()
    ministry_score_values: list[float] = []

    _KEYWORD_SEPARATOR_PATTERN = re.compile(r"[,;/]+")

    for publication in publications:
        publication_year = publication.get("year", "")
        if publication_year:
            year_frequency_counter[publication_year] += 1

        for keyword_field_name in ("keywordsEN", "keywordsPL"):
            raw_keywords_text = publication.get(keyword_field_name, "")
            if raw_keywords_text:
                for keyword_text in _KEYWORD_SEPARATOR_PATTERN.split(raw_keywords_text):
                    keyword_text = keyword_text.strip().lower()
                    if len(keyword_text) > 2:
                        keyword_frequency_counter[keyword_text] += 1

        venue_name = publication.get("journal") or publication.get("publisher") or ""
        if venue_name:
            venue_frequency_counter[venue_name] += 1

        raw_score_text = publication.get("score", "")
        if raw_score_text:
            try:
                ministry_score_values.append(float(raw_score_text))
            except ValueError:
                pass

    return {
        "totalPublications": len(publications),
        "topKeywords": [keyword_text for keyword_text, _ in keyword_frequency_counter.most_common(8)],
        "topVenues": [venue_name for venue_name, _ in venue_frequency_counter.most_common(5)],
        "ministryScoreAvg": (
            round(sum(ministry_score_values) / len(ministry_score_values), 2)
            if ministry_score_values else None
        ),
        "activeYears": sorted(year_frequency_counter.keys()),
    }


async def _resolve_researcher_to_profile(
    researcher_name: str = "",
    author_id: str | None = None,
) -> tuple[bool, dict | str]:
    """
    Resolve a researcher identifier to a profile dict or a disambiguation payload.

    Returns (requires_disambiguation, payload) where:
      - requires_disambiguation=False, payload=profile dict  → single match found
      - requires_disambiguation=True,  payload=JSON string   → multiple matches, user must choose
      - requires_disambiguation=False, payload=""             → nothing found

    Path A — author_id given:
        Strip the "urn:pw-repo:WUT" prefix if present to obtain the numeric ID.
        Fetch the full profile from the API and return it.

    Path B — name only:
        Split into (first_name_part, surname) → search by surname → narrow by first name →
        parse_researcher_element on each match.
        1 result → return profile dict.
        N results → return disambiguation JSON.
    """
    # Path A — resolve by numeric ID or URN
    if author_id:
        record_id = re.sub(r"^urn:pw-repo:WUT", "", str(author_id).strip())
        author_record_elements = await search_wut_api("author", "id", record_id, maximum_results=1)
        if author_record_elements:
            researcher_profile = parse_researcher_element(author_record_elements[0])
            return False, researcher_profile
        return False, {"id": record_id, "fullName": author_id}

    # Path B — resolve by name
    if not researcher_name:
        return False, ""

    first_name_part, surname = _split_full_name(researcher_name)
    if not surname:
        return False, ""

    author_record_elements = await search_wut_api("author", "surname", surname, maximum_results=50)
    if not author_record_elements:
        return False, ""

    if first_name_part:
        author_record_elements = _filter_elements_by_first_name(author_record_elements, first_name_part)

    researcher_profiles = [parse_researcher_element(record_element) for record_element in author_record_elements]

    if len(researcher_profiles) == 1:
        return False, researcher_profiles[0]

    if len(researcher_profiles) > 1:
        disambiguation_options = []
        for option_number, researcher_profile in enumerate(researcher_profiles, start=1):
            disambiguation_options.append({
                "option": option_number,
                "id": researcher_profile["id"],
                "urn": researcher_profile["urn"],
                "fullName": researcher_profile["fullName"],
                "degree": researcher_profile["academicDegree"],
                "positionEN": researcher_profile["positionEN"],
                "unit": researcher_profile["affiliation"],
                "profileUrl": researcher_profile["profileUrl"],
            })
        return True, _fmt_disambiguation(researcher_name, disambiguation_options)

    return False, ""


async def _search_records_by_type(
    record_type: str,
    element_parser_function: Any,
    researcher: str,
    author_id: str | None,
    result_limit: int,
) -> str:
    """
    Generic publication/thesis search by researcher name or author_id.

    For non-phd record types:
      1. Resolve researcher → fetch by author.id, fallback to author.surname.

    For phd record type:
      Author and supervisor searches run ADDITIVELY (not as fallbacks) so that
      a professor's name returns all theses they supervised, and a student's name
      returns their own thesis — both in one call.
      Priority: author.id + supervisor.id when record_id is known (specific);
      author.surname + supervisor.surname when only a name is known.

    Note: The WUT REST API does not support title/keyword search on any record type.
    """
    matched_records: list[dict] = []
    seen_record_ids: set[str] = set()

    def add_unique_record(record_xml_element: ET.Element) -> None:
        parsed_record = element_parser_function(record_xml_element)
        record_id = parsed_record.get("id", "")
        deduplication_key = record_id or parsed_record.get("title", "")
        if deduplication_key and deduplication_key not in seen_record_ids:
            seen_record_ids.add(deduplication_key)
            matched_records.append(parsed_record)

    if researcher or author_id:
        requires_disambiguation, resolved_profile = await _resolve_researcher_to_profile(researcher, author_id)
        if requires_disambiguation:
            return resolved_profile  # type: ignore[return-value]

        _, surname = _split_full_name(
            researcher or (resolved_profile.get("fullName", "") if isinstance(resolved_profile, dict) else "")
        )

        if isinstance(resolved_profile, dict) and resolved_profile:
            researcher_record_id = resolved_profile.get("id", "")

            if record_type == "phd":
                # PhD theses need two simultaneous searches: one for the student
                # (author.id / author.surname) and one for the supervisor
                # (supervisor.id / supervisor.surname).  These are ADDITIVE, not
                # fallbacks — a professor's name should return all theses they
                # supervised, and a student's name should return their own thesis,
                # both in a single call.  asyncio.gather fires both in parallel.
                parallel_search_coroutines = []
                if researcher_record_id:
                    parallel_search_coroutines.append(
                        search_wut_api("phd", "author.id", researcher_record_id, maximum_results=result_limit)
                    )
                    parallel_search_coroutines.append(
                        search_wut_api("phd", "supervisor.id", researcher_record_id, maximum_results=result_limit)
                    )
                elif surname:
                    parallel_search_coroutines.append(
                        search_wut_api("phd", "author.surname", surname, maximum_results=result_limit)
                    )
                    parallel_search_coroutines.append(
                        search_wut_api("phd", "supervisor.surname", surname, maximum_results=result_limit)
                    )
                for search_result_elements in await asyncio.gather(*parallel_search_coroutines):
                    for record_xml_element in search_result_elements:
                        add_unique_record(record_xml_element)
                # If neither ID search yielded anything (e.g. the author is not
                # indexed by WUT numeric ID), fall back to surname-based search.
                if not matched_records and researcher_record_id and surname:
                    for record_xml_element in await search_wut_api("phd", "author.surname", surname, maximum_results=result_limit):
                        add_unique_record(record_xml_element)
                    for record_xml_element in await search_wut_api("phd", "supervisor.surname", surname, maximum_results=result_limit):
                        add_unique_record(record_xml_element)
            else:
                record_elements = []
                if researcher_record_id:
                    record_elements = await search_wut_api(record_type, "author.id", researcher_record_id, maximum_results=result_limit)
                if not record_elements and surname:
                    record_elements = await search_wut_api(record_type, "author.surname", surname, maximum_results=result_limit)
                for record_xml_element in record_elements:
                    add_unique_record(record_xml_element)

        elif not resolved_profile:
            if surname:
                for record_xml_element in await search_wut_api(record_type, "author.surname", surname, maximum_results=result_limit):
                    add_unique_record(record_xml_element)
                if record_type == "phd":
                    for record_xml_element in await search_wut_api(record_type, "supervisor.surname", surname, maximum_results=result_limit):
                        add_unique_record(record_xml_element)

    limited_matched_records = matched_records[:result_limit]
    name_label = researcher or author_id or ""
    n = len(limited_matched_records)
    if record_type == "phd":
        thesis_word = "PhD thesis" if n == 1 else "PhD theses"
        header = f"{n} {thesis_word} found for {name_label}:" if name_label else f"{n} {thesis_word} found:"
        return _fmt_theses_list(limited_matched_records, header)
    pub_word = "publication" if n == 1 else "publications"
    header = f"{n} {pub_word} found for {name_label}:" if name_label else f"{n} {pub_word} found:"
    return _fmt_publications_list(limited_matched_records, header)


# ---------------------------------------------------------------------------
# Section 7b — Output formatters
# ---------------------------------------------------------------------------

def _fmt_profile(profile: dict) -> str:
    """Return a human-readable researcher profile card."""
    lines = ["Researcher found in the WUT OMEGA-PSIR repository:\n"]
    w = 10
    name     = profile.get("fullName", "")
    degree   = profile.get("academicDegree") or profile.get("officialDegree") or ""
    position = profile.get("positionEN", "")
    faculty  = profile.get("affiliation", "")
    hindex   = profile.get("hindex")
    urn      = profile.get("urn", "")
    url      = profile.get("profileUrl", "")
    if name:     lines.append(f"  {'Name':<{w}} {name}")
    if degree:   lines.append(f"  {'Degree':<{w}} {degree}")
    if position: lines.append(f"  {'Position':<{w}} {position}")
    if faculty:  lines.append(f"  {'Faculty':<{w}} {faculty}")
    if hindex is not None:
                 lines.append(f"  {'h-index':<{w}} {hindex}")
    if urn:      lines.append(f"  {'URN':<{w}} {urn}")
    if url:      lines.append(f"  {'Profile':<{w}} [View profile]({url})")
    return "\n".join(lines)


def _fmt_disambiguation(name: str, options: list) -> str:
    """Return a disambiguation prompt listing all matching researchers."""
    n = len(options)
    lines = [f'The name "{name}" is ambiguous — {n} WUT researchers match:\n']
    for opt in options:
        full = opt.get("fullName", "")
        deg  = opt.get("degree", "")
        unit = opt.get("unit", "")
        num  = opt.get("option", "")
        sep    = " · " if deg and unit else ""
        detail = f"{deg}{sep}{unit}".strip()
        lines.append(f"  Option {num} — {full}")
        if detail:
            lines.append(f"             {detail}")
        lines.append("")
    lines.append(
        f"→ Please reply with the option number (1–{n}), then call the tool again\n"
        "  with author_id set to the matching id value."
    )
    return "\n".join(lines)


def _fmt_statistics(profile: dict, stats: dict) -> str:
    """Return a formatted publication analysis block."""
    name   = profile.get("fullName", "Researcher")
    total  = stats.get("totalPublications", 0)
    years  = stats.get("activeYears", [])
    avg    = stats.get("ministryScoreAvg")
    kws    = stats.get("topKeywords", [])
    venues = stats.get("topVenues", [])

    lines = [f"Publication analysis for {name}:\n"]
    lines.append(f"  Total publications: {total}")

    if years:
        span = f"{years[0]} – {years[-1]}"
        lines.append(f"  Active years:       {span}")
    if avg is not None:
        lines.append(f"  Ministry score avg: {avg} points per publication")

    if kws:
        lines.append("\n  Top research themes:")
        half = (len(kws) + 1) // 2
        left_col = kws[:half]
        right_col = kws[half:]
        for i, kw in enumerate(left_col):
            right = right_col[i] if i < len(right_col) else ""
            right_part = f"  {i + half + 1}. {right}" if right else ""
            lines.append(f"    {i + 1}. {kw:<30}{right_part}")

    if venues:
        lines.append("\n  Most frequent venues:")
        for i, v in enumerate(venues, 1):
            lines.append(f"    {i}. {v}")

    return "\n".join(lines)


def _fmt_collaborators(profile: dict, collabs: list) -> str:
    """Return a ranked collaborator list."""
    name  = profile.get("fullName", "Researcher")
    lines = [f"Top collaborators of {name} (by shared publication count):\n"]
    for i, c in enumerate(collabs, 1):
        cname = c.get("name", "")
        count = c.get("sharedPublications", 0)
        work  = "work" if count == 1 else "works"
        lines.append(f"  {i}. {cname}   — {count} co-authored {work}")
    if not collabs:
        lines.append("  No collaborators found.")
    return "\n".join(lines)


def _fmt_comparison(profiles_and_stats: list) -> str:
    """Return a side-by-side researcher comparison."""
    if len(profiles_and_stats) < 2:
        return "Comparison data unavailable."
    pA, sA = profiles_and_stats[0]["profile"], profiles_and_stats[0]["statistics"]
    pB, sB = profiles_and_stats[1]["profile"], profiles_and_stats[1]["statistics"]
    nA = pA.get("fullName", "Researcher A")
    nB = pB.get("fullName", "Researcher B")

    def _first(years: list) -> str: return years[0] if years else "—"
    def _last(years: list)  -> str: return years[-1] if years else "—"

    col = 18
    lines = [f"Comparison of {nA} vs {nB}:\n"]
    lines.append(f"  {'':30}{nA:<{col}}  {nB}")
    lines.append("  " + "─" * 62)
    lines.append(f"  {'Total publications':<30}{sA.get('totalPublications', 0):<{col}}  {sB.get('totalPublications', 0)}")
    avgA = sA.get("ministryScoreAvg") or "—"
    avgB = sB.get("ministryScoreAvg") or "—"
    lines.append(f"  {'Ministry score avg':<30}{str(avgA):<{col}}  {avgB}")
    lines.append(f"  {'Active since':<30}{_first(sA.get('activeYears', [])):<{col}}  {_first(sB.get('activeYears', []))}")
    lines.append(f"  {'Active to':<30}{_last(sA.get('activeYears', [])):<{col}}  {_last(sB.get('activeYears', []))}")

    kwA = ", ".join(sA.get("topKeywords", [])[:5])
    kwB = ", ".join(sB.get("topKeywords", [])[:5])
    if kwA or kwB:
        lines.append("\n  Research themes:")
        if kwA: lines.append(f"    {nA} → {kwA}")
        if kwB: lines.append(f"    {nB} → {kwB}")

    vA = " · ".join(sA.get("topVenues", [])[:3])
    vB = " · ".join(sB.get("topVenues", [])[:3])
    if vA or vB:
        lines.append("\n  Top venues:")
        if vA: lines.append(f"    {nA} → {vA}")
        if vB: lines.append(f"    {nB} → {vB}")

    return "\n".join(lines)



def _tbl(s: object) -> str:
    """Escape pipe characters so a string is safe inside a markdown table cell."""
    return str(s or "").replace("|", "\\|")


def _fmt_publications_list(results: list, header: str) -> str:
    """Format publications grouped by type as markdown tables (one table per type)."""
    if not results:
        return f"{header}\n\nNo publications found."

    groups: dict[str, list[dict]] = {}
    for pub in results:
        label = pub.get("type", "Publication")
        groups.setdefault(label, []).append(pub)

    ordered_keys = [k for k in _PUB_TYPE_ORDER if k in groups]
    ordered_keys += [k for k in groups if k not in _PUB_TYPE_ORDER]

    total = sum(len(v) for v in groups.values())
    lines = [
        f"> **Complete list — {total} publications in {len(ordered_keys)} categories. "
        f"Present every table row exactly as shown.**",
        "",
        header,
        "",
    ]

    for label in ordered_keys:
        pubs = groups[label]
        is_book = label in _BOOK_DISPLAY_TYPES
        lines.append(f"\n### {label} ({len(pubs)})\n")

        if is_book:
            lines.append("| Year | Title | Authors | Publisher | ISBN | DOI |")
            lines.append("|------|-------|---------|-----------|------|-----|")
        else:
            lines.append("| Year | Title | Authors | Venue | DOI |")
            lines.append("|------|-------|---------|-------|-----|")

        for pub in pubs:
            year  = str(pub.get("year") or "—")
            raw_title = pub.get("title") or "(no title)"
            auths = pub.get("authors", [])
            authors_cell = _tbl(", ".join(auths) if isinstance(auths, list) else str(auths))
            doi   = pub.get("doi") or ""
            score = pub.get("score") or ""
            url   = pub.get("url") or ""

            # Title is a clickable link to the WUT record — survives Claude reformatting
            title_cell = f"[{_tbl(raw_title)}]({url})" if url else _tbl(raw_title)

            doi_cell = f"[{doi}](https://doi.org/{doi})" if doi else "—"
            if doi and score:
                doi_cell += f" · {score} pts"

            if is_book:
                pub_raw  = pub.get("publisher") or ""
                pub_cell  = _tbl(pub_raw[:50] + "…" if len(pub_raw) > 50 else pub_raw)
                isbn_cell = _tbl(pub.get("isbn") or "")
                lines.append(f"| {year} | {title_cell} | {authors_cell} | {pub_cell} | {isbn_cell} | {doi_cell} |")
            else:
                venue_raw  = pub.get("journal") or ""
                venue_cell = _tbl(venue_raw[:60] + "…" if len(venue_raw) > 60 else venue_raw)
                lines.append(f"| {year} | {title_cell} | {authors_cell} | {venue_cell} | {doi_cell} |")

    return "\n".join(lines)


def _fmt_theses_list(results: list, header: str) -> str:
    """Format PhD theses as a markdown table."""
    if not results:
        return f"{header}\n\nNo PhD theses found."

    lines = [header, "", f"\n### PhD Theses ({len(results)})\n"]
    lines.append("| Year | Title | PhD Candidate | Supervisor | Keywords |")
    lines.append("|------|-------|---------------|------------|----------|")

    for thesis in results:
        year      = str(thesis.get("year") or "—")
        raw_title = thesis.get("title") or thesis.get("titleEN") or thesis.get("titlePL") or "(no title)"
        author     = _tbl(thesis.get("author") or "")
        supervisor = _tbl(thesis.get("supervisor") or "")
        kws_raw    = thesis.get("keywordsEN") or thesis.get("keywordsPL") or ""
        kws        = _tbl(" · ".join(k.strip() for k in kws_raw.split(",") if k.strip()) if kws_raw else "")
        url        = thesis.get("url") or ""
        title_cell = f"[{_tbl(raw_title)}]({url})" if url else _tbl(raw_title)
        lines.append(f"| {year} | {title_cell} | {author} | {supervisor} | {kws} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 8 — Tool implementations
# ---------------------------------------------------------------------------

async def handle_search_people(
    name: str = "",
    author_id: str | None = None,
    mode: str = "profile",
    compare_with: str | None = None,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
) -> str:
    """
    Researcher lookup with four modes: profile, analyze, collaborators, compare.

    profile     — Return the researcher's structured profile card.
    analyze     — Profile + publication statistics (keywords, venues, scores, years).
    collaborators — List co-authors from the researcher's publications.
    compare     — Side-by-side comparison of two researchers (name vs compare_with).
    """
    mode = (mode or "profile").lower().strip()

    # ---- resolve primary researcher ----
    requires_disambiguation, primary_researcher_profile = await _resolve_researcher_to_profile(name, author_id)
    if requires_disambiguation:
        return primary_researcher_profile  # type: ignore[return-value]
    if not primary_researcher_profile:
        return f"No WUT researcher found for '{name or author_id}'."

    assert isinstance(primary_researcher_profile, dict)

    if mode == "profile":
        return _fmt_profile(primary_researcher_profile)

    # Fetch publications for analysis / collaborators
    primary_researcher_record_id = primary_researcher_profile.get("id", "")
    article_elements: list[ET.Element] = []

    async def fetch_article_elements_for_researcher(
        researcher_record_id: str,
        researcher_full_name: str,
    ) -> list[ET.Element]:
        fetched_article_elements = []
        if researcher_record_id:
            fetched_article_elements = await search_wut_api(
                "article", "author.id", researcher_record_id, maximum_results=100
            )
        if not fetched_article_elements and researcher_full_name:
            _, researcher_surname = _split_full_name(researcher_full_name)
            if researcher_surname:
                fetched_article_elements = await search_wut_api(
                    "article", "author.surname", researcher_surname, maximum_results=100
                )
        return fetched_article_elements

    if mode in ("analyze", "collaborators"):
        article_elements = await fetch_article_elements_for_researcher(
            primary_researcher_record_id,
            primary_researcher_profile.get("fullName", ""),
        )

    if mode == "analyze":
        publications = [parse_article_element(article_element) for article_element in article_elements]

        # Optional year filter
        if year_from or year_to:
            year_filtered_publications = []
            for publication in publications:
                publication_year_string = publication.get("year", "")
                if publication_year_string:
                    try:
                        publication_year_int = int(publication_year_string)
                        if year_from and publication_year_int < year_from:
                            continue
                        if year_to and publication_year_int > year_to:
                            continue
                    except ValueError:
                        pass
                year_filtered_publications.append(publication)
            publications = year_filtered_publications

        publication_statistics = _compute_publication_statistics(publications)
        return _fmt_statistics(primary_researcher_profile, publication_statistics)

    if mode == "collaborators":
        collaborator_frequency_counter: Counter = Counter()
        for article_element in article_elements:
            for author_entry in _parse_author_list(article_element):
                if author_entry["id"] != primary_researcher_record_id and author_entry["name"]:
                    collaborator_frequency_counter[author_entry["name"]] += 1
        top_collaborators = [
            {"name": collaborator_name, "sharedPublications": shared_publication_count}
            for collaborator_name, shared_publication_count in collaborator_frequency_counter.most_common(limit)
        ]
        return _fmt_collaborators(primary_researcher_profile, top_collaborators)

    if mode == "compare":
        if not compare_with:
            return "compare_with parameter required for compare mode."
        secondary_requires_disambiguation, secondary_researcher_profile = await _resolve_researcher_to_profile(compare_with, None)
        if secondary_requires_disambiguation:
            return secondary_researcher_profile  # type: ignore[return-value]
        if not secondary_researcher_profile:
            return f"No WUT researcher found for '{compare_with}'."

        assert isinstance(secondary_researcher_profile, dict)

        secondary_researcher_record_id = secondary_researcher_profile.get("id", "")
        primary_article_elements, secondary_article_elements = await asyncio.gather(
            fetch_article_elements_for_researcher(
                primary_researcher_record_id,
                primary_researcher_profile.get("fullName", ""),
            ),
            fetch_article_elements_for_researcher(
                secondary_researcher_record_id,
                secondary_researcher_profile.get("fullName", ""),
            ),
        )
        primary_publications = [parse_article_element(article_element) for article_element in primary_article_elements]
        secondary_publications = [parse_article_element(article_element) for article_element in secondary_article_elements]

        return _fmt_comparison([
            {
                "profile": primary_researcher_profile,
                "statistics": _compute_publication_statistics(primary_publications),
            },
            {
                "profile": secondary_researcher_profile,
                "statistics": _compute_publication_statistics(secondary_publications),
            },
        ])

    return f"Unknown mode '{mode}'. Valid modes: profile, analyze, collaborators, compare."


async def handle_search_publications(
    researcher: str = "",
    author_id: str | None = None,
    topic: str = "",
    pub_type: str = "all",
    year_from: int | None = None,
    year_to: int | None = None,
    year: int | None = None,
    limit: int = 25,
) -> str:

    limit = min(max(1, limit), 100)
    matched_publications: list[dict] = []
    seen_publication_ids: set[str] = set()

    def add_unique_publication(parsed_publication: dict) -> None:
        deduplication_key = (
            parsed_publication.get("id")
            or parsed_publication.get("doi")
            or parsed_publication.get("title", "")
        )
        if deduplication_key and deduplication_key not in seen_publication_ids:
            seen_publication_ids.add(deduplication_key)
            matched_publications.append(parsed_publication)

    # Determine which record types to search
    requested_publication_type = (pub_type or "all").lower()
    publication_type_to_record_type_map: dict[str, tuple[str, Any]] = {
        "article": ("article", parse_article_element),
        "book": ("book", parse_book_element),
    }

    if requested_publication_type in publication_type_to_record_type_map:
        record_types_to_search = [publication_type_to_record_type_map[requested_publication_type]]
    else:
        record_types_to_search = list(publication_type_to_record_type_map.values())

    # --- topic / keyword search ---
    if topic and not researcher and not author_id:
        topic_stripped = topic.strip()
        for rec_type, parser in record_types_to_search:
            for search_field in ("keywordsEN", "keywordsPL"):
                for elem in await search_wut_api(rec_type, search_field, topic_stripped, limit):
                    add_unique_publication(parser(elem))
                if matched_publications:
                    break
        n = len(matched_publications[:limit])
        pub_word = "publication" if n == 1 else "publications"
        header = f"{n} {pub_word} found for topic \"{topic_stripped}\":"
        return _fmt_publications_list(matched_publications[:limit], header)

    # --- researcher search ---
    if researcher or author_id:
        requires_disambiguation, researcher_profile = await _resolve_researcher_to_profile(researcher, author_id)
        if requires_disambiguation:
            return researcher_profile  # type: ignore[return-value]
        if not researcher_profile:
            return f"No WUT researcher found for '{researcher or author_id}'."

        assert isinstance(researcher_profile, dict)
        researcher_record_id = researcher_profile.get("id", "")
        researcher_full_name = researcher_profile.get("fullName", "")
        _, surname = _split_full_name(researcher or researcher_full_name)

        for record_type, element_parser_function in record_types_to_search:
            if researcher_record_id:
                for record_element in await search_wut_api(record_type, "author.id", researcher_record_id, maximum_results=limit):
                    add_unique_publication(element_parser_function(record_element))
            if not matched_publications and surname:
                for record_element in await search_wut_api(record_type, "author.surname", surname, maximum_results=limit):
                    add_unique_publication(element_parser_function(record_element))

    # --- year-only (no researcher) ---
    elif year or year_from or year_to:
        return (
            "The WUT repository API does not support year-only searches. "
            "Please provide a researcher name or author_id."
        )

    # --- year filter (applied post-fetch) ---
    if year_from or year_to or year:
        year_range_start = year_from or (year if year else 1900)
        year_range_end   = year_to   or (year if year else 2099)
        year_filtered_publications = []
        for publication_record in matched_publications:
            publication_year_string = publication_record.get("year", "")
            if not publication_year_string:
                year_filtered_publications.append(publication_record)
                continue
            try:
                publication_year_int = int(publication_year_string)
                if year_range_start <= publication_year_int <= year_range_end:
                    year_filtered_publications.append(publication_record)
            except ValueError:
                year_filtered_publications.append(publication_record)
        matched_publications = year_filtered_publications

    limited_publications = matched_publications[:limit]
    name_label = researcher or author_id or ""
    year_parts = []
    if year:
        year_parts.append(str(year))
    elif year_from or year_to:
        year_parts.append(f"{year_from or ''}–{year_to or ''}")
    year_suffix = f" ({', '.join(year_parts)})" if year_parts else ""
    n = len(limited_publications)
    pub_word = "publication" if n == 1 else "publications"
    header = f"{n} {pub_word} found for {name_label}{year_suffix}:" if name_label else f"{n} {pub_word} found{year_suffix}:"
    return _fmt_publications_list(limited_publications, header)


async def handle_search_phd_theses(
    researcher: str = "",
    author_id: str | None = None,
    limit: int = 25,
) -> str:
    """
    Search WUT doctoral dissertations (PhD theses).

    Searches by researcher name or author_id. Matches theses where the given name
    is the thesis author OR the supervisor — both search paths are tried.

    Note: The WUT REST API does not support title/keyword search on phd records.
    """
    return await _search_records_by_type(
        record_type="phd",
        element_parser_function=parse_phd_thesis_element,
        researcher=researcher,
        author_id=author_id,
        result_limit=min(max(1, limit), 100),
    )




# ---------------------------------------------------------------------------
# Section 9 — Tool registry
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="search_people",
        description=(
            "Search for WUT researchers and analyse their profiles. "
            "Supports four modes:\n"
            "  profile      — Full profile card (degree, position, h-index, affiliation).\n"
            "  analyze      — Profile + publication statistics (keywords, venues, scores, years).\n"
            "  collaborators — Ranked list of co-authors from the researcher's publications.\n"
            "  compare      — Side-by-side comparison of two researchers.\n\n"
            "Use author_id (numeric WUT id or full URN) to bypass name disambiguation.\n\n"
            "IMPORTANT: Present the tool output exactly as returned — do not reformat, "
            "summarise, or omit any fields. Always show the Profile URL and URN."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Full or partial researcher name (e.g. 'Jan Kowalski').",
                },
                "author_id": {
                    "type": "string",
                    "description": (
                        "WUT numeric ID or URN (urn:pw-repo:WUT…). "
                        "Skips disambiguation when provided."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["profile", "analyze", "collaborators", "compare"],
                    "default": "profile",
                    "description": "Analysis mode.",
                },
                "compare_with": {
                    "type": "string",
                    "description": "Second researcher name — required for compare mode.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max collaborators to return (collaborators mode).",
                },
                "year_from": {
                    "type": "integer",
                    "description": "Start year filter for analyze mode.",
                },
                "year_to": {
                    "type": "integer",
                    "description": "End year filter for analyze mode.",
                },
            },
        },
    ),
    Tool(
        name="search_publications",
        description=(
            "Search WUT publications: journal articles and books.\n\n"
            "Two search modes:\n"
            "  • By researcher — provide researcher name or author_id.\n"
            "  • By topic/keyword — provide topic (e.g. 'machine learning', 'neural networks');\n"
            "    searches the keywordsEN and keywordsPL fields across all publications.\n\n"
            "Optionally filter by pub_type ('article', 'book', or 'all') and year range.\n\n"
            "CRITICAL: The tool returns complete markdown tables. You MUST render every "
            "table row exactly as returned — do NOT summarize, do NOT write 'highlights', "
            "do NOT reduce the number of rows. Every publication has its own row. "
            "Copy the tables verbatim; they ARE the answer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "researcher": {
                    "type": "string",
                    "description": "Researcher full name to filter publications.",
                },
                "author_id": {
                    "type": "string",
                    "description": "WUT numeric ID or URN for the researcher.",
                },
                "topic": {
                    "type": "string",
                    "description": "Research topic or keyword (e.g. 'machine learning'). Used when no researcher is specified.",
                },
                "pub_type": {
                    "type": "string",
                    "enum": ["all", "article", "book"],
                    "default": "all",
                    "description": "Publication type filter.",
                },
                "year_from": {
                    "type": "integer",
                    "description": "Filter: published from this year (inclusive).",
                },
                "year_to": {
                    "type": "integer",
                    "description": "Filter: published up to this year (inclusive).",
                },
                "year": {
                    "type": "integer",
                    "description": "Exact publication year filter.",
                },
                "limit": {
                    "type": "integer",
                    "default": 25,
                    "description": "Maximum number of results to return (max 100).",
                },
            },
        },
    ),
    Tool(
        name="search_phd_theses",
        description=(
            "Search WUT doctoral dissertations (PhD theses) in the OMEGA-PSIR repository.\n\n"
            "Search by researcher name or author_id. Automatically tries both the thesis author\n"
            "and the supervisor — so a supervisor's name will return their supervised theses.\n"
            "Returns: title (EN/PL), author, supervisor, year, defence date, abstracts, keywords.\n\n"
            "IMPORTANT: Present the tool output exactly as returned — do not reformat, "
            "summarise, or omit any fields. Always show the Record URL for every thesis entry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "researcher": {
                    "type": "string",
                    "description": "Thesis author or supervisor name to search by.",
                },
                "author_id": {
                    "type": "string",
                    "description": "WUT numeric ID or URN for the researcher.",
                },
                "limit": {
                    "type": "integer",
                    "default": 25,
                    "description": "Maximum number of results to return (max 100).",
                },
            },
        },
    ),
]

_TOOL_NAME_TO_HANDLER_MAP: dict[str, Any] = {
    "search_people":       handle_search_people,
    "search_publications": handle_search_publications,
    "search_phd_theses":   handle_search_phd_theses,
}


# ---------------------------------------------------------------------------
# Section 10 — MCP handlers
# ---------------------------------------------------------------------------

@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """Return the list of available MCP tools."""
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    Dispatch an MCP tool call to the appropriate implementation function.

    Unrecognised tool names return a JSON error payload rather than raising.
    """
    tool_handler_function = _TOOL_NAME_TO_HANDLER_MAP.get(name)
    if tool_handler_function is None:
        error_json = json.dumps(
            {"error": f"Unknown tool '{name}'. Available: {list(_TOOL_NAME_TO_HANDLER_MAP.keys())}"},
            ensure_ascii=False,
        )
        return [TextContent(type="text", text=error_json)]

    try:
        tool_execution_result = await tool_handler_function(**(arguments or {}))
    except Exception as exc:  # noqa: BLE001
        error_json = json.dumps(
            {"error": f"Tool '{name}' raised an exception: {type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
        return [TextContent(type="text", text=error_json)]

    if not isinstance(tool_execution_result, str):
        tool_execution_result = json.dumps(tool_execution_result, ensure_ascii=False, indent=2)

    return [TextContent(type="text", text=tool_execution_result)]


# ---------------------------------------------------------------------------
# Section 11 — Dual transport
# ---------------------------------------------------------------------------

def _run_stdio_transport() -> None:
    """Run the MCP server over stdio (default mode for Claude Desktop / CLI)."""
    from mcp.server.stdio import stdio_server

    async def run_async_stdio_server() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    asyncio.run(run_async_stdio_server())


def _run_sse_transport(http_port_number: int) -> None:
    """
    Run the MCP server over HTTP with both SSE and Streamable HTTP transports.

    Exposes:
      GET  /sse          — SSE transport (legacy MCP clients)
      POST /messages     — Streamable HTTP transport
      GET  /health       — Liveness probe returning {"status": "ok"}
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    sse_server_transport = SseServerTransport("/messages")

    async def handle_sse_connection(request: Request) -> None:
        async with sse_server_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as sse_streams:
            await mcp_server.run(
                sse_streams[0],
                sse_streams[1],
                mcp_server.create_initialization_options(),
            )

    async def handle_health_check(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    starlette_web_application = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse_connection),
            Mount("/messages", app=sse_server_transport.handle_post_message),
            Route("/health", endpoint=handle_health_check),
        ]
    )

    uvicorn.run(starlette_web_application, host="0.0.0.0", port=http_port_number)


def main() -> None:
    """
    Entry point: select transport based on environment variables.

    If PORT or WEBSITES_PORT is set (Azure App Service convention), start in SSE mode.
    Otherwise, start in stdio mode for local MCP clients.
    """
    port_environment_variable = os.environ.get("PORT") or os.environ.get("WEBSITES_PORT")
    if port_environment_variable:
        try:
            http_port_number = int(port_environment_variable)
        except ValueError:
            http_port_number = 8000
        _run_sse_transport(http_port_number)
    else:
        _run_stdio_transport()


if __name__ == "__main__":
    main()
