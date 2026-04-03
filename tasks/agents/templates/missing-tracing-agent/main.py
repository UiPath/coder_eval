from pydantic import BaseModel, Field


class Input(BaseModel):
    amount: float = Field(description="Amount in USD")
    target_currency: str = Field(description="Target currency code (EUR, GBP, JPY)")


class Output(BaseModel):
    converted_amount: float = Field(description="Converted amount")
    rate: float = Field(description="Exchange rate used")


# Missing @traced() decorator — required for UiPath monitoring
async def convert(input: Input) -> Output:
    """Convert USD to target currency using fixed rates."""
    rates = {"EUR": 0.92, "GBP": 0.79, "JPY": 149.50}
    rate = rates.get(input.target_currency, 1.0)
    converted = round(input.amount * rate, 2)
    return Output(converted_amount=converted, rate=rate)
