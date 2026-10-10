"""
Documents attached to a question, read on this computer: Word, PDF, Excel, PowerPoint, OpenDocument, RTF,
web pages and plain text become text every model can read, and the pictures inside them go to the models
that see pictures.

Nothing is sent anywhere and nothing is written to disk: the file is read from memory, by Python's own zip
and XML readers, and by pypdf for PDFs. Nothing in a document runs or is followed: macros, links, fields and
embedded files are left alone. What a document says about who wrote it (its properties, comment and revision
authors) isn't read. Text Word hides is kept, marked [hidden text: …], so a model can point it out.

    doc = read_document(data, "report.docx")      # or read_path("report.docx")
    doc.text       # for every model; where each picture was is marked [Picture 1], [Picture 2] …
    doc.images     # those pictures as they're stored (PNG, JPEG, GIF, BMP or WebP): the Ixel window
                   # turns them into what models take; pictures_of() does it here for PNG and JPEG
    doc.notes      # for the person: what was left out, and why
"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import struct
import time
import zipfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 50 * 1024 * 1024      # a document of any kind (the Ixel window sends at most this)
MAX_TEXT_CHARS = 60_000                 # material.MAX_MATERIAL_CHARS: what the panel reads, all files together
MAX_SECONDS = 30.0                      # reading one document; checked between pages, slides and parts
MAX_IMAGES = 8                          # pictures.MAX_PER_QUESTION
MAX_IMAGE_BYTES = 32 * 1024 * 1024      # one picture in a document, as stored
MAX_IMAGE_PIXELS = 40_000_000           # a PDF's raw picture made into a PNG
MIN_IMAGE_SIDE = 32                     # smaller ones are icons, bullets and lines, not pictures
MAX_ZIP_PARTS = 10_000
MAX_ZIP_UNPACKED = 512 * 1024 * 1024    # all parts, as the file declares them
MAX_PART_BYTES = 32 * 1024 * 1024       # one XML part read whole
MAX_PDF_PAGES = 2_000
EXCEL_COLUMNS = 16_384                  # A to XFD
MAX_NESTING = 500                       # groups inside groups (RTF)
MAX_REPEAT = 1_000                      # a repeated row or cell in an OpenDocument spreadsheet, written at most

# What a browser shows, so the Ixel window can turn it into a PNG or JPEG
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp",
               "webp": "image/webp"}

WORD = {".docx", ".docm", ".dotx", ".dotm"}
EXCEL = {".xlsx", ".xlsm", ".xltx", ".xltm"}
POWERPOINT = {".pptx", ".pptm", ".potx", ".ppsx"}
OPENDOCUMENT = {".odt", ".ott", ".ods", ".ots", ".odp", ".otp"}
OLD_OFFICE = {".doc": "Word", ".dot": "Word", ".xls": "Excel", ".ppt": "PowerPoint", ".pps": "PowerPoint"}
WEB = {".html", ".htm", ".xhtml"}
PICTURES = {".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff"}
# Files Ixel reads as documents rather than as plain text (and pictures, which are pictures)
DOCUMENTS = WORD | EXCEL | POWERPOINT | OPENDOCUMENT | WEB | {".pdf", ".rtf"} | set(OLD_OFFICE)

_PDF_LOGGER = logging.getLogger("pypdf")


class DocumentError(ValueError):
    """Said to the person as it is: the document couldn't be read."""


@dataclass
class Image:
    data: bytes
    media_type: str          # one of IMAGE_TYPES
    width: int
    height: int
    where: str = ""          # "page 3", "slide 2", "sheet Results"


@dataclass
class Document:
    name: str
    kind: str                # "Word document", "PDF" …, for the person
    text: str
    images: list[Image] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


def suffix(name: str) -> str:
    return Path(name.replace("\\", "/")).suffix.lower()


def is_document(name: str) -> bool:
    """Whether Ixel reads a file of this name as a document (not as plain text, and not as a picture)."""
    return suffix(name) in DOCUMENTS


def is_picture(name: str) -> bool:
    return suffix(name) in PICTURES


