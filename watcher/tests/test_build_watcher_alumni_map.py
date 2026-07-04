import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_watcher_alumni_map.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_watcher_alumni_map", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_watcher_alumni_map_outputs_only_watchlist_matches(tmp_path, capsys):
    csv_path = tmp_path / "alumni.csv"
    csv_path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Bosch,Software Engineer,Bosch Group,https://www.linkedin.com/in/fake-bosch
Nikola,Tesla,Software Engineer,Tesla Motors,https://www.linkedin.com/in/fake-tesla
Uma,Other,Engineer,Unwatched Co,https://www.linkedin.com/in/fake-other
""",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yml"
    watchlist_path.write_text(
        """defaults:
  target_roles: ["swe"]
companies:
  - name: "Bosch"
    ats: github_only
    aliases: ["Bosch Group"]
  - name: "Tesla"
    ats: github_only
    aliases: ["Tesla Motors"]
""",
        encoding="utf-8",
    )
    out_path = tmp_path / "company_alumni.json"

    module = _load_script()
    rc = module.main(["--csv", str(csv_path), "--watchlist", str(watchlist_path), "--out", str(out_path)])

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert set(payload) == {"bosch", "tesla"}
    assert payload["bosch"][0]["name"] == "Ada Bosch"
    assert payload["tesla"][0]["name"] == "Nikola Tesla"
    assert "Unwatched" not in json.dumps(payload)
    output = capsys.readouterr().out
    assert "Wrote 2 alumni record(s)." in output
    assert "Companies with alumni: 2." in output
    assert "Watchlist companies checked: 2." in output
