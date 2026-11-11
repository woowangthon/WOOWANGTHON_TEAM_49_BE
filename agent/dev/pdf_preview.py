"""Poppler-backed page previews for browsers without a native PDF viewer."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

from agent.services.hashing import content_hash

RENDER_LIMIT = threading.BoundedSemaphore(2)
RENDER_LOCK = threading.Lock()


def render_page(data, page, directory):
    if not 1 <= page <= 20:
        raise ValueError("Invalid page")
    executable = os.environ.get("AGENT_PDFTOPPM_PATH") or shutil.which("pdftoppm")
    if not executable:
        candidate = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe"
        )
        executable = str(candidate) if candidate.is_file() else None
    if not executable or not Path(executable).is_file():
        raise ValueError("PDF page renderer is not available; configure AGENT_PDFTOPPM_PATH")
    directory.mkdir(parents=True, exist_ok=True)
    fingerprint = content_hash(data)
    source = directory / (fingerprint + ".pdf")
    prefix = directory / f"{fingerprint}-{page}"
    target = prefix.with_suffix(".png")
    with RENDER_LIMIT, RENDER_LOCK:
        if not target.is_file():
            source.write_bytes(data)
            subprocess.run(
                [
                    executable,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-scale-to",
                    "1600",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return target.read_bytes()
