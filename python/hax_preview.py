"""Preview the course as a HAX site (real HAX theme) in the browser.

A static HAX site is an index.html that loads HAX's web components (from
HAX's CDN), which read site.json and render the pages with a theme. This
module produces those files from the course site items. No HAX server is
needed.

Security: HAX's components are third-party JavaScript. The preview is
served at a signed, short-lived link that needs no sign-in cookie, with a
Content-Security-Policy ``sandbox`` (no allow-same-origin), so the scripts
run in an isolated origin and can never see or use the instructor's session.
"""

from __future__ import annotations

from html import escape
import json
import os

DEFAULT_CDN = "https://cdn.hax.cloud/cdn/"
# HAX themes offered for the course site, chosen after rendering real built courses in each:
# Learn Two (sidebar course outline) reads most like a course; the others are cleaner alternatives.
THEMES = {
    "learn-two-theme": ("Learn Two", "@haxtheweb/learn-two-theme/learn-two-theme.js"),
    "clean-two": ("Clean Two", "@haxtheweb/clean-two/clean-two.js"),
    "twenty-six-theme": ("Twenty Six", "@haxtheweb/twenty-six-theme/twenty-six-theme.js"),
    "clean-one": ("Clean One", "@haxtheweb/clean-one/clean-one.js"),
}
DEFAULT_THEME = "learn-two-theme"


def theme(element=None):
    element = element if element in THEMES else DEFAULT_THEME
    name, path = THEMES[element]
    return {"element": element, "path": path, "name": name, "variables": {}}


THEME = theme()
TOKEN_SECONDS = 2 * 3600


def cdn():
    value = os.environ.get("HAX_CDN_URL", DEFAULT_CDN).strip()
    return value if value.endswith("/") else value + "/"


def site_json(site_id, title, items, places, theme_element=None):
    """HAX JSON Outline Schema manifest for the course site."""
    entries = []
    parent = None
    for order, item in enumerate(items):
        slug = places[item["id"]].split("/")[1]
        is_module = item["id"].startswith("module--")
        entries.append({
            "id": "item-" + slug, "title": item["title"], "location": f"pages/{slug}/index.html",
            "slug": slug, "order": order, "indent": 0 if is_module or item.get("module") is None else 1,
            "parent": None if is_module or item.get("module") is None else parent,
            "description": "", "metadata": {"published": True, "revised": bool(item.get("revised")),
                         "audience": item.get("audience", "student")},
        })
        if is_module:
            parent = "item-" + slug
    return {
        "id": site_id, "title": title, "author": "", "description": f"Course site for {title}",
        "license": "by-sa",
        "metadata": {"site": {"name": title, "created": 0, "updated": 0}, "theme": theme(theme_element),
                     "node": {"fields": {}}},
        "items": entries,
    }


def index_html(title, base_path):
    base = cdn()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="{escape(base_path, quote=True)}">
<title>{escape(title)}: HAX preview</title>
<link rel="preconnect" href="{escape(base, quote=True)}" crossorigin>
<style>
  .cc-hax-banner {{ position: fixed; z-index: 10000; bottom: 12px; right: 12px; background: #142e50; color: #fff;
    font: 600 13px/1.4 system-ui, sans-serif; padding: 8px 12px; border-radius: 8px; box-shadow: 0 2px 8px #0004; }}
</style>
<script src="config.js"></script>
</head>
<body>
<div class="cc-hax-banner" role="note">Private HAX preview, not published</div>
<haxcms-site-builder id="site" file="site.json"></haxcms-site-builder>
<script src="{escape(base, quote=True)}build-haxcms.js"></script>
<script src="{escape(base, quote=True)}build.js"></script>
</body>
</html>
"""


def config_js():
    """Globals HAX's loaders read; served as a file so the page needs no inline script."""
    base = json.dumps(cdn())
    return ("globalThis.MicroFrontendRegistryConfig = globalThis.MicroFrontendRegistryConfig || {};\n"
            "globalThis.HAXCMSContext = 'published';\n"
            f"globalThis.__appCDN = {base};\nglobalThis.WCGlobalCDNPath = {base};\n"
            "globalThis.__appCustomEnv = true; // no site-specific custom bundle\n"
            "globalThis.__appCDNBlockFallback = true;\n")


def page_html(item, places=None):
    """A HAX page is an HTML fragment (already sanitized by the course pipeline).

    Links between course pages become links to the other page's address in
    the HAX site (``<slug>``, relative to the site root, as HAX routes them).
    """
    from imscc_content import rewrite_import_item_links, sanitize_import_html
    html = sanitize_import_html(item["html"])
    if places:
        html = rewrite_import_item_links(html, {i: loc.split("/")[1] for i, loc in places.items()})
    return html


def policy(app_origin):
    """Isolated origin for third-party HAX scripts; content comes only from here and HAX's CDN."""
    base = cdn().rstrip("/")
    origin = base.split("/", 3)[:3]
    cdn_origin = "/".join(origin)
    return ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
            f"default-src 'none'; script-src {app_origin} {cdn_origin}; "
            f"style-src 'unsafe-inline' {cdn_origin} https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
            f"font-src {cdn_origin} https://fonts.gstatic.com https://cdnjs.cloudflare.com data:; "
            f"img-src {app_origin} {cdn_origin} https: data:; media-src https:; "
            f"connect-src {app_origin} {cdn_origin}; frame-src https:; base-uri {app_origin}; "
            "form-action 'none'; object-src 'none'")
