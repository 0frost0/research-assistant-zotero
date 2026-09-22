"""Pinned upstream compatibility shim; installed in worker and spawned child.

pdf2zh-next 2.9.0 hard-codes 100 rate-limit attempts and SDK retry defaults.
Keep its prompts/cache/token accounting, replace only request retry ownership.
No site-packages edits, no PDF layout implementation here.
"""
import threading
import json
import os


def install():
    import openai
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
    from pdf2zh_next.translator.translator_impl import openai as upstream

    original = upstream.OpenAITranslator
    if getattr(original, "bounded_by_reader", False):
        return

    class BoundedTranslator(original):
        bounded_by_reader = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.client.max_retries = 0
            self._fatal = None
            self._fatal_lock = threading.Lock()
            context = os.getenv("RESEARCH_FULL_TRANSLATION_CONTEXT")
            if context:
                from research_assistant.translation.ledger import TranslationLedger, metered_create, OUTPUT_TOKENS
                cfg = json.loads(context)
                ledger = TranslationLedger(cfg["database"])
                self.client.chat.completions.create = metered_create(
                    self.client.chat.completions.create, ledger, cfg["job_id"], cfg["attempt"])
                # Isolate older unbounded responses and endpoint identities.
                self.add_cache_impact_parameters("reader_meter_version", 1)
                self.add_cache_impact_parameters("reader_endpoint_hash", cfg["endpoint_hash"])
                self.add_cache_impact_parameters("reader_max_output", OUTPUT_TOKENS)

        def _invoke_bounded(self, method, *args, **kwargs):
            with self._fatal_lock:
                if self._fatal is not None:
                    raise self._fatal
            try:
                return method(self, *args, **kwargs)
            except openai.APIStatusError as exc:
                if exc.status_code in (400, 401, 402, 403, 404, 422):
                    # Other already in-flight calls may finish; no new requests.
                    with self._fatal_lock:
                        self._fatal = exc
                raise

    def retryable(exc):
        return isinstance(exc, (openai.APIConnectionError, openai.RateLimitError)) or (
            isinstance(exc, openai.APIStatusError) and exc.status_code >= 500
        )

    def bounded(method):
        # __wrapped__ bypasses the upstream 100-attempt decorator.
        raw = method.__wrapped__

        @retry(retry=retry_if_exception(retryable), stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=1, min=1, max=4), reraise=True)
        def call(self, *args, **kwargs):
            return self._invoke_bounded(raw, *args, **kwargs)
        return call

    BoundedTranslator.do_translate = bounded(original.do_translate)
    BoundedTranslator.do_llm_translate = bounded(original.do_llm_translate)
    upstream.OpenAITranslator = BoundedTranslator
