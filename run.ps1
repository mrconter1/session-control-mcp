# Start the session-control MCP server in the foreground. Ctrl+C to stop.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m session_control.server
