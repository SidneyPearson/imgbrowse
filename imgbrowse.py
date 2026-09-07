#!/usr/bin/env python3
# imgbrowse — 本地大图丝滑浏览工具
# 用法: python3 imgbrowse.py [图片目录] [--port 8765] [--no-open]
# 零依赖（仅标准库），缩略图通过 macOS 自带 sips 生成并缓存。

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

IMG_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif',
           '.gif', '.webp', '.bmp', '.tif', '.tiff'}
VID_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}
mimetypes.add_type('image/heic', '.heic')
mimetypes.add_type('image/heif', '.heif')
mimetypes.add_type('video/quicktime', '.mov')
mimetypes.add_type('video/x-m4v', '.m4v')
mimetypes.add_type('video/x-matroska', '.mkv')

CACHE_DIR = os.path.expanduser('~/Library/Caches/imgbrowse')
STATE_PATH = os.path.expanduser(
    '~/Library/Application Support/imgbrowse/state.json')
DERIV_CFG = {'thumb': (400, 65), 'grid': (560, 72),
             'cover': (720, 76), 'screen': (2048, 75)}

# 收藏 / 自定义封面：按根目录隔离，值为文件指纹 k（几乎不占空间）
STATE = {}


def load_state():
    global STATE
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            STATE = json.load(f)
    except Exception:
        STATE = {}


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix='.json',
                                   dir=os.path.dirname(STATE_PATH))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        sys.stderr.write('[state] %s\n' % e)


def rstate():
    return STATE.setdefault(ROOT, {'fav': {}, 'cover': {}})


def state_payload():
    """给前端的状态：指纹 k 换算成当前图片 id。"""
    rs = rstate()
    k2id = {r.get('k'): r['id'] for r in IMAGES}
    fav_ids = sorted(k2id[k] for k in rs['fav'] if k in k2id)
    covers = {a: k2id[k] for a, k in rs['cover'].items() if k in k2id}
    return {'favIds': fav_ids, 'covers': covers}


def sanitize_state():
    """重扫后清理失效指纹（文件已删/移动）。"""
    ks = {r.get('k') for r in IMAGES}
    rs = rstate()
    rs['fav'] = {k: v for k, v in rs['fav'].items() if k in ks}
    rs['cover'] = {a: k for a, k in rs['cover'].items() if k in ks}
    save_state()

ROOT = ''
IMAGES = []          # [{'id','album','name','rel','size'}]
ALBUMS = []          # [{'name','label','count'}]

_gen_locks = {}
_gen_locks_guard = threading.Lock()
# 限制同时运行的 sips 进程数：单个 sips 处理大图峰值内存可达百 MB 级，
# 不设闸时浏览器并发请求 + 预热会同时拉起十几个进程，可能撑爆内存。
_sips_sem = threading.Semaphore(3)


# ---------------------------------------------------------------- 扫描

def scan(root):
    images = []

    def add(fp, album, relname):
        try:
            st = os.stat(fp)
        except OSError:
            return
        ext = os.path.splitext(relname)[1].lower()
        if ext in IMG_EXT:
            typ = 'image'
        elif ext in VID_EXT:
            typ = 'video'
        else:
            return
        images.append({'album': album, 'name': os.path.basename(relname),
                       'rel': relname, 'size': st.st_size,
                       'type': typ, 'k': cache_key(fp, st)})

    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            for dp, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    fp = os.path.join(dp, f)
                    add(fp, name, os.path.relpath(fp, root))
        elif os.path.isfile(p):
            add(p, '', name)
    # 根目录散图在前，其余按 相册/相对路径 排序
    images.sort(key=lambda r: (r['album'] != '', r['album'], r['rel']))
    for i, r in enumerate(images):
        r['id'] = i
    return images, build_albums(images)


def build_albums(images, cover_ks=None):
    """按当前 images 汇总相册（张数 + 封面）。
    cover_ks: {相册名: 文件指纹k}，命中则用自定义封面，否则名字排序第一张。"""
    counts = {}
    first = {}
    k2id = {}
    for r in sorted(images, key=lambda r: r['rel']):
        counts[r['album']] = counts.get(r['album'], 0) + 1
        if r['album'] not in first:
            first[r['album']] = r['id']
        k2id[r.get('k')] = r['id']
    cover_ks = cover_ks or {}

    def cover_of(a):
        k = cover_ks.get(a)
        return k2id[k] if k in k2id else first[a]

    albums = []
    if '' in counts:
        albums.append({'name': '', 'label': '（根目录）',
                       'count': counts[''], 'cover': cover_of('')})
    for a in sorted(k for k in counts if k):
        albums.append({'name': a, 'label': a,
                       'count': counts[a], 'cover': cover_of(a)})
    return albums


def rebuild_albums():
    """用当前 ROOT 的自定义封面配置重建 ALBUMS。"""
    ALBUMS.clear()
    ALBUMS.extend(build_albums(IMAGES, rstate().get('cover', {})))


def move_images(ids, dest):
    """把图片移动到 dest 相册文件夹（不存在则创建）。返回 moved 记录列表。"""
    dest = (dest or '').strip()
    if not dest or '/' in dest or os.sep in dest or dest in ('.', '..') \
            or dest.startswith('..'):
        raise ValueError('目标文件夹名不合法')
    dest_dir = os.path.join(ROOT, dest)
    os.makedirs(dest_dir, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)
    if not (dest_real == ROOT or dest_real.startswith(ROOT + os.sep)):
        raise ValueError('目标文件夹超出根目录')

    moved = []
    for img_id in ids:
        if not (0 <= img_id < len(IMAGES)):
            continue
        rec = IMAGES[img_id]
        if rec['album'] == dest:
            continue                            # 已经在目标文件夹
        src = os.path.join(ROOT, rec['rel'])
        name = rec['name']
        dst = os.path.join(dest_dir, name)
        if os.path.exists(dst):                 # 重名自动加序号
            stem, ext = os.path.splitext(name)
            n = 1
            while os.path.exists(os.path.join(dest_dir, '%s (%d)%s' % (stem, n, ext))):
                n += 1
            name = '%s (%d)%s' % (stem, n, ext)
            dst = os.path.join(dest_dir, name)
        oldk = rec.get('k')
        shutil.move(src, dst)                   # 同盘 rename，跨盘自动复制
        rec['album'] = dest
        rec['name'] = name
        rec['rel'] = os.path.relpath(dst, ROOT)
        st = os.stat(dst)
        rec['size'] = st.st_size
        rec['k'] = cache_key(dst, st)
        # 收藏/封面指纹跟随移动
        rs = rstate()
        if oldk and oldk in rs['fav']:
            rs['fav'].pop(oldk, None)
            rs['fav'][rec['k']] = True
        for a, k in list(rs['cover'].items()):
            if k == oldk:
                rs['cover'][a] = rec['k']
        moved.append({'id': img_id, 'album': dest,
                      'name': name, 'rel': rec['rel'], 'k': rec['k']})

    rebuild_albums()
    save_state()
    return moved


def set_root(path):
    """切换浏览根目录并重新扫描。"""
    path = os.path.realpath(os.path.expanduser((path or '').strip()))
    if not os.path.isdir(path):
        raise ValueError('路径不存在或不是文件夹')
    imgs, albums = scan(path)
    if not imgs:
        raise ValueError('该文件夹（含子文件夹）下没有找到图片')
    global ROOT
    ROOT = path
    IMAGES.clear()
    IMAGES.extend(imgs)
    sanitize_state()
    rebuild_albums()
    threading.Thread(target=warm_start, daemon=True).start()
    return ROOT


def pick_folder_native():
    """弹出 macOS 原生文件夹选择框，返回用户选择的路径（取消返回 None）。"""
    r = subprocess.run(
        ['osascript', '-e',
         'POSIX path of (choose folder with prompt "选择要浏览的图片文件夹")'],
        capture_output=True, text=True, timeout=600)
    if r.returncode == 0:
        return r.stdout.strip().rstrip('/')
    return None


def rescan():
    """重扫当前 ROOT，替换全局 IMAGES/ALBUMS，并清理状态。"""
    imgs, _ = scan(ROOT)
    IMAGES.clear()
    IMAGES.extend(imgs)
    sanitize_state()
    rebuild_albums()


