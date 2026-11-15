import json
import sys

import pytest

from app.__main__ import main


def test_init_generates_private_config_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("ADMIN_TOKEN=\n")
    monkeypatch.setattr(sys, "argv", ["phinix", "init"])
    main()
    result = (tmp_path / ".env").read_text()
    assert result.startswith("ADMIN_TOKEN=") and len(result.strip().split("=")[1]) >= 32
    with pytest.raises(SystemExit):
        main()


@pytest.mark.parametrize("command", ["backup", "export"])
def test_cli_archive_commands(command, db, settings, event, tmp_path, monkeypatch):
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    monkeypatch.setattr("app.__main__.Settings", lambda: settings)
    destination = tmp_path / f"{command}.out"
    monkeypatch.setattr(sys, "argv", ["phinix", command, str(destination)])
    main()
    assert destination.exists()
    if command == "export":
        assert json.loads(destination.read_text())["events"][0]["external_id"] == "1"
    with pytest.raises(SystemExit):
        main()


def test_cli_requires_destination(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["phinix", "backup"])
    with pytest.raises(SystemExit):
        main()
