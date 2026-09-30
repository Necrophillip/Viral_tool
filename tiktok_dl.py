#!/usr/bin/env python3
"""
TikTok Bulk Downloader  —  ⌘C to download, no questions asked.

• Detecta ⌘C via NSPasteboard.changeCount() (evento real de copia)
• Sin marcas de agua (API aweme de TikTok via yt-dlp)
• Máxima calidad: H.265 1080p cuando está disponible
• Sanitización anti-tracking: elimina toda metadata, re-encoda con micro-
  transformaciones para romper fingerprint visual, de audio y de contenedor
• Dashboard en tiempo real: queue, progreso, stats
• 8 workers concurrentes + 16 fragmentos paralelos por video
"""

import subprocess
import sys
import threading
import time
import re
import argparse
import os
import signal
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# ─── Clipboard para Windows / Mac / Linux ──────────────────────────────────────
try:
    import pyperclip
    HAS_PYPERCLIP = True
    def get_change_count() -> int:
        # Pyperclip no tiene change count, así que usamos el contenido como hash
        return hash(pyperclip.paste())
    def get_clipboard() -> str:
        return pyperclip.paste()
except ImportError:
    HAS_PYPERCLIP = False
    try:
        from AppKit import NSPasteboard
        _pb = NSPasteboard.generalPasteboard()
        def get_change_count() -> int:
            return _pb.changeCount()
        def get_clipboard() -> str:
            return _pb.stringForType_("public.utf8-plain-text") or ""
        HAS_APPKIT = True
    except ImportError:
        HAS_APPKIT = False
        def get_change_count() -> int:
            return 0
        def get_clipboard() -> str:
            try:
                if sys.platform == "win32":
                    return subprocess.check_output(["powershell.exe", "-command", "Get-Clipboard"], text=True).strip()
                return subprocess.check_output(["pbpaste"], text=True).strip()
            except Exception:
                return ""

# ─── Dependencias Externas (Cross-Platform) ──────────────────────────────────
import shutil
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# ─── ANSI ────────────────────────────────────────────────────────────────────
ESC   = "\033["
CLEAR = ESC + "2J" + ESC + "H"
CLRLN = ESC + "2K\r"
UP    = lambda n: ESC + f"{n}A"
HOME  = ESC + "H"
HIDE  = "\033[?25l"
SHOW  = "\033[?25h"

def C(code): return f"\033[{code}m"
RST   = C(0);  BOLD  = C(1);  DIM   = C(2)
G     = C(92); Y     = C(93); R     = C(91)
B     = C(94); M     = C(95); CY    = C(96); W = C(97)
BG_D  = C("48;5;235")   # fondo gris oscuro

# ─── Regex TikTok ─────────────────────────────────────────────────────────────
TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/"
    r"(?:@[\w.]+/video/\d+|v/\d+|t/\w+|[A-Za-z0-9_\-]{5,20}/?)",
    re.IGNORECASE,
)

# ─── Regex YouTube ────────────────────────────────────────────────────────────
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]{11}",
    re.IGNORECASE,
)

# ─── Estado de cada descarga ──────────────────────────────────────────────────
class Status(Enum):
    QUEUED      = "queued"
    RUNNING     = "running"
    SANITIZING  = "sanitizing"   # post-procesado anti-tracking
    OK          = "ok"
    ERROR       = "error"
    DUPLICATE   = "dup"

@dataclass
class DownloadItem:
    url:              str
    status:           Status = Status.QUEUED
    progress:         str    = ""
    sanitize_step:    str    = ""   # etapa de sanitización actual
    filename:         str    = ""
    err_msg:          str    = ""
    started:          float  = 0.0
    finished:         float  = 0.0

# ─── Estado global ────────────────────────────────────────────────────────────
items:     list[DownloadItem] = []
items_map: dict[str, DownloadItem] = {}
lock = threading.Lock()
stop_event = threading.Event()
TERM_W = os.get_terminal_size().columns if sys.stdout.isatty() else 100

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n-3] + "..."

# ─── Barra de progreso ─────────────────────────────────────────────────────────
SPIN = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
_spin_idx = 0

