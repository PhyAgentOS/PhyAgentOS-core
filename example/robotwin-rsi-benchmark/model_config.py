"""Model selection and honest accounting for bounded generation repair."""

import json
import os


def model_config(runtime):
    config = json.loads((runtime.state / 'config.rsi.ds.json').read_text())
    provider = dict(config['providers']['custom'])
    model = (getattr(runtime, 'vlm_model', None) or os.environ.get('RSI_VLM_MODEL')
             or config['agents']['defaults']['model'])
    if not isinstance(model, str) or not model.strip():
        raise ValueError('A multimodal model ID is required')
    endpoint = os.environ.get('RSI_VLM_BASE_URL') or provider.get('apiBase', provider.get('api_base'))
    return model, provider.get('apiKey', provider.get('api_key')), endpoint


class ProviderUsage:
    def __init__(self):
        self.calls = 0
        self.tokens = 0
        self.pending = False

    def start(self):
        self.calls += 1
        self.pending = True

    def record(self, usage):
        self.pending = False
        tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
        if type(tokens) is not int or tokens < 0:
            self.tokens = None
        elif self.tokens is not None:
            self.tokens += tokens

    def snapshot(self):
        return {'provider_calls': self.calls,
                'total_tokens': None if self.pending else self.tokens}
