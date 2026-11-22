"""Taking a skill from somewhere else (2026-11-04, explicit instruction: "take the skill from such-and-such a file and
use it", with the pitfalls removed, and zipped skill packages supported).

`import_skills(workspace_dir, source)` turns what the owner points at into real skills under <workspace>/Skills/<name>/,
so skills.catalog() sees them like any other and they outlive the conversation:

    source = a folder       -> the folder is a skill (it holds a SKILL.md) or holds skills (any depth)
    source = .zip / .skill  -> unpacked safely (no path escapes, no links, size and count caps), then as a folder;
                               one package may carry many skills
    source = .md/.markdown/.txt/.docx -> the text becomes the SKILL.md of a skill of its own

What it fixes on the way, so a bad source does not make a bad skill:

    - no frontmatter / no name / no description -> they are written (the description from the first paragraph, and
      the result says it was derived, because the description is the only part she sees before opening a skill)
    - `skill.md` in another letter case, no SKILL.md at all but one instruction document -> SKILL.md
    - an odd name -> a safe folder name; a name that is already taken (by a code-seeded skill, a host skill, a lesson,
      another folder) is refused instead of silently shadowing or being overwritten by the next seeding
    - re-importing over an earlier import needs overwrite=true, and the old folder is kept under Skills/.replaced/
    - tool names of another agent (Bash, Read, ...) -> a note in the skill itself and a warning in the result
    - scripts in the package -> listed, never run
    - every imported folder carries `.imported.json`, which is what makes vault_backup.py keep it (a lesson skill is
      one SKILL.md, an imported one can be a whole folder)

Nothing is fetched from the network; the source is a local path the owner named."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app import skills
from app.logging_setup import log_event

MARKER = ".imported.json"
REPLACED_DIR = ".replaced"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
ZIP_SUFFIXES = {".zip", ".skill"}
MAX_ENTRIES = 2000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_SKILLS_PER_PACKAGE = 50
JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".ts", ".rb", ".pl", ".exe", ".dll", ".jar"}
_TOOLS = "Bash|Read|Write|Edit|MultiEdit|Glob|Grep|WebFetch|WebSearch|Task|TodoWrite|NotebookEdit|AskUserQuestion"
_FOREIGN_TOOL = re.compile(
    rf"`({_TOOLS})(?:\(|`)|\b({_TOOLS})\s+tool\b|\b(AskUserQuestion|TodoWrite|WebFetch|WebSearch|NotebookEdit|MultiEdit)\b"
)
FOREIGN_NOTE = (
    "> Imported note: this skill was written for another agent. Tool names in it (%s) are that agent's; "
    "do the same step with your own nearest tool."
)


class ImportProblem(Exception):
    """A source that cannot become a skill; the message is for the model, so it says what to do instead."""


@dataclass
class _Candidate:
    folder: Path
    hint: str


@dataclass
class _Prepared:
    name: str
    description: str
    text: str
    folder: Path
    warnings: list[str] = field(default_factory=list)


# --- reading a source ------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ImportProblem(f'"{path.name}" is not a readable .docx ({exc}).') from exc
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    xml = re.sub(r"<w:br\s*/>", "\n", xml)
    paragraphs = []
    for chunk in xml.split("</w:p>"):
        text = re.sub(r"<[^>]+>", "", chunk)
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
        paragraphs.append(text.strip())
    return "\n\n".join(p for p in paragraphs if p)


def _fix_zip_name(info: zipfile.ZipInfo) -> str:
    """Windows archivers without the UTF-8 flag store names in the OEM code page; Python reads them as cp437."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "cp866"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _clean_relative(name: str) -> list[str] | None:
    """The parts of a path inside a package; None for junk to leave out; ImportProblem for an escape."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." or ":" in p for p in parts) or name.startswith(("/", "\\")):
        raise ImportProblem(f'The package holds an unsafe path "{name}"; it was not imported.')
    if parts[0] == "__MACOSX" or any(p.startswith(".") or p.lower() in JUNK_FILES for p in parts):
        return None
    return [re.sub(r'[<>"|?*]', "_", p).rstrip(" .") or "_" for p in parts]


def extract_zip_safely(archive: Path, dest: Path) -> int:
    """Unpacks a zip into `dest` (created): no path may leave it, links and hidden files are dropped, the entry count
    and the unpacked size are capped (also against a header that lies about the size). Returns the files written."""
    written = 0
    total = 0
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportProblem(f'"{archive.name}" is not a readable zip ({exc}).') from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ImportProblem(f'"{archive.name}" holds {len(infos)} entries; more than {MAX_ENTRIES} is not a skill package.')
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ImportProblem(f'"{archive.name}" unpacks to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB; not imported.')
        for info in infos:
            if info.flag_bits & 0x1:
                raise ImportProblem(f'"{archive.name}" is password-protected; ask the owner for an unprotected copy.')
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # a symbolic link
            parts = _clean_relative(_fix_zip_name(info))
            if parts is None:
                continue
            target = root.joinpath(*parts)
            if root != target.resolve() and root not in target.resolve().parents:
                raise ImportProblem(f'The package holds an unsafe path "{info.filename}"; it was not imported.')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise ImportProblem(f'"{archive.name}" unpacks to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB; not imported.')
                    out.write(chunk)
            written += 1
    return written


def _copy_folder(src: Path, dest: Path) -> int:
    """A filtered copy: no links, no hidden or junk files, capped like a zip."""
    count = 0
    total = 0
    dest.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__MACOSX" and not (Path(current) / d).is_symlink())
        rel = Path(current).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or name.lower() in JUNK_FILES or path.is_symlink():
                continue
            count += 1
            total += path.stat().st_size
            if count > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
                raise ImportProblem(f'"{src.name}" is too big for a skill (over {MAX_ENTRIES} files or {MAX_TOTAL_BYTES // (1024 * 1024)} MB); not imported.')
            shutil.copy2(path, dest / rel / name)
    return count


def _write_text(path: Path, text: str) -> None:
    """UTF-8 with plain LF line ends on every platform (write_text would turn each LF into CRLF on Windows)."""
    path.write_bytes(text.replace(chr(13) + chr(10), chr(10)).encode("utf-8"))


def _skill_md(folder: Path) -> Path | None:
    try:
        return next((p for p in folder.iterdir() if p.is_file() and p.name.lower() == "skill.md"), None)
    except OSError:
        return None


def _normalize_skill_md(path: Path) -> Path:
    if path.name == skills.SKILL_FILE:
        return path
    temp = path.with_name(path.name + ".renaming")
    path.rename(temp)
    final = path.with_name(skills.SKILL_FILE)
    temp.rename(final)
    return final


def _find_skill_folders(work: Path) -> list[Path]:
    found: list[Path] = []

    def walk(folder: Path) -> None:
        if _skill_md(folder):
            found.append(folder)
            return
        for child in sorted(p for p in folder.iterdir() if p.is_dir()):
            walk(child)

    walk(work)
    return found


def _bare_instruction_folder(work: Path) -> _Candidate:
    """A package with no SKILL.md: if it holds one instruction document, that document is the skill."""
    base = work
    while True:
        entries = [p for p in base.iterdir()]
        dirs = [p for p in entries if p.is_dir()]
        if len(dirs) == 1 and len(entries) == 1:
            base = dirs[0]
            continue
        break
    docs = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES]
    chosen = docs[0] if len(docs) == 1 else next(
        (p for p in docs if p.stem.lower() in ("readme", "instructions", "instruction", "prompt")), None
    )
    if chosen is None:
        listing = ", ".join(sorted(p.name for p in base.iterdir())[:20]) or "(empty)"
        raise ImportProblem(
            "No SKILL.md in the package and no single instruction document to take as one. It holds: "
            f"{listing}. Ask the owner which file holds the instructions."
        )
    chosen.rename(chosen.with_name("SKILL.tmp"))
    chosen.with_name("SKILL.tmp").rename(base / skills.SKILL_FILE)
    return _Candidate(base, base.name if base != work else work.name)


def _stage(source: Path, tmp: Path) -> list[_Candidate]:
    """Copies/unpacks the source into tmp/<name>/ and returns the skill folders found in it."""
    suffix = source.suffix.lower()
    work = tmp / (source.stem if source.is_file() else source.name)
    if source.is_dir():
        _copy_folder(source, work)
    elif suffix in ZIP_SUFFIXES:
        extract_zip_safely(source, work)
    elif suffix == ".docx":
        work.mkdir(parents=True)
        _write_text(work / skills.SKILL_FILE, _docx_text(source))
    elif suffix in TEXT_SUFFIXES:
        work.mkdir(parents=True)
        _write_text(work / skills.SKILL_FILE, _decode(source.read_bytes()))
    else:
        raise ImportProblem(
            f'Cannot take a skill from a "{suffix or "no-extension"}" file. Supported: a folder, .zip/.skill, '
            ".md/.markdown/.txt, .docx. Convert it to text first (extract the text of a PDF, for example) and import that."
        )
    folders = _find_skill_folders(work)
    if not folders:
        return [_bare_instruction_folder(work)]
    if len(folders) > MAX_SKILLS_PER_PACKAGE:
        raise ImportProblem(f"The package holds {len(folders)} skills; at most {MAX_SKILLS_PER_PACKAGE} per import.")
    return [_Candidate(f, f.name) for f in folders]


# --- making one skill of it ------------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^\w.\-]+", "-", (name or "").strip(), flags=re.UNICODE).strip("-.")[:64].strip("-.")
    return slug.lower() or "imported-skill"


def _derive_description(body: str) -> str:
    heading = ""
    in_code = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        if s.startswith("#"):
            heading = heading or s.lstrip("#").strip()
            continue
        if s.startswith(("---", "===", "|", "<")):
            continue
        s = re.sub(r"[*_`]", "", re.sub(r"^[>\-*\d.)\s]+", "", s))
        if len(s) >= 8:
            return " ".join(s.split())[: skills.MAX_DESCRIPTION_CHARS]
    return heading[: skills.MAX_DESCRIPTION_CHARS]


def _foreign_tools(text: str, meta: dict[str, str]) -> list[str]:
    found = {next(g for g in m.groups() if g) for m in _FOREIGN_TOOL.finditer(text)}
    if "allowed-tools" in meta or "allowed_tools" in meta:
        found.add("allowed-tools")
    return sorted(found)


def _prepare(candidate: _Candidate, name: str | None, description: str | None) -> _Prepared:
    md = _normalize_skill_md(_skill_md(candidate.folder))  # type: ignore[arg-type]
    meta, body = skills.split_frontmatter(_decode(md.read_bytes()).replace("\r\n", "\n"))
    if not body.strip():
        raise ImportProblem(f'The skill "{candidate.hint}" has no instructions in it (its text is empty).')
    warnings: list[str] = []
    raw_name = name or meta.get("name") or candidate.hint
    if not (name or meta.get("name")):
        h1 = next((l.lstrip("#").strip() for l in body.splitlines() if l.startswith("# ")), "")
        if candidate.hint.lower() in ("skill", "skills") and h1:
            raw_name = h1
    final_name = slugify(raw_name)
    final_description = " ".join((description or meta.get("description") or "").split())[: skills.MAX_DESCRIPTION_CHARS]
    if not final_description:
        final_description = _derive_description(body) or f"Imported skill {final_name}."
        warnings.append(
            "It had no description, so one was taken from its first paragraph. The description is the ONLY part of a "
            "skill you see before opening it: read it, and if it does not say when to use the skill, import again "
            "with description=... and overwrite=true."
        )
    foreign = _foreign_tools(body, meta)
    if foreign and "written for another agent" not in body:
        body = body.rstrip() + "\n\n" + FOREIGN_NOTE % ", ".join(foreign) + "\n"
        warnings.append(f"It mentions tools of another agent ({', '.join(foreign)}); a note about that was added to the skill.")
    keep = (
        meta.get("name", "").strip() == final_name
        and " ".join(meta.get("description", "").split()) == final_description
        and not foreign
    )
    if keep:
        text = _decode(md.read_bytes()).replace("\r\n", "\n")
    else:
        fields: dict[str, str] = {"name": final_name, "description": final_description}
        for key, value in meta.items():
            if key not in fields:
                fields[key] = " ".join(str(value).split())
        head = "\n".join(f"{k}: {v}" for k, v in fields.items())
        text = f"---\n{head}\n---\n\n{body.strip()}\n"
    scripts = sorted(
        p.relative_to(candidate.folder).as_posix()
        for p in candidate.folder.rglob("*")
        if p.is_file() and p.suffix.lower() in SCRIPT_SUFFIXES
    )
    if scripts:
        shown = ", ".join(scripts[:8]) + (" ..." if len(scripts) > 8 else "")
        warnings.append(f"It brings scripts ({shown}); none was run. Read one before running it.")
    return _Prepared(final_name, final_description, text, candidate.folder, warnings)


# --- putting it into the workspace -----------------------------------------------------------------------

def _seeded_names() -> set[str]:
    from app import skills_seed

    src = skills_seed._resolve_skills_src()
    return {p.name.lower() for p in src.iterdir() if p.is_dir()} if src else set()


def _install(workspace_dir: str, prepared: _Prepared, source: Path, overwrite: bool) -> dict[str, Any]:
    skills_dir = Path(workspace_dir) / "Skills"
    target = skills_dir / prepared.name
    if prepared.name in _seeded_names() or prepared.name == "lessons-learned":
        raise ImportProblem(
            f'"{prepared.name}" is the name of a skill the app manages itself and rewrites on every start; '
            "import it under another name (name=...)."
        )
    taken = skills.find(workspace_dir, prepared.name)
    if taken is not None and (not target.exists() or taken.folder.resolve() != target.resolve()):
        raise ImportProblem(
            f'A skill named "{prepared.name}" already exists ({taken.source}: {taken.folder}). '
            "Import this one under another name (name=...); it was not imported."
        )
    if target.exists():
        if not (target / skills.SKILL_FILE).is_file():
            raise ImportProblem(f'"Skills/{prepared.name}" is a folder of other skills, not a skill; use another name (name=...).')
        if not overwrite:
            raise ImportProblem(
                f'The skill "{prepared.name}" is already there. To replace it pass overwrite=true '
                "(the old one is kept under Skills/.replaced/)."
            )
        kept = skills_dir / REPLACED_DIR / f"{prepared.name}-{datetime.now().strftime('%Y%m%d%H%M')}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        if kept.exists():
            shutil.rmtree(kept)
        shutil.move(str(target), str(kept))
    shutil.copytree(prepared.folder, target)
    _write_text(target / skills.SKILL_FILE, prepared.text)
    (target / MARKER).write_text(
        json.dumps({"imported_at": datetime.now().isoformat(timespec="minutes"), "source": source.name}, ensure_ascii=False),
        encoding="utf-8",
    )
    files = [f for f in skills.skill_files(skills.Skill(prepared.name, prepared.description, target, "workspace"))]
    log_event("engine", "skill_imported", name=prepared.name, source=source.name, files=len(files) + 1)
    return {
        "name": prepared.name,
        "description": prepared.description,
        "folder": str(target),
        "files": len(files) + 1,
        "warnings": prepared.warnings,
    }


def import_skills(workspace_dir: str, source: str, name: str | None = None, description: str | None = None,
                  overwrite: bool = False) -> dict[str, Any]:
    """Imports every skill in `source`. {"imported": [...], "skipped": [{"name", "reason"}]}; raises ImportProblem when
    the source as a whole cannot be used."""
    path = Path(str(source or "").strip().strip('"').strip("'")).expanduser()
    if not str(source or "").strip() or not path.exists():
        raise ImportProblem(f'"{source}" does not exist. Give the full path of the file, folder or archive.')
    tmp = Path(tempfile.mkdtemp(prefix="skill-import-"))
    try:
        candidates = _stage(path, tmp)
        if (name or description) and len(candidates) > 1:
            raise ImportProblem(f"The package holds {len(candidates)} skills; name=/description= fit a single skill. Import without them.")
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for candidate in candidates:
            label = candidate.hint
            try:
                prepared = _prepare(candidate, name, description)
                label = prepared.name
                imported.append(_install(workspace_dir, prepared, path, overwrite))
            except ImportProblem as exc:
                skipped.append({"name": label, "reason": str(exc)})
        return {"imported": imported, "skipped": skipped}
    except OSError as exc:
        raise ImportProblem(f"Could not read or write while importing: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def report_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in result["imported"]:
        lines.append(f'Imported "{item["name"]}" ({item["files"]} files) into {item["folder"]}.\nDescription: {item["description"]}')
        lines.extend(f"  ! {w}" for w in item["warnings"])
    for item in result["skipped"]:
        lines.append(f'Skipped "{item["name"]}": {item["reason"]}')
    if result["imported"]:
        lines.append(
            "use_skill(name) opens it right now; its line in your skill list appears at the start of the next session. "
            "From now on its text is a standing instruction for you. It is copied to Notes by the hourly backup."
        )
    return "\n".join(lines)
