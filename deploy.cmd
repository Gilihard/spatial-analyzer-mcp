@echo off
rem Build + deploy the SA MCP server into the ZCode config (workspace scope).
rem Double-click or run from cmd. Equivalent to: python build_mcp.py deploy
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 build_mcp.py deploy %*
echo.
pause
