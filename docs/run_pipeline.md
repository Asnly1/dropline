---
version: 0.2
status: active
pipeline_mode: profile_first
---

# run_pipeline.md

## 1. 目标

系统每次运行都只基于当前 active profile：

- 角色 prompt 来自 `profiles/characters/<character_slug>/prompt/`
- 关系 prompt 来自 `profiles/characters/<character_slug>/relationships/<relationship_slug>/prompt/`
- 运行记忆与归档来自 `profiles/characters/<character_slug>/sessions/<relationship_slug>/`

核心原则：

1. 静态 prompt 文档只读
2. LLM 只提候选记忆，代码决定写入
3. 图片失败会中断本次 run 且不发信；Stage 1 已写入的用户侧记忆保留，角色侧 episode 不写入
4. 所有输入输出必须可归档、可追溯

## 2. 文档来源

### 角色 prompt

- `persona.md`
- `world_state.md`
- `cast_relations.md`
- `cast_pool.md`
- `translation_layer.md`

### 关系 prompt

- `relationship.md`

### 全局规则

- `docs/memory_promotion_rules.md`

### Session memory

- `memory/user_profile.json`
- `memory/relationship_memory.json`
- `memory/active_arc.json`
- `memory/conflict_notes.json`
- `episodes/user/*.json`
- `episodes/character/*.json`

## 3. Pipeline

```text
scheduler / run_once
  -> 加载 active profile
  -> 拉取未处理入站邮件
  -> 标准化并归档
  -> Stage 1：来信理解 + candidate memories
  -> 代码按 memory_promotion_rules 写入 session memory
  -> Stage 2：角色化回信 + character_episode（图片开启时额外产出 scene_card）
  -> 图片生成
  -> SMTP 发信
  -> 写 archive / state.db / episodes
```

## 4. Stage 1 输入

- `docs/memory_promotion_rules.md`
- 当前 `relationship.md`
- 当前 session memory
- 最近几天的 `episodes/user`
- 最近几天的 `episodes/character`
- 本批 inbound messages

## 5. Stage 2 输入

- 当前角色 prompt
- 当前 `relationship.md`
- 当前 session memory
- 最近几天的 `episodes/user`
- 最近几天的 `episodes/character`
- `stage1_result`

## 6. 输出约束

Stage 2 必须输出：

- `subject`
- `body`
- `character_episode`
- `active_arc_update_suggestion`
- `scene_card`（仅 `llm.image.enabled=true` 时要求；关闭图片时可省略）

`character_episode` 只代表当前角色本次通信留下的短期连续性，不反向改写静态角色 prompt。
