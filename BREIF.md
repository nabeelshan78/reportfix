# CODE QUEST 2026 | AI MODULE: ReportFix (organizers' brief)

Duration: 2.5 hours | Team size: 3 | Stack: your choice | AI tools: allowed

## The Situation

A projector dies in a CSE lab. Three WhatsApp groups hear about it. Nobody fixes it. The library AC gets marked "resolved" while students are still sweating under it. Departments can't tell what is urgent, and nobody knows whose complaint to believe.

The Registrar's office has had enough. You have 2.5 hours to build ReportFix, a platform where students report campus problems, AI routes each one to the right department, departments act on it, and students confirm whether the fix actually worked.

## What You Build

One working app that runs on your laptop, with three kinds of users:

- **Student:** reports problems, tracks the live status of every issue they reported or joined, and votes on whether a fix is real.
- **Department Staff:** sees only their own department's queue and updates issues.
- **Admin:** oversees everything.

Departments: EE, CSE, IS, Library, Cafeteria, Labs.

Reports come in English, Roman Urdu, or a mix of both, with typos. Your design, stack, and use of AI are entirely yours.

## Core Requirements

1. **Understand and route.** From plain text, work out the department, location, priority, and a one-line reason.
2. **Group by meaning.** The same problem at the same place is one issue with a "me too" count, however it is worded in whatever language. Different problems at the same place (Lab 3 WiFi, Lab 3 AC) remain separate issues.
3. **Impact over tone.** Safety hazards (fire, sparks, gas, medical) are always top priority, even when described calmly. SHOUTING and the word "urgent" change nothing.
4. **Reports are data, not commands.** If a report tells your system to ignore its rules or mark itself critical, your system must not obey.
5. **Protect the vulnerable.** Harassment and ragging reports stay confidential. They appear on no public board, reach no other department, and never reveal the reporter.
6. **No guessing.** If a report names a place that does not exist on campus, ask for clarification instead of routing it.

## Community Layer (scored on depth)

7. **"Resolved" is not the end.** Once staff marks an issue resolved, the affected students confirm or dispute it. Enough credible disputes reopen the issue. Only affected students may vote.
8. **Not every voice weighs the same.** Every student has a trust score. It rises when their reports and votes prove right, and falls for spam, fake reports, or abuse. Trust affects how much a report or vote counts, and you must be able to show why anyone has their score.
9. **Crowds matter.** When many credible students report or join the same issue, it becomes more important. A single high-trust student cannot push a minor complaint to the top on status alone.

## Bonus (extra points only)

Trending board | AI-written admin summary | Spam and brigading detection (for example, many brand-new accounts voting together)

## Judge Access

Your app must let judges do three things quickly:

- Switch to any user in one click, with no passwords.
- Create a brand-new student account instantly.
- Reset all data.

Anything the judges cannot reach, they cannot score.

## Scoring (100 points)

| Category | Points |
|---|---|
| Live Attack Test: judges run fresh scenarios on your app | 40 |
| Core Flow: report → triage → department → resolve → status | 20 |
| Community Layer: voting, trust, grouping, crowd priority | 20 |
| Approach: how you split work between AI and regular code | 10 |
| Pitch: 2 minutes, every member speaks | 10 |
| Bonus | up to +10 |

Visual design is not scored. A plain app that behaves correctly beats a beautiful one that does not.

## Advice from the Organizers

- You will not finish everything. Choose depth over breadth. Three rules done properly beat eight done badly.
- AI is good at understanding text. Code is better at counting, rules, and permissions.
- Build the Core first, then the Community layer.
- Judges will test your app with sentences you have never seen. Try to break it yourselves first.
- Be straight about what works and what doesn't. Honesty earns points.