def spinner() -> str:
    global _spin_idx
    c = SPIN[_spin_idx % len(SPIN)]
    _spin_idx += 1
    return c

# ─── Dashboard ────────────────────────────────────────────────────────────────
_last_lines = 0

def render_dashboard(out_dir: Path) -> None:
    global _last_lines, TERM_W
    try:
        TERM_W = os.get_terminal_size().columns
    except Exception:
        pass
    W = TERM_W

    lines: list[str] = []

    def line(text=""):
        lines.append(text)

    # Header
    header = f"  {BOLD}{CY}TikTok Bulk Downloader{RST}  {DIM}⌘C para capturar · {ts()}{RST}"
    line(header)
    line(f"  {DIM}📁 {out_dir}  {'✅ AppKit' if HAS_APPKIT else '⚠ fallback'}{RST}")
    line(f"  {DIM}{'─' * (W - 4)}{RST}")

    # Stats
    with lock:
        all_items = list(items)

    ok_n   = sum(1 for i in all_items if i.status == Status.OK)
    err_n  = sum(1 for i in all_items if i.status == Status.ERROR)
    run_n  = sum(1 for i in all_items if i.status == Status.RUNNING)
    que_n  = sum(1 for i in all_items if i.status == Status.QUEUED)
    dup_n  = sum(1 for i in all_items if i.status == Status.DUPLICATE)

    line(
        f"  {G}✓ {ok_n} OK{RST}  "
        f"{B}⬇ {run_n} activos{RST}  "
        f"{Y}⏳ {que_n} en cola{RST}  "
        f"{R}✗ {err_n} errores{RST}  "
        f"{DIM}~ {dup_n} dup{RST}"
    )
    line(f"  {DIM}{'─' * (W - 4)}{RST}")

    # Items (últimos 20)
    visible = all_items[-20:]
    for item in visible:
        if item.status == Status.RUNNING:
            icon  = f"{B}{spinner()}{RST}"
            color = B
            prog  = f" {DIM}{trunc(item.progress, 30)}{RST}" if item.progress else ""
        elif item.status == Status.SANITIZING:
            icon  = f"{M}{spinner()}{RST}"
            color = M
            prog  = f" {DIM}🧹 {item.sanitize_step}{RST}" if item.sanitize_step else f" {DIM}🧹 sanitizando...{RST}"
        elif item.status == Status.OK:
            icon  = f"{G}✓{RST}"
            color = G
            prog  = f" {DIM}{item.filename}{RST}" if item.filename else ""
        elif item.status == Status.ERROR:
            icon  = f"{R}✗{RST}"
            color = R
            prog  = f" {R}{DIM}{trunc(item.err_msg, 40)}{RST}"
        elif item.status == Status.QUEUED:
            icon  = f"{Y}○{RST}"
            color = Y
            prog  = ""
        else:
            icon  = f"{DIM}~{RST}"
            color = DIM
            prog  = f" {DIM}duplicado{RST}"

        short_url = trunc(item.url, W - 20)
        line(f"  {icon} {color}{short_url}{RST}{prog}")


    if not visible:
        line(f"  {DIM}Esperando URLs... copia cualquier enlace de TikTok con ⌘C{RST}")

    line(f"  {DIM}{'─' * (W - 4)}{RST}")
    line(f"  {DIM}Ctrl+C para salir{RST}")

    # Render: subir N líneas y sobreescribir
    out_text = ""
    if _last_lines > 0:
        out_text += f"\033[{_last_lines}A"
    for l in lines:
        out_text += f"\r\033[2K{l}\n"

    sys.stdout.write(out_text)
    sys.stdout.flush()
    _last_lines = len(lines)


# ─── Anti-tracking sanitizer ──────────────────────────────────────────────────

