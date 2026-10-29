---
version: 0.2
principle: llm_proposes_code_decides
status: active
---

# memory_promotion_rules.en.md

## 1. General Principles

- The LLM extracts candidate memories; it does not directly own long-term memory writes.
- Each time user mail is processed, classify the information first, then decide where it belongs.
- Default to remembering less, not more; missing one item is better than turning a temporary emotion into a long-term fact.
- A memory can enter long-term storage only if it clearly affects how letters should be written for more than the next 30 days.

## 2. Memory Types

1. `user_profile`: stable user facts, stable preferences, long-term goals, and long-term stressors
2. `relationship_memory`: names, agreements, shared jokes, shared memories, and unfinished conversation threads
3. `user_episode`: events, moods, temporary frustrations, or short-term issues from today or the past few days
4. `character_episode`: the character's recent tasks and short-term state
5. `active_arc`: a continuous thread that has appeared repeatedly recently and is likely to continue
6. `discard`: information that is not worth storing or is useful only in the moment

## 3. Candidate Memory Output Format

After receiving user mail, the LLM must output candidate memories first instead of writing files directly.

Fields:

- `content`: memory content
- `type`: one of the six types above
- `evidence`: quoted user wording or a summary
- `confidence`: 0.0 ~ 1.0
- `durability`: `transient` / `short` / `stable`
- `why_it_matters`: why it may be useful later
- `source_date`: YYYY-MM-DD

## 4. Promotion Rules

### 4.1 Write to `user_profile.json`

Only when at least one of the following is true:

- The user explicitly says this is a long-term fact, stable preference, or long-term goal.
- Similar information appears independently on at least 2 different dates.
- It clearly affects future letter style or advice.
- The user explicitly asks the system to remember it.
- Current implementation: the code accepts only two kinds of evidence: an explicit request ("remember", "don't forget", "call me …") in a sentence of the user's own words that quotes this content ("don't remember" and "I remember when …" do not count), or the same content mentioned on at least 2 distinct mail dates (by the Date header of the letter the evidence was found in). The `evidence` must be found in the mail body (contained as-is, or near-verbatim within one sentence with the same negation state; quotes shorter than 4 CJK characters or 2 words never count), otherwise nothing is written; transient candidates are not written; unless the user explicitly asked, content phrased about "today" or "this week" is not written; neither is a candidate with confidence < 0.85 or a durability other than stable.

### 4.2 Write to `relationship.md`

Only record:

- Names and relationship boundaries
- Important agreements
- Shared anniversaries and shared jokes
- Repeated shared topics that affect the tone of communication
- Nodes that define what the relationship is
- Current implementation: the same evidence requirement as 4.1; an explicit request (such as "call me …") is written from that letter, anything else needs 2 distinct mail dates.

### 4.3 Write to `active_arc.json`

Keep only 1-2 entries, and each must satisfy:

- It has appeared repeatedly within the past 7 days.
- It is still unresolved.
- It is very likely to be mentioned again.

Examples:

- The user has recently been under continuous academic pressure.
- The character is handling an ongoing matter.
- Current implementation: a user-side candidate needs its `evidence` found in the mail and the same content on at least 2 distinct mail dates within 7 days, or an existing matching arc.

### 4.4 Write to `episode`

- Same-day events, short-term emotions, one-off complaints, and temporary arrangements all go into `episode` first.
- `episode` may be somewhat detailed, but it should be downweighted, archived, or deleted after 14-30 days.
- Current implementation: only the last 7 days of `episode` files are read into the prompts; there is no automatic downweighting, archiving, or deletion.

## 5. Content Explicitly Forbidden From Long-Term Promotion

- One-off negative emotions, such as "I am so tired today" or "I am so annoyed today"
- Angry words or self-negating labels, such as "I just cannot do anything"
- Unconfirmed speculation
- Assumptions about other people's motives
- Personal facts invented by the model that the user never stated
- Temporary judgments made only for comfort
- Noisy details unrelated to long-term interaction

## 6. Conflict Handling

- When new information conflicts with old long-term memory, do not overwrite directly.
- First write a `conflict_note` that records the old value, new value, source date, and confidence.
- Overwrite the old value only when one of the following is true:
  - The user explicitly corrects it.
  - The new information appears repeatedly on at least 2 different dates.
  - The new information is clearly more specific, and the old value has not been reconfirmed for a long time.
- Current implementation: two override paths are automated: an explicit correction in the user's own words ("I misspoke", "correction", "not … but …"; "actually" only together with a negation or a reference to an earlier letter) in a sentence that quotes the new content, when the new content itself passes the gates, replaces the conflicting memory at once; otherwise the same `conflict_note` must accumulate 2 distinct mail dates. The replaced entry is kept and marked `status: superseded`, and the conflict note is cleared. The "more specific" path is not automated. Conflict detection relies on a negation map (like/dislike, is/is not, has/has no, hope/do not hope, can/cannot, no longer, never, and the English not / never / no longer / doesn't forms, with likes/like word forms folded); transient or low-confidence candidates can never trigger an override.

## 7. Quantity Limits

### Per User Email

- Add at most 0-2 `user_profile` entries.
- Add at most 0-1 `relationship_memory` entry.
- Add at most 0-1 `active_arc` entry.
- Everything else goes to `episode` or `discard`.
- Current implementation: these per-letter limits are enforced by code, keeping the highest-confidence candidates.

### Per Character Email

- By default, write only `episode`.
- Do not automatically promote what the character said into long-term facts.
- Content may be upgraded to `active_arc` only if it repeats for 2-3 consecutive days and remains internally consistent.

## 8. Special Constraints for Character-Side Content

- What the character handled today is only a short-term episode by default.
- New details improvised by the model must not pollute `persona.md`, `world_state.md`, or `cast_relations.md`.
- If a character-added setting conflicts with an old file, it must first enter `conflict_note`; it must not directly overwrite the old file.

## 9. Deletion and Downweighting

- Resolved `active_arc` entries should be archived, not kept permanently.
- Short-term preferences not mentioned again for 30 days should be downweighted.
- Long-term memories explicitly negated by the user should immediately be marked as `superseded`.
- Deduplicate weekly: merge semantically similar memories and keep the one that is more specific, newer, and better supported by evidence.
- Current implementation: only the first rule is automated. When the reply stage suggests `archive`, the matching `active_arc` entry is deleted from the file (nothing is archived elsewhere). 30-day downweighting, the `superseded` marker, and weekly deduplication are not automated. Write-time deduplication only merges entries that are identical after normalization, or where one contains the other and the contained one is at least 8 characters long.

## 10. Final Judgment Question

When the model wants to write long-term memory, ask first:

> Could this information still affect how the character writes to the user 30 days from now?

- If not, it probably should not enter long-term memory.
- If it is useful only today, put it in `episode`.
- If it defines who the user is, what the relationship is, or what has been happening repeatedly, then consider promotion.
