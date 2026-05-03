import time
import requests
from logger import get_logger

logger = get_logger()

BASE_URL = "https://api.airtable.com/v0"

class AirtableClient:
    def __init__(self, token, base_id, table_id, delay=0.25, max_retries=5, backoff=2):
        self.token = token
        self.base_id = base_id
        self.table_id = table_id
        self.delay = delay
        self.max_retries = max_retries
        self.backoff = backoff
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self.table_url = f"{BASE_URL}/{base_id}/{table_id}"

    def _request(self, method, url, **kwargs):
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(method, url, headers=self.headers, **kwargs, timeout=30)
                if resp.status_code == 429:
                    wait = self.backoff * attempt
                    logger.warning(f"Airtable rate limit. Waiting {wait}s (attempt {attempt})")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = self.backoff * attempt
                    logger.warning(f"Airtable server error {resp.status_code}. Retrying in {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries:
                    raise
                logger.warning(f"Airtable request failed: {e}. Retrying ({attempt}/{self.max_retries})")
                time.sleep(self.backoff * attempt)

    def get_all_records(self, force_refresh=False, cache_seconds=600):
        """Get all Airtable records.
        Caches the result for cache_seconds to avoid expensive re-fetches
        within the same script run. Pass force_refresh=True to bypass cache."""
        import time
        now = time.time()
        if not force_refresh and hasattr(self, '_records_cache'):
            cached_at, cached_data = self._records_cache
            if (now - cached_at) < cache_seconds:
                return cached_data
        records = self._fetch_all_records_uncached()
        self._records_cache = (now, records)
        return records

    def _fetch_all_records_uncached(self):
        records = []
        offset = None
        page = 0
        while True:
            page += 1
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            data = self._request("GET", self.table_url, params=params)
            records.extend(data.get("records", []))
            offset = data.get("offset")
            logger.info(f"Airtable load page {page}: {len(records)} total so far")
            if not offset:
                break
        return records

    def search_by_field(self, field_name, value):
        formula = f"({{{field_name}}}=\"{value}\")"
        data = self._request("GET", self.table_url, params={
            "filterByFormula": formula,
            "maxRecords": 2
        })
        return data.get("records", [])

    def create_record(self, fields):
        data = self._request("POST", self.table_url, json={"fields": fields})
        return data

    def patch_record(self, record_id, fields):
        url = f"{self.table_url}/{record_id}"
        data = self._request("PATCH", url, json={"fields": fields})
        return data

    def delete_record(self, record_id):
        """Delete an Airtable record. Permanent — no recovery."""
        url = f"{self.table_url}/{record_id}"
        return self._request("DELETE", url)
