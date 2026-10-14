"""Lightweight, local-only image analysis for PhotoDom's smart catalog."""

from __future__ import annotations

import colorsys
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat


SMART_MODEL_VERSION = 1
WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{2,}")


TAG_SEARCH_TERMS = {
    "portrait": "вертикальное вертикальные портретная ориентация",
    "landscape": "горизонтальное горизонтальные альбомная ориентация",
    "square": "квадрат квадратное квадратные",
    "panorama": "панорама панорамное широкое",
    "dark": "темное темные ночь ночное",
    "bright": "светлое светлые яркое",
    "colorful": "цветное цветные насыщенное",
    "monochrome": "черно-белое чёрно-белое монохром серое",
    "has_text": "текст надпись документ",
    "screenshot": "скриншот экран снимок экрана",
    "document": "документ скан текст бумага",
    "color:red": "красное красный",
    "color:orange": "оранжевое оранжевый",
    "color:yellow": "желтое жёлтое желтый жёлтый",
    "color:green": "зеленое зелёное зеленый зелёный",
    "color:cyan": "голубое бирюзовое",
    "color:blue": "синее синий",
    "color:purple": "фиолетовое фиолетовый",
    "color:gray": "серое серый",
}


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _orientation(width: int, height: int) -> tuple[str, float]:
    ratio = width / max(1, height)
    if ratio >= 2.05:
        return "panorama", ratio
    if 0.92 <= ratio <= 1.08:
        return "square", ratio
    return ("landscape" if ratio > 1 else "portrait"), ratio


def _dominant_color(red: float, green: float, blue: float, saturation: float) -> str:
    if saturation < 0.11:
        return "gray"
    hue = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)[0] * 360
    if hue < 18 or hue >= 345:
        return "red"
    if hue < 45:
        return "orange"
    if hue < 70:
        return "yellow"
    if hue < 165:
        return "green"
    if hue < 195:
        return "cyan"
    if hue < 255:
        return "blue"
    if hue < 345:
        return "purple"
    return "red"


def _ocr(image: Image.Image, languages: str, timeout: int) -> str:
    if not tesseract_available():
        return ""
    prepared = ImageOps.grayscale(image)
    prepared.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    # Improving contrast helps phone screenshots and photographed documents while
    # keeping the temporary input small enough for the old home-server CPU.
    prepared = ImageOps.autocontrast(prepared)
    temporary = tempfile.NamedTemporaryFile(prefix="photodom-ocr-", suffix=".png", delete=False)
    temporary.close()
    path = Path(temporary.name)
    try:
        prepared.save(path, "PNG", optimize=True)
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", languages, "--psm", "11"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            text=True,
            errors="ignore",
            preexec_fn=(lambda: os.nice(10)) if os.name == "posix" else None,
        )
        text = " ".join(result.stdout.replace("\x0c", " ").split())
        return text[:20_000] if len(WORD.findall(text)) >= 2 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
    finally:
        path.unlink(missing_ok=True)


def analyze_image(path: Path, name: str, *, ocr_enabled: bool = True,
                  ocr_languages: str = "rus+eng", ocr_timeout: int = 20) -> dict:
    """Return inexpensive visual properties and optional OCR text for one image."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        sample = image.convert("RGB")
        sample.thumbnail((320, 320), Image.Resampling.BILINEAR)
        stat = ImageStat.Stat(sample)
        red, green, blue = stat.mean[:3]
        brightness = round((0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255, 4)
        hsv = sample.convert("HSV")
        saturation = round(ImageStat.Stat(hsv).mean[1] / 255, 4)
        contrast = round(ImageStat.Stat(ImageOps.grayscale(sample)).stddev[0] / 128, 4)
        edges = ImageOps.grayscale(sample).filter(ImageFilter.FIND_EDGES)
        sharpness = round(ImageStat.Stat(edges).mean[0] / 255, 4)
        orientation, ratio = _orientation(width, height)
        color = _dominant_color(red, green, blue, saturation)
        lowered = name.casefold()
        screenshot = any(token in lowered for token in ("screenshot", "screen_shot", "скриншот"))
        scan_named = any(token in lowered for token in ("scan", "скан", "document", "документ"))
        ocr_text = _ocr(image, ocr_languages, ocr_timeout) if ocr_enabled else ""

    tags = [orientation, f"color:{color}"]
    if brightness < 0.24:
        tags.append("dark")
    elif brightness > 0.78:
        tags.append("bright")
    if saturation < 0.10:
        tags.append("monochrome")
    elif saturation > 0.52:
        tags.append("colorful")
    if ocr_text:
        tags.append("has_text")
    if screenshot:
        tags.append("screenshot")

    words = len(WORD.findall(ocr_text))
    document = scan_named or (words >= 10 and brightness > 0.50 and saturation < 0.30)
    if document:
        tags.append("document")
    category = "screenshot" if screenshot else "document" if document else "panorama" if ratio >= 2.05 else "photo"
    search_text = " ".join([category, *tags, *(TAG_SEARCH_TERMS.get(tag, "") for tag in tags), ocr_text.casefold()])
    return {
        "category": category,
        "tags": sorted(set(tags)),
        "ocr_text": ocr_text,
        "search_text": search_text.casefold(),
        "dominant_color": color,
        "brightness": brightness,
        "contrast": contrast,
        "sharpness": sharpness,
        "width": width,
        "height": height,
    }
