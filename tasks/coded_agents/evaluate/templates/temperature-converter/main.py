from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    celsius: float = Field(description="Temperature in Celsius to convert")


class Output(BaseModel):
    fahrenheit: float = Field(description="Temperature converted to Fahrenheit")
    kelvin: float = Field(description="Temperature converted to Kelvin")


@traced()
async def convert_temperature(input: Input) -> Output:
    """Convert Celsius temperature to Fahrenheit and Kelvin."""
    fahrenheit = (input.celsius * 9 / 5) + 32
    kelvin = input.celsius + 273.15
    return Output(fahrenheit=fahrenheit, kelvin=kelvin)
