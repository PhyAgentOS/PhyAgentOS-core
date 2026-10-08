# English README images

Localized with the built-in image generation tool. Chinese originals remain unchanged.

## control-modes

Translate this diagram into English only, preserve layout, colors, three cards, arrows and all model names. Heading: 'One Framework, Three Control Modes'. Subtitle: 'General models, action models, and hybrid control.' Left small heading 'General models · GPT-6 / DeepSeek / GLM'; middle 'Hybrid control · GPT-6 + π0.5 / DeepSeek + π0.5'; right 'Action models · π0.5 / π0 / OpenVLA'. Keep System 1, System 1 + System 2, System 2, LLM or VLM, VLA or WAM, PhyAgentOS, Action verbatim. Bottom descriptions: left 'General models generate actions or compose tools to complete tasks.' middle 'General models handle reasoning; action models handle low-level actions.' right 'Action models directly predict robot actions.' Ensure no Chinese remains. Crisp legible typography, no other changes.

## Benchmark translations — 2026-10-07

Inputs: `benchmark-libero-long-original.png`, `benchmark-robodojo-original.png`, and `benchmark-robotwin-original.png`. Outputs: corresponding `-en.png` files. Generated with the built-in image generation tool; the Chinese PNG originals are copied without modification. Exact data: [benchmark tables](../benchmarks.md).

### Shared prompt

Use case: text-localization. Translate this attached benchmark chart into English for a GitHub README. Keep every model label, numeric value, percent, time, token count, row ordering, bar length and color category EXACTLY unchanged. Preserve the light background, purple chart style and all three bottom tabs. Translate 评测结果 to Evaluation results; 任务成功率 (%) to Task success rate (%); 基于PhyAgentOS实现 to PhyAgentOS results; 数据源于公开榜单 to Public leaderboard; 平均耗时 to Avg. time; token 消耗 to Tokens. Footer: Units: success rate (%); time (seconds/task); tokens (tokens/task). M = million tokens. Preserve missing token value as — and approximate ~0.3M as approximate. Keep LIBERO-Long title and tab text unchanged, not LIBERO-10. No extra facts, no added rows, no removed rows. All text should be English except mathematical π symbol. High resolution clear typography.

### LIBERO correction prompt

Use case: text-localization correction. Change exactly ONE character/value in this English LIBERO-Long benchmark image. Under the π0.5 row (the 5th row, success 93.30%), the subtitle currently says 'Avg. time 84 · Tokens —'. Replace it with exactly 'Avg. time 84 · Tokens 0'. Zero is the supplied value, not missing. Keep every other text, number, model label, bar, color, layout and all tabs exactly unchanged. Do not change any other row. Output full image.
