#!/usr/bin/env python3
"""skill-audit: see which installed Claude Code skills you actually use.

Everything runs locally. Nothing is sent anywhere.

  audit.py report  [--days N] [--json] [--out FILE] [--html] [--no-open]
  audit.py guide   [--all] [--save FILE]
  audit.py archive [NAME ...] [--group G ...] [--unused] [--days N] [--yes]
  audit.py restore [NAME ...] [--batch ID] [--yes]
  audit.py archives

`archive` and `restore` only print a plan unless --yes is given.
Archiving moves skill folders to <config>/skills-archive/<batch>/; it never deletes.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import webbrowser
from collections import defaultdict
from pathlib import Path

SELF_NAME = "skill-audit"
ACTIVE_MIN = 3  # calls needed to count as "active"


# ---------------------------------------------------------------- discovery

def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm, key = {}, None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                val = ""
            fm[key] = val.strip("\"'")
        elif key and line[:1] in (" ", "\t"):
            fm[key] = (fm[key] + " " + line.strip()).strip()
    return fm


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def group_of(name: str) -> str:
    base = name.split(":")[-1]
    return base.split("-")[0] if "-" in base else base


def skill_record(path: Path, source: str, plugin: str = "") -> dict:
    name = path.name
    rec = {
        "name": name, "source": source, "plugin": plugin, "path": str(path),
        "is_link": path.is_symlink(), "broken": False, "size": 0, "lines": 0,
        "desc_chars": 0, "description": "", "group": group_of(name),
        "movable": source in ("user", "project"), "problems": [],
        "invoke": "/" + (plugin + ":" if plugin else "") + name,
    }
    if rec["is_link"] and not path.exists():
        rec["broken"] = True
        rec["problems"].append("broken link -> " + os.readlink(path))
        return rec
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        rec["problems"].append("no SKILL.md")
        return rec
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        rec["problems"].append(f"unreadable SKILL.md ({e.strerror})")
        return rec
    fm = parse_frontmatter(text)
    rec["fm_name"] = fm.get("name", "")
    if fm.get("user-invocable", "").lower() == "false":
        rec["invoke"] = ""
    rec["description"] = fm.get("description", "")
    rec["desc_chars"] = len(rec["description"])
    rec["lines"] = text.count("\n") + 1
    rec["size"] = dir_size(path.resolve())
    if not rec["description"]:
        rec["problems"].append("no description (Claude cannot tell when to use it)")
    return rec


def discover(cfg: Path, cwd: Path) -> list:
    records, seen = [], set()

    def add_dir(root: Path, source: str, plugin: str = ""):
        if not root.is_dir():
            return
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            if not (child.is_dir() or child.is_symlink()):
                continue
            key = (source, plugin, child.name)
            if key in seen:
                continue
            seen.add(key)
            records.append(skill_record(child, source, plugin))

    user_root = cfg / "skills"
    add_dir(user_root, "user")
    # Project skills live in <project>/.claude/skills. In the home folder that path is the
    # personal ~/.claude/skills, which is never a project, even when CLAUDE_CONFIG_DIR points elsewhere.
    proj_root = cwd / ".claude" / "skills"
    personal = {(Path.home() / ".claude" / "skills").resolve()}
    if user_root.exists():
        personal.add(user_root.resolve())
    if proj_root.is_dir() and cwd.resolve() != Path.home().resolve() and proj_root.resolve() not in personal:
        add_dir(proj_root, "project")

    installed = cfg / "plugins" / "installed_plugins.json"
    if installed.is_file():
        try:
            data = json.loads(installed.read_text())
        except (OSError, ValueError):
            data = {}
        plugins = data.get("plugins", data) if isinstance(data, dict) else {}
        for plugin_id, entries in (plugins.items() if isinstance(plugins, dict) else []):
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries if isinstance(entries, list) else []:
                ip = entry.get("installPath") if isinstance(entry, dict) else None
                if ip:
                    add_dir(Path(ip) / "skills", "plugin", plugin_id.split("@")[0])
    return records


# ---------------------------------------------------------------- usage

CMD_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
READ_RE = re.compile(r"/skills/([^/]+)/SKILL\.md$")
TS_RE = re.compile(r'"timestamp":\s*"([^"]+)"')


def _texts(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                yield b.get("text", "")


def scan_usage(cfg: Path, days):
    """Return (events_by_key, meta). events_by_key[key] = list of (timestamp, project)."""
    root = cfg / "projects"
    since = None
    if days:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    events = defaultdict(list)
    seen_ids = set()
    meta = {"main_sessions": 0, "subagent_logs": 0, "first": None, "last": None}
    if not root.is_dir():
        return events, meta

    for dirpath, _dirs, files in os.walk(root, followlinks=True):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, fn)
            if since:
                try:
                    if dt.datetime.fromtimestamp(os.path.getmtime(fp), dt.timezone.utc) < since:
                        continue
                except OSError:
                    continue
            is_sub = f"{os.sep}subagents{os.sep}" in fp
            meta["subagent_logs" if is_sub else "main_sessions"] += 1
            project_fallback = Path(dirpath).name if not is_sub else Path(dirpath).parent.parent.name
            try:
                fh = open(fp, encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                mtime = dt.datetime.fromtimestamp(os.path.getmtime(fp), dt.timezone.utc).isoformat()
                if meta["last"] is None or mtime > meta["last"]:
                    meta["last"] = mtime
            except OSError:
                pass
            got_first = False
            with fh:
                for line in fh:
                    if not got_first:
                        m = TS_RE.search(line)
                        if m:
                            got_first = True
                            first = m.group(1)
                            if since:
                                first = max(first, since.isoformat())
                            if meta["first"] is None or first < meta["first"]:
                                meta["first"] = first
                    if '"Skill"' not in line and "command-name" not in line and "SKILL.md" not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") not in ("assistant", "user"):
                        continue  # skip raw API copies and other duplicates
                    ts = d.get("timestamp")
                    if since and ts and (_parse_ts(ts) or since) < since:
                        continue
                    project = Path(d.get("cwd") or project_fallback).name
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if d.get("type") == "assistant" and isinstance(content, list):
                        for b in content:
                            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                                continue
                            uid = b.get("id")
                            if uid in seen_ids:
                                continue  # same call copied into a resumed session
                            if uid:
                                seen_ids.add(uid)
                            inp = b.get("input") or {}
                            if b.get("name") == "Skill":
                                key = inp.get("skill") or inp.get("command") or inp.get("name")
                                if key:
                                    events[str(key).lstrip("/")].append((ts, project, "skill"))
                            elif b.get("name") == "Read":
                                rm = READ_RE.search(str(inp.get("file_path", "")))
                                if rm:
                                    events[rm.group(1)].append((ts, project, "read"))
                    elif d.get("type") == "user" and not d.get("isMeta"):
                        uid = d.get("uuid")
                        if uid in seen_ids:
                            continue
                        if uid:
                            seen_ids.add(uid)
                        for t in _texts(content):
                            for name in CMD_RE.findall(t):
                                events[name.lstrip("/")].append((ts, project, "command"))
    return events, meta


def _parse_ts(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def attach_usage(records, events):
    """Fill calls/last/projects on each record. Returns keys used but not matched to any installed skill."""
    matched = set()
    local_names = set()
    for rec in records:
        if not rec["plugin"]:
            local_names.add(rec["name"])
            if rec.get("fm_name"):
                local_names.add(rec["fm_name"])
    for rec in records:
        aliases = {rec["name"]}
        if rec.get("fm_name"):
            aliases.add(rec["fm_name"])
        if rec["plugin"]:
            bare = aliases - local_names
            aliases = bare | {f"{rec['plugin']}:{a}" for a in aliases}
        calls, last, projects = 0, None, set()
        for key, evs in events.items():
            if key in aliases:
                matched.add(key)
                calls += len(evs)
                for ts, proj, _kind in evs:
                    if ts and (last is None or ts > last):
                        last = ts
                    projects.add(proj)
        rec["calls"], rec["last_used"], rec["projects"] = calls, last, sorted(projects)
        rec["status"] = "active" if calls >= ACTIVE_MIN else "occasional" if calls else "unused"
    # Slash commands that match no skill are built-in commands (/model, /exit ...): leave them out.
    unmatched = {}
    for k, evs in events.items():
        if k in matched:
            continue
        n = sum(1 for e in evs if e[2] != "command")
        if n:
            unmatched[k] = n
    return unmatched


# ---------------------------------------------------------------- report

# ---------------------------------------------------------------- guide

GUIDE_KEYS = ("what", "when", "try")


def guide_path(cfg):
    return cfg / "skill-audit" / "guide.json"


def desc_hash(desc):
    return hashlib.sha1(desc.encode("utf-8")).hexdigest()[:10]


def guide_key(r):
    """One key per installed skill: a personal skill by name, others prefixed so same-named skills never share an entry."""
    if r["plugin"]:
        return f"{r['plugin']}:{r['name']}"
    return f"project:{r['name']}" if r["source"] == "project" else r["name"]


class GuideError(Exception):
    pass


def load_guide(cfg, strict=False):
    """The saved guide entries. With strict, a file that exists but cannot be read raises instead of reading as empty."""
    path = guide_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if strict:
            raise GuideError(f"{path} could not be read ({e}). Fix or move it first; nothing was saved.")
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        if strict:
            raise GuideError(f"{path} has no 'skills' object. Fix or move it first; nothing was saved.")
        return {}
    return skills


def guide_entry(guide, r):
    """This skill's entry. Falls back to an entry saved under the bare name by v0.2.0 when its description still matches."""
    e = guide.get(guide_key(r))
    if not isinstance(e, dict) and guide_key(r) != r["name"]:
        old = guide.get(r["name"])
        if isinstance(old, dict) and old.get("desc_hash") == desc_hash(r["description"]):
            e = old
    return e if isinstance(e, dict) else None