def trash_files(ids):
    """把文件移到 macOS 废纸篓（可恢复）。返回处理数量。"""
    paths = []
    for img_id in ids:
        if 0 <= img_id < len(IMAGES):
            p = os.path.join(ROOT, IMAGES[img_id]['rel'])
            if os.path.exists(p):
                paths.append(p)
    if not paths:
        return 0
    # 优先走 Finder：真废纸篓、可放回；失败则手动移入 ~/.Trash
    spec = ', '.join('POSIX file "%s"' % p.replace('"', '\\"') for p in paths)
    r = subprocess.run(
        ['osascript', '-e',
         'tell application "Finder" to delete {%s}' % spec],
        capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        sys.stderr.write('[trash] Finder 失败，使用 ~/.Trash: %s\n'
                         % r.stderr.strip()[:200])
        trash_dir = os.path.expanduser('~/.Trash')
        os.makedirs(trash_dir, exist_ok=True)
        for p in paths:
            dst = os.path.join(trash_dir, os.path.basename(p))
            stem, ext = os.path.splitext(dst)
            n = 1
            while os.path.exists(dst):
                dst = '%s (%d)%s' % (stem, n, ext)
                n += 1
            shutil.move(p, dst)
    rescan()
    return len(paths)


def set_fav(img_id, on):
    if not (0 <= img_id < len(IMAGES)):
        raise ValueError('图片不存在')
    rs = rstate()
    k = IMAGES[img_id]['k']
    if on:
        rs['fav'][k] = True
    else:
        rs['fav'].pop(k, None)
    save_state()


def set_cover(album, img_id):
    if not (0 <= img_id < len(IMAGES)):
        raise ValueError('图片不存在')
    rec = IMAGES[img_id]
    if rec['album'] != album:
        raise ValueError('封面图片必须在该文件夹内')
    rstate()['cover'][album] = rec['k']
    save_state()
    rebuild_albums()


def image_info(img_id):
    """汇总文件信息 + sips 尺寸 + Spotlight EXIF。"""
    if not (0 <= img_id < len(IMAGES)):
        raise ValueError('图片不存在')
    rec = IMAGES[img_id]
    p = os.path.join(ROOT, rec['rel'])
    st = os.stat(p)
    info = {'name': rec['name'], 'path': p, 'size': st.st_size,
            'mtime': st.st_mtime, 'type': rec['type'],
            'album': rec['album'] or '（根目录）'}
    try:
        r = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight',
                            '-g', 'format', p],
                           capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            m = re.match(r'\s*pixelWidth:\s*(\d+)', line)
            if m:
                info['w'] = int(m.group(1))
            m = re.match(r'\s*pixelHeight:\s*(\d+)', line)
            if m:
                info['h'] = int(m.group(1))
            m = re.match(r'\s*format:\s*(\S+)', line)
            if m:
                info['format'] = m.group(1)
    except Exception:
        pass
    # Spotlight 元数据（拍摄时间/相机/曝光/ISO/焦距/时长）
    md_keys = ['kMDItemContentCreationDate', 'kMDItemAcquisitionMake',
               'kMDItemAcquisitionModel', 'kMDItemExposureTimeSeconds',
               'kMDItemFNumber', 'kMDItemISOSpeed', 'kMDItemFocalLength',
               'kMDItemDurationSeconds', 'kMDItemGPSLatitude',
               'kMDItemGPSLongitude']
    try:
        cmd = ['mdls']
        for k in md_keys:
            cmd += ['-name', k]
        cmd.append(p)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        cur = None
        for line in r.stdout.splitlines():
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()
            if val in ('(null)', ''):
                continue
            if key == 'kMDItemContentCreationDate':
                info['date'] = val.split(' +')[0].replace(' ', ' ')
            elif key == 'kMDItemAcquisitionMake':
                info['cameraMake'] = val.strip('"')
            elif key == 'kMDItemAcquisitionModel':
                info['cameraModel'] = val.strip('"')
            elif key == 'kMDItemExposureTimeSeconds':
                try:
                    ev = float(val)
                    info['exposure'] = ('1/%d' % round(1 / ev)) if ev < 0.5 \
                        else ('%.1fs' % ev)
                except ValueError:
                    pass
            elif key == 'kMDItemFNumber':
                info['fnumber'] = 'f/' + val.rstrip('0').rstrip('.') \
                    if '.' in val else 'f/' + val
            elif key == 'kMDItemISOSpeed':
                info['iso'] = val
            elif key == 'kMDItemFocalLength':
                info['focal'] = val.rstrip('0').rstrip('.') + 'mm'
            elif key == 'kMDItemDurationSeconds':
                try:
                    sec = float(val)
                    info['duration'] = '%d:%02d' % (sec // 60, sec % 60)
                except ValueError:
                    pass
            elif key == 'kMDItemGPSLatitude':
                try:
                    info['gpsLat'] = round(float(val), 5)
                except ValueError:
                    pass
            elif key == 'kMDItemGPSLongitude':
                try:
                    info['gpsLon'] = round(float(val), 5)
                except ValueError:
                    pass
    except Exception:
        pass
    return info


def rename_album(old, new):
    """重命名相册文件夹，返回重扫后的 (images, albums)。"""
    old = (old or '').strip()
    new = (new or '').strip()
    if not old or not new or '/' in new or os.sep in new \
            or new in ('.', '..') or new.startswith('..'):
        raise ValueError('文件夹名不合法')
    src = os.path.join(ROOT, old)
    dst = os.path.join(ROOT, new)
    if not os.path.isdir(src):
        raise ValueError('源文件夹不存在')
    if os.path.exists(dst):
        raise ValueError('已存在同名文件夹')
    os.rename(src, dst)
    return scan(ROOT)


# ---------------------------------------------------------------- 缩略图

def cache_key(abspath, st):
    h = hashlib.sha1()
    h.update(abspath.encode('utf-8'))
    h.update(str(st.st_mtime_ns).encode())
    h.update(str(st.st_size).encode())
    return h.hexdigest()[:20]


def _gen_video_frame(abspath, tmp, size, q):
    """用 qlmanage 抽视频帧，再用 sips 压成指定尺寸 jpg。"""
    import glob
    with tempfile.TemporaryDirectory() as td:
        with _sips_sem:
            subprocess.run(
                ['qlmanage', '-t', '-s', str(size), '-o', td, abspath],
                check=True, capture_output=True, timeout=180)
        pngs = glob.glob(os.path.join(td, '*.png'))
        if not pngs:
            raise RuntimeError('qlmanage 未生成视频帧')
        subprocess.run(
            ['sips', '-Z', str(size),
             '-s', 'format', 'jpeg', '-s', 'formatOptions', str(q),
             pngs[0], '--out', tmp],
            check=True, capture_output=True, timeout=120)


def ensure_deriv(kind, abspath):
    """返回 (缓存文件路径, etag)。图片用 sips、视频用 qlmanage 抽帧。"""
    st = os.stat(abspath)
    key = cache_key(abspath, st)
    dst_dir = os.path.join(CACHE_DIR, kind)
    dst = os.path.join(dst_dir, key + '.jpg')
    if os.path.exists(dst):
        return dst, key
    with _gen_locks_guard:
        lock = _gen_locks.setdefault(kind + ':' + key, threading.Lock())
    with lock:
        if os.path.exists(dst):
            return dst, key
        size, q = DERIV_CFG[kind]
        os.makedirs(dst_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix='.jpg', dir=dst_dir)
        os.close(fd)
        try:
            is_video = os.path.splitext(abspath)[1].lower() in VID_EXT
            if is_video:
                _gen_video_frame(abspath, tmp, size, q)
            else:
                with _sips_sem:
                    subprocess.run(
                        ['sips', '-Z', str(size),
                         '-s', 'format', 'jpeg', '-s', 'formatOptions', str(q),
                         abspath, '--out', tmp],
                        check=True, capture_output=True, timeout=120)
            os.replace(tmp, dst)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return dst, key


def warm_start():
    """后台预热所有相册封面（高清档）——首屏封面墙秒开，性价比最高。"""
    def safe(rec):
        try:
            ensure_deriv('cover', os.path.join(ROOT, rec['rel']))
        except Exception:
            pass
    pool = ThreadPoolExecutor(max_workers=4)
    for a in ALBUMS:
        if a.get('cover') is not None:
            pool.submit(safe, IMAGES[a['cover']])
    pool.shutdown(wait=False)


# ---------------------------------------------------------------- HTTP

def send_bytes(h, code, ctype, body, extra=None):
    h.send_response(code)
    h.send_header('Content-Type', ctype)
    h.send_header('Content-Length', str(len(body)))
    h.send_header('Accept-Ranges', 'bytes')
    for k, v in (extra or []):
        h.send_header(k, v)
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def serve_file(h, path, max_age=86400, etag=None):
    try:
        st = os.stat(path)
    except OSError:
        send_bytes(h, 404, 'text/plain; charset=utf-8', b'not found')
        return
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    if etag is None:
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
    headers = [('Cache-Control', 'public, max-age=%d' % max_age),
               ('ETag', etag)]
    inm = h.headers.get('If-None-Match')
    if inm and (inm == etag or inm == '*'):
        send_bytes(h, 304, ctype, b'', headers)
        return
    rng = h.headers.get('Range')
    start = end = None
    if rng:
        m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
        if m:
            a, b = m.group(1), m.group(2)
            if a or b:
                start = int(a) if a else max(0, st.st_size - int(b))
                end = int(b) if b else st.st_size - 1
                end = min(end, st.st_size - 1)
    if start is None or start >= st.st_size:
        h.send_response(200)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(st.st_size))
        h.send_header('Accept-Ranges', 'bytes')
        h.send_header('Cache-Control', 'public, max-age=%d' % max_age)
        h.send_header('ETag', etag)
        h.end_headers()
        if h.command != 'HEAD':
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.wfile.write(chunk)
        return
    if start > end:
        send_bytes(h, 416, 'text/plain; charset=utf-8', b'range not satisfiable',
                   [('Content-Range', 'bytes */%d' % st.st_size)])
        return
    length = end - start + 1
    h.send_response(206)
    h.send_header('Content-Type', ctype)
    h.send_header('Content-Length', str(length))
    h.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, st.st_size))
    h.send_header('Accept-Ranges', 'bytes')
    h.send_header('Cache-Control', 'public, max-age=%d' % max_age)
    h.send_header('ETag', etag)
    h.end_headers()
    if h.command == 'HEAD':
        return
    with open(path, 'rb') as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.wfile.write(chunk)
            remaining -= len(chunk)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass  # 安静；错误单独打印

    def log_error(self, fmt, *args):
        sys.stderr.write('[http] ' + (fmt % args) + '\n')

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == '/' or path == '/index.html':
                send_bytes(self, 200, 'text/html; charset=utf-8',
                           FRONTEND.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif path == '/api/meta':
                body = json.dumps({'root': ROOT, 'total': len(IMAGES),
                                   'albums': ALBUMS}).encode('utf-8')
                send_bytes(self, 200, 'application/json; charset=utf-8', body,
                           [('Cache-Control', 'no-store')])
            elif path == '/api/images':
                body = json.dumps(IMAGES, ensure_ascii=False).encode('utf-8')
                send_bytes(self, 200, 'application/json; charset=utf-8', body,
                           [('Cache-Control', 'no-store')])
            elif path == '/api/state':
                body = json.dumps(state_payload(), ensure_ascii=False).encode('utf-8')
                send_bytes(self, 200, 'application/json; charset=utf-8', body,
                           [('Cache-Control', 'no-store')])
            elif path == '/api/info':
                img_id = int(qs.get('id', ['-1'])[0])
                body = json.dumps(image_info(img_id), ensure_ascii=False).encode('utf-8')
                send_bytes(self, 200, 'application/json; charset=utf-8', body,
                           [('Cache-Control', 'no-store')])
            elif path == '/img':
                self.serve_img(qs)
            else:
                send_bytes(self, 404, 'text/plain; charset=utf-8', b'not found')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write('[err] %s\n' % e)
            try:
                send_bytes(self, 500, 'text/plain; charset=utf-8',
                           ('error: %s' % e).encode('utf-8'))
            except Exception:
                pass

    def serve_img(self, qs):
        try:
            img_id = int(qs.get('id', ['-1'])[0])
        except ValueError:
            img_id = -1
        kind = qs.get('t', ['screen'])[0]
        if not (0 <= img_id < len(IMAGES)) or kind not in ('thumb', 'grid', 'cover', 'screen', 'raw'):
            send_bytes(self, 400, 'text/plain; charset=utf-8', b'bad request')
            return
        rec = IMAGES[img_id]
        abspath = os.path.realpath(os.path.join(ROOT, rec['rel']))
        if not (abspath == ROOT or abspath.startswith(ROOT + os.sep)):
            send_bytes(self, 403, 'text/plain; charset=utf-8', b'forbidden')
            return
        # 视频没有 screen 转码档，灯箱直接流式播放原文件（支持 Range 拖动）
        if kind == 'raw' or (rec.get('type') == 'video' and kind == 'screen'):
            serve_file(self, abspath, max_age=86400)
        else:
            dst, key = ensure_deriv(kind, abspath)
            serve_file(self, dst, max_age=31536000, etag='"%s"' % key)

    # ---- POST ----
    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(length) or b'{}')
            if parsed.path == '/api/reveal':
                img_id = int(data.get('id', -1))
                if 0 <= img_id < len(IMAGES):
                    abspath = os.path.join(ROOT, IMAGES[img_id]['rel'])
                    subprocess.Popen(['open', '-R', abspath])
                    send_bytes(self, 200, 'application/json', b'{"ok":true}')
                else:
                    send_bytes(self, 400, 'application/json', b'{"ok":false}')
            elif parsed.path == '/api/move':
                ids = [int(x) for x in data.get('ids', [])]
                dest = str(data.get('dest', ''))
                moved = move_images(ids, dest)
                body = json.dumps({'ok': True, 'moved': moved,
                                   'albums': ALBUMS,
                                   'state': state_payload()}, ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/fav':
                img_id = int(data.get('id', -1))
                set_fav(img_id, bool(data.get('on', True)))
                body = json.dumps({'ok': True, 'state': state_payload()},
                                  ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/set-cover':
                set_cover(str(data.get('album', '')),
                          int(data.get('id', -1)))
                body = json.dumps({'ok': True, 'albums': ALBUMS,
                                   'state': state_payload()}, ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/delete':
                ids = [int(x) for x in data.get('ids', [])]
                n = trash_files(ids)
                body = json.dumps({'ok': True, 'deleted': n,
                                   'albums': ALBUMS, 'images': IMAGES,
                                   'state': state_payload()}, ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/pick-folder':
                path = pick_folder_native()
                if path:
                    body = json.dumps({'ok': True, 'path': path},
                                      ensure_ascii=False).encode('utf-8')
                else:
                    body = b'{"ok":false,"canceled":true}'
                send_bytes(self, 200, 'application/json; charset=utf-8', body,
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/set-root':
                root = set_root(str(data.get('path', '')))
                body = json.dumps({'ok': True, 'root': root,
                                   'total': len(IMAGES),
                                   'albums': ALBUMS}, ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            elif parsed.path == '/api/rename-album':
                old = str(data.get('from', ''))
                new = str(data.get('to', ''))
                imgs, _ = rename_album(old, new)
                IMAGES.clear()
                IMAGES.extend(imgs)
                sanitize_state()
                rebuild_albums()
                body = json.dumps({'ok': True, 'albums': ALBUMS,
                                   'images': IMAGES,
                                   'state': state_payload()}, ensure_ascii=False)
                send_bytes(self, 200, 'application/json; charset=utf-8',
                           body.encode('utf-8'),
                           [('Cache-Control', 'no-store')])
            else:
                send_bytes(self, 404, 'text/plain', b'not found')
        except Exception as e:
            sys.stderr.write('[err] %s\n' % e)
            body = json.dumps({'ok': False, 'error': str(e)},
                              ensure_ascii=False).encode('utf-8')
            send_bytes(self, 500, 'application/json; charset=utf-8', body)


# ---------------------------------------------------------------- 前端

FRONTEND = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>imgbrowse · 图片浏览</title>
<style>
:root{
  --bg:#14161a; --panel:#1b1e25; --panel2:#22262f; --line:#2c313c;
  --text:#e7eaef; --muted:#98a0ad; --accent:#5b9dff; --accent-dim:#2b4a7d;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC","Helvetica Neue",sans-serif;
  overflow:hidden}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#3a4150;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}

#app{display:flex;height:100vh}

/* ---------- 侧栏 ---------- */
#sidebar{width:248px;flex:none;background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;transition:margin-left .2s ease}
body.sb-closed #sidebar{margin-left:-248px}
.sb-head{padding:14px 14px 10px}
.sb-title{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.sb-sub{font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
#album-search{width:100%;margin-top:10px;padding:7px 10px;border-radius:8px;
  border:1px solid var(--line);background:var(--bg);color:var(--text);outline:none}
#album-search:focus{border-color:var(--accent-dim)}
#album-list{flex:1;overflow-y:auto;padding:4px 8px 16px}
.album{display:flex;align-items:center;gap:8px;width:100%;text-align:left;cursor:pointer;
  padding:7px 10px;border-radius:8px;color:var(--muted);white-space:nowrap}
.album:hover{background:var(--panel2);color:var(--text)}
.album.active{background:var(--accent-dim);color:#fff}
.album .nm{flex:1;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.album .ct{font-size:11px;color:var(--muted);background:var(--bg);border-radius:10px;
  padding:1px 8px;flex:none}
.album.active .ct{background:rgba(255,255,255,.15);color:#dbe7ff}
.album-rename{display:none;flex:none;background:none;border:none;color:var(--muted);
  font-size:12px;padding:2px 5px;border-radius:6px;cursor:pointer;line-height:1}
.album:hover .album-rename{display:block}
.album-rename:hover{color:#fff;background:rgba(255,255,255,.14)}
.album-edit{flex:1;min-width:0;padding:3px 7px;border-radius:7px;
  border:1px solid var(--accent);background:var(--bg);color:var(--text);
  font-size:13px;outline:none}
.card-rename{display:none;position:absolute;top:8px;left:8px;z-index:3;
  width:28px;height:28px;border-radius:8px;background:rgba(0,0,0,.5);
  border:1px solid rgba(255,255,255,.35);color:#fff;font-size:13px;
  align-items:center;justify-content:center}
.album-card:hover .card-rename{display:flex}
.card-rename:hover{background:rgba(0,0,0,.75)}

/* ---------- 主区 ---------- */
main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
#topbar{height:48px;flex:none;display:flex;align-items:center;gap:12px;padding:0 14px;
  border-bottom:1px solid var(--line);background:var(--panel)}
#btn-sidebar{font-size:18px;color:var(--muted);padding:4px 8px;border-radius:6px}
#btn-sidebar:hover{background:var(--panel2);color:var(--text)}
#cur-name{font-weight:600}
#cur-count{color:var(--muted);font-size:12px}
#grid{flex:1;overflow:hidden;padding:10px;display:grid;
  grid-template-columns:repeat(4,1fr);gap:8px;align-content:start}
.tile{position:relative;border-radius:10px;overflow:hidden;cursor:pointer;
  background:var(--panel2)}
.tile::before{content:'';display:block;padding-top:100%}  /* 撑出正方形，兼容性最好 */
.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;
  transition:transform .25s ease,opacity .3s ease;opacity:0}
.tile img.loaded{opacity:1}
.tile:hover img{transform:scale(1.06)}
.tile-label{position:absolute;left:0;right:0;bottom:0;padding:18px 8px 6px;font-size:11px;
  color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.72));
  opacity:0;transition:opacity .15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile:hover .tile-label{opacity:1}
#empty{padding:60px 20px;text-align:center;color:var(--muted)}
/* 相册封面墙：卡片更大、名称常显 */
#grid.albums{gap:14px}
.album-card{border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.35)}
.album-card .tile-label{opacity:1;padding:26px 12px 9px;font-size:13px;font-weight:600;
  display:flex;justify-content:space-between;gap:8px}
.album-card .tile-label .ct{font-weight:400;color:rgba(255,255,255,.75);flex:none}
@keyframes pageIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
#grid .tile{animation:pageIn .28s ease both}
/* 底部分页栏 */
#pager{position:absolute;bottom:16px;left:50%;transform:translateX(-50%);z-index:6;
  display:flex;align-items:center;gap:4px;padding:5px;border-radius:999px;
  background:rgba(28,30,36,.88);backdrop-filter:blur(12px);border:1px solid var(--line)}
#pager button{width:38px;height:30px;border-radius:999px;color:var(--muted);font-size:17px;
  line-height:1;display:flex;align-items:center;justify-content:center}
#pager button:hover:not(:disabled){background:var(--panel2);color:var(--text)}
#pager button:disabled{opacity:.25;cursor:default}
#pager-info{font-size:12.5px;color:var(--muted);min-width:56px;text-align:center;
  display:flex;align-items:center;gap:5px}
#pager-goto{width:44px;height:26px;text-align:center;font-size:12.5px;color:var(--text);
  background:var(--panel);border:1px solid var(--line);border-radius:8px;font-family:inherit;
  padding:0}
#pager-goto:focus{outline:none;border-color:var(--accent-dim)}
#pager-total{min-width:24px;text-align:center}
/* 多选 */
.tile-check{position:absolute;top:6px;right:6px;z-index:3;width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.45);border:1.5px solid rgba(255,255,255,.85);color:#fff;
  display:none;align-items:center;justify-content:center;font-size:13px;line-height:1}
.tile:hover .tile-check,.tile.sel .tile-check{display:flex}
.tile.sel .tile-check{background:var(--accent);border-color:var(--accent)}
.tile.sel{outline:3px solid var(--accent);outline-offset:-3px}
.album.dragover{background:var(--accent-dim)!important;color:#fff!important}
#sel-bar{position:absolute;bottom:64px;left:50%;transform:translateX(-50%);z-index:7;
  display:flex;align-items:center;gap:10px;padding:7px 8px 7px 16px;border-radius:999px;
  background:rgba(28,30,36,.94);backdrop-filter:blur(12px);border:1px solid var(--line);
  font-size:13px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
#sel-bar[hidden]{display:none}
#sel-move{background:var(--accent-dim);color:#fff;padding:6px 14px;border-radius:999px;font-size:12.5px}
#sel-move:hover{background:var(--accent)}
#sel-clear{color:var(--muted);padding:6px 10px;font-size:12.5px}
/* 弹窗（移动 / 打开文件夹） */
.picker-modal{position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center}
.picker-modal[hidden]{display:none}
.mp-back{position:absolute;inset:0;background:rgba(0,0,0,.5)}
.mp-panel{position:relative;width:min(680px,94vw);background:var(--panel);
  border:1px solid var(--line);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.5);overflow:hidden}
.mp-title{padding:14px 16px 2px;font-weight:600;font-size:13px;color:var(--muted)}
#mp-input,#op-input{margin:10px 12px 4px;width:calc(100% - 24px);padding:10px 12px;border-radius:9px;
  border:1px solid var(--line);background:var(--bg);color:var(--text);outline:none;font-size:14px}
