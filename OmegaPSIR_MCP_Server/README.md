# WUT OMEGA-PSIR MCP Server  v5

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the Warsaw University of Technology research repository ([repo.pw.edu.pl](https://repo.pw.edu.pl)) to AI assistants such as Claude Desktop and Claude.ai.

The server wraps the WUT REST API and provides three structured tools for searching researchers, publications, and doctoral dissertations.

---

## Table of Contents

1. [Features](#features)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration — Claude Desktop](#configuration--claude-desktop)
5. [Configuration — Claude.ai (SSE / HTTP)](#configuration--claudeai-sse--http)
6. [Tool Reference](#tool-reference)
   - [search_people](#search_people)
   - [search_publications](#search_publications)
   - [search_phd_theses](#search_phd_theses)
7. [Example Prompts](#example-prompts)
8. [Architecture](#architecture)
9. [Azure App Service Deployment](#azure-app-service-deployment)
10. [Project Structure](#project-structure)

---

## Features

| Tool | What it does |
|---|---|
| `search_people` | Find WUT researchers by name or ID; supports **profile**, **analyze**, **collaborators**, and **compare** modes |
| `search_publications` | Retrieve journal articles and books by researcher; filter by type and year range |
| `search_phd_theses` | Find doctoral dissertations by thesis author **or** supervisor name |

- Polish diacritics fallback — type `Rybinski` and the server automatically retries `Rybiński`
- In-memory response cache (5 min TTL) — repeated calls within a session are instant
- Researcher disambiguation — when a name matches multiple people, the server returns a numbered list and asks you to re-call with `author_id`
- Dual transport — **stdio** for local Claude Desktop / CLI, **SSE + HTTP** for cloud deployments

---

## Prerequisites

- Python **3.11** or newer
- pip

---

## Installation

```bash
# 1. Clone or copy the project
cd OmegaPSIR_MCP_Server

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration — Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "omega-psir": {
      "command": "C:\\Users\\<YOU>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": [
        "C:\\path\\to\\OmegaPSIR_MCP_Server\\server.py"
      ]
    }
  }
}
```

Replace `<YOU>` and the path with your actual values, then **restart Claude Desktop**. The three tools (`search_people`, `search_publications`, `search_phd_theses`) will appear in the tools panel.

---

## Configuration — Claude.ai (SSE / HTTP)

Set the `PORT` environment variable before starting the server; it will switch to SSE transport automatically:

```bash
PORT=8000 python server.py
```

On **Azure App Service** the `WEBSITES_PORT` variable is used instead (set it in the App Service environment variables).

The server exposes:

| Endpoint | Description |
|---|---|
| `GET  /sse` | SSE transport for legacy MCP clients |
| `POST /messages` | Streamable HTTP transport |
| `GET  /health` | Liveness probe — returns `{"status": "ok"}` |

In Claude.ai, add the server under **Settings → Integrations → Custom MCP** and enter your public URL (e.g. `https://<your-app>.azurewebsites.net/sse`).

------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

## Tool Reference

### `search_people`

Search for WUT researchers and analyse their profiles.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | Full or partial researcher name, e.g. `"Jan Kowalski"` |
| `author_id` | string | — | WUT numeric ID or URN (`urn:pw-repo:WUT…`). Skips disambiguation. |
| `mode` | enum | `"profile"` | `profile` / `analyze` / `collaborators` / `compare` |
| `compare_with` | string | — | Second researcher name (required for `compare` mode) |
| `limit` | integer | `10` | Max collaborators to return (`collaborators` mode) |
| `year_from` | integer | — | Start-year filter for `analyze` mode |
| `year_to` | integer | — | End-year filter for `analyze` mode |

**Modes**

| Mode | Returns |
|---|---|
| `profile` | Full profile card: name, degree, position, affiliation, h-index, profile URL |
| `analyze` | Profile + publication statistics: total publications, top keywords, top venues, ministry score average, active years |
| `collaborators` | Ranked list of co-authors and shared publication counts |
| `compare` | Side-by-side statistics for two researchers |

**Response shape (profile)**
```json
{
  "found": true,
  "profile": {
    "id": "WUT199a9ab6449042c2be38c2a61b448af7",
    "urn": "urn:pw-repo:WUTWUT199a9ab6449042c2be38c2a61b448af7",
    "fullName": "Kamil Rybiński",
    "firstName": "Kamil",
    "lastName": "Rybiński",
    "academicDegree": "dr inż.",
    "positionEN": "...",
    "affiliation": "...",
    "hindex": null,
    "profileUrl": "https://repo.pw.edu.pl/info/card/WUT..."
  }
}
```

**Disambiguation response** (when multiple matches exist)
```json
{
  "found": true,
  "needs_disambiguation": true,
  "count": 3,
  "message": "Found 3 WUT researchers matching 'Kowalski'. Re-call with author_id...",
  "researchers": [
    { "option": 1, "id": "WUT...", "fullName": "Jan Kowalski", "degree": "...", ... },
    ...
  ],
  "next_step": "Reply with option number. Call again with author_id set."
}
```

---

### `search_publications`

Search WUT publications (journal articles and books).

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `researcher` | string | — | Researcher full name |
| `author_id` | string | — | WUT numeric ID or URN |
| `pub_type` | enum | `"all"` | `"all"` / `"article"` / `"book"` |
| `year_from` | integer | — | Filter: published from this year (inclusive) |
| `year_to` | integer | — | Filter: published up to this year (inclusive) |
| `year` | integer | — | Filter: exact publication year |
| `limit` | integer | `25` | Max results (1–100) |

> **Note:** `researcher` or `author_id` is required. Year-only search is not supported by the WUT REST API.

**Response shape**
```json
{
  "count": 14,
  "results": [
    {
      "type": "article",
      "id": "WUT...",
      "title": "RSL-DL: Representing Domain Knowledge...",
      "authors": ["Kamil Rybiński", "Michał Śmiałek"],
      "year": "2016",
      "doi": "",
      "journal": "Lecture Notes in Computer Science",
      "collation": "pp. 123-135",
      "score": "20",
      "abstractEN": "...",
      "keywordsEN": "low-code, requirements",
      "url": "https://repo.pw.edu.pl/info/r/WUT..."
    }
  ]
}
```

> **Book type field:** Books are returned with the API's native Polish type value, e.g. `"MONOGRAFIA"` (monograph), rather than the generic `"book"`.

---

### `search_phd_theses`

Search WUT doctoral dissertations.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `researcher` | string | — | Thesis author **or** supervisor name |
| `author_id` | string | — | WUT numeric ID or URN |
| `limit` | integer | `25` | Max results (1–100) |

The server automatically tries both the **thesis author** and **supervisor** search paths and merges the results. This means:
- Searching for a professor returns all theses they supervised.
- Searching for a PhD student returns their own thesis.
- Both sets are deduplicated and returned together in one call.

**Response shape**
```json
{
  "count": 15,
  "results": [
    {
      "type": "phd_thesis",
      "id": "WUT...",
      "titleEN": "Distributed Default Reasoning in the Semantic Web",
      "titlePL": "...",
      "title": "Distributed Default Reasoning in the Semantic Web",
      "author": "Przemysław B. Więcheć",
      "supervisor": "Henryk Rybiński",
      "year": "2011",
      "defenseDate": "2011-...",
      "abstractEN": "...",
      "keywordsEN": "...",
      "url": "https://repo.pw.edu.pl/info/r/WUT..."
    }
  ]
}
```

---


## Architecture

```
Claude Desktop / Claude.ai
        │
        │  MCP protocol (stdio or SSE/HTTP)
        ▼
  server.py  ──  MCP tool dispatcher (call_tool)
        │
        │  handle_search_people
        │  handle_search_publications
        │  handle_search_phd_theses
        │
        ▼
  search_wut_api()  ──  in-memory response cache (5 min TTL)
        │
        │  httpx async HTTP  +  retry  +  diacritics fallback
        ▼
  WUT REST API  (repo.pw.edu.pl)
        │
        │  XML response  →  _parse_xml_bytes_to_element  →  _strip_xml_namespace_prefixes
        ▼
  parse_researcher_element
  parse_article_element
  parse_book_element
  parse_phd_thesis_element
        │
        ▼
  JSON string  →  TextContent  →  Claude
```

**Key design decisions**

| Decision | Reason |
|---|---|
| In-process XML parsing (no ORM) | The WUT API returns namespace-prefixed XML (`ns2:book`); `_strip_xml_namespace_prefixes()` normalises it before extraction |
| 4-step year extraction strategy | The XML contains many administrative timestamp fields (`lastModified`, `scoreDate`, `id` UUIDs with year fragments) that would contaminate a naive scan |
| Additive PhD search (author + supervisor in parallel) | A supervisor search is not a fallback — professors and students both need to be found in one call |
| Diacritics retry, not pre-normalisation | The API performs exact string matching; normalising the query before sending would miss correctly spelled accented names |
| `_ADMINISTRATIVE_TAGS_TO_SKIP` allowlist | Discovered through live API testing: `<legalBasis>` contains regulation years ("2019 r."), `<rulesetName>` has version years ("reguly_2017_v1f"), UUID `<id>` fields can contain year-like hex segments |

---

## Azure App Service Deployment

The server auto-detects Azure by checking the `WEBSITES_PORT` environment variable.

```bash
# In Azure App Service → Configuration → Application settings:
WEBSITES_PORT = 8000

# Startup command:
python server.py
```

The server starts in SSE mode on the configured port. Set the MCP URL in Claude.ai to:

```
https://<your-app>.azurewebsites.net/sse
```

---

## Project Structure

```
OmegaPSIR_MCP_Server/
├── server.py          # All server logic (single-file design)
├── requirements.txt   # Python dependencies
└── README.md          # This file
```

**`server.py` internal sections**

| Section | Contents |
|---|---|
| 1 — Constants | `WUT_API_BASE_URL`, `WUT_REPOSITORY_BASE_URL`, `MAX_RECORDS_PER_PAGE`, `mcp_server` instance |
| 2 — Diacritics helpers | `strip_diacritics`, `_generate_diacritics_variants`, `_DIACRITICS_TRANSLATION_TABLE` |
| 3 — XML utilities | `_strip_xml_namespace_prefixes`, `_parse_xml_bytes_to_element`, `_get_child_text`, `_extract_record_id`, `_get_all_direct_children` |
| 4 — Entity parsers | `_extract_publication_year`, `_parse_author_list`, `_get_author_display_names`, `parse_researcher_element`, `parse_article_element`, `parse_book_element`, `parse_phd_thesis_element` |
| 5 — HTTP client & cache | `_fetch_api_page`, `_fetch_all_api_pages`, `search_wut_api`, `_RESPONSE_CACHE`, `_CACHE_TTL_SECONDS` |
| 6 — Name helpers | `_split_full_name`, `_filter_elements_by_first_name` |
| 7 — Researcher resolution | `_build_record_urn`, `_compute_publication_statistics`, `_resolve_researcher_to_profile`, `_search_records_by_type` |
| 8 — Tool implementations | `handle_search_people`, `handle_search_publications`, `handle_search_phd_theses` |
| 9 — Tool registry | MCP `TOOLS` list and `_TOOL_NAME_TO_HANDLER_MAP` |
| 10 — MCP handlers | `list_tools`, `call_tool` |
| 11 — Dual transport | `_run_stdio_transport`, `_run_sse_transport`, `main` |

**Key constants and their roles**

| Name | Value / Type | Role |
|---|---|---|
| `WUT_API_BASE_URL` | `"https://repo.pw.edu.pl/seam/resource/rest"` | Root URL for all WUT REST API calls |
| `WUT_REPOSITORY_BASE_URL` | `"https://repo.pw.edu.pl"` | Base for building profile and record URLs |
| `MAX_RECORDS_PER_PAGE` | `25` | WUT API hard limit; used by `_fetch_all_api_pages` |
| `_DIACRITICS_TRANSLATION_TABLE` | `str.maketrans(…)` | Maps Polish accented chars to ASCII equivalents |
| `_XML_NAMESPACE_PATTERN` | `re.compile(r"\{[^}]+\}")` | Strips Clark-notation `{uri}tag` namespace prefixes |
| `_FOUR_DIGIT_YEAR_PATTERN` | `re.compile(r"\b(19|20)\d{2}\b")` | Matches valid publication years (1900–2099) |
| `_ADMINISTRATIVE_TAGS_TO_SKIP` | `frozenset` | XML tags whose text must never be treated as publication years |
| `_DIRECT_YEAR_FIELD_NAMES` | tuple of strings | Field names checked first for year extraction |
| `_NESTED_YEAR_ELEMENT_PATHS` | tuple of tuples | Two-step paths tried next (e.g. `journalissue/issueDate`) |
| `_JOURNAL_ISSUE_RECURSIVE_YEAR_FIELDS` | tuple of strings | Fields searched recursively inside `<journalissue>` |
| `_HTTP_REQUEST_HEADERS` | dict | `Accept: application/xml` header sent with every request |
| `_HTTP_CLIENT` | `httpx.AsyncClient` | Shared async HTTP client (connection-pool reuse) |
| `_RESPONSE_CACHE` | dict | URL → `(expiry_timestamp, bytes \| None)` |
| `_CACHE_TTL_SECONDS` | `300` | Cache time-to-live in seconds (5 minutes) |
| `_TOOL_NAME_TO_HANDLER_MAP` | dict | Maps MCP tool name strings to their async handler functions |

---

## Detailed Function Reference

This section explains every function in `server.py` — what it does, how it works internally, and why it was written the way it was.

---

### Section 2 — Diacritics Helpers

#### `strip_diacritics(text_with_polish_characters: str) -> str`

Converts Polish letters (ą ć ę ł ń ó ś ź ż and their uppercase equivalents) to their nearest ASCII equivalents using a pre-built `str.maketrans` translation table. For example `"Rybiński"` → `"Rybinski"`.

Used internally by `_filter_elements_by_first_name` so that a search for `"Jan"` also matches `"Ján"` or any accented first name variant. Not used when building queries (see `_generate_diacritics_variants` for why).

#### `_generate_diacritics_variants(surname_or_name: str) -> list[str]`

Generates alternative accented spellings of a name by substituting common ASCII letters with their Polish counterparts:

| ASCII | Polish |
|---|---|
| `o` | `ó` |
| `l` | `ł` |
| `z` | `ż` or `ź` |
| `a` | `ą` |
| `e` | `ę` |
| `s` | `ś` |
| `c` | `ć` |
| `n` | `ń` |

The first element of the returned list is always the original unchanged input. Subsequent elements are variants that have not already appeared (tracked by a `seen` set to avoid duplicates).

**Why this exists:** The WUT REST API performs exact string matching — `"Rybinski"` returns zero results because the API stores `"Rybiński"`. Pre-normalising the query to ASCII would break searches for correctly spelled accented names. Instead, `search_wut_api` calls this function *only after* the plain ASCII query returns nothing, and tries each variant until one succeeds.

---

### Section 3 — XML Utilities

#### `_strip_xml_namespace_prefixes(xml_root_element: ET.Element) -> None`

Removes Clark-notation namespace prefixes from every element and attribute in the XML tree. Clark notation looks like `{http://example.com/ns}tagname`; this function strips the `{uri}` portion in-place using a compiled regex.

**Why needed:** The WUT API returns namespace-qualified XML (e.g. `ns2:book`). Without stripping, every `element.find("title")` call would need to know the exact namespace URI. Stripping normalises the tree so all downstream code uses bare tag names.

#### `_parse_xml_bytes_to_element(response_bytes: bytes) -> ET.Element`

Parses raw XML bytes into an `ElementTree.Element` and immediately strips namespaces. This is the single entry point for converting HTTP response bodies into queryable XML trees.

#### `_get_child_text(parent_element, *child_tags, default="") -> str`

Walks a chain of nested tag names and returns the `.text` content of the final element, or `default` (empty string) if any step is missing. Strips surrounding whitespace from the result.

Accepts variadic `*child_tags` so callers can express multi-level paths in one call:
```python
_get_child_text(article, "journalissue", "journalseries", "title")
```
This is safer than chaining `.find()` calls manually because a missing intermediate element returns the default rather than raising `AttributeError`.

#### `_extract_record_id(record_element: ET.Element) -> str`

Returns the WUT numeric record identifier. Tries the `id` attribute first (present on most record types), then falls back to a direct `<id>` child element. Returns an empty string if neither is found.

#### `_get_all_direct_children(parent_element, child_tag) -> list[ET.Element]`

A thin wrapper around `element.findall(child_tag)`. Exists to give call-sites a self-documenting name (`_get_all_direct_children`) rather than requiring readers to remember that `findall` without `./` returns only direct children in the version of ElementTree used here.

---

### Section 4 — Entity Parsers

These functions are the boundary between WUT's XML schema and the clean JSON the tools return. Each takes a raw XML element and returns a Python dict.

#### `_parse_author_list(publication_element: ET.Element) -> list[dict]`

Iterates all `<author>` direct children of a publication element and returns a list of dicts, each with:
- `name` — display name (`presentedFullName` preferred, or `name + surname` as fallback)
- `id` — WUT numeric record ID
- `profileUrl` — full URL to the author's profile page

Used both to populate the `"authors"` field of publication dicts and by `handle_search_people` (collaborators mode) where the raw dicts (including `id`) are needed for deduplication against the primary researcher.

#### `_get_author_display_names(publication_element: ET.Element) -> list[str]`

Returns only the name strings from `_parse_author_list`. Used when building the `"authors"` field in article/book dicts where the full author object is not needed.

#### `parse_researcher_element(author_xml_element: ET.Element) -> dict`

Parses a WUT `<author>` record into a full researcher profile. Key extraction details:

- **h-index:** read from `<authorprofile><hindex>`, cast to `int`. Returns `None` if absent or non-numeric.
- **Affiliation:** tries `_get_child_text` for a plain text `<affiliation>` first. If the element exists but contains nested sub-elements (e.g. faculty and department), falls back to `itertext()` joining all text fragments with ` | `.
- **Position fields:** stored in the API as `possitionEN` and `possitionPL` (double-s misspelling in the upstream schema). The field names in the returned dict are corrected to `positionEN` / `positionPL`.

#### `_extract_publication_year(record_element: ET.Element) -> str`

Extracts a 4-digit publication year using a four-step strategy, in order:

1. **Direct children** — checks `year`, `issueDate`, `publishDate`, `publicationDate`, `publicationYear`, `datePublished`, `beginDate`, `defenseDate` as direct children of the record element. Applies a regex `\b(19|20)\d{2}\b` to the text so partial date strings (`"2019-03-15"`) are handled correctly.

2. **Named nested paths** — traverses fixed two-step paths: `journalissue/issueDate`, `book/issueDate`, `book/year`, `book/date`, `issue/year`. These cover the most common publication types (journals and conference-proceedings articles).

3. **Recursive `<journalissue>` search** — for conference papers where the date is buried inside a `<conference>` or `<journalseries>` sub-element, does a recursive `.//{field}` scan within `<journalissue>` for `issueDate`, `date`, `startDate`.

4. **Last-resort descendant scan** — iterates every descendant, but skips a known blocklist of administrative tags (`lastModified`, `scoreDate`, `evaluationDate`, `legalBasis`, `rulesetName`, `id`, etc.) that contain system timestamps or version years that would contaminate the result.

Returns an empty string if no valid year is found.

#### `parse_article_element(article_xml_element: ET.Element) -> dict`

Parses a `<article>` or `<publication>` element. Journal name resolution tries four paths in order:
1. `journalissue/journalseries/title`
2. `journalissue/journalseries/name`
3. `journalissue/title`
4. `journal`

This covers both standard journal articles and conference-proceedings articles (which are stored as `article` records with a `book` child containing the conference details).

#### `parse_book_element(book_xml_element: ET.Element) -> dict`

Parses a `<book>` or `<bookchapter>` element. Publisher resolution tries `<publisher>` (plain text) then `<publisherInstitution><name>`. The `type` field is taken from `<bookType>` if present (giving Polish values like `"MONOGRAFIA"`), defaulting to `"book"`.

#### `parse_phd_thesis_element(phd_xml_element: ET.Element) -> dict`

Parses a `<phd>` element. Three specific extraction decisions:

- **Title preference:** `titleEN` → `titlePL` → `title`. Returns the first non-empty value. Both language versions are also included as separate fields.
- **Author:** the `<author>` element is a nested record element (not plain text), so it must be found first and then its `presentedFullName` or `name + surname` read from its children.
- **Supervisor:** looks for `<supervisor>` first; if absent, tries `<promoter>` as a fallback (some older records use the Polish term). Name is assembled as `name + surname`.

---

### Section 5 — HTTP Client and Cache

#### `_fetch_api_page(record_type, search_field, search_value, pagination_offset, page_size) -> ET.Element | None`

Makes a single GET request to:
```
/accesspoint/search/{record_type}/@{search_field}='{url_encoded_value}'/{offset}/{page_size}
```

Before any network call, checks `_RESPONSE_CACHE`. If the cached entry is still within its TTL, returns the cached result immediately (or `None` if the cached result was an error).

Percent-encodes the field value with `safe=""` so even forward slashes inside values are encoded. This is necessary for Polish names and any value containing characters that would otherwise be interpreted as URL separators.

Error handling: HTTP 404 and any `httpx.HTTPError` cache `None` and return `None`. `ET.ParseError` is also caught — the WUT API returns an HTML error page (not XML) for unsupported record types or very high pagination offsets.

#### `_fetch_all_api_pages(record_type, search_field, search_value, maximum_results) -> list[ET.Element]`

Calls `_fetch_api_page` in a loop, incrementing the offset by `page_size` after each successful page. Stops when:
- A page returns `None` (error or no data), or
- A page returns fewer elements than requested (indicates the last page).

Filters out wrapper elements (`status`, `count`, `total`, `offset`, `limit`) that the API includes alongside the actual records in each response.

#### `search_wut_api(record_type, search_field, search_value, maximum_results=25) -> list[ET.Element]`

High-level wrapper over `_fetch_all_api_pages` with two automatic retry strategies:

1. **Cold-start retry:** On the WUT API, the first request after a long period of inactivity often returns empty. The function waits 500 ms and retries once if the initial call returned nothing.

2. **Diacritics fallback:** If still empty *and* the search field is a name field (`surname`, `name`, `fullName`, `presentedFullName`, `author.surname`, `supervisor.surname`), generates accented Polish variants via `_generate_diacritics_variants` and tries each with a 300 ms gap between attempts. Stops at the first successful variant.

---

### Section 6 — Name Helpers

#### `_split_full_name(full_name: str) -> tuple[str, str]`

Splits `"Jan Andrzej Kowalski"` into `("Jan Andrzej", "Kowalski")` by treating the last whitespace-delimited token as the surname. Returns `("", token)` for a single-token input and `("", "")` for an empty string.

Used throughout the code when only the surname is needed for an API query (which searches by `surname` field) but the full name string is what the caller provided.

#### `_filter_elements_by_first_name(researcher_elements, first_name_prefix) -> list[ET.Element]`

Filters a list of author XML elements to those whose `<name>` child starts with `first_name_prefix`. Both sides are lowercased and passed through `strip_diacritics` before comparison, making it case- and accent-insensitive.

**Graceful degradation:** If the filter would produce an empty list, the original list is returned unchanged. This prevents an overly specific first-name prefix (e.g. a nickname or shortened name) from silently returning no results when there are plausible candidates.

---

### Section 7 — Researcher Resolution Helpers

#### `_build_record_urn(record_id: str) -> str`

Constructs a WUT URN: `urn:pw-repo:WUT{record_id}`. Returns `""` for empty input. Idempotent — if the input already starts with `"urn:"`, it is returned unchanged.

#### `_compute_publication_statistics(publications: list[dict]) -> dict`

Computes aggregate statistics over a list of parsed publication dicts in a **single pass** using Python `Counter` objects:

- **Keywords:** collects from both `keywordsEN` and `keywordsPL`, splits on `,;/`, lowercases, and drops tokens shorter than 3 characters. Returns the 8 most frequent.
- **Venues:** counts `journal` (articles) or `publisher` (books). Returns the 5 most frequent.
- **Ministry score average:** collects all numeric `score` values, computes mean rounded to 2 decimal places. Returns `null` if no numeric scores exist.
- **Active years:** sorted list of distinct years seen across all publications.

#### `_resolve_researcher_to_profile(researcher_name="", author_id=None) -> tuple[bool, dict | str]`

The central researcher resolution function. Returns a tuple `(requires_disambiguation, payload)`:

| Result | Meaning |
|---|---|
| `(False, dict)` | Single match; `dict` is the full parsed profile |
| `(True, str)` | Multiple matches; `str` is a JSON disambiguation payload |
| `(False, "")` | No match found |

**Path A — ID given:** Strips the `urn:pw-repo:WUT` prefix if present and fetches the record directly by `id` field. Returns the parsed profile or a minimal stub if the ID is not found.

**Path B — Name only:** Splits into first name and surname, searches the API by surname (up to 50 results), optionally narrows by first name using `_filter_elements_by_first_name`, then parses all matching elements.
- 1 result → returns profile dict
- >1 result → builds and returns a disambiguation JSON payload listing all candidates with `option` numbers, asking the caller to re-invoke with a specific `author_id`

#### `_search_records_by_type(record_type, element_parser_function, researcher, author_id, result_limit) -> str`

Generic publication/thesis search dispatcher used by `handle_search_phd_theses`. Resolves the researcher, then determines which API search fields to use:

**For non-PhD types:** fetches by `author.id` first, falls back to `author.surname` if no results.

**For `phd` type (special logic):** runs author and supervisor searches *additively* in parallel with `asyncio.gather`:
- When a numeric ID is known: `author.id` + `supervisor.id` in parallel
- When only a name is known: `author.surname` + `supervisor.surname` in parallel
- If neither ID search yields results, falls back to surname-based searches

All results pass through an inner `add_unique_record` function that deduplicates by record ID (or title as fallback).

Returns a JSON string `{"count": N, "results": [...]}`.

---

### Section 8 — Tool Implementations

#### `handle_search_people(name, author_id, mode, compare_with, limit, year_from, year_to) -> str`

**`profile` mode:** Calls `_resolve_researcher_to_profile`, returns the profile dict as JSON. No publication fetch.

**`analyze` mode:**
1. Resolves the researcher.
2. Calls `fetch_article_elements_for_researcher` (inner async function) which tries `author.id` first, then `author.surname`, fetching up to 100 articles.
3. Applies `year_from` / `year_to` filter (post-fetch).
4. Calls `_compute_publication_statistics`.
5. Returns `{"found": true, "profile": {...}, "statistics": {...}}`.

**`collaborators` mode:**
1. Resolves the researcher and fetches up to 100 articles.
2. For each article, iterates all `_parse_author_list` entries and counts co-author frequency, excluding the primary researcher's own ID.
3. Returns the top `limit` co-authors ordered by shared publication count.

**`compare` mode:**
1. Resolves both the primary (`name`) and secondary (`compare_with`) researchers.
2. Fetches articles for both in parallel using `asyncio.gather`.
3. Computes `_compute_publication_statistics` independently for each.
4. Returns `{"comparison": [{profile, statistics}, {profile, statistics}]}`.

In all modes, if resolution returns a disambiguation payload, it is forwarded directly to the caller without further processing.

#### `handle_search_publications(researcher, author_id, pub_type, year_from, year_to, year, limit) -> str`

Searches for articles and/or books based on `pub_type`:
- `"article"` → only `("article", parse_article_element)`
- `"book"` → only `("book", parse_book_element)`
- `"all"` → both, sequentially

For each type, tries `author.id` first. If no results and a surname is available, falls back to `author.surname`.

Year filtering is applied **after** fetching because the WUT API has no server-side year parameter. Publications with no extractable year are kept rather than dropped. If `year`/`year_from`/`year_to` are given without a researcher identifier, a clear error message is returned (year-only search is not supported by the API).

Deduplication priority: `id` → `doi` → `title` (uses the first non-empty value as the deduplication key).

#### `handle_search_phd_theses(researcher, author_id, limit) -> str`

Delegates entirely to `_search_records_by_type` with `record_type="phd"` and `element_parser_function=parse_phd_thesis_element`. The key behaviour — searching both author and supervisor paths in parallel — is implemented in `_search_records_by_type`.

---

### Section 9 — Tool Registry

#### `TOOLS: list[Tool]`

A list of three `mcp.types.Tool` objects. Each declares its `name`, `description`, and `inputSchema` (JSON Schema) to the MCP protocol layer so clients can discover and validate tool calls without running the server code. The schemas are the contract between this server and its callers.

#### `_TOOL_NAME_TO_HANDLER_MAP`

A plain dict that maps tool name strings to their async handler functions:
```python
{
    "search_people":       handle_search_people,
    "search_publications": handle_search_publications,
    "search_phd_theses":   handle_search_phd_theses,
}
```
Used by `call_tool` for O(1) dispatch.

---

### Section 10 — MCP Handlers

#### `list_tools() -> list[Tool]`

Registered with `@mcp_server.list_tools()`. Returns the `TOOLS` list verbatim. Called by MCP clients on startup to discover available tools.

#### `call_tool(name: str, arguments: dict) -> list[TextContent]`

Registered with `@mcp_server.call_tool()`. Dispatches incoming tool calls:

1. Looks up the handler in `_TOOL_NAME_TO_HANDLER_MAP`. Unknown names return a JSON error (do not raise).
2. Calls the handler with `**arguments`, catching all exceptions and returning them as JSON error payloads so the MCP session is never terminated by a tool error.
3. Coerces non-string return values to JSON with `json.dumps`.
4. Wraps the result string in `[TextContent(type="text", text=...)]` as required by the MCP protocol.

---

### Section 11 — Dual Transport

#### `_run_stdio_transport() -> None`

Starts the server using `mcp.server.stdio.stdio_server`. An async context manager opens stdin/stdout streams and passes them to `mcp_server.run`. The outer `asyncio.run` starts the event loop. This is the default mode used by Claude Desktop and any subprocess-based MCP client.

#### `_run_sse_transport(http_port_number: int) -> None`

Starts a Starlette ASGI application served by Uvicorn. Three routes are registered:

| Route | Transport | Purpose |
|---|---|---|
| `GET /sse` | SSE | For legacy MCP clients |
| `POST /messages` | Streamable HTTP | For modern MCP clients |
| `GET /health` | — | Liveness probe for Azure App Service |

The `SseServerTransport("/messages")` object handles both the SSE connection endpoint and the POST message handler. Each SSE connection starts its own `mcp_server.run` coroutine with dedicated read/write streams.

#### `main() -> None`

Entry point. Reads `PORT` or `WEBSITES_PORT` from the environment:
- If set → parse as integer (default 8000 on parse error) → `_run_sse_transport`
- If not set → `_run_stdio_transport`

This convention means the same `server.py` file runs locally (stdio) and on Azure App Service (SSE) without any code changes.

---

## Data Flow — End-to-End Example

A call to `search_publications` for `"Jan Kowalski"` follows this path through the code:

```
MCP client sends: call_tool("search_publications", {"researcher": "Jan Kowalski"})
  │
  ▼ call_tool (Section 10)
    looks up "search_publications" in _TOOL_NAME_TO_HANDLER_MAP
    calls handle_search_publications(researcher="Jan Kowalski")
  │
  ▼ handle_search_publications (Section 8)
    calls _resolve_researcher_to_profile("Jan Kowalski")
  │
  ▼ _resolve_researcher_to_profile (Section 7)
    _split_full_name("Jan Kowalski") → ("Jan", "Kowalski")
    search_wut_api("author", "surname", "Kowalski", 50)
  │
  ▼ search_wut_api (Section 5)
    _fetch_all_api_pages("author", "surname", "Kowalski", 50)
      _fetch_api_page(..., offset=0, page_size=25) → cache miss → HTTP GET
      _parse_xml_bytes_to_element → _strip_xml_namespace_prefixes
      returns 12 <author> elements
    returns 12 elements (fewer than 50, so stops after one page)
  │
  ▼ back in _resolve_researcher_to_profile
    _filter_elements_by_first_name([12 elements], "Jan") → 2 matches
    parse_researcher_element × 2
    returns (True, disambiguation_json)   ← 2 matches, user must choose
  │
  ▼ back in handle_search_publications
    returns disambiguation_json directly to caller
  │
  ▼ call_tool wraps in TextContent and returns to MCP client

--- user picks author_id "WUT12345" and calls again ---

call_tool("search_publications", {"researcher": "Jan Kowalski", "author_id": "WUT12345"})
  │
  ▼ _resolve_researcher_to_profile("Jan Kowalski", "WUT12345")
    Path A: strip "urn:pw-repo:WUT" prefix → "12345"
    search_wut_api("author", "id", "12345", 1) → 1 element
    parse_researcher_element → profile dict
    returns (False, {profile})
  │
  ▼ back in handle_search_publications
    researcher_record_id = "12345"
    pub_type = "all" → searches both "article" and "book"
    search_wut_api("article", "author.id", "12345", 25)
      cache hit (same session) → instant
    search_wut_api("book", "author.id", "12345", 25)
    parse_article_element × N, parse_book_element × M
    deduplication → matched_publications
    year filter (not set, skipped)
    returns JSON {"count": N+M, "results": [...]}
```
