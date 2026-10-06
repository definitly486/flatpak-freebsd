#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flatpak-search: поиск Flatpak-приложений в репозитории без flatpak и ostree.
Принцип тот же, что у flatpak-freebsd.py: OSTree-репозиторий читается напрямую по HTTP.

Как работает:
  1. refs/heads/appstream/ARCH даёт хеш коммита с каталогом приложений (65 байт);
  2. commit -> dirtree -> объект .filez файла appstream.xml.gz скачивается ОДИН раз
     и кэшируется; при следующих запусках качается только если хеш коммита изменился;
  3. из AppStream-XML берутся ID, название, описание, ключевые слова, категории;
     поиск идёт локально.

Использование:
  flatpak-search.py firefox                 поиск (все слова должны встретиться)
  flatpak-search.py текстовый редактор      поиск работает и по локализованным названиям
  flatpak-search.py -i org.gnome.Calculator подробности о приложении
  flatpak-search.py -n 0                    весь каталог
  flatpak-search.py -r flathub-beta vlc     другой репозиторий (как в flatpak-freebsd.py)
  flatpak-search.py -u                      принудительно перекачать каталог
  flatpak-search.py --cached vlc            не ходить в сеть, если кэш уже есть
  flatpak-search.py --json vlc              вывод в JSON

Звёздочка * в списке — приложение уже установлено через flatpak-freebsd.py.
Установка: flatpak-freebsd.py install ID

Данные: ~/.local/share/flatpak-freebsd/appstream (или --root / $FLATPAK_FB_ROOT).
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import textwrap
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from configparser import ConfigParser

KNOWN_REMOTES = {
    "flathub": "https://dl.flathub.org/repo/",
    "flathub-beta": "https://dl.flathub.org/beta-repo/",
}
DEFAULT_REMOTE = "flathub"
APPSTREAM_REFS = ["appstream", "appstream2"]          # что пробуем в репозитории
APPSTREAM_FILES = ["appstream.xml.gz", "appstream.xml"]
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

ROOT = None


def P(*a):
    return os.path.join(ROOT, *a)


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print("предупреждение: " + msg, file=sys.stderr)


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


def read_filez(d):
    """Содержимое объекта .filez (заголовок GVariant + сырой deflate)."""
    vs = int.from_bytes(d[:4], "big")
    hdr = d[8:8 + vs]                                  # (tuuuusa(ayay)), big-endian
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


# --------------------------------------------------------- клиент OSTree/HTTP
class Repo:
    def __init__(self, base):
        self.base = base if base.endswith("/") else base + "/"

    def get(self, path, tries=5, label=None):
        for i in range(tries):
            try:
                req = urllib.request.Request(self.base + path,
                                             headers={"User-Agent": "ostree"})
                buf = bytearray()
                with urllib.request.urlopen(req, timeout=30) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        buf += chunk
                        if label and sys.stderr.isatty():
                            print("\r[%s] %.1f/%.1f MiB   " % (
                                label, len(buf) / 1048576, total / 1048576),
                                end="", file=sys.stderr, flush=True)
                if label and sys.stderr.isatty():
                    print(file=sys.stderr)
                return bytes(buf)
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


# ---------------------------------------------------------------- вспомогательное
def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


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


# ------------------------------------------------------- каталог AppStream
def find_appstream_ref(repo, arch):
    for base in APPSTREAM_REFS:
        ref = "%s/%s" % (base, arch)
        c = repo.head_opt(ref)
        if c is not None:
            return ref, c
    return None, None


def fetch_appstream(repo, commit, label):
    """Один файл из корня коммита: commit -> dirtree -> filez."""
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


def load_index(url, arch, langs, a):
    """Список приложений репозитория (кэш обновляется по хешу коммита appstream)."""
    repo = Repo(url)
    key = hashlib.sha1(url.encode()).hexdigest()[:12]
    cdir = P("appstream", key, arch)
    os.makedirs(cdir, exist_ok=True)
    meta_p, raw_p = os.path.join(cdir, "meta.json"), os.path.join(cdir, "appstream.raw")
    idx_p = os.path.join(cdir, "index-%s.json" % ("_".join(langs) or "C"))
    meta = load_json(meta_p)
    have = bool(meta) and os.path.exists(raw_p)

    if not (a.cached and have):
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
            if a.refresh or not have or meta.get("commit") != ch:
                print("[%s] скачиваю каталог приложений (%s, %s)"
                      % (a.label(url), ref, ch[:12]), file=sys.stderr)
                data = fetch_appstream(repo, commit, a.label(url))
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
    print("[%s] разбираю каталог..." % a.label(url), file=sys.stderr)
    try:
        items = parse_appstream(raw, langs)
    except (ET.ParseError, zlib.error) as e:
        os.remove(raw_p)
        die("каталог повреждён (%s), повторите команду" % e)
    write_atomic(idx_p, json.dumps({"commit": meta.get("commit"), "items": items},
                                   ensure_ascii=False).encode("utf-8"))
    return items


# ------------------------------------------------------------------ поиск
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


def is_installed(it):
    return os.path.isfile(P("apps", it["id"], it["branch"], "info.json"))


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
        for para in r["description"].split("\n"):
            print(textwrap.fill(para, width=min(shutil.get_terminal_size((100, 20)).columns, 100),
                                subsequent_indent="  " if para.startswith("•") else ""))


def main():
    global ROOT
    ap = argparse.ArgumentParser(description="Поиск Flatpak-приложений без flatpak/ostree",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("query", nargs="*", help="слова для поиска (без них — весь каталог)")
    ap.add_argument("-r", "--remote", action="append",
                    help="flathub (по умолчанию), flathub-beta, URL .flatpakrepo или "
                         "репозитория; можно несколько раз")
    ap.add_argument("--arch", help="архитектура (по умолчанию — хост)")
    ap.add_argument("--lang", help="язык названий, например ru или ru_RU (по умолчанию из LANG)")
    ap.add_argument("-n", "--limit", type=int, default=25, help="сколько показать (0 — все)")
    ap.add_argument("-i", "--info", action="store_true", help="подробности о приложении")
    ap.add_argument("-u", "--refresh", action="store_true", help="перекачать каталог")
    ap.add_argument("--cached", action="store_true",
                    help="не проверять репозиторий, если каталог уже сохранён")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--root", default=os.environ.get(
        "FLATPAK_FB_ROOT", os.path.expanduser("~/.local/share/flatpak-freebsd")))
    a = ap.parse_args()

    ROOT = os.path.abspath(a.root)
    arch = a.arch or host_arch()
    langs = detect_langs(a.lang)
    specs = a.remote or [DEFAULT_REMOTE]
    multi = len(specs) > 1
    names = {}
    a.label = lambda url: names.get(url, url)

    words = [w.casefold() for w in a.query]
    rows = []
    for spec in specs:
        url = resolve_remote(spec)
        names[url] = spec if len(spec) < 30 else url
        for it in load_index(url, arch, langs, a):
            s = score(it, words) if words else 1
            if s:
                r = dict(it)
                r.update(score=s, remote=spec if len(spec) < 30 else url,
                         installed=is_installed(it))
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
    print("\nнайдено: %d, показано: %d.  * — установлено.  Установка: "
          "flatpak-freebsd.py install ID%s" % (
              total, len(shown),
              "" if specs[0] == DEFAULT_REMOTE else " --remote " + specs[0]),
          file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("\nпрервано")
