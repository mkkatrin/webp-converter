#!/usr/bin/env python3
"""
Конвертер — картинки и видео в WebP, сжатие PDF до нужного размера. Окно в браузере.

Запуск:  python3 webp_app.py            (или значок «Конвертер» на macOS)
         python3 webp_app.py --install  (создать приложение «Конвертер»)
Откроется страница в браузере: перетащите туда картинки или видео.
Готовые файлы сохраняются в папку «Загрузки/Конвертер».

Нужно: Pillow, pikepdf (ставится сам), для видео — ffmpeg.
Необязательно: pillow-heif — для фото iPhone в формате HEIC.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "1.2"
REPO_RAW = os.environ.get("WEBP_REPO", "https://raw.githubusercontent.com/mkkatrin/webp-converter/main/")
APP_FILE = Path(__file__).resolve()

OUT_DIR = Path.home() / "Downloads" / "Конвертер"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".gif"}

try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except ImportError:
    pass


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(c):
            return c
    return None


def convert_image(src, dst, quality, lossless, width):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("Не установлен Pillow (pip install pillow)")
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if width and im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
        im.save(dst, "WEBP", quality=quality, lossless=lossless, method=6)


def convert_video(src, dst, quality, lossless, width, fps, is_gif):
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg не найден — установите: brew install ffmpeg")
    filters = [] if is_gif else [f"fps={fps}"]
    if width:
        filters.append(f"scale='min({width},iw)':-2:flags=lanczos")
    cmd = [ff, "-y", "-loglevel", "error", "-i", str(src)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-an", "-c:v", "libwebp", "-loop", "0", "-compression_level", "6"]
    cmd += ["-lossless", "1"] if lossless else ["-q:v", str(quality)]
    cmd.append(str(dst))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or "ошибка ffmpeg")


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


# ---------- Сжатие PDF ----------
# Уровни: (макс. dpi картинок, качество JPEG, подпись). 0 — без потерь.
PDF_LEVELS = [
    (None, None, "без потерь"),
    (200, 85, "почти без потерь"),
    (150, 75, "хорошее качество"),
    (110, 65, "заметное сжатие"),
    (85, 55, "сильное сжатие"),
    (72, 40, "очень сильное сжатие"),
]
PDF_STRONG = 4          # с этого уровня считаем потерю качества сильной
PDF_GOOD = 2            # уровень, который стараемся сохранить при разделении
PDF_STATE = {"status": "checking", "error": ""}
JOBS = {}
WORK_DIR = Path(tempfile.mkdtemp(prefix="converter-"))


def ensure_pdf_lib():
    """Ставит pikepdf в окружение приложения, если его ещё нет (один раз)."""
    try:
        import pikepdf  # noqa: F401
        PDF_STATE["status"] = "ready"
        return
    except ImportError:
        pass
    PDF_STATE["status"] = "installing"
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "pikepdf"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        import importlib
        importlib.invalidate_caches()
        PDF_STATE["status"] = "ready"
    else:
        PDF_STATE["status"] = "error"
        PDF_STATE["error"] = (r.stderr or r.stdout).strip()[-300:]


def wait_pdf_lib(timeout=600):
    t0 = time.time()
    while PDF_STATE["status"] in ("checking", "installing") and time.time() - t0 < timeout:
        time.sleep(0.5)
    if PDF_STATE["status"] != "ready":
        raise RuntimeError("модуль для PDF не установился. Проверьте интернет и перезапустите Конвертер. "
                           + PDF_STATE.get("error", ""))


def _image_weights(pdf):
    """Для каждой картинки — самая большая страница, на которой она стоит (в дюймах),
    и «вес» каждой страницы (сколько байт картинок на ней) для деления на части."""
    import pikepdf
    pages_in = {}
    weights = []
    for page in pdf.pages:
        box = page.mediabox
        w_in = abs(float(box[2]) - float(box[0])) / 72
        h_in = abs(float(box[3]) - float(box[1])) / 72
        side = max(w_in, h_in) or 11.7
        wsum = 0
        try:
            imgs = page.images
        except Exception:
            imgs = {}
        for _, raw in imgs.items():
            if not isinstance(raw, pikepdf.Stream):
                continue
            key = raw.objgen
            pages_in[key] = max(pages_in.get(key, 0), side)
            try:
                wsum += int(raw.get("/Length", 0))
            except Exception:
                pass
        weights.append(wsum + 5000)
    return pages_in, weights


def _recompress_images(pdf, max_dpi, quality):
    import io
    import pikepdf
    from pikepdf import Name, PdfImage
    from PIL import Image
    pages_in, _ = _image_weights(pdf)
    done = set()
    for page in pdf.pages:
        try:
            imgs = page.images
        except Exception:
            continue
        for _, raw in imgs.items():
            if not isinstance(raw, pikepdf.Stream) or raw.objgen in done:
                continue
            done.add(raw.objgen)
            try:
                if raw.get("/ImageMask", False) or int(raw.get("/BitsPerComponent", 8)) < 8:
                    continue  # чёрно-белые маски и сканы 1-бит не трогаем
                old_len = int(raw.get("/Length", 0))
                if old_len < 15000:
                    continue
                pil = PdfImage(raw).as_pil_image()
                if pil.mode in ("L", "LA", "I", "I;16", "1"):
                    pil, cs = pil.convert("L"), Name.DeviceGray
                else:
                    pil, cs = pil.convert("RGB"), Name.DeviceRGB
                side_in = pages_in.get(raw.objgen, 11.7)
                max_px = int(max_dpi * side_in)
                if max(pil.size) > max_px:
                    k = max_px / max(pil.size)
                    pil = pil.resize((max(1, round(pil.width * k)), max(1, round(pil.height * k))), Image.LANCZOS)
                buf = io.BytesIO()
                pil.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
                data = buf.getvalue()
                if len(data) >= old_len * 0.95:
                    continue  # не стало меньше — оставляем как было
                raw.write(data, filter=Name.DCTDecode)
                raw.Width, raw.Height = pil.width, pil.height
                raw.ColorSpace = cs
                raw.BitsPerComponent = 8
                for k in ("/DecodeParms", "/Decode"):
                    if k in raw:
                        del raw[k]
                if "/Mask" in raw and isinstance(raw.Mask, pikepdf.Array):
                    del raw["/Mask"]
            except Exception:
                continue  # необычную картинку оставляем как есть


def pdf_at_level(src, dst, level, pages=None):
    """Сохраняет src (или только страницы pages) в dst со сжатием уровня level."""
    import pikepdf
    with pikepdf.open(src) as pdf:
        if pages is not None:
            keep = set(pages)
            for i in range(len(pdf.pages) - 1, -1, -1):
                if i not in keep:
                    del pdf.pages[i]
        dpi, q, _ = PDF_LEVELS[level]
        if dpi:
            _recompress_images(pdf, dpi, q)
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        pdf.save(dst, compress_streams=True, recompress_flate=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return Path(dst).stat().st_size


def pdf_fit(src, dst, target, pages=None, start=0):
    """Подбирает самый мягкий уровень, при котором файл влезает в target.
    Возвращает (уровень, размер, {уровень: размер})."""
    sizes = {}
    for lvl in range(start, len(PDF_LEVELS)):
        tmp = Path(str(dst) + f".l{lvl}")
        sizes[lvl] = pdf_at_level(src, tmp, lvl, pages)
        if sizes[lvl] <= target or lvl == len(PDF_LEVELS) - 1:
            os.replace(tmp, dst)
            for other in Path(dst).parent.glob(Path(dst).name + ".l*"):
                other.unlink()
            return lvl, sizes[lvl], sizes
    raise RuntimeError("не удалось сжать")


def split_pages(weights, parts):
    """Делит страницы на parts кусков подряд, примерно равных по «весу»."""
    n = len(weights)
    parts = max(1, min(parts, n))
    total = sum(weights)
    groups, cur, acc, left = [], [], 0, parts
    for i, w in enumerate(weights):
        cur.append(i)
        acc += w
        remaining_pages = n - i - 1
        if left > 1 and (acc >= total / parts * (len(groups) + 1) - w / 2 or remaining_pages == left - 1):
            groups.append(cur)
            cur, left = [], left - 1
    if cur:
        groups.append(cur)
    return groups


def pdf_compress_job(src, name, target_mb):
    import pikepdf
    target = int(target_mb * 1024 * 1024)
    in_size = Path(src).stat().st_size
    try:
        with pikepdf.open(src) as pdf:
            n_pages = len(pdf.pages)
            _, weights = _image_weights(pdf)
    except pikepdf.PasswordError:
        raise RuntimeError("PDF защищён паролем — снимите пароль и попробуйте снова")
    jid = os.urandom(6).hex()
    res = WORK_DIR / f"{jid}.pdf"
    lvl, size, sizes = pdf_fit(src, res, target)
    job = {"src": str(src), "name": name, "res": str(res), "target": target,
           "weights": weights, "pages": n_pages, "level": lvl, "size": size}
    JOBS[jid] = job
    fits = size <= target
    strong = lvl >= PDF_STRONG or not fits
    parts = 0
    if strong and n_pages > 1:
        # сколько частей нужно, чтобы остаться на «хорошем качестве»
        good_size = sizes.get(PDF_GOOD) or size
        parts = max(2, -(-int(good_size * 1.08) // target))
        parts = min(parts, n_pages, 10)
    out = {"ok": True, "kind": "pdf", "id": jid, "in": in_size, "out": size, "pages": n_pages,
           "level": PDF_LEVELS[lvl][2], "level_n": lvl, "fits": fits, "target_mb": target_mb,
           "choice": strong, "parts": parts, "dpi": PDF_LEVELS[lvl][0]}
    if not strong:
        out.update(pdf_keep(jid))
    return out


def _out_name(stem, suffix):
    return unique(OUT_DIR / f"{stem}{suffix}")


def pdf_keep(jid):
    job = JOBS[jid]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = _out_name(Path(job["name"]).stem, " (сжатый).pdf")
    shutil.copy2(job["res"], dst)
    return {"saved": [{"name": dst.name, "url": "/out/" + quote(dst.name), "size": dst.stat().st_size,
                       "level": PDF_LEVELS[job["level"]][2]}]}


def pdf_split(jid, parts):
    job = JOBS[jid]
    groups = split_pages(job["weights"], max(2, parts))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(job["name"]).stem
    saved = []
    for i, pages in enumerate(groups, 1):
        tmp = WORK_DIR / f"{jid}-p{i}.pdf"
        lvl, size, _ = pdf_fit(job["src"], tmp, job["target"], pages=pages)
        dst = _out_name(stem, f" — часть {i} из {len(groups)}.pdf")
        shutil.move(str(tmp), dst)
        saved.append({"name": dst.name, "url": "/out/" + quote(dst.name), "size": size,
                      "level": PDF_LEVELS[lvl][2], "pages": f"{pages[0] + 1}–{pages[-1] + 1}",
                      "fits": size <= job["target"]})
    return {"saved": saved}


# ---------- Приложение «Конвертер» для macOS (запуск без Терминала) ----------
APP_NAME = "Конвертер"
BUNDLE_VERSION = "2"
BUNDLE = Path.home() / "Applications" / f"{APP_NAME}.app"
DESKTOP = Path.home() / "Desktop"

RUN_SCRIPT = """#!/bin/zsh
# Если конвертер уже запущен — просто открываем страницу
if curl -fs --max-time 1 http://127.0.0.1:8765/status >/dev/null 2>&1; then
  open "http://127.0.0.1:8765/"; exit 0
