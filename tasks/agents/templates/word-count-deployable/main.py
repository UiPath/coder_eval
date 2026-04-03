from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    text: str = Field(description="Input text to count words in")


class Output(BaseModel):
    word_count: int = Field(description="Number of words in the text")


@traced()
async def word_count(input: Input) -> Output:
    """Count the number of words in the input text."""
    count = len(input.text.split())
    return Output(word_count=count)
