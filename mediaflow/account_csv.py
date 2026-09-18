from __future__ import annotations

import csv
from pathlib import Path


def promotion_keywords(path: Path, account_name: str) -> list[str]:
    """Load the enabled promotion account's manually reviewed search terms."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    row = next((item for item in rows if item.get("账号") == account_name), None)
    if not row:
        raise RuntimeError(f"account {account_name} is missing from {path}")
    if row.get("角色") != "推广" or row.get("启用状态") != "启用":
        raise RuntimeError(f"account {account_name} is not an enabled promotion account in {path}")
    keywords = [
        keyword.strip()
        for keyword in row.get("搜索关键词", "").replace("，", ",").split(",")
        if keyword.strip()
    ]
    if not keywords:
        raise RuntimeError(f"account {account_name} has no 搜索关键词 in {path}")
    return keywords


def promotion_links(path: Path) -> list[str]:
    """Return the configured links for enabled promotion accounts, without duplicates."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = csv.DictReader(file)
        links = [
            row.get("推广链接", "").strip()
            for row in rows
            if row.get("角色") == "推广" and row.get("启用状态") == "启用"
            and row.get("推广链接", "").strip()
        ]
    return list(dict.fromkeys(links))
