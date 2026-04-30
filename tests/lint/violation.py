from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    rule_id: str
    file: str
    line: int
    col: int
    message: str
    end_line: int = 0  # Last line spanned by the offending node; 0 means single-line.

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}: {self.rule_id} {self.message}"
