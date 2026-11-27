#!/usr/bin/env python3
"""skill-audit: see which installed Claude Code skills you actually use.

Everything runs locally. Nothing is sent anywhere.

  audit.py report  [--days N] [--json] [--out FILE]
  audit.py archive [NAME ...] [--group G ...] [--unused] [--days N] [--yes]
  audit.py restore [NAME ...] [--batch ID] [--yes]
  audit.py archives

`archive` and `restore` only print a plan unless --yes is given.
Archiving moves skill folders to <config>/skills-archive/<batch>/; it never deletes.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
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
    for rec in records:
        aliases = {rec["name"]}
        if rec.get("fm_name"):
            aliases.add(rec["fm_name"])
        if rec["plugin"]:
            aliases |= {f"{rec['plugin']}:{a}" for a in list(aliases)}
        calls, last, projects = 0, None, set()
        for key, evs in events.items():
            tail = key.split(":", 1)[1] if ":" in key else None
            if key in aliases or (tail and tail in aliases and (not rec["plugin"] or key.startswith(rec["plugin"] + ":"))):
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


def report_md(cfg, records, meta, unmatched, days):
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

    problems = [r for r in records if r["problems"]]
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
    broken = [r["name"] for r in records if r["broken"]]
    if broken:
        sugg.append(f"- Broken links, safe to archive: {', '.join(broken)}")
    w("\n".join(sugg) if sugg else "Nothing to clean up.")
    w("")
    w("Archiving moves folders to `skills-archive/` and can be undone with `audit.py restore`. "
      "Only history that Claude Code still keeps is counted: a skill used rarely may show as unused.")
    return "\n".join(out)


def cmd_report(args):
    cfg, records, meta, unmatched = build(args.days)
    if args.json:
        text = json.dumps({"config": str(cfg), "meta": meta, "skills": records, "unmatched": unmatched},
                          ensure_ascii=False, indent=2)
    else:
        text = report_md(cfg, records, meta, unmatched, args.days)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Saved report to {args.out}")
    else:
        print(text)


# ---------------------------------------------------------------- archive / restore

def archive_root(cfg):
    return cfg / "skills-archive"


def cmd_archive(args):
    cfg, records, _meta, _ = build(args.days)
    by_name = {r["name"]: r for r in records}
    targets, notes = [], []
    for n in args.names:
        r = by_name.get(n)
        if not r:
            notes.append(f"not found: {n}")
        elif not r["movable"]:
            notes.append(f"{n} comes from plugin {r['plugin']}: use /plugin to remove it")
        else:
            targets.append(r)
    for g in args.group:
        targets += [r for r in records if r["group"] == g and r["movable"]]
    if args.unused:
        targets += [r for r in records if not r["calls"] and r["movable"]]
    # Project skills belong to a repository; move them only when asked for by name or with --project.
    if not args.project:
        named = set(args.names)
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
        dst = dest_root / src.name
        shutil.move(str(src), str(dst))
        manifest[src.name] = str(src)
    (dest_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nDone. Batch id: {batch}. Undo with: audit.py restore --batch {batch} --yes")
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
    plan = []
    for b, mf in batches:
        for name, original in mf.items():
            if args.names and name not in args.names:
                continue
            if (b / name).exists() or (b / name).is_symlink():
                plan.append((b, name, Path(original)))
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
    if not args.yes:
        print("\nDry run. Nothing moved. Add --yes to do it.")
        return 0
    for b, name, original in plan:
        if original.exists() or original.is_symlink():
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
    print("Done. Start a new Claude Code session to see the change.")
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    p = argparse.ArgumentParser(prog="audit.py", description="See which Claude Code skills you actually use.")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("report", help="inventory + usage report")
    r.add_argument("--days", type=int, help="only count the last N days")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    r.add_argument("--out", help="write the report to a file")
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

    l = sub.add_parser("archives", help="list archived batches")
    l.set_defaults(func=cmd_archives)

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["report"] + (argv or sys.argv[1:]))
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
