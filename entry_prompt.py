def ssb_entry_prompt() -> str:
    return (
        """## SSB FastMCP Server
Access Statistics Norway's PxWebApi 2 with a focused toolset and virtual folder browsing.

**WHAT'S INCLUDED:**
- **Virtual Folder Navigation**: `ssb_browse_folders(folderId?)` browses a reconstructed tree built from `/tables` `paths` (no `/navigation`). Root `folderId` is `"root"`; other IDs are stable hashes of path segments.
- **Search**: `ssb_search_tables(query, ...)` to find relevant tables; output is already formatted as `## Sheet: ... ##` text that slots into spreadsheets.
- **Metadata**: `ssb_get_table_info(tableId)` and `ssb_get_table_variables(tableId, variableName?)`.
- **Selection Helpers**: `ssb_test_selection(tableId, selection)` auto-translates common English/Norwegian terms (region->Region, age->Alder, gender->Kjonn) and validates values.
- **Preview & Data**: `ssb_preview_data(tableId, selection?)` emits the same `## Sheet` sections with a limited dataset plus preview notes; `ssb_get_table_data(tableId, selection?)` returns the full export.
- **Utilities**: `ssb_find_region_code(query, tableId?)` to resolve geographic codes; `ssb_check_usage()` and `ssb_get_api_status()` for limits and API reachability.

**RECOMMENDED WORKFLOW:**
1. **Discover**: `ssb_browse_folders()` (virtual tree) or `ssb_search_tables("your query")`.
2. **Explore**: `ssb_get_table_info(tableId)` -> `ssb_get_table_variables(tableId)`.
3. **Validate**: `ssb_test_selection(tableId, selection)`.
4. **Preview**: `ssb_preview_data(tableId, selection)` to verify using the smaller sheet output.
5. **Fetch**: `ssb_get_table_data(tableId, selection)` for the full multi-sheet export.

**NOTES:**
- Folder structure depends on language; IDs are language-specific.
- SSB limits: 800,000 cells per extract; 30 queries per minute (per IP).
- Query expressions like `TOP(n)`, `BOTTOM(n)`, `FROM(x)`, `[RANGE(x,y)]`, `*`, and `??` are supported; values are normalized for the API.

Selection examples:
- {'Region': ['0301'], 'Tid': ['top(1)']}  # Oslo, latest period
- {'Region': ['*'], 'Tid': ['TOP(3)']}     # All regions, top 3 periods
- {'Alder': ['tot'], 'Kjonn': ['1','2']}   # Total age, both genders
"""
    )
