"""Deterministic model boundary used by agent and HTTP tests."""

import copy

class FakeClient:
    answers = []
    calls = []

    def __init__(self, provider, model=None, recorder=None):
        self.provider, self.model, self.recorder = provider, model or "test-model", recorder

    def generate(self, instructions, context):
        self.calls.append(copy.deepcopy(context))
        if self.recorder:
            self.recorder(
                "model_call",
                {
                    "provider": self.provider,
                    "model": self.model,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        answer = self.answers.pop(0)
        return answer(context) if callable(answer) else answer

    def embed(self, texts, model):
        if self.recorder:
            self.recorder("model_call", {"provider": self.provider, "model": model, "usage": None})
        return [[1.0, 0.0] if "garden" in text.lower() else [0.0, 1.0] for text in texts]
