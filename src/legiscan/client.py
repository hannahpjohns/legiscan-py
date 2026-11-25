"""Legiscan API client with helper methods"""

import base64
import hashlib
import io
import json
import logging
import socket
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from legiscan._utils import _normalize_bill_number

logger = logging.getLogger(_name_)

LS_BASE = "https://api.legiscan.com/"

DEFAULT_DELAY = 0.4

class Client:

    def __init__(self, api_key: str, cache_dir: str = ".legiscan_cache", delay: float = DEFAULT_DELAY):
        """ Client class that stores an API key, creates an on-disk cache
        directory, and enforces the minimum delay between API calls (complies with Legiscans usage terms)"""
         
        if not api_key:
            raise ValueError("Legiscan API key required.")
        self.api_key = api_key
        self.delay = delay
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call = 0.0

    def _call(self, op: str, **params) -> Optional[dict]:
        """ Private API request method. All the following public methods route through this """
        key = json.dumps({**params, "op": op}, sort_keys=True)
        cache_file = self.cache_dir / f"{op}_{hashlib.md5(key.encode()).hexdigest()}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        time.sleep(max(0.0, self.delay - (time.time() - self._last_call)))
        url = f"{LS_BASE}?{urlencode({'key': self.api_key, 'op': op, **params})}"
        try:
            req = Request(url, headers={"User-Agent": "legislative-tracker/1.0"})
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            self._last_call = time.time()
        except (HTTPError, URLError, socket.timeout, OSError, json.JSONDecodeError) as e:
            logger.warning("LegiScan %s failed: %s", op, e)
            return None

        if data.get("status") == "ERROR":
            logger.warning("LegiScan API error op=%s: %s", op, data.get("alert", ""))
            return None
        cache_file.write_text(json.dumps(data))
        return data
    
    def sessions_for_year(self, state: str, year: int) -> list[dict]:
        """ Returns all legislative sessions in a satate that overlap with a given year.
        Output is sorted by start year then special-session number"""

        data = self._call("getSessionList", state=state) or {}
        sessions = data.get("sessions", [])
        for s in sessions:
            s["session_id"] = int(s["session_id"])
            s["special"] = int(s.get("special", 0))
        return sorted(
            (s for s in sessions if s["year_start"] <= year <= s["year_end"]),
            key=lambda s: (s["year_start"], s["special"]),
        )

    def search(self, query: str, session_id: int) -> list[int]:
        """ Runs a keyword search inside a given session and returns matching bill ids """

        data = self._call("getSearchRaw", query=query, id=session_id) or {}
        results = data.get("searchresult", {}).get("results", [])
        return [int(r["bill_id"]) for r in results if r.get("bill_id")]

    def get_bill(self, bill_id: int) -> Optional[dict]:
        """ Return the full bill record for a single bill_i, or None if the API errors"""
        data = self._call("getBill", id=bill_id)
        return data.get("bill") if data else None

    def get_master_list(self, session_id: int) -> list[dict]:
        """ Return every bill in a session as a list of {bill_id, number_upper} dicts"""

        data = self._call("getMasterListRaw", id=session_id) or {}
        master = data.get("masterlist", {})
        return [
            {
                "bill_id": int(item["bill_id"]),
                "number_upper": _normalize_bill_number(str(item.get("number", ""))),
            }
            for key, item in master.items()
            if key != "session" and isinstance(item, dict) and item.get("bill_id")
        ]

    def get_dataset_list(self, state: str = "", year: int = 0) -> list[dict]:
        """ 1 API call. Lists available datasets with access_key + dataset_hash per session. """

        params = {}
        if state: params["state"] = state
        if year:  params["year"]  = year
        data = self._call("getDatasetList", **params) or {}
        return data.get("datasetlist", [])
    
    def get_dataset(self, session_id: int, access_key: str) -> dict:
        """ 1 API call per session. Returns a base64 ZIP of every bill in the session. """

        data = self._call("getDataset", id=session_id, access_key=access_key) or {}
        return data.get("dataset", {})
    
    def unpack_dataset_to_cache(self, dataset: dict) -> int:
        """ Expands the ZIP into getBill_<hash>.json files matching the existing cache format
        so score_from_cache.py can read them unchanged. """

        raw = base64.b64decode(dataset.get("zip", ""))
        count = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if not name.endswith(".json"):
                    continue
                bill_json = json.loads(zf.read(name).decode("utf-8"))
                bill_id = bill_json.get("bill", {}).get("bill_id")
                if not bill_id:
                    continue
                key = json.dumps({"id": int(bill_id), "op": "getBill"}, sort_keys=True)
                cache_file = self.cache_dir / f"getBill_{hashlib.md5(key.encode()).hexdigest()}.json"
                cache_file.write_text(json.dumps(bill_json))
                count += 1
        return count
    
    def search_sessions_with_hits(self, queries: list[str], state: str, years: list[int]) -> set[int]:
        """ Returns session_ids (within years) where any query returned at least one hit. """

        hits: set[int] = set()
        checked: set[int] = set()
        for year in years:
            for session in self.sessions_for_year(state, year):
                sid = session["session_id"]
                if sid in checked:
                    continue
                checked.add(sid)
                for q in queries:
                    if self.search(q, sid):
                        hits.add(sid)
                        break
        return hits

    def find_bill_id(self, state: str, bill_number: str, years: list[int]) -> Optional[int]:
        """ Takes a human-readable bill number (e.g. HB1) and finds it's Legiscan bill_id by
        scanning master lists in the given years. Returns None if there is no match. """

        import re
        bn = re.sub(r'^(CS[/ ])+', '', bill_number.upper().replace(" ", "")).strip()
        candidates = list(dict.fromkeys([
            _normalize_bill_number(bill_number),
            _normalize_bill_number(bn),
        ]))
        for year in years:
            for session in self.sessions_for_year(state, year):
                lookup = {b["number_upper"]: b["bill_id"] for b in self.get_master_list(session["session_id"])}
                for cand in candidates:
                    if cand in lookup:
                        return lookup[cand]
        return None
