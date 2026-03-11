from dotenv import load_dotenv
from pydantic import BaseModel


load_dotenv()


class EchoIn(BaseModel):
    message: str
    repeat: int = 1
    prefix: str | None = None


class EchoOut(BaseModel):
    message: str


def main(input: EchoIn) -> EchoOut:
    result = []
    for _ in range(input.repeat):
        line = input.message
        if input.prefix:
            line = f"{input.prefix}: {line}"
        result.append(line)

    return EchoOut(message="\n".join(result))