#mp-input:focus,#op-input:focus{border-color:var(--accent)}
#mp-list{max-height:300px;overflow-y:auto;padding:6px;margin:4px 6px}
.mp-item{padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13.5px;
  display:flex;justify-content:space-between;gap:8px}
.mp-item .ct{color:var(--muted);font-size:12px}
.mp-item.hl{background:var(--accent-dim);color:#fff}
.mp-item.hl .ct{color:rgba(255,255,255,.8)}
.mp-item.new{color:var(--accent)}
.mp-hint{padding:9px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}
/* 轻提示 */
#toast{position:fixed;top:64px;left:50%;transform:translateX(-50%);z-index:90;
  display:flex;align-items:center;gap:12px;padding:9px 10px 9px 16px;border-radius:999px;
  background:rgba(28,30,36,.94);backdrop-filter:blur(12px);border:1px solid var(--line);
  font-size:13px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
#toast[hidden]{display:none}
#toast-undo{color:var(--accent);font-weight:600;padding:4px 8px}
#toast-x{color:var(--muted);padding:4px 8px;font-size:14px}
/* 网格角标（视频 / 收藏） */
.tile-badge{position:absolute;bottom:6px;right:6px;z-index:3;width:24px;height:24px;border-radius:50%;
  background:rgba(0,0,0,.55);color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;
  pointer-events:none}
.tile-fav{position:absolute;top:6px;left:6px;z-index:3;font-size:14px;color:#ffcf4d;
  text-shadow:0 1px 3px rgba(0,0,0,.7);pointer-events:none}
/* 右键菜单 */
#ctx{position:fixed;z-index:95;min-width:180px;padding:5px;border-radius:11px;
  background:rgba(30,33,40,.97);backdrop-filter:blur(14px);border:1px solid var(--line);
  box-shadow:0 12px 40px rgba(0,0,0,.55)}
#ctx[hidden]{display:none}
#ctx button{display:flex;width:100%;text-align:left;padding:8px 12px;border-radius:8px;
  font-size:13px;color:var(--text);gap:8px}
#ctx button:hover{background:var(--panel2)}
#ctx button.danger{color:#ff7a7a}
#ctx hr{border:none;border-top:1px solid var(--line);margin:5px 8px}
/* 灯箱视频 */
#lb-video{max-width:100%;max-height:100%;position:absolute;border-radius:4px;
  background:#000;outline:none}
/* 胶片条 */
#lb-strip{position:absolute;left:50%;bottom:74px;transform:translateX(-50%);z-index:5;
  width:min(80vw,900px);overflow-x:auto;overflow-y:hidden;padding:6px 2px;
  display:flex;gap:6px;justify-content:flex-start;scrollbar-width:thin}
#lb-strip:empty{display:none}
.lb-thumb{flex:none;width:64px;height:64px;border-radius:7px;overflow:hidden;cursor:pointer;
  border:2px solid transparent;opacity:.55;transition:opacity .15s,border-color .15s;position:relative}
.lb-thumb img{width:100%;height:100%;object-fit:cover;display:block}
.lb-thumb:hover{opacity:.9}
.lb-thumb.active{opacity:1;border-color:var(--accent)}
/* Ken Burns 幻灯片缓推 */
@keyframes kenburns{0%{transform:scale(1) translate(0,0)}100%{transform:scale(1.12) translate(-1.5%,1.5%)}}
#lb-img.kb{animation:kenburns 7s ease-in-out infinite alternate}
/* 图片信息面板 */
#lb-info{position:absolute;top:0;right:0;bottom:0;z-index:8;width:280px;padding:20px 18px;
  background:rgba(24,26,32,.94);backdrop-filter:blur(14px);border-left:1px solid var(--line);
  overflow-y:auto;font-size:12.5px}
#lb-info[hidden]{display:none}
#lb-info h3{font-size:13px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
#lb-info h3 button{color:var(--muted);font-size:14px;padding:2px 6px}
#lb-info .row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
  border-bottom:1px solid rgba(255,255,255,.05)}
#lb-info .row .k{color:var(--muted);flex:none}
#lb-info .row .v{text-align:right;word-break:break-all;overflow:hidden;text-overflow:ellipsis}
/* 快捷键速查 */
.help-list{padding:6px 18px 16px;max-height:60vh;overflow-y:auto;font-size:13px}
.help-list .row{display:flex;justify-content:space-between;gap:16px;padding:5px 0;
  border-bottom:1px solid rgba(255,255,255,.05)}
.help-list kbd{font-family:inherit;background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:1px 8px;font-size:12px;color:var(--text);white-space:nowrap}
.help-list .d{color:var(--muted);text-align:right}
#btn-back{display:none;align-items:center;gap:4px;color:var(--muted);
  padding:5px 10px;border-radius:7px;font-size:13px}
#btn-back:hover{background:var(--panel2);color:var(--text)}
#btn-open,#btn-density,#btn-help{color:var(--muted);padding:5px 11px;border-radius:7px;font-size:12.5px;
  border:1px solid var(--line)}
#btn-help{padding:5px 10px}
#btn-open:hover,#btn-density:hover,#btn-help:hover{background:var(--panel2);color:var(--text);border-color:var(--accent-dim)}

