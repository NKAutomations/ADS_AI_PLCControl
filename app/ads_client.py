"""Eigenständiger ADS-Client für ADS_KI_Maschinensteuerung.

Funktionen:
- TwinCAT-DLL-Kompatibilität für pyads 3.2.2
- ADS-Verbindung und echte Verifikation
- Lesen des vollständigen ADS-Symboluploads
- typisiertes Lesen
- kontrolliertes Schreiben über eine zentrale Methode

Der Symbolparser verwendet das offizielle TwinCAT-Layout:
6 x UINT32 + 3 x UINT16 = 30 Byte Header.
"""

from __future__ import annotations

import logging
import struct
import threading
from ctypes import create_string_buffer
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


_TYPE_ALIASES = {
    "BOOL": "BOOL",
    "INT": "INT",
    "DINT": "DINT",
    "UINT": "UINT",
    "UDINT": "UDINT",
    "REAL": "REAL",
    "LREAL": "LREAL",
    "TIME": "TIME",
    "STRING": "STRING",
    "WORD": "UINT",
    "DWORD": "UDINT",
    "BYTE": "UINT",
    "SINT": "INT",
    "USINT": "UINT",
    "LINT": "DINT",
    "ULINT": "UDINT",
}

_SYMBOL_HEADER_FORMAT = "<IIIIIIHHH"
_SYMBOL_HEADER_SIZE = struct.calcsize(_SYMBOL_HEADER_FORMAT)
_MAX_SYMBOL_UPLOAD_BYTES = 100 * 1024 * 1024


@dataclass
class Symbol:
    """Ein ADS-Symbol mit den für die Weboberfläche benötigten Angaben."""

    name: str
    tc_type: str
    data_type: str
    comment: str = ""
    supported: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pyads_module():
    """Importiert pyads mit der TwinCAT-DLL-Kompatibilität des Analyseprojekts."""

    try:
        from .dll_compat import prepare_pyads_import
    except ImportError:
        # Fallback für einen direkten Modulaufruf.
        from dll_compat import prepare_pyads_import

    dll_directory = prepare_pyads_import()
    if dll_directory is None:
        logger.warning(
            "TcAdsDll.dll wurde unter TwinCAT/Common64 oder Common32 nicht gefunden."
        )

    try:
        import pyads
    except ImportError as exc:
        raise RuntimeError(
            "pyads fehlt oder die TwinCAT-ADS-DLL konnte nicht geladen werden."
        ) from exc

    return pyads


def normalize_type(value: str) -> tuple[str, bool]:
    """Normalisiert TwinCAT-Datentypen für die typisierten ADS-Zugriffe."""

    raw = " ".join(str(value).upper().strip().split())

    # STRING(80), STRING(255) und ähnliche Varianten werden als STRING
    # behandelt. Die konkrete Stringlänge wird beim Symbolbrowser nicht für
    # den einfachen Lesezugriff benötigt.
    if raw.startswith("STRING"):
        return "STRING", True

    compact = raw.replace(" ", "").replace("_", "")
    compact = {
        "INT16": "INT",
        "UINT16": "UINT",
        "DINT32": "DINT",
        "UDINT32": "UDINT",
        "SHORT": "INT",
        "LONG": "DINT",
    }.get(compact, compact)

    data_type = _TYPE_ALIASES.get(compact)
    if data_type is None:
        return raw, False

    return data_type, True


def plc_type(data_type: str):
    """Liefert den passenden pyads-PLC-Typ."""

    pyads = pyads_module()

    return {
        "BOOL": pyads.PLCTYPE_BOOL,
        "INT": pyads.PLCTYPE_INT,
        "DINT": pyads.PLCTYPE_DINT,
        "UINT": pyads.PLCTYPE_UINT,
        "UDINT": pyads.PLCTYPE_UDINT,
        "REAL": pyads.PLCTYPE_REAL,
        "LREAL": pyads.PLCTYPE_LREAL,
        "TIME": pyads.PLCTYPE_UDINT,
        "STRING": pyads.PLCTYPE_STRING,
    }.get(data_type)


