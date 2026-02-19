# Qwen3-235B-A22B-Instruct-2507 Prompting Guide

Model: `qwen-3-235b-a22b-instruct-2507` (MoE, 235B total / 22B active, non-thinking variant)
Provider: OpenRouter (DeepInfra backend, fp8 quantization)
Context: 262K native
Last updated: 2026-02-20

---

## Table of Contents

1. [Model Characteristics](#1-model-characteristics)
2. [Sampling Parameters](#2-sampling-parameters)
3. [System Prompt Structure](#3-system-prompt-structure)
4. [Instruction Priority and Emphasis](#4-instruction-priority-and-emphasis)
5. [Tool Use and Function Calling](#5-tool-use-and-function-calling)
6. [Structured Output (Pydantic)](#6-structured-output-pydantic)
7. [Multi-Turn and Agentic Loops](#7-multi-turn-and-agentic-loops)
8. [Known Failure Modes](#8-known-failure-modes)
9. [Differences from GPT-4 / Claude](#9-differences-from-gpt-4--claude)
10. [Checklist](#10-checklist)

---

## 1. Model Characteristics

- **Non-thinking variant**: Does NOT generate `<think></think>` blocks. The `reasoning_effort` config has no effect on this model. For thinking tokens, use `qwen/qwen3-235b-a22b` or `qwen/qwen3-235b-a22b-thinking-2507`.
- **No default system prompt**: Unlike Qwen2.5, Qwen3 was trained without a built-in system prompt. This means the model was not heavily fine-tuned for system prompt compliance.
- **IFEval score**: 88.7 (vs 83.2 for the base thinking model). Instruction following is better in non-thinking mode.
- **BFCL-v3** (Berkeley Function Calling): 70.9 — decent but not top-tier for tool calling.
- **ChatML template**: Uses `<|im_start|>role` / `<|im_end|>` delimiters natively. When using the OpenAI-compatible API via OpenRouter, this is applied automatically.

---

## 2. Sampling Parameters

Official Qwen3 recommendations for the non-thinking instruct variant:

| Parameter | Recommended | Notes |
|-----------|-------------|-------|
| `temperature` | **0.7** | NEVER use 0 (greedy). Causes performance degradation and infinite loops. |
| `top_p` | **0.8** | |
| `top_k` | **20** | |
| `min_p` | **0** | |
| `presence_penalty` | 0–1.5 | Higher values reduce repetition but may cause language mixing. |
| `frequency_penalty` | **0** | Not recommended by Qwen. May interfere with repeating element IDs from prompt. |
| `repetition_penalty` | 1.0–1.05 | Use if repetition loops persist. |
| `max_tokens` | 2048+ | 2048 is fine for tool-calling; 16384 for general instruct. |

### Critical Warning

> "DO NOT use greedy decoding, as it can lead to performance degradation and endless repetitions." — Qwen official docs

---

## 3. System Prompt Structure

### System prompt adherence is weak

This is Qwen3's most documented limitation. The model was trained without a default system prompt. Community reports consistently show system-level instructions being ignored or partially followed, especially when:
- The system prompt exceeds ~4,000 tokens
- Instructions compete with information in the user message
- Multiple rules exist and the model must prioritize

### Optimal format: XML tags + markdown headers

Qwen3 uses XML natively (`<tools>`, `<tool_call>`, `<think>`), so XML tags are well-understood:

```xml
# CRITICAL RULES
1. If the needed value is already visible, enter and submit it immediately.
2. Never output raw CSS selectors.

<role>
You are a browser automation orchestrator...
</role>

<constraints>
- Keep goals small: 1-5 tool calls per step
- Reference element IDs, not text labels
</constraints>

# REMINDER: If the needed value is visible, enter and submit it. Ignore other UI.
```

### Position matters: beginning AND end

Qwen3 has strong **recency bias** — instructions closer to the generation point carry more weight. Material buried in the middle gets the least attention.

**Pattern**: Place the most critical rule at position #1 AND repeat it as the last line.

### Length: keep it short

Instruction following degrades with prompt length. The Qwen team's own advice: "shorten the message and revise the tool description to pinpoint the issue, then gradually expand it."

**Target**: Under 4,000 tokens for the system prompt. If you must go longer, use XML tags to structure sections and place critical rules at both extremes.

---

## 4. Instruction Priority and Emphasis

### The #1 workaround: reinforce in the user message

The single most impactful technique across all community sources. Qwen3 weighs user messages more heavily than system messages. When instructions in the system prompt are ignored, repeating them in the user message fixes the problem.

**For agentic use**: Inject a one-line reminder of the highest-priority rule into the dynamic per-step user message:

```
REMINDER: Check useful text lines FIRST. If the needed code/value is already present, direct the worker to enter and submit it immediately. Ignore other UI.
```

### Emphasis techniques (ranked by effectiveness)

| Technique | Effectiveness | Example |
|-----------|--------------|---------|
| **System + user reinforcement** | Highest | Same rule in system prompt AND user message |
| **Dual-position** (top + bottom of system prompt) | High | Critical rule as first AND last line |
| **XML tags with priority** | High | `<constraints priority="critical">...</constraints>` |
| **ALL CAPS keywords** | Moderate | `You MUST`, `NEVER`, `ALWAYS` |
| **Negative examples** | Moderate | Show "INCORRECT output: ..." alongside "CORRECT output: ..." |
| **Hyper-specificity** | Moderate | Replace vague intent with exact conditions and actions |
| **Numbered priority lists** | Moderate | Explicit ordering: "Rule 1 (highest priority): ..." |

### What does NOT work

- Relying on the system prompt alone for critical rules
- Lexical bans ("never use the word X") — Qwen3 routinely violates these
- Vague intent-based instructions — the model interprets ambiguity as freedom to deviate
- Single-position emphasis (top only or bottom only)

### Be hyper-specific

Qwen3 interprets flexible instructions loosely. Compare:

```
# Bad (vague)
Try the direct path first.

# Good (specific)
BEFORE considering any other action, check if the useful text lines contain the needed value
(a code, answer, password). If yes, your ONLY goal is to enter and submit that value using the
code input field. Do NOT interact with modals, radio buttons, or other UI elements.
```

---

## 5. Tool Use and Function Calling

### Reliability numbers

- Single-turn tool invocation: **69%** success
- Multi-turn tool reliance: **47%** success
- (Compare: QwQ-32B achieves 81% / 76%)

### Key rules

1. **Keep the tool list short (2-5 tools per call).** Reliability drops with more tools. If you expose many tools, consider presenting only the relevant subset per step.

2. **Do NOT use ReAct-style templates.** Qwen3 may emit stop words during reasoning that break tool call parsing. Use **Hermes-style** tool calling (which OpenRouter applies automatically via the OpenAI-compatible API).

3. **Keep tool descriptions concise and action-first.** Long, detailed tool descriptions correlate with worse tool selection.

4. **`parallel_tool_calls: False` is correct.** Qwen3 has known issues with parallel tool calls.

5. **The model sometimes skips the opening `<tool_call>` tag.** When going through OpenRouter this is handled by the provider, but if you observe malformed calls, add reinforcement: "Function calls MUST be enclosed within `<tool_call>` tags."

6. **`tool_choice='required'` is NOT supported.** Qwen3 does not accept this parameter. PydanticAI handles this correctly with `openai_supports_tool_choice_required=False`.

### When tool calling fails

The Qwen team's official debugging approach:
1. Start with a minimal system prompt and 1-2 tools
2. Verify tool calling works
3. Gradually add complexity
4. Identify the point where reliability degrades

---

## 6. Structured Output (Pydantic)

### Limitations

- `response_format` only supports `{"type": "text"}` and `{"type": "json_object"}`, NOT `{"type": "json_schema"}`.
- `openai_supports_strict_tool_definition=False` — strict schemas are disabled.
- With complex Pydantic schemas, malformed output occurs in 50-70% of cases without proper prompting.

### Best practices

1. **Schema injection is more reliable than tool-as-schema.** Rather than relying solely on PydanticAI presenting the output schema as a tool, inject the JSON schema as plain text in the prompt:
   ```
   Your response MUST be valid JSON matching this schema:
   {"action": "string", "element_id": "string", "reasoning": "string"}
   ```

2. **Keep schemas flat.** Deeply nested or recursive structures cause issues.

3. **Add explicit format constraints** in the system prompt:
   ```
   Respond only in raw JSON. No extra text or explanations.
   Do not include markdown fencing (```json).
   ```

4. **Use temperature=0.7** (not 0). While generic advice says use temperature=0 for structured output, Qwen3 specifically degrades with greedy decoding.

5. **Add retries.** "Add retries if the model occasionally emits invalid JSON" is explicitly recommended in the official docs.

### Known issue with multiple tools

When multiple tools are present (including a Pydantic output schema presented as a tool), **Qwen3 may ignore the data-class tool and revert to free-form output.** This is directly relevant to PydanticAI's approach.

---

## 7. Multi-Turn and Agentic Loops

### Context management

- **Strip old context aggressively.** Qwen3 shows attention dilution with long contexts. Only pass what's needed for the current step.
- **For the worker role**: Keep context minimal — only the goal, pruned snapshot, recent steps, and page context. Do not pass full conversation history.
- **Re-assert instructions every turn.** Qwen3 drifts across multi-turn conversations. The most recent instruction takes precedence.

### Preventing loops

Qwen3 tends to repeat the same approach rather than pivoting. Tested mitigations:

1. **Explicit anti-repetition instructions:**
   ```
   If the same action has failed twice on the same page, abandon that approach entirely.
   Try a fundamentally different strategy — not a slight variation.
   ```

2. **Structured state markers** in the prompt:
   ```
   THOUGHTS: [What I observe about current state]
   PLAN: [What I will try next and why it differs from prior attempts]
   STATUS: [in_progress | stuck | completed]
   ```

3. **Per-step success checks:** "Before each action, verify you are not repeating the exact same approach as the previous failed attempt."

4. **Oracle/health-check injection:** When stuck detection fires, inject an explicit pivot: "Your last N actions have not made progress. Abandon the current approach and try: [alternative]."

### Strategy pivoting

This is a structural weakness. The model does not naturally pivot after failures. You need **external mechanisms**:
- Stuck detection with mandatory approach change
- Oracle advisor with specific alternative recommendations
- Hard limits: after N consecutive failures on the same page, inject a "try the direct path" override

---

## 8. Known Failure Modes

| # | Failure Mode | Trigger | Workaround |
|---|-------------|---------|------------|
| 1 | **Ignores system prompt rules** | Long system prompts; competing user-message signals | Move critical rules to user message; dual-position reinforcement |
| 2 | **Gets distracted by irrelevant context** | Long page snapshots; distracting UI elements | Truncate aggressively; place important info at end of prompt |
| 3 | **Loops on same approach** | Failed action → retries with slight variation | Anti-repetition instructions; `repetition_penalty: 1.05`; structural pivot guards |
| 4 | **Overthinks explicit instructions** | Ambiguous wording; reasoning chains | Be hyper-specific; avoid ambiguity; use non-thinking variant |
| 5 | **Answers questions in content instead of following task** | Content contains a question at the end | Clearly separate task instruction from content with XML tags |
| 6 | **Structured output drift** | Complex schemas; multiple tools present | Inject schema in prompt; explicit "JSON only" constraint |
| 7 | **Infinite repetition** | Greedy decoding (temperature=0) | NEVER use temperature=0; use temp=0.7, top_p=0.8 |
| 8 | **Language mixing** | High presence_penalty; Chinese system prompt | Keep presence_penalty <= 1.5; explicit language directive |
| 9 | **Skips tool calls entirely** | System prompt > 8k tokens; many tools listed | Shorten system prompt; reduce tool count per call |
| 10 | **Violates lexical bans** | "Never use the word X" | No reliable fix; use negative examples; accept limitation |

---

## 9. Differences from GPT-4 / Claude

| Aspect | GPT-4 / Claude | Qwen3 Adjustment |
|--------|---------------|-----------------|
| System prompt adherence | Strong | Weak — reinforce in user message |
| Tool calling reliability | High (first-try) | Lower — simplify prompts, keep tool list short |
| Multi-turn stability | Stable | Drifts — re-assert instructions per turn |
| Structured output | Works with minimal guidance | Needs explicit "JSON only" + schema + example |
| Instruction precision | Tolerates ambiguity | Needs hyper-specific instructions |
| Greedy decoding | Works fine | Causes infinite loops — NEVER use |
| Prompt length tolerance | High | Degrades beyond ~4k tokens in system prompt |

---

## 10. Checklist

### Before deploying a prompt

- [ ] Critical rules appear at **top AND bottom** of system prompt
- [ ] Critical rules are **also reinforced in the user message**
- [ ] System prompt is **under 4,000 tokens**
- [ ] Tool list is **5 or fewer** tools per call
- [ ] Tool descriptions are **concise and action-first**
- [ ] `temperature=0.7, top_p=0.8` are set explicitly
- [ ] `frequency_penalty=0` (not the 0.5 default)
- [ ] No greedy decoding (`temperature` > 0)
- [ ] Instructions are **hyper-specific**, not vague/intent-based
- [ ] XML tags used for structural sections
- [ ] Structured output has explicit "JSON only" constraint + schema in prompt
- [ ] Anti-repetition instructions included for agentic loops
- [ ] Pivot mechanism exists for repeated failures

### Sources

- [Qwen3-235B-A22B-Instruct-2507 Model Card](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507)
- [Qwen3-235B-A22B Model Card](https://huggingface.co/Qwen/Qwen3-235B-A22B)
- [Qwen3 Chat Template Deep Dive (HuggingFace Blog)](https://huggingface.co/blog/qwen-3-chat-template-deep-dive)
- [Qwen3 System Prompt Discussion (HF #37)](https://huggingface.co/Qwen/Qwen3-235B-A22B/discussions/37)
- [Qwen3 Instruction Following Discussion (HF #18)](https://huggingface.co/Qwen/Qwen3-235B-A22B/discussions/18)
- [Qwen3 Tool Use Discussion (HF #20)](https://huggingface.co/Qwen/Qwen3-235B-A22B/discussions/20)
- [Qwen Function Calling Docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- [OpenRouter Reasoning Tokens Guide](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [Qwen3 Prompt Engineering for Structured Output](https://qwen3lm.com/qwen3-prompt-engineering-structured-output/)
- [Sider.ai: Qwen3-Max for Autonomous Agent Tasks](https://sider.ai/blog/ai-tools/how-to-use-qwen3-max-for-autonomous-agent-tasks-prompt-templates-that-actually-work)
- [AutoGen + Qwen3 Structured Output](https://www.dataleadsfuture.com/build-autogen-agents-with-qwen3-structured-output-thinking-mode/)
- [AI Muse: System vs User Prompts 18-Model Benchmark](https://aimuse.blog/article/2025/06/14/system-prompts-versus-user-prompts-empirical-lessons-from-an-18-model-llm-benchmark-on-hard-constraints)
- [Qwen3 Prompt Injection Analysis](https://blog.lukaszolejnik.com/prompt-injection-and-mode-drift-in-qwen3-a-security-analysis/)
- [PydanticAI Qwen3 Profile](https://ai.pydantic.dev/api/profiles/)
- [Qwen3-Coder Tool Calling Issue (#475)](https://github.com/QwenLM/Qwen3-Coder/issues/475)
- [Ollama System Prompt Issue (#10980)](https://github.com/ollama/ollama/issues/10980)
