import io
import os
import uuid
from PIL import Image, ImageOps


def compress_to_jpeg(raw: bytes, max_edge: int = 1600, quality: int = 72) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_edge / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def save_photo(raw: bytes, photo_dir: str) -> str:
    os.makedirs(photo_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    data = compress_to_jpeg(raw)
    with open(os.path.join(photo_dir, name), "wb") as f:
        f.write(data)
    return f"/photos/{name}"
