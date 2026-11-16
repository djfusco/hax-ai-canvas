"""Images that pages use, when the instructor chooses to include them.

By default nothing but text leaves the instructor's computer, and that stays the default. Without
images, though, every picture in the course is a broken link on the HAX site: a Canvas export
refers to its files as ``$IMS-CC-FILEBASE$/...``, which only Canvas can resolve. So the upload
step offers, as an explicit choice, to include the images the course pages actually show. Only
web-sized raster images are taken (never video, audio, documents or SVG), within small limits.

  collect(archive, names)        images in a slim package -> [{"path", "name", "mime", "data"}]
  resolve(html, stored, base)    point each image at its stored copy; say plainly what is missing

``resolve`` also runs when no images were included: a picture or file that cannot be shown becomes
a short note in the text instead of a broken image or a dead link.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

IMAGE_PREFIX = "course_images/"      # in the slim package; the rest is the path under web_resources/
MAX_IMAGE_BYTES = 1024 * 1024        # per image
MAX_IMAGES_BYTES = 6 * 1024 * 1024   # per course (HAX Lite accepts a 10 MiB archive by default)
MAX_IMAGES = 200
FILEBASE = re.compile(r"(?:\$|%24)IMS(?:-|_)CC(?:-|_)FILEBASE(?:\$|%24)/([^\"'?#\s>]+)", re.IGNORECASE)
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def sniff(data):
    """The image type its bytes say it is, or None. The file name is not trusted."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def collect(archive, names):
    """Read the images a slim package carries. Returns (files, warnings); bad entries are skipped."""
    files, warnings, used, total = [], [], set(), 0
    for name in sorted(n for n in names if n.startswith(IMAGE_PREFIX)):
        relative = name[len(IMAGE_PREFIX):]
        suffix = PurePosixPath(relative).suffix.lower()
        info = archive.getinfo(name)
        if suffix not in TYPES or not relative or ".." in PurePosixPath(relative).parts:
            continue
        if len(files) >= MAX_IMAGES or info.file_size > MAX_IMAGE_BYTES or total + info.file_size > MAX_IMAGES_BYTES:
            warnings.append(f"Image not included (over the size limits): {PurePosixPath(relative).name}")
            continue
        with archive.open(name) as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
        mime = sniff(data)
        if len(data) > MAX_IMAGE_BYTES or mime is None:
            warnings.append(f"Image not included (not a PNG, JPEG, GIF or WebP image): {PurePosixPath(relative).name}")
            continue
        stem = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(relative).stem.lower()).strip("-")[:60] or "image"
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[mime]
        stored, number = stem + extension, 1
        while stored in used:
            number += 1
            stored = f"{stem}-{number}{extension}"
        used.add(stored)
        total += len(data)
        files.append({"path": "web_resources/" + relative, "name": stored, "mime": mime, "data": data})
    return files, warnings


def stored_names(model):
    """Export path -> stored file name, for the images kept with this course."""
    return {m["path"]: m["stored"] for m in model.get("media_inventory", [])
            if isinstance(m.get("stored"), str) and NAME.match(m["stored"])}


def _export_path(value):
    match = FILEBASE.search(value or "")
    return "web_resources/" + unquote(match.group(1)) if match else None


def _shown(value):
    """Whether a browser outside Canvas can load this address."""
    return str(value or "").startswith(("http://", "https://", "data:image/"))


def resolve(html, stored, base="files/"):
    """Point images at their stored copies and replace what cannot be shown with a plain note.

    ``stored`` maps export paths to stored file names; ``base`` is where those files are served,
    relative to the page. Links to other course pages (already rewritten) are left alone.
    """
    if not html or ("<img" not in html and "FILEBASE" not in html.upper() and "/files/" not in html
                    and "<video" not in html and "<audio" not in html):
        return html
    from imscc_content import ITEM_PREFIX
    soup = BeautifulSoup(html, "html.parser")

    def note(text):
        tag = soup.new_tag("em")
        tag.string = text
        return tag

    for image in soup.find_all("img"):
        source = str(image.get("src") or "")
        name = stored.get(_export_path(source) or "")
        if name:
            image["src"] = base + quote(name)
        elif not _shown(source):
            label = str(image.get("alt") or "").strip()
            image.replace_with(note(f"[Image not included{': ' + label if label else ''}]"))
    for media in soup.find_all(["video", "audio"]):
        sources = [str(media.get("src") or "")] + [str(s.get("src") or "") for s in media.find_all("source")]
        if not any(_shown(s) for s in sources if s):
            media.replace_with(note(f"[{media.name.capitalize()} in the Canvas course, not included here]"))
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if href.startswith(("#", "mailto:", "tel:", ITEM_PREFIX)) or _shown(href) or href.startswith(base):
            continue
        name = stored.get(_export_path(href) or "")
        if name:
            link["href"] = base + quote(name)
        elif _export_path(href) or href.startswith("/") or href.startswith("$") or href.startswith("%24"):
            # A file or page that lives only inside Canvas: keep the words, drop the dead link.
            link.insert_after(note(" (in the Canvas course)"))
            link.unwrap()
    return str(soup)
