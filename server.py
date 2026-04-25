import sys
import traceback

from fastmcp import FastMCP

try:
    from .entry_prompt import ssb_entry_prompt
    from .tools import (
        ssb_browse_folders,
        ssb_check_usage,
        ssb_find_region_code,
        ssb_get_api_status,
        ssb_get_table_data,
        ssb_get_table_info,
        ssb_get_table_variables,
        ssb_preview_data,
        ssb_search_regions,
        ssb_search_tables,
        ssb_test_selection,
    )
except ImportError:
    from entry_prompt import ssb_entry_prompt
    from tools import (
        ssb_browse_folders,
        ssb_check_usage,
        ssb_find_region_code,
        ssb_get_api_status,
        ssb_get_table_data,
        ssb_get_table_info,
        ssb_get_table_variables,
        ssb_preview_data,
        ssb_search_regions,
        ssb_search_tables,
        ssb_test_selection,
    )

mcp: FastMCP = FastMCP("SSBServer")


mcp.tool()(ssb_get_api_status)
mcp.tool()(ssb_browse_folders)
mcp.tool()(ssb_search_tables)
mcp.tool()(ssb_get_table_info)
mcp.tool()(ssb_get_table_data)
mcp.tool()(ssb_check_usage)
mcp.tool()(ssb_search_regions)
mcp.tool()(ssb_get_table_variables)
mcp.tool()(ssb_find_region_code)
mcp.tool()(ssb_test_selection)
mcp.tool()(ssb_preview_data)


mcp.prompt()(ssb_entry_prompt)


def main():
    print("[SSB MCP] Starting SSBServer on streamable-http...", file=sys.stderr)
    try:
        mcp.run(transport="streamable-http")
        print("[SSB MCP] Finished", file=sys.stderr)
    except Exception as e:
        print(f"[SSB MCP] Error {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        print("[SSB MCP] Exiting...", file=sys.stderr)


if __name__ == "__main__":
    main()