def sanitize(item: DownloadItem, raw_path: Path) -> Path:
    """
    Elimina todo rastro de identidad de TikTok del video descargado.

    Capas de defensa:
    ──────────────────────────────────────────────────────────────────
    1. METADATA DEL CONTENEDOR
       -map_metadata -1          → borra TODOS los átomos de metadata (udta,
                                   title, artist, comment, description, etc.)
       -metadata encoder=""      → neutraliza encoder tag residual de yt-dlp/Lavf
       Nombres de handler neutros: "VideoHandler" → "" para romper ese vector

    2. THUMBNAIL EMBEBIDA (Stream 2: png)
       Se descarta con -map 0:v:0 -map 0:a:0 → solo streams A/V, sin PNG cover

    3. FINGERPRINT VISUAL (perceptual hash)
       crop=iw-4:ih-4:2:2       → recorta 2px en cada borde (imperceptible en
                                   1080p, pero cambia cada pixel del hash)
       eq=brightness=0.008       → micro ajuste de brillo (+0.8%)
       hue=s=1.005               → micro saturación (+0.5%)
       Juntos rompen MD5, dHash, pHash y similares sin afectar calidad percibida.

    4. FINGERPRINT DE AUDIO
       aresample=44100           → re-muestrea audio a 44100 Hz
       volume=1.003              → micro boost de volumen (+0.3 dB)
       Cambia la huella de audio que plataformas como ACRCloud / TikTok usan.

    5. RE-ENCODE COMPLETO
       libx264 CRF 18            → usa x264 para máxima compatibilidad de
                                   re-subida. (El original era H.265/bytevc1;
                                   cambiar codec rompe fingerprint binario total)
       aac 192k                  → re-encode de audio cierra el ciclo

    6. TIMESTAMPS
       -avoid_negative_ts make_zero  → normaliza timestamps
       -movflags +faststart          → buena práctica para MP4 streamable

    Salida: <out_dir>/clean_<id>.mp4   (nombre neutro sin "tiktok")
    ──────────────────────────────────────────────────────────────────
    """
    item.status = Status.SANITIZING
    clean_path = raw_path.parent / f"clean_{raw_path.stem.split('_')[-1]}.mp4"

    steps = [
        ("strip metadata",      True),
        ("romper hash visual",  True),
        ("romper hash audio",   True),
        ("re-encode",           True),
    ]

    item.sanitize_step = "preparando..."

    cmd = [
        FFMPEG, "-y",
        "-i", str(raw_path),
        # ── Solo streams A/V, descarta thumbnail embebida (stream 2: png) ──
        "-map", "0:v:0",
        "-map", "0:a:0",
        # ── VIDEO: re-encode con micro-transformaciones ──
        "-vf", (
            "crop=iw-4:ih-4:2:2,"          # 2px recorte por borde → rompe pHash
            "eq=brightness=0.008,"          # +0.8% brillo imperceptible
            "hue=s=1.005"                   # +0.5% saturación imperceptible
        ),
        "-c:v", "libx264",
        "-crf", "18",                       # alta calidad (visualmente lossless)
        "-preset", "fast",
        "-profile:v", "high",
        "-level", "4.1",
        # ── AUDIO: re-encode + resample ──
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", "aresample=44100,volume=1.003",   # resample + micro boost
        # ── METADATA: borrar TODO, incluyendo tags auto-escritos por ffmpeg ──
        "-map_metadata", "-1",              # borra metadata del contenedor
        "-map_metadata:s:v", "-1",          # borra metadata del stream de video
        "-map_metadata:s:a", "-1",          # borra metadata del stream de audio
        # Neutralizar handler_name (identifica VideoHandler / SoundHandler)
        "-metadata:s:v", "handler_name=",
        "-metadata:s:a", "handler_name=",
        "-metadata:s:v", "language=",
        "-metadata:s:a", "language=",
        "-fflags", "+bitexact",             # suprime encoder/date auto-inyectados
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        # ── TIMESTAMPS y flags ──
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(clean_path),
    ]

    item.sanitize_step = "re-encodando + limpiando metadata..."
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            # Fallback: si falla x264, intentar copy sin re-encode pero sí limpiar metadata
            item.sanitize_step = "fallback: solo strip metadata..."
            cmd_fallback = [
                FFMPEG, "-y",
                "-i", str(raw_path),
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "copy",
                "-map_metadata", "-1",
                "-movflags", "+faststart",
                str(clean_path),
            ]
            result2 = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=300)
            if result2.returncode != 0:
                raise RuntimeError(result.stderr[-200:])

        # ── Pase 2: limpiar stream tags residuales (handler_name, encoder, language)
        #    que libx264 reescribe en el encode. Stream copy = sin re-encode.
        item.sanitize_step = "limpiando stream tags residuales..."
        tmp_path = clean_path.with_suffix(".tmp.mp4")
        clean_path.rename(tmp_path)
        cmd_strip = [
            FFMPEG, "-y",
            "-i", str(tmp_path),
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c", "copy",
            "-map_metadata", "-1",
            "-map_metadata:s:v", "-1",
            "-map_metadata:s:a", "-1",
            "-metadata:s:v", "handler_name=",
            "-metadata:s:a", "handler_name=",
            "-movflags", "+faststart",
            str(clean_path),
        ]
        result3 = subprocess.run(cmd_strip, capture_output=True, text=True, timeout=120)
        tmp_path.unlink(missing_ok=True)
        if result3.returncode != 0:
            # Si falla el strip, dejar el archivo del pase 1 (ya sanitizado parcialmente)
            clean_path.write_bytes(tmp_path.read_bytes()) if tmp_path.exists() else None

        # ── Pase 3: exiftool — borra todos los tags escribibles restantes ──
        #    (CreateDate, ModifyDate, encoder tags, XMP, etc.)
        item.sanitize_step = "exiftool: borrando tags restantes..."
        exiftool_path = shutil.which("exiftool")
        if exiftool_path:
            subprocess.run(
                [exiftool_path, "-all=", "-overwrite_original", str(clean_path)],
                capture_output=True, text=True, timeout=30,
            )

        # Eliminar el archivo raw (sucio)
        raw_path.unlink(missing_ok=True)
        item.sanitize_step = ""
        return clean_path

    except subprocess.TimeoutExpired:
        raise RuntimeError("sanitize timeout")



