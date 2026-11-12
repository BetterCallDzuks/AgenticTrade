from typing import List

from pydantic import BaseModel, Field


class TokenAnalysisDecision(BaseModel):
    decision: str = Field(
        description="Konačna odluka agenta: 'BUY', 'WATCH', ili 'SKIP'"
    )
    risk_score: int = Field(
        description="Ocjena sigurnosti od 0 (čisti scam) do 100 (vrlo siguran)"
    )
    hype_score: int = Field(
        description="Ocjena narativa, memea, Smart Money signala i zamaha volumena od 0 do 100"
    )
    summary_hr: str = Field(
        description="Kratko i jasno obrazloženje odluke na hrvatskom jeziku (2-3 rečenice)"
    )
    red_flags: List[str] = Field(
        description="Lista uočenih opasnosti"
    )
    green_flags: List[str] = Field(
        description="Lista pozitivnih signala"
    )