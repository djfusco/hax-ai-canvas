"""A Canvas-importable Common Cartridge (.imscc) of approved revised documents.

Uses the Canvas flavour of Common Cartridge (canvas_export.txt, wiki_content/,
per-assignment assignment_settings.xml, course_settings/rubrics.xml and
module_meta.xml), so Canvas keeps points, due dates and rubrics.

Nothing in the course is overwritten on import: every item is new, marked
"(revised)" and unpublished, inside one unpublished module. The revised
syllabus is a page; the instructor copies it into the Syllabus when ready.
No course settings are included, so importing changes no course settings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import hashlib
import io
import re
import zipfile

CANVAS_NS = "http://canvas.instructure.com/xsd/cccv1p0"
CANVAS_SCHEMA = f'xmlns="{CANVAS_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ' \
                f'xsi:schemaLocation="{CANVAS_NS} https://canvas.instructure.com/xsd/cccv1p0.xsd"'
MODULE_TITLE = "Course Consultant revisions"
SUFFIX = " (revised)"


class PackageError(ValueError):
    pass


def _id(*parts):
    return "g" + hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:31]


def _x(value):
    return escape("" if value is None else str(value), quote=False)


def _slug(title, used):
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60] or "page"
    base, n = slug, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def _number(value):
    return f"{float(value):.1f}" if isinstance(value, (int, float)) and value == value else ""


def _latest(documents):
    """The latest approved document for each target."""
    chosen = {}
    for document in documents:
        if document.get("status") != "approved":
            continue
        key = (document["kind"], document.get("item_id"))
        stamp = document.get("updated_at") or document.get("created_at") or ""
        if key not in chosen or stamp >= (chosen[key].get("updated_at") or chosen[key].get("created_at") or ""):
            chosen[key] = document
    return list(chosen.values())


def _title(document, model):
    items = {i["id"]: i for i in model["items"]}
    if document["kind"] == "syllabus":
        return "Syllabus" + SUFFIX
    if document["kind"] == "tutor":
        return "Course AI tutor kit (instructor only)"
    original = items.get(document.get("item_id") or "")
    return ((original or {}).get("title") or document.get("title") or "Untitled")[:230] + SUFFIX


def _page_html(identifier, title, body):
    return (f'<html>\n<head>\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
            f'<title>{_x(title)}</title>\n<meta name="identifier" content="{identifier}"/>\n'
            f'<meta name="editing_roles" content="teachers"/>\n<meta name="workflow_state" content="unpublished"/>\n'
            f'</head>\n<body>\n{body}\n</body>\n</html>\n')


def _assignment_html(title, body):
    return (f'<html>\n<head>\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
            f'<title>Assignment: {_x(title)}</title>\n</head>\n<body>\n{body}\n</body>\n</html>\n')


def _rubric_from_document(document):
    criteria = []
    for c in document.get("rubric") or []:
        ratings = [{"description": r["label"], "long_description": r["description"], "points": r["points"]}
                   for r in c["ratings"]]
        criteria.append({"description": c["criterion"], "long_description": c.get("description", ""),
                         "points": max((r["points"] for r in ratings), default=0), "ratings": ratings})
    return {"title": document["title"] + SUFFIX, "criteria": criteria,
            "points": sum(c["points"] for c in criteria)}


def _rubric_xml(identifier, rubric):
    criteria = []
    for n, c in enumerate(rubric.get("criteria") or [], 1):
        cid = f"_{n}"
        ratings = "".join(
            f"<rating><description>{_x(r.get('description'))}</description>"
            f"<long_description>{_x(r.get('long_description'))}</long_description>"
            f"<points>{_number(r.get('points')) or '0.0'}</points><criterion_id>{cid}</criterion_id>"
            f"<id>{cid}_{m}</id></rating>"
            for m, r in enumerate(c.get("ratings") or [], 1))
        criteria.append(
            f"<criterion><criterion_id>{cid}</criterion_id><points>{_number(c.get('points')) or '0.0'}</points>"
            f"<description>{_x(c.get('description'))}</description>"
            f"<long_description>{_x(c.get('long_description'))}</long_description>"
            f"<ratings>{ratings}</ratings></criterion>")
    points = rubric.get("points")
    if not isinstance(points, (int, float)):
        points = sum(c.get("points") or 0 for c in rubric.get("criteria") or [])
    return (f'<rubric identifier="{identifier}"><read_only>false</read_only>'
            f"<title>{_x(rubric.get('title') or 'Rubric')}</title><reusable>false</reusable><public>false</public>"
            f"<points_possible>{_number(points) or '0.0'}</points_possible><hide_score_total>false</hide_score_total>"
            f"<free_form_criterion_comments>false</free_form_criterion_comments>"
            f"<criteria>{''.join(criteria)}</criteria></rubric>")


def _assignment_settings(identifier, title, details, rubric_ref):
    submission = ",".join(details.get("submission_types") or []) or "online_upload"
    rows = [f"<title>{_x(title)}</title>"]
    if details.get("due_at"):
        rows.append(f"<due_at>{_x(details['due_at'])}</due_at>")
    if rubric_ref:
        rows += [f"<rubric_identifierref>{rubric_ref}</rubric_identifierref>",
                 "<rubric_use_for_grading>false</rubric_use_for_grading>",
                 "<rubric_hide_score_total>false</rubric_hide_score_total>"]
    if _number(details.get("points")):
        rows.append(f"<points_possible>{_number(details['points'])}</points_possible>")
    rows += [f"<grading_type>{_x(details.get('grading_type') or 'points')}</grading_type>",
             f"<submission_types>{_x(submission)}</submission_types>",
             f"<peer_reviews>{'true' if details.get('peer_reviews') else 'false'}</peer_reviews>",
             "<workflow_state>unpublished</workflow_state>"]
    if details.get("allowed_attempts"):
        rows.append(f"<allowed_attempts>{int(details['allowed_attempts'])}</allowed_attempts>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<assignment identifier="{identifier}" {CANVAS_SCHEMA}>\n  '
            + "\n  ".join(rows) + "\n</assignment>\n")


def build(course_title, model, documents):
    """Bytes of the .imscc package and a summary of what it holds."""
    chosen = _latest(documents)
    if not chosen:
        raise PackageError("Approve at least one document first.")
    items = {i["id"]: i for i in model["items"]}
    files, resources, module_items, rubrics = {}, [], [], []
    used = set()
    rubric_for_item = {}
    for document in chosen:
        if document["kind"] == "rubric" and document.get("rubric"):
            ref = _id("rubric", document["id"])
            rubrics.append((ref, _rubric_from_document(document)))
            rubric_for_item[document.get("item_id")] = ref
    summary = {"pages": 0, "assignments": 0, "rubrics": len(rubrics)}
    order = {"syllabus": 0, "tutor": 9}
    for document in sorted(chosen, key=lambda d: (order.get(d["kind"], 1), d.get("title") or "")):
        kind = document["kind"]
        if kind == "rubric":
            continue
        title = _title(document, model)
        identifier = _id(kind, document["id"])
        original = items.get(document.get("item_id") or "")
        is_assignment = kind == "assignment" and original is not None and original["id"] in model["assignments"]
        slug = _slug(title, used)
        if is_assignment:
            details = model["assignments"][original["id"]]
            rubric_ref = rubric_for_item.get(original["id"])
            if rubric_ref is None and details.get("rubric_id") in model["rubrics"]:
                rubric_ref = _id("rubric", details["rubric_id"])
                rubric = dict(model["rubrics"][details["rubric_id"]])
                rubric["title"] = (rubric.get("title") or "Rubric") + SUFFIX
                rubrics.append((rubric_ref, rubric))
                rubric_for_item[original["id"]] = rubric_ref
                summary["rubrics"] += 1
            html_path = f"{identifier}/{slug}.html"
            files[html_path] = _assignment_html(title, document["html"])
            files[f"{identifier}/assignment_settings.xml"] = _assignment_settings(identifier, title, details, rubric_ref)
            resources.append(
                f'<resource identifier="{identifier}" type="associatedcontent/imscc_xmlv1p1/learning-application-resource" '
                f'href="{html_path}"><file href="{html_path}"/><file href="{identifier}/assignment_settings.xml"/></resource>')
            module_items.append((identifier, title, "Assignment"))
            summary["assignments"] += 1
        else:
            path = f"wiki_content/{slug}.html"
            files[path] = _page_html(identifier, title, document["html"])
            resources.append(f'<resource identifier="{identifier}" type="webcontent" href="{path}">'
                             f'<file href="{path}"/></resource>')
            module_items.append((identifier, title, "WikiPage"))
            summary["pages"] += 1

    module_id = _id("module", course_title)
    return _package(course_title + SUFFIX, [(module_id, MODULE_TITLE, module_items)], resources, files, rubrics,
                    "Revised course items from Course Consultant."), summary


def _package(manifest_title, modules, resources, files, rubrics, note):
    """Write module_meta.xml, rubrics.xml, the manifest and the ZIP. ``modules`` is
    [(identifier, title, [(resource ref, title, Canvas content type), ...]), ...]."""
    module_xml = []
    for position, (module_id, module_title, entries) in enumerate(modules, 1):
        rows = "".join(
            f"<item identifier=\"{_id('module-item', module_id, ref)}\"><content_type>{content}</content_type>"
            f"<workflow_state>unpublished</workflow_state><title>{_x(title)}</title>"
            f"<identifierref>{ref}</identifierref><position>{n}</position><new_tab/><indent>0</indent></item>"
            for n, (ref, title, content) in enumerate(entries, 1))
        module_xml.append(
            f'<module identifier="{module_id}"><title>{_x(module_title)}</title>'
            f"<workflow_state>unpublished</workflow_state><position>{position}</position>"
            f"<require_sequential_progress>false</require_sequential_progress><locked>false</locked>"
            f"<items>{rows}</items></module>")
    files["course_settings/module_meta.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<modules {CANVAS_SCHEMA}>\n' + "\n".join(module_xml) + "\n</modules>\n")
    files["course_settings/rubrics.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<rubrics {CANVAS_SCHEMA}>\n'
        + "\n".join(_rubric_xml(ref, rubric) for ref, rubric in rubrics) + "\n</rubrics>\n")
    files["course_settings/canvas_export.txt"] = (
        note + " Import with Course Settings > Import Course Content > Common Cartridge 1.x Package.\n")
    settings_files = "".join(f'<file href="course_settings/{n}"/>' for n in
                             ("canvas_export.txt", "module_meta.xml", "rubrics.xml"))
    resources = [f'<resource identifier="{_id("settings", manifest_title)}" '
                 f'type="associatedcontent/imscc_xmlv1p1/learning-application-resource" '
                 f'href="course_settings/canvas_export.txt">{settings_files}</resource>'] + resources
    organization = "".join(
        f'<item identifier="{module_id}"><title>{_x(module_title)}</title>'
        + "".join(f'<item identifier="{_id("org-item", module_id, ref)}" identifierref="{ref}"><title>{_x(title)}</title></item>'
                  for ref, title, _ in entries) + "</item>"
        for module_id, module_title, entries in modules)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files["imsmanifest.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<manifest identifier="{_id("manifest", manifest_title, stamp)}" '
        'xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" '
        'xmlns:lom="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource" '
        'xmlns:lomimscc="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1 '
        'http://www.imsglobal.org/profile/cc/ccv1p1/ccv1p1_imscp_v1p2_v1p0.xsd '
        'http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource http://www.imsglobal.org/profile/cc/ccv1p1/LOM/ccv1p1_lomresource_v1p0.xsd '
        'http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest http://www.imsglobal.org/profile/cc/ccv1p1/LOM/ccv1p1_lommanifest_v1p0.xsd">\n'
        "<metadata><schema>IMS Common Cartridge</schema><schemaversion>1.1.0</schemaversion>"
        f"<lomimscc:lom><lomimscc:general><lomimscc:title><lomimscc:string>{_x(manifest_title)}"
        "</lomimscc:string></lomimscc:title></lomimscc:general>"
        f"<lomimscc:lifeCycle><lomimscc:contribute><lomimscc:date><lomimscc:dateTime>{stamp}</lomimscc:dateTime>"
        "</lomimscc:date></lomimscc:contribute></lomimscc:lifeCycle></lomimscc:lom></metadata>\n"
        f'<organizations><organization identifier="org_1" structure="rooted-hierarchy">'
        f'<item identifier="LearningModules">{organization}</item></organization></organizations>\n'
        f"<resources>{''.join(resources)}</resources>\n</manifest>\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("imsmanifest.xml", files.pop("imsmanifest.xml"))
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def build_course(course_title, model, documents, items):
    """The whole built course as a Canvas package: one module per section (Start here, each week,
    resources, instructor), assignments with their settings and rubrics, and pages linked to each
    other. Meant for a new, empty Canvas course; everything arrives unpublished."""
    from imscc_content import rewrite_import_item_links
    if not items:
        raise PackageError("Build the course first.")
    files, resources, rubrics, modules = {}, [], [], []
    used, refs, links = set(), {}, {}
    rubric_docs = {d.get("item_id"): d for d in _latest(documents) if d["kind"] == "rubric" and d.get("rubric")}
    # Plan every resource first so pages can link to each other.
    for item in items:
        identifier = _id("built", item["id"])
        is_assignment = item["id"] in model["assignments"] and item.get("type") == "assignments"
        slug = _slug(item["title"], used)
        refs[item["id"]] = (identifier, slug, is_assignment)
        links[item["id"]] = (f"$CANVAS_OBJECT_REFERENCE$/assignments/{identifier}" if is_assignment
                             else f"$WIKI_REFERENCE$/pages/{slug}")
    summary = {"pages": 0, "assignments": 0, "rubrics": 0, "modules": 0}
    current = None
    for item in items:
        if item["id"].endswith("--rubric"):
            continue  # rubrics are attached to their assignment
        identifier, slug, is_assignment = refs[item["id"]]
        html = rewrite_import_item_links(item["html"], links)
        if current is None or item["id"].startswith("module--") or item["id"] == "home":
            if item["id"] == "home":
                title = "Start here"
            else:
                title = item.get("module") or item["title"]
            if current is None or current[1] != title:
                current = (_id("built-module", title), title, [])
                modules.append(current)
        if is_assignment:
            details = model["assignments"][item["id"]]
            rubric_ref = None
            if item["id"] in rubric_docs:
                rubric_ref = _id("rubric", rubric_docs[item["id"]]["id"])
                rubrics.append((rubric_ref, _rubric_from_document(rubric_docs[item["id"]])))
            elif details.get("rubric_id") in model["rubrics"]:
                rubric_ref = _id("rubric", details["rubric_id"])
                rubrics.append((rubric_ref, model["rubrics"][details["rubric_id"]]))
            html_path = f"{identifier}/{slug}.html"
            files[html_path] = _assignment_html(item["title"], html)
            files[f"{identifier}/assignment_settings.xml"] = _assignment_settings(identifier, item["title"],
                                                                                  details, rubric_ref)
            resources.append(
                f'<resource identifier="{identifier}" type="associatedcontent/imscc_xmlv1p1/learning-application-resource" '
                f'href="{html_path}"><file href="{html_path}"/><file href="{identifier}/assignment_settings.xml"/></resource>')
            current[2].append((identifier, item["title"], "Assignment"))
            summary["assignments"] += 1
        else:
            path = f"wiki_content/{slug}.html"
            files[path] = _page_html(identifier, item["title"], html)
            resources.append(f'<resource identifier="{identifier}" type="webcontent" href="{path}">'
                             f'<file href="{path}"/></resource>')
            current[2].append((identifier, item["title"], "WikiPage"))
            summary["pages"] += 1
    unique = {}
    for ref, rubric in rubrics:
        unique.setdefault(ref, rubric)
    rubrics = list(unique.items())
    summary["rubrics"], summary["modules"] = len(rubrics), len(modules)
    note = (f"{course_title}: the course built with Course Consultant. Import into a new, empty Canvas course; "
            "everything arrives unpublished, and the instructor section should stay unpublished.")
    return _package(course_title, modules, resources, files, rubrics, note), summary