fi
cd "$HOME/webp-tool" || exit 1
exec .venv/bin/python webp_app.py --app >> "$HOME/webp-tool/log.txt" 2>&1
"""

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{name}</string>
  <key>CFBundleDisplayName</key><string>{name}</string>
  <key>CFBundleIdentifier</key><string>com.mkkatrin.converter</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{ver}</string>
  <key>CFBundleVersion</key><string>{bver}</string>
  <key>LSUIElement</key><true/>
</dict></plist>
"""


def make_icon(path: Path):
    """Рисует иконку приложения и сохраняет в .icns."""
    from PIL import Image, ImageDraw
    S = 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):  # градиент сверху вниз: голубой → синий
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(64 + (10 - 64) * t), int(170 + (90 - 170) * t), 255, 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=185, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # «фотография»: рамка, солнце и горы
    d.rounded_rectangle([250, 290, 774, 734], radius=60, outline=(255, 255, 255, 255), width=44)
    d.ellipse([350, 370, 450, 470], fill=(255, 255, 255, 255))
    d.polygon([(300, 690), (470, 500), (580, 610), (640, 550), (730, 690)], fill=(255, 255, 255, 255))
    img.save(path, sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])


def install_bundle(force=False):
    """Создаёт ~/Applications/Конвертер.app и значок на Рабочем столе.
    Возвращает True, если приложение было создано или обновлено сейчас."""
    if sys.platform != "darwin":
        return False
    plist = BUNDLE / "Contents" / "Info.plist"
    if not force and plist.exists() and f"<string>{BUNDLE_VERSION}</string>" in plist.read_text("utf-8"):
        return False
    macos = BUNDLE / "Contents" / "MacOS"
    res = BUNDLE / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    run = macos / "run"
    run.write_text(RUN_SCRIPT, "utf-8")
    run.chmod(0o755)
    plist.write_text(INFO_PLIST.format(name=APP_NAME, ver=VERSION, bver=BUNDLE_VERSION), "utf-8")
    try:
        make_icon(res / "icon.icns")
    except Exception as e:
        print("Иконка не создана:", e)
    lsreg = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
             "LaunchServices.framework/Support/lsregister")
    for cmd in (["xattr", "-cr", str(BUNDLE)], ["touch", str(BUNDLE)], [lsreg, "-f", str(BUNDLE)]):
        try:
            subprocess.run(cmd, capture_output=True)
        except OSError:
            pass
    # Значок на Рабочем столе вместо старого WebP.command
    try:
        old = DESKTOP / "WebP.command"
        if old.is_file() and "webp-tool" in old.read_text("utf-8", "ignore"):
            old.unlink()
        link = DESKTOP / APP_NAME
        if not link.exists() and not link.is_symlink():
            link.symlink_to(BUNDLE)
    except Exception as e:
        print("Значок на Рабочем столе не создан:", e)
    return True