/* ---------- 灯箱 ---------- */
#lightbox{position:fixed;inset:0;z-index:50;background:rgba(8,9,12,.97);
  display:flex;align-items:center;justify-content:center;
  user-select:none;-webkit-user-select:none}
#lightbox[hidden]{display:none}
#lb-stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  overflow:hidden;touch-action:none;cursor:grab}
#lb-stage.dragging{cursor:grabbing}
#lb-stage img{max-width:100%;max-height:100%;position:absolute;
  -webkit-user-drag:none;user-drag:none}
#lb-thumb{filter:blur(28px) saturate(1.1);transform:scale(1.06);
  opacity:0;transition:opacity .25s ease;pointer-events:none}
#lb-img{transform-origin:center center;will-change:transform}
#lb-img.anim{transition:transform .2s ease-out,opacity .2s ease}
#lb-caption{position:absolute;top:14px;left:16px;right:16px;display:flex;gap:12px;
  align-items:baseline;font-size:12.5px;color:rgba(255,255,255,.85);
  text-shadow:0 1px 4px #000;pointer-events:none;flex-wrap:wrap}
#lb-caption .path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb-arrow{position:absolute;top:50%;transform:translateY(-50%);z-index:5;
  width:52px;height:72px;font-size:40px;color:rgba(255,255,255,.75);
  background:rgba(255,255,255,.06);border-radius:12px;line-height:1;
  display:flex;align-items:center;justify-content:center;transition:background .15s}
.lb-arrow:hover{background:rgba(255,255,255,.16);color:#fff}
#lb-prev{left:18px}#lb-next{right:18px}
#lb-bar{position:absolute;bottom:18px;left:50%;transform:translateX(-50%);z-index:5;
  display:flex;gap:4px;padding:6px;border-radius:14px;
  background:rgba(28,30,36,.85);backdrop-filter:blur(12px);border:1px solid var(--line)}
#lb-bar button{padding:7px 13px;border-radius:9px;font-size:12.5px;color:var(--muted);
  white-space:nowrap}
#lb-bar button:hover{background:var(--panel2);color:var(--text)}
#lb-bar button.on{color:#fff;background:var(--accent-dim)}
/* 鼠标静止时自动隐藏灯箱界面元素，干净看图 */
#lb-bar,#lb-strip,.lb-arrow,#lb-caption{transition:opacity .3s ease}
#lightbox.idle #lb-bar,#lightbox.idle #lb-strip,
#lightbox.idle .lb-arrow,#lightbox.idle #lb-caption{opacity:0;pointer-events:none}
#lightbox.idle #lb-stage{cursor:none}
#lb-tip{position:absolute;bottom:20px;right:20px;font-size:11px;color:rgba(255,255,255,.4)}
@media (max-width:720px){
  #sidebar{position:absolute;z-index:20;height:100%}
  #lb-bar button{padding:7px 9px;font-size:12px}
  #lb-tip{display:none}
}
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="sb-head">
      <div class="sb-title">🖼 imgbrowse</div>
      <div class="sb-sub" id="sb-root">…</div>
      <input id="album-search" type="search" placeholder="过滤相册…">
    </div>
    <nav id="album-list"></nav>
  </aside>
  <main>
    <header id="topbar">
      <button id="btn-sidebar" title="侧栏 (b)">☰</button>
      <button id="btn-open" title="打开其他文件夹">📁 打开文件夹</button>
      <button id="btn-back" title="返回相册总览">‹ 相册总览</button>
      <span id="cur-name">相册总览</span>
      <span id="cur-count"></span>
      <span style="flex:1"></span>
      <button id="btn-density" title="切换网格密度">▦ 标准</button>
      <button id="btn-help" title="快捷键帮助 (?)">？</button>
    </header>
    <div id="grid"></div>
    <div id="pager">
      <button id="pager-prev" title="上一页 (← / ↑)">‹</button>
      <span id="pager-info"><input id="pager-goto" inputmode="numeric"
        value="1" title="输入页码后回车跳转"><span class="pager-sep">/</span><span id="pager-total">1</span></span>
      <button id="pager-next" title="下一页 (→ / ↓ / 空格)">›</button>
    </div>
    <div id="sel-bar" hidden>
      <span id="sel-count">已选 0 张</span>
      <button id="sel-move">📂 移动到…</button>
      <button id="sel-clear">取消</button>
    </div>
  </main>
</div>

<div id="lightbox" hidden>
  <div id="lb-stage">
    <img id="lb-thumb" alt="">
    <img id="lb-img" class="anim" alt="" draggable="false">
    <video id="lb-video" playsinline controls hidden></video>
  </div>
  <div id="lb-caption"><span class="path"></span><span class="meta"></span></div>
  <button id="lb-prev" class="lb-arrow" title="上一张 (←)">‹</button>
  <button id="lb-next" class="lb-arrow" title="下一张 (→)">›</button>
  <div id="lb-strip"></div>
  <div id="lb-bar">
    <button data-act="prev" title="←">‹ 上一张</button>
    <button data-act="next" title="→">下一张 ›</button>
    <button data-act="fav" title="收藏 (.)">☆ 收藏</button>
    <button data-act="move" title="移动到其他文件夹 (M)">📂 移动</button>
    <button data-act="del" title="移到废纸篓 (Delete)">🗑 删除</button>
    <button data-act="info" title="图片信息 (I)">ℹ 信息</button>
    <button data-act="fit" title="双击图片也可切换">1:1</button>
    <button data-act="raw">原图</button>
    <button data-act="reveal">Finder</button>
    <button data-act="speed" title="幻灯片速度">⏱ 3s</button>
    <button data-act="shuffle" title="随机顺序">🔀</button>
    <button data-act="slide" title="幻灯片 (S)">▶ 幻灯片</button>
    <button data-act="close" title="Esc">✕</button>
  </div>
  <div id="lb-info" hidden>
    <h3>图片信息 <button id="lbi-close" title="关闭 (I)">✕</button></h3>
    <div id="lbi-body"></div>
  </div>
</div>

<div id="move-picker" class="picker-modal" hidden>
  <div class="mp-back"></div>
  <div class="mp-panel">
    <div class="mp-title">移动图片到…</div>
    <input id="mp-input" type="text" placeholder="搜索文件夹，或输入新文件夹名后回车">
    <div id="mp-list"></div>
    <div class="mp-hint">↑↓ 选择 · Enter 确认 · Esc 取消 · 输入不存在的名字即新建文件夹</div>
  </div>
</div>

<div id="open-picker" class="picker-modal" hidden>
  <div class="mp-back"></div>
  <div class="mp-panel">
    <div class="mp-title">打开图片文件夹</div>
    <input id="op-input" type="text" placeholder="粘贴或输入文件夹的完整路径，如 /Users/你的用户名/Pictures">
    <div class="mp-hint">
      <button id="op-browse" style="color:var(--accent);cursor:pointer">💻 用系统对话框选择…</button>
      &nbsp;·&nbsp; Enter 打开 · Esc 取消
    </div>
  </div>
</div>

<div id="help-picker" class="picker-modal" hidden>
  <div class="mp-back"></div>
  <div class="mp-panel" style="width:min(560px,94vw)">
    <div class="mp-title">快捷键速查</div>
    <div class="help-list" id="help-list"></div>
  </div>
</div>

<div id="ctx" hidden></div>

<div id="toast" hidden>
  <span id="toast-msg"></span>
  <button id="toast-undo">撤销</button>
  <button id="toast-x">✕</button>
</div>

<script>
const $ = s => document.querySelector(s);
let META = null, IMGS = [];
let mode = 'albums';            // 'albums' 封面总览 | 'photos' 相册内网格 | 'fav' 收藏
let album = '';                 // photos 模式下的相册名（根目录为 ''）
let view = [];                  // 当前网格/灯箱遍历的全局 id 列表
let pos = -1;                   // 灯箱在 view 中的位置
let scale = 1, tx = 0, ty = 0;
let curGid = -1, wantActual = false, slideTimer = null;
let navDir = 0;                 // 灯箱翻页方向（用于滑入动画）
let favSet = new Set();         // 收藏的图片 id
let coverMap = {};              // 相册名 -> 封面 id
let slideSpeed = 3000, slideShuffle = false, slideOrder = [];
let anchorGid = -1;             // Shift 连选的锚点

// 网格密度三档：minCard
const DENSITY = [
  {key: 'compact', label: '紧凑', min: 96},
  {key: 'normal', label: '标准', min: 140},
  {key: 'large', label: '大图', min: 210},
];
let density = 1;

