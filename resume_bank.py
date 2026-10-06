#!/usr/bin/env python3
"""
resume_bank - parses the master resume .docx templates into a searchable bullet bank,
and rebuilds a .docx containing only a chosen subset of bullets.

Because selection is done by KEEPING or DROPPING whole paragraphs from the original
file, the output keeps the template's exact fonts, spacing and bullet formatting, and
every line of text is verbatim from a master template. Nothing is rewritten.

  python resume_bank.py --scan            # parse templates, print the bank summary
  python resume_bank.py --dump bank.json  # write the parsed bank to JSON
"""
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from xml.etree import ElementTree as ET

log = logging.getLogger("resume_bank")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ET.register_namespace("w", W[1:-1])

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolved next to this file, so the watcher works from any working directory.
TEMPLATE_DIR = os.path.expanduser(
    os.environ.get("JOBWATCH_TEMPLATES") or os.path.join(HERE, "templates"))


# Only these files count as master templates. Without a filter, every .docx in the
# folder - including resumes this tool generated earlier - lands in the bullet bank.
TEMPLATE_INCLUDE = []      # list of fnmatch patterns, e.g. ["*MASTER*.docx"]
TEMPLATE_EXCLUDE = []


def set_template_dir(path, include=None, exclude=None):
    """Point the bank at a folder anywhere on disk (config: tailor.templates_dir)."""
    global TEMPLATE_DIR, TEMPLATE_INCLUDE, TEMPLATE_EXCLUDE
    if path:
        TEMPLATE_DIR = os.path.abspath(os.path.expanduser(path))
    if include is not None:
        TEMPLATE_INCLUDE = list(include or [])
    if exclude is not None:
        TEMPLATE_EXCLUDE = list(exclude or [])
    return TEMPLATE_DIR

# Section headings as they appear in the templates (upper-case, no bullet).
SECTION_HEADS = ("KEY ACHIEVEMENTS", "PROFESSIONAL EXPERIENCE", "SKILLS", "EDUCATION")

MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"


def job_key(company_line):
    """'AccentureFeb 2021 - Dec 2023' -> 'accenture' so the same employer
    lines up across all four templates."""
    t = re.sub(MONTHS + r"[a-z]*\.?\s*\d{4}.*$", "", company_line, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" ,-\u2013").lower()


def _text(p):
    return "".join(t.text or "" for t in p.iter(W + "t")).strip()


def _is_bullet(p):
    pPr = p.find(W + "pPr")
    if pPr is None:
        return False
    return pPr.find(W + "numPr") is not None


