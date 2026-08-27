# FarmVibes.AI MCP server

The `farmvibes-mcp` package exposes FarmVibes.AI workflows and runs to Model
Context Protocol clients over stdio.

Install the FarmVibes.AI client and MCP server from this repository:

```bash
pip install ./src/vibe_core ./src/farmvibes_mcp
```

Then configure your MCP client to launch:

```json
{
  "mcpServers": {
    "farmvibes": {
      "command": "farmvibes-mcp"
    }
  }
}
```

The server uses the same target discovery as the Python client. It connects to
the configured remote cluster and its private bearer token when
`remote_service_url` exists; otherwise it uses the configured local cluster or
the localhost fallback. Tokens are never MCP tool arguments or results.

The server provides seven tools:

- `list_workflows`
- `describe_workflow`
- `submit_run`
- `list_runs`
- `get_run`
- `get_run_output`
- `cancel_run`

`submit_run` accepts a GeoJSON geometry plus timezone-aware ISO 8601 start and
end timestamps. Run submission is nonblocking; use `get_run` and
`get_run_output` to inspect progress and results.

This initial server supports stdio only. It does not expose prompts, resources,
asset downloads, or a blocking wait tool.