def read_path(path: str | Path) -> Document:
    """A document on disk, read whole into memory first (at most MAX_FILE_BYTES)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise DocumentError(f"{path.name} is {_mb(size)}; documents can be up to {_mb(MAX_FILE_BYTES)}.")
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise DocumentError(f"{path.name}: couldn't read it ({exc.strerror or exc}).") from None
    return read_document(data, path.name)


def read_document(data: bytes, name: str, *, max_chars: int = MAX_TEXT_CHARS) -> Document:
    """
    A document's text and pictures, from its bytes and file name (which says what kind it is; a PDF or a
    zip-based Office file is also known by its first bytes). Plain text of any kind is read as text.
    DocumentError when it can't be read, with what to do instead.
    """
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"{name} is {_mb(len(data))}; documents can be up to {_mb(MAX_FILE_BYTES)}.")
    ext = suffix(name)
    if ext in OLD_OFFICE:
        raise DocumentError(f"{name} is an old {OLD_OFFICE[ext]} file, which Ixel can't read. Save it as "
                            f".{'docx' if OLD_OFFICE[ext] == 'Word' else 'xlsx' if OLD_OFFICE[ext] == 'Excel' else 'pptx'}"
                            " (File > Save As) and attach that.")
    reader = _Reader(name, max_chars)
    try:
        if ext == ".pdf" or data.startswith(b"%PDF-"):
            reader.pdf(data)
        elif ext in WORD | EXCEL | POWERPOINT | OPENDOCUMENT:
            reader.package(data, ext)
        elif ext == ".rtf" or data.startswith(b"{\\rtf"):
            reader.rtf(data)
        elif ext in WEB:
            reader.kind = "web page"
            reader.html(_decode(data, name))
        else:
            reader.kind = "text file"
            text = _decode(data, name).replace("\r\n", "\n")
            reader.run(lambda: reader.add(text))
        return reader.document()
    except DocumentError:
        raise
    except RecursionError:  # parts inside parts, a thousand deep: no document is
        raise DocumentError(f"{name} is nested too deeply to be a document Ixel reads.") from None
    except MemoryError:
        raise DocumentError(f"{name} is too big to read.") from None
    except Exception:  # noqa: BLE001 — a damaged or unusual file is something to tell the person, never a crash
        raise DocumentError(f"{name} couldn't be read: it may be damaged. Try saving it again, or as a PDF.") \
            from None


def pictures_of(document: Document, room: int = MAX_IMAGES, first: int = 1, max_bytes: int | None = None):
    """
    The document's PNG and JPEG pictures as pictures.Picture (cleaned of their metadata, as every picture
    Ixel sends is), up to room of them and max_bytes in all, with a note for each kind left out, and its text
    with the pictures numbered from first on, as they're sent (one left out says so). For the command line:
    the Ixel window converts the other kinds, and shrinks big ones, before they're sent. → (pictures, notes, text)
    """
    from ixel_mat.pictures import MAX_BYTES, MAX_PER_QUESTION_BYTES, PictureError, megabytes, read_picture
    max_bytes = MAX_PER_QUESTION_BYTES if max_bytes is None else max_bytes
    found, notes, other, too_big, many, over = [], [], 0, 0, 0, 0
    numbers: dict[str, str] = {}
    for i, image in enumerate(document.images, 1):
        numbers[str(i)] = _LEFT_OUT
        if image.media_type not in ("image/png", "image/jpeg"):
            other += 1
            continue
        if len(image.data) > MAX_BYTES:
            too_big += 1
            continue
        if len(found) >= room:
            many += 1
            continue
        try:
            picture = read_picture(image.data)
        except PictureError:
            other += 1
            continue
        if sum(len(p.data) for p in found) + len(picture.data) > max_bytes:
            over += 1
            continue
        found.append(picture)
        numbers[str(i)] = f"[Picture {first + len(found) - 1}]"
    if too_big:
        notes.append(f"{document.name}: {_count(too_big, 'picture')} over {megabytes(MAX_BYTES)} left out "
                     "(attach the document in the Ixel window, which makes them smaller).")
    if other:
        notes.append(f"{document.name}: {_count(other, 'picture')} in a kind the command line can't send left out "
                     "(the Ixel window converts them).")
    if many:
        notes.append(f"{document.name}: {_count(many, 'picture')} left out (a question takes at most {MAX_IMAGES}).")
    if over:
        notes.append(f"{document.name}: {_count(over, 'picture')} left out: with them, the question's pictures "
                     f"would be over {megabytes(MAX_PER_QUESTION_BYTES)}, more than a model takes at once.")
    text = _PICTURE.sub(lambda m: numbers.get(m.group(1), m.group(0)), document.text)
    return found, notes, text


_PICTURE = re.compile(r"\[Picture (\d+)\]")
_LEFT_OUT = "[picture left out]"  # in place of a [Picture N] that isn't sent: the reader leaves room for it


def shown(text: str) -> int:
    """How long text is as the models see it, hidden characters shown as [U+…]: what the panel's limit counts
    (material.check)."""
    from ixel_mat.material import mark_hidden_characters
    return len(mark_hidden_characters(text))


def _fitting(text: str, room: int) -> str:
    """The longest start of text that's at most room long as the models see it."""
    low, high = 0, max(min(len(text), room), 0)
    while low < high:
        middle = (low + high + 1) // 2
        if shown(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def numbered(document: Document, first: int = 1, room: int = MAX_IMAGES, max_bytes: int = 64 * 1024 * 1024):
    """
    For the Ixel window, which converts and shrinks pictures itself: the document's text with its pictures
    numbered from first on, as they'll be attached, the first room of them (and no more than max_bytes in
    all), and a note if the rest are left out. → (text, images, notes)
    """
    images, numbers, size, many, over = [], {}, 0, 0, 0
    for i, image in enumerate(document.images, 1):
        numbers[str(i)] = _LEFT_OUT
        if len(images) >= max(room, 0):
            many += 1
        elif size + len(image.data) > max_bytes:
            over += 1
        else:
            images.append(image)
            size += len(image.data)
            numbers[str(i)] = f"[Picture {first + len(images) - 1}]"
    notes = []
    if many:
        notes.append(f"{document.name}: {_count(many, 'picture')} left out (a question takes at most {MAX_IMAGES}).")
    if over:
        notes.append(f"{document.name}: {_count(over, 'picture')} left out: together they're over "
                     f"{_mb(max_bytes)}.")
    return _PICTURE.sub(lambda m: numbers.get(m.group(1), m.group(0)), document.text), images, notes


# ── Reading ───────────────────────────────────────────────────────────────────

def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + " MB"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _decode(data: bytes, name: str) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16, as PowerShell 5.1 and Notepad write it
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:8192]:
        raise DocumentError(f"{name} isn't a document or text Ixel can read. It reads Word, PDF, Excel, "
                            "PowerPoint, OpenDocument, RTF, web pages and text files, and pictures.")
    return data.decode("utf-8-sig", errors="replace")


