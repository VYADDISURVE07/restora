"""Tiny JSON path helpers: 'customer.email', 'items[0].qty', '$.data.id'."""
from __future__ import annotations

import re
from typing import Any, Iterator

_TOKEN = re.compile(r"\[(\d+)\]|\[['\"]([^'\"]+)['\"]\]|([^.\[\]]+)")


def parse(path: str) -> list:
    """'$.a.b[0]["c d"]' -> ['a', 'b', 0, 'c d']"""
    path = (path or "").strip()
    if path.startswith("$"):
        path = path[1:]
    out: list = []
    for m in _TOKEN.finditer(path):
        idx, quoted, plain = m.groups()
        if idx is not None:
            out.append(int(idx))
        elif quoted is not None:
            out.append(quoted)
        elif plain is not None and plain.strip():
            out.append(plain.strip())
    return out


def fmt(parts: list) -> str:
    s = "$"
    for p in parts:
        if isinstance(p, int):
            s += f"[{p}]"
        elif re.fullmatch(r"[A-Za-z_][\w\-]*", str(p)):
            s += f".{p}"
        else:
            s += f'["{p}"]'
    return s


def get(obj: Any, parts: list, default: Any = None) -> Any:
    cur = obj
    for p in parts:
        if isinstance(p, int) and isinstance(cur, list) and -len(cur) <= p < len(cur):
            cur = cur[p]
        elif isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, dict) and isinstance(p, str):
            match = next((k for k in cur if str(k).lower() == p.lower()), None)   # forgiving about case
            if match is None:
                return default
            cur = cur[match]
        else:
            return default
    return cur


def exists(obj: Any, parts: list) -> bool:
    marker = object()
    return get(obj, parts, marker) is not marker


def put(obj: Any, parts: list, value: Any) -> None:
    """Set a value, creating objects on the way when needed."""
    if not parts:
        raise ValueError("empty path")
    cur = obj
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(cur, dict):
            if p not in cur or not isinstance(cur[p], (dict, list)):
                cur[p] = [] if isinstance(nxt, int) else {}
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int):
            while len(cur) <= p:
                cur.append({})
            cur = cur[p]
        else:
            raise ValueError(f"cannot walk into {type(cur).__name__} at {fmt(parts[:i + 1])}")
    last = parts[-1]
    if isinstance(cur, dict):
        existing = next((k for k in cur if isinstance(last, str) and str(k).lower() == last.lower()), last)
        cur[existing] = value
    elif isinstance(cur, list) and isinstance(last, int):
        while len(cur) <= last:
            cur.append(None)
        cur[last] = value
    else:
        raise ValueError(f"cannot set {fmt(parts)}")


def delete(obj: Any, parts: list) -> bool:
    parent = get(obj, parts[:-1]) if len(parts) > 1 else obj
    last = parts[-1] if parts else None
    if isinstance(parent, dict):
        key = next((k for k in parent if str(k).lower() == str(last).lower()), None)
        if key is not None:
            del parent[key]
            return True
    if isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
        parent.pop(last)
        return True
    return False


def leaves(obj: Any, prefix: list | None = None, limit: int = 2000) -> Iterator[tuple]:
    """Yield (parts, key, value) for every scalar in a JSON document (depth-first, capped)."""
    prefix = prefix or []
    count = 0
    stack = [(prefix, None, obj)]
    while stack and count < limit:
        parts, key, value = stack.pop(0)
        if isinstance(value, dict):
            stack[0:0] = [(parts + [k], k, v) for k, v in value.items()]
        elif isinstance(value, list):
            stack[0:0] = [(parts + [i], key, v) for i, v in enumerate(value[:50])]
        else:
            count += 1
            yield parts, (key if key is not None else ""), value


def find_key(obj: Any, name: str, match) -> list | None:
    """Path of the first key in obj whose name satisfies match(key) (breadth-first)."""
    queue = [([], obj)]
    while queue:
        parts, cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if match(k):
                    return parts + [k]
                queue.append((parts + [k], v))
        elif isinstance(cur, list):
            queue.extend((parts + [i], v) for i, v in enumerate(cur[:20]))
    return None
