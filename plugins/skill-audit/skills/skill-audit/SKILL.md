---
name: skill-audit
description: Audit the user's installed Claude Code skills - list every skill, explain in plain words what each one does, when to use it and what to say to trigger it, count how often each was actually used in past sessions, and propose moving unused ones to an archive folder (reversible, never deleted). Use when the user asks which skills they have, what a skill does or how to use it, which ones they use or never use, wants to clean up, declutter or slim down skills, says Claude picks the wrong skill or has too many skills, asks for a skill guide, or types /skill-audit. Also for "스킬 정리", "안 쓰는 스킬", "내 스킬 뭐 있어", "이 스킬 뭐야", "스킬 사용법", "스킬 안내서", "스킬 사용 기록".
---

# Skill audit

Shows what each installed skill does and how to use it, which ones the user actually uses (from the session history Claude Code keeps on this machine), and helps them archive the rest. Many users install skills without knowing what they are for: the guide is as important as the counts. Everything stays local: never send the report, file paths or session content anywhere.

The script is `scripts/audit.py` in this skill's base directory. It needs only Python 3.8+ and takes about 10-20 seconds on a few hundred sessions.

## 1. Write the guide

```bash
python3 "<this skill's base directory>/scripts/audit.py" guide
```

It prints JSON: `to_write` lists the skills that have no plain-language guide entry yet (or whose description changed since). If it is empty, go to step 2. Otherwise write one entry per listed skill, in the user's language, and save them:

- `what`: one sentence saying what the skill does, in everyday words a non-developer understands. No jargon, no file names, no flags. Under about 25 words.
- `when`: the situation in which to reach for it, one short sentence.
- `try`: a sentence the user could type to Claude, word for word, that would trigger it ("이 릴스 캡션 써줘"). Short, concrete, in the user's language.

Base each entry on the skill's description; read its `SKILL.md` only when the description is missing or unclear. Never claim a skill does something its description does not say. For a skill meant for developers, say so plainly ("코드로 영상을 만드는 개발자용 도구예요").

Write all entries to a temporary JSON file shaped `{"skill-name": {"what": "...", "when": "...", "try": "..."}}` and save it:

```bash
python3 ".../scripts/audit.py" guide --save /path/to/entries.json
```

Entries are kept in `<config>/skill-audit/guide.json` and reused, so later runs only ask for new or changed skills. With more than about 40 skills, write and save them in batches. If the user asks to rewrite the guide (another language, simpler words), run `guide --all` and save over it.

## 2. Run the report

```bash
python3 "<this skill's base directory>/scripts/audit.py" report --html
```

This prints the text report for you to read, and also writes a visual page (headline numbers, the skill guide as a card per skill with its command and a copy button, searchable by what the user wants to do, a bar per group, a sortable table of every skill, archived batches with copyable undo commands) to `<config>/skill-audit/report.html` and opens it in the user's browser. The page follows the browser language (Korean or English) and light or dark mode. Tell the user they can bookmark that file; it is a snapshot, refreshed each time `/skill-audit` runs.

Options: `--no-open` writes the page without opening a browser (use it when the user asks for text only, or there is no desktop browser); `--days 30` counts only the last 30 days; `--json` gives machine-readable output; plain `report` without `--html` prints only the text.

## 3. Explain it to the user

The user has the page open, so keep the chat short. Answer in the user's language. If the user only asked what their skills are or how to use one, answer from the guide (what it does, the sentence to try, the command) and point to the page; skip the clean-up. Otherwise lead with the one headline line ("104 skills installed, 16 used"), then:

- the group table, so they see which bundles do nothing
- the most-used skills, which are what they actually rely on
- the description cost: how much of every session's skill list comes from skills they never use
- problems: broken links, folders with no SKILL.md, skills with no description

Rules for how you explain it:

- An unused skill is not a bad skill. Say "you have not used it", never "it is useless".
- Name the limit once: only the history Claude Code still keeps is counted (see `cleanupPeriodDays` in settings), so a skill used once a quarter can show as unused.
- "Used, but not installed in these folders" lists built-in skills, skills from Claude apps, and skills the user removed. These cannot be archived here.
- Do not quote session content back to the user. Names, counts, dates and project folder names are enough.

## 4. Offer a clean-up, and only do it after a yes

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
