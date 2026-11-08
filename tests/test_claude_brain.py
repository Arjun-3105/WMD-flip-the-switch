"""ClaudeBrain without network: the request it builds, and how it reads responses and refusals."""

from types import SimpleNamespace

from mandate import negotiator


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def brain_with(monkeypatch, response):
    import anthropic

    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(response)))
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    b = negotiator.ClaudeBrain("acme-buyer", "Acme", "nimbus-seller", "target $45k")
    return b, fake.beta.messages


def test_request_shape_and_text_extraction(monkeypatch):
    resp = SimpleNamespace(stop_reason="end_turn",
                           content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="Counter.\naccept O-3")])
    brain, msgs = brain_with(monkeypatch, resp)
    out = brain.next_move(negotiator.Turn("keeper", "@acme-buyer your move", ["keeper: O-3 STAMPED"]))
    assert out == "Counter.\naccept O-3"
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "target $45k" in call["system"] and "O-3 STAMPED" in call["messages"][0]["content"]


def test_refusal_returns_no_move(monkeypatch):
    brain, _ = brain_with(monkeypatch, SimpleNamespace(stop_reason="refusal", content=[]))
    assert brain.next_move(negotiator.Turn("keeper", "go", [])) is None
