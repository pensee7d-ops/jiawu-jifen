import io
import os
import uuid
from PIL import Image, ImageOps


def compress_to_jpeg(raw: bytes, max_edge: int = 1280, quality: int = 72) -> bytes:
    img = Image.open(io.BytesIO(raw))
    # draft 在 JPEG 解码阶段就降采样，手机大图能快几倍（对 PNG 等无副作用）。
    img.draft("RGB", (max_edge, max_edge))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_edge / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    out = io.BytesIO()
    # 不用 optimize（额外一遍编码，单核 VPS 上慢）；progressive 关。
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def save_photo(raw: bytes, photo_dir: str) -> str:
    os.makedirs(photo_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    data = compress_to_jpeg(raw)
    with open(os.path.join(photo_dir, name), "wb") as f:
        f.write(data)
    return f"/photos/{name}"
