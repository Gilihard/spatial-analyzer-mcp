"""Build + deploy helper for the SA MCP server (ZCode / MCP stdio).

Automates the dev loop for a server that is still being worked on:

    python build_mcp.py            # validate, register in ZCode config, tips
    python build_mcp.py build      # validate only: compile all .py + MCP probe
    python build_mcp.py deploy     # same as default
    python build_mcp.py start      # run server.py in foreground (debug)

Validation never touches the SA COM engine: importing server.py is COM-free
(the bridge is created lazily on the first tool call), and the probe only
lists tools over MCP stdio. Registration writes an entry pointing straight at
<python.exe> + <project>\\server.py, so ZCode always launches the current
code as a fresh process.

Scope of the config entry:
  --user (default)       ~/.zcode/cli/config.json        - EVERY project/session
  --workspace            <project>\\.zcode\\config.json  - only when this folder
                         is the opened project in ZCode

Workspace scope loads only when that folder is the active workspace, so after
a restart ZCode may open another project and the server is gone. Register in
user scope when the tools must be available regardless of the open folder.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SERVER_PY = PROJECT_DIR / "server.py"
SERVER_NAME = "spatial-analyzer"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def say(msg=""):
    print(msg)


def die(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def check_python():
    if not SERVER_PY.exists():
        die(f"server.py not found next to build_mcp.py: {SERVER_PY}")
    if not (PROJECT_DIR / "sa_sdk.py").exists() or not (PROJECT_DIR / "sa_app.py").exists():
        die("sa_sdk.py / sa_app.py missing - run this from the project folder")


def compile_sources():
    """Byte-compile every top-level .py so syntax errors surface before ZCode."""
    files = sorted(p for p in PROJECT_DIR.glob("*.py"))
    ok = True
    for path in files:
        try:
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as exc:
            ok = False
            print(f"  [FAIL] syntax {path.name}:{exc.lineno}: {exc.msg}")
    if not ok:
        die("syntax errors found - fix them and rerun")
    print(f"[OK] syntax: {len(files)} python files")
    return files


def probe_server():
    """Start server.py over MCP stdio and list its tools. COM-free."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        die(f"mcp package not importable under {sys.executable}: {exc}")

    import asyncio

    async def _probe():
        params = StdioServerParameters(
            command=str(sys.executable),
            args=[str(SERVER_PY)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    try:
        names = asyncio.run(asyncio.wait_for(_probe(), timeout=60))
    except Exception as exc:  # noqa: BLE001
        die(f"MCP probe failed - ZCode would not connect either:\n  {exc}")
    expected = ("sa_status", "sa_run_step")
    missing = [n for n in expected if n not in names]
    if missing:
        die(f"probe ok but core tools missing: {missing}")
    print(f"[OK] MCP probe: server '{SERVER_NAME}' up, {len(names)} tools "
          f"listed (first: {', '.join(names[:5])}...)")
    return names


def server_entry():
    return {
        "type": "stdio",
        "command": str(sys.executable),
        "args": [str(SERVER_PY)],
        "env": {"PYTHONUNBUFFERED": "1"},
        "enabled": True,
    }


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def register(scope):
    if scope == "workspace":
        path = PROJECT_DIR / ".zcode" / "config.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("mcp", {}).setdefault("servers", {})[SERVER_NAME] = server_entry()
        write_json(path, data)
        where = "workspace (this folder)"
    else:
        path = Path.home() / ".zcode" / "cli" / "config.json"
        if not path.exists():
            die(f"user ZCode config not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("mcp", {}).setdefault("servers", {})[SERVER_NAME] = server_entry()
        write_json(path, data)
        where = "user scope (all projects)"
    print(f"[OK] registered '{SERVER_NAME}' in {where}: {path}")
    return path


def print_next_steps(scope):
    if scope == "workspace":
        activate = ("Откройте эту папку как проект в ZCode (или начните новую "
                    "сессию в ней) - сервер подключится автоматически.")
    else:
        activate = "Перезапустите сессию ZCode - сервер подключится автоматически."
    print("""
[Как подхватить в ZCode]
  1. {activate}
  2. Либо вручную: Settings -> MCP -> spatial-analyzer -> Connect.
  3. Проверка: инструменты sa_status, sa_connect, sa_run_step, sa_best_fit_*.

[Цикл разработки: изменили код ->]
  python build_mcp.py
  ... затем перезапустите MCP-сервер в ZCode (Settings -> MCP -> Reload /
  новая сессия): код загружается в процесс при старте, работающий сервер
  старую версию не подхватит.

[Ручной запуск / отладка]
  python server.py                 (stdio-сервер, ждёт клиента)
  python build_mcp.py start        (то же самое)
  npx -y @modelcontextprotocol/inspector python server.py

Внимание: server.py не создаёт SA-движок при импорте. Реальный COM
(движок SpatialAnalyzerSDK.exe) поднимается только при первом вызове
инструмента и только если окно SA видимо (см. AGENTS.md).""".format(
        activate=activate))


def cmd_build(args):
    compile_sources()
    if not args.no_probe:
        probe_server()
    return 0


def cmd_deploy(args):
    compile_sources()
    if not args.no_probe:
        probe_server()
    path = register(args.scope)
    print(f"\nКонфиг записан: {path}")
    print_next_steps(args.scope)
    return 0


def cmd_start(args):
    check_python()
    print(f"Starting SA MCP server (stdio): {SERVER_PY}\n"
          "Press Ctrl+C to stop.", flush=True)
    try:
        # subprocess.call (not os.execv): execv on Windows does not quote
        # argv, so a path with spaces ('desktop 2026') gets truncated.
        rc = subprocess.call([str(sys.executable), str(SERVER_PY)])
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="deploy",
                        choices=["build", "deploy", "start"],
                        help="deploy = build + register in ZCode config (default)")
    parser.add_argument("--scope", choices=["workspace", "user"],
                        default="user",
                        help="where to write the ZCode MCP entry "
                             "(default: user = every project)")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the live MCP tools/list probe")
    args = parser.parse_args()

    check_python()
    try:
        if args.command == "start":
            return cmd_start(args)
        if args.command == "build":
            return cmd_build(args)
        return cmd_deploy(args)
    except KeyboardInterrupt:
        die("interrupted")


if __name__ == "__main__":
    main()
