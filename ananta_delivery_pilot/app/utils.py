from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=True)


def nfmt(value: float) -> str:
    return f"{value:,.2f}"


def amount_to_words_naira(value: float) -> str:
    # Lightweight conversion for operational readability; not legal-grade wording.
    ones = [
        "ZERO",
        "ONE",
        "TWO",
        "THREE",
        "FOUR",
        "FIVE",
        "SIX",
        "SEVEN",
        "EIGHT",
        "NINE",
        "TEN",
        "ELEVEN",
        "TWELVE",
        "THIRTEEN",
        "FOURTEEN",
        "FIFTEEN",
        "SIXTEEN",
        "SEVENTEEN",
        "EIGHTEEN",
        "NINETEEN",
    ]
    tens = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY"]

    def under_thousand(number: int) -> str:
        words = []
        if number >= 100:
            words.append(ones[number // 100])
            words.append("HUNDRED")
            number %= 100
        if number >= 20:
            words.append(tens[number // 10])
            if number % 10:
                words.append(ones[number % 10])
        elif number > 0:
            words.append(ones[number])
        return " ".join(words) if words else "ZERO"

    integer_value = int(Decimal(str(value)).quantize(Decimal("1")))
    if integer_value == 0:
        return "ZERO NAIRA ONLY"

    scales = ["", "THOUSAND", "MILLION", "BILLION", "TRILLION"]
    chunks = []
    scale = 0
    while integer_value:
        integer_value, remainder = divmod(integer_value, 1000)
        if remainder:
            chunk_words = under_thousand(remainder)
            scale_word = scales[scale]
            chunks.append(f"{chunk_words} {scale_word}".strip())
        scale += 1

    return " ".join(reversed(chunks)).strip() + " NAIRA ONLY"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(8192)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def txt_to_pdf(txt_path: Path, pdf_path: Path) -> None:
    ensure_dir(pdf_path.parent)
    command = ["/usr/sbin/cupsfilter", "-m", "application/pdf", str(txt_path)]
    process = subprocess.run(command, check=False, capture_output=True)
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"cupsfilter failed for {txt_path}: {stderr}")
    pdf_path.write_bytes(process.stdout)


def utc_timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"


def safe_write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def env_or_default(name: str, default: str) -> str:
    return os.environ.get(name, default)
