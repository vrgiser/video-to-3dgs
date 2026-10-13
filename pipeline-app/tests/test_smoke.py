"""Smoke tests for static mounts and critical routes.

Guards against the two regressions we already hit once:
  1. The /viewer-static mount disappearing (originally /juzhou was missing).
  2. The /viewer/{job_id} route being shadowed by the static mount.
"""
import pytest
from fastapi.testclient import TestClient

from pipeline_app.api import app


@pytest.fixture
def client():
    return TestClient(app)


# --- /viewer-static mount (the SuperSplat build) --------------------------

class TestViewerStaticMount:
    """The SuperSplat build directory must be reachable under /viewer-static/."""

    def test_index_js(self, client):
        r = client.get("/viewer-static/index.js")
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 100_000  # > 100 KB — sanity check it's not stub HTML

    def test_index_css(self, client):
        r = client.get("/viewer-static/index.css")
        assert r.status_code == 200
        assert "css" in r.headers["content-type"]

    def test_index_html(self, client):
        r = client.get("/viewer-static/index.html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# --- /static mount (the SPA) ----------------------------------------------

class TestSpaStaticMount:
    """The SPA shell must be reachable under /static/."""

    def test_app_js(self, client):
        r = client.get("/static/app.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_app_css(self, client):
        r = client.get("/static/app.css")
        assert r.status_code == 200
        assert "css" in r.headers["content-type"]

    def test_index_html(self, client):
        r = client.get("/static/index.html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# --- /viewer/{job_id} route must not be shadowed by the mount -------------

class TestViewerRouteNotShadowed:
    """Regression: the /viewer/{job_id} route lives at /viewer/* and the
    static mount at /viewer-static/*. Both must work in isolation. The way
    to catch mount-shadowing is: hit each prefix and assert the right handler
    answered (route → "job not found"; mount → file content)."""

    def test_viewer_static_still_serves(self, client):
        """Sanity: mount still works even with route registered."""
        r = client.get("/viewer-static/index.js")
        assert r.status_code == 200

    def test_viewer_route_answered_by_handler(self, client):
        """A non-existent job ID must produce "job not found" (handler 404),
        not a generic {"detail":"Not Found"} (which would mean no route matched,
        i.e. the route was shadowed)."""
        r = client.get("/viewer/00000000000000000000000000000000")
        assert r.status_code == 404
        detail = r.json().get("detail", "")
        assert "job not found" in detail.lower(), (
            f"expected 'job not found' from route handler, got {detail!r} — "
            f"the route is likely being shadowed by the static mount"
        )

    def test_viewer_static_index_js_not_under_viewer_prefix(self, client):
        """/viewer/index.js must NOT serve the static file — there's no mount
        at /viewer/ (we deliberately moved it to /viewer-static/). If this
        returns 200, something added the mount back at /viewer/ and is
        shadowing the route."""
        r = client.get("/viewer/index.js")
        assert r.status_code == 404, (
            f"expected 404 (no /viewer/ mount by design), got {r.status_code}: "
            f"this means a mount at /viewer/ exists and shadows the route"
        )


# --- API endpoints the SPA depends on at load ----------------------------

class TestApiSmoke:
    """Sanity checks for endpoints hit during initial page load."""

    def test_jobs_list(self, client):
        r = client.get("/api/jobs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_spa_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # The SPA shell should reference app.js with the cache-buster.
        assert "/static/app.js" in r.text


# --- Static assets on disk (catch missing-files breakage) -----------------

class TestStaticAssetsOnDisk:
    """Files that the server reads at runtime (not via HTTP mount) must exist
    on disk. If a deploy step forgets them, the server 500s on the first
    request that needs them — this catches it at test time instead."""

    def test_export_template_exists(self):
        from pipeline_app.config import VIEWER_SINGLE_FILE
        assert VIEWER_SINGLE_FILE.is_file(), (
            f"missing export template: {VIEWER_SINGLE_FILE}"
        )
        # Sanity: the template is ~10MB (viewer + sample SOG). A truncated or
        # stub file would slip past an existence check.
        assert VIEWER_SINGLE_FILE.stat().st_size > 1_000_000, (
            f"export template suspiciously small: "
            f"{VIEWER_SINGLE_FILE.stat().st_size} bytes"
        )

    def test_viewer_dir_exists(self):
        from pipeline_app.config import VIEWER_DIR
        assert VIEWER_DIR.is_dir(), f"missing viewer dir: {VIEWER_DIR}"
        # The mount depends on these specific files.
        for name in ("index.html", "index.js", "index.css"):
            assert (VIEWER_DIR / name).is_file(), (
                f"missing viewer asset: {VIEWER_DIR / name}"
            )
