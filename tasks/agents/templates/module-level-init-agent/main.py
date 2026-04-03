from pydantic import BaseModel, Field
from uipath.tracing import traced
from uipath.platform import UiPath

# ANTI-PATTERN: module-level instantiation fails during `uipath init`
# because auth hasn't been set up when the module is imported for introspection.
sdk = UiPath()


class Input(BaseModel):
    asset_name: str = Field(description="Name of the UiPath asset to retrieve")


class Output(BaseModel):
    value: str = Field(description="Asset value as string")


@traced()
async def get_asset(input: Input) -> Output:
    """Retrieve a UiPath asset value by name."""
    asset = sdk.assets.retrieve(name=input.asset_name)
    return Output(value=str(asset.value))
