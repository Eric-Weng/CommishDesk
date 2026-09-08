# voices/ — narrator voice zone

This package holds the `Voice` protocol: `system_prompt: str`, `banned_topics:
frozenset[str]`, and `voice_id: str` — the system prompt and banned-topic set that shape
the LLM narrator's prose (the banned topics merge into the Layer-2 safety check, AD-12),
plus a stable identifier with no engine consumer yet. The protocol is defined in
[`__init__.py`](__init__.py) and documented, with its signature, in
[`/docs/EXTENDING.md`](../../docs/EXTENDING.md); voice evaluation material (eval prompts,
expected-tone samples, the scoring rubric) lives in `tests/eval/voices/`. This zone carries
**at most one reference implementation**, enforced by
[`tests/test_extension_zones.py`](../../tests/test_extension_zones.py) — the mild "beat
writer" default in [`beat_writer.py`](beat_writer.py), returned by `load_default_voice()`,
is that one impl, and the public repo will only ever ship the single mild default. Nothing
here prescribes how to write a voice.
