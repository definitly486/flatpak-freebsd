#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flatpak-freebsd: скачивание и запуск Flatpak-приложений на FreeBSD через Linuxulator.
Без ostree и flatpak: репозиторий читается напрямую по HTTP.

Команды:
  install ЦЕЛЬ     скачать приложение и его рантайм, сделать обёртки
  start ЦЕЛЬ       установить, если не стоит, и запустить
  run ID           запустить установленное (после -- идут аргументы приложения)
  update [ID]      обновить одно или все приложения (качаются только изменения)
  search [СЛОВА]   поиск в каталоге репозитория (-i: подробности, -u: обновить каталог)
  list             что установлено
  info ID          подробности об установленном
  remove ID        удалить (--prune: заодно неиспользуемые рантаймы)
  link ID          ссылка /app -> файлы приложения (нужен root, один раз)
  wrap ID          пересоздать обёртки для вспомогательных процессов
  umount ID        размонтировать chroot приложения (после аварийного завершения)
  check            диагностика окружения

ЦЕЛЬ: ID приложения (org.mozilla.firefox), либо путь/URL к .flatpakref.
Репозиторий задаётся --remote: flathub (по умолчанию), flathub-beta,
URL .flatpakrepo или URL самого репозитория.

Примеры:
  flatpak-freebsd.py install org.gnome.Calculator
  flatpak-freebsd.py start com.kagi.Orion --branch beta \\
      --remote https://flatpak.orionbrowser.com/orion-beta.flatpakrepo
  flatpak-freebsd.py run org.gnome.Calculator
  flatpak-freebsd.py update
  flatpak-freebsd.py search текстовый редактор
  flatpak-freebsd.py search -i org.gnome.Calculator

Режим --chroot (run/start): приложение запускается в корне, собранном как в Flatpak
(/usr = рантайм, /app = приложение), без подмены загрузчиков и ссылки в /compat/linux.
Нужен root для mount/chroot (doas или sudo). Проверка без root: --dry-run.

