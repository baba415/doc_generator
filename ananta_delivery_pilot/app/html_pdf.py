from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote


class HtmlPdfRenderError(RuntimeError):
    pass


PDF_LINK_RE = re.compile(r"\[Page as pdf\]\(([^)]+)\)")
MIN_PDF_BYTES = 5_000


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _extract_readable_lines(html: str, *, max_lines: int = 44) -> list[str]:
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?i)</?(p|div|br|li|tr|h1|h2|h3|h4|h5|h6|section|header|footer|table|thead|tbody|td|th)[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    lines: list[str] = []
    for raw in text.splitlines():
        cleaned = re.sub(r"\s+", " ", raw).strip()
        if not cleaned:
            continue
        while len(cleaned) > 92:
            lines.append(cleaned[:92])
            cleaned = cleaned[92:]
        lines.append(cleaned)
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def _render_fallback_pdf(*, html: str, doc_key: str, reason: str) -> bytes:
    headline = f"Fallback PDF rendering used for {doc_key}"
    reason_line = f"Reason: {reason}"[:180]
    lines = [headline, reason_line, "-----"] + _extract_readable_lines(html)
    if not lines:
        lines = [headline, reason_line, "No content extracted from HTML."]

    text_commands = ["BT", "/F1 10.5 Tf", "42 806 Td"]
    first_line = True
    for line in lines:
        escaped = _pdf_escape(line)
        if first_line:
            text_commands.append(f"({escaped}) Tj")
            first_line = False
        else:
            text_commands.append("0 -14 Td")
            text_commands.append(f"({escaped}) Tj")
    text_commands.append("ET")
    content_stream = "\n".join(text_commands).encode("utf-8")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content_stream)).encode("ascii")
        + b" >>\nstream\n"
        + content_stream
        + b"\nendstream",
    ]

    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_start = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF"
        ).encode("ascii")
    )
    return bytes(payload)


def _run_command(
    cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 90,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=True,
    )


def _ensure_playwright_cache(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    if any(target_dir.glob("chromium-*")):
        return

    default_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not default_cache.exists():
        return

    for child in default_cache.iterdir():
        if child.name == "daemon":
            continue
        dest = target_dir / child.name
        if dest.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)


