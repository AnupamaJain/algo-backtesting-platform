"""sitemap.xml, robots.txt and the canonical origin.

Search engines need three things before any keyword work matters: a list of
the pages that exist, permission to read them, and one unambiguous URL per
page. None of it helps while the site is only reachable on localhost --
these are the prerequisites, not the strategy.

The origin comes from ``VRIDDHIX_PUBLIC_URL``. It has no default beyond
localhost on purpose: emitting ``https://example.com`` canonicals from a dev
box is how a staging site ends up telling Google that production is a
duplicate of it.
"""

from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Request, Response

from ..deps import ProvenanceDep, SessionDep

router = APIRouter(tags=["seo"], include_in_schema=False)


def public_origin(request: Request | None = None) -> str:
    """The origin to publish in canonical URLs and the sitemap."""
    configured = os.getenv("VRIDDHIX_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:8787"


def is_public(request: Request | None = None) -> bool:
    """Whether this instance is actually reachable from the internet.

    A local instance must not invite crawling: robots.txt below disallows
    everything until VRIDDHIX_PUBLIC_URL says otherwise.
    """
    return bool(os.getenv("VRIDDHIX_PUBLIC_URL", "").strip())


#: Pages worth indexing, with how often they genuinely change. Frequencies
#: are honest rather than aspirational -- claiming hourly updates on a page
#: that changes monthly trains crawlers to ignore the field.
INDEXABLE = [
    ("/",       "daily",   "1.0"),
    ("/learn",  "monthly", "0.8"),
    ("/signup", "yearly",  "0.4"),
]


@router.get("/robots.txt", response_class=Response)
def robots(request: Request) -> Response:
    origin = public_origin(request)

    if not is_public(request):
        # Local or unconfigured: refuse crawling outright rather than risk a
        # dev instance competing with production in the index.
        body = (
            "# This instance has no VRIDDHIX_PUBLIC_URL configured and is\n"
            "# treated as private. Set it before inviting crawlers.\n"
            "User-agent: *\n"
            "Disallow: /\n"
        )
        return Response(body, media_type="text/plain")

    body = f"""# Tathya
User-agent: *
Allow: /$
Allow: /learn
Allow: /signup

# Account-scoped and machine surfaces. Nothing here is useful in an index,
# and /api/ in particular would burn crawl budget on JSON.
Disallow: /api/
Disallow: /login

Sitemap: {origin}/sitemap.xml
"""
    return Response(body, media_type="text/plain")


@router.get("/sitemap.xml", response_class=Response)
def sitemap(request: Request, session: SessionDep, prov: ProvenanceDep) -> Response:
    """Every indexable page, with a lastmod that reflects real change.

    The landing page moves whenever a scan lands, so its lastmod is the last
    scan date rather than today -- a sitemap that claims every page changed
    today is one a crawler learns to distrust.
    """
    origin = public_origin(request)
    scanned = prov.as_of.isoformat() if prov.as_of else date.today().isoformat()
    today = date.today().isoformat()

    urls = []
    for path, freq, priority in INDEXABLE:
        lastmod = scanned if path == "/" else today
        urls.append(
            "  <url>\n"
            f"    <loc>{origin}{path}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemap.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    ).replace("www.sitemap.org", "www.sitemaps.org")

    return Response(body, media_type="application/xml")