const pref = {
  get(k, d) { try { const v = localStorage.getItem('imgbrowse:' + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem('imgbrowse:' + k, JSON.stringify(v)); } catch (_) {} },
};

const fmtSize = n => n >= 1048576 ? (n/1048576).toFixed(1)+' MB'
                   : Math.max(1, Math.round(n/1024)) + ' KB';
const isVideo = gid => IMGS[gid] && IMGS[gid].type === 'video';
// v= 文件指纹：切换文件夹/重排/移动后同 id 指向不同文件时，URL 变化，缓存自动失效
const imgUrl = (id, t) => {
  const k = (IMGS[id] && IMGS[id].k) ? '&v=' + IMGS[id].k : '';
  return `/img?id=${id}&t=${t}${k}`;
};

function applyState(st) {
  favSet = new Set(st.favIds || []);
  coverMap = st.covers || {};
  META.albums.forEach(a => { if (coverMap[a.name] != null) a.cover = coverMap[a.name]; });
}

// ------------------------------------------------------------ 初始化
const albumHash = n => '#a/' + encodeURIComponent(n || '__root__');

async function init() {
  density = DENSITY.findIndex(d => d.key === pref.get('density', 'normal'));
  if (density < 0) density = 1;
  const [meta, imgs, st] = await Promise.all([
    fetch('/api/meta').then(r => r.json()),
    fetch('/api/images').then(r => r.json()),
    fetch('/api/state').then(r => r.json()).catch(() => ({favIds: [], covers: {}})),
  ]);
  META = meta; IMGS = imgs;
  applyState(st);
  $('#sb-root').textContent = META.root;
  buildSidebar();
  bindUI();
  updateDensityBtn();

  // hash 恢复：#<id> 直接打开灯箱；#a/<相册> 定位到相册；否则按记忆或回封面墙
  const h = location.hash;
  const pm = h.match(/^#(\d+)$/);
  if (pm && IMGS[+pm[1]]) {
    enterAlbum(IMGS[+pm[1]].album, false);
    openAt(+pm[1]);
  } else {
    const am = h.match(/^#a\/(.+)$/);
    const fav = h === '#fav';
    if (fav) {
      showFavorites(false);
    } else if (am) {
      const n = decodeURIComponent(am[1]);
      enterAlbum(n === '__root__' ? '' : n, false);
    } else {
      const mem = pref.get('loc:' + META.root, null);
      if (mem && mem.album !== undefined && IMGS.some(r => r.album === mem.album)) {
        enterAlbum(mem.album, false, mem.page);
      } else {
        landDefault(false);
      }
    }
  }
  // 空库（.app 首次启动 / 选择的目录没有图片）：自动弹出选文件夹框
  if (!META.total) openFolderPicker();
}

// 记住每个相册看到的页
function rememberLocation() {
  if (mode === 'photos') pref.set('loc:' + META.root, {album, page: pager.page});
}

function buildSidebar() {
  const list = $('#album-list');
  list.innerHTML = '';
  const mk = (name, label, count, onClick) => {
    const b = document.createElement('div');
    b.className = 'album';
    b.dataset.album = name;
    b.innerHTML = `<span class="nm"></span><span class="ct">${count}</span>`;
    b.querySelector('.nm').textContent = label;
    b.onclick = onClick;
    const realAlbum = !name.startsWith('__');
    // 重命名（真实相册才有）
    if (realAlbum && name !== '') {
      const rn = document.createElement('button');
      rn.className = 'album-rename';
      rn.textContent = '✎';
      rn.title = '重命名文件夹';
      rn.onclick = e => {
        e.stopPropagation();
        const nm = b.querySelector('.nm');
        startRename(name, nm,
          v => renameAlbum(name, v),
          () => buildSidebar());
      };
      b.insertBefore(rn, b.querySelector('.ct'));   // 铅笔放在张数数字前面
    }
    // 拖拽图片到相册名上 → 移动
    if (realAlbum && name !== '') {
      b.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        b.classList.add('dragover');
      });
      b.addEventListener('dragleave', () => b.classList.remove('dragover'));
      b.addEventListener('drop', e => {
        e.preventDefault();
        b.classList.remove('dragover');
        try {
          const ids = JSON.parse(e.dataTransfer.getData('text/plain') || '[]');
          if (ids.length) doMove(ids, name);
        } catch (_) {}
      });
    }
    list.appendChild(b);
  };
  mk('__overview__', '🖼 相册总览', META.albums.length, () => showOverview());
  mk('__fav__', '⭐ 我的收藏', favSet.size, () => showFavorites());
  META.albums.forEach(a =>
    mk(a.name, a.label, a.count, () => enterAlbum(a.name)));
  syncSidebar();

  $('#album-search').oninput = e => {
    const q = e.target.value.trim().toLowerCase();
    list.querySelectorAll('.album').forEach(el => {
      if (el.dataset.album === '__overview__') {
        el.style.display = q ? 'none' : '';   // 搜索时隐藏"总览"项
      } else {
        el.style.display = (!q || el.dataset.album.toLowerCase().includes(q)) ? '' : 'none';
      }
    });
  };
}

function syncSidebar() {
  document.querySelectorAll('.album').forEach(el => {
    const da = el.dataset.album;
    const on = mode === 'albums' ? da === '__overview__'
             : mode === 'fav'    ? da === '__fav__'
             : da === album;
    el.classList.toggle('active', on);
  });
}

// 落地页：有子文件夹相册 → 封面墙；全是散图 → 直接进图片网格
function landDefault(rewrite = true) {
  const hasSub = META.albums.some(a => a.name !== '');
  const hasRoot = META.albums.some(a => a.name === '');
  if (!hasSub && hasRoot) enterAlbum('', rewrite);
  else showOverview(rewrite);
}

// 封面墙（默认首页）
function showOverview(rewrite = true) {
  mode = 'albums';
  const grid = $('#grid');
  grid.classList.add('albums');
  $('#btn-back').style.display = 'none';
  $('#cur-name').textContent = '相册总览';
  $('#cur-count').textContent =
    `${META.albums.length} 个相册 · 共 ${IMGS.length} 张图片`;
  syncSidebar();
  pager.items = META.albums.slice();
  pager.goto(0);
  if (rewrite) history.replaceState(null, '', '#');
}

// 收藏视图
function showFavorites(rewrite = true) {
  mode = 'fav';
  view = [...favSet].sort((a, b) =>
    (IMGS[a].album + IMGS[a].name).localeCompare(IMGS[b].album + IMGS[b].name, 'zh'));
  const grid = $('#grid');
  grid.classList.remove('albums');
  $('#btn-back').style.display = '';
  $('#cur-name').textContent = '⭐ 我的收藏';
  $('#cur-count').textContent = `共 ${view.length} 张`;
  syncSidebar();
  pager.items = view.slice();
  pager.goto(0);
  if (rewrite) history.replaceState(null, '', '#fav');
}

// 进入某个相册
function enterAlbum(name, rewrite = true, page = 0) {
  mode = 'photos';
  album = name;
  const grid = $('#grid');
  grid.classList.remove('albums');
  $('#btn-back').style.display = '';
  view = IMGS.filter(r => r.album === album)
             .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
             .map(r => r.id);
  $('#cur-name').textContent = album === '' ? '（根目录）' : album;
  $('#cur-count').textContent = `共 ${view.length} 张`;
  syncSidebar();
  pager.items = view.slice();
  pager.goto(page);
  if (rewrite) history.replaceState(null, '', albumHash(album));
  rememberLocation();
}

// ------------------------------------------------------------ 网格（分页）
const io = 'IntersectionObserver' in window
  ? new IntersectionObserver(es => es.forEach(en => {
      if (en.isIntersecting) {
        const im = en.target;
        im.src = im.dataset.src;
        io.unobserve(im);
      }
    }), {rootMargin: '600px'})
  : null;

const pager = {
  items: [], page: 0, cols: 4, perPage: 24,
};

const selection = new Set();   // 选中的全局图片 id

function makePhotoTile(gid, idx) {
  const it = IMGS[gid];
  const tile = document.createElement('div');
  tile.className = 'tile' + (selection.has(gid) ? ' sel' : '');
  tile.title = it.name;
  tile.draggable = true;
  tile.dataset.gid = gid;
  const im = document.createElement('img');
  im.alt = it.name;
  im.draggable = false;
  im.dataset.src = imgUrl(gid, 'grid');
  im.onload = () => im.classList.add('loaded');
  const ck = document.createElement('button');
  ck.className = 'tile-check';
  ck.textContent = '✓';
  ck.title = '选择';
  ck.onclick = e => { e.stopPropagation(); toggleSelect(gid); };
  const lb = document.createElement('span');
  lb.className = 'tile-label';
  lb.textContent = it.name;
  tile.append(im, ck, lb);
  if (favSet.has(gid)) {
    const f = document.createElement('span');
    f.className = 'tile-fav'; f.textContent = '⭐';
    tile.appendChild(f);
  }
  if (isVideo(gid)) {
    const v = document.createElement('span');
    v.className = 'tile-badge'; v.textContent = '▶';
    tile.appendChild(v);
  }
  tile.onclick = e => {
    if (e.shiftKey && anchorGid >= 0) {            // Shift 连选
      const a = pager.items.indexOf(anchorGid), b = pager.items.indexOf(gid);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        pager.items.slice(lo, hi + 1).forEach(g => selection.add(g));
        refreshSelUI();
        return;
      }
    }
    anchorGid = gid;
    if (selection.size) { toggleSelect(gid); return; }  // 选择模式下点击=勾选
    openLightbox(idx);
  };
  tile.addEventListener('contextmenu', e => {
    e.preventDefault();
    if (!selection.has(gid)) { selection.clear(); selection.add(gid); refreshSelUI(); }
    showPhotoMenu(e.clientX, e.clientY, [...selection]);
  });
  tile.addEventListener('dragstart', e => {
    if (!selection.has(gid)) { selection.clear(); selection.add(gid); refreshSelUI(); }
    e.dataTransfer.setData('text/plain', JSON.stringify([...selection]));
    e.dataTransfer.effectAllowed = 'move';
  });
  tile.addEventListener('dragend', () => {
    document.querySelectorAll('.album.dragover')
      .forEach(el => el.classList.remove('dragover'));
  });
  if (io) io.observe(im); else im.src = im.dataset.src;
  return tile;
}

function refreshSelUI() {
  document.querySelectorAll('#grid .tile').forEach(t =>
    t.classList.toggle('sel', selection.has(+t.dataset.gid)));
  const bar = $('#sel-bar');
  bar.hidden = selection.size === 0;
  $('#sel-count').textContent = `已选 ${selection.size} 张`;
}

function toggleSelect(gid, on) {
  const add = on === undefined ? !selection.has(gid) : on;
  if (add) selection.add(gid); else selection.delete(gid);
  refreshSelUI();
}

function clearSelection() {
  selection.clear();
  document.querySelectorAll('#grid .tile.sel').forEach(t => t.classList.remove('sel'));
  $('#sel-bar').hidden = true;
}

function makeAlbumCard(a) {
  const card = document.createElement('div');
  card.className = 'tile album-card';
  const im = document.createElement('img');
  im.alt = a.label;
  im.dataset.src = imgUrl(a.cover, 'cover');
  im.onload = () => im.classList.add('loaded');
  const lb = document.createElement('span');
  lb.className = 'tile-label';
  lb.innerHTML = `<span class="nm"></span><span class="ct">${a.count} 张</span>`;
  lb.querySelector('.nm').textContent = a.label;
  card.append(im, lb);
  card.onclick = () => enterAlbum(a.name);
  if (a.name !== '') {
    const rn = document.createElement('button');
    rn.className = 'card-rename';
    rn.textContent = '✎';
    rn.title = '重命名文件夹';
    rn.onclick = e => {
      e.stopPropagation();
      const nm = card.querySelector('.tile-label .nm');
      startRename(a.name, nm,
        v => renameAlbum(a.name, v),
        () => pager.goto(pager.page));   // 取消：重绘当前页恢复
    };
    card.appendChild(rn);
  }
  if (io) io.observe(im); else im.src = im.dataset.src;
  return card;
}

// 按窗口尺寸算每页能放多少张正方形卡片
function calcPageSize() {
  const grid = $('#grid');
  const gap = mode === 'albums' ? 14 : 8;
  const minCard = mode === 'albums' ? 200 : DENSITY[density].min;
  const W = grid.clientWidth - 20;   // 去掉 padding
  const H = grid.clientHeight - 20;
  pager.cols = Math.max(1, Math.floor((W + gap) / (minCard + gap)));
  const cardW = (W - (pager.cols - 1) * gap) / pager.cols;  // 正方形：宽=高
  const rows = Math.max(1, Math.floor((H + gap) / (cardW + gap)));
  pager.perPage = pager.cols * rows;
}

function renderPage() {
  const grid = $('#grid');
  grid.innerHTML = '';
  if (!pager.items.length) {
    const msg = mode === 'albums'
      ? '还没有相册 — 点左上角「📁 打开文件夹」选择图片目录'
      : (mode === 'fav' ? '还没有收藏 — 灯箱里按 . 或点 ★ 收藏图片'
                        : '这里没有图片');
    grid.innerHTML = '<div id="empty"></div>';
    $('#empty').textContent = msg;
    updatePagerUI();
    return;
  }
  grid.style.gridTemplateColumns = `repeat(${pager.cols},1fr)`;
  const start = pager.page * pager.perPage;
  const frag = document.createDocumentFragment();
  let i = 0;
  for (const item of pager.items.slice(start, start + pager.perPage)) {
    frag.appendChild(mode === 'albums'
      ? makeAlbumCard(item)
      : makePhotoTile(item, start + i));
    i++;
  }
  grid.appendChild(frag);
  // 预加载下一页缩略图，翻页时零等待
  pager.items.slice(start + pager.perPage, start + 2 * pager.perPage)
    .forEach(item => {
      const gid = mode === 'albums' ? item.cover : item;
      const tier = mode === 'albums' ? 'cover' : 'grid';
      new Image().src = imgUrl(gid, tier);
    });
  updatePagerUI();
}

function updatePagerUI() {
  const total = pager.items.length;
  const pages = Math.max(1, Math.ceil(total / pager.perPage));
  $('#pager').style.display = total > pager.perPage ? '' : 'none';
  const goto = $('#pager-goto');
  if (document.activeElement !== goto) goto.value = pager.page + 1;
  $('#pager-total').textContent = pages;
  $('#pager-prev').disabled = pager.page === 0;
  $('#pager-next').disabled = pager.page >= pages - 1;
}

pager.goto = function (p) {
  calcPageSize();
  const pages = Math.max(1, Math.ceil(this.items.length / this.perPage));
  this.page = Math.min(Math.max(0, p), pages - 1);
  renderPage();
};
pager.turn = function (d) {
  const pages = Math.max(1, Math.ceil(this.items.length / this.perPage));
  const np = this.page + d;
  if (np < 0 || np >= pages) return;
  this.page = np;
  renderPage();
};

// 窗口尺寸变化时重排（防抖）
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { calcPageSize(); pager.goto(pager.page); }, 200);
});

// 滚轮 / 触控板上下滑动 → 翻页
// 连续事件（含触控板惯性）视为一次手势，每手势最多翻一页；
// 事件停止 200ms 后才算手势结束、重新就绪，避免惯性导致连翻两页。
const gridEl = $('#grid');
let wheelAcc = 0, wheelArmed = true, wheelQuietTimer = null;
gridEl.addEventListener('wheel', e => {
  if (lb.hidden === false) return;
  e.preventDefault();
  clearTimeout(wheelQuietTimer);
  wheelQuietTimer = setTimeout(() => { wheelAcc = 0; wheelArmed = true; }, 200);
  if (!wheelArmed) return;
  wheelAcc += e.deltaY;
  if (Math.abs(wheelAcc) > 60) {
    pager.turn(wheelAcc > 0 ? 1 : -1);
    wheelArmed = false;
    wheelAcc = 0;
  }
}, {passive: false});

