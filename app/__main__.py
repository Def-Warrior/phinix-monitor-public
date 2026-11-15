"""Local setup, safe backup, export, and server entrypoint."""

import argparse
import json
import secrets
from pathlib import Path

from app.config import Settings
from app.storage import Database


def main():
    parser = argparse.ArgumentParser(description="Phinix Monitor")
    parser.add_argument("command", choices=["init", "serve", "backup", "export"])
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    if args.command == "init":
        target = Path(".env")
        source = Path(".env.example")
        if target.exists():
            parser.error(".env already exists; edit it instead of overwriting it")
        content = source.read_text(encoding="utf-8").replace(
            "ADMIN_TOKEN=\n", f"ADMIN_TOKEN={secrets.token_urlsafe(36)}\n"
        )
        with target.open("x", encoding="utf-8") as file:
            file.write(content)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        print("Created .env with an admin token. Edit .env locally to add credentials; never commit it.")
        return
    if args.command == "serve":
        import uvicorn

        from app.main import create_app

        uvicorn.run(
            create_app(Settings(local_only_access=True)),
            host="127.0.0.1",
            port=8000,
            workers=1,
            access_log=False,
            proxy_headers=False,
        )
        return
    if not args.destination:
        parser.error("A destination path is required")
    destination = Path(args.destination)
    if destination.exists():
        parser.error("Destination exists; choose a new path")
    cfg = Settings()
    db = Database(cfg.database_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "backup":
        db.backup(destination)
    else:
        with db.connection() as conn:
            events = [
                json.loads(r[0])
                for r in conn.execute("SELECT payload FROM events ORDER BY provider,collection,external_id")
            ]
        with destination.open("x", encoding="utf-8") as file:
            json.dump({"format": "phinix-events-v1", "events": events}, file, indent=2)
    print("Completed. Keep this output private; it may contain authorized report metadata.")


if __name__ == "__main__":
    main()
