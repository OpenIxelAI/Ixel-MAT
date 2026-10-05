"""
Pictures attached to a question, for the models that can see them.

The Ixel window re-encodes each picture before sending it (at most 2048 px on a side), which already
drops what a camera writes into a photo. This side checks again: it takes only a PNG or JPEG it can
read the size of, takes out any metadata still in it (EXIF and GPS, XMP, comments, PNG text and
times), drops anything after the end of the image, and keeps it in memory only: for 30 minutes, at
most 64 MB in all, gone when Ixel stops. Nothing is written to disk.

Only HTTP models marked as seeing pictures get them; programs (Claude Code, Codex…) never do.
"""
from __future__ import annotations

import base64
import secrets
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable

MAX_BYTES = 3_900_000           # one picture: as base64 it's under the 5 MB Claude's API takes (the window sends 3.7)
MAX_PER_QUESTION = 8
MAX_PER_QUESTION_BYTES = 20_000_000  # as base64 that's under the 32 MB a Claude API call can be
MAX_SIDE = 8000                 # pixels; the window sends 2048 at most
KEEP_FOR = 30 * 60              # seconds
MAX_KEPT = 64 * 1024 * 1024     # all pictures held at once
MAX_KEPT_COUNT = 256

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# What a PNG needs to look right; everything else (tEXt, zTXt, iTXt, eXIf, tIME, animation…) goes
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"pHYs"}
# JPEG frame headers carry the size: SOF0–SOF15, except DHT (C4), JPG (C8) and DAC (CC)
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# What decoding needs, kept whole: the frame, Huffman and quantization tables, arithmetic coding, restart
# interval, number of lines, start of scan. Any other segment goes, apart from the three below.
_JPEG_KEEP = _SOF | {0xC4, 0xCC, 0xDB, 0xDC, 0xDD, 0xDA}


def megabytes(n: int) -> str:
    return f"{n / 1_000_000:.1f}".removesuffix(".0") + " MB"


class PictureError(ValueError):
    """Said to the person as it is."""


class PictureGone(PictureError):
    """A question names a picture Ixel no longer has (kept 30 minutes, or pushed out by newer ones)."""


@dataclass(frozen=True)
class Picture:
    media_type: str   # image/png or image/jpeg
    data: bytes
    width: int
    height: int

    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.base64()}"


def _size_ok(width: int, height: int) -> None:
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
        raise PictureError(f"That picture is {width}×{height}; pictures can be at most {MAX_SIDE} pixels on a side.")


def _clean_png(data: bytes) -> Picture:
    out = bytearray(PNG_SIGNATURE)
    pos, width, height, seen_end = len(PNG_SIGNATURE), 0, 0, False
    first = True
    while pos < len(data):
        if pos + 8 > len(data):
            raise PictureError("That PNG is cut short.")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise PictureError("That PNG is cut short.")
        if first:
            if kind != b"IHDR" or length != 13:
                raise PictureError("That isn't a PNG Ixel can read.")
            width, height = struct.unpack(">II", data[pos + 8:pos + 16])
            first = False
        if kind in _PNG_KEEP:
            out += data[pos:end]
        pos = end
        if kind == b"IEND":
            seen_end = True
            break  # anything after the end isn't part of the picture
    if not seen_end:
        raise PictureError("That PNG is cut short.")
    _size_ok(width, height)
    return Picture("image/png", bytes(out), width, height)


def _segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def _jpeg_segment(marker: int, body: bytes) -> bytes | None:
    """What's written for one segment: the parts decoding needs, never more; None leaves it out."""
    if marker in _JPEG_KEEP:
        return _segment(marker, body)
    if marker == 0xE0 and body.startswith(b"JFIF\x00") and len(body) >= 14:
        return _segment(marker, body[:12] + b"\x00\x00")  # JFIF's header, without its thumbnail
    if marker == 0xE2 and body.startswith(b"ICC_PROFILE\x00"):
        return _segment(marker, body)  # the colour profile
    if marker == 0xEE and body.startswith(b"Adobe") and len(body) >= 12:
        return _segment(marker, body[:12])  # Adobe's colour transform, needed to decode right
    return None  # EXIF, XMP, IPTC, comments, thumbnails and anything else


