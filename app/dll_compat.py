"""Beckhoff/pyads-Kompatibilität für aktuelle TwinCAT-Installationen.

pyads 3.2.2 erwartet bei manchen Installationen den alten Pfad
TwinCAT\\3.1\\..\\AdsApi\\TcAdsDll\\x64.
Neuere TwinCAT-Installationen stellen TcAdsDll.dll dagegen unter
TwinCAT\\Common64 bereit.

Die DLL wird nicht kopiert oder verändert. Der fehlerhafte pyads-Pfad wird
nur beim Import auf das vorhandene Beckhoff-Verzeichnis umgeleitet.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Optional

_PATCHED = False
_PATH_PREPARED = False
_ORIGINAL_ADD_DLL_DIRECTORY = None


def find_ads_dll_directory() -> Optional[Path]:
    """Findet das vorhandene TwinCAT-Verzeichnis mit TcAdsDll.dll."""

    program_files_x86 = os.environ.get(
        "ProgramFiles(x86)",
        r"C:\Program Files (x86)",
    )
    program_files = os.environ.get(
        "ProgramFiles",
        r"C:\Program Files",
    )

    candidates = [
        Path(program_files_x86) / "Beckhoff" / "TwinCAT" / "Common64",
        Path(program_files) / "Beckhoff" / "TwinCAT" / "Common64",
    ]

    # 32-Bit-Python benötigt bei einer 32-Bit-TwinCAT-Installation Common32.
    if struct.calcsize("P") * 8 == 32:
        candidates = [
            path.with_name("Common32") for path in candidates
        ] + candidates

    for directory in candidates:
        if (directory / "TcAdsDll.dll").exists():
            return directory

    return None


def prepare_pyads_import() -> Optional[Path]:
    """Bereitet den pyads-Import vor und gibt das DLL-Verzeichnis zurück."""

    global _PATCHED, _PATH_PREPARED, _ORIGINAL_ADD_DLL_DIRECTORY

    real_directory = find_ads_dll_directory()
    if real_directory is None:
        return None

    # Den TwinCAT-Pfad höchstens einmal in PATH eintragen. Die bisherige
    # Version hat diesen Pfad bei jedem ADS-Lesezugriff erneut vorangestellt.
    # Dadurch ist PATH unter Windows immer weiter gewachsen und konnte die
    # Grenze von 32767 Zeichen überschreiten.
    if not _PATH_PREPARED:
        real_path = os.path.normcase(os.path.normpath(str(real_directory)))
        current_path = os.environ.get("PATH", "")
        entries = current_path.split(os.pathsep) if current_path else []

        # Bereits vorhandene identische Einträge entfernen. Andere PATH-
        # Einträge bleiben in ihrer bisherigen Reihenfolge erhalten.
        filtered_entries = []
        seen_real_path = False
        for entry in entries:
            normalized_entry = os.path.normcase(os.path.normpath(entry))
            if normalized_entry == real_path:
                if not seen_real_path:
                    filtered_entries.append(str(real_directory))
                    seen_real_path = True
                continue
            filtered_entries.append(entry)

        if not seen_real_path:
            filtered_entries.insert(0, str(real_directory))

        os.environ["PATH"] = os.pathsep.join(filtered_entries)
        _PATH_PREPARED = True

    # pyads 3.2.2 ruft unter Windows os.add_dll_directory() mit dem alten
    # AdsApi-Pfad auf. Nur diesen nicht vorhandenen Pfad umleiten.
    if not _PATCHED and hasattr(os, "add_dll_directory"):
        _ORIGINAL_ADD_DLL_DIRECTORY = os.add_dll_directory

        def redirected_add_dll_directory(path):
            requested = str(path)
            normalized = requested.replace("/", "\\").lower()

            is_old_pyads_path = (
                "adsapi" in normalized
                and "tcadsdll" in normalized
                and normalized.endswith("\\x64")
                and not Path(requested).exists()
            )

            if is_old_pyads_path:
                return _ORIGINAL_ADD_DLL_DIRECTORY(str(real_directory))

            return _ORIGINAL_ADD_DLL_DIRECTORY(path)

        os.add_dll_directory = redirected_add_dll_directory
        _PATCHED = True

    return real_directory
