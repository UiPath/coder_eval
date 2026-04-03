from pydantic import BaseModel, Field
from uipath.tracing import traced


class Input(BaseModel):
    income: float = Field(description="Annual income in USD")
    filing_status: str = Field(description="'single' or 'married'")


class Output(BaseModel):
    tax_owed: float = Field(description="Federal tax owed in USD")
    effective_rate: float = Field(description="Effective tax rate as a decimal")


@traced()
async def calculate_tax(input: Input) -> Output:
    """Calculate simplified federal income tax.

    Uses 2024 IRS tax brackets (simplified to first 3 brackets).
    """
    if input.filing_status == "single":
        if input.income <= 11000:
            tax = input.income * 0.10
        elif input.income <= 40000:  # BUG: should be 44725
            tax = 1100 + (input.income - 11000) * 0.12
        else:
            tax = 4580 + (input.income - 40000) * 0.22  # BUG: base tax is wrong too
    else:  # married
        if input.income <= 22000:
            tax = input.income * 0.10
        elif input.income <= 89450:
            tax = 2200 + (input.income - 22000) * 0.12
        else:
            tax = 10294 + (input.income - 89450) * 0.22

    effective_rate = round(tax / input.income, 4) if input.income > 0 else 0.0
    return Output(tax_owed=round(tax, 2), effective_rate=effective_rate)
