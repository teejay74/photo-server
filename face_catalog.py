"""Local face detection and embedding extraction using OpenCV Zoo models."""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


FACE_MODEL_VERSION = 1
FACE_MATCH_THRESHOLD = 0.48
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


class FaceEngine:
    def __init__(self, model_dir: Path):
        detector_path = model_dir / "yunet.onnx"
        recognizer_path = model_dir / "sface.onnx"
        if not detector_path.is_file() or not recognizer_path.is_file():
            raise FileNotFoundError("Не найдены модели распознавания лиц")
        self.detector = cv2.FaceDetectorYN.create(
            str(detector_path), "", (320, 320), 0.87, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")

    def analyze(self, path: Path) -> list[dict]:
        with Image.open(path) as source:
            oriented = ImageOps.exif_transpose(source).convert("RGB")
            original_width, original_height = oriented.size
            scale = min(1.0, 1920 / max(original_width, original_height))
            if scale < 1:
                oriented = oriented.resize(
                    (max(1, round(original_width * scale)), max(1, round(original_height * scale))),
                    Image.Resampling.LANCZOS,
                )
            frame = cv2.cvtColor(np.asarray(oriented), cv2.COLOR_RGB2BGR)

        height, width = frame.shape[:2]
        self.detector.setInputSize((width, height))
        _status, detected = self.detector.detect(frame)
        if detected is None:
            return []
        results = []
        inverse = 1 / scale
        for face in sorted(detected, key=lambda value: float(value[-1]), reverse=True):
            x, y, box_width, box_height = (float(value) for value in face[:4])
            if box_width < 24 or box_height < 24:
                continue
            aligned = self.recognizer.alignCrop(frame, face)
            embedding = self.recognizer.feature(aligned).astype(np.float32).reshape(-1)
            norm = float(np.linalg.norm(embedding))
            if not norm:
                continue
            embedding /= norm
            results.append({
                "x": max(0, round(x * inverse)),
                "y": max(0, round(y * inverse)),
                "width": min(original_width, round(box_width * inverse)),
                "height": min(original_height, round(box_height * inverse)),
                "confidence": round(float(face[-1]), 5),
                "embedding": embedding.tobytes(),
            })
        return results


def get_engine(model_dir: Path) -> FaceEngine:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = FaceEngine(model_dir)
    return _ENGINE


def embedding_array(value: bytes) -> np.ndarray:
    return np.frombuffer(value, dtype=np.float32)