# ─── Descarga individual ──────────────────────────────────────────────────────
def download(item: DownloadItem, out_dir: Path) -> None:
    item.status  = Status.RUNNING
    item.started = time.time()

    output_tpl = str(out_dir / "%(uploader)s_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com",
        "-f", (
            "bestvideo[format_id!=download][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[format_id!=download]+bestaudio"
            "/best[format_id!=download]"
        ),
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "16",
        "--retries", "5",
        "--fragment-retries", "10",
        "--no-warnings",
        "--newline",
        "--progress",
        "--ffmpeg-location", FFMPEG,
        # No embebemos metadata/thumbnail aquí — sanitize() la maneja
        "--no-embed-metadata",
        "--print", "after_move:%(filename)s",
        "-o", output_tpl,
        item.url,
    ]

    raw_path: Path | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        last_filename = ""
        for raw_line in proc.stdout:
            ln = raw_line.strip()
            if not ln:
                continue
            if "[download]" in ln and "%" in ln:
                item.progress = ln.replace("[download]", "").strip()
            elif ln.endswith(".mp4") or ln.endswith(".mkv"):
                last_filename = ln
        proc.wait(timeout=300)

        if proc.returncode != 0:
            item.status  = Status.ERROR
            item.err_msg = f"yt-dlp exit {proc.returncode}"
            return

        if not last_filename:
            # Buscar el archivo más reciente en out_dir como fallback
            mp4s = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            last_filename = str(mp4s[-1]) if mp4s else ""

        raw_path = Path(last_filename) if last_filename else None

        # ── Post-proceso: sanitización anti-tracking ──
        if raw_path and raw_path.exists():
            item.progress = ""
            clean = sanitize(item, raw_path)
            item.status   = Status.OK
            item.filename = clean.name
        else:
            item.status   = Status.OK
            item.filename = Path(last_filename).name if last_filename else "?"

    except subprocess.TimeoutExpired:
        if raw_path and raw_path.exists():
            raw_path.unlink(missing_ok=True)
        item.status  = Status.ERROR
        item.err_msg = "timeout"
    except RuntimeError as e:
        item.status  = Status.ERROR
        item.err_msg = f"sanitize: {str(e)[:50]}"
    except Exception as e:
        item.status  = Status.ERROR
        item.err_msg = str(e)[:60]

    item.finished = time.time()



