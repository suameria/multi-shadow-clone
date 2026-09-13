"""Deterministic walkthrough only; deliberately not an AI or quality benchmark."""

import json

from ..ports import Request, Result


class OfflineProvider:
    def contract(self):
        return {"kind": "offline-fixture", "version": 1}

    def preflight(self) -> None:
        pass

    def execute(self, request: Request) -> Result:
        data = json.loads(request.prompt.split("\nDATA_JSON\n", 1)[1])
        node = data["contract"]["node"]
        if request.stage == "audit":
            return Result("completed", {"approved": True, "reason": "Offline walkthrough audit fixture.", "defects": []})
        values = {}
        if request.node_id == "mainPlanner":
            values["nodes"] = [{"id": "summary", "role_id": "R07", "instruction": "Summarize the supplied synthetic material.",
                                "dependencies": [], "source_ids": [s for s in node["source_ids"] if s != "kagebunshinRoleIndex"]}]
        for rule in node["rules"]:
            if rule["kind"] == "equals" and rule["path"].startswith("values."):
                values[rule["path"].split(".", 1)[1]] = rule["expected"]
        return Result("completed", {"text": "オフラインの動作例。AIの生成・専門能力の証拠ではありません。",
                                     "source_ids": node["source_ids"], "limits": ["fixture"], "values": values})

    def reconcile(self, attempt: dict) -> Result | None:
        return None
