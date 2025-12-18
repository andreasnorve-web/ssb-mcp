# SSB MCP Server

An MCP (Model Context Protocol) server that provides access to Statistics Norway (Statistisk sentralbyrå / SSB) PxWebApi v2 for querying Norwegian official statistics.

## What it does

This server exposes tools for exploring and querying Norwegian statistics data, including:

- Virtual folder navigation of the SSB database structure
- Table search with keyword and category filtering
- Table metadata and variable exploration
- Data retrieval with selection validation
- Region code resolution for municipalities and counties
- Rate limit monitoring and usage tracking

## Running locally

```bash
pip install -r requirements.txt
python server.py
```

The server runs on port 8007 using streamable HTTP transport.

## Docker

```bash
docker build -t ssb-mcp .
docker run -p 8007:8007 ssb-mcp
```

Or with docker-compose:

```bash
docker-compose up
```

## Available tools

| Tool | Description |
|------|-------------|
| `ssb_get_api_status` | Get API configuration and current rate limit status |
| `ssb_browse_folders` | Browse the SSB database folder structure (virtual tree from table paths) |
| `ssb_search_tables` | Search for tables with optional filters (keywords, category, recency) |
| `ssb_get_table_info` | Get detailed metadata for a specific table |
| `ssb_get_table_variables` | List variables and sample values for a table |
| `ssb_get_table_data` | Retrieve table data with validated selection |
| `ssb_preview_data` | Fetch a safe, limited preview of data before full query |
| `ssb_test_selection` | Validate a selection against table metadata without fetching data |
| `ssb_find_region_code` | Resolve municipality/area names to SSB region codes |
| `ssb_search_regions` | Find region-related tables to guide code discovery |
| `ssb_check_usage` | Inspect current client-side rate limit window usage |

## Data source

All data comes from the public Statistics Norway API at https://data.ssb.no/api/pxwebapi/v2. No authentication required.

