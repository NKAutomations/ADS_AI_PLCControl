"""Wecklogik fuer den Supervisor-Loop.

Drei Strategien:
  wait_for_timer  – schlaeft N Sekunden, prueft Stopp-Flag im Poll-Intervall
  wait_for_event  – pollt ein ADS-Symbol bis zum Zielwert, Timeout oder Stopp
  wait_for_ack    – Alias fuer wait_for_event mit value=True (Quittierung)

Alle Funktionen fuehren KEINE Schreibvorgaenge aus.
Rueckgabewerte: "woken" | "stopped" | "timeout"
"""
from __future__ import annotations

import time
import threading
from typing import Any, Callable, Literal

WakeResult = Literal["woken", "stopped", "timeout"]

_POLL = 0.1   # Standard-Poll-Intervall in Sekunden


def wait_for_timer(
    seconds: float,
    stop_event: threading.Event,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = _POLL,
) -> WakeResult:
    """Wartet `seconds` Sekunden.

    Prueft das Stopp-Flag alle `poll_interval` Sekunden.
    Gibt "stopped" zurueck, wenn das Flag gesetzt wird.
    Gibt "woken" zurueck, wenn die Zeit abgelaufen ist.
    """
    if seconds <= 0:
        return "woken"
    deadline = time.monotonic() + seconds
    while True:
        if stop_event.is_set():
            return "stopped"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "woken"
        sleep_fn(min(poll_interval, remaining))


def wait_for_event(
    ads: Any,
    symbol: str,
    data_type: str,
    expected_value: Any,
    timeout: float,
    poll_interval: float,
    stop_event: threading.Event,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> WakeResult:
    """Pollt `symbol` bis `expected_value` erreicht ist, Timeout oder Stopp.

    Gibt "woken"   zurueck, wenn der Zielwert erreicht wurde.
    Gibt "stopped" zurueck, wenn das Stopp-Flag gesetzt wird.
    Gibt "timeout" zurueck, wenn das Zeitlimit abgelaufen ist.
    """
    if timeout <= 0:
        return "timeout"
    deadline = time.monotonic() + timeout
    while True:
        if stop_event.is_set():
            return "stopped"
        value, ok, _ = ads.read_value(symbol, data_type)
        if ok and type(value) is type(expected_value) and value == expected_value:
            return "woken"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        sleep_fn(min(poll_interval, remaining))


def wait_for_ack(
    ads: Any,
    ack_symbol: str,
    data_type: str,
    timeout: float,
    poll_interval: float,
    stop_event: threading.Event,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> WakeResult:
    """Wartet auf True am Quittierungssignal.

    Alias fuer wait_for_event mit expected_value=True.
    Die Anwendung schreibt das Signal NICHT selbst.
    """
    return wait_for_event(
        ads=ads,
        symbol=ack_symbol,
        data_type=data_type,
        expected_value=True,
        timeout=timeout,
        poll_interval=poll_interval,
        stop_event=stop_event,
        sleep_fn=sleep_fn,
    )
