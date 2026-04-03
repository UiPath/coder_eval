from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    text: str = Field(description="Text to process")


class Output(BaseModel):
    result: str = Field(description="Processed result")


@traced()
async def process(input: Input) -> Output:
    """Process text by converting to uppercase."""
    return Output(result=input.text.upper())