Данные: ~/.local/share/flatpak-freebsd (или --root / $FLATPAK_FB_ROOT).
Настройки окружения: файл ROOT/env (для всех) и ROOT/apps/ID/env (для одного),
строки KEY=VALUE. Переменные вашей оболочки имеют приоритет над файлами.
"""
import argparse
import hashlib
import io
import json
import grp
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from configparser import ConfigParser

KNOWN_REMOTES = {
    "flathub": "https://dl.flathub.org/repo/",
    "flathub-beta": "https://dl.flathub.org/beta-repo/",
}
DEFAULT_REMOTE = "flathub"
BRANCH_GUESS = ["stable", "beta", "master", "main"]
APPSTREAM_REFS = ["appstream", "appstream2"]          # каталог приложений для search
APPSTREAM_FILES = ["appstream.xml.gz", "appstream.xml"]
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

ARCH_INFO = {
    "x86_64": ("x86_64-linux-gnu", ["lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                                    "lib64/ld-linux-x86-64.so.2",
                                    "lib/ld-linux-x86-64.so.2"]),
    "aarch64": ("aarch64-linux-gnu", ["lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
                                      "lib64/ld-linux-aarch64.so.1",
                                      "lib/ld-linux-aarch64.so.1"]),
}

# Значения по умолчанию для окружения приложения (переопределяются файлами env и оболочкой).
ENV_DEFAULTS = {
    "GDK_BACKEND": "x11",
    "QT_QPA_PLATFORM": "xcb",
    "GSK_RENDERER": "cairo",
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS": "1",   # bwrap в Linuxulator нет
    "WEBKIT_DISABLE_COMPOSITING_MODE": "1",
    "GTK_A11Y": "none",
    "NO_AT_BRIDGE": "1",
    "GIO_USE_PROXY_RESOLVER": "dummy",
    # песочницы Mozilla (seccomp, user namespaces) в Linuxulator недоступны
    "MOZ_DISABLE_CONTENT_SANDBOX": "1",
    "MOZ_DISABLE_GMP_SANDBOX": "1",
    "MOZ_DISABLE_RDD_SANDBOX": "1",
    "MOZ_DISABLE_SOCKET_PROCESS_SANDBOX": "1",
    "MOZ_DISABLE_UTILITY_SANDBOX": "1",
}

ROOT = None


def P(*a):
    return os.path.join(ROOT, *a)


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def host_arch():
    return {"amd64": "x86_64", "x86_64": "x86_64",
            "arm64": "aarch64", "aarch64": "aarch64"}.get(os.uname().machine, "x86_64")


# ------------------------------------------------- минимальный разбор GVariant
def osz(n):
    return 1 if n < 256 else 2 if n < 65536 else 4


def rd(b, off, sz):
    return int.from_bytes(b[off:off + sz], "little")


def parse_array(a):
    if not a:
        return []
    o = osz(len(a))
    last = rd(a, len(a) - o, o)
    n = (len(a) - last) // o
    out, s = [], 0
    for i in range(n):
        e = rd(a, last + i * o, o)
        out.append(a[s:e])
        s = e
    return out


def parse_commit(d):
    o = osz(len(d)) * 6
    return d[-o - 64:-o - 32], d[-o - 32:-o]          # dirtree, dirmeta


def parse_dirtree(d):
    o = osz(len(d))
    fend = rd(d, len(d) - o, o)
    files, dirs = [], []
    for e in parse_array(d[:fend]):
        oe = osz(len(e))
        ne = rd(e, len(e) - oe, oe)
        files.append((e[:ne - 1].decode(), e[ne:ne + 32]))
    for e in parse_array(d[fend:len(d) - o]):
        oe = osz(len(e))
        ne = rd(e, len(e) - oe, oe)
        te = rd(e, len(e) - 2 * oe, oe)
        dirs.append((e[:ne - 1].decode(), e[ne:te]))
    return files, dirs


# --------------------------------------------------------- клиент OSTree/HTTP
class Repo:
    def __init__(self, base, workers=16):
        self.base = base if base.endswith("/") else base + "/"
        self.workers = workers
        self.lock = threading.Lock()
        self.tree_cache = {}
        self.bytes = self.done = self.total = 0

    def _read(self, req, label):
        if label is None:
            return urllib.request.urlopen(req, timeout=30).read()
        buf, tty = bytearray(), sys.stderr.isatty()
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                buf += chunk
                if tty:
                    print("\r[%s] %.1f/%.1f MiB   " % (label, len(buf) / 1048576,
                                                       total / 1048576),
                          end="", file=sys.stderr, flush=True)
        if tty:
            print(file=sys.stderr)
        return bytes(buf)

    def get(self, path, tries=6, label=None):
        for i in range(tries):
            try:
                req = urllib.request.Request(self.base + path,
                                             headers={"User-Agent": "ostree"})
                data = self._read(req, label)
                with self.lock:
                    self.bytes += len(data)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404 or i == tries - 1:
                    raise
                time.sleep(1 + i)
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(1 + i)

    def obj(self, csum, ext, label=None):
        h = csum.hex()
        return self.get("objects/%s/%s.%s" % (h[:2], h[2:], ext), label=label)

    def head_opt(self, ref):
        """Хеш коммита или None, если такого ref нет в репозитории."""
        try:
            return bytes.fromhex(self.get("refs/heads/" + ref).decode().strip())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def head(self, ref):
        try:
            c = self.head_opt(ref)
        except Exception as e:
            die("репозиторий %s недоступен: %s" % (self.base, e))
        if c is None:
            die("ref %s не найден в %s" % (ref, self.base))
        return c

    def _load_tree(self, job):
        tree, dest = job
        os.makedirs(dest, exist_ok=True)
        parsed = self.tree_cache.get(tree)
        if parsed is None:
            parsed = parse_dirtree(self.obj(tree, "dirtree"))
            self.tree_cache[tree] = parsed
        files, dirs = parsed
        return ([(c, os.path.join(dest, n)) for n, c in files],
                [(t, os.path.join(dest, n)) for n, t in dirs])

    def _fetch(self, item):
        csum, paths = item
        todo = [p for p in paths if not os.path.lexists(p)]
        if not todo:
            with self.lock:
                self.done += len(paths)
            return
        d = self.obj(csum, "filez")
        vs = int.from_bytes(d[:4], "big")
        hdr = d[8:8 + vs]                      # (tuuuusa(ayay)), big-endian
        size = int.from_bytes(hdr[0:8], "big")
        mode = int.from_bytes(hdr[16:20], "big")
        o = osz(len(hdr))
        se = rd(hdr, len(hdr) - o, o)
        if stat.S_ISLNK(mode):
            target = hdr[24:se - 1].decode()
            for p in todo:
                os.symlink(target, p)
        else:
            if size == 0:
                data = b""
            else:
                data = None
                for start in ((8 + vs + 7) & ~7, 8 + vs):
                    try:
                        cand = zlib.decompressobj(-15).decompress(d[start:])
                        if len(cand) == size:
                            data = cand
                            break
                    except zlib.error:
                        pass
                if data is None:
                    raise ValueError("не удалось распаковать (size=%d)" % size)
            perm = (mode & 0o777) | 0o600
            first = todo[0]
            tmp = first + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, perm)
            os.rename(tmp, first)
            for p in todo[1:]:                 # одинаковый объект -> жёсткая ссылка
                try:
                    os.link(first, p)
                except OSError:
                    with open(p, "wb") as f:
                        f.write(data)
                    os.chmod(p, perm)
        with self.lock:
            self.done += len(paths)

    def _progress(self, label, stop):
        t0 = time.time()
        while not stop.is_set():
            el = max(time.time() - t0, 0.1)
            rate = self.done / el
            eta = (self.total - self.done) / rate if rate > 0 else 0
            print("\r[%s] файлы: %d/%d | %.0f MiB | %.1f MiB/s | ETA %dm%02ds   " % (
                label, self.done, self.total, self.bytes / 1048576,
                self.bytes / 1048576 / el, eta // 60, eta % 60), end="", flush=True)
            stop.wait(0.5)

    def pull(self, ref, out, label):
        """Скачать ref в каталог out (готовые файлы пропускаются)."""
        self.bytes = self.done = self.total = 0
        commit = self.head(ref)
        tree, _ = parse_commit(self.obj(commit, "commit"))
        print("[%s] читаю структуру каталогов..." % label, flush=True)
        all_files, level, ndirs = [], [(tree, out)], 0
        with ThreadPoolExecutor(self.workers) as ex:
            while level:
                nxt = []
                for files, dirs in ex.map(self._load_tree, level):
                    all_files += files
                    nxt += dirs
                ndirs += len(level)
                print("\r  каталогов: %d, файлов: %d   " % (ndirs, len(all_files)),
                      end="", flush=True)
                level = nxt
        print()
        by = {}
        for c, p in all_files:
            by.setdefault(c, []).append(p)
        self.total = len(all_files)
        errors = []

        def safe(item):
            try:
                self._fetch(item)
            except Exception as e:
                errors.append((item[1][0], repr(e)))

        stop = threading.Event()
        th = threading.Thread(target=self._progress, args=(label, stop), daemon=True)
        th.start()
        try:
            with ThreadPoolExecutor(self.workers) as ex:
                list(ex.map(safe, by.items()))
        finally:
            stop.set()
            th.join()
        print()
        if errors:
            for p, e in errors[:20]:
                print("  ошибка:", p, e, file=sys.stderr)
            die("[%s] ошибок: %d. Повторите команду: готовое пропустится."
                % (label, len(errors)))
        return commit.hex()


# ---------------------------------------------------------------- вспомогательное
def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def fetch_text(src):
    if os.path.exists(src):
        with open(src, encoding="utf-8", errors="replace") as f:
            return f.read()
    try:
        req = urllib.request.Request(src, headers={"User-Agent": "flatpak"})
        return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        die("не удалось получить %s: %s" % (src, e))


def parse_ini(text):
    cp = ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except Exception:
        return {}
    return {s: dict(cp.items(s)) for s in cp.sections()}


def resolve_remote(spec):
    """flathub | flathub-beta | URL .flatpakrepo | URL репозитория -> URL репозитория."""
    spec = spec or DEFAULT_REMOTE
    if spec in KNOWN_REMOTES:
        return KNOWN_REMOTES[spec]
    if spec.endswith(".flatpakrepo"):
        url = parse_ini(fetch_text(spec)).get("Flatpak Repo", {}).get("Url")
        if not url:
            die("в %s нет строки Url=" % spec)
        return url if url.endswith("/") else url + "/"
    return spec if spec.endswith("/") else spec + "/"


def split_ref(r):
    parts = r.split("/")
    if len(parts) != 3:
        die("странная запись runtime=%s" % r)
    return parts


# ---------------------------------------------------------------- установка
def sync(repo, ref, dest, label, force=False):
    """Скачать ref в dest атомарно (через dest.new). True, если что-то менялось."""
    commit = repo.head(ref).hex()
    cur = read_text(os.path.join(dest, ".commit"))
    if not force and cur == commit:
        print("[%s] актуально (%s)" % (label, commit[:12]))
        return False
    if os.path.isdir(dest) and cur is None and not force:
        print("[%s] каталог без метки: докачиваю на месте (%s)" % (label, commit[:12]))
        repo.pull(ref, dest, label)
        with open(os.path.join(dest, ".commit"), "w") as f:
            f.write(commit)
        return True
    staging, old = dest + ".new", dest + ".old"
    marker = os.path.join(staging, ".pulling")
    if os.path.isdir(staging) and read_text(marker) != commit:
        shutil.rmtree(staging)
    os.makedirs(staging, exist_ok=True)
    with open(marker, "w") as f:
        f.write(commit)
    print("[%s] %s, коммит %s" % (label, ref, commit[:12]))
    repo.pull(ref, staging, label)
    os.remove(marker)
    with open(os.path.join(staging, ".commit"), "w") as f:
        f.write(commit)
    if os.path.exists(dest):
        shutil.rmtree(old, ignore_errors=True)
        os.rename(dest, old)
    os.rename(staging, dest)
    shutil.rmtree(old, ignore_errors=True)
    return True


def find_app_ref(repo, appid, arch, branch):
    tried = [branch] if branch else BRANCH_GUESS
    for b in tried:
        ref = "app/%s/%s/%s" % (appid, arch, b)
        if repo.head_opt(ref) is not None:
            return ref, b
    die("в %s нет приложения %s (пробовал ветки: %s). Укажите --branch/--remote."
        % (repo.base, appid, ", ".join(tried)))


def app_dir(appid, branch):
    return P("apps", appid, branch)


def runtime_dir(rid, rarch, rbranch):
    return P("runtimes", rid, rarch, rbranch)


def load_info(appid, branch=None):
    base = P("apps", appid)
    if not os.path.isdir(base):
        die("%s не установлено" % appid)
    brs = sorted(b for b in os.listdir(base)
                 if os.path.isfile(os.path.join(base, b, "info.json")))
    if not brs:
        die("%s не установлено" % appid)
    if branch is None:
        if len(brs) > 1:
            die("несколько веток (%s): укажите --branch" % ", ".join(brs))
        branch = brs[0]
    elif branch not in brs:
        die("ветка %s не установлена (есть: %s)" % (branch, ", ".join(brs)))
    with open(os.path.join(base, branch, "info.json")) as f:
        return json.load(f)


def app_metadata(adir):
    return parse_ini(read_text(os.path.join(adir, "metadata")) or "")


def install_runtime(info, jobs, force=False):
    rid, rarch, rbranch = split_ref(info["runtime"])
    ref = "runtime/%s" % info["runtime"]
    cands = []
    for spec in (info.get("runtime_remote"), info.get("remote"), DEFAULT_REMOTE):
        if spec:
            url = resolve_remote(spec)
            if url not in cands:
                cands.append(url)
    for url in cands:
        repo = Repo(url, jobs)
        if repo.head_opt(ref) is not None:
            sync(repo, ref, runtime_dir(rid, rarch, rbranch), "рантайм " + rid, force)
            return
    die("рантайм %s не найден ни в одном репозитории: %s" % (ref, ", ".join(cands)))


def do_install(t, jobs, force=False):
    appid, arch = t["id"], t["arch"]
    app_url = resolve_remote(t.get("remote"))
    repo = Repo(app_url, jobs)
    ref, branch = find_app_ref(repo, appid, arch, t.get("branch"))
    adir = app_dir(appid, branch)
    os.makedirs(os.path.dirname(adir), exist_ok=True)
    sync(repo, ref, adir, appid, force)
    meta = app_metadata(adir).get("Application", {})
    if not meta.get("runtime"):
        die("в metadata нет runtime=. Это точно приложение, а не рантайм?")
    info = {"id": appid, "branch": branch, "arch": arch, "remote": app_url,
            "runtime_remote": t.get("runtime_remote"), "runtime": meta["runtime"],
            "command": meta.get("command", appid)}
    install_runtime(info, jobs, force)
    with open(os.path.join(adir, "info.json"), "w") as f:
        json.dump(info, f, indent=1)
    wrap(info)
    return info


# ------------------------------------------------------------ обёртки
def elf_interp(path):
    """True для динамически слинкованного исполняемого ELF64 LE (есть PT_INTERP)."""
    try:
        with open(path, "rb") as f:
            h = f.read(64)
            if len(h) < 64 or h[:4] != b"\x7fELF" or h[4] != 2 or h[5] != 1:
                return False
            phoff = int.from_bytes(h[32:40], "little")
            phsz = int.from_bytes(h[54:56], "little")
            phnum = int.from_bytes(h[56:58], "little")
            f.seek(phoff)
            ph = f.read(phsz * phnum)
    except OSError:
        return False
    return any(int.from_bytes(ph[i * phsz:i * phsz + 4], "little") == 3
               for i in range(phnum))


def paths_for(info):
    adir = app_dir(info["id"], info["branch"])
    rid, rarch, rbranch = split_ref(info["runtime"])
    rdir = runtime_dir(rid, rarch, rbranch)
    return os.path.join(adir, "files"), os.path.join(rdir, "files"), rdir


def find_ld(rfiles, arch):
    for rel in ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[1]:
        p = os.path.join(rfiles, rel)
        if os.path.exists(p):
            return p
    return None


def lib_path(afiles, rfiles, arch):
    tri = ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[0]
    return ":".join([afiles + "/lib64", afiles + "/lib", afiles + "/lib/" + tri,
                     rfiles + "/lib/" + tri, rfiles + "/lib/" + tri + "/pulseaudio",
                     rfiles + "/lib", rfiles + "/lib64"])


def command_path(info, afiles):
    c = info.get("command") or info["id"]
    if c.startswith("/app/"):
        return os.path.join(afiles, c[5:])
    if c.startswith("/"):
        return c
    return os.path.join(afiles, "bin", c)


WRAP_MARK = ".wrap-v6"
INTERP_NAME = ".ld"                  # копия загрузчика рантайма в каталоге приложения
INTERP_PATH = b"/app/" + INTERP_NAME.encode()   # короткий путь, влезает в PT_INTERP


def patch_interp(path):
    """Заменить PT_INTERP (/lib64/ld-linux-...) на /app/.ld, то есть на загрузчик рантайма.

    Тогда программа запускается напрямую, и /proc/self/exe — это она сама, а не ld.so.
    Это нужно Firefox и подобным: дочерние процессы он запускает через свой же exe.
    Возвращает True, если файл изменён."""
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IWUSR)
        with open(path, "r+b") as f:
            h = f.read(64)
            if len(h) < 64 or h[:4] != b"\x7fELF" or h[4] != 2 or h[5] != 1:
                return False
            phoff = int.from_bytes(h[32:40], "little")
            phsz = int.from_bytes(h[54:56], "little")
            phnum = int.from_bytes(h[56:58], "little")
            f.seek(phoff)
            ph = f.read(phsz * phnum)
            for i in range(phnum):
                e = ph[i * phsz:(i + 1) * phsz]
                if int.from_bytes(e[0:4], "little") != 3:
                    continue
                off = int.from_bytes(e[8:16], "little")
                size = int.from_bytes(e[32:40], "little")
                f.seek(off)
                cur = f.read(size).split(b"\0")[0]
                if cur == INTERP_PATH:
                    return False
                if not cur.startswith(b"/lib") or len(INTERP_PATH) + 1 > size:
                    return False
                f.seek(off)
                f.write(INTERP_PATH.ljust(size, b"\0"))
                return True
    except OSError:
        pass
    return False


def install_interp(afiles, ld):
    """Положить загрузчик рантайма как <files>/.ld (копия: ссылка на путь в рантайме
    не нужна, ядро берёт интерпретатор по пути /app/.ld)."""
    dst = os.path.join(afiles, INTERP_NAME)
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(ld):
        return
    tmp = dst + ".tmp"
    shutil.copyfile(ld, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dst)


def is_launcher_stub(p):
    """Крошечный запускатор вида «execv(/proc/self/exe + '-bin')» (firefox)."""
    try:
        if os.path.islink(p) or os.path.getsize(p) > 65536 or not os.path.exists(p + "-bin"):
            return False
        with open(p, "rb") as f:
            data = f.read()
    except OSError:
        return False
    return data[:4] == b"\x7fELF" and b"/proc/self/exe" in data and b"-bin" in data


def wrap(info, quiet=False):
    """Программы приложения должны работать с библиотеками рантайма: системный glibc
    Linuxulator для них слишком старый. У каждого ELF интерпретатор заменяется на
    загрузчик рантайма (/app/.ld); пути к библиотекам даёт LD_LIBRARY_PATH из do_run."""
    afiles, rfiles, _ = paths_for(info)
    ld = find_ld(rfiles, info["arch"])
    if ld is None:
        die("в рантайме нет загрузчика ld-linux (arch=%s)" % info["arch"])
    install_interp(afiles, ld)
    count = 0
    elfs = []
    for d, dirs, files in os.walk(afiles):
        dirs[:] = [x for x in dirs if x not in ("share", "include")]
        for name in files:
            p = os.path.join(d, name)
            if name == ".ld.so":                       # остаток старой схемы
                os.unlink(p)
                continue
            if name.endswith(".real"):                 # старая схема: вернуть оригинал
                orig = p[:-5]
                if os.path.exists(orig) and not os.path.islink(orig):
                    os.replace(p, orig)
                continue
            if ".so" in name or os.path.islink(p) or name.startswith(".ld"):
                continue
            elfs.append(p)
    for p in elfs:
        if elf_interp(p):
            if patch_interp(p):
                count += 1
    for p in elfs:                                     # запускатели -> ссылка на X-bin
        if is_launcher_stub(p):
            os.unlink(p)
            os.symlink(os.path.basename(p) + "-bin", p)
    try:
        open(os.path.join(afiles, WRAP_MARK), "w").close()
    except OSError:
        pass
    if not quiet:
        print("исправлено ELF-файлов: %d" % count)
    return count


RT_MARK = ".wrap-rt-v1"


def wrap_runtime(rdir, rfiles, quiet=False):
    """То же, что wrap(), но для исполняемых файлов рантайма (zenity, bash, ...).

    Дочерние процессы, которые приложение запускает из рантайма, иначе получают
    старый загрузчик из /compat/linux вместе с новым glibc рантайма
    (ошибка вида: undefined symbol __pointer_chk_guard, version GLIBC_PRIVATE)."""
    mark = os.path.join(rdir, RT_MARK)
    if os.path.exists(mark):
        return 0
    count = 0
    for d, dirs, files in os.walk(rfiles):
        dirs[:] = [x for x in dirs if x not in ("share", "include")]
        for name in files:
            if ".so" in name:
                continue
            p = os.path.join(d, name)
            if os.path.islink(p):
                continue
            if elf_interp(p) and patch_interp(p):
                count += 1
    try:
        open(mark, "w").close()
    except OSError:
        pass
    if not quiet:
        print("рантайм: исправлено ELF-файлов: %d" % count)
    return count


# ------------------------------------------------------------ ссылка /app
def link_path():
    return "/compat/linux/app" if os.path.isdir("/compat/linux") else "/app"


def link_ok(afiles):
    link = link_path()
    return os.path.islink(link) and os.path.realpath(link) == os.path.realpath(afiles)


def ensure_link(afiles, quiet=False):
    link = link_path()

    if link_ok(afiles):
        return True

    # Если мы root — создаём ссылку напрямую.
    if os.geteuid() == 0:
        try:
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(afiles, link)
            if not quiet:
                print("создана ссылка %s -> %s" % (link, afiles))
            return True
        except OSError as e:
            if not quiet:
                print("не удалось создать ссылку %s: %s" % (link, e),
                      file=sys.stderr)
            return False

    # Обычный пользователь: автоматически используем doas или sudo.
    runner = None

    if shutil.which("doas"):
        runner = "doas"
    elif shutil.which("sudo"):
        runner = "sudo"

    if runner is None:
        if not quiet:
            print(
                "\nДля запуска приложения требуется root-доступ, "
                "чтобы настроить %s." % link,
                file=sys.stderr
            )
            print(
                "Установите/настройте doas или sudo, либо запустите "
                "скрипт от root.",
                file=sys.stderr
            )
        return False

    try:
        subprocess.run(
            [
                runner,
                "ln",
                "-sfn",
                afiles,
                link,
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        if not quiet:
            print(
                "\nНе удалось настроить %s для приложения." % link,
                file=sys.stderr
            )
        return False
    except OSError as e:
        if not quiet:
            print("ошибка запуска %s: %s" % (runner, e),
                  file=sys.stderr)
        return False

    if not link_ok(afiles):
        if not quiet:
            print(
                "ссылка %s не указывает на %s" % (link, afiles),
                file=sys.stderr
            )
        return False

    if not quiet:
        print("ссылка %s -> %s" % (link, afiles))

    return True


def host_timezone():
    """Имя часового пояса хоста (Area/City) или None."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if re.fullmatch(r"[A-Za-z_+\-0-9]+(/[A-Za-z_+\-0-9]+)+", tz or ""):
        return tz
    try:                                   # FreeBSD: tzsetup пишет имя сюда
        with open("/var/db/zoneinfo") as f:
            tz = f.read().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:                                   # на случай, если /etc/localtime - симлинк
        tz = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in tz:
            return tz.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def ensure_timezone(quiet=False):
    """Создаёт /compat/linux/etc/timezone (нужен libecal/GNOME Calendar)."""
    if not os.path.isdir("/compat/linux/etc"):
        return
    target = "/compat/linux/etc/timezone"
    if os.path.exists(target):
        return
    tz = host_timezone()
    if not tz:
        if not quiet:
            print("предупреждение: не удалось определить часовой пояс; "
                  "создайте %s вручную (например: Europe/Helsinki)" % target,
                  file=sys.stderr)
        return
    if os.geteuid() == 0:
        runner = []
    else:
        runner = [r for r in ("doas", "sudo") if shutil.which(r)][:1]
        if not runner:
            if not quiet:
                print("предупреждение: нет doas/sudo, не могу создать %s" % target,
                      file=sys.stderr)
            return
    try:
        subprocess.run(runner + ["tee", target], input=tz + "\n", text=True,
                       stdout=subprocess.DEVNULL, check=True)
        if not quiet:
            print("создан %s (%s)" % (target, tz))
    except (subprocess.CalledProcessError, OSError) as e:
        if not quiet:
            print("не удалось создать %s: %s" % (target, e), file=sys.stderr)


