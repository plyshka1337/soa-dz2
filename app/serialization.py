from decimal import Decimal

from pydantic import BaseModel, field_serializer


class ContractModel(BaseModel):
    @field_serializer("*", when_used="json")
    def serialize_decimal(self, value):
        return float(value) if isinstance(value, Decimal) else value
