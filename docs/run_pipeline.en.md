---
version: 0.2
status: active
pipeline_mode: profile_first
---

# run_pipeline.en.md

## 1. Goal

Each system run is based only on the current active profile:

- Character prompts come from `profiles/characters/<character_slug>/prompt/`.
- Relationship prompts come from `profiles/characters/<character_slug>/relationships/<relationship_slug>/prompt/`.
- Runtime memory and archives come from `profiles/characters/<character_slug>/sessions/<relationship_slug>/`.

Core principles:

1. Static prompt documents are read-only.
2. The LLM only proposes candidate memories; code decides what to write.
3. Image failure interrupts the current run and no email is sent (or degrades it to a text-only email when `llm.image.on_failure=text_only`); user-side memory already written by Stage 1 is kept, and the character-side episode is not written.
4. All inputs and outputs must be archived and traceable.

## 2. Document Sources

### Character Prompt

- `persona.md`
- `world_state.md`
- `cast_relations.md`
- `cast_pool.md`
- `translation_layer.md`

### Relationship Prompt

- `relationship.md`

### Global Rules

- `docs/memory_promotion_rules.en.md` when `llm.language` is `English`
- `docs/memory_promotion_rules.md` otherwise

### Session Memory

- `memory/user_profile.json`
- `memory/relationship_memory.json`
- `memory/active_arc.json`
- `memory/conflict_notes.json`
- `episodes/user/*.json`
- `episodes/character/*.json`

## 3. Pipeline

```text
scheduler / run_once
  -> load active profile
  -> fetch unprocessed inbound mail
  -> normalize and archive
  -> Stage 1: inbound understanding + candidate memories
  -> code writes session memory according to memory_promotion_rules
  -> Stage 2: character reply + character_episode, plus scene_card when image generation is enabled
  -> image generation
  -> SMTP send
  -> write archive / state.db / episodes
```

## 4. Stage 1 Input

- `docs/memory_promotion_rules.en.md` when `llm.language` is `English`
- Current `relationship.md`
- Current session memory
- Recent `episodes/user`
- Recent `episodes/character`
- This batch of inbound messages

## 5. Stage 2 Input

- Current character prompt
- Current `relationship.md`
- Current session memory
- Recent `episodes/user`
- Recent `episodes/character`
- `stage1_result`

## 6. Output Constraints

Stage 2 must output:

- `subject`
- `body`
- `character_episode`
- `active_arc_update_suggestion`
- `scene_card`, only when `llm.image.enabled=true`; it may be omitted when image generation is disabled

`character_episode` only represents short-term continuity left by this character in the current communication. It must not rewrite static character prompts.

## 7. Failure Handling

- A failed run records `error_stage` / `error_message` in the `runs` table, increments `consecutive_failures` in the kv store and updates `last_failed_at`; any successful run resets the counter.
- When a reply-mode run fails in the stage1 / stage2 / validation / image stage, every inbound message of the batch gets `attempt_count` +1 plus `last_error` / `last_attempt_at`; reaching `schedule.max_batch_attempts` (`min(max_batch_attempts, 2)` for validation failures) sets `quarantined_at`, after which the message is excluded from batches and runs fall back to initiative mode. `requeue-inbound --uid N` clears the quarantine and the counter.
- A retry of the same `batch_id` reuses the artifacts archived by the latest failed run: with `stage2.json` both Stage 1 and Stage 2 are skipped; with only `stage1.json` just Stage 2 runs again and the user-side episode is not written twice.
- Scheduler: back off for `schedule.retry_after_minutes` after a failure, at most `schedule.max_runs_per_day` scheduled runs per day, and only one attempt at catching up yesterday's missed send; at the start of every tick, runs still `running` for more than 6 hours are marked `failed`.
- When consecutive failures reach `mail.alerts.after_failures`, or inbound mail is quarantined, a plain-text system alert whose subject starts with `[dropline] ` is sent to `mail.alerts.recipient` (at most one per day).
