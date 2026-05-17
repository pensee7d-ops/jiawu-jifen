import io
from PIL import Image
from chores.images import compress_to_jpeg, save_photo


def _big_png_bytes():
    img = Image.new("RGB", (4000, 3000), (120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compress_reduces_dimensions_and_size():
    raw = _big_png_bytes()
    out = compress_to_jpeg(raw, max_edge=1600)
    img = Image.open(io.BytesIO(out))
    assert max(img.size) <= 1600
    assert img.format == "JPEG"
    assert len(out) < len(raw)


def test_save_photo_writes_unique_jpg(tmp_path):
    raw = _big_png_bytes()
    p1 = save_photo(raw, str(tmp_path))
    p2 = save_photo(raw, str(tmp_path))
    assert p1.endswith(".jpg") and p2.endswith(".jpg")
    assert p1 != p2
    assert (tmp_path / p1.split("/")[-1]).exists()
