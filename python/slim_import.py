"""Import a slim Canvas course package into the normalized course model.

A slim package is a Canvas ``.imscc`` export reduced in the browser to its
text entries (manifest, HTML, XML, QTI and ``course_settings``), plus:

* ``media_inventory.json``: ``[{"path", "bytes", "mime"}]`` for every entry
  that was left out (videos, images, documents);
* ``materials/<name>``: documents the instructor chose to include as text.

A full (unslimmed) export is also accepted: media entries are inventoried
from the archive itself and never copied.

Parsing and archive-safety checks reuse ``imscc_import._Cartridge``. This
module adds the Canvas metadata the consultant needs: rubrics, assignment
group weights, learning outcomes, module requirements, assignment and quiz
settings, grading scheme and late policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import mimetypes
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tempfile
from urllib.parse import unquote
import uuid
import zipfile
import zlib

from imscc_import import IMSCCImportError, _Cartridge, _local, _nodes, _text
import course_model as cm

MAX_SLIM_BYTES = 48 * 1024 * 1024
TEXT_SUFFIXES = (".xml", ".html", ".htm", ".qti", ".txt")
MATERIAL_PREFIX = "materials/"
MATERIAL_SUFFIXES = (".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".csv")
FILEBASE = re.compile(r"(?:\$|%24)IMS(?:-|_)CC(?:-|_)FILEBASE(?:\$|%24)/([^\"'?#\s>]+)", re.IGNORECASE)


class SlimImportError(ValueError):
    """The slim package cannot be imported; the message is safe to show."""


# ── XML helpers ──────────────────────────────────────────────────────────────

def _child(node, name):
    """Direct child by local name (``_text`` searches all descendants)."""
    if node is None:
        return None
    return next((c for c in node if _local(c.tag) == name), None)


def _ctext(node, name, default=""):
    child = _child(node, name)
    return "".join(child.itertext()).strip() if child is not None else default


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _int(value):
    number = _float(value)
    return int(number) if number is not None else None


def _bool(value):
    return str(value).strip().lower() in ("true", "1", "yes")


def _clip(value, maximum):
    return value if len(value) <= maximum else value[:maximum]


# ── importer ─────────────────────────────────────────────────────────────────

class _SlimImporter:
    def __init__(self, archive: zipfile.ZipFile, workdir: Path):
        self.archive = archive
        self.workdir = workdir
        self.names = [i.filename for i in archive.infolist() if not i.is_dir()]
        self.inventory = self._inventory()
        self.cartridge = _Cartridge(archive, workdir, "c" + uuid.uuid4().hex[:12],
                                    slim=True, inventory={m["path"] for m in self.inventory})
        self.model = None
        self.resource_to_item = {}

    # Inventory of omitted media: from the package, or from the archive itself.
    def _inventory(self):
        if "media_inventory.json" in self.names:
            info = self.archive.getinfo("media_inventory.json")
            if info.file_size > 4 * 1024 * 1024:
                raise SlimImportError("The media inventory is too large.")
            try:
                rows = json.loads(self.archive.read(info).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise SlimImportError("The media inventory is not valid JSON.") from None
            if not isinstance(rows, list):
                raise SlimImportError("The media inventory must be a list.")
            result = []
            for row in rows[:cm.MAX_MEDIA]:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise SlimImportError("A media inventory entry is malformed.")
                size = row.get("bytes")
                result.append({
                    "path": _clip(row["path"], 1000),
                    "mime": _clip(str(row.get("mime") or mimetypes.guess_type(row["path"])[0] or ""), 200),
                    "bytes": size if type(size) in (int, float) and size >= 0 else 0,
                    "referenced_by": [],
                })
            return result
        result = []
        for info in self.archive.infolist():
            name = info.filename
            if info.is_dir() or name == "imsmanifest.xml" or name.startswith(MATERIAL_PREFIX):
                continue
            if name.lower().endswith(TEXT_SUFFIXES) or name.startswith("course_settings/"):
                continue
            result.append({"path": name, "mime": mimetypes.guess_type(name)[0] or "",
                           "bytes": info.file_size, "referenced_by": []})
        return result[:cm.MAX_MEDIA]

    def _xml(self, path):
        return self.cartridge.xml(path) if path in self.cartridge.entries else None

    def _load(self, folder):
        directory = self.workdir / folder
        if not directory.is_dir():
            return []
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
        return rows

    def run(self, title_hint=None):
        title, _, _ = self.cartridge.parse()
        model = cm.empty_course("export", _clip(title_hint or title, cm.MAX_TITLE))
        self.model = model
        model["media_inventory"] = self.inventory
        self._items()
        self._course_settings()
        self._groups()
        self._outcomes()
        self._rubrics()
        self._assignment_settings()
        self._quiz_settings()
        self._modules()
        self._outcome_links()
        _link_media(model)
        self._materials()
        counts = {
            "pages": sum(1 for i in model["items"] if i["kind"] == "page"),
            "assignments": sum(1 for i in model["items"] if i["kind"] == "assignment"),
            "quizzes": sum(1 for i in model["items"] if i["kind"] == "quiz"),
            "discussions": sum(1 for i in model["items"] if i["kind"] == "discussion"),
            "modules": len(model["modules"]),
            "rubrics": len(model["rubrics"]),
            "outcomes": len(model["outcomes"]),
            "media": len(model["media_inventory"]),
            "materials": len(model["materials"]),
        }
        warnings = [_clip(w, 1000) for w in self.cartridge.warnings]
        model["provenance"].update(
            imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            counts=counts, warnings=warnings[:cm.MAX_WARNINGS])
        return model

    # ── content items ────────────────────────────────────────────────────────
    def _add_item(self, kind, row, title, html):
        item = {"id": row["id"], "kind": kind, "title": _clip((title or "Untitled").strip() or "Untitled", cm.MAX_TITLE),
                "html": html or "", "module_id": None, "published": bool(row.get("published", True)),
                "source_path": row.get("source_path", "")}
        self.model["items"].append(item)
        identifier = row.get("source_identifier")
        if identifier:
            self.resource_to_item[identifier] = item["id"]
        return item

    def _items(self):
        model = self.model
        for row in self._load("pages"):
            self._add_item("page", row, row.get("title"), row.get("body", ""))
        for row in self._load("assignments"):
            self._add_item("assignment", row, row.get("name"), row.get("description", ""))
            model["assignments"][row["id"]] = {
                "points": row.get("points_possible"), "due_at": row.get("due_at"),
                "submission_types": [s[:200] for s in row.get("submission_types", [])][:20],
                "grading_type": None, "group_id": None, "rubric_id": None,
                "peer_reviews": False, "group_work": False, "allowed_attempts": None,
            }
        for row in self._load("discussions"):
            self._add_item("discussion", row, row.get("title"), row.get("message", ""))
        for row in self._load("quizzes"):
            self._add_item("quiz", row, row.get("title"), row.get("description", ""))
            model["quizzes"][row["id"]] = {
                "time_limit": None, "allowed_attempts": None, "points": None, "quiz_type": None,
                "questions": [{"text": _clip(q.get("question_text", ""), cm.MAX_TEXT),
                               "type": _clip(q.get("question_type") or "", 100),
                               "points": _float(q.get("points_possible"))}
                              for q in row.get("questions", [])],
            }
        syllabus = self._load("syllabus")
        if syllabus:
            model["course"]["syllabus_html"] = syllabus[0].get("body", "")
        if len(model["items"]) > cm.MAX_ITEMS:
            raise SlimImportError(
                f"This course has {len(model['items'])} items; the limit is {cm.MAX_ITEMS}.")

    # ── course_settings ──────────────────────────────────────────────────────
    def _course_settings(self):
        course = self.model["course"]
        root = self._xml("course_settings/course_settings.xml")
        if root is not None:
            course["code"] = _clip(_ctext(root, "course_code"), cm.MAX_TITLE)
            course["weighted_groups"] = _ctext(root, "group_weighting_scheme").lower() == "percent"
            start = _ctext(root, "start_at")
            course["term"] = _clip(start[:10], cm.MAX_TITLE) if start else ""
        root = self._xml("course_settings/grading_standards.xml")
        if root is not None:
            standard = next(iter(_nodes(root, "gradingStandard")), None)
            data = _ctext(standard, "data") if standard is not None else ""
            try:
                rows = json.loads(data) if data else []
                levels = [{"name": _clip(str(name), 50), "min": float(value)}
                          for name, value in rows[:50] if str(name).strip()]
            except (ValueError, TypeError):
                levels = []
                self.cartridge.warn("Grading scheme could not be read.")
            if levels:
                course["grading_scheme"] = {"title": _clip(_ctext(standard, "title"), cm.MAX_TITLE), "levels": levels}
        root = self._xml("course_settings/late_policy.xml")
        if root is not None:
            policy = {}
            for child in list(root)[:30]:
                text = "".join(child.itertext()).strip()
                key = _local(child.tag)[:100]
                number = _float(text)
                policy[key] = (_bool(text) if text.lower() in ("true", "false")
                               else number if number is not None else _clip(text, 200))
            course["late_policy"] = policy or None

    def _groups(self):
        root = self._xml("course_settings/assignment_groups.xml")
        if root is None:
            return
        self.groups_by_ref = {}
        for position, node in enumerate(_nodes(root, "assignmentGroup")[:cm.MAX_GROUPS], 1):
            ref = node.get("identifier") or f"group-{position}"
            group = {"id": "group-" + ref[:150], "name": _clip(_ctext(node, "title"), cm.MAX_TITLE),
                     "weight": _float(_ctext(node, "group_weight")),
                     "position": _float(_ctext(node, "position")) or position}
            if group["weight"] is not None and group["weight"] < 0:
                group["weight"] = None
            self.groups_by_ref[ref] = group["id"]
            self.model["assignment_groups"].append(group)

    def _outcomes(self):
        self.outcomes_by_ref = {}
        root = self._xml("course_settings/learning_outcomes.xml")
        if root is None:
            return
        for node in _nodes(root, "learningOutcome")[:cm.MAX_OUTCOMES]:
            ref = node.get("identifier")
            title = _ctext(node, "title")
            if not ref or not title:
                continue
            outcome = {"id": "outcome-" + ref[:150], "title": _clip(title, cm.MAX_TITLE),
                       "description": _clip(_ctext(node, "description"), cm.MAX_TEXT),
                       "mastery_points": _float(_ctext(node, "mastery_points")), "item_ids": []}
            self.outcomes_by_ref[ref] = outcome["id"]
            self.model["outcomes"].append(outcome)

    def _rubrics(self):
        self.rubrics_by_ref = {}
        root = self._xml("course_settings/rubrics.xml")
        if root is None:
            return
        for node in _nodes(root, "rubric")[:cm.MAX_RUBRICS]:
            ref = node.get("identifier")
            if not ref:
                continue
            criteria = []
            container = _child(node, "criteria")
            for criterion in (list(container) if container is not None else [])[:cm.MAX_CRITERIA]:
                ratings_node = _child(criterion, "ratings")
                ratings = [{"description": _clip(_ctext(r, "description"), cm.MAX_TEXT),
                            "long_description": _clip(_ctext(r, "long_description"), cm.MAX_TEXT),
                            "points": _float(_ctext(r, "points"))}
                           for r in (list(ratings_node) if ratings_node is not None else [])[:cm.MAX_RATINGS]]
                outcome_ref = _ctext(criterion, "learning_outcome_identifierref")
                criteria.append({
                    "description": _clip(_ctext(criterion, "description"), cm.MAX_TEXT),
                    "long_description": _clip(_ctext(criterion, "long_description"), cm.MAX_TEXT),
                    "points": _float(_ctext(criterion, "points")),
                    "outcome_id": self.outcomes_by_ref.get(outcome_ref) if outcome_ref else None,
                    "ratings": ratings,
                })
            rubric_id = "rubric-" + ref[:150]
            self.rubrics_by_ref[ref] = rubric_id
            self.model["rubrics"][rubric_id] = {
                "title": _clip(_ctext(node, "title"), cm.MAX_TITLE),
                "points": _float(_ctext(node, "points_possible")), "criteria": criteria,
            }

    # ── per-item settings ────────────────────────────────────────────────────
    def _settings_for(self, item, filename):
        directory = posixpath.dirname(item.get("source_path", ""))
        return self._xml(posixpath.join(directory, filename) if directory else filename)

    def _assignment_settings(self):
        groups = getattr(self, "groups_by_ref", {})
        for item in self.model["items"]:
            if item["kind"] != "assignment":
                continue
            root = self._settings_for(item, "assignment_settings.xml")
            if root is None:
                continue
            details = self.model["assignments"][item["id"]]
            grading = _ctext(root, "grading_type")
            attempts = _int(_ctext(root, "allowed_attempts"))
            details.update(
                grading_type=grading[:50] or None,
                group_id=groups.get(_ctext(root, "assignment_group_identifierref")),
                rubric_id=self.rubrics_by_ref.get(_ctext(root, "rubric_identifierref")),
                peer_reviews=_bool(_ctext(root, "peer_reviews")),
                group_work=bool(_ctext(root, "group_category")),
                allowed_attempts=attempts if attempts is not None and attempts > 0 else None,
            )

    def _quiz_settings(self):
        groups = getattr(self, "groups_by_ref", {})
        for item in self.model["items"]:
            if item["kind"] != "quiz":
                continue
            root = self._settings_for(item, "assessment_meta.xml")
            if root is None:
                continue
            quiz = self.model["quizzes"][item["id"]]
            attempts = _int(_ctext(root, "allowed_attempts"))
            quiz.update(time_limit=_float(_ctext(root, "time_limit")),
                        allowed_attempts=attempts if attempts is not None and attempts > 0 else None,
                        points=_float(_ctext(root, "points_possible")),
                        quiz_type=_ctext(root, "quiz_type")[:50] or None)
            assignment = _child(root, "assignment")
            if quiz["quiz_type"] in ("assignment", "graded_survey") or assignment is not None:
                self.model["assignments"][item["id"]] = {
                    "points": quiz["points"] if quiz["points"] is not None
                    else _float(_ctext(assignment, "points_possible")),
                    "due_at": (_ctext(root, "due_at") or _ctext(assignment, "due_at")) or None,
                    "submission_types": ["online_quiz"], "grading_type": "points",
                    "group_id": groups.get(_ctext(assignment, "assignment_group_identifierref")
                                           or _ctext(root, "assignment_group_identifierref")),
                    "rubric_id": None, "peer_reviews": False, "group_work": False,
                    "allowed_attempts": quiz["allowed_attempts"],
                }

    # ── modules ──────────────────────────────────────────────────────────────
    def _modules(self):
        model = self.model
        items_by_id = {i["id"]: i for i in model["items"]}
        rows = sorted(self._load("modules"), key=lambda r: _float(r.get("position")) or 0)
        meta = self._xml("course_settings/module_meta.xml")
        meta_by_ref = {n.get("identifier"): n for n in _nodes(meta, "module")} if meta is not None else {}
        module_by_ref = {}
        for row in rows[:cm.MAX_MODULES]:
            module = {"id": row["id"], "title": _clip(str(row.get("name") or ""), cm.MAX_TITLE),
                      "position": _float(row.get("position")), "items": [],
                      "prerequisites": [], "requirements": [], "sequential": False}
            module_by_ref[row.get("source_identifier")] = module
            item_by_ref = {}
            for entry in row.get("items", []):
                content_id = entry.get("content_id")
                if not content_id:
                    url = entry.get("external_url")
                    kind = {"ExternalUrl": "external_link",
                            "ContextExternalTool": "external_tool"}.get(entry.get("type") or "")
                    if kind and len(model["items"]) < cm.MAX_ITEMS:
                        content_id = entry["id"]
                        external = {"id": content_id, "kind": kind,
                                    "title": _clip((entry.get("title") or "External link").strip() or "External link", cm.MAX_TITLE),
                                    "html": "", "module_id": None, "published": True,
                                    "url": _clip(url, 2000) if isinstance(url, str) else None}
                        model["items"].append(external)
                        items_by_id[content_id] = external
                if content_id and content_id in items_by_id and content_id not in module["items"]:
                    module["items"].append(content_id)
                    if items_by_id[content_id]["module_id"] is None:
                        items_by_id[content_id]["module_id"] = module["id"]
                    if entry.get("source_identifier"):
                        item_by_ref[entry["source_identifier"]] = content_id
            node = meta_by_ref.get(row.get("source_identifier"))
            if node is not None:
                module["sequential"] = _bool(_ctext(node, "require_sequential_progress"))
                container = _child(node, "completionRequirements")
                for requirement in (list(container) if container is not None else []):
                    target = item_by_ref.get(_ctext(requirement, "identifierref"))
                    if target:
                        module["requirements"].append({
                            "item_id": target, "type": (requirement.get("type") or "requirement")[:50],
                            "min_score": _float(_ctext(requirement, "min_score")),
                        })
                prerequisites = _child(node, "prerequisites")
                module["_prereq_refs"] = ([_ctext(p, "identifierref") for p in _nodes(prerequisites, "prerequisite")]
                                          if prerequisites is not None else [])
            model["modules"].append(module)
        for module in model["modules"]:
            refs = module.pop("_prereq_refs", [])
            module["prerequisites"] = [module_by_ref[r]["id"] for r in refs
                                       if r in module_by_ref and module_by_ref[r] is not module]

    # ── derived links ────────────────────────────────────────────────────────
    def _outcome_links(self):
        outcomes = {o["id"]: o for o in self.model["outcomes"]}
        for item_id, details in self.model["assignments"].items():
            rubric = self.model["rubrics"].get(details.get("rubric_id"))
            for criterion in (rubric or {}).get("criteria", []):
                outcome = outcomes.get(criterion.get("outcome_id"))
                if outcome is not None and item_id not in outcome["item_ids"]:
                    outcome["item_ids"].append(item_id)

    def _materials(self):
        from mentor_materials import MaterialError, extract_material
        names = [n for n in self.names if n.startswith(MATERIAL_PREFIX) and n.lower().endswith(MATERIAL_SUFFIXES)]
        if len(names) > cm.MAX_MATERIALS:
            raise SlimImportError(f"At most {cm.MAX_MATERIALS} documents can be included.")
        for index, name in enumerate(names, 1):
            filename = PurePosixPath(name).name
            target = self.workdir / f"material-{index}{PurePosixPath(name).suffix.lower()}"
            with self.archive.open(name) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            try:
                material = extract_material(target, original_name=filename, role="reference")
            except MaterialError as exc:
                self.cartridge.warn(f"Document not included ({filename}): {exc}")
                continue
            self.model["materials"].append({"id": f"material-{index}", "name": _clip(material["name"], cm.MAX_TITLE),
                                            "role": "reference", "text": material["text"][:cm.MAX_MATERIAL_CHARS]})


def import_slim(path, title_hint=None) -> dict:
    """Import a slim package (or full export) at ``path``; return a validated model."""
    path = Path(path)
    if path.stat().st_size > MAX_SLIM_BYTES:
        raise SlimImportError("The slim course package exceeds 48 MiB. Re-run slimming in the browser.")
    workdir = Path(tempfile.mkdtemp(prefix="slim-"))
    try:
        with zipfile.ZipFile(path) as archive:
            importer = _SlimImporter(archive, workdir)
            model = importer.run(title_hint)
        for item in model["items"]:
            item.pop("source_path", None)
        return cm.validate_course(model)
    except (SlimImportError, cm.CourseModelError):
        raise
    except IMSCCImportError as exc:
        raise SlimImportError(str(exc)) from None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, zlib.error):
        raise SlimImportError("The course package is not a readable ZIP archive.") from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _link_media(model):
    by_path = {m["path"]: m for m in model["media_inventory"]}
    for item in model["items"]:
        for match in FILEBASE.findall(item["html"]):
            media = by_path.get("web_resources/" + unquote(match))
            if media is not None and item["id"] not in media["referenced_by"]:
                media["referenced_by"].append(item["id"])
