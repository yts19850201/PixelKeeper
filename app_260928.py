"""Local drag-and-drop media optimizer. Requires FFmpeg and ffprobe on PATH."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OUTPUT = ROOT / "output_260928"
PAGE = ASSETS / "index_260928.html"
FFMPEG = ASSETS / "runtime" / "ffmpeg.exe"
FFPROBE = ASSETS / "runtime" / "ffprobe.exe"
MAX_UPLOAD = 1024 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".avif", ".gif", ".apng"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SETTINGS = {
    "high": {"webp": "90", "avif": "25", "h264": "18", "vp9": "25", "jpeg": "92"},
    "balanced": {"webp": "82", "avif": "32", "h264": "23", "vp9": "32", "jpeg": "85"},
    "compact": {"webp": "74", "avif": "38", "h264": "28", "vp9": "38", "jpeg": "76"},
}
DOWNLOADS: dict[str, Path] = {}
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries",
         "stream=index,codec_type,width,height,nb_frames,duration,pix_fmt:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ValueError("읽을 수 없는 이미지 또는 영상입니다.")
    data = json.loads(result.stdout)
    streams = [item for item in data.get("streams", []) if item.get("codec_type") == "video"]
    if not streams:
        raise ValueError("영상 또는 이미지의 해상도를 확인할 수 없습니다.")
    frame_number = lambda item: int(item["nb_frames"]) if str(item.get("nb_frames", "")).isdigit() else 0
    stream = max(streams, key=frame_number)
    if not stream.get("width") or not stream.get("height"):
        raise ValueError("영상 또는 이미지의 해상도를 확인할 수 없습니다.")
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frames": frame_number(stream),
        "stream_index": int(stream["index"]),
        "duration": float(data.get("format", {}).get("duration") or stream.get("duration") or 0),
        "pix_fmt": stream.get("pix_fmt", ""),
    }


def has_multiple_frames(path: Path, info: dict) -> bool:
    if info["frames"] > 1:
        return True
    args = [str(FFPROBE), "-v", "error"]
    if path.suffix.lower() != ".avif":
        args += ["-read_intervals", "%+#2"]
    args += ["-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        return False
    streams = json.loads(result.stdout).get("streams", [])
    return any(str(item.get("nb_read_frames", "")).isdigit() and int(item["nb_read_frames"]) > 1 for item in streams)

def media_kind(path: Path, info: dict) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in {".gif", ".apng"}:
        return "animated"
    if ext in {".webp", ".avif"} and has_multiple_frames(path, info):
        return "animated"
    return "image"

def command(source: Path, target: Path, fmt: str, kind: str, quality: str, stream_index: int) -> list[str]:
    q = SETTINGS[quality]
    args = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", f"0:{stream_index}", "-an", "-sn"]
    if kind == "image":
        args += ["-frames:v", "1"]
    if fmt == "webp":
        args += ["-c:v", "libwebp_anim" if kind != "image" else "libwebp", "-quality", q["webp"], "-compression_level", "6"]
        if kind != "image":
            args += ["-loop", "0"]
    elif fmt == "avif":
        args += ["-c:v", "libaom-av1", "-crf", q["avif"], "-b:v", "0", "-cpu-used", "6", "-pix_fmt", "yuv420p"]
        if kind == "image":
            args += ["-still-picture", "1"]
    elif fmt == "mp4":
        args += ["-c:v", "libx264", "-preset", "slow", "-crf", q["h264"], "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    elif fmt == "webm":
        args += ["-c:v", "libvpx-vp9", "-crf", q["vp9"], "-b:v", "0", "-pix_fmt", "yuv420p", "-row-mt", "1"]
    elif fmt == "jpg":
        args += ["-c:v", "mjpeg", "-q:v", str(max(2, round((100 - int(q["jpeg"])) / 4)))]
    elif fmt == "png":
        args += ["-c:v", "png", "-compression_level", "9"]
    else:
        raise ValueError("지원하지 않는 형식입니다.")
    return args + [str(target)]


def run_with_progress(args: list[str], duration: float, progress) -> tuple[int, str]:
    """Read FFmpeg's encoded timestamp to report conversion progress."""
    args = args[:-1] + ["-progress", "pipe:1", "-nostats", args[-1]]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
    timed_out = threading.Event()
    def stop() -> None:
        timed_out.set()
        process.kill()
    timer = threading.Timer(900, stop)
    timer.start()
    recent = []
    try:
        for line in process.stdout:
            line = line.strip()
            recent.append(line)
            recent = recent[-20:]
            if duration > 0 and line.startswith("out_time_us="):
                try:
                    progress(min(0.99, max(0.0, int(line.split("=", 1)[1]) / (duration * 1_000_000))))
                except ValueError:
                    pass
        code = process.wait()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(args, 900)
        return code, "\n".join(recent)
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()


