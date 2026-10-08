#!/usr/bin/env python3
"""
Конвертер — перевод картинок и видео в WebP, с окном в браузере.

Запуск:  python3 webp_app.py            (или значок «Конвертер» на macOS)
         python3 webp_app.py --install  (создать приложение «Конвертер»)
Откроется страница в браузере: перетащите туда картинки или видео.
Готовые файлы сохраняются в папку «Загрузки/WebP».

Нужно: Pillow (pip install pillow), для видео — ffmpeg.
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

VERSION = "1.1"
REPO_RAW = os.environ.get("WEBP_REPO", "https://raw.githubusercontent.com/mkkatrin/webp-converter/main/")
APP_FILE = Path(__file__).resolve()

OUT_DIR = Path.home() / "Downloads" / "WebP"
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
                        "migrated": MIGRATED[0], "app": sys.platform == "darwin"})
        elif u.path == "/update/check":
            self._json(check_update())
        elif u.path.startswith("/out/"):
            f = OUT_DIR / Path(unquote(u.path[5:])).name
            if f.is_file():
                self._send(200, f.read_bytes(), "image/webp")
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
.ver a{color:var(--muted);margin-left:10px}
.note{display:none;margin:0 0 18px;padding:14px 16px;border-radius:14px;background:rgba(31,157,85,.1);border:1px solid var(--ok);font-size:14px}
.note b{display:block;font-size:15px;margin-bottom:4px}
.bye{display:none;text-align:center;padding:80px 16px;color:var(--muted)}
.bye b{display:block;color:var(--text);font-size:20px;margin-bottom:6px}
</style></head><body><div class="wrap">
<h1>Конвертер</h1>
<p class="sub">Картинки и видео → WebP. Всё обрабатывается на этом компьютере.</p>

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
  <div class="small">или нажмите, чтобы выбрать · JPG, PNG, HEIC, GIF, MP4, MOV…</div>
  <input type="file" id="pick" multiple accept="image/*,video/*,.heic,.heif" hidden>
</div>
<div class="warn" id="warn">ffmpeg не найден — картинки конвертируются, а видео и GIF нет. Установите: <b>brew install ffmpeg</b> и перезапустите приложение.</div>

<div class="panel">
  <div><label>Качество: <b id="qv">80</b></label><input type="range" id="q" min="10" max="100" value="80"></div>
  <div><label>Макс. ширина, px</label><input type="number" id="w" placeholder="как в оригинале" min="16" step="10"></div>
  <div><label>Кадров/с для видео</label><select id="fps"><option>8</option><option>10</option><option>12</option><option selected>15</option><option>20</option><option>24</option><option>30</option></select></div>
  <div><label class="chk"><input type="checkbox" id="lossless"> Без потерь</label></div>
</div>

<div class="list" id="list"></div>

<div class="foot">
  <span class="path" id="path"></span>
  <span><button class="btn ghost" id="clear">Очистить список</button> <button class="btn" id="open">Открыть папку WebP</button></span>
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
async function run(){if(busy)return;busy=true;while(queue.length){const {f,el}=queue.shift();const meta=el.querySelector('.meta');
meta.textContent=size(f.size)+' · конвертирую…';const sp=document.createElement('div');sp.className='spin';el.appendChild(sp);
const p=new URLSearchParams({name:f.name,q:$('q').value,w:$('w').value||'0',fps:$('fps').value,lossless:$('lossless').checked?'1':'0'});
try{const r=await fetch('/convert?'+p,{method:'POST',body:f});const j=await r.json();sp.remove();
if(j.ok){const pct=Math.round((1-j.out/j.in)*100);const t=`/out/${encodeURIComponent(j.name)}?t=${Date.now()}`;
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


if __name__ == "__main__":
    main()
