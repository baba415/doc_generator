from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


def _escape_pdf_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
        .replace("\n", "")
    )


def _jpeg_info(data: bytes) -> Tuple[int, int, int]:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise ValueError("Unsupported image format; expected JPEG")

    index = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }

    while index < len(data):
        while index < len(data) and data[index] != 0xFF:
            index += 1
        if index + 1 >= len(data):
            break

        marker = data[index + 1]
        index += 2

        if marker in (0xD8, 0xD9):
            continue

        if index + 1 >= len(data):
            break
        segment_len = (data[index] << 8) + data[index + 1]
        index += 2

        if marker in sof_markers:
            if index + 7 >= len(data):
                break
            height = (data[index + 1] << 8) + data[index + 2]
            width = (data[index + 3] << 8) + data[index + 4]
            components = data[index + 5]
            return width, height, components

        index += segment_len - 2

    raise ValueError("Unable to parse JPEG dimensions")


@dataclass
class _ImageDef:
    name: str
    path: Path
    width: int
    height: int
    color_space: str
    data: bytes


class PDFPage:
    def __init__(self, doc: "PDFDocument") -> None:
        self.doc = doc
        self.commands: List[str] = []
        self.image_keys: List[str] = []

    @property
    def width(self) -> float:
        return self.doc.page_width

    @property
    def height(self) -> float:
        return self.doc.page_height

    def _to_bottom_left_y(self, top_y: float) -> float:
        return self.height - top_y

    def line(
        self,
        x1: float,
        y1_top: float,
        x2: float,
        y2_top: float,
        width: float = 1.0,
        gray: float = 0.0,
    ) -> None:
        y1 = self._to_bottom_left_y(y1_top)
        y2 = self._to_bottom_left_y(y2_top)
        gray_val = min(1.0, max(0.0, gray))
        self.commands.append(
            f"q {gray_val:.3f} G {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S Q"
        )

    def rect(
        self,
        x: float,
        y_top: float,
        width: float,
        height: float,
        stroke_width: float = 1.0,
        stroke_gray: float = 0.0,
        fill_gray: float | None = None,
    ) -> None:
        y_bottom = self._to_bottom_left_y(y_top + height)
        stroke = min(1.0, max(0.0, stroke_gray))
        if fill_gray is None:
            self.commands.append(
                f"q {stroke:.3f} G {stroke_width:.2f} w {x:.2f} {y_bottom:.2f} {width:.2f} {height:.2f} re S Q"
            )
            return

        fill = min(1.0, max(0.0, fill_gray))
        self.commands.append(
            f"q {fill:.3f} g {stroke:.3f} G {stroke_width:.2f} w {x:.2f} {y_bottom:.2f} {width:.2f} {height:.2f} re B Q"
        )

    def text(
        self,
        x: float,
        y_top: float,
        value: str,
        size: float = 10.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        font_name = "F2" if bold else "F1"
        y = self._to_bottom_left_y(y_top)
        escaped = _escape_pdf_text(value)
        gray_val = min(1.0, max(0.0, gray))
        self.commands.append(
            f"q {gray_val:.3f} g BT /{font_name} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET Q"
        )

    def text_right(
        self,
        right_x: float,
        y_top: float,
        value: str,
        size: float = 10.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        width = self.text_width(value, size, bold=bold)
        x = right_x - width
        self.text(x, y_top, value, size=size, bold=bold, gray=gray)

    def text_center(
        self,
        center_x: float,
        y_top: float,
        value: str,
        size: float = 10.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        width = self.text_width(value, size, bold=bold)
        self.text(center_x - width / 2.0, y_top, value, size=size, bold=bold, gray=gray)

    def text_block(
        self,
        x: float,
        y_top: float,
        width: float,
        text: str,
        size: float = 10.0,
        line_height: float = 13.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> float:
        words = text.split()
        if not words:
            return y_top

        line = []
        current_y = y_top
        for word in words:
            candidate = " ".join(line + [word])
            if self.text_width(candidate, size, bold=bold) <= width:
                line.append(word)
                continue
            self.text(x, current_y, " ".join(line), size=size, bold=bold, gray=gray)
            current_y += line_height
            line = [word]

        if line:
            self.text(x, current_y, " ".join(line), size=size, bold=bold, gray=gray)
            current_y += line_height

        return current_y

    def image(self, path: Path, x: float, y_top: float, width: float, height: float) -> None:
        image_name = self.doc.register_image(path)
        y_bottom = self._to_bottom_left_y(y_top + height)
        self.commands.append(f"q {width:.2f} 0 0 {height:.2f} {x:.2f} {y_bottom:.2f} cm /{image_name} Do Q")
        if image_name not in self.image_keys:
            self.image_keys.append(image_name)

    @staticmethod
    def text_width(value: str, size: float = 10.0, bold: bool = False) -> float:
        if not value:
            return 0.0
        base = 0.515 if not bold else 0.535
        width = 0.0
        for char in value:
            if char in "ilI.,:;|!":
                width += size * (base - 0.23)
            elif char in "MW@#%&":
                width += size * (base + 0.18)
            elif char == " ":
                width += size * (base - 0.19)
            else:
                width += size * base
        return width


class PDFDocument:
    def __init__(self, page_width: float = 595.0, page_height: float = 842.0) -> None:
        self.page_width = page_width
        self.page_height = page_height
        self._pages: List[PDFPage] = []
        self._images_by_path: Dict[str, _ImageDef] = {}

    def new_page(self) -> PDFPage:
        page = PDFPage(self)
        page.commands.append(f"q 1 1 1 rg 0 0 {self.page_width:.2f} {self.page_height:.2f} re f Q")
        self._pages.append(page)
        return page

    def register_image(self, path: Path) -> str:
        key = str(path.resolve())
        if key in self._images_by_path:
            return self._images_by_path[key].name

        data = path.read_bytes()
        width, height, components = _jpeg_info(data)
        image_name = f"Im{len(self._images_by_path) + 1}"
        color_space = "/DeviceGray" if components == 1 else "/DeviceRGB"
        self._images_by_path[key] = _ImageDef(
            name=image_name,
            path=path,
            width=width,
            height=height,
            color_space=color_space,
            data=data,
        )
        return image_name

    def render(self) -> bytes:
        objects: List[bytes] = [b""]

        def add_object(payload: bytes) -> int:
            objects.append(payload)
            return len(objects) - 1

        # Fonts
        font_regular_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        font_bold_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

        image_obj_by_name: Dict[str, int] = {}
        for image in self._images_by_path.values():
            image_dict = (
                f"<< /Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} "
                f"/ColorSpace {image.color_space} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(image.data)} >>\n"
            ).encode("ascii")
            image_stream = image_dict + b"stream\n" + image.data + b"\nendstream"
            image_obj_by_name[image.name] = add_object(image_stream)

        pages_placeholder_id = add_object(b"<< /Type /Pages /Kids [] /Count 0 >>")
        page_ids: List[int] = []

        for page in self._pages:
            content_data = ("\n".join(page.commands) + "\n").encode("latin-1", errors="replace")
            content_stream = f"<< /Length {len(content_data)} >>\nstream\n".encode("ascii") + content_data + b"endstream"
            content_id = add_object(content_stream)

            image_resources = ""
            if page.image_keys:
                mappings = []
                for name in page.image_keys:
                    mappings.append(f"/{name} {image_obj_by_name[name]} 0 R")
                image_resources = f"/XObject << {' '.join(mappings)} >>"

            page_dict = (
                f"<< /Type /Page /Parent {pages_placeholder_id} 0 R "
                f"/MediaBox [0 0 {self.page_width:.2f} {self.page_height:.2f}] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> {image_resources} >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
            page_ids.append(add_object(page_dict))

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        pages_dict = f"<< /Type /Pages /Kids [ {kids} ] /Count {len(page_ids)} >>".encode("ascii")
        objects[pages_placeholder_id] = pages_dict

        catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_placeholder_id} 0 R >>".encode("ascii"))

        output = bytearray()
        output.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        offsets = [0] * len(objects)
        for object_id in range(1, len(objects)):
            offsets[object_id] = len(output)
            output.extend(f"{object_id} 0 obj\n".encode("ascii"))
            output.extend(objects[object_id])
            output.extend(b"\nendobj\n")

        xref_position = len(output)
        output.extend(f"xref\n0 {len(objects)}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for object_id in range(1, len(objects)):
            output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))

        output.extend(
            (
                f"trailer\n<< /Size {len(objects)} /Root {catalog_id} 0 R >>\n"
                f"startxref\n{xref_position}\n%%EOF\n"
            ).encode("ascii")
        )
        return bytes(output)