def convert(source: Path, original_name: str, requested: str, quality: str, progress=None) -> dict:
    progress = progress or (lambda percent, stage: None)
    progress(0, "파일 분석 중")
    if quality not in SETTINGS:
        raise ValueError("화질 설정이 올바르지 않습니다.")
    before = probe(source)
    kind = media_kind(source, before)
    choices = (["webp", "avif"] if kind == "image" else ["webp", "mp4", "webm"])
    if requested != "auto":
        allowed = {"webp", "avif", "jpg", "png"} if kind == "image" else {"webp", "avif", "mp4", "webm"}
        if requested not in allowed:
            raise ValueError("선택한 형식은 이 파일에 사용할 수 없습니다.")
        choices = [requested]
    safe_stem = re.sub(r"[^\w가-힣.-]+", "_", Path(original_name).stem, flags=re.UNICODE).strip("._")[:70] or "media"
    job_dir = OUTPUT / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    candidates = []
    errors = []
    started = time.monotonic()
    for index, fmt in enumerate(choices):
        dest = job_dir / f"{safe_stem}_{fmt}_{datetime.now():%y%m%d}.{fmt}"
        progress(round(index / len(choices) * 100), f"{fmt.upper()} 변환 중")
        try:
            if fmt == "mp4" and (before["width"] % 2 or before["height"] % 2):
                raise ValueError("MP4(H.264)는 가로·세로 픽셀이 짝수여야 합니다. 해상도를 유지하려면 WebM, WebP 또는 AVIF를 선택하세요.")
            code, error_text = run_with_progress(
                command(source, dest, fmt, kind, quality, before["stream_index"]), before["duration"],
                lambda fraction: progress(min(99, round((index + fraction) / len(choices) * 100)), f"{fmt.upper()} 변환 중"),
            )
            if code or not dest.exists() or not dest.stat().st_size:
                raise ValueError((error_text or "변환에 실패했습니다.").strip()[-350:])
            after = probe(dest)
            if (before["width"], before["height"]) != (after["width"], after["height"]):
                raise ValueError("출력 해상도가 원본과 달라졌습니다.")
            if kind != "image" and not has_multiple_frames(dest, after):
                raise ValueError("움직이는 파일이 한 장의 이미지로 변환됐습니다.")
            candidates.append((dest.stat().st_size, dest, fmt))
        except (ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{fmt}: {exc}")
            dest.unlink(missing_ok=True)
        progress(min(99, round((index + 1) / len(choices) * 100)), "결과 확인 중")
    original_size = source.stat().st_size
    if requested == "auto":
        smaller = [item for item in candidates if item[0] < original_size]
        if smaller:
            chosen = min(smaller)
            status = "최소 용량 결과를 선택했습니다."
        else:
            ext = Path(original_name).suffix.lower() or ".bin"
            dest = job_dir / f"{safe_stem}_original_{datetime.now():%y%m%d}{ext}"
            shutil.copy2(source, dest)
            chosen = (original_size, dest, "original")
            status = "해상도를 유지하며 더 작게 만든 결과가 없어 원본을 보관했습니다."
    else:
        if not candidates:
            raise ValueError("변환에 실패했습니다. " + "; ".join(errors))
        chosen = candidates[0]
        status = "변환을 완료했습니다."
    for _, candidate, _ in candidates:
        if candidate != chosen[1]:
            candidate.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    with LOCK:
        DOWNLOADS[token] = chosen[1]
    progress(100, "완료")
    return {
        "name": chosen[1].name, "format": chosen[2], "original_bytes": original_size,
        "output_bytes": chosen[0], "saved_percent": round((1 - chosen[0] / original_size) * 100, 1),
        "width": before["width"], "height": before["height"], "seconds": round(time.monotonic() - started, 1),
        "message": status, "download": "/download/" + token, "preview": "/preview/" + token, "notes": errors,
    }


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)

    def json_response(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def preview_response(self, file: Path) -> None:
        mime = {".webm": "video/webm", ".mp4": "video/mp4", ".webp": "image/webp",
                ".avif": "image/avif", ".gif": "image/gif", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".png": "image/png"}.get(file.suffix.lower(), "application/octet-stream")
        size = file.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if not match:
                self.send_error(416)
                return
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with file.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.json_response(200, {"service": "PixelKeeper", "version": "v4"})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            with LOCK:
                job = JOBS.get(job_id)
                snapshot = job.copy() if job else None
            if snapshot:
                self.json_response(200, snapshot)
            else:
                self.json_response(404, {"error": "작업을 찾을 수 없습니다."})
            return
        if path == "/":
            data = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/preview/"):
            token = path.removeprefix("/preview/")
            with LOCK:
                file = DOWNLOADS.get(token)
            if file and file.is_file():
                self.preview_response(file)
                return
        if path.startswith("/download/"):
            token = path.removeprefix("/download/")
            with LOCK:
                file = DOWNLOADS.get(token)
            if file and file.is_file():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{__import__('urllib.parse', fromlist=['quote']).quote(file.name)}")
                self.send_header("Content-Length", str(file.stat().st_size))
                self.end_headers()
                with file.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile)
                return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/convert", "/api/jobs"}:
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= MAX_UPLOAD:
            self.json_response(413, {"error": "파일 크기는 최대 1GB입니다."})
            return
        params = parse_qs(parsed.query)
        name = unquote(params.get("name", [""])[0])
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXT | VIDEO_EXT:
            self.json_response(400, {"error": "지원하지 않는 파일 형식입니다."})
            return
        requested = params.get("format", ["auto"])[0]
        quality = params.get("quality", ["high"])[0]
        async_job = parsed.path == "/api/jobs"
        handed_off = False
        try:
            with tempfile.NamedTemporaryFile(prefix="media_input_", suffix=ext, delete=False) as handle:
                temp = Path(handle.name)
                remaining = int(length)
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("파일 수신이 중단되었습니다.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            if async_job:
                job_id = uuid.uuid4().hex
                with LOCK:
                    JOBS[job_id] = {"state": "running", "stage": "파일 분석 중", "progress": 0}
                def worker() -> None:
                    def update(percent: int, stage: str) -> None:
                        with LOCK:
                            JOBS[job_id].update(progress=percent, stage=stage)
                    try:
                        result = convert(temp, name, requested, quality, update)
                        with LOCK:
                            JOBS[job_id].update(state="done", progress=100, stage="완료", result=result)
                    except Exception as exc:
                        with LOCK:
                            JOBS[job_id].update(state="error", stage="오류", error=str(exc))
                    finally:
                        temp.unlink(missing_ok=True)
                threading.Thread(target=worker, daemon=True).start()
                handed_off = True
                self.json_response(202, {"job_id": job_id})
            else:
                self.json_response(200, convert(temp, name, requested, quality))
        except (ValueError, subprocess.TimeoutExpired) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"처리 중 오류가 발생했습니다: {exc}"})
        finally:
            if "temp" in locals() and not handed_off:
                temp.unlink(missing_ok=True)


def main() -> None:
    for executable in (FFMPEG, FFPROBE):
        if not executable.is_file():
            sys.exit(f"변환 엔진을 찾지 못했습니다: {executable}")
    OUTPUT.mkdir(exist_ok=True)
    try:
        server = LocalHTTPServer(("127.0.0.1", 38927), Handler)
    except OSError:
        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"로컬 미디어 최적화 도구: {url}", flush=True)
    print(f"결과 폴더: {OUTPUT}", flush=True)
    if os.environ.get("PIXELKEEPER_NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
