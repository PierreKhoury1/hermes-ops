"""Computer vision for the desk: cameras as a first-class input.

Pipeline per camera tick (see desk/scheduler.py `camera_watch`):

    grab()  →  motion()  →  detect()  →  rule()  →  vision_event row  →  (optional) describe()  →  desk run

- grab      one JPEG from a webcam index, an RTSP URL (ffmpeg), an HTTP snapshot URL or a file.
- motion    cheap frame-difference score against the previous frame (numpy) — gates the expensive steps.
- detect    local YOLO (ultralytics, optional) → [{label, conf, box}]. Falls back to the vision model when
            ultralytics is not installed (Render), or to nothing at all in demo mode.
- rule      "is this worth the desk's attention?" — watched labels, minimum count, hours, cooldown.
- describe  ask a vision-language model a question about the frame (OpenAI-compatible image message).
- annotate  draw boxes for the portal / snapshot archive (PIL).

Everything degrades: no ultralytics → VLM or motion-only; no model key → detections only; no cv2 → RTSP via
ffmpeg, webcams unavailable. Nothing here decides what to *do* — the orchestrator and approvals do that.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import config as cfg

MODELS_DIR = cfg.DATA_DIR / "models"
SNAP_DIR = cfg.DATA_DIR / "snapshots"
YOLO_WEIGHTS = os.environ.get("VISION_YOLO", str(MODELS_DIR / "yolov8n.pt"))
DEFAULT_VLM = os.environ.get("VISION_MODEL", os.environ.get("ATLAS_FREE_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"))   # free by default; set VISION_MODEL for paid eyes
DEFAULT_VLM_PROVIDER = os.environ.get("VISION_PROVIDER", "openrouter")
MAX_SIDE = 960                      # frames are downscaled to this before detection / VLM
FRAME_TIMEOUT = float(os.environ.get("VISION_GRAB_TIMEOUT", "12"))

# COCO labels the rule engine understands as synonyms
SYNONYMS = {"people": "person", "human": "person", "man": "person", "woman": "person", "customer": "person",
            "vehicle": "car", "van": "truck", "lorry": "truck", "bike": "bicycle", "phone": "cell phone"}


def _norm_label(s: str) -> str:
    s = s.strip().lower()
    return SYNONYMS.get(s, s)


# ---------------------------------------------------------------------------- frames
def _pil():
    from PIL import Image  # noqa: WPS433 (optional at import time, required at runtime)
    return Image


def _to_jpeg(img, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _shrink(jpeg: bytes, max_side: int = MAX_SIDE) -> bytes:
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg))
    w, h = img.size
    if max(w, h) <= max_side:
        return jpeg
    s = max_side / float(max(w, h))
    img = img.resize((int(w * s), int(h * s)))
    return _to_jpeg(img)


def frame_size(jpeg: bytes) -> tuple[int, int]:
    return _pil().open(io.BytesIO(jpeg)).size


def source_kind(source: str) -> str:
    s = (source or "").strip()
    if re.fullmatch(r"\d+", s):
        return "webcam"
    if s.lower().startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if s.lower().startswith(("http://", "https://")):
        return "http"
    if Path(s).suffix.lower() in VIDEO_EXT:
        return "video"
    return "file"


VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
_VIDEO_T0: dict[str, float] = {}      # path -> wall-clock start: a recording plays as a live camera, looping
_VIDEO_DUR: dict[str, float] = {}


def _grab_video(path: str) -> bytes:
    """The frame a recording would be showing right now if it had started playing at the first grab (loops)."""
    if not Path(path).is_file():
        raise RuntimeError(f"camera source not found: {path[:120]}")
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is required to play a video file as a camera")
    if path not in _VIDEO_DUR:
        _VIDEO_DUR[path] = _ffprobe_duration(path)
    dur = _VIDEO_DUR[path]
    t0 = _VIDEO_T0.setdefault(path, time.time())
    t = ((time.time() - t0) % dur) if dur > 0.5 else 0.0
    cmd = [ff, "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-pix_fmt", "yuvj420p",
           "-f", "image2", "-q:v", "3", "pipe:1"]                  # yuvj420p: limited-range sources (MPEG-2, H.264) otherwise fail in mjpeg
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT * 2)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("video frame grab timed out") from exc
    if p.returncode != 0 or p.stdout[:2] != b"\xff\xd8":
        raise RuntimeError(f"video frame grab failed: {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout


def _grab_webcam(index: int) -> bytes:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("webcam capture needs opencv-python (pip install opencv-python)") from exc
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(index)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"webcam {index} not available")
        ok, frame = False, None
        for _ in range(6):                          # first frames from a cold webcam are dark / stale
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"webcam {index} returned no frame")
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return bytes(enc.tobytes())
    finally:
        cap.release()


def _grab_rtsp(url: str) -> bytes:
    ff = shutil.which("ffmpeg")
    if ff:
        cmd = [ff, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
               "-frames:v", "1", "-f", "image2", "-q:v", "3", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"rtsp grab timed out after {FRAME_TIMEOUT:.0f}s") from exc
        if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8":
            return p.stdout
        err = (p.stderr or b"").decode(errors="replace").strip().splitlines()
        last = err[-1] if err else f"ffmpeg exit {p.returncode}"
        # fall through to OpenCV only if ffmpeg failed to connect; otherwise report the real error
        if "Connection refused" not in last and "timed out" not in last.lower() and "401" not in last:
            raise RuntimeError(f"rtsp grab failed: {last[:200]}")
        cv_err = last
    else:
        cv_err = "ffmpeg not installed"
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(f"rtsp grab failed ({cv_err}); opencv fallback unavailable") from exc
    cap = cv2.VideoCapture(url)
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"rtsp grab failed: {cv_err}")
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return bytes(enc.tobytes())
    finally:
        cap.release()


def _grab_http(url: str) -> bytes:
    """Snapshot URL (Hikvision /ISAPI/Streaming/channels/101/picture, ESP32-CAM /capture, any JPEG URL).
    Credentials may be embedded (http://user:pass@host/...) — Basic and Digest are both tried."""
    u = httpx.URL(url)
    auth = None
    if u.username:
        auth = httpx.DigestAuth(u.username, u.password or "")
        url = str(u.copy_with(username=None, password=None))
    with httpx.Client(timeout=FRAME_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url, auth=auth)
        if r.status_code == 401 and auth is not None:
            r = c.get(url, auth=(u.username, u.password or ""))
        r.raise_for_status()
        data = r.content
    ctype = r.headers.get("content-type", "")
    if data[:2] != b"\xff\xd8":
        if "multipart/x-mixed-replace" in ctype or b"\xff\xd8" in data[:4096]:
            i = data.find(b"\xff\xd8"); j = data.find(b"\xff\xd9", i + 2)
            if i >= 0 and j > i:
                return data[i:j + 2]
        # PNG or anything PIL can read → re-encode
        try:
            return _to_jpeg(_pil().open(io.BytesIO(data)))
        except Exception as exc:
            raise RuntimeError(f"snapshot URL did not return an image ({ctype or 'no content-type'})") from exc
    return data


def grab(source: str) -> bytes:
    """One JPEG frame from any supported source. Raises RuntimeError with a human-readable reason."""
    kind = source_kind(source)
    s = source.strip()
    try:                                                   # a live feed already decoding this source: share its frame
        from . import live as _live
        feed = _live.get(s)
    except Exception:
        feed = None
    if feed is not None:
        jpeg = feed.raw_jpeg(MAX_SIDE)
        if jpeg:
            return jpeg
    if kind == "webcam":
        jpeg = _grab_webcam(int(s))
    elif kind == "rtsp":
        jpeg = _grab_rtsp(s)
    elif kind == "http":
        jpeg = _grab_http(s)
    elif kind == "video":
        jpeg = _grab_video(s)
    else:
        p = Path(s)
        if not p.is_file():
            raise RuntimeError(f"camera source not found: {s[:120]}")
        data = p.read_bytes()
        jpeg = data if data[:2] == b"\xff\xd8" else _to_jpeg(_pil().open(io.BytesIO(data)))
    return _shrink(jpeg)


# ---------------------------------------------------------------------------- motion
def _gray_small(jpeg: bytes, side: int = 64):
    import numpy as np
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((side, side))
    return np.asarray(img, dtype="float32") / 255.0


def motion(prev_jpeg: bytes | None, cur_jpeg: bytes) -> float:
    """0..1 — mean absolute difference between two frames on a 64x64 grey thumbnail. ~0.02 is noise,
    >0.08 is somebody walking through, >0.3 is a scene change / camera moved."""
    if not prev_jpeg:
        return 1.0
    try:
        a, b = _gray_small(prev_jpeg), _gray_small(cur_jpeg)
        return float(abs(a - b).mean())
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------- detection
class Detector:
    """Local object detector (ultralytics YOLO). Loaded once, thread-safe, optional."""

    def __init__(self, weights: str = YOLO_WEIGHTS):
        self.weights = weights
        self._model = None
        self._lock = threading.Lock()
        self.error = ""

    @property
    def available(self) -> bool:
        if self._model is not None:
            return True
        if self.error:
            return False
        try:
            import ultralytics  # noqa: F401
            return True
        except Exception as exc:
            self.error = f"ultralytics not installed ({type(exc).__name__})"
            return False

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO
            Path(self.weights).parent.mkdir(parents=True, exist_ok=True)
            self._model = YOLO(self.weights)          # downloads the standard weights on first use
        return self._model

    def new_model(self):
        """A private model for one long-running consumer (a live feed). The shared instance serialises every caller
        behind one lock and carries ONE tracker state, so five cameras on it ran at a fifth of the speed each and
        had their track ids mixed together."""
        from ultralytics import YOLO
        self._load()                                   # makes sure the weights are on disk
        return YOLO(self.weights)

    def detect(self, jpeg: bytes, conf: float = 0.35) -> list[dict[str, Any]]:
        if not self.available:
            return []
        Image = _pil()
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        with self._lock:
            try:
                model = self._load()
                res = model.predict(img, conf=conf, verbose=False)[0]
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                return []
        out = []
        for b in res.boxes:
            x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]
            out.append({"label": model.names[int(b.cls)], "conf": round(float(b.conf), 2), "box": [x1, y1, x2, y2]})
        out.sort(key=lambda d: -d["conf"])
        return out


