from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    text: str = Field(description="Text to reverse")


class Output(BaseModel):
    reversed: str = Field(description="Reversed text")  # BUG: eval expects 'reversed_text'


@traced()
async def reverse(input: Input) -> Output:
    """Reverse the input text."""
    return Output(reversed=input.text[::-1])
