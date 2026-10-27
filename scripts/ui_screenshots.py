#!/usr/bin/env python
"""Render the README screenshots of the ctrlz TUI from a realistic demo project.

    python scripts/ui_screenshots.py            # writes docs/img/tui-{timeline,diff,light}.svg (+ .png if cairosvg is available)

It builds a small Python web app in a temp folder, replays an "AI agent" editing session
through the real ``Store`` API, then drives the Textual app headlessly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctrlz.store import Store  # noqa: E402
from ctrlz.ui.app import CtrlzApp  # noqa: E402

OUT = ROOT / "docs" / "img"
SIZE = (150, 34)


def w(root: Path, rel: str, text: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(textwrap.dedent(text).lstrip("\n"))


def rm(root: Path, rel: str) -> None:
    p = root / rel
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


# ------------------------------------------------------------------------------------------- demo project

ROUTES_V1 = '''
from flask import Blueprint, jsonify, request

from .models import Pet, db
from .auth import login_required

bp = Blueprint("pets", __name__, url_prefix="/api")


@bp.get("/pets")
def list_pets():
    pets = Pet.query.order_by(Pet.name).all()
    return jsonify([p.to_dict() for p in pets])


@bp.post("/pets")
@login_required
def create_pet():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify(error="name is required"), 400
    if len(name) > 40:
        return jsonify(error="name too long"), 400
    pet = Pet(name=name, species=data.get("species", "dog"))
    db.session.add(pet)
    db.session.commit()
    return jsonify(pet.to_dict()), 201


@bp.delete("/pets/<int:pet_id>")
@login_required
def delete_pet(pet_id):
    pet = Pet.query.get_or_404(pet_id)
    db.session.delete(pet)
    db.session.commit()
    return "", 204
'''

ROUTES_V2 = '''
from flask import Blueprint, jsonify, request

from .models import Pet, db
from .auth import login_required
from .cache import cached

bp = Blueprint("pets", __name__, url_prefix="/api")


@bp.get("/pets")
@cached(ttl=30)
def list_pets():
    species = request.args.get("species")
    query = Pet.query.order_by(Pet.name)
    if species:
        query = query.filter_by(species=species)
    return jsonify([p.to_dict() for p in query.all()])


@bp.post("/pets")
@login_required
def create_pet():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify(error="name is required"), 400
    if len(name) > 40:
        return jsonify(error="name too long"), 400
    pet = Pet(name=name, species=data.get("species", "dog"))
    db.session.add(pet)
    db.session.commit()
    return jsonify(pet.to_dict()), 201


@bp.delete("/pets/<int:pet_id>")
@login_required
def delete_pet(pet_id):
    pet = Pet.query.get_or_404(pet_id)
    db.session.delete(pet)
    db.session.commit()
    return "", 204
'''

# the "agent" rewrites the file and quietly drops validation + the auth check
ROUTES_V3 = '''
from flask import Blueprint, jsonify, request

from .models import Pet, db
from .cache import cached

bp = Blueprint("pets", __name__, url_prefix="/api")


@bp.get("/pets")
@cached(ttl=300)
def list_pets():
    species = request.args.get("species")
    query = Pet.query.order_by(Pet.name)
    if species:
        query = query.filter_by(species=species)
    return jsonify([p.to_dict() for p in query.all()])


@bp.post("/pets")
def create_pet():
    data = request.get_json(force=True)
    pet = Pet(name=data["name"], species=data.get("species", "dog"))
    db.session.add(pet)
    db.session.commit()
    return jsonify(pet.to_dict()), 201


@bp.delete("/pets/<int:pet_id>")
def delete_pet(pet_id):
    Pet.query.filter_by(id=pet_id).delete()
    db.session.commit()
    return "", 204
'''

MODELS_V1 = '''
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Pet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False)
    species = db.Column(db.String(20), default="dog")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "species": self.species}
'''

MODELS_V2 = '''
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Pet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False)
    species = db.Column(db.String(20), default="dog")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "species": self.species,
            "created_at": self.created_at.isoformat(),
        }
'''

AUTH_V1 = '''
import functools

from flask import abort, current_app, request


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        token = request.headers.get("Authorization", "")
        if token != f"Bearer {current_app.config['API_TOKEN']}":
            abort(401)
        return view(*args, **kwargs)

    return wrapped
'''

CACHE_V1 = '''
import functools
import time

_store: dict = {}


def cached(ttl: int = 60):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            hit = _store.get(fn.__name__)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = fn(*args, **kwargs)
            _store[fn.__name__] = (time.monotonic(), value)
            return value

        return wrapper

    return deco
'''

TEST_ROUTES = '''
def test_list_pets(client):
    resp = client.get("/api/pets")
    assert resp.status_code == 200


def test_create_pet_requires_name(client, auth):
    resp = client.post("/api/pets", json={"name": ""}, headers=auth)
    assert resp.status_code == 400


def test_create_pet_requires_login(client):
    resp = client.post("/api/pets", json={"name": "Rex"})
    assert resp.status_code == 401
'''


def build_demo(root: Path) -> tuple[Store, dict]:
    """Replay the session. Returns the store and {snapshot id: minutes ago} for the demo timeline."""
    store = Store.open(root, create=True)
    steps: dict[str, float] = {}
    named: dict[str, str] = {}

    def snap(trigger: str, label: str | None = None, command: str | None = None, ago: float = 0) -> None:
        s = store.snap(trigger, label=label, command=command)
        if s is not None:
            steps[s.id] = ago

    # 1 baseline ------------------------------------------------------------------------------
    w(root, "README.md", "# pawtrack\n\nA tiny pet-tracking API.\n")
    w(root, "requirements.txt", "flask==3.0.3\nflask-sqlalchemy==3.1.1\npytest==8.2.0\n")
    w(root, "config.py", 'DEBUG = False\nAPI_TOKEN = "change-me"\nSQLALCHEMY_DATABASE_URI = "sqlite:///pawtrack.db"\n')
    w(root, "pawtrack/__init__.py", 'from flask import Flask\n\nfrom .models import db\nfrom .routes import bp\n\n\ndef create_app():\n    app = Flask(__name__)\n    app.config.from_object("config")\n    db.init_app(app)\n    app.register_blueprint(bp)\n    return app\n')
    w(root, "pawtrack/models.py", MODELS_V1)
    w(root, "pawtrack/auth.py", AUTH_V1)
    w(root, "pawtrack/routes.py", ROUTES_V1)
    w(root, "pawtrack/legacy/importer.py", "def import_csv(path):\n    raise NotImplementedError\n")
    w(root, "templates/index.html", "<!doctype html>\n<title>pawtrack</title>\n<h1>pawtrack</h1>\n")
    w(root, "static/logo.png", bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13]) + bytes(range(256)) * 6)
    w(root, "tests/test_routes.py", TEST_ROUTES.split("def test_create_pet_requires_login")[0])
    snap("manual", "baseline", ago=118)

    named["baseline"] = next(iter(steps))
    # 2 you tweak config by hand
    w(root, "config.py", 'DEBUG = True\nAPI_TOKEN = "dev-token"\nSQLALCHEMY_DATABASE_URI = "sqlite:///pawtrack.db"\n')
    snap("watch", ago=96)
    w(root, "templates/index.html", "<!doctype html>\n<title>pawtrack</title>\n<h1>pawtrack</h1>\n<p>Track every good boy.</p>\n")
    snap("watch", ago=90)

    # 3 an agent session begins
    w(root, "pawtrack/cache.py", CACHE_V1)
    snap("hook", "claude: Write", ago=71)
    w(root, "pawtrack/routes.py", ROUTES_V2)
    snap("hook", "claude: Edit", ago=70)
    w(root, "pawtrack/models.py", MODELS_V2)
    w(root, "tests/test_routes.py", TEST_ROUTES)
    snap("hook", "claude: MultiEdit", ago=64)
    w(root, "README.md", "# pawtrack\n\nA tiny pet-tracking API.\n\n## Run\n\n    flask --app pawtrack run\n")
    snap("watch", ago=52)

    # you experiment, then roll one file back (this creates a restore-before snapshot)
    w(root, "config.py", 'DEBUG = True\nAPI_TOKEN = "oops"\nSQLALCHEMY_DATABASE_URI = "sqlite:///other.db"\n')
    res = store.restore(named["baseline"], paths=["config.py"])
    for extra in store.log(limit=2):  # the restore-before snapshot and the one holding the restored state
        steps.setdefault(extra.id, 47)
    snap("watch", ago=46)

    # a command run
    w(root, "pawtrack/__init__.py", 'from flask import Flask, jsonify\n\nfrom .models import db\nfrom .routes import bp\n\n\ndef create_app():\n    app = Flask(__name__)\n    app.config.from_object("config")\n    db.init_app(app)\n    app.register_blueprint(bp)\n\n    @app.errorhandler(404)\n    def not_found(_):\n        return jsonify(error="not found"), 404\n\n    return app\n')
    snap("run-before", None, "claude -p 'add species filter'", ago=41)
    rm(root, "pawtrack/legacy")
    w(root, "requirements.txt", "flask==3.0.3\nflask-sqlalchemy==3.1.1\npytest==8.2.0\ngunicorn==22.0.0\n")
    w(root, "Dockerfile", 'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\nCMD ["gunicorn", "pawtrack:create_app()"]\n')
    snap("hook", "claude: Bash", "rm -rf pawtrack/legacy", ago=33)
    named["bash"] = list(steps)[-1]
    snap("run-after", None, "claude -p 'add species filter'", ago=33)

    # 4 the bad edit: validation and the auth check quietly disappear
    w(root, "pawtrack/routes.py", ROUTES_V3)
    rm(root, "pawtrack/auth.py")
    snap("hook", "claude: Edit", ago=9)
    named["bad"] = list(steps)[-1]
    w(root, "static/logo.png", bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13]) + bytes(range(256)) * 14)
    snap("hook", "claude: Write", ago=3.5)
    w(root, "config.py", 'DEBUG = True\nAPI_TOKEN = "dev-token"\nCACHE_TTL = 300\nSQLALCHEMY_DATABASE_URI = "sqlite:///pawtrack.db"\n')
    snap("hook", "claude: Edit", ago=0.2)
    steps["__named__"] = named  # type: ignore[assignment]
    return store, steps


class DemoStore:
    """Wraps a real Store and rewrites snapshot times so the screenshot has a believable timeline."""

    def __init__(self, store: Store, ages: dict[str, float]):
        self._s = store
        self._ages = ages
        self.display_root = "~/code/pawtrack"  # shown in the TUI header instead of the temp dir
        self._now = datetime.now().astimezone()

    def __getattr__(self, name):
        return getattr(self._s, name)

    def _fix(self, snap):
        age = self._ages.get(snap.id)
        if age is None:
            return snap
        return dataclasses.replace(snap, time=self._now - timedelta(minutes=age))

    def log(self, *a, **k):
        return [self._fix(s) for s in self._s.log(*a, **k)]

    def get(self, ref):
        return self._fix(self._s.get(ref))


async def drive(app: CtrlzApp, pilot, target_id: str, out_name: str, focus: str, pick_file: str | None = None) -> None:
    async def settle(cond, timeout=8.0):
        t = 0.0
        while t < timeout:
            await pilot.pause(0.05)
            t += 0.05
            if cond():
                await pilot.pause(0.15)
                return
        raise TimeoutError(out_name)

    await settle(lambda: app._cur is not None and (app._doc is not None or app._doc_note is not None))
    idx = [s.id for s in app._view].index(target_id)
    for _ in range(idx):
        await pilot.press("j")
        await pilot.pause(0.03)
    await settle(lambda: app._sel_id == target_id and app._changes_for == target_id and app._cur is not None and app._doc is not None)
    if pick_file:
        await pilot.press("tab")
        for _ in range(len(app._changes)):
            if app._cur.path == pick_file:
                break
            await pilot.press("j")
            await pilot.pause(0.1)
        await settle(lambda: app._cur.path == pick_file and (app._doc is not None or app._doc_note is not None))
    elif focus == "changes":
        await pilot.press("tab")
    await pilot.pause(0.4)
    app.save_screenshot(f"{out_name}.svg", path=str(OUT))
    print("wrote", OUT / f"{out_name}.svg")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ctrlz-demo-")) / "pawtrack"
    tmp.mkdir()
    try:
        real, ages = build_demo(tmp)
        named = ages.pop("__named__")
        store = DemoStore(real, ages)
        for name, idx, focus, f, theme in (
            ("tui-timeline", named["bash"], "timeline", None, "tokyo-night"),
            ("tui-diff", named["bad"], "changes", "pawtrack/routes.py", "tokyo-night"),
            ("tui-light", named["bad"], "changes", "pawtrack/routes.py", "textual-light"),
        ):
            app = CtrlzApp(store, poll_interval=3600, theme=theme)
            async with app.run_test(size=SIZE) as pilot:
                await drive(app, pilot, idx, name, focus, f)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    try:
        import cairosvg  # type: ignore
    except Exception:
        print("cairosvg not available: skipping PNG export")
        return
    for svg in OUT.glob("tui-*.svg"):
        # the SVG asks for Fira Code (web font); use a locally installed monospace for the PNG
        data = svg.read_text().replace("Fira Code", "DejaVu Sans Mono")
        cairosvg.svg2png(bytestring=data.encode(), write_to=str(svg.with_suffix(".png")), scale=1.2)
        print("wrote", svg.with_suffix(".png"))


if __name__ == "__main__":
    asyncio.run(main())