class AdsClient:
    """ADS-Verbindung mit Symbolbrowser, Lesen und kontrolliertem Schreiben."""

    def __init__(
        self,
        host: str,
        ams_net_id: str,
        port: int,
        timeout_seconds: float = 3.0,
        notification_cycle_ms: int = 100,
    ) -> None:
        self.host = host
        self.ams_net_id = ams_net_id
        self.port = int(port)
        self.timeout_seconds = float(timeout_seconds)
        self.notification_cycle_ms = int(notification_cycle_ms)

        self._plc = None
        self._connected = False
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> tuple[bool, str]:
        """Stellt die ADS-Verbindung her und verifiziert die Runtime."""

        try:
            pyads = pyads_module()
        except Exception as exc:
            return False, str(exc)

        with self._lock:
            if self._plc is not None:
                try:
                    self._plc.close()
                except Exception:
                    pass
                self._plc = None

            try:
                plc = pyads.Connection(
                    self.ams_net_id,
                    self.port,
                    self.host,
                )
                plc.open()

                # Die Verbindung gilt erst nach echter Kommunikation als
                # verifiziert. Das entspricht dem Verhalten des Analyseprojekts.
                device_info = plc.read_device_info()
                ads_state = plc.read_state()

                self._plc = plc
                self._connected = True

                device_name = device_info[0]
                state_value = ads_state[0]
                message = (
                    "ADS verifiziert - echte TwinCAT-Runtime\n"
                    f"Gerät: {device_name} | ADS-State: {state_value}"
                )
                logger.info(message.replace("\n", " | "))
                return True, message

            except Exception as exc:
                self._connected = False
                self._plc = None
                return False, f"ADS-Verbindung fehlgeschlagen: {exc}"

    def disconnect(self) -> None:
        """Schließt die ADS-Verbindung."""

        with self._lock:
            if self._plc is not None:
                try:
                    self._plc.close()
                except Exception as exc:
                    logger.warning("Fehler beim Schließen der ADS-Verbindung: %s", exc)

            self._plc = None
            self._connected = False

    def read_value(
        self,
        symbol: str,
        data_type: str,
    ) -> tuple[Any, bool, str]:
        """Liest ein Symbol mit dem erwarteten pyads-Datentyp."""

        with self._lock:
            if not self._connected or self._plc is None:
                return None, False, "ADS nicht verbunden"

            try:
                value_type = plc_type(data_type)
                if value_type is None:
                    return (
                        None,
                        False,
                        f"Nicht unterstützter Datentyp: {data_type}",
                    )

                value = self._plc.read_by_name(symbol, value_type)
                return value, True, ""

            except Exception as exc:
                logger.warning("Lesefehler für %s: %s", symbol, exc)
                return None, False, str(exc)

    def write_value(
        self,
        symbol: str,
        data_type: str,
        value: Any,
    ) -> tuple[bool, str]:
        """Schreibt einen bereits fachlich geprüften Wert über ADS.

        Die Whitelist-, Freigabe- und Sicherheitsprüfung erfolgt vor dem Aufruf
        dieser Methode im ControlService.
        """

        with self._lock:
            if not self._connected or self._plc is None:
                return False, "ADS nicht verbunden"

            try:
                value_type = plc_type(data_type)
                if value_type is None:
                    return False, f"Nicht unterstützter Datentyp: {data_type}"

                self._plc.write_by_name(symbol, value, value_type)
                logger.info("ADS-Schreibvorgang: %s = %r", symbol, value)
                return True, ""

            except Exception as exc:
                logger.warning("Schreibfehler für %s: %s", symbol, exc)
                return False, str(exc)

    def read_all_symbols(self) -> tuple[list[Symbol], str]:
        """Liest alle veröffentlichten ADS-Symbole der verbundenen Runtime."""

        with self._lock:
            if not self._connected or self._plc is None:
                return [], "ADS nicht verbunden"

            try:
                symbols = self._read_all_symbols_compat()
                logger.info("%d ADS-Symbole gelesen", len(symbols))
                return symbols, ""

            except Exception as exc:
                logger.exception("Fehler beim Lesen des ADS-Symboluploads")
                return [], f"ADS-Symbole konnten nicht gelesen werden: {exc}"

    def _read_all_symbols_compat(self) -> list[Symbol]:
        """Parst den ADS-Symbolupload kompatibel zu pyads 3.2.2.

        ADS-Symbolheader laut TwinCAT/pyads:

        - entryLength       UINT32
        - indexGroup        UINT32
        - indexOffset       UINT32
        - symbolSize        UINT32
        - adsDataType       UINT32
        - flags             UINT32
        - nameLength        UINT16
        - typeLength        UINT16
        - commentLength     UINT16

        Der Header ist damit 30 Byte groß. Danach folgen Name, Datentyp und
        Kommentar jeweils als nullterminierte Bytefolgen.
        """

        import ctypes

        pyads = pyads_module()

        try:
            from pyads import constants

            upload_info_group = constants.ADSIGRP_SYM_UPLOADINFO2
            upload_group = constants.ADSIGRP_SYM_UPLOAD
            device_state_offset = constants.ADSIOFFS_DEVDATA_ADSSTATE

        except (ImportError, AttributeError):
            # Fallbackwerte gemäß ADS-Spezifikation für pyads-Versionen, in
            # denen die Konstanten nicht exportiert werden.
            upload_info_group = 0xF00F
            upload_group = 0xF00B
            device_state_offset = 0

        info = self._plc.read(
            upload_info_group,
            device_state_offset,
            pyads.PLCTYPE_STRING,
            return_ctypes=True,
        )
        raw_info = bytes(info)

        if len(raw_info) < 8:
            raise RuntimeError(
                f"ADS-Symbolinfo ist zu kurz: {len(raw_info)} Byte"
            )

        symbol_count, symbol_bytes = struct.unpack_from("<II", raw_info, 0)

        if symbol_count < 0:
            raise RuntimeError("Ungültige Symbolanzahl")

        if symbol_bytes <= 0 or symbol_bytes > _MAX_SYMBOL_UPLOAD_BYTES:
            raise RuntimeError(
                f"Ungültige Größe der ADS-Symbolliste: {symbol_bytes} Byte"
            )

        payload_type = lambda size=symbol_bytes: create_string_buffer(size)
        payload = self._plc.read(
            upload_group,
            device_state_offset,
            payload_type,
            return_ctypes=True,
        )
        data = bytes(payload)

        symbols: list[Symbol] = []
        position = 0

        for index in range(symbol_count):
            if position + _SYMBOL_HEADER_SIZE > len(data):
                logger.warning(
                    "ADS-Symbolliste endet bei Eintrag %d/%d",
                    index,
                    symbol_count,
                )
                break

            (
                entry_size,
                index_group,
                index_offset,
                symbol_size,
                ads_data_type,
                flags,
                name_length,
                type_length,
                comment_length,
            ) = struct.unpack_from(
                _SYMBOL_HEADER_FORMAT,
                data,
                position,
            )

            del index_group, index_offset, symbol_size, ads_data_type, flags

            if entry_size < _SYMBOL_HEADER_SIZE:
                logger.warning(
                    "Ungültige Symbolgröße bei Eintrag %d: %d",
                    index,
                    entry_size,
                )
                break

            entry_end = position + entry_size
            if entry_end > len(data):
                logger.warning(
                    "Symboleintrag %d überschreitet die ADS-Symbolliste: %d > %d",
                    index,
                    entry_end,
                    len(data),
                )
                break

            text_start = position + _SYMBOL_HEADER_SIZE
            text_length = (
                name_length
                + 1
                + type_length
                + 1
                + comment_length
                + 1
            )

            if text_start + text_length > entry_end:
                logger.warning(
                    "Textlängen des Symbols %d sind ungültig",
                    index,
                )
                position = entry_end
                continue

            name_start = text_start
            type_start = name_start + name_length + 1
            comment_start = type_start + type_length + 1

            name = self._decode_ads_text(
                data[name_start:name_start + name_length]
            )
            tc_type = self._decode_ads_text(
                data[type_start:type_start + type_length]
            )
            comment = self._decode_ads_text(
                data[comment_start:comment_start + comment_length]
            )

            if name:
                data_type, supported = normalize_type(tc_type)
                symbols.append(
                    Symbol(
                        name=name,
                        tc_type=tc_type,
                        data_type=data_type,
                        comment=comment,
                        supported=supported,
                    )
                )

            position = entry_end

        return symbols

    @staticmethod
    def _decode_ads_text(value: bytes) -> str:
        """Dekodiert ADS-Texte robust für UTF-8 und Windows-1252."""

        value = value.split(b"\0", 1)[0]

        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue

        return value.decode("latin-1", errors="replace")