def render_html_to_pdf(html: str, doc_key: str = "doc") -> bytes:
    with TemporaryDirectory(prefix="ananta_html_pdf_", dir="/tmp") as tmp_dir:
        work_dir = Path(tmp_dir)
        html_path = work_dir / "document.html"
        html_path.write_text(html, encoding="utf-8")

        cache_dir = Path(os.environ.get("ANANTA_PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright"))
        _ensure_playwright_cache(cache_dir)
        playwright_home = Path(os.environ.get("ANANTA_PLAYWRIGHT_HOME", "/tmp/ananta-playwright-home"))
        playwright_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache_dir)
        env["HOME"] = str(playwright_home)

        def _is_addr_in_use(stdout: str, stderr: str) -> bool:
            combined = f"{stdout}\n{stderr}".lower()
            return ("eaddrinuse" in combined) or ("address already in use" in combined)

        def _cleanup_stale_socket(session_name: str) -> None:
            base = Path(tempfile.gettempdir()) / "playwright-cli"
            if not base.exists():
                return
            for sock in base.rglob(f"{session_name}*"):
                try:
                    sock.unlink()
                except Exception:
                    pass

        def _new_session_name(attempt: str) -> str:
            # Keep it short to avoid unix socket path truncation collisions.
            return f"anp2-{attempt}-{uuid.uuid4().hex[:10]}"

        def _render_once(render_url: str, *, attempt: str) -> tuple[bytes, str]:
            pdf_filename = f"document-{attempt}.pdf"
            last_open_error: str = ""
            max_open_retries = 4
            for open_attempt in range(max_open_retries):
                session_name = _new_session_name(attempt)
                try:
                    # Best-effort cleanup in case a stale socket/session exists.
                    try:
                        _run_command(["playwright-cli", "--session", session_name, "close"], cwd=work_dir, env=env)
                    except Exception:
                        pass
                    _cleanup_stale_socket(session_name)

                    _run_command(["playwright-cli", "--session", session_name, "open"], cwd=work_dir, env=env)
                    _run_command(["playwright-cli", "--session", session_name, "goto", render_url], cwd=work_dir, env=env)
                    try:
                        _run_command(
                            ["playwright-cli", "--session", session_name, "run-code", "await page.waitForLoadState('networkidle')"],
                            cwd=work_dir,
                            env=env,
                        )
                    except Exception:
                        pass

                    pdf_result = _run_command(
                        ["playwright-cli", "--session", session_name, "pdf", "--filename", pdf_filename],
                        cwd=work_dir,
                        env=env,
                    )
                    combined = f"{pdf_result.stdout}\n{pdf_result.stderr}"
                    direct_pdf_path = work_dir / pdf_filename
                    if direct_pdf_path.exists():
                        pdf_path = direct_pdf_path
                    else:
                        match = PDF_LINK_RE.search(combined)
                        if match:
                            relative_pdf = match.group(1).strip()
                            pdf_path = work_dir / relative_pdf
                        else:
                            pdf_dir = work_dir / ".playwright-cli"
                            candidates = sorted(pdf_dir.glob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True)
                            pdf_path = candidates[0] if candidates else None

                    if not pdf_path or not pdf_path.exists():
                        raise HtmlPdfRenderError(
                            f"PDF output not found for {doc_key} ({attempt}). Playwright output: {combined.strip()}"
                        )
                    return pdf_path.read_bytes(), combined
                except subprocess.CalledProcessError as error:
                    stdout = error.stdout or ""
                    stderr = error.stderr or ""
                    if _is_addr_in_use(stdout, stderr) and open_attempt < (max_open_retries - 1):
                        last_open_error = f"{stdout.strip()} {stderr.strip()}".strip()
                        _cleanup_stale_socket(session_name)
                        continue
                    raise HtmlPdfRenderError(
                        f"Playwright render failed for {doc_key} ({attempt}): {stdout.strip()} {stderr.strip()}".strip()
                    ) from error
                finally:
                    try:
                        _run_command(["playwright-cli", "--session", session_name, "close"], cwd=work_dir, env=env)
                    except Exception:
                        pass
            raise HtmlPdfRenderError(
                f"Playwright render failed for {doc_key} ({attempt}) after retries: {last_open_error}".strip()
            )

        primary_error = ""
        try:
            file_url = html_path.as_uri()
            pdf_bytes, combined_output = _render_once(file_url, attempt="file")
            if len(pdf_bytes) >= MIN_PDF_BYTES:
                return pdf_bytes

            # Some local environments fail to navigate `file://` reliably via playwright-cli.
            # Retry with a data URL before giving up.
            data_url = "data:text/html;charset=utf-8," + quote(html, safe="")
            retry_bytes, retry_output = _render_once(data_url, attempt="data")
            if len(retry_bytes) >= MIN_PDF_BYTES:
                return retry_bytes
            primary_error = (
                f"PDF output too small ({len(retry_bytes)} bytes) for {doc_key} (likely blank). "
                f"File attempt output: {combined_output.strip()} | Data attempt output: {retry_output.strip()}"
            )
        except Exception as error:  # noqa: BLE001
            primary_error = str(error)

        try:
            return _render_fallback_pdf(html=html, doc_key=doc_key, reason=primary_error or "Unknown Playwright failure")
        except Exception as fallback_error:  # noqa: BLE001
            raise HtmlPdfRenderError(
                f"Render failed for {doc_key}. Playwright: {primary_error or 'n/a'} | Fallback: {fallback_error}"
            ) from fallback_error
