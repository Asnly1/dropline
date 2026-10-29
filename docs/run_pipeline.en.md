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
3. Image failure interrupts the current run and no email is sent; user-side memory already written by Stage 1 is kept, and the character-side episode is not written.
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
