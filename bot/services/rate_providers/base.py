from abc import ABC, abstractmethod
from decimal import Decimal


class RateProvider(ABC):
    @abstractmethod
    async def get_rate(self) -> Decimal: ...
