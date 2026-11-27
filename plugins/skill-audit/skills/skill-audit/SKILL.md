---
name: skill-audit
description: Audit the user's installed Claude Code skills - list every skill, count how often each one was actually used in past sessions, show which projects used it, and propose moving unused ones to an archive folder (reversible, never deleted). Use when the user asks which skills they have, which ones they use or never use, wants to clean up, declutter or slim down skills, says Claude picks the wrong skill or has too many skills, or types /skill-audit. Also for "스킬 정리", "안 쓰는 스킬", "내 스킬 뭐 있어", "스킬 사용 기록".
---

# Skill audit

Shows which installed skills the user actually uses, from the session history Claude Code keeps on this machine, and helps them archive the rest. Everything stays local: never send the report, file paths or session content anywhere.

The script is `scripts/audit.py` in this skill's base directory. It needs only Python 3.8+ and takes about 10-20 seconds on a few hundred sessions.

## 1. Run the report

```bash
python3 "<this skill's base directory>/scripts/audit.py" report
```

Options: `--days 30` counts only the last 30 days; `--json` gives machine-readable output; `--out report.md` saves the report to a file.

## 2. Explain it to the user

Answer in the user's language. Lead with the one headline line ("104 skills installed, 16 used"), then:

- the group table, so they see which bundles do nothing
- the most-used skills, which are what they actually rely on
- the description cost: how much of every session's skill list comes from skills they never use
- problems: broken links, folders with no SKILL.md, skills with no description

Rules for how you explain it:

- An unused skill is not a bad skill. Say "you have not used it", never "it is useless".
- Name the limit once: only the history Claude Code still keeps is counted (see `cleanupPeriodDays` in settings), so a skill used once a quarter can show as unused.
- "Used, but not installed in these folders" lists built-in skills, skills from Claude apps, and skills the user removed. These cannot be archived here.
- Do not quote session content back to the user. Names, counts, dates and project folder names are enough.

## 3. Offer a clean-up, and only do it after a yes

1. Propose a concrete set, normally from "Suggested clean-up": whole groups that were never used first, then single unused skills.
2. Show the dry run, which moves nothing:
   ```bash
   python3 ".../scripts/audit.py" archive --group gsd
   python3 ".../scripts/audit.py" archive ig-dm yt-seo
   python3 ".../scripts/audit.py" archive --unused
   ```
3. Ask the user to confirm the exact list. Only after a clear yes, run the same command with `--yes`.
4. Tell them the batch id and the undo command the script prints, and that a new Claude Code session is needed to see the change.

Never delete skill folders, and never run `--yes` without the user approving that exact list. Skills that come from a plugin cannot be archived: tell the user to remove them with `/plugin`. Project skills (in `./.claude/skills`) are left alone unless the user names them or agrees to `--project`, because they belong to a repository other people may share.

## Undo

```bash
python3 ".../scripts/audit.py" archives                   # list archived batches
python3 ".../scripts/audit.py" restore --batch <id> --yes  # put a whole batch back
python3 ".../scripts/audit.py" restore ig-dm --yes          # put one skill back
```

Archived skills sit in `<config>/skills-archive/<batch>/`, where `<config>` is `$CLAUDE_CONFIG_DIR` or `~/.claude`.
