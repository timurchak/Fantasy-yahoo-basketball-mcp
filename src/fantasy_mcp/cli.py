import argparse
import asyncio
import getpass
import json
import logging
import sys
import webbrowser
from pathlib import Path

from fantasy_mcp.config import Config
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.oauth import OAuth
from fantasy_mcp.server import build_service, create_server


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "event": record.getMessage()}
        for key in ("operation", "status", "elapsed_ms", "attempt", "error_type"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


def logging_setup(debug: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("fantasy_mcp")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


async def execute(args: argparse.Namespace, config: Config) -> int:
    service, http = build_service(config)
    try:
        match args.command:
            case "auth":
                oauth = OAuth(config, http)
                url, state = oauth.authorization_url()
                print("Open this Yahoo authorization URL in your browser:\n" + url)
                webbrowser.open(url)
                print(
                    "Approve access. The localhost page may fail to load; copy the FULL final URL."
                )
                callback = getpass.getpass("Paste callback URL (hidden): ")
                await oauth.finish(callback, state)
                service.store.clear_remote_cache()
                print("Authorization saved. Run fantasy-mcp doctor --live.")
                return 0
            case "doctor":
                result = await service.doctor(args.live)
                print(result.model_dump_json(indent=2))
                failed = any(
                    isinstance(v, dict) and (not v.get("ok", True) or v.get("stale"))
                    for v in result.data.values()
                )
                return 1 if failed else 0
            case "leagues":
                print((await service.repo.leagues(True)).model_dump_json(indent=2))
            case "sync":
                print((await service.sync()).model_dump_json(indent=2))
            case "draft":
                print((await service.draft_state(args.draft_id)).model_dump_json(indent=2))
            case _:
                raise FantasyError("INVALID_COMMAND", "Use --help to list supported commands.")
    finally:
        await http.aclose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Yahoo Fantasy Basketball Intelligence MCP")
    parser.add_argument("--env-file", type=Path, help="Absolute .env path; useful for MCP clients")
    parser.add_argument("--debug", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--live", action="store_true")
    commands.add_parser("leagues")
    commands.add_parser("sync")
    commands.add_parser("serve")
    draft = commands.add_parser("draft")
    draft.add_argument("action", choices=["status"])
    draft.add_argument("--draft-id")
    args = parser.parse_args()
    config = Config(_env_file=args.env_file) if args.env_file else Config()  # type: ignore[call-arg]
    logging_setup(args.debug or config.debug)
    try:
        if args.command == "serve":
            create_server(config=config).run(transport="stdio", show_banner=False)
        else:
            sys.exit(asyncio.run(execute(args, config)))
    except FantasyError as error:
        print(json.dumps({"code": error.code, "message": error.message}), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)


if __name__ == "__main__":
    main()