class Template:
    """One master resume: its paragraphs, its bullets, and where they live."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.kind = self._kind()
        with zipfile.ZipFile(path) as z:
            self.doc_xml = z.read("word/document.xml").decode("utf-8")
        self.root = ET.fromstring(self.doc_xml)
        self.body = self.root.find(W + "body")
        self.paras = [p for p in self.body if p.tag == W + "p"]
        self._parse()

    def _kind(self):
        n = self.name.upper()
        for key, label in (("CLOUD", "cloud_devops"), ("DEVOPS", "cloud_devops"),
                           ("BACKEND", "backend"), ("FULLSTACK", "fullstack"),
                           ("FULL_STACK", "fullstack"), ("FRONTEND", "frontend")):
            if key in n:
                return label
        return self.name.lower()

    def _parse(self):
        """Walk the body once, recording section, current job, and every bullet."""
        self.sections = {}          # heading -> index in self.paras
        self.jobs = []              # {title, company, meta, header_idx, bullets:[idx]}
        self.achievements = []      # bullet indices under KEY ACHIEVEMENTS
        self.bullets = {}           # idx -> {text, section, job(key)}
        self.skill_lines = []       # (idx, text) of the SKILLS section paragraphs
        section = None
        job = None
        pending_header = []

        for i, p in enumerate(self.paras):
            txt = _text(p)
            if not txt:
                continue
            if txt.upper() in SECTION_HEADS:
                section = txt.upper()
                self.sections[section] = i
                job = None
                pending_header = []
                continue
            if _is_bullet(p):
                if section == "KEY ACHIEVEMENTS":
                    self.achievements.append(i)
                    self.bullets[i] = {"text": txt, "section": section, "job": None}
                elif section == "PROFESSIONAL EXPERIENCE" and job is not None:
                    job["bullets"].append(i)
                    self.bullets[i] = {"text": txt, "section": section, "job": job["key"]}
                continue
            if section == "PROFESSIONAL EXPERIENCE":
                # Job headers come in pairs: "<Title> <Location>" then "<Company> <Dates>"
                pending_header.append((i, txt))
                if len(pending_header) == 2:
                    (hi, title), (_, company) = pending_header
                    job = {"title": title, "company": company, "key": job_key(company),
                           "header_idx": hi, "bullets": []}
                    self.jobs.append(job)
                    pending_header = []
            elif section == "SKILLS":
                self.skill_lines.append((i, txt))

    # ---------------------------------------------------------------- rebuilding
    def render(self, blocks, out_path, skill_text=None, compact=False, line_spacing=None):
        """Write a .docx whose bullets are exactly `blocks`, in the order given.

        blocks:     {"achievements"|<job key>: [paragraph Element, ...]} - each element is a
                    bullet paragraph copied verbatim from some master template.
        skill_text: {paragraph_idx: new_text} for the SKILLS lines only.
        compact:    tighten margins/line spacing so a one-page selection fits one page.
        line_spacing: multiplier applied to every paragraph, overriding the template's
                    own styles. 1.0 is single, 1.15 is Word's default "1.15". None
                    leaves the template alone.

        Every bullet paragraph originates in a template; nothing is written or reworded.
        """
        root = ET.fromstring(self.doc_xml)
        body = root.find(W + "body")
        para_nodes = [p for p in body if p.tag == W + "p"]

        anchors = {}
        if self.sections.get("KEY ACHIEVEMENTS") is not None:
            anchors["achievements"] = para_nodes[self.sections["KEY ACHIEVEMENTS"]]
        for j in self.jobs:
            # header_idx is the title line; the company/date line follows it
            anchors[j["key"]] = para_nodes[min(j["header_idx"] + 1, len(para_nodes) - 1)]

        for i in self.bullets:                       # clear every original bullet
            body.remove(para_nodes[i])

        for key, nodes in (blocks or {}).items():
            anchor = anchors.get(key)
            if anchor is None:
                continue
            pos = list(body).index(anchor)
            for off, node in enumerate(nodes, start=1):
                body.insert(pos + off, node)

        if skill_text:
            for idx, new_text in skill_text.items():
                runs = [t for t in para_nodes[idx].iter(W + "t")]
                if not runs:
                    continue
                runs[-1].text = new_text
                runs[-1].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

        new_xml = ET.tostring(root, encoding="UTF-8", xml_declaration=True).decode("utf-8")
        parts = {}
        if compact:
            new_xml = _tighten_margins(new_xml)
        if compact or line_spacing:
            with zipfile.ZipFile(self.path) as z:
                parts["word/styles.xml"] = _tighten_spacing(
                    z.read("word/styles.xml").decode("utf-8"),
                    line_spacing=line_spacing, compact=compact)
        parts["word/document.xml"] = new_xml
        shutil.copy(self.path, out_path)
        _replace_in_zip(out_path, parts)
        return out_path

    def bullet_node(self, idx):
        """A deep copy of one bullet paragraph, for splicing into another template."""
        root = ET.fromstring(self.doc_xml)
        body = root.find(W + "body")
        return [p for p in body if p.tag == W + "p"][idx]


def _replace_in_zip(path, parts):
    """parts: {member_name: new_text}"""
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            payload = parts.get(item.filename)
            zout.writestr(item, payload.encode("utf-8") if payload is not None
                          else zin.read(item.filename))
    os.replace(tmp, path)


def _tighten_margins(xml):
    """Narrow the page margins so a one-page selection actually fits on one page.
    Only whitespace changes - no text is touched."""
    return re.sub(r'<w:pgMar[^/]*/>',
                  '<w:pgMar w:top="450" w:right="700" w:bottom="450" w:left="700" '
                  'w:header="360" w:footer="360" w:gutter="0"/>', xml)


def _tighten_spacing(styles_xml, line_spacing=None, compact=True):
    """Rewrite paragraph spacing in styles.xml.

    line_spacing is a multiplier - 1.0 single, 1.15 Word's default "1.15". Word stores
    it in 240ths of a line, so 1.15 becomes 276. None keeps the template's own spacing
    (or forces single when `compact` is set, which is the historical behaviour).
    compact also pulls the gap after each paragraph down to 20 twentieths of a point.
    """
    line = int(round(240 * float(line_spacing))) if line_spacing else (240 if compact else None)
    after = "20" if compact else None

    def _full(m):
        a = after if after is not None else m.group(1)
        l = line if line is not None else m.group(2)
        return f'<w:spacing w:after="{a}" w:line="{l}" w:lineRule="auto"/>'

    out = re.sub(r'<w:spacing w:after="(\d+)" w:line="(\d+)" w:lineRule="auto"/>', _full, styles_xml)
    if after is not None:
        out = re.sub(r'<w:spacing w:after="\d+"/>', f'<w:spacing w:after="{after}"/>', out)
    if line is not None:
        # paragraphs that declare only `after` need an explicit rule to be respaced
        out = re.sub(r'<w:spacing w:after="(\d+)"/>',
                     lambda m: f'<w:spacing w:after="{m.group(1)}" w:line="{line}" '
                               f'w:lineRule="auto"/>', out)
    return out


def load_templates(directory=None):
    directory = os.path.expanduser(directory or TEMPLATE_DIR)
    if not os.path.isdir(directory):
        raise FileNotFoundError(
            f"No templates directory at {directory}. Put your master resume .docx files "
            f"there, or set JOBWATCH_TEMPLATES to where they live.")
    out, skipped = [], []
    for f in sorted(os.listdir(directory)):
        if not f.lower().endswith(".docx") or f.startswith("~$") or f.startswith("."):
            continue
        if TEMPLATE_INCLUDE and not any(fnmatch.fnmatch(f, p) for p in TEMPLATE_INCLUDE):
            skipped.append(f)
            continue
        if any(fnmatch.fnmatch(f, p) for p in TEMPLATE_EXCLUDE):
            skipped.append(f)
            continue
        out.append(Template(os.path.join(directory, f)))
    if skipped:
        log.info("ignoring %d non-template .docx in %s: %s", len(skipped), directory,
                 ", ".join(skipped[:6]))
    if not out:
        raise FileNotFoundError(
            f"No matching .docx templates in {directory}"
            + (f" for patterns {TEMPLATE_INCLUDE}" if TEMPLATE_INCLUDE else ""))
    return out


def bank_summary(templates):
    rows = []
    for t in templates:
        rows.append({
            "kind": t.kind,
            "file": os.path.basename(t.path),
            "achievements": len(t.achievements),
            "jobs": [{"title": j["title"], "company": j["company"], "bullets": len(j["bullets"])}
                     for j in t.jobs],
            "total_bullets": len(t.bullets),
        })
    return rows


def main():
    templates = load_templates()
    if "--dump" in sys.argv:
        path = sys.argv[sys.argv.index("--dump") + 1]
        bank = {t.kind: {"file": t.path, "achievements": [t.bullets[i]["text"] for i in t.achievements],
                         "jobs": [{"title": j["title"], "company": j["company"],
                                   "bullets": [t.bullets[i]["text"] for i in j["bullets"]]}
                                  for j in t.jobs]} for t in templates}
        with open(path, "w") as f:
            json.dump(bank, f, indent=2)
        print(f"wrote {path}")
        return
    total = 0
    for row in bank_summary(templates):
        print(f"\n{row['kind']:<14} {row['file']}")
        print(f"  key achievements: {row['achievements']}")
        for j in row["jobs"]:
            print(f"  {j['bullets']:>3} bullets  {j['title'][:42]:<44} {j['company'][:40]}")
        total += row["total_bullets"]
    print(f"\nbullet bank: {total} bullets across {len(templates)} templates")


if __name__ == "__main__":
    main()