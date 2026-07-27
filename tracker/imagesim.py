"""Perceptual image similarity via dHash (Pillow only — no numpy needed)."""
from PIL import Image, ImageOps


def dhash(path_or_img, size=8):
    """64-bit difference hash. Returns int, or None on unreadable image."""
    try:
        img = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
        img = ImageOps.exif_transpose(img).convert("L").resize((size + 1, size), Image.LANCZOS)
    except Exception:
        return None
    px = list(img.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def dhash_mirrored(path):
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        return dhash(img.transpose(Image.FLIP_LEFT_RIGHT))
    except Exception:
        return None


def similarity(h1, h2, bits=64):
    if h1 is None or h2 is None:
        return None
    return 1.0 - bin(h1 ^ h2).count("1") / bits


def best_similarity(ref_hashes, cand_hash):
    """Max similarity of candidate against reference hash(es) incl. mirror."""
    sims = [similarity(h, cand_hash) for h in ref_hashes if h is not None]
    sims = [s for s in sims if s is not None]
    return max(sims) if sims else None
