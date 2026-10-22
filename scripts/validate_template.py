import json
from pathlib import Path

template_path = Path(__file__).resolve().parent.parent / "infra" / "template.json"
with open(template_path, "r", encoding="utf-8") as f:
    t = json.load(f)

assert "$schema" in t
assert "resources" in t
assert t["resources"][0]["type"] == "Microsoft.App/containerApps"
print(f"ARM template valid! Target type: {t['resources'][0]['type']}")
print(f"Containers: {len(t['resources'][0]['properties']['template']['containers'])}")
print(f"Environment variables configured: {len(t['resources'][0]['properties']['template']['containers'][0]['env'])}")