# ---------- Автовыключение, когда вкладка закрыта ----------
LAST_SEEN = [0.0]
MIGRATED = [False]
IDLE_LIMIT = 180  # секунд без связи со страницей


def watchdog(srv):
    import time
    while True:
        time.sleep(10)
        if LAST_SEEN[0] and time.time() - LAST_SEEN[0] > IDLE_LIMIT:
            srv.shutdown()
            return


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def fetch(name, timeout=6):
    """Скачать файл из репозитория (через curl — он есть на любом Mac)."""
    r = subprocess.run(["curl", "-fsSL", "--max-time", str(timeout), REPO_RAW + name],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("нет связи с сервером обновлений")
    return r.stdout


def check_update():
    try:
        info = json.loads(fetch("version.json", 5).decode("utf-8"))
    except Exception:
        return {"available": False, "current": VERSION}
    latest = str(info.get("version", "0"))
    return {"available": vtuple(latest) > vtuple(VERSION), "current": VERSION,
            "latest": latest, "notes": info.get("notes", [])}


def apply_update():
    new = fetch("webp_app.py", 30)
    tmp = APP_FILE.with_suffix(".new")
    tmp.write_bytes(new)
    try:
        compile(new, str(tmp), "exec")  # проверка, что файл целый
    except SyntaxError:
        tmp.unlink()
        raise RuntimeError("скачанный файл повреждён, попробуйте позже")
    if b"VERSION" not in new:
        tmp.unlink()
        raise RuntimeError("неожиданный файл обновления")
    backup = APP_FILE.with_suffix(".py.bak")
    shutil.copy2(APP_FILE, backup)
    os.replace(tmp, APP_FILE)


def restart(port):
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    env = dict(os.environ, WEBP_NO_BROWSER="1", WEBP_PORT=str(port))
    os.execve(sys.executable, [sys.executable, str(APP_FILE)], env)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        LAST_SEEN[0] = time.time()
        u = urlparse(self.path)
        if u.path == "/ping":
            self._json({"ok": True})
        elif u.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/status":
            self._json({"ffmpeg": bool(find_ffmpeg()), "out": str(OUT_DIR), "version": VERSION,
                        "migrated": MIGRATED[0], "app": sys.platform == "darwin",
                        "pdf": PDF_STATE["status"]})
        elif u.path.startswith("/pdf/preview/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            if job and Path(job["res"]).is_file():
                self._send(200, Path(job["res"]).read_bytes(), "application/pdf")
            else:
                self._send(404, b"not found", "text/plain")
        elif u.path == "/update/check":
            self._json(check_update())
        elif u.path.startswith("/out/"):
            f = OUT_DIR / Path(unquote(u.path[5:])).name
            if f.is_file():
                ctype = "application/pdf" if f.suffix.lower() == ".pdf" else "image/webp"
                self._send(200, f.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        LAST_SEEN[0] = time.time()
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def g(k, d=""):
            return q.get(k, [d])[0]

        length = int(self.headers.get("Content-Length", 0) or 0)

        if u.path == "/update/apply":
            try:
                apply_update()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            self._json({"ok": True})
            port = self.server.server_address[1]
            threading.Timer(0.5, restart, args=(port,)).start()
            return

        if u.path == "/pdf/compress":
            name = Path(g("name", "file.pdf")).name
            try:
                target_mb = max(0.5, float(g("target", "20") or 20))
            except ValueError:
                target_mb = 20.0
            src = WORK_DIR / (os.urandom(6).hex() + ".src.pdf")
            with open(src, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            try:
                wait_pdf_lib()
                return self._json(pdf_compress_job(src, name, target_mb))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path in ("/pdf/keep", "/pdf/split"):
            jid = g("id")
            if jid not in JOBS:
                return self._json({"ok": False, "error": "задача не найдена — загрузите PDF ещё раз"})
            try:
                if u.path == "/pdf/keep":
                    r = pdf_keep(jid)
                else:
                    r = pdf_split(jid, int(g("parts", "2") or 2))
                return self._json(dict(ok=True, **r))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if u.path == "/open":
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
            subprocess.Popen([opener, str(OUT_DIR)])
            return self._json({"ok": True})

        if u.path != "/convert":
            return self._send(404, b"not found", "text/plain")

        name = Path(g("name", "file")).name
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXT | VIDEO_EXT:
            self.rfile.read(length)
            return self._json({"ok": False, "error": "Этот формат не поддерживается"})

        quality = max(0, min(100, int(g("q", "80") or 80)))
        lossless = g("lossless") == "1"
        width = int(g("w", "0") or 0) or None
        fps = float(g("fps", "15") or 15)

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / ("src" + ext)
            with open(src, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            dst = unique(OUT_DIR / (Path(name).stem + ".webp"))
            try:
                if ext in VIDEO_EXT:
                    convert_video(src, dst, quality, lossless, width, fps, ext == ".gif")
                else:
                    convert_image(src, dst, quality, lossless, width)
            except Exception as e:
                if dst.exists():
                    dst.unlink()
                return self._json({"ok": False, "error": str(e)})

        self._json({"ok": True, "name": dst.name, "in": length,
                    "out": dst.stat().st_size, "url": "/out/" + quote(dst.name)})


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Конвертер</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--accent:#0a7cff;--ok:#1f9d55;--err:#d93025;--drop:#eef5ff}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#222225;--text:#f2f2f4;--muted:#9a9aa0;--line:#333338;--accent:#4c9dff;--ok:#3ccf7a;--err:#ff6b5e;--drop:#1c2633}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif}
.wrap{max-width:780px;margin:0 auto;padding:32px 16px 48px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 22px}
.drop{border:2px dashed var(--line);border-radius:18px;background:var(--card);padding:44px 20px;text-align:center;cursor:pointer;transition:.15s}
.drop.over{border-color:var(--accent);background:var(--drop)}
.drop .big{font-size:18px;font-weight:600}
.drop .small{color:var(--muted);margin-top:6px}
.drop svg{width:44px;height:44px;color:var(--accent);margin-bottom:10px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-top:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px 20px;align-items:end}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input[type=range]{width:100%;accent-color:var(--accent)}
input[type=number],select{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--text);font:inherit}
.chk{display:flex;gap:8px;align-items:center;color:var(--text);font-size:15px;margin:0;padding-bottom:8px}
.chk input{width:18px;height:18px;accent-color:var(--accent)}
.warn{display:none;margin-top:14px;padding:10px 14px;border-radius:12px;background:rgba(217,48,37,.1);color:var(--err);font-size:14px}
.list{margin-top:18px;display:flex;flex-direction:column;gap:8px}
.item{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px}
.thumb{width:56px;height:56px;border-radius:10px;background:var(--bg);flex:none;object-fit:cover}
.info{flex:1;min-width:0}
.name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{font-size:13px;color:var(--muted)}
.meta .good{color:var(--ok);font-weight:600}
.meta .bad{color:var(--err)}
.dl{flex:none;color:var(--accent);text-decoration:none;font-weight:600;font-size:14px;padding:6px 10px;border-radius:8px}
.dl:hover{background:var(--drop)}
.spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite;flex:none}
@keyframes s{to{transform:rotate(360deg)}}
.foot{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-top:20px}
.btn{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.path{color:var(--muted);font-size:13px}
.upd{display:none;margin:0 0 18px;padding:14px 16px;border-radius:14px;background:var(--drop);border:1px solid var(--accent)}
.upd b{font-size:16px}
.upd ul{margin:6px 0 10px;padding-left:20px;color:var(--text)}
.upd .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.upd .msg{font-size:14px;color:var(--muted)}
.ver{color:var(--muted);font-size:12px;margin-top:14px;text-align:center}
.item{flex-wrap:wrap}
.pdft{width:56px;height:56px;border-radius:10px;background:#e5484d;color:#fff;font-weight:700;font-size:15px;display:flex;align-items:center;justify-content:center;flex:none}
.choice{flex-basis:100%;margin-top:4px;padding:12px 14px;border-radius:12px;background:var(--bg);font-size:14px}
.choice p{margin:0 0 10px}
.choice .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.choice .btn{padding:8px 12px;font-size:14px;text-decoration:none;display:inline-block}
.choice .hint{color:var(--muted);font-size:13px;margin-top:8px}
.files{flex-basis:100%;display:flex;flex-direction:column;gap:4px;margin-top:4px}
.files .f{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:6px 10px;border-radius:8px;background:var(--bg)}
.files .f a{color:var(--accent);font-weight:600;text-decoration:none;white-space:nowrap}
.files .f span{min-width:0;overflow:hidden;text-overflow:ellipsis}
.ver a{color:var(--muted);margin-left:10px}
.note{display:none;margin:0 0 18px;padding:14px 16px;border-radius:14px;background:rgba(31,157,85,.1);border:1px solid var(--ok);font-size:14px}
.note b{display:block;font-size:15px;margin-bottom:4px}
.bye{display:none;text-align:center;padding:80px 16px;color:var(--muted)}
.bye b{display:block;color:var(--text);font-size:20px;margin-bottom:6px}
</style></head><body><div class="wrap">
<h1>Конвертер</h1>
<p class="sub">Картинки и видео → WebP, PDF → сжатие до нужного размера. Всё обрабатывается на этом компьютере.</p>

<div class="note" id="note"><b>Теперь без Терминала</b>Конвертер запускается значком «Конвертер» на Рабочем столе или в Launchpad. Окно Терминала можно закрыть — при следующем запуске оно больше не появится.</div>

<div class="upd" id="upd">
  <b id="updTitle">Доступно обновление</b>
  <div class="msg">Что нового:</div>
  <ul id="updNotes"></ul>
  <div class="row"><button class="btn" id="updGo">Обновить</button><button class="btn ghost" id="updLater">Позже</button><span class="msg" id="updMsg"></span></div>
</div>

<div class="drop" id="drop">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
  <div class="big">Перетащите сюда файлы</div>
  <div class="small">или нажмите, чтобы выбрать · JPG, PNG, HEIC, GIF, MP4, MOV, PDF…</div>
  <input type="file" id="pick" multiple accept="image/*,video/*,.heic,.heif,.pdf,application/pdf" hidden>
</div>
<div class="warn" id="warn">ffmpeg не найден — картинки конвертируются, а видео и GIF нет. Установите: <b>brew install ffmpeg</b> и перезапустите приложение.</div>

<div class="panel">
  <div><label>Качество: <b id="qv">80</b></label><input type="range" id="q" min="10" max="100" value="80"></div>
  <div><label>Макс. ширина, px</label><input type="number" id="w" placeholder="как в оригинале" min="16" step="10"></div>
  <div><label>Кадров/с для видео</label><select id="fps"><option>8</option><option>10</option><option>12</option><option selected>15</option><option>20</option><option>24</option><option>30</option></select></div>
  <div><label>PDF: сжать до, МБ</label><input type="number" id="pdfmb" value="20" min="1" step="1"></div>
  <div><label class="chk"><input type="checkbox" id="lossless"> Без потерь</label></div>
</div>

<div class="list" id="list"></div>

<div class="foot">
  <span class="path" id="path"></span>
  <span><button class="btn ghost" id="clear">Очистить список</button> <button class="btn" id="open">Открыть папку</button></span>
</div>
<div class="ver"><span id="ver"></span><a href="#" id="quit">Закрыть конвертер</a></div>
</div>
<div class="bye" id="bye"><b>Конвертер закрыт</b>Чтобы снова открыть его, запустите значок «Конвертер».
</div>
<script>
const $=id=>document.getElementById(id);
const drop=$('drop'),pick=$('pick'),list=$('list');
const queue=[];let busy=false;
function size(n){if(n<1024)return n+' Б';if(n<1048576)return (n/1024).toFixed(0)+' КБ';return (n/1048576).toFixed(1)+' МБ'}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
fetch('/status').then(r=>r.json()).then(s=>{$('path').textContent='Сохраняется в: '+s.out.replace(/^\/Users\/[^/]+/,'~');if(!s.ffmpeg)$('warn').style.display='block';$('ver').textContent='Версия '+s.version;if(s.migrated)$('note').style.display='block'});
setInterval(()=>fetch('/ping').catch(()=>{}),20000);
$('quit').onclick=e=>{e.preventDefault();fetch('/quit',{method:'POST'}).catch(()=>{});document.querySelector('.wrap').style.display='none';$('bye').style.display='block'};
let LATEST='';
fetch('/update/check').then(r=>r.json()).then(u=>{if(!u.available)return;LATEST=u.latest;
$('updTitle').textContent=`Доступно обновление ${u.latest}`;
$('updNotes').innerHTML=(u.notes||[]).map(n=>`<li>${esc(n)}</li>`).join('')||'<li>Улучшения и исправления</li>';
$('upd').style.display='block'}).catch(()=>{});
$('updLater').onclick=()=>$('upd').style.display='none';
$('updGo').onclick=async()=>{const b=$('updGo');b.disabled=true;$('updLater').disabled=true;$('updMsg').textContent='Скачиваю обновление…';
try{const j=await (await fetch('/update/apply',{method:'POST'})).json();
if(!j.ok){$('updMsg').textContent='Не получилось: '+j.error;b.disabled=false;$('updLater').disabled=false;return}
$('updMsg').textContent='Перезапускаю…';
for(let i=0;i<40;i++){await new Promise(r=>setTimeout(r,500));try{const r=await fetch('/status',{cache:'no-store'});const st=await r.json();if(st.version===LATEST){location.reload();return}}catch(e){}}
$('updMsg').textContent='Перезапустите WebP.command вручную'}catch(e){$('updMsg').textContent='Нет связи с приложением'}};
$('q').oninput=e=>$('qv').textContent=e.target.value;
drop.onclick=()=>pick.click();
pick.onchange=()=>{add(pick.files);pick.value=''};
['dragenter','dragover'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>add(e.dataTransfer.files));
window.addEventListener('dragover',e=>e.preventDefault());window.addEventListener('drop',e=>e.preventDefault());
$('open').onclick=()=>fetch('/open',{method:'POST'});
$('clear').onclick=()=>{list.querySelectorAll('.item.done').forEach(n=>n.remove())};
function add(files){for(const f of files){if(!f.size&&!f.type)continue;const el=document.createElement('div');el.className='item';
el.innerHTML=`<div class="thumb"></div><div class="info"><div class="name">${esc(f.name)}</div><div class="meta">${size(f.size)} · в очереди</div></div>`;
list.prepend(el);queue.push({f,el})}run()}
function plural(n,one,few,many){const m10=n%10,m100=n%100;return m10==1&&m100!=11?one:(m10>=2&&m10<=4&&(m100<10||m100>=20)?few:many)}
function showFiles(el,saved){const box=document.createElement('div');box.className='files';
box.innerHTML=saved.map(x=>`<div class="f"><span>${esc(x.name)} · ${size(x.size)} · ${esc(x.level)}${x.pages?' · стр. '+x.pages:''}${x.fits===false?' · <b class="bad">больше лимита</b>':''}</span><a href="/out/${encodeURIComponent(x.name)}" target="_blank">Открыть</a></div>`).join('');el.appendChild(box)}
async function pdfAction(el,url,label){const ch=el.querySelector('.choice');ch.innerHTML=`<div class="row"><div class="spin"></div><span>${label}</span></div>`;
try{const j=await (await fetch(url,{method:'POST'})).json();ch.remove();
if(j.ok){if(j.saved.length>1){const m=el.querySelector('.meta');m.textContent=m.textContent.replace(/ · [^·]+$/,` · разделён на ${j.saved.length} ${plural(j.saved.length,'файл','файла','файлов')}`)}showFiles(el,j.saved)}else el.querySelector('.meta').innerHTML=`<span class="bad">Ошибка: ${esc(j.error)}</span>`}
catch(e){ch.innerHTML='<span class="bad">Нет связи — конвертер закрыт?</span>'}}
function pdfResult(el,meta,j){const pct=Math.round((1-j.out/j.in)*100);
meta.innerHTML=`${size(j.in)} → ${size(j.out)} · ${j.pages} ${plural(j.pages,'страница','страницы','страниц')} · ${esc(j.level)}`;
if(!j.choice){showFiles(el,j.saved);return}
const ch=document.createElement('div');ch.className='choice';
const why=j.fits?`Чтобы уложиться в ${j.target_mb} МБ, картинки внутри PDF пришлось сильно сжать. Текст останется чётким, но фото и мелкие детали могут стать размытыми.`
:`Даже при максимальном сжатии файл весит ${size(j.out)} — это больше ${j.target_mb} МБ.`;
let html=`<p>${why} Как поступить?</p><div class="row"><a class="btn ghost" href="/pdf/preview/${j.id}" target="_blank">Посмотреть, как получилось</a>
<button class="btn ghost" data-a="keep">Оставить одним файлом · ${size(j.out)}</button>`;
if(j.parts)html+=`<button class="btn" data-a="split">Разделить на ${j.parts} ${plural(j.parts,'файл','файла','файлов')} · качество лучше</button>`;
html+=`</div><div class="hint">Один файл удобнее отправлять, если качество картинок не критично. Разделение сохраняет качество, но получится несколько файлов.</div>`;
ch.innerHTML=html;el.appendChild(ch);
ch.querySelector('[data-a=keep]').onclick=()=>pdfAction(el,`/pdf/keep?id=${j.id}`,'Сохраняю…');
const sp=ch.querySelector('[data-a=split]');if(sp)sp.onclick=()=>pdfAction(el,`/pdf/split?id=${j.id}&parts=${j.parts}`,`Делю на ${j.parts} ${plural(j.parts,'часть','части','частей')} и сжимаю…`)}
async function run(){if(busy)return;busy=true;while(queue.length){const {f,el}=queue.shift();const meta=el.querySelector('.meta');
const isPdf=/\.pdf$/i.test(f.name)||f.type==='application/pdf';
meta.textContent=size(f.size)+(isPdf?' · сжимаю PDF… большие файлы — до пары минут':' · конвертирую…');const sp=document.createElement('div');sp.className='spin';el.appendChild(sp);
if(isPdf)el.querySelector('.thumb').outerHTML='<div class="pdft">PDF</div>';
try{let r;
if(isPdf){const p=new URLSearchParams({name:f.name,target:$('pdfmb').value||'20'});r=await fetch('/pdf/compress?'+p,{method:'POST',body:f})}
else{const p=new URLSearchParams({name:f.name,q:$('q').value,w:$('w').value||'0',fps:$('fps').value,lossless:$('lossless').checked?'1':'0'});r=await fetch('/convert?'+p,{method:'POST',body:f})}
const j=await r.json();sp.remove();
if(j.ok&&j.kind==='pdf')pdfResult(el,meta,j);
else if(j.ok){const pct=Math.round((1-j.out/j.in)*100);const t=`/out/${encodeURIComponent(j.name)}?t=${Date.now()}`;
el.querySelector('.thumb').outerHTML=`<img class="thumb" src="${t}" alt="">`;
meta.innerHTML=`${size(j.in)} → ${size(j.out)} · <span class="${pct>=0?'good':'bad'}">${pct>=0?'−'+pct:'+'+(-pct)}%</span>`;
const a=document.createElement('a');a.className='dl';a.href=t;a.download=j.name;a.textContent='Скачать';el.appendChild(a)}
else meta.innerHTML=`<span class="bad">Ошибка: ${esc(j.error||'неизвестно')}</span>`}
catch(e){sp.remove();meta.innerHTML='<span class="bad">Нет связи — конвертер закрыт? Запустите значок «Конвертер»</span>'}
el.classList.add('done')}busy=false}
</script></body></html>"""


def main():
    if "--install" in sys.argv:
        install_bundle(force=True)
        print(f"Приложение «{APP_NAME}» создано:", BUNDLE)
        return
    threading.Thread(target=ensure_pdf_lib, daemon=True).start()
    try:
        MIGRATED[0] = install_bundle()
    except Exception as e:
        print("Не удалось создать приложение:", e)
    srv = None
    pref = int(os.environ.get("WEBP_PORT", "0") or 0)
    ports = ([pref] if pref else []) + [8765, 8766, 8767, 0]
    for port in ports:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"{APP_NAME} {VERSION} запущен:", url)
    print("Готовые файлы:", OUT_DIR)
    LAST_SEEN[0] = time.time()
    threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    if not os.environ.get("WEBP_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
