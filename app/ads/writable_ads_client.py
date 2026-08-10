"""Small additive write extension for the repository AdsClient.

The parent client remains responsible for connection, symbol discovery and reads.
All writes are exposed through this one method and still require validation above it.
"""
from __future__ import annotations
from app.ads.ads_client import AdsClient, _get_pyads_type

class WritableAdsClient(AdsClient):
    def write_value(self, symbol: str, data_type: str, value: object) -> tuple[bool, str]:
        with self._lock:
            if not self._connected or self._plc is None:
                return False, "Nicht verbunden."
            plc_type = _get_pyads_type(data_type)
            if plc_type is None:
                return False, f"Nicht unterstuetzter Typ: {data_type}"
            try:
                self._plc.write_by_name(symbol, value, plc_type)
                return True, ""
            except Exception as exc:
                return False, str(exc)
