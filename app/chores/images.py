import io
import os
import uuid
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # 仍允许旧环境启动，HEIC 会返回明确的格式错误。
    pass


MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class PhotoError(ValueError):
    pass


def compress_to_jpeg(raw: bytes, max_edge: int = 1280, quality: int = 72) -> bytes:
    if not raw:
        raise PhotoError("没有读取到照片")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoError("照片不能超过 15MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise PhotoError("照片格式不支持或文件已经损坏") from exc
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
    try:
        img.save(out, format="JPEG", quality=quality)
    except Exception as exc:
        raise PhotoError("照片处理失败，请换一张重试") from exc
    return out.getvalue()


def save_photo(raw: bytes, photo_dir: str) -> str:
    os.makedirs(photo_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    data = compress_to_jpeg(raw)
    with open(os.path.join(photo_dir, name), "wb") as f:
        f.write(data)
    return f"/photos/{name}"