// 触屏上下滑动
let touchY = null;
gridEl.addEventListener('touchstart', e => { touchY = e.touches[0].clientY; },
  {passive: true});
gridEl.addEventListener('touchend', e => {
  if (touchY === null) return;
  const dy = touchY - e.changedTouches[0].clientY;
  if (Math.abs(dy) > 50) pager.turn(dy > 0 ? 1 : -1);
  touchY = null;
}, {passive: true});

// ------------------------------------------------------------ 移动图片
let mpIds = [], mpIdx = 0, mpItems = [], lastMove = null, toastTimer = null;

function openMovePicker(ids) {
  mpIds = ids.slice();
  $('.mp-title').textContent = `移动 ${ids.length} 张图片到…`;
  $('#move-picker').hidden = false;
  const input = $('#mp-input');
  input.value = '';
  mpIdx = 0;
  renderMpList('');
  input.focus();
}
function closeMovePicker() { $('#move-picker').hidden = true; }

function renderMpList(q) {
  const list = $('#mp-list');
  list.innerHTML = '';
  const items = META.albums
    .filter(a => a.name !== '' && a.label.toLowerCase().includes(q.toLowerCase()));
  const exact = items.some(a => a.label.toLowerCase() === q.toLowerCase());
  const canNew = q.trim() && !exact && !/[/\\]/.test(q);
  mpItems = [];
  if (canNew) mpItems.push({dest: q.trim(), label: `＋ 新建文件夹「${q.trim()}」`, isNew: true});
  items.forEach(a => mpItems.push({dest: a.name, label: a.label, count: a.count}));
  if (!mpItems.length)
    mpItems.push({dest: null, label: '（无匹配文件夹，输入名称即可新建）', disabled: true});
  mpIdx = Math.min(mpIdx, mpItems.length - 1);
  const all = mpItems;
  all.forEach((it, i) => {
    const d = document.createElement('div');
    d.className = 'mp-item' + (i === mpIdx ? ' hl' : '') + (it.isNew ? ' new' : '');
    d.innerHTML = `<span class="nm"></span>${it.count != null ? `<span class="ct">${it.count} 张</span>` : ''}`;
    d.querySelector('.nm').textContent = it.label;
    if (!it.disabled) {
      d.onmouseenter = () => { mpIdx = i; renderMpList(q); };
      d.onclick = () => confirmMove(it.dest);
    }
    list.appendChild(d);
  });
}

function confirmMove(dest) {
  if (!dest) return;
  closeMovePicker();
  doMove(mpIds, dest);
}

async function doMove(ids, dest) {
  const source = mode === 'photos' ? album : (IMGS[ids[0]] ? IMGS[ids[0]].album : '');
  const todo = ids.filter(g => IMGS[g] && IMGS[g].album !== dest);
  if (!todo.length) { showToast('图片已经在这个文件夹里', false); return; }
  try {
    const r = await fetch('/api/move', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: todo, dest})});
    const data = await r.json();
    if (!data.ok) { showToast('移动失败：' + (data.error || '未知错误'), false); return; }
    data.moved.forEach(m => Object.assign(IMGS[m.id], m));
    META.albums = data.albums;
    if (data.state) applyState(data.state);
    lastMove = {ids: todo.slice(), source, dest};
    clearSelection();
    refreshView();
    showToast(`已移动 ${data.moved.length} 张到「${dest}」`, true);
    // 相册内灯箱连续归档：当前图移走后，原位显示下一张；收藏视图跨相册不移除
    if (!lb.hidden && mode === 'photos') {
      view = view.filter(g => IMGS[g].album === source);
      if (!view.length) closeLightbox();
      else { pos = Math.min(pos, view.length - 1); navDir = 0; show(); }
    }
  } catch (e) {
    showToast('移动失败：' + e, false);
  }
}

function refreshAfterMove() { refreshView(); }

// ------------------------------------------------------------ 切换浏览目录
function openFolderPicker() {
  $('#open-picker').hidden = false;
  const input = $('#op-input');
  input.value = META ? META.root : '';
  input.focus();
  input.select();
}
function closeFolderPicker() { $('#open-picker').hidden = true; }

async function pickFolderNative() {
  showToast('系统文件夹选择框已弹出，请在屏幕上选择…', false);
  try {
    const r = await fetch('/api/pick-folder', {method: 'POST'});
    const d = await r.json();
    if (d.ok && d.path) {
      $('#op-input').value = d.path;
      await applyRoot(d.path);
    }
  } catch (e) {
    showToast('调用系统选择框失败：' + e, false);
  }
}

async function applyRoot(path) {
  try {
    const r = await fetch('/api/set-root', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path})});
    const d = await r.json();
    if (!d.ok) { showToast('打开失败：' + (d.error || '未知错误'), false); return; }
    IMGS = await fetch('/api/images').then(x => x.json());
    META = {root: d.root, total: d.total, albums: d.albums};
    const st = await fetch('/api/state').then(x => x.json())
                                        .catch(() => null);
    if (st) applyState(st);
    $('#sb-root').textContent = META.root;
    clearSelection();
    if (!lb.hidden) closeLightbox();
    closeFolderPicker();
    buildSidebar();
    landDefault(false);
    showToast(`已加载：${d.root}（${d.albums.length} 个相册 / ${d.total} 张）`, false);
  } catch (e) {
    showToast('打开失败：' + e, false);
  }
}

// ------------------------------------------------------------ 重命名相册
function startRename(currentName, anchorEl, onCommit, onCancel) {
  const input = document.createElement('input');
  input.className = 'album-edit';
  input.value = currentName;
  anchorEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = commit => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (commit && v && v !== currentName) onCommit(v);
    else onCancel();
  };
  input.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('click', e => e.stopPropagation());
}

async function renameAlbum(from, to) {
  try {
    const r = await fetch('/api/rename-album', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({from, to})});
    const data = await r.json();
    if (!data.ok) { showToast('重命名失败：' + (data.error || '未知错误'), false); return; }
    if (!lb.hidden) closeLightbox();   // 重命名触发重扫，id 会重排
    IMGS = data.images;
    META.albums = data.albums;
    if (data.state) applyState(data.state);
    if (mode === 'photos' && album === from) album = to;
    refreshView();
    if (mode === 'photos') history.replaceState(null, '', albumHash(album));
    showToast(`已重命名为「${to}」`, false);
  } catch (e) {
    showToast('重命名失败：' + e, false);
  }
}

function showToast(msg, canUndo) {
  $('#toast-msg').textContent = msg;
  $('#toast-undo').style.display = canUndo ? '' : 'none';
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000);
}

// ------------------------------------------------------------ 灯箱
const lb = $('#lightbox'), stage = $('#lb-stage'),
      lbImg = $('#lb-img'), lbThumb = $('#lb-thumb'),
      lbVideo = $('#lb-video'), lbStrip = $('#lb-strip');

// 鼠标静止 2.2s 自动隐藏工具栏/胶片条/箭头/标题，干净看图；一动即恢复
let idleTimer = null;
function scheduleIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    // 鼠标正停在工具栏/胶片条上，或信息面板开着时，保持显示
    if ($('#lb-bar:hover') || $('#lb-strip:hover') || !$('#lb-info').hidden) {
      scheduleIdle();
      return;
    }
    lb.classList.add('idle');
  }, 2200);
}
function pokeChrome() {
  if (lb.hidden) return;
  lb.classList.remove('idle');
  scheduleIdle();
}
lb.addEventListener('mousemove', pokeChrome);

function openLightbox(idx) {
  pos = idx;
  navDir = 0;
  lb.hidden = false;
  document.body.style.overflow = 'hidden';
  buildStrip();
  show();
  pokeChrome();
}
function closeLightbox() {
  lb.hidden = true;
  document.body.style.overflow = '';
  clearTimeout(idleTimer);
  lb.classList.remove('idle');
  stopSlide();
  pauseVideo();
  $('#lb-info').hidden = true;
  lbStrip.innerHTML = '';
  history.replaceState(null, '',
    mode === 'photos' ? albumHash(album) : (mode === 'fav' ? '#fav' : '#'));
}

function pauseVideo() {
  lbVideo.pause();
  lbVideo.removeAttribute('src');
  lbVideo.load();
}

// 胶片条：打开灯箱时建一次，翻页只做高亮 + 滚动
function buildStrip() {
  lbStrip.innerHTML = '';
  const frag = document.createDocumentFragment();
  view.forEach((gid, i) => {
    const d = document.createElement('div');
    d.className = 'lb-thumb' + (i === pos ? ' active' : '');
    const im = document.createElement('img');
    im.loading = 'lazy';
    im.alt = '';
    im.src = imgUrl(gid, 'thumb');
    d.appendChild(im);
    if (isVideo(gid)) {
      const b = document.createElement('span');
      b.className = 'tile-badge';
      b.textContent = '▶';
      b.style.inset = 'auto 2px 2px auto';
      d.appendChild(b);
    }
    d.onclick = () => {
      if (i === pos) return;
      navDir = i > pos ? 1 : -1;
      pos = i;
      show();
    };
    frag.appendChild(d);
  });
  lbStrip.appendChild(frag);
}
function syncStrip() {
  const ths = lbStrip.children;
  for (let i = 0; i < ths.length; i++) {
    ths[i].classList.toggle('active', i === pos);
    if (i === pos)
      ths[i].scrollIntoView({block: 'nearest', inline: 'center',
                             behavior: 'smooth'});
  }
}

function show() {
  const gid = view[pos];
  curGid = gid;
  const it = IMGS[gid];
  const video = isVideo(gid);
  $('#lb-caption .path').textContent =
    (it.album ? it.album + ' / ' : '') + it.name;
  $('#lb-caption .meta').textContent =
    `${pos + 1} / ${view.length} · ${fmtSize(it.size)}`;
  resetZoom(false);
  wantActual = false;
  updateFavBtn();

  if (video) {
    // 视频：隐藏图片层，<video> 直接播放原文件（screen 层即流式输出原文件）
    lbImg.hidden = true;
    lbThumb.hidden = true;
    lbVideo.hidden = false;
    lbVideo.src = imgUrl(gid, 'screen');
    lbVideo.play().catch(() => {});
    slideAnim(lbVideo);
  } else {
    if (!lbVideo.hidden) { pauseVideo(); lbVideo.hidden = true; }
    lbImg.hidden = false;
    lbThumb.hidden = false;
    lbImg.classList.remove('kb');
    lbImg.dataset.kind = 'screen';

    // blur-up：先显示已缓存的缩略图，screen 图到了淡入
    lbThumb.src = imgUrl(gid, 'thumb');
    lbThumb.style.opacity = '1';
    lbImg.style.opacity = '0';
    const pre = new Image();
    pre.onload = () => {
      if (curGid !== gid) return;      // 翻页太快，丢弃旧图
      if (lbImg.dataset.kind === 'raw') return;  // raw 已先加载，不回退
      lbImg.src = pre.src;
      lbImg.dataset.kind = 'screen';
      lbImg.style.opacity = '1';
      lbThumb.style.opacity = '0';
      if (wantActual) { wantActual = false; doActual(); }
      if (slideTimer) lbImg.classList.add('kb');   // 幻灯片 Ken Burns 缓推
      slideAnim(lbImg);
    };
    pre.src = imgUrl(gid, 'screen');
  }

  history.replaceState(null, '', '#' + gid);
  // 预加载前后各 2 张（视频不预载，避免抢带宽）
  [-2, -1, 1, 2].forEach(d => {
    const n = pos + d;
    if (n >= 0 && n < view.length && !isVideo(view[n]))
      new Image().src = imgUrl(view[n], 'screen');
  });
  $('#lb-prev').style.visibility = pos === 0 ? 'hidden' : '';
  $('#lb-next').style.visibility = pos === view.length - 1 ? 'hidden' : '';
  syncStrip();
  if (!$('#lb-info').hidden) loadInfo();
}

// 翻页方向滑入动画
function slideAnim(el) {
  if (!navDir) return;
  const dx = navDir > 0 ? 70 : -70;
  el.animate(
    [{transform: `translateX(${dx}px)`}, {transform: 'translateX(0)'}],
    {duration: 240, easing: 'cubic-bezier(.22,.61,.36,1)'});
}

function nav(d) {
  const n = pos + d;
  if (n < 0 || n >= view.length) return;
  navDir = d;
  pos = n;
  show();
}