DETECTOR = Detector()


class PreciseDetector:
    """The detector for the RECORD (object catalogue, audits), not for the live picture.

    Measured against human ground truth on the campus clips, the live detector (nano model, whole frame squeezed to
    640 px) finds 63% of the people in a lobby and 4% of the people on a distant road: far objects are a few pixels
    tall by the time the model sees them. This one looks at the full-resolution frame twice: once whole, and once as
    overlapping tiles so small, distant objects reach the model at a usable size; the passes are merged with NMS.
    Slower (hundreds of ms), so it runs at the analysis rate, never per displayed frame. One instance per consumer."""

    def __init__(self, weights: str = "", tiles: tuple[int, int] | None = None, tile_size: int = 0, full_size: int = 0,
                 conf: float = 0.0):
        self.weights = weights or os.environ.get("VISION_YOLO_PRECISE", str(MODELS_DIR / "yolov8s.pt"))
        t = os.environ.get("VISION_TILES", "2x2").lower().split("x")
        self.tiles = tiles or (int(t[0]), int(t[1]))
        self.tile_size = tile_size or int(os.environ.get("VISION_TILE_SIZE", "960"))
        self.full_size = full_size or int(os.environ.get("VISION_FULL_SIZE", "1280"))
        self.conf = conf or float(os.environ.get("VISION_PRECISE_CONF", "0.35"))
        self.overlap = 0.2
        self._model = None
        self.error = ""

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO
            Path(self.weights).parent.mkdir(parents=True, exist_ok=True)
            self._model = YOLO(self.weights)
        return self._model

    def detect_bgr(self, frame) -> list[dict[str, Any]]:
        """Detections on a BGR frame, boxes in that frame's pixels, best first."""
        import torch
        import torchvision
        m = self._load()
        h, w = frame.shape[:2]
        nx, ny = self.tiles
        boxes: list[list[float]] = []
        scores: list[float] = []
        clss: list[int] = []
        if nx * ny > 1 and min(h, w) >= 480:
            ov = self.overlap
            tw, th = int(w / (nx - (nx - 1) * ov)), int(h / (ny - (ny - 1) * ov))
            crops, offs = [], []
            for j in range(ny):
                for i in range(nx):
                    x0, y0 = min(w - tw, int(i * tw * (1 - ov))), min(h - th, int(j * th * (1 - ov)))
                    crops.append(frame[y0:y0 + th, x0:x0 + tw])
                    offs.append((x0, y0))
            for r, (x0, y0) in zip(m.predict(crops, imgsz=self.tile_size, conf=self.conf, verbose=False), offs):
                for b in r.boxes:
                    x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                    cut = (x1 < 2 and x0 > 0) or (y1 < 2 and y0 > 0) or (x2 > tw - 2 and x0 + tw < w) or (y2 > th - 2 and y0 + th < h)
                    if cut and (x2 - x1) * (y2 - y1) > 0.25 * tw * th:
                        continue                           # a big object cut by the tile edge: the whole-frame pass owns it
                    boxes.append([x1 + x0, y1 + y0, x2 + x0, y2 + y0]); scores.append(float(b.conf)); clss.append(int(b.cls))
        for b in m.predict(frame, imgsz=self.full_size, conf=self.conf, verbose=False)[0].boxes:
            boxes.append([float(v) for v in b.xyxy[0]]); scores.append(float(b.conf)); clss.append(int(b.cls))
        if not boxes:
            return []
        B, S, C = torch.tensor(boxes), torch.tensor(scores), torch.tensor(clss)
        keep = torchvision.ops.batched_nms(B, S, C, 0.5)
        out = [{"label": m.names[int(C[k])], "conf": round(float(S[k]), 3), "box": [int(v) for v in B[k].tolist()]} for k in keep]
        out.sort(key=lambda d: -d["conf"])
        return out