# ─── YouTube: Sanitizer 9:16 ─────────────────────────────────────────────────
def sanitize_youtube_916(item: DownloadItem, raw_path: Path) -> Path:
    """
    Convierte el video a formato vertical 9:16 (1080×1920) listo para
    TikTok/Shorts y aplica los mismos filtros anti-tracking de sanitize().

    Pipeline de video:
      1. crop=ih*9/16:ih:(iw-ih*9/16)/2:0   → recorta centrado a aspecto 9:16
      2. scale=1080:1920                       → escala exactamente a 1080×1920
      3. crop=iw-4:ih-4:2:2                   → micro-recorte anti-pHash
      4. eq=brightness=0.008                  → micro brillo
      5. hue=s=1.005                          → micro saturación
    """
    item.status = Status.SANITIZING
    clean_path = raw_path.parent / f"clean_916_{raw_path.stem.split('_')[-1]}.mp4"

    vf = (
        "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"   # recorte 9:16 centrado
        "scale=1080:1920,"                      # escala a 1080×1920
        "crop=iw-4:ih-4:2:2,"                  # anti-pHash
        "eq=brightness=0.008,"                  # micro brillo
        "hue=s=1.005"                           # micro saturación
    )

    cmd = [
        FFMPEG, "-y",
        "-i", str(raw_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k",
        "-af", "aresample=44100,volume=1.003",
        "-map_metadata", "-1",
        "-map_metadata:s:v", "-1",
        "-map_metadata:s:a", "-1",
        "-metadata:s:v", "handler_name=",
        "-metadata:s:a", "handler_name=",
        "-metadata:s:v", "language=",
        "-metadata:s:a", "language=",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(clean_path),
    ]

    item.sanitize_step = "re-encodando 9:16 + anti-tracking..."
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-200:])
        raw_path.unlink(missing_ok=True)
        item.sanitize_step = ""
        return clean_path
    except subprocess.TimeoutExpired:
        raise RuntimeError("sanitize_916 timeout")


# ─── YouTube: Descarga individual ────────────────────────────────────────────
def download_youtube(item: DownloadItem, out_dir: Path, fmt: str) -> None:
    """
    Descarga un video de YouTube y aplica sanitización anti-tracking.

    fmt = "native"  → máxima calidad original + sanitize() estándar
    fmt = "916"     → ídem descarga + crop/scale 9:16 + sanitize_youtube_916()
    """
    item.status  = Status.RUNNING
    item.started = time.time()

    output_tpl = str(out_dir / "%(uploader)s_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "16",
        "--retries", "5",
        "--fragment-retries", "10",
        "--no-warnings",
        "--newline",
        "--progress",
        "--ffmpeg-location", FFMPEG,
        "--no-embed-metadata",
        "--print", "after_move:%(filename)s",
        "-o", output_tpl,
        item.url,
    ]

    raw_path: Path | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        last_filename = ""
        for raw_line in proc.stdout:
            ln = raw_line.strip()
            if not ln:
                continue
            if "[download]" in ln and "%" in ln:
                item.progress = ln.replace("[download]", "").strip()
            elif ln.endswith(".mp4") or ln.endswith(".mkv") or ln.endswith(".webm"):
                last_filename = ln
        proc.wait(timeout=600)

        if proc.returncode != 0:
            item.status  = Status.ERROR
            item.err_msg = f"yt-dlp exit {proc.returncode}"
            return

        if not last_filename:
            mp4s = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            last_filename = str(mp4s[-1]) if mp4s else ""

        raw_path = Path(last_filename) if last_filename else None

        if raw_path and raw_path.exists():
            item.progress = ""
            if fmt == "916":
                clean = sanitize_youtube_916(item, raw_path)
            else:
                clean = sanitize(item, raw_path)
            item.status   = Status.OK
            item.filename = clean.name
        else:
            item.status   = Status.OK
            item.filename = Path(last_filename).name if last_filename else "?"

    except subprocess.TimeoutExpired:
        if raw_path and raw_path.exists():
            raw_path.unlink(missing_ok=True)
        item.status  = Status.ERROR
        item.err_msg = "timeout"
    except RuntimeError as e:
        item.status  = Status.ERROR
        item.err_msg = f"sanitize: {str(e)[:50]}"
    except Exception as e:
        item.status  = Status.ERROR
        item.err_msg = str(e)[:60]

    item.finished = time.time()