function openAt(gid) {
  const idx = view.indexOf(gid);
  if (idx >= 0) openLightbox(idx);
}

// ------------------------------------------------------------ 缩放 / 平移
function resetZoom(anim = true) {
  scale = 1; tx = 0; ty = 0;
  lbImg.classList.toggle('anim', anim);
  applyTransform();
}
function applyTransform() {
  lbImg.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
}
function zoomAt(clientX, clientY, factor) {
  const r = lbImg.getBoundingClientRect();
  const cx = clientX - (r.left + r.width / 2);
  const cy = clientY - (r.top + r.height / 2);
  const ns = Math.min(10, Math.max(0.1, scale * factor));
  const k = ns / scale;
  tx = cx - (cx - tx) * k;
  ty = cy - (cy - ty) * k;
  scale = ns;
  lbImg.classList.remove('anim');
  applyTransform();
  if (scale > 1.6 && lbImg.dataset.kind === 'screen') loadRaw(false);
}
function loadRaw(andActual) {
  if (lbImg.dataset.kind === 'raw' && !andActual) return;
  const gid = curGid;
  const im = new Image();
  im.onload = () => {
    if (curGid !== gid) return;
    lbImg.src = im.src;
    lbImg.dataset.kind = 'raw';
    if (andActual) { wantActual = false; doActual(); }
  };
  im.src = imgUrl(gid, 'raw');
}
function doActual() {
  // 以当前窗口布局为 1x，放大到原图物理像素
  lbImg.classList.add('anim');
  const r = lbImg.getBoundingClientRect();
  const w = lbImg.naturalWidth || r.width;
  scale = w / r.width;              // fit(1x) → 原图物理像素
  tx = 0; ty = 0;
  applyTransform();
}
function toggleActual() {
  if (!lbVideo.hidden) return;
  if (scale > 1.05) {
    resetZoom(true);
    lbImg.dataset.kind = 'screen';
    lbImg.src = imgUrl(curGid, 'screen');
  } else {
    wantActual = true;
    if (lbImg.dataset.kind === 'raw') doActual();
    else loadRaw(true);
  }
}

// 指针拖拽平移
let dragging = false, sx = 0, sy = 0, moved = false;
stage.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  if (e.target === lbVideo) return;   // 视频交给原生控件，不抓取拖拽
  dragging = true; moved = false;
  sx = e.clientX - tx; sy = e.clientY - ty;
  stage.classList.add('dragging');
  lbImg.classList.remove('anim');
  stage.setPointerCapture(e.pointerId);
});
stage.addEventListener('pointermove', e => {
  if (!dragging) return;
  const ntx = e.clientX - sx, nty = e.clientY - sy;
  if (Math.abs(ntx - tx) + Math.abs(nty - ty) > 3) moved = true;
  tx = ntx; ty = nty;
  applyTransform();
});
stage.addEventListener('pointerup', e => {
  dragging = false;
  stage.classList.remove('dragging');
});
stage.addEventListener('wheel', e => {
  if (!lbVideo.hidden) return;   // 视频用原生控件，不缩放
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.18 : 1 / 1.18);
}, {passive: false});
lbImg.addEventListener('dblclick', e => { e.stopPropagation(); toggleActual(); });
stage.addEventListener('dblclick', e => {
  if (lbVideo.hidden && (e.target === stage || e.target === lbThumb)) toggleActual();
});
// 单击背景关闭（拖拽过的不算）；界面处于自动隐藏状态时，先唤醒界面不关闭
stage.addEventListener('click', e => {
  if (lb.classList.contains('idle')) { pokeChrome(); return; }
  if (!moved && (e.target === stage || e.target === lbThumb)) closeLightbox();
});

// ------------------------------------------------------------ 幻灯片
const SLIDE_SPEEDS = [2000, 3000, 5000, 8000];
function toggleSlide() {
  if (slideTimer) { stopSlide(); return; }
  const btn = document.querySelector('[data-act=slide]');
  btn.classList.add('on');
  btn.textContent = '❚❚ 暂停';
  if (!isVideo(curGid)) lbImg.classList.add('kb');
  slideTimer = setInterval(() => {
    if (slideShuffle && view.length > 1) {
      let n;
      do { n = Math.floor(Math.random() * view.length); } while (n === pos);
      navDir = n > pos ? 1 : -1;
      pos = n;
    } else {
      navDir = 1;
      pos = pos >= view.length - 1 ? 0 : pos + 1;
    }
    show();
  }, slideSpeed);
}
function stopSlide() {
  if (slideTimer) clearInterval(slideTimer);
  slideTimer = null;
  lbImg.classList.remove('kb');
  const btn = document.querySelector('[data-act=slide]');
  if (btn) { btn.classList.remove('on'); btn.textContent = '▶ 幻灯片'; }
}
function cycleSpeed() {
  const i = SLIDE_SPEEDS.indexOf(slideSpeed);
  slideSpeed = SLIDE_SPEEDS[(i + 1) % SLIDE_SPEEDS.length];
  document.querySelector('[data-act=speed]').textContent =
    '⏱ ' + (slideSpeed / 1000) + 's';
  if (slideTimer) { stopSlide(); toggleSlide(); }   // 用新速度重启
}
function toggleShuffle() {
  slideShuffle = !slideShuffle;
  document.querySelector('[data-act=shuffle]')
    .classList.toggle('on', slideShuffle);
}

// ------------------------------------------------------------ 收藏
function updateFavCount() {
  const el = document.querySelector('.album[data-album="__fav__"] .ct');
  if (el) el.textContent = favSet.size;
}
function updateFavBtn() {
  const btn = document.querySelector('[data-act=fav]');
  if (!btn) return;
  const on = favSet.has(curGid);
  btn.textContent = on ? '★ 已收藏' : '☆ 收藏';
  btn.classList.toggle('on', on);
}
async function setFav(gid, on) {
  if (favSet.has(gid) === on) return;
  try {
    const r = await fetch('/api/fav', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: gid, on})});
    const d = await r.json();
    if (!d.ok) return;
    applyState(d.state);
    // 网格角标
    document.querySelectorAll('#grid .tile').forEach(t => {
      const g = +t.dataset.gid;
      const star = t.querySelector('.tile-fav');
      if (favSet.has(g) && !star) {
        const f = document.createElement('span');
        f.className = 'tile-fav';
        f.textContent = '⭐';
        t.appendChild(f);
      } else if (!favSet.has(g) && star) star.remove();
    });
    updateFavBtn();
    updateFavCount();
    // 收藏视图里取消收藏 → 从视图移除
    if (mode === 'fav' && !on) {
      view = view.filter(g => g !== gid);
      pager.items = view.slice();
      if (!lb.hidden) {
        const idx = view.indexOf(curGid);
        if (idx < 0) { closeLightbox(); pager.goto(pager.page); return; }
        pos = idx;
        navDir = 0;
        show();
      }
      pager.goto(pager.page);
    }
  } catch (_) {}
}
const toggleFav = gid => setFav(gid, !favSet.has(gid));

// ------------------------------------------------------------ 删除（移到废纸篓）
async function doDelete(ids) {
  ids = ids.filter(g => IMGS[g]);
  if (!ids.length) return;
  if (!confirm(`把 ${ids.length} 张图片移到废纸篓？\n（可在废纸篓中恢复）`))
    return;
  // 删除会触发服务端重扫、id 重排：先记下要继续看的图的指纹 k
  const gone = new Set(ids);
  let keepK = null;
  if (!lb.hidden) {
    let keepGid = -1;
    for (let i = pos; i < view.length; i++)
      if (!gone.has(view[i])) { keepGid = view[i]; break; }
    if (keepGid < 0)
      for (let i = pos - 1; i >= 0; i--)
        if (!gone.has(view[i])) { keepGid = view[i]; break; }
    if (keepGid >= 0 && IMGS[keepGid]) keepK = IMGS[keepGid].k;
  }
  try {
    const r = await fetch('/api/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    const d = await r.json();
    if (!d.ok) { showToast('删除失败：' + (d.error || ''), false); return; }
    IMGS = d.images;
    META.albums = d.albums;
    applyState(d.state);
    clearSelection();
    closeCtx();
    refreshView();
    if (!lb.hidden) {
      const rec = keepK != null ? IMGS.find(x => x.k === keepK) : null;
      const idx = rec ? view.indexOf(rec.id) : -1;
      if (idx < 0) closeLightbox();
      else { pos = idx; navDir = 0; buildStrip(); show(); }
    }
    showToast(`已移到废纸篓 ${d.deleted} 张`, false);
  } catch (e) {
    showToast('删除失败：' + e, false);
  }
}

// 按当前模式重建视图（删除/移动后调用）
function refreshView() {
  buildSidebar();
  if (mode === 'albums') {
    pager.items = META.albums.slice();
  } else if (mode === 'fav') {
    view = [...favSet].filter(g => IMGS[g]).sort((a, b) =>
      (IMGS[a].album + IMGS[a].name).localeCompare(
        IMGS[b].album + IMGS[b].name, 'zh'));
    pager.items = view.slice();
  } else {
    view = IMGS.filter(r => r.album === album)
               .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
               .map(r => r.id);
    pager.items = view.slice();
  }
  pager.goto(pager.page);
}

// ------------------------------------------------------------ 自定义封面
async function setCover(albumName, gid) {
  try {
    const r = await fetch('/api/set-cover', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({album: albumName, id: gid})});
    const d = await r.json();
    if (!d.ok) { showToast('设置封面失败：' + (d.error || ''), false); return; }
    META.albums = d.albums;
    applyState(d.state);
    if (mode === 'albums') {
      pager.items = META.albums.slice();
      pager.goto(pager.page);
    }
    showToast('已设为文件夹封面', false);
  } catch (e) {
    showToast('设置封面失败：' + e, false);
  }
}

// ------------------------------------------------------------ 右键菜单
let ctxIds = [];
function showPhotoMenu(x, y, ids) {
  ctxIds = ids;
  const menu = $('#ctx');
  const allFav = ids.every(g => favSet.has(g));
  const single = ids.length === 1;
  const g0 = ids[0];
  menu.innerHTML = '';
  const addBtn = (t, fn, danger) => {
    const b = document.createElement('button');
    b.textContent = t;
    if (danger) b.className = 'danger';
    b.onclick = () => { closeCtx(); fn(); };
    menu.appendChild(b);
  };
  addBtn(allFav ? '☆ 取消收藏' : '⭐ 收藏',
         () => ids.forEach(g => setFav(g, !allFav)));
  addBtn('📂 移动到…', () => openMovePicker(ids));
  addBtn('🗑 移到废纸篓', () => doDelete(ids), true);
  menu.appendChild(document.createElement('hr'));
  if (single)
    addBtn('🖼 设为文件夹封面', () => setCover(IMGS[g0].album, g0));
  addBtn('🔍 在 Finder 中显示', () => fetch('/api/reveal', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: g0})}));
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 8)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 8)) + 'px';
}
function closeCtx() { $('#ctx').hidden = true; }
document.addEventListener('click', e => {
  if (!$('#ctx').hidden && !$('#ctx').contains(e.target)) closeCtx();
});
document.addEventListener('scroll', closeCtx, true);

// ------------------------------------------------------------ 图片信息面板
async function loadInfo() {
  const gid = curGid;
  const body = $('#lbi-body');
  body.textContent = '读取中…';
  try {
    const d = await fetch('/api/info?id=' + gid).then(r => r.json());
    if (gid !== curGid || $('#lb-info').hidden) return;
    const rows = [];
    const add = (k, v) => { if (v != null && v !== '') rows.push([k, v]); };
    add('文件名', d.name);
    add('文件夹', d.album);
    add('类型', d.type === 'video' ? '视频' : '图片');
    if (d.w) add('分辨率', d.w + ' × ' + d.h);
    if (d.format) add('格式', d.format);
    add('大小', fmtSize(d.size));
    if (d.duration) add('时长', d.duration);
    if (d.date) add('拍摄时间', d.date);
    const cam = [d.cameraMake, d.cameraModel].filter(Boolean).join(' ');
    if (cam) add('相机', cam);
    if (d.exposure) add('快门', d.exposure);
    if (d.fnumber) add('光圈', d.fnumber);
    if (d.iso) add('ISO', d.iso);
    if (d.focal) add('焦距', d.focal);
    if (d.gpsLat != null) add('GPS', d.gpsLat + ', ' + d.gpsLon);
    body.innerHTML = '';
    rows.forEach(([k, v]) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<span class="k"></span><span class="v"></span>';
      row.querySelector('.k').textContent = k;
      row.querySelector('.v').textContent = v;
      body.appendChild(row);
    });
  } catch (e) {
    body.textContent = '读取失败：' + e;
  }
}
function toggleInfo() {
  const panel = $('#lb-info');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) loadInfo();
}

