"""Consume AstrBot's streaming Provider contract without exposing partial verdicts."""
import asyncio


class StreamingUnavailable(NotImplementedError):
    """Provider explicitly lacks a streaming implementation; legacy calls may work."""


class StreamingCallError(RuntimeError):
    """A supported stream failed or ended without a complete response."""


async def collect_provider_stream(provider, system_prompt, prompt, image_urls=None):
    """Use text_chat_stream and accept only its final, non-chunk LLMResponse.

    AstrBot emits incremental frames followed by one complete response. Joining
    both duplicates content; accepting only partial frames can approve a message
    from truncated JSON. Consume to normal EOF and require the final response.
    """
    stream_fn = getattr(provider, "text_chat_stream", None)
    if not callable(stream_fn):
        raise StreamingUnavailable("Provider has no text_chat_stream")
    kwargs = {"system_prompt": system_prompt, "prompt": prompt}
    if image_urls is not None:
        kwargs["image_urls"] = image_urls
    stream = None
    received = False
    final_text = None
    try:
        try:
            stream = stream_fn(**kwargs)
            async for response in stream:
                received = True
                if getattr(response, "role", "assistant") == "err":
                    raise RuntimeError("Provider returned an error frame")
                if getattr(response, "is_chunk", True) is False:
                    final_text = getattr(response, "completion_text", None)
        finally:
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
    except asyncio.CancelledError:
        raise
    except NotImplementedError as exc:
        if not received:
            raise StreamingUnavailable("Provider streaming is not implemented") from exc
        raise StreamingCallError("Provider stopped after streaming began") from exc
    except Exception as exc:
        raise StreamingCallError("LLM stream failed: " + type(exc).__name__) from exc
    if not isinstance(final_text, str) or not final_text.strip():
        raise StreamingCallError("LLM stream ended without a complete text response")
    return final_text
