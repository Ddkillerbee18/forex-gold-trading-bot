import time
import requests
from datetime import datetime, timezone

class NewsFilter:
    def __init__(self, config):
        self.config = config
        self.enabled = config.get("news_filter_enabled", True)
        self.url = config.get("news_calendar_url", "")
        self.buffer_min = config.get("news_buffer_minutes", 30)
        self.currency = config.get("news_currency", "USD")
        self.events = []
        self.last_fetch = 0
        self.cache_ttl = config.get("news_cache_ttl_minutes", 180) * 60

    def fetch_events(self):
        now = time.time()
        if now - self.last_fetch < self.cache_ttl and self.events:
            return

        try:
            resp = requests.get(self.url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.events = [
                    item for item in data 
                    if item.get("country") == self.currency and item.get("impact") == "High"
                ]
                self.last_fetch = now
        except Exception:
            if self.config.get("news_fail_open", True):
                self.events = []

    def is_news_impact_time(self):
        if not self.enabled:
            return False

        self.fetch_events()
        now_utc = datetime.now(timezone.utc)

        for event in self.events:
            try:
                event_time_str = event.get("date")
                if not event_time_str:
                    continue
                
                event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                diff_minutes = abs((event_time - now_utc).total_seconds()) / 60.0

                if diff_minutes <= self.buffer_min:
                    return True
            except Exception:
                continue

        return False