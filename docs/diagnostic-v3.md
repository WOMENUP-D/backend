# Diagnostic v3: the profile block, the score, and the courses chosen from them

Version 3 of the development check-in asks who she is and what she wants
before it measures where she stands, and ends with courses picked from the
real catalogue for that goal. This note records how it is built so the next
change starts from the design rather than from the code.

## 1. What was wrong with version 2

- 25 questions, all scored, one `goals` question at the end. Every user saw
  the same list; a schoolgirl and a business owner answered identical
  questions about employment.
- The result said *where* she was weak but nothing about *what for*: no
  interests, no situation, no self-rated level in a field, no open text.
- Recommendations were "the next course in the weak dimension" — a
  catalogue walk, not a match. Courses were never explained.

## 2. The instrument (`data/diagnostic_v3.json`, 41 questions)

Two blocks, one question per screen, in the order they are asked:

| Block | Codes | Shown when | Scored |
|---|---|---|---|
| About her: situation, goals (main + up to 3 more), goal in her own words, interests (up to 4) | `pro.q1`–`pro.q6` | always (`pro.q2`, `pro.q4`, `pro.q5` optional) | no |
| Her situation in detail | `sit.q7`–`sit.q15` | by `pro.q1`: student → studies/practice/first job; employed/freelancer → position, wish, experience; entrepreneur → stage, challenges; seeking/not working → how long | no |
| Self-rated level in a field (0 none … 4 professional) | `skl.q16`–`skl.q20` | by `pro.q6` (and `pro.q3` for English and finance) | no |
| Difficulties (up to 3), formats, hours a week, language | `pro.q21`–`pro.q24` | always | no |
| Development Score: two questions per dimension plus the family routing question | `edu.q1,q2 car.q5,q6 ent.q7,q8 fin.q10,q11 dig.q13,q15 hea.q17,q18 fam.q19–q21 lea.q22,q23` | family questions by `fam.q19` | yes |

Conditions travel with the question (`show_if`: `{question, in}` or
`{any: [...]}`) and are evaluated identically in the browser
(`components/diagnostic/branching.ts`) and on the server
(`services/diagnostic.visible`). An answer to a hidden question is dropped,
never scored. Question types: `single_choice`, `routing`, `goals`,
`multi_choice` (with `max_choices`), `text` (with `max_length`).

## 3. Scoring

Unchanged in kind — each scored option carries 0/25/50/75/100, a dimension
is the mean of its answers, the overall score the mean of dimensions — but
the profile now feeds the priorities:

- `goal_match`: version-3 goals map to the version-2 goal codes
  (`GOAL_LEGACY`) so `GOAL_DIRECT/INDIRECT` keep working.
- `urgency`: her situation (`SITUATION_URGENCY`: job seeker 100, not
  working or graduate 75) replaces the old self-rating for employment.
- The profile itself is stored on the attempt (`assessments.profile`) and
  returned by `/diagnostic/result`.

## 4. The course recommendation (`services/course_match.py`)

Four steps, each inspectable:

1. **Needs.** Every goal, interest, difficulty and business challenge names
   the taxonomy skills it calls for (`GOAL_SKILLS`, `INTEREST_SKILLS`,
   `DIFFICULTY_SKILLS`, `BUSINESS_CHALLENGE_SKILLS`). The main goal's skills
   weigh 3.0 (graded by position: the first two full, then 2.5, then 2.0),
   secondary goals, interests and difficulties 2.0, a weak Development Score
   dimension (< 50) adds its foundations (`DIMENSION_FOUNDATIONS`) at 1.0.
   For a goal whose direction *is* the field she is drawn to
   (`DIRECTION_GOALS`: change direction, new profession, improve skills,
   discover, academic) her interests carry the goal's weight.
2. **Gaps.** Skills on record (`skill_service.skill_keys`) and any field she
   rated at work level or above (`FIELD_SKILLS`, level ≥ 3) are subtracted.
3. **Candidates.** Published courses tagged with any spelling of a gap skill
   (slug, three names, aliases — the taxonomy's `SkillIndex`), fit for her
   age and region, not finished. Ranked by the strongest need they close,
   then by score: coverage (two strongest needs in full, the rest by half) ×
   precision (discounted by resolved tags outside her needs, holdings and
   fields), + language (her language 2, neighbour 1), level fit (±), length
   against her hours, +1 for our own courses, +1.5 for one she has started.
   Candidates are spread so no skill takes more than four of the 24 places.
4. **Words.** The rules write an explanation at once (why: the need it
   closes; outcome: the course's own description) and a route (foundation →
   broadening → practice, with CV/apply/business-plan/mentor activities
   where they fit). When the model is on, the attempt is marked `ai_pending`
   and the worker asks the model (`explain`) for its words on the top six
   candidates: it may refer to a course only by candidate number, must say
   what a course gives only from its description, may ask one follow-up
   question, and may put non-course activities on the route where the
   catalogue has nothing. Its answer is checked (`parse_model`): unknown
   numbers, repeats and empty texts are dropped; if nothing usable remains
   the rules' words stay. Two failed tries end the wait.

Stored on the attempt (`assessments.recommendation`) with `mode`
(`ai` / `rules` / `insufficient`), the trace (model, prompt version,
trace id) and an `AiInteraction` row (`feature = diagnostic_courses`).

Endpoints: `GET /diagnostic/recommendations[?attempt_id]` builds once and
serves from the attempt; `POST /diagnostic/recommendations/refresh` rebuilds
(counted as an expensive request). A course unpublished since is left out of
the response rather than named.

## 5. Course metadata the matcher reads

`skills_taught` (tags, resolved through the taxonomy), `language`, `level`,
`duration_hours`, `external_url` (ours or a partner's), `target_age_*`,
`target_regions`, `description_i18n`, `learning_outcomes`. Of the 845
partner courses only 26 carry a level or hours, and tags average 1.3 per
course — the ranking degrades gracefully (unknown level and length are
neutral) but tag quality is the ceiling of the rules' precision; the model's
reading of the description is what lifts it.

## 6. Personas (rules and model)

Seven profiles — student in IT wanting a first job, employee aiming for
promotion, beginner entrepreneur, career change into marketing, returning
to work after a break, professional going international, undecided graduate
— were run against the real catalogue. The rules alone give a sensible,
diverse set in every case (no empty list; the undecided graduate gets
foundations and a follow-up question). With the model (Opus 5, effort low,
≈45 s a call) each persona gets three or four courses with reasons drawn
from the descriptions, a staged route with non-course steps, and a specific
follow-up question; it declines courses that are off-level or off-field and
says so when the catalogue has nothing for a need.

## 7. Known limits

- Tag noise in the partner catalogue (e.g. "job search" on unrelated
  courses) reaches the candidate list; the model filters it, the rules
  only discount it.
- The model answers in all three languages at once, which is what makes a
  call take ≈45 s; hence the worker path and `ai_pending`.
- No course for a need is reported, not papered over: IELTS, CV writing and
  communication have few or no catalogue courses today.
