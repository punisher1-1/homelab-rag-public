"""Build a curated Docling derivative; never mutate the raw PDF or trust flags
inside an incoming document as permission to discard content.
"""
from __future__ import annotations

import copy


ITEM_ARRAYS = ("texts", "tables", "pictures", "groups", "key_value_items", "form_items")


def reference(value):
    return (value.get("cref") or value.get("$ref")) if isinstance(value, dict) else value


def exclude_pdf_pages(document: dict, pages: list[int]) -> dict:
    if document.get("schema_name") != "DoclingDocument":
        raise ValueError("page exclusion requires a DoclingDocument")
    if not pages or any(type(p) is not int or p < 1 for p in pages):
        raise ValueError("pages must be positive 1-based PDF page numbers")
    excluded = set(pages)
    known = {int(p) for p in document.get("pages", {})}
    items = {f"#/{name}/{i}": item for name in ITEM_ARRAYS
             for i, item in enumerate(document.get(name, []))}
    for item in items.values():
        known.update(p["page_no"] for p in item.get("prov", []) if "page_no" in p)
    if not excluded <= known:
        raise ValueError(f"unknown PDF pages: {sorted(excluded - known)}")

    removed = set()
    for ref, item in items.items():
        provenance = {p.get("page_no") for p in item.get("prov", [])}
        if provenance & excluded:
            if provenance - excluded:
                raise ValueError(f"{ref} spans excluded and retained pages; region review required")
            removed.add(ref)
    # A picture/table/group can own unpaginated children. Do not let them leak
    # into the retained document, or silently discard a retained-page child.
    queue = list(removed)
    while queue:
        for child in items[queue.pop()].get("children", []):
            ref = reference(child)
            if ref in items and ref not in removed:
                provenance = {p.get("page_no") for p in items[ref].get("prov", [])}
                if provenance - excluded:
                    raise ValueError(f"{ref} is retained content inside an excluded parent")
                removed.add(ref)
                queue.append(ref)

    return remove_items(document, removed)


def remove_items(document: dict, removed: set[str]) -> dict:
    """Remove explicitly selected items and rewrite all Docling references."""
    mapping = {}
    result = copy.deepcopy(document)
    for name in ITEM_ARRAYS:
        if name not in result:
            continue
        retained = []
        for i, item in enumerate(result[name]):
            old = f"#/{name}/{i}"
            if old not in removed:
                mapping[old] = f"#/{name}/{len(retained)}"
                retained.append(item)
        result[name] = retained

    def rewrite(value):
        if isinstance(value, dict):
            if reference(value) in removed:
                return None
            return {k: rewrite(v) for k, v in value.items()
                    if not (isinstance(v, dict) and reference(v) in removed)}
        if isinstance(value, list):
            return [rewrite(v) for v in value
                    if not (isinstance(v, dict) and reference(v) in removed)]
        if isinstance(value, str):
            return mapping.get(value, value)
        return value

    result = rewrite(result)
    # Keep the original page map, including PDF indices and printed footers.
    # Only content items are removed; page numbers are never renumbered.
    if not any(result.get(name) for name in ("texts", "tables", "pictures")):
        raise ValueError("exclusion would remove all document content")
    return result