def _scan_end(data: bytes, pos: int) -> int:
    """Where a scan's image data, starting at pos, ends: the next marker. Inside it 0xFF is always
    followed by 0x00 (a stuffed byte) or a restart marker (D0–D7)."""
    while True:
        at = data.find(b"\xff", pos)
        if at < 0 or at + 1 >= len(data):
            raise PictureError("That JPEG is cut short or damaged.")
        following = data[at + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            pos = at + 2
            continue
        return at


def _clean_jpeg(data: bytes) -> Picture:
    out = bytearray(b"\xff\xd8")
    pos, width, height = 2, 0, 0
    while True:
        while pos + 1 < len(data) and data[pos] == 0xFF and data[pos + 1] == 0xFF:
            pos += 1  # fill bytes
        if pos + 2 > len(data) or data[pos] != 0xFF:
            raise PictureError("That JPEG is cut short or damaged.")
        marker = data[pos + 1]
        if marker == 0xD9:  # end of image: anything after it isn't part of the picture
            out += b"\xff\xd9"
            break
        if 0xD0 <= marker <= 0xD7:  # a restart marker on its own: it has no length
            out += data[pos:pos + 2]
            pos += 2
            continue
        if pos + 4 > len(data):
            raise PictureError("That JPEG is cut short or damaged.")
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise PictureError("That JPEG is cut short or damaged.")
        body = data[pos + 4:end]
        if marker in _SOF:
            if len(body) < 5:
                raise PictureError("That JPEG is cut short or damaged.")
            height, width = struct.unpack(">HH", body[1:5])
        kept = _jpeg_segment(marker, body)
        if kept is not None:
            out += kept
        pos = end
        if marker == 0xDA:  # start of scan: its image data follows; a progressive JPEG has several scans,
            stop = _scan_end(data, pos)  # with segments between them that are checked like any other
            out += data[pos:stop]
            pos = stop
    if not width:
        raise PictureError("That isn't a JPEG Ixel can read.")
    _size_ok(width, height)
    return Picture("image/jpeg", bytes(out), width, height)


def read_picture(data: bytes) -> Picture:
    """A PNG or JPEG with its metadata taken out; PictureError if it isn't one, or is too big."""
    if len(data) > MAX_BYTES:
        raise PictureError(f"That picture is over {megabytes(MAX_BYTES)}.")
    if data.startswith(PNG_SIGNATURE):
        return _clean_png(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _clean_jpeg(data)
    raise PictureError("Pictures can be PNG or JPEG. (The Ixel window turns other kinds into one of those.)")


class PictureStore:
    """Pictures waiting for their question, in memory only."""

    def __init__(self, keep_for: float = KEEP_FOR, max_bytes: int = MAX_KEPT, max_count: int = MAX_KEPT_COUNT,
                 clock: Callable[[], float] = time.monotonic):
        self.keep_for = keep_for
        self.max_bytes = max_bytes
        self.max_count = max_count
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, Picture]] = OrderedDict()
        self.size = 0  # bytes held

    def _pop_oldest(self) -> None:
        _, (_, picture) = self._items.popitem(last=False)
        self.size -= len(picture.data)

    def _prune(self) -> None:
        now = self.clock()
        while self._items and now - next(iter(self._items.values()))[0] > self.keep_for:
            self._pop_oldest()  # oldest first, so the first one that's fresh ends it

    def add(self, picture: Picture) -> str:
        self._prune()
        while self._items and (self.size + len(picture.data) > self.max_bytes or len(self._items) >= self.max_count):
            self._pop_oldest()
        key = secrets.token_urlsafe(12)
        self._items[key] = (self.clock(), picture)
        self.size += len(picture.data)
        return key

    def take(self, keys: Iterable[str]) -> list[Picture]:
        """The pictures for a question, in order. They stay, so the same question can be asked again."""
        self._prune()
        keys = list(keys)
        if len(keys) > MAX_PER_QUESTION:
            raise PictureError(f"A question can have at most {MAX_PER_QUESTION} pictures.")
        found = []
        for key in keys:
            item = self._items.get(key) if isinstance(key, str) else None
            if item is None:
                raise PictureGone("A picture you attached is no longer here (they're kept for 30 minutes), so "
                                  "it's being attached again. Ask once more.")
            found.append(item[1])
        if sum(len(p.data) for p in found) > MAX_PER_QUESTION_BYTES:
            raise PictureError(f"These pictures add up to more than {megabytes(MAX_PER_QUESTION_BYTES)}, more than "
                               "a model takes in one question. Take one or two out.")
        return found

    def clear(self) -> None:
        self._items.clear()
        self.size = 0