def guide_state(records, guide):
    """Split loadable skills into (has an up-to-date guide entry, needs one)."""
    ok, need = [], []
    for r in records:
        e = guide_entry(guide, r)
        fresh = e and e.get("what") and e.get("desc_hash") == desc_hash(r["description"])
        (ok if fresh else need).append(r)
    return ok, need


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def cmd_guide(args):
    cfg = config_dir()
    records = [r for r in discover(cfg, Path.cwd()) if is_skill(r)]
    path = guide_path(cfg)
    try:
        guide = load_guide(cfg, strict=bool(args.save))
    except GuideError as e:
        print(e, file=sys.stderr)
        return 1
    if args.save:
        try:
            new = json.loads(Path(args.save).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Could not read {args.save}: {e}", file=sys.stderr)
            return 1
        if isinstance(new, dict) and isinstance(new.get("skills"), dict):
            new = new["skills"]
        by_key = {guide_key(r): r for r in records}
        by_name = defaultdict(list)
        for r in records:
            by_name[r["name"]].append(r)
        saved, skipped, ambiguous = [], [], []
        for name, e in (new.items() if isinstance(new, dict) else []):
            r = by_key.get(name)
            if r is None and len(by_name.get(name, [])) == 1:
                r = by_name[name][0]
            elif r is None and len(by_name.get(name, [])) > 1:
                ambiguous.append(name)
                continue
            if r is None or not isinstance(e, dict) or not str(e.get("what", "")).strip():
                skipped.append(name)
                continue
            entry = {k: " ".join(str(e.get(k, "")).split())[:400] for k in GUIDE_KEYS}
            entry["desc_hash"] = desc_hash(r["description"])
            key = guide_key(r)
            guide[key] = entry
            old = guide.get(r["name"])
            if key != r["name"] and isinstance(old, dict) and r["name"] not in by_key:
                del guide[r["name"]]  # an entry v0.2.0 saved under the bare name
            saved.append(key)
        write_atomic(path, json.dumps({"version": 2, "skills": guide}, ensure_ascii=False, indent=1) + "\n")
        print(f"Saved guide entries for {len(saved)} skills to {path}")
        if skipped:
            print(f"Skipped {len(skipped)} (not installed, or no 'what'): {', '.join(skipped)}")
        if ambiguous:
            print(f"Skipped {len(ambiguous)} names that more than one skill uses; save them under their key "
                  f"from `audit.py guide`: {', '.join(ambiguous)}")
        _ok, need = guide_state(records, guide)
        print(f"{len(records) - len(need)} of {len(records)} installed skills now have a guide.")
        return 0
    ok, need = guide_state(records, guide)
    todo = records if args.all else need
    print(json.dumps({
        "guide_file": str(path),
        "installed": len(records),
        "with_guide": len(ok),
        "to_write": [{"key": guide_key(r), "name": r["name"], "invoke": r["invoke"], "source": r["source"],
                      "description": r["description"], "skill_md": str(Path(r["path"]) / "SKILL.md")}
                     for r in todo],
    }, ensure_ascii=False, indent=1))
    return 0


def self_cmd():
    """How to run this script from any terminal, carrying CLAUDE_CONFIG_DIR when this config is not ~/.claude."""
    cmd = f"python3 {shlex.quote(str(Path(__file__).resolve()))}"
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        cmd = f"CLAUDE_CONFIG_DIR={shlex.quote(str(Path(env).expanduser()))} {cmd}"
    return cmd


def build(days):
    cfg = config_dir()
    records = discover(cfg, Path.cwd())
    events, meta = scan_usage(cfg, days)
    unmatched = attach_usage(records, events)
    return cfg, records, meta, unmatched


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def short_date(ts):
    return ts[:10] if ts else "-"


def is_skill(r):
    """A folder Claude Code would actually load: has a SKILL.md and is not a broken link."""
    return not r["broken"] and "no SKILL.md" not in r["problems"]


def report_md(cfg, records, meta, unmatched, days):
    all_records = records
    records = [r for r in all_records if is_skill(r)]
    out = []
    w = out.append
    total = len(records)
    used = [r for r in records if r["calls"]]
    calls = sum(r["calls"] for r in records)
    span = ""
    if meta["first"] and meta["last"]:
        d0, d1 = _parse_ts(meta["first"]), _parse_ts(meta["last"])
        span = f"{short_date(meta['first'])} to {short_date(meta['last'])} ({(d1 - d0).days + 1} days)" if d0 and d1 else ""

    w("# Skill audit\n")
    w(f"Config: `{cfg}`  ")
    w(f"History read: {meta['main_sessions']} sessions + {meta['subagent_logs']} subagent logs, {span or 'no history found'}"
      + (f" (limited to last {days} days)" if days else "") + "\n")
    pct = f"{len(used) / total * 100:.0f}%" if total else "0%"
    w(f"**{total} skills installed. {len(used)} used ({pct}). {calls} calls in total.**\n")

    # groups
    groups = defaultdict(list)
    for r in records:
        groups[r["group"]].append(r)
    w("## By group\n")
    w("| Group | Installed | Used | Calls | Never used |")
    w("| --- | ---: | ---: | ---: | --- |")
    for g, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        never = [r["name"] for r in rs if not r["calls"]]
        never_s = "all" if len(never) == len(rs) and len(rs) > 1 else ", ".join(never[:6]) + (f" +{len(never) - 6}" if len(never) > 6 else "")
        w(f"| {g} | {len(rs)} | {len(rs) - len(never)} | {sum(r['calls'] for r in rs)} | {never_s or '-'} |")
    w("")

    if used:
        w("## Most used\n")
        w("| Skill | Calls | Last used | Projects |")
        w("| --- | ---: | --- | --- |")
        for r in sorted(used, key=lambda r: (-r["calls"], r["name"]))[:15]:
            projs = ", ".join(r["projects"][:3]) + (f" +{len(r['projects']) - 3}" if len(r["projects"]) > 3 else "")
            w(f"| {r['name']} | {r['calls']} | {short_date(r['last_used'])} | {projs} |")
        w("")

    unused = [r for r in records if not r["calls"]]
    desc_all = sum(r["desc_chars"] for r in records)
    desc_unused = sum(r["desc_chars"] for r in unused)
    w("## Cost of keeping them\n")
    w(f"- Skill descriptions Claude reads at the start of every session: {desc_all:,} characters, "
      f"{desc_unused:,} of them ({(desc_unused / desc_all * 100) if desc_all else 0:.0f}%) from skills never used.")
    w(f"- Disk: {human(sum(r['size'] for r in records))} in total, {human(sum(r['size'] for r in unused))} in unused skills.\n")

    problems = [r for r in all_records if r["problems"]]
    if problems:
        w("## Problems\n")
        for r in problems:
            w(f"- **{r['name']}** ({r['source']}): {'; '.join(r['problems'])}")
        w("")

    if unmatched:
        w("## Used, but not installed in these folders\n")
        w("Built-in skills, skills from Claude apps, or skills you removed:\n")
        w(", ".join(f"{k} ({n})" for k, n in sorted(unmatched.items(), key=lambda kv: -kv[1])[:15]) + "\n")

    # suggestions
    w("## Suggested clean-up\n")
    sugg = []
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        movable = [r for r in rs if r["movable"] and r["name"] != SELF_NAME]
        if len(rs) >= 2 and all(not r["calls"] for r in rs) and movable:
            sugg.append(f"- Group **{g}**: {len(rs)} skills, none used. `audit.py archive --group {g}`")
    loose = [r["name"] for r in unused if r["movable"] and r["name"] != SELF_NAME
             and not all(not x["calls"] for x in groups[r["group"]])]
    if loose:
        sugg.append(f"- Unused skills in groups you do use: {', '.join(loose)}")
    plug = sorted({r["plugin"] for r in unused if r["source"] == "plugin"})
    if plug:
        sugg.append(f"- Unused plugin skills ({', '.join(plug)}): remove them with `/plugin`, not by moving folders.")
    broken = [r["name"] for r in all_records if r["broken"]]
    if broken:
        sugg.append(f"- Broken links, safe to archive: {', '.join(broken)}")
    w("\n".join(sugg) if sugg else "Nothing to clean up.")
    w("")
    _ok, need = guide_state(records, load_guide(cfg))
    w(f"Plain-language guide: {total - len(need)} of {total} skills have one"
      + (f"; {len(need)} to write (`audit.py guide`)." if need else "."))
    w("")
    w("Archiving moves folders to `skills-archive/` and can be undone with `audit.py restore`. "
      "Only history that Claude Code still keeps is counted: a skill used rarely may show as unused.")
    return "\n".join(out)


def write_html(cfg, records, meta, unmatched, days, out_path, open_it):
    skills = [r for r in records if is_skill(r)]
    keep = ("name", "group", "source", "plugin", "calls", "last_used", "projects", "status",
            "desc_chars", "description", "size", "is_link", "invoke")
    rows = []
    for r in skills:
        row = {k: r.get(k) for k in keep}
        row["key"] = guide_key(r)
        rows.append(row)
    guide = load_guide(cfg)
    restore = f"{self_cmd()} restore --batch {{id}} --yes"
    data = {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "meta": meta,
        "days": days,
        "skills": rows,
        "problems": [{"name": r["name"], "source": r["source"], "problems": r["problems"]}
                     for r in records if r["problems"]],
        "unmatched": unmatched,
        "batches": [{"id": b.name, "names": sorted(mf)} for b, mf in load_batches(cfg)],
        "restore_cmd": restore,
        "guide": {guide_key(r): {k: guide_entry(guide, r).get(k, "") for k in GUIDE_KEYS}
                  for r in skills if guide_entry(guide, r)},
        "guide_missing": len(guide_state(skills, guide)[1]),
    }
    # Write every "<" as \u003c, so no value (a "</script>" or a "<!--<script") can end or confuse the <script> tag.
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = (Path(__file__).resolve().parent / "report_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", blob, 1)
    out_path = Path(out_path) if out_path else cfg / "skill-audit" / "report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    opened = False
    if open_it:
        try:
            opened = bool(webbrowser.open(out_path.resolve().as_uri()))
        except webbrowser.Error:
            opened = False
    return out_path, opened


def cmd_report(args):
    cfg, records, meta, unmatched = build(args.days)
    html_note = None
    if args.html:
        path, opened = write_html(cfg, records, meta, unmatched, args.days, args.html_out, not args.no_open)
        html_note = f"Visual report: {path}" + (" (opened in your browser)" if opened else
                                                "" if args.no_open else " (could not open a browser: open this file yourself)")
    if args.json:
        text = json.dumps({"config": str(cfg), "meta": meta, "skills": records, "unmatched": unmatched},
                          ensure_ascii=False, indent=2)
    else:
        text = report_md(cfg, records, meta, unmatched, args.days)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Saved report to {args.out}", file=sys.stderr if args.json else sys.stdout)
    else:
        print(text)
    if html_note:
        print(("" if args.out else "\n") + html_note, file=sys.stderr if args.json and not args.out else sys.stdout)


# ---------------------------------------------------------------- archive / restore

def archive_root(cfg):
    return cfg / "skills-archive"


def cmd_archive(args):
    cfg, records, _meta, _ = build(args.days)
    by_name = defaultdict(list)
    for r in records:
        by_name[r["name"]].append(r)
    targets, notes = [], []
    for n in args.names:
        rs = by_name.get(n, [])
        user = [r for r in rs if r["movable"] and r["source"] != "project"]
        proj = [r for r in rs if r["movable"] and r["source"] == "project"]
        plug = [r for r in rs if not r["movable"]]
        if not rs:
            notes.append(f"not found: {n}")
            continue
        if user:
            targets += user
            if proj and not args.project:
                notes.append(f"{n}: moving your personal one; a project skill has the same name (add --project to move it too)")
            elif proj:
                targets += proj
        elif proj:
            targets += proj
        for r in plug:
            notes.append(f"{n} {'also ' if user or proj else ''}comes from plugin {r['plugin']}: use /plugin to remove it")
    for g in args.group:
        targets += [r for r in records if r["group"] == g and r["movable"]]
    if args.unused:
        targets += [r for r in records if not r["calls"] and r["movable"]]
    # Project skills belong to a repository; move them only when asked for by name or with --project.
    if not args.project:
        named = {n for n in args.names if not any(r["movable"] and r["source"] != "project" for r in by_name.get(n, []))}
        dropped = [r["name"] for r in targets if r["source"] == "project" and r["name"] not in named]
        if dropped:
            notes.append(f"leaving {len(dropped)} project skill(s) alone (add --project to include them)")
        targets = [r for r in targets if r["source"] != "project" or r["name"] in named]
    uniq = {}
    for r in targets:
        if r["name"] == SELF_NAME:
            notes.append("skipping skill-audit itself")
            continue
        uniq[r["path"]] = r
    targets = list(uniq.values())

    for n in notes:
        print("note:", n)
    if not targets:
        print("Nothing to archive.")
        return 1 if args.names or args.group else 0

    batch = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while (archive_root(cfg) / batch).exists():
        n += 1
        batch = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{n}"
    dest_root = archive_root(cfg) / batch
    print(f"{'Archiving' if args.yes else 'Would archive'} {len(targets)} skill(s) to {dest_root}:")
    for r in sorted(targets, key=lambda r: r["name"]):
        print(f"  - {r['name']}  ({r['source']}, {r['calls']} calls{', link' if r['is_link'] else ''})")
    if not args.yes:
        print("\nDry run. Nothing moved. Add --yes to do it.")
        return 0

    dest_root.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for r in targets:
        src = Path(r["path"])
        label = src.name
        if label in manifest:  # two skills with the same folder name: keep both, side by side
            label = f"{src.name}__{r['source']}"
            i = 2
            while label in manifest:
                label, i = f"{src.name}__{r['source']}{i}", i + 1
        shutil.move(str(src), str(dest_root / label))
        manifest[label] = str(src)
    (dest_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nDone. Batch id: {batch}. Undo from any terminal with:\n  {self_cmd()} restore --batch {batch} --yes")
    print("Start a new Claude Code session to see the change.")
    return 0


def load_batches(cfg):
    root = archive_root(cfg)
    if not root.is_dir():
        return []
    out = []
    for b in sorted(root.iterdir()):
        mf = b / "manifest.json"
        if mf.is_file():
            try:
                out.append((b, json.loads(mf.read_text())))
            except ValueError:
                pass
    return out


def cmd_archives(_args):
    batches = load_batches(config_dir())
    if not batches:
        print("No archived skills.")
        return 0
    for b, mf in batches:
        print(f"{b.name}: {len(mf)} skill(s)  {', '.join(sorted(mf))}")
    return 0


def cmd_restore(args):
    cfg = config_dir()
    batches = load_batches(cfg)
    if args.batch:
        batches = [x for x in batches if x[0].name == args.batch]
        if not batches:
            print(f"No batch {args.batch}.")
            return 1
    plan, dupes = {}, []
    for b, mf in batches:
        for name, original in mf.items():
            if args.names and name not in args.names and Path(original).name not in args.names:
                continue
            if (b / name).exists() or (b / name).is_symlink():
                if original in plan:  # archived again later: the newest copy goes back
                    dupes.append(f"{name} in batch {plan[original][0].name}")
                plan[original] = (b, name, Path(original))
    plan = list(plan.values())
    if not args.names and not args.batch:
        print("Name skills or give --batch. Use `audit.py archives` to list batches.")
        return 1
    if not plan:
        print("Nothing to restore.")
        return 1
    print(f"{'Restoring' if args.yes else 'Would restore'} {len(plan)} skill(s):")
    for b, name, original in plan:
        clash = " (BLOCKED: something already exists there)" if original.exists() or original.is_symlink() else ""
        print(f"  - {name} -> {original}{clash}")
    for d in dupes:
        print(f"  note: an older copy stays archived ({d})")
    if not args.yes:
        print("\nDry run. Nothing moved. Add --yes to do it.")
        return 0
    restored, blocked = [], []
    for b, name, original in plan:
        if original.exists() or original.is_symlink():
            blocked.append((name, original))
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(b / name), str(original))
        mf_path = b / "manifest.json"
        mf = json.loads(mf_path.read_text())
        mf.pop(name, None)
        if mf:
            mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2))
        else:
            mf_path.unlink()
            try:
                b.rmdir()
            except OSError:
                pass
        restored.append(name)
    if blocked:
        print(f"\nNot restored ({len(blocked)}): something already exists at the original place. "
              "Move or rename it, then run this again:")
        for name, original in blocked:
            print(f"  - {name} -> {original}")
    if not restored:
        print("\nNothing was restored.")
        return 1
    print(f"\nRestored {len(restored)} skill(s). Start a new Claude Code session to see the change.")
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    p = argparse.ArgumentParser(prog="audit.py", description="See which Claude Code skills you actually use.")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("report", help="inventory + usage report")
    r.add_argument("--days", type=int, help="only count the last N days")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    r.add_argument("--out", help="write the report to a file")
    r.add_argument("--html", action="store_true",
                   help="also write a visual HTML report (default <config>/skill-audit/report.html) and open it")
    r.add_argument("--html-out", metavar="FILE", help="with --html: where to write the HTML report")
    r.add_argument("--no-open", action="store_true", help="with --html: do not open the browser")
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("archive", help="move skills to skills-archive/ (dry run unless --yes)")
    a.add_argument("names", nargs="*")
    a.add_argument("--group", action="append", default=[], help="every skill in a group, e.g. gsd")
    a.add_argument("--unused", action="store_true", help="every skill with no recorded use")
    a.add_argument("--days", type=int, help="with --unused: only count the last N days")
    a.add_argument("--project", action="store_true", help="also move skills from ./.claude/skills")
    a.add_argument("--yes", action="store_true", help="actually move")
    a.set_defaults(func=cmd_archive)

    s = sub.add_parser("restore", help="move archived skills back (dry run unless --yes)")
    s.add_argument("names", nargs="*")
    s.add_argument("--batch", help="batch id from `archives`")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_restore)

    g = sub.add_parser("guide", help="list skills that need a plain-language guide entry, or save entries")
    g.add_argument("--all", action="store_true", help="list every skill, not only those missing an entry")
    g.add_argument("--save", metavar="FILE", help='merge {"skill-name": {"what", "when", "try"}} from FILE')
    g.set_defaults(func=cmd_guide)

    l = sub.add_parser("archives", help="list archived batches")
    l.set_defaults(func=cmd_archives)

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["report"] + (argv or sys.argv[1:]))
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
