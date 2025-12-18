import hashlib
import json
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from .config import BASE_URL, DEFAULT_LANGUAGE, REQUEST_TIMEOUT_SECONDS, USER_AGENT
except ImportError:
    from config import BASE_URL, DEFAULT_LANGUAGE, REQUEST_TIMEOUT_SECONDS, USER_AGENT


class MemoryCache:
    """Simple in-memory cache with TTL support"""

    def __init__(self, default_ttl: int = 300) -> None:  # 5 minutes default
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            value, expires_at = self.cache[key]
            if time.time() < expires_at:
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = time.time() + (ttl or self.default_ttl)
        self.cache[key] = (value, expires_at)

    def clear(self) -> None:
        self.cache.clear()


class RateLimiter:
    def __init__(self, max_calls: int = 30, window_seconds: int = 60) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls_made = 0
        self.window_start = time.time()
        self.remaining = max_calls
        self.reset_time = self.window_start + window_seconds

    def reset_window_if_needed(self) -> None:
        now = time.time()
        if now >= self.reset_time:
            self.calls_made = 0
            self.window_start = now
            self.reset_time = now + self.window_seconds
            self.remaining = self.max_calls

    def check(self) -> None:
        self.reset_window_if_needed()
        if self.calls_made >= self.max_calls:
            wait_sec = int(max(0, self.reset_time - time.time()))
            raise RuntimeError(
                f"Rate limit exceeded. Try again in {wait_sec} seconds. Usage: {self.calls_made}/{self.max_calls}"
            )

    def note(self) -> None:
        self.calls_made += 1
        self.remaining = max(0, self.max_calls - self.calls_made)


class SSBClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = RateLimiter()
        self.cache = MemoryCache()
        self.session = requests.Session()  # Reuse connections

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _cache_key(self, method: str, path: str, body: Optional[Any] = None) -> str:
        """Generate cache key for request"""
        key_data = f"{method}:{path}"
        if body:
            key_data += f":{json.dumps(body, sort_keys=True)}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def _get(self, path: str, cache_ttl: Optional[int] = None) -> Any:
        # Check cache first
        cache_key = self._cache_key("GET", path)
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        self.rate_limiter.check()
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        self.rate_limiter.note()

        if resp.status_code == 429:
            raise RuntimeError("Rate limit exceeded (429)")
        if not resp.ok:
            raise RuntimeError(f"GET {url} failed: {resp.status_code} {resp.text[:200]}")
        ct = resp.headers.get("content-type", "")
        if "application/json" not in ct:
            raise RuntimeError(f"Expected JSON, got {ct} from {url}")

        data = resp.json()
        # Cache successful responses
        self.cache.set(cache_key, data, cache_ttl or 300)  # 5 min default
        return data

    def _post(self, path: str, body: Any, cache_ttl: Optional[int] = None) -> Any:
        # Check cache for post requests too (for data queries)
        cache_key = self._cache_key("POST", path, body)
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        self.rate_limiter.check()
        url = f"{self.base_url}{path}"
        resp = self.session.post(
            url,
            json=body,
            headers={**self._headers(), "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self.rate_limiter.note()

        if resp.status_code == 429:
            raise RuntimeError("Rate limit exceeded (429)")
        if resp.status_code == 403:
            raise RuntimeError(
                "Request forbidden (403). Likely too many data cells; narrow selection."
            )
        if resp.status_code == 400:
            msg = resp.text
            raise RuntimeError(f"Bad request (400): {msg[:300]}")
        if not resp.ok:
            raise RuntimeError(f"POST {url} failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        # Cache successful data responses for shorter time (1 min)
        self.cache.set(cache_key, data, cache_ttl or 60)
        return data

    def get_config(self) -> Any:
        try:
            return self._get("/config")
        except RuntimeError as err:
            return {
                "note": "SSB PxWebApi v2 does not document a /config endpoint; returning client limits only.",
                "error": str(err),
            }

    def get_navigation(self, folder_id: Optional[str], lang: str = DEFAULT_LANGUAGE) -> Any:
        # Navigation reconstructed from /tables
        return self._get_virtual_navigation(folder_id=folder_id, lang=lang)

    def _get_all_tables(
        self, lang: str = DEFAULT_LANGUAGE, page_size: int = 10000
    ) -> List[Dict[str, Any]]:
        """Fetch all tables across pages. Uses large page size with pagination fallback."""
        params = f"?pagesize={page_size}&pagenumber=1&lang={lang}"
        first = self._get(f"/tables{params}")
        tables: List[Dict[str, Any]] = list(first.get("tables", []))
        page = first.get("page", {})
        total_pages = page.get("totalPages") or page.get("totalpages") or 1
        # If server caps page size, iterate remaining pages
        for p in range(2, total_pages + 1):
            resp = self._get(f"/tables?pagesize={page_size}&pagenumber={p}&lang={lang}")
            tables.extend(resp.get("tables", []))
        return tables

    def _normalize_path_variants(self, paths_value: Any) -> List[List[str]]:
        """Return list of path segment lists from the 'paths' field in table objects.
        Handles shapes: ["A","B"], [["A","B"],["A","C"]], or list of dicts.
        """
        if not paths_value:
            return []
        if isinstance(paths_value, str):
            if "/" in paths_value:
                return [[seg.strip() for seg in paths_value.split("/") if seg.strip()]]
            if ">" in paths_value:
                return [[seg.strip() for seg in paths_value.split(">") if seg.strip()]]
            return [[paths_value]]
        # List of lists
        if (
            isinstance(paths_value, list)
            and paths_value
            and all(isinstance(x, list) for x in paths_value)
        ):
            return [[self._segment_to_text(seg) for seg in path if seg] for path in paths_value]
        # Single path as list
        if isinstance(paths_value, list):
            return [[self._segment_to_text(seg) for seg in paths_value if seg]]
        return []

    def _segment_to_text(self, seg: Any) -> str:
        if isinstance(seg, str):
            return seg
        if isinstance(seg, dict):
            return str(
                seg.get("label") or seg.get("name") or seg.get("id") or seg.get("code") or ""
            )
        return str(seg)

    def _folder_id_for(self, segments: List[str]) -> str:
        """Deterministic ID for a folder path, stable across runs."""
        if not segments:
            return "root"
        return hashlib.md5(("|".join(segments)).encode("utf-8")).hexdigest()

    def _get_virtual_navigation(
        self, folder_id: Optional[str], lang: str = DEFAULT_LANGUAGE
    ) -> Any:
        """Construct a virtual navigation tree from table 'paths' and return a folder view.
        Results are cached per-language.
        """
        cache_key = f"virtual_nav_index:{lang}"
        cached_index = self.cache.get(cache_key)
        if not cached_index:
            tables = self._get_all_tables(lang=lang)
            # Build folder index
            index: Dict[str, Dict[str, Any]] = {}
            children_by_parent: Dict[str, List[str]] = {}
            root_id = "root"
            index[root_id] = {
                "id": root_id,
                "label": "Root",
                "path": [],
                "folderContents": {"folders": [], "tables": []},
            }
            children_by_parent[root_id] = []

            for t in tables:
                table_paths = self._normalize_path_variants(
                    t.get("paths") or t.get("path") or t.get("Path")
                ) or [[]]
                for path_segments in table_paths:
                    parent_segments: List[str] = []
                    parent_id = root_id
                    # Ensure parent entry exists
                    if parent_id not in children_by_parent:
                        children_by_parent[parent_id] = []
                    for seg in path_segments:
                        parent_segments.append(seg)
                        node_id = self._folder_id_for(parent_segments)
                        if node_id not in index:
                            index[node_id] = {
                                "id": node_id,
                                "label": seg,
                                "path": list(parent_segments),
                                "folderContents": {"folders": [], "tables": []},
                            }
                            if parent_id not in children_by_parent:
                                children_by_parent[parent_id] = []
                            children_by_parent[parent_id].append(node_id)
                        parent_id = node_id
                        if parent_id not in children_by_parent:
                            children_by_parent[parent_id] = []
                    # Attach table to the deepest folder for this path
                    table_entry = {
                        "id": t.get("id"),
                        "label": t.get("label"),
                        "category": t.get("category"),
                        "updated": t.get("updated"),
                        "firstPeriod": t.get("firstPeriod"),
                        "lastPeriod": t.get("lastPeriod"),
                        "variables": t.get("variableNames", []),
                        "discontinued": t.get("discontinued", False),
                    }
                    index[parent_id]["folderContents"]["tables"].append(table_entry)

            # Populate folder children lists
            for pid, child_ids in children_by_parent.items():
                index[pid]["folderContents"]["folders"] = [
                    {
                        "id": cid,
                        "label": index[cid]["label"],
                        "path": index[cid]["path"],
                    }
                    for cid in child_ids
                ]

            cached_index = {"index": index, "rootId": root_id}
            # Cache for 1 hour
            self.cache.set(cache_key, cached_index, ttl=3600)

        index_map = cached_index["index"]
        root_id = cached_index["rootId"]
        node_id = folder_id or root_id
        return index_map.get(node_id, index_map[root_id])

    def search_tables(
        self,
        query: Optional[str] = None,
        pastDays: Optional[int] = None,
        includeDiscontinued: Optional[bool] = None,
        pageNumber: Optional[int] = None,
        pageSize: Optional[int] = None,
        lang: Optional[str] = None,
    ) -> Any:
        params: List[str] = []
        if query:
            params.append(f"query={requests.utils.quote(query)}")
        if pastDays:
            params.append(f"pastdays={pastDays}")
        if includeDiscontinued is not None:
            params.append(f"includeDiscontinued={str(includeDiscontinued).lower()}")
        if pageNumber:
            params.append(f"pagenumber={pageNumber}")
        if pageSize:
            params.append(f"pagesize={pageSize}")
        if lang:
            params.append(f"lang={lang}")
        query_str = ("?" + "&".join(params)) if params else ""
        return self._get(f"/tables{query_str}")

    def get_table_metadata(self, table_id: str, lang: str = DEFAULT_LANGUAGE) -> Any:
        return self._get(f"/tables/{table_id}/metadata?lang={lang}")

    def translate_variables(
        self, selection: Dict[str, List[str]], available_vars: Optional[List[str]] = None
    ) -> Dict[str, List[str]]:
        mapping: Dict[str, List[str]] = {
            "year": ["Tid"],
            "time": ["Tid"],
            "month": ["Tid"],
            "tid": ["Tid"],
            "ar": ["Tid"],
            "sex": ["Kjonn", "Kon"],
            "gender": ["Kjonn", "Kon"],
            "kjonn": ["Kjonn"],
            "kon": ["Kon"],
            "age": ["Alder"],
            "alder": ["Alder"],
            "region": ["Region"],
            "county": ["Region"],
            "municipality": ["Region"],
            "kommune": ["Region"],
            "fylke": ["Region"],
            "education": ["Utdanningsniva", "UtbildningsNiva"],
            "employment": ["Sysselsetting", "Sysselsattning"],
            "income": ["Inntekt", "Inkomst"],
            "family_type": ["Familietype", "Familjetyp"],
            "marital_status": ["Sivilstand", "Civilstand"],
            "contentscode": ["ContentsCode"],
            "observations": ["ContentsCode"],
            "contents": ["ContentsCode"],
        }
        out: Dict[str, List[str]] = {}
        for k, v in selection.items():
            if available_vars and k in available_vars:
                out[k] = v
                continue
            candidates = mapping.get(k.lower())
            if not candidates:
                out[k] = v
                continue
            key = candidates[0]
            if available_vars:
                for cand in candidates:
                    if cand in available_vars:
                        key = cand
                        break
            out[key] = v
        return out

    def translate_values(self, variable: str, values: List[str]) -> List[str]:
        valmap: Dict[str, Dict[str, str]] = {
            "Alder": {"total": "tot", "all": "tot", "totalt": "tot"},
            "Tid": {"latest": "top(1)", "recent": "top(3)", "current": "top(1)"},
            "Kjonn": {
                "total": "tot",
                "all": "tot",
                "male": "1",
                "female": "2",
                "men": "1",
                "women": "2",
            },
            "Kon": {
                "total": "tot",
                "all": "tot",
                "male": "1",
                "female": "2",
                "men": "1",
                "women": "2",
            },
            "*": {"all": "*"},
        }
        out: List[str] = []
        for val in values:
            vm = valmap.get(variable, {})
            low = val.lower()
            if low in vm:
                out.append(vm[low])
                continue
            vm_any = valmap.get("*", {})
            if low in vm_any:
                out.append(vm_any[low])
                continue
            if variable == "Tid" and len(val) == 4 and val.isdigit():
                out.append(val)
                continue
            if (
                variable == "Tid"
                and len(val) == 7
                and val[4] == "-"
                and val[:4].isdigit()
                and val[5:].isdigit()
            ):
                out.append(val.replace("-", "M"))
                continue
            out.append(val)
        return out

    def _dimension_codes(self, dim_def: Any) -> List[str]:
        index = dim_def.get("category", {}).get("index", {})
        if isinstance(index, dict):
            return list(index.keys())
        if isinstance(index, list):
            return list(index)
        return list(index or [])

    def _is_value_expression(self, value: str) -> bool:
        if not value:
            return False
        text = value.strip()
        upper = text.upper()
        if text == "*":
            return True
        if "*" in text or "?" in text:
            return True
        if upper.startswith("TOP(") or upper.startswith("BOTTOM(") or upper.startswith("FROM("):
            return True
        if "RANGE(" in upper:
            return True
        return False

    def _normalize_value_token(self, value: str) -> str:
        text = value.strip()
        upper = text.upper()
        if upper.startswith("TOP("):
            return f"top{text[text.find('(') :]}"
        if upper.startswith("BOTTOM("):
            return f"bottom{text[text.find('(') :]}"
        if upper.startswith("FROM("):
            return f"from{text[text.find('(') :]}"
        if upper.startswith("[RANGE(") and text.endswith("]"):
            inner = text[1:-1]
            return f"[range{inner[inner.find('(') :]}]"
        if upper.startswith("RANGE("):
            return f"[range{text[text.find('(') :]}]"
        return text

    def _encode_value_codes(self, values: List[str]) -> str:
        normalized = [self._normalize_value_token(v) for v in values]
        encoded = [requests.utils.quote(v, safe="*(),[]") for v in normalized]
        return ",".join(encoded)

    def validate_selection(
        self, table_id: str, selection: Dict[str, List[str]], lang: str = DEFAULT_LANGUAGE
    ) -> Dict[str, Any]:
        metadata = self.get_table_metadata(table_id, lang)
        errors: List[str] = []
        suggestions: List[str] = []
        dims = metadata.get("dimension", {})
        available_vars = list(dims.keys())

        translated_vars = self.translate_variables(selection, available_vars)
        translated: Dict[str, List[str]] = {}
        for k, v in translated_vars.items():
            translated[k] = self.translate_values(k, v)

        selected_vars = list(translated.keys())
        missing_required: List[str] = []
        missing_optional: List[str] = []
        for var in available_vars:
            if var in selected_vars:
                continue
            if dims.get(var, {}).get("elimination"):
                missing_optional.append(var)
            else:
                missing_required.append(var)
        if missing_required:
            errors.append(f"Missing mandatory variables: {', '.join(missing_required)}")
            suggestions.append("Use '*' to include all values for missing dimensions.")
        if missing_optional:
            suggestions.append(
                f"Optional dimensions not selected (eliminable): {', '.join(missing_optional)}"
            )

        for var_code, values in translated.items():
            if var_code not in available_vars:
                errors.append(f"Variable '{var_code}' not found in table")
                continue
            available_values = self._dimension_codes(dims[var_code])
            for val in values:
                if self._is_value_expression(val):
                    continue
                if val not in available_values:
                    errors.append(f"Value '{val}' not found for variable '{var_code}'")
                    similar = [x for x in available_values if val.lower() in x.lower()][:3]
                    if similar:
                        suggestions.append(f"For '{var_code}', did you mean: {', '.join(similar)}?")
                    else:
                        suggestions.append(
                            f"Use ssb_get_table_variables with variableName='{var_code}' to list values."
                        )

        return {
            "isValid": len(errors) == 0,
            "errors": errors,
            "suggestions": suggestions,
            "translatedSelection": translated,
        }

    def get_table_data(
        self,
        table_id: str,
        selection: Optional[Dict[str, List[str]]] = None,
        lang: str = DEFAULT_LANGUAGE,
        output_format: str = "json-stat2",
    ) -> Any:
        params: List[str] = [f"lang={lang}", f"outputformat={output_format}"]
        if not selection:
            return self._get(f"/tables/{table_id}/data?{'&'.join(params)}")
        validation = self.validate_selection(table_id, selection, lang)
        if not validation["isValid"]:
            errs = "\n".join(validation["errors"]) + (
                "\n\n" + "\n".join(validation["suggestions"]) if validation["suggestions"] else ""
            )
            raise RuntimeError(f"Selection validation failed:\n{errs}")
        final_sel = validation.get("translatedSelection", selection)
        for var_code, values in final_sel.items():
            if not values:
                continue
            params.append(f"valueCodes[{var_code}]={self._encode_value_codes(values)}")
        return self._get(f"/tables/{table_id}/data?{'&'.join(params)}")

    def transform_jsonstat2(
        self, dataset: Any, selection: Optional[Dict[str, List[str]]] = None
    ) -> Dict[str, Any]:
        records: List[Dict[str, Any]] = []
        dims = dataset.get("dimension") or {}
        values = dataset.get("value") or []
        if not dims or values is None:
            return {
                "query": {
                    "selection": selection or {},
                    "table_id": dataset.get("id", [None])[0] if dataset.get("id") else None,
                    "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                "data": [],
                "metadata": {
                    "source": dataset.get("source", "Statistics Norway"),
                    "updated": dataset.get("updated"),
                    "table_name": dataset.get("label"),
                },
                "summary": {"total_records": 0, "non_null_records": 0, "has_data": False},
            }
        dimension_items: List[Tuple[str, Any]] = list(dims.items())
        dim_sizes: List[int] = [
            len(self._dimension_codes(dim_def)) for _, dim_def in dimension_items
        ]
        codes_cache: List[List[str]] = [
            self._dimension_codes(dim_def) for _, dim_def in dimension_items
        ]
        labels_cache: List[Dict[str, str]] = [
            dim_def.get("category", {}).get("label", {}) for _, dim_def in dimension_items
        ]

        for flat_idx, val in enumerate(values):
            if val is None:
                continue
            record: Dict[str, Any] = {}
            temp = flat_idx
            for i in range(len(dimension_items) - 1, -1, -1):
                dim_name, dim_def = dimension_items[i]
                dim_size = dim_sizes[i]
                idx = temp % dim_size
                temp //= dim_size
                code = codes_cache[i][idx]
                label = labels_cache[i].get(code, code)
                base = self._base_name(dim_name)
                record[f"{base}_code"] = code
                record[f"{base}_name"] = label
            record["value"] = val
            records.append(record)

        non_null = len(records)
        return {
            "query": {
                "selection": selection or {},
                "table_id": dataset.get("id", [None])[0] if dataset.get("id") else None,
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "data": records,
            "metadata": {
                "source": dataset.get("source", "Statistics Norway"),
                "updated": dataset.get("updated"),
                "table_name": dataset.get("label"),
                "data_shape": dataset.get("size"),
                "dimensions": [
                    {
                        "name": n,
                        "label": d.get("label"),
                        "values_count": len(self._dimension_codes(d)),
                    }
                    for n, d in dimension_items
                ],
            },
            "summary": {
                "total_records": len(records),
                "non_null_records": non_null,
                "has_data": len(records) > 0,
            },
        }

    def _base_name(self, dim_name: str) -> str:
        mapping: Dict[str, str] = {
            "Region": "region",
            "Alder": "age",
            "Kjonn": "sex",
            "Kon": "sex",
            "Tid": "time",
            "Utdanningsniva": "education_level",
            "UtbildningsNiva": "education_level",
            "ContentsCode": "observation_type",
            "Sysselsetting": "employment_status",
            "Sysselsattning": "employment_status",
            "Sivilstand": "marital_status",
            "Civilstand": "marital_status",
            "Familietype": "family_type",
            "Familjetyp": "family_type",
            "Inntekt": "income",
            "Inkomst": "income",
        }
        return mapping.get(dim_name, dim_name.lower())

    def get_usage(self) -> Dict[str, Any]:
        return {
            "requestCount": self.rate_limiter.calls_made,
            "windowStart": self.rate_limiter.window_start,
            "rateLimitInfo": {
                "remaining": self.rate_limiter.remaining,
                "resetTime": self.rate_limiter.reset_time,
                "maxCalls": self.rate_limiter.max_calls,
                "timeWindow": self.rate_limiter.window_seconds,
            },
        }


_client = SSBClient()


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(_stringify_cell(v) for v in value if v is not None)
    elif isinstance(value, dict):
        text = ", ".join(f"{k}={_stringify_cell(v)}" for k, v in value.items())
    else:
        text = str(value)
    return text.replace("\n", " ").replace("|", "/").strip()


def _format_sheet(sheet_name: str, headers: List[str], rows: List[List[Any]]) -> str:
    sanitized_headers = [_stringify_cell(h) for h in headers]
    filler_count = max(len(headers) - 1, 0)
    normalized_rows = rows if rows else [["No data"] + [""] * filler_count]
    formatted_rows: List[str] = []
    for row in normalized_rows:
        padded = list(row)[: len(headers)]
        if len(padded) < len(headers):
            padded.extend([""] * (len(headers) - len(padded)))
        formatted_rows.append("|".join(_stringify_cell(cell) for cell in padded))
    lines = [f"## Sheet: {sheet_name} ##", "", "|".join(sanitized_headers)]
    lines.extend(formatted_rows)
    return "\n".join(lines)


def _format_dict_sheet(sheet_name: str, data: Optional[Dict[str, Any]]) -> Optional[str]:
    if not data:
        return None
    rows = [[key, value] for key, value in data.items()]
    return _format_sheet(sheet_name, ["Key", "Value"], rows)


def _format_dimensions_sheet(dimensions: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not dimensions:
        return None
    rows = [
        [
            dim.get("name"),
            dim.get("label"),
            dim.get("values_count"),
        ]
        for dim in dimensions
    ]
    return _format_sheet("Dimensions", ["Code", "Label", "Values"], rows)


def _format_period(period: Optional[Dict[str, Any]]) -> str:
    if not period:
        return ""
    start = period.get("start")
    end = period.get("end")
    if start and end and start != end:
        return f"{start} - {end}"
    return start or end or ""


def _derive_data_columns(
    records: List[Dict[str, Any]], dimensions: List[Dict[str, Any]]
) -> List[str]:
    if not records:
        return ["value"]
    ordered_keys: List[str] = []
    for dim in dimensions:
        base = _client._base_name(dim.get("name", ""))
        code_key = f"{base}_code"
        name_key = f"{base}_name"
        if any(code_key in rec for rec in records):
            ordered_keys.append(code_key)
        if any(name_key in rec for rec in records):
            ordered_keys.append(name_key)
    seen: List[str] = list(ordered_keys)
    for record in records:
        for key in record.keys():
            if key not in seen:
                seen.append(key)
    if "value" in seen:
        seen.remove("value")
        seen.append("value")
    return seen


def _build_dataset_sheets(
    structured: Dict[str, Any], extra_sections: Optional[List[Optional[str]]] = None
) -> str:
    records: List[Dict[str, Any]] = structured.get("data", [])
    dimensions: List[Dict[str, Any]] = structured.get("metadata", {}).get("dimensions", [])
    columns = _derive_data_columns(records, dimensions)
    rows = [[record.get(col) for col in columns] for record in records]
    metadata_info = {
        k: v for k, v in (structured.get("metadata") or {}).items() if k != "dimensions"
    }
    sheets = [
        _format_sheet("Data", columns, rows),
        _format_dict_sheet("Query", structured.get("query")),
        _format_dict_sheet("Metadata", metadata_info),
        _format_dimensions_sheet(dimensions),
        _format_dict_sheet("Summary", structured.get("summary")),
    ]
    if extra_sections:
        sheets.extend(extra_sections)
    return _join_sheets(sheets)


def _join_sheets(sheets: List[Optional[str]]) -> str:
    return "\n\n".join([sheet for sheet in sheets if sheet])


def ssb_get_api_status() -> Any:
    """Get API configuration and current rate limit status.

    Purpose:
        Returns SSB PX-Web v2 configuration (version, languages, limits) and this client's
        current in-memory rate limit window usage to help plan safe request pacing.

    Args:
        None.

    Returns:
        dict: {
            "config": object,   # Raw config JSON from /config
            "usage": {
                "requestCount": int,
                "windowStart": float,  # epoch seconds
                "rateLimitInfo": {"remaining": int, "resetTime": float, "maxCalls": int, "timeWindow": int}
            }
        }

    Raises:
        RuntimeError: If the underlying HTTP call fails or a non-JSON response is returned.
    """
    return {"config": _client.get_config(), "usage": _client.get_usage()}


def ssb_browse_folders(folderId: Optional[str] = None, language: str = DEFAULT_LANGUAGE) -> Any:
    """Browse the SSB database folder structure.

    Purpose:
        Navigate a virtual hierarchical folder tree reconstructed from `/tables` `paths`
        to discover subjects and tables. The root folderId is "root". Other folderIds
        are stable hashes of the path segments and are language-specific.

    Args:
        folderId (str, optional): Folder ID to browse; None or omitted for root.
        language (str, optional): Language code like 'en'. Defaults to DEFAULT_LANGUAGE.

    Returns:
        dict: Folder JSON including folderContents: { folders[], tables[] }.

    Raises:
        RuntimeError: On HTTP failure or unexpected content type.
    """
    return _client.get_navigation(folderId, language)


def ssb_search_tables(
    query: Optional[str] = None,
    pastDays: Optional[int] = None,
    includeDiscontinued: Optional[bool] = None,
    pageSize: int = 20,
    pageNumber: int = 1,
    language: str = DEFAULT_LANGUAGE,
    category: Optional[str] = None,
) -> str:
    """Search for SSB tables with optional filters and return spreadsheet-friendly text.

    Purpose:
        Find candidate tables by keyword and optionally filter results by a broad category
        heuristic (population, labour, economy, housing) to improve relevance while keeping
        the response compact enough for spreadsheet-style clients.

    Args:
        query (str, optional): Keyword(s) for table search.
        pastDays (int, optional): Only include tables updated within the last N days.
        includeDiscontinued (bool, optional): Include discontinued tables when True.
        pageSize (int, optional): Page size (server supports up to 100). Default 20.
        pageNumber (int, optional): Page number. Default 1.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.
        category (str, optional): Heuristic filter: 'population'|'labour'|'economy'|'housing'.

    Returns:
        str: One or more `## Sheet: ... ##` sections that can be parsed into spreadsheet tabs.

    Raises:
        RuntimeError: On HTTP failure or unexpected content type.
    """
    res = _client.search_tables(
        query=query,
        pastDays=pastDays,
        includeDiscontinued=includeDiscontinued,
        pageNumber=pageNumber,
        pageSize=pageSize,
        lang=language,
    )
    tables = res.get("tables", [])
    filtered = tables
    if category:
        cl = category.lower()

        def match(tbl: Dict[str, Any]) -> bool:
            label_raw = tbl.get("label") or tbl.get("title") or ""
            vars_raw = " ".join(tbl.get("variableNames") or tbl.get("variables") or [])
            label = (
                unicodedata.normalize("NFKD", label_raw)
                .encode("ascii", "ignore")
                .decode("ascii")
                .lower()
            )
            vars_join = (
                unicodedata.normalize("NFKD", vars_raw)
                .encode("ascii", "ignore")
                .decode("ascii")
                .lower()
            )
            if cl == "population":
                return (
                    "population" in label
                    or "befolkning" in label
                    or "region" in vars_join
                    or "demographic" in label
                )
            if cl == "labour":
                return any(x in label for x in ["labour", "employment", "arbeid", "sysselsetting"])
            if cl == "economy":
                return any(x in label for x in ["gdp", "income", "okonomi", "bnp", "inntekt"])
            if cl == "housing":
                return any(x in label for x in ["housing", "dwelling", "bolig", "leilighet"])
            return True

        filtered = [t for t in tables if match(t)]

    table_rows: List[List[Any]] = []
    for table in filtered[: pageSize or 20]:
        variables = table.get("variableNames") or table.get("variables") or []
        period = {"start": table.get("firstPeriod"), "end": table.get("lastPeriod")}
        table_rows.append(
            [
                table.get("id"),
                table.get("label") or table.get("title"),
                _stringify_cell(table.get("description")),
                _format_period(period),
                ", ".join(variables),
                table.get("updated"),
                table.get("source"),
                table.get("category"),
                "Yes" if table.get("discontinued") else "No",
            ]
        )

    page_info = res.get("page", {})
    pagination = {
        "current_page": page_info.get("pageNumber") or page_info.get("pagenumber"),
        "total_pages": page_info.get("totalPages") or page_info.get("totalpages"),
        "total_results": page_info.get("totalElements") or page_info.get("totalelements"),
        "page_size": page_info.get("pageSize") or page_info.get("pagesize"),
    }
    query_info = {
        "search_term": query,
        "category_filter": category,
        "past_days": pastDays,
        "include_discontinued": includeDiscontinued,
        "page_size": pageSize,
        "page_number": pageNumber,
        "language": language,
    }
    metadata = {
        "total_filtered": len(filtered),
        "total_unfiltered": len(tables),
        "has_category_filter": bool(category),
    }
    sheets = [
        _format_sheet(
            "Tables",
            [
                "TableID",
                "Title",
                "Description",
                "Period",
                "Variables",
                "Updated",
                "Source",
                "Category",
                "Discontinued",
            ],
            table_rows,
        ),
        _format_dict_sheet("Query", query_info),
        _format_dict_sheet("Pagination", pagination),
        _format_dict_sheet("Metadata", metadata),
    ]
    return _join_sheets(sheets)


def ssb_get_table_info(tableId: str, language: str = DEFAULT_LANGUAGE) -> Any:
    """Get detailed metadata for a specific table.

    Purpose:
        Retrieve table metadata (dimensions, labels, contacts, notes) to understand
        variables and valid codes prior to querying data.

    Args:
        tableId (str): SSB table ID (e.g., '05810'). Required.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        dict: Raw metadata document from /tables/{tableId}/metadata.

    Raises:
        RuntimeError: On HTTP failure or unexpected content type.
    """
    return _client.get_table_metadata(tableId, language)


def ssb_get_table_data(
    tableId: str, selection: Optional[Dict[str, List[str]]] = None, language: str = DEFAULT_LANGUAGE
) -> str:
    """Retrieve table data and return spreadsheet-friendly text.

    Purpose:
        Validate selection, request data (JSON-stat2), and emit one or more sheet sections
        (Data, Query, Metadata, Dimensions, Summary) that downstream clients can ingest as
        compact tables instead of verbose JSON.

    Args:
        tableId (str): SSB table ID. Required.
        selection (dict[str, list[str]], optional): Mapping of variable -> value codes.
            Special expressions supported: '*', '??', 'TOP(n)', 'BOTTOM(n)', 'FROM(x)', '[RANGE(a,b)]'.
            English names auto-map to API variable names (e.g., region->Region).
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        str: Spreadsheet-style sections ready for parsing into tabs.

    Raises:
        RuntimeError: If validation fails, HTTP errors occur, or bad request hints are returned.
    """
    dataset = _client.get_table_data(tableId, selection, language)
    structured = _client.transform_jsonstat2(dataset, selection)
    return _build_dataset_sheets(structured)


def ssb_check_usage() -> Any:
    """Inspect current client-side rate limit window usage.

    Purpose:
        Exposes in-memory counters for calls made, remaining, reset time, etc., so callers
        can throttle or batch requests appropriately.

    Args:
        None.

    Returns:
        dict: { requestCount, windowStart, rateLimitInfo { remaining, resetTime, maxCalls, timeWindow } }.

    Raises:
        None (purely in-memory computation).
    """
    return _client.get_usage()


def ssb_search_regions(query: str, language: str = DEFAULT_LANGUAGE) -> Any:
    """Find region-related tables to guide region code discovery.

    Purpose:
        Helps locate tables that likely contain a 'Region' variable relevant to a query
        term (e.g., a municipality name), providing next steps to inspect values.

    Args:
        query (str): Free-text region hint (e.g., 'Oslo', 'Bergen'). Required.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        dict: { query, matches[] } with candidate tables (id, title, variables).

    Raises:
        RuntimeError: On HTTP failure.
    """
    res = _client.search_tables(query=f"region kommune {query}", pageSize=5, lang=language)
    tables = res.get("tables", [])
    region_tables = [
        t
        for t in tables
        if (
            "region" in (t.get("label", "").lower())
            or any("region" in (v.lower()) for v in (t.get("variableNames") or []))
            or query.lower() in (t.get("label", "").lower())
        )
    ]
    if not region_tables:
        return {
            "query": query,
            "matches": [],
            "suggestions": [
                "Try broader terms (kommune, fylke)",
                "Browse with ssb_browse_folders",
                "Try Norwegian or English terms",
            ],
        }
    return {
        "query": query,
        "matches": [
            {"id": t.get("id"), "title": t.get("label"), "variables": t.get("variableNames", [])}
            for t in region_tables[:3]
        ],
    }


def ssb_get_table_variables(
    tableId: str, language: str = DEFAULT_LANGUAGE, variableName: Optional[str] = None
) -> Any:
    """List variables and sample values for a table as structured JSON.

    Purpose:
        Provide a quick, machine-readable overview of available variables and example
        value codes to help construct valid selections.

    Args:
        tableId (str): SSB table ID. Required.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.
        variableName (str, optional): Filter to a specific variable by code or label substring.

    Returns:
        dict: { table_id, table_name, query, variables[], metadata }.

    Raises:
        RuntimeError: On HTTP failure.
    """
    meta = _client.get_table_metadata(tableId, language)
    dims = meta.get("dimension", {})
    items = list(dims.items())
    if variableName:
        items = [
            i
            for i in items
            if i[0].lower() == variableName.lower()
            or variableName.lower() in (i[1].get("label", "").lower())
        ]
    if not items:
        return {"table_id": tableId, "error": f"Variable '{variableName}' not found"}

    def build(var_code: str, var_def: Any) -> Dict[str, Any]:
        index = var_def.get("category", {}).get("index", {})
        labels = var_def.get("category", {}).get("label", {})
        if isinstance(index, dict):
            ordered_codes = list(index.keys())
            positions = index
        elif isinstance(index, list):
            ordered_codes = list(index)
            positions = {code: pos for pos, code in enumerate(ordered_codes)}
        else:
            ordered_codes = list(index or [])
            positions = {code: pos for pos, code in enumerate(ordered_codes)}
        all_values = [
            {"code": c, "label": labels.get(c, c), "index": positions.get(c)} for c in ordered_codes
        ]
        return {
            "variable_code": var_code,
            "variable_name": var_def.get("label"),
            "variable_type": var_code.lower(),
            "total_values": len(all_values),
            "sample_values": all_values[:10],
            "has_more": len(all_values) > 10,
            "usage_example": {var_code: [all_values[0]["code"]] if all_values else ["value"]},
        }

    return {
        "table_id": tableId,
        "table_name": meta.get("label"),
        "query": {"variable_filter": variableName or None, "language": language},
        "variables": [build(code, definition) for code, definition in items],
        "metadata": {
            "total_variables": len(dims),
            "filtered_variables": len(items),
            "source": meta.get("source", "Statistics Norway"),
            "updated": meta.get("updated"),
        },
    }


def ssb_find_region_code(
    query: str, tableId: Optional[str] = None, language: str = DEFAULT_LANGUAGE
) -> Any:
    """Resolve municipality/area names to SSB region codes with usage example.

    Purpose:
        Search a suitable table for the 'Region' dimension and return matching codes for
        the given query along with a ready-to-use selection example.

    Args:
        query (str): Municipality/region name like 'Oslo', 'Bergen'. Required.
        tableId (str, optional): Specific table to search in (ensures compatibility).
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        dict: On success: { query, matches[], primary_match, usage_example, source_table }.
              If no match: { query, matches: [], error, source_table?, common_codes? }.

    Raises:
        RuntimeError: On HTTP failure.
    """
    if not tableId:
        search = _client.search_tables(
            query="population municipality region befolkning kommune", pageSize=10, lang=language
        )
        tables = search.get("tables", [])
        cand = [
            t
            for t in tables
            if any("region" in v.lower() for v in (t.get("variableNames") or []))
            and (
                "population" in (t.get("label", "").lower())
                or "befolkning" in (t.get("label", "").lower())
            )
        ]
        if not cand:
            return {
                "query": query,
                "matches": [],
                "error": "No suitable regional tables found",
                "common_codes": [
                    {"code": "0301", "name": "Oslo"},
                    {"code": "4601", "name": "Bergen"},
                    {"code": "5001", "name": "Trondheim"},
                    {"code": "1103", "name": "Stavanger"},
                    {"code": "4204", "name": "Kristiansand"},
                ],
            }
        tableId = cand[0].get("id")
    meta = _client.get_table_metadata(tableId, language)
    dim = meta.get("dimension", {}).get("Region")
    if not dim:
        return {
            "query": query,
            "error": f"Could not access region data from table {tableId}",
            "source_table": tableId,
        }
    entries = _client._dimension_codes(dim)
    labels = dim.get("category", {}).get("label", {})

    def normalize(text: str) -> str:
        return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()

    query_norm = normalize(query)
    exact = [
        (code, labels.get(code, ""))
        for code in entries
        if query_norm in code.lower() or query_norm in normalize(labels.get(code, ""))
    ]
    if not exact:
        query_tokens = [w for w in query_norm.split() if w]
        partial = [
            (code, labels.get(code, ""))
            for code in entries
            if any(w in normalize(labels.get(code, "")) or w in code.lower() for w in query_tokens)
        ][:10]
        if not partial:
            return {
                "query": query,
                "matches": [],
                "error": f"No regions found matching '{query}'",
                "source_table": {"id": tableId, "name": meta.get("label")},
            }
        matches = [
            {"code": c, "name": n or "Unknown region", "match_type": "partial"} for c, n in partial
        ]
        primary = matches[0]
        return {
            "query": query,
            "matches": matches,
            "match_type": "partial_matches",
            "primary_match": primary,
            "usage_example": {"Region": [primary["code"]]},
            "source_table": {"id": tableId, "name": meta.get("label")},
        }
    matches = [{"code": c, "name": n or "Unknown region", "match_type": "exact"} for c, n in exact][
        :5
    ]
    primary = matches[0]
    return {
        "query": query,
        "matches": matches,
        "match_type": "exact_matches",
        "total_matches": len(exact),
        "primary_match": primary,
        "usage_example": {"Region": [primary["code"]]},
        "source_table": {"id": tableId, "name": meta.get("label")},
    }


def ssb_test_selection(
    tableId: str, selection: Dict[str, List[str]], language: str = DEFAULT_LANGUAGE
) -> Any:
    """Validate a selection against table metadata without fetching data.

    Purpose:
        Catch common issues early (unknown variables/values, missing dimensions) and also
        auto-translate English/common terms to proper API variable/value codes.

    Args:
        tableId (str): SSB table ID. Required.
        selection (dict[str, list[str]]): Variable -> values mapping. Required.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        dict: { isValid: bool, errors: list[str], suggestions: list[str], translatedSelection?: dict }.

    Raises:
        RuntimeError: On HTTP failure while loading metadata.
    """
    return _client.validate_selection(tableId, selection, language)


def ssb_preview_data(
    tableId: str, selection: Optional[Dict[str, List[str]]] = None, language: str = DEFAULT_LANGUAGE
) -> str:
    """Fetch a safe, small preview of data as spreadsheet-formatted text.

    Purpose:
        Automatically constrains selections (e.g., '*' -> TOP(3)) to return a small sample
        so you can verify the query works before requesting large datasets.

    Args:
        tableId (str): SSB table ID. Required.
        selection (dict[str, list[str]], optional): Variable -> values mapping. Optional.
        language (str, optional): Language code like 'en'. Default DEFAULT_LANGUAGE.

    Returns:
        str: Spreadsheet-style sections (Data, Query, Metadata, Dimensions, Summary, Preview Info).

    Raises:
        RuntimeError: If validation fails or HTTP request fails.
    """
    preview = selection
    if selection:
        preview = {}
        for k, vals in selection.items():
            if any(
                v == "*"
                or v.upper().startswith("TOP(")
                or v.upper().startswith("BOTTOM(")
                or v.upper().startswith("FROM(")
                or "RANGE(" in v.upper()
                for v in vals
            ):
                preview[k] = ["TOP(3)" if v == "*" else v for v in vals]
            else:
                preview[k] = vals[:3]
    dataset = _client.get_table_data(tableId, preview, language)
    structured = _client.transform_jsonstat2(dataset, preview)
    structured["preview_info"] = {
        "is_preview": True,
        "original_selection": selection,
        "preview_selection": preview,
        "note": "This is a limited preview. Use ssb_get_table_data for full dataset.",
    }
    preview_sheet = _format_dict_sheet("Preview Info", structured.get("preview_info"))
    return _build_dataset_sheets(structured, extra_sections=[preview_sheet])
