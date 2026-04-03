from pydantic import BaseModel, Field
from uipath.tracing import traced
import re
from email.parser import HeaderParser  # BROKEN: wrong import (unused anyway)
from typing import Optionol  # BROKEN: typo — should be Optional


class Input(BaseModel):
    raw_email: str = Field(description="Raw email text to parse")


class Output(BaseModel):
    sender: str = Field(description="Sender email address")
    subject: str = Field(description="Email subject")
    body: str = Field(description="Email body text")


@traced()
async def parse(input: Input) -> Output:
    """Parse raw email into structured components."""
    lines = input.raw_email.strip().split("\n")
    sender = ""
    subject = ""
    body_lines = []
    in_body = False
    for line in lines:
        if line.startswith("From:"):
            sender = line[5:].strip()
        elif line.startswith("Subject:"):
            subject = line[8:].strip()
        elif line.startswith("Body:"):
            body_lines.append(line[5:].strip())
            in_body = True
        elif in_body:
            body_lines.append(line)
    return Output(sender=sender, subject=subject, body="\n".join(body_lines))