def _local(tag) -> str:
    """An XML name without its namespace: Office's strict and transitional files name things alike."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(element, name: str, default: str = "") -> str:
    """An attribute by its name without namespace (r:embed, xlink:href, w:val …)."""
    for key, value in (element.attrib.items() if element is not None else ()):
        if _local(key) == name:
            return value
    return default


def _rid(element) -> str:
    """A relationship's id (r:id), not the element's own id (a slide's id="256" r:id="rId2")."""
    for key, value in (element.attrib.items() if element is not None else ()):
        if key.startswith("{") and _local(key) == "id":
            return value
    return ""


def _child(element, name: str):
    return next((c for c in element if _local(c.tag) == name), None) if element is not None else None


def _children(element, name: str):
    return [c for c in element if _local(c.tag) == name] if element is not None else []


def _find(element, *path: str):
    for name in path:
        element = _child(element, name)
        if element is None:
            return None
    return element


def _xml(data: bytes, part: str) -> ET.Element:
    # A DTD is how XML declares entities (the "billion laughs" that fill memory); no Office or OpenDocument part
    # has one, and text can't spell one (it would be &lt;!DOCTYPE)
    for marker in ("<!DOCTYPE", "<!ENTITY"):
        if any(marker.encode(encoding) in data for encoding in ("ascii", "utf-16-le", "utf-16-be")):
            raise DocumentError(f"Part of this file ({part}) declares a DTD, which documents never need, so it "
                                "isn't read.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentError(f"Part of this file is damaged ({part}: {exc}).") from None
    except (ValueError, LookupError):  # an encoding Python doesn't have, or can't parse with
        raise DocumentError(f"Part of this file ({part}) is in an encoding Ixel can't read.") from None


def image_size(data: bytes) -> tuple[str, int, int]:
    """(kind, width, height) of a picture by its first bytes; kind is a key of IMAGE_TYPES, or "" for another."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return ("png", *struct.unpack(">II", data[16:24]))
        if data.startswith(b"\xff\xd8"):
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    pos += 2 if marker != 0xFF else 1
                    continue
                length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
                    return "jpeg", width, height
                pos += 2 + length
            return "jpeg", 0, 0
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ("gif", *struct.unpack("<HH", data[6:10]))
        if data.startswith(b"BM") and len(data) >= 26:
            width, height = struct.unpack("<ii", data[18:26])
            return "bmp", abs(width), abs(height)
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return "webp", width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return "webp", int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            return "webp", 0, 0
    except struct.error:
        pass
    return "", 0, 0


def _png(width: int, height: int, gray: bool, pixels: bytes) -> bytes:
    """A PNG of 8-bit pixels, rows unfiltered."""
    row = width * (1 if gray else 3)
    raw = b"".join(b"\0" + pixels[y * row:(y + 1) * row] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 0 if gray else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


class _Full(Exception):
    """The text has reached the panel's limit: reading stops."""


class _Reader:
    def __init__(self, name: str, max_chars: int):
        self.name = name
        self.kind = "document"
        self.max_chars = max_chars
        self.end = (f"\n\n[… the rest of {name} was left out: the panel reads up to {MAX_TEXT_CHARS:,} "
                    "characters.]")
        # For the text, leaving room for the line that ends it. Counted as the models will see it (shown(): hidden
        # characters as [U+…]), which is how the panel's limit is checked
        self.room = max(max_chars - shown(self.end) - 1, 0)
        self.parts: list[str] = []
        self.size = 0
        self.where = ""          # the page, slide or sheet being read, for a note if the text is cut there
        self.of = ""             # " of 40 pages" when that's known
        self.cut = False
        self.images: list[Image] = []
        self.left_out = {"many": 0, "kind": 0, "big": 0}
        self.notes: list[str] = []
        self.started = time.monotonic()
        self.zip: zipfile.ZipFile | None = None
        self.unpacked = 0

    # Text

    def add(self, text: str) -> None:
        """A block of text (a paragraph, a table, a page), cut at the limit, which ends the reading."""
        text = text.strip("\n")
        if not text.strip():
            return
        room = self.room - self.size
        size = shown(text) + 2
        if size > room:
            cut = _fitting(text, room - 2)
            line = cut.rfind("\n")
            if line > len(cut) * 0.6:
                cut = cut[:line]
            if cut.strip():
                self.parts.append(cut.rstrip())
                self.size += shown(self.parts[-1]) + 2
            self.cut = True
            raise _Full
        self.parts.append(text)
        self.size += size

    def tick(self) -> None:
        if time.monotonic() - self.started > MAX_SECONDS:
            raise DocumentError(f"Reading {self.name} took over {MAX_SECONDS:.0f} seconds, so it was stopped. "
                                "Attach a smaller part of it.")

    # Pictures

    def picture(self, data: bytes | None, where: str = "") -> str:
        """Keep a picture found in the document; what the text says in its place."""
        if not data:
            return ""
        kind, width, height = image_size(data)
        if 0 < width < MIN_IMAGE_SIDE or 0 < height < MIN_IMAGE_SIDE:
            return ""
        if not kind:
            self.left_out["kind"] += 1
            return "[picture left out: a kind Ixel can't send]"
        if len(data) > MAX_IMAGE_BYTES:
            self.left_out["big"] += 1
            return "[picture left out: too big]"
        if len(self.images) >= MAX_IMAGES:
            self.left_out["many"] += 1
            return "[picture left out]"
        self.images.append(Image(data, IMAGE_TYPES[kind], width, height, where or self.where))
        self.size += len(_LEFT_OUT) - len("[Picture 1]")  # room for it to say it was left out, if it is
        return f"[Picture {len(self.images)}]"

    def document(self) -> Document:
        text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(self.parts)).strip()
        if self.cut and text:
            where = f" It stops partway through {self.where}{self.of}." if self.where else ""
            self.notes.append(f"{self.name}: only the first {len(text):,} characters fit (the panel reads up to "
                              f"{MAX_TEXT_CHARS:,} in all).{where}")
            text += self.end
        elif self.cut and self.images:
            self.notes.append(f"{self.name}: its text left out, as there was no room for it (the panel reads up "
                              f"to {MAX_TEXT_CHARS:,} characters in all).")
        if self.left_out["many"]:
            self.notes.append(f"{self.name}: {_count(self.left_out['many'], 'more picture')} left out "
                              f"(a question takes at most {MAX_IMAGES}).")
        if self.left_out["kind"]:
            self.notes.append(f"{self.name}: {_count(self.left_out['kind'], 'picture')} in a kind Ixel can't send "
                              "(such as EMF, TIFF or JPEG 2000) left out.")
        if self.left_out["big"]:
            self.notes.append(f"{self.name}: {_count(self.left_out['big'], 'picture')} over "
                              f"{_mb(MAX_IMAGE_BYTES)} left out.")
        if not text and not self.images:
            raise DocumentError(f"{self.name} has no text or pictures Ixel can read.")
        return Document(self.name, self.kind, text + "\n" if text else "", self.images, self.notes)

    def run(self, read) -> None:
        """Read, until the text is full."""
        try:
            read()
        except _Full:
            pass

    # Zip-based files: Word, Excel, PowerPoint, OpenDocument

    def package(self, data: bytes, ext: str) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
            if data.startswith(b"\xd0\xcf\x11\xe0"):  # an encrypted Office file is kept in an old-style container
                raise DocumentError(f"{self.name} is password-protected. Save a copy without the password and "
                                    "attach that.") from None
            raise DocumentError(f"{self.name} is damaged or isn't really a {ext} file.") from None
        if len(infos) > MAX_ZIP_PARTS or sum(i.file_size for i in infos) > MAX_ZIP_UNPACKED:
            raise DocumentError(f"{self.name} unpacks to far more than a document does, so it isn't read.")
        self.by_name = {i.filename.lower(): i for i in infos}  # Office's part names are case-insensitive
        if ext in WORD:
            self.kind = "Word document"
            self.run(self.word)
        elif ext in EXCEL:
            self.kind = "Excel workbook"
            self.run(self.excel)
        elif ext in POWERPOINT:
            self.kind = "PowerPoint deck"
            self.run(self.powerpoint)
        else:
            self.kind = "OpenDocument file"
            self.run(self.opendocument)

    def part(self, name: str, cap: int = MAX_PART_BYTES) -> bytes | None:
        info = self.by_name.get(name.lower())
        if info is None:
            return None
        self.tick()
        if info.file_size > cap:
            raise DocumentError(f"Part of {self.name} ({name}) is {_mb(info.file_size)} unpacked, too big to read.")
        if self.unpacked + info.file_size > MAX_ZIP_UNPACKED:
            raise DocumentError(f"{self.name} unpacks to far more than a document does, so it isn't read.")
        try:
            with self.zip.open(info) as handle:
                data = handle.read(cap + 1)
        except RuntimeError:  # a zip password
            raise DocumentError(f"{self.name} is password-protected. Save a copy without the password and attach "
                                "that.") from None
        except (zipfile.BadZipFile, OSError, EOFError, zlib.error, NotImplementedError):
            raise DocumentError(f"Part of {self.name} ({name}) is damaged.") from None
        if len(data) > cap:  # it said it was smaller
            raise DocumentError(f"Part of {self.name} ({name}) is too big to read.")
        self.unpacked += len(data)  # what it really was, whatever the zip said
        if self.unpacked > MAX_ZIP_UNPACKED:
            raise DocumentError(f"{self.name} unpacks to far more than a document does, so it isn't read.")
        return data

    def xml(self, name: str) -> ET.Element | None:
        data = self.part(name)
        return None if data is None else _xml(data, name)

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """A part's relationships: id → (type, the part it points to). Links to outside the file are left out."""
        folder, base = posixpath.split(part)
        root = self.xml(posixpath.join(folder, "_rels", base + ".rels"))
        found = {}
        for rel in root if root is not None else []:
            if _attr(rel, "TargetMode") == "External":
                continue
            target = _attr(rel, "Target")
            target = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
            found[_attr(rel, "Id")] = (_attr(rel, "Type").rsplit("/", 1)[-1], posixpath.normpath(target))
        return found

    def media(self, rels: dict, rid: str, where: str = "") -> str:
        kind, target = rels.get(rid, ("", ""))
        if kind != "image":
            return ""
        return self.picture(self.part(target, MAX_IMAGE_BYTES + 1) if target else None, where)

    # Word

    def word(self) -> None:
        main = "word/document.xml"
        content_types = self.xml("[Content_Types].xml")
        for override in content_types if content_types is not None else []:
            if _attr(override, "ContentType").endswith((".document.main+xml", ".template.main+xml",
                                                         ".document.macroEnabled.main+xml")):
                main = _attr(override, "PartName").lstrip("/")
        root = self.xml(main)
        if root is None:
            raise DocumentError(f"{self.name} has no document in it.")
        self.word_rels = self.rels(main)
        self.word_styles = {}
        styles = self.xml(posixpath.join(posixpath.dirname(main), "styles.xml"))
        for style in _children(styles, "style"):
            name = _attr(_child(style, "name"), "val").lower() if _child(style, "name") is not None else ""
            level = _attr(_find(style, "pPr", "outlineLvl"), "val") if _find(style, "pPr", "outlineLvl") is not None else ""
            self.word_styles[_attr(style, "styleId")] = (name, level)
        body = _child(root, "body")
        self.notes_text: list[str] = []
        self.word_blocks(body)
        for which in ("footnotes", "endnotes"):
            kind = which[:-1]
            target = next((t for k, t in self.word_rels.values() if k == which), None)
            notes = self.xml(target) if target else None
            for note in _children(notes, kind):
                if _attr(note, "type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                text = " ".join(self.word_paragraph(p) for p in note.iter() if _local(p.tag) == "p").strip()
                if text:
                    self.notes_text.append(f"[{kind} {_attr(note, 'id')}] {text}")
        if self.notes_text:
            self.add("Notes:\n" + "\n".join(self.notes_text))

    def word_blocks(self, parent) -> None:
        for element in parent if parent is not None else []:
            name = _local(element.tag)
            if name == "p":
                self.add(self.word_paragraph(element, block=True))
            elif name == "tbl":
                self.add(self.word_table(element))
            elif name == "sdt":
                self.word_blocks(_child(element, "sdtContent"))
            elif name in ("customXml", "ins", "moveTo", "smartTag"):
                self.word_blocks(element)
            self.tick()

    def word_paragraph(self, p, block: bool = False) -> str:
        segments: list[tuple[str, bool]] = []
        self.word_inline(p, segments, False)
        text, hidden_run = "", []
        for piece, hidden in segments + [("", False)]:
            if hidden:
                hidden_run.append(piece)
                continue
            if hidden_run and "".join(hidden_run).strip():
                text += f"[hidden text: {''.join(hidden_run).strip()}]"
            hidden_run = []
            text += piece
        text = text.strip()
        if not block or not text:
            return text
        style = _attr(_find(p, "pPr", "pStyle"), "val") if _find(p, "pPr", "pStyle") is not None else ""
        name, level = self.word_styles.get(style, ("", ""))
        own = _find(p, "pPr", "outlineLvl")
        level = _attr(own, "val") if own is not None else level
        if name == "title":
            return f"# {text}"
        heading = re.fullmatch(r"heading (\d)", name)
        if heading or (level.isdigit() and int(level) < 9):
            return "#" * min(int(heading.group(1)) if heading else int(level) + 1, 6) + " " + text
        numbering = _find(p, "pPr", "numPr")
        if numbering is not None:
            depth = _attr(_child(numbering, "ilvl"), "val") if _child(numbering, "ilvl") is not None else "0"
            return "  " * (int(depth) if depth.isdigit() else 0) + "- " + text
        return text

    def word_inline(self, element, segments: list, hidden: bool) -> None:
        for child in element:
            name = _local(child.tag)
            if name in ("pPr", "rPr", "delText", "instrText", "delInstrText", "del", "moveFrom", "fldData"):
                continue
            if name == "r":
                vanish = _find(child, "rPr", "vanish")
                run_hidden = hidden or (vanish is not None and _attr(vanish, "val", "true") not in ("0", "false", "off"))
                self.word_inline(child, segments, run_hidden)
            elif name == "t":
                segments.append((child.text or "", hidden))
            elif name == "tab":
                segments.append(("\t", hidden))
            elif name in ("br", "cr"):
                segments.append(("\n", hidden))
            elif name == "noBreakHyphen":
                segments.append(("-", hidden))
            elif name in ("footnoteReference", "endnoteReference"):
                segments.append((f"[{name[:-9]} {_attr(child, 'id')}]", hidden))
            elif name in ("blip", "imagedata"):
                segments.append((self.media(self.word_rels, _attr(child, "embed") or _rid(child)), False))
            elif name == "txbxContent":
                inner = "\n".join(t for t in (self.word_paragraph(p, block=True) for p in child
                                              if _local(p.tag) == "p") if t)
                if inner:
                    segments.append((f"\n{inner}\n", hidden))
            elif name == "tbl":
                segments.append(("\n" + self.word_table(child) + "\n", hidden))
            else:
                self.word_inline(child, segments, hidden)

    def word_table(self, table) -> str:
        rows = []
        for row in table.iter():
            if _local(row.tag) != "tr":
                continue
            cells = []
            for cell in row:
                if _local(cell.tag) == "sdt":
                    cell = _child(cell, "sdtContent")
                    cell = _child(cell, "tc") if cell is not None else None
                if cell is None or _local(cell.tag) != "tc":
                    continue
                cells.append(" ".join(self.word_paragraph(p) for p in cell if _local(p.tag) == "p").strip())
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    # Excel

    def excel(self) -> None:
        book = self.xml("xl/workbook.xml")
        if book is None:
            raise DocumentError(f"{self.name} has no workbook in it.")
        rels = self.rels("xl/workbook.xml")
        shared = []
        strings = self.xml(next((t for k, t in rels.values() if k == "sharedStrings"), "xl/sharedStrings.xml"))
        for item in _children(strings, "si"):
            shared.append("".join(t.text or "" for t in self._texts(item, skip=("rPh",))))
        dates = self._excel_date_styles()
        base1904 = _attr(_child(book, "workbookPr"), "date1904") in ("1", "true")
        sheets = _children(_child(book, "sheets"), "sheet")
        self.of = f" of {len(sheets)} sheets" if len(sheets) > 1 else ""
        for sheet in sheets:
            name = _attr(sheet, "name")
            if _attr(sheet, "state") in ("hidden", "veryHidden"):
                name += " (hidden)"
            self.where = f"sheet {name}"
            kind, target = rels.get(_rid(sheet), ("", ""))
            root = self.xml(target) if kind == "worksheet" else None
            rows = []
            for row in _children(_child(root, "sheetData"), "row"):
                cells: dict[int, str] = {}
                for cell in _children(row, "c"):
                    value = self._excel_value(cell, shared, dates, base1904)
                    column = _column(_attr(cell, "r")) if _attr(cell, "r") else len(cells)
                    if value and column < EXCEL_COLUMNS:  # past Excel's last column, XFD, a cell is made up
                        cells[column] = value
                if cells:
                    rows.append(" | ".join(cells.get(i, "") for i in range(max(cells) + 1)))
                if len(rows) % 200 == 0:
                    self.tick()
            if rows:
                self.add(f"## Sheet: {name}\n" + "\n".join(rows))
            for rel_kind, part in (self.rels(target).values() if target else []):
                if rel_kind == "drawing":  # pictures placed on the sheet
                    drawing_rels = self.rels(part)
                    self.add(" ".join(m for m in (self.media(drawing_rels, rid) for rid in drawing_rels) if m))

    def _excel_date_styles(self) -> set[int]:
        """The cell styles (by index) that show a number as a date."""
        styles = self.xml("xl/styles.xml")
        custom = {}
        for fmt in _children(_child(styles, "numFmts"), "numFmt"):
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", _attr(fmt, "formatCode")).lower()
            custom[_attr(fmt, "numFmtId")] = bool(re.search(r"[dy]|m(?!.*s)", code)) and "h" not in code
        found = set()
        for index, xf in enumerate(_children(_child(styles, "cellXfs"), "xf")):
            fmt = _attr(xf, "numFmtId", "0")
            if (fmt.isdigit() and (14 <= int(fmt) <= 17 or int(fmt) in (22, 30, 57, 58))) or custom.get(fmt):
                found.add(index)
        return found

    @staticmethod
    def _excel_value(cell, shared: list[str], dates: set[int], base1904: bool) -> str:
        kind = _attr(cell, "t", "n")
        value = _child(cell, "v")
        value = value.text if value is not None and value.text else ""
        if kind == "s":
            return shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
        if kind == "inlineStr":
            return "".join(t.text or "" for t in _Reader._texts(_child(cell, "is")))
        if kind == "b":
            return "TRUE" if value == "1" else "FALSE"
        if kind == "n" and value and _attr(cell, "s").isdigit() and int(_attr(cell, "s")) in dates:
            try:
                day = date(1904, 1, 1) if base1904 else date(1899, 12, 30)
                return (day + timedelta(days=int(float(value)))).isoformat()
            except (ValueError, OverflowError):
                return value
        if kind == "n" and re.fullmatch(r"-?\d+\.\d{10,}(E-?\d+)?", value):
            return f"{float(value):.10g}"  # 0.30000000000000004 as Excel shows it
        return value

    @staticmethod
    def _texts(element, skip: tuple[str, ...] = ()):
        for child in element if element is not None else []:
            name = _local(child.tag)
            if name in skip:
                continue
            if name == "t":
                yield child
            else:
                yield from _Reader._texts(child, skip)

    # PowerPoint

    def powerpoint(self) -> None:
        main = "ppt/presentation.xml"
        root = self.xml(main)
        if root is None:
            raise DocumentError(f"{self.name} has no slides in it.")
        rels = self.rels(main)
        slides = [rels.get(_rid(s), ("", ""))[1] for s in _children(_child(root, "sldIdLst"), "sldId")]
        self.of = f" of {len(slides)} slides"
        for number, part in enumerate(slides, 1):
            if not part:
                continue
            self.where = f"slide {number}"
            slide = self.xml(part)
            slide_rels = self.rels(part)
            lines = []
            self.slide_shapes(_find(slide, "cSld", "spTree"), slide_rels, lines)
            notes_part = next((t for k, t in slide_rels.values() if k == "notesSlide"), None)
            notes = self.xml(notes_part) if notes_part else None
            said = []
            for shape in (notes.iter() if notes is not None else []):
                placeholder = _find(shape, "nvSpPr", "nvPr", "ph")
                if _local(shape.tag) == "sp" and (placeholder is None or _attr(placeholder, "type") != "sldNum"):
                    said += [t for t in self.drawing_paragraphs(_child(shape, "txBody")) if t]
            if _attr(slide, "show") == "0":
                lines.insert(0, "(hidden slide)")
            if said:
                lines.append("Speaker notes: " + " ".join(said))
            self.add(f"## Slide {number}\n" + "\n".join(lines))

    def slide_shapes(self, tree, rels: dict, lines: list[str]) -> None:
        for shape in tree if tree is not None else []:
            name = _local(shape.tag)
            if name == "sp":
                lines += [t for t in self.drawing_paragraphs(_child(shape, "txBody")) if t]
            elif name == "grpSp":
                self.slide_shapes(shape, rels, lines)
            elif name == "pic":
                blip = next((b for b in shape.iter() if _local(b.tag) == "blip"), None)
                marker = self.media(rels, _attr(blip, "embed")) if blip is not None else ""
                if marker:
                    lines.append(marker)
            elif name == "graphicFrame":
                table = next((t for t in shape.iter() if _local(t.tag) == "tbl"), None)
                for row in _children(table, "tr"):
                    cells = [" ".join(self.drawing_paragraphs(_child(c, "txBody"))) for c in _children(row, "tc")]
                    if any(cells):
                        lines.append(" | ".join(cells))

    @staticmethod
    def drawing_paragraphs(body) -> list[str]:
        out = []
        for paragraph in _children(body, "p"):
            pieces = []
            for run in paragraph:
                name = _local(run.tag)
                if name in ("r", "fld"):
                    pieces.append("".join(t.text or "" for t in run if _local(t.tag) == "t"))
                elif name == "br":
                    pieces.append("\n")
            out.append("".join(pieces).strip())
        return out

    # OpenDocument

    def opendocument(self) -> None:
        root = self.xml("content.xml")
        if root is None:
            raise DocumentError(f"{self.name} has no content in it.")
        body = _child(_child(root, "body"), "text") or _child(_child(root, "body"), "spreadsheet") \
            or _child(_child(root, "body"), "presentation")
        if body is None:
            raise DocumentError(f"{self.name} has no text, sheets or slides in it.")
        kind = _local(body.tag)
        if kind == "spreadsheet":
            for table in _children(body, "table"):
                self.where = f"sheet {_attr(table, 'name')}"
                rows = self.od_rows(table)
                if rows:
                    self.add(f"## Sheet: {_attr(table, 'name')}\n" + "\n".join(rows))
        elif kind == "presentation":
            pages = _children(body, "page")
            self.of = f" of {len(pages)} slides"
            for number, page in enumerate(pages, 1):
                self.where = f"slide {number}"
                lines = []
                for frame in page:
                    if _local(frame.tag) == "notes":
                        said = [self.od_inline(p) for p in frame.iter() if _local(p.tag) == "p"]
                        if any(said):
                            lines.append("Speaker notes: " + " ".join(s for s in said if s))
                    else:
                        self.od_blocks(frame, lines)
                self.add(f"## Slide {number}\n" + "\n".join(lines))
        else:
            lines: list[str] = []
            self.od_blocks(body, lines, flush=True)

    def od_blocks(self, parent, lines: list[str], flush: bool = False, depth: int = 0) -> None:
        for element in parent:
            name = _local(element.tag)
            text = None
            if name == "h":
                level = _attr(element, "outline-level", "1")
                text = "#" * min(int(level) if level.isdigit() else 1, 6) + " " + self.od_inline(element)
            elif name == "p":
                text = self.od_inline(element)
            elif name == "list":
                for item in _children(element, "list-item"):
                    inner: list[str] = []
                    self.od_blocks(item, inner, depth=depth + 1)
                    if inner:
                        text = "  " * depth + "- " + "\n".join(inner)
                        self._od_out(text, lines, flush)
                text = None
            elif name == "table":
                text = "\n".join(self.od_rows(element))
            elif name == "image":
                text = self.picture(self.part(_attr(element, "href"), MAX_IMAGE_BYTES + 1))
            elif name in ("section", "frame", "text-box", "g", "custom-shape", "rect", "index-body",
                          "table-of-content", "alphabetical-index", "illustration-index", "table-index"):
                self.od_blocks(element, lines, flush, depth)
            if text and text.strip("# "):
                self._od_out(text, lines, flush)
            self.tick()

    def _od_out(self, text: str, lines: list[str], flush: bool) -> None:
        if flush:
            self.add(text)
        else:
            lines.append(text)

    def od_inline(self, element) -> str:
        pieces = [element.text or ""]
        for child in element:
            name = _local(child.tag)
            if name == "s":
                count = _attr(child, "c", "1")
                pieces.append(" " * (int(count) if count.isdigit() and int(count) < 100 else 1))
            elif name == "tab":
                pieces.append("\t")
            elif name == "line-break":
                pieces.append("\n")
            elif name == "note":
                body = _child(child, "note-body")
                said = " ".join(self.od_inline(p) for p in (body.iter() if body is not None else [])
                                if _local(p.tag) == "p")
                pieces.append(f" [note: {said.strip()}]" if said.strip() else "")
            elif name in ("annotation", "bookmark", "bookmark-start", "bookmark-end", "change", "change-start",
                          "change-end", "soft-page-break", "tracked-changes"):
                pass
            elif name == "frame":
                images = [i for i in child.iter() if _local(i.tag) == "image"]
                pieces += [self.picture(self.part(_attr(i, "href"), MAX_IMAGE_BYTES + 1)) for i in images]
            else:
                pieces.append(self.od_inline(child))
            pieces.append(child.tail or "")
        return "".join(pieces).strip()

    def od_rows(self, table) -> list[str]:
        rows: list[str] = []
        for row in table.iter():
            if _local(row.tag) != "table-row":
                continue
            cells: list[str] = []
            for cell in row:
                if _local(cell.tag) not in ("table-cell", "covered-table-cell"):
                    continue
                text = " ".join(self.od_inline(p) for p in cell if _local(p.tag) in ("p", "h"))
                repeat = _attr(cell, "number-columns-repeated", "1")
                times = int(repeat) if repeat.isdigit() else 1
                cells += [text] * (min(times, MAX_REPEAT) if text else min(times, 1))
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                repeat = _attr(row, "number-rows-repeated", "1")
                rows += [" | ".join(cells)] * min(int(repeat) if repeat.isdigit() else 1, MAX_REPEAT)
            if len(rows) % 200 == 0:
                self.tick()
        return rows

    # PDF

    def pdf(self, data: bytes) -> None:
        self.kind = "PDF"
        try:
            from pypdf import PdfReader
        except ImportError:  # an install from before pypdf was needed
            raise DocumentError("Reading PDFs needs pypdf, which this install doesn't have yet. Run: ixel update") \
                from None
        level = _PDF_LOGGER.level
        _PDF_LOGGER.setLevel(logging.ERROR)  # its warnings about damaged files would land in the terminal
        try:
            try:
                reader = PdfReader(io.BytesIO(data), strict=False)
                if reader.is_encrypted and not reader.decrypt(""):  # "" opens one locked only against changes
                    raise DocumentError(f"{self.name} needs a password to open. Save a copy without one and "
                                        "attach that.")
                pages = reader.pages
                count = len(pages)
            except DocumentError:
                raise
            except Exception as exc:  # noqa: BLE001 — pypdf raises many kinds for a damaged file
                if "password" in str(exc).lower() or "decrypt" in str(exc).lower():
                    raise DocumentError(f"{self.name} needs a password to open. Save a copy without one and "
                                        "attach that.") from None
                raise DocumentError(f"{self.name} is damaged or isn't really a PDF.") from None
            self.of = f" of {count} pages" if count > 1 else ""
            text_chars, unreadable = 0, 0

            def pages_text() -> None:
                nonlocal text_chars, unreadable
                for number in range(1, min(count, MAX_PDF_PAGES) + 1):
                    self.tick()
                    self.where = f"page {number}"
                    page = pages[number - 1]
                    markers = [self.picture(image, self.where) for image in self.pdf_images(page)]
                    try:
                        text = page.extract_text() or ""
                    except Exception:  # noqa: BLE001
                        text, unreadable = "", unreadable + 1
                    text_chars += len(text.strip())
                    body = "\n".join(part for part in (text.strip(), " ".join(m for m in markers if m)) if part)
                    if body:
                        self.add(f"--- Page {number} ---\n{body}" if count > 1 else body)
            self.run(pages_text)
            if count > MAX_PDF_PAGES and not self.cut:
                self.notes.append(f"{self.name}: only its first {MAX_PDF_PAGES:,} pages were read.")
            if unreadable:
                self.notes.append(f"{self.name}: the text of {_count(unreadable, 'page')} couldn't be read.")
            if text_chars < 10 * min(count, MAX_PDF_PAGES) and not self.cut:
                self.notes.append(f"{self.name} has little or no text in it (a scan, perhaps): "
                                  + ("its pictures go to the models that see pictures, and the others can't read it."
                                     if self.images else "the models can't read what's on its pages. Attach the "
                                     "pages as pictures (a screenshot or photo of each) instead."))
        finally:
            _PDF_LOGGER.setLevel(level)

    def pdf_images(self, page, depth: int = 0) -> list[bytes]:
        """The pictures a page uses, as JPEG or PNG bytes: a JPEG as it's stored, and 8-bit RGB or gray pixels
        made into a PNG. Other kinds (JPEG 2000, fax-style black and white, CMYK…) are counted as left out."""
        found = []
        try:
            resources = page.get("/Resources")
            resources = resources.get_object() if resources is not None else None
            objects = resources.get("/XObject") if resources is not None else None
            objects = objects.get_object() if objects is not None else {}
        except Exception:  # noqa: BLE001
            return found
        for name in list(objects or {})[:200]:
            self.tick()
            try:
                image = objects[name].get_object()
                subtype = image.get("/Subtype")
                if subtype == "/Form" and depth < 3:
                    found += self.pdf_images(image, depth + 1)
                    continue
                if subtype != "/Image":
                    continue
                width, height = int(image.get("/Width", 0)), int(image.get("/Height", 0))
                if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE or image.get("/ImageMask"):
                    continue
                filters = image.get("/Filter")
                filters = [str(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
                if filters and filters[-1] == "/DCTDecode":
                    found.append(image.get_data())
                    continue
                space = image.get("/ColorSpace")
                space = space.get_object() if space is not None else None
                if isinstance(space, list) and space and str(space[0]) == "/ICCBased":
                    components = int(space[1].get_object().get("/N", 0))
                else:
                    components = {"/DeviceRGB": 3, "/CalRGB": 3, "/DeviceGray": 1, "/CalGray": 1}.get(str(space), 0)
                if components not in (1, 3) or int(image.get("/BitsPerComponent", 0)) != 8 \
                        or any(f not in ("/FlateDecode", "/LZWDecode", "/RunLengthDecode") for f in filters) \
                        or width * height > MAX_IMAGE_PIXELS:
                    self.left_out["kind"] += 1
                    continue
                if filters == ["/FlateDecode"] and not _inflates_within(image, height * (width * components + 1)):
                    self.left_out["big"] += 1  # it unpacks to far more than its size says: left out unread
                    continue
                pixels = image.get_data()
                if len(pixels) < width * height * components:
                    continue
                found.append(_png(width, height, components == 1, pixels))
            except Exception:  # noqa: BLE001 — a damaged picture is left out, the rest is read
                continue
        return found

    # RTF

    def rtf(self, data: bytes) -> None:
        self.kind = "RTF document"
        text = data.decode("latin-1")
        out: list[str] = []
        stack: list[tuple[bool, bool, int]] = []
        skip, hidden, skip_bytes, uc = False, False, 0, 1
        hidden_text: list[str] = []
        destinations = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer",
                        "headerl", "headerr", "footerl", "footerr", "listtable", "listoverridetable", "rsidtbl",
                        "generator", "themedata", "colorschememapping", "datastore", "latentstyles", "xmlnstbl",
                        "fldinst", "revtbl", "pgdsctbl", "filetbl", "mmathPr", "operator", "author"}
        pos = 0

        def put(piece: str) -> None:
            (hidden_text if hidden else out).append(piece)

        def end_hidden() -> None:
            if hidden_text and "".join(hidden_text).strip():
                out.append(f"[hidden text: {''.join(hidden_text).strip()}]")
            hidden_text.clear()

        pattern = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+")
        for match in pattern.finditer(text):
            word, arg, hexed, symbol, brace, plain = match.groups()
            if pos % 5000 == 0:
                self.tick()
            pos += 1
            if brace == "{":
                if len(stack) >= MAX_NESTING:
                    raise DocumentError(f"{self.name} is nested too deeply to be a document Ixel reads.")
                stack.append((skip, hidden, uc))
                continue
            if brace == "}":
                was_hidden = hidden
                skip, hidden, uc = stack.pop() if stack else (False, False, 1)
                if was_hidden and not hidden:
                    end_hidden()
                continue
            if skip_bytes and (plain or hexed or symbol):
                if plain:
                    taken = min(skip_bytes, len(plain))
                    skip_bytes -= taken
                    plain = plain[taken:]
                    if not plain:
                        continue
                else:
                    skip_bytes -= 1
                    continue
            if word:
                if word in destinations:
                    skip = True
                elif skip:
                    continue
                elif word in ("par", "line", "sect", "page"):
                    put("\n")
                elif word == "tab":
                    put("\t")
                elif word == "cell":
                    put(" | ")
                elif word == "row":
                    put("\n")
                elif word == "u" and arg:
                    put(chr(int(arg) % 65536))
                    skip_bytes = uc
                elif word == "uc" and arg:
                    uc = int(arg)
                elif word == "v":
                    if arg == "0" and hidden:
                        end_hidden()
                    hidden = arg != "0"
                elif word in ("emdash", "endash"):
                    put("—" if word == "emdash" else "–")
                elif word in ("lquote", "rquote"):
                    put("'")
                elif word in ("ldblquote", "rdblquote"):
                    put('"')
                elif word == "bullet":
                    put("•")
            elif symbol:
                if symbol == "*":
                    skip = True
                elif not skip and symbol in "\\{}":
                    put(symbol)
                elif not skip and symbol == "~":
                    put("\u00a0")
            elif hexed and not skip:
                put(bytes([int(hexed, 16)]).decode("cp1252", errors="replace"))
            elif plain and not skip:
                put(plain)
        end_hidden()
        self.run(lambda: [self.add(block) for block in re.split(r"\n{2,}", "".join(out))])

    # Web pages

    def html(self, source: str) -> None:
        parser = _Html()
        parser.feed(source)
        parser.close()
        self.run(lambda: [self.add(block) for block in re.split(r"\n{2,}", parser.text())])


def _inflates_within(stream, limit: int) -> bool:
    """Whether a PDF's Flate-packed stream unpacks to about limit bytes or fewer, found out without unpacking
    more than that: a picture a few pixels big can say so and unpack to gigabytes."""
    raw = getattr(stream, "_data", None)  # as it's stored in the file
    if not isinstance(raw, bytes):
        return True  # pypdf's own cap (75 MB a stream) still holds
    inflater = zlib.decompressobj()
    try:
        inflater.decompress(raw, limit + 4096)
    except zlib.error:
        return True  # damaged: pypdf finds that out, within its own limits
    return not inflater.unconsumed_tail


def _column(ref: str) -> int:
    """A1's column as a number from 0 (EXCEL_COLUMNS or more for one past Excel's last)."""
    n = 0
    for letter in re.match(r"[A-Za-z]{0,4}", ref).group().upper():
        n = n * 26 + ord(letter) - 64
    return max(n - 1, 0)


class _Html(HTMLParser):
    """A web page's text: no scripts or styles, nothing fetched, pictures named by their alt text."""
    BLOCKS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "blockquote", "pre",
              "ul", "ol", "table", "form", "figure", "figcaption", "hr", "dl", "dt", "dd"}

    SKIP = {"script", "style", "template", "head", "svg", "noscript"}
    EMPTY = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping: list[str] = []  # the tags being left out, with what's inside them

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skipping or tag in self.SKIP or "hidden" in attrs:
            if tag not in self.EMPTY:
                self.skipping.append(tag)
            return
        if tag in self.BLOCKS:
            self.out.append("\n\n")
        elif tag in ("br", "tr"):
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "img" and attrs.get("alt"):
            self.out.append(f"[picture: {attrs['alt']}]")

    def handle_startendtag(self, tag, attrs):
        if not self.skipping and tag not in self.SKIP:
            self.handle_starttag(tag, attrs)
            if tag not in self.EMPTY:
                self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.skipping:  # unclosed tags inside it close with it
                del self.skipping[len(self.skipping) - 1 - self.skipping[::-1].index(tag):]
        elif tag in self.BLOCKS or re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines = [line.strip(" ") for line in "".join(self.out).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