# ─── Monitor de clipboard ─────────────────────────────────────────────────────
def monitor_clipboard(out_dir: Path, workers: int) -> None:
    executor   = ThreadPoolExecutor(max_workers=workers)
    prev_count = get_change_count()
    prev_text  = get_clipboard()

    while not stop_event.is_set():
        count = get_change_count()
        if count != prev_count:
            prev_count = count
            text = get_clipboard()
            if text and text != prev_text:
                prev_text = text
                urls = list(dict.fromkeys(TIKTOK_RE.findall(text)))
                for url in urls:
                    with lock:
                        if url in items_map:
                            dup = DownloadItem(url=url, status=Status.DUPLICATE)
                            items.append(dup)
                            continue
                        item = DownloadItem(url=url)
                        items.append(item)
                        items_map[url] = item
                    executor.submit(download, item, out_dir)
        time.sleep(0.2)

    executor.shutdown(wait=True, cancel_futures=False)


# ─── Dashboard loop ───────────────────────────────────────────────────────────
def dashboard_loop(out_dir: Path) -> None:
    while not stop_event.is_set():
        render_dashboard(out_dir)
        time.sleep(0.3)
    # Render final
    render_dashboard(out_dir)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="TikTok bulk downloader — ⌘C y descarga sola",
    )
    parser.add_argument("--out", "-o",
        default=str(Path.home() / "Downloads" / "TikTok"),
        help="Directorio de salida (default: ~/Downloads/TikTok)",
    )
    parser.add_argument("--workers", "-w", type=int, default=8,
        help="Descargas concurrentes (default: 8)",
    )
    parser.add_argument("--url", "-u", nargs="*",
        help="URLs directas (sin monitor de clipboard)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Verificar yt-dlp
    if not shutil.which("yt-dlp"):
        print(f"{R}Error: yt-dlp no encontrado. Instala con: pip install yt-dlp{RST}")
        sys.exit(1)

    # Ctrl+C limpio
    def _on_sigint(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)

    # Menú interactivo si no hay argumentos de url
    mode_urls = []
    use_clipboard = False
    youtube_mode  = False
    yt_fmt        = "native"

    if args.url:
        mode_urls = args.url
    else:
        print(CLEAR, end="")
        print(f"  {BOLD}{CY}TikTok / YouTube Downloader Pro{RST}\n")
        print(f"  {G}1){RST} Modo Portapapeles (Monitoreo automático via ⌘C · TikTok)")
        print(f"  {G}2){RST} Modo Cuenta (Descargar videos de un perfil TikTok)")
        print(f"  {G}3){RST} YouTube (Descarga individual con opción de formato)")
        print(f"  {G}4){RST} Salir\n")

        choice = input("  Seleccione un modo: ").strip()

        if choice == "1":
            use_clipboard = True
        elif choice == "2":
            profile_url = input(f"\n  {B}Ingrese el link del perfil (ej. https://www.tiktok.com/@usuario): {RST}").strip()
            # Limpiar URL de parámetros de rastreo (?_t=...)
            if "?" in profile_url:
                profile_url = profile_url.split("?")[0]

            count_str = input(f"  {B}¿Cuántos videos descargar? (Dejar en blanco para todos): {RST}").strip()

            print(f"\n  {DIM}Obteniendo enlaces... esto puede tardar unos segundos...{RST}")
            cmd = [
                "yt-dlp",
                "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com",
                "--flat-playlist",
                "--print", "webpage_url"
            ]
            if count_str.isdigit() and int(count_str) > 0:
                cmd.extend(["--playlist-end", count_str])
            cmd.append(profile_url)

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                mode_urls = [u.strip() for u in result.stdout.split('\n') if u.strip()]
                print(f"  {G}¡Se encontraron {len(mode_urls)} videos para descargar!{RST}\n")
                time.sleep(1)
            except subprocess.CalledProcessError as e:
                print(f"{R}Error extrayendo videos del perfil: {e.stderr}{RST}")
                sys.exit(1)
        elif choice == "3":
            # ── Sub-menú YouTube ──────────────────────────────────────────────
            print(f"\n  {BOLD}{CY}YouTube Downloader{RST}")
            print(f"  {DIM}{'─' * 44}{RST}")
            yt_url = input(f"\n  {B}URL de YouTube: {RST}").strip()
            # Conservar ?v= pero eliminar otros parámetros de rastreo si no hay v=
            if "?" in yt_url and "v=" not in yt_url:
                yt_url = yt_url.split("?")[0]

            print(f"\n  {BOLD}Formato de salida:{RST}")
            print(f"  {G}1){RST} Nativo  (máxima calidad original, ej. 1080p/4K)")
            print(f"  {G}2){RST} 9:16    (recorte vertical 1080×1920, Shorts/TikTok-ready)\n")
            fmt_choice = input("  Seleccione formato: ").strip()

            yt_fmt       = "916" if fmt_choice == "2" else "native"
            youtube_mode = True
            mode_urls    = [yt_url]
        else:
            sys.exit(0)

    # Ocultar cursor
    sys.stdout.write(HIDE)
    sys.stdout.flush()

    # Limpiar pantalla para el dashboard
    print(CLEAR, end="")

    if mode_urls:
        # Modo directo o cuenta — carga URLs y arranca monitor dashboard
        for url in mode_urls:
            item = DownloadItem(url=url)
            with lock:
                items.append(item)
                items_map[url] = item

        executor = ThreadPoolExecutor(max_workers=args.workers)
        for item in items:
            if youtube_mode:
                executor.submit(download_youtube, item, out_dir, yt_fmt)
            else:
                executor.submit(download, item, out_dir)

        # Dashboard mientras descargan
        t_dash = threading.Thread(target=dashboard_loop, args=(out_dir,), daemon=True)
        t_dash.start()

        # Esperar a que terminen
        try:
            while not stop_event.is_set():
                with lock:
                    pending = any(i.status in (Status.QUEUED, Status.RUNNING, Status.SANITIZING) for i in items)
                if not pending:
                    break
                time.sleep(0.5)
        except Exception:
            pass

        stop_event.set()
        executor.shutdown(wait=False)
        t_dash.join(timeout=1)

    elif use_clipboard:
        # Modo monitor clipboard
        if not HAS_APPKIT:
            print(f"{Y}⚠  AppKit no disponible — usando fallback (detección por contenido){RST}\n")

        # Dashboard en hilo separado
        t_dash = threading.Thread(target=dashboard_loop, args=(out_dir,), daemon=True)
        t_dash.start()

        # Monitor de clipboard en hilo separado
        t_clip = threading.Thread(
            target=monitor_clipboard, args=(out_dir, args.workers), daemon=False
        )
        t_clip.start()
        t_clip.join()

        stop_event.set()
        t_dash.join(timeout=1)

    # Restaurar cursor
    sys.stdout.write(SHOW + "\n")
    sys.stdout.flush()

    with lock:
        ok_n  = sum(1 for i in items if i.status == Status.OK)
        err_n = sum(1 for i in items if i.status == Status.ERROR)
        dup_n = sum(1 for i in items if i.status == Status.DUPLICATE)

    print(
        f"\n{BOLD}Sesión terminada.{RST}  "
        f"{G}✓ {ok_n} descargados{RST}  "
        f"{R}✗ {err_n} errores{RST}  "
        f"{DIM}~ {dup_n} duplicados ignorados{RST}"
    )
    print(f"{DIM}Archivos en: {out_dir}{RST}\n")


if __name__ == "__main__":
    main()