// ------------------------------------------------------------ 网格密度
function cycleDensity() {
  density = (density + 1) % DENSITY.length;
  pref.set('density', DENSITY[density].key);
  updateDensityBtn();
  pager.goto(pager.page);
}
function updateDensityBtn() {
  $('#btn-density').textContent = '▦ ' + DENSITY[density].label;
}

// ------------------------------------------------------------ 快捷键帮助
const HELP_ROWS = [
  ['← / → / 空格', '灯箱：上一张 / 下一张'],
  ['↑ / ↓ / PgUp / PgDn', '网格翻页（双指上下滑动亦可）'],
  ['底部页码框', '输入页码后回车，直接跳到该页'],
  ['Home / End', '跳到第一张 / 最后一张'],
  ['Enter', '打开当前页首图'],
  ['Esc', '关闭灯箱 / 取消选择 / 返回'],
  ['⌫ (Delete)', '灯箱内或选中后：移到废纸篓'],
  ['M', '移动图片'],
  ['. （句号）', '收藏 / 取消收藏'],
  ['I', '图片信息面板'],
  ['S', '幻灯片播放 / 暂停'],
  ['F', '全屏'],
  ['? （Shift+/）', '本帮助'],
  ['B', '收起 / 展开侧栏'],
  ['单击 / ✓ / Shift+单击', '选择 / 连选；右键弹出菜单'],
  ['拖拽到侧栏相册', '移动图片'],
  ['双击灯箱图片', '适应窗口 / 原图 1:1'],
];
function showHelp() {
  const list = $('#help-list');
  if (!list.children.length) {
    HELP_ROWS.forEach(([k, desc]) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<kbd></kbd><span class="d"></span>';
      row.querySelector('kbd').textContent = k;
      row.querySelector('.d').textContent = desc;
      list.appendChild(row);
    });
  }
  $('#help-picker').hidden = false;
}
function closeHelp() { $('#help-picker').hidden = true; }

// ------------------------------------------------------------ 事件绑定
function bindUI() {
  $('#btn-sidebar').onclick = () =>
    document.body.classList.toggle('sb-closed');
  $('#btn-back').onclick = () => showOverview();
  $('#pager-prev').onclick = () => pager.turn(-1);
  $('#pager-next').onclick = () => pager.turn(1);
  // 页码输入框：输入页码回车跳转
  const pg = $('#pager-goto');
  pg.addEventListener('focus', () => pg.select());
  pg.addEventListener('input', () => { pg.value = pg.value.replace(/\D/g, '').slice(0, 5); });
  pg.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      const n = parseInt(pg.value, 10);
      if (!isNaN(n)) pager.goto(n - 1);
      pg.blur();
    } else if (e.key === 'Escape') {
      pg.blur();
    }
  });
  // 多选 / 移动 / 提示条
  $('#sel-move').onclick = () => openMovePicker([...selection]);
  $('#sel-clear').onclick = () => clearSelection();
  $('#toast-undo').onclick = () => {
    $('#toast').hidden = true;
    if (lastMove && lastMove.source !== '') doMove(lastMove.ids, lastMove.source);
  };
  $('#toast-x').onclick = () => { $('#toast').hidden = true; };
  document.querySelectorAll('#move-picker .mp-back').forEach(el =>
    el.onclick = () => closeMovePicker());
  document.querySelectorAll('#open-picker .mp-back').forEach(el =>
    el.onclick = () => closeFolderPicker());
  $('#btn-open').onclick = () => openFolderPicker();
  $('#op-browse').onclick = e => { e.stopPropagation(); pickFolderNative(); };
  $('#op-input').addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); applyRoot(e.target.value); }
    else if (e.key === 'Escape') closeFolderPicker();
  });
  $('#op-input').addEventListener('click', e => e.stopPropagation());
  $('#mp-input').addEventListener('input', e => { mpIdx = 0; renderMpList(e.target.value); });
  $('#mp-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const it = mpItems[mpIdx];
      if (it && it.dest) confirmMove(it.dest);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault(); mpIdx = Math.min(mpItems.length - 1, mpIdx + 1);
      renderMpList(e.target.value);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); mpIdx = Math.max(0, mpIdx - 1);
      renderMpList(e.target.value);
    } else if (e.key === 'Escape') {
      closeMovePicker();
    }
  });
  $('#btn-density').onclick = cycleDensity;
  $('#btn-help').onclick = showHelp;
  document.querySelectorAll('#help-picker .mp-back').forEach(el =>
    el.onclick = () => closeHelp());
  $('#lbi-close').onclick = () => { $('#lb-info').hidden = true; };
  $('#lb-prev').onclick = () => nav(-1);
  $('#lb-next').onclick = () => nav(1);
  $('#lb-bar').onclick = e => {
    const act = e.target.dataset.act;
    if (act === 'prev') nav(-1);
    else if (act === 'next') nav(1);
    else if (act === 'fit') toggleActual();
    else if (act === 'move') openMovePicker([curGid]);
    else if (act === 'fav') toggleFav(curGid);
    else if (act === 'del') doDelete([curGid]);
    else if (act === 'info') toggleInfo();
    else if (act === 'speed') cycleSpeed();
    else if (act === 'shuffle') toggleShuffle();
    else if (act === 'close') closeLightbox();
    else if (act === 'slide') toggleSlide();
    else if (act === 'raw') window.open(imgUrl(curGid, 'raw'), '_blank');
    else if (act === 'reveal')
      fetch('/api/reveal', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: curGid})});
  };
  window.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    // 弹层优先响应 Esc
    if (!$('#help-picker').hidden) { if (e.key === 'Escape') closeHelp(); return; }
    if (!$('#ctx').hidden) { if (e.key === 'Escape') closeCtx(); return; }
    if (e.key === '?') { e.preventDefault(); showHelp(); return; }
    if (lb.hidden) {
      if (!$('#move-picker').hidden || !$('#open-picker').hidden) return;
      if (e.key === 'b') {
        document.body.classList.toggle('sb-closed');
      } else if (e.key === 'Escape' && selection.size) {
        clearSelection();
      } else if ((e.key === 'Backspace' || e.key === 'Delete') && selection.size) {
        e.preventDefault();
        doDelete([...selection]);
      } else if ((e.key === 'Escape' || e.key === 'Backspace') &&
                 (mode === 'photos' || mode === 'fav')) {
        e.preventDefault();
        showOverview();
      } else if ((e.key === 'm' || e.key === 'M') && selection.size) {
        e.preventDefault();   // 阻止字母落进弹框里刚聚焦的输入框
        openMovePicker([...selection]);
      } else if (e.key === 'Enter' && mode !== 'albums' && view.length) {
        openLightbox(Math.min(pager.page * pager.perPage, view.length - 1));
      } else if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(e.key)) {
        e.preventDefault(); pager.turn(1);
      } else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(e.key)) {
        e.preventDefault(); pager.turn(-1);
      } else if (e.key === 'Home') {
        e.preventDefault(); pager.goto(0);
      } else if (e.key === 'End') {
        e.preventDefault(); pager.goto(9999);
      }
      return;
    }
    pokeChrome();   // 灯箱内任何按键都唤醒界面
    switch (e.key) {
      case 'ArrowLeft': case 'PageUp': e.preventDefault(); nav(-1); break;
      case 'ArrowRight': case 'PageDown': case ' ':
        e.preventDefault(); nav(1); break;
      case 'Home': e.preventDefault(); pos = 0; show(); break;
      case 'End': e.preventDefault(); pos = view.length - 1; show(); break;
      case 'Escape': closeLightbox(); break;
      case 'f': case 'F':
        document.fullscreenElement ? document.exitFullscreen()
          : document.documentElement.requestFullscreen();
        break;
      case 's': case 'S': toggleSlide(); break;
      case 'm': case 'M':
        e.preventDefault();   // 同上，阻止字母落入移动弹框输入框
        openMovePicker([curGid]); break;
      case 'Backspace': case 'Delete':
        e.preventDefault(); doDelete([curGid]); break;
      case 'i': case 'I': toggleInfo(); break;
      case '.': toggleFav(curGid); break;
    }
  });
  window.addEventListener('hashchange', () => {
    const h = location.hash;
    if (h === '#fav') {                 // #fav：收藏视图
      if (!lb.hidden) closeLightbox();
      if (mode !== 'fav') showFavorites(false);
      return;
    }
    const pm = h.match(/^#(\d+)$/);
    if (pm && IMGS[+pm[1]]) {           // #<id>：打开某张图
      const gid = +pm[1];
      if (gid === curGid) return;
      if (!view.includes(gid))
        enterAlbum(IMGS[gid].album, false);
      openAt(gid);
      return;
    }
    const am = h.match(/^#a\/(.+)$/);
    if (am) {                           // #a/<相册>：进入相册
      const n = decodeURIComponent(am[1]);
      const real = n === '__root__' ? '' : n;
      if (!lb.hidden) closeLightbox();
      if (mode !== 'photos' || album !== real) enterAlbum(real, false);
      return;
    }
    // 纯 '#'：先关灯箱；已在相册/收藏网格则退回封面墙
    if (!lb.hidden) closeLightbox();
    else if (mode !== 'albums') showOverview(false);
  });
}

init();
</script>
</body>
</html>
'''


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='本地图片丝滑浏览工具')
    ap.add_argument('dir', nargs='?', default=None,
                    help='图片根目录（默认当前目录）')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    ap.add_argument('--pick', action='store_true',
                    help='启动时空库待选（前端自动弹出选文件夹框）')
    args = ap.parse_args()

    global ROOT, IMAGES, ALBUMS
    load_state()

    frozen = getattr(sys, 'frozen', False)   # PyInstaller 打包后的 .app
    target = args.dir
    pick_mode = args.pick
    if target is None and not pick_mode:
        if frozen:
            # .app 双击启动（无参数）：先弹系统选文件夹框；取消则进入待选模式
            picked = pick_folder_native()
            if picked:
                target = picked
            else:
                pick_mode = True
        else:
            target = os.getcwd()

    if pick_mode:
        # 空库待选：用临时空目录做 ROOT，所有接口正常工作，
        # 用户在网页里选好文件夹后 /api/set-root 切换
        ROOT = tempfile.mkdtemp(prefix='imgbrowse-empty-')
        IMAGES, ALBUMS = [], []
    else:
        ROOT = os.path.realpath(target)
        if not os.path.isdir(ROOT):
            sys.exit('目录不存在: %s' % ROOT)
        IMAGES, _ = scan(ROOT)
        if not IMAGES:
            if frozen:
                # .app 拖入/选择的目录里没图：进入待选模式而不是退出
                pick_mode = True
                ROOT = tempfile.mkdtemp(prefix='imgbrowse-empty-')
                IMAGES, ALBUMS = [], []
            else:
                sys.exit('目录下没有找到图片/视频: %s' % ROOT)
        else:
            sanitize_state()
            rebuild_albums()

    port = args.port
    httpd = None
    for _ in range(30):
        try:
            httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        sys.exit('找不到可用端口')

    url = 'http://127.0.0.1:%d/' % port
    print('imgbrowse 已启动')
    if pick_mode:
        print('  （待选模式：请在网页中选择要浏览的文件夹）')
    else:
        print('  目录: %s' % ROOT)
        print('  图片: %d 张（%d 个相册）' % (len(IMAGES), len(ALBUMS)))
    print('  地址: %s' % url)
    print('  缓存: %s' % CACHE_DIR)
    print('  按 Ctrl+C 退出')

    threading.Thread(target=warm_start, daemon=True).start()
    if not args.no_open:
        threading.Timer(0.6, lambda: subprocess.Popen(['open', url])).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n再见')


if __name__ == '__main__':
    main()
