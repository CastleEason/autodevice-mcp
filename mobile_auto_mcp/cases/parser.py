"""Heuristic Markdown testcase parser for abnormal response rules."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mobile_auto_mcp.state.storage import LocalStore


ACTION_ALIASES = {
    "不存在": "missing",
    "缺失": "missing",
    "为空": "empty",
    "空值": "empty",
    "空串": "empty",
    "空数组": "empty_array",
    "空列表": "empty_array",
    "空对象": "empty_object",
    "超长": "long_text",
    "emoji": "emoji",
    "特殊字符": "special_char",
    "图片不可达": "image_unreachable",
    "图片异常": "image_unreachable",
}


def import_case_file(store: LocalStore, case_file: str, knowledge_dir: str = "") -> dict[str, Any]:
    """Handle import case file using the supplied state and inputs."""
    path = Path(case_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(str(path))
    text = path.read_text(encoding="utf-8")
    rules = extract_rules_from_markdown(text, source_file=str(path), knowledge_dir=knowledge_dir)
    saved = store.save_rules(rules) if rules else []
    return {"case_file": str(path), "rules": len(rules), "saved": saved}


def extract_rules_from_markdown(text: str, source_file: str = "", knowledge_dir: str = "") -> list[dict[str, Any]]:
    """Extract rules from markdown using the supplied state and inputs."""
    contracts = _extract_request_contracts(text)
    apis = [contract["api"] for contract in contracts]
    rules: list[dict[str, Any]] = []
    current_feature = "未分组需求"
    for source_line, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading_level = len(stripped) - len(stripped.lstrip("#"))
            if heading_level < 4:
                current_feature = stripped.lstrip("#").strip() or current_feature
                continue
        if not _is_case_definition(stripped):
            continue
        mutation = _extract_mutation(stripped)
        if mutation:
            case_name = _case_name(stripped, mutation)
            contract = _choose_request_contract(stripped, contracts)
            api = str(contract.get("api") or "")
            rules.append(
                {
                    "id": _stable_rule_id(source_file, api, mutation, case_name),
                    "case_name": case_name,
                    "api": api,
                    "host": str(contract.get("host") or ""),
                    "method": str(contract.get("method") or ""),
                    "mutations": [mutation],
                    "expected": _expected(stripped),
                    "source_feature": current_feature,
                    "source_module": "",
                    "source_file": source_file,
                    "source_line": source_line,
                    "knowledge_dir": knowledge_dir,
                    "enabled": True,
                }
            )
    if not rules and apis:
        contract = contracts[0]
        rules.append(
            {
                "case_name": "目标接口连通性验证",
                "api": apis[0],
                "host": str(contract.get("host") or ""),
                "method": str(contract.get("method") or ""),
                "mutations": [],
                "expected": "目标接口可被代理捕获",
                "source_feature": current_feature,
                "source_file": source_file,
                "knowledge_dir": knowledge_dir,
                "enabled": True,
            }
        )
    return rules


def _is_case_definition(line: str) -> bool:
    """Return whether case definition using the supplied state and inputs."""
    if line.startswith("####"):
        return True
    if not re.match(r"^[-*]\s+", line):
        return False
    content = re.sub(r"^[-*]\s+", "", line).strip()
    return not re.match(r"^(前置条件|操作步骤\d*|预期结果|期望结果)[:：]", content)


def _stable_rule_id(source_file: str, api: str, mutation: dict[str, Any], case_name: str) -> str:
    """Handle stable rule id using the supplied state and inputs."""
    normalized = "|".join(
        (
            str(Path(source_file).expanduser()) if source_file else "",
            api.strip(),
            str(mutation.get("field") or "").strip(),
            str(mutation.get("action") or "").strip(),
            " ".join(case_name.split()),
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def analyze_case_file(case_file: str) -> dict[str, Any]:
    """Handle analyze case file using the supplied state and inputs."""
    path = Path(case_file).expanduser()
    text = path.read_text(encoding="utf-8")
    rules = extract_rules_from_markdown(text, source_file=str(path))
    return {"case_file": str(path), "rules": len(rules), "apis": _extract_apis(text), "preview": rules[:10]}


def _extract_apis(text: str) -> list[str]:
    """Extract de-duplicated full URLs or paths without discarding request hosts."""
    return [contract["api"] for contract in _extract_request_contracts(text)]


def _extract_request_contracts(text: str) -> list[dict[str, str]]:
    """Extract API, exact host, and HTTP method from each Markdown line."""
    contracts: list[dict[str, str]] = []
    url_pattern = r"https?://[^\s`|，。；；)）<>\"]+"
    path_pattern = r"/(?:api|json|data|v\d+)[^\s`|，。；；)）<>\"]+"
    for line in text.splitlines():
        method_match = re.search(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", line, flags=re.IGNORECASE)
        method = method_match.group(1).upper() if method_match else ""
        candidates = re.findall(url_pattern, line, flags=re.IGNORECASE)
        if not candidates:
            candidates = re.findall(path_pattern, line)
        for item in candidates:
            api = item.rstrip(".,;，。；")
            parts = urlsplit(api)
            contract = {"api": api, "host": str(parts.hostname or ""), "method": method}
            if contract not in contracts:
                contracts.append(contract)
    return contracts


def _extract_mutation(line: str) -> dict[str, Any] | None:
    """Extract mutation using the supplied state and inputs."""
    action = ""
    for keyword, mapped in ACTION_ALIASES.items():
        if keyword in line.lower() or keyword in line:
            action = mapped
            break
    if not action:
        return None
    field = _extract_field(line)
    if not field:
        return None
    return {"field": field, "action": action, "params": {}}


def _extract_field(line: str) -> str:
    """Extract field using the supplied state and inputs."""
    for pattern in (r"`([^`]+)`", r"字段[:：]\s*([A-Za-z0-9_.\[\]-]+)", r"\|\s*([A-Za-z0-9_.\[\]-]+)\s*\|"):
        match = re.search(pattern, line)
        if match:
            return match.group(1).strip()
    match = re.search(r"\b([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+|\[\d+\])*)\b", line)
    return match.group(1) if match else ""


def _choose_api(line: str, apis: list[str]) -> str:
    """Handle choose api using the supplied state and inputs."""
    for api in apis:
        if api in line:
            return api
    return apis[0] if apis else ""


def _choose_request_contract(line: str, contracts: list[dict[str, str]]) -> dict[str, str]:
    """Choose the line-local contract or fall back to the first declared request contract."""
    for contract in contracts:
        if contract["api"] in line:
            return dict(contract)
    return dict(contracts[0]) if contracts else {"api": "", "host": "", "method": ""}


def _case_name(line: str, mutation: dict[str, Any]) -> str:
    """Handle case name using the supplied state and inputs."""
    value = re.sub(r"^\|?[-*\d.\s]+", "", line).strip(" |")
    return value[:80] or f"{mutation['field']} {mutation['action']}"


def _expected(line: str) -> str:
    """Handle expected using the supplied state and inputs."""
    parts = re.split(r"期望|预期|expected", line, flags=re.IGNORECASE)
    return parts[-1].strip(" ：:|")[:120] if len(parts) > 1 else "页面容错正常，无崩溃、无异常空白"
