# Studio

On-demand agents for one-off work: analyses, mockups, proposals, offers, audits.
Lives in the `/studio` volume on the server, **not in git**, and separate from the
build team's project.

```
/studio/
  agents/      one .md file per studio agent (add your own: same format)
  brand/       company.md (your details, rates, VAT, terms) + logo
  jobs/        one folder per job: input/ research/ mockups/ output/
```

## Telegram
- `/agents`: list studio agents
- `/run <agent> <brief>`: start a job, for example `/run project-proposal https://exemplu.ro focus on mobile`
- While a job is open, your messages and files go to it. `/pm` switches back to the PM.
- `/job`: the current job · `/job files`: its outputs · `/job send <file>` · `/job end`
- `/jobs`: recent jobs · `/job open <id>`: reopen an old job

## Adding an agent
Create `/studio/agents/<name>.md`:
```
---
name: <name>
description: <when to use it - one or two lines>
---
<instructions>
```
Or just ask in any job: "create a studio agent that …". It appears in `/agents` immediately.
Agents run one at a time; a job's lead can call other studio agents as helpers.
