import sys, io
sys.path.insert(0, "/home/user/Ixel-MAT")
from PIL import Image, PngImagePlugin
from ixel_mat import pictures as P

def img(w=64, h=48):
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = (x*4 % 256, y*5 % 256, (x+y) % 256)
    return im

def check(label, data):
    try:
        pic = P.read_picture(data)
    except P.PictureError as e:
        print(f"{label}: REFUSED: {e}"); return None
    try:
        out = Image.open(io.BytesIO(pic.data)); out.load()
    except Exception as e:
        print(f"{label}: cleaned output UNDECODABLE: {e!r}"); return pic
    info = {k: (len(str(v)) if k in ("exif","icc_profile","xmp") else v) for k, v in out.info.items()}
    orig = Image.open(io.BytesIO(data)); orig.load()
    same = list(orig.convert("RGB").getdata()) == list(out.convert("RGB").getdata())
    print(f"{label}: ok {pic.width}x{pic.height} {len(data)}->{len(pic.data)} bytes, pixels identical={same}, info left={info}, exif={bool(out.getexif())}")
    return pic

im = img()
exif = Image.Exif(); exif[0x010F] = "SecretCam"; exif[0x9286] = "hidden comment"; pass
# baseline JPEG with EXIF + comment + xmp
b = io.BytesIO(); im.save(b, "JPEG", quality=85, exif=exif.tobytes(), comment=b"secret comment", xmp=b"<x:xmpmeta>gps</x:xmpmeta>")
check("baseline jpeg exif/comment/xmp", b.getvalue())
# progressive with EXIF
b = io.BytesIO(); im.save(b, "JPEG", quality=85, progressive=True, exif=exif.tobytes(), optimize=True)
check("progressive jpeg", b.getvalue())
# JPEG with ICC profile + restart markers? PIL can't set restart interval easily; add trailing garbage
b = io.BytesIO(); im.save(b, "JPEG", quality=50)
check("jpeg + trailing data", b.getvalue() + b"TRAILING SECRET DATA" * 10)
# CMYK JPEG (Adobe APP14)
b = io.BytesIO(); im.convert("CMYK").save(b, "JPEG", quality=85)
check("cmyk jpeg adobe app14", b.getvalue())
# grayscale
b = io.BytesIO(); im.convert("L").save(b, "JPEG", quality=85)
check("grayscale jpeg", b.getvalue())
# JPEG with TEM marker (FF 01) inserted after SOI (standalone marker)
b = io.BytesIO(); im.save(b, "JPEG", quality=85); d = b.getvalue()
check("jpeg with TEM marker FF01 after SOI", d[:2] + b"\xff\x01" + d[2:])
# JPEG with fill bytes FF FF before a marker
check("jpeg with fill bytes", d[:2] + b"\xff\xff" + d[2:])
# PNG with text chunks, time, exif
meta = PngImagePlugin.PngInfo(); meta.add_text("Comment", "secret"); meta.add_itxt("Author", "Jane"); meta.add_text("zz", "x"*500, zip=True)
b = io.BytesIO(); im.save(b, "PNG", pnginfo=meta, exif=exif.tobytes())
check("png text/itxt/ztxt/exif", b.getvalue())
# palette PNG with transparency
b = io.BytesIO(); im.convert("P").save(b, "PNG", transparency=0)
check("palette png + tRNS", b.getvalue())
# APNG
frames = [img(), img(32, 32).resize((64, 48))]
b = io.BytesIO(); frames[0].save(b, "PNG", save_all=True, append_images=frames[1:], duration=100)
check("apng", b.getvalue())
# PNG with trailing data after IEND
b = io.BytesIO(); im.save(b, "PNG")
check("png + trailing", b.getvalue() + b"SECRET" * 50)
# PNG with a chunk whose declared length is huge
d = b.getvalue()
import struct
check("png huge chunk length", d[:8] + struct.pack(">I", 0xFFFFFFF0) + b"IHDR" + d[16:])
# 16-bit RGBA PNG
b = io.BytesIO(); Image.new("RGBA", (10, 10), (1,2,3,4)).save(b, "PNG")
check("rgba png", b.getvalue())
# WebP / GIF refused?
b = io.BytesIO(); im.save(b, "WEBP"); check("webp", b.getvalue())
b = io.BytesIO(); im.save(b, "GIF"); check("gif", b.getvalue())
# eviction behaviour
clock = [0.0]
st = P.PictureStore(keep_for=10, clock=lambda: clock[0])
pic = P.read_picture(open("/dev/null","rb").read() or b"") if False else None
import struct
small = P.read_picture(io.BytesIO() or b"") if False else None
b = io.BytesIO(); Image.new("RGB", (2,2)).save(b, "PNG"); p2 = P.read_picture(b.getvalue())
k = st.add(p2); clock[0] = 9.9; print("take at 9.9:", len(st.take([k])))
clock[0] = 10.1
try: st.take([k]); print("take at 10.1: still there")
except P.PictureGone as e: print("take at 10.1: gone ->", str(e)[:60])
