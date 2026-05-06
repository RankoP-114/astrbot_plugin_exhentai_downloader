import logging
from pathlib import Path

import numpy as np
from PIL import Image as PILImage, ImageFilter

from .log import logger

COVER_CACHE_EXTENSION = ".jpg"
COVER_CACHE_QUALITY = 82


def apply_gaussian_blur(image: PILImage.Image, radius: int) -> PILImage.Image:
    frame = image.convert("RGBA")
    background = PILImage.new("RGB", frame.size, (255, 255, 255))
    background.paste(frame, mask=frame.getchannel("A"))
    return background.filter(ImageFilter.GaussianBlur(radius=radius))


def apply_fgsm(image: PILImage.Image, eps: float) -> PILImage.Image:
    frame = image.convert("RGBA")
    background = PILImage.new("RGB", frame.size, (255, 255, 255))
    background.paste(frame, mask=frame.getchannel("A"))

    arr = np.array(background, dtype=np.float32)

    if arr.ndim == 3 and arr.shape[2] >= 3:
        gray = np.mean(arr[:, :, :3], axis=2)
    else:
        gray = arr.astype(np.float32)

    grad_y = np.gradient(gray, axis=0)
    grad_x = np.gradient(gray, axis=1)
    mag = np.sqrt(grad_x**2 + grad_y**2)
    mag = mag / (mag.max() + 1e-8)

    noise = np.random.normal(0, 1, arr.shape[:2])
    noise = np.sign(noise)

    perturbation = np.zeros_like(arr, dtype=np.float32)
    for c in range(min(arr.shape[2], 3)):
        perturbation[:, :, c] = noise * mag * eps
        perturbation[:, :, c] = np.clip(perturbation[:, :, c], -eps, eps)

    perturbed = np.clip(arr + perturbation, 0, 255).astype(np.uint8)
    return PILImage.fromarray(perturbed)


def process_cover(
    source_path: str,
    method: str = "gaussian",
    blur_radius: int = 14,
    fgsm_eps: float = 4.0,
    suffix: str = "",
) -> str:

    source = Path(source_path)
    tag = f"{method}{suffix}"
    out_path = source.with_name(f"{source.stem}.{tag}{COVER_CACHE_EXTENSION}")

    if _is_valid_image(str(out_path)):
        return str(out_path)

    with PILImage.open(source_path) as img:
        img.load()

        if method == "fgsm":
            processed = apply_fgsm(img, eps=fgsm_eps)
        else:
            processed = apply_gaussian_blur(img, radius=blur_radius)

        processed.save(str(out_path), format="JPEG", quality=COVER_CACHE_QUALITY, optimize=True)

    return str(out_path) if _is_valid_image(str(out_path)) else ""


def _is_valid_image(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            sample = f.read(32)
    except OSError:
        return False
    if not sample:
        return False
    if sample.startswith(b"\xff\xd8\xff"):
        return True
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(sample) >= 12 and sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return True
    return sample.startswith(b"BM")
