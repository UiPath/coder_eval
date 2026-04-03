from pydantic import BaseModel, Field
from uipath.tracing import traced
from uipath import UiPath  # WRONG: should be `from uipath.platform import UiPath`


class Input(BaseModel):
    name: str = Field(description="Name to greet")


class Output(BaseModel):
    greeting: str = Field(description="The greeting message")


@traced()
async def greet(input: Input) -> Output:
    """Create a greeting message."""
    return Output(greeting=f"Hello, {input.name}!")