# ------------------------------------------- .flatpak-info и 32-битные расширения
I386_EXT = "org.freedesktop.Platform.Compat.i386"


def root_run(*cmd):
    """Выполнить команду от root (напрямую, через doas или через sudo)."""
    if os.geteuid() == 0:
        runner = []
    else:
        runner = [r for r in ("doas", "sudo") if shutil.which(r)][:1]
        if not runner:
            print("предупреждение: нет doas/sudo, не могу выполнить: %s" % " ".join(cmd),
                  file=sys.stderr)
            return False
    try:
        subprocess.run(runner + list(cmd), check=True)
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        print("не удалось выполнить %s: %s" % (" ".join(cmd), e), file=sys.stderr)
        return False


def ensure_flatpak_info(info, afiles, rfiles, exts=(), quiet=False, base=None, var=None):
    """Создаёт /compat/linux/.flatpak-info (его читают Steam и другие обёртки).

    Файл один на всю систему, поэтому при запуске другого приложения он
    перезаписывается."""
    direct = base is not None            # свой корень (chroot): пишем без root
    if base is None:
        base = "/compat/linux" if os.path.isdir("/compat/linux") else ""
    target = base + "/.flatpak-info"
    text = (
        "[Application]\n"
        "name=%(id)s\n"
        "runtime=runtime/%(runtime)s\n"
        "\n"
        "[Instance]\n"
        "instance-id=1\n"
        "instance-path=%(var)s\n"
        "app-path=%(afiles)s\n"
        "runtime-path=%(rfiles)s\n"
        "branch=%(branch)s\n"
        "arch=%(arch)s\n"
        "flatpak-version=1.15.0\n"
        "runtime-extensions=%(rext)s\n"
        "app-extensions=\n"
        "\n"
        "[Context]\n"
        "shared=network;ipc;\n"
        "sockets=x11;pulseaudio;\n"
        "devices=dri;all;\n"
        "features=devel;multiarch;bluetooth;canbus;\n"
    ) % {
        "id": info["id"], "runtime": info["runtime"],
        "var": var or os.path.expanduser("~/.var/app/%s" % info["id"]),
        "afiles": afiles, "rfiles": rfiles,
        "branch": info["branch"], "arch": info["arch"],
        "rext": "".join("%s=%s;" % e for e in exts),
    }
    if read_text(target) == text:
        return True
    if direct:
        with open(target, "w") as f:
            f.write(text)
        os.chmod(target, 0o644)
        return True
    fd, tmp = tempfile.mkstemp(prefix="flatpak-info-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        ok = root_run("cp", tmp, target) and root_run("chmod", "644", target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if ok and not quiet:
        print("создан %s" % target)
    return ok


def wants_i386(info, afiles):
    """Приложению нужны 32-битные библиотеки (features=multiarch в metadata)."""
    if info["arch"] != "x86_64":
        return False
    meta = read_text(os.path.join(os.path.dirname(afiles), "metadata")) or ""
    return "multiarch" in meta


def ensure_i386(info, jobs=4, patch_base=True):
    """Скачивает Compat.i386 и подменяет 32-битный загрузчик Linuxulator.
    Возвращает (каталог_с_библиотеками, коммит) или None."""
    import glob
    rid, rarch, rbranch = split_ref(info["runtime"])
    edir = runtime_dir(I386_EXT, rarch, rbranch)
    ref = "runtime/%s/%s/%s" % (I386_EXT, rarch, rbranch)
    efiles = os.path.join(edir, "files")
    if not os.path.isdir(efiles):
        print("скачиваю 32-битное расширение %s (может занять время)" % I386_EXT)
        for spec in (info.get("runtime_remote"), info.get("remote"), DEFAULT_REMOTE):
            if not spec:
                continue
            repo = Repo(resolve_remote(spec), jobs)
            if repo.head_opt(ref) is not None:
                sync(repo, ref, edir, "расширение " + I386_EXT)
                break
        else:
            print("предупреждение: %s не найден ни в одном репозитории" % ref,
                  file=sys.stderr)
            return None
    commit = read_text(os.path.join(edir, ".commit")) or "0"

    # 32-битные программы просят /lib/ld-linux.so.2: подменяем его загрузчиком
    # расширения (иначе берётся старый из Linuxulator -> ошибка GLIBC_PRIVATE)
    found = glob.glob(os.path.join(efiles, "**", "ld-linux.so.2"), recursive=True)
    if not found:
        print("предупреждение: в %s нет ld-linux.so.2" % efiles, file=sys.stderr)
    elif patch_base:
        ld32 = os.path.realpath(found[0])
        link_dir = "/compat/linux/lib" if os.path.isdir("/compat/linux") else "/lib"
        target = link_dir + "/ld-linux.so.2"
        if os.path.lexists(target) and not os.path.islink(target):
            print("предупреждение: %s - обычный файл, не трогаю" % target,
                  file=sys.stderr)
        elif os.path.realpath(target) != ld32:
            if os.path.islink(target):
                print("замена ссылки %s (было -> %s)" % (target, os.readlink(target)),
                      file=sys.stderr)
            root_run("ln", "-sfn", ld32, target)
    return efiles, commit


# ------------------------------------------------------------ запуск
def linux_loaded():
    try:
        dn = subprocess.DEVNULL
        if subprocess.run(["sysctl", "-n", "compat.linux.osrelease"],
                          stdout=dn, stderr=dn).returncode == 0:
            return True
        return subprocess.run(["kldstat", "-q", "-m", "linux64"], stderr=dn).returncode == 0
    except OSError:
        return None


def read_env_file(path):
    out = {}
    txt = read_text(path)
    for line in (txt or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def gio_tls_dir(rdir, rfiles, arch):
    """Каталог только с TLS-модулем GLib: путь по умолчанию в Linuxulator не существует."""
    tri = ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[0]
    src = os.path.join(rfiles, "lib", tri, "gio", "modules", "libgiognutls.so")
    if not os.path.exists(src):
        return None
    d = os.path.join(rdir, "gio-tls")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "libgiognutls.so")
    if not os.path.lexists(dst):
        os.symlink(src, dst)
    return d


def shebang(path):
    """Вернуть (интерпретатор, аргумент) из #!-строки или None."""
    try:
        with open(path, "rb") as f:
            first = f.readline(256)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").split(None, 1)
    if not parts:
        return None
    return parts[0], (parts[1].strip() if len(parts) > 1 else None)


def resolve_interp(interp, afiles, rfiles):
    """Найти интерпретатор скрипта: в рантайме, в приложении, в /compat/linux."""
    base = os.path.basename(interp)
    cands = [os.path.join(rfiles, "bin", base),
             os.path.join(afiles, "bin", base),
             "/compat/linux" + interp,
             interp]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def do_run(info, extra, isolate=False):
    afiles, rfiles, rdir = paths_for(info)
    arch = info["arch"]
    ld = find_ld(rfiles, arch)
    cmd = command_path(info, afiles)
    if ld is None or not os.path.exists(cmd):
        die("приложение не готово. Выполните: install %s" % info["id"])
    if not os.path.exists(os.path.join(afiles, WRAP_MARK)):
        wrap(info, quiet=True)                         # обновить обёртки до новой схемы
    install_interp(afiles, ld)                         # /app/.ld нужен и бинарникам рантайма
    wrap_runtime(rdir, rfiles)
    if linux_loaded() is False:
        print("предупреждение: Linuxulator не загружен (service linux start)", file=sys.stderr)
    if not link_ok(afiles) and not ensure_link(afiles):
        die("ссылка %s должна указывать на %s: через неё ядро находит загрузчик рантайма"
            % (link_path(), afiles))
    ensure_timezone()
    i386 = ensure_i386(info) if wants_i386(info, afiles) else None
    ensure_flatpak_info(info, afiles, rfiles,
                        [(I386_EXT, i386[1])] if i386 else [])

    merged = dict(ENV_DEFAULTS)
    merged.update(read_env_file(P("env")))
    merged.update(read_env_file(P("apps", info["id"], "env")))
    gd = gio_tls_dir(rdir, rfiles, arch)
    if gd:
        merged.setdefault("GIO_MODULE_DIR", gd)
    merged["FLATPAK_ID"] = info["id"]
    env = dict(os.environ)
        # Ensure a usable D-Bus session bus.
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
            try:
                p = subprocess.run(
                    ["dbus-launch", "--sh-syntax"],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                if p.returncode == 0:
                    for line in p.stdout.splitlines():
                        line = line.strip()

                        if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                            value = line.split("=", 1)[1].strip()

                            # dbus-launch outputs:
                            # DBUS_SESSION_BUS_ADDRESS='unix:path=...';
                            if value.endswith(";"):
                                value = value[:-1].rstrip()

                            if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                                value = value[1:-1]
                            elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                                value = value[1:-1]

                            env["DBUS_SESSION_BUS_ADDRESS"] = value

                        elif line.startswith("DBUS_SESSION_BUS_PID="):
                            value = line.split("=", 1)[1].strip()

                            if value.endswith(";"):
                                value = value[:-1].rstrip()

                            if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                                value = value[1:-1]
                            elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                                value = value[1:-1]

                            env["DBUS_SESSION_BUS_PID"] = value

                else:
                    print(
                        "предупреждение: не удалось запустить D-Bus session bus: "
                        + p.stderr.strip(),
                        file=sys.stderr,
                    )

            except OSError as e:
                print(
                    "предупреждение: dbus-launch недоступен: %s" % e,
                    file=sys.stderr,
                )
            print(
            "D-BUS ADDRESS:",
            repr(env.get("DBUS_SESSION_BUS_ADDRESS")),
            file=sys.stderr,
        )
    for k, v in merged.items():
        env.setdefault(k, v)
    ours = "%s/share:%s/share" % (afiles, rfiles)
    env["XDG_DATA_DIRS"] = ours + (":" + env["XDG_DATA_DIRS"] if env.get("XDG_DATA_DIRS") else "")
    env["PATH"] = ":".join([os.path.join(afiles, "bin"), os.path.join(rfiles, "bin"),
                            env.get("PATH", "/usr/bin:/bin")])
    if isolate:
        var = os.path.expanduser("~/.var/app/%s" % info["id"])
        for k, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                       ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
            os.makedirs(os.path.join(var, sub), exist_ok=True)
            env[k] = os.path.join(var, sub)
    else:
        # как в настоящем Flatpak, эти переменные заданы всегда (Steam читает их без .get)
        home = os.path.expanduser("~")
        for k, v in (("XDG_CONFIG_HOME", os.path.join(home, ".config")),
                     ("XDG_DATA_HOME", os.path.join(home, ".local", "share")),
                     ("XDG_CACHE_HOME", os.path.join(home, ".cache")),
                     ("XDG_STATE_HOME", os.path.join(home, ".local", "state"))):
            if not env.get(k):
                os.makedirs(v, exist_ok=True)
                env[k] = v
    if not env.get("XDG_RUNTIME_DIR"):                 # на FreeBSD обычно не задан
        rt = "/tmp/xdg-runtime-%d" % os.getuid()
        os.makedirs(rt, mode=0o700, exist_ok=True)
        env["XDG_RUNTIME_DIR"] = rt
    if not env.get("DISPLAY"):
        print("предупреждение: DISPLAY не задан", file=sys.stderr)
    lp = lib_path(afiles, rfiles, arch)
    if i386:
        lp += ":" + i386[0]     # 64-битные процессы пропустят ELF другого класса
    env["LD_LIBRARY_PATH"] = lp + (":" + env["LD_LIBRARY_PATH"]
                                   if env.get("LD_LIBRARY_PATH") else "")
        # GObject Introspection (.typelib)
    gi_paths = [
        os.path.join(
            afiles,
            "lib",
            ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[0],
            "girepository-1.0",
        ),
        os.path.join(
            rfiles,
            "lib",
            ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[0],
            "girepository-1.0",
        ),
        os.path.join(afiles, "lib", "girepository-1.0"),
        os.path.join(rfiles, "lib", "girepository-1.0"),
    ]

    gi_paths = [p for p in gi_paths if os.path.isdir(p)]

    old_gi = env.get("GI_TYPELIB_PATH")
    if old_gi:
        gi_paths.append(old_gi)

    if gi_paths:
        env["GI_TYPELIB_PATH"] = ":".join(gi_paths)
    if elf_interp(cmd):
        argv = [cmd] + extra                           # PT_INTERP уже указывает на /app/.ld
    else:
        sb = shebang(cmd)
        if sb:
            interp, arg = sb
            via_env = interp.endswith("/env") and arg
            if via_env:                                  # #!/usr/bin/env python3
                real = resolve_interp("/usr/bin/" + arg.split()[0], afiles, rfiles)
            else:
                real = resolve_interp(interp, afiles, rfiles)
            if real is None:
                die("не найден интерпретатор скрипта %s: %s" % (cmd, interp))
            if elf_interp(real):
                argv = [ld, "--library-path", lp, real]
            else:
                argv = [real]                            # например, родной /bin/sh
            if arg and not via_env:
                argv.append(arg)
            argv += [cmd] + extra
        else:
            argv = [ld, "--library-path", lp, cmd] + extra
    os.execve(argv[0], argv, env)


# ------------------------------------------------------------------ chroot
# Режим --chroot повторяет раскладку настоящего Flatpak: /usr - это рантайм,
# /app - приложение, /lib64 -> usr/lib64. Загрузчик и библиотеки всегда из одного
# рантайма, поэтому подмена PT_INTERP, хитрости с LD_LIBRARY_PATH и ссылка
# /compat/linux/app не нужны: и /bin/sh, и env, и rm внутри - те же, что в рантайме.
# Корень лежит в ROOT/chroot/ID. От root выполняются только mount, chroot и umount
# (через doas/sudo), всё остальное - от пользователя.
CHROOT_MODS = ("nullfs", "tmpfs", "fdescfs", "linprocfs", "linsysfs")
I386_DIR = "/usr/lib/i386-linux-gnu"


SPAWN_SHIM = """#!/bin/sh
# flatpak-spawn без портала: на FreeBSD сервиса org.freedesktop.portal.Flatpak нет,
# поэтому команда просто выполняется здесь же, внутри chroot (--host тоже).
while [ $# -gt 0 ]; do
    case "$1" in
        --) shift; break ;;
        --env=*) export "${1#--env=}" ;;
        --unset-env=*) unset "${1#--unset-env=}" ;;
        --directory=*) cd "${1#--directory=}" || exit 1 ;;
        -*) ;;
        *) break ;;
    esac
    shift
done
[ $# -gt 0 ] || exit 0
exec "$@"
"""
SHIM_DIR = "/opt/flatpak-fb/bin"


def root_runner():
    """Префикс для команд от root: [] если мы root, ['doas'|'sudo'], None если нечем."""
    if os.geteuid() == 0:
        return []
    r = [x for x in ("doas", "sudo") if shutil.which(x)][:1]
    return r or None


def chroot_dir(appid):
    return P("chroot", appid)


def _link(path, target):
    """Симлинк path -> target. Чужую ссылку заменяет, обычные файлы не трогает."""
    if os.path.islink(path):
        if os.readlink(path) == target:
            return
        os.unlink(path)
    elif os.path.lexists(path):
        warn("%s - не ссылка, не трогаю" % path)
        return
    os.symlink(target, path)


def _write(path, data, mode=0o644):
    if isinstance(data, str):
        data = data.encode()
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return
    except OSError:
        pass
    if os.path.islink(path):
        os.unlink(path)
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def _mkpoint(path):
    """Каталог-точка монтирования. Битую или абсолютную симлинку (в рантайме такие
    бывают на месте точек расширений) заменяет каталогом; обычный файл - ошибка."""
    if os.path.islink(path):
        if os.path.isabs(os.readlink(path)) or not os.path.isdir(path):
            os.unlink(path)
        else:
            return
    if os.path.isdir(path):
        return
    if os.path.lexists(path):
        die("%s - не каталог, смонтировать сюда нельзя" % path)
    os.makedirs(path, exist_ok=True)


def meta_env(path):
    """Секция [Environment] файла metadata: Flatpak передаёт её приложению как есть."""
    return parse_ini(read_text(path) or "").get("Environment", {})


def start_dbus(env):
    """Если сессионной шины нет - запустить dbus-launch (как это делает do_run)."""
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    try:
        p = subprocess.run(["dbus-launch", "--sh-syntax"], env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        warn("dbus-launch недоступен: %s" % e)
        return
    if p.returncode != 0:
        warn("не удалось запустить D-Bus session bus: " + p.stderr.strip())
        return
    for key in ("DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID"):
        m = re.search(key + r"""=(?:'([^']*)'|"([^"]*)"|([^;\s]+))""", p.stdout)
        if m:
            env[key] = next(g for g in m.groups() if g is not None)


def build_chroot_tree(info, root, rfiles, home, pw, var, i386=False):
    """Каталоги, ссылки и /etc корня. Всё это делается от обычного пользователя."""
    for d in ("usr", "app", "dev", "proc", "sys", "tmp", "run", "etc", "var", "home", "root"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for d in ("tmp", "cache", "log", "lib"):
        p = os.path.join(root, "var", d)
        os.makedirs(p, exist_ok=True)
        os.chmod(p, 0o1777)
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        if name == "lib32" and not os.path.exists(os.path.join(rfiles, "lib32")):
            continue
        _link(os.path.join(root, name), "usr/" + name)
    _link(os.path.join(root, "var", "run"), "../run")
    shim_dir = root + SHIM_DIR
    os.makedirs(shim_dir, exist_ok=True)
    _write(os.path.join(shim_dir, "flatpak-spawn"), SPAWN_SHIM, 0o755)

    # /etc: сгенерированные файлы + ссылки на usr/etc рантайма (как у Flatpak)
    try:
        gname = grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        gname = pw.pw_name
    gen = {
        "passwd": "root:x:0:0:root:/root:/bin/sh\n"
                  "%s:x:%d:%d:%s:%s:/bin/sh\n"
                  "nobody:x:65534:65534:nobody:/nonexistent:/bin/false\n"
                  % (pw.pw_name, pw.pw_uid, pw.pw_gid,
                     pw.pw_gecos.split(",")[0].replace(":", " "), home),
        "group": "root:x:0:\n%s:x:%d:\nnogroup:x:65534:\n" % (gname, pw.pw_gid),
        "hostname": os.uname().nodename + "\n",
    }
    hosts = read_text("/etc/hosts")
    gen["hosts"] = (hosts + "\n") if hosts else "127.0.0.1 localhost\n::1 localhost\n"
    resolv = read_text("/etc/resolv.conf")
    if resolv:
        gen["resolv.conf"] = resolv + "\n"
    else:
        warn("нет /etc/resolv.conf: в chroot не будет DNS")
    tri = ARCH_INFO.get(info["arch"], ARCH_INFO["x86_64"])[0]
    ldc = ["/app/lib", "/app/lib/" + tri]
    if os.path.exists(os.path.join(rfiles, "etc", "ld.so.conf")):
        ldc.append("include /usr/etc/ld.so.conf")
    else:
        ldc.append("/usr/lib/" + tri)
    if i386:
        ldc.append(I386_DIR)
    gen["ld.so.conf"] = "\n".join(ldc) + "\n"
    tz = host_timezone()
    if tz:
        gen["timezone"] = tz + "\n"
    try:
        with open("/etc/localtime", "rb") as f:
            gen["localtime"] = f.read()
    except OSError:
        pass
    etc = os.path.join(root, "etc")
    for name, data in gen.items():
        _write(os.path.join(etc, name), data)
    if not os.path.exists(os.path.join(etc, "machine-id")):
        _write(os.path.join(etc, "machine-id"), os.urandom(16).hex() + "\n")
    cache = os.path.join(etc, "ld.so.cache")
    if os.path.islink(cache):                  # кэш свой, а не ссылка на рантайм (он только для чтения)
        os.unlink(cache)
    skip = set(gen) | {"machine-id", "ld.so.cache"}
    try:
        names = os.listdir(os.path.join(rfiles, "etc"))
    except OSError:
        names = []
    for n in names:
        if n not in skip:
            _link(os.path.join(etc, n), "../usr/etc/" + n)

    # ~ внутри песочницы и ~/.var/app/ID - один и тот же каталог (--persist=. у Flatpak)
    for sub in ("config", "data", "cache", ".local/state"):
        try:
            os.makedirs(os.path.join(var, sub), exist_ok=True)
        except OSError:
            pass
    os.makedirs(os.path.join(var, ".var", "app"), exist_ok=True)
    _link(os.path.join(var, ".var", "app", info["id"]), "../..")


def find_ldconfig(rfiles):
    """Путь к ldconfig внутри корня (/usr = рантайм) или None."""
    for rel in ("sbin/ldconfig", "bin/ldconfig"):
        if os.path.exists(os.path.join(rfiles, rel)):
            return "/usr/" + rel
    return None


def refresh_ld_cache(info, root, rroot, afiles, rfiles, i386, runner, chroot_bin, pw, dry):
    """Как Flatpak: кэш загрузчика с каталогами приложения (/app/lib). Без него
    ctypes.util.find_library и подобные поиски не видят библиотек приложения."""
    ldconfig = find_ldconfig(rfiles)
    if ldconfig is None:
        warn("в рантайме нет ldconfig: ld.so.cache не обновлён")
        return
    stamp = "|".join([read_text(os.path.join(os.path.dirname(afiles), ".commit")) or "0",
                      read_text(os.path.join(os.path.dirname(rfiles), ".commit")) or "0",
                      i386[1] if i386 else "-"])
    sfile = os.path.join(root, "etc", ".ld-stamp")
    if os.path.exists(os.path.join(root, "etc", "ld.so.cache")) and read_text(sfile) == stamp:
        return
    cmd = runner + [chroot_bin, "-u", pw.pw_name, rroot, ldconfig, "-X"]
    if dry:
        print("+ " + shq(cmd))
        return
    if subprocess.run(cmd).returncode == 0:
        _write(sfile, stamp + "\n")
    else:
        warn("ldconfig завершился с ошибкой: библиотеки /app/lib найдутся только через LD_LIBRARY_PATH")


def copy_xauthority(var):
    src = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    if not os.path.isfile(src):
        return False
    dst = os.path.join(var, ".Xauthority")
    try:
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
    except OSError:
        return False
    return True


def _locale_ok(rfiles, v):
    if v in ("", "C", "POSIX") or v.lower().replace("-", "") == "c.utf8":
        return True
    name, _, enc = v.partition(".")
    cand = [v, name + "." + enc.lower().replace("-", "")] if enc else [name]
    return any(os.path.isdir(os.path.join(rfiles, "lib", "locale", c)) for c in cand)


def chroot_env(info, afiles, rfiles, rdir, i386, home, pw, xr, xauth):
    arch = info["arch"]
    tri = ARCH_INFO.get(arch, ARCH_INFO["x86_64"])[0]
    var = "%s/.var/app/%s" % (home, info["id"])
    # как и Flatpak, не тащим в песочницу всё окружение хозяина (SSL_CERT_FILE и т. п.)
    keep = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID",
            "PULSE_SERVER", "PULSE_COOKIE", "TERM", "COLORTERM", "LANG", "LANGUAGE", "TZ",
            "XDG_SESSION_TYPE", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY",
            "HTTPS_PROXY", "NO_PROXY")
    host = {k: v for k, v in os.environ.items()
            if k in keep or k.startswith("LC_")
            or (k.startswith("FLATPAK_") and not k.startswith("FLATPAK_FB_"))}
    layer = dict(meta_env(os.path.join(rdir, "metadata")))
    layer.update(meta_env(os.path.join(os.path.dirname(afiles), "metadata")))
    layer.update(ENV_DEFAULTS)
    layer.update(read_env_file(P("env")))
    layer.update(read_env_file(P("apps", info["id"], "env")))

    env = dict(host)
    env.update({
        "FLATPAK_ID": info["id"],
        "container": "flatpak",
        "PATH": "/app/bin:/usr/bin",
        "XDG_DATA_DIRS": "/app/share:/usr/share:/usr/share/runtime/share",
        "XDG_CONFIG_DIRS": "/app/etc/xdg:/etc/xdg",
    })
    for k, v in layer.items():                    # переменные оболочки сильнее файлов
        env[k] = os.environ[k] if k in os.environ and not k.startswith("LD_") else v
    lp = lib_path("/app", "/usr", arch)
    if i386:
        lp += ":" + I386_DIR                      # 64-битные процессы пропустят чужой класс ELF
    if layer.get("LD_LIBRARY_PATH"):
        lp += ":" + layer["LD_LIBRARY_PATH"]
    env["LD_LIBRARY_PATH"] = lp
    env["PATH"] = SHIM_DIR + ":" + env["PATH"]    # flatpak-spawn-шим первым (портала нет)
    gi = ["/app/lib/%s/girepository-1.0" % tri, "/usr/lib/%s/girepository-1.0" % tri,
          "/app/lib/girepository-1.0", "/usr/lib/girepository-1.0"]
    if env.get("GI_TYPELIB_PATH"):
        gi.append(env["GI_TYPELIB_PATH"])
    env["GI_TYPELIB_PATH"] = ":".join(gi)
    env.update({
        "HOME": home, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
        "XDG_CONFIG_HOME": var + "/config",
        "XDG_DATA_HOME": var + "/data",
        "XDG_CACHE_HOME": var + "/cache",
        "XDG_STATE_HOME": var + "/.local/state",
        "XDG_RUNTIME_DIR": xr,
    })
    if xauth:
        env["XAUTHORITY"] = home + "/.Xauthority"
    # локали живут в отдельном расширении Flatpak; нет нужной - C.UTF-8 вместо предупреждения GTK
    for k in [k for k in env if k == "LANG" or k.startswith("LC_")]:
        if not _locale_ok(rfiles, env[k]):
            if k == "LANG":
                env[k] = "C.UTF-8"
            else:
                del env[k]
    return env


def chroot_mkpoints(root, mounts):
    """Создать точки монтирования. Если точка внутри уже смонтированного каталога
    (например, под /usr или домом), каталог создаётся в его источнике на диске."""
    done = []
    for typ, src, dst, _ in mounts:
        best = None
        for t2, s2 in done:
            if (dst == t2 or dst.startswith(t2.rstrip("/") + "/")) \
                    and (best is None or len(t2) > len(best[0])):
                best = (t2, s2)
        if best is not None and not os.path.isabs(best[1]):
            base = None                           # devfs/tmpfs: каталог создаёт сама ФС
        elif best is not None:
            base = best[1] + dst[len(best[0]):]
        else:
            base = root + dst
        if base is not None and os.path.isabs(src):
            _mkpoint(base)
        done.append((dst, src))


def mounted_under(rroot):
    """Точки монтирования внутри rroot, самые глубокие первыми."""
    try:
        out = subprocess.run(["mount", "-p"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True).stdout
    except OSError:
        return []
    pts = set()
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 2 and (f[1] == rroot or f[1].startswith(rroot + "/")):
            pts.add(f[1].replace("\\040", " "))
    return sorted(pts, key=len, reverse=True)


def shq(cmd):
    return " ".join(shlex.quote(a) for a in cmd)


def umount_tree(root, runner, dry=False):
    rroot = os.path.realpath(root)
    pts = mounted_under(rroot)
    if not pts:
        return True
    script = "; ".join("umount %s || umount -f %s" % (shlex.quote(p), shlex.quote(p))
                       for p in pts)
    if dry:
        print("+ " + shq(runner + ["sh", "-c", script]))
        return True
    subprocess.run(runner + ["sh", "-c", script], stderr=subprocess.DEVNULL)
    left = mounted_under(rroot)
    if left:
        warn("не удалось размонтировать: %s\n  что держит: fuser -cm %s\n"
             "  потом: %s umount ID" % (", ".join(left), left[-1], sys.argv[0]))
        return False
    return True


def live_users(pids):
    out = []
    try:
        names = os.listdir(pids)
    except OSError:
        return out
    for n in names:
        if not n.isdigit() or int(n) == os.getpid():
            continue
        try:
            os.kill(int(n), 0)
            out.append(int(n))
        except ProcessLookupError:
            try:
                os.unlink(os.path.join(pids, n))
            except OSError:
                pass
        except PermissionError:
            out.append(int(n))
    return out


def do_run_chroot(info, extra, binds=(), command=None, dry=False):
    afiles, rfiles, rdir = paths_for(info)
    arch = info["arch"]
    ld = find_ld(rfiles, arch)
    cmd_host = command_path(info, afiles)
    if ld is None or (not command and not os.path.exists(cmd_host)):
        die("приложение не готово. Выполните: install %s" % info["id"])
    runner = root_runner()
    if not dry:
        if not sys.platform.startswith("freebsd"):
            die("--chroot работает только на FreeBSD (для проверки есть --dry-run)")
        if runner is None:
            die("--chroot нужен root для mount и chroot: установите doas или sudo")
    runner = runner or ["sudo"]
    pw = pwd.getpwuid(os.getuid())
    home = pw.pw_dir
    var = os.path.expanduser("~/.var/app/%s" % info["id"])
    root = chroot_dir(info["id"])
    rroot = os.path.realpath(root)

    install_interp(afiles, ld)         # у уже обёрнутых ELF PT_INTERP = /app/.ld
    if not dry and linux_loaded() is False:
        warn("Linuxulator не загружен (service linux start)")
    i386 = ensure_i386(info, patch_base=False) if wants_i386(info, afiles) else None

    os.makedirs(root, exist_ok=True)
    os.makedirs(var, exist_ok=True)
    build_chroot_tree(info, root, rfiles, home, pw, var, bool(i386))
    ensure_flatpak_info(info, "/app", "/usr",
                        [(I386_EXT, i386[1])] if i386 else [], quiet=True,
                        base=root, var="%s/.var/app/%s" % (home, info["id"]))
    xauth = copy_xauthority(var)
    if not os.environ.get("DISPLAY"):
        warn("DISPLAY не задан")
    xr = os.environ.get("XDG_RUNTIME_DIR")
    if not xr:
        xr = "/tmp/xdg-runtime-%d" % os.getuid()
        os.makedirs(xr, mode=0o700, exist_ok=True)

    mounts = [("nullfs", rfiles, "/usr", "ro"),
              ("nullfs", afiles, "/app", "ro")]
    if i386:
        _mkpoint(os.path.join(rfiles, "lib", "i386-linux-gnu"))
        mounts.append(("nullfs", i386[0], I386_DIR, "ro"))
    mounts += [("devfs", "devfs", "/dev", None),
               ("fdescfs", "fdescfs", "/dev/fd", "linrdlnk"),
               ("tmpfs", "tmpfs", "/dev/shm", "mode=1777"),
               ("linprocfs", "linprocfs", "/proc", None),
               ("linsysfs", "linsysfs", "/sys", None),
               ("nullfs", "/tmp", "/tmp", None),
               ("nullfs", var, home, None)]
    if not xr.startswith("/tmp/") and os.path.isdir(xr):
        mounts.append(("nullfs", xr, xr, None))
    if os.path.isdir("/var/run/dbus"):
        mounts.append(("nullfs", "/var/run/dbus", "/run/dbus", None))
    for b in binds:
        src, _, dst = b.partition(":")
        src = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(src):
            die("--bind: %s не каталог" % src)
        mounts.append(("nullfs", src, os.path.abspath(dst or src), None))
    fdirs = []
    for src, dst in (("/usr/local/share/fonts", "/run/host/fonts"),
                     (os.path.expanduser("~/.local/share/fonts"), "/run/host/user-fonts"),
                     (os.path.expanduser("~/.fonts"), "/run/host/user-fonts-legacy")):
        if os.path.isdir(src):
            mounts.append(("nullfs", src, dst, "ro"))
            fdirs.append(dst)
    os.makedirs(os.path.join(root, "run", "host"), exist_ok=True)
    _write(os.path.join(root, "run", "host", "font-dirs.xml"),
           '<?xml version="1.0"?>\n'
           '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n<fontconfig>\n'
           + "".join("  <dir>%s</dir>\n" % d for d in fdirs) + "</fontconfig>\n")
    chroot_mkpoints(root, mounts)

    # env -i обнуляет PATH, поэтому chroot нужен по абсолютному пути
    chroot_bin = shutil.which("chroot") or "/usr/sbin/chroot"
    env = chroot_env(info, afiles, rfiles, rdir, i386, home, pw, xr, xauth)
    start_dbus(env)
    if command:
        inner = command
    else:
        c = info.get("command") or info["id"]
        inner = c if c.startswith("/") else "/app/bin/" + c
    argv = runner + ["env", "-i"] + ["%s=%s" % kv for kv in sorted(env.items())] \
        + [chroot_bin, "-u", pw.pw_name, rroot, inner] + list(extra)

    def mount_cmd(m):
        typ, src, dst, opts = m
        return ["mount", "-t", typ] + (["-o", opts] if opts else []) + [src, rroot + dst]

    already = set(mounted_under(rroot))
    todo = [mount_cmd(m) for m, dst in ((m, rroot + m[2]) for m in mounts)
            if dst not in already]
    mount_script = " && ".join(shq(c) for c in todo)

    if dry:
        print("# корень: %s" % rroot)
        if todo:
            print("+ " + shq(runner + ["sh", "-c", mount_script]))
        refresh_ld_cache(info, root, rroot, afiles, rfiles, i386, runner, chroot_bin, pw, True)
        print("+ " + shq(argv))
        print("# после выхода (если других экземпляров нет):")
        for p in sorted((rroot + m[2] for m in mounts), key=len, reverse=True):
            print("+ " + shq(runner + ["umount", p]))
        return

    pids = root + ".pids"
    os.makedirs(pids, exist_ok=True)
    mine = os.path.join(pids, str(os.getpid()))
    open(mine, "w").close()
    rc = 1
    try:
        if todo:
            for m in CHROOT_MODS:
                if subprocess.run(["kldstat", "-q", "-m", m]).returncode != 0:
                    subprocess.run(runner + ["kldload", m], stderr=subprocess.DEVNULL)
            if subprocess.run(runner + ["sh", "-c", mount_script]).returncode != 0:
                die("не удалось смонтировать корень %s (см. сообщения выше)" % rroot)
        refresh_ld_cache(info, root, rroot, afiles, rfiles, i386, runner, chroot_bin, pw, False)
        proc = subprocess.Popen(argv)
        while True:
            try:
                rc = proc.wait()
                break
            except KeyboardInterrupt:      # SIGINT получил и дочерний процесс
                continue
    finally:
        try:
            os.unlink(mine)
        except OSError:
            pass
        if not live_users(pids):
            umount_tree(root, runner)
    sys.exit(rc if rc >= 0 else 128 - rc)


def cmd_umount(a):
    root = chroot_dir(a.target)
    pids = root + ".pids"
    users = live_users(pids)
    if users and not a.force:
        die("chroot используют процессы %s; --force размонтирует всё равно"
            % ", ".join(map(str, users)))
    runner = root_runner()
    if runner is None:
        die("нужен root: установите doas или sudo")
    if not umount_tree(root, runner):
        sys.exit(1)
    print("размонтировано: %s" % os.path.realpath(root))


# ------------------------------------------------------------------ поиск
# Каталог приложений лежит в ветке appstream/ARCH того же OSTree-репозитория:
# ref -> commit -> dirtree -> файл appstream.xml.gz (один объект .filez).
# Кэш обновляется по хешу коммита: проверка стоит один запрос в 65 байт.
def warn(msg):
    print("предупреждение: " + msg, file=sys.stderr)


def read_filez(d):
    """Содержимое объекта .filez (заголовок GVariant + сырой deflate)."""
    vs = int.from_bytes(d[:4], "big")
    hdr = d[8:8 + vs]
    size = int.from_bytes(hdr[0:8], "big")
    if size == 0:
        return b""
    for start in ((8 + vs + 7) & ~7, 8 + vs):
        try:
            cand = zlib.decompressobj(-15).decompress(d[start:])
            if len(cand) == size:
                return cand
        except zlib.error:
            pass
    raise ValueError("не удалось распаковать объект (size=%d)" % size)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def detect_langs(opt):
    """ru_RU.UTF-8 -> ['ru_RU', 'ru']; C/POSIX -> []."""
    s = opt
    if s is None:
        s = next((os.environ[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG")
                  if os.environ.get(k)), "")
    s = s.split(".")[0].split("@")[0]
    if not s or s in ("C", "POSIX"):
        return []
    langs = [s]
    if "_" in s:
        langs.append(s.split("_")[0])
    return langs


def find_appstream_ref(repo, arch):
    for base in APPSTREAM_REFS:
        ref = "%s/%s" % (base, arch)
        c = repo.head_opt(ref)
        if c is not None:
            return ref, c
    return None, None


def fetch_appstream(repo, commit, label):
    """Один файл из корня коммита, остальное дерево не трогаем."""
    tree, _ = parse_commit(repo.obj(commit, "commit"))
    files, _dirs = parse_dirtree(repo.obj(tree, "dirtree"))
    byname = dict(files)
    for name in APPSTREAM_FILES:
        if name in byname:
            return read_filez(repo.obj(byname[name], "filez", label=label))
    die("в коммите каталога нет файла %s" % " / ".join(APPSTREAM_FILES))


def texts_by_lang(parent, path):
    out = {}
    for e in parent.iterfind(path):
        t = "".join(e.itertext()).strip()
        if t:
            out.setdefault(e.get(XML_LANG), []).append(t)
    return out


def pick(d, langs):
    """Значение для первого найденного языка, иначе значение по умолчанию."""
    for lg in langs:
        if lg in d:
            return d[lg]
    if None in d:
        return d[None]
    return next(iter(d.values()), [])


def describe(comp, langs):
    by = {}
    for desc in comp.iterfind("description"):
        base = desc.get(XML_LANG)
        for ch in desc:
            lg = ch.get(XML_LANG) or base
            if ch.tag == "p":
                t = " ".join("".join(ch.itertext()).split())
                if t:
                    by.setdefault(lg, []).append(t)
            elif ch.tag in ("ul", "ol"):
                for li in ch.iterfind("li"):
                    t = " ".join("".join(li.itertext()).split())
                    if t:
                        by.setdefault(li.get(XML_LANG) or lg, []).append("• " + t)
    return "\n".join(pick(by, langs))[:1500]


def parse_appstream(raw, langs):
    if raw[:2] == b"\x1f\x8b":
        raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
    items, root = {}, None
    for ev, el in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
        if ev == "start":
            if root is None:
                root = el
            continue
        if el.tag != "component":
            continue
        bundle = next(((b.text or "").strip() for b in el.iterfind("bundle")
                       if b.get("type") == "flatpak"), "")
        parts = bundle.split("/")
        if len(parts) == 4 and parts[0] == "app":
            name = texts_by_lang(el, "name")
            summ = texts_by_lang(el, "summary")
            kws = texts_by_lang(el, "keywords/keyword")
            kw = list(kws.get(None, []))
            for lg in langs:
                if lg in kws:
                    kw += kws[lg]
                    break
            dev = (texts_by_lang(el, "developer_name") or
                   texts_by_lang(el, "developer/name"))
            ver = next((r.get("version") for r in el.iterfind("releases/release")
                        if r.get("version")), "")
            lic = next(iter(texts_by_lang(el, "project_license").get(None, [])), "")
            home = next((("".join(u.itertext())).strip() for u in el.iterfind("url")
                         if u.get("type") == "homepage"), "")
            n0 = (name.get(None) or next(iter(name.values()), [""]))[0]
            s0 = (summ.get(None) or next(iter(summ.values()), [""]))[0]
            items[(parts[1], parts[3])] = {
                "id": parts[1], "branch": parts[3], "arch": parts[2],
                "name": (pick(name, langs) or [n0])[0], "name0": n0,
                "summary": (pick(summ, langs) or [s0])[0], "summary0": s0,
                "keywords": kw,
                "categories": [c.text.strip() for c in el.iterfind("categories/category")
                               if c.text],
                "developer": (pick(dev, langs) or [""])[0],
                "license": lic, "homepage": home, "version": ver or "",
                "description": describe(el, langs),
            }
        el.clear()
        if root is not None:
            root.clear()
    return sorted(items.values(), key=lambda x: (x["name"].casefold(), x["id"]))


def load_index(url, label, arch, langs, cached=False, refresh=False):
    """Список приложений репозитория; каталог перекачивается при смене коммита."""
    repo = Repo(url)
    key = hashlib.sha1(url.encode()).hexdigest()[:12]
    cdir = P("appstream", key, arch)
    os.makedirs(cdir, exist_ok=True)
    meta_p, raw_p = os.path.join(cdir, "meta.json"), os.path.join(cdir, "appstream.raw")
    idx_p = os.path.join(cdir, "index-%s.json" % ("_".join(langs) or "C"))
    meta = load_json(meta_p)
    have = bool(meta) and os.path.exists(raw_p)

    if not (cached and have):
        try:
            ref, commit = find_appstream_ref(repo, arch)
        except Exception as e:
            if not have:
                die("репозиторий %s недоступен: %s" % (url, e))
            warn("репозиторий недоступен (%s), использую сохранённый каталог" % e)
            ref = commit = None
        else:
            if ref is None:
                die("в %s нет ветки appstream/%s: у репозитория нет каталога приложений"
                    % (url, arch))
            ch = commit.hex()
            if refresh or not have or meta.get("commit") != ch:
                print("[%s] скачиваю каталог приложений (%s, %s)"
                      % (label, ref, ch[:12]), file=sys.stderr)
                data = fetch_appstream(repo, commit, label)
                write_atomic(raw_p, data)
                meta = {"url": url, "ref": ref, "commit": ch, "time": int(time.time())}
                write_atomic(meta_p, json.dumps(meta).encode())
                for f in os.listdir(cdir):
                    if f.startswith("index-"):
                        os.remove(os.path.join(cdir, f))

    idx = load_json(idx_p)
    if idx and idx.get("commit") == meta.get("commit"):
        return idx["items"]
    with open(raw_p, "rb") as f:
        raw = f.read()
    print("[%s] разбираю каталог..." % label, file=sys.stderr)
    try:
        items = parse_appstream(raw, langs)
    except (ET.ParseError, zlib.error) as e:
        os.remove(raw_p)
        die("каталог повреждён (%s), повторите команду" % e)
    write_atomic(idx_p, json.dumps({"commit": meta.get("commit"), "items": items},
                                   ensure_ascii=False).encode("utf-8"))
    return items


def score(it, words):
    """0 — не подходит; иначе сумма очков по каждому слову (слова связаны по И)."""
    idl = it["id"].lower()
    last = idl.rsplit(".", 1)[-1]
    names = [it["name"].casefold(), it["name0"].casefold()]
    summ = [it["summary"].casefold(), it["summary0"].casefold()]
    kws = [k.casefold() for k in it["keywords"]]
    cats = [c.casefold() for c in it["categories"]]
    total = 0
    for w in words:
        if idl == w:
            s = 100
        elif last == w:
            s = 90
        elif w in names:
            s = 80
        elif any(n.startswith(w) for n in names):
            s = 60
        elif any(w in n for n in names):
            s = 40
        elif any(w in k for k in kws):
            s = 25
        elif w in idl:
            s = 20
        elif any(w in x for x in summ):
            s = 15
        elif any(w in c for c in cats):
            s = 5
        else:
            return 0
        total += s
    return total


def clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:max(n - 1, 0)] + "…"


def show_list(rows, multi):
    width = shutil.get_terminal_size((110, 20)).columns
    nw = min(max(len(r["name"]) for r in rows), 28)
    iw = min(max(len(r["id"]) for r in rows), 46)
    bw = 8
    rw = max(len(r["remote"]) for r in rows) if multi else 0
    fixed = 2 + nw + 1 + iw + 1 + bw + 1 + (rw + 1 if multi else 0)
    sw = max(width - fixed, 10)
    head = "  %-*s %-*s %-*s " % (nw, "Название", iw, "ID", bw, "Ветка")
    if multi:
        head += "%-*s " % (rw, "Репозиторий")
    print(head + "Описание")
    for r in rows:
        line = "%s %-*s %-*s %-*s " % ("*" if r["installed"] else " ",
                                       nw, clip(r["name"], nw), iw, clip(r["id"], iw),
                                       bw, clip(r["branch"], bw))
        if multi:
            line += "%-*s " % (rw, r["remote"])
        print(line + clip(r["summary"], sw))


def show_info(r):
    rows = [("Название", r["name"]), ("ID", r["id"]), ("Ветка", r["branch"]),
            ("Архитектура", r["arch"]), ("Версия", r["version"]),
            ("Разработчик", r["developer"]), ("Лицензия", r["license"]),
            ("Сайт", r["homepage"]), ("Категории", ", ".join(r["categories"])),
            ("Ключевые слова", ", ".join(r["keywords"])),
            ("Репозиторий", r["remote"]),
            ("Установлено", "да" if r["installed"] else "нет")]
    for k, v in rows:
        if v:
            print("%-15s %s" % (k + ":", v))
    if r["summary"]:
        print("\n" + r["summary"])
    if r["description"]:
        print()
        w = min(shutil.get_terminal_size((100, 20)).columns, 100)
        for para in r["description"].split("\n"):
            print(textwrap.fill(para, width=w,
                                subsequent_indent="  " if para.startswith("•") else ""))


def cmd_search(a):
    arch = a.arch or host_arch()
    langs = detect_langs(a.lang)
    specs = a.remote or [DEFAULT_REMOTE]
    multi = len(specs) > 1
    words = [w.casefold() for w in a.query]
    rows = []
    for spec in specs:
        url = resolve_remote(spec)
        label = spec if len(spec) < 30 else url
        for it in load_index(url, label, arch, langs, a.cached, a.refresh):
            s = score(it, words) if words else 1
            if s:
                r = dict(it)
                r.update(score=s, remote=label, installed=os.path.isfile(
                    P("apps", it["id"], it["branch"], "info.json")))
                rows.append(r)
    rows.sort(key=lambda r: (-r["score"], r["name"].casefold(), r["id"]))

    if a.info:
        if not words:
            die("укажите ID или название приложения")
        exact = [r for r in rows if r["id"].casefold() == " ".join(words)]
        pool = exact or (rows if len(rows) == 1 else [])
        if not pool:
            if not rows:
                die("ничего не найдено")
            show_list(rows[:a.limit or None], multi)
            die("\nнесколько совпадений: укажите точный ID")
        if a.json:
            print(json.dumps(pool[0], ensure_ascii=False, indent=1))
        else:
            show_info(pool[0])
        return

    total = len(rows)
    shown = rows[:a.limit] if a.limit > 0 else rows
    if a.json:
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return
    if not shown:
        print("ничего не найдено", file=sys.stderr)
        sys.exit(1)
    show_list(shown, multi)
    print("\nнайдено: %d, показано: %d.  * — установлено.  Установка: %s install ID%s" % (
        total, len(shown), os.path.basename(sys.argv[0]),
        "" if specs[0] == DEFAULT_REMOTE else " --remote " + specs[0]), file=sys.stderr)


# ------------------------------------------------------------ команды
def make_target(a):
    arch = getattr(a, "arch", None) or host_arch()
    t = {"id": a.target, "branch": getattr(a, "branch", None), "arch": arch,
         "remote": getattr(a, "remote", None),
         "runtime_remote": getattr(a, "runtime_remote", None)}
    if a.target.endswith(".flatpakref"):
        s = parse_ini(fetch_text(a.target)).get("Flatpak Ref", {})
        if not s.get("Name"):
            die("в %s нет Name=" % a.target)
        if s.get("IsRuntime", "").lower() == "true":
            die("это ссылка на рантайм, а не на приложение")
        t["id"] = s["Name"]
        t["branch"] = t["branch"] or s.get("Branch")
        t["remote"] = t["remote"] or s.get("Url")
        t["runtime_remote"] = t["runtime_remote"] or s.get("RuntimeRepo")
    elif not re.match(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_-]+)+$", a.target):
        die("не похоже на ID приложения: %s" % a.target)
    return t


def installed_list():
    out = []
    base = P("apps")
    if os.path.isdir(base):
        for aid in sorted(os.listdir(base)):
            for br in sorted(os.listdir(os.path.join(base, aid))):
                if os.path.isfile(os.path.join(base, aid, br, "info.json")):
                    out.append((aid, br))
    return out


def cmd_list():
    items = installed_list()
    if not items:
        print("ничего не установлено")
    for aid, br in items:
        info = load_info(aid, br)
        c = (read_text(os.path.join(app_dir(aid, br), ".commit")) or "?")[:12]
        print("%-40s %-8s %s  runtime=%s" % (aid, br, c, info["runtime"]))


def cmd_update(a):
    items = [(a.target, a.branch)] if a.target else installed_list()
    if not items:
        print("нечего обновлять")
    for aid, br in items:
        info = load_info(aid, br)
        print("== %s (%s)" % (aid, info["branch"]))
        t = {"id": aid, "branch": info["branch"], "arch": info["arch"],
             "remote": info["remote"], "runtime_remote": info.get("runtime_remote")}
        do_install(t, a.jobs, a.force)


def cmd_remove(a):
    info = load_info(a.target, a.branch)
    shutil.rmtree(app_dir(info["id"], info["branch"]))
    base = P("apps", info["id"])
    if not [x for x in os.listdir(base) if os.path.isdir(os.path.join(base, x))]:
        shutil.rmtree(base)
    print("удалено:", info["id"])
    if a.prune:
        used = set()
        for aid, br in installed_list():
            used.add(load_info(aid, br)["runtime"])
        rbase = P("runtimes")
        for rid in (os.listdir(rbase) if os.path.isdir(rbase) else []):
            for ra in os.listdir(os.path.join(rbase, rid)):
                for rb in os.listdir(os.path.join(rbase, rid, ra)):
                    if "%s/%s/%s" % (rid, ra, rb) not in used:
                        shutil.rmtree(os.path.join(rbase, rid, ra, rb))
                        print("удалён рантайм: %s/%s/%s" % (rid, ra, rb))


def cmd_check():
    ll = linux_loaded()
    rows = [
        ("Linuxulator", {True: "включён", False: "НЕ включён: service linux start",
                         None: "не удалось проверить"}[ll]),
        ("resolv.conf (Linux)", "ok" if os.path.exists("/compat/linux/etc/resolv.conf")
         else "нет: sudo cp /etc/resolv.conf /compat/linux/etc/"),
        ("корневые сертификаты", "ok" if os.path.exists(
            "/compat/linux/etc/ssl/certs/ca-certificates.crt") else
         "нет: pkg install ca_root_nss; ln -s /usr/local/share/certs/ca-root-nss.crt "
         "/compat/linux/etc/ssl/certs/ca-certificates.crt"),
        ("ссылка %s" % link_path(), os.path.realpath(link_path())
         if os.path.islink(link_path()) else "нет"),
        ("DISPLAY", os.environ.get("DISPLAY") or "не задан"),
        ("архитектура", host_arch()),
        ("каталог данных", ROOT),
        ("установлено", "%d" % len(installed_list())),
    ]
    for k, v in rows:
        print("%-22s %s" % (k, v))


def main():
    global ROOT
    ap = argparse.ArgumentParser(description="Flatpak на FreeBSD через Linuxulator",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--root", default=os.environ.get(
        "FLATPAK_FB_ROOT", os.path.expanduser("~/.local/share/flatpak-freebsd")))
    ap.add_argument("-j", "--jobs", type=int, default=16)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_install_opts(p):
        p.add_argument("target")
        p.add_argument("--remote")
        p.add_argument("--runtime-remote")
        p.add_argument("--branch")
        p.add_argument("--arch")
        p.add_argument("--force", action="store_true")

    def add_chroot_opts(p):
        p.add_argument("--chroot", action="store_true",
                       help="запуск в корне, собранном как во Flatpak (нужен root: doas/sudo)")
        p.add_argument("--dry-run", action="store_true",
                       help="с --chroot: собрать корень и показать команды, ничего не монтируя")
        p.add_argument("--bind", action="append", default=[], metavar="SRC[:DST]",
                       help="с --chroot: примонтировать каталог (можно несколько раз)")
        p.add_argument("--command", help="с --chroot: запустить это вместо команды приложения "
                                         "(например, --command bash)")

    for name in ("install", "start"):
        add_install_opts(sub.add_parser(name))
    add_chroot_opts(sub.choices["start"])
    sub.choices["start"].add_argument("--isolate", action="store_true",
                                      help="данные приложения в ~/.var/app/ID, как во Flatpak")
    sub.choices["start"].add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("run")
    p.add_argument("target")
    p.add_argument("--branch")
    p.add_argument("--isolate", action="store_true")
    add_chroot_opts(p)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("update")
    p.add_argument("target", nargs="?")
    p.add_argument("--branch")
    p.add_argument("--force", action="store_true")
    sub.add_parser("list")
    sub.add_parser("check")
    p = sub.add_parser("search", help="поиск в каталоге репозитория")
    p.add_argument("query", nargs="*", help="слова для поиска (без них — весь каталог)")
    p.add_argument("-r", "--remote", action="append",
                   help="flathub (по умолчанию), flathub-beta, URL .flatpakrepo или "
                        "репозитория; можно несколько раз")
    p.add_argument("--arch")
    p.add_argument("--lang", help="язык названий: ru, ru_RU (по умолчанию из LANG)")
    p.add_argument("-n", "--limit", type=int, default=25, help="сколько показать (0 — все)")
    p.add_argument("-i", "--info", action="store_true", help="подробности о приложении")
    p.add_argument("-u", "--refresh", action="store_true", help="перекачать каталог")
    p.add_argument("--cached", action="store_true",
                   help="не проверять репозиторий, если каталог уже сохранён")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    for name in ("info", "link", "wrap"):
        p = sub.add_parser(name)
        p.add_argument("target")
        p.add_argument("--branch")
    p = sub.add_parser("umount")
    p.add_argument("target")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("remove")
    p.add_argument("target")
    p.add_argument("--branch")
    p.add_argument("--prune", action="store_true")

    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    os.makedirs(ROOT, exist_ok=True)
    extra = [x for x in getattr(a, "rest", []) if x != "--"]
    if a.cmd in ("run", "start") and not a.chroot and (a.bind or a.command or a.dry_run):
        die("--bind, --command и --dry-run работают только вместе с --chroot")

    def launch(info):
        if a.chroot:
            do_run_chroot(info, extra, a.bind, a.command, a.dry_run)
        else:
            do_run(info, extra, a.isolate)

    if a.cmd == "install":
        info = do_install(make_target(a), a.jobs, a.force)
        ensure_link(paths_for(info)[0])
        print("готово. Запуск: %s run %s" % (sys.argv[0], info["id"]))
    elif a.cmd == "start":
        t = make_target(a)
        try:
            info = load_info(t["id"], t["branch"])
            if find_ld(paths_for(info)[1], info["arch"]) is None:
                raise SystemExit
        except SystemExit:
            print("не установлено, ставлю (может занять время)")
            info = do_install(t, a.jobs, a.force)
        launch(info)
    elif a.cmd == "run":
        launch(load_info(a.target, a.branch))
    elif a.cmd == "umount":
        cmd_umount(a)
    elif a.cmd == "update":
        cmd_update(a)
    elif a.cmd == "search":
        cmd_search(a)
    elif a.cmd == "list":
        cmd_list()
    elif a.cmd == "info":
        info = load_info(a.target, a.branch)
        print(json.dumps(info, indent=1, ensure_ascii=False))
        print("файлы приложения:", paths_for(info)[0])
    elif a.cmd == "remove":
        cmd_remove(a)
    elif a.cmd == "link":
        ensure_link(paths_for(load_info(a.target, a.branch))[0])
    elif a.cmd == "wrap":
        wrap(load_info(a.target, a.branch))
    elif a.cmd == "check":
        cmd_check()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("\nпрервано (повторный запуск продолжит с места остановки)")
