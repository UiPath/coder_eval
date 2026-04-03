from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    text: str = Field(description="Text to summarize")
    max_sentences: int = Field(default=3, description="Maximum sentences in summary")


class Output(BaseModel):
    summary: str = Field(description="Summary of the input text")
    key_points: list[str] = Field(description="Key points extracted from the text")


@traced()
async def summarize(input: Input) -> Output:
    """Summarize text by extracting key sentences."""
    sentences = [s.strip() for s in input.text.replace("!", ".").replace("?", ".").split(".") if s.strip()]
    summary_sentences = sentences[: input.max_sentences]
    summary = ". ".join(summary_sentences) + "." if summary_sentences else ""
    key_points = [s for s in sentences[:5] if len(s) > 20]
    return Output(summary=summary, key_points=key_points)
