"""Save this machine's PipeWire graph, trimmed to what graph.py reads, as the test fixture.

    uv run python scripts/capture_fixture.py [out.json]   (default tests/fixtures/pw-dump.json)
"""
import json, subprocess, sys

KEEP = {"PipeWire:Interface:Node", "PipeWire:Interface:Port", "PipeWire:Interface:Link",
        "PipeWire:Interface:Metadata", "PipeWire:Interface:Device"}
out = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/pw-dump.json"
dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, check=True).stdout)
trimmed = []
for o in dump:
    if o["type"].endswith("Metadata"):  # no "info": props and metadata are top-level
        if (o.get("props") or {}).get("metadata.name") == "settings":
            trimmed.append({k: o[k] for k in ("id", "type", "props", "metadata")})
        continue
    if o["type"] not in KEEP or not o.get("info"):
        continue
    info = {"props": o["info"].get("props", {})}
    if o["type"].endswith("Node"):
        info["params"] = {"Format": [{"rate": f.get("rate")} for f in
                                     (o["info"].get("params") or {}).get("Format", [])]}
    if o["type"].endswith("Link"):
        info.update({k: o["info"][k] for k in ("output-node-id", "output-port-id",
                                               "input-node-id", "input-port-id", "state")})
    if o["type"].endswith("Port"):
        info["direction"] = o["info"].get("direction")
        # (its latency figures: a hardware card's delay, Show latency)
        info["params"] = {"Latency": (o["info"].get("params") or {}).get("Latency", [])}
    trimmed.append({"id": o["id"], "type": o["type"], "info": info})
json.dump(trimmed, open(out, "w"), indent=1)
print(f"{out}: {len(trimmed)} objects")
