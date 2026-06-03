# WUT OMEGA-PSIR MCP Server — Deep Implementation Reference

This document explains every function and MCP tool in `server.py` in full detail: what it does, how it works internally, what data it receives, what it returns, and why it was implemented the way it was.

---

## Table of Contents

1. [Diacritics Helpers](#1-diacritics-helpers)
2. [XML Utilities](#2-xml-utilities)
3. [Entity Parsers](#3-entity-parsers)
4. [HTTP Client and Cache](#4-http-client-and-cache)
5. [Name Helpers](#5-name-helpers)
6. [Researcher Resolution Helpers](#6-researcher-resolution-helpers)
7. [MCP Tools — Detailed](#7-mcp-tools--detailed)
   - [search_people](#search_people)
   - [search_publications](#search_publications)
   - [search_phd_theses](#search_phd_theses)
8. [MCP Protocol Handlers](#8-mcp-protocol-handlers)
9. [Transport Layer](#9-transport-layer)
10. [Appendix — Full Call Graph](#appendix--full-call-graph)

----------------------------------------------------------------------------------------------------------------------------------------------------
## 1. Diacritics Helpers
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_DIACRITICS_TRANSLATION_TABLE`

```python
_DIACRITICS_TRANSLATION_TABLE = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)
```

A pre-built Python translation table created once at module load time. Maps each Polish letter to its nearest ASCII equivalent character-by-character. `str.maketrans` takes two equal-length strings and maps each character in the first to the character at the same position in the second.

This table is used by `strip_diacritics` for comparison purposes only — never for building API queries.

---

### `strip_diacritics(text_with_polish_characters: str) -> str`

**Input:** Any string, potentially containing Polish diacritical characters.  
**Output:** The same string with all Polish characters replaced by their ASCII equivalents.

**How it works:**
Calls Python's built-in `str.translate()` with the pre-built `_DIACRITICS_TRANSLATION_TABLE`. This is an O(n) operation on the string length and is significantly faster than regex-based substitution.

**Where it is used:**
Exclusively in `_filter_elements_by_first_name` — when comparing an API-returned `<name>` field (which may contain accented characters) against a user-supplied prefix (which may not). Both sides are stripped before comparison so `"Jan"` matches `"Ján"` and vice versa.

**What it does NOT do:**
It does not normalise queries sent to the WUT API. The API requires exact accent matching, so stripping before querying would break searches for correctly accented names. See `_generate_diacritics_variants` for the correct approach.

---

### `_generate_diacritics_variants(surname_or_name: str) -> list[str]`

**Input:** A single name or surname string, typically as typed by a user without Polish characters.  
**Output:** A list where index 0 is always the original unchanged input, and subsequent elements are variants produced by substituting ASCII characters with their Polish counterparts.

**How it works — step by step:**

```python
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
```

1. Initialises `seen_variants` as a set containing the original to prevent duplicates.
2. Iterates through 9 substitution pairs. Note that `z` appears twice (`ż` and `ź`) — these are two separate Polish letters sharing the same ASCII base.
3. For each pair, replaces **all** occurrences of the ASCII character in the **original** string (not in previous variants). This avoids compounding substitutions.
4. Adds the variant only if it has not been seen before.

**Example — `"Rybinski"`:**
```
[0] "Rybinski"   ← original
[1] "Rybiński"   ← n → ń
```
The variants are then tried one by one in `search_wut_api` until one returns results.

**Why substitutions are applied to the original only:**
Compounding variants (e.g. first `o→ó` then `l→ł` on the result) would produce an exponential number of combinations. Since the API returns all records matching a surname, we only need to find the correct accented form — not enumerate every combination.

**Where it is used:**
Only in `search_wut_api`, and only when the initial unaccented query returned no results AND the search field is a name/surname field.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 2. XML Utilities
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_XML_NAMESPACE_PATTERN`

```python
_XML_NAMESPACE_PATTERN = re.compile(r"\{[^}]+\}")
```

A compiled regex that matches Clark-notation namespace prefixes — the `{uri}` portion of tags like `{http://repo.pw.edu.pl/ns/publication}article`. Compiled once at module load for efficiency.

---

### `_strip_xml_namespace_prefixes(xml_root_element: ET.Element) -> None`

**Input:** The root `ET.Element` of a parsed XML tree.  
**Output:** None — modifies the tree in-place.

**How it works:**

```python
for node in xml_root_element.iter():
    node.tag = _XML_NAMESPACE_PATTERN.sub("", node.tag)
    node.attrib = {_XML_NAMESPACE_PATTERN.sub("", k): v for k, v in node.attrib.items()}
```

Uses `element.iter()` to walk the entire tree depth-first. For each node:
- Strips the `{uri}` prefix from the tag name string.
- Rebuilds the attributes dict with stripped keys (values are unchanged).

**Why this is necessary:**
Python's `xml.etree.ElementTree` preserves namespace prefixes in Clark notation in all tag names. Without stripping, `element.find("title")` would never match `{http://...}title`. This function is called immediately after parsing every HTTP response so all downstream code uses bare tag names.

**Performance note:**
The operation is O(n) on the number of nodes. For typical WUT API responses (tens of records per page, each with ~30 fields), this is negligible.

---

### `_parse_xml_bytes_to_element(response_bytes: bytes) -> ET.Element`

**Input:** Raw XML bytes from an HTTP response body.  
**Output:** The root `ET.Element` with namespaces stripped.

**How it works:**

```python
xml_root_element = ET.fromstring(response_bytes)
_strip_xml_namespace_prefixes(xml_root_element)
return xml_root_element
```

`ET.fromstring` parses the bytes directly (no intermediate string conversion needed — ElementTree handles encoding). `_strip_xml_namespace_prefixes` is applied immediately so the returned element is always namespace-clean.

**Called by:** `_fetch_api_page` on every successful HTTP response, and also when serving a cached response (re-parses the cached bytes to return a fresh element tree, avoiding shared mutable state).

---

### `_get_child_text(parent_element, *child_tags, default="") -> str`

**Input:**
- `parent_element` — any `ET.Element`
- `*child_tags` — zero or more tag name strings representing a path to walk
- `default` — the value to return if any step fails or the final element has no text

**Output:** The stripped text content of the final element, or `default`.

**How it works:**

```python
current_node = parent_element
for child_tag in child_tags:
    child_element = current_node.find(child_tag)
    if child_element is None:
        return default
    current_node = child_element
return (current_node.text or "").strip() or default
```

Walks the chain of tags. If `.find()` returns `None` at any step, returns `default` immediately without continuing. The final `or default` handles the case where `.text` is an empty string or whitespace-only.

**Why variadic args instead of a path string:**
A variadic API (`*child_tags`) lets callers write natural Python rather than XPath strings, and avoids the overhead of string splitting:

```python
# Clear — three levels of nesting, no XPath syntax
_get_child_text(article, "journalissue", "journalseries", "title")

# Less clear — XPath string, raises on some edge cases
article.findtext("journalissue/journalseries/title") or ""
```

---

### `_extract_record_id(record_element: ET.Element) -> str`

**Input:** Any XML record element (author, article, book, phd).  
**Output:** The WUT numeric record ID string, or `""` if not found.

**How it works:**

```python
attribute_id_value = record_element.get("id", "").strip()
if attribute_id_value:
    return attribute_id_value
id_child_element = record_element.find("id")
if id_child_element is not None and id_child_element.text:
    return id_child_element.text.strip()
return ""
```

Two strategies in priority order:
1. `id` attribute on the element itself (e.g. `<author id="WUT123">`)
2. `<id>` child element containing the ID as text

**Why two strategies:** The WUT API is inconsistent across record types. Author records typically use the attribute form; publication records sometimes use child element form.

---

### `_get_all_direct_children(parent_element, child_tag) -> list[ET.Element]`

**Input:** An element and a tag name string.  
**Output:** List of all direct children with that tag.

A one-line wrapper: `return parent_element.findall(child_tag)`.

`findall` without a path prefix returns only direct children (not descendants), which is the behaviour needed when iterating all `<author>` elements on a publication without accidentally picking up nested author references in sub-elements.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 3. Entity Parsers
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_parse_author_list(publication_element: ET.Element) -> list[dict]`

**Input:** A publication XML element (article, book, or phd).  
**Output:** A list of dicts, one per `<author>` direct child.

**Each dict contains:**
```python
{
    "name": str,        # display name
    "id": str,          # WUT numeric record ID
    "profileUrl": str,  # full URL to profile card
}
```

**How it works:**

```python
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
```

The name resolution tries `presentedFullName` first (a pre-formatted display string provided by the API), then falls back to concatenating `name` + `surname` fields. The `profileUrl` is only built when an ID is available.

**Why `_get_all_direct_children` and not `findall(".//author")`:**
`./author` (default findall) returns only direct children, which is correct. `".//author"` would return descendants too, potentially picking up nested author references inside sub-elements.

**Used by:**
- `_get_author_display_names` — for publication `"authors"` field
- `handle_search_people` collaborators mode — needs both `name` and `id` for deduplication

---

### `_get_author_display_names(publication_element: ET.Element) -> list[str]`

**Input:** A publication XML element.  
**Output:** A flat list of author display name strings.

A thin wrapper over `_parse_author_list` that extracts only the `name` field. Filters out empty strings (authors with no resolvable name). Used when building the `"authors"` array in parsed publication dicts.

---

### `parse_researcher_element(author_xml_element: ET.Element) -> dict`

**Input:** A WUT `<author>` XML element from the API.  
**Output:** A structured researcher profile dict.

**Full output shape:**
```python
{
    "id": str,              # numeric WUT ID
    "urn": str,             # urn:pw-repo:WUT{id}
    "fullName": str,        # display name
    "firstName": str,       # given name(s)
    "lastName": str,        # family name
    "academicDegree": str,  # e.g. "dr inż."
    "officialDegree": str,  # from <officialAcademicDegree><value>
    "positionEN": str,      # English job title
    "positionPL": str,      # Polish job title
    "affiliation": str,     # department/faculty
    "status": str,          # active/inactive/etc
    "researchArea": str,    # subject area
    "hindex": int | None,   # Hirsch index
    "profileUrl": str,      # https://repo.pw.edu.pl/info/card/WUT{id}
}
```

#### H-index extraction

```python
raw_hindex_text = _get_child_text(author_xml_element, "authorprofile", "hindex")
try:
    hindex = int(raw_hindex_text) if raw_hindex_text else None
except ValueError:
    hindex = None
```

The h-index is nested two levels deep (`<authorprofile><hindex>`). The `try/except` guards against non-numeric values (the API occasionally returns placeholder strings).

---

#### Affiliation extraction

```python
affiliation_text = _get_child_text(author_xml_element, "affiliation")
if not affiliation_text:
    affiliation_element = author_xml_element.find("affiliation")
    if affiliation_element is not None:
        affiliation_text = " | ".join(
            text_chunk.strip()
            for text_chunk in affiliation_element.itertext()
            if text_chunk.strip()
        )
```

Some `<affiliation>` elements contain plain text; others contain nested sub-elements (faculty > department > unit). `_get_child_text` only retrieves direct `.text` content. When that is empty, `itertext()` walks all descendant text nodes and joins them with ` | `.

---

#### API field misspelling

The WUT API uses `possitionEN` and `possitionPL` (double `s`). The parsed dict corrects this to `positionEN` / `positionPL` in the output key names while still using the misspelled names in the XML lookups.

---

### `_extract_publication_year(record_element: ET.Element) -> str`

**Input:** Any publication XML element (article, book, phd).  
**Output:** A 4-digit year string (e.g. `"2021"`), or `""` if no year can be found.

This is the most complex extraction function. A 4-digit year could theoretically appear in dozens of XML fields, but many of those fields contain administrative timestamps that should not be mistaken for publication years. Four strategies are tried in order.

---

#### Step 1 — Direct children

Covers most books and simple records.

```python
_DIRECT_YEAR_FIELD_NAMES = (
    "year", "issueDate", "publishDate", "publicationDate",
    "publicationYear", "datePublished", "beginDate", "defenseDate",
)
for year_field_name in _DIRECT_YEAR_FIELD_NAMES:
    year_candidate_element = record_element.find(year_field_name)
    if year_candidate_element is not None and year_candidate_element.text:
        year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(year_candidate_element.text)
        if year_regex_match:
            return year_regex_match.group(0)
```

Uses a regex (`\b(19|20)\d{2}\b`) rather than string slicing because date fields may contain full ISO dates (`"2019-03-15"`), year + text (`"2019 r."`), or just a year (`"2019"`).

---

#### Step 2 — Named nested paths

Covers journal articles and conference papers.

```python
_NESTED_YEAR_ELEMENT_PATHS = (
    ("journalissue", "issueDate"),
    ("book", "issueDate"),     # conference proceedings stored as article + book
    ("book", "year"),
    ("book", "date"),
    ("issue", "year"),
)
```

Tries each two-level path in order. The `book/issueDate` path handles the unusual case where a conference article stores its proceedings date inside a nested `<book>` child.

---

#### Step 3 — Recursive search within `<journalissue>`

Handles conference/proceedings papers where the date is buried inside a `<conference>` or `<journalseries>` sub-element.

```python
journal_issue_element = record_element.find("journalissue")
if journal_issue_element is not None:
    for year_field_name in _JOURNAL_ISSUE_RECURSIVE_YEAR_FIELDS:
        year_candidate_element = journal_issue_element.find(".//" + year_field_name)
```

The `".//fieldname"` XPath finds the first matching descendant at any depth.

---

#### Step 4 — Last-resort descendant scan with blocklist

```python
_ADMINISTRATIVE_TAGS_TO_SKIP = frozenset({
    "metaData", "lastModified", "lastModifiedBy", "created", "meta_datestamp",
    "scoreDate", "evaluationDate", "verificationDate", "disciplinesApprovalDate",
    "responseDate", "lastTransferDate",
    "legalBasis",    # e.g. "Rozporządzenie MNiSW z dnia 22 lutego 2019 r."
    "rulesetName",   # e.g. "reguly_2017_v1f"
    "id",            # UUID fragments like "WEITI-8ad8b193-1981-4d19-..."
})
for descendant_node in record_element.iter():
    if descendant_node.tag in _ADMINISTRATIVE_TAGS_TO_SKIP:
        continue
    if descendant_node.text:
        year_regex_match = _FOUR_DIGIT_YEAR_PATTERN.search(descendant_node.text)
        if year_regex_match:
            return year_regex_match.group(0)
```

The blocklist was built from live API testing. Key discoveries:
- `<legalBasis>` contains Polish regulation year strings like `"Rozporządzenie MNiSW z dnia 22 lutego 2019 r."` — the `2019` would be a false positive.
- `<rulesetName>` contains scoring rule version strings like `"reguly_2017_v1f"` — `2017` would be a false positive.
- `<id>` fields contain UUID-like strings with year-looking hex segments, e.g. `"WEITI-8ad8b193-1981-4d19-..."` — `1981` is a hex fragment, not a year.
- All `*Date` admin fields contain the current year (when the record was last modified), not the publication year.

---

### `parse_article_element(article_xml_element: ET.Element) -> dict`

**Input:** A WUT `<article>` or `<publication>` XML element.  
**Output:** A structured article dict.

#### Journal name resolution

Four fallbacks tried in order:

```python
journal_name = (
    _get_child_text(article_xml_element, "journalissue", "journalseries", "title")
    or _get_child_text(article_xml_element, "journalissue", "journalseries", "name")
    or _get_child_text(article_xml_element, "journalissue", "title")
    or _get_child_text(article_xml_element, "journal")
)
```

| Path | Covers |
|---|---|
| `journalissue/journalseries/title` | Standard journal article with a series record |
| `journalissue/journalseries/name` | Same structure but `name` used instead of `title` |
| `journalissue/title` | Journal article without a series sub-element |
| `journal` | Legacy or simplified records with a flat `<journal>` field |

---

#### Output shape

```python
{
    "type": "article",
    "id": str,
    "urn": str,
    "title": str,
    "authors": list[str],
    "year": str,
    "doi": str,
    "journal": str,
    "collation": str,     # page range, e.g. "pp. 123-135"
    "score": str,         # Polish ministry score, e.g. "70"
    "abstractEN": str,
    "keywordsEN": str,
    "keywordsPL": str,
    "url": str,           # https://repo.pw.edu.pl/info/r/WUT{id}
}
```

---

### `parse_book_element(book_xml_element: ET.Element) -> dict`

**Input:** A WUT `<book>` or `<bookchapter>` XML element.  
**Output:** A structured book dict.

#### Publisher name resolution

```python
publisher_name = (
    _get_child_text(book_xml_element, "publisher")
    or _get_child_text(book_xml_element, "publisherInstitution", "name")
)
```

#### `type` field

Taken from `<bookType>` if present (returns API values like `"MONOGRAFIA"`, `"ROZDZIAL_W_MONOGRAFII"`). Falls back to the generic string `"book"` if absent.

---

#### Output shape

```python
{
    "type": str,          # "MONOGRAFIA", "ROZDZIAL_W_MONOGRAFII", or "book"
    "id": str,
    "urn": str,
    "title": str,
    "authors": list[str],
    "year": str,
    "doi": str,
    "isbn": str,
    "publisher": str,
    "collation": str,
    "abstractEN": str,
    "keywordsEN": str,
    "keywordsPL": str,
    "url": str,
}
```

---

### `parse_phd_thesis_element(phd_xml_element: ET.Element) -> dict`

**Input:** A WUT `<phd>` doctoral dissertation XML element.  
**Output:** A structured PhD thesis dict.

#### Title preference chain

```python
thesis_title = (
    _get_child_text(phd_xml_element, "titleEN")
    or _get_child_text(phd_xml_element, "titlePL")
    or _get_child_text(phd_xml_element, "title")
)
```

`titleEN` is preferred for international usability. Falls through to Polish then generic. All three are also returned as separate fields so the caller can use whichever they need.

---

#### Author extraction from nested element

```python
author_element = phd_xml_element.find("author")
if author_element is not None:
    thesis_author_name = (
        _get_child_text(author_element, "presentedFullName")
        or f"{_get_child_text(author_element, 'name')} {_get_child_text(author_element, 'surname')}".strip()
    )
```

Unlike publication articles where `<author>` contains plain text, PhD `<author>` elements are nested records with their own child elements. Attempting `_get_child_text(phd, "author")` would return the element's direct `.text` (empty), not the nested name.

---

#### Supervisor with promoter fallback

```python
supervisor_element = phd_xml_element.find("supervisor")
if supervisor_element is None:
    supervisor_element = phd_xml_element.find("promoter")
```

Older records in the database use `<promoter>` (the Polish term for thesis supervisor) while newer records use `<supervisor>`. Both are tried so no supervisor is missed.

---

#### Output shape

```python
{
    "type": "phd_thesis",
    "id": str,
    "urn": str,
    "titleEN": str,
    "titlePL": str,
    "title": str,           # preferred title (EN → PL → generic)
    "author": str,
    "supervisor": str,
    "year": str,
    "defenseDate": str,
    "abstractEN": str,
    "abstractPL": str,
    "keywordsEN": str,
    "keywordsPL": str,
    "url": str,
}
```

----------------------------------------------------------------------------------------------------------------------------------------------------
## 4. HTTP Client and Cache
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_HTTP_CLIENT` — singleton `httpx.AsyncClient`

```python
_HTTP_CLIENT = httpx.AsyncClient(
    headers={"Accept": "application/xml", "User-Agent": "OmegaPSIR-MCP/3.0"},
    timeout=30.0,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)
```

A single shared async HTTP client created at module load time. Connection pooling means repeated requests to the same host (all WUT API calls) reuse existing TCP connections, reducing latency and load.

| Setting | Value | Reason |
|---|---|---|
| timeout | 30 s | WUT API is occasionally slow on cold start; 30 s avoids false timeouts |
| max_connections | 10 | Prevents overwhelming the WUT API server during heavy parallel usage |
| max_keepalive_connections | 5 | Retains 5 warm connections for reuse |

---

### `_RESPONSE_CACHE` — in-memory URL cache

```python
_RESPONSE_CACHE: dict[str, tuple[float, bytes | None]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes
```

An in-memory dict keyed by the full request URL. Each entry stores:
- `expiry_timestamp` — a `time.monotonic()` value representing when this cache entry expires
- `bytes | None` — the raw response bytes, or `None` to represent a confirmed empty/error response

**Why cache `None`:**
Without it, a query that returns 404 (e.g. an invalid record type) would hammer the API on every call. Caching `None` for the same TTL means dead endpoints are tried at most once per 5 minutes.

**Why `time.monotonic()`:**
System clock (`time.time()`) can go backwards (NTP adjustments, daylight saving). `monotonic()` is guaranteed to never decrease, making it safe for TTL comparisons.

**TTL = 300 s:**
Matches Claude's prompt-cache TTL window. Within a single Claude session, repeated tool calls for the same query return cached results instantly without making new HTTP requests.

---

### `_fetch_api_page(record_type, search_field, search_value, pagination_offset, page_size) -> ET.Element | None`

**Input:**
- `record_type` — `"author"`, `"article"`, `"book"`, or `"phd"`
- `search_field` — the API field name to search on (e.g. `"surname"`, `"author.id"`)
- `search_value` — the value to match (e.g. `"Kowalski"`)
- `pagination_offset` — how many records to skip (0-indexed)
- `page_size` — how many records to return (max 25)

**Output:** Parsed and namespace-stripped `ET.Element` root, or `None` on error/empty.

#### URL construction

```python
url_encoded_field_value = quote(search_value, safe="")
request_url = (
    f"{WUT_API_BASE_URL}/accesspoint/search/{record_type}"
    f"/@{search_field}='{url_encoded_field_value}'/{pagination_offset}/{page_size}"
)
```

`quote(value, safe="")` percent-encodes the entire value including forward slashes. This is critical for Polish characters (ą, ó, etc.) and for values that happen to contain `/`.

---

#### Cache check

```python
current_monotonic_time = time.monotonic()
if request_url in _RESPONSE_CACHE:
    cache_expiry_time, cached_response_bytes = _RESPONSE_CACHE[request_url]
    if current_monotonic_time < cache_expiry_time:
        if cached_response_bytes is None:
            return None
        return _parse_xml_bytes_to_element(cached_response_bytes)
```

Cache is checked before any network call. If the entry is within TTL and holds `None`, returns `None` immediately. If it holds bytes, re-parses and returns a fresh element tree (never shares the same mutable tree object across calls).

---

#### HTTP call and error handling

```python
http_response = await _HTTP_CLIENT.get(request_url)
if http_response.status_code == 404:
    _RESPONSE_CACHE[request_url] = (current_monotonic_time + _CACHE_TTL_SECONDS, None)
    return None
http_response.raise_for_status()
response_bytes = http_response.content
_RESPONSE_CACHE[request_url] = (current_monotonic_time + _CACHE_TTL_SECONDS, response_bytes)
return _parse_xml_bytes_to_element(response_bytes)
```

404 is handled explicitly (before `raise_for_status`) and cached as `None`. Both `httpx.HTTPError` (4xx/5xx) and `ET.ParseError` (the API serving an HTML error page instead of XML for unsupported record types or very high pagination offsets) are caught in the outer `except` and cached as `None`.

---

### `_fetch_all_api_pages(record_type, search_field, search_value, maximum_results) -> list[ET.Element]`

**Input:** Same as `_fetch_api_page` minus offset and page_size; plus `maximum_results`.  
**Output:** A flat list of all record `ET.Element` objects collected across all pages.

**How it works:**

```python
accumulated_record_elements: list[ET.Element] = []
pagination_offset = 0
while len(accumulated_record_elements) < maximum_results:
    page_size = min(MAX_RECORDS_PER_PAGE, maximum_results - len(accumulated_record_elements))
    page_root_element = await _fetch_api_page(
        record_type, search_field, search_value, pagination_offset, page_size
    )
    if page_root_element is None:
        break
    page_record_elements = [
        record_element for record_element in page_root_element
        if record_element.tag not in ("status", "count", "total", "offset", "limit")
    ]
    if not page_record_elements:
        break
    accumulated_record_elements.extend(page_record_elements)
    if len(page_record_elements) < page_size:
        break      # last page — fewer records than requested
    pagination_offset += page_size
return accumulated_record_elements
```

#### Stopping conditions

| Condition | Meaning |
|---|---|
| `page_root_element is None` | Error or 404 response |
| `page_record_elements` is empty | API returned a valid response but only wrapper elements |
| `len(page_record_elements) < page_size` | Partial page — no more records exist |
| `len(accumulated) >= maximum_results` | Loop exit condition |

**Page size calculation:**
`min(MAX_RECORDS_PER_PAGE, maximum_results - len(accumulated))` ensures the final request asks for exactly the remaining number needed, never exceeding the API's 25-record limit.

**Wrapper element filtering:**
The API sometimes includes `<status>`, `<count>`, `<total>`, `<offset>`, `<limit>` elements as siblings of the actual records inside the `<collection>` root. These are skipped.

---

### `search_wut_api(record_type, search_field, search_value, maximum_results=25) -> list[ET.Element]`

**Input:** Search parameters.  
**Output:** List of record `ET.Element` objects, possibly empty.

The high-level search function with three automatic retry/fallback stages.

#### Stage 1 — Initial attempt

```python
record_elements = await _fetch_all_api_pages(record_type, search_field, search_value, maximum_results)
```

---

#### Stage 2 — Cold-start retry (500 ms wait)

```python
if not record_elements:
    await asyncio.sleep(0.5)
    record_elements = await _fetch_all_api_pages(...)
```

The WUT API is deployed on infrastructure that suspends when idle. The first request after a period of inactivity may return an empty response even for valid queries. A single 500 ms pause is enough for the server to initialise.

---

#### Stage 3 — Diacritics fallback (300 ms between variants)

```python
if not record_elements and search_field in (
    "surname", "name", "fullName", "presentedFullName",
    "author.surname", "supervisor.surname",
):
    for diacritics_variant in _generate_diacritics_variants(search_value)[1:]:
        await asyncio.sleep(0.3)
        record_elements = await _fetch_all_api_pages(
            record_type, search_field, diacritics_variant, maximum_results
        )
        if record_elements:
            break
```

Only triggered for name/surname fields (not ID fields or title fields). Tries variants one at a time with 300 ms gaps to avoid overwhelming the API. Stops at the first successful variant.

**Why 0.3 s gaps (not simultaneous):**
Firing all variants simultaneously would multiply the load on the API. Sequential with short gaps is sufficient since we stop as soon as one works.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 5. Name Helpers
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_split_full_name(full_name: str) -> tuple[str, str]`

**Input:** A full name string as provided by the user.  
**Output:** `(first_names, surname)` tuple.

**How it works:**
```python
name_parts = full_name.strip().split()
if not name_parts:
    return ("", "")
if len(name_parts) == 1:
    return ("", name_parts[0])
return (" ".join(name_parts[:-1]), name_parts[-1])
```

Splits on any whitespace (handles multiple spaces, tabs). Treats the **last token** as the surname — handles compound first names like `"Jan Andrzej"`. A single token is returned as a surname with empty first name.

**Why last-token-is-surname:**
Polish naming conventions put the surname last. This heuristic covers the vast majority of cases. Edge cases (e.g. `"de la Cruz"` style surnames) are uncommon in the WUT researcher database.

**Used by:** `_resolve_researcher_to_profile`, `handle_search_publications`, `handle_search_people`, and `_search_records_by_type` — anywhere a full name needs to be broken down for the `surname` API field.

---

### `_filter_elements_by_first_name(researcher_elements, first_name_prefix) -> list[ET.Element]`

**Input:**
- `researcher_elements` — list of `<author>` XML elements returned by a surname search
- `first_name_prefix` — the first name (or partial first name) to filter by

**Output:** A filtered subset of `researcher_elements`, or the original list unchanged if filtering would return nothing.

**How it works:**
```python
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
```

**Why `startswith` and not equality:**
Users may type a shortened first name (`"Jan"` for `"Janusz"`) or just the first letter. `startswith` allows partial matching.

**Why the graceful degradation:**
If a user types `"J. Kowalski"`, the prefix `"J."` won't match `"Jan"` after `strip_diacritics`. Without the fallback, this would return an empty list and the tool would report no researcher found — worse than returning all Kowalskis and letting the user see the disambiguation list.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 6. Researcher Resolution Helpers
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_build_record_urn(record_id: str) -> str`

**Input:** A WUT numeric record ID or URN string.  
**Output:** A properly formatted URN string.

```python
if not record_id:
    return ""
if record_id.startswith("urn:"):
    return record_id
return f"urn:pw-repo:WUT{record_id}"
```

Idempotent: calling it on an already-formed URN returns it unchanged. Calling it on an empty string returns `""`. Callers can invoke it defensively without conditional checks.

---

### `_compute_publication_statistics(publications: list[dict]) -> dict`

**Input:** A list of parsed publication dicts (from `parse_article_element` or `parse_book_element`).  
**Output:** An aggregated statistics dict.

**Full output shape:**
```python
{
    "totalPublications": int,
    "topKeywords": list[str],       # up to 8, most frequent first
    "topVenues": list[str],         # up to 5, most frequent first
    "ministryScoreAvg": float | None,
    "activeYears": list[str],       # sorted ascending
}
```

**How it works — single pass over all publications:**

```python
year_frequency_counter: Counter = Counter()
keyword_frequency_counter: Counter = Counter()
venue_frequency_counter: Counter = Counter()
ministry_score_values: list[float] = []

_KEYWORD_SEPARATOR_PATTERN = re.compile(r"[,;/]+")

for publication in publications:
    if publication.get("year", ""):
        year_frequency_counter[publication["year"]] += 1

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
```

#### Keyword normalisation

- Split on `,`, `;`, or `/` (and combinations)
- Lowercase
- Discard tokens shorter than 3 characters (removes articles, abbreviations, noise)
- Merge English and Polish keywords into a single frequency count

---

#### Ministry score

The Polish Ministry of Education assigns a numeric score (e.g. 20, 40, 70, 100, 140, 200) to each publication. The average is returned as a float rounded to 2 decimal places, or `None` if no scores were present. Higher scores indicate more prestigious venues.

---

### `_resolve_researcher_to_profile(researcher_name="", author_id=None) -> tuple[bool, dict | str]`

**This is the most important helper function.** It is called by all three tool handlers and determines whether a name or ID can be uniquely resolved to a researcher profile.

**Return value semantics:**
```
(False, dict)  → single unambiguous match; dict is the full profile
(True,  str)   → multiple matches; str is JSON disambiguation payload
(False, "")    → no match found
```

#### Path A — Resolve by author_id

```python
if author_id:
    record_id = re.sub(r"^urn:pw-repo:WUT", "", str(author_id).strip())
    author_record_elements = await search_wut_api("author", "id", record_id, maximum_results=1)
    if author_record_elements:
        return False, parse_researcher_element(author_record_elements[0])
    return False, {"id": record_id, "fullName": author_id}
```

Strips the URN prefix if present (so both `"12345"` and `"urn:pw-repo:WUT12345"` work). Fetches with `maximum_results=1` since IDs are unique. If not found, returns a minimal stub dict instead of `""` so callers can still use the ID to build publication queries.

---

#### Path B — Resolve by name

```python
first_name_part, surname = _split_full_name(researcher_name)
author_record_elements = await search_wut_api("author", "surname", surname, maximum_results=50)
if first_name_part:
    author_record_elements = _filter_elements_by_first_name(author_record_elements, first_name_part)
researcher_profiles = [parse_researcher_element(e) for e in author_record_elements]
```

- Fetches up to 50 results by surname (covers common surnames with many bearers).
- Narrows by first name if provided (with graceful degradation).
- Parses all matches.

---

#### Single match — return profile

```python
if len(researcher_profiles) == 1:
    return False, researcher_profiles[0]
```

---

#### Multiple matches — disambiguation payload

```python
disambiguation_options = []
for option_number, profile in enumerate(researcher_profiles, start=1):
    disambiguation_options.append({
        "option": option_number,
        "id": profile["id"],
        "urn": profile["urn"],
        "fullName": profile["fullName"],
        "degree": profile["academicDegree"],
        "positionEN": profile["positionEN"],
        "unit": profile["affiliation"],
        "profileUrl": profile["profileUrl"],
    })
return True, json.dumps({
    "found": True,
    "needs_disambiguation": True,
    "count": len(researcher_profiles),
    "message": f"Found {len(researcher_profiles)} WUT researchers matching ...",
    "researchers": disambiguation_options,
    "next_step": "Reply with option number. Call again with author_id set.",
}, ensure_ascii=False, indent=2)
```

The disambiguation payload is JSON-serialised here (not as a dict) because the caller returns it directly as the tool response string without further processing.

---

### `_search_records_by_type(record_type, element_parser_function, researcher, author_id, result_limit) -> str`

**Input:**
- `record_type` — `"phd"`, `"article"`, or `"book"`
- `element_parser_function` — the appropriate parser (`parse_phd_thesis_element`, etc.)
- `researcher` — researcher name string
- `author_id` — WUT ID or URN
- `result_limit` — max records to return

**Output:** JSON string `{"count": N, "results": [...]}` or disambiguation JSON.

#### Inner deduplication function

```python
def add_unique_record(record_xml_element: ET.Element) -> None:
    parsed_record = element_parser_function(record_xml_element)
    record_id = parsed_record.get("id", "")
    deduplication_key = record_id or parsed_record.get("title", "")
    if deduplication_key and deduplication_key not in seen_record_ids:
        seen_record_ids.add(deduplication_key)
        matched_records.append(parsed_record)
```

Uses record ID as the primary deduplication key, title as fallback (for records missing IDs). This prevents the same record appearing twice when author and supervisor searches both return it.

---

#### Standard flow — non-PhD record types

```python
if researcher_record_id:
    record_elements = await search_wut_api(record_type, "author.id", researcher_record_id, ...)
if not record_elements and surname:
    record_elements = await search_wut_api(record_type, "author.surname", surname, ...)
```

Author ID is tried first (exact match, faster). Falls back to surname only when ID search returns nothing.

---

#### PhD-specific parallel search

```python
parallel_search_coroutines = []
if researcher_record_id:
    parallel_search_coroutines.append(
        search_wut_api("phd", "author.id", researcher_record_id, ...)
    )
    parallel_search_coroutines.append(
        search_wut_api("phd", "supervisor.id", researcher_record_id, ...)
    )
elif surname:
    parallel_search_coroutines.append(
        search_wut_api("phd", "author.surname", surname, ...)
    )
    parallel_search_coroutines.append(
        search_wut_api("phd", "supervisor.surname", surname, ...)
    )
for search_result_elements in await asyncio.gather(*parallel_search_coroutines):
    for record_xml_element in search_result_elements:
        add_unique_record(record_xml_element)
```

`asyncio.gather` fires both searches concurrently, halving latency. Results are merged through `add_unique_record` which deduplicates. The WUT PhD record type has separate `author.id` and `supervisor.id` search fields — a professor who supervised 20 theses and also wrote one would only appear in results if both paths are searched.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 7. MCP Tools — Detailed
----------------------------------------------------------------------------------------------------------------------------------------------------

### `search_people`

**Handler function:** `handle_search_people`  
**Signature:** `(name, author_id, mode, compare_with, limit, year_from, year_to) -> str`

This tool is the researcher-centric entry point. The same function handles four conceptually distinct operations by branching on `mode`.

---

### Mode: `profile`

**What it does:**
Returns the researcher's structured profile card as JSON.

**Code path:**
```python
requires_disambiguation, primary_researcher_profile = await _resolve_researcher_to_profile(name, author_id)
if requires_disambiguation:
    return primary_researcher_profile   # forward disambiguation JSON
if not primary_researcher_profile:
    return json.dumps({"found": False, "message": ...})
return json.dumps({"found": True, "profile": primary_researcher_profile}, ...)
```

**What is returned:**
```json
{
  "found": true,
  "profile": {
    "id": "WUT...",
    "urn": "urn:pw-repo:WUT...",
    "fullName": "Jan Kowalski",
    "firstName": "Jan",
    "lastName": "Kowalski",
    "academicDegree": "prof. dr hab. inż.",
    "positionEN": "Full Professor",
    "positionPL": "Profesor zwyczajny",
    "affiliation": "Faculty of Electronics ...",
    "status": "active",
    "researchArea": "Computer Science",
    "hindex": 12,
    "profileUrl": "https://repo.pw.edu.pl/info/card/WUT..."
  }
}
```

---

### Mode: `analyze`

**What it does:**
Returns the researcher's profile plus aggregate statistics computed from their publications.

**Inner fetch function (shared by analyze, collaborators, and compare):**
```python
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
```

Defined as an inner async function so it can be reused without code duplication. Fetches up to 100 articles (not just 25) to give statistics meaningful coverage.

**Year filtering (post-fetch):**
```python
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
```

Applied after parsing. Publications with no extractable year pass through unchanged rather than being dropped.

**What is returned:**
```json
{
  "found": true,
  "profile": { ... },
  "statistics": {
    "totalPublications": 47,
    "topKeywords": ["machine learning", "neural networks", ...],
    "topVenues": ["Lecture Notes in Computer Science", ...],
    "ministryScoreAvg": 52.34,
    "activeYears": ["2010", "2012", "2013", ...]
  }
}
```

---

### Mode: `collaborators`

**What it does:**
Returns the researcher's profile plus a ranked list of co-authors.

**How collaboration is counted:**
```python
collaborator_frequency_counter: Counter = Counter()
for article_element in article_elements:
    for author_entry in _parse_author_list(article_element):
        if author_entry["id"] != primary_researcher_record_id and author_entry["name"]:
            collaborator_frequency_counter[author_entry["name"]] += 1
```

For each article, iterates all authors. If an author's ID differs from the primary researcher's ID, increments their count. Uses `author_entry["id"]` (not name) for exclusion — this correctly handles cases where the researcher's name appears in slightly different forms across records.

**What is returned:**
```json
{
  "found": true,
  "profile": { ... },
  "collaborators": [
    {"name": "Anna Nowak", "sharedPublications": 14},
    {"name": "Piotr Wiśniewski", "sharedPublications": 9},
    ...
  ]
}
```

Limited to `limit` entries (default 10) using `Counter.most_common(limit)`.

---

### Mode: `compare`

**What it does:**
Fetches profiles and computes statistics for two researchers in parallel and returns them side by side.

**Parallel fetch:**
```python
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
```

`asyncio.gather` fires both HTTP fetch chains concurrently. Since each researcher may need multiple API pages, parallel fetching can save significant latency.

**What is returned:**
```json
{
  "found": true,
  "comparison": [
    {
      "profile": { "fullName": "Jan Kowalski", ... },
      "statistics": { "totalPublications": 47, ... }
    },
    {
      "profile": { "fullName": "Anna Nowak", ... },
      "statistics": { "totalPublications": 31, ... }
    }
  ]
}
```

---

### `search_publications`

**Handler function:** `handle_search_publications`  
**Signature:** `(researcher, author_id, pub_type, year_from, year_to, year, limit) -> str`

Searches for journal articles and books. Returns a unified list regardless of type.

#### Limit clamping

```python
limit = min(max(1, limit), 100)
```

Enforces 1 ≤ limit ≤ 100.

---

#### Publication type routing

```python
publication_type_to_record_type_map = {
    "article": ("article", parse_article_element),
    "book":    ("book",    parse_book_element),
}
if requested_publication_type in publication_type_to_record_type_map:
    record_types_to_search = [publication_type_to_record_type_map[requested_publication_type]]
else:
    record_types_to_search = list(publication_type_to_record_type_map.values())
```

`"all"` (default) produces both types. `"article"` or `"book"` selects only one.

---

#### Researcher resolution

```python
requires_disambiguation, researcher_profile = await _resolve_researcher_to_profile(researcher, author_id)
if requires_disambiguation:
    return researcher_profile  # forward disambiguation JSON
```

If disambiguation is required, the JSON is returned immediately without searching for publications.

---

#### Per-type search with fallback

```python
for record_type, element_parser_function in record_types_to_search:
    if researcher_record_id:
        for record_element in await search_wut_api(record_type, "author.id", researcher_record_id, ...):
            add_unique_publication(element_parser_function(record_element))
    if not matched_publications and surname:
        for record_element in await search_wut_api(record_type, "author.surname", surname, ...):
            add_unique_publication(element_parser_function(record_element))
```

`if not matched_publications` means the surname fallback only fires if the ID search found nothing across *all* types searched so far. This avoids unnecessary API calls when the ID search already returned results.

---

#### Year-only search guard

```python
elif year or year_from or year_to:
    return json.dumps({
        "found": False,
        "message": "The WUT repository API does not support year-only searches..."
    })
```

The WUT API cannot search by year alone. Rather than silently returning nothing, an informative error is returned.

---

#### Post-fetch year filter

```python
year_range_start = year_from or (year if year else 1900)
year_range_end   = year_to   or (year if year else 2099)
for publication_record in matched_publications:
    publication_year_string = publication_record.get("year", "")
    if not publication_year_string:
        year_filtered_publications.append(publication_record)  # keep if year unknown
        continue
    try:
        publication_year_int = int(publication_year_string)
        if year_range_start <= publication_year_int <= year_range_end:
            year_filtered_publications.append(publication_record)
    except ValueError:
        year_filtered_publications.append(publication_record)  # keep if unparseable
```

Publications without an extractable year are kept rather than dropped — it is more useful to show a result with unknown year than to silently discard it.

---

#### Deduplication key priority

```python
deduplication_key = (
    parsed_publication.get("id")
    or parsed_publication.get("doi")
    or parsed_publication.get("title", "")
)
```

| Key | Strength | Reason |
|---|---|---|
| `id` | Strongest | Unique in the database |
| `doi` | Medium | Used when ID is absent (some conference papers lack database IDs) |
| `title` | Fallback | Last resort for records with neither ID nor DOI |

---

#### What is returned

```json
{
  "count": 14,
  "results": [
    {
      "type": "article",
      "id": "WUT...",
      "title": "...",
      "authors": ["Jan Kowalski", "Anna Nowak"],
      "year": "2021",
      "doi": "10.1234/...",
      "journal": "Lecture Notes in Computer Science",
      "collation": "pp. 45-58",
      "score": "70",
      "abstractEN": "...",
      "keywordsEN": "machine learning, neural networks",
      "url": "https://repo.pw.edu.pl/info/r/WUT..."
    },
    {
      "type": "MONOGRAFIA",
      "isbn": "978-...",
      "publisher": "Wydawnictwo PW"
    }
  ]
}
```

---

### `search_phd_theses`

**Handler function:** `handle_search_phd_theses`  
**Signature:** `(researcher, author_id, limit) -> str`

Searches for WUT doctoral dissertations. Entirely delegates to `_search_records_by_type`:

```python
return await _search_records_by_type(
    record_type="phd",
    element_parser_function=parse_phd_thesis_element,
    researcher=researcher,
    author_id=author_id,
    result_limit=min(max(1, limit), 100),
)
```

#### Key behaviour from `_search_records_by_type`

| Step | What happens |
|---|---|
| 1 | Resolves the researcher via `_resolve_researcher_to_profile` |
| 2 | If a numeric ID is known: fires `author.id` + `supervisor.id` in parallel via `asyncio.gather` |
| 3 | If only a name/surname is known: fires `author.surname` + `supervisor.surname` in parallel |
| 4 | If ID searches return nothing, falls back to surname searches |
| 5 | All results merged through `add_unique_record` for deduplication |

---

#### Practical effect

| Input | What is returned |
|---|---|
| Professor's name | All theses they supervised + any they authored |
| PhD student's name | Their own thesis + any they co-supervised |

---

#### What is returned

```json
{
  "count": 5,
  "results": [
    {
      "type": "phd_thesis",
      "id": "WUT...",
      "titleEN": "Distributed Default Reasoning in the Semantic Web",
      "titlePL": "Rozproszone wnioskowanie domyślne w Sieci Semantycznej",
      "title": "Distributed Default Reasoning in the Semantic Web",
      "author": "Przemysław Więcheć",
      "supervisor": "Henryk Rybiński",
      "year": "2011",
      "defenseDate": "2011-06-23",
      "abstractEN": "...",
      "keywordsEN": "semantic web, default reasoning",
      "url": "https://repo.pw.edu.pl/info/r/WUT..."
    }
  ]
}
```

----------------------------------------------------------------------------------------------------------------------------------------------------
## 8. MCP Protocol Handlers
----------------------------------------------------------------------------------------------------------------------------------------------------

### `list_tools() -> list[Tool]`

```python
@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS
```

Registered with the MCP server via the `@mcp_server.list_tools()` decorator. Called by MCP clients during the protocol handshake to discover what tools are available and what their input schemas are. Returns the `TOOLS` list verbatim.

---

### `call_tool(name: str, arguments: dict) -> list[TextContent]`

```python
@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
```

The single dispatch point for all tool calls.

#### Step 1 — Tool lookup

```python
tool_handler_function = _TOOL_NAME_TO_HANDLER_MAP.get(name)
if tool_handler_function is None:
    return [TextContent(type="text", text=json.dumps({
        "error": f"Unknown tool '{name}'. Available: {list(_TOOL_NAME_TO_HANDLER_MAP.keys())}"
    }))]
```

Unknown tool names return a JSON error payload, not a Python exception. This keeps the MCP session alive rather than terminating it.

---

#### Step 2 — Execution with error catching

```python
try:
    tool_execution_result = await tool_handler_function(**(arguments or {}))
except Exception as exc:
    return [TextContent(type="text", text=json.dumps({
        "error": f"Tool '{name}' raised an exception: {type(exc).__name__}: {exc}"
    }))]
```

`**(arguments or {})` expands the arguments dict as keyword arguments. The `or {}` guard handles cases where `arguments` is `None`. All exceptions are caught and returned as structured JSON — a tool error should never crash the MCP server.

---

#### Step 3 — Coerce to string and wrap

```python
if not isinstance(tool_execution_result, str):
    tool_execution_result = json.dumps(tool_execution_result, ensure_ascii=False, indent=2)
return [TextContent(type="text", text=tool_execution_result)]
```

All tool handlers return strings, but this coercion adds safety. `TextContent(type="text", text=...)` is the MCP protocol format for text responses.

----------------------------------------------------------------------------------------------------------------------------------------------------
## 9. Transport Layer
----------------------------------------------------------------------------------------------------------------------------------------------------

### `_run_stdio_transport() -> None`

```python
def _run_stdio_transport() -> None:
    from mcp.server.stdio import stdio_server
    async def run_async_stdio_server() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )
    asyncio.run(run_async_stdio_server())
```

Starts the MCP server reading from stdin and writing to stdout. `stdio_server()` is an async context manager that opens the streams; `mcp_server.run()` processes MCP protocol messages on those streams. `asyncio.run()` starts the event loop (blocking until the process is terminated).

Used when the server is launched as a subprocess by Claude Desktop or the MCP CLI.

---

### `_run_sse_transport(http_port_number: int) -> None`

Starts a Starlette ASGI app served by Uvicorn. Exposes three routes.

#### Route: `GET /sse` — SSE transport

```python
async def handle_sse_connection(request: Request) -> None:
    async with sse_server_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as sse_streams:
        await mcp_server.run(
            sse_streams[0],
            sse_streams[1],
            mcp_server.create_initialization_options(),
        )
```

Each incoming SSE connection gets its own `mcp_server.run` coroutine with dedicated read/write streams. Multiple clients can connect simultaneously; each is served independently.

---

#### Route: `POST /messages` — Streamable HTTP transport

```python
Mount("/messages", app=sse_server_transport.handle_post_message)
```

Modern MCP clients use HTTP POST for sending tool call requests. The `SseServerTransport` object handles routing POST bodies to the correct SSE connection's input stream.

---

#### Route: `GET /health` — liveness probe

```python
async def handle_health_check(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
```

Returns HTTP 200 with `{"status": "ok"}`. Azure App Service periodically checks this endpoint to verify the server is running. Without it, Azure would mark the deployment as unhealthy and restart it.

---

#### Binding

```python
uvicorn.run(starlette_web_application, host="0.0.0.0", port=http_port_number)
```

`0.0.0.0` binds to all network interfaces, which is required in containerised environments (Azure App Service) where the container's external IP is not known at startup time.

---

### `main() -> None`

```python
def main() -> None:
    port_environment_variable = os.environ.get("PORT") or os.environ.get("WEBSITES_PORT")
    if port_environment_variable:
        try:
            http_port_number = int(port_environment_variable)
        except ValueError:
            http_port_number = 8000
        _run_sse_transport(http_port_number)
    else:
        _run_stdio_transport()
```

Reads two environment variables in priority order:

| Variable | Set by |
|---|---|
| `PORT` | Generic cloud platform convention |
| `WEBSITES_PORT` | Azure App Service (set automatically) |

If either is set, starts in SSE/HTTP mode on that port. If neither is set, starts in stdio mode. The `try/except` around `int()` ensures a non-numeric port value (e.g. `"auto"`) doesn't crash the server — it defaults to port 8000.

**Why this design:**
The same `server.py` file works in three scenarios without any code changes:

| Scenario | Environment | Mode |
|---|---|---|
| Local Claude Desktop | No env vars set | stdio |
| Local HTTP testing | `PORT=8000 python server.py` | SSE |
| Azure App Service | `WEBSITES_PORT` set automatically | SSE |

----------------------------------------------------------------------------------------------------------------------------------------------------
## Appendix — Full Call Graph
----------------------------------------------------------------------------------------------------------------------------------------------------

```
main()
├── _run_stdio_transport()
│   └── mcp_server.run()
│       ├── list_tools() → TOOLS
│       └── call_tool(name, arguments)
│           ├── handle_search_people(**arguments)
│           │   ├── _resolve_researcher_to_profile()
│           │   │   ├── search_wut_api("author", "id"|"surname", ...)
│           │   │   │   ├── _fetch_all_api_pages()
│           │   │   │   │   └── _fetch_api_page()  [cache + HTTP]
│           │   │   │   │       └── _parse_xml_bytes_to_element()
│           │   │   │   │           └── _strip_xml_namespace_prefixes()
│           │   │   │   ├── [cold-start retry 500ms]
│           │   │   │   └── [diacritics fallback 300ms each]
│           │   │   │       └── _generate_diacritics_variants()
│           │   │   ├── _split_full_name()
│           │   │   ├── _filter_elements_by_first_name()
│           │   │   │   └── strip_diacritics()
│           │   │   └── parse_researcher_element()
│           │   │       └── _build_record_urn()
│           │   ├── [mode=analyze]
│           │   │   ├── fetch_article_elements_for_researcher()
│           │   │   │   └── search_wut_api(...)
│           │   │   ├── parse_article_element() × N
│           │   │   │   ├── _extract_record_id()
│           │   │   │   ├── _get_author_display_names()
│           │   │   │   │   └── _parse_author_list()
│           │   │   │   └── _extract_publication_year()
│           │   │   └── _compute_publication_statistics()
│           │   ├── [mode=collaborators]
│           │   │   ├── fetch_article_elements_for_researcher()
│           │   │   └── _parse_author_list() × N
│           │   └── [mode=compare]
│           │       └── asyncio.gather(fetch × 2)
│           │           └── fetch_article_elements_for_researcher() × 2
│           ├── handle_search_publications(**arguments)
│           │   ├── _resolve_researcher_to_profile()
│           │   ├── search_wut_api("article"|"book", ...)
│           │   └── parse_article_element() | parse_book_element()
│           └── handle_search_phd_theses(**arguments)
│               └── _search_records_by_type("phd", ...)
│                   ├── _resolve_researcher_to_profile()
│                   ├── asyncio.gather(
│                   │     search_wut_api("phd", "author.id", ...),
│                   │     search_wut_api("phd", "supervisor.id", ...)
│                   │   )
│                   └── parse_phd_thesis_element() × N
└── _run_sse_transport(port)
    └── uvicorn.run(Starlette app)
        ├── GET  /sse      → mcp_server.run() [per connection]
        ├── POST /messages → sse_server_transport.handle_post_message
        └── GET  /health   → {"status": "ok"}
```
