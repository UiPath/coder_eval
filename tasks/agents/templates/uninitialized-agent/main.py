from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    items: list[str] = Field(description="List of strings to sort")


class Output(BaseModel):
    sorted_items: list[str] = Field(description="Alphabetically sorted list")
    count: int = Field(description="Number of items")


@traced()
async def sort_items(input: Input) -> Output:
    """Sort a list of strings alphabetically."""
    sorted_list = sorted(input.items)
    return Output(sorted_items=sorted_list, count=len(sorted_list))
