# skill-audit

**설치한 스킬 중 몇 개를 실제로 쓰고 있나요? 명령 한 줄로 확인하세요.**

저는 Claude Code에 스킬을 104개 깔았고, 두 달 동안 실제로 쓴 건 16개였습니다. 67개짜리 묶음 하나는 한 번도 호출되지 않았습니다. 그런데도 그 스킬들의 설명문은 매 대화마다 Claude에게 읽히고 있었습니다.

skill-audit는 내 컴퓨터에 남은 Claude Code 대화 기록을 읽어서 다음을 보여줍니다.

- 설치된 모든 스킬 (개인 폴더, 프로젝트 폴더, 플러그인)
- 스킬별 사용 횟수, 마지막 사용일, 사용한 프로젝트
- 한 번도 안 쓴 스킬 묶음과 그 스킬들이 차지하는 비용
- 깨진 링크, 설명 없는 스킬 같은 문제
- 정리 제안: 안 쓰는 스킬을 보관 폴더로 옮기기 (삭제 아님, 언제든 되돌리기 가능)

모든 분석은 내 컴퓨터 안에서만 합니다. 아무것도 외부로 보내지 않습니다.

## 설치

Claude Code에서:

```
/plugin marketplace add findsoul84/skill-audit
/plugin install skill-audit@skill-audit
```

플러그인 없이 쓰려면 `plugins/skill-audit/skills/skill-audit` 폴더를 `~/.claude/skills/`에 복사하세요.

## 사용

Claude에게 "안 쓰는 스킬 정리해줘"라고 하거나 `/skill-audit`을 입력하세요. Claude가 리포트를 보여주고, 무엇을 보관할지 묻고, 승인한 것만 옮깁니다.

스크립트를 직접 돌려도 됩니다:

```bash
python3 audit.py report                 # 리포트
python3 audit.py report --days 30       # 최근 30일만
python3 audit.py archive --group gsd    # 미리보기 (아무것도 안 옮김)
python3 audit.py archive --group gsd --yes
python3 audit.py archives               # 보관 기록
python3 audit.py restore --batch <id> --yes
```

## 리포트 예시

```
**104 skills installed. 16 used (15%). 31 calls in total.**

| Group | Installed | Used | Calls | Never used |
| gsd   | 67        | 0    | 0     | all        |
| ig    | 13        | 5    | 8     | ig-audit, ig-carousel, ... |
| yt    | 11        | 3    | 3     | yt-audit, yt-chapters, ... |

- Skill descriptions Claude reads at the start of every session:
  19,036 characters, 11,273 of them (59%) from skills never used.
```

## 한계

- Claude Code가 보관 중인 대화 기록만 셉니다 (`cleanupPeriodDays` 설정, 기본 30일). 몇 달에 한 번 쓰는 스킬은 "안 씀"으로 보일 수 있습니다.
- 안 쓴 스킬이 나쁜 스킬이라는 뜻은 아닙니다. 당신이 안 썼다는 뜻입니다.
- 플러그인으로 설치한 스킬은 옮기지 않습니다. `/plugin`으로 지우세요.

---

## English

**How many of your installed skills do you actually use? Find out with one command.**

I had 104 skills installed in Claude Code. In two months I used 16. One bundle of 67 was never called once, yet its descriptions were loaded into every session.

skill-audit reads the session history Claude Code keeps on your machine and shows every installed skill, how often and where each was used, which bundles were never used and what they cost, and broken links or missing descriptions. It can move unused skills to an archive folder: nothing is deleted, and `restore` puts them back. Everything runs locally and nothing is sent anywhere.

Install with `/plugin marketplace add findsoul84/skill-audit` then `/plugin install skill-audit@skill-audit`, and ask Claude "which skills do I never use?" or type `/skill-audit`.

Requires Python 3.8+ with no extra packages. MIT licensed.