def counts(dets: list[dict[str, Any]]) -> dict[str, int]:
    c: dict[str, int] = {}
    for d in dets:
        c[d["label"]] = c.get(d["label"], 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def counts_text(c: dict[str, int]) -> str:
    if not c:
        return "nothing recognised"
    parts = []
    for k, n in c.items():
        parts.append(f"{n} {k}" + ("s" if n != 1 and not k.endswith("s") and k != "person" else "") if k != "person"
                     else (f"{n} person" if n == 1 else f"{n} people"))
    return ", ".join(parts)


def annotate(jpeg: bytes, dets: list[dict[str, Any]], banner: str = "") -> bytes:
    from PIL import ImageDraw
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    dr = ImageDraw.Draw(img)
    for d in dets:
        x1, y1, x2, y2 = d["box"]
        col = (76, 144, 240) if d["label"] == "person" else (52, 211, 153)
        dr.rectangle([x1, y1, x2, y2], outline=col, width=2)
        tag = f"{d['label']} {int(d['conf'] * 100)}%"
        tw = 7 * len(tag) + 6
        dr.rectangle([x1, max(0, y1 - 14), x1 + tw, y1], fill=col)
        dr.text((x1 + 3, max(0, y1 - 13)), tag, fill=(10, 12, 16))
    if banner:
        dr.rectangle([0, img.size[1] - 16, img.size[0], img.size[1]], fill=(11, 14, 19))
        dr.text((4, img.size[1] - 14), banner[:120], fill=(230, 233, 238))
    return _to_jpeg(img, 80)


# ---------------------------------------------------------------------------- vision-language model
def _vlm_cfg(model: str = "") -> tuple[str, str, str]:
    """(base_url, api_key, model) for the vision model — an OpenAI-compatible provider from config/providers.json."""
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    pname = DEFAULT_VLM_PROVIDER
    pc = (providers.get("providers") or {}).get(pname) or cfg.DEFAULT_PROVIDERS["providers"]["openrouter"]
    key = (pc.get("api_key") or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    return pc.get("base_url", "https://openrouter.ai/api/v1").rstrip("/"), key, (model or DEFAULT_VLM)


def vlm_chain(model: str = "") -> list[dict[str, str]]:
    """Ordered free-first provider chain for vision calls. Each entry: {name, base, key, model}.
    A caller-forced model pins the call to the OpenRouter entry; otherwise we try Groq (fastest),
    then Gemini (best free quota), then the configured OpenRouter free model. Keys come from env:
    GROQ_API_KEY, GEMINI_API_KEY — each is a separate free daily bucket, so the chain stacks quota."""
    base, orkey, _m = _vlm_cfg()
    if model:                                        # explicit model (theatre dropdown / config) → no silent substitution
        return [{"name": "openrouter", "base": base, "key": orkey, "model": model}]
    chain: list[dict[str, str]] = []
    gq = os.environ.get("GROQ_API_KEY", "").strip()
    if gq:
        chain.append({"name": "groq", "base": "https://api.groq.com/openai/v1", "key": gq,
                      "model": os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")})
    gm = os.environ.get("GEMINI_API_KEY", "").strip()
    if gm:
        chain.append({"name": "gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": gm,
                      "model": os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-flash-lite")})
    if orkey:
        chain.append({"name": "openrouter", "base": base, "key": orkey, "model": DEFAULT_VLM})
    return chain


def _retryable(status: int) -> bool:
    # any provider-side failure moves us down the chain (wrong model id, quota, auth, outage) —
    # the request payload is identical for every OpenAI-compatible provider, so 4xx here means "this provider", not "this request"
    return status >= 400


def vlm_ready() -> bool:
    return bool(_vlm_cfg()[1])


def describe(jpeg: bytes, question: str, model: str = "", context: str = "", max_tokens: int = 400,
             transport: httpx.BaseTransport | None = None) -> str:
    """Ask the vision model one question about the frame. Plain-text answer, never invents what it can't see."""
    chain = [e for e in vlm_chain(model) if e["key"]]
    if not chain:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)")
    system = ("You are the vision analyst of a small business's operations desk. You look at one camera frame and "
              "answer the owner's question precisely. State only what is visible; say 'not visible' rather than guess. "
              "Count carefully. Keep it under 80 words unless asked for detail. Plain text only — no markdown, no headings, no asterisks. "
              "Never identify people by name.")
    if context:
        system += "\n\nContext: " + context[:800]
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": question.strip() or "Describe what is happening in this frame."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        ]},
    ]
    r = None
    with httpx.Client(timeout=90, transport=transport) as c:
        for i, e in enumerate(chain):
            headers = {"Authorization": f"Bearer {e['key']}", "Content-Type": "application/json",
                       "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
            try:
                r = c.post(e["base"] + "/chat/completions", headers=headers,
                           json={"model": e["model"], "max_tokens": max_tokens, "temperature": 0.1, "messages": msgs, **_extras(e["model"])})
            except httpx.HTTPError:
                if i + 1 < len(chain):
                    continue
                raise
            if _retryable(r.status_code) and i + 1 < len(chain):
                continue
            break
    if r is None or r.status_code >= 400:
        raise RuntimeError(f"vision model HTTP {r.status_code if r is not None else '?'}: {r.text[:200] if r is not None else 'no provider reachable'}")
    j = r.json()
    try:
        msg = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision model returned no answer: {json.dumps(j)[:200]}") from exc
    if isinstance(msg, list):
        msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return (msg or "").strip()


def _extras(model: str) -> dict[str, Any]:
    from .providers import model_extras
    return model_extras(model)


def vlm_detect(jpeg: bytes, labels: list[str], model: str = "", transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """Detector fallback when ultralytics is not installed: the vision model counts the watched labels.
    Boxes are unknown (empty); confidence is nominal."""
    want = ", ".join(labels) or "person"
    q = (f"Count how many of each of these are visible: {want}. Reply with JSON only, like "
         f'{{"person": 2, "car": 0}} — one integer per label, nothing else.')
    txt = describe(jpeg, q, model=model, max_tokens=120, transport=transport)
    m = re.search(r"\{.*\}", txt, re.S)
    out: list[dict[str, Any]] = []
    if not m:
        return out
    try:
        data = json.loads(m.group(0))
    except Exception:
        return out
    for k, v in data.items():
        try:
            n = int(v)
        except Exception:
            continue
        out.extend({"label": _norm_label(str(k)), "conf": 0.5, "box": [0, 0, 0, 0]} for _ in range(max(0, min(n, 50))))
    return out


# ---------------------------------------------------------------------------- rules
def parse_hours(spec: str) -> tuple[int, int] | None:
    """'20:00-07:00' → (1200, 420) minutes; blank → None (always)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*", spec or "")
    if not m:
        return None
    a = int(m.group(1)) * 60 + int(m.group(2) or 0)
    b = int(m.group(3)) * 60 + int(m.group(4) or 0)
    return a, b


def in_hours(spec: str, now: datetime | None = None) -> bool:
    win = parse_hours(spec)
    if not win:
        return True
    now = now or datetime.now()
    t = now.hour * 60 + now.minute
    a, b = win
    return a <= t < b if a <= b else (t >= a or t < b)      # overnight windows wrap


def rule_config(config: dict[str, Any]) -> dict[str, Any]:
    labels = [_norm_label(x) for x in str(config.get("watch_for") or "person").split(",") if x.strip()]
    try:
        min_count = max(1, int(config.get("min_count") or 1))
    except Exception:
        min_count = 1
    try:
        cooldown = max(0, float(config.get("cooldown_min") or 10))
    except Exception:
        cooldown = 10.0
    try:
        motion_min = float(config.get("motion_min") or 0.03)
    except Exception:
        motion_min = 0.03
    try:
        alert_on_motion = float(config.get("alert_on_motion") or 0)      # 0 = off; e.g. 0.2 = wake the desk on any scene change
    except Exception:
        alert_on_motion = 0.0
    try:
        dwell_min = max(0.0, float(config.get("dwell_min") or 0))      # >0: "N present for at least M minutes"
    except Exception:
        dwell_min = 0.0
    repeat = str(config.get("repeat") or "changes").strip().lower()
    if repeat not in ("changes", "always", "once"):
        repeat = "changes"
    return {"labels": labels, "min_count": min_count, "cooldown_min": cooldown, "hours": str(config.get("hours") or ""),
            "motion_min": motion_min, "alert_on_motion": alert_on_motion, "dwell_min": dwell_min, "repeat": repeat,
            "alerts": str(config.get("alerts", "1")).strip().lower() not in ("0", "false", "off", "no"),
            "question": str(config.get("question") or ""), "task": str(config.get("task") or "")}


def evaluate(config: dict[str, Any], dets: list[dict[str, Any]], prev_counts: dict[str, int] | None,
             last_trigger_ts: float | None, now_ts: float | None = None, mot: float = 0.0,
             present_since: float | None = None) -> tuple[bool, str]:
    """Should this frame wake the desk? Returns (triggered, reason).

    Fires when the watched count reaches min_count inside the hours window. Repeats are governed by `repeat`:
      changes (default)  after the cooldown, only if the count changed or the scene moved (motion >= motion_min);
                         a parked car or a person standing still never pages the owner twice
      always             after the cooldown even if nothing changed (the old behaviour)
      once               only when the count crosses min_count from below
    `dwell_min` > 0 turns the rule into "N present for at least M minutes" (a queue that has been waiting):
    the caller tracks `present_since` (when the count first reached min_count) and the rule fires once per stay."""
    r = rule_config(config)
    now_ts = now_ts or time.time()
    c = counts(dets)
    lab = "/".join(r["labels"])
    n = sum(c.get(l, 0) for l in r["labels"])
    prev_n = sum((prev_counts or {}).get(l, 0) for l in r["labels"])
    cooled = last_trigger_ts is None or (now_ts - last_trigger_ts) >= r["cooldown_min"] * 60
    moved = mot >= r["motion_min"]
    if r["alert_on_motion"] and mot >= r["alert_on_motion"] and prev_counts is not None \
            and in_hours(r["hours"], datetime.fromtimestamp(now_ts)) and cooled:
        return True, f"scene changed (motion {mot:.2f} ≥ {r['alert_on_motion']:g})"
    if n < r["min_count"]:
        return False, f"{n} {lab} (< {r['min_count']})"
    if not in_hours(r["hours"], datetime.fromtimestamp(now_ts)):
        return False, f"{n} {lab} but outside {r['hours']}"
    if r["dwell_min"] > 0:
        if present_since is None:
            return False, f"{n} {lab} present, dwell timer not started"
        held = now_ts - present_since
        if held < r["dwell_min"] * 60:
            return False, f"{n} {lab} present {held:.0f}s (alert after {r['dwell_min']:g} min)"
        if last_trigger_ts is None or last_trigger_ts < present_since:
            return True, f"{n} {lab} present for {held / 60:.1f} min"
        if not cooled:
            return False, f"{n} {lab} still waiting ({held / 60:.0f} min), already alerted"
        if r["repeat"] == "always" or (r["repeat"] == "changes" and (n != prev_n or moved)):
            return True, f"{n} {lab} still waiting after {held / 60:.0f} min"
        return False, f"{n} {lab} still waiting ({held / 60:.0f} min), nothing new"
    if not cooled:
        return False, (f"{lab} {prev_n} → {n} (cooldown)" if n != prev_n else f"{n} {lab} unchanged")
    if prev_n < r["min_count"]:
        return True, f"{lab} count {prev_n} → {n}"
    if r["repeat"] == "once":
        return False, f"{n} {lab} still present (alert once per stay)"
    if n != prev_n:
        return True, f"{lab} count {prev_n} → {n}"
    if last_trigger_ts is None:
        return True, f"{n} {lab} present, not yet alerted"
    if r["repeat"] == "always":
        return True, f"{n} {lab} still present after cooldown"
    if moved:
        return True, f"{n} {lab} still present, scene changed (motion {mot:.2f})"
    return False, f"{n} {lab} still present, nothing new"


# ---------------------------------------------------------------------------- snapshots
def save_snapshot(desk_id: int, camera: str, jpeg: bytes, keep: int = 600) -> str:
    d = SNAP_DIR / f"desk{desk_id}"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", camera)[:40] or "cam"
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{int((time.time() % 1) * 1000):03d}-{safe}.jpg"
    (d / name).write_bytes(jpeg)
    files = sorted(d.glob("*.jpg"))
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(d / name)


def event_text(camera: str, c: dict[str, int], mot: float, reason: str, answer: str = "") -> str:
    s = f"{camera}: {counts_text(c)} (motion {mot:.2f}) — {reason}"
    return s + (f". Analyst: {answer}" if answer else "")


def analyse(source: str, config: dict[str, Any], prev_jpeg: bytes | None = None, live_vlm: bool = False,
            question: str = "", detector: Detector | None = None) -> dict[str, Any]:
    """Grab + motion + detect (+ optional VLM answer). Pure function of the inputs; the caller decides what to store."""
    det = detector or DETECTOR
    jpeg = grab(source)
    mot = motion(prev_jpeg, jpeg)
    r = rule_config(config)
    dets: list[dict[str, Any]] = []
    backend = "none"
    if det.available:
        dets, backend = det.detect(jpeg), "yolo"
    elif live_vlm and vlm_ready() and mot >= r["motion_min"]:
        dets, backend = vlm_detect(jpeg, r["labels"], model=str(config.get("vlm_model") or "")), "vlm"
    c = counts(dets)
    answer = ""
    q = question or ""
    if q and live_vlm and vlm_ready():
        try:
            answer = describe(jpeg, q, model=str(config.get("vlm_model") or ""), context=str(config.get("notes") or ""))
        except Exception as exc:
            answer = f"(vision model unavailable: {str(exc)[:120]})"
    w, h = frame_size(jpeg)
    return {"jpeg": jpeg, "annotated": annotate(jpeg, dets, f"{time.strftime('%d %b %H:%M:%S')}  {counts_text(c)}"),
            "detections": dets, "counts": c, "motion": round(mot, 3), "backend": backend, "answer": answer,
            "size": [w, h], "detector_error": det.error}


# ---------------------------------------------------------------------------- video understanding
def _ffprobe_duration(path: str) -> float:
    fp = shutil.which("ffprobe")
    if not fp:
        return 0.0
    try:
        p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=30)
        return float((p.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def sample_video_frames(source: str, n: int = 8) -> tuple[list[tuple[float, bytes]], float]:
    """Evenly sample up to n JPEG frames from a local video file (or any URL ffmpeg can read).
    Returns ([(timestamp_s, jpeg), ...], duration_s). Requires ffmpeg."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is required for video understanding and is not installed")
    dur = _ffprobe_duration(source)
    n = max(1, min(int(n or 8), 10))
    if dur > 0:
        stamps = [dur * (i + 1) / (n + 1) for i in range(n)]
    else:                                            # duration unknown (stream/pipe): take the first frames spaced 2s
        stamps = [i * 2.0 for i in range(n)]
    frames: list[tuple[float, bytes]] = []
    for t in stamps:
        cmd = [ff, "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", source,
               "-frames:v", "1", "-f", "image2", "-q:v", "3", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT * 2)
        except subprocess.TimeoutExpired:
            continue
        if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8":
            frames.append((t, p.stdout))
    if not frames:
        raise RuntimeError(f"could not extract any frames from {source}")
    return frames, dur


def chat_images(system: str, text: str, images: list[tuple[str, bytes]], model: str = "", max_tokens: int = 500,
                transport: httpx.BaseTransport | None = None) -> str:
    """One vision-model call with several labelled frames (RAG re-look, comparisons). Same free-first provider
    chain as describe(); each image is preceded by its label so the model can cite it."""
    chain = [e for e in vlm_chain(model) if e["key"]]
    if not chain:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for label, jpeg in images:
        content.append({"type": "text", "text": f"Frame {label}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}})
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    r = None
    with httpx.Client(timeout=120, transport=transport) as c:
        for i, e in enumerate(chain):
            headers = {"Authorization": f"Bearer {e['key']}", "Content-Type": "application/json",
                       "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
            try:
                r = c.post(e["base"] + "/chat/completions", headers=headers,
                           json={"model": e["model"], "max_tokens": max_tokens, "temperature": 0.1, "messages": msgs, **_extras(e["model"])})
            except httpx.HTTPError:
                if i + 1 < len(chain):
                    continue
                raise
            if _retryable(r.status_code) and i + 1 < len(chain):
                continue
            break
    if r is None or r.status_code >= 400:
        raise RuntimeError(f"vision model HTTP {r.status_code if r is not None else '?'}: {r.text[:200] if r is not None else 'no provider reachable'}")
    j = r.json()
    try:
        msg = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision model returned no answer: {json.dumps(j)[:200]}") from exc
    if isinstance(msg, list):
        msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return (msg or "").strip()


def chat_images_stream(system: str, text: str, images: list[tuple[str, bytes]], model: str = "", max_tokens: int = 500,
                       transport: httpx.BaseTransport | None = None):
    """Streaming twin of chat_images(): yields a meta dict {provider, model} first, then text deltas as the vision
    model produces them. Same free-first provider chain and retry rules."""
    chain = [e for e in vlm_chain(model) if e["key"]]
    if not chain:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for label, jpeg in images:
        content.append({"type": "text", "text": f"Frame {label}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}})
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    with httpx.Client(timeout=120, transport=transport) as c:
        for i, e in enumerate(chain):
            headers = {"Authorization": f"Bearer {e['key']}", "Content-Type": "application/json",
                       "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
            try:
                with c.stream("POST", e["base"] + "/chat/completions", headers=headers,
                              json={"model": e["model"], "max_tokens": max_tokens, "temperature": 0.1, "stream": True,
                                    "messages": msgs, **_extras(e["model"])}) as r:
                    if r.status_code >= 400:
                        r.read()
                        if _retryable(r.status_code) and i + 1 < len(chain):
                            continue
                        raise RuntimeError(f"vision model HTTP {r.status_code}: {r.text[:200]}")
                    yield {"provider": e["name"], "model": e["model"]}
                    for line in r.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            j = json.loads(data)
                            delta = j["choices"][0].get("delta", {}).get("content") or ""
                        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(delta, list):
                            delta = "".join(p.get("text", "") for p in delta if isinstance(p, dict))
                        if delta:
                            yield delta
                    return
            except httpx.HTTPError:
                if i + 1 < len(chain):
                    continue
                raise


def describe_video(source: str, question: str = "", model: str = "", frames: int = 8, context: str = "",
                   transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Watch a video: sample frames evenly, send them all to the vision model in one call, get a timeline
    description + an answer to the question. Returns {answer, duration_s, frames:[{t, jpeg}]}."""
    sampled, dur = sample_video_frames(source, frames)
    base, key, model = _vlm_cfg(model)
    if not key:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY or config/providers.json openrouter.api_key)")
    system = ("You are the vision analyst of a small business's operations desk. You are shown frames sampled evenly "
              "from ONE video, each labelled with its timestamp. Describe what happens over time as a short timeline "
              "(what changes between frames), then answer the owner's question. State only what is visible; say 'not "
              "visible' rather than guess. Never identify people by name. Plain text only.")
    if context:
        system += "\n\nContext: " + context[:800]
    content: list[dict[str, Any]] = [{"type": "text", "text":
        (question.strip() or "Describe this video.") + f"\n\nVideo duration ≈{dur:.0f}s; {len(sampled)} frames follow, in order."}]
    for t, jpeg in sampled:
        content.append({"type": "text", "text": f"[frame at {t:.1f}s]"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}})
    payload = {"model": model, "max_tokens": 700, "temperature": 0.1,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}], **_extras(model)}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
    with httpx.Client(timeout=180, transport=transport) as c:
        r = c.post(base + "/chat/completions", headers=headers, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"vision model HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    try:
        msg = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision model returned no answer: {json.dumps(j)[:200]}") from exc
    if isinstance(msg, list):
        msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return {"answer": (msg or "").strip(), "duration_s": dur,
            "frames": [{"t": t, "jpeg": jpeg} for t, jpeg in sampled]}


def describe_stream(jpeg: bytes, question: str, model: str = "", context: str = "", max_tokens: int = 220):
    """Streaming variant of describe(): yields text deltas as the vision model produces them.
    Used by the live theatre page."""
    chain = [e for e in vlm_chain(model) if e["key"]]
    if not chain:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)")
    system = ("You are the live vision commentator of a small business's operations desk. You get one CCTV frame every "
              "few seconds from a feed you are watching continuously. In 1-2 short sentences, log what is happening NOW "
              "and what CHANGED since your previous notes (given as context). Count people carefully. State only what "
              "is visible; never invent details; never identify anyone by name. Plain text, no markdown.")
    if context:
        system += "\n\nYour previous notes on this feed:\n" + context[:1200]
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": question.strip() or "What is happening in this frame? What changed?"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        ]},
    ]
    with httpx.Client(timeout=120) as c:
        for i, e in enumerate(chain):
            headers = {"Authorization": f"Bearer {e['key']}", "Content-Type": "application/json",
                       "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk live vision"}
            try:
                with c.stream("POST", e["base"] + "/chat/completions", headers=headers,
                              json={"model": e["model"], "max_tokens": max_tokens, "temperature": 0.1,
                                    "stream": True, "messages": msgs}) as r:
                    if r.status_code >= 400:
                        r.read()
                        if _retryable(r.status_code) and i + 1 < len(chain):
                            continue
                        raise RuntimeError(f"vision model HTTP {r.status_code}: {r.text[:200]}")
                    yield {"provider": e["name"], "model": e["model"]}     # meta first, then text deltas
                    for line in r.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            j = json.loads(data)
                            delta = j["choices"][0].get("delta", {}).get("content") or ""
                        except (KeyError, IndexError, json.JSONDecodeError):
                            continue
                        if delta:
                            yield delta
                    return
            except httpx.HTTPError:
                if i + 1 < len(chain):
                    continue
                raise
