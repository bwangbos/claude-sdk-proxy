"""Native caller-tool namespace shared by registration, replay, and validation."""

# Keep protocol versioning in MCP server metadata, not in model-visible names.
SDK_MCP_SERVER_NAME = "caller_tools"
SDK_TOOL_PREFIX = f"mcp__{SDK_MCP_SERVER_NAME}__"
