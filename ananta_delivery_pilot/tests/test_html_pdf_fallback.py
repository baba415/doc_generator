from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from app.html_pdf import HtmlPdfRenderError, render_html_to_pdf


class HtmlPdfFallbackTests(unittest.TestCase):
    def test_render_uses_fallback_when_playwright_fails(self) -> None:
        html = "<html><body><h1>Fallback path proof</h1><p>Playwright unavailable</p></body></html>"
        called_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["playwright-cli", "pdf"],
            output="playwright failed",
            stderr="EADDRINUSE",
        )
        with patch("app.html_pdf._ensure_playwright_cache", return_value=None), patch(
            "app.html_pdf._run_command", side_effect=called_error
        ):
            pdf_bytes = render_html_to_pdf(html, doc_key="fallback-proof")

        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"Fallback PDF rendering used for fallback-proof", pdf_bytes)

    def test_render_raises_structured_error_when_primary_and_fallback_fail(self) -> None:
        html = "<html><body><h1>Structured failure proof</h1></body></html>"
        called_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["playwright-cli", "pdf"],
            output="playwright failed",
            stderr="missing browser",
        )
        with patch("app.html_pdf._ensure_playwright_cache", return_value=None), patch(
            "app.html_pdf._run_command", side_effect=called_error
        ), patch("app.html_pdf._render_fallback_pdf", side_effect=RuntimeError("fallback renderer crashed")):
            with self.assertRaises(HtmlPdfRenderError) as ctx:
                render_html_to_pdf(html, doc_key="fallback-proof")

        message = str(ctx.exception)
        self.assertIn("Render failed for fallback-proof", message)
        self.assertIn("Playwright:", message)
        self.assertIn("Fallback:", message)


if __name__ == "__main__":
    unittest.main()
