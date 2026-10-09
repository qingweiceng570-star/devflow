from app.core.config import Settings
from app.integrations.llm.adapter import CompatibleModel
from app.integrations.llm.fixture import FixtureModel


def create_model(settings: Settings, source: str):
    if source == "fixture" or settings.llm_provider == "fixture":
        return FixtureModel()
    return CompatibleModel(settings)
