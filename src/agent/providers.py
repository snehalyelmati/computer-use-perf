# Models that support response_format={"type": "json_object"}.
# Reasoning models (qwen3, etc.) produce <think> tags that break
# Groq/Cerebras JSON validation — omit them from this set.
JSON_MODE_MODELS = {
    # Groq
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
    "moonshotai/kimi-k2-instruct-0905",
    # Cerebras
    "llama-3.3-70b",
    "llama3.1-8b",
}

# Models that support reasoning_effort parameter.
# Maps model name → default reasoning effort level.
# Models not in this dict do not support reasoning.
REASONING_MODELS = {
    "qwen/qwen3-32b": "none",  # supports reasoning but default to none
}

PROVIDER_MODELS = {
    "groq": {
        "model": "qwen/qwen3-32b",
        "oracle": "qwen/qwen3-32b",
        "action": "meta-llama/llama-4-scout-17b-16e-instruct",
        "filter": "llama-3.1-8b-instant",
    },
    "cerebras": {
        "model": "zai-glm-4.7",
        "oracle": "zai-glm-4.7",
        "action": "llama-3.3-70b",
        "filter": "llama3.1-8b",
    },
}
